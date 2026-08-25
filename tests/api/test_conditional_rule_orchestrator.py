from __future__ import annotations

from typing import Any

import pytest

from apps.api import ceo, conditional_rules, user_order_orchestrator
from apps.api import conditional_rule_orchestrator as orchestrator
from apps.api.conditional_rule_workflow import InMemoryConditionalRuleRepository
from apps.api.user_order_workflow import InMemoryUserOrderRequestRepository
from orchestration.conditional_rules import RuleState

USER_ID = "11111111-1111-4111-8111-111111111111"
FUND_ID = "22222222-2222-4222-8222-222222222222"
BOOK_ID = "33333333-3333-4333-8333-333333333333"
INSTRUMENT_ID = "44444444-4444-4444-8444-444444444444"


def _candidate() -> conditional_rules.ConditionalRuleCandidate:
    return conditional_rules.ConditionalRuleCandidate.model_validate(
        {
            "symbol": "삼성전자",
            "condition": {
                "type": "CROSS",
                "operator": "ABOVE",
                "left": {
                    "type": "INDICATOR",
                    "name": "RSI",
                    "timeframe": "5M",
                },
                "right": {"type": "LITERAL", "value": "30", "unit": "NUMBER"},
            },
            "action": {
                "side": "BUY",
                "sizing": {"type": "FIXED_SHARES", "value": "1"},
            },
            "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "5M"},
        }
    )


def _price_candidate(
    *, threshold: str, quantity: str, operator: str
) -> conditional_rules.ConditionalRuleCandidate:
    return conditional_rules.ConditionalRuleCandidate.model_validate(
        {
            "symbol": "하이닉스",
            "condition": {
                "type": "COMPARISON",
                "operator": operator,
                "left": {"type": "MARKET", "field": "LAST_PRICE"},
                "right": {
                    "type": "LITERAL",
                    "value": threshold,
                    "unit": "PRICE",
                },
            },
            "action": {
                "side": "BUY",
                "sizing": {"type": "FIXED_SHARES", "value": quantity},
            },
            "evaluation": {"clock": "QUOTE"},
        }
    )


def _install_workflow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw_instruction: str = "삼성전자 5분봉 RSI가 30을 상향 돌파하면 1주 매수",
) -> tuple[
    InMemoryUserOrderRequestRepository,
    InMemoryConditionalRuleRepository,
    dict[str, dict[str, Any]],
]:
    orders = InMemoryUserOrderRequestRepository()
    rules = InMemoryConditionalRuleRepository()
    tasks: dict[str, dict[str, Any]] = {}

    monkeypatch.setattr(ceo, "fetch_current_mandate_by_fund", lambda _fund: None)
    monkeypatch.setattr(ceo, "user_order_repository", lambda: orders)
    monkeypatch.setattr(
        ceo,
        "require_trading_book_access",
        lambda owner, fund, book: {
            "user_id": owner,
            "fund_id": fund,
            "book_id": book,
        },
    )

    def create_task(**kwargs: Any) -> dict[str, str]:
        task_id = "t_root1" if not tasks else "t_trade1"
        status = "running"
        tasks[task_id] = {
            **kwargs,
            "id": task_id,
            "task_id": task_id,
            "status": status,
            "parents": [],
        }
        return {"task_id": task_id, "status": status}

    monkeypatch.setattr(ceo.hermes_boundary, "create_kanban_task", create_task)
    monkeypatch.setattr(
        ceo.hermes_boundary, "comment_root_scope", lambda **_kwargs: True
    )

    def complete_root(*, task_id: str, result: str) -> bool:
        assert result
        tasks[task_id]["status"] = "done"
        return True

    monkeypatch.setattr(ceo.hermes_boundary, "complete_kanban_task", complete_root)
    monkeypatch.setattr(
        ceo.hermes_boundary, "unblock_kanban_task", lambda **_kwargs: True
    )

    response = ceo.ceo_query(
        ceo.CeoAsk(
            query=raw_instruction,
            request_id="request-conditional-bridge",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )
    assert response["conditional_rule"] is True

    monkeypatch.setattr(orchestrator, "user_order_repository", lambda: orders)
    monkeypatch.setattr(orchestrator, "conditional_rule_repository", lambda: rules)
    monkeypatch.setattr(
        user_order_orchestrator.hermes_boundary,
        "show_kanban_task",
        lambda task_id, **_kwargs: tasks.get(task_id),
    )
    monkeypatch.setattr(
        conditional_rules,
        "require_trading_book_access",
        lambda subject, fund, book: {
            "user_id": subject,
            "fund_id": fund,
            "book_id": book,
        },
    )
    monkeypatch.setattr(
        conditional_rules,
        "resolve_active_trading_instrument",
        lambda symbol, instrument_id: {
            "instrument_id": INSTRUMENT_ID,
            "symbol": "005930",
        },
    )
    return orders, rules, tasks


def test_valid_hermes_ast_is_immediately_active_and_replay_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orders, rules, tasks = _install_workflow(monkeypatch)

    first = orchestrator.process_user_conditional_paper_rule(
        root_task_id="t_root1",
        trading_task_id="t_trade1",
        candidate=_candidate(),
    )

    assert first["binding"] is True
    assert first["mode"] == "PAPER"
    assert first["rule_active"] is True
    assert first["state"] == "ACTIVE"
    assert first["summary"]["symbol"] == "005930"
    assert first["summary"]["repeat_policy"] == "ONCE"
    assert len(rules.list_for_user(USER_ID)) == 1
    record = next(iter(orders._records.values()))
    assert record.state == "COMPLETED"
    assert record.canonical_payload is not None
    assert record.canonical_payload["kind"] == "CONDITIONAL_PAPER_RULE"

    tasks["t_root1"]["status"] = "done"
    tasks["t_trade1"]["status"] = "done"
    replay = orchestrator.process_user_conditional_paper_rule(
        root_task_id="t_root1",
        trading_task_id="t_trade1",
        candidate=_candidate(),
    )

    assert replay["rule_id"] == first["rule_id"]
    assert replay["spec_sha256"] == first["spec_sha256"]
    assert len(rules.list_for_user(USER_ID)) == 1


def test_missing_ast_requires_clarification_without_creating_a_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orders, rules, _tasks = _install_workflow(monkeypatch)

    result = orchestrator.process_user_conditional_paper_rule(
        root_task_id="t_root1",
        trading_task_id="t_trade1",
        candidate=None,
        clarification_reason="QUANTITY_REQUIRED",
    )

    assert result["binding"] is False
    assert result["rule_active"] is False
    assert result["reason_codes"] == ["QUANTITY_REQUIRED"]
    assert rules.list_for_user(USER_ID) == []
    record = next(iter(orders._records.values()))
    assert record.state == "CLARIFICATION_REQUIRED"


def test_multiple_price_actions_activate_as_independent_one_shot_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "하이닉스 1주에 1,603,000이 넘으면 2개 매수해주고, 1,601,000아래로 떨어지면 1개 매수해줘."
    orders, rules, _tasks = _install_workflow(
        monkeypatch, raw_instruction=raw
    )
    record = next(iter(orders._records.values()))

    result = orchestrator.process_user_conditional_paper_rule(
        root_task_id="t_root1",
        trading_task_id="t_trade1",
        candidates=(
            _price_candidate(threshold="1603000", quantity="2", operator="GT"),
            _price_candidate(threshold="1601000", quantity="1", operator="LT"),
        ),
    )

    assert result["binding"] is True
    assert result["rule_active"] is True
    assert len(result["rule_ids"]) == 2
    stored = rules.list_for_user(USER_ID)
    assert len(stored) == 2
    assert {item.state for item in stored} == {RuleState.ACTIVE}
    assert {str(item.spec.action.sizing.value) for item in stored} == {"1", "2"}
    outcome = orders.get(record.order_request_id)
    assert outcome is not None
    assert outcome.canonical_payload is not None
    assert outcome.canonical_payload["kind"] == "CONDITIONAL_PAPER_RULE_SET"
    assert outcome.canonical_payload["rule_ids"] == result["rule_ids"]

    status = orchestrator.get_user_conditional_paper_rule_status(
        root_task_id="t_root1", trading_task_id="t_trade1"
    )
    assert status["schema_version"] == "conditional-paper-execution-status-set.v1"
    assert status["authority_verified"] is True
    assert len(status["rules"]) == 2
    assert {item["workflow_state"] for item in status["rules"]} == {
        "WAITING_FOR_TRIGGER"
    }


def test_rule_repository_never_leaves_pending_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _orders, rules, _tasks = _install_workflow(monkeypatch)

    result = orchestrator.process_user_conditional_paper_rule(
        root_task_id="t_root1",
        trading_task_id="t_trade1",
        candidate=_candidate(),
    )

    stored = rules.get(result["rule_id"], user_id=USER_ID)
    assert stored is not None
    assert stored.state is RuleState.ACTIVE
    assert stored.confirmed_at is not None


def test_status_reader_reuses_linked_rule_and_authoritative_directive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _orders, rules, tasks = _install_workflow(monkeypatch)
    activated = orchestrator.process_user_conditional_paper_rule(
        root_task_id="t_root1",
        trading_task_id="t_trade1",
        candidate=_candidate(),
    )
    stored = rules.get(activated["rule_id"], user_id=USER_ID)
    assert stored is not None
    rules._records[stored.rule_id] = stored.__class__(
        **{**stored.__dict__, "directive_id": "55555555-5555-4555-8555-555555555555"}
    )
    tasks["t_root1"]["status"] = "done"
    tasks["t_trade1"]["status"] = "done"

    monkeypatch.setattr(
        orchestrator,
        "read_paper_directive_status_for_admitted_authority",
        lambda **_kwargs: {
            "directive_id": "55555555-5555-4555-8555-555555555555",
            "state": "COMPLETED",
            "mode": "PAPER",
            "error_code": "TRADING_FILL_ACCOUNTING_PENDING",
            "legs": [
                {
                    "symbol": "005930",
                    "side": "BUY",
                    "order_type": "MARKET",
                    "requested_quantity": "1",
                    "filled_quantity": "1",
                    "average_fill_price": "248250",
                    "broker_order_id": "ls-paper:12695",
                }
            ],
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_workflow_state_from_directive",
        lambda _directive: "ACCOUNTING_PENDING",
    )

    status = orchestrator.get_user_conditional_paper_rule_status(
        root_task_id="t_root1",
        trading_task_id="t_trade1",
    )

    assert status["authority_verified"] is True
    assert status["workflow_state"] == "ACCOUNTING_PENDING"
    assert status["filled_quantity"] == "1"
    assert status["average_fill_price"] == "248250"
    assert "체결 수량 : 1주" in status["final_answer"]
    assert "평균 체결가 : 248,250원" in status["final_answer"]
