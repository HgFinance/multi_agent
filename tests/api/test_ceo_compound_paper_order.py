from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from apps.api import (
    ceo,
    conditional_rule_workflow,
    conditional_rules,
    paper_order_bundle,
)
from apps.api.user_order_workflow import InMemoryUserOrderRequestRepository

USER_ID = "11111111-1111-4111-8111-111111111111"
FUND_ID = "22222222-2222-4222-8222-222222222222"
BOOK_ID = "33333333-3333-4333-8333-333333333333"


class _BundleRepo:
    def __init__(self) -> None:
        self.bundle = None

    def create(self, **kwargs: Any):
        self.bundle = SimpleNamespace(
            bundle_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            conditional_rule_id=None,
            state="RECEIVED",
        )
        return self.bundle

    def bind_conditional_rule(self, bundle_id: str, rule_id: str):
        assert bundle_id == self.bundle.bundle_id
        self.bundle.conditional_rule_id = rule_id
        self.bundle.state = "WAITING_FOR_IMMEDIATE_FILL"
        return self.bundle


class _RuleRepo:
    def create_pending(self, **kwargs: Any):
        assert kwargs["parser_source"] == "DETERMINISTIC"
        return SimpleNamespace(rule_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def test_compound_route_composes_existing_order_and_rule_authorities(monkeypatch) -> None:
    repository = InMemoryUserOrderRequestRepository()
    bundle_repo = _BundleRepo()
    rule_repo = _RuleRepo()
    immediate_route = {}

    monkeypatch.setenv("USER_PAPER_ORDER_WORKFLOW_ENABLED", "true")
    monkeypatch.setattr(ceo, "user_order_repository", lambda: repository)
    monkeypatch.setattr(
        ceo,
        "require_trading_book_access",
        lambda *_args: {"user_id": USER_ID, "fund_id": FUND_ID, "book_id": BOOK_ID},
    )
    monkeypatch.setattr(
        paper_order_bundle,
        "paper_order_bundle_repository",
        lambda: bundle_repo,
    )
    monkeypatch.setattr(
        conditional_rule_workflow,
        "conditional_rule_repository",
        lambda: rule_repo,
    )
    monkeypatch.setattr(
        conditional_rules,
        "_build_preview",
        lambda request, subject: SimpleNamespace(activatable=True, spec=object()),
    )

    def route(req, **kwargs):
        immediate_route["query"] = req.query
        immediate_route["request_id"] = req.request_id
        immediate_route["pre_admitted_record"] = kwargs["pre_admitted_record"]
        for key in (
            "discord_channel_id",
            "discord_message_id",
            "discord_guild_id",
            "discord_thread_id",
        ):
            immediate_route[key] = kwargs[key]
        return {
            "task_id": "t_buy1",
            "task": {},
            "trading_task_id": "t_trade1",
        }

    monkeypatch.setattr(ceo, "_route_user_paper_order", route)

    result = ceo.ceo_query(
        ceo.CeoAsk(
            query="삼성전자 5주 시장가로 매수해줘. 그리고 265000 넘으면 즉시 5개 매도해줘.",
            request_id="discord:compound-1",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
            source="web",
        ),
        owner_id=USER_ID,
        discord_channel_id="channel-1",
        discord_message_id="message-1",
        discord_guild_id="guild-1",
        discord_thread_id="thread-1",
    )

    assert immediate_route["query"] == "삼성전자 5주 시장가로 매수해줘"
    assert immediate_route["request_id"] == "discord:compound-1:buy"
    assert immediate_route["pre_admitted_record"].raw_instruction == immediate_route["query"]
    assert immediate_route["discord_channel_id"] == "channel-1"
    assert immediate_route["discord_message_id"] == "message-1"
    assert immediate_route["discord_guild_id"] == "guild-1"
    assert immediate_route["discord_thread_id"] == "thread-1"
    assert result["compound_paper_order"] is True
    assert result["entry_exit_bracket"] is False
    assert result["order_state"] == "WAITING_FOR_IMMEDIATE_FILL"
    assert result["conditional_rule_id"] == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def test_compound_route_defers_one_entry_exit_bracket_rule_until_full_buy_fill(
    monkeypatch,
) -> None:
    repository = InMemoryUserOrderRequestRepository()
    bundle_repo = _BundleRepo()
    rule_repo = _RuleRepo()
    captured = {}

    monkeypatch.setenv("USER_PAPER_ORDER_WORKFLOW_ENABLED", "true")
    monkeypatch.setattr(ceo, "user_order_repository", lambda: repository)
    monkeypatch.setattr(
        ceo,
        "require_trading_book_access",
        lambda *_args: {"user_id": USER_ID, "fund_id": FUND_ID, "book_id": BOOK_ID},
    )
    monkeypatch.setattr(
        paper_order_bundle,
        "paper_order_bundle_repository",
        lambda: bundle_repo,
    )
    monkeypatch.setattr(
        conditional_rule_workflow,
        "conditional_rule_repository",
        lambda: rule_repo,
    )

    def preview(request, subject):
        captured["candidate"] = request.candidate
        return SimpleNamespace(activatable=True, spec=object())

    monkeypatch.setattr(conditional_rules, "_build_preview", preview)
    monkeypatch.setattr(
        ceo,
        "_route_user_paper_order",
        lambda *_args, **_kwargs: {"task_id": "t_buy", "task": {}},
    )

    result = ceo.ceo_query(
        ceo.CeoAsk(
            query=(
                "삼성전자 5주 시장가 매수하고 매수가 대비 3% 상승하면 매도하고 "
                "2% 하락하면 매도해줘"
            ),
            request_id="discord:compound-bracket-1",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
            source="discord",
        ),
        owner_id=USER_ID,
    )

    condition = captured["candidate"].condition
    assert condition.type.value == "LOGICAL"
    assert condition.operator == "AND"
    assert (condition.children or ())[1].operator == "OR"
    assert result["entry_exit_bracket"] is True
    assert "전량 체결된 뒤" in result["answer"]
    assert "하나의 OR 청산 규칙" in result["answer"]


def test_compound_route_describes_fill_gated_entry_trailing_stop(monkeypatch) -> None:
    repository = InMemoryUserOrderRequestRepository()
    bundle_repo = _BundleRepo()
    rule_repo = _RuleRepo()

    monkeypatch.setenv("USER_PAPER_ORDER_WORKFLOW_ENABLED", "true")
    monkeypatch.setattr(ceo, "user_order_repository", lambda: repository)
    monkeypatch.setattr(
        ceo,
        "require_trading_book_access",
        lambda *_args: {"user_id": USER_ID, "fund_id": FUND_ID, "book_id": BOOK_ID},
    )
    monkeypatch.setattr(
        paper_order_bundle,
        "paper_order_bundle_repository",
        lambda: bundle_repo,
    )
    monkeypatch.setattr(
        conditional_rule_workflow,
        "conditional_rule_repository",
        lambda: rule_repo,
    )
    monkeypatch.setattr(
        conditional_rules,
        "_build_preview",
        lambda *_args, **_kwargs: SimpleNamespace(activatable=True, spec=object()),
    )
    monkeypatch.setattr(
        ceo,
        "_route_user_paper_order",
        lambda *_args, **_kwargs: {"task_id": "t_buy", "task": {}},
    )

    result = ceo.ceo_query(
        ceo.CeoAsk(
            query=(
                "삼성전자 5주 시장가 매수하고 매수가 대비 3% 수익 이후 "
                "고점 대비 1% 하락하면 매도"
            ),
            request_id="discord:compound-trailing-1",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
            source="discord",
        ),
        owner_id=USER_ID,
    )

    assert result["entry_exit_bracket"] is False
    assert result["entry_trailing_stop"] is True
    assert "고점 대비 지정한 비율" in result["answer"]
    assert "보유수량이 이번 매수 수량과 다르면" in result["answer"]


def test_compound_route_reports_explicit_post_fill_session_lifetime(monkeypatch) -> None:
    repository = InMemoryUserOrderRequestRepository()
    bundle_repo = _BundleRepo()
    rule_repo = _RuleRepo()
    captured = {}

    monkeypatch.setenv("USER_PAPER_ORDER_WORKFLOW_ENABLED", "true")
    monkeypatch.setattr(ceo, "user_order_repository", lambda: repository)
    monkeypatch.setattr(
        ceo,
        "require_trading_book_access",
        lambda *_args: {"user_id": USER_ID, "fund_id": FUND_ID, "book_id": BOOK_ID},
    )
    monkeypatch.setattr(
        paper_order_bundle,
        "paper_order_bundle_repository",
        lambda: bundle_repo,
    )
    monkeypatch.setattr(
        conditional_rule_workflow,
        "conditional_rule_repository",
        lambda: rule_repo,
    )

    def preview(request, subject):
        captured["candidate"] = request.candidate
        return SimpleNamespace(activatable=True, spec=object())

    monkeypatch.setattr(conditional_rules, "_build_preview", preview)
    monkeypatch.setattr(
        ceo,
        "_route_user_paper_order",
        lambda *_args, **_kwargs: {"task_id": "t_buy", "task": {}},
    )

    result = ceo.ceo_query(
        ceo.CeoAsk(
            query=(
                "하이닉스 5주 시장가 매수하고 매수가 대비 3% 수익 이후 "
                "고점 대비 1% 하락하면 매도, 최대 5거래일 동안 추적"
            ),
            request_id="discord:compound-lifetime-1",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
            source="discord",
        ),
        owner_id=USER_ID,
    )

    assert captured["candidate"].activation_lifetime_trading_days == 5
    assert "전량 체결 시점부터" in result["answer"]
    assert "5거래일째 마감" in result["answer"]


def test_traced_paper_route_receives_mirror_coordinates(monkeypatch) -> None:
    captured = {}

    def route(req, **kwargs):
        captured.update(kwargs)
        return {"task_id": "t_order", "status": "accepted"}

    monkeypatch.setattr(ceo, "_route_traced_user_paper_order", route)

    ceo.ceo_query(
        ceo.CeoAsk(
            query="삼성전자 2주 시장가로 매수해줘",
            request_id="web-order-1",
            source="web",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
        discord_channel_id="channel-1",
        discord_message_id="message-1",
        discord_guild_id="guild-1",
        discord_thread_id="thread-1",
    )

    assert captured["discord_channel_id"] == "channel-1"
    assert captured["discord_message_id"] == "message-1"
    assert captured["discord_guild_id"] == "guild-1"
    assert captured["discord_thread_id"] == "thread-1"
