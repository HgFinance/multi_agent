"""Deterministic lineage policy for evolutionary alpha search.

The model proposes economic mutations; code decides whether the claimed child is
actually different from its parent.  This is intentionally not a formula generator:
blind AST mutation produces syntactic diversity without an economic hypothesis.
"""

from __future__ import annotations

from pathlib import Path
import sys

_CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
if str(_CONTRACTS) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS))

from alpha_semantics import check_microstructure_mutations  # noqa: E402


POLICY_VERSION = "alpha-evolution-v2"

# Operators change an economic coordinate, not merely a threshold or lookback.
EVOLUTION_OPERATORS = frozenset({
    "STATE_CONDITION",
    "FAILURE_MODE_INVERSION",
    "RESIDUALIZE_PUBLIC_SIGNAL",
    "CROSS_SCALE_DISAGREEMENT",
    "MECHANISM_INTERACTION",
    "CLOCK_CHANGE",
    "L1_L10_DIVERGENCE",
    "L1_L10_CONVERGENCE",
    "QUOTE_TAPE_CONFIRMATION",
    "EVENT_NORMALIZATION",
    "VOLUME_NORMALIZATION",
    "EXECUTION_AWARE",
    "TARGET_CHANGE",
    "MARKET_STRUCTURE_TRANSFER",
})


def _items(value) -> tuple[str, ...]:
    raw = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    return tuple(sorted({str(item).strip().upper() for item in raw
                         if str(item).strip()}))


def assess_lineage(*, candidate: dict, parent=None, operators=(),
                   expected_increment: str = "", ablations=(), grammar) -> dict:
    """Validate parent/child provenance and return durable JSON metadata.

    A candidate without a parent is a population seed.  Once a parent is declared,
    its exact formula and parameter-insensitive shape must both differ.  Numeric-only
    tuning remains a trial in the existing family, never a new economic child.
    """
    child = grammar.parse(candidate)
    ops = _items(operators)
    unknown = sorted(set(ops) - EVOLUTION_OPERATORS)
    if unknown:
        raise ValueError(f"unknown EVOLUTION_OPERATORS: {unknown}")
    mutation_alignment = check_microstructure_mutations(
        ops, grammar.fields_of(child),
        operators=(grammar.operators_of(child)
                   if hasattr(grammar, "operators_of") else ()))
    if not mutation_alignment["ok"]:
        raise ValueError(
            "EVOLUTION_OPERATORS do not match the child AST: "
            + "; ".join(mutation_alignment["missing"]))
    ablation_items = tuple(str(item).strip() for item in (
        ablations if isinstance(ablations, (list, tuple, set))
        else str(ablations or "").split("|")
    ) if str(item).strip())
    increment = str(expected_increment or "").strip()

    if parent in (None, ""):
        if ops or increment or ablation_items:
            raise ValueError(
                "evolution metadata requires PARENT_SIGNAL_EXPR; omit all lineage "
                "fields for a population seed")
        return {
            "evolution_policy_version": POLICY_VERSION,
            "evolution_role": "SEED",
            "parent_signal_expr": None,
            "parent_ast_fingerprint": "",
            "parent_ast_shape_fingerprint": "",
            "child_vs_parent_similarity": None,
            "evolution_operators": [],
            "expected_increment": "",
            "ablations": [],
        }

    base = grammar.parse(parent)
    if not ops:
        raise ValueError("a child with PARENT_SIGNAL_EXPR requires EVOLUTION_OPERATORS")
    if not increment:
        raise ValueError("a child with PARENT_SIGNAL_EXPR requires EXPECTED_INCREMENT")
    if not ablation_items:
        raise ValueError("a child with PARENT_SIGNAL_EXPR requires ABLATIONS")
    child_fp, parent_fp = grammar.fingerprint(child), grammar.fingerprint(base)
    child_shape = grammar.shape_fingerprint(child)
    parent_shape = grammar.shape_fingerprint(base)
    if child_fp == parent_fp:
        raise ValueError("evolution child exactly reuses its parent formula")
    if child_shape == parent_shape:
        raise ValueError(
            "evolution child changed only tunable parameters; an economic child "
            "must change the AST shape")

    return {
        "evolution_policy_version": POLICY_VERSION,
        "evolution_role": "CHILD",
        "parent_signal_expr": base,
        "parent_ast_fingerprint": parent_fp,
        "parent_ast_shape_fingerprint": parent_shape,
        "child_vs_parent_similarity": grammar.structural_similarity(child, base),
        "evolution_operators": list(ops),
        "expected_increment": increment,
        "ablations": list(ablation_items),
    }


def _selftest() -> None:
    import intraday_ast_contract as grammar

    parent = {"op": "rolling_mean", "arg": {
        "op": "field", "field": "trade_flow_imbalance"}, "seconds": 30}
    child = {"op": "where", "condition": {"op": "gt", "args": [
        {"op": "field", "field": "spread_bps"}, {"const": 5, "unit": "BPS"}]},
        "then": parent, "else": {"const": 0, "unit": "RATIO"}}
    result = assess_lineage(
        candidate=child, parent=parent, operators=["STATE_CONDITION"],
        expected_increment="Wide spread isolates urgent liquidity demand.",
        ablations=["remove spread gate"], grammar=grammar)
    assert result["evolution_role"] == "CHILD"
    assert result["parent_ast_fingerprint"] == grammar.fingerprint(parent)

    tuned = {**parent, "seconds": 60}
    try:
        assess_lineage(candidate=tuned, parent=parent, operators=["CLOCK_CHANGE"],
                       expected_increment="longer persistence",
                       ablations=["30 second clock"], grammar=grammar)
    except ValueError as exc:
        assert "tunable parameters" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("parameter-only child passed")


if __name__ == "__main__":
    _selftest()
    print("alpha_evolution: 2 areas passed")
