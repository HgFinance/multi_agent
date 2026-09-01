from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from apps.api import ceo
from apps.api.user_order_workflow import InMemoryUserOrderRequestRepository
from orchestration import llm_observability
from orchestration.ceo_workflow_scope import (
    requested_by_from_body,
    user_paper_order_scope_from_body,
    workflow_role_from_body,
    workflow_root_from_body,
)

USER_ID = "11111111-1111-4111-8111-111111111111"
FUND_ID = "22222222-2222-4222-8222-222222222222"
BOOK_ID = "33333333-3333-4333-8333-333333333333"


def test_hr_read_only_e2e_marker_bypasses_order_high_recall_router() -> None:
    raw = (
        "hr-department E2E 통합 검증: 실제 주문·투자·원장 변경 금지, "
        "Discord·LangSmith·Notion 로그만 확인"
    )

    assert ceo._is_read_only_hr_e2e_request(raw) is True
    assert ceo._is_read_only_hr_e2e_request("삼성전자 매수 10주 시장가") is False


def test_pure_negated_order_finishes_without_question_or_kanban(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_task = Mock()
    monkeypatch.setattr(ceo, "fetch_current_mandate_by_fund", lambda _fund: None)
    monkeypatch.setattr(ceo.hermes_boundary, "create_kanban_task", create_task)

    response = ceo.ceo_query(
        ceo.CeoAsk(query="7만 원 안 넘으면 사지 마", request_id="no-action-1")
    )

    create_task.assert_not_called()
    assert response["task_id"] == ""
    assert response["task"]["status"] == "completed"
    assert "만들지 않았습니다" in response["answer"]
    assert response["planning"]["selected_departments"] == []


def test_risk_read_only_e2e_marker_bypasses_legal_example_order_router() -> None:
    raw = (
        "[RISK-E2E] Risk 부서만 수행하세요. PAPER 읽기 전용 분석입니다. "
        "임직원이 취득 후 6개월 이내 매도한 가상 법률 사례를 검토하되, "
        "실제 주문·매매·승인·원장 변경은 절대 수행하지 마세요."
    )

    assert ceo._is_read_only_risk_e2e_request(raw) is True
    assert ceo._is_read_only_risk_e2e_request("삼성전자 매도 10주 시장가") is False


class _OrderedRepository(InMemoryUserOrderRequestRepository):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def admit(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.events.append("admit")
        return super().admit(**kwargs)

    def bind_root(self, order_request_id: str, root_task_id: str):  # type: ignore[no-untyped-def]
        self.events.append("bind-root")
        return super().bind_root(order_request_id, root_task_id)

    def bind_trading_task(  # type: ignore[no-untyped-def]
        self, order_request_id: str, trading_task_id: str
    ):
        self.events.append("bind-trading")
        return super().bind_trading_task(order_request_id, trading_task_id)


def _install_successful_route(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[str],
    repository: InMemoryUserOrderRequestRepository,
) -> Mock:
    monkeypatch.setattr(ceo, "fetch_current_mandate_by_fund", lambda _fund: None)
    monkeypatch.setattr(ceo, "user_order_repository", lambda: repository)

    def require_access(owner_id: str, fund_id: str, book_id: str) -> dict[str, str]:
        events.append("authorize")
        assert (owner_id, fund_id, book_id) == (USER_ID, FUND_ID, BOOK_ID)
        return {"user_id": USER_ID, "fund_id": FUND_ID, "book_id": BOOK_ID}

    monkeypatch.setattr(ceo, "require_trading_book_access", require_access)
    create = Mock()

    def create_task(**kwargs: Any) -> dict[str, str]:
        index = create.call_count
        create(**kwargs)
        if index == 0:
            events.append("create-root-blocked")
            return {"task_id": "t_root1", "status": "blocked"}
        events.append("create-trading-blocked")
        return {"task_id": "t_trade1", "status": "blocked"}

    monkeypatch.setattr(ceo.hermes_boundary, "create_kanban_task", create_task)

    def comment_scope(**_kwargs: str) -> bool:
        events.append("comment-root-scope")
        return True

    monkeypatch.setattr(ceo.hermes_boundary, "comment_root_scope", comment_scope)

    def complete(*, task_id: str, result: str) -> bool:
        assert result
        events.append(f"complete-{task_id}")
        return True

    def unblock(*, task_id: str) -> bool:
        events.append(f"release-{task_id}")
        return True

    monkeypatch.setattr(ceo.hermes_boundary, "complete_kanban_task", complete)
    monkeypatch.setattr(ceo.hermes_boundary, "unblock_kanban_task", unblock)
    return create


def test_exact_sample_is_durably_bound_before_either_card_is_finalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USER_PAPER_ORDER_DETERMINISTIC_FAST_PATH_ENABLED", "true")
    events: list[str] = []
    repository = _OrderedRepository(events)
    create = _install_successful_route(
        monkeypatch, events=events, repository=repository
    )

    response = ceo.ceo_query(
        ceo.CeoAsk(
            query="삼성전자 매수 10주 시장가",
            request_id="request-100",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )

    assert events == [
        "authorize",
        "admit",
        "create-root-blocked",
        "comment-root-scope",
        "bind-root",
        "create-trading-blocked",
        "bind-trading",
        "complete-t_root1",
        "complete-t_trade1",
    ]
    assert create.call_count == 2
    root_call, trading_call = create.call_args_list
    assert root_call.kwargs["initial_status"] == "blocked"
    assert trading_call.kwargs["initial_status"] == "blocked"

    root_body = root_call.kwargs["body"]
    trading_body = trading_call.kwargs["body"]
    root_scope = user_paper_order_scope_from_body(root_body)
    assert root_scope is not None
    assert user_paper_order_scope_from_body(trading_body) == root_scope
    assert requested_by_from_body(root_body) == USER_ID
    assert workflow_root_from_body(trading_body) == "t_root1"
    assert workflow_role_from_body(trading_body) == "primary"
    assert "삼성전자 매수 10주 시장가" in root_body
    assert "삼성전자 매수 10주 시장가" in trading_body
    assert "selected_primary_profiles=trading-department" in root_body
    assert "interpreter=DETERMINISTIC_EXACT_EVIDENCE" in trading_body
    assert "authority=server_verified_paper_only" in trading_body
    assert "instrument resolution, idempotency, OMS state" in trading_body
    assert "process_user_paper_order exactly once" not in trading_body

    stored = repository.get(response["order_request_id"])
    assert stored is not None
    assert stored.ceo_root_task_id == "t_root1"
    assert stored.trading_task_id == "t_trade1"
    assert stored.mode == "PAPER"
    assert response["order_mode"] == "PAPER"
    assert response["binding"] is False
    assert response["task"]["status"] == "done"



def test_account_sell_all_uses_the_deterministic_paper_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    repository = _OrderedRepository(events)
    create = _install_successful_route(
        monkeypatch, events=events, repository=repository
    )
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("USER_PAPER_ORDER_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv(
        "USER_PAPER_ORDER_DETERMINISTIC_FAST_PATH_ENABLED", "true"
    )

    execution = {
        "decision": "EXECUTE",
        "mode": "PAPER",
        "binding": True,
        "order_submitted": True,
        "order_request_id": "sell-all-by-mock",
        "request_state": "IN_PROGRESS",
        "user_message": "PAPER 전량 매도 주문을 제출했고 보유 종목별 체결을 추적 중입니다.",
    }

    def process(**kwargs: Any) -> dict[str, Any]:
        events.append("deterministic-process")
        interpretation = kwargs["interpretation"]
        assert interpretation["action"] == "SELL_ALL"
        assert interpretation["instrument_mention"] is None
        assert interpretation["side"] is None
        assert interpretation["quantity"] is None
        assert interpretation["order_type"] is None
        return execution

    monkeypatch.setattr(ceo, "process_deterministic_user_paper_order", process)
    response = ceo.ceo_query(
        ceo.CeoAsk(
            query="보유종목 전량 매도해줘",
            request_id="request-sell-all-fast-100",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )

    assert create.call_count == 2
    _, trading_call = create.call_args_list
    assert "interpreter=DETERMINISTIC_EXACT_EVIDENCE" in trading_call.kwargs["body"]
    assert events == [
        "authorize",
        "admit",
        "create-root-blocked",
        "comment-root-scope",
        "bind-root",
        "create-trading-blocked",
        "bind-trading",
        "complete-t_root1",
        "deterministic-process",
        "complete-t_trade1",
    ]
    assert response["order_state"] == "IN_PROGRESS"
    assert response["answer"] == execution["user_message"]


@pytest.mark.parametrize(
    ("raw", "instrument", "quantity"),
    [
        ("<@1536991290842030130> 삼성전자 3주 매수", "삼성전자", "3"),
        (
            "124500 아이티센글로벌 시장가로 30주 매수해줘",
            "124500 아이티센글로벌",
            "30",
        ),
    ],
)
def test_unambiguous_production_order_uses_deterministic_fast_path(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    instrument: str,
    quantity: str,
) -> None:
    events: list[str] = []
    repository = _OrderedRepository(events)
    create = _install_successful_route(
        monkeypatch, events=events, repository=repository
    )
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("USER_PAPER_ORDER_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv(
        "USER_PAPER_ORDER_DETERMINISTIC_FAST_PATH_ENABLED", "true"
    )

    execution = {
        "decision": "EXECUTE",
        "mode": "PAPER",
        "binding": True,
        "order_submitted": True,
        "order_request_id": "filled-by-mock",
        "request_state": "COMPLETED",
        "user_message": (
            "PAPER 주문 완료: 005930 매수 시장가(가격 미지정) "
            "요청 3주/체결 3주, 평균 체결가 271,000원 (FILLED, LS 주문번호 17566)."
        ),
    }

    def process(**kwargs: Any) -> dict[str, Any]:
        events.append("deterministic-process")
        assert kwargs["root_task_id"] == "t_root1"
        assert kwargs["trading_task_id"] == "t_trade1"
        interpretation = kwargs["interpretation"]
        assert interpretation["instrument_mention"] == instrument
        assert interpretation["side"] == "BUY"
        assert interpretation["quantity"] == quantity
        return execution

    monkeypatch.setattr(ceo, "process_deterministic_user_paper_order", process)
    response = ceo.ceo_query(
        ceo.CeoAsk(
            query=raw,
            request_id="request-fast-100",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )

    assert create.call_count == 2
    _, trading_call = create.call_args_list
    assert trading_call.kwargs["initial_status"] == "blocked"
    assert "user-paper-order-deterministic.v1" in trading_call.kwargs["body"]
    assert "mcp_tool=process_user_paper_order" not in trading_call.kwargs["body"]
    assert events == [
        "authorize",
        "admit",
        "create-root-blocked",
        "comment-root-scope",
        "bind-root",
        "create-trading-blocked",
        "bind-trading",
        "complete-t_root1",
        "deterministic-process",
        "complete-t_trade1",
    ]
    assert response["order_state"] == "COMPLETED"
    assert response["answer"] == execution["user_message"]
    assert response["execution"] == execution
    assert response["task"]["status"] == "done"


def test_same_notional_basket_uses_the_discord_deterministic_paper_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    repository = _OrderedRepository(events)
    _install_successful_route(monkeypatch, events=events, repository=repository)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("USER_PAPER_ORDER_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv(
        "USER_PAPER_ORDER_DETERMINISTIC_FAST_PATH_ENABLED", "true"
    )
    execution = {
        "decision": "EXECUTE",
        "mode": "PAPER",
        "binding": True,
        "order_submitted": True,
        "order_request_id": "basket-by-mock",
        "request_state": "IN_PROGRESS",
        "user_message": "PAPER 바스켓 주문을 제출했고 종목별 체결을 추적 중입니다.",
    }

    def process(**kwargs: Any) -> dict[str, Any]:
        interpretation = kwargs["interpretation"]
        assert interpretation["action"] == "PLACE_BASKET"
        assert interpretation["basket_instrument_mentions"] == [
            "삼성전자",
            "SK하이닉스",
            "LG",
        ]
        assert interpretation["notional_krw"] == "1000000"
        return execution

    monkeypatch.setattr(ceo, "process_deterministic_user_paper_order", process)
    response = ceo.ceo_query(
        ceo.CeoAsk(
            query="삼성전자, SK하이닉스, LG 100만원씩 매수해",
            request_id="request-basket-fast-100",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )

    assert response["order_state"] == "IN_PROGRESS"
    assert response["answer"] == execution["user_message"]


def test_quantity_sell_basket_uses_the_discord_deterministic_paper_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    repository = _OrderedRepository(events)
    _install_successful_route(monkeypatch, events=events, repository=repository)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("USER_PAPER_ORDER_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv(
        "USER_PAPER_ORDER_DETERMINISTIC_FAST_PATH_ENABLED", "true"
    )
    execution = {
        "decision": "EXECUTE",
        "mode": "PAPER",
        "binding": True,
        "order_submitted": True,
        "order_request_id": "quantity-sell-basket-by-mock",
        "request_state": "IN_PROGRESS",
        "user_message": "PAPER 바스켓 매도 주문을 제출했고 종목별 체결을 추적 중입니다.",
    }

    def process(**kwargs: Any) -> dict[str, Any]:
        interpretation = kwargs["interpretation"]
        assert interpretation["action"] == "PLACE_BASKET"
        assert interpretation["basket_instrument_mentions"] == ["삼성전자", "SK하이닉스"]
        assert interpretation["basket_quantities"] == ["3", "2"]
        assert interpretation["side"] == "SELL"
        assert interpretation["notional_krw"] is None
        return execution

    monkeypatch.setattr(ceo, "process_deterministic_user_paper_order", process)
    response = ceo.ceo_query(
        ceo.CeoAsk(
            query="삼성전자 3주, SK하이닉스 2주 시장가 매도해",
            request_id="request-quantity-sell-basket-fast-100",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )

    assert response["order_state"] == "IN_PROGRESS"
    assert response["answer"] == execution["user_message"]


def test_member_notional_basket_uses_the_discord_deterministic_paper_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    repository = _OrderedRepository(events)
    _install_successful_route(monkeypatch, events=events, repository=repository)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("USER_PAPER_ORDER_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv(
        "USER_PAPER_ORDER_DETERMINISTIC_FAST_PATH_ENABLED", "true"
    )
    execution = {
        "decision": "EXECUTE",
        "mode": "PAPER",
        "binding": True,
        "order_submitted": True,
        "order_request_id": "member-notional-basket-by-mock",
        "request_state": "IN_PROGRESS",
        "user_message": "PAPER 바스켓 주문을 제출했고 종목별 체결을 추적 중입니다.",
    }

    def process(**kwargs: Any) -> dict[str, Any]:
        interpretation = kwargs["interpretation"]
        assert interpretation["action"] == "PLACE_BASKET"
        assert interpretation["basket_instrument_mentions"] == ["삼성전자", "SK하이닉스"]
        assert interpretation["basket_notionals_krw"] == ["1000000", "500000"]
        assert interpretation["basket_quantities"] == []
        assert interpretation["side"] == "BUY"
        assert interpretation["notional_krw"] is None
        return execution

    monkeypatch.setattr(ceo, "process_deterministic_user_paper_order", process)
    response = ceo.ceo_query(
        ceo.CeoAsk(
            query="삼성전자 100만원, SK하이닉스 50만원 시장가 매수해",
            request_id="request-member-notional-basket-fast-100",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )

    assert response["order_state"] == "IN_PROGRESS"
    assert response["answer"] == execution["user_message"]


@pytest.mark.parametrize(
    "raw",
    [
        "SK하이닉스 보유수량 확인해서 시장가로 1주 매도",
        "내 PAPER 계좌에서 보유 중인 삼성전자 2주 시장가 매도해줘",
        "지금 삼성전자 한 주 시장가로 매수 주문 넣어주세요",
        "모의투자 계좌에서 SK하이닉스 1주 팔아줘",
        "현재 보유잔고 조회 후 SK하이닉스 1주 매도 요청",
    ],
)
def test_safe_natural_order_variants_enter_the_bound_paper_lane(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    events: list[str] = []
    repository = _OrderedRepository(events)
    create = _install_successful_route(
        monkeypatch, events=events, repository=repository
    )

    response = ceo.ceo_query(
        ceo.CeoAsk(
            query=raw,
            request_id="request-natural-variant",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )

    assert create.call_count == 2
    root_call, trading_call = create.call_args_list
    assert root_call.kwargs["initial_status"] == "blocked"
    assert trading_call.kwargs["initial_status"] == "blocked"
    assert raw in root_call.kwargs["body"]
    assert raw in trading_call.kwargs["body"]
    assert response["order_mode"] == "PAPER"
    assert response["planning"]["selected_departments"] == ["trading-department"]

    stored = repository.get(response["order_request_id"])
    assert stored is not None
    assert stored.raw_instruction == raw


def test_conditional_command_uses_only_the_precreated_trading_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    repository = _OrderedRepository(events)
    create = _install_successful_route(
        monkeypatch, events=events, repository=repository
    )
    monkeypatch.setattr(
        llm_observability,
        "start_root_trace",
        lambda **_kwargs: SimpleNamespace(context="trace-conditional-root"),
    )
    raw = "삼성전자 5분봉 RSI가 30을 상향 돌파하면 1주 매수"

    response = ceo.ceo_query(
        ceo.CeoAsk(
            query=raw,
            request_id="request-conditional-100",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )

    assert create.call_count == 2
    root_call, trading_call = create.call_args_list
    assert root_call.kwargs["assignee"] == "ceo-agent"
    assert trading_call.kwargs["assignee"] == "trading-department"
    assert root_call.kwargs["initial_status"] == "blocked"
    assert trading_call.kwargs["initial_status"] == "blocked"
    trading_body = trading_call.kwargs["body"]
    assert "langsmith_trace_context=trace-conditional-root" in root_call.kwargs["body"]
    assert "hgfinance.user-conditional-paper-rule.v1" in trading_body
    assert "mcp_tool=process_user_conditional_paper_rule" in trading_body
    assert "activation_policy=IMMEDIATE_AFTER_DETERMINISTIC_VALIDATION" in trading_body
    assert "Do not create Risk, QA, Research, Accounting" in trading_body
    assert "MARKET uses only type+field" in trading_body
    assert "Price literals" in trading_body
    assert "unit=PRICE" in trading_body
    assert "Canonical daily-SMA example" in trading_body
    assert "TIMEFRAME_REQUIRED_FOR_CROSS" in trading_body
    assert "max_data_age_seconds=30" in trading_body
    assert "trusted KRX regular-session close default" in trading_body
    assert "For 2-10 independent conditional actions" in trading_body
    assert "CONDITION_EXPRESSION_CLARIFICATION_REQUIRED" in trading_body
    assert "pass candidates in source-text order" in trading_body
    assert "oco_mode=EXIT_BRACKET" in trading_body
    assert "oco_group_id: the trusted boundary derives it" in trading_body
    assert "multiple actions, and LIVE" not in trading_body
    assert raw in trading_body
    assert response["conditional_rule"] is True
    assert response["order_state"] == "RULE_INTERPRETATION_QUEUED"
    assert response["planning"]["selected_departments"] == ["trading-department"]
    assert response["planning"]["qa_required"] is False

    stored = repository.get(response["order_request_id"])
    assert stored is not None
    assert stored.mode == "PAPER"
    assert stored.ceo_root_task_id == "t_root1"
    assert stored.trading_task_id == "t_trade1"


def test_deterministic_paper_result_closes_existing_redacted_root_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_trace = SimpleNamespace(context="trace-paper-root", run_id="run-paper-root")
    start = Mock(return_value=root_trace)
    close = Mock(return_value=True)
    monkeypatch.setattr(llm_observability, "start_root_trace", start)
    monkeypatch.setattr(llm_observability, "close_root_trace", close)
    monkeypatch.setattr(
        ceo,
        "_route_user_paper_order",
        lambda *_args, **_kwargs: {
            "task_id": "t_paper_root",
            "trading_task_id": "t_paper_trading",
            "execution": {
                "decision": "EXECUTE",
                "mode": "PAPER",
                "order_submitted": True,
                "request_state": "COMPLETED",
            },
        },
    )

    response = ceo._route_traced_user_paper_order(
        ceo.CeoAsk(query="삼성전자 매수 1주 시장가", request_id="paper-trace-1"),
        owner_id=USER_ID,
        mandate=None,
        conditional_rule=False,
    )

    assert response["execution"]["request_state"] == "COMPLETED"
    start.assert_called_once()
    close.assert_called_once()
    kwargs = close.call_args.kwargs
    assert kwargs["department"] == "trading"
    assert kwargs["task_id"] == "t_paper_trading"
    assert kwargs["terminal_metadata"]["raw_payloads_sent"] is False
    assert kwargs["output_summary"] == {
        "execution_path": "deterministic_paper",
        "mode": "PAPER",
        "request_state": "COMPLETED",
        "decision": "EXECUTE",
        "order_submitted": True,
    }


def test_relative_time_order_activates_existing_conditional_worker_without_hermes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    repository = _OrderedRepository(events)
    create = _install_successful_route(
        monkeypatch, events=events, repository=repository
    )
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("USER_PAPER_ORDER_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv("USER_PAPER_ORDER_DETERMINISTIC_FAST_PATH_ENABLED", "true")
    raw = (
        "<@1536991290842030130>   @홍진표 대표 "
        "삼성전자 이거 4분 뒤에 1주 매수해줘"
    )

    def process(**kwargs: Any) -> dict[str, Any]:
        events.append("delayed-rule-process")
        assert kwargs["root_task_id"] == "t_root1"
        assert kwargs["trading_task_id"] == "t_trade1"
        assert kwargs["interpretation_source"] == "DETERMINISTIC"
        candidate = kwargs["candidate"]
        assert candidate.symbol == "삼성전자"
        assert candidate.condition.left.type.value == "TIME"
        assert candidate.action.sizing.value == 1
        return {
            "binding": True,
            "mode": "PAPER",
            "rule_active": True,
            "rule_id": "rule-1",
            "state": "ACTIVE",
            "user_message": "PAPER 예약 조건주문이 ACTIVE 전환되었습니다.",
        }

    monkeypatch.setattr(ceo, "process_user_conditional_paper_rule", process)

    response = ceo.ceo_query(
        ceo.CeoAsk(
            query=raw,
            request_id="request-delayed-100",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )

    assert create.call_count == 2
    _, trading_call = create.call_args_list
    assert "DETERMINISTIC_RELATIVE_TIME" in trading_call.kwargs["body"]
    assert "mcp_tool=process_user_conditional_paper_rule" not in trading_call.kwargs["body"]
    assert "release-t_trade1" not in events
    assert "delayed-rule-process" in events
    assert response["conditional_rule"] is True
    assert response["order_state"] == "COMPLETED"
    assert response["execution"]["state"] == "ACTIVE"


def test_research_then_conditional_creates_analysis_root_without_trading_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryUserOrderRequestRepository()
    create = Mock(return_value={"task_id": "t_analysis_root", "status": "ready"})
    monkeypatch.setattr(ceo, "fetch_current_mandate_by_fund", lambda _fund: None)
    monkeypatch.setattr(ceo, "user_order_repository", lambda: repository)
    monkeypatch.setattr(
        ceo,
        "require_trading_book_access",
        lambda _owner, _fund, _book: {
            "user_id": USER_ID,
            "fund_id": FUND_ID,
            "book_id": BOOK_ID,
        },
    )
    monkeypatch.setattr(ceo.hermes_boundary, "create_kanban_task", create)
    monkeypatch.setattr(ceo.hermes_boundary, "comment_root_scope", lambda **_kw: True)
    monkeypatch.setattr(
        ceo,
        "_wait_for_planning",
        lambda _task_id: {
            "status": "accepted",
            "planning": {"selected_departments": []},
            "answer": "accepted",
        },
    )

    raw = "research 분석 후 삼성전자 262,000원 초과 시 5주 매도 조건주문"
    response = ceo.ceo_query(
        ceo.CeoAsk(
            query=raw,
            request_id="discord:analysis-then-rule-1",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )

    assert create.call_count == 1
    root_body = create.call_args.kwargs["body"]
    assert "deferred_conditional=true" in root_body
    assert "deferred_conditional_required_profile=research-department" in root_body
    assert "selected_primary_profiles=trading-department" not in root_body
    assert "Research 관점의 투자 분석" in root_body
    assert response["analysis_then_conditional"] is True
    assert response["conditional_rule_activation"] == (
        "AFTER_RESEARCH_PRIMARY_COMPLETED"
    )
    assert "삼성전자 262,000원 초과 시 5주 매도 조건주문" in response["answer"]
    assert response["order_state"] == "KANBAN_QUEUED"
    stored = repository.get(response["order_request_id"])
    assert stored is not None
    assert stored.ceo_root_task_id == "t_analysis_root"
    assert stored.trading_task_id is None

    monkeypatch.setattr(
        ceo.hermes_boundary,
        "show_kanban_task",
        lambda _task_id: {"task_id": "t_analysis_root", "body": root_body},
    )
    create.reset_mock()
    replay = ceo.ceo_query(
        ceo.CeoAsk(
            query=raw,
            request_id="discord:analysis-then-rule-1",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )
    assert replay["task_id"] == "t_analysis_root"
    assert create.call_count == 0


def test_conditional_advice_question_does_not_enter_the_binding_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = Mock(return_value={"task_id": "t_root2", "status": "ready"})
    repository = Mock(side_effect=AssertionError("advice must not admit a rule"))
    monkeypatch.setattr(ceo, "fetch_current_mandate_by_fund", lambda _fund: None)
    monkeypatch.setattr(ceo, "user_order_repository", repository)
    monkeypatch.setattr(ceo.hermes_boundary, "create_kanban_task", create)
    monkeypatch.setattr(ceo.hermes_boundary, "comment_root_scope", lambda **_kw: True)
    monkeypatch.setattr(ceo, "_wait_for_planning", lambda _task_id: ceo._accepted_fallback())

    response = ceo.ceo_query(
        ceo.CeoAsk(
            query="삼성전자 RSI 30 이하이면 1주 매수해도 될까?",
            request_id="request-conditional-advice",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )

    create.assert_called_once()
    assert "initial_status" not in create.call_args.kwargs
    assert "user-conditional-paper-rule" not in create.call_args.kwargs["body"]
    assert "order_request_id" not in response
    repository.assert_not_called()


@pytest.mark.parametrize(
    ("owner_id", "fund_id", "book_id", "status_code", "detail"),
    [
        (None, FUND_ID, BOOK_ID, 401, "portfolio_authentication_required"),
        (USER_ID, None, BOOK_ID, 422, "portfolio_fund_id_required"),
        (USER_ID, FUND_ID, None, 422, "portfolio_book_id_required"),
    ],
)
def test_order_like_text_requires_exact_authenticated_fund_book_scope(
    monkeypatch: pytest.MonkeyPatch,
    owner_id: str | None,
    fund_id: str | None,
    book_id: str | None,
    status_code: int,
    detail: str,
) -> None:
    access = Mock()
    create = Mock()
    monkeypatch.setattr(ceo, "fetch_current_mandate_by_fund", lambda _fund: None)
    monkeypatch.setattr(ceo, "require_trading_book_access", access)
    monkeypatch.setattr(ceo.hermes_boundary, "create_kanban_task", create)

    with pytest.raises(HTTPException) as raised:
        ceo.ceo_query(
            ceo.CeoAsk(
                query="삼성전자 매수 10주 시장가",
                request_id="request-101",
                fund_id=fund_id,
                book_id=book_id,
            ),
            owner_id=owner_id,
        )

    assert raised.value.status_code == status_code
    assert raised.value.detail == detail
    access.assert_not_called()
    create.assert_not_called()


def test_advisory_question_keeps_the_existing_non_order_ceo_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = Mock(return_value={"task_id": "t_root2", "status": "ready"})
    access = Mock()
    repository = Mock(side_effect=AssertionError("advisory must not admit an order"))
    monkeypatch.setattr(ceo, "fetch_current_mandate_by_fund", lambda _fund: None)
    monkeypatch.setattr(ceo, "require_trading_book_access", access)
    monkeypatch.setattr(ceo, "user_order_repository", repository)
    monkeypatch.setattr(ceo.hermes_boundary, "create_kanban_task", create)
    monkeypatch.setattr(ceo.hermes_boundary, "comment_root_scope", lambda **_kw: True)
    monkeypatch.setattr(ceo, "_wait_for_planning", lambda _task_id: ceo._accepted_fallback())

    response = ceo.ceo_query(
        ceo.CeoAsk(
            query="삼성전자 전망과 리스크를 분석해줘",
            request_id="request-102",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )

    create.assert_called_once()
    assert "initial_status" not in create.call_args.kwargs
    assert "user-paper-order-request" not in create.call_args.kwargs["body"]
    assert "order_request_id" not in response
    access.assert_not_called()
    repository.assert_not_called()


def test_explicit_live_request_is_routed_to_the_paper_only_rejection_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    repository = _OrderedRepository(events)
    create = _install_successful_route(
        monkeypatch, events=events, repository=repository
    )
    raw = "LIVE 계좌로 삼성전자 매수 10주 시장가"

    response = ceo.ceo_query(
        ceo.CeoAsk(
            query=raw,
            request_id="request-103",
            fund_id=FUND_ID,
            book_id=BOOK_ID,
        ),
        owner_id=USER_ID,
    )

    assert create.call_count == 2
    assert raw in create.call_args_list[0].kwargs["body"]
    assert raw in create.call_args_list[1].kwargs["body"]
    assert "execution_mode=PAPER_ONLY" in create.call_args_list[1].kwargs["body"]
    assert response["order_mode"] == "PAPER"
    assert response["binding"] is False


def test_production_without_hermes_runtime_fails_before_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = Mock()
    repository = Mock()
    create = Mock()
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("USER_PAPER_ORDER_WORKFLOW_ENABLED", raising=False)
    monkeypatch.setattr(ceo, "fetch_current_mandate_by_fund", lambda _fund: None)
    monkeypatch.setattr(ceo, "require_trading_book_access", access)
    monkeypatch.setattr(ceo, "user_order_repository", repository)
    monkeypatch.setattr(ceo.hermes_boundary, "create_kanban_task", create)

    with pytest.raises(HTTPException) as raised:
        ceo.ceo_query(
            ceo.CeoAsk(
                query="삼성전자 매수 10주 시장가",
                request_id="request-104",
                fund_id=FUND_ID,
                book_id=BOOK_ID,
            ),
            owner_id=USER_ID,
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == "paper_order_hermes_runtime_unavailable"
    access.assert_not_called()
    repository.assert_not_called()
    create.assert_not_called()


def test_indicator_prompt_names_every_parameter_the_validator_accepts() -> None:
    """The interpreter used to receive indicator names only.

    Without the parameter vocabulary it had to guess spellings, and Korean HTS
    notation such as "bollingerband(종가,2,0,20)" turned that guess into
    UNSUPPORTED_INDICATOR_PARAMETER on 2026-08-28.  The catalog is built from
    the registry, so a new indicator cannot silently miss the prompt.
    """

    from orchestration.conditional_rules import list_supported_indicators

    catalog = ceo._conditional_rule_indicator_catalog_prompt()
    assert "BOLLINGER(PERIOD=20,STDDEV=2,OFFSET=0)->UPPER|MIDDLE|LOWER" in catalog
    assert "BROKER_SEARCH_MATCH(SEARCH_ID=required)->VALUE" in catalog
    for item in list_supported_indicators():
        assert f"{item['name']}(" in catalog, item["name"]
        for parameter in {*item["defaults"], *item["required_parameters"]}:
            assert parameter in catalog, (item["name"], parameter)
