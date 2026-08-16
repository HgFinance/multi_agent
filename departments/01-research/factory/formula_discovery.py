"""Typed financial-mathematics contract for LLM-proposed intraday formulas.

The LLM owns the scientific prior and proposes an equation skeleton.  Code owns
syntax, units, semantic alignment, complexity, and whether the claimed
functional form is actually visible in the AST.  Numeric OOS fitting never
occurs here; empirical survival remains the quant evaluator's job.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError


CONTRACT_VERSION = "formula-discovery-v2"

# These names are the canonical ``alpha_semantics.OUTPUTS`` values.  Keeping a
# second set of near-synonyms here used to make gross-markout and passive
# formulas impossible to submit: the thesis had to equal SEMANTIC_PLAN.output,
# while the two validators accepted different spellings.
TARGETS = frozenset({
    "MIDPRICE_MARKOUT", "TAKER_NET_PNL", "PASSIVE_FILL_ADJUSTED_PNL",
})
FUNCTIONAL_FORMS = frozenset({
    "MONOTONE", "REVERSAL", "INTERACTION", "STATE_CONDITIONAL",
    "CROSS_SCALE", "DEPTH_DIVERGENCE",
})
COEFFICIENT_POLICIES = frozenset({
    "FIXED_FROM_SOURCE", "PREREGISTERED_NO_OOS_FIT", "STRUCTURE_ONLY",
})
EXPECTED_SIGNS = frozenset({"POSITIVE", "NEGATIVE", "STATE_DEPENDENT"})
DECISION_RULES = frozenset({
    "POSITIVE_SCORE",
    "PREDICTED_MARKOUT_CLEARS_COST",
})
TERM_ROLES = frozenset({
    "PRESSURE", "LIQUIDITY", "STATE", "SCALE", "VOLATILITY",
    "FRESHNESS", "ACTIVITY", "CAPACITY",
})


class FormulaThesisV1(BaseModel):
    """Decoder/runtime schema; AST-dependent invariants are checked by ``assess``."""

    model_config = ConfigDict(extra="forbid")

    target: str
    functional_form: str
    expected_sign: str
    coefficient_policy: str
    decision_rule: str
    terms: dict[str, str]
    identification: str = Field(min_length=20)


def _text(value, name: str) -> str:
    result = str(value or "").strip().upper()
    if not result:
        raise ValueError(f"FORMULA_THESIS.{name} is required")
    return result


def assess(thesis, *, candidate: dict, semantic_plan: dict, grammar) -> dict:
    """Validate an LLM equation thesis against the executable AST."""
    profile = {
        "fields": sorted(grammar.fields_of(candidate)),
        "operators": sorted(grammar.operators_of(candidate)),
        "clocks_seconds": sorted(grammar.clocks_of(candidate)),
        "complexity_nodes": grammar.count_nodes(candidate),
        "output_unit": grammar.unit_of(candidate),
    }
    if thesis in (None, ""):
        raise ValueError("FORMULA_THESIS is required for an intraday AST_READY lead")
    if not isinstance(thesis, dict):
        raise ValueError("FORMULA_THESIS must be a JSON object")
    try:
        thesis = FormulaThesisV1.model_validate(thesis).model_dump()
    except ValidationError as exc:
        raise ValueError(f"FORMULA_THESIS schema violation: {exc}") from exc

    target = _text(thesis.get("target"), "target")
    form = _text(thesis.get("functional_form"), "functional_form")
    sign = _text(thesis.get("expected_sign"), "expected_sign")
    coefficient_policy = _text(
        thesis.get("coefficient_policy"), "coefficient_policy")
    decision_rule = _text(thesis.get("decision_rule"), "decision_rule")
    for value, allowed, name in (
        (target, TARGETS, "target"),
        (form, FUNCTIONAL_FORMS, "functional_form"),
        (sign, EXPECTED_SIGNS, "expected_sign"),
        (coefficient_policy, COEFFICIENT_POLICIES, "coefficient_policy"),
        (decision_rule, DECISION_RULES, "decision_rule"),
    ):
        if value not in allowed:
            raise ValueError(f"FORMULA_THESIS.{name}={value!r} is not controlled")
    if target != str(semantic_plan.get("output") or "").upper():
        raise ValueError("FORMULA_THESIS.target must equal SEMANTIC_PLAN.output")

    # A target measured in basis points cannot be represented by a bare,
    # dimensionless pressure score.  The old contract allowed that mismatch and
    # the evaluator then interpreted every positive tick as an executable trade.
    # Keep the LLM on equation structure; deterministic execution policy decides
    # whether the predicted move clears spread and statutory round-trip costs.
    if profile["output_unit"] != "BPS":
        raise ValueError(
            "FORMULA_THESIS target is measured in BPS, so the AST output unit "
            f"must be BPS, not {profile['output_unit']}")
    pnl_target = target in {
        "TAKER_NET_PNL", "PASSIVE_FILL_ADJUSTED_PNL",
    }
    if pnl_target and decision_rule != "PREDICTED_MARKOUT_CLEARS_COST":
        raise ValueError(
            "net-PnL targets require decision_rule="
            "PREDICTED_MARKOUT_CLEARS_COST")
    execution = str(semantic_plan.get("execution") or "").upper()
    expected_execution = {
        "TAKER_NET_PNL": "TAKER",
        "PASSIVE_FILL_ADJUSTED_PNL": "PASSIVE_FIFO_LOWER_BOUND",
    }.get(target)
    if expected_execution and execution != expected_execution:
        raise ValueError(
            f"FORMULA_THESIS.target={target} requires "
            f"SEMANTIC_PLAN.execution={expected_execution}")

    raw_terms = thesis.get("terms")
    if not isinstance(raw_terms, dict):
        raise ValueError("FORMULA_THESIS.terms must map every AST field to a role")
    terms = {str(field): _text(role, f"terms.{field}")
             for field, role in raw_terms.items()}
    if sorted(terms) != profile["fields"]:
        raise ValueError(
            "FORMULA_THESIS.terms keys must exactly match CANDIDATE_SIGNAL_EXPR fields")
    unknown_roles = sorted(set(terms.values()) - TERM_ROLES)
    if unknown_roles:
        raise ValueError(f"unknown FORMULA_THESIS term roles: {unknown_roles}")
    identification = str(thesis.get("identification") or "").strip()
    if len(identification) < 20:
        raise ValueError("FORMULA_THESIS.identification must be falsifiable and specific")

    operators = set(profile["operators"])
    clocks = profile["clocks_seconds"]
    fields = set(profile["fields"])
    if form == "STATE_CONDITIONAL" and "where" not in operators:
        raise ValueError("STATE_CONDITIONAL thesis requires a where AST node")
    if form == "CROSS_SCALE" and len(clocks) < 2:
        raise ValueError("CROSS_SCALE thesis requires at least two distinct clocks")
    if form == "DEPTH_DIVERGENCE" and not (
            any("_l1" in field for field in fields)
            and any("_l10" in field for field in fields)):
        raise ValueError("DEPTH_DIVERGENCE thesis requires both L1 and L10 fields")
    if form == "INTERACTION" and not (operators & {"mul", "div", "where"}):
        raise ValueError("INTERACTION thesis requires mul, div, or where")
    if form == "REVERSAL" and not (
            operators & {"neg"} or
            str(semantic_plan.get("direction") or "").upper() == "REVERT"):
        raise ValueError("REVERSAL thesis requires neg or Direction=REVERT")

    normalized = {
        "target": target,
        "functional_form": form,
        "expected_sign": sign,
        "coefficient_policy": coefficient_policy,
        "decision_rule": decision_rule,
        "terms": {field: terms[field] for field in sorted(terms)},
        "identification": identification,
    }
    return {
        "formula_discovery_version": CONTRACT_VERSION,
        "formula_contract_complete": True,
        "formula_thesis": normalized,
        "formula_math_profile": profile,
    }


def _selftest() -> None:
    from pathlib import Path
    import sys

    contracts = Path(__file__).resolve().parents[1] / "contracts"
    sys.path.insert(0, str(contracts))
    import intraday_ast_contract as grammar

    expr = {"op": "where", "condition": {"op": "lt", "args": [
        {"op": "field", "field": "spread_bps"},
        {"const": 5, "unit": "BPS"}]}, "then": {
            "op": "mul", "args": [
                {"op": "rolling_mean", "seconds": 30,
                 "arg": {"op": "field", "field": "normalized_quote_ofi"}},
                {"op": "field", "field": "realized_volatility_bps"}]},
        "else": {"const": 0, "unit": "BPS"}}
    result = assess({
        "target": "TAKER_NET_PNL",
        "functional_form": "STATE_CONDITIONAL",
        "expected_sign": "POSITIVE",
        "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
        "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
        "terms": {"spread_bps": "LIQUIDITY",
                  "normalized_quote_ofi": "PRESSURE",
                  "realized_volatility_bps": "VOLATILITY"},
        "identification": "Pressure must predict positive net markout only in tight spreads.",
    }, candidate=expr, semantic_plan={"output": "TAKER_NET_PNL",
                                      "execution": "TAKER"}, grammar=grammar)
    assert result["formula_contract_complete"]


if __name__ == "__main__":
    _selftest()
    print("formula_discovery: typed equation contract passed")
