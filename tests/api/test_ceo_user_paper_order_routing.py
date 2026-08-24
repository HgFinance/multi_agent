from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from apps.api import ceo
from apps.api.user_order_workflow import InMemoryUserOrderRequestRepository
from orchestration.ceo_workflow_scope import (
    requested_by_from_body,
    user_paper_order_scope_from_body,
    workflow_role_from_body,
    workflow_root_from_body,
)


USER_ID = "11111111-1111-4111-8111-111111111111"
FUND_ID = "22222222-2222-4222-8222-222222222222"
BOOK_ID = "33333333-3333-4333-8333-333333333333"


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
            events.append("create-root-running")
            return {"task_id": "t_root1", "status": "running"}
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


def test_exact_sample_is_durably_bound_before_either_card_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        "create-root-running",
        "comment-root-scope",
        "bind-root",
        "create-trading-blocked",
        "bind-trading",
        "complete-t_root1",
        "release-t_trade1",
    ]
    assert create.call_count == 2
    root_call, trading_call = create.call_args_list
    assert root_call.kwargs["initial_status"] == "running"
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
    assert "managed omission default: order_type=MARKET" in trading_body
    assert "limit_price=null, and no ORDER_TYPE evidence" in trading_body
    assert "conflicting market/limit language, must CLARIFY" in trading_body
    assert "Every evidence item must include normalized" in trading_body

    stored = repository.get(response["order_request_id"])
    assert stored is not None
    assert stored.ceo_root_task_id == "t_root1"
    assert stored.trading_task_id == "t_trade1"
    assert stored.mode == "PAPER"
    assert response["order_mode"] == "PAPER"
    assert response["binding"] is False
    assert response["task"]["status"] == "done"


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
    assert trading_call.kwargs["initial_status"] == "running"
    assert "user-paper-order-deterministic.v1" in trading_call.kwargs["body"]
    assert "mcp_tool=process_user_paper_order" not in trading_call.kwargs["body"]
    assert events == [
        "authorize",
        "admit",
        "create-root-running",
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
    assert root_call.kwargs["initial_status"] == "running"
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
    assert root_call.kwargs["initial_status"] == "running"
    assert trading_call.kwargs["initial_status"] == "blocked"
    trading_body = trading_call.kwargs["body"]
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
    assert "trusted 10-minute default" in trading_body
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
