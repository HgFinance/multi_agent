"""Deterministic guards for AI-QA Worker input and evidence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from .contracts import QASkillContext, QASkillResult, hash_payload, make_result


class QASkillGuardError(ValueError):
    """Raised when QA input cannot safely enter a Skill graph."""


def build_context(
    payload: dict[str, Any],
    *,
    worker_id: str,
    profile_version: str = "qa.worker-context.v1",
    allowed_scopes: Sequence[str] | None = None,
    timeout_ms: int = 8_000,
    attempt: int = 1,
) -> QASkillContext:
    if not isinstance(payload, dict):
        raise QASkillGuardError("INVALID_INPUT")
    # Worker Registry scopes are trusted.  Payload scopes remain available for
    # direct Skill tests, but a compiled Worker must pass its registry scopes
    # explicitly so a missing payload field cannot widen access.
    raw_scopes = allowed_scopes if allowed_scopes is not None else payload.get("allowed_scopes", ())
    if isinstance(raw_scopes, str):
        raw_scopes = (raw_scopes,)
    if not isinstance(raw_scopes, (list, tuple, set)):
        raise QASkillGuardError("INVALID_SCOPE_FORMAT")
    return QASkillContext(
        trace_id=str(payload.get("trace_id") or f"local:{hash_payload(payload)[:16]}"),
        case_id=(str(payload["case_id"]) if payload.get("case_id") is not None else None),
        worker_id=worker_id,
        profile_version=profile_version,
        as_of=(
            payload.get("as_of")
            or payload.get("decision_time")
            or datetime.now(timezone.utc)
        ),
        input_hash=str(payload.get("input_hash") or hash_payload(payload)),
        allowed_scopes=tuple(str(scope) for scope in raw_scopes),
        timeout_ms=timeout_ms,
        attempt=attempt,
    )


def scope_check(
    context: QASkillContext, requested_scope: str | None
) -> QASkillResult:
    if not requested_scope or not context.allowed_scopes or requested_scope not in context.allowed_scopes:
        return make_result(
            "guard.scope_check.v1",
            "ESCALATE",
            {"requested_scope": requested_scope},
            error_code="SCOPE_DENIED",
            escalate=True,
        )
    return make_result(
        "guard.scope_check.v1", "COMPLETED", {"scope": requested_scope}
    )


def _parse_time(value: str | datetime) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def pit_check(
    context: QASkillContext,
    observed_at: str | datetime | None,
    *,
    published_at: str | datetime | None = None,
) -> QASkillResult:
    """Reject missing, malformed, or future evidence timestamps."""

    if observed_at is None:
        return make_result(
            "guard.pit_filter.v1",
            "ESCALATE",
            error_code="MISSING_OBSERVED_AT",
            escalate=True,
        )
    try:
        observed = _parse_time(observed_at)
        published = _parse_time(published_at) if published_at is not None else None
    except (TypeError, ValueError):
        return make_result(
            "guard.pit_filter.v1",
            "ESCALATE",
            error_code="INVALID_EVIDENCE_TIME",
            escalate=True,
        )

    as_of = context.as_of
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    if observed > as_of or (published is not None and published > as_of):
        return make_result(
            "guard.pit_filter.v1",
            "ESCALATE",
            {"observed_at": observed.isoformat()},
            error_code="FUTURE_EVIDENCE",
            escalate=True,
        )
    if published is not None and published > observed:
        return make_result(
            "guard.pit_filter.v1",
            "ESCALATE",
            error_code="INVALID_EVIDENCE_ORDER",
            escalate=True,
        )
    return make_result(
        "guard.pit_filter.v1",
        "COMPLETED",
        {"observed_at": observed.isoformat()},
    )


def normalize_tool_output(value: Any) -> QASkillResult:
    if isinstance(value, QASkillResult):
        return value
    if isinstance(value, dict):
        return make_result("context.internal_api.v1", "COMPLETED", value)
    return make_result(
        "context.internal_api.v1",
        "ESCALATE",
        error_code="INVALID_TOOL_RESPONSE",
        escalate=True,
    )
