from __future__ import annotations

from pathlib import Path
import stat

import pytest

import sys

AUTONOMOUS_DIR = Path(__file__).resolve().parents[2] / "departments/01-research/autonomous"
if str(AUTONOMOUS_DIR) not in sys.path:
    sys.path.insert(0, str(AUTONOMOUS_DIR))

from autonomous_research_ingress import (  # noqa: E402
    ResearchIntake,
    ResearchRequestConflict,
    looks_like_strategy_research,
)


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
    assert stat.S_IMODE((intake.intake_dir / "research-01.json").stat().st_mode) == 0o644
    with pytest.raises(ResearchRequestConflict):
        intake.submit(_payload() | {"goal": "A different research objective"})


def test_tracking_root_update_keeps_shared_manifest_readable(tmp_path: Path) -> None:
    intake = ResearchIntake(tmp_path / "research")
    intake.submit(_payload())

    intake.bind_kanban_root("research-01", "t_strategy_root")

    manifest = intake.intake_dir / "research-01.json"
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o644
    assert intake._read_json(manifest)["kanban_root_task_id"] == "t_strategy_root"


def test_materialize_creates_an_isolated_persistent_lab(tmp_path: Path) -> None:
    intake = ResearchIntake(tmp_path / "research")
    intake.submit(_payload())

    lab_path = intake.materialize("research-01", repo_root=tmp_path)
    status = intake.status("research-01")

    assert lab_path == tmp_path / "research" / "labs" / "research-01"
    assert (lab_path / "objective.json").exists()
    assert (lab_path / "request.json").exists()
    assert stat.S_IMODE((lab_path / "request.json").stat().st_mode) == 0o644
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


def test_discord_correlation_is_persisted_with_the_request_but_not_invented() -> None:
    payload = _payload() | {
        "source": "discord",
        "source_message_id": "discord-message-1",
        "discord_channel_id": "channel-1",
        "discord_message_id": "message-1",
        "discord_guild_id": "guild-1",
        "discord_thread_id": "thread-1",
    }

    from autonomous_research_ingress import normalize_request

    normalized = normalize_request(payload)
    assert normalized["discord_channel_id"] == "channel-1"
    assert normalized["discord_message_id"] == "message-1"
    assert normalized["discord_thread_id"] == "thread-1"
    assert normalize_request(_payload())["discord_channel_id"] is None


def test_worker_error_is_visible_until_the_next_successful_materialization(tmp_path: Path) -> None:
    intake = ResearchIntake(tmp_path / "research")
    intake.submit(_payload())
    intake.record_error("research-01", phase="MATERIALIZE", error="temporary repository error")

    assert intake.status("research-01")["status"] == "BLOCKED"
    assert intake.status("research-01")["error"] == "temporary repository error"

    intake.materialize("research-01", repo_root=tmp_path)
    assert intake.status("research-01")["status"] == "RESEARCHING"
    assert intake.status("research-01")["error"] is None


def test_shared_worker_error_is_readable_by_the_status_consumer(tmp_path: Path) -> None:
    intake = ResearchIntake(tmp_path / "research")
    intake.submit(_payload())
    intake.record_error("research-01", phase="MATERIALIZE", error="temporary repository error")

    error_path = intake.errors_dir / "research-01.json"
    assert stat.S_IMODE(error_path.stat().st_mode) == 0o644


def test_blocked_result_is_visible_as_blocked_without_worker_error(tmp_path: Path) -> None:
    intake = ResearchIntake(tmp_path / "research")
    intake.submit(_payload("research-04"))
    lab_path = intake.materialize("research-04", repo_root=tmp_path)
    (lab_path / "results").mkdir(exist_ok=True)
    (lab_path / "results" / "plan-1.json").write_text(
        '{"status":"BLOCKED","failure_reason":"insufficient daily history"}',
        encoding="utf-8",
    )

    status = intake.status("research-04")

    assert status is not None
    assert status["status"] == "BLOCKED"
    assert status["error"] == "insufficient daily history"


def test_completed_result_is_visible_as_completed_without_replaying_work(tmp_path: Path) -> None:
    intake = ResearchIntake(tmp_path / "research")
    intake.submit(_payload("research-completed"))
    lab_path = intake.materialize("research-completed", repo_root=tmp_path)
    (lab_path / "results" / "plan-1.json").write_text(
        '{"status":"COMPLETED","plan_id":"plan-1"}',
        encoding="utf-8",
    )

    status = intake.status("research-completed")

    assert status is not None
    assert status["status"] == "COMPLETED"


def test_strategy_backtest_only_query_routes_to_strategy_research() -> None:
    assert looks_like_strategy_research(
        "미래에셋증권 ５일선이 ２０일선 골든 크로스시 매수 데드 크로스시 매도하는 전략 백테스트 해줘"
    ) is True
    assert looks_like_strategy_research("미래에셋증권 주가 알려줘") is False
