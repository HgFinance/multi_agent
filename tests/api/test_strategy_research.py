from __future__ import annotations

from pathlib import Path

import pytest

from apps.api import strategy_research


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
