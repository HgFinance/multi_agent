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
        "삼성전자 주가가 5일 이동평균선보다 높으면 1주 매수",
        "네이버 주가가 200000원 아래로 떨어지면 1주 매도",
        "삼성전자 볼린저밴드 상단에 닿으면 1주 매도",
    ),
)
def test_explicit_korean_condition_endings_route_to_conditional_lane(raw: str) -> None:
    assert looks_like_conditional_paper_rule(raw) is True


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



def test_omitted_expiry_defaults_to_ten_minutes(monkeypatch) -> None:
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

    assert preview.spec.expires_at == NOW + timedelta(minutes=10)


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
