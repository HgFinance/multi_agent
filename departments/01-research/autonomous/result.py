"""Validation and decision rules for experiment artifacts."""

from __future__ import annotations

from typing import Any, Mapping

from models import ExperimentResult, from_result_dict


def parse_result(
    payload: Mapping[str, Any],
    *,
    expected_plan_id: str | None = None,
    expected_preregistration_hash: str | None = None,
) -> ExperimentResult:
    result = from_result_dict(payload)
    if expected_plan_id and result.plan_id != expected_plan_id:
        raise ValueError(
            f"result plan_id {result.plan_id!r} does not match {expected_plan_id!r}"
        )
    if expected_preregistration_hash and result.preregistration_hash != expected_preregistration_hash:
        raise ValueError("result preregistration_hash does not match the registered plan")
    if not result.robustness:
        raise ValueError("robustness must contain named checks; an empty mapping is not evidence")
    if result.status == "COMPLETED" and not result.metrics:
        raise ValueError("completed result requires measured metrics; do not fabricate zero")
    return result


def decision_for(result: ExperimentResult) -> tuple[str, str]:
    """Return a conservative next action without collapsing evidence to Sharpe."""

    if result.leakage_detected:
        return "REJECT", "look-ahead or data leakage was detected"
    if result.status != "COMPLETED":
        return "PAUSE", result.failure_reason or "experiment did not complete"
    if not result.cost_included:
        return "PAUSE", "transaction costs were not measured"
    if not result.oos_evaluated:
        return "PAUSE", "out-of-sample evaluation is missing"
    if not all(bool(value) for value in result.robustness.values()):
        return "PIVOT", "at least one robustness check failed"
    return "CANDIDATE", "evidence satisfies the research gates; production promotion is not implied"


def candidate_report(result: ExperimentResult, *, hypothesis: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    decision, rationale = decision_for(result)
    return {
        "schema": "autonomous-strategy-candidate.v1",
        "decision": decision,
        "rationale": rationale,
        "strategy_thesis": hypothesis.get("statement"),
        "mechanism": hypothesis.get("mechanism"),
        "logic": hypothesis.get("expected_behavior"),
        "discovery_path": {
            "hypothesis_id": hypothesis.get("hypothesis_id"),
            "plan_id": plan.get("plan_id"),
            "parent_id": hypothesis.get("parent_id"),
            "role": hypothesis.get("role"),
        },
        "evidence": {
            "status": result.status,
            "preregistration_hash": result.preregistration_hash,
            "oos_evaluated": result.oos_evaluated,
            "cost_included": result.cost_included,
            "robustness": dict(result.robustness),
            "metrics": dict(result.metrics),
            "artifacts": list(result.artifacts),
        },
        "failure_modes": list(result.failure_modes),
        "limitations": list(result.limitations),
        "confidence": "evidence-gated candidate; not a live-trading approval",
    }
