from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import sys


AUTONOMOUS_DIR = Path(__file__).resolve().parents[2] / "departments/01-research/autonomous"
if str(AUTONOMOUS_DIR) not in sys.path:
    sys.path.insert(0, str(AUTONOMOUS_DIR))

from autonomous_research_ingress import ResearchIntake  # noqa: E402
import strategy_hermes_supervisor as supervisor  # noqa: E402


def _args(root: Path, *, retry_blocked: bool = False) -> Namespace:
    return Namespace(
        lab_root=root,
        repo_root=root,
        interval_min=0.5,
        timeout_seconds=30,
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
