from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


PIPELINE = (Path(__file__).resolve().parents[1] / "departments"
            / "04-quant-backtest" / "pipeline")
RESEARCH_COLLECTORS = (Path(__file__).resolve().parents[1] / "departments"
                       / "01-research" / "collectors")
for path in (PIPELINE, RESEARCH_COLLECTORS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import data_resolution  # noqa: E402
import backtest_runner  # noqa: E402
import config_binding  # noqa: E402
import experiment_orchestrator as orchestrator  # noqa: E402
import factory_bridge  # noqa: E402
import intraday_experiment_runner as intraday_runner  # noqa: E402
import release_gate  # noqa: E402
from intraday_alpha_ast import EXPLICIT_FEATURE_WINDOW_CONTRACT  # noqa: E402
from trial_family import pressure  # noqa: E402


class _LedgerCursor:
    def __init__(self, state, *, hypothesis_row=None,
                 fail_pressure_query=False):
        self.state = state
        self.hypothesis_row = hypothesis_row
        self.fail_pressure_query = fail_pressure_query
        self.rows = []
        self.one = None
        self.fingerprint = None

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        self.rows = []
        self.one = None
        if "select hypothesis_id, title, expected_edge" in normalized:
            self.one = self.hypothesis_row
        elif "select material_fingerprint from quant.hypotheses" in normalized:
            self.one = (self.fingerprint,)
        elif "set status='PREREGISTERED'" in normalized:
            self.fingerprint = params[0]
        elif "pg_advisory_xact_lock" in normalized:
            self.state["locks"].append(params[0])
            self.one = (None,)
        elif (orchestrator.TRIAL_RESERVATION_KEY in normalized
              and normalized.startswith("select hypothesis_id::text")):
            families = set(params[0])
            self.rows = [
                (hid, payload) for hid, payload in self.state["reservations"].items()
                if payload["trial_family_id"] in families
            ]
        elif ("from quant.experiments" in normalized
              and "trial_family_id = any" in normalized):
            if self.fail_pressure_query:
                raise RuntimeError("pressure ledger unavailable")
            families = set(params[0])
            self.rows = [
                (row["experiment_id"], row["hypothesis_id"],
                 row["trial_family_id"], row["trial_number"])
                for row in self.state["experiments"]
                if row["trial_family_id"] in families
            ]
        elif (normalized.startswith("update quant.hypotheses")
              and "jsonb_set" in normalized):
            self.state["reservations"][str(params[1])] = json.loads(params[0])
        elif (normalized.startswith("update quant.experiments")
              and "trial_family_id is null" in normalized):
            family, number, hid = params[:3]
            experiment_id = str(params[3]) if len(params) == 4 else None
            for row in self.state["experiments"]:
                if (row["hypothesis_id"] == str(hid)
                        and row["trial_family_id"] is None
                        and (experiment_id is None
                             or row["experiment_id"] == experiment_id)):
                    row["trial_family_id"] = family
                    row["trial_number"] = int(number)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.one


class _LedgerConnection:
    def __init__(self, state, *, hypothesis_row=None,
                 fail_pressure_query=False):
        self.cursor_instance = _LedgerCursor(
            state, hypothesis_row=hypothesis_row,
            fail_pressure_query=fail_pressure_query)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _state():
    return {"reservations": {}, "experiments": [], "locks": []}


def _explicit_intraday_edge() -> dict:
    return {
        "type": "order_flow_imbalance",
        "research_lane": "INTRADAY_EVENT",
        "universe_key": "krx_all",
        "feature_window_contract_version":
            EXPLICIT_FEATURE_WINDOW_CONTRACT,
        "intraday_signal_expr": {
            "op": "field",
            "field": "realized_volatility_bps",
            "seconds": 2,
        },
        "semantic_plan": {
            "event": "VOLATILITY_BURST",
            "context": ["ALL"],
            "qualities": ["PERSISTENCE"],
            "direction": "FOLLOW",
            "output": "TAKER_NET_PNL",
            "execution": "TAKER",
            "horizon_seconds": 5,
        },
        "horizon_seconds": 5,
        "execution": "TAKER",
        "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
        "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
    }


def _resolved_intraday(*_args, **_kwargs):
    return SimpleNamespace(
        ok=True,
        datasets=("krx-intraday-events/v1",),
        verdict="PASS",
        unmapped=(),
        notes=(),
        execution_contract=None,
    )


def test_lane_aware_preflight_accepts_current_explicit_intraday_contract():
    hyp = {"expected_edge": _explicit_intraday_edge()}

    assert orchestrator.execution_surface_rejection_reasons(hyp) == []


def test_intraday_proposal_surface_has_one_runner_source_of_truth():
    assert factory_bridge.INTRADAY_EDGE_KEYS is \
        intraday_runner.INTRADAY_PROPOSAL_PARAMETER_KEYS
    assert orchestrator.TRIAL_RESERVATION_KEY == \
        intraday_runner.TRIAL_RESERVATION_KEY
    assert orchestrator.TRIAL_RESERVATION_KEY in \
        intraday_runner.INTRADAY_SYSTEM_METADATA_KEYS
    assert config_binding.SYSTEM_METADATA_KEYS == frozenset({
        orchestrator.TRIAL_RESERVATION_KEY,
    })


@pytest.mark.parametrize(
    "lane",
    (None, "", "DAILY_CROSS_SECTIONAL", " daily_cross_sectional "),
)
def test_daily_lane_preflight_normalizes_missing_case_and_whitespace(lane):
    edge = {"type": "momentum", "universe_key": "krx_all"}
    if lane is not None:
        edge["research_lane"] = lane

    assert orchestrator.execution_surface_rejection_reasons({
        "expected_edge": edge,
    }) == []


def test_intraday_lane_preflight_normalizes_case_and_whitespace():
    edge = {**_explicit_intraday_edge(),
            "research_lane": " intraday_event "}

    assert orchestrator.execution_surface_rejection_reasons({
        "expected_edge": edge,
    }) == []


def test_unknown_lane_is_rejected_instead_of_falling_through_to_daily():
    reasons = orchestrator.execution_surface_rejection_reasons({
        "expected_edge": {
            "type": "momentum", "universe_key": "krx_all",
            "research_lane": "INTRDAY_EVENT",
        },
    })

    assert len(reasons) == 1
    assert "unsupported research_lane" in reasons[0]
    assert "INTRDAY_EVENT" in reasons[0]


def test_unknown_lane_is_blocked_before_resolution_or_lifecycle(monkeypatch):
    row = (
        "h-unknown-lane",
        "unknown lane",
        {
            "type": "momentum", "universe_key": "krx_all",
            "research_lane": "INTRDAY_EVENT",
        },
        ["krx-basket-daily/v2"],
        "PROPOSED",
    )
    cursor = orchestrator._FakeCursor(row, ["krx-basket-daily/v2"])
    conn = orchestrator._FakeConn(cursor)

    def forbidden_resolution(*_args, **_kwargs):
        raise AssertionError("unknown lane must be rejected before resolution")

    monkeypatch.setattr(data_resolution, "resolve", forbidden_resolution)
    report = orchestrator.orchestrate(
        "h-unknown-lane", conn=conn, market_conn=object())

    assert report.verdict == "NOT_RUNNABLE"
    assert report.missing == ["contract:RESEARCH_LANE"]
    assert "unsupported research_lane" in report.backlog[0]
    assert cursor.updates == []
    assert conn.commits == 0


def test_current_intraday_preflight_rejects_unknown_primary_key():
    edge = {**_explicit_intraday_edge(), "evaluation_day": 60}

    reasons = orchestrator.execution_surface_rejection_reasons({
        "expected_edge": edge,
    })

    assert len(reasons) == 1
    assert "unsupported keys" in reasons[0]
    assert "evaluation_day" in reasons[0]


def test_current_intraday_preflight_rejects_unknown_sidecar_key():
    edge = {
        **_explicit_intraday_edge(),
        "screening_population": [{"evaluation_day": 60}],
    }

    reasons = orchestrator.execution_surface_rejection_reasons({
        "expected_edge": edge,
    })

    assert len(reasons) == 1
    assert "screening_population[0].evaluation_day" in reasons[0]


def test_intraday_preflight_validator_exception_fails_closed(monkeypatch):
    def broken_validator(_edge):
        raise RuntimeError("validator unavailable")

    monkeypatch.setattr(intraday_runner, "config_from_edge", broken_validator)

    reasons = orchestrator.execution_surface_rejection_reasons({
        "expected_edge": _explicit_intraday_edge(),
    })

    assert len(reasons) == 1
    assert "INTRADAY_EVENT execution-surface validation failed closed" in reasons[0]
    assert "RuntimeError" in reasons[0]


def test_invalid_intraday_contract_is_blocked_before_lifecycle_or_trial(
        monkeypatch):
    edge = {**_explicit_intraday_edge(), "evaluation_days": 59}
    row = (
        "h-invalid-intraday",
        "invalid intraday execution surface",
        edge,
        ["market_quotes", "market_ticks"],
        "PROPOSED",
    )
    cursor = orchestrator._FakeCursor(row, ["krx-intraday-events/v1"])
    conn = orchestrator._FakeConn(cursor)
    calls = {"reservation": 0, "prepare": 0, "chain": 0}

    monkeypatch.setattr(data_resolution, "resolve", _resolved_intraday)

    def forbidden_reservation(*_args, **_kwargs):
        calls["reservation"] += 1
        raise AssertionError("invalid contract must not reserve a trial")

    def forbidden_prepare(*_args, **_kwargs):
        calls["prepare"] += 1
        raise AssertionError("invalid contract must not prepare a replay")

    def forbidden_chain(*_args, **_kwargs):
        calls["chain"] += 1
        raise AssertionError("invalid contract must not enter the run chain")

    monkeypatch.setattr(
        orchestrator, "_reserve_trial_family", forbidden_reservation)
    monkeypatch.setattr(intraday_runner, "prepare", forbidden_prepare)

    report = orchestrator.orchestrate(
        "h-invalid-intraday",
        conn=conn,
        market_conn=object(),
        run_chain=forbidden_chain,
    )

    assert report.verdict == "NOT_RUNNABLE"
    assert any("evaluation_days" in reason for reason in report.missing)
    assert calls == {"reservation": 0, "prepare": 0, "chain": 0}
    assert cursor.updates == []
    assert conn.commits == 0


def test_current_explicit_intraday_reaches_data_preflight_not_not_runnable(
        monkeypatch):
    row = (
        "h-current-intraday",
        "current explicit intraday execution surface",
        _explicit_intraday_edge(),
        ["market_quotes", "market_ticks"],
        "PROPOSED",
    )
    cursor = orchestrator._FakeCursor(row, ["krx-intraday-events/v1"])
    conn = orchestrator._FakeConn(cursor)
    monkeypatch.setattr(data_resolution, "resolve", _resolved_intraday)
    monkeypatch.setattr(
        intraday_runner,
        "prepare",
        lambda *_args, **_kwargs: {
            "selected": {
                "status": "INSUFFICIENT_SESSIONS",
                "causal_sessions_available": 60,
            },
        },
    )
    monkeypatch.setattr(
        intraday_runner,
        "record_data_feasibility",
        lambda *_args, **_kwargs: {"status": "NEEDS_DATA"},
    )

    report = orchestrator.orchestrate(
        "h-current-intraday",
        conn=conn,
        market_conn=object(),
    )

    assert report.verdict == "NEEDS_DATA"
    assert report.verdict != "NOT_RUNNABLE"
    assert cursor.updates == []
    assert conn.commits == 0


@pytest.mark.parametrize(
    ("provisional", "decision", "failed", "unmeasured", "expected"),
    (
        ("SUPPORTED", "SUBMIT_TO_QA", [], [], "SUPPORTED"),
        ("SUPPORTED", "HOLD", ["excess_return"], [], "REJECTED"),
        ("SUPPORTED", "HOLD", ["pbo"], ["pbo"], "INCONCLUSIVE"),
        ("SUPPORTED", "HOLD", [], [], "INCONCLUSIVE"),
        ("REJECTED", "SUBMIT_TO_QA", [], [], "REJECTED"),
        ("INCONCLUSIVE", "SUBMIT_TO_QA", [], [], "INCONCLUSIVE"),
    ),
)
def test_release_gate_overlays_robustness_fail_closed(
        provisional, decision, failed, unmeasured, expected):
    assert orchestrator.release_to_status(
        provisional,
        decision,
        failed=failed,
        unmeasured=unmeasured,
    ) == expected


def _valid_reservation(**updates) -> dict:
    payload = {
        "reservation_id": "cb6735d6-29ed-456c-bfba-5b2619cab88c",
        "trial_family_id": "fam_0123456789abcdef",
        "trial_number": 2,
        "trial_budget": 10,
        "orchestrator_version": orchestrator.ORCH_VERSION,
    }
    payload.update(updates)
    return payload


def test_trial_reservation_payload_accepts_current_and_legacy_ids():
    current = _valid_reservation()
    legacy = _valid_reservation(
        reservation_id="legacy-experiment:e-existing")

    assert orchestrator._reservation_payload(current) == current
    assert orchestrator._reservation_payload(legacy) == legacy


def test_trial_reservation_payload_requires_exact_schema():
    for field in orchestrator.TRIAL_RESERVATION_REQUIRED_KEYS:
        payload = _valid_reservation()
        payload.pop(field)
        with pytest.raises(ValueError, match="schema mismatch"):
            orchestrator._reservation_payload(payload)

    with pytest.raises(ValueError, match="schema mismatch"):
        orchestrator._reservation_payload({
            **_valid_reservation(), "unexpected": True,
        })


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("orchestrator_version", "old", "orchestrator_version"),
        ("reservation_id", "not-a-uuid", "UUID"),
        ("trial_family_id", "family", "family"),
        ("trial_number", True, "number"),
        ("trial_number", "2", "number"),
        ("trial_number", 0, "number"),
        ("trial_budget", True, "budget"),
        ("trial_budget", "10", "budget"),
        ("trial_budget", 0, "budget"),
    ),
)
def test_trial_reservation_payload_rejects_invalid_typed_fields(
        field, value, message):
    with pytest.raises(ValueError, match=message):
        orchestrator._reservation_payload(_valid_reservation(**{field: value}))


def test_trial_reservation_payload_validates_alias_schema():
    with pytest.raises(ValueError, match="counted_aliases"):
        orchestrator._reservation_payload(_valid_reservation(
            counted_aliases="fam_alias"))
    with pytest.raises(ValueError, match="not unique"):
        orchestrator._reservation_payload(_valid_reservation(
            counted_aliases=["fam_alias", "fam_alias"]))


def test_execution_preflight_rejects_malformed_system_reservation():
    reasons = orchestrator.execution_surface_rejection_reasons({
        "expected_edge": {
            "type": "momentum", "universe_key": "krx_all",
            orchestrator.TRIAL_RESERVATION_KEY: {"trial_number": 1},
        },
    })

    assert len(reasons) == 1
    assert "execution-surface validation failed closed" in reasons[0]
    assert "schema mismatch" in reasons[0]


def test_trial_reservation_metadata_does_not_change_runner_configs_or_hashes():
    reservation = _valid_reservation()
    daily_edge = {"type": "momentum", "universe_key": "krx_all"}
    daily_with_reservation = {
        **daily_edge,
        orchestrator.TRIAL_RESERVATION_KEY: reservation,
    }
    base = dict(backtest_runner.DEFAULT_CONFIG)
    daily_before = config_binding.bind(
        {"expected_edge": daily_edge}, base)
    daily_after = config_binding.bind(
        {"expected_edge": daily_with_reservation}, base)

    assert daily_before.rejected == daily_after.rejected == []
    assert daily_before.ignored == daily_after.ignored
    assert daily_before.config == daily_after.config
    assert backtest_runner.input_hash(
        "dataset", daily_before.config, "code", 0) == \
        backtest_runner.input_hash(
            "dataset", daily_after.config, "code", 0)

    intraday_edge = _explicit_intraday_edge()
    intraday_before, spec_before = intraday_runner.config_from_edge(
        intraday_edge)
    intraday_after, spec_after = intraday_runner.config_from_edge({
        **intraday_edge,
        orchestrator.TRIAL_RESERVATION_KEY: reservation,
    })
    assert intraday_runner.validate_current_explicit_v2_execution_edge({
        **intraday_edge,
        orchestrator.TRIAL_RESERVATION_KEY: reservation,
    }) == (intraday_after, spec_after)
    assert intraday_before == intraday_after
    assert spec_before == spec_after
    assert intraday_runner._input_hash("h", intraday_before) == \
        intraday_runner._input_hash("h", intraday_after)


def test_failed_exposed_trial_keeps_family_pressure_and_retry_is_idempotent():
    state = _state()
    conn = _LedgerConnection(state)
    cur = conn.cursor()

    first = orchestrator._reserve_trial_family(
        conn, cur, hypothesis_id="h-1", hyp={"expected_edge": {}},
        families=("fam_stock_microstructure",), budget=10,
        pressure_fn=pressure)
    assert first["trial_number"] == 1
    assert first["reserved_before_evaluation"] is True
    assert state["locks"] == ["fam_stock_microstructure"]

    # The evaluator registered and exposed evidence, then failed.  Status is
    # deliberately irrelevant: assignment is append-only trial pressure.
    state["experiments"].append({
        "experiment_id": "e-failed", "hypothesis_id": "h-1",
        "trial_family_id": None, "trial_number": None, "status": "FAILED",
        "exposed": True,
    })
    orchestrator._attach_trial_reservation(
        cur, hypothesis_id="h-1", pressure=first)
    assert state["experiments"][0]["trial_family_id"] == \
        "fam_stock_microstructure"
    assert state["experiments"][0]["trial_number"] == 1

    replay = orchestrator._reserve_trial_family(
        conn, cur, hypothesis_id="h-1", hyp={"expected_edge": {}},
        families=("fam_stock_microstructure",), budget=10,
        pressure_fn=pressure)
    assert replay["trial_number"] == 1
    assert replay["reservation_id"] == first["reservation_id"]

    second = orchestrator._reserve_trial_family(
        conn, cur, hypothesis_id="h-2", hyp={"expected_edge": {}},
        families=("fam_stock_microstructure",), budget=10,
        pressure_fn=pressure)
    assert second["trial_number"] == 2
    assert second["trials_used"] == 1


def test_pressure_query_failure_blocks_orchestrator_evaluation(monkeypatch):
    # An explicit, non-canonical daily discriminator must route daily, while
    # the actual binder receives the execution-equivalent lane-free copy.
    edge = {
        "type": "momentum", "universe_key": "krx_all",
        "research_lane": " daily_cross_sectional ",
    }
    row = ("h-pressure", "pressure failure", edge,
           ["krx-basket-daily/v2"], "PROPOSED")
    state = _state()
    conn = _LedgerConnection(
        state, hypothesis_row=row, fail_pressure_query=True)
    monkeypatch.setattr(
        data_resolution, "resolve",
        lambda *args, **kwargs: SimpleNamespace(
            ok=True, datasets=("krx-basket-daily/v2",), verdict="PASS",
            unmapped=(), notes=()))

    evaluated = False

    def forbidden_chain(_hyp, _hypothesis_id):
        nonlocal evaluated
        evaluated = True
        raise AssertionError("evaluation must not run without trial pressure")

    report = orchestrator.orchestrate(
        "h-pressure", conn=conn, market_conn=object(),
        run_chain=forbidden_chain)

    assert report.verdict == "TRIAL_PRESSURE_UNAVAILABLE"
    assert evaluated is False
    assert conn.rollbacks == 1
    assert any("blocked fail-closed" in item for item in report.backlog)


class _TerminalCursor:
    def __init__(self, *, governed_stock_evidence: bool):
        self.governed_stock_evidence = governed_stock_evidence
        self.executed: list[tuple[str, object]] = []
        self.rowcount = 1
        self._one = None

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        self.executed.append((normalized, params))
        self._one = None
        if normalized.startswith("select exists"):
            self._one = (self.governed_stock_evidence,)

    def fetchone(self):
        return self._one


class _TerminalConnection:
    def __init__(self, *, governed_stock_evidence: bool):
        self.cursor_instance = _TerminalCursor(
            governed_stock_evidence=governed_stock_evidence)
        self.commits = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1


def _outcome(*, decision: str, failed_criteria=()):
    return factory_bridge.build_outcome(
        experiment_id="legacy-mixed-experiment",
        hypothesis_id="h-terminal",
        trial_family_id="fam-terminal",
        trial_number=1,
        decision=decision,
        failed_criteria=failed_criteria,
    )


@pytest.mark.parametrize(
    ("new_status", "decision"),
    (
        ("SUPPORTED", "OBSERVE"),
        ("PROMOTED", "OBSERVE"),
        ("REJECTED", "PROMOTED"),
    ),
)
def test_factory_bridge_blocks_promotion_without_governed_stock_evidence(
        new_status, decision):
    conn = _TerminalConnection(governed_stock_evidence=False)

    with pytest.raises(RuntimeError, match="STOCK-only evidence"):
        factory_bridge.finalize(
            conn,
            hypothesis_id="h-terminal",
            new_status=new_status,
            outcome=_outcome(decision=decision),
        )

    sql = [statement for statement, _params in
           conn.cursor_instance.executed]
    assert len(sql) == 1
    assert "quant.current_krx_stock_instrument_identity" in sql[0]
    assert "quant.dataset_manifests" in sql[0]
    assert "insert into research.experiment_outcomes" not in sql[0]
    assert conn.commits == 0


def test_factory_bridge_rejects_qa_positive_outcome_for_non_supported_status():
    conn = _TerminalConnection(governed_stock_evidence=True)

    with pytest.raises(
            RuntimeError,
            match="SUBMIT_TO_QA outcome requires SUPPORTED"):
        factory_bridge.finalize(
            conn,
            hypothesis_id="h-terminal",
            new_status="REJECTED",
            outcome=_outcome(decision="SUBMIT_TO_QA"),
        )

    assert conn.cursor_instance.executed == []
    assert conn.commits == 0


@pytest.mark.parametrize(
    ("new_status", "decision", "failed_criteria"),
    (
        ("REJECTED", "REJECT", ["no_evidence"]),
        ("INCONCLUSIVE", "NO_EVIDENCE", []),
        ("INCONCLUSIVE", "GATE_HOLD", ["underpowered"]),
    ),
)
def test_factory_bridge_still_finalizes_non_promotion_without_attestation(
        new_status, decision, failed_criteria):
    conn = _TerminalConnection(governed_stock_evidence=False)

    factory_bridge.finalize(
        conn,
        hypothesis_id="h-terminal",
        new_status=new_status,
        outcome=_outcome(
            decision=decision, failed_criteria=failed_criteria),
    )

    sql = [statement for statement, _params in
           conn.cursor_instance.executed]
    assert not any(statement.startswith("select exists") for statement in sql)
    assert any("insert into research.experiment_outcomes" in statement
               for statement in sql)
    assert any("update quant.hypotheses" in statement for statement in sql)
    assert conn.commits == 1


def test_injected_robust_chain_cannot_promote_legacy_mixed_experiment(
        monkeypatch):
    row = (
        "h-terminal",
        "injected result must be re-attested",
        {"type": "momentum"},
        ["krx-basket-daily/v1"],
        "PROPOSED",
    )
    cursor = orchestrator._FakeCursor(
        row, ["krx-basket-daily/v1"], governed_stock_evidence=False)
    conn = orchestrator._FakeConn(cursor)
    monkeypatch.setattr(
        data_resolution,
        "resolve",
        lambda *args, **kwargs: SimpleNamespace(
            ok=True,
            datasets=("krx-basket-daily/v1",),
            verdict="PASS",
            unmapped=(),
            notes=(),
        ),
    )
    monkeypatch.setattr(
        release_gate,
        "evaluate",
        lambda *args, **kwargs: release_gate.ReleaseDecision(
            decision="SUBMIT_TO_QA",
            passed=["all"],
        ),
    )

    with pytest.raises(RuntimeError, match="STOCK-only evidence"):
        orchestrator.orchestrate(
            "h-terminal",
            conn=conn,
            market_conn=orchestrator._FakeMarket(),
            run_chain=lambda _hyp, _hid: {
                "experiment_id": "legacy-mixed-experiment",
                "fragility": "ROBUST",
            },
        )

    guard_sql = cursor._last[0]
    assert "select exists" in guard_sql.lower()
    assert "quant.current_krx_stock_instrument_identity" in guard_sql
    assert not any("set status = %s" in sql for sql in cursor.update_sqls)


class _DailyReleaseCursor(orchestrator._FakeCursor):
    def __init__(self, hypothesis_row, datasets, *, gate_rows=(),
                 fail_gate_query=False, governed_stock_evidence=True):
        super().__init__(
            hypothesis_row,
            datasets,
            governed_stock_evidence=governed_stock_evidence,
        )
        self.gate_rows = list(gate_rows)
        self.fail_gate_query = fail_gate_query

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split()).lower()
        if (self.fail_gate_query
                and normalized.startswith(
                    "select metric, value, split, dimensions")
                and "from quant.experiment_metrics" in normalized):
            raise RuntimeError("release metrics unavailable")
        super().execute(sql, params)

    def fetchall(self):
        normalized = " ".join(str(getattr(self, "_last", ("", ()))[0]).split()).lower()
        if (normalized.startswith("select metric, value, split, dimensions")
                and "from quant.experiment_metrics" in normalized):
            return list(self.gate_rows)
        if (normalized.startswith("select metric, value from")
                and "from quant.experiment_metrics" in normalized):
            return [(row[0], row[1]) for row in self.gate_rows]
        return super().fetchall()


def _run_robust_daily_release(monkeypatch, cursor):
    monkeypatch.setattr(
        data_resolution,
        "resolve",
        lambda *args, **kwargs: SimpleNamespace(
            ok=True,
            datasets=("krx-basket-daily/v1",),
            verdict="PASS",
            unmapped=(),
            notes=(),
        ),
    )
    return orchestrator.orchestrate(
        "h-release",
        conn=orchestrator._FakeConn(cursor),
        market_conn=orchestrator._FakeMarket(),
        run_chain=lambda _hyp, _hid: {
            "experiment_id": "e-release",
            "fragility": "ROBUST",
        },
    )


def _daily_gate_rows(*, excess_return_pct=20.0):
    values = {
        "excess_return_pct": excess_return_pct,
        "information_ratio": 1.1,
        "max_drawdown_pct": -20.0,
        "turnover": 20.0,
        "deflated_sharpe": 0.98,
        "bootstrap_ci_low": 0.1,
        "pbo": 0.2,
    }
    return [(metric, value, "TEST", {})
            for metric, value in values.items()]


def _daily_release_cursor(**kwargs):
    row = (
        "h-release",
        "daily release gate",
        {"type": "momentum"},
        ["krx-basket-daily/v1"],
        "PROPOSED",
    )
    return _DailyReleaseCursor(row, ["krx-basket-daily/v1"], **kwargs)


def test_robust_daily_run_requires_a_clean_release_gate(monkeypatch):
    report = _run_robust_daily_release(
        monkeypatch,
        _daily_release_cursor(gate_rows=_daily_gate_rows()),
    )

    assert report.transitions[-1] == "RUNNING->SUPPORTED"
    assert report.release["decision"] == "SUBMIT_TO_QA"
    assert report.feedback["decision"] == "SUBMIT_TO_QA"


def test_robust_daily_run_with_measured_gate_failure_is_rejected(monkeypatch):
    report = _run_robust_daily_release(
        monkeypatch,
        _daily_release_cursor(
            gate_rows=_daily_gate_rows(excess_return_pct=5.0)),
    )

    assert report.transitions[-1] == "RUNNING->REJECTED"
    assert report.release["decision"] == "HOLD"
    assert report.release["failed"] == ["excess_return"]
    assert report.feedback["decision"] == "REJECT"


def test_robust_daily_run_with_unmeasured_gate_is_inconclusive(monkeypatch):
    report = _run_robust_daily_release(
        monkeypatch,
        _daily_release_cursor(gate_rows=[]),
    )

    assert report.transitions[-1] == "RUNNING->INCONCLUSIVE"
    assert report.release["decision"] == "HOLD"
    assert report.release["unmeasured"]
    assert report.feedback["decision"] == "GATE_HOLD"


def test_robust_daily_run_with_gate_query_failure_is_inconclusive(monkeypatch):
    report = _run_robust_daily_release(
        monkeypatch,
        _daily_release_cursor(fail_gate_query=True),
    )

    assert report.transitions[-1] == "RUNNING->INCONCLUSIVE"
    assert report.release["decision"] == "HOLD"
    assert report.feedback["decision"] == "GATE_HOLD"
