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


def test_notifier_posts_a_new_final_after_a_retry_result_is_decided(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "research"
    lab = _lab(root)
    _write(tmp_path / "state" / "sent.json", {"initialized_at": "2026-01-01T00:00:00+00:00", "sent": {}})
    monkeypatch.setenv("STRATEGY_DISCORD_REPORT_ENABLED", "true")
    monkeypatch.setenv("DISCORD_BOT_TOKEN_CEO", "token")
    monkeypatch.setenv("DISCORD_CEO_CHANNEL_ID", "channel-1")
    posts: list[str] = []
    monkeypatch.setattr(
        notifier,
        "_post_to_discord",
        lambda _token, _correlation, content: posts.append(content) or True,
    )
    worker = notifier.StrategyReportNotifier(root, tmp_path / "state")

    assert worker.run_once()["posted"] == 1
    _write(
        lab / "results" / "plan-1.json",
        {
            "plan_id": "plan-1",
            "status": "BLOCKED",
            "failure_reason": "LS t1444 timed out",
        },
    )
    _write(
        lab / "results" / "plan-2.json",
        {
            "plan_id": "plan-2",
            "status": "COMPLETED",
            "metrics": {"5/20": {"out_of_sample": {"trade_count": 3}}},
        },
    )
    with (lab / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"event_type": "DECISION", "payload": {"plan_id": "plan-2", "decision": "PIVOT"}}
            )
            + "\n"
        )

    assert worker.run_once()["posted"] == 1
    assert len(posts) == 2
    assert "상태: COMPLETED · 최종판정: PIVOT" in posts[1]
    assert "이력상 BLOCKED 1건" in posts[1]
    assert "주요 사유: LS t1444 timed out" not in posts[1]


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


def test_report_formats_breakout_metrics_and_plan_scope(tmp_path: Path) -> None:
    lab = _lab(tmp_path / "research")
    _write(
        lab / "plans" / "plan-1.json",
        {
            "method": {
                "strategy": "target_t = open_t + alpha * (high_{t-1} - low_{t-1})",
                "alphas": [0.25, 0.5, 0.75, 1.0],
                "primary_alpha": 0.5,
            },
            "signature": {"signature": "daily_t8451_adjusted_fixed8_v1"},
            "data_requirements": [
                json.dumps(
                    {
                        "adjusted": True,
                        "range": ["20200101", "20260826"],
                        "symbols": ["005930", "000660"],
                        "tr_code": "t8451",
                    }
                )
            ],
            "splits": [
                json.dumps(
                    {
                        "development": "20200101-20221231",
                        "validation": "20230101-20241231",
                        "out_of_sample": "20250101-20260826",
                    }
                )
            ],
            "cost_model": json.dumps(
                {"primary": "fee=0.0010 plus slippage=0.0010 per side"}
            ),
        },
    )
    result = {
        "plan_id": "plan-1",
        "status": "FAILED",
        "metrics": {
            "development_2020_2022": {
                "mean_net_return": -0.001,
                "profit_factor": 0.9,
                "trade_count": 10,
            },
            "validation_2023_2024": {
                "mean_net_return": -0.002,
                "profit_factor": 0.8,
                "trade_count": 8,
            },
            "oos_2025_20260826": {
                "compound_return": -0.1,
                "mean_net_return": -0.003,
                "hit_rate": 0.43,
                "max_drawdown": -0.2,
                "profit_factor": 0.7,
                "trade_count": 6,
            },
            "alpha_grid_mean_net_return": {
                "0.25": -0.001,
                "0.5": -0.003,
                "0.75": 0.001,
                "1.0": -0.002,
            },
        },
        "robustness": {
            "parameter_grid_has_positive_oos": False,
            "delayed_execution_survives": False,
        },
        "failure_reason": "증거 게이트 실패",
    }
    report = notifier._report_content(
        {"request_id": "request-1", "goal": "돌파 전략", "_lab_path": str(lab)},
        result,
        events=[
            {"event_type": "DECISION", "payload": {"plan_id": "plan-1", "decision": "PAUSE"}}
        ],
        lab_id="lab-1",
    )

    assert len(report) <= 1950
    assert "[핵심 지표]" in report
    assert "OOS: 복리 -10.00%" in report
    assert "승률 43.00%" in report
    assert "α 민감도: 0.25 -0.10% · 0.5 -0.30% · 0.75 +0.10% · 1.0 -0.20%" in report
    assert "데이터: t8451 조정 일봉" in report
    assert "구간: 개발 2020.01.01~2022.12.31" in report
    assert '"adjusted"' not in report
    assert "추적: request=request-1 · lab=lab-1" in report


def test_report_formats_intraday_stage_with_missing_development_data() -> None:
    result = {
        "plan_id": "intraday-1",
        "status": "BLOCKED",
        "metrics": {
            "intraday_primary_stages": {
                "development": {
                    "daily_proxy": {
                        "mean_net_return": -0.003,
                        "trade_count": 12,
                    },
                    "intraday": {"trade_count": 0},
                },
                "validation": {
                    "intraday": {
                        "mean_net_return": 0.002,
                        "hit_rate": 0.6,
                        "trade_count": 20,
                    }
                },
            },
            "alpha_sensitivity": {
                "0.5": {"mean_net_return": 0.002},
                "0.25": {"mean_net_return": 0.001},
            },
        },
        "failure_reason": "개발구간 장중 데이터 없음",
    }
    report = notifier._report_content(
        {"request_id": "request-2", "goal": "장중 체결 검증"},
        result,
        events=[],
        lab_id="lab-2",
    )

    assert "개발 일봉 proxy:" in report
    assert "장중자료 없음" in report
    assert "검증 장중:" in report
    assert "승률 60.00%" in report
    assert "α 민감도: 0.25 +0.10% · 0.5 +0.20%" in report


def test_report_formats_direct_hermes_intraday_metrics_and_scope(tmp_path: Path) -> None:
    lab = tmp_path / "labs" / "direct-hermes"
    _write(
        lab / "plans" / "p1.json",
        {
            "method": "{'signal': 'close>SMA5>SMA20>SMA60', 'exit': 'target +2%'}",
            "data_requirements": [json.dumps({
                "end_date": "20251230",
                "start_date": "20240102",
                "symbols": ["005930", "000660"],
                "timeframe": "3-minute integrated adjusted bars",
                "tr_code": "t8452",
            })],
            "splits": [json.dumps({
                "development": {"start": "20240102", "end": "20241231"},
                "validation": {"start": "20250102", "end": "20250630"},
                "out_of_sample": {"start": "20250701", "end": "20251230"},
            })],
        },
    )
    report = notifier._report_content(
        {"request_id": "request-direct", "goal": "3분봉 정배열", "_lab_path": str(lab)},
        {
            "plan_id": "p1",
            "status": "BLOCKED",
            "metrics": {
                "pooled_available_window": {
                    "compound_net_return": 2.4958,
                    "mean_net_return": 0.0180,
                    "max_drawdown": -0.0054,
                    "trades": 70,
                    "win_rate": 0.9714,
                }
            },
            "failure_reason": "개발·검증 분봉 데이터 없음",
        },
        events=[],
        lab_id="direct-hermes",
    )

    assert "가용구간 종합: 복리 +249.58%" in report
    assert "승률 97.14%" in report
    assert "거래 70회" in report
    assert "데이터: t8452 3-minute integrated adjusted bars" in report
    assert "조정 일봉" not in report


def test_report_formats_max_range_hermes_oos_and_actual_scope() -> None:
    report = notifier._report_content(
        {"request_id": "request-max", "goal": "최대 가용 3분봉 검증"},
        {
            "plan_id": "max-1",
            "status": "COMPLETED",
            "metrics": {
                "aggregate_strategy": {
                    "cumulative_compounded": 7.0,
                    "mean_net_return": 0.0135,
                    "max_drawdown": -0.417,
                    "trades": 168,
                },
                "by_symbol": {
                    "005930": {
                        "first_bar": "20250901 090300",
                        "last_bar": "20260827 153000",
                        "out_of_sample": {"mean_net_return": 0.0020, "trades": 19},
                    }
                },
            },
        },
        events=[{"event_type": "DECISION", "payload": {"plan_id": "max-1", "decision": "PIVOT"}}],
        lab_id="max-1",
    )

    assert "전체: 복리 +700.00%" in report
    assert "005930 OOS: 평균순익 +0.20%" in report
    assert "실제 반환: 005930 20250901 090300~20260827 153000" in report
    assert "상태: COMPLETED · 판정: PIVOT" in report


def test_notifier_waits_for_all_registered_experiments_before_final_report(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "research"
    lab = _lab(root)
    _write(lab / "plans" / "plan-1.json", {"plan_id": "plan-1"})
    _write(lab / "plans" / "plan-2.json", {"plan_id": "plan-2"})
    _write(
        lab / "results" / "plan-2.json",
        {"plan_id": "plan-2", "status": "FAILED", "failure_reason": "추가 검증 실패"},
    )
    _write(tmp_path / "state" / "sent.json", {"initialized_at": "2026-01-01T00:00:00+00:00", "sent": {}})
    monkeypatch.setenv("STRATEGY_DISCORD_REPORT_ENABLED", "true")
    monkeypatch.setenv("DISCORD_BOT_TOKEN_CEO", "token")
    monkeypatch.setenv("DISCORD_CEO_CHANNEL_ID", "channel-1")
    posts: list[str] = []
    monkeypatch.setattr(notifier, "_post_to_discord", lambda _token, _correlation, content: posts.append(content) or True)
    worker = notifier.StrategyReportNotifier(root, tmp_path / "state")

    assert worker.run_once()["posted"] == 0
    with (lab / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"event_type": "DECISION", "payload": {"plan_id": "plan-2", "decision": "PAUSE"}}
            )
            + "\n"
        )

    assert worker.run_once()["posted"] == 1
    assert "전략 Hermes 백테스트 완료 · 최종 보고서" in posts[0]
    assert "실험 2건 완료" in posts[0]
    assert "plan-1" in posts[0] and "plan-2" in posts[0]


def test_unregistered_plan_artifact_does_not_hold_back_a_final_report(tmp_path: Path) -> None:
    lab = _lab(tmp_path / "research")
    _write(lab / "plans" / "plan-1.json", {"plan_id": "plan-1"})
    _write(lab / "plans" / "plan-2.json", {"plan_id": "plan-2"})
    with (lab / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"event_type": "PLAN_CREATED", "payload": {"plan_id": "plan-1"}}
            )
            + "\n"
        )

    assert notifier._lab_is_final(
        lab,
        events=notifier._events(lab / "events.jsonl"),
        result_ids={"plan-1"},
    ) is True
