#!/usr/bin/env python3
"""risk-api의 `RiskAssessment` -> 우리 `RiskDecision` 어댑터 (Contract Test 포함).

소유: 도현 (트레이딩본부)
근거: docs/02-engineering/RISK_QA_DOMAIN_API_SPEC.md 2.1
      (`decision.verdict` = APPROVE|RESIZE|REJECT, `risk_request_id`, `approved_legs`,
       `check_results`, `reason_codes`, `calculation_version`, `input_hash`, `trading_state`)
      docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.3, CLAUDE.local.md 원칙 2

**판정을 만들지 않는다.** 리스크본부가 낸 판정을 우리 계약으로 옮기기만 한다.
verdict를 여기서 계산하거나 승격시키는 경로가 없는 것이 이 파일의 요지다.

경계에서 실제로 어긋나는 것 세 가지 - 그래서 이 어댑터가 필요하다:

  1. **대소문자가 다르다.** risk-api는 `APPROVE`, 우리 `RiskVerdict`는 `approve`다.
     문자열을 그대로 넣으면 pydantic이 거부한다.
  2. **승인 수량 자리가 다르다.** 응답은 `approved_legs`(멀티레그)에 담고 우리
     계약은 `approved_quantity` 하나를 본다. 단일 Leg 주문만 받는다 - Leg가 둘
     이상인 판정을 한 수량으로 접으면 어느 다리가 승인된 건지 사라진다.
  3. **`risk_decision_id`가 응답 핵심 필드 목록에 없다**(Event payload에는 있다).
     없으면 거부한다 - `RISK_APPROVED`는 상태가 아니라 전제조건이고, 유효한
     decision id 없이는 제출할 수 없다(팀 가이드 4.3).

그리고 **`trading_state`가 거래를 막고 있으면 verdict가 APPROVE여도 거부한다.**
Kill Switch가 걸린 상태에서 앞선 승인으로 주문이 나가면 Kill Switch를 둔 의미가 없다.
모르는 값이면 막는 쪽으로 떨어진다(개발 원칙 9).

자체 점검: python departments/02-trading/contracts/risk_gate.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from contracts import RiskDecision, RiskVerdict

# 이 상태에서만 신규 진입을 허용한다. 나머지(REDUCE_ONLY/ENTRY_BLOCKED/HALTED)와
# 모르는 값은 전부 막는다 - 목록에 없는 상태를 허용으로 읽지 않는다.
ENTRY_ALLOWED_TRADING_STATES = frozenset({"ENABLED"})


class RiskGateError(Exception):
    """리스크 판정을 주문 권한으로 옮길 수 없는 경우. 통과시키지 않는다."""


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RiskGateError(f"{field}를 수량으로 읽을 수 없습니다: {value!r}") from exc


def _approved_quantity(assessment: Mapping[str, Any]) -> Decimal | None:
    """승인 수량. `approved_legs`가 있으면 거기서, 없으면 최상위에서 읽는다.

    Leg가 둘 이상이면 거부한다 - 합쳐서 한 수량으로 만들면 어느 다리가 얼마나
    승인됐는지가 사라지고, 그 상태로 낸 주문은 판정과 다른 주문이 된다.
    """
    legs = assessment.get("approved_legs")
    if legs:
        if len(legs) > 1:
            raise RiskGateError(
                f"멀티레그 판정({len(legs)}개 Leg)은 단일 Intent로 옮길 수 없습니다(F30 별도 경로)")
        leg = legs[0]
        quantity = leg.get("approved_quantity", leg.get("quantity"))
        return _decimal(quantity, "approved_legs[0].approved_quantity")
    if assessment.get("approved_quantity") is None:
        return None
    return _decimal(assessment["approved_quantity"], "approved_quantity")


def to_risk_decision(assessment: Mapping[str, Any], *, order_intent_id: UUID,
                     expires_at: datetime) -> RiskDecision:
    """risk-api 응답을 우리 계약으로 옮긴다. **판정 내용은 바꾸지 않는다.**

    `expires_at`은 호출자가 준다 - 응답에 만료가 없고, 만료 없는 승인은 영구
    승인이 되기 때문이다(팀 가이드 4.3: 전송 직전에 만료를 다시 본다).
    """
    decision = assessment.get("decision") or {}
    raw_verdict = decision.get("verdict") if isinstance(decision, Mapping) else decision
    if not raw_verdict:
        raise RiskGateError("판정에 verdict가 없습니다. 없는 승인을 만들지 않습니다")

    try:
        verdict = RiskVerdict(str(raw_verdict).lower())
    except ValueError as exc:
        raise RiskGateError(f"알 수 없는 verdict입니다: {raw_verdict!r}") from exc

    decision_id = assessment.get("risk_decision_id") or (
        decision.get("risk_decision_id") if isinstance(decision, Mapping) else None)
    if not decision_id:
        raise RiskGateError(
            "risk_decision_id가 없습니다. 유효한 판정 id 없이는 제출할 수 없습니다"
            "(RISK_APPROVED는 상태가 아니라 전제조건)")

    trading_state = assessment.get("trading_state")
    if verdict is not RiskVerdict.REJECT and trading_state not in ENTRY_ALLOWED_TRADING_STATES:
        raise RiskGateError(
            f"trading_state가 {trading_state!r}라 신규 진입을 허용하지 않습니다. "
            "승인 판정이어도 Kill Switch가 우선합니다")

    quantity = None if verdict is RiskVerdict.REJECT else _approved_quantity(assessment)
    reasons = assessment.get("reason_codes") or []
    return RiskDecision(
        risk_decision_id=UUID(str(decision_id)),
        order_intent_id=order_intent_id,
        verdict=verdict,
        approved_quantity=quantity,
        expires_at=expires_at,
        reason="; ".join(str(r) for r in reasons)[:2000],
        decided_by=str(assessment.get("calculation_version") or "risk-api"),
    )


if __name__ == "__main__":
    from datetime import timedelta
    from uuid import uuid4

    sys.path.insert(0, str(_HERE.parent / "oms"))
    sys.path.insert(0, str(_HERE.parent / "multileg"))
    sys.path.insert(0, str(_HERE.parent / "capability"))
    from contracts import MarketSnapshot, OrderIntent, OrderType, Side, TimeInForce
    from oms import OMS, OMSError

    D = Decimal
    now = datetime.now(timezone.utc)
    intent_id = uuid4()
    expires = now + timedelta(hours=1)

    def assessment(verdict="APPROVE", *, quantity="100", state="ENABLED",
                   legs=None, decision_id=None, **extra) -> dict:
        body = {
            "risk_request_id": str(uuid4()),
            "risk_decision_id": str(decision_id or uuid4()),
            "decision": {"verdict": verdict},
            "trading_state": state,
            "check_results": [],
            "reason_codes": ["LIMIT_OK"],
            "calculation_version": "risk-engine-v1",
            "input_hash": "h",
            **extra,
        }
        if legs is not None:
            body["approved_legs"] = legs
        elif quantity is not None:
            body["approved_quantity"] = quantity
        return body

    def rejects(fn, why: str) -> None:
        try:
            fn()
        except RiskGateError:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. 대소문자가 다른 verdict를 우리 계약으로 옮긴다
    decision = to_risk_decision(assessment("APPROVE"), order_intent_id=intent_id,
                                expires_at=expires)
    assert decision.verdict is RiskVerdict.APPROVE and decision.approved_quantity == D("100")
    assert decision.order_intent_id == intent_id and decision.expires_at == expires
    assert to_risk_decision(assessment("RESIZE", quantity="40"), order_intent_id=intent_id,
                            expires_at=expires).approved_quantity == D("40")

    # 2. REJECT는 수량을 갖지 않는다 (계약이 강제하지만 어댑터도 넘기지 않는다)
    rejected = to_risk_decision(assessment("REJECT", quantity="100"),
                                order_intent_id=intent_id, expires_at=expires)
    assert rejected.verdict is RiskVerdict.REJECT and rejected.approved_quantity is None

    # 3. **승인 판정이어도 Kill Switch가 우선한다**
    for state in ("HALTED", "ENTRY_BLOCKED", "REDUCE_ONLY", None, "알수없음"):
        rejects(lambda s=state: to_risk_decision(assessment(state=s),
                                                 order_intent_id=intent_id, expires_at=expires),
                f"trading_state={state}에서 신규 진입")
    # REJECT는 상태와 무관하게 옮겨진다 - 거부를 기록하는 것까지 막을 이유가 없다
    assert to_risk_decision(assessment("REJECT", state="HALTED"), order_intent_id=intent_id,
                            expires_at=expires).verdict is RiskVerdict.REJECT

    # 4. risk_decision_id가 없으면 거부한다. 없는 승인을 만들지 않는다
    no_id = assessment()
    no_id.pop("risk_decision_id")
    rejects(lambda: to_risk_decision(no_id, order_intent_id=intent_id, expires_at=expires),
            "decision id 없는 판정")

    # 5. 모르는 verdict를 승인으로 읽지 않는다
    for bad in ("APPROVED", "OK", "", None, "hold"):
        body = assessment(bad) if bad else assessment()
        if not bad:
            body["decision"] = {"verdict": bad}
        rejects(lambda b=body: to_risk_decision(b, order_intent_id=intent_id,
                                                expires_at=expires),
                f"verdict={bad!r}")

    # 6. 멀티레그 판정을 한 수량으로 접지 않는다
    assert to_risk_decision(assessment(legs=[{"approved_quantity": "70"}]),
                            order_intent_id=intent_id,
                            expires_at=expires).approved_quantity == D("70")
    rejects(lambda: to_risk_decision(
        assessment(legs=[{"approved_quantity": "70"}, {"approved_quantity": "30"}]),
        order_intent_id=intent_id, expires_at=expires), "멀티레그 판정")

    # 7. **승인분만 Paper OMS에 진입한다.** 어댑터를 통과한 판정을 실제 OMS에 태운다
    oms = OMS(adapter="paper")
    ids = {k: uuid4() for k in ("case", "fund", "book", "strategy", "instrument")}

    def make_intent(key: str) -> OrderIntent:
        return OrderIntent(
            trade_case_id=ids["case"], fund_id=ids["fund"], book_id=ids["book"],
            strategy_id=ids["strategy"], instrument_id=ids["instrument"],
            side=Side.BUY, order_type=OrderType.LIMIT, quantity=D("100"),
            limit_price=D("70000"), time_in_force=TimeInForce.DAY,
            valid_until=now + timedelta(hours=6),
            snapshot=MarketSnapshot(market_snapshot_id="snap_risk", as_of=now,
                                    bid=D("70000"), ask=D("70100")),
            idempotency_key=key, created_by="svc_selfcheck", trace_id="trace_risk_gate",
        )

    approved_intent = make_intent("risk_gate_approve")
    rec = oms.register_intent(approved_intent)
    oms.request_risk_review(rec)
    oms.apply_risk_decision(rec, to_risk_decision(
        assessment("APPROVE"), order_intent_id=rec.order_intent_id, expires_at=expires))
    order = oms.create_broker_order(rec, approved_intent)
    assert order is not None, "승인된 Intent가 주문이 되지 않았다"

    # 거부된 Intent는 주문이 되지 않는다
    denied_intent = make_intent("risk_gate_reject")
    denied = oms.register_intent(denied_intent)
    oms.request_risk_review(denied)
    oms.apply_risk_decision(denied, to_risk_decision(
        assessment("REJECT"), order_intent_id=denied.order_intent_id, expires_at=expires))
    try:
        oms.create_broker_order(denied, denied_intent)
        raise AssertionError("거부된 Intent가 주문이 됐다")
    except OMSError:
        pass

    # 8. Kill Switch에 막힌 판정은 애초에 OMS까지 가지 못한다
    halted_intent = make_intent("risk_gate_halted")
    halted = oms.register_intent(halted_intent)
    oms.request_risk_review(halted)
    rejects(lambda: to_risk_decision(assessment(state="HALTED"),
                                     order_intent_id=halted.order_intent_id,
                                     expires_at=expires), "HALTED 상태의 승인")
    try:
        oms.create_broker_order(halted, halted_intent)
        raise AssertionError("판정이 없는 Intent가 주문이 됐다")
    except OMSError:
        pass

    print("ok - Risk 판정 어댑터 8개 영역 점검 통과 "
          "(대소문자·수량 자리·decision id·Kill Switch 우선, 승인분만 OMS 진입)")
