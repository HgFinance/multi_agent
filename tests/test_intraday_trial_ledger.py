from __future__ import annotations

import sys
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest


PIPELINE = Path(__file__).resolve().parents[1] / "departments" / "04-quant-backtest" / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

from intraday_trial_ledger import (  # noqa: E402
    ADAPTIVE_SEARCH,
    ARRIVAL_TIME_CAUSAL,
    CALIBRATION,
    DISCOVERY_6,
    EVENT_TIME_HISTORICAL_ONLY,
    FORWARD,
    FORWARD_CONFIRMATION,
    FULL_60,
    VALIDATION_20,
    CandidateLineage,
    ExperimentRung,
    LedgerConflict,
    allocate_experiment_rung,
    candidate_identity_from_source_contract,
    find_latest_candidate_lineage,
    record_forward_confirmation,
    record_session_access,
    record_session_exposure,
    register_candidate_lineage,
    stable_fingerprint,
)


def _id(n: int) -> str:
    return str(uuid.UUID(int=n))


class EchoCursor:
    def __init__(self, conn: "EchoConnection") -> None:
        self.conn = conn
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split()).lower()
        self.conn.executed.append((normalized, params))
        if self.conn.scripted_rows:
            self.row = self.conn.scripted_rows.pop(0)
            return
        if "insert into quant.intraday_candidate_lineages" in normalized:
            self.row = (
                params[0], params[1], params[2], params[3], params[4], params[5],
                params[6], params[7], params[8], params[9], params[10], params[11],
                params[12], params[13],
            )
        elif "insert into quant.intraday_experiment_rungs" in normalized:
            instrument_ids = params[10]
            if self.conn.uuid_array_as_text:
                instrument_ids = "{" + ",".join(map(str, instrument_ids)) + "}"
            self.row = (
                params[0], params[1], params[2], params[3], params[5], params[6],
                params[8], instrument_ids, params[11], params[12], params[13],
                params[14], params[18],
            )
        elif "insert into quant.intraday_session_accesses" in normalized:
            self.row = (
                params[0], params[1], params[2], params[3], params[5], params[6],
                params[7], params[8],
            )
        elif "insert into quant.intraday_session_exposures" in normalized:
            self.row = (
                params[0], params[2], params[3], params[4], params[6], params[7],
                params[8], params[11],
            )
        elif "insert into quant.intraday_forward_confirmations" in normalized:
            self.row = (params[0], params[1], params[2], params[4], params[10])
        else:
            self.row = None

    def fetchone(self):
        return self.row


class EchoConnection:
    def __init__(self, scripted_rows=None, *, uuid_array_as_text=False) -> None:
        self.executed = []
        self.scripted_rows = list(scripted_rows or [])
        self.uuid_array_as_text = uuid_array_as_text
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return EchoCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _assert_insert_placeholders_match_params(conn: EchoConnection) -> None:
    inserts = [(sql, params) for sql, params in conn.executed
               if sql.startswith("insert into quant.intraday_")]
    assert inserts
    for sql, params in inserts:
        assert sql.count("%s") == len(params)


def _candidate() -> CandidateLineage:
    return CandidateLineage(
        candidate_lineage_id=_id(10),
        root_lineage_id=_id(10),
        parent_lineage_id=None,
        hypothesis_id=_id(11),
        candidate_identity_fingerprint="0" * 64,
        candidate_ast_fingerprint="1" * 64,
        semantic_plan_fingerprint="2" * 64,
        baseline_ast_fingerprint=None,
        feature_spec_fingerprint="3" * 64,
        label_spec_fingerprint="4" * 64,
        model_spec_fingerprint="5" * 64,
        economic_family_id="fam_order_flow",
        evaluator_version="intraday-runner-v9",
        cost_model_version="krx-intraday-execution-v1",
    )


def _rung(kind=DISCOVERY_6, *, sessions=None, predecessor_count=6) -> ExperimentRung:
    candidate = _candidate()
    values = sessions or tuple(date(2026, 7, d) for d in range(1, predecessor_count + 1))
    return ExperimentRung(
        experiment_rung_id=_id(20 if kind == DISCOVERY_6 else 21),
        candidate=candidate,
        experiment_id=_id(30 if kind == DISCOVERY_6 else 31),
        dataset_id=_id(40),
        rung=kind,
        planned_session_dates=tuple(values),
        planned_instrument_count=2,
        planned_instrument_ids=(_id(100), _id(101)),
        session_set_fingerprint=stable_fingerprint([v.isoformat() for v in values]),
        instrument_set_fingerprint=stable_fingerprint([_id(100), _id(101)]),
        rung_plan_fingerprint="8" * 64,
        lockbox_cutoff_session_date=(date(2026, 7, 31) if kind == FORWARD else None),
    )


def _access(rung: ExperimentRung, *, session=None,
            clock=EVENT_TIME_HISTORICAL_ONLY):
    purpose = (FORWARD_CONFIRMATION if rung.rung == FORWARD
               else CALIBRATION if rung.rung == CALIBRATION
               else ADAPTIVE_SEARCH)
    return record_session_access(
        EchoConnection(), rung=rung,
        session_date=session or rung.planned_session_dates[0],
        instrument_ids=rung.planned_instrument_ids,
        knowledge_cutoff=datetime(2026, 8, 3, 8, tzinfo=timezone.utc),
        source_watermark={"phase": "PRE_RAW_ACCESS"},
        accessed_by="unit-test", access_purpose=purpose,
        knowledge_clock_mode=(ARRIVAL_TIME_CAUSAL if rung.rung == FORWARD
                              else clock),
    )


def test_register_root_is_deterministic_and_does_not_backfill_exposures():
    conn = EchoConnection()
    lineage = register_candidate_lineage(
        conn,
        hypothesis_id=_id(1),
        candidate_ast={"op": "feature", "name": "quote_event_ofi"},
        semantic_plan={"mechanism": "queue_pressure"},
        feature_spec={"version": "micro-v5"},
        label_spec={"horizon_seconds": 60},
        model_spec={"type": "identity_ast"},
        economic_family_id="fam_order_flow",
        evaluator_version="intraday-runner-v9",
        cost_model_version="krx-intraday-execution-v1",
        created_by="unit-test",
        candidate_lineage_id=_id(2),
    )

    assert lineage.root_lineage_id == _id(2)
    assert lineage.parent_lineage_id is None
    assert lineage.candidate_identity_fingerprint == stable_fingerprint(
        {
            "candidate_ast": lineage.candidate_ast_fingerprint,
            "semantic_plan": lineage.semantic_plan_fingerprint,
            "baseline_ast": None,
            "feature_spec": lineage.feature_spec_fingerprint,
            "label_spec": lineage.label_spec_fingerprint,
            "model_spec": lineage.model_spec_fingerprint,
            "evaluator_version": "intraday-runner-v9",
            "cost_model_version": "krx-intraday-execution-v1",
        }
    )
    assert conn.commits == 1
    assert len(conn.executed) == 1
    assert "intraday_candidate_lineages" in conn.executed[0][0]
    assert all("session_exposures" not in sql for sql, _ in conn.executed)


def test_child_inherits_root_and_material_conflict_fails_closed():
    parent = _candidate()
    conn = EchoConnection()
    child = register_candidate_lineage(
        conn,
        hypothesis_id=_id(12),
        candidate_ast={"op": "neg", "arg": {"op": "feature", "name": "spread_bps"}},
        semantic_plan={"mechanism": "liquidity_reversal"},
        feature_spec={"version": "micro-v5"},
        label_spec={"horizon_seconds": 60},
        model_spec={"type": "identity_ast"},
        economic_family_id="fam_liquidity",
        evaluator_version="intraday-runner-v9",
        cost_model_version="krx-intraday-execution-v1",
        created_by="unit-test",
        parent=parent,
        candidate_lineage_id=_id(13),
    )
    assert child.parent_lineage_id == parent.candidate_lineage_id
    assert child.root_lineage_id == parent.root_lineage_id

    # The INSERT conflicts, and the durable row has a different family.  The
    # helper must not silently accept the reused idempotency key.
    existing = list(conn.executed[0][1][:14])
    existing[11] = "fam_wrong"
    conflict_conn = EchoConnection([None, tuple(existing)])
    with pytest.raises(LedgerConflict):
        register_candidate_lineage(
            conflict_conn,
            hypothesis_id=_id(12),
            candidate_ast={"op": "neg", "arg": {"op": "feature", "name": "spread_bps"}},
            semantic_plan={"mechanism": "liquidity_reversal"},
            feature_spec={"version": "micro-v5"},
            label_spec={"horizon_seconds": 60},
            model_spec={"type": "identity_ast"},
            economic_family_id="fam_liquidity",
            evaluator_version="intraday-runner-v9",
            cost_model_version="krx-intraday-execution-v1",
            created_by="unit-test",
            parent=parent,
            candidate_lineage_id=_id(13),
        )
    assert conflict_conn.rollbacks == 1


def test_latest_parent_identity_separates_same_ast_follow_30_and_revert_600():
    expression = {"op": "feature", "name": "quote_event_ofi"}
    common = {
        "candidate_ast": expression,
        "baseline_ast": None,
        "feature_spec": {"version": "micro-v5"},
        "label_spec": {"execution": "TAKER", "horizon_seconds": 30},
        "model_spec": {"type": "identity_ast"},
        "evaluator_version": "intraday-runner-v9",
        "cost_model_version": "krx-intraday-execution-v1",
    }
    follow = {
        **common,
        "semantic_plan": {"direction": "FOLLOW", "horizon_seconds": 30},
    }
    revert = {
        **common,
        "semantic_plan": {"direction": "REVERT", "horizon_seconds": 600},
        "label_spec": {"execution": "TAKER", "horizon_seconds": 600},
    }
    assert candidate_identity_from_source_contract(follow) != \
        candidate_identity_from_source_contract(revert)

    registered = EchoConnection()
    parent = register_candidate_lineage(
        registered,
        hypothesis_id=_id(201), candidate_ast=expression,
        semantic_plan=follow["semantic_plan"], baseline_ast=None,
        feature_spec=follow["feature_spec"],
        label_spec=follow["label_spec"], model_spec=follow["model_spec"],
        economic_family_id="fam_follow_30",
        evaluator_version=follow["evaluator_version"],
        cost_model_version=follow["cost_model_version"],
        created_by="unit-test", candidate_lineage_id=_id(202),
    )
    durable_row = tuple(registered.executed[0][1][:14])
    lookup = EchoConnection([durable_row])
    assert find_latest_candidate_lineage(
        lookup, source_contract=follow) == parent
    sql, params = lookup.executed[0]
    assert "where candidate_identity_fingerprint=%s" in sql
    assert "where candidate_ast_fingerprint=%s" not in sql
    assert params == (parent.candidate_identity_fingerprint,)

    # Even if a broken/mock database returned the FOLLOW row for a REVERT
    # query, the reader rechecks all durable identity components and fails
    # closed instead of merging the two roots.
    with pytest.raises(LedgerConflict, match="durable components"):
        find_latest_candidate_lineage(
            EchoConnection([durable_row]), source_contract=revert)


def test_generated_record_ids_do_not_break_idempotent_retries():
    kwargs = dict(
        hypothesis_id=_id(70),
        candidate_ast={"op": "feature", "name": "quote_event_ofi"},
        semantic_plan={"mechanism": "queue_pressure"},
        feature_spec={"version": "micro-v5"},
        label_spec={"horizon_seconds": 60},
        model_spec={"type": "identity_ast"},
        economic_family_id="fam_order_flow",
        evaluator_version="intraday-runner-v9",
        cost_model_version="krx-intraday-execution-v1",
        created_by="unit-test",
    )
    first_conn = EchoConnection()
    first = register_candidate_lineage(first_conn, **kwargs)
    _assert_insert_placeholders_match_params(first_conn)
    durable_lineage_row = tuple(first_conn.executed[0][1][:14])
    retry = register_candidate_lineage(
        EchoConnection([None, durable_lineage_row]), **kwargs,
    )
    assert retry.candidate_lineage_id == first.candidate_lineage_id

    rung_kwargs = dict(
        candidate=first,
        experiment_id=_id(71),
        dataset_id=_id(72),
        rung=CALIBRATION,
        session_dates=[date(2026, 6, 30)],
        instrument_ids=[_id(100)],
        selection_policy_version="teacher-v1",
        dataset_cutoff="2026-07-01T00:00:00Z",
        source_watermark={"source": "teacher"},
        allocation_reason="freeze teacher fit",
        allocated_by="unit-test",
    )
    first_rung_conn = EchoConnection()
    first_rung = allocate_experiment_rung(first_rung_conn, **rung_kwargs)
    _assert_insert_placeholders_match_params(first_rung_conn)
    durable_rung_row = EchoConnection().cursor()
    durable_rung_row.execute(
        "insert into quant.intraday_experiment_rungs", first_rung_conn.executed[0][1]
    )
    retry_rung = allocate_experiment_rung(
        EchoConnection([None, durable_rung_row.fetchone()]), **rung_kwargs,
    )
    assert retry_rung.experiment_rung_id == first_rung.experiment_rung_id

    access_kwargs = dict(
        rung=first_rung, session_date=date(2026, 6, 30),
        instrument_ids=[_id(100)],
        knowledge_cutoff="2026-07-01T00:00:00Z",
        source_watermark={"phase": "PRE_RAW_ACCESS"},
        accessed_by="unit-test",
    )
    first_access_conn = EchoConnection()
    first_access = record_session_access(first_access_conn, **access_kwargs)
    assert first_access_conn.commits == 1
    durable_access_row = EchoConnection().cursor()
    durable_access_row.execute(
        "insert into quant.intraday_session_accesses",
        first_access_conn.executed[0][1],
    )
    retry_access = record_session_access(
        EchoConnection([None, durable_access_row.fetchone()]), **access_kwargs)
    assert retry_access.session_access_id == first_access.session_access_id
    assert retry_access.inserted is False
    with pytest.raises(LedgerConflict, match="changed its frozen cutoff"):
        record_session_access(
            EchoConnection([None, durable_access_row.fetchone()]),
            **{**access_kwargs,
               "source_watermark": {"phase": "CHANGED_AFTER_RETRY"}},
        )
    with pytest.raises(LedgerConflict, match="changed its frozen cutoff"):
        record_session_access(
            EchoConnection([None, durable_access_row.fetchone()]),
            **{
                **access_kwargs,
                "knowledge_cutoff": "2026-07-01T00:00:01Z",
            },
        )

    exposure_kwargs = dict(
        access=first_access,
        rung=first_rung,
        session_date=date(2026, 6, 30),
        instrument_ids=[_id(100)],
        session_content_fingerprint="a" * 64,
        quote_row_count=10,
        trade_row_count=5,
        knowledge_cutoff="2026-07-01T00:00:00Z",
        source_watermark={"source": "teacher"},
        exposed_by="unit-test",
    )
    first_exposure_conn = EchoConnection()
    first_exposure = record_session_exposure(first_exposure_conn, **exposure_kwargs)
    _assert_insert_placeholders_match_params(first_exposure_conn)
    durable_exposure_row = EchoConnection().cursor()
    durable_exposure_row.execute(
        "insert into quant.intraday_session_exposures",
        first_exposure_conn.executed[0][1],
    )
    retry_exposure = record_session_exposure(
        EchoConnection([None, durable_exposure_row.fetchone()]), **exposure_kwargs,
    )
    assert retry_exposure.session_exposure_id == first_exposure.session_exposure_id
    assert retry_exposure.inserted is False
    with pytest.raises(LedgerConflict, match="changed immutable content"):
        record_session_exposure(
            EchoConnection([None, durable_exposure_row.fetchone()]),
            **{**exposure_kwargs, "session_content_fingerprint": "b" * 64},
        )
    with pytest.raises(LedgerConflict, match="changed immutable content"):
        record_session_exposure(
            EchoConnection([None, durable_exposure_row.fetchone()]),
            **{
                **exposure_kwargs,
                "knowledge_cutoff": "2026-07-01T00:00:01Z",
            },
        )


def test_nested_rung_reuse_requires_identical_content_evidence() -> None:
    day = date(2026, 7, 1)
    discovery = _rung(DISCOVERY_6)
    validation = ExperimentRung(
        experiment_rung_id=_id(21), candidate=discovery.candidate,
        experiment_id=discovery.experiment_id, dataset_id=discovery.dataset_id,
        rung=VALIDATION_20,
        planned_session_dates=tuple(date(2026, 7, d) for d in range(1, 21)),
        planned_instrument_count=2,
        planned_instrument_ids=discovery.planned_instrument_ids,
        session_set_fingerprint="6" * 64,
        instrument_set_fingerprint=discovery.instrument_set_fingerprint,
        rung_plan_fingerprint="7" * 64,
        lockbox_cutoff_session_date=None,
    )
    access = _access(discovery, session=day)
    cutoff = datetime(2026, 8, 3, 8, tzinfo=timezone.utc)
    watermark = {"source": "external-daily-manifest-v1"}
    content = "a" * 64
    prior = (
        _id(50), discovery.experiment_rung_id,
        discovery.candidate.candidate_lineage_id,
        discovery.candidate.root_lineage_id, day, ADAPTIVE_SEARCH,
        EVENT_TIME_HISTORICAL_ONLY, "9" * 64,
        content, stable_fingerprint(list(discovery.planned_instrument_ids)),
        10, 5, cutoff, watermark,
    )
    common = dict(
        access=access, rung=validation, session_date=day,
        instrument_ids=validation.planned_instrument_ids,
        session_content_fingerprint=content,
        quote_row_count=10, trade_row_count=5,
        knowledge_cutoff=cutoff, source_watermark=watermark,
        exposed_by="unit-test",
    )
    reused = record_session_exposure(EchoConnection([prior]), **common)
    assert reused.inserted is False
    with pytest.raises(LedgerConflict, match="nested rung observed content"):
        record_session_exposure(
            EchoConnection([prior]),
            **{**common, "session_content_fingerprint": "b" * 64},
        )


def test_nested_child_reuses_identical_content_across_horizon_cutoffs() -> None:
    day = date(2026, 7, 1)
    parent_rung = _rung(DISCOVERY_6)
    child = replace(
        parent_rung.candidate,
        candidate_lineage_id=_id(12),
        parent_lineage_id=parent_rung.candidate.candidate_lineage_id,
        candidate_identity_fingerprint="c" * 64,
        candidate_ast_fingerprint="d" * 64,
    )
    child_rung = ExperimentRung(
        experiment_rung_id=_id(21), candidate=child,
        experiment_id=_id(31), dataset_id=parent_rung.dataset_id,
        rung=DISCOVERY_6,
        planned_session_dates=parent_rung.planned_session_dates,
        planned_instrument_count=parent_rung.planned_instrument_count,
        planned_instrument_ids=parent_rung.planned_instrument_ids,
        session_set_fingerprint=parent_rung.session_set_fingerprint,
        instrument_set_fingerprint=parent_rung.instrument_set_fingerprint,
        rung_plan_fingerprint="7" * 64,
        lockbox_cutoff_session_date=None,
    )
    access = _access(parent_rung, session=day)
    parent_300s_cutoff = datetime(
        2026, 8, 14, 6, 25, 1, tzinfo=timezone.utc
    )
    child_30s_cutoff = datetime(
        2026, 8, 14, 6, 20, 31, tzinfo=timezone.utc
    )
    watermark = {"source": "external-daily-manifest-v1"}
    content = "a" * 64
    prior = (
        _id(50), parent_rung.experiment_rung_id,
        parent_rung.candidate.candidate_lineage_id,
        parent_rung.candidate.root_lineage_id, day, ADAPTIVE_SEARCH,
        EVENT_TIME_HISTORICAL_ONLY, "9" * 64,
        content, stable_fingerprint(list(parent_rung.planned_instrument_ids)),
        10, 5, parent_300s_cutoff, watermark,
    )
    common = dict(
        access=access, rung=child_rung, session_date=day,
        instrument_ids=child_rung.planned_instrument_ids,
        session_content_fingerprint=content,
        quote_row_count=10, trade_row_count=5,
        knowledge_cutoff=child_30s_cutoff, source_watermark=watermark,
        exposed_by="unit-test",
    )
    reused = record_session_exposure(EchoConnection([prior]), **common)
    assert reused.inserted is False

    changed_observations = (
        {"session_content_fingerprint": "b" * 64},
        {"quote_row_count": 11},
        {"trade_row_count": 6},
        {"source_watermark": {"source": "changed-manifest"}},
    )
    for changed in changed_observations:
        with pytest.raises(LedgerConflict, match="nested rung observed content"):
            record_session_exposure(
                EchoConnection([prior]), **{**common, **changed}
            )

    for changed_index, changed_value in ((8, None), (9, "e" * 64)):
        changed_prior = list(prior)
        changed_prior[changed_index] = changed_value
        with pytest.raises(LedgerConflict, match="nested rung observed content"):
            record_session_exposure(
                EchoConnection([tuple(changed_prior)]), **common
            )

    for changed_index, changed_value in (
        (5, FORWARD_CONFIRMATION),
        (6, ARRIVAL_TIME_CAUSAL),
    ):
        non_historical_prior = list(prior)
        non_historical_prior[changed_index] = changed_value
        with pytest.raises(LedgerConflict, match="nested rung observed content"):
            record_session_exposure(
                EchoConnection([tuple(non_historical_prior)]), **common
            )


def test_rung_allocation_decodes_psycopg_uuid_array_text_without_conflict():
    """A fresh INSERT may return uuid[] as text without psycopg UUID adapters."""

    conn = EchoConnection(uuid_array_as_text=True)
    rung = allocate_experiment_rung(
        conn,
        candidate=_candidate(),
        experiment_id=_id(73),
        dataset_id=_id(74),
        rung=CALIBRATION,
        session_dates=[date(2026, 6, 30)],
        instrument_ids=[_id(101), _id(100)],
        selection_policy_version="teacher-v1",
        dataset_cutoff="2026-07-01T00:00:00Z",
        source_watermark={"source": "teacher"},
        allocation_reason="freeze teacher fit",
        allocated_by="unit-test",
    )

    assert rung.planned_instrument_ids == (_id(100), _id(101))
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_rung_plan_freezes_sorted_sessions_and_requires_ordered_progression():
    candidate = _candidate()
    conn = EchoConnection()
    calibration = allocate_experiment_rung(
        conn,
        candidate=candidate,
        experiment_id=_id(29),
        dataset_id=_id(40),
        rung=CALIBRATION,
        session_dates=[date(2026, 6, 30)],
        instrument_ids=[_id(100), _id(101)],
        selection_policy_version="teacher-calibration-v1-stock-only",
        dataset_cutoff=datetime(2026, 8, 1, tzinfo=timezone.utc),
        source_watermark={"source": "external", "cutoff": "2026-08-01"},
        allocation_reason="teacher calibration is consumed before screening",
        allocated_by="unit-test",
        experiment_rung_id=_id(19),
    )
    sessions = [date(2026, 7, d) for d in (6, 1, 5, 2, 4, 3)]
    discovery = allocate_experiment_rung(
        conn,
        candidate=candidate,
        experiment_id=_id(29),
        dataset_id=_id(40),
        rung=DISCOVERY_6,
        session_dates=sessions,
        instrument_ids=[_id(101), _id(100)],
        selection_policy_version="stratified-session-v1-stock-only",
        dataset_cutoff=datetime(2026, 8, 1, tzinfo=timezone.utc),
        source_watermark={"source": "external", "cutoff": "2026-08-01"},
        allocation_reason="first cheap futility rung",
        allocated_by="unit-test",
        predecessor=calibration,
        experiment_rung_id=_id(20),
    )
    assert discovery.planned_session_dates == tuple(sorted(sessions))
    assert discovery.planned_instrument_count == 2
    assert discovery.planned_instrument_ids == (_id(100), _id(101))
    assert conn.commits == 2

    with pytest.raises(ValueError, match="requires predecessor"):
        allocate_experiment_rung(
            EchoConnection(),
            candidate=candidate,
            experiment_id=_id(31),
            dataset_id=_id(40),
            rung="VALIDATION_20",
            session_dates=[date(2026, 7, d) for d in range(1, 21)],
            instrument_ids=[_id(100)],
            selection_policy_version="v1",
            dataset_cutoff=datetime(2026, 8, 1, tzinfo=timezone.utc),
            source_watermark={"source": "external"},
            allocation_reason="invalid skip",
            allocated_by="unit-test",
        )


@pytest.mark.parametrize(
    ("rung", "count"),
    ((DISCOVERY_6, 5), (VALIDATION_20, 19), (FULL_60, 59), (FORWARD, 19)),
)
def test_named_rungs_fail_closed_on_noncanonical_session_counts(rung, count):
    with pytest.raises(ValueError, match="does not allow"):
        allocate_experiment_rung(
            EchoConnection(),
            candidate=_candidate(),
            experiment_id=_id(80),
            dataset_id=_id(81),
            rung=rung,
            session_dates=[date(2026, 9, 1) + timedelta(days=offset)
                           for offset in range(count)],
            instrument_ids=[_id(100)],
            selection_policy_version="exact-count-v1",
            dataset_cutoff=datetime(2026, 10, 1, tzinfo=timezone.utc),
            source_watermark={"source": "unit"},
            allocation_reason="reject malformed bracket",
            allocated_by="unit-test",
            lockbox_cutoff_session_date=(date(2026, 8, 31) if rung == FORWARD else None),
        )


def test_predecessor_is_scoped_to_same_experiment_dataset_universe_and_nested_dates():
    candidate = _candidate()
    common = dict(
        candidate=candidate,
        experiment_id=_id(82),
        dataset_id=_id(83),
        instrument_ids=[_id(100), _id(101)],
        selection_policy_version="immutable-bracket-v1",
        dataset_cutoff=datetime(2026, 10, 1, tzinfo=timezone.utc),
        source_watermark={"source": "unit"},
        allocation_reason="freeze bracket",
        allocated_by="unit-test",
    )
    calibration = allocate_experiment_rung(
        EchoConnection(), rung=CALIBRATION,
        session_dates=[date(2026, 8, 31)], **common,
    )
    with pytest.raises(ValueError, match="same experiment"):
        allocate_experiment_rung(
            EchoConnection(), rung=DISCOVERY_6,
            experiment_id=_id(84), dataset_id=_id(83), candidate=candidate,
            session_dates=[date(2026, 9, d) for d in range(1, 7)],
            instrument_ids=[_id(100), _id(101)], predecessor=calibration,
            selection_policy_version="immutable-bracket-v1",
            dataset_cutoff=datetime(2026, 10, 1, tzinfo=timezone.utc),
            source_watermark={"source": "unit"}, allocation_reason="bad run splice",
            allocated_by="unit-test",
        )
    with pytest.raises(ValueError, match="instrument universe"):
        allocate_experiment_rung(
            EchoConnection(), rung=DISCOVERY_6,
            session_dates=[date(2026, 9, d) for d in range(1, 7)],
            predecessor=calibration, **{
                **common, "instrument_ids": [_id(100)]
            },
        )

    discovery = allocate_experiment_rung(
        EchoConnection(), rung=DISCOVERY_6,
        session_dates=[date(2026, 9, d) for d in range(1, 7)],
        predecessor=calibration, **common,
    )
    with pytest.raises(ValueError, match="every predecessor session"):
        allocate_experiment_rung(
            EchoConnection(), rung=VALIDATION_20,
            session_dates=[date(2026, 9, d) for d in range(2, 22)],
            predecessor=discovery, **common,
        )


def test_exposure_requires_exact_uuid_array_even_for_manual_rung_objects():
    malformed = replace(_rung(), planned_instrument_ids=())
    with pytest.raises(ValueError, match="exact frozen instrument UUID array"):
        record_session_exposure(
            EchoConnection(), access=_access(_rung()), rung=malformed,
            session_date=malformed.planned_session_dates[0],
            instrument_ids=[_id(100), _id(101)],
            session_content_fingerprint="a" * 64,
            quote_row_count=1, trade_row_count=1,
            knowledge_cutoff=datetime(2026, 8, 1, tzinfo=timezone.utc),
            source_watermark={"source": "unit"}, exposed_by="unit-test",
        )


def test_forward_exposure_rejects_historical_clock_and_preexposed_date():
    candidate = _candidate()
    forward_sessions = tuple(date(2026, 8, d) for d in range(1, 21))
    forward = ExperimentRung(
        experiment_rung_id=_id(22),
        candidate=candidate,
        experiment_id=_id(32),
        dataset_id=_id(40),
        rung=FORWARD,
        planned_session_dates=forward_sessions,
        planned_instrument_count=2,
        planned_instrument_ids=(_id(100), _id(101)),
        session_set_fingerprint="6" * 64,
        instrument_set_fingerprint="7" * 64,
        rung_plan_fingerprint="8" * 64,
        lockbox_cutoff_session_date=date(2026, 7, 31),
    )
    common = dict(
        access=_access(forward),
        rung=forward,
        session_date=forward_sessions[0],
        instrument_ids=[_id(100), _id(101)],
        session_content_fingerprint="8" * 64,
        quote_row_count=10,
        trade_row_count=4,
        knowledge_cutoff=datetime(2026, 8, 3, 8, tzinfo=timezone.utc),
        source_watermark={"received_at": "2026-08-03T08:00:00Z"},
        exposed_by="unit-test",
    )
    with pytest.raises(ValueError, match="arrival-time-causal"):
        record_session_exposure(
            EchoConnection(),
            **common,
            knowledge_clock_mode=EVENT_TIME_HISTORICAL_ONLY,
        )

    conn = EchoConnection()
    exposure = record_session_exposure(
        conn, **common, knowledge_clock_mode=ARRIVAL_TIME_CAUSAL,
        session_exposure_id=_id(50),
    )
    assert exposure.inserted is True
    assert exposure.exposure_purpose == FORWARD_CONFIRMATION

    existing_discovery = (
        _id(51), _id(20), candidate.candidate_lineage_id,
        candidate.root_lineage_id, forward_sessions[0], ADAPTIVE_SEARCH,
        EVENT_TIME_HISTORICAL_ONLY, "9" * 64,
    )
    conflict_conn = EchoConnection([None, existing_discovery])
    with pytest.raises(LedgerConflict, match="already exposed"):
        record_session_exposure(
            conflict_conn, **common, knowledge_clock_mode=ARRIVAL_TIME_CAUSAL,
        )
    assert conflict_conn.rollbacks == 1


def test_calibration_rung_seals_teacher_session_with_typed_purpose():
    calibration = ExperimentRung(
        experiment_rung_id=_id(18),
        candidate=_candidate(),
        experiment_id=_id(28),
        dataset_id=_id(40),
        rung=CALIBRATION,
        planned_session_dates=(date(2026, 6, 30),),
        planned_instrument_count=2,
        planned_instrument_ids=(_id(100), _id(101)),
        session_set_fingerprint="6" * 64,
        instrument_set_fingerprint="7" * 64,
        rung_plan_fingerprint="8" * 64,
        lockbox_cutoff_session_date=None,
    )
    common = dict(
        access=_access(calibration),
        rung=calibration,
        session_date=date(2026, 6, 30),
        instrument_ids=[_id(100), _id(101)],
        session_content_fingerprint="9" * 64,
        quote_row_count=12,
        trade_row_count=5,
        knowledge_cutoff=datetime(2026, 7, 1, tzinfo=timezone.utc),
        source_watermark={"source": "teacher"},
        exposed_by="unit-test",
    )
    result = record_session_exposure(EchoConnection(), **common)
    assert result.exposure_purpose == CALIBRATION

    with pytest.raises(ValueError, match="CALIBRATION rung"):
        record_session_exposure(
            EchoConnection(), **common, exposure_purpose=ADAPTIVE_SEARCH,
        )


def test_forward_confirmation_is_append_only_and_pass_has_no_failures():
    forward_sessions = tuple(date(2026, 8, d) for d in range(1, 21))
    forward = ExperimentRung(
        experiment_rung_id=_id(22),
        candidate=_candidate(),
        experiment_id=_id(32),
        dataset_id=_id(40),
        rung=FORWARD,
        planned_session_dates=forward_sessions,
        planned_instrument_count=2,
        planned_instrument_ids=(_id(100), _id(101)),
        session_set_fingerprint="6" * 64,
        instrument_set_fingerprint="7" * 64,
        rung_plan_fingerprint="8" * 64,
        lockbox_cutoff_session_date=date(2026, 7, 31),
    )
    with pytest.raises(ValueError, match="PASS cannot"):
        record_forward_confirmation(
            EchoConnection(),
            rung=forward,
            decision="PASS",
            gate_version="intraday-final-v1",
            gate_statistics={"net_edge_after_cost_bps": 2.1},
            gate_failures=["PBO_UNMEASURED"],
            decision_reason="invalid",
            confirmed_by="unit-test",
        )

    conn = EchoConnection()
    result = record_forward_confirmation(
        conn,
        rung=forward,
        decision="INCONCLUSIVE",
        gate_version="intraday-final-v1",
        gate_statistics={"sessions": 20, "opportunities": 400},
        gate_failures=["INSUFFICIENT_FORWARD_SESSIONS"],
        decision_reason="more new sessions are required",
        confirmed_by="unit-test",
        forward_confirmation_id=_id(60),
    )
    assert result.forward_confirmation_id == _id(60)
    assert len(result.evidence_fingerprint) == 64
    sql = conn.executed[0][0]
    assert "on conflict (experiment_rung_id) do nothing" in sql
    _assert_insert_placeholders_match_params(conn)
    assert conn.commits == 1

    durable_row = (result.forward_confirmation_id, result.experiment_rung_id,
                   result.candidate_lineage_id, result.decision,
                   result.evidence_fingerprint)
    retry_conn = EchoConnection([None, durable_row])
    retry = record_forward_confirmation(
        retry_conn,
        rung=forward,
        decision="INCONCLUSIVE",
        gate_version="intraday-final-v1",
        gate_statistics={"sessions": 20, "opportunities": 400},
        gate_failures=["INSUFFICIENT_FORWARD_SESSIONS"],
        decision_reason="more new sessions are required",
        confirmed_by="unit-test",
    )
    assert retry.forward_confirmation_id == result.forward_confirmation_id
    assert "or candidate_lineage_id" not in retry_conn.executed[1][0]
