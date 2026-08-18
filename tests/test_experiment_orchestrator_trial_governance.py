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
import experiment_orchestrator as orchestrator  # noqa: E402
import factory_bridge  # noqa: E402
import release_gate  # noqa: E402
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
    edge = {"type": "momentum", "universe_key": "krx_all"}
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
