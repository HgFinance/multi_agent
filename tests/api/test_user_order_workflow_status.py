from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apps.api import user_orders
from apps.api.user_order_workflow import (
    UserOrderRequestRecord,
    UserOrderWorkflowUnavailable,
)


OWNER_ID = str(uuid4())
OTHER_USER_ID = str(uuid4())
FUND_ID = str(uuid4())
BOOK_ID = str(uuid4())
ORDER_REQUEST_ID = str(uuid4())
DIRECTIVE_ID = str(uuid4())


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
        "mode": "PAPER",
        "state": "KANBAN_QUEUED",
        "action": "PLACE_ORDER",
        "ceo_root_task_id": "ceo-root-0001",
        "trading_task_id": "trading-task-0001",
        "clarification_code": None,
        "error_code": None,
        "error_message": None,
        "directive": None,
    }
    repository.mark_outcome.assert_not_called()
    read_status.assert_not_called()


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
