"""Shared contract for the redacted QA feedback plane.

The evaluator, Discord adapter, and API must agree on the same vocabulary.
Keeping the vocabulary here prevents a new finding or lifecycle state from
being silently accepted by one surface and dropped by another.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any


FEEDBACK_DECISIONS = frozenset(
    {
        "OBSERVED_PASS",
        "REVIEW_REQUIRED",
        "REVIEW_WORTHY",
        "IMPROVEMENT_CANDIDATE",
        "EVOLUTION_PROPOSAL",
    }
)
REVIEW_DECISIONS = FEEDBACK_DECISIONS - {"OBSERVED_PASS"}

IMPROVEMENT_TYPES = frozenset(
    {
        "SKILL_CREATE",
        "SKILL_EVOLVE",
        "CODE_FIX",
        "PROMPT_POLICY",
        "RUNTIME_CONFIG",
        "DATA_QUALITY",
        "NO_ACTION",
    }
)

# These findings are safe to publish as bounded, metadata-only QA work.  A
# finding can be review-worthy without being evidence that a Skill should be
# changed.
ACTIONABLE_FEEDBACK_CODES = frozenset(
    {
        "LANGFUSE_OBSERVABILITY_UNAVAILABLE",
        "WORKER_OR_WORKFLOW_DEGRADED",
        "LATENCY_ABOVE_THRESHOLD",
        "STRUCTURED_EVAL_SCORE_LOW",
        "SEMANTIC_QA_FAILED",
        "SEMANTIC_QA_SCORE_LOW",
        "SEMANTIC_QA_RELEVANCE_LOW",
        "HALLUCINATION_DETECTED",
        "HARMFUL_CONTENT_DETECTED",
        "RELEVANCE_LOW",
        "LENGTH_TERMINATION_HIGH",
        "PRIVACY_PAYLOAD_PRESENT",
        "REDACTION_MARKER_MISSING",
        "CORRELATION_METADATA_MISSING",
        "DEPARTMENT_METADATA_MISSING",
    }
)

REVIEW_REQUIRED_FINDINGS = frozenset(
    {"PRIVACY_PAYLOAD_PRESENT", "REDACTION_MARKER_MISSING"}
)

PERFORMANCE_FINDINGS = frozenset({"LATENCY_ABOVE_THRESHOLD"})
OBSERVABILITY_FINDINGS = frozenset(
    {"CORRELATION_METADATA_MISSING", "DEPARTMENT_METADATA_MISSING"}
)

# Approval is an external human action.  The Discord gateway is the only
# supported producer of an approval identity, so keep the identity format
# narrow at the shared store boundary as well as at the HTTP boundary.
_DISCORD_APPROVER_RE = re.compile(r"^discord:(?P<user_id>[0-9]{6,24})$")


def configured_qa_approver_user_ids(
    *, env: Mapping[str, str] | None = None
) -> frozenset[str]:
    """Return the configured Discord users allowed to approve QA work."""

    values = os.environ if env is None else env
    raw = str(values.get("QA_DISCORD_APPROVER_USER_IDS", "") or "")
    return frozenset(
        part
        for part in re.split(r"[\s,]+", raw)
        if part.isdigit() and 6 <= len(part) <= 24
    )


def human_approver_user_id(value: Any) -> str | None:
    """Extract a real Discord user ID from a persisted approval identity."""

    match = _DISCORD_APPROVER_RE.fullmatch(str(value or "").strip())
    return match.group("user_id") if match else None


def is_human_approver(
    value: Any, *, allowed_user_ids: Any | None = None
) -> bool:
    """Validate an approval identity and, when supplied, its allowlist."""

    user_id = human_approver_user_id(value)
    if user_id is None:
        return False
    if allowed_user_ids is None:
        return True
    allowed = {
        str(item).strip()
        for item in allowed_user_ids
        if str(item).strip().isdigit()
    }
    return user_id in allowed


def qa_approver_is_allowed(
    value: Any,
    *,
    env: Mapping[str, str] | None = None,
    allowlist_required: bool | None = None,
) -> bool:
    """Apply the shared fail-closed QA approver policy."""

    values = os.environ if env is None else env
    required = (
        allowlist_required
        if allowlist_required is not None
        else str(values.get("QA_APPROVER_ALLOWLIST_REQUIRED", "true"))
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )
    allowed = configured_qa_approver_user_ids(env=values)
    if required and not allowed:
        return False
    return is_human_approver(value, allowed_user_ids=allowed if required else None)


def is_actionable_feedback(finding_codes: Any) -> bool:
    """Return whether one finding set can be sent to the QA Discord lane."""

    if not isinstance(finding_codes, (list, tuple, set, frozenset)):
        return False
    return bool(
        ACTIONABLE_FEEDBACK_CODES.intersection(
            str(code).strip().upper() for code in finding_codes
        )
    )


def feedback_decision_label(value: Any) -> str:
    """Return one manager-facing label for a lifecycle decision."""

    return {
        "OBSERVED_PASS": "관측상 정상",
        "REVIEW_REQUIRED": "검토 필수",
        "REVIEW_WORTHY": "검토 대상",
        "IMPROVEMENT_CANDIDATE": "개선 후보",
        "EVOLUTION_PROPOSAL": "Evolution 제안",
    }.get(str(value or "").strip().upper(), "확인 필요")
