from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

import intraday_experiment_runner as runner  # noqa: E402
import experiment_worker as worker  # noqa: E402
from intraday_microstructure import QuoteEvent  # noqa: E402


class _RowsCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.executed = (str(sql), params)

    def fetchall(self):
        return list(self.rows)


class _RowsConnection:
    def __init__(self, rows):
        self.cursor_instance = _RowsCursor(rows)

    def cursor(self):
        return self.cursor_instance

    def rollback(self):
        pass


class _UpdateCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.conn.executed.append((" ".join(str(sql).split()), params))
        self.rowcount = 1


class _UpdateConnection:
    def __init__(self):
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _UpdateCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _PublicationCursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).lower().split())
        self.conn.executed.append(normalized)
        self.result = None
        self.rowcount = 0
        state = self.conn.state
        if ("select exists (" in normalized and
                "join quant.dataset_manifests dataset" in normalized and
                "join quant.hypotheses hypothesis" in normalized):
            self.result = (self.conn.governed_stock_evidence,)
        elif "select report.report_revision_id::text" in normalized:
            if state.get("report"):
                report = state["report"]
                self.result = (
                    report["report_revision_id"], self.conn.confirmation_id,
                    report["report_fingerprint"],
                    report["outcome_revision_id"], "SUPPORTED", "PASS",
                    state["outcome"]["fingerprint"], report["lifecycle"],
                    state["qa"]["qa_handoff_id"], state["hypothesis_status"],
                )
        elif "select e.hypothesis_id::text" in normalized:
            self.result = (
                self.conn.hypothesis_id, "family-1", 1, "proposal-1",
                "Forward test", state["hypothesis_status"], "b" * 64,
                "base-outcome-1",
            )
        elif "insert into research.experiment_outcome_revisions" in normalized:
            state["outcome"] = {
                "outcome_revision_id": str(params[0]),
                "fingerprint": str(params[-1]),
                "base_outcome_id": str(params[1]),
                "decision": str(params[4]),
            }
            outcome = state["outcome"]
            self.result = (
                outcome["outcome_revision_id"], outcome["fingerprint"],
                outcome["base_outcome_id"], outcome["decision"],
            )
        elif "insert into quant.intraday_forward_report_revisions" in normalized:
            state["report"] = {
                "report_revision_id": self.conn.report_revision_id,
                "report_fingerprint": str(params[3]),
                "outcome_revision_id": str(params[5]),
                "lifecycle": json.loads(params[8]),
            }
            report = state["report"]
            self.result = (
                report["report_revision_id"], report["report_fingerprint"],
                report["outcome_revision_id"],
            )
        elif "insert into quant.intraday_forward_qa_handoffs" in normalized:
            state["qa"] = {
                "qa_handoff_id": self.conn.qa_handoff_id,
                "report_revision_id": str(params[1]),
                "experiment_id": str(params[2]),
                "request_payload": json.loads(params[3]),
            }
        elif "from quant.intraday_forward_qa_handoffs" in normalized:
            qa = state["qa"]
            self.result = (
                qa["report_revision_id"], qa["experiment_id"],
                qa["request_payload"],
            )
        elif normalized.startswith("update quant.hypotheses"):
            if ("status <> 'archived'" in normalized
                    and state["hypothesis_status"] != "ARCHIVED"):
                state["hypothesis_status"] = str(params[0])
                self.rowcount = 1
            elif state["hypothesis_status"] == "INCONCLUSIVE":
                state["hypothesis_status"] = str(params[0])
                self.rowcount = 1
        elif normalized.startswith("select status from quant.hypotheses"):
            self.result = (state["hypothesis_status"],)
        else:
            raise AssertionError(f"unexpected publication SQL: {normalized}")

    def fetchone(self):
        return self.result


class _PublicationConnection:
    experiment_id = "00000000-0000-0000-0000-000000000031"
    hypothesis_id = "00000000-0000-0000-0000-000000000032"
    confirmation_id = "00000000-0000-0000-0000-000000000033"
    report_revision_id = "00000000-0000-0000-0000-000000000034"
    qa_handoff_id = "00000000-0000-0000-0000-000000000035"

    def __init__(self, *, governed_stock_evidence: bool = True):
        self.state = {"hypothesis_status": "INCONCLUSIVE"}
        self.governed_stock_evidence = governed_stock_evidence
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _PublicationCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _session_rows(count: int, *, start: date = date(2026, 8, 18)):
    rows = []
    known = datetime(2026, 7, 31, tzinfo=timezone.utc)
    for offset in range(count):
        day = start + timedelta(days=offset)
        opened = datetime.combine(
            day, datetime.min.time(), timezone.utc) + timedelta(hours=6)
        rows.append((day, opened, opened + timedelta(hours=7),
                     "00000000-0000-0000-0000-000000000099", 2,
                     "c" * 64, known))
    return rows


def test_forward_access_markers_are_all_committed_before_raw_replay(
        monkeypatch) -> None:
    days = (date(2026, 9, 1), date(2026, 9, 2))
    rung = SimpleNamespace(
        planned_session_dates=days,
        planned_instrument_ids=(
            "00000000-0000-0000-0000-000000000001",),
        rung_plan_fingerprint="a" * 64,
    )
    events = []

    def commit_access(_conn, **kwargs):
        day = kwargs["session_date"]
        events.append(day)
        return SimpleNamespace(
            session_access_id=f"access-{day}", inserted=True,
            access_fingerprint="b" * 64)

    monkeypatch.setattr(runner, "record_session_access", commit_access)
    result = runner._record_forward_accesses(
        object(), rung=rung,
        instrument_ids=list(rung.planned_instrument_ids),
        cutoff=datetime(2026, 9, 3, tzinfo=timezone.utc),
        spec=SimpleNamespace(purge_gap=timedelta(seconds=10)),
    )
    assert events == list(days)
    assert len(result) == 2


def test_actual_raw_digest_changes_when_ladder_content_changes() -> None:
    at = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)

    def quote(size: float) -> QuoteEvent:
        return QuoteEvent(
            event_time=at, received_at=at, observed_at=at,
            instrument_id="00000000-0000-0000-0000-000000000001",
            bid_prices=(100.0, 99.9), bid_sizes=(size, 20.0),
            ask_prices=(100.1, 100.2), ask_sizes=(11.0, 21.0),
            source_event_id="quote-1")

    first = runner._new_forward_raw_digest(at.date())
    runner._append_forward_raw_events(
        first, instrument_id=quote(10.0).instrument_id,
        quotes=[quote(10.0)], trades=[])
    first = runner._finish_forward_raw_digest(
        first, purge_gap=timedelta(seconds=5))
    changed = runner._new_forward_raw_digest(at.date())
    runner._append_forward_raw_events(
        changed, instrument_id=quote(10.5).instrument_id,
        quotes=[quote(10.5)], trades=[])
    changed = runner._finish_forward_raw_digest(
        changed, purge_gap=timedelta(seconds=5))

    assert first["quote_rows"] == changed["quote_rows"] == 1
    assert first["max_available_at"] == changed["max_available_at"]
    assert first["content_fingerprint"] != changed["content_fingerprint"]
    assert first["content_digest_version"] == "ACTUAL_RAW_REPLAY_V1"


def test_local_session_manifest_digest_changes_with_same_count_and_clock(
        ) -> None:
    at = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
    common = [
        ("market.market_quotes", 12, at, "11", "33"),
        ("market.market_ticks", 4, at, "44", "66"),
    ]
    changed = [
        ("market.market_quotes", 12, at, "11", "34"),
        ("market.market_ticks", 4, at, "44", "66"),
    ]
    kwargs = {
        "day": at.date(),
        "keys": ["00000000-0000-0000-0000-000000000001"],
        "cutoff": at + timedelta(hours=1),
        "event_source": runner.LOCAL_EVENT_SOURCE,
    }
    first_conn = _RowsConnection(common)
    changed_conn = _RowsConnection(changed)
    first = runner._session_exposure_evidence(first_conn, **kwargs)
    second = runner._session_exposure_evidence(changed_conn, **kwargs)

    assert first["quote_rows"] == second["quote_rows"] == 12
    assert first["trade_rows"] == second["trade_rows"] == 4
    assert first["content_fingerprint"] != second["content_fingerprint"]
    table = first["source_watermark"]["tables"][0]
    assert table["payload_digest_version"] == \
        runner.LOCAL_SOURCE_CONTENT_HASH_CONTRACT
    assert len(table["payload_multiset_digest"]) == 2
    sql = first_conn.cursor_instance.executed[0].lower()
    assert "hash_record_extended(quotes, 0)" in sql
    assert "hash_record_extended(ticks, 1)" in sql
    assert "bit_xor" in sql and "sum(h1)" in sql
    assert "jsonb_build_array" not in sql


def test_external_session_manifest_requires_typed_full_row_source_hash() -> None:
    day = date(2026, 9, 1)
    watermark = datetime(2026, 9, 2, tzinfo=timezone.utc)
    source_hash = "a" * 64
    valid = _RowsConnection([(
        "005930", 100, 25, source_hash,
        runner.EXTERNAL_SOURCE_CONTENT_HASH_CONTRACT,
        source_hash, watermark,
    )])
    report = runner._session_exposure_evidence(
        valid, day=day, keys=["005930"], cutoff=watermark,
        event_source=runner.EXTERNAL_EVENT_SOURCE)
    row = report["source_watermark"]["daily_manifest_rows"]
    assert row == 1
    assert report["source_watermark"]["source_content_hash_contract"] == \
        runner.EXTERNAL_SOURCE_CONTENT_HASH_CONTRACT
    sql = valid.cursor_instance.executed[0]
    assert "values->>'source_content_fingerprint'" in sql
    assert "values->>'source_content_hash_contract'" in sql
    assert "values->>'source_content_fingerprint'" in \
        runner._EXTERNAL_SLICE_EVIDENCE_SQL

    # A legacy input_hash alone is not content evidence after the builder
    # contract upgrade; deployment backfills these rows before enabling replay.
    legacy = _RowsConnection([(
        "005930", 100, 25, None, None, source_hash, watermark,
    )])
    with pytest.raises(RuntimeError, match="full-row SHA256"):
        runner._session_exposure_evidence(
            legacy, day=day, keys=["005930"], cutoff=watermark,
            event_source=runner.EXTERNAL_EVENT_SOURCE)

    unknown_contract = _RowsConnection([(
        "005930", 100, 25, source_hash, "legacy-v1", source_hash, watermark,
    )])
    with pytest.raises(RuntimeError, match="unknown content hash contract"):
        runner._session_exposure_evidence(
            unknown_contract, day=day, keys=["005930"], cutoff=watermark,
            event_source=runner.EXTERNAL_EVENT_SOURCE)

    mismatch = _RowsConnection([(
        "005930", 100, 25, source_hash,
        runner.EXTERNAL_SOURCE_CONTENT_HASH_CONTRACT,
        "b" * 64, watermark,
    )])
    with pytest.raises(RuntimeError, match="not bound to source content"):
        runner._session_exposure_evidence(
            mismatch, day=day, keys=["005930"], cutoff=watermark,
            event_source=runner.EXTERNAL_EVENT_SOURCE)


def test_forward_nomination_requires_exact_sole_open_gate() -> None:
    assert runner._is_forward_nominee({
        "decision": "HOLD",
        "failed_criteria": ["INDEPENDENT_FORWARD_CONFIRMATION_PENDING"],
    })
    for gate in (
        {"decision": "HOLD", "failed_criteria": []},
        {"decision": "SUBMIT_TO_QA", "failed_criteria": []},
        {"decision": "HOLD", "failed_criteria": [
            "INDEPENDENT_FORWARD_CONFIRMATION_PENDING", "OVERFIT_PBO"]},
        {"decision": "HOLD", "failed_criteria": [
            "INDEPENDENT_FORWARD_CONFIRMATION_PENDING",
            "FORWARD_COST_NET_EDGE_NOT_POSITIVE"]},
    ):
        assert runner._is_forward_nominee(gate) is False


def test_forward_candidate_query_uses_root_access_cutoff_and_frozen_forward(
        ) -> None:
    sql = " ".join(runner._FORWARD_CANDIDATES_SQL.lower().split())
    assert "from quant.intraday_session_accesses access" in sql
    assert "access.root_lineage_id = full_rung.root_lineage_id" in sql
    assert ("coalesce(forward_rung.lockbox_cutoff_session_date, "
            "root_access.latest_access_date) as search_cutoff") in sql


def test_forward_candidate_and_enqueue_share_fail_closed_stock_evidence(
        ) -> None:
    common = " ".join(
        runner._GOVERNED_FORWARD_STOCK_EVIDENCE.lower().split())
    for statement in (
            runner._FORWARD_CANDIDATES_SQL,
            runner._FORWARD_ENQUEUE_SQL):
        sql = " ".join(statement.lower().split())
        assert common in sql
        assert "evaluation_identity_complete" in sql
        assert "all-stock-full-replay-v1" in sql
        assert "intraday-forward-reproduction-runtime-v1" in sql
        assert "quant.current_krx_stock_instrument_identity" in sql


def test_h1_h2_h1_retry_reuses_h1_before_global_latest(monkeypatch) -> None:
    expression = {"const": 1.0, "unit": "RATIO"}
    semantic = {"event": "QUEUE_PRESSURE", "direction": "POSITIVE"}
    feature_spec = {"feature": "frozen"}
    label_spec = {"label": "frozen"}
    model_spec = {"model": "frozen"}
    family = runner._economic_family_id(semantic)
    h1 = SimpleNamespace(
        hypothesis_id="00000000-0000-0000-0000-000000000123",
        candidate_ast_fingerprint=runner.stable_fingerprint(expression),
        semantic_plan_fingerprint=runner.stable_fingerprint(semantic),
        baseline_ast_fingerprint=None,
        feature_spec_fingerprint=runner.stable_fingerprint(feature_spec),
        label_spec_fingerprint=runner.stable_fingerprint(label_spec),
        model_spec_fingerprint=runner.stable_fingerprint(model_spec),
        economic_family_id=family,
        evaluator_version=runner.EVALUATOR_VERSION,
        cost_model_version=runner.COST_MODEL_VERSION,
    )
    h2 = SimpleNamespace(
        hypothesis_id="00000000-0000-0000-0000-000000000456")
    global_calls = []

    monkeypatch.setattr(
        runner, "_find_same_hypothesis_ast_lineage",
        lambda *_args, **_kwargs: h1)

    def global_latest(*_args, **_kwargs):
        global_calls.append(True)
        return h2

    monkeypatch.setattr(runner, "find_latest_candidate_lineage", global_latest)
    monkeypatch.setattr(
        runner, "_candidate_specs",
        lambda *_args, **_kwargs: (feature_spec, label_spec, model_spec))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("idempotent retry attempted to re-register primary")

    monkeypatch.setattr(runner, "register_candidate_lineage", forbidden)
    primary, lineages = runner._register_trial_lineages(
        object(), hypothesis_id=h1.hypothesis_id, config={
            "intraday_signal_expr": expression,
            "semantic_plan": semantic,
            "horizon_seconds": 5,
            "execution": "TAKER",
            "entry_policy": "POSITIVE",
            "coefficient_policy": "FIXED_EQUATION",
            "screening_population": [],
        })
    assert primary is h1
    assert list(lineages.values()) == [h1]
    assert global_calls == []


def test_h1_h2_h1_retry_rejects_changed_h1_identity_before_global_latest(
        monkeypatch) -> None:
    expression = {"const": 1.0, "unit": "RATIO"}
    semantic = {"event": "QUEUE_PRESSURE", "direction": "POSITIVE"}
    feature_spec = {"feature": "frozen"}
    label_spec = {"label": "frozen"}
    frozen_model_spec = {"model": "frozen"}
    changed_model_spec = {"model": "changed"}
    h1 = SimpleNamespace(
        hypothesis_id="00000000-0000-0000-0000-000000000123",
        candidate_ast_fingerprint=runner.stable_fingerprint(expression),
        semantic_plan_fingerprint=runner.stable_fingerprint(semantic),
        baseline_ast_fingerprint=None,
        feature_spec_fingerprint=runner.stable_fingerprint(feature_spec),
        label_spec_fingerprint=runner.stable_fingerprint(label_spec),
        model_spec_fingerprint=runner.stable_fingerprint(frozen_model_spec),
        economic_family_id=runner._economic_family_id(semantic),
        evaluator_version=runner.EVALUATOR_VERSION,
        cost_model_version=runner.COST_MODEL_VERSION,
    )
    h2 = SimpleNamespace(
        hypothesis_id="00000000-0000-0000-0000-000000000456")
    global_calls = []

    monkeypatch.setattr(
        runner, "_find_same_hypothesis_ast_lineage",
        lambda *_args, **_kwargs: h1)

    def global_latest(*_args, **_kwargs):
        global_calls.append(True)
        return h2

    monkeypatch.setattr(runner, "find_latest_candidate_lineage", global_latest)
    monkeypatch.setattr(
        runner, "_candidate_specs",
        lambda *_args, **_kwargs: (
            feature_spec, label_spec, changed_model_spec))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("changed retry attempted to register a new node")

    monkeypatch.setattr(runner, "register_candidate_lineage", forbidden)
    with pytest.raises(RuntimeError, match="same-hypothesis.*model spec"):
        runner._register_trial_lineages(
            object(), hypothesis_id=h1.hypothesis_id, config={
                "intraday_signal_expr": expression,
                "semantic_plan": semantic,
                "horizon_seconds": 5,
                "execution": "TAKER",
                "entry_policy": "POSITIVE",
                "coefficient_policy": "FIXED_EQUATION",
                "screening_population": [],
            })
    assert global_calls == []


def test_same_ast_different_semantics_starts_new_root_and_preserves_baselines(
        monkeypatch) -> None:
    expression = {"op": "field", "field": "microprice_offset_bps"}
    sidecar_expr = {"op": "field", "field": "spread_bps"}
    primary_baseline = {"op": "field", "field": "queue_imbalance_l1"}
    sidecar_baseline = {"op": "field", "field": "queue_imbalance_l10"}
    follow_30 = {
        "event": "MICROPRICE_DISLOCATION", "context": ["ALL"],
        "qualities": ["PERSISTENCE"], "direction": "FOLLOW",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 30,
    }
    revert_600 = {
        **follow_30, "direction": "REVERT", "horizon_seconds": 600,
    }
    old_follow = SimpleNamespace(
        candidate_lineage_id="old-follow", root_lineage_id="old-follow-root",
        candidate_identity_fingerprint="1" * 64,
        hypothesis_id="00000000-0000-0000-0000-000000000111")
    lookup_contracts = []
    registered = []

    monkeypatch.setattr(
        runner, "_find_same_hypothesis_ast_lineage",
        lambda *_args, **_kwargs: None)

    def exact_lookup(_conn, *, source_contract=None,
                     candidate_identity=None):
        assert candidate_identity is None
        lookup_contracts.append(source_contract)
        # This represents an older row with the same AST.  It is a parent only
        # for the exact FOLLOW/30s contract, never for REVERT/600s.
        return old_follow if source_contract["semantic_plan"] == follow_30 \
            else None

    monkeypatch.setattr(runner, "find_latest_candidate_lineage", exact_lookup)

    def fake_register(_conn, **kwargs):
        registered.append(kwargs)
        parent = kwargs.get("parent")
        index = len(registered)
        return SimpleNamespace(
            candidate_lineage_id=f"new-{index}",
            root_lineage_id=(parent.root_lineage_id if parent else f"root-{index}"),
            parent_lineage_id=(parent.candidate_lineage_id if parent else None),
            candidate_identity_fingerprint=f"{index + 1}" * 64,
            hypothesis_id=kwargs["hypothesis_id"])

    monkeypatch.setattr(runner, "register_candidate_lineage", fake_register)
    monkeypatch.setattr(
        runner, "_candidate_specs",
        lambda _config, row: (
            {"feature": "frozen"},
            {"horizon_seconds": row["horizon_seconds"]},
            {"coefficient_policy": row["coefficient_policy"]},
        ))

    primary, lineages = runner._register_trial_lineages(
        object(), hypothesis_id=
        "00000000-0000-0000-0000-000000000222", config={
            "intraday_signal_expr": expression,
            "source_baseline_expr": primary_baseline,
            "semantic_plan": revert_600,
            "horizon_seconds": 600, "execution": "TAKER",
            "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
            "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
            "screening_population": [{
                "intraday_signal_expr": sidecar_expr,
                "ast_fingerprint": runner.fingerprint(sidecar_expr),
                "source_baseline_expr": sidecar_baseline,
                "semantic_plan": follow_30,
                "horizon_seconds": 30, "execution": "TAKER",
                "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
                "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
                "candidate_role": "LINKED_CANDIDATE",
            }],
        })

    assert lookup_contracts[0]["candidate_ast"] == expression
    assert lookup_contracts[0]["semantic_plan"] == revert_600
    assert lookup_contracts[0]["baseline_ast"] == primary_baseline
    assert primary.root_lineage_id != old_follow.root_lineage_id
    assert registered[0]["parent"] is None
    assert registered[0]["baseline_ast"] == primary_baseline
    assert registered[1]["baseline_ast"] == sidecar_baseline
    assert registered[1]["parent"] is primary
    assert len(lineages) == 2


def test_missing_explicit_screening_parent_fails_closed(monkeypatch) -> None:
    expression = {"op": "field", "field": "microprice_offset_bps"}
    child = {"op": "field", "field": "spread_bps"}
    plan = {
        "event": "MICROPRICE_DISLOCATION", "context": ["ALL"],
        "qualities": ["PERSISTENCE"], "direction": "FOLLOW",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 30,
    }
    primary = SimpleNamespace(
        candidate_lineage_id="primary", root_lineage_id="primary-root",
        candidate_identity_fingerprint="1" * 64,
        hypothesis_id="00000000-0000-0000-0000-000000000333")
    monkeypatch.setattr(
        runner, "_find_same_hypothesis_ast_lineage",
        lambda *_args, **_kwargs: primary)
    monkeypatch.setattr(
        runner, "_assert_same_hypothesis_retry_identity",
        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner, "_candidate_specs",
        lambda *_args, **_kwargs: ({"f": 1}, {"l": 1}, {"m": 1}))

    with pytest.raises(RuntimeError, match="missing or cyclic explicit parent"):
        runner._register_trial_lineages(
            object(), hypothesis_id=primary.hypothesis_id, config={
                "intraday_signal_expr": expression,
                "semantic_plan": plan,
                "horizon_seconds": 30, "execution": "TAKER",
                "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
                "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
                "screening_population": [{
                    "intraday_signal_expr": child,
                    "ast_fingerprint": runner.fingerprint(child),
                    "semantic_plan": plan,
                    "horizon_seconds": 30, "execution": "TAKER",
                    "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
                    "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
                    "parent_ast_fingerprint": "missing-parent",
                }],
            })


def test_explicit_population_parent_is_registered_before_evolved_primary(
        monkeypatch) -> None:
    parent_expr = {"op": "field", "field": "microprice_offset_bps"}
    child_expr = {"op": "neg", "arg": parent_expr}
    parent_fp = runner.fingerprint(parent_expr)
    plan = {
        "event": "MICROPRICE_DISLOCATION", "context": ["ALL"],
        "qualities": ["PERSISTENCE"], "direction": "REVERT",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 30,
    }
    registered = []
    lookups = []
    monkeypatch.setattr(
        runner, "_find_same_hypothesis_ast_lineage",
        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner, "_candidate_specs",
        lambda *_args, **_kwargs: ({"f": 1}, {"l": 1}, {"m": 1}))

    def lookup(_conn, **kwargs):
        lookups.append(kwargs)
        return None

    def register(_conn, **kwargs):
        registered.append(kwargs)
        parent = kwargs.get("parent")
        index = len(registered)
        return SimpleNamespace(
            candidate_lineage_id=f"lineage-{index}",
            root_lineage_id=(parent.root_lineage_id if parent else "root-parent"),
            parent_lineage_id=(parent.candidate_lineage_id if parent else None),
            candidate_identity_fingerprint=f"{index}" * 64,
            hypothesis_id=kwargs["hypothesis_id"])

    monkeypatch.setattr(runner, "find_latest_candidate_lineage", lookup)
    monkeypatch.setattr(runner, "register_candidate_lineage", register)
    primary, lineages = runner._register_trial_lineages(
        object(), hypothesis_id=
        "00000000-0000-0000-0000-000000000444", config={
            "intraday_signal_expr": child_expr,
            "source_baseline_expr": parent_expr,
            "parent_ast_fingerprint": parent_fp,
            "semantic_plan": plan,
            "horizon_seconds": 30, "execution": "TAKER",
            "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
            "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
            "screening_population": [{
                "intraday_signal_expr": parent_expr,
                "ast_fingerprint": parent_fp,
                "source_baseline_expr": parent_expr,
                "semantic_plan": plan,
                "horizon_seconds": 30, "execution": "TAKER",
                "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
                "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
                "candidate_role": "LINEAGE_PARENT",
            }],
        })

    assert registered[0]["candidate_ast"] == parent_expr
    assert registered[0]["parent"] is None
    assert registered[1]["candidate_ast"] == child_expr
    assert registered[1]["parent"].candidate_lineage_id == "lineage-1"
    assert primary.root_lineage_id == "root-parent"
    assert set(lineages) == {parent_fp, runner.fingerprint(child_expr)}
    assert len(lookups) == 1
    assert lookups[0]["source_contract"]["candidate_ast"] == parent_expr


def test_forward_pass_becomes_explicit_qa_handoff_without_auto_promotion() -> None:
    historical = {
        "decision": "HOLD",
        "failed_criteria": ["INDEPENDENT_FORWARD_CONFIRMATION_PENDING"],
        "evidence_tier": "SEARCH_EXPOSED_HISTORICAL_SUPPORT",
        "forward_lockbox": {"status": "AWAITING_NEW_SESSIONS"},
    }
    confirmation = {
        "forward_confirmation_id": "forward-1",
        "decision": "PASS", "gate_failures": [], "session_count": 20,
    }
    report = runner._overlay_forward_confirmation(historical, confirmation)
    assert report["decision"] == "SUBMIT_TO_QA"
    assert report["evidence_tier"] == "INDEPENDENT_FORWARD_CONFIRMATION"
    assert report["qa_handoff"]["status"] == "REQUESTED"
    assert report["qa_handoff"]["automatic_promotion"] is False
    assert report["qa_handoff"]["promotion_authority"] is False
    assert report["asset_contract"]["asset_class"] == "EQUITY"
    assert report["asset_contract"]["instrument_type"] == "STOCK"
    assert report["qa_handoff"]["asset_contract"]["asset_scope"] == \
        "KRX_ACTIVE_STOCK_ONLY"
    assert report["forward_lockbox"]["legacy_61_sessions_eligible"] is False

    durable = SimpleNamespace(
        forward_confirmation_id="forward-1", experiment_rung_id="rung-1",
        candidate_lineage_id="candidate-1", evidence_fingerprint="a" * 64)
    dimensions = runner._forward_metric_dimensions(durable, {
        "decision": "PASS", "gate_failures": []})
    assert dimensions["qa_submission_requested"] is True
    assert dimensions["promotion_authority"] is False
    assert dimensions["next_owner"] == "QA_REPRODUCTION"

    failed = runner._overlay_forward_confirmation(historical, {
        **confirmation,
        "decision": "FAIL",
        "gate_failures": ["FORWARD_COST_NET_EDGE_NOT_POSITIVE"],
    })
    assert failed["decision"] == "REJECT"
    assert failed["qa_handoff"]["status"] == "NOT_REQUESTED"
    assert runner._forward_outcome_decision("FAIL") == (
        "REJECT", "REJECTED", ["FORWARD_CONFIRMATION_FAILED"])

    inconclusive = runner._overlay_forward_confirmation(historical, {
        **confirmation,
        "decision": "INCONCLUSIVE",
        "gate_failures": ["FORWARD_SESSION_WITHOUT_TRADES"],
    })
    assert inconclusive["decision"] == "HOLD"
    assert runner._forward_outcome_decision("INCONCLUSIVE")[1] == \
        "INCONCLUSIVE"


def test_forward_publication_is_atomic_versioned_and_idempotent(
        monkeypatch) -> None:
    conn = _PublicationConnection()
    confirmation = {
        "forward_confirmation_id": conn.confirmation_id,
        "experiment_rung_id": "00000000-0000-0000-0000-000000000036",
        "candidate_lineage_id": "00000000-0000-0000-0000-000000000037",
        "decision": "PASS",
        "evidence_fingerprint": "c" * 64,
        "gate_statistics": {"fixed_horizon": True},
        "gate_failures": [],
        "decision_reason": "passed",
        "start_session": "2026-09-01",
        "end_session": "2026-09-20",
        "session_count": 20,
        "confirmed_at": "2026-09-21T00:00:00+00:00",
    }
    historical = {
        "decision": "SUBMIT_TO_QA",
        "failed_criteria": [],
        "evidence_tier": "INDEPENDENT_FORWARD_CONFIRMATION",
        "qa_handoff": {
            "status": "REQUESTED", "next_owner": "QA_REPRODUCTION",
            "automatic_promotion": False, "promotion_authority": False,
        },
    }
    monkeypatch.setattr(
        runner, "_load_forward_confirmation_status",
        lambda *_args, **_kwargs: confirmation)
    monkeypatch.setattr(
        runner, "_load_completed_report",
        lambda *_args, **_kwargs: historical)
    monkeypatch.setattr(
        runner, "_forward_runtime_artifact_attestation",
        lambda *_args, **_kwargs: {"reproduction_route_available": True})

    first = runner._publish_forward_finalization(conn, conn.experiment_id)
    assert first["decision"] == "PASS"
    assert first["hypothesis_status"] == "INCONCLUSIVE"
    assert first["requested_hypothesis_status"] == "SUPPORTED"
    assert first["qa_handoff_requested"] is True
    assert first["outcome_revision_id"] == conn.confirmation_id
    assert first["lifecycle"]["request_created"] is True
    assert first["lifecycle"]["not_a_promotion"]
    assert conn.state["qa"]["request_payload"]["outcome_revision_id"] == \
        conn.confirmation_id
    assert conn.state["hypothesis_status"] == "INCONCLUSIVE"
    assert conn.commits == 1
    assert not any("insert into research.experiment_outcomes " in sql
                   for sql in conn.executed)

    # A legacy optimistic state must be demoted in the publication transaction;
    # the immutable QA verdict is the only path back to SUPPORTED.
    legacy_supported = _PublicationConnection()
    legacy_supported.state["hypothesis_status"] = "SUPPORTED"
    legacy_result = runner._publish_forward_finalization(
        legacy_supported, legacy_supported.experiment_id)
    assert legacy_result["hypothesis_status"] == "INCONCLUSIVE"
    assert legacy_supported.state["hypothesis_status"] == "INCONCLUSIVE"

    inserts_after_first = sum(" insert into " in f" {sql} "
                              for sql in conn.executed)
    # A later archive is a valid downstream transition, not an incomplete
    # forward finalization that should be "repaired" back to SUPPORTED.
    conn.state["hypothesis_status"] = "ARCHIVED"
    second = runner._publish_forward_finalization(conn, conn.experiment_id)
    assert second["idempotent_retry"] is True
    assert second["report_fingerprint"] == first["report_fingerprint"]
    assert sum(" insert into " in f" {sql} "
               for sql in conn.executed) == inserts_after_first

    # Archival can also race the repair path after confirmation commit but
    # before publication. Preserve the monotonic archive while publishing the
    # missing immutable evidence and QA request.
    archived = _PublicationConnection()
    archived.state["hypothesis_status"] = "ARCHIVED"
    published_after_archive = runner._publish_forward_finalization(
        archived, archived.experiment_id)
    assert published_after_archive["hypothesis_status"] == "ARCHIVED"
    assert published_after_archive["requested_hypothesis_status"] == \
        "SUPPORTED"
    assert archived.state["hypothesis_status"] == "ARCHIVED"
    assert archived.state["qa"]["qa_handoff_id"] == archived.qa_handoff_id


def test_pass_publication_stops_before_writes_when_runtime_route_is_lost(
        monkeypatch) -> None:
    conn = _PublicationConnection()
    monkeypatch.setattr(
        runner, "_load_forward_confirmation_status",
        lambda *_args, **_kwargs: {
            "decision": "PASS",
            "forward_confirmation_id": conn.confirmation_id,
        })
    monkeypatch.setattr(
        runner, "_load_completed_report",
        lambda *_args, **_kwargs: {"reproduction_runtime": {}})
    monkeypatch.setattr(
        runner, "_forward_runtime_artifact_attestation",
        lambda *_args, **_kwargs: {
            "reproduction_route_available": False,
            "status": "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE",
        })

    with pytest.raises(RuntimeError, match="cannot be published"):
        runner._publish_forward_finalization(conn, conn.experiment_id)

    assert conn.state == {"hypothesis_status": "INCONCLUSIVE"}
    assert conn.commits == 0
    assert not any("insert into " in sql for sql in conn.executed)


def test_forward_pass_publication_rejects_incomplete_or_legacy_stock_evidence(
        monkeypatch) -> None:
    conn = _PublicationConnection(governed_stock_evidence=False)
    confirmation = {
        "forward_confirmation_id": conn.confirmation_id,
        "experiment_rung_id": "00000000-0000-0000-0000-000000000036",
        "candidate_lineage_id": "00000000-0000-0000-0000-000000000037",
        "decision": "PASS",
        "evidence_fingerprint": "c" * 64,
        "gate_statistics": {"fixed_horizon": True},
        "gate_failures": [],
        "decision_reason": "passed",
        "start_session": "2026-09-01",
        "end_session": "2026-09-20",
        "session_count": 20,
        "confirmed_at": "2026-09-21T00:00:00+00:00",
    }
    historical_loads = []
    monkeypatch.setattr(
        runner, "_load_forward_confirmation_status",
        lambda *_args, **_kwargs: confirmation)
    monkeypatch.setattr(
        runner, "_load_completed_report",
        lambda *_args, **_kwargs: historical_loads.append(True))

    with pytest.raises(
            RuntimeError,
            match="governed KRX ACTIVE STOCK-only FULL_60 evidence"):
        runner._publish_forward_finalization(conn, conn.experiment_id)

    assert historical_loads == []
    assert conn.state == {"hypothesis_status": "INCONCLUSIVE"}
    assert conn.commits == 0
    assert not any("insert into " in sql for sql in conn.executed)


def test_forward_sessions_are_first_n_complete_days_after_freeze_day() -> None:
    metadata = _RowsConnection(_session_rows(20, start=date(2026, 8, 18)))
    frozen_at = datetime(2026, 8, 17, 5, 0, tzinfo=timezone.utc)  # 14:00 KST
    cutoff = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    report = runner._forward_sessions(
        metadata,
        search_cutoff=date(2026, 8, 14), frozen_at=frozen_at,
        knowledge_cutoff=cutoff, required_sessions=20)

    assert report["status"] == "READY"
    assert report["sessions"][0] == date(2026, 8, 18)
    assert report["sessions"] == sorted(report["sessions"])
    assert report["current_kst_day_excluded"] is True
    assert report["historical_event_time_replay_eligible"] is False
    sql, params = metadata.cursor_instance.executed
    # The candidate-freeze day itself is excluded wholesale, so an afternoon-
    # only partial group cannot survive after its morning rows are filtered.
    assert params[1] == date(2026, 8, 18)
    assert "reference.market_calendar_versions" in sql
    assert "reference.market_sessions" in sql
    assert "market.market_quotes" not in sql
    assert report["calendar"]["calendar_version"] == 2
    assert report["raw_market_read_before_exposure"] is False
    assert "NO_QUOTE_TRADE_OR_RETURN_DATE_FILTER" in report["selection_rule"]
    assert "NO_SKIPPING" in report["selection_rule"]


def test_forward_sessions_wait_read_only_below_twenty() -> None:
    metadata = _RowsConnection(_session_rows(19))
    report = runner._forward_sessions(
        metadata,
        search_cutoff=date(2026, 8, 14),
        frozen_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        knowledge_cutoff=datetime(2026, 9, 20, tzinfo=timezone.utc),
        required_sessions=19)  # runtime minimum remains 20
    assert report["status"] == "WAITING"
    assert report["required_sessions"] == 20
    assert report["available_sessions"] == 19


def test_forward_dataset_cutoff_is_deterministic_not_worker_wall_clock() -> None:
    sessions = [date(2026, 8, 18) + timedelta(days=index)
                for index in range(20)]
    cohort = {
        "status": "READY", "sessions": sessions,
        "calendar": {"calendar_known_at": "2026-07-31T02:41:43+00:00"},
    }
    spec = SimpleNamespace(purge_gap=timedelta(seconds=30))
    cutoff = runner._deterministic_forward_dataset_cutoff(cohort, spec)
    expected = datetime.combine(
        sessions[-1] + timedelta(days=1), datetime.min.time(),
        runner.KST).astimezone(timezone.utc) + timedelta(seconds=30)
    assert cutoff == expected


def _forward_rung():
    sessions = tuple(date(2026, 9, 1) + timedelta(days=index)
                     for index in range(20))
    return SimpleNamespace(planned_session_dates=sessions)


def _forward_report(value: float = 2.0) -> dict:
    rung = _forward_rung()
    returns = {day.isoformat(): value for day in rung.planned_session_dates}
    return {
        "session_returns_bps": returns,
        "summary": {
            "opportunities": 1_000,
            "instrument_coverage": 0.95,
            "mean_net_bps_per_opportunity": value,
            "mean_implementation_drag_bps": 0.5,
        },
        "causality": [{"status": "PASS"}],
    }


def test_forward_gate_uses_all_sessions_and_fails_data_inconclusively() -> None:
    rung = _forward_rung()
    exposures = [{"quote_rows": 100, "trade_rows": 10}
                 for _ in rung.planned_session_dates]
    passed = runner._forward_gate(
        _forward_report(), rung=rung, exposure_evidence=exposures)
    assert passed["decision"] == "PASS"
    assert passed["gate_failures"] == []
    assert passed["statistics"]["fixed_horizon"] is True
    assert passed["statistics"]["score_or_model_refit"] is False
    assert passed["statistics"]["forward_test_index"] == 1
    assert passed["statistics"]["two_sided_alpha_spent"] == 0.05

    later = runner._forward_gate(
        _forward_report(), rung=rung, exposure_evidence=exposures,
        forward_test_index=2)
    assert later["statistics"]["two_sided_alpha_spent"] < 0.05
    assert later["statistics"]["one_sided_familywise_alpha_budget"] == 0.05

    exposures[4]["trade_rows"] = 0
    inconclusive = runner._forward_gate(
        _forward_report(), rung=rung, exposure_evidence=exposures)
    assert inconclusive["decision"] == "INCONCLUSIVE"
    assert "FORWARD_SESSION_WITHOUT_TRADES" in inconclusive["gate_failures"]
    # The no-trade day remains in the exact 20-day return vector; it was not
    # filtered out while discovering the cohort.
    assert len(inconclusive["statistics"]["session_returns_bps"]) == 20


def test_forward_gate_rejects_negative_cost_net_effect() -> None:
    rung = _forward_rung()
    exposures = [{"quote_rows": 100, "trade_rows": 10}
                 for _ in rung.planned_session_dates]
    failed = runner._forward_gate(
        _forward_report(-1.0), rung=rung, exposure_evidence=exposures)
    assert failed["decision"] == "FAIL"
    assert "FORWARD_COST_NET_EDGE_NOT_POSITIVE" in failed["gate_failures"]


def test_forward_gate_blocks_when_frozen_runtime_has_no_execution_route() -> None:
    runtime = runner._qa_reproduction_runtime_manifest(
        hypothesis_id="20000000-0000-0000-0000-000000000004",
        config={"asset_scope": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY"},
    )
    exact_gate, exact = runner._forward_gate_with_runtime_artifact(
        {"decision": "PASS", "gate_failures": [], "statistics": {}},
        {"reproduction_runtime": runtime},
    )
    assert exact["reproduction_route_available"] is True
    assert exact_gate["decision"] == "PASS"
    assert exact_gate["statistics"]["performance_evidence_authority"] is True

    source = runtime["source_manifest"]
    first_path = sorted(source["files"])[0]
    source["files"][first_path] = "0" * 64
    source["source_fingerprint"] = runner.stable_fingerprint({
        "version": source["version"],
        "files": source["files"],
    })
    runtime["runtime_manifest_fingerprint"] = runner.stable_fingerprint({
        key: value for key, value in runtime.items()
        if key != "runtime_manifest_fingerprint"
    })

    blocked_gate, blocked = runner._forward_gate_with_runtime_artifact(
        {"decision": "PASS", "gate_failures": [], "statistics": {}},
        {"reproduction_runtime": runtime},
    )
    assert blocked["reproduction_route_available"] is False
    assert blocked["status"] == "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE"
    assert "runtime_source_set" in blocked["mismatches"]
    assert blocked_gate["decision"] == "INCONCLUSIVE"
    assert blocked_gate["gate_failures"] == [
        "FORWARD_RUNTIME_ARTIFACT_UNAVAILABLE"]
    assert blocked_gate["statistics"][
        "performance_evidence_authority"] is False


def _qa_reproduction_bundle() -> tuple[dict, dict]:
    sessions = tuple(date(2026, 9, 1) + timedelta(days=index)
                     for index in range(20))
    instruments = (
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    )
    session_fp = runner.stable_fingerprint(
        [value.isoformat() for value in sessions])
    instrument_fp = runner.stable_fingerprint(list(instruments))
    hypothesis_id = "20000000-0000-0000-0000-000000000004"
    frozen_config = {
        "asset_scope": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
        "fast_screen_min_opportunities": 100,
    }
    reproduction_runtime = runner._qa_reproduction_runtime_manifest(
        hypothesis_id=hypothesis_id, config=frozen_config)
    report_revision = {
        "score_calibration": {"status": "PASS"},
        "reproduction_runtime": reproduction_runtime,
    }
    raw = {
        value.isoformat(): {
            "content_fingerprint": f"{index + 1:064x}",
            "quote_rows": 100,
            "trade_rows": 10,
        } for index, value in enumerate(sessions)
    }
    replay = _forward_report()
    replay["forward_replay"] = {"session_evidence": raw}
    gate = runner._forward_gate(
        replay, rung=SimpleNamespace(planned_session_dates=sessions),
        exposure_evidence=[{"quote_rows": 100, "trade_rows": 10}
                           for _ in sessions],
        forward_test_index=1)
    bundle = {
        "contract_version": runner.QA_REPRODUCTION_INPUT_VERSION,
        "work_item": {
            "work_item_id": "10000000-0000-0000-0000-000000000001",
            "reproduction_request_id":
                "10000000-0000-0000-0000-000000000002",
            "lease_token": "10000000-0000-0000-0000-000000000003",
        },
        "request": {
            "payload_fingerprint": "4" * 64,
            "reproduction_contract": {
                "requested_action": "INDEPENDENT_QA_REPRODUCTION",
                "promotion_authority": False,
                "asset_class": "EQUITY",
                "instrument_type": "STOCK",
                "asset_scope": "KRX_ACTIVE_STOCK_ONLY",
                "product_filter": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
                "experiment_id": "20000000-0000-0000-0000-000000000001",
                "hypothesis_id": hypothesis_id,
                "forward_confirmation_id":
                    "20000000-0000-0000-0000-000000000002",
                "report_revision_id":
                    "20000000-0000-0000-0000-000000000003",
                "instrument_count": len(instruments),
                "instrument_set_fingerprint": instrument_fp,
                "session_count": len(sessions),
                "session_set_fingerprint": session_fp,
                "rung_plan_fingerprint": "5" * 64,
                "confirmation_evidence_fingerprint": "6" * 64,
            },
        },
        "experiment": {
            "experiment_id": "20000000-0000-0000-0000-000000000001",
            "hypothesis_id": hypothesis_id,
            "input_hash": reproduction_runtime["experiment_input_hash"],
            "code_version": runner.RUNNER_VERSION,
            "cost_model_version": runner.COST_MODEL_VERSION,
        },
        "candidate": {
            "candidate_lineage_id":
                "30000000-0000-0000-0000-000000000001",
            "root_lineage_id":
                "30000000-0000-0000-0000-000000000001",
            "candidate_identity_fingerprint": "7" * 64,
        },
        "forward_rung": {
            "experiment_rung_id":
                "30000000-0000-0000-0000-000000000002",
            "candidate_lineage_id":
                "30000000-0000-0000-0000-000000000001",
            "root_lineage_id":
                "30000000-0000-0000-0000-000000000001",
            "planned_session_dates": [value.isoformat() for value in sessions],
            "planned_session_count": len(sessions),
            "planned_instrument_ids": list(instruments),
            "planned_instrument_count": len(instruments),
            "session_set_fingerprint": session_fp,
            "instrument_set_fingerprint": instrument_fp,
            "rung_plan_fingerprint": "5" * 64,
            "dataset_cutoff": "2026-10-01T00:00:00+00:00",
            "forward_test_index": 1,
        },
        "report_revision": {
            "report_revision_id":
                "20000000-0000-0000-0000-000000000003",
            "report_fingerprint": runner.stable_fingerprint(report_revision),
            "report": report_revision,
        },
        "confirmation": {
            "forward_confirmation_id":
                "20000000-0000-0000-0000-000000000002",
            "decision": "PASS",
            "gate_version": runner.FORWARD_GATE_VERSION,
            "gate_statistics": gate["statistics"],
            "gate_failures": [],
            "confirmation_evidence_fingerprint": "6" * 64,
        },
        "session_exposures": [{
            "session_date": session,
            "session_content_fingerprint": evidence[
                "content_fingerprint"],
            "quote_row_count": evidence["quote_rows"],
            "trade_row_count": evidence["trade_rows"],
            "instrument_set_fingerprint": instrument_fp,
            "instrument_count": len(instruments),
        } for session, evidence in raw.items()],
    }
    return bundle, replay


def test_qa_reproduction_is_write_free_exact_and_stock_only(monkeypatch) -> None:
    bundle, replay = _qa_reproduction_bundle()
    # A mutable metadata projection is deliberately hostile.  The evaluator
    # must execute only the config frozen inside the immutable report revision.
    bundle["experiment"]["config"] = {
        "asset_scope": "ALL_PRODUCTS", "threshold": 999999}
    monkeypatch.setattr(runner, "_validate_frozen_candidate",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_forward_spec", lambda _config: object())
    calls = []

    def evaluate(market, **kwargs):
        calls.append((market, kwargs))
        return replay

    monkeypatch.setattr(runner, "_evaluate_forward_replay", evaluate)
    market = object()
    result = runner.reproduce_forward_confirmation(market, bundle)

    assert result["verdict"] == "PASS"
    assert result["failed_checks"] == []
    assert result["stock_only"] is True
    assert result["score_or_model_refit"] is False
    assert result["promotion_authority"] is False
    assert len(result["result_fingerprint"]) == 64
    assert calls[0][0] is market
    assert calls[0][1]["config"] == bundle["report_revision"]["report"][
        "reproduction_runtime"]["frozen_config"]
    assert calls[0][1]["instrument_ids"] == \
        bundle["forward_rung"]["planned_instrument_ids"]


def test_qa_reproduction_rejects_changed_runtime_identity(monkeypatch) -> None:
    bundle, _replay = _qa_reproduction_bundle()
    bundle["experiment"]["input_hash"] = "f" * 64

    with pytest.raises(RuntimeError, match="immutable evidence"):
        runner.reproduce_forward_confirmation(object(), bundle)


def test_qa_reproduction_completes_inconclusive_when_old_runtime_is_absent(
        monkeypatch) -> None:
    bundle, _replay = _qa_reproduction_bundle()
    monkeypatch.setattr(runner, "_validate_frozen_candidate",
                        lambda *_args, **_kwargs: None)
    report = bundle["report_revision"]["report"]
    runtime = report["reproduction_runtime"]
    source = runtime["source_manifest"]
    first_path = sorted(source["files"])[0]
    source["files"][first_path] = "0" * 64
    source["source_fingerprint"] = runner.stable_fingerprint({
        "version": source["version"],
        "files": source["files"],
    })
    runtime["runtime_manifest_fingerprint"] = runner.stable_fingerprint({
        key: value for key, value in runtime.items()
        if key != "runtime_manifest_fingerprint"
    })
    bundle["report_revision"]["report_fingerprint"] = \
        runner.stable_fingerprint(report)

    monkeypatch.setattr(
        runner,
        "_evaluate_forward_replay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unavailable old runtime must not execute")),
    )
    result = runner.reproduce_forward_confirmation(object(), bundle)

    assert result["verdict"] == "INCONCLUSIVE"
    assert result["reason_code"] == "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE"
    assert result["failed_checks"] == ["runtime_artifact_available"]
    assert result["promotion_authority"] is False
    assert len(result["result_fingerprint"]) == 64


def test_normal_runner_version_upgrade_is_metadata_only_inconclusive(
        monkeypatch) -> None:
    bundle, _replay = _qa_reproduction_bundle()
    frozen_version = bundle["report_revision"]["report"][
        "reproduction_runtime"]["code_version"]
    monkeypatch.setattr(runner, "RUNNER_VERSION", frozen_version + "-next")
    monkeypatch.setattr(
        runner, "_evaluate_forward_replay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("version-mismatched runtime must not replay")),
    )

    preflight = runner.preflight_qa_reproduction_runtime(bundle)
    result = runner.reproduce_forward_confirmation(object(), bundle)

    assert preflight["reproduction_route_available"] is False
    assert "runtime_code_version" in preflight["mismatches"]
    assert result["verdict"] == "INCONCLUSIVE"
    assert result["reason_code"] == "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE"


def test_runtime_source_manifest_covers_real_ast_and_evidence_dependencies() -> None:
    paths = set(runner._QA_REPRODUCTION_SOURCE_PATHS)
    assert "departments/01-research/contracts/intraday_ast_contract.py" in paths
    assert "departments/04-quant-backtest/pipeline/intraday_trial_ledger.py" in paths
    assert "departments/04-quant-backtest/pipeline/stock_universe.py" in paths
    manifest = runner._qa_reproduction_source_manifest()
    assert set(manifest["files"]) == paths


def test_forward_runtime_mismatch_stops_before_candidate_or_market_work(
        monkeypatch) -> None:
    forbidden_calls = []

    def forbidden(*_args, **_kwargs):
        forbidden_calls.append(True)
        raise AssertionError("unavailable runtime touched candidate or market")

    monkeypatch.setattr(
        runner, "_forward_runtime_artifact_attestation",
        lambda *_args, **_kwargs: {
            "reproduction_route_available": False,
            "status": "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE",
        })
    monkeypatch.setattr(runner, "load_candidate_lineage", forbidden)
    monkeypatch.setattr(runner, "_evaluate_forward_replay", forbidden)
    monkeypatch.setattr(runner, "allocate_experiment_rung", forbidden)
    result = runner.run_forward_confirmation(
        object(), object(), {
            "experiment_id": "experiment",
            "candidate_lineage_id": "lineage",
            "final_gate": {"decision": "HOLD", "failed_criteria": [
                "INDEPENDENT_FORWARD_CONFIRMATION_PENDING"]},
            "config": {
                "asset_scope": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY"},
            "governance_report": {},
        })

    assert result["status"] == "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE"
    assert result["decision"] == "INCONCLUSIVE"
    assert forbidden_calls == []


def test_qa_reproduction_completes_as_fail_on_raw_digest_mismatch(
        monkeypatch) -> None:
    bundle, replay = _qa_reproduction_bundle()
    replay["forward_replay"]["session_evidence"][
        "2026-09-01"]["content_fingerprint"] = "f" * 64
    monkeypatch.setattr(runner, "_validate_frozen_candidate",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_forward_spec", lambda _config: object())
    monkeypatch.setattr(runner, "_evaluate_forward_replay",
                        lambda *_args, **_kwargs: replay)

    result = runner.reproduce_forward_confirmation(object(), bundle)

    assert result["verdict"] == "FAIL"
    assert "raw_session_evidence_exact" in result["failed_checks"]
    assert result["promotion_authority"] is False


def test_forward_orchestrator_orders_access_raw_replay_then_evidence(
        monkeypatch) -> None:
    order = []
    candidate = SimpleNamespace(
        candidate_lineage_id="lineage", candidate_identity_fingerprint="a" * 64,
        candidate_ast_fingerprint="b" * 64)
    full = SimpleNamespace(planned_instrument_ids=("stock-a", "stock-b"))
    forward = SimpleNamespace(
        experiment_rung_id="forward",
        planned_session_dates=tuple(
            date(2026, 9, 1) + timedelta(days=index) for index in range(20)),
        rung_plan_fingerprint="c" * 64)
    monkeypatch.setattr(runner, "load_candidate_lineage",
                        lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(runner, "_validate_frozen_candidate",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "load_experiment_rung",
                        lambda *_args, **kwargs: full if kwargs["rung"] == runner.FULL_60 else forward)
    def stock_scope(*_args, **kwargs):
        order.append("forward-stock-scope" if kwargs.get("session_dates")
                     is not None else "full-stock-scope")
        return ["stock-a", "stock-b"]

    monkeypatch.setattr(runner, "_forward_stock_universe", stock_scope)
    monkeypatch.setattr(
        runner, "_forward_spec",
        lambda _config: SimpleNamespace(purge_gap=timedelta(seconds=5)))
    monkeypatch.setattr(runner, "_forward_sessions", lambda *_args, **_kwargs: {
        "status": "READY", "sessions": list(forward.planned_session_dates),
        "session_manifest": [], "available_sessions": 20,
        "required_sessions": 20,
        "calendar": {"calendar_known_at": "2026-07-31T00:00:00+00:00"}})
    allocated = {}

    def allocate(*_args, **kwargs):
        allocated.update(kwargs)
        return forward

    monkeypatch.setattr(runner, "allocate_experiment_rung", allocate)

    def accesses(*_args, **_kwargs):
        order.append("accesses")
        return {day.isoformat(): SimpleNamespace()
                for day in forward.planned_session_dates}

    monkeypatch.setattr(runner, "_record_forward_accesses", accesses)

    def exposures(*_args, **_kwargs):
        order.append("exposures")
        return [{"quote_rows": 1, "trade_rows": 1,
                 "evidence_fingerprint": "d" * 64} for _ in range(20)]

    monkeypatch.setattr(runner, "_record_forward_exposures", exposures)

    def replay(*_args, **_kwargs):
        order.append("raw")
        report = _forward_report()
        report["forward_replay"] = {"session_evidence": {
            day.isoformat(): {
                "content_fingerprint": "c" * 64,
                "content_digest_version": "ACTUAL_RAW_REPLAY_V1",
                "quote_rows": 1, "trade_rows": 1,
            } for day in forward.planned_session_dates}}
        return report

    monkeypatch.setattr(runner, "_evaluate_forward_replay", replay)
    monkeypatch.setattr(runner, "_forward_test_index",
                        lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(runner, "_forward_gate", lambda *_args, **_kwargs: {
        "decision": "PASS", "gate_failures": [], "statistics": {}})
    monkeypatch.setattr(
        runner, "_forward_runtime_artifact_attestation",
        lambda _report: {
            "reproduction_route_available": True,
            "status": "CURRENT_RUNTIME_EXACT",
        })
    confirmation = SimpleNamespace(
        forward_confirmation_id="confirmation", experiment_rung_id="forward",
        candidate_lineage_id="lineage", evidence_fingerprint="e" * 64)
    monkeypatch.setattr(runner, "record_forward_confirmation",
                        lambda *_args, **_kwargs: confirmation)
    monkeypatch.setattr(runner, "_persist_forward_metric",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_publish_forward_finalization",
                        lambda *_args, **_kwargs: {"status": "PUBLISHED"})
    row = {
        "experiment_id": "experiment", "dataset_id": "dataset",
        "candidate_lineage_id": "lineage",
        "final_gate": {"decision": "HOLD", "failed_criteria": [
            "INDEPENDENT_FORWARD_CONFIRMATION_PENDING"]},
        "config": {
            "asset_scope": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
            "forward_confirmation_min_new_sessions": 20,
            "fast_screen_min_opportunities": 100,
        },
        "governance_report": {}, "score_calibration": {"ok": True},
        "frozen_at": datetime(2026, 8, 17, tzinfo=timezone.utc),
        "search_cutoff": date(2026, 8, 14), "forward_rung_id": None,
    }
    result = runner.run_forward_confirmation(
        object(), object(), row,
        now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert result["decision"] == "PASS"
    assert order == [
        "full-stock-scope", "forward-stock-scope",
        "accesses", "raw", "exposures"]
    assert allocated["instrument_ids"] == ["stock-a", "stock-b"]
    assert allocated["session_dates"] == list(forward.planned_session_dates)
    assert allocated["rung"] == runner.FORWARD
    assert allocated["predecessor"] is full


def test_forward_stock_universe_rejects_spac_labeled_as_stock() -> None:
    instrument_id = "00000000-0000-0000-0000-000000000001"
    full = SimpleNamespace(
        planned_instrument_ids=(instrument_id,),
        planned_session_dates=(date(2026, 8, 14),),
    )
    meta = _RowsConnection([
        (instrument_id, "STOCK", "EQUITY", "KRX", "ACTIVE",
         date(2020, 1, 1), None, True),
    ])

    with pytest.raises(RuntimeError, match="STOCK/SPAC/listing"):
        runner._forward_stock_universe(meta, full)
    sql = meta.cursor_instance.executed[0].lower()
    assert "from quant.current_krx_stock_instrument_identity" in sql
    assert "reference.instruments" not in sql
    assert "metadata" not in sql


def test_forward_stock_universe_checks_exact_new_session_dates() -> None:
    instrument_id = "00000000-0000-0000-0000-000000000001"
    full = SimpleNamespace(
        planned_instrument_ids=(instrument_id,),
        planned_session_dates=(date(2026, 8, 14),),
    )
    meta = _RowsConnection([
        (instrument_id, "STOCK", "EQUITY", "KRX", "ACTIVE",
         date(2020, 1, 1), date(2026, 8, 31), False),
    ])

    with pytest.raises(RuntimeError, match="STOCK/SPAC/listing"):
        runner._forward_stock_universe(
            meta, full, session_dates=(date(2026, 9, 1),))


def test_waiting_candidate_does_not_allocate_expose_or_read_raw(monkeypatch) -> None:
    candidate = SimpleNamespace(candidate_lineage_id="lineage")
    full = SimpleNamespace(planned_instrument_ids=("stock-a", "stock-b"))
    monkeypatch.setattr(runner, "load_candidate_lineage",
                        lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(runner, "_validate_frozen_candidate",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "load_experiment_rung",
                        lambda *_args, **_kwargs: full)
    monkeypatch.setattr(runner, "_forward_stock_universe",
                        lambda *_args, **_kwargs: ["stock-a", "stock-b"])
    monkeypatch.setattr(runner, "_forward_spec", lambda _config: object())
    monkeypatch.setattr(
        runner, "_forward_runtime_artifact_attestation",
        lambda *_args, **_kwargs: {"reproduction_route_available": True})
    monkeypatch.setattr(runner, "_forward_sessions", lambda *_args, **_kwargs: {
        "status": "WAITING", "sessions": [], "available_sessions": 19,
        "required_sessions": 20})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("WAITING path performed a write or raw replay")

    monkeypatch.setattr(runner, "allocate_experiment_rung", forbidden)
    monkeypatch.setattr(runner, "_record_forward_accesses", forbidden)
    monkeypatch.setattr(runner, "_record_forward_exposures", forbidden)
    monkeypatch.setattr(runner, "_evaluate_forward_replay", forbidden)
    row = {
        "experiment_id": "experiment", "dataset_id": "dataset",
        "candidate_lineage_id": "lineage",
        "final_gate": {"decision": "HOLD", "failed_criteria": [
            "INDEPENDENT_FORWARD_CONFIRMATION_PENDING"]},
        "config": {
            "asset_scope": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
            "forward_confirmation_min_new_sessions": 20,
        },
        "governance_report": {}, "score_calibration": {},
        "frozen_at": datetime(2026, 8, 17, tzinfo=timezone.utc),
        "search_cutoff": date(2026, 8, 14), "forward_rung_id": None,
    }
    result = runner.run_forward_confirmation(
        object(), object(), row,
        now=datetime(2026, 9, 20, tzinfo=timezone.utc))
    assert result["status"] == "WAITING_FOR_NEW_LOCAL_SESSIONS"
    assert result["available_sessions"] == 19
    assert result["historical_61_session_reuse"] is False


def test_forward_work_release_persists_waiting_and_error_backoff() -> None:
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    claim = {
        "experiment_id": "00000000-0000-0000-0000-000000000001",
        "lease_token": "00000000-0000-0000-0000-000000000002",
        "attempt_count": 7,
        "error_count": 2,
        "max_error_count": 8,
    }
    waiting_conn = _UpdateConnection()
    assert runner._finish_forward_work_item(
        waiting_conn, claim=claim,
        result={"status": "WAITING_FOR_NEW_LOCAL_SESSIONS"},
        worker="worker-a", now=now)
    _, waiting_params = waiting_conn.executed[-1]
    assert waiting_params[0] == "WAITING"
    assert waiting_params[1] == now + timedelta(
        hours=runner.FORWARD_WAIT_RETRY_HOURS)
    assert waiting_params[2] == 2  # scientific waiting is not an error
    assert waiting_params[-2:] == ("worker-a", claim["lease_token"])

    error_conn = _UpdateConnection()
    assert runner._finish_forward_work_item(
        error_conn, claim=claim,
        result={"status": "ERROR", "error": "temporary database outage"},
        worker="worker-a", now=now)
    _, error_params = error_conn.executed[-1]
    assert error_params[0] == "RETRY"
    assert error_params[1] == now + timedelta(minutes=60)
    assert error_params[2] == 3
    assert "temporary database outage" in error_params[4]
    assert error_conn.commits == 1 and error_conn.rollbacks == 0

    terminal_conn = _UpdateConnection()
    terminal_claim = {**claim, "error_count": 7}
    assert runner._finish_forward_work_item(
        terminal_conn, claim=terminal_claim,
        result={"status": "ERROR", "error": "eighth operational failure"},
        worker="worker-a", now=now)
    _, terminal_params = terminal_conn.executed[-1]
    assert terminal_params[0] == "FAILED"
    assert terminal_params[1] is None
    assert terminal_params[2] == 8
    assert "eighth operational failure" in terminal_params[4]

    artifact_conn = _UpdateConnection()
    assert runner._finish_forward_work_item(
        artifact_conn, claim=claim,
        result={"status": "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE"},
        worker="worker-a", now=now)
    _, artifact_params = artifact_conn.executed[-1]
    assert artifact_params[0] == "FAILED"
    assert artifact_params[1] is None
    assert artifact_params[2] == claim["max_error_count"]
    assert artifact_params[4] == "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE"


def test_expired_forward_lease_spends_bounded_error_budget() -> None:
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    conn = _UpdateConnection()
    assert runner._expire_stale_forward_leases(conn, now=now) == 1
    sql, params = conn.executed[-1]
    lowered = sql.lower()
    assert "where work.status='leased'" in lowered
    assert "work.lease_expires_at <=" in lowered
    assert "work.error_count + 1" in lowered
    assert "then 'failed'" in lowered
    assert "else 'retry'" in lowered
    assert params[-2:] == (now, now)
    assert conn.commits == 1


def test_forward_heartbeat_is_owner_token_and_expiry_fenced() -> None:
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    claim = {
        "experiment_id": "00000000-0000-0000-0000-000000000001",
        "lease_token": "00000000-0000-0000-0000-000000000002",
    }
    conn = _UpdateConnection()
    assert runner._heartbeat_forward_work_item(
        conn, claim=claim, worker="worker-a", now=now)
    sql, params = conn.executed[-1]
    lowered = sql.lower()
    assert "and leased_by=%s" in lowered
    assert "and lease_token=%s::uuid" in lowered
    assert "and lease_expires_at > %s::timestamptz" in lowered
    assert params[-3:] == ("worker-a", claim["lease_token"], now)


def test_publication_repair_is_due_bounded_and_does_not_steal_live_lease(
        monkeypatch) -> None:
    experiment_id = "00000000-0000-0000-0000-000000000041"
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    conn = _RowsConnection([(experiment_id,)])
    failures = []
    monkeypatch.setattr(
        runner, "_publish_forward_finalization",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("publication unavailable")))
    monkeypatch.setattr(
        runner, "_record_forward_publication_repair_failure",
        lambda _conn, **kwargs: failures.append(kwargs))
    result = runner._repair_forward_publications(conn, limit=3, now=now)
    sql, params = conn.cursor_instance.executed
    lowered = " ".join(sql.lower().split())
    assert "work.status not in ('failed', 'leased')" in lowered
    assert "work.next_attempt_at <= %s::timestamptz" in lowered
    assert params == (now, 3)
    assert result["finalized"] == []
    assert result["failed"][0]["experiment_id"] == experiment_id
    assert failures == [{
        "experiment_id": experiment_id,
        "error": "RuntimeError: publication unavailable",
        "now": now,
    }]


def test_reconciliation_requires_complete_semantic_publication() -> None:
    conn = _UpdateConnection()
    assert runner._reconcile_forward_work_items(conn) == 1
    sql, _params = conn.executed[-1]
    lowered = sql.lower()
    assert "intraday_forward_report_revisions" in lowered
    assert "experiment_outcome_revisions" in lowered
    assert "intraday_forward_qa_handoffs" in lowered
    assert "outcome.decision = case confirmation.decision" in lowered
    assert "when 'pass' then 'submit_to_qa'" in lowered
    assert "when 'fail' then 'reject'" in lowered
    assert "when 'inconclusive' then 'gate_hold'" in lowered
    assert "confirmation.decision <> 'pass'" in lowered
    assert "('inconclusive', 'supported', 'rejected')" in lowered


def test_forward_sweep_processes_later_nominee_when_oldest_is_waiting(
        monkeypatch) -> None:
    old_id = "00000000-0000-0000-0000-000000000011"
    new_id = "00000000-0000-0000-0000-000000000012"
    claims = [{
        "experiment_id": old_id,
        "lease_token": "00000000-0000-0000-0000-000000000021",
        "attempt_count": 9,
        "error_count": 0,
    }, {
        "experiment_id": new_id,
        "lease_token": "00000000-0000-0000-0000-000000000022",
        "attempt_count": 1,
        "error_count": 0,
    }]
    monkeypatch.setattr(runner, "_repair_forward_metrics", lambda _conn: 0)
    monkeypatch.setattr(runner, "_repair_forward_publications",
                        lambda _conn, *, limit: {
                            "finalized": [], "failed": []})
    monkeypatch.setattr(runner, "_reconcile_forward_work_items",
                        lambda _conn: 0)
    monkeypatch.setattr(runner, "_enqueue_forward_candidates",
                        lambda _conn: 0)
    monkeypatch.setattr(runner, "_lease_forward_work_items",
                        lambda *_args, **_kwargs: claims)
    monkeypatch.setattr(runner, "_heartbeat_forward_work_item",
                        lambda *_args, **_kwargs: True)
    monkeypatch.setattr(runner, "_forward_candidate_rows",
                        lambda *_args, **_kwargs: [
                            {"experiment_id": old_id},
                            {"experiment_id": new_id},
                        ])

    processed = []

    def run_one(_meta, _market, row, **_kwargs):
        processed.append(row["experiment_id"])
        return {
            "experiment_id": row["experiment_id"],
            "status": ("WAITING_FOR_NEW_LOCAL_SESSIONS"
                       if row["experiment_id"] == old_id else "CONFIRMED"),
        }

    releases = []
    monkeypatch.setattr(runner, "run_forward_confirmation", run_one)
    monkeypatch.setattr(
        runner, "_finish_forward_work_item",
        lambda _conn, *, claim, result, **_kwargs: releases.append(
            (claim["experiment_id"], result["status"])) is None or True)

    result = runner.run_forward_confirmations(
        object(), object(), limit=2,
        now=datetime(2026, 9, 20, tzinfo=timezone.utc), worker="worker-a")
    assert processed == [old_id, new_id]
    assert releases == [
        (old_id, "WAITING_FOR_NEW_LOCAL_SESSIONS"),
        (new_id, "CONFIRMED"),
    ]
    assert result["checked"] == result["leased"] == 2
    lease_sql = " ".join(runner._FORWARD_LEASE_SQL.lower().split())
    assert "for update skip locked" in lease_sql
    assert "lease_token = gen_random_uuid()" in lease_sql
    assert "and not exists" in lease_sql


def test_empty_forward_sweep_runs_repair_enqueue_and_fair_lease(monkeypatch) -> None:
    monkeypatch.setattr(runner, "_repair_forward_metrics", lambda _conn: 0)
    monkeypatch.setattr(runner, "_repair_forward_publications",
                        lambda _conn, *, limit: {
                            "finalized": [], "failed": []})
    monkeypatch.setattr(runner, "_reconcile_forward_work_items",
                        lambda _conn: 0)
    monkeypatch.setattr(runner, "_enqueue_forward_candidates",
                        lambda _conn: 0)
    monkeypatch.setattr(runner, "_lease_forward_work_items",
                        lambda _conn, **_kwargs: [])
    monkeypatch.setattr(runner, "_forward_candidate_rows",
                        lambda _conn, *, experiment_ids: [])
    result = runner.run_forward_confirmations(object(), object(), limit=1)
    assert result["checked"] == 0
    assert result["results"] == []
    assert result["repaired_forward_metrics"] == 0
    assert result["historical_61_session_reuse"] is False


def test_idle_forward_sweep_throttles_and_closes_market_connection() -> None:
    class Meta:
        def rollback(self):
            pass

    class Market:
        closed = False

        def close(self):
            self.closed = True

    market = Market()
    connects = []

    def connect():
        connects.append(True)
        return market

    calls = []

    def run(meta, supplied_market, *, limit, worker):
        calls.append((meta, supplied_market, limit, worker))
        return {"checked": 0, "results": []}

    previous = worker._last_forward_sweep
    try:
        worker._last_forward_sweep = None
        first = worker.sweep_forward_confirmations(
            Meta(), monotonic_fn=lambda: 1000.0,
            market_connect=connect, runner=run)
        second = worker.sweep_forward_confirmations(
            Meta(), monotonic_fn=lambda: 1001.0,
            market_connect=connect, runner=run)
    finally:
        worker._last_forward_sweep = previous
    assert first == {"checked": 0, "results": []}
    assert second is None
    assert len(connects) == 1 and len(calls) == 1
    assert calls[0][2] == worker.FORWARD_SWEEP_BATCH
    assert calls[0][3]
    assert market.closed is True


def test_busy_ordinary_queue_does_not_starve_forward_sweep(monkeypatch) -> None:
    import job_queue

    ordinary = {"job_id": "ordinary-job"}
    monkeypatch.setattr(worker, "reclaim_hypotheses",
                        lambda _conn: {"requeued": 0})
    monkeypatch.setattr(worker, "cancel_terminal_zombies",
                        lambda _conn: {"cancelled": 0})
    monkeypatch.setattr(job_queue, "reclaim",
                        lambda _conn: {"reclaimed": 0})
    monkeypatch.setattr(worker, "sweep_orphans", lambda _conn: None)
    call_order = []

    def lease(_conn, *, worker):
        call_order.append("ordinary-lease")
        return [ordinary]

    monkeypatch.setattr(job_queue, "lease", lease)
    sweep_calls = []

    def sweep(meta):
        call_order.append("forward-sweep")
        sweep_calls.append(meta)
        return {"checked": 1, "results": [{"status": "WAITING"}]}

    monkeypatch.setattr(worker, "sweep_forward_confirmations", sweep)
    monkeypatch.setattr(
        worker, "run_with_lease_heartbeat",
        lambda _conn, job, *, worker: {
            "job_id": job["job_id"], "worker": worker})

    meta = object()
    result = worker.tick(meta, worker="quant-worker-test")

    assert sweep_calls == [meta]
    assert call_order == ["forward-sweep", "ordinary-lease"]
    assert result["picked"] == 1
    assert result["results"] == [{
        "job_id": "ordinary-job", "worker": "quant-worker-test"}]
    assert result["forward_confirmations"]["checked"] == 1


def test_quant_experiment_batch_runs_jobs_concurrently_on_isolated_connections(
    monkeypatch,
) -> None:
    """A leased batch must not serialize long replays on one DB connection."""

    monkeypatch.setenv("QUANT_EXPERIMENT_WORKERS", "2")
    jobs = [
        {"job_id": "job-a", "hypothesis_id": "hyp-a"},
        {"job_id": "job-b", "hypothesis_id": "hyp-b"},
    ]
    connections = []

    class JobConnection:
        def close(self):
            self.closed = True

    def connect():
        connection = JobConnection()
        connections.append(connection)
        return connection

    barrier = threading.Barrier(2)
    seen = []

    def run_with_heartbeat(conn, job, *, worker, connect):
        del worker, connect
        seen.append(conn)
        barrier.wait(timeout=2)
        return {"job_id": job["job_id"], "result": "DONE"}

    monkeypatch.setattr(worker, "_conn", connect)
    monkeypatch.setattr(worker, "run_with_lease_heartbeat", run_with_heartbeat)

    result = worker.execute_jobs(object(), jobs, worker="quant-worker-test")

    assert [item["job_id"] for item in result] == ["job-a", "job-b"]
    assert len(seen) == 2
    assert seen[0] is not seen[1]
    assert len(connections) == 2
    assert all(getattr(connection, "closed", False) for connection in connections)


def test_quant_experiment_worker_healthcheck_requires_a_recent_heartbeat(
    tmp_path, monkeypatch
) -> None:
    health_path = tmp_path / "worker-health"
    monkeypatch.setattr(worker, "HEALTH_PATH", health_path)

    worker._touch_health()
    touched_at = health_path.stat().st_mtime

    assert worker.healthcheck(now=touched_at) is True
    assert worker.healthcheck(now=touched_at + worker.HEALTH_STALE_SEC + 1) is False
