from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from apps.api import conditional_rules as api
from apps.api.conditional_rule_language import looks_like_conditional_paper_rule
from apps.api.conditional_rule_workflow import (
    ConditionalRuleConflict,
    InMemoryConditionalRuleRepository,
)
from orchestration.compound_paper_orders import (
    build_compound_conditional_candidate,
    parse_compound_paper_order,
)
from orchestration.conditional_rules import RuleState


NOW = datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc)
USER_ID = "10000000-0000-0000-0000-000000000001"
FUND_ID = "20000000-0000-0000-0000-000000000001"
BOOK_ID = "30000000-0000-0000-0000-000000000001"
INSTRUMENT_ID = "40000000-0000-0000-0000-000000000001"


def install_scope(monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "require_trading_book_access",
        lambda subject, fund_id, book_id: {
            "user_id": USER_ID,
            "fund_id": FUND_ID,
            "book_id": BOOK_ID,
        },
    )
    monkeypatch.setattr(
        api,
        "resolve_active_trading_instrument",
        lambda symbol, instrument_id: {
            "instrument_id": INSTRUMENT_ID,
            "symbol": "005930",
        },
    )


def profit_candidate() -> dict:
    return {
        "symbol": "삼성전자",
        "condition": {
            "type": "COMPARISON",
            "operator": "GTE",
            "left": {
                "type": "ARITHMETIC",
                "operator": "SUB",
                "left": {
                    "type": "ARITHMETIC",
                    "operator": "DIV",
                    "left": {"type": "MARKET", "field": "LAST_PRICE"},
                    "right": {"type": "PORTFOLIO", "field": "AVG_ENTRY_PRICE"},
                },
                "right": {"type": "LITERAL", "value": "1", "unit": "RATIO"},
            },
            "right": {"type": "LITERAL", "value": "0.05", "unit": "RATIO"},
        },
        "action": {
            "side": "SELL",
            "sizing": {"type": "POSITION_PERCENT", "value": "0.20"},
        },
        "evaluation": {"clock": "QUOTE"},
        "expires_at": (NOW + timedelta(days=30)).isoformat(),
    }


def preview_request(raw: str, candidate: dict | None = None):
    return api.ConditionalRulePreviewRequest.model_validate(
        {
            "fund_id": FUND_ID,
            "book_id": BOOK_ID,
            "raw_instruction": raw,
            "candidate": candidate or profit_candidate(),
        }
    )



@pytest.mark.parametrize(
    "raw",
    (
        "000660 SK하이닉스 현재가가 2000000원보다 낮으면 1800000원 지정가로 1주 매수해줘",
        "삼성전자 3분봉 60일선 돌파시 1주 매수해줘",
        "삼성전자 주가가 5일 이동평균선보다 높으면 1주 매수",
        "네이버 주가가 200000원 아래로 떨어지면 1주 매도",
        "삼성전자 볼린저밴드 상단에 닿으면 1주 매도",
        "삼성전자 1퍼 오르면 매도해주고 1퍼 내리면 매수해",
        "현대약품 오르면 사줘",
        "삼성전자 1% 오르먼 1주 매수",
    ),
)
def test_explicit_korean_condition_endings_route_to_conditional_lane(raw: str) -> None:
    assert looks_like_conditional_paper_rule(raw) is True


def test_semantic_rejection_is_a_client_error_that_names_the_field(monkeypatch) -> None:
    """A bad field name is the caller's mistake, not a server fault.

    This escaped as an unhandled 500 on 2026-08-27, so the rejection reached
    the user as a bare UNSUPPORTED_PORTFOLIO_FIELD with nothing to correct.
    """

    install_scope(monkeypatch)
    candidate = {
        "symbol": "한온시스템",
        "condition": {
            "type": "COMPARISON",
            "operator": "GTE",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": {
                "type": "ARITHMETIC",
                "operator": "MUL",
                "left": {"type": "PORTFOLIO", "field": "AVG_BUY_PRICE"},
                "right": {"type": "LITERAL", "value": "1.01", "unit": "NUMBER"},
            },
        },
        "action": {"side": "SELL", "sizing": {"type": "ALL"}},
        "evaluation": {"clock": "QUOTE"},
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
    }

    with pytest.raises(api.HTTPException) as raised:
        api._build_preview(
            preview_request("한온시스템 매수가 대비 1% 오르면 전량 매도", candidate),
            subject=USER_ID,
            now=NOW,
        )

    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "UNSUPPORTED_PORTFOLIO_FIELD"
    assert "AVG_BUY_PRICE" in raised.value.detail["message"]


def test_explicit_intrabar_rsi_cross_keeps_one_existing_conditional_route(monkeypatch) -> None:
    install_scope(monkeypatch)
    candidate = {
        "symbol": "삼성전자",
        "condition": {
            "type": "CROSS",
            "operator": "ABOVE",
            "left": {"type": "INDICATOR", "name": "RSI", "timeframe": "1M"},
            "right": {"type": "LITERAL", "value": "70", "unit": "NUMBER"},
        },
        "action": {"side": "SELL", "sizing": {"type": "FIXED_SHARES", "value": "1"}},
        "evaluation": {"clock": "INTRABAR", "primary_timeframe": "1M"},
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
    }

    preview = api._build_preview(
        preview_request("삼성전자 1분봉 RSI 70을 장중 실시간 돌파하면 1주 매도", candidate),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.spec.evaluation.clock.value == "INTRABAR"
    assert preview.spec.evaluation.primary_timeframe.value == "1M"


def test_intrabar_rejects_unfinished_volume_input(monkeypatch) -> None:
    install_scope(monkeypatch)
    candidate = {
        "symbol": "삼성전자",
        "condition": {
            "type": "COMPARISON",
            "operator": "GTE",
            "left": {"type": "MARKET", "field": "VOLUME"},
            "right": {"type": "LITERAL", "value": "100", "unit": "VOLUME"},
        },
        "action": {"side": "SELL", "sizing": {"type": "FIXED_SHARES", "value": "1"}},
        "evaluation": {"clock": "INTRABAR", "primary_timeframe": "1M"},
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
    }

    with pytest.raises(api.HTTPException) as raised:
        api._build_preview(
            preview_request("삼성전자 장중 거래량이 100 이상이면 1주 매도", candidate),
            subject=USER_ID,
            now=NOW,
        )

    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "INTRABAR_FIELD_UNSUPPORTED"


def test_korean_relative_move_without_baseline_or_quantity_is_not_activated(monkeypatch) -> None:
    """A relative move pair must not invent its baseline or share counts."""

    install_scope(monkeypatch)
    raw = "삼성전자 1퍼 오르면 매도해주고 1퍼 내리면 매수해"
    candidate = {
        "symbol": "삼성전자",
        "condition": {
            "type": "COMPARISON",
            "operator": "GTE",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": {
                "type": "ARITHMETIC",
                "operator": "MUL",
                "left": {"type": "PORTFOLIO", "field": "AVG_ENTRY_PRICE"},
                "right": {"type": "LITERAL", "value": "1.01", "unit": "NUMBER"},
            },
        },
        "action": {"side": "SELL", "sizing": {"type": "FIXED_SHARES", "value": "1"}},
        "evaluation": {"clock": "QUOTE"},
    }

    preview = api._build_preview(
        preview_request(raw, candidate),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is False
    assert preview.clarification_codes == (
        "QUANTITY_REQUIRED",
        "AMBIGUOUS_RETURN_BASELINE",
    )


def test_front_loaded_single_share_quantity_is_bound_to_later_sell_action(monkeypatch) -> None:
    """A trigger between ``10주`` and ``매도`` must not erase the sizing."""

    install_scope(monkeypatch)
    raw = "원익 10주 1분봉 엔빌로프(20,5) 상단선 돌파시 시장가 매도"
    candidate = {
        "symbol": "원익",
        "condition": {
            "type": "CROSS",
            "operator": "ABOVE",
            "left": {"type": "MARKET", "field": "CLOSE"},
            "right": {
                "type": "INDICATOR",
                "name": "ENVELOPE",
                "output": "UPPER",
                "timeframe": "1M",
                "parameters": {"PERIOD": 20, "PERCENT": 5},
            },
        },
        "action": {
            "side": "SELL",
            "sizing": {"type": "FIXED_SHARES", "value": "10"},
            "order_type": "MARKET",
            "time_in_force": "DAY",
        },
        "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "1M"},
    }

    preview = api._build_preview(
        preview_request(raw, candidate),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is True
    assert preview.clarification_codes == ()


@pytest.mark.parametrize(
    "raw",
    (
        "원익 10주 20주 1분봉 엔빌로프 상단선 돌파시 시장가 매도",
        "원익 10주 1분봉 엔빌로프 상단선 돌파시 시장가 매도 후 매수",
    ),
)
def test_front_loaded_quantity_stays_ambiguous_for_multiple_sizes_or_actions(
    monkeypatch, raw: str
) -> None:
    """The fallback may bind only a unique quantity to a unique action."""

    install_scope(monkeypatch)
    candidate = {
        "symbol": "원익",
        "condition": {
            "type": "CROSS",
            "operator": "ABOVE",
            "left": {"type": "MARKET", "field": "CLOSE"},
            "right": {
                "type": "INDICATOR",
                "name": "ENVELOPE",
                "output": "UPPER",
                "timeframe": "1M",
                "parameters": {"PERIOD": 20, "PERCENT": 5},
            },
        },
        "action": {
            "side": "SELL",
            "sizing": {"type": "FIXED_SHARES", "value": "10"},
        },
        "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "1M"},
    }

    preview = api._build_preview(
        preview_request(raw, candidate),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is False
    assert preview.clarification_codes == ("QUANTITY_REQUIRED",)


@pytest.mark.parametrize(
    "raw",
    (
        # The shape that was rejected on 2026-08-27: a wall-clock trigger with
        # two instruments never reached the conditional lane, so Trading
        # refused it for missing the conditional-rule marker.
        "두산로보틱스, 레인보우로보틱스 각각 10주 1주 15:15 되면 시장가 매수 해줘",
        "두산로보틱스 15:15 되면 10주 시장가 매수",
        "두산로보틱스 15시 15분에 10주 시장가 매수",
        "오후 3시에 삼성전자 1주 매수",
    ),
)
def test_absolute_clock_time_triggers_route_to_conditional_lane(raw: str) -> None:
    assert looks_like_conditional_paper_rule(raw) is True


@pytest.mark.parametrize(
    "raw",
    (
        # "3시간" must stay with the relative grammar, and neither an immediate
        # order nor a question may be pulled into the conditional lane.
        "삼성전자 3시간 뒤에 사도 될까?",
        "삼성전자 지금 1주 매수해줘",
        "하이닉스 1,674,000원에 2주 매수하는거 추천해?",
    ),
)
def test_non_conditional_requests_stay_off_the_conditional_lane(raw: str) -> None:
    assert looks_like_conditional_paper_rule(raw) is False


def test_three_minute_request_preserves_the_requested_completed_bar_feed(monkeypatch) -> None:
    install_scope(monkeypatch)
    candidate = {
        "symbol": "삼성전자",
        "condition": {
            "type": "CROSS",
            "operator": "ABOVE",
            "left": {"type": "MARKET", "field": "CLOSE"},
            "right": {
                "type": "INDICATOR",
                "name": "SMA",
                "timeframe": "3M",
                "parameters": {"PERIOD": 60},
            },
        },
        "action": {"side": "BUY", "sizing": {"type": "FIXED_SHARES", "value": "1"}},
        "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "3M"},
    }

    preview = api._build_preview(
        preview_request("삼성전자 3분봉 60일선 돌파시 1주 매수해줘", candidate),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is True
    assert preview.spec.evaluation.primary_timeframe.value == "3M"
    assert preview.spec.condition.right.timeframe.value == "3M"
    assert "TIMEFRAME_FALLBACK_3M_TO_5M" not in preview.assumptions
    assert preview.summary["condition_overview"] == {
        "trigger_style": "EDGE",
        "referenced_timeframes": ["3M"],
        "indicators": [
            {
                "name": "SMA",
                "output": "VALUE",
                "timeframe": "3M",
                "parameters": {"PERIOD": "60"},
            }
        ],
        "evaluation_boundary": "LATEST_COMPLETED_BAR_AT_OR_BEFORE_PRIMARY_CLOSE",
        "time_window_kst": [],
    }


def test_trailing_stop_preview_explains_its_durable_high_watermark(monkeypatch) -> None:
    install_scope(monkeypatch)
    raw = "하이닉스 평균 매입가 대비 2% 수익이 난 뒤 고점 대비 1% 하락하면 전량 매도해줘"
    assert looks_like_conditional_paper_rule(raw) is True
    candidate = {
        "symbol": "하이닉스",
        "condition": {
            "type": "TRAILING_STOP",
            "parameters": {"DRAWDOWN": "0.01", "ACTIVATION_RETURN": "0.02"},
        },
        "action": {"side": "SELL", "sizing": {"type": "ALL"}},
        "evaluation": {"clock": "QUOTE"},
    }

    preview = api._build_preview(
        preview_request(
            raw,
            candidate,
        ),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is True
    assert {
        "DURABLE_HIGH_WATERMARK",
        "TRAILING_STOP_SELL_ONLY",
        "FRESH_QUOTE_ONLY",
    } <= set(preview.assumptions)
    assert preview.summary["condition_overview"]["trailing_stop"] == {
        "drawdown_ratio": "0.01",
        "drawdown_mode": "PRICE_RATIO",
        "activation_return_ratio": "0.02",
        "watermark": "HIGHEST_FRESH_QUOTE_SINCE_ACTIVE",
        "expected_position_quantity": None,
    }


def test_multi_timeframe_indicator_confirmation_is_activatable_and_summarized(monkeypatch) -> None:
    install_scope(monkeypatch)
    candidate = {
        "symbol": "하이닉스",
        "condition": {
            "type": "LOGICAL",
            "operator": "AND",
            "children": [
                {
                    "type": "CROSS",
                    "operator": "ABOVE",
                    "left": {
                        "type": "INDICATOR",
                        "name": "SMA",
                        "timeframe": "3M",
                        "parameters": {"PERIOD": 5},
                    },
                    "right": {
                        "type": "INDICATOR",
                        "name": "SMA",
                        "timeframe": "3M",
                        "parameters": {"PERIOD": 20},
                    },
                },
                {
                    "type": "COMPARISON",
                    "operator": "LT",
                    "left": {
                        "type": "INDICATOR",
                        "name": "RSI",
                        "timeframe": "15M",
                        "parameters": {"PERIOD": 14},
                    },
                    "right": {"type": "LITERAL", "value": "70", "unit": "NUMBER"},
                },
            ],
        },
        "action": {"side": "BUY", "sizing": {"type": "FIXED_SHARES", "value": "2"}},
        "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "3M"},
    }

    preview = api._build_preview(
        preview_request(
            "하이닉스 3분봉 5선이 20선 상향 돌파하고 15분봉 RSI(14)가 70 미만이면 2주 시장가 매수",
            candidate,
        ),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is True
    assert preview.clarification_codes == ()
    overview = preview.summary["condition_overview"]
    assert overview["trigger_style"] == "EDGE"
    assert overview["referenced_timeframes"] == ["3M", "15M"]
    assert overview["evaluation_boundary"] == "LATEST_COMPLETED_BAR_AT_OR_BEFORE_PRIMARY_CLOSE"
    assert overview["time_window_kst"] == []


def test_indicator_rule_with_explicit_kst_time_window_is_activatable(monkeypatch) -> None:
    install_scope(monkeypatch)
    candidate = {
        "symbol": "하이닉스",
        "condition": {
            "type": "LOGICAL",
            "operator": "AND",
            "children": [
                {
                    "type": "CROSS",
                    "operator": "ABOVE",
                    "left": {
                        "type": "INDICATOR",
                        "name": "SMA",
                        "timeframe": "3M",
                        "parameters": {"PERIOD": 5},
                    },
                    "right": {
                        "type": "INDICATOR",
                        "name": "SMA",
                        "timeframe": "3M",
                        "parameters": {"PERIOD": 20},
                    },
                },
                {
                    "type": "COMPARISON",
                    "operator": "GTE",
                    "left": {"type": "TIME", "field": "KST_SECONDS_SINCE_MIDNIGHT"},
                    "right": {"type": "LITERAL", "value": "36000", "unit": "NUMBER"},
                },
                {
                    "type": "COMPARISON",
                    "operator": "LTE",
                    "left": {"type": "TIME", "field": "KST_SECONDS_SINCE_MIDNIGHT"},
                    "right": {"type": "LITERAL", "value": "52200", "unit": "NUMBER"},
                },
            ],
        },
        "action": {"side": "BUY", "sizing": {"type": "FIXED_SHARES", "value": "2"}},
        "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "3M"},
    }

    preview = api._build_preview(
        preview_request(
            "하이닉스 3분봉 5선이 20선 상향 돌파하고 10:00~14:30에만 2주 시장가 매수",
            candidate,
        ),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is True
    assert preview.clarification_codes == ()
    assert {"KST_TIME_WINDOW", "MARKET_SESSION_GUARD"} <= set(preview.assumptions)
    assert preview.summary["condition_overview"]["time_window_kst"] == [
        "GTE 10:00:00",
        "LTE 14:30:00",
    ]


def test_one_hour_korean_timeframe_text_is_accepted_as_explicit_evidence(monkeypatch) -> None:
    install_scope(monkeypatch)
    candidate = {
        "symbol": "삼성전자",
        "condition": {
            "type": "COMPARISON",
            "operator": "GT",
            "left": {"type": "INDICATOR", "name": "ADX", "timeframe": "1H"},
            "right": {"type": "LITERAL", "value": "25", "unit": "NUMBER"},
        },
        "action": {"side": "BUY", "sizing": {"type": "FIXED_SHARES", "value": "1"}},
        "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "1H"},
    }

    preview = api._build_preview(
        preview_request("삼성전자 1시간봉 ADX(14)가 25 초과면 1주 매수", candidate),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is True
    assert preview.clarification_codes == ()


def test_ambiguous_return_baseline_and_position_percent_are_blocked(monkeypatch) -> None:
    install_scope(monkeypatch)

    preview = api._build_preview(
        preview_request("삼성전자 5% 이상 상승시 비중 20% 매도"),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is False
    assert set(preview.clarification_codes) == {
        "AMBIGUOUS_POSITION_PERCENT",
        "AMBIGUOUS_RETURN_BASELINE",
    }


def test_explicit_average_entry_and_holding_percent_are_activatable(monkeypatch) -> None:
    install_scope(monkeypatch)

    preview = api._build_preview(
        preview_request("삼성전자 평균 매입가 대비 5% 상승시 보유수량의 20% 매도"),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is True
    assert preview.clarification_codes == ()
    assert preview.assumptions == (
        "PAPER_ONLY",
        "ONE_SHOT",
        "MARKET_CLOSED_REJECTS_WITHOUT_ORDER",
    )


@pytest.mark.parametrize(
    "raw",
    (
        "삼성전자 현재가가 7만원 이하면 100만원어치 시장가 매수",
        "삼성전자 현재가가 7만원 이하면 100만원 시장가 매수",
        "삼성전자 현재가가 7만원 이하면 100만원을 매수",
    ),
)
def test_explicit_krw_notional_is_activatable_and_exposes_execution_boundary(
    monkeypatch, raw: str
) -> None:
    install_scope(monkeypatch)
    candidate = {
        "symbol": "삼성전자",
        "condition": {
            "type": "COMPARISON",
            "operator": "LTE",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": {"type": "LITERAL", "value": "70000", "unit": "PRICE"},
        },
        "action": {
            "side": "BUY",
            "sizing": {"type": "NOTIONAL_KRW", "value": "1000000"},
        },
        "evaluation": {"clock": "QUOTE"},
    }

    preview = api._build_preview(
        preview_request(raw, candidate),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is True
    assert preview.clarification_codes == ()
    assert preview.summary["sizing"] == {
        "type": "NOTIONAL_KRW",
        "value": "1000000",
    }
    assert {
        "KRW_NOTIONAL_MAXIMUM",
        "FRESH_PRICE_AND_LOT_SIZE_AT_EXECUTION",
        "TRADING_QUOTE_CAP_RECHECK",
    } <= set(preview.assumptions)


def test_notional_candidate_must_match_the_exact_source_order_amount(monkeypatch) -> None:
    install_scope(monkeypatch)
    candidate = {
        "symbol": "삼성전자",
        "condition": {
            "type": "COMPARISON",
            "operator": "LTE",
            "left": {"type": "MARKET", "field": "LAST_PRICE"},
            "right": {"type": "LITERAL", "value": "70000", "unit": "PRICE"},
        },
        "action": {
            "side": "BUY",
            "sizing": {"type": "NOTIONAL_KRW", "value": "2000000"},
        },
        "evaluation": {"clock": "QUOTE"},
    }

    preview = api._build_preview(
        preview_request("삼성전자 현재가가 7만원 이하면 100만원 매수", candidate),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is False
    assert preview.clarification_codes == ("NOTIONAL_AMOUNT_MISMATCH",)


def test_deferred_entry_exit_bracket_candidate_is_a_valid_single_sell_rule(
    monkeypatch,
) -> None:
    install_scope(monkeypatch)
    plan = parse_compound_paper_order(
        "삼성전자 5주 시장가 매수하고 매수가 대비 3% 상승하면 매도하고 "
        "2% 하락하면 매도"
    )
    assert plan is not None

    preview = api._build_preview(
        preview_request(plan.conditional_instruction, build_compound_conditional_candidate(plan)),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is True
    assert preview.clarification_codes == ()
    assert preview.spec.action.side.value == "SELL"
    assert preview.spec.condition.type.value == "LOGICAL"
    assert preview.spec.condition.operator == "AND"
    assert (preview.spec.condition.children or ())[1].operator == "OR"


def test_deferred_entry_trailing_candidate_is_a_valid_position_bound_sell_rule(
    monkeypatch,
) -> None:
    install_scope(monkeypatch)
    plan = parse_compound_paper_order(
        "삼성전자 5주 시장가 매수하고 매수가 대비 3% 수익 이후 "
        "고점 대비 1% 하락하면 매도"
    )
    assert plan is not None

    preview = api._build_preview(
        preview_request(plan.conditional_instruction, build_compound_conditional_candidate(plan)),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is True
    assert preview.clarification_codes == ()
    assert preview.spec.condition.type.value == "TRAILING_STOP"
    assert preview.summary["condition_overview"]["trailing_stop"] == {
        "drawdown_ratio": "0.01",
        "drawdown_mode": "PRICE_RATIO",
        "activation_return_ratio": "0.03",
        "watermark": "HIGHEST_FRESH_QUOTE_SINCE_ACTIVE",
        "expected_position_quantity": "5",
    }


def test_deferred_entry_exit_lifetime_uses_a_pending_outer_deadline(monkeypatch) -> None:
    """The requested lifetime begins on full fill, not on route admission."""

    install_scope(monkeypatch)
    plan = parse_compound_paper_order(
        "하이닉스 5주 시장가 매수하고 매수가 대비 3% 수익 이후 "
        "고점 대비 1% 하락하면 매도, 최대 5거래일 동안 추적"
    )
    assert plan is not None

    preview = api._build_preview(
        preview_request(
            plan.conditional_instruction,
            build_compound_conditional_candidate(plan),
        ),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is True
    assert preview.spec.activation_lifetime_trading_days == 5
    assert preview.spec.expires_at == NOW + timedelta(
        days=api.PENDING_ENTRY_ACTIVATION_WINDOW_DAYS
    )
    assert "ENTRY_EXIT_LIFETIME_STARTS_AFTER_FULL_FILL" in preview.assumptions
    assert "DEFAULT_EXPIRY_KRX_REGULAR_CLOSE" not in preview.assumptions
    assert preview.summary["expiry_basis"] == "KRX_REGULAR_CLOSE_AFTER_FULL_FILL"


def test_activation_lifetime_cannot_be_smuggled_into_an_unbundled_rule(monkeypatch) -> None:
    install_scope(monkeypatch)
    candidate = profit_candidate()
    candidate.pop("expires_at")
    candidate["activation_lifetime_trading_days"] = 5
    preview = api._build_preview(
        preview_request("삼성전자 평균 매입가 대비 5% 상승시 보유수량의 20% 매도", candidate),
        subject=USER_ID,
        now=NOW,
    )
    request = api.ConditionalRuleCreateRequest(
        client_request_id="web:unbundled-lifetime-1",
        raw_instruction="삼성전자 평균 매입가 대비 5% 상승시 보유수량의 20% 매도",
        expected_spec_sha256=preview.spec_sha256,
        spec=preview.spec,
    )

    with pytest.raises(api.HTTPException) as raised:
        api._validate_create(request, subject=USER_ID)

    assert raised.value.status_code == 422
    assert raised.value.detail == "conditional_rule_activation_lifetime_requires_entry_bundle"



def test_omitted_expiry_defaults_to_krx_regular_close(monkeypatch) -> None:
    install_scope(monkeypatch)
    candidate = profit_candidate()
    candidate.pop("expires_at")

    preview = api._build_preview(
        preview_request(
            "삼성전자 평균 매입가 대비 5% 상승시 보유수량의 20% 매도",
            candidate,
        ),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.spec.expires_at == datetime(
        2026, 8, 20, 6, 30, tzinfo=timezone.utc
    )
    assert "DEFAULT_EXPIRY_KRX_REGULAR_CLOSE" in preview.assumptions


def test_omitted_expiry_after_close_uses_next_weekday_close() -> None:
    friday_after_close = datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc)

    assert api._expiry(None, now=friday_after_close) == datetime(
        2026, 8, 24, 6, 30, tzinfo=timezone.utc
    )


def test_unqualified_indicator_requires_timeframe_clarification(monkeypatch) -> None:
    install_scope(monkeypatch)
    candidate = {
        "symbol": "삼성전자",
        "condition": {
            "type": "COMPARISON",
            "operator": "GTE",
            "left": {"type": "INDICATOR", "name": "RSI", "timeframe": "1D"},
            "right": {"type": "LITERAL", "value": "70", "unit": "NUMBER"},
        },
        "action": {"side": "SELL", "sizing": {"type": "FIXED_SHARES", "value": "2"}},
        "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "1D"},
        "expires_at": (NOW + timedelta(days=30)).isoformat(),
    }

    preview = api._build_preview(
        preview_request("삼성전자 RSI 70 이상이면 2주 매도", candidate),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is False
    assert preview.clarification_codes == ("TIMEFRAME_NOT_IN_INSTRUCTION",)


def test_non_daily_timeframe_without_text_evidence_requires_clarification(monkeypatch) -> None:
    install_scope(monkeypatch)
    candidate = {
        "symbol": "삼성전자",
        "condition": {
            "type": "COMPARISON",
            "operator": "GTE",
            "left": {"type": "INDICATOR", "name": "RSI", "timeframe": "5M"},
            "right": {"type": "LITERAL", "value": "70", "unit": "NUMBER"},
        },
        "action": {"side": "SELL", "sizing": {"type": "FIXED_SHARES", "value": "2"}},
        "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "5M"},
        "expires_at": (NOW + timedelta(days=30)).isoformat(),
    }

    preview = api._build_preview(
        preview_request("삼성전자 RSI 70 이상이면 2주 매도", candidate),
        subject=USER_ID,
        now=NOW,
    )

    assert preview.activatable is False
    assert preview.clarification_codes == ("TIMEFRAME_NOT_IN_INSTRUCTION",)


def test_repository_requires_exact_fingerprint_before_activation(monkeypatch) -> None:
    install_scope(monkeypatch)
    preview = api._build_preview(
        preview_request("삼성전자 평균 매입가 대비 5% 상승시 보유수량의 20% 매도"),
        subject=USER_ID,
        now=NOW,
    )
    repository = InMemoryConditionalRuleRepository()
    record = repository.create_pending(
        spec=preview.spec,
        raw_instruction="삼성전자 평균 매입가 대비 5% 상승시 보유수량의 20% 매도",
        client_request_id="discord:conditional:1",
        parser_source="HERMES",
    )

    with pytest.raises(ConditionalRuleConflict):
        repository.activate(record.rule_id, user_id=USER_ID, confirmation_sha256="f" * 64)
    active = repository.activate(
        record.rule_id,
        user_id=USER_ID,
        confirmation_sha256=record.spec_sha256,
    )

    assert active.state is RuleState.ACTIVE
    assert active.confirmed_at is not None


def test_in_memory_repository_cannot_activate_expired_rule(monkeypatch) -> None:
    install_scope(monkeypatch)
    preview = api._build_preview(
        preview_request("삼성전자 평균 매입가 대비 5% 상승시 보유수량의 20% 매도"),
        subject=USER_ID,
        now=NOW,
    )
    expired_spec = preview.spec.model_copy(
        update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
    )
    repository = InMemoryConditionalRuleRepository()
    record = repository.create_pending(
        spec=expired_spec,
        raw_instruction="expired",
        client_request_id="discord:conditional:expired",
        parser_source="HERMES",
    )

    with pytest.raises(ConditionalRuleConflict):
        repository.activate(
            record.rule_id,
            user_id=USER_ID,
            confirmation_sha256=record.spec_sha256,
        )


def test_in_memory_oco_activation_is_all_or_nothing(monkeypatch) -> None:
    install_scope(monkeypatch)
    preview = api._build_preview(
        preview_request("삼성전자 평균 매입가 대비 5% 상승시 보유수량의 20% 매도"),
        subject=USER_ID,
        now=NOW,
    )
    activeable_spec = preview.spec.model_copy(update={"oco_group_id": uuid4()})
    expired_spec = activeable_spec.model_copy(
        update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
    )
    repository = InMemoryConditionalRuleRepository()
    first = repository.create_pending(
        spec=activeable_spec,
        raw_instruction="take profit",
        client_request_id="discord:conditional:oco:first",
        parser_source="HERMES",
    )
    second = repository.create_pending(
        spec=expired_spec,
        raw_instruction="stop loss",
        client_request_id="discord:conditional:oco:second",
        parser_source="HERMES",
    )

    with pytest.raises(ConditionalRuleConflict):
        repository.activate_group(
            (
                (first.rule_id, first.spec_sha256),
                (second.rule_id, second.spec_sha256),
            ),
            user_id=USER_ID,
        )

    assert repository.get(first.rule_id, user_id=USER_ID).state is RuleState.PENDING_CONFIRMATION
    assert repository.get(second.rule_id, user_id=USER_ID).state is RuleState.PENDING_CONFIRMATION


def test_client_request_replay_cannot_change_rule(monkeypatch) -> None:
    install_scope(monkeypatch)
    first = api._build_preview(
        preview_request("삼성전자 평균 매입가 대비 5% 상승시 보유수량의 20% 매도"),
        subject=USER_ID,
        now=NOW,
    )
    changed_candidate = profit_candidate()
    changed_candidate["action"]["sizing"]["value"] = "0.30"
    second = api._build_preview(
        preview_request("삼성전자 평균 매입가 대비 5% 상승시 보유수량의 30% 매도", changed_candidate),
        subject=USER_ID,
        now=NOW,
    )
    repository = InMemoryConditionalRuleRepository()
    repository.create_pending(
        spec=first.spec,
        raw_instruction="first",
        client_request_id="discord:conditional:2",
        parser_source="HERMES",
    )

    with pytest.raises(ConditionalRuleConflict):
        repository.create_pending(
            spec=second.spec,
            raw_instruction="second",
            client_request_id="discord:conditional:2",
            parser_source="HERMES",
        )


def test_market_closed_guard_is_visible_to_the_user(monkeypatch) -> None:
    install_scope(monkeypatch)
    preview = api._build_preview(
        preview_request("삼성전자 평균 매입가 대비 5% 상승시 보유수량의 20% 매도"),
        subject=USER_ID,
        now=NOW,
    )
    record = InMemoryConditionalRuleRepository().create_pending(
        spec=preview.spec,
        raw_instruction="test",
        client_request_id="discord:conditional:closed",
        parser_source="HERMES",
    )

    view = api._view(
        replace(
            record,
            state=RuleState.FAILED,
            last_execution_state="GUARD_REJECTED",
            last_guard_code="MARKET_CLOSED_NO_ORDER",
        )
    )

    assert view.last_guard_code == "MARKET_CLOSED_NO_ORDER"
    assert "장이 열려 있지 않아" in (view.status_message or "")
    assert "체결·원장 반영도 없습니다" in (view.status_message or "")


@pytest.mark.parametrize(
    ("raw", "expected_seconds"),
    (
        ("3분 뒤 금호전기 5주 시장가 매수해줘", 180),
        ("3분 기다렸다가 금호전기 5주 시장가 매수해줘", 180),
        ("3분 후에 금호전기 5주 시장가 매수해줘", 180),
        ("3분 후 금호전기 5주 시장가 매수해줘", 180),
        ("10초 있다가 금호전기 5주 시장가 매수해줘", 10),
        ("1시간 지나서 금호전기 5주 시장가 매수해줘", 3600),
        ("3분 기다린 뒤 금호전기 5주 시장가 매수해줘", 180),
        ("3분 지난 후에 금호전기 5주 시장가 매수해줘", 180),
    ),
)
def test_natural_korean_delay_wordings_all_reach_the_delayed_lane(
    raw: str, expected_seconds: int
) -> None:
    """Only "뒤" was recognized, so "3분 기다렸다가" fell to the immediate lane.

    There the unparsed phrase corrupted the instrument span and the user was
    told the *instrument* was missing or conflicting (2026-08-28).
    """

    from orchestration.user_order_language import deterministic_delayed_order_plan

    plan = deterministic_delayed_order_plan(raw)
    assert plan is not None, raw
    assert plan.delay_seconds == expected_seconds


@pytest.mark.parametrize(
    "raw",
    (
        "3분봉 60일선 돌파 후 금호전기 5주 시장가 매수해줘",
        "금호전기 5주 시장가 매수해줘",
        "3시간 후반 금호전기 5주 시장가 매수해줘",
    ),
)
def test_delay_grammar_does_not_swallow_non_delay_phrases(raw: str) -> None:
    from orchestration.user_order_language import deterministic_delayed_order_plan

    assert deterministic_delayed_order_plan(raw) is None, raw


def test_unparsed_duration_is_not_reported_as_an_instrument_problem() -> None:
    """"3분 기다렸다가" was answered with MISSING_OR_CONFLICTING_INSTRUMENT.

    The instrument was in the sentence and resolvable; the delay wording was
    what the parser could not read (2026-08-28).  Blaming the instrument sent
    the user to fix the one part that was already correct.
    """

    import hashlib

    from orchestration.user_order_language import verify_order_candidate

    raw = "3분 뜸들이다가 금호전기 5주 시장가 매수해줘"
    mention = "금호전기"
    start = raw.index(mention)
    candidate = {
        "schema_version": "user-paper-order-interpretation.v1",
        "mode": "PAPER",
        "binding": False,
        "raw_text_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "decision": "EXECUTE",
        "action": "PLACE_ORDER",
        "instrument_mention": mention,
        "side": "BUY",
        "quantity": "5",
        "order_type": "MARKET",
        "limit_price": None,
        "evidence": [
            {
                "field": "INSTRUMENT",
                "text": mention,
                "start": start,
                "end": start + len(mention),
            },
            {"field": "SIDE", "text": "매수", "start": raw.index("매수"), "end": raw.index("매수") + 2},
            {"field": "QUANTITY", "text": "5주", "start": raw.index("5주"), "end": raw.index("5주") + 2},
            {
                "field": "ORDER_TYPE",
                "text": "시장가",
                "start": raw.index("시장가"),
                "end": raw.index("시장가") + 3,
            },
        ],
        "reason_codes": [],
    }
    result = verify_order_candidate(raw, candidate)
    codes = {code.value for code in result.reason_codes}
    assert "UNSUPPORTED_DELAY_EXPRESSION" in codes
    assert "MISSING_OR_CONFLICTING_INSTRUMENT" not in codes


@pytest.mark.parametrize(
    "code",
    (
        "UNSUPPORTED_DELAY_EXPRESSION",
        "MISSING_OR_CONFLICTING_INSTRUMENT",
        "MISSING_OR_CONFLICTING_QUANTITY",
        "paper_order_instrument_clarification_required",
        "trading_market_no_ask",
        "trading_market_no_bid",
        "trading_market_quote_stale",
    ),
)
def test_known_rejections_reach_the_user_as_korean_sentences(code: str) -> None:
    """Every one of these used to surface as the bare enum name."""

    from apps.api.user_order_orchestrator import _non_execution_user_message

    message = _non_execution_user_message([code])
    assert message, code
    assert code not in message
