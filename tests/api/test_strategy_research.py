from __future__ import annotations

from pathlib import Path

import pytest

from apps.api import strategy_research


@pytest.fixture(autouse=True)
def no_real_kanban_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        strategy_research,
        "_ensure_tracking_root",
        lambda **_kwargs: (None, "UNAVAILABLE"),
    )


def test_strategy_research_api_enqueues_a_natural_language_goal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_research, "_lab_root", lambda: tmp_path / "research")
    request = strategy_research.StrategyResearchAsk(
        query="코스피 단기 반전 전략을 연구하고 백테스트해줘",
    )

    accepted = strategy_research.strategy_research_ask(request, "user-a")
    status = strategy_research.strategy_research_status(accepted.request_id, "user-a")

    assert accepted.accepted is True
    assert accepted.status == "QUEUED"
    assert accepted.lab_id == accepted.request_id
    assert status.status == "QUEUED"
    assert status.goal == request.query


def test_strategy_research_status_is_scoped_to_the_requesting_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_research, "_lab_root", lambda: tmp_path / "research")
    accepted = strategy_research.strategy_research_ask(
        strategy_research.StrategyResearchAsk(
            query="Find a robust cross-sectional alpha strategy",
            request_id="research-02",
        ),
        "user-a",
    )

    with pytest.raises(strategy_research.HTTPException) as error:
        strategy_research.strategy_research_status(accepted.request_id, "user-b")

    assert error.value.status_code == 403


def test_duplicate_submission_reports_the_current_lab_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_research, "_lab_root", lambda: tmp_path / "research")
    request = strategy_research.StrategyResearchAsk(query="Find a robust cross-sectional alpha strategy", request_id="research-03")
    first = strategy_research.strategy_research_ask(request, "user-a")
    intake = strategy_research.ResearchIntake(tmp_path / "research")
    intake.materialize(first.request_id, repo_root=tmp_path)

    replay = strategy_research.strategy_research_ask(request, "user-a")

    assert replay.duplicate is True
    assert replay.status == "RESEARCHING"


def test_strategy_tracking_root_is_persisted_when_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(strategy_research, "_lab_root", lambda: tmp_path / "research")
    def fake_tracking_root(**kwargs):
        kwargs["intake"].bind_kanban_root(kwargs["request_id"], "t_strategy_root")
        return "t_strategy_root", "CREATED"

    monkeypatch.setattr(strategy_research, "_ensure_tracking_root", fake_tracking_root)

    accepted = strategy_research.strategy_research_ask(
        strategy_research.StrategyResearchAsk(
            query="코스피 돌파 전략을 연구하고 백테스트해줘",
            request_id="research-root-01",
        ),
        "user-a",
    )

    assert accepted.kanban_root_task_id == "t_strategy_root"
    assert accepted.kanban_tracking_status == "CREATED"
    request = (tmp_path / "research" / "intake" / "research-root-01.json").read_text()
    assert "t_strategy_root" in request


def test_blocked_strategy_can_only_be_escalated_for_human_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(strategy_research, "_lab_root", lambda: tmp_path / "research")
    accepted = strategy_research.strategy_research_ask(
        strategy_research.StrategyResearchAsk(
            query="코스피 돌파 전략을 연구하고 백테스트해줘",
            request_id="research-promotion-01",
        ),
        "user-a",
    )
    intake = strategy_research.ResearchIntake(tmp_path / "research")
    intake.record_error(accepted.request_id, phase="DATA", error="PIT universe unavailable")

    promoted = strategy_research.strategy_research_promote(
        accepted.request_id,
        strategy_research.StrategyPromotionAsk(
            mode="live", confirm=True, override_blocked=True
        ),
        "user-a",
    )

    assert promoted.status == "REVIEW_REQUIRED"
    assert "강제 배포하지 않고" in promoted.message
