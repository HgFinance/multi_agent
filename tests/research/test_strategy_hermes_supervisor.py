from __future__ import annotations

import sys
from types import SimpleNamespace
import threading
import time
from argparse import Namespace
from pathlib import Path

AUTONOMOUS_DIR = Path(__file__).resolve().parents[2] / "departments/01-research/autonomous"
if str(AUTONOMOUS_DIR) not in sys.path:
    sys.path.insert(0, str(AUTONOMOUS_DIR))

import strategy_hermes_supervisor as supervisor
from autonomous_research_ingress import ResearchIntake
from models import ExperimentPlan, ExperimentResult


def _args(root: Path, *, retry_blocked: bool = False) -> Namespace:
    return Namespace(
        lab_root=root,
        repo_root=root,
        interval_min=0.5,
        timeout_seconds=30,
        max_concurrency=2,
        request_id=None,
        retry_blocked=retry_blocked,
    )


def test_persisted_blocked_error_is_not_replayed_by_service_loop(tmp_path: Path) -> None:
    root = tmp_path / "research"
    intake = ResearchIntake(root)
    intake.submit(
        {
            "request_id": "research-01",
            "goal": "Find a robust strategy",
            "source": "web",
        }
    )
    lab_path = intake.materialize("research-01", repo_root=tmp_path)
    intake.record_error("research-01", phase="HERMES_OR_VERIFY", error="invalid result")

    report = supervisor.run_once(_args(root))

    assert report["labs"] == [
        {"lab_id": "research-01", "status": "BLOCKED", "error": "invalid result"}
    ]
    assert not list((lab_path / "agent-runs").glob("*"))


def test_blocked_retry_requires_an_explicit_operator_flag(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "research"
    intake = ResearchIntake(root)
    intake.submit(
        {
            "request_id": "research-02",
            "goal": "Find another robust strategy",
            "source": "web",
        }
    )
    intake.materialize("research-02", repo_root=tmp_path)
    intake.record_error("research-02", phase="HERMES_OR_VERIFY", error="retry me")
    monkeypatch.setattr(
        supervisor,
        "_run_lab",
        lambda _args, _lab: {"lab_id": "research-02", "status": "RETRIED"},
    )

    report = supervisor.run_once(_args(root, retry_blocked=True))

    assert report["labs"] == [{"lab_id": "research-02", "status": "RETRIED"}]


def test_only_market_data_timeout_is_eligible_for_explicit_retry() -> None:
    blocked = SimpleNamespace(
        failure_reason=(
            "BLOCKED: the allow-listed LS market-data ranking call to t1444 "
            "timed out without returning rows"
        )
    )
    schema_error = SimpleNamespace(
        failure_reason="ValueError: artifacts must be a sequence of strings"
    )

    assert supervisor._is_retryable_market_data_block(blocked) is True
    assert supervisor._is_retryable_market_data_block(schema_error) is False


def test_timed_out_agent_has_a_terminal_blocked_result_shape(tmp_path: Path) -> None:
    root = tmp_path / "research"
    intake = ResearchIntake(root)
    intake.submit(
        {
            "request_id": "research-timeout",
            "goal": "Record an interrupted experiment",
            "source": "web",
        }
    )
    lab_path = intake.materialize("research-timeout", repo_root=tmp_path)
    lab = supervisor.ResearchLab(lab_path)
    lab.record_plan(
        ExperimentPlan(
            plan_id="plan-timeout",
            hypothesis_id="hyp-timeout",
            objective="Record interruption",
            method="No measurement",
            data_requirements=("none",),
            splits=("none",),
            cost_model="none",
            seed=1,
            signature={"test": "timeout"},
            preregistration_hash="hash-timeout",
        )
    )
    lab.update_state(active_plan_id="plan-timeout")

    result = supervisor._agent_failure_result(
        lab,
        SimpleNamespace(
            run_id="hermes-timeout",
            plan_id=None,
            status="TIMED_OUT",
            output_path=str(lab_path / "agent-runs/hermes-timeout.txt"),
            usage_path=None,
            error="Hermes timed out",
            duration_seconds=30.0,
        ),
    )

    result.validate()
    assert result.plan_id == "plan-timeout"
    assert result.status == "BLOCKED"
    assert result.preregistration_hash == "hash-timeout"
    assert result.metrics["measured_strategy_metrics_available"] is False


def test_retry_flag_reopens_a_lab_with_a_persisted_blocked_result(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "research"
    intake = ResearchIntake(root)
    intake.submit(
        {
            "request_id": "research-blocked-result",
            "goal": "Retry a transient market-data failure",
            "source": "web",
        }
    )
    lab_path = intake.materialize("research-blocked-result", repo_root=tmp_path)
    supervisor.ResearchLab(lab_path).record_result(
        ExperimentResult(
            plan_id="plan-t1444",
            status="BLOCKED",
            cost_included=False,
            oos_evaluated=False,
            leakage_detected=False,
            robustness={"transport": False},
            failure_reason=(
                "BLOCKED: the allow-listed LS market-data ranking call to t1444 "
                "timed out without returning rows"
            ),
        )
    )
    (lab_path / supervisor.MANAGED_MARKER).touch()

    class _RetryAgent:
        def __init__(self, **_kwargs):
            pass

        def run(self):
            return SimpleNamespace(
                run_id="retry-run",
                plan_id=None,
                status="COMPLETED",
                returncode=0,
                output_path=str(lab_path / "agent-runs" / "retry-run.txt"),
                usage_path=None,
                error=None,
                duration_seconds=0.0,
            )

    monkeypatch.setattr(supervisor, "StrategyHermesAgent", _RetryAgent)
    monkeypatch.setattr(supervisor, "sync_agent_artifacts", lambda _lab: [])

    report = supervisor._run_lab(_args(root, retry_blocked=True), lab_path)

    assert report["status"] == "CYCLE_COMPLETED"


def test_completed_lab_is_not_replayed_on_the_next_supervisor_cycle(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "research"
    intake = ResearchIntake(root)
    intake.submit(
        {
            "request_id": "research-completed",
            "goal": "Keep a completed research result durable",
            "source": "web",
        }
    )
    lab_path = intake.materialize("research-completed", repo_root=tmp_path)
    lab = supervisor.ResearchLab(lab_path)
    lab.update_state(cycle=1, last_action="HERMES_RUNNING")

    monkeypatch.setattr(
        supervisor.ResearchLab,
        "results",
        lambda _lab: [SimpleNamespace(status="COMPLETED", plan_id="plan-1")],
    )

    def fail_if_replayed(*_args, **_kwargs):
        raise AssertionError("completed labs must not start Hermes again")

    monkeypatch.setattr(supervisor, "StrategyHermesAgent", fail_if_replayed)

    report = supervisor.run_once(_args(root))

    assert report["labs"] == [
        {
            "lab_id": "research-completed",
            "status": "COMPLETED",
            "cycle": 1,
            "last_result": "plan-1",
            "decisions": [],
            "result_available": True,
        }
    ]
    assert lab.state()["last_action"] == "RESULT_RECORDED"


def test_independent_active_labs_execute_concurrently_in_stable_order(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "research"
    intake = ResearchIntake(root)
    for request_id in ("research-03", "research-04"):
        intake.submit(
            {
                "request_id": request_id,
                "goal": f"Find strategy {request_id}",
                "source": "web",
            }
        )
        intake.materialize(request_id, repo_root=tmp_path)

    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_run(_args, lab_path):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.08)
        with lock:
            active -= 1
        return {"lab_id": lab_path.name, "status": "CYCLE_COMPLETED"}

    monkeypatch.setattr(supervisor, "_run_lab", fake_run)

    report = supervisor.run_once(_args(root))

    assert max_active == 2
    assert [lab["lab_id"] for lab in report["labs"]] == [
        "research-03",
        "research-04",
    ]
    assert report["execution"]["max_concurrency"] == 2
