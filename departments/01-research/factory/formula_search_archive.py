"""Deterministic quality-diversity core for intraday formula search.

The LLM proposes economically motivated ASTs.  This module owns the mechanical
parts of search: a MAP-Elites niche archive, adaptive-data exposure accounting,
multi-fidelity promotion and process KPIs.  It deliberately has no database,
clock, random-number, or model dependency, so a persisted payload replays to the
same decisions.

``F1`` is screening-only.  It may supply a breeding parent, but it never carries
production-promotion authority.  ``NO_EVIDENCE`` means the search learned that a
candidate/data exposure yielded no usable observations and therefore counts as
an adaptive trial.  ``INFRA_FAILURE`` means no selection-visible market result
was produced; it is retryable and does not increase the effective trial count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "formula-search-archive-v3"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

F0 = "F0_STATIC"
F1 = "F1_SCREEN"
F2 = "F2_OOS"
F3 = "F3_FORWARD"
FIDELITIES = (F0, F1, F2, F3)
_FIDELITY_INDEX = {value: index for index, value in enumerate(FIDELITIES)}

VALID = "VALID"
EVIDENCE = "EVIDENCE"
NO_EVIDENCE = "NO_EVIDENCE"
INFRA_FAILURE = "INFRA_FAILURE"
OUTCOMES = frozenset({VALID, EVIDENCE, NO_EVIDENCE, INFRA_FAILURE})

# These are measured economic failures, not missing observations.  Keeping the
# distinction explicit prevents a losing formula from being retried forever as
# if the loader had failed, while still retaining it as evolutionary memory.
MEASURED_FAILURE_CODES = frozenset({
    "NO_COST_FEASIBLE_ENTRY",
    "NON_POSITIVE_DIRECTIONAL_RELATION",
})
NO_EVIDENCE_CODES = frozenset({"NO_EXECUTABLE_OBSERVATIONS"})

PROMOTE = "PROMOTE"
SURVIVOR = "SURVIVOR"
REJECT = "REJECT"
HOLD_NO_EVIDENCE = "HOLD_NO_EVIDENCE"
RETRY_INFRA = "RETRY_INFRA"

_DEFAULT_WEIGHTS = {
    "cost_net": 0.30,
    "oos": 0.20,
    "coverage": 0.15,
    "robustness": 0.15,
    "novelty": 0.15,
    "simplicity": 0.05,
}


def _json_copy(value: Any) -> Any:
    """Return a canonical JSON value and reject NaN/non-serializable state."""
    encoded = json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    return json.loads(encoded)


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _unit_interval(value: Any, name: str) -> float:
    result = _finite(value, name)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{name} must be in [0, 1]")
    return result


def _identifier(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} is required")
    return result


def _sha256_identifier(value: Any, name: str) -> str:
    result = _identifier(value, name)
    if not _SHA256.fullmatch(result):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return result


def _canonical_coordinate(value: Any, name: str) -> str:
    result = _identifier(value, name).upper().replace(" ", "_")
    if "|" in result:
        raise ValueError(f"{name} cannot contain '|'")
    return result


def horizon_bucket(horizon_seconds: int) -> str:
    """Map a causal prediction horizon onto the fixed QD coordinate."""
    if isinstance(horizon_seconds, bool):
        raise ValueError("horizon_seconds must be a positive integer")
    try:
        seconds = int(horizon_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("horizon_seconds must be a positive integer") from exc
    if seconds <= 0 or seconds != horizon_seconds:
        raise ValueError("horizon_seconds must be a positive integer")
    if seconds <= 5:
        return "1_5S"
    if seconds <= 30:
        return "6_30S"
    if seconds <= 300:
        return "31_300S"
    if seconds <= 3600:
        return "301_3600S"
    return "GT_3600S"


@dataclass(frozen=True, order=True)
class Niche:
    """MAP-Elites cell: mechanism x horizon x information-clock domain."""

    economic_mechanism: str
    horizon_bucket: str
    clock_domain: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "economic_mechanism",
            _canonical_coordinate(self.economic_mechanism, "economic_mechanism"),
        )
        allowed_horizons = {
            "1_5S", "6_30S", "31_300S", "301_3600S", "GT_3600S",
        }
        horizon = _canonical_coordinate(self.horizon_bucket, "horizon_bucket")
        if horizon not in allowed_horizons:
            raise ValueError(f"unknown horizon_bucket: {horizon}")
        object.__setattr__(self, "horizon_bucket", horizon)
        object.__setattr__(
            self, "clock_domain",
            _canonical_coordinate(self.clock_domain, "clock_domain"),
        )

    @classmethod
    def create(
        cls,
        economic_mechanism: str,
        horizon_seconds: int,
        clock_domains: str | Iterable[str],
    ) -> "Niche":
        raw = ([clock_domains] if isinstance(clock_domains, str)
               else list(clock_domains))
        domains = sorted({
            _canonical_coordinate(value, "clock_domain") for value in raw
        })
        if not domains:
            raise ValueError("at least one clock_domain is required")
        return cls(
            economic_mechanism=economic_mechanism,
            horizon_bucket=horizon_bucket(horizon_seconds),
            clock_domain="+".join(domains),
        )

    @property
    def key(self) -> str:
        return "|".join((
            self.economic_mechanism, self.horizon_bucket, self.clock_domain,
        ))

    def to_payload(self) -> dict[str, str]:
        return {
            "economic_mechanism": self.economic_mechanism,
            "horizon_bucket": self.horizon_bucket,
            "clock_domain": self.clock_domain,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Niche":
        return cls(
            economic_mechanism=payload.get("economic_mechanism", ""),
            horizon_bucket=payload.get("horizon_bucket", ""),
            clock_domain=payload.get("clock_domain", ""),
        )


def _bounded_signed(value: float, scale: float) -> float:
    """Monotone finite map from the real line to (0, 1)."""
    return 0.5 + 0.5 * value / (scale + abs(value))


@dataclass(frozen=True)
class ObjectiveVector:
    """Measured objectives; quality converts every component to higher-is-better."""

    cost_net_bps: float
    oos_sharpe: float
    coverage_ratio: float
    robustness_score: float
    novelty_score: float
    complexity_nodes: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "cost_net_bps", _finite(self.cost_net_bps, "cost_net_bps"))
        object.__setattr__(
            self, "oos_sharpe", _finite(self.oos_sharpe, "oos_sharpe"))
        for name in ("coverage_ratio", "robustness_score", "novelty_score"):
            object.__setattr__(self, name, _unit_interval(getattr(self, name), name))
        if isinstance(self.complexity_nodes, bool):
            raise ValueError("complexity_nodes must be a positive integer")
        try:
            complexity = int(self.complexity_nodes)
        except (TypeError, ValueError) as exc:
            raise ValueError("complexity_nodes must be a positive integer") from exc
        if complexity <= 0 or complexity != self.complexity_nodes:
            raise ValueError("complexity_nodes must be a positive integer")
        object.__setattr__(self, "complexity_nodes", complexity)

    def components(self) -> dict[str, float]:
        return {
            "cost_net": _bounded_signed(self.cost_net_bps, 5.0),
            "oos": _bounded_signed(self.oos_sharpe, 2.0),
            "coverage": self.coverage_ratio,
            "robustness": self.robustness_score,
            "novelty": self.novelty_score,
            # One node is maximally simple; every ten extra nodes halves this
            # component.  Complexity cannot improve by crossing an arbitrary cap.
            "simplicity": 1.0 / (1.0 + (self.complexity_nodes - 1) / 10.0),
        }

    def quality_score(
        self, weights: Mapping[str, float] | None = None,
    ) -> float:
        chosen = dict(_DEFAULT_WEIGHTS if weights is None else weights)
        if set(chosen) != set(_DEFAULT_WEIGHTS):
            raise ValueError(
                "objective weights must exactly cover cost_net, oos, coverage, "
                "robustness, novelty and simplicity")
        normalized = {name: _finite(value, f"weight.{name}")
                      for name, value in chosen.items()}
        if any(value < 0 for value in normalized.values()):
            raise ValueError("objective weights cannot be negative")
        total = sum(normalized.values())
        if total <= 0:
            raise ValueError("at least one objective weight must be positive")
        values = self.components()
        return sum(values[name] * normalized[name]
                   for name in sorted(values)) / total

    def dominates(self, other: "ObjectiveVector") -> bool:
        mine, theirs = self.components(), other.components()
        return (all(mine[name] >= theirs[name] for name in mine)
                and any(mine[name] > theirs[name] for name in mine))

    def to_payload(self) -> dict[str, float | int]:
        return {
            "cost_net_bps": self.cost_net_bps,
            "oos_sharpe": self.oos_sharpe,
            "coverage_ratio": self.coverage_ratio,
            "robustness_score": self.robustness_score,
            "novelty_score": self.novelty_score,
            "complexity_nodes": self.complexity_nodes,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ObjectiveVector":
        return cls(**{name: payload.get(name) for name in (
            "cost_net_bps", "oos_sharpe", "coverage_ratio",
            "robustness_score", "novelty_score", "complexity_nodes",
        )})


@dataclass(frozen=True)
class FormulaEvaluation:
    """One immutable rung result for a durable candidate and market exposure.

    ``candidate_identity_fingerprint`` is the full 64-hex scientific identity,
    not the grammar's short AST lookup key.  Measured evidence additionally
    carries the exact AST, semantic-plan and source-lineage provenance needed
    for a downstream breeder to reproduce and audit the parent.
    """

    candidate_identity_fingerprint: str
    niche: Niche
    fidelity: str
    outcome: str
    objectives: ObjectiveVector | None = None
    candidate_payload: dict[str, Any] = field(default_factory=dict)
    exposure_fingerprint: str = ""
    sessions: int = 0
    opportunities: int = 0
    reason_code: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidate_identity_fingerprint",
            _sha256_identifier(
                self.candidate_identity_fingerprint,
                "candidate_identity_fingerprint"),
        )
        if self.fidelity not in FIDELITIES:
            raise ValueError(f"unknown fidelity: {self.fidelity}")
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome: {self.outcome}")
        if not isinstance(self.niche, Niche):
            raise ValueError("niche must be a Niche")
        payload = _json_copy(self.candidate_payload)
        if not isinstance(payload, dict):
            raise ValueError("candidate_payload must be a JSON object")
        claimed_identity = payload.get("candidate_identity_fingerprint")
        if (claimed_identity is not None
                and claimed_identity != self.candidate_identity_fingerprint):
            raise ValueError(
                "candidate_payload candidate identity does not match result")
        object.__setattr__(self, "candidate_payload", payload)
        for name in ("sessions", "opportunities"):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) != value or int(value) < 0:
                raise ValueError(f"{name} must be a non-negative integer")
            object.__setattr__(self, name, int(value))
        exposure = str(self.exposure_fingerprint or "").strip()
        reason = str(self.reason_code or "").strip().upper()
        object.__setattr__(self, "exposure_fingerprint", exposure)
        object.__setattr__(self, "reason_code", reason)
        root_lineage_id = str(payload.get("root_lineage_id") or "").strip()
        if (self.outcome in {EVIDENCE, NO_EVIDENCE}
                and not root_lineage_id):
            raise ValueError(
                "selection-visible market results require root_lineage_id")
        if reason in MEASURED_FAILURE_CODES and self.outcome != EVIDENCE:
            raise ValueError(f"{reason} is a measured EVIDENCE failure")
        if reason in NO_EVIDENCE_CODES and self.outcome != NO_EVIDENCE:
            raise ValueError(f"{reason} must use NO_EVIDENCE")

        if self.outcome == VALID:
            if self.fidelity != F0 or self.objectives is not None or exposure:
                raise ValueError("VALID is a market-data-free F0 result")
        elif self.outcome == INFRA_FAILURE:
            if self.objectives is not None or exposure:
                raise ValueError(
                    "INFRA_FAILURE cannot contain selection-visible evidence")
            if not reason:
                raise ValueError("INFRA_FAILURE requires reason_code")
        elif self.outcome == NO_EVIDENCE:
            if self.fidelity == F0 or self.objectives is not None:
                raise ValueError("NO_EVIDENCE is only valid for market rungs")
            if not exposure or not reason:
                raise ValueError(
                    "NO_EVIDENCE requires exposure_fingerprint and reason_code")
        elif self.outcome == EVIDENCE:
            if self.fidelity == F0 or self.objectives is None:
                raise ValueError("EVIDENCE requires objectives on F1/F2/F3")
            if not exposure or self.sessions <= 0 or self.opportunities <= 0:
                raise ValueError(
                    "EVIDENCE requires an exposure, sessions and opportunities")
            required = {
                "expression", "candidate_ast_fingerprint",
                "semantic_plan_fingerprint", "root_lineage_id",
                "source_lead_ids",
            }
            missing = sorted(required - set(payload))
            if missing:
                raise ValueError(
                    "EVIDENCE candidate_payload is missing durable identity: "
                    + ", ".join(missing))
            expression = payload.get("expression")
            if (not isinstance(expression, dict)
                    or _fingerprint(expression) != _sha256_identifier(
                        payload.get("candidate_ast_fingerprint"),
                        "candidate_ast_fingerprint")):
                raise ValueError(
                    "candidate_ast_fingerprint does not match expression")
            _sha256_identifier(
                payload.get("semantic_plan_fingerprint"),
                "semantic_plan_fingerprint")
            if not str(payload.get("root_lineage_id") or "").strip():
                raise ValueError("root_lineage_id is required for EVIDENCE")
            source_lead_ids = payload.get("source_lead_ids")
            if (not isinstance(source_lead_ids, list) or not source_lead_ids
                    or any(not isinstance(value, str) or not value.strip()
                           for value in source_lead_ids)
                    or source_lead_ids != sorted(set(source_lead_ids))):
                raise ValueError(
                    "source_lead_ids must be a sorted unique non-empty list")

    @property
    def candidate_fingerprint(self) -> str:
        """Compatibility alias; the value is a durable identity, not AST16."""
        return self.candidate_identity_fingerprint

    @property
    def screening_only(self) -> bool:
        return self.fidelity == F1

    @property
    def independent_evidence(self) -> bool:
        return self.outcome == EVIDENCE and self.fidelity in {F2, F3}

    @property
    def production_promotion_authority(self) -> bool:
        # A search result still requires the governed QA/risk/forward gate.
        return False

    @property
    def result_identity(self) -> str:
        # Citation aliases and observation timestamps are provenance metadata,
        # not a changed scientific result.  Root lineage is an explicit ledger
        # coordinate because the durable candidate identity is root-independent.
        payload = self.candidate_payload
        return _fingerprint({
            "candidate_identity_fingerprint": (
                self.candidate_identity_fingerprint),
            "root_lineage_id": self.root_lineage_id,
            "candidate_contract": {
                name: payload.get(name) for name in (
                    "candidate_ast_fingerprint",
                    "semantic_plan_fingerprint",
                    "economic_family_id",
                    "evaluator_version",
                    "cost_model_version",
                    "evidence_scope",
                    "measurement_scope",
                    "explicit_survivor",
                )
            },
            "niche": self.niche.to_payload(),
            "fidelity": self.fidelity,
            "outcome": self.outcome,
            "objectives": (self.objectives.to_payload()
                           if self.objectives is not None else None),
            "exposure_fingerprint": self.exposure_fingerprint,
            "sessions": self.sessions,
            "opportunities": self.opportunities,
            "reason_code": self.reason_code,
        })

    @property
    def root_lineage_id(self) -> str:
        return str(self.candidate_payload.get("root_lineage_id") or "").strip()

    @property
    def trial_identity(self) -> str:
        if self.fidelity == F0 or not self.exposure_fingerprint:
            return ""
        return _fingerprint({
            "candidate_identity_fingerprint": (
                self.candidate_identity_fingerprint),
            "root_lineage_id": self.root_lineage_id,
            "fidelity": self.fidelity,
            "exposure_fingerprint": self.exposure_fingerprint,
        })

    def to_payload(self) -> dict[str, Any]:
        return {
            "candidate_identity_fingerprint": (
                self.candidate_identity_fingerprint),
            "niche": self.niche.to_payload(),
            "fidelity": self.fidelity,
            "outcome": self.outcome,
            "objectives": (self.objectives.to_payload()
                           if self.objectives is not None else None),
            "candidate_payload": _json_copy(self.candidate_payload),
            "exposure_fingerprint": self.exposure_fingerprint,
            "sessions": self.sessions,
            "opportunities": self.opportunities,
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FormulaEvaluation":
        objective = payload.get("objectives")
        return cls(
            candidate_identity_fingerprint=payload.get(
                "candidate_identity_fingerprint", ""),
            niche=Niche.from_payload(payload.get("niche") or {}),
            fidelity=payload.get("fidelity", ""),
            outcome=payload.get("outcome", ""),
            objectives=(ObjectiveVector.from_payload(objective)
                        if isinstance(objective, Mapping) else None),
            candidate_payload=payload.get("candidate_payload") or {},
            exposure_fingerprint=payload.get("exposure_fingerprint", ""),
            sessions=payload.get("sessions", 0),
            opportunities=payload.get("opportunities", 0),
            reason_code=payload.get("reason_code", ""),
        )


@dataclass(frozen=True)
class ArchiveEntry:
    evaluation: FormulaEvaluation
    quality_score: float
    inserted_cycle: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "evaluation": self.evaluation.to_payload(),
            "quality_score": self.quality_score,
            "inserted_cycle": self.inserted_cycle,
        }


@dataclass(frozen=True)
class ArchiveUpdate:
    action: str
    niche_key: str
    candidate_identity_fingerprint: str
    incumbent_identity_fingerprint: str | None
    quality_score: float | None

    @property
    def candidate_fingerprint(self) -> str:
        return self.candidate_identity_fingerprint

    @property
    def incumbent_fingerprint(self) -> str | None:
        return self.incumbent_identity_fingerprint


class FormulaSearchArchive:
    """One best measured formula per economic behavior cell."""

    def __init__(self, weights: Mapping[str, float] | None = None) -> None:
        chosen = dict(_DEFAULT_WEIGHTS if weights is None else weights)
        # Validate once using a harmless objective vector.
        ObjectiveVector(0, 0, 0, 0, 0, 1).quality_score(chosen)
        self._weights = {name: float(chosen[name]) for name in sorted(chosen)}
        self._entries: dict[str, ArchiveEntry] = {}
        self._seen_results: set[str] = set()
        self._outcome_counts = {name: 0 for name in sorted(OUTCOMES)}

    @property
    def entries(self) -> tuple[ArchiveEntry, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))

    @property
    def outcome_counts(self) -> dict[str, int]:
        return dict(self._outcome_counts)

    def get(self, niche: Niche) -> ArchiveEntry | None:
        return self._entries.get(niche.key)

    @staticmethod
    def _evidence_rank(evaluation: FormulaEvaluation) -> int:
        # Screening can seed evolution but never overwrite independent evidence.
        return {F1: 0, F2: 1, F3: 2}.get(evaluation.fidelity, -1)

    def _challenger_wins(
        self, challenger: FormulaEvaluation, incumbent: FormulaEvaluation,
    ) -> bool:
        challenger_rank = self._evidence_rank(challenger)
        incumbent_rank = self._evidence_rank(incumbent)
        if challenger_rank != incumbent_rank:
            return challenger_rank > incumbent_rank
        assert challenger.objectives is not None
        assert incumbent.objectives is not None
        if challenger.objectives.dominates(incumbent.objectives):
            return True
        if incumbent.objectives.dominates(challenger.objectives):
            return False
        challenger_score = challenger.objectives.quality_score(self._weights)
        incumbent_score = incumbent.objectives.quality_score(self._weights)
        if challenger_score != incumbent_score:
            return challenger_score > incumbent_score
        if (challenger.objectives.complexity_nodes
                != incumbent.objectives.complexity_nodes):
            return (challenger.objectives.complexity_nodes
                    < incumbent.objectives.complexity_nodes)
        return (challenger.candidate_identity_fingerprint
                < incumbent.candidate_identity_fingerprint)

    def observe(self, evaluation: FormulaEvaluation, *, cycle: int) -> ArchiveUpdate:
        if isinstance(cycle, bool) or int(cycle) != cycle or int(cycle) < 0:
            raise ValueError("cycle must be a non-negative integer")
        identity = evaluation.result_identity
        incumbent = self._entries.get(evaluation.niche.key)
        incumbent_fp = (
            incumbent.evaluation.candidate_identity_fingerprint
            if incumbent else None)
        if identity in self._seen_results:
            return ArchiveUpdate(
                "DUPLICATE_RESULT", evaluation.niche.key,
                evaluation.candidate_identity_fingerprint, incumbent_fp,
                (evaluation.objectives.quality_score(self._weights)
                 if evaluation.objectives else None),
            )
        self._seen_results.add(identity)
        self._outcome_counts[evaluation.outcome] += 1

        if evaluation.outcome == INFRA_FAILURE:
            action = "INFRA_FAILURE"
        elif evaluation.outcome == NO_EVIDENCE:
            action = "NO_EVIDENCE"
        elif evaluation.outcome == VALID:
            action = "STATIC_ONLY"
        else:
            assert evaluation.objectives is not None
            score = evaluation.objectives.quality_score(self._weights)
            entry = ArchiveEntry(evaluation, score, int(cycle))
            if incumbent is None:
                self._entries[evaluation.niche.key] = entry
                action = "INSERTED"
            elif self._challenger_wins(evaluation, incumbent.evaluation):
                self._entries[evaluation.niche.key] = entry
                action = "REPLACED"
            else:
                action = "RETAINED"
            return ArchiveUpdate(
                action, evaluation.niche.key,
                evaluation.candidate_identity_fingerprint,
                incumbent_fp, score,
            )
        return ArchiveUpdate(
            action, evaluation.niche.key,
            evaluation.candidate_identity_fingerprint,
            incumbent_fp, None,
        )

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "weights": dict(self._weights),
            "entries": [entry.to_payload() for entry in self.entries],
            "seen_results": sorted(self._seen_results),
            "outcome_counts": self.outcome_counts,
        }
        _json_copy(payload)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FormulaSearchArchive":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported formula-search archive schema")
        result = cls(payload.get("weights") or {})
        for raw in payload.get("entries") or []:
            evaluation = FormulaEvaluation.from_payload(raw.get("evaluation") or {})
            if evaluation.outcome != EVIDENCE:
                raise ValueError("archive entry must contain measured evidence")
            score = evaluation.objectives.quality_score(result._weights)  # type: ignore[union-attr]
            stored_score = _finite(raw.get("quality_score"), "quality_score")
            if not math.isclose(score, stored_score, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError("archive quality_score does not match objectives")
            cycle = raw.get("inserted_cycle")
            if isinstance(cycle, bool) or int(cycle) != cycle or int(cycle) < 0:
                raise ValueError("inserted_cycle must be a non-negative integer")
            key = evaluation.niche.key
            if key in result._entries:
                raise ValueError(f"duplicate persisted niche: {key}")
            result._entries[key] = ArchiveEntry(evaluation, score, int(cycle))
        result._seen_results = {
            _identifier(value, "seen_result")
            for value in (payload.get("seen_results") or [])
        }
        counts = payload.get("outcome_counts") or {}
        if set(counts) != OUTCOMES:
            raise ValueError("outcome_counts must cover every controlled outcome")
        result._outcome_counts = {}
        for name in sorted(OUTCOMES):
            value = counts[name]
            if isinstance(value, bool) or int(value) != value or int(value) < 0:
                raise ValueError("outcome counts must be non-negative integers")
            result._outcome_counts[name] = int(value)
        if sum(result._outcome_counts.values()) != len(result._seen_results):
            raise ValueError("outcome_counts do not match persisted result identities")
        if any(entry.evaluation.result_identity not in result._seen_results
               for entry in result._entries.values()):
            raise ValueError("a persisted elite is missing from seen_results")
        return result


@dataclass(frozen=True)
class ExposureRecord:
    trial_identity: str
    result_identity: str
    candidate_identity_fingerprint: str
    root_lineage_id: str
    fidelity: str
    exposure_fingerprint: str
    cycle: int
    outcome: str

    @property
    def screening_only(self) -> bool:
        return self.fidelity == F1

    @property
    def candidate_fingerprint(self) -> str:
        return self.candidate_identity_fingerprint

    def to_payload(self) -> dict[str, Any]:
        return {
            "trial_identity": self.trial_identity,
            "result_identity": self.result_identity,
            "candidate_identity_fingerprint": (
                self.candidate_identity_fingerprint),
            "root_lineage_id": self.root_lineage_id,
            "fidelity": self.fidelity,
            "exposure_fingerprint": self.exposure_fingerprint,
            "cycle": self.cycle,
            "outcome": self.outcome,
        }


class ExposureLedger:
    """Adaptive market-data exposure ledger with screening cooldowns."""

    def __init__(self) -> None:
        self._records: dict[str, ExposureRecord] = {}

    @property
    def records(self) -> tuple[ExposureRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def record(self, evaluation: FormulaEvaluation, *, cycle: int) -> bool:
        """Record selection-visible evidence; return false on an exact retry."""
        if evaluation.outcome not in {EVIDENCE, NO_EVIDENCE}:
            return False
        if isinstance(cycle, bool) or int(cycle) != cycle or int(cycle) < 0:
            raise ValueError("cycle must be a non-negative integer")
        key = evaluation.trial_identity
        record = ExposureRecord(
            trial_identity=key,
            result_identity=evaluation.result_identity,
            candidate_identity_fingerprint=(
                evaluation.candidate_identity_fingerprint),
            root_lineage_id=evaluation.root_lineage_id,
            fidelity=evaluation.fidelity,
            exposure_fingerprint=evaluation.exposure_fingerprint,
            cycle=int(cycle),
            outcome=evaluation.outcome,
        )
        previous = self._records.get(key)
        if previous is not None:
            if previous.result_identity == record.result_identity:
                return False
            raise ValueError(
                "the same formula/fidelity/exposure trial changed its durable result")
        self._records[key] = record
        return True

    def projected_effective_count(self, evaluation: FormulaEvaluation) -> int:
        count = self.effective_trial_count
        if (evaluation.outcome in {EVIDENCE, NO_EVIDENCE}
                and evaluation.trial_identity not in self._records):
            return count + 1
        return count

    @property
    def effective_trial_count(self) -> int:
        # One candidate x root x rung x immutable exposure is one adaptive look.
        return len(self._records)

    @property
    def measured_trial_count(self) -> int:
        return sum(record.outcome == EVIDENCE for record in self._records.values())

    @property
    def no_evidence_trial_count(self) -> int:
        return sum(record.outcome == NO_EVIDENCE
                   for record in self._records.values())

    def can_screen(
        self,
        *,
        candidate_identity_fingerprint: str,
        root_lineage_id: str,
        exposure_fingerprint: str,
        current_cycle: int,
        cooldown_cycles: int,
    ) -> tuple[bool, str]:
        candidate = _sha256_identifier(
            candidate_identity_fingerprint,
            "candidate_identity_fingerprint")
        exposure = _identifier(exposure_fingerprint, "exposure_fingerprint")
        root = _identifier(root_lineage_id, "root_lineage_id")
        for name, value in (("current_cycle", current_cycle),
                            ("cooldown_cycles", cooldown_cycles)):
            if isinstance(value, bool) or int(value) != value or int(value) < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        trial = _fingerprint({
            "candidate_identity_fingerprint": candidate,
            "root_lineage_id": root,
            "fidelity": F1,
            "exposure_fingerprint": exposure,
        })
        if trial in self._records:
            return False, "DUPLICATE_SCREENING_EXPOSURE"
        prior_cycles = [
            record.cycle for record in self._records.values()
            if record.screening_only
            and record.candidate_identity_fingerprint == candidate
            and record.root_lineage_id == root
        ]
        if not prior_cycles:
            return True, "ELIGIBLE"
        last_cycle = max(prior_cycles)
        if current_cycle < last_cycle:
            raise ValueError("current_cycle cannot precede a durable exposure")
        if current_cycle - last_cycle < cooldown_cycles:
            return False, "SCREENING_COOLDOWN"
        return True, "ELIGIBLE"

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "records": [record.to_payload() for record in self.records],
        }
        _json_copy(payload)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ExposureLedger":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported exposure-ledger schema")
        result = cls()
        for raw in payload.get("records") or []:
            fidelity = raw.get("fidelity")
            outcome = raw.get("outcome")
            if fidelity not in {F1, F2, F3} or outcome not in {EVIDENCE, NO_EVIDENCE}:
                raise ValueError("invalid persisted exposure record")
            candidate = _sha256_identifier(
                raw.get("candidate_identity_fingerprint"),
                "candidate_identity_fingerprint")
            exposure = _identifier(
                raw.get("exposure_fingerprint"), "exposure_fingerprint")
            root = _identifier(raw.get("root_lineage_id"), "root_lineage_id")
            cycle = raw.get("cycle")
            if isinstance(cycle, bool) or int(cycle) != cycle or int(cycle) < 0:
                raise ValueError("cycle must be a non-negative integer")
            expected = _fingerprint({
                "candidate_identity_fingerprint": candidate,
                "root_lineage_id": root,
                "fidelity": fidelity,
                "exposure_fingerprint": exposure,
            })
            if raw.get("trial_identity") != expected or expected in result._records:
                raise ValueError("invalid or duplicate persisted trial identity")
            result_identity = _identifier(
                raw.get("result_identity"), "result_identity")
            result._records[expected] = ExposureRecord(
                expected, result_identity, candidate, root, fidelity, exposure,
                int(cycle), outcome)
        return result


@dataclass(frozen=True)
class FidelityThreshold:
    cost_net_bps_exclusive: float
    min_oos_sharpe: float
    min_coverage_ratio: float
    min_robustness_score: float
    min_novelty_score: float
    max_complexity_nodes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "cost_net_bps_exclusive", _finite(
            self.cost_net_bps_exclusive, "cost_net_bps_exclusive"))
        object.__setattr__(self, "min_oos_sharpe", _finite(
            self.min_oos_sharpe, "min_oos_sharpe"))
        for name in ("min_coverage_ratio", "min_robustness_score",
                     "min_novelty_score"):
            object.__setattr__(self, name, _unit_interval(getattr(self, name), name))
        maximum = self.max_complexity_nodes
        if isinstance(maximum, bool) or int(maximum) != maximum or int(maximum) <= 0:
            raise ValueError("max_complexity_nodes must be a positive integer")
        object.__setattr__(self, "max_complexity_nodes", int(maximum))

    def to_payload(self) -> dict[str, Any]:
        return {
            "cost_net_bps_exclusive": self.cost_net_bps_exclusive,
            "min_oos_sharpe": self.min_oos_sharpe,
            "min_coverage_ratio": self.min_coverage_ratio,
            "min_robustness_score": self.min_robustness_score,
            "min_novelty_score": self.min_novelty_score,
            "max_complexity_nodes": self.max_complexity_nodes,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FidelityThreshold":
        return cls(**{name: payload.get(name) for name in (
            "cost_net_bps_exclusive", "min_oos_sharpe",
            "min_coverage_ratio", "min_robustness_score",
            "min_novelty_score", "max_complexity_nodes",
        )})


DEFAULT_THRESHOLDS = {
    F1: FidelityThreshold(0.0, -0.25, 0.05, 0.15, 0.05, 48),
    F2: FidelityThreshold(0.0, 0.25, 0.20, 0.40, 0.05, 40),
    F3: FidelityThreshold(0.0, 0.50, 0.25, 0.55, 0.00, 36),
}


@dataclass(frozen=True)
class PromotionDecision:
    action: str
    current_fidelity: str
    next_fidelity: str | None
    failures: tuple[str, ...]
    effective_trial_count: int
    applied_thresholds: dict[str, Any]
    retryable: bool
    production_promotion_authority: bool = False

    @property
    def survived_rung(self) -> bool:
        return self.action in {PROMOTE, SURVIVOR}


class FidelityScheduler:
    """Successive-halving policy with a trial-count-aware OOS hurdle."""

    def __init__(
        self,
        thresholds: Mapping[str, FidelityThreshold] | None = None,
        *,
        oos_penalty_per_log_trial: float = 0.05,
    ) -> None:
        self.thresholds = dict(DEFAULT_THRESHOLDS if thresholds is None else thresholds)
        if set(self.thresholds) != {F1, F2, F3}:
            raise ValueError("thresholds must exactly cover F1, F2 and F3")
        if not all(isinstance(value, FidelityThreshold)
                   for value in self.thresholds.values()):
            raise ValueError("every threshold must be FidelityThreshold")
        self.oos_penalty_per_log_trial = _finite(
            oos_penalty_per_log_trial, "oos_penalty_per_log_trial")
        if self.oos_penalty_per_log_trial < 0:
            raise ValueError("oos_penalty_per_log_trial cannot be negative")

    def decide(
        self, evaluation: FormulaEvaluation, ledger: ExposureLedger,
    ) -> PromotionDecision:
        count = ledger.projected_effective_count(evaluation)
        if evaluation.outcome == INFRA_FAILURE:
            return PromotionDecision(
                RETRY_INFRA, evaluation.fidelity, evaluation.fidelity,
                (evaluation.reason_code,), count, {}, True)
        if evaluation.outcome == NO_EVIDENCE:
            return PromotionDecision(
                HOLD_NO_EVIDENCE, evaluation.fidelity, None,
                (evaluation.reason_code,), count, {}, False)
        if evaluation.outcome == VALID:
            return PromotionDecision(
                PROMOTE, F0, F1, (), count, {}, False)

        assert evaluation.objectives is not None
        threshold = self.thresholds[evaluation.fidelity]
        adjusted_oos = threshold.min_oos_sharpe
        if evaluation.fidelity in {F2, F3}:
            adjusted_oos += self.oos_penalty_per_log_trial * math.log1p(
                max(0, count - 1))
        values = evaluation.objectives
        failures = ([evaluation.reason_code]
                    if evaluation.reason_code in MEASURED_FAILURE_CODES else [])
        if values.cost_net_bps <= threshold.cost_net_bps_exclusive:
            failures.append("COST_NET_FLOOR")
        if values.oos_sharpe < adjusted_oos:
            failures.append("TRIAL_ADJUSTED_OOS_FLOOR")
        if values.coverage_ratio < threshold.min_coverage_ratio:
            failures.append("COVERAGE_FLOOR")
        if values.robustness_score < threshold.min_robustness_score:
            failures.append("ROBUSTNESS_FLOOR")
        if values.novelty_score < threshold.min_novelty_score:
            failures.append("NOVELTY_FLOOR")
        if values.complexity_nodes > threshold.max_complexity_nodes:
            failures.append("COMPLEXITY_CAP")
        applied = threshold.to_payload()
        applied["trial_adjusted_min_oos_sharpe"] = adjusted_oos
        if failures:
            return PromotionDecision(
                REJECT, evaluation.fidelity, None, tuple(failures), count,
                applied, False)
        if evaluation.fidelity == F3:
            # SURVIVOR means eligible for the independent governed gate, not alpha.
            return PromotionDecision(
                SURVIVOR, F3, None, (), count, applied, False)
        next_fidelity = FIDELITIES[_FIDELITY_INDEX[evaluation.fidelity] + 1]
        return PromotionDecision(
            PROMOTE, evaluation.fidelity, next_fidelity, (), count, applied, False)

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "thresholds": {
                key: self.thresholds[key].to_payload()
                for key in sorted(self.thresholds)
            },
            "oos_penalty_per_log_trial": self.oos_penalty_per_log_trial,
        }
        _json_copy(payload)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FidelityScheduler":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported fidelity-scheduler schema")
        raw = payload.get("thresholds") or {}
        thresholds = {key: FidelityThreshold.from_payload(value)
                      for key, value in raw.items()}
        return cls(
            thresholds,
            oos_penalty_per_log_trial=payload.get(
                "oos_penalty_per_log_trial", 0.05),
        )


class SearchKPIAccumulator:
    """Idempotent compute-normalized throughput metrics for the formula factory."""

    def __init__(self) -> None:
        self._generation_events: set[str] = set()
        self._valid_ast_fingerprints: set[str] = set()
        self._unique_niches: set[str] = set()
        self._evaluation_results: set[str] = set()
        self._survivor_candidate_identities: set[str] = set()
        self.generation_compute_seconds = 0.0
        self.evaluation_compute_seconds = 0.0

    def record_generation(
        self,
        *,
        generation_id: str,
        candidate_fingerprint: str,
        valid: bool,
        compute_seconds: float,
        niche: Niche | None = None,
    ) -> bool:
        event = _identifier(generation_id, "generation_id")
        candidate = _identifier(candidate_fingerprint, "candidate_fingerprint")
        seconds = _finite(compute_seconds, "compute_seconds")
        if seconds < 0:
            raise ValueError("compute_seconds cannot be negative")
        if event in self._generation_events:
            return False
        self._generation_events.add(event)
        self.generation_compute_seconds += seconds
        if valid:
            self._valid_ast_fingerprints.add(candidate)
            if niche is not None:
                self._unique_niches.add(niche.key)
        return True

    def record_evaluation(
        self,
        evaluation: FormulaEvaluation,
        decision: PromotionDecision,
        *,
        compute_seconds: float,
    ) -> bool:
        seconds = _finite(compute_seconds, "compute_seconds")
        if seconds < 0:
            raise ValueError("compute_seconds cannot be negative")
        identity = evaluation.result_identity
        if identity in self._evaluation_results:
            return False
        self._evaluation_results.add(identity)
        self.evaluation_compute_seconds += seconds
        if (decision.survived_rung and evaluation.fidelity != F0
                and evaluation.outcome == EVIDENCE):
            self._survivor_candidate_identities.add(
                evaluation.candidate_identity_fingerprint)
        return True

    def snapshot(self) -> dict[str, Any]:
        total_compute = (
            self.generation_compute_seconds + self.evaluation_compute_seconds)
        return {
            "valid_ast_per_minute": (
                len(self._valid_ast_fingerprints) * 60.0
                / self.generation_compute_seconds
                if self.generation_compute_seconds else 0.0),
            "unique_niche_per_hour": (
                len(self._unique_niches) * 3600.0 / total_compute
                if total_compute else 0.0),
            "survivor_per_compute_hour": (
                len(self._survivor_candidate_identities) * 3600.0
                / total_compute
                if total_compute else 0.0),
            "generated_events": len(self._generation_events),
            "valid_ast_unique": len(self._valid_ast_fingerprints),
            "unique_niches": len(self._unique_niches),
            "evaluated_results": len(self._evaluation_results),
            "survivor_unique": len(self._survivor_candidate_identities),
            "generation_compute_seconds": self.generation_compute_seconds,
            "evaluation_compute_seconds": self.evaluation_compute_seconds,
            "total_compute_seconds": total_compute,
        }

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generation_events": sorted(self._generation_events),
            "valid_ast_fingerprints": sorted(self._valid_ast_fingerprints),
            "unique_niches": sorted(self._unique_niches),
            "evaluation_results": sorted(self._evaluation_results),
            "survivor_candidate_identities": sorted(
                self._survivor_candidate_identities),
            "generation_compute_seconds": self.generation_compute_seconds,
            "evaluation_compute_seconds": self.evaluation_compute_seconds,
        }
        _json_copy(payload)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SearchKPIAccumulator":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported search-KPI schema")
        result = cls()
        for target, name in (
            (result._generation_events, "generation_events"),
            (result._valid_ast_fingerprints, "valid_ast_fingerprints"),
            (result._unique_niches, "unique_niches"),
            (result._evaluation_results, "evaluation_results"),
            (result._survivor_candidate_identities,
             "survivor_candidate_identities"),
        ):
            target.update(_identifier(value, name)
                          for value in (payload.get(name) or []))
        result.generation_compute_seconds = _finite(
            payload.get("generation_compute_seconds", 0),
            "generation_compute_seconds")
        result.evaluation_compute_seconds = _finite(
            payload.get("evaluation_compute_seconds", 0),
            "evaluation_compute_seconds")
        if min(result.generation_compute_seconds,
               result.evaluation_compute_seconds) < 0:
            raise ValueError("persisted compute seconds cannot be negative")
        return result


@dataclass(frozen=True)
class SearchProcessResult:
    archive_update: ArchiveUpdate
    decision: PromotionDecision
    new_result: bool


class FormulaSearchState:
    """Small composition root used by a worker or scheduler checkpoint."""

    def __init__(
        self,
        *,
        archive: FormulaSearchArchive | None = None,
        exposure_ledger: ExposureLedger | None = None,
        kpis: SearchKPIAccumulator | None = None,
    ) -> None:
        self.archive = archive or FormulaSearchArchive()
        self.exposure_ledger = exposure_ledger or ExposureLedger()
        self.kpis = kpis or SearchKPIAccumulator()

    def process_result(
        self,
        evaluation: FormulaEvaluation,
        *,
        cycle: int,
        compute_seconds: float,
        scheduler: FidelityScheduler,
    ) -> SearchProcessResult:
        # The ledger conflict check runs first.  A reused immutable trial key
        # must not partially mutate the archive before failing closed.
        self.exposure_ledger.record(evaluation, cycle=cycle)
        update = self.archive.observe(evaluation, cycle=cycle)
        is_new = update.action != "DUPLICATE_RESULT"
        decision = scheduler.decide(evaluation, self.exposure_ledger)
        if is_new:
            self.kpis.record_evaluation(
                evaluation, decision, compute_seconds=compute_seconds)
        return SearchProcessResult(update, decision, is_new)

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "archive": self.archive.to_payload(),
            "exposure_ledger": self.exposure_ledger.to_payload(),
            "kpis": self.kpis.to_payload(),
        }
        _json_copy(payload)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FormulaSearchState":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported formula-search state schema")
        return cls(
            archive=FormulaSearchArchive.from_payload(payload.get("archive") or {}),
            exposure_ledger=ExposureLedger.from_payload(
                payload.get("exposure_ledger") or {}),
            kpis=SearchKPIAccumulator.from_payload(payload.get("kpis") or {}),
        )


__all__ = [
    "EVIDENCE", "F0", "F1", "F2", "F3", "HOLD_NO_EVIDENCE",
    "INFRA_FAILURE", "MEASURED_FAILURE_CODES", "NO_EVIDENCE",
    "NO_EVIDENCE_CODES", "PROMOTE", "REJECT", "RETRY_INFRA",
    "SURVIVOR", "VALID", "ArchiveEntry", "ArchiveUpdate", "ExposureLedger",
    "ExposureRecord", "FidelityScheduler", "FidelityThreshold",
    "FormulaEvaluation", "FormulaSearchArchive", "FormulaSearchState", "Niche",
    "ObjectiveVector", "PromotionDecision", "SearchKPIAccumulator",
    "SearchProcessResult", "horizon_bucket",
]
