"""Deterministic, typed population search for intraday microstructure ASTs.

This module is deliberately a *candidate generator*, not an alpha oracle.  It
turns contract-valid seeds and adaptive-screening outcomes into a diverse batch
of executable formulas.  Every emitted candidate remains adaptively selected,
has no promotion authority, and must be evaluated by the governed backtest and
forward protocol.

The engine has no LLM dependency.  An LLM may contribute seeds through
``FormulaSeed.from_llm``; generation, type checking, lineage, failure-memory
avoidance, and deduplication are deterministic code paths.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from pathlib import Path
import random
import re
import sys
from time import perf_counter
from typing import Iterable, Mapping, Sequence


_CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
if str(_CONTRACTS) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS))

import intraday_ast_contract as grammar  # noqa: E402


ENGINE_VERSION = "intraday-formula-evolution-v2"
ADAPTIVE_EVIDENCE_SCOPES = frozenset({
    "ADAPTIVE_SCREENING", "CALIBRATION", "PRIMARY_DISCOVERY",
})
LOCKBOX_EVIDENCE_SCOPES = frozenset({
    "FORWARD", "FORWARD_LOCKBOX", "FINAL_HOLDOUT", "PROMOTION",
})
SURVIVOR_OUTCOMES = frozenset({
    "SURVIVED", "PROMOTED", "SUBMIT_TO_QA", "NET_SURVIVOR",
})
FAILED_OUTCOMES = frozenset({
    "FAILED", "REJECT", "REJECTED", "KILLED", "DEMOTED", "GATE_HOLD",
    "FUTILITY_GATE_REJECTED",
})

# These values are structural search choices, not fitted coefficients.  They
# remain fixed for an entire generation and are recorded on the batch.
DEFAULT_TIMEFRAMES_SECONDS = (2, 5, 10, 30, 60, 300, 600)

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_FIELD_FAMILIES = {
    "BOOK_PRESSURE": frozenset({
        "queue_imbalance_l1", "queue_imbalance_l10",
        "microprice_offset_bps", "depth_imbalance_slope",
    }),
    "QUOTE_FLOW": frozenset({
        "quote_event_ofi", "normalized_quote_ofi",
        "multi_level_quote_ofi_l10", "normalized_multi_level_quote_ofi_l10",
        "quote_ofi_depth_divergence", "normalized_quote_ofi_per_event",
        "quote_ofi_per_trade_volume",
    }),
    "TRADE_FLOW": frozenset({
        "trade_flow_imbalance", "signed_trade_volume",
        "trade_side_known_ratio",
    }),
    "LIQUIDITY": frozenset({
        "spread_bps", "bid_depth_l1", "ask_depth_l1", "book_depth_l1",
        "book_depth_l10", "quote_age_ms",
    }),
    "ACTIVITY": frozenset({
        "trade_volume", "trade_count", "quote_count", "trade_intensity",
    }),
    "VOLATILITY": frozenset({"realized_volatility_bps"}),
}

_DIRECTIONAL_FIELDS = frozenset().union(
    _FIELD_FAMILIES["BOOK_PRESSURE"],
    _FIELD_FAMILIES["QUOTE_FLOW"],
    _FIELD_FAMILIES["TRADE_FLOW"],
) - {"trade_side_known_ratio"}

_REGIME_CONDITIONS = (
    ("spread_bps", "lt", 8.0),
    ("quote_age_ms", "lt", 1_500.0),
    ("trade_side_known_ratio", "gt", 0.5),
    ("realized_volatility_bps", "lt", 25.0),
)


def _stable_int(*parts: object) -> int:
    payload = "|".join(str(part) for part in parts)
    return int(hashlib.sha256(payload.encode()).hexdigest()[:16], 16)


def _optional_sha256(value: object, *, field_name: str) -> str:
    """Normalize a supplied durable fingerprint without inventing content."""
    text = str(value or "").strip()
    if not text:
        return ""
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{field_name} must be a 64-character hex digest")
    return text.lower()


def _candidate_identity(value: object, *, observation_id: object) -> str:
    """Return a durable identity, with an observation-scoped legacy fallback.

    Governed adaptive rows carry the persisted 64-hex candidate identity.  Old
    observations predate that contract, so they get an observation-namespaced
    digest rather than falling back to the AST fingerprint.  Consequently two
    economic candidates that happen to share an AST can never retire each
    other merely because their formulas look alike.
    """
    supplied = str(value or "").strip()
    if supplied:
        return _optional_sha256(
            supplied, field_name="candidate_identity_fingerprint")
    observation = str(observation_id or "").strip()
    if not observation:
        raise ValueError(
            "candidate_identity_fingerprint or observation_id is required")
    return hashlib.sha256(
        f"legacy-observation-v1:{observation}".encode()).hexdigest()


def _source_lead_ids(values: object) -> tuple[str, ...]:
    """Normalize already-verified lead provenance, preserving stable order."""
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, Sequence):
        raise ValueError("source_lead_ids must be a sequence of strings")
    normalized: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            raise ValueError("source_lead_ids cannot contain an empty id")
        if text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _source_contract_fingerprints(values: object) -> tuple[str, ...]:
    """Normalize exact executable source-contract identities.

    Lead UUIDs are citations and may have aliases.  This identity is the
    genetic-parent boundary used for retirement and crossover review.
    """
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, Sequence):
        raise ValueError(
            "source_contract_fingerprints must be a sequence of SHA-256 values")
    normalized = {
        _optional_sha256(value, field_name="source_contract_fingerprint")
        for value in values
    }
    normalized.discard("")
    return tuple(sorted(normalized))


def _masked_shape(node):
    """Match the parameter-insensitive shape used by intraday experience."""
    if isinstance(node, list):
        return [_masked_shape(value) for value in node]
    if not isinstance(node, dict):
        return node
    return {
        key: "#" if key in {"const", "seconds"} else _masked_shape(value)
        for key, value in sorted(node.items())
    }


def subtree_shape_fingerprint(node: dict) -> str:
    payload = json.dumps(
        _masked_shape(node), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def subtree_shape_fingerprints(expr: dict, *, min_nodes: int = 2
                               ) -> frozenset[str]:
    """Return every non-trivial subtree shape, including boolean predicates."""
    root = grammar.parse(expr)
    found: set[str] = set()

    def walk(node: dict) -> None:
        if grammar.count_nodes(node) >= min_nodes:
            found.add(subtree_shape_fingerprint(node))
        for key in ("arg", "condition", "then", "else"):
            child = node.get(key)
            if isinstance(child, dict):
                walk(child)
        for child in node.get("args") or ():
            if isinstance(child, dict):
                walk(child)

    walk(root)
    return frozenset(found)


@dataclass(frozen=True)
class FormulaSeed:
    """A formula proposed by an LLM, memory archive, or deterministic caller."""

    expression: dict
    seed_id: str
    source: str = "MANUAL"
    economic_mechanism: str = "UNSPECIFIED"
    generation: int = 0
    semantic_plan_fingerprint: str = ""
    source_contract_fingerprint: str = ""
    source_lead_ids: tuple[str, ...] = ()

    @classmethod
    def from_llm(cls, candidate: Mapping, *, seed_id: str,
                 economic_mechanism: str = "") -> "FormulaSeed":
        expression = candidate.get("intraday_signal_expr", candidate)
        if not isinstance(expression, dict):
            raise ValueError("LLM candidate must contain an AST object")
        return cls(
            expression=deepcopy(expression), seed_id=str(seed_id), source="LLM",
            economic_mechanism=(str(economic_mechanism).strip()
                                or "LLM_PROPOSED"),
        )


@dataclass(frozen=True)
class FormulaOutcome:
    """Measured search memory; never independent promotion evidence.

    ``search_score`` must be a preregistered scalar from an adaptive discovery
    screen (for example a cost-net multi-objective rank).  The engine neither
    computes nor modifies it.  Lockbox/final-holdout rows are retained for audit
    but are never eligible for parent selection.
    """

    expression: dict
    outcome: str
    observation_id: str
    search_score: float | None = None
    evidence_scope: str = "ADAPTIVE_SCREENING"
    economic_mechanism: str = "UNSPECIFIED"
    generation: int = 0
    lesson_codes: tuple[str, ...] = ()
    diagnostics: Mapping[str, float | int | str | None] = field(
        default_factory=dict)
    observed_at: str = ""
    candidate_identity_fingerprint: str = ""
    semantic_plan_fingerprint: str = ""
    economic_family_id: str = ""
    source_lead_ids: tuple[str, ...] = ()
    source_contract_fingerprints: tuple[str, ...] = ()
    root_lineage_id: str = ""
    exposure_fingerprint: str = ""

    @classmethod
    def from_result_row(cls, row: Mapping, *, search_score: float | None = None,
                        evidence_scope: str = "ADAPTIVE_SCREENING"
                        ) -> "FormulaOutcome":
        """Adapt a governed result row without inferring or recomputing fitness."""
        expression = (row.get("intraday_signal_expr") or row.get("expression")
                      or row.get("expr"))
        if not isinstance(expression, dict):
            raise ValueError("result row has no intraday AST")
        code_values: list[object] = []
        for key in ("lesson_codes", "failed_criteria", "statuses"):
            raw = row.get(key) or ()
            if isinstance(raw, str):
                raw = raw.replace("|", ",").split(",")
            code_values.extend(raw)
        codes = tuple(sorted({str(code).strip().upper()
                              for code in code_values if str(code).strip()}))
        summary = row.get("oos_summary")
        summary = summary if isinstance(summary, Mapping) else {}
        lineage = row.get("lineage")
        lineage = lineage if isinstance(lineage, Mapping) else {}
        diagnostics = dict(row.get("diagnostics") or {})
        for key in ("calibration_observations", "min_cost_hurdle_bps",
                    "max_calibrated_markout_bps"):
            if key in row:
                diagnostics.setdefault(key, row[key])
            elif key in summary:
                diagnostics.setdefault(key, summary[key])
        return cls(
            expression=deepcopy(expression),
            outcome=str(row.get("outcome") or row.get("decision")
                        or row.get("status") or "UNRESOLVED"),
            observation_id=str(row.get("observation_id")
                               or row.get("experiment_id")
                               or row.get("id") or "UNKNOWN"),
            search_score=search_score, evidence_scope=evidence_scope,
            economic_mechanism=str(row.get("economic_mechanism")
                                   or "UNSPECIFIED"),
            generation=int(row.get("generation") or 0), lesson_codes=codes,
            diagnostics=diagnostics,
            observed_at=str(row.get("observed_at") or row.get("created_at")
                            or row.get("decided_at") or ""),
            candidate_identity_fingerprint=str(
                row.get("candidate_identity_fingerprint")
                or lineage.get("candidate_identity_fingerprint") or ""),
            semantic_plan_fingerprint=str(
                row.get("semantic_plan_fingerprint")
                or lineage.get("semantic_plan_fingerprint") or ""),
            economic_family_id=str(
                row.get("economic_family_id")
                or lineage.get("economic_family_id") or ""),
            source_lead_ids=_source_lead_ids(
                row.get("source_lead_ids") or ()),
            source_contract_fingerprints=_source_contract_fingerprints(
                row.get("source_contract_fingerprints") or ()),
            root_lineage_id=str(
                row.get("root_lineage_id")
                or lineage.get("root_lineage_id") or ""),
            exposure_fingerprint=str(
                row.get("exposure_fingerprint") or ""),
        )


@dataclass(frozen=True)
class FailedSubtree:
    """A repeated losing shape used as a search-prior veto, not a causal fact."""

    subtree_fingerprint: str
    support: int = 1
    reason: str = "REPEATED_ADAPTIVE_FAILURE"

    @classmethod
    def from_record(cls, record: Mapping) -> "FailedSubtree":
        fingerprint = str(record.get("subtree_fingerprint") or "").strip()
        if not fingerprint and isinstance(record.get("expression"), dict):
            fingerprint = subtree_shape_fingerprint(record["expression"])
        if not fingerprint and isinstance(record.get("shape"), dict):
            fingerprint = subtree_shape_fingerprint(record["shape"])
        if not fingerprint:
            raise ValueError("failed-subtree record has no fingerprint or AST")
        return cls(
            subtree_fingerprint=fingerprint,
            support=max(1, int(record.get("support") or
                               record.get("losing_support") or 1)),
            reason=str(record.get("reason") or "REPEATED_ADAPTIVE_FAILURE"),
        )


@dataclass(frozen=True)
class EconomicNiche:
    """Behavior descriptor for quality-diversity archives, not a fitness score."""

    pressure_source: str
    mechanism: str
    regime: str
    clock_bucket: str
    output_unit: str

    @property
    def key(self) -> str:
        return "/".join((self.pressure_source, self.mechanism, self.regime,
                         self.clock_bucket, self.output_unit))


@dataclass(frozen=True)
class EvolutionCandidate:
    candidate_id: str
    expression: dict
    fingerprint: str
    shape_fingerprint: str
    niche: EconomicNiche
    arm: str
    origin: str
    operation: str
    parent_fingerprints: tuple[str, ...]
    parent_seed_ids: tuple[str, ...]
    parent_source_contract_fingerprints: tuple[str, ...]
    generation: int
    economic_mechanism: str
    adaptive_selection: bool = True
    promotion_authority: bool = False
    requires_preregistered_evaluation: bool = True

    def to_dict(self) -> dict:
        result = asdict(self)
        result["niche"]["key"] = self.niche.key
        return result


@dataclass(frozen=True)
class ThroughputKPI:
    requested: int
    attempted: int
    contract_valid: int
    emitted: int
    shortfall: int
    elapsed_seconds: float
    candidates_per_second: float
    yield_rate: float
    rejection_counts: dict[str, int]
    emitted_by_arm: dict[str, int]


@dataclass(frozen=True)
class EvolutionBatch:
    engine_version: str
    deterministic_seed: int
    generation: int
    candidates: tuple[EvolutionCandidate, ...]
    kpi: ThroughputKPI
    audit: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "engine_version": self.engine_version,
            "deterministic_seed": self.deterministic_seed,
            "generation": self.generation,
            "candidates": [candidate.to_dict()
                           for candidate in self.candidates],
            "kpi": asdict(self.kpi),
            "audit": deepcopy(self.audit),
        }


@dataclass(frozen=True)
class EvolutionConfig:
    deterministic_seed: int = 20260818
    population_size: int = 32
    exploration_fraction: float = 0.5
    max_attempt_multiplier: int = 40
    timeframes_seconds: tuple[int, ...] = DEFAULT_TIMEFRAMES_SECONDS
    completed_second_only: bool = True
    execution: str = "TAKER"
    min_failed_subtree_support: int = 2
    enable_crossover: bool = True

    def __post_init__(self) -> None:
        if self.population_size < 1:
            raise ValueError("population_size must be positive")
        if not 0.0 <= self.exploration_fraction <= 1.0:
            raise ValueError("exploration_fraction must be in [0, 1]")
        if self.max_attempt_multiplier < 1:
            raise ValueError("max_attempt_multiplier must be positive")
        cleaned = tuple(sorted({int(value) for value in self.timeframes_seconds}))
        if not cleaned or any(value < grammar.MIN_SECONDS or
                              value > grammar.MAX_SECONDS for value in cleaned):
            raise ValueError("timeframes_seconds violate intraday AST limits")
        object.__setattr__(self, "timeframes_seconds", cleaned)


@dataclass(frozen=True)
class _Parent:
    expression: dict
    fingerprint: str
    shape_fingerprint: str
    seed_id: str
    candidate_identity_fingerprint: str
    semantic_plan_fingerprint: str
    economic_family_id: str
    source_lead_ids: tuple[str, ...]
    source_contract_fingerprints: tuple[str, ...]
    root_lineage_id: str
    exposure_fingerprint: str
    source: str
    mechanism: str
    generation: int
    score: float | None = None


def _clock_bucket(clocks: set[int]) -> str:
    if not clocks:
        return "EVENT_LEVEL"
    longest = max(clocks)
    if longest <= 5:
        return "1_5S"
    if longest <= 30:
        return "6_30S"
    if longest <= 300:
        return "31_300S"
    return "301_3600S"


def describe_economic_niche(expr: dict) -> EconomicNiche:
    parsed = grammar.parse(expr)
    fields = grammar.fields_of(parsed)
    operators = grammar.operators_of(parsed)
    sources = [name for name in ("BOOK_PRESSURE", "QUOTE_FLOW", "TRADE_FLOW")
               if fields & _FIELD_FAMILIES[name]]
    pressure = "+".join(sources) if sources else "NON_DIRECTIONAL"
    if "where" in operators:
        mechanism = "STATE_CONDITIONAL"
    elif len(grammar.clocks_of(parsed)) >= 2:
        mechanism = "CROSS_SCALE"
    elif "delta" in operators or "lag" in operators:
        mechanism = "CHANGE"
    elif operators & {"mul", "div"}:
        mechanism = "INTERACTION"
    elif operators & grammar.TEMPORAL_OPS:
        mechanism = "PERSISTENCE"
    else:
        mechanism = "LEVEL"
    regimes = [name for name in ("LIQUIDITY", "ACTIVITY", "VOLATILITY")
               if fields & _FIELD_FAMILIES[name]]
    return EconomicNiche(
        pressure_source=pressure,
        mechanism=mechanism,
        regime="+".join(regimes) if regimes else "UNCONDITIONED",
        clock_bucket=_clock_bucket(grammar.clocks_of(parsed)),
        output_unit=grammar.unit_of(parsed),
    )


def _diagnostic_number(outcome: FormulaOutcome, key: str) -> float | None:
    try:
        value = outcome.diagnostics.get(key)
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (AttributeError, TypeError, ValueError):
        return None


def _cost_infeasible_failure(outcome: FormulaOutcome) -> bool:
    """Whether observed signal capacity cannot clear the measured cost hurdle.

    This comparison only decides whether to stop breeding a failed family.  It
    never manufactures a score, adjusts a coefficient, or weakens the hurdle.
    """
    codes = {str(code).strip().upper() for code in outcome.lesson_codes}
    if "NO_COST_FEASIBLE_ENTRY" not in codes:
        return False
    hurdle = _diagnostic_number(outcome, "min_cost_hurdle_bps")
    markout = _diagnostic_number(outcome, "max_calibrated_markout_bps")
    return hurdle is None or markout is None or markout <= hurdle


def _direction_inversion_eligible(outcome: FormulaOutcome) -> bool:
    codes = {str(code).strip().upper() for code in outcome.lesson_codes}
    return ("NON_POSITIVE_DIRECTIONAL_RELATION" in codes
            and not _cost_infeasible_failure(outcome))


def _zero(unit: str) -> dict:
    return {"const": 0.0, "unit": unit}


def _field(name: str) -> dict:
    return {"op": "field", "field": name}


def _walk_replacements(node: dict, predicate, replacements) -> list[dict]:
    """Return copies with exactly one matching subtree replaced."""
    results: list[dict] = []
    if predicate(node):
        results.extend(deepcopy(value) for value in replacements(node))
    for key in ("arg", "condition", "then", "else"):
        child = node.get(key)
        if not isinstance(child, dict):
            continue
        for replacement in _walk_replacements(child, predicate, replacements):
            candidate = deepcopy(node)
            candidate[key] = replacement
            results.append(candidate)
    for index, child in enumerate(node.get("args") or ()):
        if not isinstance(child, dict):
            continue
        for replacement in _walk_replacements(child, predicate, replacements):
            candidate = deepcopy(node)
            candidate["args"][index] = replacement
            results.append(candidate)
    return results


def _typed_mutations(parent: _Parent, config: EvolutionConfig,
                     rng: random.Random) -> list[tuple[str, dict]]:
    expr = parent.expression
    unit = grammar.unit_of(expr)
    variants: list[tuple[str, dict]] = []
    clocks = config.timeframes_seconds

    # Root transforms create different economic shapes; constants and clocks are
    # fixed search coordinates and never optimized against holdout performance.
    variants.append(
        ("DIRECTION_INVERSION", {"op": "neg", "arg": deepcopy(expr)}))
    if unit == grammar.RATIO:
        variants.append((
            "SIGNED_STATE", {"op": "sign", "arg": deepcopy(expr)}))
    for op in ("rolling_mean", "ewma", "delta"):
        for seconds in clocks:
            variants.append((
                f"{op.upper()}_{seconds}S",
                {"op": op, "arg": deepcopy(expr), "seconds": seconds},
            ))
    if unit == grammar.RATIO:
        for seconds in clocks:
            variants.append((
                f"ROLLING_ZSCORE_{seconds}S",
                {"op": "rolling_zscore", "arg": deepcopy(expr),
                 "seconds": seconds},
            ))
    for short, long in zip(clocks, clocks[1:]):
        variants.append((
            f"CROSS_SCALE_{short}S_{long}S",
            {"op": "sub", "args": [
                {"op": "rolling_mean", "arg": deepcopy(expr),
                 "seconds": short},
                {"op": "rolling_mean", "arg": deepcopy(expr),
                 "seconds": long},
            ]},
        ))

    for condition_field, comparison, threshold in _REGIME_CONDITIONS:
        if (config.completed_second_only and condition_field not in
                grammar.COMPLETED_SECOND_REPLAYABLE_FIELDS):
            continue
        condition_unit = grammar.FIELDS[condition_field]
        variants.append((
            f"STATE_GATE_{condition_field.upper()}",
            {"op": "where", "condition": {
                "op": comparison, "args": [
                    _field(condition_field),
                    {"const": threshold, "unit": condition_unit},
                ]}, "then": deepcopy(expr), "else": _zero(unit)},
        ))

    allowed_fields = (grammar.COMPLETED_SECOND_REPLAYABLE_FIELDS
                      if config.completed_second_only else frozenset(grammar.FIELDS))
    same_unit_fields = sorted(
        name for name in allowed_fields if grammar.FIELDS[name] == unit)
    for other in same_unit_fields:
        if other in grammar.fields_of(expr):
            continue
        for op in ("add", "sub", "min", "max"):
            variants.append((
                f"{op.upper()}_{other.upper()}",
                {"op": op, "args": [deepcopy(expr), _field(other)]},
            ))

    ratio_interactions = sorted(
        name for name in allowed_fields
        if grammar.FIELDS[name] == grammar.RATIO and name not in grammar.fields_of(expr))
    for other in ratio_interactions:
        variants.append((
            f"INTERACT_{other.upper()}",
            {"op": "mul", "args": [deepcopy(expr), _field(other)]},
        ))

    # Same-unit field replacement explores a new observable without violating a
    # parent operator's dimensional contract.
    def field_replacements(node: dict) -> list[dict]:
        old = node["field"]
        old_unit = grammar.FIELDS[old]
        return [_field(name) for name in sorted(allowed_fields)
                if grammar.FIELDS[name] == old_unit and name != old]

    for candidate in _walk_replacements(
            expr, lambda node: node.get("op") == "field", field_replacements):
        variants.append(("SAME_UNIT_FIELD_SWAP", candidate))

    # Change one existing temporal operator while preserving its explicit clock.
    def temporal_replacements(node: dict) -> list[dict]:
        return [{"op": op, "arg": deepcopy(node["arg"]),
                 "seconds": node["seconds"]}
                for op in sorted(grammar.TEMPORAL_OPS) if op != node["op"]]

    for candidate in _walk_replacements(
            expr, lambda node: node.get("op") in grammar.TEMPORAL_OPS,
            temporal_replacements):
        variants.append(("TEMPORAL_OPERATOR_SWAP", candidate))

    # Randomness changes only proposal order; the proposal set is deterministic.
    rng.shuffle(variants)
    return variants


def _typed_crossovers(left: _Parent, right: _Parent,
                      rng: random.Random) -> list[tuple[str, dict]]:
    if left.fingerprint == right.fingerprint:
        return []
    a, b = left.expression, right.expression
    unit_a, unit_b = grammar.unit_of(a), grammar.unit_of(b)
    variants: list[tuple[str, dict]] = []
    if unit_a == unit_b:
        for op in ("add", "sub", "min", "max"):
            variants.append((
                f"CROSSOVER_{op.upper()}",
                {"op": op, "args": [deepcopy(a), deepcopy(b)]},
            ))
        variants.append((
            "CROSSOVER_CONFIRMATION",
            {"op": "where", "condition": {"op": "gt", "args": [
                deepcopy(b), _zero(unit_b)]},
             "then": deepcopy(a), "else": _zero(unit_a)},
        ))
        variants.append((
            "CROSSOVER_RELATIVE_VALUE",
            {"op": "div", "args": [deepcopy(a), deepcopy(b)]},
        ))
    if unit_a == grammar.RATIO:
        variants.append((
            "CROSSOVER_RATIO_SCALE", {"op": "mul", "args": [
                deepcopy(a), deepcopy(b)]},
        ))
    elif unit_b == grammar.RATIO:
        variants.append((
            "CROSSOVER_RATIO_SCALE", {"op": "mul", "args": [
                deepcopy(a), deepcopy(b)]},
        ))
    rng.shuffle(variants)
    return variants


class FormulaEvolutionEngine:
    """Generate a governed, quality-diverse batch from seeds and memory."""

    def __init__(self, config: EvolutionConfig | None = None):
        self.config = config or EvolutionConfig()

    @staticmethod
    def _parse_seed(seed: FormulaSeed) -> _Parent:
        expr = grammar.parse(seed.expression)
        seed_id = str(seed.seed_id).strip()
        if not seed_id:
            raise ValueError("seed_id is required")
        semantic = _optional_sha256(
            seed.semantic_plan_fingerprint,
            field_name="semantic_plan_fingerprint")
        declared_contract = _optional_sha256(
            seed.source_contract_fingerprint,
            field_name="source_contract_fingerprint")
        if not declared_contract:
            # Manual callers may not yet have a persisted lead contract.  Keep
            # the fallback semantic-aware and deterministic; governed breeder
            # callers always supply the exact baseline-aware contract digest.
            declared_contract = hashlib.sha256(json.dumps({
                "candidate_ast": hashlib.sha256(json.dumps(
                    expr, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest(),
                "semantic_plan": semantic or None,
                "contract_scope": "UNPERSISTED_SEED_CONTRACT_V1",
            }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        source_ids = _source_lead_ids(seed.source_lead_ids) or (seed_id,)
        return _Parent(
            expression=expr, fingerprint=grammar.fingerprint(expr),
            shape_fingerprint=grammar.shape_fingerprint(expr),
            seed_id=seed_id,
            candidate_identity_fingerprint=_candidate_identity(
                "", observation_id=f"seed:{seed_id}"),
            semantic_plan_fingerprint=semantic, economic_family_id="",
            source_lead_ids=source_ids,
            source_contract_fingerprints=(declared_contract,),
            root_lineage_id="",
            exposure_fingerprint="", source=str(seed.source).upper(),
            mechanism=str(seed.economic_mechanism or "UNSPECIFIED"),
            generation=int(seed.generation),
        )

    @staticmethod
    def _parse_outcome(outcome: FormulaOutcome) -> _Parent:
        expr = grammar.parse(outcome.expression)
        score = outcome.search_score
        if score is not None and (isinstance(score, bool) or
                                  not math.isfinite(float(score))):
            raise ValueError("search_score must be finite numeric or None")
        observation_id = str(outcome.observation_id).strip()
        candidate_identity = _candidate_identity(
            outcome.candidate_identity_fingerprint,
            observation_id=observation_id)
        return _Parent(
            expression=expr, fingerprint=grammar.fingerprint(expr),
            shape_fingerprint=grammar.shape_fingerprint(expr),
            seed_id=observation_id,
            candidate_identity_fingerprint=candidate_identity,
            semantic_plan_fingerprint=_optional_sha256(
                outcome.semantic_plan_fingerprint,
                field_name="semantic_plan_fingerprint"),
            economic_family_id=str(outcome.economic_family_id or "").strip(),
            source_lead_ids=_source_lead_ids(outcome.source_lead_ids),
            source_contract_fingerprints=_source_contract_fingerprints(
                outcome.source_contract_fingerprints),
            root_lineage_id=str(outcome.root_lineage_id or "").strip(),
            exposure_fingerprint=_optional_sha256(
                outcome.exposure_fingerprint,
                field_name="exposure_fingerprint"),
            source="OUTCOME_MEMORY",
            mechanism=str(outcome.economic_mechanism or "UNSPECIFIED"),
            generation=int(outcome.generation),
            score=None if score is None else float(score),
        )

    def generate_population(
            self, *, seeds: Sequence[FormulaSeed],
            outcomes: Sequence[FormulaOutcome] = (),
            failed_subtrees: Sequence[FailedSubtree | Mapping] = (),
            known_expressions: Sequence[dict] = (),
            known_exact_fingerprints: Iterable[str] = (),
            known_shape_fingerprints: Iterable[str] = (),
            population_size: int | None = None,
            generation: int = 1) -> EvolutionBatch:
        """Generate one deterministic population.

        Exact and parameter-insensitive shape deduplication apply against both
        supplied archives and this batch.  Failed subtree memory is a hard search
        veto only after ``min_failed_subtree_support``; it never changes measured
        evidence or promotes another candidate by subtraction.
        """
        started = perf_counter()
        size = self.config.population_size if population_size is None else int(
            population_size)
        if size < 1:
            raise ValueError("population_size must be positive")
        if not seeds and not outcomes:
            raise ValueError("at least one formula seed or outcome is required")

        rng = random.Random(_stable_int(
            self.config.deterministic_seed, generation, size))
        rejection = Counter()
        parsed_seeds: list[_Parent] = []
        for seed in seeds:
            try:
                parent = self._parse_seed(seed)
                if self.config.completed_second_only:
                    grammar.validate_completed_second_candidate(
                        parent.expression, execution=self.config.execution)
                parsed_seeds.append(parent)
            except (TypeError, ValueError, grammar.IntradayExprError):
                rejection["INVALID_SEED"] += 1
        parsed_seeds.sort(key=lambda parent: (
            parent.source_contract_fingerprints,
            parent.seed_id,
            parent.fingerprint,
        ))

        parsed_outcomes: list[tuple[FormulaOutcome, _Parent]] = []
        for outcome in outcomes:
            try:
                parent = self._parse_outcome(outcome)
                if self.config.completed_second_only:
                    grammar.validate_completed_second_candidate(
                        parent.expression, execution=self.config.execution)
                parsed_outcomes.append((outcome, parent))
            except (TypeError, ValueError, grammar.IntradayExprError):
                rejection["INVALID_OUTCOME"] += 1

        if not parsed_seeds and not parsed_outcomes:
            raise ValueError("no contract-valid seed or outcome remains")

        known_exact = {str(value) for value in known_exact_fingerprints}
        known_shapes = {str(value) for value in known_shape_fingerprints}
        for expression in known_expressions:
            parsed = grammar.parse(expression)
            known_exact.add(grammar.fingerprint(parsed))
            known_shapes.add(grammar.shape_fingerprint(parsed))
        # Every measured formula is already spent trial material.  It may parent
        # a child, but cannot re-enter the batch under a new label or clock.
        for _, parent in parsed_outcomes:
            known_exact.add(parent.fingerprint)
            known_shapes.add(parent.shape_fingerprint)

        blocked_subtrees: set[str] = set()
        for item in failed_subtrees:
            record = item if isinstance(item, FailedSubtree) else \
                FailedSubtree.from_record(item)
            if record.support >= self.config.min_failed_subtree_support:
                blocked_subtrees.add(record.subtree_fingerprint)

        # Candidate retirement follows the newest result for one durable
        # candidate *within one root lineage*.  AST fingerprints are deliberately
        # absent from this key: the same formula may represent different label
        # horizons, execution contracts, or independently preregistered roots.
        # ISO-8601 timestamps sort chronologically; legacy rows without a stamp
        # use caller order as the explicit deterministic tie-break (later wins).
        latest_by_identity: dict[
            tuple[str, str], tuple[tuple[int, str, int], FormulaOutcome, _Parent]
        ] = {}
        for index, (outcome, parent) in enumerate(parsed_outcomes):
            status = str(outcome.outcome).upper()
            if status not in SURVIVOR_OUTCOMES | FAILED_OUTCOMES:
                # A later bookkeeping/no-evidence row must not erase the last
                # measured terminal state for this candidate and root.
                continue
            stamp = str(outcome.observed_at or "").strip()
            key = (1 if stamp else 0, stamp, index)
            identity_key = (
                parent.candidate_identity_fingerprint,
                parent.root_lineage_id,
            )
            previous = latest_by_identity.get(identity_key)
            if previous is None or key > previous[0]:
                latest_by_identity[identity_key] = (
                    key, outcome, parent)
        terminal_rows = [(outcome, parent)
                         for _, outcome, parent in latest_by_identity.values()]

        # Repeated-subtree support and survivor exemptions are based only on the
        # active terminal state of each identity/root pair.  Superseded F1 rows
        # cannot keep vetoing an F2 survivor (or exempting a later failure).
        losing_shape_support = Counter()
        surviving_shapes: set[str] = set()
        for outcome, parent in terminal_rows:
            status = str(outcome.outcome).upper()
            shapes = subtree_shape_fingerprints(parent.expression)
            if status in SURVIVOR_OUTCOMES:
                surviving_shapes.update(shapes)
            elif status in FAILED_OUTCOMES:
                losing_shape_support.update(shapes)
        blocked_subtrees.update(
            shape for shape, support in losing_shape_support.items()
            if support >= self.config.min_failed_subtree_support
            and shape not in surviving_shapes
        )

        # A source seed is retired only by exact, verified lead provenance.  An
        # unrelated candidate sharing its AST (possibly at another horizon)
        # cannot kill it.
        failed_source_lead_ids = {
            lead_id
            for outcome, parent in terminal_rows
            if str(outcome.outcome).upper() in FAILED_OUTCOMES
            for lead_id in parent.source_lead_ids
        }
        failed_source_contract_fingerprints = {
            contract
            for outcome, parent in terminal_rows
            if str(outcome.outcome).upper() in FAILED_OUTCOMES
            for contract in parent.source_contract_fingerprints
        }
        exploratory = []
        for parent in parsed_seeds:
            if (set(parent.source_contract_fingerprints)
                    & failed_source_contract_fingerprints):
                rejection["FAILED_SOURCE_CONTRACT_RESEED"] += 1
                rejection["FAILED_FORMULA_RESEED"] += 1
            elif parent.seed_id in failed_source_lead_ids:
                rejection["FAILED_FORMULA_RESEED"] += 1
            elif subtree_shape_fingerprints(parent.expression) & blocked_subtrees:
                rejection["FAILED_SUBTREE"] += 1
            else:
                exploratory.append(parent)
        cost_infeasible_identities = {
            (parent.candidate_identity_fingerprint, parent.root_lineage_id)
            for outcome, parent in terminal_rows
            if str(outcome.outcome).upper() in FAILED_OUTCOMES
            and _cost_infeasible_failure(outcome)
        }
        survivor_rows = [
            (outcome, parent) for outcome, parent in terminal_rows
            if str(outcome.outcome).upper() in SURVIVOR_OUTCOMES
            and str(outcome.evidence_scope).upper() in ADAPTIVE_EVIDENCE_SCOPES
            # A later measured capacity proof retires stale positive memory for
            # this candidate/root.  Otherwise an old screen survivor could keep
            # breeding after a 3.98bp signal faced a 23bp executable hurdle.
            and (parent.candidate_identity_fingerprint,
                 parent.root_lineage_id) not in cost_infeasible_identities
        ]
        survivor_rows.sort(key=lambda pair: (
            pair[1].score is None,
            -(pair[1].score if pair[1].score is not None else 0.0),
            pair[1].candidate_identity_fingerprint,
            pair[1].root_lineage_id,
        ))
        exploitative = [parent for _, parent in survivor_rows]
        inversion_rows = [
            (outcome, parent) for outcome, parent in terminal_rows
            if str(outcome.outcome).upper() in FAILED_OUTCOMES
            and str(outcome.evidence_scope).upper() in ADAPTIVE_EVIDENCE_SCOPES
            and _direction_inversion_eligible(outcome)
        ]
        cost_infeasible_failures = sum(
            str(outcome.outcome).upper() in FAILED_OUTCOMES
            and _cost_infeasible_failure(outcome)
            for outcome, _ in terminal_rows
        )
        emitted: list[EvolutionCandidate] = []
        batch_exact: set[str] = set()
        batch_shapes: set[str] = set()
        attempts = 0
        contract_valid = 0
        arm_counts = Counter()
        exploration_target = round(size * self.config.exploration_fraction)
        exploitation_target = size - exploration_target

        def accept(expression: dict, *, arm: str, origin: str, operation: str,
                   parents: tuple[_Parent, ...], mechanism: str,
                   lineage_parents: tuple[_Parent, ...] | None = None) -> bool:
            nonlocal attempts, contract_valid
            attempts += 1
            try:
                parsed = grammar.parse(expression)
                if self.config.completed_second_only:
                    grammar.validate_completed_second_candidate(
                        parsed, execution=self.config.execution)
            except (TypeError, ValueError, grammar.IntradayExprError):
                rejection["TYPE_OR_CONTRACT"] += 1
                return False
            contract_valid += 1
            if not (grammar.fields_of(parsed) & _DIRECTIONAL_FIELDS):
                rejection["NON_DIRECTIONAL"] += 1
                return False
            fingerprint = grammar.fingerprint(parsed)
            shape = grammar.shape_fingerprint(parsed)
            if fingerprint in known_exact or fingerprint in batch_exact:
                rejection["EXACT_DUPLICATE"] += 1
                return False
            if shape in known_shapes or shape in batch_shapes:
                rejection["SHAPE_DUPLICATE"] += 1
                return False
            overlap = subtree_shape_fingerprints(parsed) & blocked_subtrees
            if overlap:
                rejection["FAILED_SUBTREE"] += 1
                return False
            parent_fps = tuple(parent.fingerprint for parent in parents)
            if any(shape == parent.shape_fingerprint for parent in parents):
                rejection["PARAMETER_ONLY_CHILD"] += 1
                return False
            provenance_parents = (
                parents if lineage_parents is None else lineage_parents)
            parent_source_contracts = tuple(sorted({
                contract for parent in provenance_parents
                for contract in parent.source_contract_fingerprints
            }))
            parent_semantics = tuple(sorted({
                parent.semantic_plan_fingerprint
                for parent in provenance_parents
                if parent.semantic_plan_fingerprint
            }))
            parent_candidate_identities = tuple(sorted({
                parent.candidate_identity_fingerprint
                for parent in provenance_parents
                if not parent.source_contract_fingerprints
            }))
            candidate_id = hashlib.sha256(json.dumps({
                "generation": generation, "fingerprint": fingerprint,
                "parents": parent_fps, "operation": operation,
                "parent_source_contracts": parent_source_contracts,
                "parent_semantic_plans": parent_semantics,
                "parent_candidate_identities": parent_candidate_identities,
            }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
            emitted.append(EvolutionCandidate(
                candidate_id=candidate_id, expression=parsed,
                fingerprint=fingerprint, shape_fingerprint=shape,
                niche=describe_economic_niche(parsed), arm=arm, origin=origin,
                operation=operation, parent_fingerprints=parent_fps,
                parent_seed_ids=tuple(dict.fromkeys(
                    lead_id for parent in provenance_parents
                    for lead_id in parent.source_lead_ids)),
                parent_source_contract_fingerprints=parent_source_contracts,
                generation=generation, economic_mechanism=mechanism,
            ))
            batch_exact.add(fingerprint)
            batch_shapes.add(shape)
            arm_counts[arm] += 1
            return True

        # A direction failure may seed exactly one explicit inversion only when
        # the measured signal could clear costs.  A 4 bps signal facing a 23 bps
        # hurdle is abandoned instead of being cosmetically negated or gated.
        for outcome, parent in inversion_rows:
            if len(emitted) >= size or arm_counts["EXPLORATION"] >= exploration_target:
                break
            accept(
                {"op": "neg", "arg": deepcopy(parent.expression)},
                arm="EXPLORATION", origin="FAILURE_MEMORY",
                operation="FAILURE_MODE_DIRECTION_INVERSION",
                parents=(parent,),
                mechanism="DIRECTION_INVERSION:" + parent.mechanism,
            )

        # Untested seeds are useful population members in their own right.  LLM
        # seeds get no score or authority merely by being syntactically valid.
        seed_order = list(exploratory)
        rng.shuffle(seed_order)
        for parent in seed_order:
            if len(emitted) >= size or arm_counts["EXPLORATION"] >= exploration_target:
                break
            accept(parent.expression, arm="EXPLORATION", origin=parent.source,
                   operation="SEED", parents=(), mechanism=parent.mechanism,
                   lineage_parents=(parent,))

        proposal_cache: dict[tuple[str, str], list[tuple[str, dict]]] = {}
        crossover_cache: dict[tuple[str, str], list[tuple[str, dict]]] = {}
        max_attempts = max(size, size * self.config.max_attempt_multiplier)
        cursor = Counter()

        while len(emitted) < size and attempts < max_attempts:
            need_explore = arm_counts["EXPLORATION"] < exploration_target
            need_exploit = arm_counts["EXPLOITATION"] < exploitation_target
            if need_explore and need_exploit:
                arm = "EXPLORATION" if (
                    arm_counts["EXPLORATION"] <= arm_counts["EXPLOITATION"]
                ) else "EXPLOITATION"
            elif need_explore:
                arm = "EXPLORATION"
            elif need_exploit:
                arm = "EXPLOITATION"
            else:
                arm = "EXPLORATION" if len(emitted) % 2 == 0 else "EXPLOITATION"
            pool = exploratory if arm == "EXPLORATION" else exploitative
            if not pool:
                arm = ("EXPLOITATION" if arm == "EXPLORATION"
                       else "EXPLORATION")
                pool = exploratory if arm == "EXPLORATION" else exploitative
            if not pool:
                break

            parent = pool[cursor[(arm, "parent")] % len(pool)]
            cursor[(arm, "parent")] += 1
            key = (arm, parent.fingerprint)
            if key not in proposal_cache:
                proposal_cache[key] = _typed_mutations(parent, self.config, rng)
            proposals = proposal_cache[key]

            # Every third exploitation attempt tries a typed mechanism crossover
            # when a genuinely different parent is available.
            crossover = (self.config.enable_crossover
                         and arm == "EXPLOITATION" and len(pool) > 1
                         and cursor[(arm, "parent")] % 3 == 0)
            parents = (parent,)
            if crossover:
                others = [item for item in pool
                          if item.fingerprint != parent.fingerprint]
                partner = others[cursor[(arm, "partner")] % len(others)]
                cursor[(arm, "partner")] += 1
                cross_key = (parent.fingerprint, partner.fingerprint)
                if cross_key not in crossover_cache:
                    crossover_cache[cross_key] = _typed_crossovers(
                        parent, partner, rng)
                cross = crossover_cache[cross_key]
                index = cursor[(arm, "cross", *cross_key)]
                cursor[(arm, "cross", *cross_key)] += 1
                if index < len(cross):
                    operation, expression = cross[index]
                    parents = (parent, partner)
                elif proposals:
                    index = cursor[(arm, "mutation", parent.fingerprint)]
                    cursor[(arm, "mutation", parent.fingerprint)] += 1
                    operation, expression = proposals[index % len(proposals)]
                else:
                    rejection["NO_TYPED_PROPOSAL"] += 1
                    attempts += 1
                    continue
            elif proposals:
                index = cursor[(arm, "mutation", parent.fingerprint)]
                cursor[(arm, "mutation", parent.fingerprint)] += 1
                operation, expression = proposals[index % len(proposals)]
            else:
                rejection["NO_TYPED_PROPOSAL"] += 1
                attempts += 1
                continue

            accept(expression, arm=arm,
                   origin=("OUTCOME_EVOLUTION"
                           if parent.source == "OUTCOME_MEMORY"
                           else "DETERMINISTIC_EVOLUTION"),
                   operation=operation, parents=parents,
                   mechanism=" + ".join(dict.fromkeys(
                       item.mechanism for item in parents)))

        elapsed = max(perf_counter() - started, 1e-12)
        kpi = ThroughputKPI(
            requested=size, attempted=attempts, contract_valid=contract_valid,
            emitted=len(emitted), shortfall=max(0, size - len(emitted)),
            elapsed_seconds=elapsed,
            candidates_per_second=len(emitted) / elapsed,
            yield_rate=(len(emitted) / attempts if attempts else 0.0),
            rejection_counts=dict(sorted(rejection.items())),
            emitted_by_arm={
                "EXPLORATION": arm_counts["EXPLORATION"],
                "EXPLOITATION": arm_counts["EXPLOITATION"],
            },
        )
        lockbox_ignored = sum(
            str(outcome.evidence_scope).upper() in LOCKBOX_EVIDENCE_SCOPES
            and str(outcome.outcome).upper() in SURVIVOR_OUTCOMES
            for outcome, _ in parsed_outcomes
        )
        audit = {
            "selection_policy": "FIXED_EXPLORATION_EXPLOITATION_SPLIT",
            "exploration_fraction": self.config.exploration_fraction,
            "fixed_timeframes_seconds": list(self.config.timeframes_seconds),
            "exact_dedup": True,
            "shape_dedup": True,
            "failed_subtree_veto_support": self.config.min_failed_subtree_support,
            "blocked_subtree_shapes": len(blocked_subtrees),
            "exploration_parent_count": len(exploratory),
            "exploit_parent_count": len(exploitative),
            "cost_infeasible_families_abandoned": cost_infeasible_failures,
            "failed_source_contracts": len(
                failed_source_contract_fingerprints),
            "direction_inversion_eligible_failures": len(inversion_rows),
            "lockbox_survivors_ignored_for_selection": lockbox_ignored,
            "promotion_authority": False,
            "adaptive_selection": True,
            "fitness_computed_by_engine": False,
            "coefficient_fitting": False,
            "warning": (
                "Generated formulas are adaptive discovery hypotheses. They require "
                "preregistered cost-aware evaluation and untouched forward sessions."
            ),
        }
        return EvolutionBatch(
            engine_version=ENGINE_VERSION,
            deterministic_seed=self.config.deterministic_seed,
            generation=generation, candidates=tuple(emitted), kpi=kpi,
            audit=audit,
        )


__all__ = [
    "ENGINE_VERSION", "EconomicNiche", "EvolutionBatch",
    "EvolutionCandidate", "EvolutionConfig", "FailedSubtree",
    "FormulaEvolutionEngine", "FormulaOutcome", "FormulaSeed",
    "ThroughputKPI", "describe_economic_niche",
    "subtree_shape_fingerprint", "subtree_shape_fingerprints",
]
