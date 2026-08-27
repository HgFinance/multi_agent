from __future__ import annotations

from pathlib import Path

import pytest

import sys

AUTONOMOUS_DIR = Path(__file__).resolve().parents[2] / "departments/01-research/autonomous"
if str(AUTONOMOUS_DIR) not in sys.path:
    sys.path.insert(0, str(AUTONOMOUS_DIR))

from autonomous_research_ingress import ResearchIntake, ResearchRequestConflict  # noqa: E402


def _payload(request_id: str = "research-01") -> dict[str, object]:
    return {
        "request_id": request_id,
        "goal": "Find a robust short-horizon strategy",
        "universe": "Korean equities",
        "horizon": "1-5 days",
        "constraints": ["No live orders"],
        "actor_id": "user-a",
        "source": "web",
    }


def test_intake_is_idempotent_and_rejects_rebinding(tmp_path: Path) -> None:
    intake = ResearchIntake(tmp_path / "research")

    first, created = intake.submit(_payload())
    replay, replay_created = intake.submit(_payload())

    assert created is True
    assert replay_created is False
    assert replay == first
    with pytest.raises(ResearchRequestConflict):
        intake.submit(_payload() | {"goal": "A different research objective"})


def test_materialize_creates_an_isolated_persistent_lab(tmp_path: Path) -> None:
    intake = ResearchIntake(tmp_path / "research")
    intake.submit(_payload())

    lab_path = intake.materialize("research-01", repo_root=tmp_path)
    status = intake.status("research-01")

    assert lab_path == tmp_path / "research" / "labs" / "research-01"
    assert (lab_path / "objective.json").exists()
    assert (lab_path / "request.json").exists()
    assert (lab_path / "RESOURCE_MAP.md").exists()
    assert not (tmp_path / "research" / "intake" / "research-01.json").exists()
    assert status is not None
    assert status["status"] == "RESEARCHING"
    assert status["actor_id"] == "user-a"


def test_materialize_is_safe_to_retry_after_marker_survives(tmp_path: Path) -> None:
    intake = ResearchIntake(tmp_path / "research")
    intake.submit(_payload())
    first = intake.materialize("research-01", repo_root=tmp_path)

    # A second worker sees the already materialized lab and does not create a
    # second objective or change the request identity.
    second = intake.materialize("research-01", repo_root=tmp_path)

    assert second == first
    assert intake.status("research-01")["goal"] == _payload()["goal"]


def test_worker_error_is_visible_until_the_next_successful_materialization(tmp_path: Path) -> None:
    intake = ResearchIntake(tmp_path / "research")
    intake.submit(_payload())
    intake.record_error("research-01", phase="MATERIALIZE", error="temporary repository error")

    assert intake.status("research-01")["status"] == "BLOCKED"
    assert intake.status("research-01")["error"] == "temporary repository error"

    intake.materialize("research-01", repo_root=tmp_path)
    assert intake.status("research-01")["status"] == "RESEARCHING"
    assert intake.status("research-01")["error"] is None
