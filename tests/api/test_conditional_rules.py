from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from apps.api import conditional_rules as api
from apps.api.conditional_rule_language import looks_like_conditional_paper_rule
from apps.api.conditional_rule_workflow import (
    ConditionalRuleConflict,
    InMemoryConditionalRuleRepository,
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


def test_three_minute_request_uses_supported_five_minute_feed_and_discloses_fallback(monkeypatch) -> None:
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
    assert preview.spec.evaluation.primary_timeframe.value == "5M"
    assert preview.spec.condition.right.timeframe.value == "5M"
    assert "TIMEFRAME_FALLBACK_3M_TO_5M" in preview.assumptions
    assert preview.summary["timeframe_fallback"] == {
        "requested": "3M",
        "used": "5M",
        "reason": "3M_UNSUPPORTED",
    }


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


def test_unqualified_daily_indicator_is_visible_confirmation_assumption(monkeypatch) -> None:
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

    assert preview.activatable is True
    assert "DEFAULTED_TO_DAILY_COMPLETED_BAR" in preview.assumptions


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
