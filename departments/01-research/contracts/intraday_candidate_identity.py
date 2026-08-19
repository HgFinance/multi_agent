"""Canonical durable identity for one evaluated intraday candidate contract."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


SHA256 = re.compile(r"^[0-9a-f]{64}$")
IDENTITY_COMPONENTS = (
    "candidate_ast", "semantic_plan", "baseline_ast", "feature_spec",
    "label_spec", "model_spec", "evaluator_version", "cost_model_version",
)


def stable_fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def candidate_identity_fingerprint(
    *, candidate_ast_fingerprint: str,
    semantic_plan_fingerprint: str,
    baseline_ast_fingerprint: str | None,
    feature_spec_fingerprint: str,
    label_spec_fingerprint: str,
    model_spec_fingerprint: str,
    evaluator_version: str,
    cost_model_version: str,
) -> str:
    """Reproduce the append-only ledger's exact scientific identity."""

    values = {
        "candidate_ast": candidate_ast_fingerprint,
        "semantic_plan": semantic_plan_fingerprint,
        "baseline_ast": baseline_ast_fingerprint,
        "feature_spec": feature_spec_fingerprint,
        "label_spec": label_spec_fingerprint,
        "model_spec": model_spec_fingerprint,
        "evaluator_version": evaluator_version,
        "cost_model_version": cost_model_version,
    }
    for name in IDENTITY_COMPONENTS:
        value = values[name]
        if name == "baseline_ast" and value is None:
            continue
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} identity component is required")
        if name not in {"evaluator_version", "cost_model_version"} \
                and not SHA256.fullmatch(value):
            raise ValueError(f"{name} must be lowercase SHA-256")
    return stable_fingerprint(values)


def lineage_identity_matches(lineage: Mapping[str, Any]) -> bool:
    """Fail closed when any persisted lineage component was altered/omitted."""

    claimed = str(lineage.get("candidate_identity_fingerprint") or "")
    if not SHA256.fullmatch(claimed):
        return False
    try:
        actual = candidate_identity_fingerprint(
            candidate_ast_fingerprint=str(
                lineage.get("candidate_ast_fingerprint") or ""),
            semantic_plan_fingerprint=str(
                lineage.get("semantic_plan_fingerprint") or ""),
            baseline_ast_fingerprint=(
                str(lineage["baseline_ast_fingerprint"])
                if lineage.get("baseline_ast_fingerprint") is not None
                else None),
            feature_spec_fingerprint=str(
                lineage.get("feature_spec_fingerprint") or ""),
            label_spec_fingerprint=str(
                lineage.get("label_spec_fingerprint") or ""),
            model_spec_fingerprint=str(
                lineage.get("model_spec_fingerprint") or ""),
            evaluator_version=str(lineage.get("evaluator_version") or ""),
            cost_model_version=str(lineage.get("cost_model_version") or ""),
        )
    except ValueError:
        return False
    return actual == claimed


__all__ = [
    "IDENTITY_COMPONENTS", "SHA256", "candidate_identity_fingerprint",
    "lineage_identity_matches", "stable_fingerprint",
]
