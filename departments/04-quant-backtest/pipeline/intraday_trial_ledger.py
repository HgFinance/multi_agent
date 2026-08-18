"""Append-only lineage, rung, session-exposure, and forward evidence ledger.

This module has no database-driver dependency.  It accepts a DB-API connection
and only appends immutable facts to the Supabase metadata plane.  In
particular, registering a candidate does *not* infer or backfill any historical
session as unused.  A caller must durably commit
:func:`record_session_access` before touching raw rows, then append
:func:`record_session_exposure` with the resulting content evidence.

The database remains the final safety boundary.  Its constraints and triggers
prevent descendants from laundering an already observed date into a FORWARD
rung, and require arrival-time-causal evidence for independent confirmation.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence

_CONTRACTS = Path(__file__).resolve().parents[2] / "01-research" / "contracts"
if str(_CONTRACTS) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS))

from intraday_candidate_identity import (  # noqa: E402
    candidate_identity_fingerprint,
)


MODULE_VERSION = "intraday-trial-ledger-v2"

CALIBRATION = "CALIBRATION"
DISCOVERY_6 = "DISCOVERY_6"
VALIDATION_20 = "VALIDATION_20"
FULL_60 = "FULL_60"
FORWARD = "FORWARD"
RUNGS = (CALIBRATION, DISCOVERY_6, VALIDATION_20, FULL_60, FORWARD)

ADAPTIVE_SEARCH = "ADAPTIVE_SEARCH"
INDEPENDENT_FORWARD = "INDEPENDENT_FORWARD"

FORWARD_CONFIRMATION = "FORWARD_CONFIRMATION"

ARRIVAL_TIME_CAUSAL = "ARRIVAL_TIME_CAUSAL"
EVENT_TIME_HISTORICAL_ONLY = "EVENT_TIME_HISTORICAL_ONLY"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PREDECESSOR = {
    CALIBRATION: None,
    DISCOVERY_6: CALIBRATION,
    VALIDATION_20: DISCOVERY_6,
    FULL_60: VALIDATION_20,
    FORWARD: FULL_60,
}


class LedgerConflict(RuntimeError):
    """An idempotency key already names materially different evidence."""


@dataclass(frozen=True)
class CandidateLineage:
    candidate_lineage_id: str
    root_lineage_id: str
    parent_lineage_id: str | None
    hypothesis_id: str
    candidate_identity_fingerprint: str
    candidate_ast_fingerprint: str
    semantic_plan_fingerprint: str
    baseline_ast_fingerprint: str | None
    feature_spec_fingerprint: str
    label_spec_fingerprint: str
    model_spec_fingerprint: str
    economic_family_id: str
    evaluator_version: str
    cost_model_version: str


@dataclass(frozen=True)
class ExperimentRung:
    experiment_rung_id: str
    candidate: CandidateLineage
    experiment_id: str
    dataset_id: str
    rung: str
    planned_session_dates: tuple[date, ...]
    planned_instrument_count: int
    session_set_fingerprint: str
    instrument_set_fingerprint: str
    rung_plan_fingerprint: str
    lockbox_cutoff_session_date: date | None
    planned_instrument_ids: tuple[str, ...] = ()
    forward_test_index: int | None = None


@dataclass(frozen=True)
class SessionAccess:
    session_access_id: str
    experiment_rung_id: str
    candidate_lineage_id: str
    root_lineage_id: str
    session_date: date
    access_purpose: str
    knowledge_clock_mode: str
    access_fingerprint: str
    inserted: bool


@dataclass(frozen=True)
class SessionExposure:
    session_exposure_id: str
    experiment_rung_id: str
    candidate_lineage_id: str
    root_lineage_id: str
    session_date: date
    exposure_purpose: str
    knowledge_clock_mode: str
    exposure_evidence_fingerprint: str
    inserted: bool


@dataclass(frozen=True)
class ForwardConfirmation:
    forward_confirmation_id: str
    experiment_rung_id: str
    candidate_lineage_id: str
    decision: str
    evidence_fingerprint: str


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime, uuid.UUID)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def stable_fingerprint(value: Any) -> str:
    """Return a deterministic SHA-256 fingerprint for a JSON-compatible value."""

    blob = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _uuid(value: str | uuid.UUID, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _text(value: str, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty")
    return cleaned


def _hash(value: str, field: str) -> str:
    cleaned = str(value or "").strip().lower()
    if not _HEX64.fullmatch(cleaned):
        raise ValueError(f"{field} must be a lowercase SHA-256 fingerprint")
    return cleaned


def _mapping(value: Mapping[str, Any], field: str, *, nonempty: bool = False) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    result = dict(value)
    if nonempty and not result:
        raise ValueError(f"{field} must not be empty")
    # Fail before opening a transaction if the payload cannot be persisted.
    json.dumps(result, sort_keys=True, default=_json_default)
    return result


def _as_date(value: date | str, field: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f"{field} must be a date, not a datetime")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def _as_timestamp(value: datetime | str, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _dates(values: Sequence[date | str]) -> tuple[date, ...]:
    parsed = tuple(_as_date(v, "session_dates") for v in values)
    if not parsed:
        raise ValueError("session_dates must not be empty")
    if len(set(parsed)) != len(parsed):
        raise ValueError("session_dates must be unique")
    return tuple(sorted(parsed))


def _instrument_set(values: Iterable[str | uuid.UUID]) -> tuple[str, ...]:
    parsed = tuple(_uuid(v, "instrument_id") for v in values)
    if not parsed:
        raise ValueError("instrument_ids must not be empty")
    if len(set(parsed)) != len(parsed):
        raise ValueError("instrument_ids must be unique")
    return tuple(sorted(parsed))


def _uuid_array_from_db(value: Any, field: str) -> tuple[str, ...]:
    """Decode a PostgreSQL ``uuid[]`` from either native or text form.

    psycopg2 only decodes ``uuid[]`` to a Python sequence when its UUID type
    adapter has been registered on the connection.  Pooler-created connections
    can therefore return the same column as ``{uuid,uuid}``.  Iterating that
    string would turn the frozen universe into a tuple of characters and make a
    just-inserted rung look like an idempotency conflict.

    UUID array elements never require PostgreSQL's quoted-array escaping, so a
    strict brace/comma decoder keeps this path driver-independent and fails
    closed on malformed database values.
    """

    if isinstance(value, str):
        encoded = value.strip()
        if not (encoded.startswith("{") and encoded.endswith("}")):
            raise ValueError(f"{field} must be a PostgreSQL UUID array")
        body = encoded[1:-1]
        values: Iterable[Any] = () if not body else body.split(",")
    elif isinstance(value, Sequence) and not isinstance(
        value, (bytes, bytearray)
    ):
        values = value
    else:
        raise ValueError(f"{field} must be a UUID array")

    parsed = tuple(_uuid(v, field) for v in values)
    if not parsed:
        raise ValueError(f"{field} must not be empty")
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{field} must contain unique UUIDs")
    return parsed


def _rollback(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _lineage_from_row(row: Sequence[Any]) -> CandidateLineage:
    return CandidateLineage(
        candidate_lineage_id=str(row[0]),
        root_lineage_id=str(row[1]),
        parent_lineage_id=str(row[2]) if row[2] is not None else None,
        hypothesis_id=str(row[3]),
        candidate_identity_fingerprint=str(row[4]),
        candidate_ast_fingerprint=str(row[5]),
        semantic_plan_fingerprint=str(row[6]),
        baseline_ast_fingerprint=str(row[7]) if row[7] is not None else None,
        feature_spec_fingerprint=str(row[8]),
        label_spec_fingerprint=str(row[9]),
        model_spec_fingerprint=str(row[10]),
        economic_family_id=str(row[11]),
        evaluator_version=str(row[12]),
        cost_model_version=str(row[13]),
    )


_LINEAGE_COLUMNS = """
candidate_lineage_id, root_lineage_id, parent_lineage_id, hypothesis_id,
candidate_identity_fingerprint, candidate_ast_fingerprint,
semantic_plan_fingerprint, baseline_ast_fingerprint,
feature_spec_fingerprint, label_spec_fingerprint, model_spec_fingerprint,
economic_family_id, evaluator_version, cost_model_version
"""


def register_candidate_lineage(
    conn: Any,
    *,
    hypothesis_id: str,
    candidate_ast: Any,
    semantic_plan: Any,
    feature_spec: Any,
    label_spec: Any,
    model_spec: Any,
    economic_family_id: str,
    evaluator_version: str,
    cost_model_version: str,
    created_by: str,
    baseline_ast: Any | None = None,
    parent: CandidateLineage | None = None,
    metadata: Mapping[str, Any] | None = None,
    candidate_lineage_id: str | None = None,
) -> CandidateLineage:
    """Append one exact candidate node without recording any session exposure."""

    hypothesis = _uuid(hypothesis_id, "hypothesis_id")
    lineage_id = _uuid(candidate_lineage_id or uuid.uuid4(), "candidate_lineage_id")
    parent_id = parent.candidate_lineage_id if parent else None
    root_id = parent.root_lineage_id if parent else lineage_id
    family = _text(economic_family_id, "economic_family_id")
    evaluator = _text(evaluator_version, "evaluator_version")
    cost_model = _text(cost_model_version, "cost_model_version")
    actor = _text(created_by, "created_by")
    meta = _mapping(metadata or {}, "metadata")

    ast_fp = stable_fingerprint(candidate_ast)
    semantic_fp = stable_fingerprint(semantic_plan)
    baseline_fp = stable_fingerprint(baseline_ast) if baseline_ast is not None else None
    feature_fp = stable_fingerprint(feature_spec)
    label_fp = stable_fingerprint(label_spec)
    model_fp = stable_fingerprint(model_spec)
    identity_fp = candidate_identity_fingerprint(
        candidate_ast_fingerprint=ast_fp,
        semantic_plan_fingerprint=semantic_fp,
        baseline_ast_fingerprint=baseline_fp,
        feature_spec_fingerprint=feature_fp,
        label_spec_fingerprint=label_fp,
        model_spec_fingerprint=model_fp,
        evaluator_version=evaluator,
        cost_model_version=cost_model,
    )

    explicit_lineage_id = candidate_lineage_id is not None
    expected = CandidateLineage(
        lineage_id, root_id, parent_id, hypothesis, identity_fp, ast_fp,
        semantic_fp, baseline_fp, feature_fp, label_fp, model_fp, family,
        evaluator, cost_model,
    )
    params = (
        lineage_id, root_id, parent_id, hypothesis, identity_fp, ast_fp,
        semantic_fp, baseline_fp, feature_fp, label_fp, model_fp, family,
        evaluator, cost_model, actor,
        json.dumps(meta, sort_keys=True, separators=(",", ":"), default=_json_default),
    )

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                insert into quant.intraday_candidate_lineages (
                  candidate_lineage_id, root_lineage_id, parent_lineage_id,
                  hypothesis_id, candidate_identity_fingerprint,
                  candidate_ast_fingerprint, semantic_plan_fingerprint,
                  baseline_ast_fingerprint, feature_spec_fingerprint,
                  label_spec_fingerprint, model_spec_fingerprint,
                  economic_family_id, evaluator_version, cost_model_version,
                  created_by, metadata
                ) values (
                  %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                )
                on conflict (hypothesis_id, candidate_identity_fingerprint)
                do nothing
                returning {_LINEAGE_COLUMNS}
                """,
                params,
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    f"""select {_LINEAGE_COLUMNS}
                          from quant.intraday_candidate_lineages
                         where hypothesis_id=%s::uuid
                           and candidate_identity_fingerprint=%s""",
                    (hypothesis, identity_fp),
                )
                row = cur.fetchone()
            if row is None:
                raise RuntimeError("candidate lineage append returned no durable row")
            actual = _lineage_from_row(row)
            same_material = (
                actual.hypothesis_id == expected.hypothesis_id
                and actual.candidate_identity_fingerprint
                    == expected.candidate_identity_fingerprint
                and actual.candidate_ast_fingerprint
                    == expected.candidate_ast_fingerprint
                and actual.semantic_plan_fingerprint
                    == expected.semantic_plan_fingerprint
                and actual.baseline_ast_fingerprint
                    == expected.baseline_ast_fingerprint
                and actual.feature_spec_fingerprint
                    == expected.feature_spec_fingerprint
                and actual.label_spec_fingerprint == expected.label_spec_fingerprint
                and actual.model_spec_fingerprint == expected.model_spec_fingerprint
                and actual.economic_family_id == expected.economic_family_id
                and actual.evaluator_version == expected.evaluator_version
                and actual.cost_model_version == expected.cost_model_version
            )
            same_ancestry = (
                actual.parent_lineage_id == parent_id
                and (
                    (parent is None
                     and actual.root_lineage_id == actual.candidate_lineage_id)
                    or (parent is not None and actual.root_lineage_id == root_id)
                )
            )
            same_explicit_id = (
                not explicit_lineage_id
                or actual.candidate_lineage_id == lineage_id
            )
            if not (same_material and same_ancestry and same_explicit_id):
                raise LedgerConflict(
                    "candidate identity already exists with different ancestry or family"
                )
        conn.commit()
        return actual
    except Exception:
        _rollback(conn)
        raise


def candidate_identity_from_source_contract(
        source_contract: Mapping[str, Any]) -> str:
    """Compute identity only from a complete, explicit evaluation contract.

    An AST is not a candidate: direction, horizon, baseline, features, label,
    model, evaluator, and costs all change what was actually tested.  Requiring
    this complete payload prevents a syntactically identical FOLLOW/30s and
    REVERT/600s equation from silently sharing ancestry.
    """
    contract = _mapping(source_contract, "source_contract", nonempty=True)
    required = {
        "candidate_ast", "semantic_plan", "baseline_ast", "feature_spec",
        "label_spec", "model_spec", "evaluator_version",
        "cost_model_version",
    }
    missing = sorted(required - set(contract))
    if missing:
        raise ValueError(
            "source_contract is incomplete: " + ", ".join(missing))
    return candidate_identity_fingerprint(
        candidate_ast_fingerprint=stable_fingerprint(
            contract["candidate_ast"]),
        semantic_plan_fingerprint=stable_fingerprint(
            contract["semantic_plan"]),
        baseline_ast_fingerprint=(
            stable_fingerprint(contract["baseline_ast"])
            if contract["baseline_ast"] is not None else None),
        feature_spec_fingerprint=stable_fingerprint(contract["feature_spec"]),
        label_spec_fingerprint=stable_fingerprint(contract["label_spec"]),
        model_spec_fingerprint=stable_fingerprint(contract["model_spec"]),
        evaluator_version=_text(
            contract["evaluator_version"], "evaluator_version"),
        cost_model_version=_text(
            contract["cost_model_version"], "cost_model_version"),
    )


def find_latest_candidate_lineage(
        conn: Any, *, source_contract: Mapping[str, Any] | None = None,
        candidate_identity: str | None = None) -> CandidateLineage | None:
    """Find prior ancestry by exact identity, never by AST resemblance.

    ``source_contract`` is the normal runner path.  ``candidate_identity`` is
    reserved for an explicitly declared, already-durable evolutionary parent.
    Exactly one selector is required; AST-only lookup is intentionally absent.
    """
    if (source_contract is None) == (candidate_identity is None):
        raise ValueError(
            "exactly one of source_contract or candidate_identity is required")
    identity_fp = (
        candidate_identity_from_source_contract(source_contract)
        if source_contract is not None
        else _hash(str(candidate_identity), "candidate_identity"))
    with conn.cursor() as cur:
        cur.execute(
            f"""select {_LINEAGE_COLUMNS}
                  from quant.intraday_candidate_lineages
                 where candidate_identity_fingerprint=%s
                 order by created_at desc, candidate_lineage_id desc
                 limit 1""",
            (identity_fp,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    lineage = _lineage_from_row(row)
    recomputed = candidate_identity_fingerprint(
        candidate_ast_fingerprint=lineage.candidate_ast_fingerprint,
        semantic_plan_fingerprint=lineage.semantic_plan_fingerprint,
        baseline_ast_fingerprint=lineage.baseline_ast_fingerprint,
        feature_spec_fingerprint=lineage.feature_spec_fingerprint,
        label_spec_fingerprint=lineage.label_spec_fingerprint,
        model_spec_fingerprint=lineage.model_spec_fingerprint,
        evaluator_version=lineage.evaluator_version,
        cost_model_version=lineage.cost_model_version,
    )
    if recomputed != identity_fp or lineage.candidate_identity_fingerprint != identity_fp:
        raise LedgerConflict(
            "candidate lineage identity does not match its durable components")
    return lineage


def load_candidate_lineage(conn: Any, candidate_lineage_id: str
                           ) -> CandidateLineage:
    """Load one immutable candidate node by its durable identity."""
    lineage_id = _uuid(candidate_lineage_id, "candidate_lineage_id")
    with conn.cursor() as cur:
        cur.execute(
            f"""select {_LINEAGE_COLUMNS}
                  from quant.intraday_candidate_lineages
                 where candidate_lineage_id=%s::uuid""",
            (lineage_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"candidate lineage not found: {lineage_id}")
    return _lineage_from_row(row)


def _check_rung_count(rung: str, count: int) -> None:
    valid = {
        CALIBRATION: 1 <= count <= 5,
        DISCOVERY_6: count == 6,
        VALIDATION_20: count == 20,
        FULL_60: count == 60,
        FORWARD: count >= 20,
    }
    if rung not in valid:
        raise ValueError(f"unknown rung: {rung}")
    if not valid[rung]:
        raise ValueError(f"{rung} does not allow {count} planned sessions")


def _rung_from_row(row: Sequence[Any], candidate: CandidateLineage) -> ExperimentRung:
    return ExperimentRung(
        experiment_rung_id=str(row[0]),
        candidate=candidate,
        experiment_id=str(row[3]),
        dataset_id=str(row[4]),
        rung=str(row[5]),
        planned_session_dates=tuple(_as_date(v, "planned_session_dates") for v in row[6]),
        planned_instrument_count=int(row[8]),
        session_set_fingerprint=str(row[9]),
        instrument_set_fingerprint=str(row[10]),
        rung_plan_fingerprint=str(row[11]),
        lockbox_cutoff_session_date=(
            _as_date(row[12], "lockbox_cutoff_session_date")
            if row[12] is not None else None
        ),
        planned_instrument_ids=_uuid_array_from_db(
            row[7], "planned_instrument_ids"
        ),
        forward_test_index=(int(row[13]) if len(row) > 13 and row[13] is not None
                            else None),
    )


_RUNG_COLUMNS = """
experiment_rung_id, candidate_lineage_id, root_lineage_id, experiment_id,
dataset_id, rung, planned_session_dates, planned_instrument_ids,
planned_instrument_count,
session_set_fingerprint, instrument_set_fingerprint, rung_plan_fingerprint,
lockbox_cutoff_session_date, forward_test_index
"""


def load_experiment_rung(conn: Any, *, experiment_id: str, rung: str,
                         candidate: CandidateLineage | None = None
                         ) -> ExperimentRung:
    """Load one frozen rung without reconstructing any plan inputs."""
    experiment = _uuid(experiment_id, "experiment_id")
    if rung not in RUNGS:
        raise ValueError(f"unknown rung: {rung}")
    with conn.cursor() as cur:
        cur.execute(
            f"""select {_RUNG_COLUMNS}
                  from quant.intraday_experiment_rungs
                 where experiment_id=%s::uuid and rung=%s
                 limit 1""",
            (experiment, rung),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"experiment rung not found: {experiment}/{rung}")
    loaded_candidate = candidate or load_candidate_lineage(conn, str(row[1]))
    if loaded_candidate.candidate_lineage_id != str(row[1]):
        raise LedgerConflict("loaded rung belongs to a different candidate")
    return _rung_from_row(row, loaded_candidate)


def allocate_experiment_rung(
    conn: Any,
    *,
    candidate: CandidateLineage,
    experiment_id: str,
    dataset_id: str,
    rung: str,
    session_dates: Sequence[date | str],
    instrument_ids: Iterable[str | uuid.UUID],
    selection_policy_version: str,
    dataset_cutoff: datetime | str,
    source_watermark: Mapping[str, Any],
    allocation_reason: str,
    allocated_by: str,
    predecessor: ExperimentRung | None = None,
    lockbox_cutoff_session_date: date | str | None = None,
    experiment_rung_id: str | None = None,
) -> ExperimentRung:
    """Freeze one rung before any of its sessions are exposed to the evaluator."""

    experiment = _uuid(experiment_id, "experiment_id")
    dataset = _uuid(dataset_id, "dataset_id")
    rung_id = _uuid(experiment_rung_id or uuid.uuid4(), "experiment_rung_id")
    sessions = _dates(session_dates)
    instruments = _instrument_set(instrument_ids)
    _check_rung_count(rung, len(sessions))
    policy = _text(selection_policy_version, "selection_policy_version")
    reason = _text(allocation_reason, "allocation_reason")
    actor = _text(allocated_by, "allocated_by")
    watermark = _mapping(source_watermark, "source_watermark", nonempty=True)
    dataset_cutoff_at = _as_timestamp(dataset_cutoff, "dataset_cutoff")

    expected_predecessor = _PREDECESSOR[rung]
    if expected_predecessor is None:
        if predecessor is not None:
            raise ValueError("CALIBRATION cannot have a predecessor")
    else:
        if predecessor is None or predecessor.rung != expected_predecessor:
            raise ValueError(f"{rung} requires predecessor {expected_predecessor}")
        if predecessor.candidate.candidate_lineage_id != candidate.candidate_lineage_id:
            raise ValueError("predecessor must belong to the same exact candidate")
        if predecessor.experiment_id != experiment:
            raise ValueError("predecessor must belong to the same experiment")
        if predecessor.dataset_id != dataset:
            raise ValueError("predecessor must use the same frozen dataset")
        if predecessor.planned_instrument_ids != instruments:
            raise ValueError("rungs must use the same frozen instrument universe")
        if rung in (VALIDATION_20, FULL_60) and not set(
            predecessor.planned_session_dates
        ).issubset(sessions):
            raise ValueError(f"{rung} must contain every predecessor session")

    cutoff = (
        _as_date(lockbox_cutoff_session_date, "lockbox_cutoff_session_date")
        if lockbox_cutoff_session_date is not None else None
    )
    if rung == FORWARD:
        if cutoff is None:
            raise ValueError("FORWARD requires a lockbox cutoff")
        if sessions[0] <= cutoff:
            raise ValueError("FORWARD sessions must be newer than the lockbox cutoff")
        purpose = INDEPENDENT_FORWARD
    else:
        if cutoff is not None:
            raise ValueError("adaptive-search rungs cannot declare a lockbox cutoff")
        purpose = ADAPTIVE_SEARCH

    session_fp = stable_fingerprint([v.isoformat() for v in sessions])
    instrument_fp = stable_fingerprint(list(instruments))
    plan_fp = stable_fingerprint(
        {
            "candidate_identity": candidate.candidate_identity_fingerprint,
            "candidate_lineage_id": candidate.candidate_lineage_id,
            "root_lineage_id": candidate.root_lineage_id,
            "experiment_id": experiment,
            "predecessor_rung_id": (
                predecessor.experiment_rung_id if predecessor else None
            ),
            "dataset_id": dataset,
            "rung": rung,
            "evidence_purpose": purpose,
            "session_dates": [v.isoformat() for v in sessions],
            "instrument_ids": list(instruments),
            "selection_policy_version": policy,
            "dataset_cutoff": dataset_cutoff_at,
            "source_watermark": watermark,
            "lockbox_cutoff_session_date": cutoff,
            "allocation_reason": reason,
            "allocated_by": actor,
        }
    )
    explicit_rung_id = experiment_rung_id is not None
    expected = ExperimentRung(
        rung_id, candidate, experiment, dataset, rung, sessions, len(instruments),
        session_fp, instrument_fp, plan_fp, cutoff, instruments,
    )
    params = (
        rung_id, candidate.candidate_lineage_id, candidate.root_lineage_id,
        experiment, predecessor.experiment_rung_id if predecessor else None,
        dataset, rung, purpose, list(sessions), len(sessions),
        list(instruments), len(instruments),
        session_fp, instrument_fp, plan_fp, policy, dataset_cutoff_at,
        json.dumps(watermark, sort_keys=True, separators=(",", ":"), default=_json_default),
        cutoff, reason, actor,
    )

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                insert into quant.intraday_experiment_rungs (
                  experiment_rung_id, candidate_lineage_id, root_lineage_id,
                  experiment_id, predecessor_rung_id, dataset_id, rung,
                  evidence_purpose, planned_session_dates,
                  planned_session_count, planned_instrument_ids,
                  planned_instrument_count,
                  session_set_fingerprint, instrument_set_fingerprint,
                  rung_plan_fingerprint, selection_policy_version,
                  dataset_cutoff, source_watermark,
                  lockbox_cutoff_session_date, allocation_reason, allocated_by
                ) values (
                  %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                  %s, %s, %s::date[], %s, %s::uuid[], %s, %s, %s, %s, %s,
                  %s::timestamptz, %s::jsonb, %s::date, %s, %s
                )
                on conflict (experiment_id, rung) do nothing
                returning {_RUNG_COLUMNS}
                """,
                params,
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    f"""select {_RUNG_COLUMNS}
                          from quant.intraday_experiment_rungs
                         where experiment_id=%s::uuid and rung=%s
                         limit 1""",
                    (experiment, rung),
                )
                row = cur.fetchone()
            if row is None:
                raise RuntimeError("rung allocation returned no durable row")
            if (str(row[1]) != candidate.candidate_lineage_id
                    or str(row[2]) != candidate.root_lineage_id):
                raise LedgerConflict(
                    "experiment rung belongs to a different candidate lineage"
                )
            actual = _rung_from_row(row, candidate)
            same_plan = (
                actual.experiment_id == expected.experiment_id
                and actual.dataset_id == expected.dataset_id
                and actual.rung == expected.rung
                and actual.planned_session_dates == expected.planned_session_dates
                and actual.planned_instrument_count
                    == expected.planned_instrument_count
                and actual.planned_instrument_ids
                    == expected.planned_instrument_ids
                and actual.session_set_fingerprint
                    == expected.session_set_fingerprint
                and actual.instrument_set_fingerprint
                    == expected.instrument_set_fingerprint
                and actual.rung_plan_fingerprint == expected.rung_plan_fingerprint
                and actual.lockbox_cutoff_session_date
                    == expected.lockbox_cutoff_session_date
            )
            same_explicit_id = (
                not explicit_rung_id or actual.experiment_rung_id == rung_id
            )
            if not (same_plan and same_explicit_id):
                raise LedgerConflict(
                    "candidate or experiment rung already exists with a different frozen slice"
                )
        conn.commit()
        return actual
    except Exception:
        _rollback(conn)
        raise


def _exposure_from_row(row: Sequence[Any], *, inserted: bool) -> SessionExposure:
    return SessionExposure(
        session_exposure_id=str(row[0]),
        experiment_rung_id=str(row[1]),
        candidate_lineage_id=str(row[2]),
        root_lineage_id=str(row[3]),
        session_date=_as_date(row[4], "session_date"),
        exposure_purpose=str(row[5]),
        knowledge_clock_mode=str(row[6]),
        exposure_evidence_fingerprint=str(row[7]),
        inserted=inserted,
    )


_EXPOSURE_COLUMNS = """
session_exposure_id, experiment_rung_id, candidate_lineage_id,
root_lineage_id, session_date, exposure_purpose, knowledge_clock_mode,
exposure_evidence_fingerprint
"""


_ACCESS_COLUMNS = """
session_access_id, experiment_rung_id, candidate_lineage_id,
root_lineage_id, session_date, access_purpose, knowledge_clock_mode,
access_fingerprint
"""


def _access_from_row(row: Sequence[Any], *, inserted: bool) -> SessionAccess:
    return SessionAccess(
        session_access_id=str(row[0]),
        experiment_rung_id=str(row[1]),
        candidate_lineage_id=str(row[2]),
        root_lineage_id=str(row[3]),
        session_date=_as_date(row[4], "session_date"),
        access_purpose=str(row[5]),
        knowledge_clock_mode=str(row[6]),
        access_fingerprint=str(row[7]),
        inserted=inserted,
    )


def record_session_access(
    conn: Any,
    *,
    rung: ExperimentRung,
    session_date: date | str,
    instrument_ids: Iterable[str | uuid.UUID],
    knowledge_cutoff: datetime | str,
    source_watermark: Mapping[str, Any],
    accessed_by: str,
    access_purpose: str | None = None,
    knowledge_clock_mode: str = EVENT_TIME_HISTORICAL_ONLY,
    session_access_id: str | None = None,
) -> SessionAccess:
    """Commit an immutable date-consumption marker before any raw read."""
    access_id = _uuid(session_access_id or uuid.uuid4(), "session_access_id")
    session = _as_date(session_date, "session_date")
    if session not in rung.planned_session_dates:
        raise ValueError("session was not frozen in the rung plan")
    instruments = _instrument_set(instrument_ids)
    if instruments != rung.planned_instrument_ids:
        raise ValueError("session instruments differ from the frozen universe")
    cutoff = _as_timestamp(knowledge_cutoff, "knowledge_cutoff")
    watermark = _mapping(source_watermark, "source_watermark", nonempty=True)
    actor = _text(accessed_by, "accessed_by")
    purpose = access_purpose or (
        FORWARD_CONFIRMATION if rung.rung == FORWARD
        else CALIBRATION if rung.rung == CALIBRATION else ADAPTIVE_SEARCH)
    if knowledge_clock_mode not in (
            ARRIVAL_TIME_CAUSAL, EVENT_TIME_HISTORICAL_ONLY):
        raise ValueError("unknown knowledge_clock_mode")
    if rung.rung == FORWARD:
        if (purpose != FORWARD_CONFIRMATION
                or knowledge_clock_mode != ARRIVAL_TIME_CAUSAL):
            raise ValueError(
                "FORWARD requires arrival-time-causal confirmation access")
    elif rung.rung == CALIBRATION and purpose != CALIBRATION:
        raise ValueError("CALIBRATION rung access must be CALIBRATION")
    elif rung.rung != CALIBRATION and purpose != ADAPTIVE_SEARCH:
        raise ValueError("search rung access must be ADAPTIVE_SEARCH")
    access_fp = stable_fingerprint({
        "experiment_rung_id": rung.experiment_rung_id,
        "candidate_lineage_id": rung.candidate.candidate_lineage_id,
        "root_lineage_id": rung.candidate.root_lineage_id,
        "dataset_id": rung.dataset_id,
        "session_date": session,
        "access_purpose": purpose,
        "knowledge_clock_mode": knowledge_clock_mode,
        "instrument_ids": list(instruments),
        "knowledge_cutoff": cutoff,
        "source_watermark": watermark,
        "accessed_by": actor,
    })
    params = (
        access_id, rung.experiment_rung_id,
        rung.candidate.candidate_lineage_id, rung.candidate.root_lineage_id,
        rung.dataset_id, session, purpose, knowledge_clock_mode, access_fp,
        list(instruments), len(instruments), cutoff,
        json.dumps(watermark, sort_keys=True, separators=(",", ":"),
                   default=_json_default), actor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                insert into quant.intraday_session_accesses (
                  session_access_id, experiment_rung_id,
                  candidate_lineage_id, root_lineage_id, dataset_id,
                  session_date, access_purpose, knowledge_clock_mode,
                  access_fingerprint, instrument_ids, instrument_count,
                  knowledge_cutoff, source_watermark, accessed_by
                ) values (
                  %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::date,
                  %s, %s, %s, %s::uuid[], %s, %s::timestamptz, %s::jsonb, %s
                )
                on conflict (root_lineage_id, session_date) do nothing
                returning {_ACCESS_COLUMNS}
                """, params)
            row = cur.fetchone()
            inserted = row is not None
            if row is None:
                cur.execute(
                    f"""select {_ACCESS_COLUMNS}
                          from quant.intraday_session_accesses
                         where root_lineage_id=%s::uuid and session_date=%s::date""",
                    (rung.candidate.root_lineage_id, session))
                row = cur.fetchone()
            if row is None:
                raise RuntimeError("session access append returned no durable row")
            actual = _access_from_row(row, inserted=inserted)
            exact_retry = (
                actual.experiment_rung_id == rung.experiment_rung_id
                and actual.candidate_lineage_id ==
                    rung.candidate.candidate_lineage_id
                and actual.access_purpose == purpose
                and actual.knowledge_clock_mode == knowledge_clock_mode
                and actual.access_fingerprint == access_fp)
            same_rung = actual.experiment_rung_id == rung.experiment_rung_id
            if same_rung and not exact_retry:
                raise LedgerConflict(
                    "session access retry changed its frozen cutoff or watermark")
            if rung.rung == FORWARD and not exact_retry:
                raise LedgerConflict(
                    "FORWARD session was already accessed by this lineage root")
        conn.commit()
        return actual
    except Exception:
        _rollback(conn)
        raise


def record_session_exposure(
    conn: Any,
    *,
    access: SessionAccess,
    rung: ExperimentRung,
    session_date: date | str,
    instrument_ids: Iterable[str | uuid.UUID],
    session_content_fingerprint: str,
    quote_row_count: int,
    trade_row_count: int,
    knowledge_cutoff: datetime | str,
    source_watermark: Mapping[str, Any],
    exposed_by: str,
    exposure_purpose: str | None = None,
    knowledge_clock_mode: str = EVENT_TIME_HISTORICAL_ONLY,
    session_exposure_id: str | None = None,
) -> SessionExposure:
    """Append content evidence after a durable access marker and raw read.

    A repeated adaptive read returns the root's original exposure with
    ``inserted=False``.  FORWARD never accepts such reuse: it raises
    :class:`LedgerConflict` unless the existing row is the exact same append.
    """

    exposure_id = _uuid(session_exposure_id or uuid.uuid4(), "session_exposure_id")
    session = _as_date(session_date, "session_date")
    if session not in rung.planned_session_dates:
        raise ValueError("session was not frozen in the rung plan")
    instruments = _instrument_set(instrument_ids)
    if (not rung.planned_instrument_ids
            or len(rung.planned_instrument_ids) != rung.planned_instrument_count):
        raise ValueError("rung is missing its exact frozen instrument UUID array")
    if instruments != rung.planned_instrument_ids:
        raise ValueError("session instruments differ from the frozen universe")
    content_fp = _hash(session_content_fingerprint, "session_content_fingerprint")
    if int(quote_row_count) < 0 or int(trade_row_count) < 0:
        raise ValueError("row counts must be non-negative")
    watermark = _mapping(source_watermark, "source_watermark", nonempty=True)
    knowledge_cutoff_at = _as_timestamp(knowledge_cutoff, "knowledge_cutoff")
    actor = _text(exposed_by, "exposed_by")
    if knowledge_clock_mode not in (ARRIVAL_TIME_CAUSAL, EVENT_TIME_HISTORICAL_ONLY):
        raise ValueError("unknown knowledge_clock_mode")

    purpose = exposure_purpose or (
        FORWARD_CONFIRMATION if rung.rung == FORWARD
        else CALIBRATION if rung.rung == CALIBRATION
        else ADAPTIVE_SEARCH
    )
    if rung.rung == FORWARD:
        if purpose != FORWARD_CONFIRMATION or knowledge_clock_mode != ARRIVAL_TIME_CAUSAL:
            raise ValueError("FORWARD requires arrival-time-causal confirmation evidence")
    elif rung.rung == CALIBRATION and purpose != CALIBRATION:
        raise ValueError("CALIBRATION rung exposure must be CALIBRATION")
    elif rung.rung != CALIBRATION and purpose != ADAPTIVE_SEARCH:
        raise ValueError("search rung exposure must be ADAPTIVE_SEARCH")
    if (access.root_lineage_id != rung.candidate.root_lineage_id
            or access.session_date != session):
        raise ValueError("session evidence does not match its durable access marker")
    exact_access = (
        access.experiment_rung_id == rung.experiment_rung_id
        and access.candidate_lineage_id == rung.candidate.candidate_lineage_id
        and access.access_purpose == purpose
        and access.knowledge_clock_mode == knowledge_clock_mode)
    if rung.rung == FORWARD and not exact_access:
        raise ValueError("FORWARD evidence requires its exact pre-read access marker")
    if not exact_access:
        # Nested adaptive rungs deliberately reuse the root's first date access
        # and evidence.  Do not manufacture a second post-read fact under a
        # different rung, but prove the newly observed immutable content is
        # byte-for-byte equivalent before returning the durable evidence.
        instrument_fp = stable_fingerprint(list(instruments))
        with conn.cursor() as cur:
            cur.execute(
                f"""select {_EXPOSURE_COLUMNS},
                           session_content_fingerprint,
                           instrument_set_fingerprint,
                           quote_row_count, trade_row_count,
                           knowledge_cutoff, source_watermark
                      from quant.intraday_session_exposures
                     where root_lineage_id=%s::uuid and session_date=%s::date""",
                (rung.candidate.root_lineage_id, session))
            existing = cur.fetchone()
        if existing is None:
            raise LedgerConflict(
                "adaptive access belongs to an earlier rung without content evidence")
        existing_watermark = existing[13]
        if isinstance(existing_watermark, str):
            existing_watermark = json.loads(existing_watermark)
        # ``knowledge_cutoff`` is the candidate/rung-specific purge boundary,
        # not a property of the immutable raw session bytes.  Descendants can
        # legitimately use a shorter horizon (for example 30s after a 300s
        # parent) and therefore seal a different cutoff while reading the same
        # frozen session.  The new cutoff remains durable on its experiment
        # rung; reuse is allowed only when every raw-content identity field
        # below is unchanged.  Exact same-rung retries and FORWARD evidence
        # still include the cutoff in their fingerprints and fail closed.
        reusable_historical_search = (
            str(existing[5]) in (CALIBRATION, ADAPTIVE_SEARCH)
            and str(existing[6]) == EVENT_TIME_HISTORICAL_ONLY
        )
        same_content = (
            reusable_historical_search
            and str(existing[8]) == content_fp
            and str(existing[9]) == instrument_fp
            and int(existing[10]) == int(quote_row_count)
            and int(existing[11]) == int(trade_row_count)
            and dict(existing_watermark or {}) == dict(watermark)
        )
        if not same_content:
            raise LedgerConflict(
                "nested rung observed content different from its immutable "
                "earlier exposure")
        return _exposure_from_row(existing, inserted=False)

    instrument_fp = stable_fingerprint(list(instruments))
    evidence_fp = stable_fingerprint(
        {
            "experiment_rung_id": rung.experiment_rung_id,
            "session_access_id": access.session_access_id,
            "candidate_lineage_id": rung.candidate.candidate_lineage_id,
            "root_lineage_id": rung.candidate.root_lineage_id,
            "dataset_id": rung.dataset_id,
            "session_date": session,
            "exposure_purpose": purpose,
            "knowledge_clock_mode": knowledge_clock_mode,
            "session_content_fingerprint": content_fp,
            "instrument_ids": list(instruments),
            "quote_row_count": int(quote_row_count),
            "trade_row_count": int(trade_row_count),
            "knowledge_cutoff": knowledge_cutoff_at,
            "source_watermark": watermark,
            "exposed_by": actor,
        }
    )
    params = (
        exposure_id, access.session_access_id, rung.experiment_rung_id,
        rung.candidate.candidate_lineage_id, rung.candidate.root_lineage_id,
        rung.dataset_id, session, purpose, knowledge_clock_mode, content_fp,
        instrument_fp, evidence_fp, list(instruments), len(instruments),
        int(quote_row_count),
        int(trade_row_count), knowledge_cutoff_at,
        json.dumps(watermark, sort_keys=True, separators=(",", ":"), default=_json_default),
        actor,
    )

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                insert into quant.intraday_session_exposures (
                  session_exposure_id, session_access_id, experiment_rung_id,
                  candidate_lineage_id, root_lineage_id, dataset_id,
                  session_date, exposure_purpose, knowledge_clock_mode,
                  session_content_fingerprint, instrument_set_fingerprint,
                  exposure_evidence_fingerprint, instrument_ids,
                  instrument_count,
                  quote_row_count, trade_row_count,
                  knowledge_cutoff, source_watermark, exposed_by
                ) values (
                  %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                  %s::date,
                  %s, %s, %s, %s, %s, %s::uuid[], %s, %s, %s,
                  %s::timestamptz,
                  %s::jsonb, %s
                )
                on conflict (root_lineage_id, session_date) do nothing
                returning {_EXPOSURE_COLUMNS}
                """,
                params,
            )
            row = cur.fetchone()
            inserted = row is not None
            if row is None:
                cur.execute(
                    f"""select {_EXPOSURE_COLUMNS}
                          from quant.intraday_session_exposures
                         where root_lineage_id=%s::uuid and session_date=%s::date""",
                    (rung.candidate.root_lineage_id, session),
                )
                row = cur.fetchone()
            if row is None:
                raise RuntimeError("session exposure append returned no durable row")
            actual = _exposure_from_row(row, inserted=inserted)
            exact_retry = (
                actual.experiment_rung_id == rung.experiment_rung_id
                and actual.candidate_lineage_id == rung.candidate.candidate_lineage_id
                and actual.exposure_purpose == purpose
                and actual.knowledge_clock_mode == knowledge_clock_mode
                and actual.exposure_evidence_fingerprint == evidence_fp
            )
            if not exact_retry:
                if rung.rung == FORWARD:
                    raise LedgerConflict(
                        "FORWARD session was already exposed to this lineage root")
                raise LedgerConflict(
                    "session exposure retry changed immutable content evidence")
        conn.commit()
        return actual
    except Exception:
        _rollback(conn)
        raise


def record_forward_confirmation(
    conn: Any,
    *,
    rung: ExperimentRung,
    decision: str,
    gate_version: str,
    gate_statistics: Mapping[str, Any],
    gate_failures: Sequence[str],
    decision_reason: str,
    confirmed_by: str,
    forward_confirmation_id: str | None = None,
) -> ForwardConfirmation:
    """Append a terminal result for a complete, already exposed FORWARD rung."""

    if rung.rung != FORWARD or rung.lockbox_cutoff_session_date is None:
        raise ValueError("only a FORWARD rung can be confirmed")
    _check_rung_count(FORWARD, len(rung.planned_session_dates))
    if (not rung.planned_instrument_ids
            or len(rung.planned_instrument_ids) != rung.planned_instrument_count):
        raise ValueError("FORWARD rung is missing its frozen instrument UUID array")
    verdict = str(decision or "").strip().upper()
    if verdict not in ("PASS", "FAIL", "INCONCLUSIVE"):
        raise ValueError("decision must be PASS, FAIL, or INCONCLUSIVE")
    failures = [_text(v, "gate_failure") for v in gate_failures]
    if verdict == "PASS" and failures:
        raise ValueError("PASS cannot contain gate failures")
    stats = _mapping(gate_statistics, "gate_statistics", nonempty=True)
    gate = _text(gate_version, "gate_version")
    reason = _text(decision_reason, "decision_reason")
    actor = _text(confirmed_by, "confirmed_by")
    explicit_confirmation_id = forward_confirmation_id is not None
    confirmation_id = _uuid(
        forward_confirmation_id or uuid.uuid4(), "forward_confirmation_id"
    )
    evidence_fp = stable_fingerprint(
        {
            "rung_id": rung.experiment_rung_id,
            "candidate_identity": rung.candidate.candidate_identity_fingerprint,
            "sessions": [v.isoformat() for v in rung.planned_session_dates],
            "cutoff": rung.lockbox_cutoff_session_date.isoformat(),
            "decision": verdict,
            "gate_version": gate,
            "statistics": stats,
            "failures": failures,
        }
    )
    params = (
        confirmation_id, rung.experiment_rung_id,
        rung.candidate.candidate_lineage_id, rung.candidate.root_lineage_id,
        verdict, gate, rung.lockbox_cutoff_session_date,
        rung.planned_session_dates[0], rung.planned_session_dates[-1],
        len(rung.planned_session_dates), evidence_fp,
        json.dumps(stats, sort_keys=True, separators=(",", ":"), default=_json_default),
        json.dumps(failures, ensure_ascii=False, separators=(",", ":")),
        reason, actor,
    )

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into quant.intraday_forward_confirmations (
                  forward_confirmation_id, experiment_rung_id,
                  candidate_lineage_id, root_lineage_id, decision, gate_version,
                  prior_search_max_session_date, forward_start_session_date,
                  forward_end_session_date, forward_session_count,
                  confirmation_evidence_fingerprint, gate_statistics,
                  gate_failures, decision_reason, confirmed_by
                ) values (
                  %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s, %s::date,
                  %s::date, %s::date, %s, %s, %s::jsonb, %s::jsonb, %s, %s
                )
                on conflict (experiment_rung_id) do nothing
                returning forward_confirmation_id, experiment_rung_id,
                          candidate_lineage_id, decision,
                          confirmation_evidence_fingerprint
                """,
                params,
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """select forward_confirmation_id, experiment_rung_id,
                              candidate_lineage_id, decision,
                              confirmation_evidence_fingerprint
                         from quant.intraday_forward_confirmations
                        where experiment_rung_id=%s::uuid
                         limit 1""",
                    (rung.experiment_rung_id,),
                )
                row = cur.fetchone()
            if row is None:
                raise RuntimeError("forward confirmation append returned no durable row")
            actual = ForwardConfirmation(
                forward_confirmation_id=str(row[0]),
                experiment_rung_id=str(row[1]),
                candidate_lineage_id=str(row[2]),
                decision=str(row[3]),
                evidence_fingerprint=str(row[4]),
            )
            expected = ForwardConfirmation(
                confirmation_id, rung.experiment_rung_id,
                rung.candidate.candidate_lineage_id, verdict, evidence_fp,
            )
            same_evidence = (
                actual.experiment_rung_id == expected.experiment_rung_id
                and actual.candidate_lineage_id == expected.candidate_lineage_id
                and actual.decision == expected.decision
                and actual.evidence_fingerprint == expected.evidence_fingerprint
            )
            same_explicit_id = (
                not explicit_confirmation_id
                or actual.forward_confirmation_id == confirmation_id
            )
            if not (same_evidence and same_explicit_id):
                raise LedgerConflict(
                    "candidate forward confirmation already exists with different evidence"
                )
        conn.commit()
        return actual
    except Exception:
        _rollback(conn)
        raise
