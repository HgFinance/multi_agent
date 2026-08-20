from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi import HTTPException

from apps.api import ceo, user_order_orchestrator as orchestrator
from apps.api.user_order_workflow import InMemoryUserOrderRequestRepository
from apps.api.user_orders import (
    DirectiveLeg,
    DirectiveAction as ResponseAction,
    DirectiveState,
    UserDirectiveResponse,
)
from orchestration.ceo_workflow_scope import UserPaperOrderScope, build_root_body
from orchestration.contracts.user_paper_order import (
    CandidateDecision,
    DirectiveAction,
    EvidenceField,
    HermesOrderCandidate,
    OrderSide,
    OrderType,
    TextEvidence,
)
from orchestration.user_order_language import raw_text_sha256


USER_ID = "11111111-1111-4111-8111-111111111111"
FUND_ID = "22222222-2222-4222-8222-222222222222"
BOOK_ID = "33333333-3333-4333-8333-333333333333"
DIRECTIVE_ID = "44444444-4444-4444-8444-444444444444"
ROOT_TASK_ID = "t_root1"
TRADING_TASK_ID = "t_trade1"


def _evidence(
    raw: str, field: EvidenceField, text: str, normalized: str
) -> TextEvidence:
    start = raw.index(text)
    return TextEvidence(
        field=field,
        start=start,
        end=start + len(text),
        text=text,
        normalized=normalized,
    )


def _execute_candidate(
    raw: str,
    *,
    quantity: str = "10",
    raw_hash: str | None = None,
) -> dict[str, Any]:
    return HermesOrderCandidate(
        raw_text_sha256=raw_hash or raw_text_sha256(raw),
        decision=CandidateDecision.EXECUTE,
        action=DirectiveAction.PLACE_ORDER,
        instrument_mention="삼성전자",
        side=OrderSide.BUY,
        quantity=quantity,
        order_type=OrderType.MARKET,
        evidence=(
            _evidence(raw, EvidenceField.INSTRUMENT, "삼성전자", "삼성전자"),
            _evidence(raw, EvidenceField.SIDE, "매수", "BUY"),
            _evidence(raw, EvidenceField.QUANTITY, "10주", quantity),
            _evidence(raw, EvidenceField.ORDER_TYPE, "시장가", "MARKET"),
        ),
    ).model_dump(mode="json")


def _workflow(
    monkeypatch: pytest.MonkeyPatch,
    raw: str = "삼성전자 매수 10주 시장가",
) -> SimpleNamespace:
    repository = InMemoryUserOrderRequestRepository()
    record = repository.admit(
        user_id=USER_ID,
        fund_id=FUND_ID,
        book_id=BOOK_ID,
        client_request_id="request-200",
        raw_instruction=raw,
    )
    record = repository.bind_root(record.order_request_id, ROOT_TASK_ID)
    record = repository.bind_trading_task(record.order_request_id, TRADING_TASK_ID)
    scope = UserPaperOrderScope(
        order_request_id=record.order_request_id,
        raw_instruction_sha256=record.raw_instruction_sha256,
        fund_id=FUND_ID,
        book_id=BOOK_ID,
    )
    root = {
        "id": ROOT_TASK_ID,
        "assignee": "ceo-agent",
        "status": "ready",
        "body": build_root_body(
            raw,
            "request-200",
            workflow_mode="binding",
            requested_by=USER_ID,
            user_paper_order_scope=scope,
        ),
        "parents": [],
    }
    trading = {
        "id": TRADING_TASK_ID,
        "assignee": "trading-department",
        "status": "running",
        "body": ceo._paper_order_child_body(
            query=raw,
            scope=scope,
            root_task_id=ROOT_TASK_ID,
            request_id="request-200",
            has_mandate=False,
        ),
        "parents": [],
    }
    tasks = {ROOT_TASK_ID: root, TRADING_TASK_ID: trading}
    monkeypatch.setattr(orchestrator, "user_order_repository", lambda: repository)
    monkeypatch.setattr(
        orchestrator.hermes_boundary,
        "show_kanban_task",
        lambda task_id, **_kwargs: tasks.get(task_id),
    )
    return SimpleNamespace(
        raw=raw,
        repository=repository,
        record=record,
        root=root,
        trading=trading,
        tasks=tasks,
    )


def _directive_response(
    record: Any,
    *,
    state: DirectiveState = DirectiveState.IN_PROGRESS,
    error_code: str | None = None,
) -> UserDirectiveResponse:
    now = datetime.now(timezone.utc)
    return UserDirectiveResponse(
        directive_id=UUID(DIRECTIVE_ID),
        state=state,
        action=ResponseAction.PLACE_ORDER,
        priority=1000,
        fund_id=UUID(FUND_ID),
        book_id=UUID(BOOK_ID),
        idempotency_key=f"ceo-paper:{record.order_request_id}",
        instruction_ref="instruction-ref",
        payload_sha256="a" * 64,
        created_at=now,
        updated_at=now,
        completed_at=now if state is DirectiveState.COMPLETED else None,
        error_code=error_code,
        legs=[],
    )


def _process(context: SimpleNamespace, candidate: dict[str, Any]) -> dict[str, Any]:
    return orchestrator.process_user_paper_order(
        root_task_id=ROOT_TASK_ID,
        trading_task_id=TRADING_TASK_ID,
        interpretation=candidate,
    )


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, 12.0),
        ("8", 8.0),
        ("0", 2.0),
        ("999", 30.0),
        ("nan", 12.0),
        ("not-a-number", 12.0),
    ],
)
def test_paper_order_authority_read_uses_bounded_dedicated_timeout(
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
    expected: float,
) -> None:
    context = _workflow(monkeypatch)
    observed: list[float | None] = []
    if configured is None:
        monkeypatch.delenv("PAPER_ORDER_KANBAN_READ_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("PAPER_ORDER_KANBAN_READ_TIMEOUT_SECONDS", configured)
    # This generic presentation/planning setting must not affect the dedicated
    # PAPER-order authority read.
    monkeypatch.setenv("CEO_PLANNING_READ_TIMEOUT_SECONDS", "0.25")

    def show(task_id: str, *, timeout: float | None = None):
        observed.append(timeout)
        return context.tasks.get(task_id)

    monkeypatch.setattr(orchestrator.hermes_boundary, "show_kanban_task", show)

    task = orchestrator._required_task(
        ROOT_TASK_ID,
        expected_profile="ceo-agent",
    )

    assert task["id"] == ROOT_TASK_ID
    assert observed == [expected]


@pytest.mark.parametrize(
    ("tamper", "expected_code"),
    [
        ("scope", "KANBAN_ORDER_SCOPE_MISMATCH"),
        ("user", "ROOT_USER_MISMATCH"),
        ("hash", "RAW_TEXT_HASH_SCOPE_MISMATCH"),
        ("query", "ROOT_USER_TEXT_MISMATCH"),
        ("workflow_root", "TRADING_WORKFLOW_ROOT_MISMATCH"),
        ("parent", "TRADING_PRIMARY_PARENT_FORBIDDEN"),
        ("task_binding", "TRADING_TASK_BINDING_MISMATCH"),
        ("assignee", "KANBAN_ASSIGNEE_MISMATCH"),
    ],
)
def test_scope_task_user_hash_and_card_tampering_is_rejected_before_submit(
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    expected_code: str,
) -> None:
    context = _workflow(monkeypatch)
    if tamper == "scope":
        context.trading["body"] = context.trading["body"].replace(
            f"book_id={BOOK_ID}", "book_id=99999999-9999-4999-8999-999999999999"
        )
    elif tamper == "user":
        context.root["body"] = context.root["body"].replace(
            f"requested_by={USER_ID}",
            "requested_by=99999999-9999-4999-8999-999999999999",
        )
    elif tamper == "hash":
        original = context.record.raw_instruction_sha256
        context.root["body"] = context.root["body"].replace(original, "0" * 64)
        context.trading["body"] = context.trading["body"].replace(original, "0" * 64)
    elif tamper == "query":
        marker = "\n## User request\n"
        prefix, _query = context.root["body"].split(marker, 1)
        context.root["body"] = f"{prefix}{marker}현대차 매수 10주 시장가"
    elif tamper == "workflow_root":
        context.trading["body"] = context.trading["body"].replace(
            f"workflow_root_task_id={ROOT_TASK_ID}",
            "workflow_root_task_id=t_evil1",
        )
    elif tamper == "parent":
        context.trading["parents"] = [ROOT_TASK_ID]
    elif tamper == "task_binding":
        changed = replace(
            context.repository.get(context.record.order_request_id),
            trading_task_id="t_other1",
        )
        context.repository._records[context.record.order_request_id] = changed
    elif tamper == "assignee":
        context.trading["assignee"] = "research-department"

    submit = Mock()
    monkeypatch.setattr(orchestrator, "submit_verified_paper_directive", submit)
    with pytest.raises(orchestrator.PaperOrderOrchestrationRejected) as raised:
        _process(context, _execute_candidate(context.raw))

    assert raised.value.code == expected_code
    submit.assert_not_called()


@pytest.mark.parametrize(
    "status",
    [
        None,
        "todo",
        "scheduled",
        "blocked",
        "review",
        "archived",
        "failed",
        "cancelled",
    ],
)
def test_non_executable_root_state_is_rejected_before_submit(
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
) -> None:
    context = _workflow(monkeypatch)
    if status is None:
        context.root.pop("status")
        expected_code = "CEO_ROOT_STATUS_MISSING"
    else:
        context.root["status"] = status
        expected_code = "CEO_ROOT_STATE_NOT_EXECUTABLE"
    submit = Mock()
    monkeypatch.setattr(orchestrator, "submit_verified_paper_directive", submit)

    with pytest.raises(orchestrator.PaperOrderOrchestrationRejected) as raised:
        _process(context, _execute_candidate(context.raw))

    assert raised.value.code == expected_code
    submit.assert_not_called()
    failed = context.repository.get(context.record.order_request_id)
    assert failed is not None
    assert failed.state == "FAILED"
    assert failed.error_code == expected_code


@pytest.mark.parametrize(
    "status",
    [
        None,
        "todo",
        "scheduled",
        "ready",
        "blocked",
        "review",
        "done",
        "archived",
        "failed",
        "cancelled",
    ],
)
def test_non_executable_trading_state_cannot_make_a_first_submission(
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
) -> None:
    context = _workflow(monkeypatch)
    if status is None:
        context.trading.pop("status")
        expected_code = "TRADING_TASK_STATUS_MISSING"
    else:
        context.trading["status"] = status
        expected_code = "TRADING_TASK_STATE_NOT_EXECUTABLE"
    submit = Mock()
    monkeypatch.setattr(orchestrator, "submit_verified_paper_directive", submit)

    with pytest.raises(orchestrator.PaperOrderOrchestrationRejected) as raised:
        _process(context, _execute_candidate(context.raw))

    assert raised.value.code == expected_code
    submit.assert_not_called()
    failed = context.repository.get(context.record.order_request_id)
    assert failed is not None
    assert failed.state == "FAILED"
    assert failed.error_code == expected_code


def test_trading_state_is_rechecked_immediately_before_first_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _workflow(monkeypatch)
    trading_reads = 0

    def show(task_id: str, **_kwargs: Any):
        nonlocal trading_reads
        task = context.tasks.get(task_id)
        if task_id != TRADING_TASK_ID or task is None:
            return task
        trading_reads += 1
        return {
            **task,
            "status": "running" if trading_reads == 1 else "blocked",
        }

    monkeypatch.setattr(orchestrator.hermes_boundary, "show_kanban_task", show)
    submit = Mock()
    monkeypatch.setattr(orchestrator, "submit_verified_paper_directive", submit)

    with pytest.raises(orchestrator.PaperOrderOrchestrationRejected) as raised:
        _process(context, _execute_candidate(context.raw))

    assert raised.value.code == "TRADING_TASK_STATE_NOT_EXECUTABLE"
    assert trading_reads == 2
    submit.assert_not_called()
    failed = context.repository.get(context.record.order_request_id)
    assert failed is not None
    assert failed.state == "FAILED"
    assert failed.error_code == "TRADING_TASK_STATE_NOT_EXECUTABLE"


@pytest.mark.parametrize(
    ("raw", "quantity", "expected_decision", "expected_reason"),
    [
        (
            "삼성전자 매수 10주 시장가",
            "11",
            "CLARIFY",
            "CANDIDATE_MISMATCH",
        ),
        (
            "삼성전자 매수 10주 시장가 해도 돼?",
            "10",
            "NOT_ORDER",
            "QUESTION_OR_ADVICE",
        ),
        (
            "삼성전자 매수 10주 시장가 하지 마",
            "10",
            "NOT_ORDER",
            "NEGATED_OR_PROHIBITED",
        ),
        (
            "LIVE 계좌로 삼성전자 매수 10주 시장가",
            "10",
            "CLARIFY",
            "LIVE_MODE_FORBIDDEN",
        ),
    ],
)
def test_candidate_mismatch_question_negation_and_live_never_submit(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    quantity: str,
    expected_decision: str,
    expected_reason: str,
) -> None:
    context = _workflow(monkeypatch, raw)
    submit = Mock()
    monkeypatch.setattr(orchestrator, "submit_verified_paper_directive", submit)

    result = _process(context, _execute_candidate(raw, quantity=quantity))

    assert result["decision"] == expected_decision
    assert expected_reason in result["reason_codes"]
    assert result["binding"] is False
    assert result["directive"] is None
    submit.assert_not_called()


def test_valid_execution_submits_exactly_once_and_replay_only_reads_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _workflow(monkeypatch)
    submitted = _directive_response(context.record)
    completed = _directive_response(context.record, state=DirectiveState.COMPLETED)
    submit = Mock(return_value=submitted)
    read = Mock(return_value=completed)
    monkeypatch.setattr(orchestrator, "submit_verified_paper_directive", submit)
    monkeypatch.setattr(orchestrator, "read_verified_paper_directive_status", read)
    candidate = _execute_candidate(context.raw)

    first = _process(context, candidate)
    # The worker completes its card only after receiving the first durable
    # result. A later exact replay may reconcile that directive, but must never
    # create another one.
    context.root["status"] = "done"
    context.trading["status"] = "done"
    replay = _process(context, candidate)

    submit.assert_called_once_with(
        subject=USER_ID,
        fund_id=FUND_ID,
        book_id=BOOK_ID,
        action="PLACE_ORDER",
        payload={
            "instrument_mention": "삼성전자",
            "side": "BUY",
            "quantity": "10",
            "order_type": "MARKET",
            "time_in_force": "DAY",
            "limit_price": None,
        },
        idempotency_key=f"ceo-paper:{context.record.order_request_id}",
    )
    read.assert_called_once_with(
        subject=USER_ID,
        fund_id=FUND_ID,
        book_id=BOOK_ID,
        directive_id=DIRECTIVE_ID,
    )
    assert first["request_state"] == "IN_PROGRESS"
    assert first["binding"] is True
    assert first["mode"] == "PAPER"
    assert replay["request_state"] == "COMPLETED"
    assert replay["directive"]["directive_id"] == DIRECTIVE_ID
    assert context.repository.get(context.record.order_request_id).state == "COMPLETED"


def test_deterministic_entry_records_distinct_non_hermes_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _workflow(monkeypatch)
    submit = Mock(return_value=_directive_response(context.record))
    monkeypatch.setattr(orchestrator, "submit_verified_paper_directive", submit)
    monkeypatch.setenv("PAPER_ORDER_STATUS_WAIT_SECONDS", "0")

    result = orchestrator.process_deterministic_user_paper_order(
        root_task_id=ROOT_TASK_ID,
        trading_task_id=TRADING_TASK_ID,
        interpretation=_execute_candidate(context.raw),
    )

    assert result["request_state"] == "IN_PROGRESS"
    assert (context.record.order_request_id, "DETERMINISTIC") in (
        context.repository._interpretations
    )
    assert (context.record.order_request_id, "HERMES") not in (
        context.repository._interpretations
    )
    submit.assert_called_once()


def test_direct_order_waits_read_only_and_reports_broker_fill_correlation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _workflow(monkeypatch)
    submitted = _directive_response(context.record)
    completed = _directive_response(context.record, state=DirectiveState.COMPLETED)
    completed = completed.model_copy(
        update={
            "legs": [
                DirectiveLeg.model_validate(
                    {
                        "leg_id": "55555555-5555-4555-8555-555555555555",
                        "leg_index": 0,
                        "symbol": "000660",
                        "side": "SELL",
                        "order_type": "MARKET",
                        "requested_quantity": "2",
                        "filled_quantity": "2",
                        "average_fill_price": "1681500.00",
                        "state": "FILLED",
                        "reduce_only": True,
                        "broker_order_id": "ls-paper:12693",
                        "broker_event_id": "ls-paper:ack:12693",
                    }
                )
            ]
        }
    )
    submit = Mock(return_value=submitted)
    read = Mock(return_value=completed)
    monkeypatch.setattr(orchestrator, "submit_verified_paper_directive", submit)
    monkeypatch.setattr(orchestrator, "read_verified_paper_directive_status", read)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PAPER_ORDER_STATUS_WAIT_SECONDS", "0.1")
    monkeypatch.setattr(orchestrator, "_STATUS_POLL_SECONDS", 0.001)

    result = _process(context, _execute_candidate(context.raw))

    submit.assert_called_once()
    read.assert_called_once()
    assert result["request_state"] == "COMPLETED"
    assert result["correlation"]["client_request_id"] == "request-200"
    assert result["correlation"]["legs"][0]["broker_order_no"] == "12693"
    assert "000660 매도 시장가(가격 미지정) 요청 2주/체결 2주" in result["user_message"]
    assert "평균 체결가 1,681,500원" in result["user_message"]
    assert "LS 주문번호 12693" in result["user_message"]
    events = context.repository.events_for(context.record.order_request_id)
    snapshots = [
        event for event in events
        if event["event_type"] == "BROKER_EXECUTION_SNAPSHOT"
    ]
    assert len(snapshots) == 2
    assert snapshots[-1]["payload"]["legs"][0]["filled_quantity"] == "2"


def test_discord_message_normalizes_database_decimal_quantities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _workflow(monkeypatch)
    completed = _directive_response(context.record, state=DirectiveState.COMPLETED)
    completed = completed.model_copy(
        update={
            "legs": [
                DirectiveLeg.model_validate(
                    {
                        "leg_id": "55555555-5555-4555-8555-555555555555",
                        "leg_index": 0,
                        "symbol": "005930",
                        "side": "BUY",
                        "order_type": "MARKET",
                        "requested_quantity": "3.0000000000",
                        "filled_quantity": "3.0000000000",
                        "average_fill_price": "271000.0000000000",
                        "state": "FILLED",
                        "reduce_only": False,
                        "broker_order_id": "ls-paper:17566",
                    }
                )
            ]
        }
    )
    monkeypatch.setattr(
        orchestrator, "submit_verified_paper_directive", Mock(return_value=completed)
    )
    monkeypatch.setenv("PAPER_ORDER_STATUS_WAIT_SECONDS", "0")

    result = _process(context, _execute_candidate(context.raw))

    assert "요청 3주/체결 3주" in result["user_message"]
    assert "3.0000000000주" not in result["user_message"]


def test_changed_interpretation_replay_conflicts_even_after_directive_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _workflow(monkeypatch)
    submit = Mock(return_value=_directive_response(context.record))
    read = Mock()
    monkeypatch.setattr(orchestrator, "submit_verified_paper_directive", submit)
    monkeypatch.setattr(orchestrator, "read_verified_paper_directive_status", read)
    _process(context, _execute_candidate(context.raw))

    with pytest.raises(orchestrator.PaperOrderOrchestrationRejected) as raised:
        _process(context, _execute_candidate(context.raw, quantity="11"))

    assert raised.value.code == "INTERPRETATION_REPLAY_CONFLICT"
    submit.assert_called_once()
    read.assert_not_called()


def test_transport_unknown_is_persisted_and_never_auto_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _workflow(monkeypatch)
    submit = Mock(
        side_effect=HTTPException(status_code=503, detail="trading_api_unavailable")
    )
    monkeypatch.setattr(orchestrator, "submit_verified_paper_directive", submit)
    candidate = _execute_candidate(context.raw)

    first = _process(context, candidate)
    context.root["status"] = "done"
    context.trading["status"] = "done"
    replay = _process(context, candidate)

    assert first["decision"] == "UNKNOWN"
    assert replay["decision"] == "UNKNOWN"
    assert replay["reason_codes"] == ["SUBMISSION_COMMIT_STATUS_UNKNOWN"]
    submit.assert_called_once()
    record = context.repository.get(context.record.order_request_id)
    assert record.state == "UNKNOWN"
    assert record.directive_id is None


def test_transport_unknown_recovers_exact_committed_directive_without_resubmit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _workflow(monkeypatch)
    submit = Mock(
        side_effect=HTTPException(status_code=503, detail="trading_api_unavailable")
    )
    read = Mock(
        return_value=_directive_response(
            context.record,
            state=DirectiveState.COMPLETED,
        )
    )
    monkeypatch.setattr(orchestrator, "submit_verified_paper_directive", submit)
    monkeypatch.setattr(orchestrator, "read_verified_paper_directive_status", read)
    candidate = _execute_candidate(context.raw)
    context.repository.find_committed_directive = Mock(  # type: ignore[method-assign]
        return_value=DIRECTIVE_ID
    )

    recovered = _process(context, candidate)

    assert recovered["request_state"] == "COMPLETED"
    assert recovered["directive"]["directive_id"] == DIRECTIVE_ID
    submit.assert_called_once()
    read.assert_called_once()
    record = context.repository.get(context.record.order_request_id)
    assert record.state == "COMPLETED"
    assert record.directive_id == DIRECTIVE_ID


def test_closed_market_reports_explicit_non_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _workflow(monkeypatch)
    submit = Mock(
        side_effect=HTTPException(
            status_code=409,
            detail="trading_market_session_closed",
        )
    )
    monkeypatch.setattr(orchestrator, "submit_verified_paper_directive", submit)

    result = _process(context, _execute_candidate(context.raw))

    assert result["decision"] == "REJECTED"
    assert result["binding"] is False
    assert result["order_submitted"] is False
    assert result["directive"] is None
    assert result["reason_codes"] == ["trading_market_session_closed"]
    assert result["user_message"] == (
        "\ud604\uc7ac KRX \uc815\uaddc\uc7a5\uc774 \uc5f4\ub824 "
        "\uc788\uc9c0 \uc54a\uc544 PAPER \uc8fc\ubb38\uc744 "
        "\uc81c\ucd9c\ud558\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4. "
        "\uc8fc\ubb38\u00b7\uccb4\uacb0\u00b7\uc6d0\uc7a5 "
        "\ubc18\uc601\uc740 \uc5c6\uc2b5\ub2c8\ub2e4."
    )
    submit.assert_called_once()


def test_accounting_pending_is_not_reported_as_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _workflow(monkeypatch)
    pending = _directive_response(
        context.record,
        state=DirectiveState.IN_PROGRESS,
        error_code="TRADING_FILL_ACCOUNTING_PENDING",
    )
    submit = Mock(return_value=pending)
    monkeypatch.setattr(orchestrator, "submit_verified_paper_directive", submit)

    result = _process(context, _execute_candidate(context.raw))

    assert result["request_state"] == "ACCOUNTING_PENDING"
    assert result["directive"]["state"] == "IN_PROGRESS"
    assert result["directive"]["error_code"] == "TRADING_FILL_ACCOUNTING_PENDING"
    record = context.repository.get(context.record.order_request_id)
    assert record.state == "ACCOUNTING_PENDING"
    assert record.completed_at is None
