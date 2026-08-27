"""BFF ingress adapter for the Strategy Hermes-owned research lab.

This module admits and reads request manifests; it is not the Strategy Hermes
researcher and it is not a Research HQ execution surface. The direct Hermes
worker owns hypothesis, code, backtest, result and lineage writes after intake.
It may create one blocked, tracking-only Kanban root for observability; that
root is never an execution parent and never dispatches a second researcher.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parents[2]
_AUTONOMOUS_DIR = _ROOT / "departments" / "01-research" / "autonomous"
if str(_AUTONOMOUS_DIR) not in sys.path:
    sys.path.insert(0, str(_AUTONOMOUS_DIR))

from autonomous_research_ingress import (
    ResearchIntake,
    ResearchRequestConflict,
    looks_like_strategy_research,
)
from models import utc_now

from orchestration.canonical_profiles import (
    canonical_profile_for_department,
)
from orchestration.ceo_workflow_scope import build_root_body

try:
    from . import hermes_boundary
except ImportError:  # pragma: no cover
    import hermes_boundary  # type: ignore[no-redef]

try:
    from .current_user import optional_current_user
except ImportError:  # pragma: no cover
    from current_user import optional_current_user  # type: ignore[no-redef]
try:
    from .strategy_runtime_client import strategy_runtime_request_sync
except ImportError:  # pragma: no cover
    from strategy_runtime_client import (
        strategy_runtime_request_sync,  # type: ignore[no-redef]
    )


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
    status: Literal["QUEUED", "RESEARCHING", "COMPLETED", "BLOCKED", "CANDIDATE"] = "QUEUED"
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
    status: Literal["QUEUED", "RESEARCHING", "COMPLETED", "BLOCKED", "CANDIDATE"]
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
    latest_report: str | None = None
    latest_result: dict[str, Any] | None = None
    deployment_count: int = 0
    deployments: list[dict[str, Any]] = Field(default_factory=list)


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


class StrategyDeploymentAsk(BaseModel):
    """Human release request for one already-tested, exact stock scope.

    The request carries no code or arbitrary artifact path. The server resolves
    both from the immutable research lab identified by the URL, then records a
    content-addressed handoff for the downstream QA/Risk release gate.
    """

    mode: Literal["shadow", "paper", "live"] = "paper"
    symbols: list[str] = Field(min_length=1, max_length=20)
    confirm: bool = False
    reason: str = Field(min_length=4, max_length=500)


class StrategyDeploymentAccepted(BaseModel):
    schema_version: str = "autonomous-strategy-deployment.v1"
    request_id: str
    deployment_id: str
    mode: Literal["shadow", "paper", "live"]
    symbols: list[str]
    status: Literal[
        "AWAITING_APPROVAL", "REQUESTED", "REVIEW_REQUIRED", "BLOCKED",
        "APPROVED", "DEPLOYING", "ACTIVE", "PAUSED", "FAILED", "REMOVED"
    ]
    research_status: str
    plan_id: str | None = None
    result_hash: str | None = None
    approval_required: bool = True
    override_review_required: bool = False
    approved_by: str | None = None
    bundle_hash: str | None = None
    runtime_status: str = "NOT_STARTED"
    execution_status: str = "NOT_STARTED"
    container_name: str | None = None
    container_id: str | None = None
    runtime_detail: dict[str, Any] = Field(default_factory=dict)
    backtest_summary: dict[str, Any] = Field(default_factory=dict)
    message: str


class StrategyDeploymentList(BaseModel):
    schema_version: str = "autonomous-strategy-deployments.v1"
    request_id: str
    deployments: list[StrategyDeploymentAccepted] = Field(default_factory=list)


class StrategyDeploymentApprovalAsk(BaseModel):
    """Explicit human confirmation for a previously reviewed request.

    ``override_review_required`` is a separate, auditable authority path. It
    is never inferred from ``confirm`` and is only accepted for a configured
    top-level human approver.
    """

    confirm: bool = False
    override_review_required: bool = False
    reason: str = Field(min_length=4, max_length=500)


class StrategyDeploymentPowerAsk(BaseModel):
    """Start or stop one exact PAPER deployment container."""

    action: Literal["start", "stop"]
    reason: str = Field(min_length=4, max_length=500)


class StrategyDeploymentRemoveAsk(BaseModel):
    """Retire one deployment while retaining all research evidence."""

    confirm: bool = False
    reason: str = Field(min_length=4, max_length=500)


def _owner(actor: str | None) -> str:
    return str(actor or "anonymous").strip() or "anonymous"


def _configured_user_ids(env_name: str) -> set[str]:
    raw = os.getenv(env_name, "")
    return {value for value in re.split(r"[\s,]+", raw.strip()) if value}


def _require_top_level_human_approver(owner_id: str) -> None:
    """Authorize the exceptional PAPER release path with an explicit allowlist."""

    if owner_id not in _configured_user_ids("STRATEGY_TOP_LEVEL_APPROVER_USER_IDS"):
        raise HTTPException(
            status_code=403,
            detail="strategy_deployment_top_level_approver_required",
        )


_KRX_SYMBOL_RE = re.compile(r"^\d{6}$")
_DEPLOYMENT_SYMBOL_ALIASES = {
    "하이닉스": "000660",
    "sk하이닉스": "000660",
    "sk hynix": "000660",
    "삼성전자": "005930",
}


def _normalize_deployment_symbols(symbols: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in symbols:
        value = str(raw or "").strip().casefold()
        value = _DEPLOYMENT_SYMBOL_ALIASES.get(value, value)
        if not _KRX_SYMBOL_RE.fullmatch(value):
            raise HTTPException(
                status_code=422,
                detail="strategy_deployment_symbols_must_be_six_digit_krx_codes",
            )
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise HTTPException(status_code=422, detail="strategy_deployment_symbols_required")
    return normalized


def _deployment_paths(intake: ResearchIntake, request_id: str) -> list[Path]:
    lab_path = intake.lab_path(request_id)
    return sorted((lab_path / "deployments").glob("*.json")) if lab_path.exists() else []


def _deployment_records(intake: ResearchIntake, request_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in _deployment_paths(intake, request_id):
        try:
            record = intake._read_json(path)
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _latest_result(intake: ResearchIntake, request_id: str) -> tuple[Path, dict[str, Any]] | None:
    lab_path = intake.lab_path(request_id)
    result_paths = sorted((lab_path / "results").glob("*.json")) if lab_path.exists() else []
    for path in reversed(result_paths):
        try:
            return path, intake._read_json(path)
        except (OSError, ValueError):
            continue
    return None


def _file_hash(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        # A worker may be atomically replacing an artifact. Treat that as
        # unavailable evidence rather than creating a handoff without a hash.
        return None


def _latest_plan(intake: ResearchIntake, request_id: str, plan_id: str | None) -> dict[str, Any]:
    if not plan_id or Path(plan_id).name != plan_id:
        return {}
    path = intake.lab_path(request_id) / "plans" / f"{plan_id}.json"
    try:
        return intake._read_json(path) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def _latest_lab_decision(intake: ResearchIntake, request_id: str) -> str | None:
    events_path = intake.lab_path(request_id) / "events.jsonl"
    if not events_path.exists():
        return None
    decision: str | None = None
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("event_type") != "DECISION":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict) and str(payload.get("decision") or "").strip():
            decision = str(payload["decision"]).strip().upper()
    return decision


def _strategy_signature_is_supported(signature: dict[str, Any]) -> bool:
    """Accept only the fixed 3-minute SMA strategy signature.

    Older test fixtures used separate ``timeframe``/``fast``/``mid``/``slow``
    fields. Production Strategy Hermes plans register the same contract as one
    pipe-delimited signature string, so both representations are normalized
    here without widening the allowlist.
    """

    normalized = {
        str(key).casefold(): str(value).casefold().strip()
        for key, value in signature.items()
    }
    if (
        normalized.get("timeframe") in {"3m", "3min", "3-minute"}
        and normalized.get("fast") == "5"
        and normalized.get("mid") == "20"
        and normalized.get("slow") == "60"
    ):
        return True

    raw = normalized.get("signature", "")
    if not raw:
        return False
    tokens = {part.strip() for part in raw.split("|") if part.strip()}
    return {
        "sma_alignment_v1",
        "3m",
        "sma=5,20,60",
        "target=2pct",
        "next_open_plus_one_bar",
    }.issubset(tokens)


def _strategy_signature_metadata(plan: dict[str, Any]) -> dict[str, Any]:
    """Preserve the registered signature regardless of its JSON shape."""

    raw_signature = plan.get("signature")
    if isinstance(raw_signature, dict):
        return dict(raw_signature)
    if isinstance(raw_signature, str):
        return {"signature": raw_signature}
    return {}


def _deployment_response(record: dict[str, Any]) -> StrategyDeploymentAccepted:
    return StrategyDeploymentAccepted(
        request_id=str(record["request_id"]),
        deployment_id=str(record["deployment_id"]),
        mode=str(record["mode"]),
        symbols=list(record.get("symbols") or []),
        status=str(record["status"]),
        research_status=str(record.get("research_status") or "UNKNOWN"),
        plan_id=record.get("plan_id"),
        result_hash=record.get("result_hash"),
        approval_required=bool(record.get("approval_required", True)),
        override_review_required=bool(record.get("override_review_required", False)),
        approved_by=record.get("approved_by"),
        bundle_hash=record.get("bundle_hash"),
        runtime_status=str(record.get("runtime_status") or "NOT_STARTED"),
        execution_status=str(record.get("execution_status") or "NOT_STARTED"),
        container_name=record.get("container_name"),
        container_id=record.get("container_id"),
        runtime_detail=(
            dict(record.get("runtime_detail"))
            if isinstance(record.get("runtime_detail"), dict)
            else {}
        ),
        backtest_summary=(
            dict(record.get("backtest_summary"))
            if isinstance(record.get("backtest_summary"), dict)
            else {}
        ),
        message=str(record.get("message") or ""),
    )


def _backtest_summary(
    result: dict[str, Any], *, symbols: list[str], decision: str | None
) -> dict[str, Any]:
    """Build the small human-facing review card from recorded facts only.

    The approval screen must not make a new performance calculation. It gets a
    bounded projection of the immutable result and clearly marks missing values
    as unknown rather than turning them into zeroes.
    """

    metrics = result.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    by_symbol = metrics.get("by_symbol")
    by_symbol = by_symbol if isinstance(by_symbol, dict) else {}
    aggregate = metrics.get("aggregate")
    aggregate = aggregate if isinstance(aggregate, dict) else {}

    def pick(*names: str) -> Any:
        for source in (aggregate, metrics, result):
            if not isinstance(source, dict):
                continue
            for name in names:
                if source.get(name) is not None:
                    return source.get(name)
        return None

    return {
        "result_status": str(result.get("status") or "UNKNOWN").upper(),
        "decision": decision,
        "symbols": list(symbols),
        "period": pick("period", "date_range", "tested_period", "scope"),
        "timeframe": pick("timeframe", "bar_interval", "interval"),
        "trade_count": pick("trade_count", "trades", "total_trades"),
        "return_pct": pick("return_pct", "total_return_pct", "net_return_pct", "return"),
        "compound_return_pct": pick("compound_return_pct", "cagr_pct", "compound_return"),
        "win_rate_pct": pick("win_rate_pct", "win_rate", "hit_rate_pct"),
        "mdd_pct": pick("mdd_pct", "max_drawdown_pct", "drawdown_pct"),
        "cost_model": pick("cost_model", "cost_model_version"),
        "per_symbol": {
            symbol: by_symbol.get(symbol, {})
            for symbol in symbols
            if isinstance(by_symbol.get(symbol, {}), dict)
        },
    }


def _backtest_summary_text(summary: dict[str, Any]) -> str:
    """Render a compact report for Discord/Web without inventing metrics."""

    def shown(value: Any) -> str:
        return "미확인" if value is None or value == "" else str(value)

    return (
        "백테스트 요약\n"
        f"- 유니버스: {', '.join(summary.get('symbols') or ())}\n"
        f"- 기간: {shown(summary.get('period'))}\n"
        f"- 타임프레임: {shown(summary.get('timeframe'))}\n"
        f"- 거래: {shown(summary.get('trade_count'))}회 · 승률: {shown(summary.get('win_rate_pct'))}\n"
        f"- 수익률: {shown(summary.get('return_pct'))} · 복리: {shown(summary.get('compound_return_pct'))}\n"
        f"- MDD: {shown(summary.get('mdd_pct'))} · 비용모델: {shown(summary.get('cost_model'))}\n"
        f"- 최종판정: {shown(summary.get('decision'))}"
    )


def _deployment_record_path(intake: ResearchIntake, request_id: str, deployment_id: str) -> Path:
    if not re.fullmatch(r"deployment-[0-9a-f]{24}", deployment_id):
        raise HTTPException(status_code=422, detail="strategy_deployment_id_invalid")
    path = intake.lab_path(request_id) / "deployments" / f"{deployment_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="strategy_deployment_not_found")
    return path


def _strategy_bundle_from_record(
    *,
    intake: ResearchIntake,
    record: dict[str, Any],
    owner_id: str,
    allow_human_override: bool = False,
    override_reason: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """Create the only runtime program this release surface is allowed to run.

    Hermes can propose research text, but it cannot smuggle executable Python
    or an arbitrary image through the release request. The first supported
    runtime is the explicitly requested 3-minute SMA 5/20/60 alignment with a
    2% take-profit. Unsupported signatures remain review-only.
    """

    if record.get("mode") != "paper":
        raise HTTPException(status_code=409, detail="strategy_deployment_paper_only")
    record_status = str(record.get("status") or "").upper()
    if record_status != "AWAITING_APPROVAL" and not (
        allow_human_override and record_status in {"REVIEW_REQUIRED", "FAILED"}
    ):
        raise HTTPException(status_code=409, detail="strategy_deployment_not_awaiting_approval")
    if record.get("requested_by") != owner_id and not allow_human_override:
        raise HTTPException(status_code=403, detail="strategy_research_request_forbidden")

    request_id = str(record["request_id"])
    status = _research_status(intake, request_id)
    research_status = str(status.get("status") or "").upper()
    if research_status != "CANDIDATE" and not (
        allow_human_override and research_status == "COMPLETED"
    ):
        raise HTTPException(status_code=409, detail="strategy_deployment_candidate_required")
    decision = str(record.get("decision") or "").upper()
    if decision in {"PIVOT", "PAUSE", "REVIEW"} and not allow_human_override:
        raise HTTPException(status_code=409, detail="strategy_deployment_release_decision_not_approved")

    latest = _latest_result(intake, request_id)
    if latest is None:
        raise HTTPException(status_code=409, detail="strategy_deployment_result_missing")
    result_path, result = latest
    current_hash = _file_hash(result_path)
    if not current_hash or current_hash != record.get("result_hash"):
        raise HTTPException(status_code=409, detail="strategy_deployment_result_changed")
    if str(result.get("status") or "").upper() != "COMPLETED":
        raise HTTPException(status_code=409, detail="strategy_deployment_result_not_completed")

    plan = _latest_plan(intake, request_id, str(record.get("plan_id") or ""))
    signature = _strategy_signature_metadata(plan)
    if not _strategy_signature_is_supported(signature):
        raise HTTPException(status_code=409, detail="strategy_deployment_signature_not_supported")

    goal = str(status.get("goal") or "")
    if not re.search(r"(?<!\d)2\s*%", goal):
        raise HTTPException(status_code=409, detail="strategy_deployment_take_profit_not_explicit")
    candidate_path = intake.lab_path(request_id) / "candidate.json"
    candidate_hash = _file_hash(candidate_path)
    if not allow_human_override and (
        not candidate_hash or candidate_hash != record.get("candidate_hash")
    ):
        raise HTTPException(status_code=409, detail="strategy_deployment_candidate_changed")

    bundle = {
        "schema": "autonomous-strategy-paper-bundle.v1",
        "bundle_version": "sma-alignment-3m-v1",
        "deployment_id": record["deployment_id"],
        "request_id": request_id,
        "approved_by": owner_id,
        "approved_at": utc_now(),
        "approval_type": (
            "HUMAN_TOP_LEVEL_OVERRIDE"
            if allow_human_override
            else "HUMAN_STANDARD_APPROVAL"
        ),
        "override_review_required": allow_human_override,
        "override_reason": override_reason if allow_human_override else None,
        "source_result_hash": current_hash,
        "source_plan_id": record.get("plan_id"),
        "symbols": list(record.get("symbols") or []),
        "mode": "PAPER",
        "strategy": {
            "kind": "SMA_ALIGNMENT",
            "timeframe": "3M",
            "fast": 5,
            "mid": 20,
            "slow": 60,
            "entry": "CLOSE_GT_SMA5_GT_SMA20_GT_SMA60",
            "take_profit_pct": "0.02",
            "entry_execution": "NEXT_BAR_OPEN",
            "exit_execution": "NEXT_BAR_OPEN",
        },
        # This runtime deliberately produces auditable PAPER signals only. It
        # has no broker credential and cannot submit an order by itself.
        "execution": {
            "orders_enabled": False,
            "signal_only": True,
            "trading_route": "StrategySignal -> Trading/Risk/OMS (not wired by this bundle)",
        },
    }
    bundle_dir = intake.lab_path(request_id) / "deployments" / "bundles"
    bundle_path = bundle_dir / f"{record['deployment_id']}.json"
    with intake._locked():
        intake._write_json(bundle_path, bundle, mode=0o644)
    return bundle, bundle_path


def _start_strategy_paper_container(record: dict[str, Any], *, bundle_path: Path) -> dict[str, Any]:
    """Ask the private runtime-control sidecar to launch the fixed executor."""

    payload = strategy_runtime_request_sync(
        "POST",
        "/deploy",
        body={
            "deployment_id": record["deployment_id"],
            "request_id": record["request_id"],
            "bundle_path": str(bundle_path),
            "bundle_hash": record.get("bundle_hash"),
        },
    )
    return payload if isinstance(payload, dict) else {}


def _strategy_runtime_command(
    *, path: str, payload: dict[str, Any], timeout_seconds: float = 20.0
) -> dict[str, Any]:
    """Call one private runtime-control command and preserve its error boundary."""

    value = strategy_runtime_request_sync(
        "POST", path, body=payload, timeout_seconds=timeout_seconds
    )
    return value if isinstance(value, dict) else {}


def _strategy_runtime_snapshot(*, deployment_id: str) -> dict[str, Any]:
    """Read live state from the private runtime-control sidecar."""

    value = strategy_runtime_request_sync(
        "GET", f"/deployments/{deployment_id}", timeout_seconds=10.0
    )
    return value if isinstance(value, dict) else {}


def _research_status(intake: ResearchIntake, request_id: str) -> dict[str, Any]:
    try:
        status = intake.status(request_id)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="strategy_research_request_not_found") from exc
    if status is None:
        raise HTTPException(status_code=404, detail="strategy_research_request_not_found")
    return status


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
    except Exception:
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
    latest_result = None
    lab_path = intake.lab_path(request_id)
    result_paths = sorted((lab_path / "results").glob("*.json")) if lab_path.exists() else []
    if result_paths:
        try:
            latest_result = intake._read_json(result_paths[-1])
        except (OSError, ValueError):
            latest_result = None
    status["latest_result"] = latest_result
    deployment_records = _deployment_records(intake, request_id)
    status["deployment_count"] = len(deployment_records)
    status["deployments"] = deployment_records
    if latest_result:
        try:
            from .strategy_research_discord_notifier import (
                _aggregate_report_content,
                _events,
                _lab_is_final,
                _report_content,
            )
            events = _events(lab_path / "events.jsonl")
            result_objects = []
            for result_path in result_paths:
                try:
                    result = intake._read_json(result_path)
                except (OSError, ValueError):
                    continue
                result_objects.append(result)
            result_ids = {path.stem for path in result_paths}
            report_request = {**status, "_lab_path": str(lab_path)}
            if _lab_is_final(lab_path, events=events, result_ids=result_ids):
                status["latest_report"] = _aggregate_report_content(
                    report_request,
                    result_objects,
                    events=events,
                    lab_id=request_id,
                    lab_path=lab_path,
                )
            else:
                status["latest_report"] = _report_content(
                    report_request,
                    latest_result,
                    events=events,
                    lab_id=request_id,
                )
        except (ImportError, OSError, ValueError, TypeError):
            status["latest_report"] = None
    status["kanban_tracking_status"] = (
        "CREATED" if status.get("kanban_root_task_id") else "UNAVAILABLE"
    )
    return StrategyResearchStatus.model_validate(status)


def request_strategy_deployment(
    request_id: str,
    request: StrategyDeploymentAsk,
    owner_id: str | None,
) -> StrategyDeploymentAccepted:
    """Record a human release handoff for an immutable research result.

    This is deliberately the report/release-request boundary, not an approval
    or broker control path. A separate explicit approval revalidates the
    immutable evidence before creating the allowlisted PAPER signal Bundle.
    """

    if not owner_id:
        raise HTTPException(status_code=401, detail="strategy_deployment_authentication_required")
    intake = ResearchIntake(_lab_root())
    status = _research_status(intake, request_id)
    if status.get("actor_id") != owner_id:
        raise HTTPException(status_code=403, detail="strategy_research_request_forbidden")

    symbols = _normalize_deployment_symbols(request.symbols)
    latest = _latest_result(intake, request_id)
    result_path, result = latest if latest else (None, {})
    result_hash = _file_hash(result_path)
    plan_id = str(result.get("plan_id") or "").strip() or None
    plan = _latest_plan(intake, request_id, plan_id)
    candidate_path = intake.lab_path(request_id) / "candidate.json"
    candidate_hash = _file_hash(candidate_path if candidate_path.exists() else None)

    # The first command is a request, not the approval itself. ``confirm`` is
    # retained in the wire contract for backwards compatibility, but it never
    # starts a container from this endpoint.
    deployment_key = hashlib.sha256(
        json.dumps(
            {
                "request_id": request_id,
                "mode": request.mode,
                "symbols": symbols,
                "result_hash": result_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    deployment_id = f"deployment-{deployment_key}"
    deployment_path = intake.lab_path(request_id) / "deployments" / f"{deployment_id}.json"

    existing_records = _deployment_records(intake, request_id)
    existing = next(
        (record for record in existing_records if record.get("deployment_id") == deployment_id),
        None,
    )
    if existing is not None:
        return _deployment_response(existing)

    result_status = str(result.get("status") or "").upper()
    metrics = result.get("metrics")
    by_symbol = metrics.get("by_symbol") if isinstance(metrics, dict) else None
    missing_symbols = [symbol for symbol in symbols if not isinstance(by_symbol, dict) or symbol not in by_symbol]
    current_status = str(status.get("status") or "UNKNOWN").upper()
    decision = _latest_lab_decision(intake, request_id)

    deployment_status: Literal[
        "AWAITING_APPROVAL", "REQUESTED", "REVIEW_REQUIRED", "BLOCKED",
        "ACTIVE", "PAUSED", "FAILED", "REMOVED"
    ]
    if request.mode == "live":
        deployment_status = "BLOCKED"
        message = "LIVE 배포는 지원하지 않습니다. QA·Risk·CEO의 별도 운영 권한이 필요합니다."
    elif latest is None or result_status != "COMPLETED" or result_hash is None:
        deployment_status = "BLOCKED"
        message = "완료된 백테스트 결과가 없어 배포 요청을 접수할 수 없습니다."
    elif missing_symbols:
        deployment_status = "BLOCKED"
        message = "요청한 종목이 해당 백테스트 유니버스에 없어 배포 범위를 거부했습니다."
    elif not candidate_path.exists() or current_status != "CANDIDATE" or decision in {"PIVOT", "PAUSE", "REVIEW"}:
        deployment_status = "REVIEW_REQUIRED"
        message = (
            "사람의 배포 요청은 기록했지만 후보 아티팩트·QA·Risk 릴리스 게이트가 아직 없어 "
            "활성화하지 않았습니다. 백테스트 요약을 확인하고 별도 승인을 기다립니다."
        )
    else:
        deployment_status = "AWAITING_APPROVAL"
        message = (
            f"{request.mode.upper()} 배포 요청을 접수했습니다. 아래 백테스트 요약을 확인한 뒤 "
            "사람이 명시적으로 승인해야 PAPER 컨테이너가 생성됩니다."
        )

    summary = _backtest_summary(result, symbols=symbols, decision=decision)
    record = {
        "schema": "autonomous-strategy-deployment.v1",
        "deployment_id": deployment_id,
        "request_id": request_id,
        "requested_by": owner_id,
        "requested_at": utc_now(),
        "mode": request.mode,
        "symbols": symbols,
        "confirm": request.confirm,
        "reason": request.reason,
        "status": deployment_status,
        "research_status": current_status,
        "decision": decision,
        "plan_id": plan_id,
        "result_path": str(result_path) if result_path is not None else None,
        "result_hash": result_hash,
        "candidate_path": str(candidate_path) if candidate_path.exists() else None,
        "candidate_hash": candidate_hash,
        "approval_required": True,
        "approved_by": None,
        "approved_at": None,
        "bundle_path": None,
        "bundle_hash": None,
        "runtime_status": "NOT_STARTED",
        "execution_status": "NOT_STARTED",
        "backtest_summary": summary,
        # Only server-built, non-code runtime metadata crosses the handoff.
        "runtime_config": {
            "executor": "strategy-paper-runtime",
            "activation": "qa-risk-release-gate-required",
            "orders_enabled": False,
            "mode": request.mode.upper(),
            "symbols": symbols,
            "plan_id": plan_id,
            "signature": _strategy_signature_metadata(plan),
        },
        "message": f"{message}\n\n{_backtest_summary_text(summary)}",
    }
    with intake._locked():
        # The future Trading release consumer runs as a non-root service user.
        # The manifest contains only bounded audit/config metadata, never a
        # credential or executable code, so it uses the same shared IPC mode
        # as the research intake manifest.
        intake._write_json(deployment_path, record, mode=0o644)
    return _deployment_response(record)


@router.post(
    "/requests/{request_id}/deploy",
    response_model=StrategyDeploymentAccepted,
    status_code=202,
)
def strategy_research_deploy(
    request_id: str,
    request: StrategyDeploymentAsk,
    owner_id: str | None = Depends(optional_current_user),
) -> StrategyDeploymentAccepted:
    return request_strategy_deployment(request_id, request, owner_id)


@router.get(
    "/requests/{request_id}/deployments",
    response_model=StrategyDeploymentList,
)
def strategy_research_deployments(
    request_id: str,
    owner_id: str | None = Depends(optional_current_user),
) -> StrategyDeploymentList:
    if not owner_id:
        raise HTTPException(status_code=401, detail="strategy_deployment_authentication_required")
    intake = ResearchIntake(_lab_root())
    status = _research_status(intake, request_id)
    if status.get("actor_id") != owner_id:
        raise HTTPException(status_code=403, detail="strategy_research_request_forbidden")
    return StrategyDeploymentList(
        request_id=request_id,
        deployments=[_deployment_response(record) for record in _deployment_records(intake, request_id)],
    )


@router.get(
    "/requests/{request_id}/deployments/{deployment_id}",
    response_model=StrategyDeploymentAccepted,
)
def strategy_research_deployment_status(
    request_id: str,
    deployment_id: str,
    owner_id: str | None = Depends(optional_current_user),
) -> StrategyDeploymentAccepted:
    """Return the persisted handoff plus live private-container state."""

    if not owner_id:
        raise HTTPException(status_code=401, detail="strategy_deployment_authentication_required")
    intake = ResearchIntake(_lab_root())
    status = _research_status(intake, request_id)
    if status.get("actor_id") != owner_id:
        raise HTTPException(status_code=403, detail="strategy_research_request_forbidden")
    path = _deployment_record_path(intake, request_id, deployment_id)
    record = intake._read_json(path)
    if not isinstance(record, dict) or record.get("requested_by") != owner_id:
        raise HTTPException(status_code=403, detail="strategy_research_request_forbidden")
    if str(record.get("status") or "").upper() == "REMOVED":
        return _deployment_response(record)
    runtime = _strategy_runtime_snapshot(deployment_id=deployment_id)
    projected = dict(record)
    projected.update(
        {
            "container_name": runtime.get("container_name", record.get("container_name")),
            "container_id": (runtime.get("container") or {}).get("container_id", record.get("container_id"))
            if isinstance(runtime.get("container"), dict)
            else record.get("container_id"),
            "runtime_status": (
                "RUNNING"
                if isinstance(runtime.get("container"), dict) and runtime["container"].get("running")
                else "STOPPED"
            ),
            "runtime_detail": runtime,
        }
    )
    return _deployment_response(projected)


def approve_strategy_deployment(
    request_id: str,
    deployment_id: str,
    request: StrategyDeploymentApprovalAsk,
    owner_id: str | None,
) -> StrategyDeploymentAccepted:
    """Approve one exact request and launch its PAPER signal container.

    The result and bundle are re-hashed immediately before launch. A stale
    backtest, changed symbol list, unsupported strategy signature, or missing
    private runtime leaves the deployment non-active and auditable.
    """

    if not owner_id:
        raise HTTPException(status_code=401, detail="strategy_deployment_authentication_required")
    if not request.confirm:
        raise HTTPException(status_code=422, detail="strategy_deployment_approval_confirm_required")
    if request.override_review_required:
        _require_top_level_human_approver(owner_id)

    intake = ResearchIntake(_lab_root())
    status = _research_status(intake, request_id)
    if status.get("actor_id") != owner_id and not request.override_review_required:
        raise HTTPException(status_code=403, detail="strategy_research_request_forbidden")
    path = _deployment_record_path(intake, request_id, deployment_id)
    try:
        record = intake._read_json(path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="strategy_deployment_not_found") from exc
    if not isinstance(record, dict) or str(record.get("request_id")) != request_id:
        raise HTTPException(status_code=404, detail="strategy_deployment_not_found")
    if record.get("requested_by") != owner_id and not request.override_review_required:
        raise HTTPException(status_code=403, detail="strategy_research_request_forbidden")
    if str(record.get("status") or "").upper() == "ACTIVE":
        return _deployment_response(record)

    _bundle, bundle_path = _strategy_bundle_from_record(
        intake=intake,
        record=record,
        owner_id=owner_id,
        allow_human_override=request.override_review_required,
        override_reason=request.reason,
    )
    bundle_hash = _file_hash(bundle_path)
    if bundle_hash is None:
        raise HTTPException(status_code=409, detail="strategy_deployment_bundle_hash_unavailable")

    approved = dict(record)
    approval_audit = list(record.get("approval_audit") or [])
    approval_audit.append(
        {
            "approved_by": owner_id,
            "approved_at": utc_now(),
            "approval_type": (
                "HUMAN_TOP_LEVEL_OVERRIDE"
                if request.override_review_required
                else "HUMAN_STANDARD_APPROVAL"
            ),
            "override_review_required": request.override_review_required,
            "reason": request.reason,
        }
    )
    approved.update(
        {
            "status": "DEPLOYING",
            "approved_by": owner_id,
            "approved_at": utc_now(),
            "approval_reason": request.reason,
            "approval_type": (
                "HUMAN_TOP_LEVEL_OVERRIDE"
                if request.override_review_required
                else "HUMAN_STANDARD_APPROVAL"
            ),
            "override_review_required": request.override_review_required,
            "override_reason": request.reason if request.override_review_required else None,
            "approval_audit": approval_audit,
            "bundle_path": str(bundle_path),
            "bundle_hash": bundle_hash,
            "runtime_status": "STARTING",
            "execution_status": "SIGNAL_ONLY",
            "message": (
                (
                    "최상위 사람의 예외 승인을 확인했습니다. PIVOT/REVIEW_REQUIRED "
                    "릴리스 게이트를 감사 기록과 함께 우회하여 immutable PAPER Bundle을 만들고 "
                )
                if request.override_review_required
                else "사람 승인을 확인했습니다. immutable PAPER Bundle을 만들고 "
            )
            + "전용 전략 컨테이너를 시작합니다. 주문은 아직 생성하지 않습니다.",
        }
    )
    with intake._locked():
        intake._write_json(path, approved, mode=0o644)

    try:
        runtime = _start_strategy_paper_container(approved, bundle_path=bundle_path)
    except HTTPException as exc:
        failed = dict(approved)
        failed.update(
            {
                "status": "FAILED",
                "runtime_status": "START_FAILED",
                "message": f"PAPER 전략 컨테이너를 시작하지 못했습니다: {exc.detail}",
            }
        )
        with intake._locked():
            intake._write_json(path, failed, mode=0o644)
        return _deployment_response(failed)

    active = dict(approved)
    active.update(
        {
            "status": "ACTIVE",
            "runtime_status": str(runtime.get("runtime_status") or "RUNNING"),
            "container_name": runtime.get("container_name"),
            "container_id": runtime.get("container_id"),
            "execution_status": "SIGNAL_ONLY",
            "message": (
                "PAPER 전략 컨테이너가 실행 중입니다. 현재 Bundle은 신호 생성 전용이며, "
                "Trading/Risk/OMS 주문 연결은 별도 검증 전까지 비활성입니다."
            ),
        }
    )
    with intake._locked():
        intake._write_json(path, active, mode=0o644)
    return _deployment_response(active)


@router.post(
    "/requests/{request_id}/deployments/{deployment_id}/approve",
    response_model=StrategyDeploymentAccepted,
    status_code=202,
)
def strategy_research_deployment_approve(
    request_id: str,
    deployment_id: str,
    request: StrategyDeploymentApprovalAsk,
    owner_id: str | None = Depends(optional_current_user),
) -> StrategyDeploymentAccepted:
    return approve_strategy_deployment(request_id, deployment_id, request, owner_id)


def _owned_deployment_for_lifecycle(
    *,
    intake: ResearchIntake,
    owner_id: str,
    request_id: str | None,
    deployment_id: str | None,
    statuses: set[str],
) -> tuple[str, dict[str, Any]]:
    """Resolve one owner-scoped deployment; never guess among multiple matches."""

    candidates: list[tuple[str, dict[str, Any]]] = []
    lab_paths = [intake.lab_path(request_id)] if request_id else (
        sorted(intake.labs_dir.iterdir(), reverse=True) if intake.labs_dir.exists() else []
    )
    for lab_path in lab_paths:
        if not lab_path.is_dir():
            continue
        current_request_id = request_id or lab_path.name
        for record in _deployment_records(intake, current_request_id):
            if (
                record.get("requested_by") == owner_id
                and (deployment_id is None or record.get("deployment_id") == deployment_id)
                and str(record.get("status") or "").upper() in statuses
            ):
                candidates.append((current_request_id, record))
    if len(candidates) != 1:
        raise HTTPException(
            status_code=422,
            detail="strategy_deployment_id_required_or_unique_lifecycle_target_unavailable",
        )
    return candidates[0]


def power_strategy_deployment(
    request_id: str,
    deployment_id: str,
    request: StrategyDeploymentPowerAsk,
    owner_id: str | None,
) -> StrategyDeploymentAccepted:
    """Start/stop one approved PAPER container and persist its lifecycle state."""

    if not owner_id:
        raise HTTPException(status_code=401, detail="strategy_deployment_authentication_required")
    intake = ResearchIntake(_lab_root())
    status = _research_status(intake, request_id)
    if status.get("actor_id") != owner_id:
        raise HTTPException(status_code=403, detail="strategy_research_request_forbidden")
    path = _deployment_record_path(intake, request_id, deployment_id)
    record = intake._read_json(path)
    if not isinstance(record, dict) or record.get("requested_by") != owner_id:
        raise HTTPException(status_code=403, detail="strategy_research_request_forbidden")
    current = str(record.get("status") or "").upper()
    if current not in {"ACTIVE", "PAUSED"}:
        raise HTTPException(status_code=409, detail="strategy_deployment_not_running_or_paused")
    runtime = _strategy_runtime_command(
        path=f"/deployments/{deployment_id}/power",
        payload={"action": request.action},
    )
    updated = dict(record)
    updated.update(
        {
            "status": "ACTIVE" if request.action == "start" else "PAUSED",
            "runtime_status": str(runtime.get("runtime_status") or ("RUNNING" if request.action == "start" else "STOPPED")),
            "execution_status": "SIGNAL_ONLY",
            "lifecycle_reason": request.reason,
            "container_name": runtime.get("container_name", record.get("container_name")),
            "container_id": runtime.get("container_id", record.get("container_id")),
            "message": (
                "PAPER 전략 컨테이너를 다시 시작했습니다. 주문은 생성하지 않습니다."
                if request.action == "start"
                else "PAPER 전략 컨테이너를 중지했습니다. 연구 증거와 배포 Bundle은 보존됩니다."
            ),
        }
    )
    with intake._locked():
        intake._write_json(path, updated, mode=0o644)
    return _deployment_response(updated)


def remove_strategy_deployment(
    request_id: str,
    deployment_id: str,
    request: StrategyDeploymentRemoveAsk,
    owner_id: str | None,
) -> StrategyDeploymentAccepted:
    """Retire one PAPER deployment; preserve immutable research evidence."""

    if not owner_id:
        raise HTTPException(status_code=401, detail="strategy_deployment_authentication_required")
    if not request.confirm:
        raise HTTPException(status_code=422, detail="strategy_deployment_remove_confirm_required")
    intake = ResearchIntake(_lab_root())
    status = _research_status(intake, request_id)
    if status.get("actor_id") != owner_id:
        raise HTTPException(status_code=403, detail="strategy_research_request_forbidden")
    path = _deployment_record_path(intake, request_id, deployment_id)
    record = intake._read_json(path)
    if not isinstance(record, dict) or record.get("requested_by") != owner_id:
        raise HTTPException(status_code=403, detail="strategy_research_request_forbidden")
    if str(record.get("status") or "").upper() == "REMOVED":
        return _deployment_response(record)
    if str(record.get("status") or "").upper() not in {"ACTIVE", "PAUSED", "FAILED"}:
        raise HTTPException(status_code=409, detail="strategy_deployment_not_active")
    runtime = _strategy_runtime_command(
        path=f"/deployments/{deployment_id}/remove",
        payload={},
    )
    updated = dict(record)
    updated.update(
        {
            "status": "REMOVED",
            "runtime_status": str(runtime.get("runtime_status") or "REMOVED"),
            "execution_status": "DISABLED",
            "removed_by": owner_id,
            "removed_at": utc_now(),
            "removal_reason": request.reason,
            "message": (
                "PAPER 전략 컨테이너를 제거했습니다. 연구 원본·백테스트 결과·immutable Bundle은 "
                "감사 추적을 위해 보존됩니다."
            ),
        }
    )
    with intake._locked():
        intake._write_json(path, updated, mode=0o644)
    return _deployment_response(updated)


@router.post(
    "/requests/{request_id}/deployments/{deployment_id}/power",
    response_model=StrategyDeploymentAccepted,
    status_code=202,
)
def strategy_research_deployment_power(
    request_id: str,
    deployment_id: str,
    request: StrategyDeploymentPowerAsk,
    owner_id: str | None = Depends(optional_current_user),
) -> StrategyDeploymentAccepted:
    return power_strategy_deployment(request_id, deployment_id, request, owner_id)


@router.post(
    "/requests/{request_id}/deployments/{deployment_id}/remove",
    response_model=StrategyDeploymentAccepted,
    status_code=202,
)
def strategy_research_deployment_remove(
    request_id: str,
    deployment_id: str,
    request: StrategyDeploymentRemoveAsk,
    owner_id: str | None = Depends(optional_current_user),
) -> StrategyDeploymentAccepted:
    return remove_strategy_deployment(request_id, deployment_id, request, owner_id)


def looks_like_strategy_deployment(text: str) -> bool:
    """Recognize an explicit human strategy deployment command."""

    value = str(text or "").casefold()
    noun = r"(?:전략|알파|시그널|트레이딩\s*전략|strategy|alpha|signal)"
    action = r"(?:배포\s*(?:해줘|해|하자|부탁|요청)|배포한다|가동\s*(?:해줘|해|하자)|켜\s*(?:줘|자)|돌려\s*(?:줘|보자)|deploy|run)"
    return bool(re.search(rf"{noun}.*{action}|{action}.*{noun}", value, re.IGNORECASE))


def looks_like_strategy_deployment_approval(text: str) -> bool:
    value = str(text or "").casefold()
    return bool(
        re.search(r"(?:전략|알파|strategy|alpha).{0,40}(?:배포\s*)?(?:승인|approve)", value)
        or re.search(r"(?:승인|approve).{0,40}(?:전략|알파|strategy|alpha)", value)
    )


def looks_like_strategy_deployment_override(text: str) -> bool:
    """Recognize an explicit top-level human exception approval."""

    value = str(text or "").casefold()
    has_approval = bool(re.search(r"승인|approve", value, re.IGNORECASE))
    has_override = bool(
        re.search(
            r"예외|우회|강제|override|pivot|review[_\s-]*required|릴리스\s*게이트",
            value,
            re.IGNORECASE,
        )
    )
    return has_approval and has_override and looks_like_strategy_deployment_approval(value)


def _deployment_symbols_from_text(text: str) -> list[str]:
    value = str(text or "").casefold()
    symbols: list[str] = []
    for alias, symbol in _DEPLOYMENT_SYMBOL_ALIASES.items():
        if alias in value and symbol not in symbols:
            symbols.append(symbol)
    for symbol in re.findall(r"(?<!\d)\d{6}(?!\d)", value):
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _deployment_request_id_from_text(text: str) -> str | None:
    match = re.search(
        r"(?:request(?:_id)?|lab(?:_id)?|연구실|실험)\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9_-]{7,127})",
        str(text or ""),
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _deployment_id_from_text(text: str) -> str | None:
    match = re.search(r"\bdeployment-[0-9a-f]{24}\b", str(text or ""), re.IGNORECASE)
    return match.group(0) if match else None


def _resolve_unique_completed_lab(
    intake: ResearchIntake,
    *,
    actor_id: str,
    symbols: list[str],
) -> str | None:
    matches: list[str] = []
    if not intake.labs_dir.exists():
        return None
    for lab_path in sorted(intake.labs_dir.iterdir(), reverse=True):
        request_path = lab_path / "request.json"
        if not lab_path.is_dir() or not request_path.exists():
            continue
        try:
            request = intake._read_json(request_path)
            request_id = str(request.get("request_id") or lab_path.name)
            current = intake.status(request_id)
            latest = _latest_result(intake, request_id)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not current or request.get("actor_id") != actor_id or not latest:
            continue
        result = latest[1]
        metrics = result.get("metrics")
        by_symbol = metrics.get("by_symbol") if isinstance(metrics, dict) else None
        if (
            str(result.get("status") or "").upper() == "COMPLETED"
            and isinstance(by_symbol, dict)
            and all(symbol in by_symbol for symbol in symbols)
        ):
            matches.append(request_id)
    return matches[0] if len(matches) == 1 else None


def request_strategy_deployment_from_text(
    *,
    query: str,
    actor_id: str | None,
    source: str = "web",
) -> StrategyDeploymentAccepted:
    """Turn a natural-language human command into the explicit deploy contract."""

    owner = _owner(actor_id)
    symbols = _deployment_symbols_from_text(query)
    if not symbols:
        raise HTTPException(
            status_code=422,
            detail="strategy_deployment_symbol_required",
        )
    request_id = _deployment_request_id_from_text(query)
    intake = ResearchIntake(_lab_root())
    if request_id is None:
        request_id = _resolve_unique_completed_lab(
            intake, actor_id=owner, symbols=symbols
        )
    if request_id is None:
        raise HTTPException(
            status_code=422,
            detail="strategy_deployment_request_id_required_or_unique_completed_lab_unavailable",
        )
    mode = "shadow" if re.search(r"shadow|섀도우", query, re.IGNORECASE) else "paper"
    return request_strategy_deployment(
        request_id,
        StrategyDeploymentAsk(
            mode=mode,
            symbols=symbols,
            # ``배포해줘`` is only the request step. A separate approval command
            # or UI action is required before the container can be started.
            confirm=False,
            reason=f"{source} human deployment command: {str(query).strip()[:450]}",
        ),
        owner,
    )


def approve_strategy_deployment_from_text(
    *, query: str, actor_id: str | None
) -> StrategyDeploymentAccepted:
    """Resolve a deployment ID from a human approval message."""

    owner = _owner(actor_id)
    intake = ResearchIntake(_lab_root())
    override_review_required = looks_like_strategy_deployment_override(query)
    allowed_statuses = (
        {"AWAITING_APPROVAL", "REVIEW_REQUIRED", "FAILED"}
        if override_review_required
        else {"AWAITING_APPROVAL"}
    )
    deployment_id = _deployment_id_from_text(query)
    request_id = _deployment_request_id_from_text(query)

    candidates: list[tuple[str, str]] = []
    if request_id:
        for record in _deployment_records(intake, request_id):
            if (
                record.get("deployment_id")
                and (record.get("requested_by") == owner or override_review_required)
                and str(record.get("status") or "").upper() in allowed_statuses
            ):
                candidates.append((request_id, str(record["deployment_id"])))
    elif deployment_id:
        if intake.labs_dir.exists():
            for lab_path in intake.labs_dir.iterdir():
                if not lab_path.is_dir():
                    continue
                candidate_request_id = lab_path.name
                for record in _deployment_records(intake, candidate_request_id):
                    if (
                        record.get("deployment_id") == deployment_id
                        and (
                            record.get("requested_by") == owner
                            or override_review_required
                        )
                        and str(record.get("status") or "").upper() in allowed_statuses
                    ):
                        candidates.append((candidate_request_id, deployment_id))
                        break
    else:
        # The request ID is not guessed from all users. Only one pending
        # deployment owned by this actor may be approved without an ID.
        if intake.labs_dir.exists():
            for lab_path in sorted(intake.labs_dir.iterdir(), reverse=True):
                if not lab_path.is_dir():
                    continue
                for record in _deployment_records(intake, lab_path.name):
                    if (
                        record.get("requested_by") == owner
                        and str(record.get("status") or "").upper() in allowed_statuses
                    ):
                        candidates.append((lab_path.name, str(record["deployment_id"])))

    if len(candidates) != 1:
        raise HTTPException(
            status_code=422,
            detail="strategy_deployment_id_required_or_unique_pending_request_unavailable",
        )
    resolved_request_id, resolved_deployment_id = candidates[0]
    return approve_strategy_deployment(
        resolved_request_id,
        resolved_deployment_id,
        StrategyDeploymentApprovalAsk(
            confirm=True,
            override_review_required=override_review_required,
            reason=f"human deployment approval: {str(query).strip()[:450]}",
        ),
        owner,
    )


def looks_like_strategy_deployment_power(text: str) -> bool:
    value = str(text or "").casefold()
    has_strategy = bool(re.search(r"전략|알파|strategy|alpha", value))
    has_power = bool(re.search(r"전략을?\s*(?:켜|끄)|(?:켜|끄|시작|중지|재개|정지)\s*(?:줘|자|해)|start|stop|pause|resume", value))
    return has_strategy and has_power and not looks_like_strategy_deployment_removal(value)


def looks_like_strategy_deployment_removal(text: str) -> bool:
    value = str(text or "").casefold()
    return bool(
        re.search(r"(?:전략|알파|strategy|alpha).{0,50}(?:제거|삭제|철회|해제|remove|delete|retire)", value)
        or re.search(r"(?:제거|삭제|철회|해제|remove|delete|retire).{0,50}(?:전략|알파|strategy|alpha)", value)
    )


def _lifecycle_target_from_text(
    *, query: str, actor_id: str | None, statuses: set[str]
) -> tuple[str, dict[str, Any]]:
    owner = _owner(actor_id)
    intake = ResearchIntake(_lab_root())
    return _owned_deployment_for_lifecycle(
        intake=intake,
        owner_id=owner,
        request_id=_deployment_request_id_from_text(query),
        deployment_id=_deployment_id_from_text(query),
        statuses=statuses,
    )


def power_strategy_deployment_from_text(
    *, query: str, actor_id: str | None
) -> StrategyDeploymentAccepted:
    request_id, record = _lifecycle_target_from_text(
        query=query, actor_id=actor_id, statuses={"ACTIVE", "PAUSED"}
    )
    action = "stop" if re.search(r"끄|중지|정지|stop|pause", query, re.IGNORECASE) else "start"
    return power_strategy_deployment(
        request_id,
        str(record["deployment_id"]),
        StrategyDeploymentPowerAsk(
            action=action,
            reason=f"human lifecycle command: {str(query).strip()[:450]}",
        ),
        _owner(actor_id),
    )


def remove_strategy_deployment_from_text(
    *, query: str, actor_id: str | None
) -> StrategyDeploymentAccepted:
    request_id, record = _lifecycle_target_from_text(
        query=query, actor_id=actor_id, statuses={"ACTIVE", "PAUSED", "FAILED"}
    )
    return remove_strategy_deployment(
        request_id,
        str(record["deployment_id"]),
        StrategyDeploymentRemoveAsk(
            confirm=True,
            reason=f"human removal command: {str(query).strip()[:450]}",
        ),
        _owner(actor_id),
    )


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


__all__ = [
    "StrategyDeploymentAccepted",
    "StrategyDeploymentApprovalAsk",
    "StrategyDeploymentAsk",
    "StrategyDeploymentList",
    "StrategyDeploymentPowerAsk",
    "StrategyDeploymentRemoveAsk",
    "accept_strategy_research_query",
    "approve_strategy_deployment",
    "approve_strategy_deployment_from_text",
    "looks_like_strategy_deployment",
    "looks_like_strategy_deployment_approval",
    "looks_like_strategy_deployment_override",
    "looks_like_strategy_deployment_power",
    "looks_like_strategy_deployment_removal",
    "looks_like_strategy_research",
    "power_strategy_deployment",
    "power_strategy_deployment_from_text",
    "remove_strategy_deployment",
    "remove_strategy_deployment_from_text",
    "request_strategy_deployment",
    "request_strategy_deployment_from_text",
    "router",
]
