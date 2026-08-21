"""Trusted Trading-Hermes bridge for immediately active PAPER rules.

The CEO/BFF admits the authenticated user/Fund/Book tuple before Hermes sees
the text.  This module reloads that durable admission and both scoped Kanban
cards, treats the Hermes AST as untrusted, runs the existing schema/semantic
preview, and activates the exact validated fingerprint.  It never grants LIVE
authority and never invokes the strategy Risk/QA workflow.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from orchestration.canonical_profiles import canonical_profile_for_department
from orchestration.conditional_rules import RuleSemanticError, RuleState

try:
    from .conditional_rules import (
        ConditionalRuleCandidate,
        ConditionalRulePreviewRequest,
        _build_preview,
    )
    from .conditional_rule_workflow import (
        ConditionalRuleConflict,
        ConditionalRuleUnavailable,
        conditional_rule_repository,
    )
    from .user_order_orchestrator import (
        PaperOrderOrchestrationRejected,
        _required_task,
        _scope_from_task,
        _task_status,
        _validate_workflow_authority,
    )
    from .user_order_workflow import (
        UserOrderRequestConflict,
        UserOrderRequestStateError,
        canonical_payload_sha256,
        user_order_repository,
    )
except ImportError:  # pragma: no cover - direct module execution compatibility
    from conditional_rules import (  # type: ignore[no-redef]
        ConditionalRuleCandidate,
        ConditionalRulePreviewRequest,
        _build_preview,
    )
    from conditional_rule_workflow import (  # type: ignore[no-redef]
        ConditionalRuleConflict,
        ConditionalRuleUnavailable,
        conditional_rule_repository,
    )
    from user_order_orchestrator import (  # type: ignore[no-redef]
        PaperOrderOrchestrationRejected,
        _required_task,
        _scope_from_task,
        _task_status,
        _validate_workflow_authority,
    )
    from user_order_workflow import (  # type: ignore[no-redef]
        UserOrderRequestConflict,
        UserOrderRequestStateError,
        canonical_payload_sha256,
        user_order_repository,
    )


RESULT_SCHEMA_VERSION = "conditional-paper-rule-orchestration.v1"
_ROOT_EXECUTABLE = frozenset({"ready", "running", "done"})
_TRADING_NEW = frozenset({"running"})
_TRADING_REPLAY = frozenset({"done"})


def _reject(code: str) -> None:
    raise PaperOrderOrchestrationRejected(code)


def _validate_task_states(
    *, root: Mapping[str, object], trading: Mapping[str, object], terminal: bool
) -> None:
    if (
        _task_status(root, rejection_code="CEO_ROOT_STATUS_MISSING")
        not in _ROOT_EXECUTABLE
    ):
        _reject("CEO_ROOT_STATE_NOT_EXECUTABLE")
    trading_status = _task_status(
        trading, rejection_code="TRADING_TASK_STATUS_MISSING"
    )
    if trading_status in _TRADING_NEW:
        return
    if terminal and trading_status in _TRADING_REPLAY:
        return
    _reject("TRADING_TASK_STATE_NOT_EXECUTABLE")


def _clarification_message(codes: tuple[str, ...]) -> str:
    labels = {
        "AMBIGUOUS_POSITION_PERCENT": "매도 비중의 기준(보유수량 기준)을 명시해 주세요",
        "AMBIGUOUS_RETURN_BASELINE": "상승·하락률의 기준(평균 매입가 등)을 명시해 주세요",
        "TIMEFRAME_NOT_IN_INSTRUCTION": "지표의 봉 주기를 명시해 주세요",
    }
    details = "; ".join(labels.get(code, code) for code in codes)
    return (
        "조건주문을 활성화하지 않았습니다. "
        + (details or "조건을 한 가지 의미로 확정할 수 없습니다")
        + ". 주문·체결·원장 반영은 없습니다."
    )


def _active_result(record: Any) -> dict[str, Any]:
    spec = record.spec
    sizing = spec.action.sizing
    sizing_text = (
        "전량"
        if sizing.type.value == "ALL"
        else f"{sizing.value} ({sizing.type.value})"
    )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "binding": True,
        "mode": "PAPER",
        "rule_active": True,
        "rule_id": record.rule_id,
        "state": record.state.value,
        "spec_sha256": record.spec_sha256,
        "summary": {
            "symbol": spec.symbol,
            "side": spec.action.side.value,
            "sizing_type": sizing.type.value,
            "sizing_value": str(sizing.value) if sizing.value is not None else None,
            "order_type": spec.action.order_type,
            "limit_price": (
                str(spec.action.limit_price)
                if spec.action.limit_price is not None
                else None
            ),
            "evaluation_clock": spec.evaluation.clock.value,
            "primary_timeframe": (
                spec.evaluation.primary_timeframe.value
                if spec.evaluation.primary_timeframe
                else None
            ),
            "expires_at": spec.expires_at.isoformat(),
            "repeat_policy": "ONCE",
        },
        "user_message": (
            "조건주문이 PAPER 모드로 즉시 활성화되었습니다. "
            f"종목 {spec.symbol}, {spec.action.side.value}, 수량 {sizing_text}, "
            f"주문유형 {spec.action.order_type}"
            + (
                f" {spec.action.limit_price}원"
                if spec.action.limit_price is not None
                else ""
            )
            + ", "
            "1회 실행 규칙입니다. 조건 충족 시 deterministic guard를 통과한 "
            "경우에만 PAPER OMS로 제출됩니다."
        ),
    }


def process_user_conditional_paper_rule(
    *,
    root_task_id: str,
    trading_task_id: str,
    candidate: ConditionalRuleCandidate | None,
    clarification_reason: str | None = None,
) -> dict[str, Any]:
    """Validate one Hermes AST and immediately activate the exact PAPER rule."""

    root = _required_task(
        root_task_id, expected_profile=canonical_profile_for_department("ceo")
    )
    trading = _required_task(
        trading_task_id,
        expected_profile=canonical_profile_for_department("trading"),
    )
    scope = _scope_from_task(root)
    orders = user_order_repository()
    admission = orders.get(scope.order_request_id)
    if admission is None:
        _reject("ORDER_REQUEST_NOT_FOUND")
    _validate_workflow_authority(
        root_task_id=root_task_id,
        trading_task_id=trading_task_id,
        record=admission,
        root=root,
        trading=trading,
    )
    _validate_task_states(
        root=root,
        trading=trading,
        terminal=admission.state in {"COMPLETED", "CLARIFICATION_REQUIRED", "FAILED"},
    )

    if candidate is None:
        reason = str(clarification_reason or "CONDITIONAL_RULE_AST_REQUIRED")[:500]
        orders.mark_outcome(
            admission.order_request_id,
            state="CLARIFICATION_REQUIRED",
            clarification_code=reason,
        )
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "binding": False,
            "mode": "PAPER",
            "rule_active": False,
            "reason_codes": [reason],
            "user_message": _clarification_message((reason,)),
        }

    interpretation = candidate.model_dump(mode="json", exclude_none=True)
    interpretation_digest = canonical_payload_sha256(interpretation)
    try:
        admission = orders.record_interpretation(
            admission.order_request_id,
            trading_task_id=trading_task_id,
            interpretation=interpretation,
            interpretation_sha256=interpretation_digest,
            source="HERMES",
        )
    except (UserOrderRequestConflict, UserOrderRequestStateError) as exc:
        raise PaperOrderOrchestrationRejected(
            "CONDITIONAL_INTERPRETATION_REPLAY_CONFLICT"
        ) from exc

    request = ConditionalRulePreviewRequest(
        fund_id=admission.fund_id,
        book_id=admission.book_id,
        raw_instruction=admission.raw_instruction,
        candidate=candidate,
    )
    try:
        # Anchor default expiry to durable admission time. Replaying the exact
        # Hermes AST must produce the same fingerprint instead of drifting by
        # a few seconds and colliding with the original client request ID.
        preview = _build_preview(
            request,
            subject=admission.user_id,
            now=admission.created_at,
        )
    except (HTTPException, RuleSemanticError, ValueError) as exc:
        detail = (
            getattr(exc, "detail", None)
            or getattr(exc, "code", None)
            or type(exc).__name__
        )
        code = str(detail)[:500]
        orders.mark_outcome(
            admission.order_request_id,
            state="CLARIFICATION_REQUIRED",
            clarification_code=code,
        )
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "binding": False,
            "mode": "PAPER",
            "rule_active": False,
            "reason_codes": [code],
            "user_message": _clarification_message((code,)),
        }

    if not preview.activatable:
        orders.mark_outcome(
            admission.order_request_id,
            state="CLARIFICATION_REQUIRED",
            clarification_code=",".join(preview.clarification_codes),
        )
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "binding": False,
            "mode": "PAPER",
            "rule_active": False,
            "reason_codes": list(preview.clarification_codes),
            "assumptions": list(preview.assumptions),
            "user_message": _clarification_message(preview.clarification_codes),
        }

    try:
        rules = conditional_rule_repository()
        rule = rules.create_pending(
            spec=preview.spec,
            raw_instruction=admission.raw_instruction,
            client_request_id=admission.client_request_id,
            parser_source="HERMES",
        )
        if rule.state is RuleState.PENDING_CONFIRMATION:
            rule = rules.activate(
                rule.rule_id,
                user_id=admission.user_id,
                confirmation_sha256=rule.spec_sha256,
            )
        elif rule.state is not RuleState.ACTIVE:
            raise ConditionalRuleConflict(
                f"conditional rule is not immediately activatable from {rule.state.value}"
            )
    except (ConditionalRuleUnavailable, ConditionalRuleConflict) as exc:
        orders.mark_outcome(
            admission.order_request_id,
            state="FAILED",
            error_code=type(exc).__name__,
            error_message="conditional PAPER rule activation failed closed",
        )
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "binding": False,
            "mode": "PAPER",
            "rule_active": False,
            "reason_codes": [type(exc).__name__],
            "user_message": (
                "조건주문 저장소 검증을 완료하지 못해 활성화하지 않았습니다. "
                "주문·체결·원장 반영은 없습니다."
            ),
        }

    payload = {
        "kind": "CONDITIONAL_PAPER_RULE",
        "rule_id": rule.rule_id,
        "spec": rule.spec.model_dump(mode="json", exclude_none=True),
    }
    orders.mark_outcome(
        admission.order_request_id,
        state="COMPLETED",
        canonical_payload=payload,
        payload_sha256=canonical_payload_sha256(payload),
    )
    return _active_result(rule)


__all__ = [
    "RESULT_SCHEMA_VERSION",
    "process_user_conditional_paper_rule",
]
