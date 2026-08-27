"""BFF ingress adapter for the Strategy Hermes-owned research lab.

This module admits and reads request manifests; it is not the Strategy Hermes
researcher and it is not a Research HQ execution surface. The direct Hermes
worker owns hypothesis, code, backtest, result and lineage writes after intake.
It may create one blocked, tracking-only Kanban root for observability; that
root is never an execution parent and never dispatches a second researcher.
"""

from __future__ import annotations

import os
import logging
from pathlib import Path
import sys
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parents[2]
_AUTONOMOUS_DIR = _ROOT / "departments" / "01-research" / "autonomous"
if str(_AUTONOMOUS_DIR) not in sys.path:
    sys.path.insert(0, str(_AUTONOMOUS_DIR))

from autonomous_research_ingress import (  # noqa: E402
    ResearchIntake,
    ResearchRequestConflict,
    looks_like_strategy_research,
)
from orchestration.ceo_workflow_scope import build_root_body  # noqa: E402
from orchestration.canonical_profiles import canonical_profile_for_department  # noqa: E402
try:
    from . import hermes_boundary  # noqa: E402
except ImportError:  # pragma: no cover
    import hermes_boundary  # type: ignore[no-redef]

try:
    from .current_user import optional_current_user
except ImportError:  # pragma: no cover
    from current_user import optional_current_user  # type: ignore[no-redef]


router = APIRouter(prefix="/ui/strategy-research", tags=["autonomous-strategy-research"])
_LOGGER = logging.getLogger("strategy-research-intake")


def _lab_root() -> Path:
    return Path(os.getenv("AUTONOMOUS_RESEARCH_LAB_ROOT", "/var/lib/autonomous-research"))


class StrategyResearchAsk(BaseModel):
    query: str = Field(min_length=8, max_length=4000)
    request_id: str | None = Field(default=None, min_length=8, max_length=128)
    universe: str = Field(default="unspecified", min_length=1, max_length=500)
    horizon: str = Field(default="unspecified", min_length=1, max_length=500)
    constraints: list[str] = Field(default_factory=list, max_length=20)


class StrategyResearchAccepted(BaseModel):
    schema_version: str = "autonomous-research-request.v1"
    accepted: bool = True
    duplicate: bool = False
    request_id: str
    lab_id: str
    status: Literal["QUEUED", "RESEARCHING", "BLOCKED", "CANDIDATE"] = "QUEUED"
    message: str
    status_url: str
    kanban_root_task_id: str | None = None
    kanban_tracking_status: Literal["CREATED", "UNAVAILABLE"] = "UNAVAILABLE"


class StrategyResearchStatus(BaseModel):
    schema_version: str = "autonomous-research-status.v1"
    request_id: str
    lab_id: str
    goal: str
    universe: str
    horizon: str
    status: Literal["QUEUED", "RESEARCHING", "BLOCKED", "CANDIDATE"]
    cycle: int
    last_action: str | None = None
    active_plan_id: str | None = None
    plan_count: int = 0
    result_count: int = 0
    candidate_available: bool = False
    updated_at: str
    error: str | None = None
    kanban_root_task_id: str | None = None
    kanban_tracking_status: Literal["CREATED", "UNAVAILABLE"] = "UNAVAILABLE"


class StrategyPromotionAsk(BaseModel):
    mode: Literal["shadow", "paper", "live"] = "paper"
    confirm: bool = False
    override_blocked: bool = False


class StrategyPromotionAccepted(BaseModel):
    schema_version: str = "autonomous-strategy-promotion.v1"
    request_id: str
    promotion_id: str
    mode: Literal["shadow", "paper", "live"]
    status: Literal["REQUESTED", "REVIEW_REQUIRED", "BLOCKED"]
    message: str


def _owner(actor: str | None) -> str:
    return str(actor or "anonymous").strip() or "anonymous"


def _request_payload(request: StrategyResearchAsk, actor: str | None) -> dict[str, Any]:
    return {
        "request_id": request.request_id or uuid4().hex,
        "goal": request.query,
        "universe": request.universe,
        "horizon": request.horizon,
        "constraints": request.constraints,
        "actor_id": _owner(actor),
        "source": "web",
    }


def _ensure_tracking_root(
    *, payload: dict[str, Any], intake: ResearchIntake, request_id: str
) -> tuple[str | None, str]:
    """Create a blocked Kanban tracking root without creating an execution task."""

    existing = str(payload.get("kanban_root_task_id") or "").strip()
    if existing:
        return existing, "CREATED"
    root_body = build_root_body(
        str(payload["goal"]),
        request_id,
        workflow_mode="analysis",
        source=str(payload.get("source") or "web"),
        requested_by=str(payload.get("actor_id") or "anonymous"),
        discord_channel_id=payload.get("discord_channel_id"),
        discord_message_id=payload.get("discord_message_id"),
        discord_guild_id=payload.get("discord_guild_id"),
        discord_thread_id=payload.get("discord_thread_id"),
        qa_enabled=False,
        qa_blocks_response=False,
    )
    root_body = "\n".join(
        (
            root_body,
            "strategy-research-tracking.v1",
            "strategy_research_tracking_only=true",
            f"strategy_request_id={request_id}",
            "strategy_execution_owner=strategy-hermes",
            "strategy_execution_parent=none",
        )
    )
    try:
        root = hermes_boundary.create_kanban_task(
            assignee=canonical_profile_for_department("ceo"),
            title=f"Strategy Hermes 추적: {str(payload['goal'])[:120]}",
            body=root_body,
            idempotency_key=f"strategy-research-root:{request_id}",
            initial_status="blocked",
        )
        root_id = str((root or {}).get("task_id") or "").strip()
        if not root_id:
            _LOGGER.error(
                "strategy-research tracking root unavailable request_id=%s", request_id
            )
            return None, "UNAVAILABLE"
        intake.bind_kanban_root(request_id, root_id)
        return root_id, "CREATED"
    except Exception:  # noqa: BLE001 - tracking failure must not drop research intake
        _LOGGER.exception(
            "strategy-research tracking root create failed request_id=%s", request_id
        )
        return None, "UNAVAILABLE"


def accept_strategy_research_query(
    *,
    query: str,
    request_id: str | None = None,
    actor_id: str | None = None,
    source: str = "web",
    universe: str = "unspecified",
    horizon: str = "unspecified",
    constraints: list[str] | None = None,
    source_message_id: str | None = None,
    discord_channel_id: str | None = None,
    discord_message_id: str | None = None,
    discord_guild_id: str | None = None,
    discord_thread_id: str | None = None,
) -> StrategyResearchAccepted:
    """Admit one strategy objective and create its tracking-only root.

    Both the dedicated strategy endpoint and the central CEO/Discord router use
    this function so they cannot drift into separate intake contracts. Kanban
    creation is idempotent and best-effort: research intake remains durable if
    the tracking board is temporarily unavailable.
    """

    request = StrategyResearchAsk(
        query=query,
        request_id=request_id,
        universe=universe,
        horizon=horizon,
        constraints=constraints or [],
    )
    intake = ResearchIntake(_lab_root())
    try:
        payload, created = intake.submit(
            {
                **_request_payload(request, actor_id),
                "source": source,
                "source_message_id": source_message_id,
                "discord_channel_id": discord_channel_id,
                "discord_message_id": discord_message_id,
                "discord_guild_id": discord_guild_id,
                "discord_thread_id": discord_thread_id,
            }
        )
    except ResearchRequestConflict as exc:
        raise HTTPException(status_code=409, detail="strategy_research_request_conflict") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    admitted_id = str(payload["request_id"])
    root_id, tracking_status = _ensure_tracking_root(
        payload=payload, intake=intake, request_id=admitted_id
    )
    current = intake.status(admitted_id)
    current_status = str((current or {}).get("status") or "QUEUED")
    return StrategyResearchAccepted(
        duplicate=not created,
        request_id=admitted_id,
        lab_id=admitted_id,
        status=current_status,  # type: ignore[arg-type]
        message=(
            "자율 전략 연구실에 목표를 등록했습니다. Hermes가 연구실을 생성하고 "
            "가설·실험·검증을 반복합니다."
        ),
        status_url=f"/ui/strategy-research/requests/{admitted_id}",
        kanban_root_task_id=root_id,
        kanban_tracking_status=tracking_status,
    )


@router.post("/ask", response_model=StrategyResearchAccepted, status_code=202)
def strategy_research_ask(
    request: StrategyResearchAsk,
    owner_id: str | None = Depends(optional_current_user),
) -> StrategyResearchAccepted:
    return accept_strategy_research_query(
        query=request.query,
        request_id=request.request_id,
        actor_id=owner_id,
        source="web",
        universe=request.universe,
        horizon=request.horizon,
        constraints=request.constraints,
    )


@router.get("/requests/{request_id}", response_model=StrategyResearchStatus)
def strategy_research_status(
    request_id: str,
    owner_id: str | None = Depends(optional_current_user),
) -> StrategyResearchStatus:
    intake = ResearchIntake(_lab_root())
    try:
        status = intake.status(request_id)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="strategy_research_request_not_found") from exc
    if status is None:
        raise HTTPException(status_code=404, detail="strategy_research_request_not_found")
    if owner_id and status.get("actor_id") != owner_id:
        raise HTTPException(status_code=403, detail="strategy_research_request_forbidden")
    status.pop("actor_id", None)
    status["kanban_tracking_status"] = (
        "CREATED" if status.get("kanban_root_task_id") else "UNAVAILABLE"
    )
    return StrategyResearchStatus.model_validate(status)


@router.post("/requests/{request_id}/promote", response_model=StrategyPromotionAccepted, status_code=202)
def strategy_research_promote(
    request_id: str,
    request: StrategyPromotionAsk,
    owner_id: str | None = Depends(optional_current_user),
) -> StrategyPromotionAccepted:
    """Record an explicit promotion request without letting research self-deploy.

    Candidate artifacts may enter the existing release workflow in shadow or
    paper mode. A BLOCKED result can be explicitly escalated for human review,
    but this endpoint never converts missing evidence into a live strategy and
    never places an order. Live deployment requires the separate release/risk
    authority, so it is durably recorded as BLOCKED here.
    """

    if not owner_id:
        raise HTTPException(status_code=401, detail="strategy_promotion_authentication_required")
    intake = ResearchIntake(_lab_root())
    status = intake.status(request_id)
    if status is None:
        raise HTTPException(status_code=404, detail="strategy_research_request_not_found")
    if status.get("actor_id") != owner_id:
        raise HTTPException(status_code=403, detail="strategy_research_request_forbidden")
    lab_path = intake.lab_path(request_id)
    candidate = lab_path / "candidate.json"
    current_status = str(status.get("status") or "").upper()
    if current_status == "BLOCKED" and request.override_blocked and request.confirm:
        promotion_status: Literal["REQUESTED", "REVIEW_REQUIRED", "BLOCKED"] = "REVIEW_REQUIRED"
        message = "BLOCKED 결과를 강제 배포하지 않고, 별도 인간·Risk 검토 요청으로 등록했습니다."
    elif not candidate.exists():
        promotion_status = "BLOCKED"
        message = "후보 아티팩트가 없어 승격 요청을 실행할 수 없습니다."
    elif not request.confirm:
        promotion_status = "BLOCKED"
        message = "명시적 confirm=true가 없어 승격 요청을 거부했습니다."
    elif request.mode == "live":
        promotion_status = "BLOCKED"
        message = "LIVE 배포는 이 연구 API의 권한 범위가 아닙니다. QA·Risk·사람 승인을 거쳐야 합니다."
    else:
        promotion_status = "REQUESTED"
        message = f"{request.mode.upper()} 승격 요청을 release gate로 전달할 준비가 됐습니다."
    promotion_id = f"promotion-{uuid4().hex}"
    path = lab_path / "promotion-requests" / f"{promotion_id}.json"
    intake._write_json(path, {
        "schema": "autonomous-strategy-promotion.v1",
        "promotion_id": promotion_id,
        "request_id": request_id,
        "requested_by": owner_id,
        "mode": request.mode,
        "confirm": request.confirm,
        "override_blocked": request.override_blocked,
        "research_status": current_status,
        "candidate_path": str(candidate) if candidate.exists() else None,
        "status": promotion_status,
        "message": message,
    })
    return StrategyPromotionAccepted(
        request_id=request_id,
        promotion_id=promotion_id,
        mode=request.mode,
        status=promotion_status,
        message=message,
    )


__all__ = ["accept_strategy_research_query", "looks_like_strategy_research", "router"]
