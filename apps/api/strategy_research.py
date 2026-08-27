"""BFF ingress adapter for the Strategy Hermes-owned research lab.

This module admits and reads request manifests; it is not the Strategy Hermes
researcher and it is not a Research HQ execution surface. The direct Hermes
worker owns hypothesis, code, backtest, result and lineage writes after intake.
"""

from __future__ import annotations

import os
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

try:
    from .current_user import optional_current_user
except ImportError:  # pragma: no cover
    from current_user import optional_current_user  # type: ignore[no-redef]


router = APIRouter(prefix="/ui/strategy-research", tags=["autonomous-strategy-research"])


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
    """Admit one strategy objective without touching CEO/Kanban state.

    Both the dedicated strategy endpoint and the central CEO/Discord router use
    this function so they cannot drift into separate intake contracts.
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
    return StrategyResearchStatus.model_validate(status)


__all__ = ["accept_strategy_research_query", "looks_like_strategy_research", "router"]
