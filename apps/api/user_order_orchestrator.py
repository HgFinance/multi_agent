"""Trusted bridge from a Trading Hermes interpretation to the PAPER OMS.

Hermes is intentionally non-authoritative.  This module reloads the exact
browser-admitted request, validates both Kanban cards and their immutable
scope, deterministically verifies every text-evidence span, rechecks current
Fund/Book access, and only then calls the existing payload-bound PAPER gate.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException

from orchestration.canonical_profiles import canonical_profile_for_department
from orchestration.ceo_workflow_scope import (
    UserPaperOrderScope,
    WorkflowScopeViolation,
    requested_by_from_body,
    user_paper_order_scope_from_body,
    workflow_role_from_body,
    workflow_root_from_body,
)
from orchestration.contracts.user_paper_order import (
    CandidateDecision,
    NotOrder,
    OrderClarification,
    VerifiedPaperDirective,
)
from orchestration.user_order_language import verify_order_candidate

try:
    from . import hermes_boundary
    from .ceo_kanban_read import extract_user_query
    from .user_order_workflow import (
        UserOrderRequestConflict,
        UserOrderRequestRecord,
        UserOrderRequestStateError,
        canonical_payload_sha256,
        directive_execution_event_payload,
        raw_instruction_sha256,
        recover_committed_directive,
        user_order_repository,
    )
    from .user_orders import (
        DirectiveState,
        UserDirectiveResponse,
        read_verified_paper_directive_status,
        submit_verified_paper_directive,
    )
except ImportError:  # pragma: no cover - direct module execution compatibility
    import hermes_boundary  # type: ignore[no-redef]
    from ceo_kanban_read import extract_user_query  # type: ignore[no-redef]
    from user_order_workflow import (  # type: ignore[no-redef]
        UserOrderRequestConflict,
        UserOrderRequestRecord,
        UserOrderRequestStateError,
        canonical_payload_sha256,
        directive_execution_event_payload,
        raw_instruction_sha256,
        recover_committed_directive,
        user_order_repository,
    )
    from user_orders import (  # type: ignore[no-redef]
        DirectiveState,
        UserDirectiveResponse,
        read_verified_paper_directive_status,
        submit_verified_paper_directive,
    )


RESULT_SCHEMA_VERSION = "user-paper-order-orchestration.v1"
logger = logging.getLogger(__name__)
_TASK_ID_RE = re.compile(r"^t_[A-Za-z0-9]{4,64}$")
_NO_DIRECTIVE_TERMINAL_STATES = frozenset({"FAILED", "REJECTED"})
_ROOT_EXECUTION_STATUSES = frozenset({"ready", "running", "done"})
_TRADING_EXECUTION_STATUSES = frozenset({"running"})
_TRADING_REPLAY_STATUSES = frozenset({"done"})
_EXECUTION_STATE_REJECTION_CODES = frozenset(
    {
        "CEO_ROOT_STATUS_MISSING",
        "CEO_ROOT_STATE_NOT_EXECUTABLE",
        "TRADING_TASK_STATUS_MISSING",
        "TRADING_TASK_STATE_NOT_EXECUTABLE",
    }
)
_NO_DIRECTIVE_REPLAY_STATES = frozenset(
    {"CLARIFICATION_REQUIRED", "NOT_ORDER", "REJECTED", "FAILED", "UNKNOWN"}
)
_DEFAULT_KANBAN_READ_TIMEOUT_SECONDS = 12.0
_MIN_KANBAN_READ_TIMEOUT_SECONDS = 2.0
_MAX_KANBAN_READ_TIMEOUT_SECONDS = 30.0
_MAX_STATUS_WAIT_SECONDS = 15.0
_STATUS_POLL_SECONDS = 0.5

_MARKET_SESSION_CLOSED_MESSAGE = (
    "\ud604\uc7ac KRX \uc815\uaddc\uc7a5\uc774 \uc5f4\ub824 "
    "\uc788\uc9c0 \uc54a\uc544 PAPER \uc8fc\ubb38\uc744 "
    "\uc81c\ucd9c\ud558\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4. "
    "\uc8fc\ubb38\u00b7\uccb4\uacb0\u00b7\uc6d0\uc7a5 "
    "\ubc18\uc601\uc740 \uc5c6\uc2b5\ub2c8\ub2e4."
)


class PaperOrderOrchestrationRejected(ValueError):
    """The supplied Kanban/DB authority scope did not match exactly."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _reject(code: str) -> None:
    raise PaperOrderOrchestrationRejected(code)


def _paper_order_kanban_read_timeout_seconds() -> float:
    """Return the bounded timeout for PAPER-order authority reads only."""

    raw = os.getenv(
        "PAPER_ORDER_KANBAN_READ_TIMEOUT_SECONDS",
        str(_DEFAULT_KANBAN_READ_TIMEOUT_SECONDS),
    )
    try:
        configured = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_KANBAN_READ_TIMEOUT_SECONDS
    if not math.isfinite(configured):
        return _DEFAULT_KANBAN_READ_TIMEOUT_SECONDS
    return min(
        _MAX_KANBAN_READ_TIMEOUT_SECONDS,
        max(_MIN_KANBAN_READ_TIMEOUT_SECONDS, configured),
    )


def _paper_order_status_wait_seconds() -> float:
    """Bound synchronous Discord tracking without widening order authority."""

    default = (
        "8" if os.getenv("APP_ENV", "").strip().lower() == "production" else "0"
    )
    try:
        configured = float(os.getenv("PAPER_ORDER_STATUS_WAIT_SECONDS", default))
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(configured):
        return float(default)
    return min(_MAX_STATUS_WAIT_SECONDS, max(0.0, configured))


def _required_task(task_id: str, *, expected_profile: str) -> dict[str, object]:
    if not _TASK_ID_RE.fullmatch(str(task_id or "")):
        _reject("KANBAN_TASK_ID_INVALID")
    task = hermes_boundary.show_kanban_task(
        str(task_id),
        timeout=_paper_order_kanban_read_timeout_seconds(),
    )
    if not task:
        _reject("KANBAN_TASK_NOT_FOUND")
    observed_id = str(task.get("id") or task.get("task_id") or "")
    if observed_id != task_id:
        _reject("KANBAN_TASK_ID_MISMATCH")
    if str(task.get("assignee") or "") != expected_profile:
        _reject("KANBAN_ASSIGNEE_MISMATCH")
    return task


def _task_status(task: Mapping[str, object], *, rejection_code: str) -> str:
    """Return one exact current Hermes task state or reject an incomplete read."""

    raw = task.get("status")
    if not isinstance(raw, str) or not raw.strip():
        _reject(rejection_code)
    return raw.strip().casefold()


def _durable_non_submission_replay(record: UserOrderRequestRecord) -> bool:
    """Whether a completed interpreter can only replay a durable safe result."""

    return bool(record.directive_id or record.state in _NO_DIRECTIVE_REPLAY_STATES)


def _validate_task_execution_states(
    *,
    root: Mapping[str, object],
    trading: Mapping[str, object],
    record: UserOrderRequestRecord,
    new_submission: bool,
) -> None:
    """Fail closed unless both cards are in an observed executable state.

    A CEO root may already be ``done`` because its only responsibility in this
    lane is to confirm the pre-created Trading primary.  A new directive,
    however, is accepted only while that Trading primary is actually
    ``running``.  ``done`` is retained solely for an exact replay whose durable
    request row proves that no first submission can occur.
    """

    root_status = _task_status(root, rejection_code="CEO_ROOT_STATUS_MISSING")
    if root_status not in _ROOT_EXECUTION_STATUSES:
        _reject("CEO_ROOT_STATE_NOT_EXECUTABLE")

    trading_status = _task_status(trading, rejection_code="TRADING_TASK_STATUS_MISSING")
    if trading_status in _TRADING_EXECUTION_STATUSES:
        return
    if (
        not new_submission
        and trading_status in _TRADING_REPLAY_STATUSES
        and _durable_non_submission_replay(record)
    ):
        return
    _reject("TRADING_TASK_STATE_NOT_EXECUTABLE")


def _record_execution_state_rejection(
    repository: Any,
    record: UserOrderRequestRecord,
    exc: PaperOrderOrchestrationRejected,
) -> None:
    """Close an authenticated, never-submitted request instead of stranding it.

    This helper is called only after the immutable Kanban/DB authority scope
    matched. It must never overwrite an existing directive or a durable
    terminal non-submission replay.
    """

    if (
        exc.code not in _EXECUTION_STATE_REJECTION_CODES
        or record.directive_id
        or record.state in _NO_DIRECTIVE_REPLAY_STATES
    ):
        return
    try:
        repository.mark_outcome(
            record.order_request_id,
            state="FAILED",
            error_code=exc.code,
            error_message="Kanban execution state rejected before PAPER submission",
        )
    except (UserOrderRequestConflict, UserOrderRequestStateError):
        # Preserve the original fail-closed rejection if a concurrent terminal
        # transition won the durable state race.
        return


def _scope_from_task(task: Mapping[str, object]) -> UserPaperOrderScope:
    try:
        scope = user_paper_order_scope_from_body(str(task.get("body") or ""))
    except WorkflowScopeViolation:
        _reject("KANBAN_ORDER_SCOPE_INVALID")
    if scope is None:
        _reject("KANBAN_ORDER_SCOPE_MISSING")
    return scope


def _parents(task: Mapping[str, object]) -> tuple[str, ...]:
    value = task.get("parents")
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            identifier = item.get("id") or item.get("task_id")
        else:
            identifier = item
        if identifier:
            result.append(str(identifier))
    return tuple(result)


def _validate_workflow_authority(
    *,
    root_task_id: str,
    trading_task_id: str,
    record: UserOrderRequestRecord,
    root: Mapping[str, object],
    trading: Mapping[str, object],
) -> UserPaperOrderScope:
    root_body = str(root.get("body") or "")
    trading_body = str(trading.get("body") or "")
    root_scope = _scope_from_task(root)
    trading_scope = _scope_from_task(trading)
    if trading_scope != root_scope:
        _reject("KANBAN_ORDER_SCOPE_MISMATCH")
    if root_scope.mode != "PAPER" or record.mode != "PAPER":
        _reject("ORDER_MODE_NOT_PAPER")
    if record.order_request_id != root_scope.order_request_id:
        _reject("ORDER_REQUEST_ID_MISMATCH")
    if record.ceo_root_task_id != root_task_id:
        _reject("CEO_ROOT_BINDING_MISMATCH")
    if record.trading_task_id != trading_task_id:
        _reject("TRADING_TASK_BINDING_MISMATCH")
    if (record.fund_id, record.book_id) != (
        root_scope.fund_id,
        root_scope.book_id,
    ):
        _reject("FUND_BOOK_SCOPE_MISMATCH")
    if record.raw_instruction_sha256 != root_scope.raw_instruction_sha256:
        _reject("RAW_TEXT_HASH_SCOPE_MISMATCH")
    if raw_instruction_sha256(record.raw_instruction) != root_scope.raw_instruction_sha256:
        _reject("RAW_TEXT_HASH_INVALID")
    if extract_user_query(root_body) != record.raw_instruction:
        _reject("ROOT_USER_TEXT_MISMATCH")
    if requested_by_from_body(root_body) != record.user_id:
        _reject("ROOT_USER_MISMATCH")
    if workflow_root_from_body(trading_body) != root_task_id:
        _reject("TRADING_WORKFLOW_ROOT_MISMATCH")
    if workflow_role_from_body(trading_body) != "primary":
        _reject("TRADING_WORKFLOW_ROLE_MISMATCH")
    # Primary cards are scope-linked through their body and deliberately have
    # no execution-parent edge. Any parent here is an unexpected authority
    # path and is rejected.
    if _parents(trading):
        _reject("TRADING_PRIMARY_PARENT_FORBIDDEN")
    return root_scope


def _request_state(response: UserDirectiveResponse) -> str:
    if response.state is DirectiveState.COMPLETED:
        return "COMPLETED"
    if response.state is DirectiveState.FAILED:
        return "FAILED"
    if response.state is DirectiveState.UNKNOWN:
        return "UNKNOWN"
    if response.error_code == "TRADING_FILL_ACCOUNTING_PENDING":
        return "ACCOUNTING_PENDING"
    return "IN_PROGRESS"


def _directive_result(
    *,
    record: UserOrderRequestRecord,
    response: UserDirectiveResponse,
) -> dict[str, Any]:
    correlation = directive_execution_event_payload(record, response)
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "decision": CandidateDecision.EXECUTE.value,
        "mode": "PAPER",
        "binding": True,
        "order_submitted": True,
        "order_request_id": record.order_request_id,
        "request_state": record.state,
        "directive": response.model_dump(mode="json"),
        "correlation": correlation,
        "user_message": _directive_user_message(record=record, response=response),
    }


def _raw_broker_order_no(value: str | None) -> str | None:
    broker_order_id = str(value or "").strip()
    if not broker_order_id:
        return None
    if broker_order_id.startswith("ls-paper:"):
        return broker_order_id.split(":", 1)[1] or None
    return broker_order_id


def _format_krw(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    rendered = f"{amount:,f}"
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _directive_user_message(
    *, record: UserOrderRequestRecord, response: UserDirectiveResponse
) -> str:
    """Build the only execution wording Discord may repeat to the user."""

    if response.state is DirectiveState.COMPLETED:
        headline = "PAPER 주문 완료"
    elif response.state is DirectiveState.FAILED:
        headline = "PAPER 주문 실패"
    elif response.state is DirectiveState.UNKNOWN:
        headline = "PAPER 주문 상태 미확정"
    elif response.error_code == "TRADING_FILL_ACCOUNTING_PENDING":
        headline = "PAPER 체결 확인·원장 반영 대기"
    else:
        headline = "PAPER 주문 추적 중"

    leg_messages: list[str] = []
    for leg in response.legs:
        symbol = leg.symbol or "종목 미확인"
        side = {"BUY": "매수", "SELL": "매도"}.get(
            leg.side.value if leg.side else "", "방향 미확인"
        )
        requested = leg.requested_quantity or "?"
        filled = leg.filled_quantity
        if leg.order_type and leg.order_type.value == "LIMIT":
            requested_price = _format_krw(leg.limit_price)
            order_price_text = f"지정가 {requested_price or '미확인'}원"
        else:
            order_price_text = "시장가(가격 미지정)"
        average_fill_price = _format_krw(leg.average_fill_price)
        fill_price_text = (
            f", 평균 체결가 {average_fill_price}원"
            if average_fill_price
            else ""
        )
        broker_order_no = _raw_broker_order_no(leg.broker_order_id)
        broker_text = (
            f", LS 주문번호 {broker_order_no}"
            if broker_order_no
            else ", LS 주문번호 미확인"
        )
        error_text = f", 오류 {leg.error_code}" if leg.error_code else ""
        leg_messages.append(
            f"{symbol} {side} {order_price_text} 요청 {requested}주/체결 {filled}주"
            f"{fill_price_text}"
            f" ({leg.state}{broker_text}{error_text})"
        )

    detail = "; ".join(leg_messages) if leg_messages else "주문 leg 없음"
    suffix = ""
    if response.state is DirectiveState.UNKNOWN:
        suffix = " 제출 성공 여부를 단정할 수 없어 자동 재시도하지 않습니다."
    elif response.state is DirectiveState.FAILED:
        suffix = " 추가 주문은 자동으로 제출하지 않았습니다."
    return (
        f"{headline}: {detail}. 요청 ID {record.order_request_id}, "
        f"지시 ID {response.directive_id}.{suffix}"
    )


def _directive_is_terminal(response: UserDirectiveResponse) -> bool:
    return response.state in {
        DirectiveState.COMPLETED,
        DirectiveState.FAILED,
        DirectiveState.UNKNOWN,
    }


def _await_directive_status(
    *,
    repository: Any,
    record: UserOrderRequestRecord,
    response: UserDirectiveResponse,
) -> tuple[UserOrderRequestRecord, UserDirectiveResponse]:
    """Read status until terminal/accounting completion, never resubmitting."""

    wait_seconds = _paper_order_status_wait_seconds()
    if wait_seconds <= 0 or _directive_is_terminal(response):
        return record, response

    deadline = time.monotonic() + wait_seconds
    latest = response
    latest_digest = canonical_payload_sha256(latest.model_dump(mode="json"))
    while not _directive_is_terminal(latest):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_STATUS_POLL_SECONDS, remaining))
        try:
            observed = read_verified_paper_directive_status(
                subject=record.user_id,
                fund_id=record.fund_id,
                book_id=record.book_id,
                directive_id=str(response.directive_id),
            )
        except HTTPException as exc:
            logger.warning(
                "paper-order-status-read-failed order_request=%s directive=%s detail=%s",
                record.order_request_id,
                response.directive_id,
                str(exc.detail)[:80],
            )
            break
        observed_digest = canonical_payload_sha256(observed.model_dump(mode="json"))
        latest = observed
        if observed_digest == latest_digest:
            continue
        latest_digest = observed_digest
        snapshot = directive_execution_event_payload(record, observed)
        record = repository.mark_outcome(
            record.order_request_id,
            state=_request_state(observed),
            directive_id=str(observed.directive_id),
            error_code=observed.error_code,
            error_message=observed.error_message,
            event_type="BROKER_EXECUTION_SNAPSHOT",
            event_payload=snapshot,
        )
        logger.info(
            "paper-order-status-updated client_request=%s order_request=%s directive=%s state=%s legs=%s",
            record.client_request_id,
            record.order_request_id,
            observed.directive_id,
            observed.state.value,
            len(observed.legs),
        )
    return record, latest


def _non_execution_user_message(reason_codes: list[str]) -> str | None:
    """Return a deterministic Discord-safe explanation for known rejections."""

    if "trading_market_session_closed" in reason_codes:
        return _MARKET_SESSION_CLOSED_MESSAGE
    return None


def _non_execution_result(
    *,
    record: UserOrderRequestRecord,
    decision: str,
    reason_codes: list[str],
) -> dict[str, Any]:
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "decision": decision,
        "mode": "PAPER",
        "binding": False,
        "order_submitted": False,
        "order_request_id": record.order_request_id,
        "request_state": record.state,
        "reason_codes": reason_codes,
        "directive": None,
    }
    user_message = _non_execution_user_message(reason_codes)
    if user_message:
        result["user_message"] = user_message
    return result


def _existing_directive_result(
    record: UserOrderRequestRecord,
) -> dict[str, Any]:
    if not record.directive_id:
        _reject("DIRECTIVE_ID_MISSING")
    response = read_verified_paper_directive_status(
        subject=record.user_id,
        fund_id=record.fund_id,
        book_id=record.book_id,
        directive_id=record.directive_id,
    )
    snapshot = directive_execution_event_payload(record, response)
    repository = user_order_repository()
    updated = repository.mark_outcome(
        record.order_request_id,
        state=_request_state(response),
        directive_id=str(response.directive_id),
        error_code=response.error_code,
        error_message=response.error_message,
        event_type="BROKER_EXECUTION_SNAPSHOT",
        event_payload=snapshot,
    )
    logger.info(
        "paper-order-correlated client_request=%s order_request=%s directive=%s state=%s legs=%s",
        updated.client_request_id,
        updated.order_request_id,
        response.directive_id,
        response.state.value,
        len(response.legs),
    )
    updated, response = _await_directive_status(
        repository=repository,
        record=updated,
        response=response,
    )
    return _directive_result(record=updated, response=response)


def _submission_failure_state(exc: HTTPException) -> tuple[str, str]:
    detail = exc.detail if isinstance(exc.detail, str) else "paper_order_submission_failed"
    # A transport timeout or malformed post-mutation response has uncertain
    # commit status and must never be retried automatically.
    if detail in {"trading_api_unavailable", "trading_api_invalid_response"}:
        return "UNKNOWN", detail
    if exc.status_code in {409, 422} or detail.startswith("portfolio_"):
        return "REJECTED", detail
    return "FAILED", detail


def process_user_paper_order(
    *,
    root_task_id: str,
    trading_task_id: str,
    interpretation: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one Hermes interpretation and, if safe, submit one PAPER directive."""

    root = _required_task(
        root_task_id, expected_profile=canonical_profile_for_department("ceo")
    )
    trading = _required_task(
        trading_task_id,
        expected_profile=canonical_profile_for_department("trading"),
    )
    root_scope = _scope_from_task(root)
    repository = user_order_repository()
    record = repository.get(root_scope.order_request_id)
    if record is None:
        _reject("ORDER_REQUEST_NOT_FOUND")
    _validate_workflow_authority(
        root_task_id=root_task_id,
        trading_task_id=trading_task_id,
        record=record,
        root=root,
        trading=trading,
    )
    try:
        _validate_task_execution_states(
            root=root,
            trading=trading,
            record=record,
            new_submission=False,
        )
    except PaperOrderOrchestrationRejected as exc:
        _record_execution_state_rejection(repository, record, exc)
        raise

    try:
        interpretation_dict = dict(interpretation)
        interpretation_sha256 = canonical_payload_sha256(interpretation_dict)
    except (TypeError, ValueError) as exc:
        _reject("INTERPRETATION_NOT_CANONICAL_JSON")
        raise AssertionError from exc  # pragma: no cover

    verified = verify_order_candidate(record.raw_instruction, interpretation_dict)
    try:
        record = repository.record_interpretation(
            record.order_request_id,
            trading_task_id=trading_task_id,
            interpretation=interpretation_dict,
            interpretation_sha256=interpretation_sha256,
            source="HERMES",
        )
    except (UserOrderRequestConflict, UserOrderRequestStateError) as exc:
        raise PaperOrderOrchestrationRejected("INTERPRETATION_REPLAY_CONFLICT") from exc

    if record.state == "UNKNOWN" and not record.directive_id:
        record = recover_committed_directive(repository, record)
    if record.directive_id:
        return _existing_directive_result(record)
    if record.state == "UNKNOWN":
        return _non_execution_result(
            record=record,
            decision="UNKNOWN",
            reason_codes=["SUBMISSION_COMMIT_STATUS_UNKNOWN"],
        )
    if record.state in _NO_DIRECTIVE_TERMINAL_STATES:
        return _non_execution_result(
            record=record,
            decision=record.state,
            reason_codes=[record.error_code or "ORDER_REQUEST_TERMINAL"],
        )

    if isinstance(verified, OrderClarification):
        reasons = [reason.value for reason in verified.reason_codes]
        record = repository.mark_outcome(
            record.order_request_id,
            state="CLARIFICATION_REQUIRED",
            clarification_code=",".join(reasons),
        )
        return _non_execution_result(
            record=record,
            decision=verified.decision.value,
            reason_codes=reasons,
        )
    if isinstance(verified, NotOrder):
        reasons = [reason.value for reason in verified.reason_codes]
        record = repository.mark_outcome(
            record.order_request_id,
            state="NOT_ORDER",
            clarification_code=",".join(reasons),
        )
        return _non_execution_result(
            record=record,
            decision=verified.decision.value,
            reason_codes=reasons,
        )
    if not isinstance(verified, VerifiedPaperDirective):  # pragma: no cover
        _reject("VERIFIER_RESULT_INVALID")

    canonical_payload = verified.canonical_payload()
    canonical_digest = canonical_payload_sha256(canonical_payload)
    idempotency_key = f"ceo-paper:{record.order_request_id}"

    # Re-read immediately before the only mutating boundary.  This narrows the
    # Kanban/SQL cross-store race and, critically, prevents a completed replay
    # from becoming a first submission after verifier behavior changes.
    current_root = _required_task(
        root_task_id, expected_profile=canonical_profile_for_department("ceo")
    )
    current_trading = _required_task(
        trading_task_id,
        expected_profile=canonical_profile_for_department("trading"),
    )
    _validate_workflow_authority(
        root_task_id=root_task_id,
        trading_task_id=trading_task_id,
        record=record,
        root=current_root,
        trading=current_trading,
    )
    try:
        _validate_task_execution_states(
            root=current_root,
            trading=current_trading,
            record=record,
            new_submission=True,
        )
    except PaperOrderOrchestrationRejected as exc:
        _record_execution_state_rejection(repository, record, exc)
        raise
    try:
        response = submit_verified_paper_directive(
            subject=record.user_id,
            fund_id=record.fund_id,
            book_id=record.book_id,
            action=verified.action.value,
            payload=canonical_payload,
            idempotency_key=idempotency_key,
        )
    except HTTPException as exc:
        state, code = _submission_failure_state(exc)
        record = repository.mark_outcome(
            record.order_request_id,
            state=state,
            action=verified.action.value,
            canonical_payload=canonical_payload,
            payload_sha256=canonical_digest,
            error_code=code,
            error_message="PAPER directive submission did not complete safely",
        )
        return _non_execution_result(
            record=record,
            decision=state,
            reason_codes=[code],
        )

    snapshot = directive_execution_event_payload(record, response)
    record = repository.mark_outcome(
        record.order_request_id,
        state=_request_state(response),
        action=verified.action.value,
        canonical_payload=canonical_payload,
        payload_sha256=canonical_digest,
        directive_id=str(response.directive_id),
        error_code=response.error_code,
        error_message=response.error_message,
        event_type="BROKER_EXECUTION_SNAPSHOT",
        event_payload=snapshot,
    )
    logger.info(
        "paper-order-correlated client_request=%s order_request=%s directive=%s state=%s legs=%s",
        record.client_request_id,
        record.order_request_id,
        response.directive_id,
        response.state.value,
        len(response.legs),
    )
    record, response = _await_directive_status(
        repository=repository,
        record=record,
        response=response,
    )
    return _directive_result(record=record, response=response)


__all__ = [
    "PaperOrderOrchestrationRejected",
    "RESULT_SCHEMA_VERSION",
    "process_user_paper_order",
]
