"""Deterministic guards for Risk Worker input and evidence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from .contracts import RiskSkillContext, RiskSkillResult, hash_payload, make_result


class RiskSkillGuardError(ValueError):
    """Raised when input cannot safely enter a Risk Skill graph."""


def build_context(
    payload: dict[str, Any],
    *,
    worker_id: str,
    profile_version: str = "risk.worker-context.v1",
    allowed_scopes: Sequence[str] | None = None,
    timeout_ms: int = 8_000,
    attempt: int = 1,
) -> RiskSkillContext:
    if not isinstance(payload, dict):
        raise RiskSkillGuardError("INVALID_INPUT")

    # Worker Registry scopes are trusted.  Payload scopes remain available for
    # direct Skill tests, but a compiled Worker must pass its registry scopes
    # explicitly so a missing payload field cannot widen access.
    raw_scopes = (
        allowed_scopes
        if allowed_scopes is not None
        else payload.get("allowed_scopes", ())
    )
    if isinstance(raw_scopes, str):
        raw_scopes = (raw_scopes,)
    if not isinstance(raw_scopes, (list, tuple, set)):
        raise RiskSkillGuardError("INVALID_SCOPE_FORMAT")

    return RiskSkillContext(
        trace_id=str(payload.get("trace_id") or f"local:{hash_payload(payload)[:16]}"),
        case_id=(
            str(payload["case_id"]) if payload.get("case_id") is not None else None
        ),
        worker_id=worker_id,
        profile_version=profile_version,
        as_of=payload.get("as_of") or datetime.now(timezone.utc),
        input_hash=str(payload.get("input_hash") or hash_payload(payload)),
        allowed_scopes=tuple(str(scope) for scope in raw_scopes),
        timeout_ms=timeout_ms,
        attempt=attempt,
    )


def scope_check(
    context: RiskSkillContext, requested_scope: str | None
) -> RiskSkillResult:
    """Allow only an explicitly granted scope when scopes are supplied."""

    if (
        not requested_scope
        or not context.allowed_scopes
        or requested_scope not in context.allowed_scopes
    ):
        return make_result(
            "guard.scope_check.v1",
            "ESCALATE",
            {"requested_scope": requested_scope},
            error_code="SCOPE_DENIED",
            escalate=True,
        )
    return make_result("guard.scope_check.v1", "COMPLETED", {"scope": requested_scope})


def freshness_check(
    context: RiskSkillContext,
    observed_at: str | datetime | None,
    *,
    max_age_seconds: int,
) -> RiskSkillResult:
    """Apply a point-in-time and freshness check to tool data."""

    if observed_at is None:
        return make_result(
            "guard.pit_filter.v1",
            "ESCALATE",
            error_code="MISSING_OBSERVED_AT",
            escalate=True,
        )
    try:
        observed = (
            observed_at
            if isinstance(observed_at, datetime)
            else datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        )
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
    except ValueError:
        return make_result(
            "guard.pit_filter.v1",
            "ESCALATE",
            error_code="INVALID_OBSERVED_AT",
            escalate=True,
        )

    as_of = context.as_of
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    age = (as_of - observed).total_seconds()
    if age < 0:
        return make_result(
            "guard.pit_filter.v1",
            "ESCALATE",
            {"age_seconds": age},
            error_code="FUTURE_INPUT",
            escalate=True,
        )
    if age > max_age_seconds:
        return make_result(
            "guard.pit_filter.v1",
            "ESCALATE",
            {"age_seconds": age},
            error_code="STALE_INPUT",
            escalate=True,
        )
    return make_result("guard.pit_filter.v1", "COMPLETED", {"age_seconds": age})


def normalize_tool_output(value: Any) -> RiskSkillResult:
    """Normalize a callable Tool response into the shared Skill contract."""

    if isinstance(value, RiskSkillResult):
        return value
    if isinstance(value, dict):
        return make_result("context.internal_api.v1", "COMPLETED", value)
    return make_result(
        "context.internal_api.v1",
        "ESCALATE",
        error_code="INVALID_TOOL_RESPONSE",
        escalate=True,
    )
