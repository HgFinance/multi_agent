from __future__ import annotations

import sys
import threading
import time
from argparse import Namespace
from pathlib import Path

AUTONOMOUS_DIR = Path(__file__).resolve().parents[2] / "departments/01-research/autonomous"
if str(AUTONOMOUS_DIR) not in sys.path:
    sys.path.insert(0, str(AUTONOMOUS_DIR))

import strategy_hermes_supervisor as supervisor
from autonomous_research_ingress import ResearchIntake


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
