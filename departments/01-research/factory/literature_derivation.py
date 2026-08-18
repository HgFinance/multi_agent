"""Deterministic policy separating public baselines from derived alpha candidates.

Public research is evidence for a mechanism, not evidence that its published formula
still earns alpha.  The scout therefore records the closest executable public
baseline separately from the candidate it derives.  This module decides whether that
derivation is eligible to consume an experiment trial; an LLM never grades its own
novelty.
"""

from __future__ import annotations

from pathlib import Path
import sys

_CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
if str(_CONTRACTS) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS))

import alpha_ast_surface as ast  # noqa: E402
from alpha_semantics import check_microstructure_mutations  # noqa: E402

POLICY_VERSION = "literature-derivation-v2"

DIRECT_REPLICATION = "DIRECT_REPLICATION"
MECHANISM_MUTATION = "MECHANISM_MUTATION"
CROSS_DOMAIN_TRANSFER = "CROSS_DOMAIN_TRANSFER"
DERIVATION_MODES = frozenset({
    DIRECT_REPLICATION, MECHANISM_MUTATION, CROSS_DOMAIN_TRANSFER,
})

# These are hypotheses about *why* the published mechanism may still contain an
# unexploited residual.  Window/threshold tuning is deliberately absent: changing a
# number is a parameter search inside the public family, not a new economic idea.
DERIVATION_TRANSFORMS = frozenset({
    "STATE_CONDITION",
    "CLOCK_CHANGE",
    "BOOK_DEPTH_CHANGE",
    "MECHANISM_INTERACTION",
    "RESIDUALIZE_PUBLIC_SIGNAL",
    "FAILURE_MODE_INVERSION",
    "MARKET_STRUCTURE_TRANSFER",
    "TARGET_CHANGE",
    "CROSS_SCALE_DISAGREEMENT",
    "L1_L10_DIVERGENCE",
    "L1_L10_CONVERGENCE",
    "QUOTE_TAPE_CONFIRMATION",
    "EVENT_NORMALIZATION",
    "VOLUME_NORMALIZATION",
    "EXECUTION_AWARE",
})


def _items(value) -> tuple[str, ...]:
    raw = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    return tuple(sorted({str(item).strip().upper() for item in raw if str(item).strip()}))


def assess(*, candidate: dict, mode: str, source_baseline=None,
           transforms=(), novelty_rationale: str = "", ast_module=None) -> dict:
    """Validate and describe the derivation from public method to local candidate."""
    grammar = ast_module or ast
    candidate = grammar.parse(candidate)
    mode = str(mode or "").strip().upper()
    if mode not in DERIVATION_MODES:
        raise ValueError(
            "DERIVATION_MODE must be DIRECT_REPLICATION, MECHANISM_MUTATION, "
            "or CROSS_DOMAIN_TRANSFER")

    transform_items = _items(transforms)
    unknown = sorted(set(transform_items) - DERIVATION_TRANSFORMS)
    if unknown:
        raise ValueError(f"unknown DERIVATION_TRANSFORMS: {unknown}")
    mutation_alignment = check_microstructure_mutations(
        transform_items, grammar.fields_of(candidate),
        operators=(grammar.operators_of(candidate)
                   if hasattr(grammar, "operators_of") else ()))
    if not mutation_alignment["ok"]:
        raise ValueError(
            "DERIVATION_TRANSFORMS do not match the candidate AST: "
            + "; ".join(mutation_alignment["missing"]))

    baseline = None
    if source_baseline not in (None, ""):
        baseline = grammar.parse(source_baseline)

    candidate_fp = grammar.fingerprint(candidate)
    candidate_shape = grammar.shape_fingerprint(candidate)
    baseline_fp = grammar.fingerprint(baseline) if baseline else ""
    baseline_shape = grammar.shape_fingerprint(baseline) if baseline else ""
    similarity = (grammar.structural_similarity(candidate, baseline)
                  if baseline is not None else None)
    rationale = str(novelty_rationale or "").strip()

    if mode == DIRECT_REPLICATION:
        if baseline is None:
            raise ValueError("DIRECT_REPLICATION requires SOURCE_BASELINE_EXPR")
        if candidate_fp != baseline_fp:
            raise ValueError(
                "DIRECT_REPLICATION candidate must exactly equal SOURCE_BASELINE_EXPR")
        if transform_items:
            raise ValueError("DIRECT_REPLICATION cannot declare DERIVATION_TRANSFORMS")
        eligible = False
        classification = "PUBLIC_BASELINE_CONTROL"
    elif mode == MECHANISM_MUTATION:
        if baseline is None:
            raise ValueError("MECHANISM_MUTATION requires SOURCE_BASELINE_EXPR")
        if not transform_items:
            raise ValueError("MECHANISM_MUTATION requires DERIVATION_TRANSFORMS")
        if not rationale:
            raise ValueError("MECHANISM_MUTATION requires NOVELTY_RATIONALE")
        if candidate_fp == baseline_fp:
            raise ValueError("MECHANISM_MUTATION cannot reuse the exact public formula")
        if candidate_shape == baseline_shape:
            raise ValueError(
                "MECHANISM_MUTATION changed only tunable parameters; the AST shape "
                "must differ from the public baseline")
        eligible = True
        classification = "DERIVED_ALPHA_CANDIDATE"
    else:
        if "MARKET_STRUCTURE_TRANSFER" not in transform_items:
            raise ValueError(
                "CROSS_DOMAIN_TRANSFER requires MARKET_STRUCTURE_TRANSFER")
        if not rationale:
            raise ValueError("CROSS_DOMAIN_TRANSFER requires NOVELTY_RATIONALE")
        if baseline is not None and candidate_fp == baseline_fp:
            raise ValueError(
                "CROSS_DOMAIN_TRANSFER cannot submit the unchanged source formula")
        eligible = True
        classification = "CROSS_DOMAIN_ALPHA_CANDIDATE"

    return {
        "novelty_policy_version": POLICY_VERSION,
        "derivation_mode": mode,
        "derivation_transforms": list(transform_items),
        "novelty_rationale": rationale,
        "source_baseline_expr": baseline,
        "source_baseline_fingerprint": baseline_fp,
        "source_baseline_shape_fingerprint": baseline_shape,
        "candidate_vs_source_similarity": similarity,
        "alpha_candidate_eligible": eligible,
        "novelty_classification": classification,
    }


def _selftest() -> None:
    public = {"op": "ts_mean", "field": "order_flow_imbalance", "n": 5}
    direct = assess(candidate=public, mode=DIRECT_REPLICATION,
                    source_baseline=public)
    assert not direct["alpha_candidate_eligible"]

    derived = {"op": "sub", "args": [
        public,
        {"op": "ts_mean", "field": "spread_bps", "n": 5},
    ]}
    result = assess(
        candidate=derived, mode=MECHANISM_MUTATION, source_baseline=public,
        transforms=["MECHANISM_INTERACTION"],
        novelty_rationale="Spread separates informed pressure from costly noise.")
    assert result["alpha_candidate_eligible"]

    tuned = {"op": "ts_mean", "field": "order_flow_imbalance", "n": 10}
    try:
        assess(candidate=tuned, mode=MECHANISM_MUTATION, source_baseline=public,
               transforms=["CLOCK_CHANGE"], novelty_rationale="Different window")
    except ValueError as exc:
        assert "tunable parameters" in str(exc)
    else:  # pragma: no cover - executable invariant
        raise AssertionError("window-only public factor mutation passed")


if __name__ == "__main__":
    _selftest()
    print("literature_derivation: 3 areas passed")
