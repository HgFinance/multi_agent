"""Bridge verified Kanban QA findings into the governed improvement queue.

MemoHarness D5 is allowed to remember a verified outcome, but it must not
rewrite the deterministic router or any production policy.  This module adds
the missing observer-side handoff:

    completed QA -> redacted improvement candidate -> human approval
    -> deterministic admission benchmark -> central-router regression queue

There is a separate CEO-owned self-review lane.  It converts only allow-listed
structured findings into bounded corrective guardrails for the next CEO
synthesis.  This is a policy reminder, not a QA command, skill activation, or
automatic code/router mutation.

The existing ``FeedbackLedger`` is used as the durable approval/benchmark
index.  Candidate metadata is deliberately payload-free: no user request,
answer, provider output, or QA prose is copied into the ledger.  A benchmark
passing here means only that the candidate is structurally safe to hand to a
regression owner; it is not evidence that production code was changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

from orchestration.ceo_query_routing import verify_primary_route
from orchestration.ceo_workflow_scope import read_marker, user_query_from_body
from orchestration.experience_bank import ExperienceRecord
from orchestration.langsmith_feedback import (
    EvaluationResult,
    FeedbackConfig,
    FeedbackLedger,
)

D5_IMPROVEMENT_SCHEMA = "hgfinance.memo-harness.d5-improvement.v1"
D5_ADMISSION_BENCHMARK_VERSION = "d5-improvement-admission-v1"
D5_SOURCE = "memo_harness_d5"
CEO_SELF_IMPROVEMENT_SCHEMA = "hgfinance.memo-harness.ceo-self-improvement.v1"
_CHECK_VALUE_RE = re.compile(
    r"^(PASS_WITH_LIMITATION|PASS|WARN_UNVERIFIABLE|WARN|FAIL|DEFER)\b",
    re.IGNORECASE,
)

# QA may identify a problem; only this application-owned table decides which
# safe, deterministic CEO self-check can be activated.  No free-form QA text
# can become an instruction and no entry here changes routing or authority.
_CEO_GUARDRAILS: dict[str, dict[str, str]] = {
    "D5_ROUTING_MISMATCH": {
        "id": "CEO_ROUTE_RECHECK",
        "rule": (
            "Before final synthesis, compare the canonical deterministic route "
            "with the materialized primary set. Do not silently accept or repair "
            "a mismatch; disclose the bounded failure or defer."
        ),
    },
    "D5_CHECK_LANGSMITH_AUTHORITATIVE_EXECUTION": {
        "id": "CEO_TRACE_EVIDENCE_RECHECK",
        "rule": (
            "Treat an unavailable authoritative execution trace as unverified. "
            "A published receipt or metadata-only record is not proof that a "
            "trace exists."
        ),
    },
    "D5_FINDING_QA_F_001": {
        "id": "CEO_TRACE_EVIDENCE_RECHECK",
        "rule": (
            "Treat an unavailable authoritative execution trace as unverified. "
            "A published receipt or metadata-only record is not proof that a "
            "trace exists."
        ),
    },
    "D5_CHECK_EVIDENCE": {
        "id": "CEO_EVIDENCE_BOUNDARY_RECHECK",
        "rule": (
            "Separate observed facts, source-backed evidence, and unresolved "
            "claims. Do not turn missing evidence into a conclusion."
        ),
    },
    "D5_CHECK_EVIDENCE_GROUNDING": {
        "id": "CEO_EVIDENCE_BOUNDARY_RECHECK",
        "rule": (
            "Separate observed facts, source-backed evidence, and unresolved "
            "claims. Do not turn missing evidence into a conclusion."
        ),
    },
    "D5_CHECK_REPRODUCIBILITY": {
        "id": "CEO_REPRODUCIBILITY_RECHECK",
        "rule": (
            "State what can be independently reproduced from the supplied "
            "artifacts and keep non-reproducible execution claims qualified."
        ),
    },
    "D5_CHECK_UNSUPPORTED_CLAIMS": {
        "id": "CEO_UNSUPPORTED_CLAIMS_RECHECK",
        "rule": (
            "Remove or qualify claims that lack a supplied source, calculation, "
            "or execution record; do not fill the gap with model inference."
        ),
    },
    "D5_CHECK_OVERALL_RESULT": {
        "id": "CEO_FINAL_VERDICT_RECHECK",
        "rule": (
            "Reconcile the final verdict with every material limitation. If a "
            "high-severity check is unresolved, preserve the bounded warning or "
            "DEFER state instead of presenting PASS."
        ),
    },
}


def _bounded(value: Any, limit: int = 160) -> str:
    return " ".join(str(value or "").split())[:limit]


def _safe_code(value: Any, *, prefix: str = "D5") -> str:
    rendered = _bounded(value, 80).upper()
    rendered = re.sub(r"[^A-Z0-9]+", "_", rendered).strip("_")
    return f"{prefix}_{rendered}"[:96] if rendered else ""


def _status(value: Any) -> str:
    rendered = _bounded(value, 80).upper().replace(" ", "_")
    match = _CHECK_VALUE_RE.match(rendered)
    if match:
        return match.group(1).upper()
    return rendered


def _check_name(item: Mapping[str, Any]) -> Any:
    return (
        item.get("check")
        or item.get("name")
        or item.get("key")
        or item.get("code")
        or item.get("label")
        or item.get("title")
        or item.get("id")
    )


def _check_codes(checks: Any) -> list[str]:
    """Convert only explicit non-pass check identities into stable codes."""

    codes: list[str] = []
    if isinstance(checks, Mapping):
        items = checks.items()
    elif isinstance(checks, Sequence) and not isinstance(
        checks, (str, bytes, bytearray)
    ):

        def _sequence_items():
            for item in checks:
                if isinstance(item, Mapping):
                    yield _check_name(item), item
                    continue
                if isinstance(item, str) and item.strip():
                    rendered = item.strip()
                    match = re.match(
                        r"^([a-zA-Z0-9_.:/ -]+?)\s*(?:=|:|—|-)\s*"
                        r"(PASS_WITH_LIMITATION|PASS|WARN_UNVERIFIABLE|WARN|FAIL|DEFER)\b",
                        rendered,
                        re.IGNORECASE,
                    )
                    if match:
                        yield match.group(1).strip(), match.group(2)
                    else:
                        yield rendered, "UNKNOWN"

        items = _sequence_items()
    else:
        items = ()
    for key, value in items:
        name = _bounded(key, 80)
        if isinstance(value, Mapping):
            result = _status(value.get("result") or value.get("status"))
        else:
            result = _status(value)
        if not name or result in {"PASS", "PASS_WITH_LIMITATION", ""}:
            continue
        code = _safe_code(name, prefix="D5_CHECK")
        if code and code not in codes:
            codes.append(code)
    return codes[:8]


def _finding_codes(findings: Any) -> list[str]:
    """Use structured finding IDs/codes only; never derive codes from prose."""

    if not isinstance(findings, Sequence) or isinstance(
        findings, (str, bytes, bytearray)
    ):
        return []
    codes: list[str] = []
    for item in findings[:8]:
        if not isinstance(item, Mapping):
            continue
        identity = (
            item.get("finding_code")
            or item.get("code")
            or item.get("reason_code")
            or item.get("type")
            or item.get("finding_id")
            or item.get("id")
        )
        code = _safe_code(identity, prefix="D5_FINDING")
        if code and code not in codes:
            codes.append(code)
    return codes[:8]


def _routing_projection(
    *, root_body: str, actual_primary_profiles: Sequence[str]
) -> tuple[str, tuple[str, ...], tuple[str, ...], bool]:
    query = user_query_from_body(root_body)
    actual = tuple(
        dict.fromkeys(
            _bounded(profile, 96)
            for profile in actual_primary_profiles
            if _bounded(profile, 96)
        )
    )
    if not query:
        return (
            _bounded(read_marker(root_body, "routing_category") or "UNKNOWN", 64),
            (),
            actual,
            False,
        )
    verification = verify_primary_route(query, actual)
    return (
        _bounded(verification.expected_category or "UNKNOWN", 64),
        verification.expected_primary_profiles,
        verification.actual_primary_profiles,
        not verification.valid,
    )


def _regression_case_hash(
    *,
    category: str,
    finding_code: str,
    expected: Sequence[str],
    actual: Sequence[str],
) -> str:
    manifest = {
        "schema": D5_IMPROVEMENT_SCHEMA,
        "category": category,
        "finding_code": finding_code,
        "expected_primary_profiles": list(expected),
        "actual_primary_profiles": list(actual),
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _candidate_metadata(
    *,
    root_id: str,
    qa_task_id: str,
    category: str,
    finding_code: str,
    expected: Sequence[str],
    actual: Sequence[str],
    routing_mismatch: bool,
) -> dict[str, Any]:
    candidate_type = "ROUTE_RULE_CHANGE" if routing_mismatch else "QA_CONTRACT_REVIEW"
    regression_target = (
        "tests/orchestration/test_ceo_bff_routing.py"
        if routing_mismatch
        else "tests/orchestration/test_qa_discord_feedback.py"
    )
    return {
        "schema_version": D5_IMPROVEMENT_SCHEMA,
        "source": D5_SOURCE,
        "trace_kind": "kanban_qa",
        "root_id": _bounded(root_id, 128),
        "task_id": _bounded(qa_task_id, 128),
        "category": category,
        "finding_code": finding_code,
        "candidate_type": candidate_type,
        "improvement_owner": "ceo",
        "improvement_lane": "ceo_self_review",
        "expected_primary_profiles": list(expected)[:8],
        "actual_primary_profiles": list(actual)[:8],
        "route_verification": "mismatch" if routing_mismatch else "not_applicable",
        "regression_test_target": regression_target,
        "regression_case_hash": _regression_case_hash(
            category=category,
            finding_code=finding_code,
            expected=expected,
            actual=actual,
        ),
        "promotion_gate": "human_approval_and_central_router_regression",
        "raw_payloads_sent": False,
    }


def build_d5_improvement_results(
    *,
    root_id: str,
    root_payload: Mapping[str, Any],
    qa_task_id: str,
    projection_result: Mapping[str, Any],
    record: ExperienceRecord,
) -> tuple[EvaluationResult, ...]:
    """Build redacted candidates from one persisted terminal QA projection."""

    decision = _bounded(
        projection_result.get("canonical_decision") or "UNKNOWN", 32
    ).upper()
    if decision == "PASS":
        return ()
    root = _bounded(root_id, 128)
    qa_id = _bounded(qa_task_id, 128)
    if not root or not qa_id:
        return ()
    root_body = str(root_payload.get("body") or "")
    category, expected, actual, route_mismatch = _routing_projection(
        root_body=root_body,
        actual_primary_profiles=record.primary_departments,
    )
    codes: list[str] = []
    if route_mismatch or "ROUTING_MISMATCH" in record.failure_codes:
        codes.append("D5_ROUTING_MISMATCH")
        route_mismatch = True
    for code in _check_codes(projection_result.get("checks")):
        if code not in codes:
            codes.append(code)
    for code in _finding_codes(projection_result.get("findings")):
        if code not in codes:
            codes.append(code)
    if not codes:
        codes.append(_safe_code(decision, prefix="D5_QA"))

    source_run_id = f"d5-qa:{root}:{qa_id}"[:240]
    results: list[EvaluationResult] = []
    for code in codes[:8]:
        metadata = _candidate_metadata(
            root_id=root,
            qa_task_id=qa_id,
            category=category,
            finding_code=code,
            expected=expected,
            actual=actual,
            routing_mismatch=route_mismatch and code == "D5_ROUTING_MISMATCH",
        )
        results.append(
            EvaluationResult(
                source_run_id=f"{source_run_id}:{code}"[:240],
                department="ceo-workflow",
                workflow_role="qa-observer",
                decision="REVIEW_REQUIRED"
                if decision == "FAIL"
                else "IMPROVEMENT_CANDIDATE",
                score=None,
                finding_codes=(code,),
                summaries=(
                    "verified post-response QA finding requires owner review and regression evidence",
                ),
                metadata=metadata,
            )
        )
    return tuple(results)


def record_verified_d5_candidates(
    ledger: FeedbackLedger,
    *,
    root_id: str,
    root_payload: Mapping[str, Any],
    qa_task_id: str,
    projection_result: Mapping[str, Any],
    record: ExperienceRecord,
) -> tuple[str, ...]:
    """Persist verified candidates idempotently; never mutates routing policy."""

    if str(projection_result.get("status") or "").casefold() not in {
        "persisted",
        "duplicate",
    }:
        return ()
    artifact_ids: list[str] = []
    for result in build_d5_improvement_results(
        root_id=root_id,
        root_payload=root_payload,
        qa_task_id=qa_task_id,
        projection_result=projection_result,
        record=record,
    ):
        artifact_ids.append(
            ledger.complete(
                result.source_run_id, f"d5-qa-eval:{root_id}:{qa_task_id}", result
            )
        )
    return tuple(dict.fromkeys(artifact_ids))


def d5_feedback_ledger_from_env() -> FeedbackLedger:
    """Use the existing QA ledger unless an explicit D5 state path is set."""

    path = os.getenv("MEMOHARNESS_D5_IMPROVEMENT_STATE_PATH", "").strip()
    return FeedbackLedger(path or FeedbackConfig.from_env().state_path)


def build_ceo_self_improvement_hint(
    ledger: FeedbackLedger, *, limit: int = 8
) -> dict[str, Any] | None:
    """Build a CEO-owned corrective hint from verified D5 identities only.

    The result is deliberately not an experience-memory hint.  Failed runs
    remain in the audit/improvement ledger, while the CEO receives only static
    corrective checks selected by this code.  Unknown or malformed findings
    are ignored, and all reads fail open so the CEO path remains usable while
    the feedback store is unavailable.
    """

    try:
        finding_codes = ledger.d5_finding_codes(limit=400)
    except Exception:  # noqa: BLE001 - self-review is advisory/fail-open.
        return None
    if not finding_codes:
        return None

    guardrails: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for code in finding_codes:
        candidate = _CEO_GUARDRAILS.get(code)
        if candidate is None or candidate["id"] in seen_ids:
            continue
        guardrails.append(dict(candidate))
        seen_ids.add(candidate["id"])
        if len(guardrails) >= max(1, min(int(limit), 8)):
            break
    if not guardrails:
        return None
    return {
        "schema_version": CEO_SELF_IMPROVEMENT_SCHEMA,
        "owner": "ceo",
        "source": D5_SOURCE,
        "mode": "corrective_guardrails_only",
        "verified_qa_required": True,
        "raw_payloads_sent": False,
        "guardrails": guardrails,
    }


def bounded_ceo_self_improvement_hint(
    hint: Mapping[str, Any] | None, *, limit: int = 8
) -> dict[str, Any] | None:
    """Re-validate a CEO self-review hint at the prompt boundary.

    Only the exact rule text owned by this module is accepted.  This prevents
    a future caller from treating the section as a generic instruction
    channel, even if it accidentally passes QA prose or a skill payload.
    """

    if not isinstance(hint, Mapping):
        return None
    if (
        hint.get("owner") != "ceo"
        or hint.get("mode") != "corrective_guardrails_only"
        or hint.get("verified_qa_required") is not True
        or hint.get("raw_payloads_sent") is not False
    ):
        return None
    raw_guardrails = hint.get("guardrails")
    if not isinstance(raw_guardrails, list):
        return None
    allowed = {item["id"]: item for item in _CEO_GUARDRAILS.values()}
    guardrails: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_guardrails[:8]:
        if not isinstance(item, Mapping):
            continue
        guardrail_id = str(item.get("id") or "").strip()
        expected = allowed.get(guardrail_id)
        if expected is None or guardrail_id in seen:
            continue
        if str(item.get("rule") or "").strip() != expected["rule"]:
            continue
        guardrails.append(dict(expected))
        seen.add(guardrail_id)
        if len(guardrails) >= max(1, min(int(limit), 8)):
            break
    if not guardrails:
        return None
    return {
        "schema_version": CEO_SELF_IMPROVEMENT_SCHEMA,
        "owner": "ceo",
        "mode": "corrective_guardrails_only",
        "guardrails": guardrails,
    }


def d5_regression_candidates(
    ledger: FeedbackLedger, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Return approved/admitted D5 candidates awaiting central-router work."""

    return ledger.approved_benchmark_candidates(
        source=D5_SOURCE,
        limit=limit,
    )


__all__ = [
    "D5_ADMISSION_BENCHMARK_VERSION",
    "D5_IMPROVEMENT_SCHEMA",
    "D5_SOURCE",
    "CEO_SELF_IMPROVEMENT_SCHEMA",
    "bounded_ceo_self_improvement_hint",
    "build_ceo_self_improvement_hint",
    "build_d5_improvement_results",
    "d5_feedback_ledger_from_env",
    "d5_regression_candidates",
    "record_verified_d5_candidates",
]
