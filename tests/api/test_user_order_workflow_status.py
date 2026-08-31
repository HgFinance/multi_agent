from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import inspect
from unittest.mock import ANY, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apps.api import user_order_workflow as workflow
from apps.api import user_orders
from apps.api.user_order_workflow import (
    PostgresUserOrderRequestRepository,
    UserOrderRequestRecord,
    UserOrderWorkflowUnavailable,
)


OWNER_ID = str(uuid4())
OTHER_USER_ID = str(uuid4())
FUND_ID = str(uuid4())
BOOK_ID = str(uuid4())
ORDER_REQUEST_ID = str(uuid4())
DIRECTIVE_ID = str(uuid4())


def test_production_order_repository_requires_isolated_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CONTROL_DATABASE_URL", "postgresql://generic.invalid/control")
    monkeypatch.delenv("ORDER_ORCHESTRATOR_DATABASE_URL", raising=False)

    with pytest.raises(UserOrderWorkflowUnavailable, match="dedicated order"):
        workflow._order_orchestrator_database_url()


def test_order_repository_prefers_isolated_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = "postgresql://order.invalid/control"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CONTROL_DATABASE_URL", "postgresql://generic.invalid/control")
    monkeypatch.setenv("ORDER_ORCHESTRATOR_DATABASE_URL", isolated)

    assert workflow._order_orchestrator_database_url() == isolated


def _client(*, subject: str = OWNER_ID) -> TestClient:
    app = FastAPI()
    app.include_router(user_orders.router)
    app.dependency_overrides[user_orders.current_user] = lambda: subject
    return TestClient(app)


def _record(
    *,
    user_id: str = OWNER_ID,
    state: str = "KANBAN_QUEUED",
    directive_id: str | None = None,
) -> UserOrderRequestRecord:
    now = datetime.now(timezone.utc)
    return UserOrderRequestRecord(
        order_request_id=ORDER_REQUEST_ID,
        user_id=user_id,
        fund_id=FUND_ID,
        book_id=BOOK_ID,
        client_request_id="browser-request-0001",
        raw_instruction="삼성전자 매수 10주 시장가",
        normalized_instruction="삼성전자 매수 10주 시장가",
        raw_instruction_sha256="a" * 64,
        state=state,
        ceo_root_task_id="ceo-root-0001",
        trading_task_id="trading-task-0001",
        action="PLACE_ORDER",
        directive_id=directive_id,
        created_at=now,
        updated_at=now,
    )


def _directive(
    *,
    state: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> user_orders.UserDirectiveResponse:
    now = datetime.now(timezone.utc)
    return user_orders.UserDirectiveResponse.model_validate(
        {
            "directive_id": DIRECTIVE_ID,
            "state": state,
            "action": "PLACE_ORDER",
            "priority": 1000,
            "fund_id": FUND_ID,
            "book_id": BOOK_ID,
            "idempotency_key": "ceo-paper-request-0001",
            "instruction_ref": "instruction-0001",
            "payload_sha256": "b" * 64,
            "created_at": now,
            "updated_at": now,
            "completed_at": now if state in {"COMPLETED", "FAILED"} else None,
            "error_code": error_code,
            "error_message": error_message,
            "legs": [],
        }
    )


def _access(user_id: str = OWNER_ID) -> dict[str, str]:
    return {
        "user_id": user_id,
        "fund_id": FUND_ID,
        "book_id": BOOK_ID,
        "role": "OWNER",
    }


def test_status_requires_current_book_access_and_exact_request_owner() -> None:
    repository = MagicMock()
    repository.get.return_value = _record(user_id=OWNER_ID)
    with (
        patch.object(user_orders, "user_order_repository", return_value=repository),
        patch.object(
            user_orders,
            "require_trading_book_access",
            return_value=_access(OTHER_USER_ID),
        ) as require_access,
        patch.object(
            user_orders, "read_verified_paper_directive_status"
        ) as read_status,
    ):
        response = _client(subject=OTHER_USER_ID).get(
            f"/ui/paper-order-requests/{ORDER_REQUEST_ID}"
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "paper_order_request_not_found"
    require_access.assert_called_once_with(OTHER_USER_ID, FUND_ID, BOOK_ID)
    read_status.assert_not_called()


def test_status_propagates_book_access_denial_without_reading_trading() -> None:
    repository = MagicMock()
    repository.get.return_value = _record()
    with (
        patch.object(user_orders, "user_order_repository", return_value=repository),
        patch.object(
            user_orders,
            "require_trading_book_access",
            side_effect=HTTPException(
                status_code=403, detail="portfolio_trading_book_forbidden"
            ),
        ),
        patch.object(
            user_orders, "read_verified_paper_directive_status"
        ) as read_status,
    ):
        response = _client(subject=OTHER_USER_ID).get(
            f"/ui/paper-order-requests/{ORDER_REQUEST_ID}"
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "portfolio_trading_book_forbidden"
    read_status.assert_not_called()


def test_owner_can_read_admitted_request_before_directive_exists() -> None:
    repository = MagicMock()
    repository.get.return_value = _record()
    with (
        patch.object(user_orders, "user_order_repository", return_value=repository),
        patch.object(
            user_orders, "require_trading_book_access", return_value=_access()
        ),
        patch.object(
            user_orders, "read_verified_paper_directive_status"
        ) as read_status,
    ):
        response = _client().get(
            f"/ui/paper-order-requests/{ORDER_REQUEST_ID}"
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "user-paper-order-status.v1",
        "order_request_id": ORDER_REQUEST_ID,
        "client_request_id": "browser-request-0001",
        "request_source": "WEB_OR_API",
        "mode": "PAPER",
        "state": "KANBAN_QUEUED",
        "action": "PLACE_ORDER",
        "ceo_root_task_id": "ceo-root-0001",
        "trading_task_id": "trading-task-0001",
        "clarification_code": None,
        "error_code": None,
        "error_message": None,
        "directive": None,
        "correlation": None,
        # A conditional request's rule outcome rides beside the request state,
        # which on its own read as success while the rule had already FAILED.
        "conditional_rules": None,
    }
    repository.mark_outcome.assert_not_called()
    read_status.assert_not_called()


def test_unknown_status_recovers_committed_directive_without_resubmission() -> None:
    unknown = replace(
        _record(state="UNKNOWN"),
        canonical_payload={"symbol": "005930"},
        payload_sha256="c" * 64,
        error_code="trading_api_unavailable",
        error_message="submission response was ambiguous",
    )
    bound = replace(unknown, directive_id=DIRECTIVE_ID)
    completed = replace(
        bound,
        state="COMPLETED",
        error_code=None,
        error_message=None,
    )
    repository = MagicMock()
    repository.get.return_value = unknown
    repository.find_committed_directive.return_value = DIRECTIVE_ID
    repository.mark_outcome.side_effect = [bound, completed]
    directive = _directive(state="COMPLETED")

    with (
        patch.object(user_orders, "user_order_repository", return_value=repository),
        patch.object(
            user_orders, "require_trading_book_access", return_value=_access()
        ),
        patch.object(
            user_orders,
            "read_verified_paper_directive_status",
            return_value=directive,
        ),
    ):
        response = _client().get(
            f"/ui/paper-order-requests/{ORDER_REQUEST_ID}"
        )

    assert response.status_code == 200
    assert response.json()["state"] == "COMPLETED"
    assert response.json()["directive"]["directive_id"] == DIRECTIVE_ID
    repository.find_committed_directive.assert_called_once_with(unknown)
    assert repository.mark_outcome.call_count == 2


def test_postgres_unknown_recovery_matches_durable_authority_and_idempotency() -> None:
    source = inspect.getsource(
        PostgresUserOrderRequestRepository.find_committed_directive
    )

    for field in (
        "user_id=%s",
        "fund_id=%s",
        "book_id=%s",
        "idempotency_key=%s",
        "action=%s",
        "source_order_request_id",
    ):
        assert field in source
    assert "payload_sha256=%s" not in source


def test_unknown_request_is_404_without_authority_or_trading_lookup() -> None:
    repository = MagicMock()
    repository.get.return_value = None
    with (
        patch.object(user_orders, "user_order_repository", return_value=repository),
        patch.object(user_orders, "require_trading_book_access") as require_access,
        patch.object(
            user_orders, "read_verified_paper_directive_status"
        ) as read_status,
    ):
        response = _client().get(
            f"/ui/paper-order-requests/{ORDER_REQUEST_ID}"
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "paper_order_request_not_found"
    require_access.assert_not_called()
    read_status.assert_not_called()


def test_accounting_pending_wins_over_inconsistent_completed_directive() -> None:
    record = _record(state="IN_PROGRESS", directive_id=DIRECTIVE_ID)
    directive = _directive(
        state="COMPLETED",
        error_code="TRADING_FILL_ACCOUNTING_PENDING",
        error_message="fill awaits accounting acknowledgment",
    )
    updated = replace(
        record,
        state="ACCOUNTING_PENDING",
        error_code=directive.error_code,
        error_message=directive.error_message,
    )
    repository = MagicMock()
    repository.get.return_value = record
    repository.mark_outcome.return_value = updated
    with (
        patch.object(user_orders, "user_order_repository", return_value=repository),
        patch.object(
            user_orders, "require_trading_book_access", return_value=_access()
        ),
        patch.object(
            user_orders,
            "read_verified_paper_directive_status",
            return_value=directive,
        ),
    ):
        response = _client().get(
            f"/ui/paper-order-requests/{ORDER_REQUEST_ID}"
        )

    assert response.status_code == 200
    assert response.json()["state"] == "ACCOUNTING_PENDING"
    assert response.json()["error_code"] == "TRADING_FILL_ACCOUNTING_PENDING"
    repository.mark_outcome.assert_called_once_with(
        ORDER_REQUEST_ID,
        state="ACCOUNTING_PENDING",
        directive_id=DIRECTIVE_ID,
        error_code="TRADING_FILL_ACCOUNTING_PENDING",
        error_message="fill awaits accounting acknowledgment",
        event_type="BROKER_EXECUTION_SNAPSHOT",
        event_payload=ANY,
    )


@pytest.mark.parametrize(
    ("directive_state", "error_code", "error_message"),
    [
        ("COMPLETED", None, None),
        ("FAILED", "TRADING_ORDER_REJECTED", "paper broker rejected order"),
    ],
)
def test_terminal_directive_state_is_persisted_and_returned(
    directive_state: str,
    error_code: str | None,
    error_message: str | None,
) -> None:
    record = _record(state="IN_PROGRESS", directive_id=DIRECTIVE_ID)
    directive = _directive(
        state=directive_state,
        error_code=error_code,
        error_message=error_message,
    )
    updated = replace(
        record,
        state=directive_state,
        error_code=error_code,
        error_message=error_message,
    )
    repository = MagicMock()
    repository.get.return_value = record
    repository.mark_outcome.return_value = updated
    with (
        patch.object(user_orders, "user_order_repository", return_value=repository),
        patch.object(
            user_orders, "require_trading_book_access", return_value=_access()
        ),
        patch.object(
            user_orders,
            "read_verified_paper_directive_status",
            return_value=directive,
        ),
    ):
        response = _client().get(
            f"/ui/paper-order-requests/{ORDER_REQUEST_ID}"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == directive_state
    assert body["error_code"] == error_code
    assert body["error_message"] == error_message
    assert body["directive"]["state"] == directive_state
    repository.mark_outcome.assert_called_once_with(
        ORDER_REQUEST_ID,
        state=directive_state,
        directive_id=DIRECTIVE_ID,
        error_code=error_code,
        error_message=error_message,
        event_type="BROKER_EXECUTION_SNAPSHOT",
        event_payload=ANY,
    )


@pytest.mark.parametrize("failure_point", ["repository_factory", "repository_get"])
def test_repository_read_unavailable_returns_stable_503(failure_point: str) -> None:
    repository = MagicMock()
    if failure_point == "repository_get":
        repository.get.side_effect = UserOrderWorkflowUnavailable("database offline")
        factory = MagicMock(return_value=repository)
    else:
        factory = MagicMock(
            side_effect=UserOrderWorkflowUnavailable("database not configured")
        )
    with (
        patch.object(user_orders, "user_order_repository", factory),
        patch.object(user_orders, "require_trading_book_access") as require_access,
    ):
        response = _client().get(
            f"/ui/paper-order-requests/{ORDER_REQUEST_ID}"
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "paper_order_workflow_unavailable"
    require_access.assert_not_called()


def test_repository_transition_unavailable_returns_stable_503() -> None:
    record = _record(state="IN_PROGRESS", directive_id=DIRECTIVE_ID)
    repository = MagicMock()
    repository.get.return_value = record
    repository.mark_outcome.side_effect = UserOrderWorkflowUnavailable(
        "database disconnected"
    )
    with (
        patch.object(user_orders, "user_order_repository", return_value=repository),
        patch.object(
            user_orders, "require_trading_book_access", return_value=_access()
        ),
        patch.object(
            user_orders,
            "read_verified_paper_directive_status",
            return_value=_directive(state="COMPLETED"),
        ),
    ):
        response = _client().get(
            f"/ui/paper-order-requests/{ORDER_REQUEST_ID}"
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "paper_order_workflow_unavailable"


def test_conditional_rule_failure_travels_with_the_completed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A COMPLETED request said nothing about the rule it created failing.

    The activation workflow finishes and marks the request COMPLETED, but the
    rule can fail minutes later at execution without ever producing a
    directive.  The UI showed only "PAPER 주문 상태 COMPLETED" while the rule
    was FAILED on TRADING_MARKET_QUOTE_STALE (2026-08-28).
    """

    from types import SimpleNamespace

    from apps.api import user_orders

    record = SimpleNamespace(
        client_request_id="browser-request-0001",
        user_id=OWNER_ID,
    )
    rule = SimpleNamespace(
        rule_id="b1a19294-d41b-43bc-9442-90cb1a6d4534",
        client_request_id="browser-request-0001",
        state="FAILED",
        last_execution_state="FAILED",
        last_guard_code="READY_FOR_PAPER_DIRECTIVE",
        last_error_code="MARKET_QUOTE_STALE",
    )
    unrelated = SimpleNamespace(
        rule_id="00000000-0000-4000-8000-0000000000ff",
        client_request_id="some-other-request",
        state="ACTIVE",
        last_execution_state=None,
        last_guard_code=None,
        last_error_code=None,
    )
    monkeypatch.setattr(
        user_orders,
        "conditional_rule_repository",
        lambda: SimpleNamespace(list_for_user=lambda _user: [rule, unrelated]),
    )

    outcomes = user_orders._conditional_rule_outcomes(record)
    assert outcomes is not None
    assert len(outcomes) == 1
    assert str(outcomes[0].rule_id) == rule.rule_id
    assert outcomes[0].state == "FAILED"
    assert outcomes[0].status_message  # a Korean sentence, not a bare code
    assert "MARKET_QUOTE_STALE" not in outcomes[0].status_message


def test_compound_trailing_stop_failure_travels_with_its_immediate_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from apps.api import user_orders

    record = SimpleNamespace(
        order_request_id="77777777-7777-4777-8777-777777777777",
        client_request_id="compound-buy-request-0001",
        user_id=OWNER_ID,
    )
    rule = SimpleNamespace(
        rule_id="b1a19294-d41b-43bc-9442-90cb1a6d4534",
        client_request_id="derived-sell-rule-request",
        state="CANCELLED",
        last_execution_state=None,
        last_guard_code=None,
        last_error_code="ENTRY_POSITION_QUANTITY_MISMATCH",
    )
    monkeypatch.setattr(
        user_orders,
        "conditional_rule_repository",
        lambda: SimpleNamespace(list_for_user=lambda _user: [rule]),
    )
    monkeypatch.setattr(
        user_orders,
        "paper_order_bundle_repository",
        lambda: SimpleNamespace(
            get_by_immediate_order_request=lambda **_kwargs: SimpleNamespace(
                conditional_rule_id=rule.rule_id
            )
        ),
    )

    outcomes = user_orders._conditional_rule_outcomes(record)

    assert outcomes is not None
    assert len(outcomes) == 1
    assert outcomes[0].state == "CANCELLED"
    assert outcomes[0].last_error_code == "ENTRY_POSITION_QUANTITY_MISMATCH"
    assert "자동 매도를 안전하게 중단" in str(outcomes[0].status_message)


def test_expired_rule_status_says_that_no_more_paper_order_is_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from apps.api import user_orders

    record = SimpleNamespace(
        client_request_id="expired-condition-request-0001",
        user_id=OWNER_ID,
    )
    rule = SimpleNamespace(
        rule_id="b1a19294-d41b-43bc-9442-90cb1a6d4534",
        client_request_id="expired-condition-request-0001",
        state="EXPIRED",
        last_execution_state=None,
        last_guard_code=None,
        last_error_code=None,
        effective_expires_at=datetime(2026, 8, 29, 6, 30, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        user_orders,
        "conditional_rule_repository",
        lambda: SimpleNamespace(list_for_user=lambda _user: [rule]),
    )

    outcomes = user_orders._conditional_rule_outcomes(record)

    assert outcomes is not None
    assert outcomes[0].effective_expires_at == rule.effective_expires_at
    assert "추가 PAPER 주문을 제출하지 않았습니다" in str(outcomes[0].status_message)


def test_failed_rule_status_says_that_no_more_paper_order_is_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from apps.api import user_orders

    record = SimpleNamespace(
        client_request_id="blocked-condition-request-0001",
        user_id=OWNER_ID,
    )
    rule = SimpleNamespace(
        rule_id="b1a19294-d41b-43bc-9442-90cb1a6d4534",
        client_request_id="blocked-condition-request-0001",
        state="FAILED",
        last_execution_state=None,
        last_guard_code=None,
        last_error_code=None,
        effective_expires_at=None,
    )
    monkeypatch.setattr(
        user_orders,
        "conditional_rule_repository",
        lambda: SimpleNamespace(list_for_user=lambda _user: [rule]),
    )

    outcomes = user_orders._conditional_rule_outcomes(record)

    assert outcomes is not None
    assert "안전하게 중단" in str(outcomes[0].status_message)


def test_rule_lookup_failure_never_breaks_the_request_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from apps.api import user_orders

    def _boom() -> None:
        raise RuntimeError("rule store unavailable")

    monkeypatch.setattr(user_orders, "conditional_rule_repository", _boom)
    record = SimpleNamespace(client_request_id="browser-request-0001", user_id=OWNER_ID)
    assert user_orders._conditional_rule_outcomes(record) is None


def test_a_plain_order_request_reports_no_conditional_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from apps.api import user_orders

    monkeypatch.setattr(
        user_orders,
        "conditional_rule_repository",
        lambda: SimpleNamespace(list_for_user=lambda _user: []),
    )
    record = SimpleNamespace(client_request_id="browser-request-0001", user_id=OWNER_ID)
    assert user_orders._conditional_rule_outcomes(record) is None
