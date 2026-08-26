"""Trusted Trading-Hermes bridge for immediately active PAPER rules.

The CEO/BFF admits the fixed local user/Fund/Book tuple before Hermes sees the
text. This module reloads that durable admission and both scoped Kanban
cards, treats the Hermes AST as untrusted, runs the existing schema/semantic
preview, and activates the exact validated fingerprint.  It never grants LIVE
authority and never invokes the strategy Risk/QA workflow.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import timedelta, timezone
from typing import Any

from fastapi import HTTPException

from orchestration.canonical_profiles import canonical_profile_for_department
from orchestration.conditional_rules import RuleSemanticError, RuleState

try:
    from .conditional_rule_status import build_conditional_execution_status
    from .conditional_rule_workflow import (
        ConditionalRuleConflict,
        ConditionalRuleUnavailable,
        conditional_rule_repository,
    )
    from .conditional_rules import (
        ConditionalRuleCandidate,
        ConditionalRulePreviewRequest,
        _build_preview,
        relative_time_trigger_at,
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
    from .user_orders import (
        _workflow_state_from_directive,
        read_paper_directive_status_for_admitted_authority,
    )
except ImportError:  # pragma: no cover - direct module execution compatibility
    from conditional_rule_status import (
        build_conditional_execution_status,  # type: ignore[no-redef]
    )
    from conditional_rule_workflow import (  # type: ignore[no-redef]
        ConditionalRuleConflict,
        ConditionalRuleUnavailable,
        conditional_rule_repository,
    )
    from conditional_rules import (  # type: ignore[no-redef]
        ConditionalRuleCandidate,
        ConditionalRulePreviewRequest,
        _build_preview,
        relative_time_trigger_at,
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
    from user_orders import (  # type: ignore[no-redef]
        _workflow_state_from_directive,
        read_paper_directive_status_for_admitted_authority,
    )


RESULT_SCHEMA_VERSION = "conditional-paper-rule-orchestration.v1"
_ROOT_EXECUTABLE = frozenset({"ready", "running", "done"})
_TRADING_NEW = frozenset({"blocked", "running"})
_TRADING_REPLAY = frozenset({"done"})
_MAX_RULES_PER_REQUEST = 4


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
    trading_status = _task_status(trading, rejection_code="TRADING_TASK_STATUS_MISSING")
    if trading_status in _TRADING_NEW:
        return
    if terminal and trading_status in _TRADING_REPLAY:
        return
    _reject("TRADING_TASK_STATE_NOT_EXECUTABLE")


def _clarification_message(
    codes: tuple[str, ...], *, raw_instruction: str | None = None
) -> str:
    labels = {
        "AMBIGUOUS_POSITION_PERCENT": "매도 비중의 기준(보유수량 기준)을 명시해 주세요",
        "AMBIGUOUS_RETURN_BASELINE": "상승·하락률의 기준(평균 매입가 등)을 명시해 주세요",
        "TIMEFRAME_NOT_IN_INSTRUCTION": "지표의 봉 주기를 명시해 주세요",
        "TIMEFRAME_3M_UNSUPPORTED": "3분봉 데이터는 지원하지 않습니다. 5분봉으로 수행하려면 5분봉으로 다시 요청해 주세요",
        "QUANTITY_REQUIRED": "매수 수량을 명시해 주세요(예: 1주)",
    }
    details = "; ".join(labels.get(code, code) for code in codes)
    if raw_instruction and "3분봉" in " ".join(raw_instruction.split()):
        details = "3분봉 데이터는 지원하지 않아 5분봉으로 수행합니다" + (
            "; " + details if details else ""
        )
    return (
        "조건주문을 활성화하지 않았습니다. "
        + (details or "조건을 한 가지 의미로 확정할 수 없습니다")
        + ". 주문·체결·원장 반영은 없습니다."
    )


def _active_result(record: Any, *, assumptions: tuple[str, ...] = ()) -> dict[str, Any]:
    spec = record.spec
    sizing = spec.action.sizing
    sizing_text = (
        "전량"
        if sizing.type.value == "ALL"
        else f"{sizing.value} ({sizing.type.value})"
    )
    timeframe_fallback = "TIMEFRAME_FALLBACK_3M_TO_5M" in assumptions
    expiry_kst = spec.expires_at.astimezone(timezone(timedelta(hours=9)))
    trigger_at = relative_time_trigger_at(spec.condition)
    trigger_kst = (
        trigger_at.astimezone(timezone(timedelta(hours=9)))
        if trigger_at is not None
        else None
    )
    user_message = (
        (
            "요청한 3분봉 기능이 없어 5분봉 완성봉 기준으로 대체했습니다. "
            "이 안내는 조건주문 요약에도 기록됩니다. "
        )
        if timeframe_fallback
        else ""
    ) + (
        (
            "PAPER 예약 조건주문이 ACTIVE 전환되었습니다. "
            f"실행 기준 시각은 {trigger_kst:%Y-%m-%d %H:%M:%S} KST이며, "
            "해당 시각 후 5분 안에 최신 시세·장 운영·자금 검증을 "
            "모두 통과한 경우에만 PAPER OMS로 제출됩니다. "
            if trigger_kst is not None
            else "조건주문은 접수 처리 시점에 PAPER 모드 ACTIVE 전환이 완료되었습니다. "
        )
        + "이 문구는 생성 영수증이며 현재 상태 조회 결과가 아닙니다. "
        f"종목 {spec.symbol}, {spec.action.side.value}, 수량 {sizing_text}, "
        f"주문유형 {spec.action.order_type}"
        + (
            f" {spec.action.limit_price}원"
            if spec.action.limit_price is not None
            else ""
        )
        + ", 1회 실행 규칙입니다. 조건 충족 시 deterministic guard를 통과한 "
        "경우에만 PAPER OMS로 제출됩니다. "
        f"추적 만료는 {expiry_kst:%Y-%m-%d %H:%M} KST입니다."
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
            "trigger_at": trigger_at.isoformat() if trigger_at is not None else None,
            **(
                {
                    "timeframe_fallback": {
                        "requested": "3M",
                        "used": "5M",
                        "reason": "3M_UNSUPPORTED",
                    }
                }
                if timeframe_fallback
                else {}
            ),
        },
        "assumptions": list(assumptions),
        "user_message": user_message,
    }


def _candidate_batch(
    *,
    candidate: ConditionalRuleCandidate | None,
    candidates: tuple[ConditionalRuleCandidate, ...] | list[ConditionalRuleCandidate] | None,
) -> tuple[ConditionalRuleCandidate, ...]:
    batch = tuple(candidates or ())
    if candidate is not None:
        if batch:
            _reject("CONDITIONAL_CANDIDATE_ENVELOPE_CONFLICT")
        batch = (candidate,)
    if len(batch) > _MAX_RULES_PER_REQUEST:
        _reject("TOO_MANY_CONDITIONAL_ACTIONS")
    return batch


def _rule_client_request_id(base: str, *, index: int, count: int) -> str:
    if count == 1:
        return base
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:48]
    return f"conditional-set:{digest}:{index + 1}"


def _cancel_created_rules(rules: Any, records: list[Any], *, user_id: str) -> bool:
    """Compensate a partially created batch; return False if state is uncertain."""

    complete = True
    for record in reversed(records):
        if record.state not in {
            RuleState.PENDING_CONFIRMATION,
            RuleState.ACTIVE,
            RuleState.PAUSED,
        }:
            continue
        try:
            rules.transition(record.rule_id, user_id=user_id, target=RuleState.CANCELLED)
        except (ConditionalRuleUnavailable, ConditionalRuleConflict):
            complete = False
    return complete


def _active_batch_result(
    records: list[Any], *, assumptions: list[tuple[str, ...]]
) -> dict[str, Any]:
    if len(records) == 1:
        return _active_result(records[0], assumptions=assumptions[0])
    items = [
        _active_result(record, assumptions=item_assumptions)
        for record, item_assumptions in zip(records, assumptions, strict=True)
    ]
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "binding": True,
        "mode": "PAPER",
        "rule_active": True,
        "rule_ids": [record.rule_id for record in records],
        "state": "ACTIVE",
        "rules": [item["summary"] for item in items],
        "assumptions": [item["assumptions"] for item in items],
        "user_message": (
            f"서로 독립적인 PAPER 조건주문 {len(records)}개는 접수 처리 시점에 "
            "ACTIVE 전환이 완료되었습니다. 이 문구는 생성 영수증이며 현재 상태 "
            "조회 결과가 아닙니다. "
            "각 규칙은 1회만 실행되며, 각 조건 충족 시 deterministic guard를 "
            "통과한 경우에만 PAPER OMS로 제출됩니다. 규칙별 추적 만료 시각은 "
            "요약의 expires_at에 기록했습니다."
        ),
    }


def process_user_conditional_paper_rule(
    *,
    root_task_id: str,
    trading_task_id: str,
    candidate: ConditionalRuleCandidate | None = None,
    candidates: tuple[ConditionalRuleCandidate, ...]
    | list[ConditionalRuleCandidate]
    | None = None,
    clarification_reason: str | None = None,
    interpretation_source: str = "HERMES",
) -> dict[str, Any]:
    """Validate one tool call containing one or more independent PAPER rules."""

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

    batch = _candidate_batch(candidate=candidate, candidates=candidates)
    if not batch:
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
            "user_message": _clarification_message(
                (reason,), raw_instruction=admission.raw_instruction
            ),
        }

    source = str(interpretation_source or "").strip().upper()
    if source not in {"HERMES", "DETERMINISTIC"}:
        _reject("CONDITIONAL_INTERPRETATION_SOURCE_INVALID")
    interpretation = {
        "schema_version": "conditional-rule-candidate-set.v1",
        "candidates": [
            item.model_dump(mode="json", exclude_none=True) for item in batch
        ],
    }
    interpretation_digest = canonical_payload_sha256(interpretation)
    try:
        admission = orders.record_interpretation(
            admission.order_request_id,
            trading_task_id=trading_task_id,
            interpretation=interpretation,
            interpretation_sha256=interpretation_digest,
            source=source,
        )
    except (UserOrderRequestConflict, UserOrderRequestStateError) as exc:
        raise PaperOrderOrchestrationRejected(
            "CONDITIONAL_INTERPRETATION_REPLAY_CONFLICT"
        ) from exc

    try:
        # Anchor default expiry to durable admission time. Replaying the exact
        # Hermes AST must produce the same fingerprint instead of drifting by
        # a few seconds and colliding with the original client request ID.
        previews = [
            _build_preview(
                ConditionalRulePreviewRequest(
                    fund_id=admission.fund_id,
                    book_id=admission.book_id,
                    raw_instruction=admission.raw_instruction,
                    candidate=item,
                ),
                subject=admission.user_id,
                now=admission.created_at,
            )
            for item in batch
        ]
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
            "user_message": _clarification_message(
                (code,), raw_instruction=admission.raw_instruction
            ),
        }

    clarification_codes = tuple(
        dict.fromkeys(
            code
            for preview in previews
            for code in preview.clarification_codes
        )
    )
    if clarification_codes:
        orders.mark_outcome(
            admission.order_request_id,
            state="CLARIFICATION_REQUIRED",
            clarification_code=",".join(clarification_codes),
        )
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "binding": False,
            "mode": "PAPER",
            "rule_active": False,
            "reason_codes": list(clarification_codes),
            "assumptions": [list(preview.assumptions) for preview in previews],
            "user_message": _clarification_message(
                clarification_codes, raw_instruction=admission.raw_instruction
            ),
        }

    fingerprints = [preview.spec_sha256 for preview in previews]
    if len(set(fingerprints)) != len(fingerprints):
        orders.mark_outcome(
            admission.order_request_id,
            state="CLARIFICATION_REQUIRED",
            clarification_code="DUPLICATE_CONDITIONAL_ACTION",
        )
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "binding": False,
            "mode": "PAPER",
            "rule_active": False,
            "reason_codes": ["DUPLICATE_CONDITIONAL_ACTION"],
            "user_message": _clarification_message(
                ("DUPLICATE_CONDITIONAL_ACTION",),
                raw_instruction=admission.raw_instruction,
            ),
        }

    created: list[Any] = []
    try:
        rules = conditional_rule_repository()
        for index, preview in enumerate(previews):
            created.append(
                rules.create_pending(
                    spec=preview.spec,
                    raw_instruction=admission.raw_instruction,
                    client_request_id=_rule_client_request_id(
                        admission.client_request_id,
                        index=index,
                        count=len(previews),
                    ),
                    parser_source=source,
                )
            )
        activated: list[Any] = []
        for rule in created:
            if rule.state is RuleState.PENDING_CONFIRMATION:
                rule = rules.activate(
                    rule.rule_id,
                    user_id=admission.user_id,
                    confirmation_sha256=rule.spec_sha256,
                )
            elif rule.state is not RuleState.ACTIVE:
                raise ConditionalRuleConflict(
                    "conditional rule is not immediately activatable from "
                    f"{rule.state.value}"
                )
            activated.append(rule)
    except (ConditionalRuleUnavailable, ConditionalRuleConflict) as exc:
        compensated = (
            _cancel_created_rules(rules, created, user_id=admission.user_id)
            if "rules" in locals()
            else True
        )
        orders.mark_outcome(
            admission.order_request_id,
            state="FAILED" if compensated else "UNKNOWN",
            error_code=type(exc).__name__,
            error_message="conditional PAPER rule activation failed closed",
        )
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "binding": False,
            "mode": "PAPER",
            "rule_active": False,
            "reason_codes": [
                type(exc).__name__
                if compensated
                else "CONDITIONAL_RULE_COMPENSATION_UNCERTAIN"
            ],
            "user_message": (
                "조건주문 저장소 검증을 완료하지 못했습니다. "
                + (
                    "생성된 규칙을 모두 취소했으며 주문·체결·원장 반영은 없습니다."
                    if compensated
                    else "일부 규칙 상태를 확정하지 못해 자동 재시도하지 않습니다."
                )
            ),
        }

    payload = (
        {
            "kind": "CONDITIONAL_PAPER_RULE",
            "rule_id": activated[0].rule_id,
            "spec": activated[0].spec.model_dump(mode="json", exclude_none=True),
        }
        if len(activated) == 1
        else {
            "kind": "CONDITIONAL_PAPER_RULE_SET",
            "rule_ids": [rule.rule_id for rule in activated],
            "rules": [
                {
                    "rule_id": rule.rule_id,
                    "spec": rule.spec.model_dump(mode="json", exclude_none=True),
                }
                for rule in activated
            ],
        }
    )
    orders.mark_outcome(
        admission.order_request_id,
        state="COMPLETED",
        canonical_payload=payload,
        payload_sha256=canonical_payload_sha256(payload),
    )
    return _active_batch_result(
        activated,
        assumptions=[preview.assumptions for preview in previews],
    )


def get_user_conditional_paper_rule_status(
    *, root_task_id: str, trading_task_id: str
) -> dict[str, Any]:
    """Read the linked directive through the existing scoped Trading API."""

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
    payload = admission.canonical_payload or {}
    if payload.get("kind") == "CONDITIONAL_PAPER_RULE" and payload.get("rule_id"):
        rule_ids = [str(payload["rule_id"])]
    elif payload.get("kind") == "CONDITIONAL_PAPER_RULE_SET" and isinstance(
        payload.get("rule_ids"), list
    ):
        rule_ids = [str(rule_id) for rule_id in payload["rule_ids"] if rule_id]
    else:
        _reject("CONDITIONAL_RULE_NOT_LINKED")

    rules = conditional_rule_repository()
    statuses: list[dict[str, Any]] = []
    for rule_id in rule_ids:
        rule = rules.get(rule_id, user_id=admission.user_id)
        if rule is None:
            _reject("CONDITIONAL_RULE_NOT_FOUND")
        rule_state = rule.state.value
        if not rule.directive_id:
            workflow_state = (
                "WAITING_FOR_TRIGGER"
                if rule.state is RuleState.ACTIVE
                else rule_state
            )
            if rule.state is RuleState.ACTIVE:
                answer = (
                    "PAPER 조건주문은 활성 상태이지만 Trading 제출 이벤트는 "
                    "아직 발생하지 않았습니다."
                )
            else:
                answer = (
                    f"PAPER 조건주문 규칙은 현재 {rule_state} 상태이며 Trading "
                    "제출 이벤트는 발생하지 않았습니다."
                )
            statuses.append(
                {
                    "schema_version": "conditional-paper-execution-status.v1",
                    "authority_source": "execution.conditional_trade_rules",
                    "authority_verified": True,
                    "mode": "PAPER",
                    "rule_id": rule.rule_id,
                    "rule_state": rule_state,
                    "directive_id": None,
                    "workflow_state": workflow_state,
                    "final_answer": answer,
                }
            )
            continue
        directive = read_paper_directive_status_for_admitted_authority(
            user_id=admission.user_id,
            fund_id=admission.fund_id,
            book_id=admission.book_id,
            directive_id=rule.directive_id,
        )
        snapshot = build_conditional_execution_status(
            rule_id=rule.rule_id,
            directive=directive,
            expected_directive_id=rule.directive_id,
            workflow_state=_workflow_state_from_directive(directive),
        )
        projected = snapshot.model_dump(mode="json")
        projected["rule_state"] = rule_state
        projected["final_answer"] = (
            f"조건 규칙 상태 : {rule_state}\n{projected['final_answer']}"
        )
        statuses.append(projected)

    if len(statuses) == 1:
        return statuses[0]
    return {
        "schema_version": "conditional-paper-execution-status-set.v1",
        "authority_source": "execution.conditional_trade_rules",
        "authority_verified": True,
        "mode": "PAPER",
        "rule_ids": rule_ids,
        "workflow_state": "RULE_SET",
        "rules": statuses,
        "final_answer": (
            f"PAPER 조건주문 {len(statuses)}개의 권위 상태를 확인했습니다. "
            + " ".join(
                f"규칙 {index + 1}: {status['final_answer']}"
                for index, status in enumerate(statuses)
            )
        ),
    }


__all__ = [
    "RESULT_SCHEMA_VERSION",
    "get_user_conditional_paper_rule_status",
    "process_user_conditional_paper_rule",
]
