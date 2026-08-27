from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.api import strategy_research_discord_notifier as notifier


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _lab(root: Path) -> Path:
    lab = root / "labs" / "strategy-discord-1"
    _write(
        lab / "request.json",
        {
            "request_id": "strategy-discord-1",
            "goal": "5일선 전략을 백테스트해줘",
            "source": "discord",
            "discord_channel_id": "channel-1",
            "discord_message_id": "message-1",
            "discord_thread_id": "thread-1",
        },
    )
    (lab / "events.jsonl").write_text(
        json.dumps(
            {
                "event_type": "DECISION",
                "payload": {"plan_id": "plan-1", "decision": "PIVOT"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write(
        lab / "results" / "plan-1.json",
        {
            "plan_id": "plan-1",
            "status": "COMPLETED",
            "failure_reason": "검증기간 성과가 음수라 승격하지 않음",
            "metrics": {
                "5/20": {
                    "development": {
                        "total_return": -0.1,
                        "sharpe_0rf": -0.2,
                        "max_drawdown": -0.3,
                        "trade_count": 4,
                    },
                    "out_of_sample": {
                        "total_return": 0.2,
                        "sharpe_0rf": 0.4,
                        "max_drawdown": -0.1,
                        "trade_count": 2,
                    },
                }
            },
        },
    )
    return lab


def test_notifier_posts_one_bounded_report_and_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "research"
    _lab(root)
    _write(tmp_path / "state" / "sent.json", {"initialized_at": "2026-01-01T00:00:00+00:00", "sent": {}})
    monkeypatch.setenv("STRATEGY_DISCORD_REPORT_ENABLED", "true")
    monkeypatch.setenv("DISCORD_BOT_TOKEN_CEO", "token")
    monkeypatch.setenv("DISCORD_CEO_CHANNEL_ID", "channel-1")
    posts: list[tuple[dict[str, str], str]] = []

    monkeypatch.setattr(
        notifier,
        "_post_to_discord",
        lambda _token, correlation, content: posts.append((dict(correlation), content)) or True,
    )
    worker = notifier.StrategyReportNotifier(root, tmp_path / "state")

    assert worker.run_once() == {"status": "READY", "scanned": 1, "posted": 1, "failed": 0}
    assert worker.run_once() == {"status": "READY", "scanned": 1, "posted": 0, "failed": 0}
    assert posts[0][0] == {
        "channel_id": "channel-1",
        "message_id": "message-1",
        "thread_id": "thread-1",
    }
    assert "전략 Hermes 백테스트 완료" in posts[0][1]
    assert "주문 생성: 없음" in posts[0][1]


def test_notifier_baselines_old_results_without_posting(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "research"
    _lab(root)
    monkeypatch.setenv("STRATEGY_DISCORD_REPORT_ENABLED", "true")
    monkeypatch.setenv("DISCORD_BOT_TOKEN_CEO", "token")
    monkeypatch.setenv("DISCORD_CEO_CHANNEL_ID", "channel-1")
    monkeypatch.setattr(notifier, "_post_to_discord", lambda *_args: pytest.fail("historical result replayed"))

    worker = notifier.StrategyReportNotifier(root, tmp_path / "state")
    assert worker.run_once() == {
        "status": "BASELINE_INITIALIZED",
        "scanned": 0,
        "posted": 0,
        "failed": 0,
    }


def test_notifier_posts_a_validation_block_as_a_final_report(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "research"
    lab = _lab(root)
    (root / "labs" / lab.name / "results" / "plan-1.json").unlink()
    _write(
        root / "errors" / f"{lab.name}.json",
        {
            "request_id": lab.name,
            "phase": "HERMES_OR_VERIFY",
            "error": "ValueError: completed result requires measured metrics",
            "updated_at": "2026-08-27T03:00:00+00:00",
        },
    )
    _write(tmp_path / "state" / "sent.json", {"initialized_at": "2026-01-01T00:00:00+00:00", "sent": {}})
    monkeypatch.setenv("STRATEGY_DISCORD_REPORT_ENABLED", "true")
    monkeypatch.setenv("DISCORD_BOT_TOKEN_CEO", "token")
    monkeypatch.setenv("DISCORD_CEO_CHANNEL_ID", "channel-1")
    posts: list[str] = []
    monkeypatch.setattr(notifier, "_post_to_discord", lambda _token, _correlation, content: posts.append(content) or True)

    worker = notifier.StrategyReportNotifier(root, tmp_path / "state")
    assert worker.run_once()["posted"] == 1
    assert "전략 Hermes 연구 차단" in posts[0]
    assert "주문 생성: 없음" in posts[0]


def test_legacy_resolution_requires_one_exact_message(monkeypatch) -> None:
    request = {"goal": "<@123> 5일선 전략을 백테스트해줘", "source": "discord"}
    messages = [
        {"id": "message-1", "channel_id": "channel-1", "content": "<@999> 5일선 전략을 백테스트해줘"},
    ]
    correlation, _ = notifier._correlation(
        request,
        token="token",
        configured_channel_id="channel-1",
        recent_messages=messages,
    )
    assert correlation == {
        "channel_id": "channel-1",
        "message_id": "message-1",
        "thread_id": "message-1",
    }
