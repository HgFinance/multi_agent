from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from apps.api import ceo
from apps.api import conditional_rule_workflow
from apps.api import conditional_rules
from apps.api import paper_order_bundle
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
        ),
        owner_id=USER_ID,
    )

    assert immediate_route["query"] == "삼성전자 5주 시장가로 매수해줘"
    assert immediate_route["request_id"] == "discord:compound-1:buy"
    assert immediate_route["pre_admitted_record"].raw_instruction == immediate_route["query"]
    assert result["compound_paper_order"] is True
    assert result["order_state"] == "WAITING_FOR_IMMEDIATE_FILL"
    assert result["conditional_rule_id"] == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
