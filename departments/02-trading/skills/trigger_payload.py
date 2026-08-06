#!/usr/bin/env python3
"""직원 트리거 파생. **승인은 읽기만 하고 만들지 않는다.**

소유: 도현 (트레이딩본부)
근거: CLAUDE.local.md 원칙 2(Risk 승인 없이 SUBMITTED 불가), 개발 원칙 9번
      orchestration/workflows/investment-case.yaml (trading=2, risk=3)

조건부 직원 4명은 payload 에 특정 키가 있어야 돈다. 그런데 공용
`orchestration/adapters/paper_pipeline.py` 가 `{case_request, research_packet}` 두 개만
줘서 4명이 런타임에 한 번도 안 돌았다. 그 파일은 다른 본부도 쓰는 공용이라 안 고치고,
**트레이딩에 들어오는 유일한 문**인 `employee_workers.run_employee_workers()` 에서
우리 계약으로부터 파생한다.

철칙 둘:

  1. **호출자가 준 키는 절대 덮어쓰지 않는다.** setdefault 의미론이다. 호출자가 이미
     판단해 넣은 값을 우리가 다시 계산해 바꾸면 상위 결정이 조용히 뒤집힌다.
  2. **승인을 만들지 않는다.** `approved_risk` 는 `risk_gate.to_risk_decision()` 에
     그대로 태워 통과할 때만 True 다 — OMS 가 실제 주문을 만들 때 쓰는 것과 **완전히
     같은 게이트**라 직원이 도는 조건과 주문이 나가는 조건이 갈라질 수 없다.
     Kill Switch·만료·verdict 검사를 여기서 다시 쓰지 않는다.

**paper 경로에서 order-constraint / execution-planning 은 이 파생 뒤에도 안 돈다.**
워크플로가 trading=sequence 2, risk=sequence 3 이라 그 시점에 `risk_decision` 이
존재하지 않기 때문이다. 없는 승인을 만들어 넣는 것이 정확히 가짜 승인이므로 그렇게
하지 않고, 대신 `trigger_provenance` 에 **왜 안 켰는지**를 남긴다 - 침묵하는
not_executed 가 없어야 나중에 "미완성"과 "정상"을 구분할 수 있다.

자체 점검: python departments/02-trading/skills/trigger_payload.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

_HERE = Path(__file__).resolve().parent
_DEPT = _HERE.parent
for _p in (str(_DEPT / "contracts"), str(_DEPT / "capability"), str(_DEPT / "execution"),
           str(_DEPT / "oms"), str(_DEPT / "multileg")):
    if _p not in sys.path:
        sys.path.append(_p)

from contracts import RiskVerdict  # noqa: E402
from derivatives import CERTIFICATION_REQUIRED  # noqa: E402
from risk_gate import RiskGateError, to_risk_decision  # noqa: E402
from tca_memory import TcaMemoryError, load_execution_presets  # noqa: E402

# 이 verdict 만 집행 계획 단계로 넘어간다. REJECT/EXPIRED 는 당연히 아니다.
_ENTRY_VERDICTS = frozenset({RiskVerdict.APPROVE, RiskVerdict.RESIZE})

# 파생하는 트리거. `risk_decision` 은 여기 없다 - 리스크본부 산출물이라 파생 대상이 아니다.
DERIVED_TRIGGERS = ("approved_risk", "execution_request", "derivatives_signal")


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# ── approved_risk — 유일한 근거는 risk_gate 다 ─────────────────────────────
def derive_approved_risk(payload: Mapping[str, Any], *,
                         now: datetime | None = None) -> tuple[bool, str]:
    """Risk 판정이 실제로 집행 단계로 넘어갈 자격이 있는가.

    판정 로직이 이 함수에 0줄이다 - `to_risk_decision()` 이 verdict 대소문자,
    `risk_decision_id` 존재, Kill Switch(`trading_state`)를 전부 본다. 예외가 나면 False.
    """
    assessment = _mapping(payload.get("risk_decision"))
    if not assessment:
        return False, ("risk_decision 부재 — 워크플로 순서상 트레이딩(sequence 2)이 "
                       "리스크(sequence 3)보다 먼저 돈다. 없는 승인을 만들지 않는다")
    expires_at = assessment.get("expires_at")
    try:
        deadline = (datetime.fromisoformat(str(expires_at)) if expires_at
                    else _now(now) + timedelta(hours=1))
    except ValueError:
        return False, f"expires_at 을 읽을 수 없다: {expires_at!r}"
    if deadline.tzinfo is None:
        # naive 를 UTC 로 가정하면 KST 와 9시간 어긋난다. 추측하지 않는다.
        return False, "expires_at 에 timezone 이 없다 — 만료 시각을 추측하지 않는다"

    try:
        decision = to_risk_decision(assessment, order_intent_id=uuid4(), expires_at=deadline)
    except (RiskGateError, ValueError) as exc:
        return False, f"Risk 판정이 게이트를 통과하지 못했다: {exc}"

    if decision.verdict not in _ENTRY_VERDICTS:
        return False, f"verdict={decision.verdict} 는 집행 단계로 넘어가지 않는다"
    if deadline <= _now(now):
        return False, f"승인이 만료됐다 (expires_at={deadline.isoformat()})"
    return True, (f"risk_gate 통과: verdict={decision.verdict}, "
                  f"decision_id={decision.risk_decision_id}, 만료 {deadline.isoformat()}")


# ── execution_request — 비용을 계산할 대상이 실재해야 한다 ─────────────────
def derive_execution_request(payload: Mapping[str, Any]) -> tuple[bool, str]:
    snapshot = payload.get("market_snapshot")
    if not snapshot:
        return False, "market_snapshot 부재 — 시세 없이 거래비용을 추정하지 않는다"
    if payload.get("order_intent"):
        return True, "order_intent + market_snapshot 존재"
    proposal = _mapping(_mapping(payload.get("debate")).get("order_intent_proposal"))
    if proposal.get("available"):
        return True, "debate.order_intent_proposal.available + market_snapshot"
    reason = proposal.get("reason") or "제안 없음"
    return False, f"비용을 계산할 주문 후보가 없다 (제안 사유: {reason})"


# ── derivatives_signal — 파생·공매도만 켠다 ────────────────────────────────
def derive_derivatives_signal(payload: Mapping[str, Any]) -> tuple[bool, str]:
    if payload.get("derivatives"):
        return True, "derivatives 블록 존재"
    intent = _mapping(payload.get("order_intent")) or _mapping(payload.get("case_request"))
    asset_class = str(intent.get("asset_class") or "").upper()
    # 상품군 목록을 새로 만들지 않는다 - capability/derivatives.py 가 이미 소유한다.
    if asset_class in CERTIFICATION_REQUIRED:
        return True, f"asset_class={asset_class} 는 Certification 대상이다"
    if str(intent.get("position_effect") or "").upper() == "OPEN_SHORT":
        return True, "공매도 진입 — Certification 대상이다"
    return False, f"현물 경로 (asset_class={asset_class or '미상'}) — 파생 신호 없음"


# ── execution_plan — 프리셋에서 검사용 초안을 뽑는다 ───────────────────────
def _philosophy(payload: Mapping[str, Any]) -> str | None:
    direct = payload.get("philosophy")
    if direct:
        return str(direct)
    created_by = str(_mapping(payload.get("order_intent")).get("created_by") or "")
    if "/" in created_by:
        return created_by.rsplit("/", 1)[-1]
    signal = _mapping(payload.get("strategy_bundle")) or _mapping(payload.get("signal"))
    return str(signal["philosophy"]) if signal.get("philosophy") else None


def derive_execution_plan(payload: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """철학 프리셋에서 분할 초안을 만든다. **승인된 계획이 아니라 검사용 초안이다.**

    지금까지 `payload["execution_plan"]` 을 아무도 안 넣어서 `check_plan_feasible()` 이
    실행마다 `checked: False` 였다. 프리셋에서 뽑으면 브로커 한도 검사가 처음으로 실제
    판정을 낸다 - 그게 목적이다.
    """
    philosophy = _philosophy(payload)
    if not philosophy:
        return None, "philosophy 미상 — 프리셋을 추측해 계획을 만들지 않는다"
    try:
        presets = load_execution_presets()
    except (TcaMemoryError, OSError, KeyError) as exc:
        return None, f"집행 프리셋을 읽을 수 없다: {type(exc).__name__}"
    preset = presets.get(philosophy)
    if preset is None:
        return None, f"philosophies.yaml 에 없는 철학이다: {philosophy}"
    return {
        "slices": int(preset["slices"]),
        "window_minutes": float(preset.get("cancel_after_min", 30)),
        "adapter": str(payload.get("broker_adapter") or "paper"),
        # 소비자가 "승인된 계획"으로 오해하지 않게 출처를 박는다.
        "source": f"philosophies.yaml:{philosophy}",
        "approved": False,
    }, f"philosophies.yaml:{philosophy} 프리셋에서 파생한 검사용 초안"


# ── 조립 ───────────────────────────────────────────────────────────────────
def enrich_payload(payload: Mapping[str, Any], *,
                   now: datetime | None = None) -> dict[str, Any]:
    """직원 레지스트리에 넘길 payload 를 만든다. **원본을 변형하지 않는다.**"""
    enriched = dict(payload)
    provenance: dict[str, Any] = {}

    for name, derive in (
        ("approved_risk", lambda: derive_approved_risk(enriched, now=now)),
        ("execution_request", lambda: derive_execution_request(enriched)),
        ("derivatives_signal", lambda: derive_derivatives_signal(enriched)),
    ):
        if name in enriched:
            provenance[name] = {"value": bool(enriched[name]), "reason": "호출자가 직접 지정"}
            continue
        value, reason = derive()
        enriched[name] = value
        provenance[name] = {"value": value, "reason": reason}

    if "execution_plan" in enriched:
        provenance["execution_plan"] = {"value": True, "reason": "호출자가 직접 지정"}
    else:
        plan, reason = derive_execution_plan(enriched)
        if plan is not None:
            enriched["execution_plan"] = plan
        provenance["execution_plan"] = {"value": plan is not None, "reason": reason}

    # risk_decision 은 파생 대상이 아니다 - 왜 없는지는 남긴다.
    if "risk_decision" not in enriched:
        provenance["risk_decision"] = {
            "value": False,
            "reason": "리스크본부 산출물이라 파생하지 않는다 — 트레이딩이 만들면 가짜 승인이다"}

    enriched["trigger_provenance"] = provenance
    return enriched


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    import json

    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

    def assessment(verdict="APPROVE", *, state="ENABLED", decision_id=None,
                   expires=None, **extra) -> dict:
        body = {"risk_request_id": str(uuid4()),
                "risk_decision_id": str(decision_id or uuid4()),
                "decision": {"verdict": verdict}, "trading_state": state,
                "approved_quantity": "100", "reason_codes": ["LIMIT_OK"],
                "calculation_version": "risk-engine-v1",
                "expires_at": (expires or now + timedelta(hours=1)).isoformat(), **extra}
        return body

    # 1. **가짜 승인 금지** — 네 가지 실패 경로가 전부 False 다
    for label, body in (
        ("만료됨", assessment(expires=now - timedelta(minutes=1))),
        ("Kill Switch", assessment(state="HALTED")),
        ("decision_id 없음", {**assessment(), "risk_decision_id": None}),
        ("오타 verdict", assessment("APPROVED")),
        ("REJECT", assessment("REJECT")),
        ("naive 만료", {**assessment(), "expires_at": "2026-08-05T13:00:00"}),
    ):
        value, reason = derive_approved_risk({"risk_decision": body}, now=now)
        assert value is False, f"{label} 인데 승인으로 파생됐다"
        assert reason, f"{label}: 사유가 비었다"
    print("  가짜 승인 차단               OK")

    # 2. 정상 승인은 켜진다 (게이트가 항상 막기만 하는 게 아니다)
    ok, why = derive_approved_risk({"risk_decision": assessment()}, now=now)
    assert ok is True and "risk_gate 통과" in why, why
    resized, _ = derive_approved_risk({"risk_decision": assessment("RESIZE")}, now=now)
    assert resized is True
    print("  정상 승인 통과               OK")

    # 3. risk_decision 이 없으면 사유가 남는다 (paper 경로의 정상 상태)
    empty = enrich_payload({"research_packet": {"symbol": "005930"}}, now=now)
    assert empty["approved_risk"] is False
    prov = empty["trigger_provenance"]
    assert "sequence 2" in prov["approved_risk"]["reason"], prov["approved_risk"]
    assert "가짜 승인" in prov["risk_decision"]["reason"]
    print("  미승인 사유 기록             OK")

    # 4. **호출자 키는 절대 안 덮어쓴다** — 계약 테스트 호환의 생명줄
    forced = enrich_payload({"approved_risk": True, "execution_request": True,
                             "derivatives_signal": {"enabled": True},
                             "risk_decision": {"verdict": "REJECT"}}, now=now)
    assert forced["approved_risk"] is True, "호출자가 준 승인을 덮어썼다"
    assert forced["execution_request"] is True
    assert forced["derivatives_signal"] == {"enabled": True}
    assert forced["trigger_provenance"]["approved_risk"]["reason"] == "호출자가 직접 지정"
    print("  호출자 키 불변               OK")

    # 5. execution_request — 시세와 주문 후보가 둘 다 있어야 켜진다
    assert derive_execution_request({"order_intent": {"quantity": "1"}})[0] is False  # 시세 없음
    assert derive_execution_request({"market_snapshot": {"bid": "1"}})[0] is False    # 후보 없음
    assert derive_execution_request({"market_snapshot": {"bid": "1"},
                                     "order_intent": {"quantity": "1"}})[0] is True
    proposed = derive_execution_request({
        "market_snapshot": {"bid": "1"},
        "debate": {"order_intent_proposal": {"available": True}}})
    assert proposed[0] is True and "order_intent_proposal" in proposed[1]
    blocked = derive_execution_request({
        "market_snapshot": {"bid": "1"},
        "debate": {"order_intent_proposal": {"available": False, "reason": "not_grounded"}}})
    assert blocked[0] is False and "not_grounded" in blocked[1]
    print("  execution_request 파생       OK")

    # 6. derivatives_signal — 현물은 안 켜고 파생·공매도만 켠다
    assert derive_derivatives_signal({"case_request": {"asset_class": "EQUITY"}})[0] is False
    assert derive_derivatives_signal({"case_request": {"asset_class": "FUTURE"}})[0] is True
    assert derive_derivatives_signal({"order_intent": {"asset_class": "OPTION"}})[0] is True
    assert derive_derivatives_signal(
        {"order_intent": {"position_effect": "OPEN_SHORT"}})[0] is True
    assert derive_derivatives_signal({"derivatives": {"kind": "CALL"}})[0] is True
    print("  derivatives_signal 파생      OK")

    # 7. execution_plan — 프리셋에서 나오고 출처가 붙는다
    plan, why = derive_execution_plan({"philosophy": "momentum"})
    assert plan is not None and plan["slices"] == 5 and plan["window_minutes"] == 30.0
    assert plan["source"] == "philosophies.yaml:momentum" and plan["approved"] is False
    assert derive_execution_plan({})[0] is None, "철학 미상인데 계획을 만들었다"
    assert derive_execution_plan({"philosophy": "없는철학"})[0] is None
    # created_by 에서도 철학을 읽는다 (intent_builder 가 그 형식으로 쓴다)
    from_intent, _ = derive_execution_plan(
        {"order_intent": {"created_by": "trading-intent-builder/value"}})
    assert from_intent is not None and from_intent["source"].endswith("value")
    print("  execution_plan 초안 파생     OK")

    # 8. 원본 payload 를 변형하지 않는다
    original = {"research_packet": {"symbol": "005930"}, "market_snapshot": {"bid": "1"}}
    before = json.dumps(original, ensure_ascii=False, sort_keys=True)
    enrich_payload(original, now=now)
    assert json.dumps(original, ensure_ascii=False, sort_keys=True) == before, "원본이 바뀌었다"
    print("  원본 payload 불변            OK")

    # 9. 파생 전체가 provenance 에 남는다 - 켠 이유와 안 켠 이유 둘 다
    full = enrich_payload({"market_snapshot": {"bid": "1"}, "philosophy": "momentum",
                           "risk_decision": assessment()}, now=now)
    prov = full["trigger_provenance"]
    assert set(prov) >= {"approved_risk", "execution_request", "derivatives_signal",
                         "execution_plan"}
    assert all(p["reason"] for p in prov.values()), "사유 없는 파생이 있다"
    assert full["approved_risk"] is True and full["execution_plan"]["approved"] is False
    print("  trigger_provenance 완비      OK")

    print("ok - 직원 트리거 파생 9개 영역 점검 통과 "
          "(승인은 risk_gate 만이 만든다, 호출자 키 불변)")
