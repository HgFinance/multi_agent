#!/usr/bin/env python3
"""직원별 근거 주입. 판정은 하지 않고 **결정론 결과와 검색 결과를 evidence 로 옮긴다.**

소유: 도현 (트레이딩본부)
근거: CLAUDE.md 5.9(결정론 계층), skills/agentic-rag 의 grounding 원칙

직원이 서술하기 **전에** 근거가 evidence 에 들어간다. 그래서 직원은 규칙 숫자를
기억에서 꺼낼 필요가 없고, 꺼내면 `citations.py` 가 색인 밖 인용으로 잡는다.

provider 6개. **전부 기존 모듈만 부르고 새 검색 로직을 짜지 않는다.**

  bull_debate_evidence   토론 산출 중 **자기 쪽만** + Claim id + 쟁점 목록
  bear_debate_evidence   같은 규칙, 반대편
  state_machine_evidence 현재 상태에서 나가는 전이 + risk_gate 매핑 규칙
  broker_rules_evidence  LS TR 규칙표 + 분할 실현가능성 판정
  tca_evidence           과거 유사 집행의 실현 슬리피지 그룹 + 조정 제안
  certification_evidence 파생 Certification 서명 현황

**2026-08-06 소비자가 바뀌었다. provider 는 그대로다.** 아래 넷을 쓰던 조건부 직원
4명(order-constraint / execution-planning / venue-cost / derivatives-structure)이
tool 로 강등돼 `desk-runner-worker` 하나로 합쳐졌다. provider 는 원래부터 결정론
모듈을 부르기만 했으므로 지울 이유가 없다 - `desk_runner_evidence()` 가 넷을 한 번에
돌린다. 사라진 것은 그 결과를 1.7b 모델로 다시 서술하던 계층뿐이다.

**상대 원문을 절대 넣지 않는다.** Bull evidence 에 Bear 문장이 들어가면 확증편향을
막으려고 두 직원을 나눈 의미가 없어진다 - 자체 점검이 그것을 직접 검사한다.

**라우터에 복종한다.** provider 는 `plan.methods` 를 검사하고 경로가 아니면 검색을
부르지 않는다. `rag_router` 가 정책을 말하고 여기가 지킨다 - 안 그러면 라우터가 장식이다.

자체 점검: python departments/02-trading/skills/worker_evidence.py
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping

_HERE = Path(__file__).resolve().parent
_DEPT = _HERE.parent
for _p in (str(_HERE), str(_DEPT / "execution"), str(_DEPT / "contracts"),
           str(_DEPT / "capability")):
    if _p not in sys.path:
        sys.path.append(_p)

import broker_rules  # noqa: E402
import tca_memory  # noqa: E402
from broker_rules import BrokerRuleError, ExecutionPlanDraft  # noqa: E402
from contracts import (  # noqa: E402
    BROKER_TRANSITIONS,
    INTENT_TRANSITIONS,
    BrokerOrderState,
    IntentState,
)
from derivatives import (  # noqa: E402
    CERTIFICATION_REQUIRED,
    DERIVATIVE_CERTIFIERS,
)
from rag_router import (  # noqa: E402
    DETERMINISTIC_PLAN,
    RAGPlan,
    allows,
    choose_rag_route,
    route_denied,
)
from risk_gate import ENTRY_ALLOWED_TRADING_STATES  # noqa: E402
from tca_memory import TcaMemoryError  # noqa: E402

EvidenceProvider = Callable[[Mapping[str, Any], RAGPlan], dict[str, Any]]

# provider 당 상한. 공용 런타임이 evidence JSON 을 8000자에서 자르므로 상한이 없으면
# 뒤에 붙은 근거가 조용히 잘려나간다.
_MAX_RULES = 6
_MAX_TCA_GROUPS = 3


# ponytail: 규칙 문서와 튜닝 YAML 은 런타임 중 안 바뀐다는 전제로 한 번만 읽는다.
#           파일을 고치고 즉시 반영하려면 프로세스를 다시 띄운다.
@lru_cache(maxsize=1)
def _rules() -> Mapping[str, Any]:
    return broker_rules.load_rules()


@lru_cache(maxsize=1)
def _tca_settings():
    return tca_memory.load_settings()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# ── 토론 근거 (Bull / Bear) ────────────────────────────────────────────────
def _debate_evidence(payload: Mapping[str, Any], plan: RAGPlan, *, side: str) -> dict[str, Any]:
    """자기 쪽 산출 + Claim id + 쟁점 목록. **상대 원문은 넣지 않는다.**"""
    debate = _mapping(payload.get("debate"))
    if not debate:
        return {"debate": {"checked": False,
                           "reason": "토론 산출이 없다 — 논지를 추정해 단정하지 마십시오"}}
    contested = _mapping(debate.get("contested"))
    mine = _mapping(debate.get(side))
    # 2라운드 보강도 **자기 것만** 읽는다. 키가 side 로 고정돼 있어 상대 r2 는 닿지 않는다.
    mine_r2 = _mapping(debate.get(f"{side}_r2"))
    opponent = "bear" if side == "bull" else "bull"
    return {"debate": {
        "side": side,
        # Claim 은 id 목록만. 원문 전체를 실으면 프롬프트가 폭증한다.
        "claims": sorted(debate.get("claims") or {}),
        "my_case": {k: v for k, v in mine.items() if k != "claim_refs"} if mine else None,
        "my_round2": ({k: v for k, v in mine_r2.items() if k != "claim_refs"}
                      if mine_r2 else None),
        "my_refs": sorted(set(mine.get("claim_refs") or [])
                          | set(mine_r2.get("claim_refs") or [])),
        # 상대는 **어떤 Claim id 를 인용했는지만** 알 수 있다. 문장은 안 준다.
        "opponent_refs": sorted(contested.get(f"{opponent}_only_refs") or []),
        "contested_refs": sorted(contested.get("contested_refs") or []),
        "untouched_refs": sorted(contested.get("untouched_refs") or []),
        "grounded": bool(debate.get("grounded")),
        "order_intent_proposal": {
            k: _mapping(debate.get("order_intent_proposal")).get(k)
            for k in ("available", "reason", "submittable", "risk_gate_required")},
        "rule": (f"{opponent} 의 문장은 제공되지 않습니다. 상대 주장을 추측해 인용하지 마십시오. "
                 "claim: 인용은 위 claims 목록 안에서만 가능합니다."),
    }}


def bull_debate_evidence(payload: Mapping[str, Any], plan: RAGPlan) -> dict[str, Any]:
    return _debate_evidence(payload, plan, side="bull")


def bear_debate_evidence(payload: Mapping[str, Any], plan: RAGPlan) -> dict[str, Any]:
    return _debate_evidence(payload, plan, side="bear")


# ── 상태 전이표 근거 ───────────────────────────────────────────────────────
def _outgoing(table, enum, current: str | None) -> dict[str, Any]:
    if current is None:
        return {"current_state": None,
                "all_transitions": sorted(f"{a}->{b}" for a, b in table)}
    try:
        state = enum(current)
    except ValueError:
        return {"current_state": current, "unknown_state": True, "outgoing": []}
    return {"current_state": str(state),
            "outgoing": sorted(f"{a}->{b}" for a, b in table if a is state)}


def state_machine_evidence(payload: Mapping[str, Any], plan: RAGPlan) -> dict[str, Any]:
    """제약 매핑에 필요한 것: 지금 상태에서 어디로 갈 수 있나 + 승인 매핑 규칙."""
    if not allows(plan, "graph_context"):
        return {"state_transitions": route_denied("desk-runner-worker", plan,
                                                  "graph_context")}
    intent = _mapping(payload.get("order_intent"))
    order = _mapping(payload.get("broker_order"))
    return {"state_transitions": {
        "intent": _outgoing(INTENT_TRANSITIONS, IntentState, intent.get("intent_status")),
        "broker": _outgoing(BROKER_TRANSITIONS, BrokerOrderState, order.get("state")),
        "max_hops": plan.max_hops,
        # risk_gate 매핑 규칙 요약. 직원이 verdict 를 새로 만들지 못하게 한다.
        "risk_mapping": {
            "verdicts": ["APPROVE", "RESIZE", "REJECT"],
            "risk_decision_id": "필수 — 없으면 제출 불가(RISK_APPROVED 는 상태가 아니라 전제조건)",
            "entry_allowed_trading_states": sorted(ENTRY_ALLOWED_TRADING_STATES),
            "kill_switch": "trading_state 가 목록 밖이면 verdict 가 APPROVE 여도 거부된다",
        },
        "rule": ("state: 인용은 위 outgoing 목록 안에서만 가능합니다. "
                 "없는 전이를 만들지 마십시오. 두 머신의 상태를 섞지 마십시오."),
        "decided_by": "deterministic",
    }}


# ── 브로커 규칙 근거 ───────────────────────────────────────────────────────
_RULE_QUERY = ("CSPAT00601 현물주문 CSPAT00701 현물정정주문 CSPAT00801 현물취소주문 "
               "t0424 주식잔고 CSPAQ12200 예수금 /stock/order /stock/accno")

_PLAN_FIELDS = ("slices", "window_minutes", "replaces_per_slice", "cancels",
                "account_polls_per_minute", "adapter")


def _plan_draft(payload: Mapping[str, Any]) -> ExecutionPlanDraft | None:
    plan = _mapping(payload.get("execution_plan"))
    if plan.get("slices") is None or plan.get("window_minutes") is None:
        return None
    kwargs = {k: plan[k] for k in _PLAN_FIELDS if plan.get(k) is not None}
    kwargs["slices"] = int(kwargs["slices"])
    kwargs["window_minutes"] = float(kwargs["window_minutes"])
    return ExecutionPlanDraft(**kwargs)


def broker_rules_evidence(payload: Mapping[str, Any], plan: RAGPlan) -> dict[str, Any]:
    if not allows(plan, "lexical"):
        return {"broker_rules": route_denied("desk-runner-worker", plan, "lexical")}
    rules = _rules()
    adapter = str(_mapping(payload.get("execution_plan")).get("adapter") or "")
    context = broker_rules.rule_context(f"{_RULE_QUERY} {adapter}", k=_MAX_RULES, rules=rules)
    draft = _plan_draft(payload)
    if draft is None:
        feasibility: dict[str, Any] = {
            "checked": False,
            "reason": "execution_plan 에 slices/window_minutes 가 없어 검사하지 않았다"}
    else:
        feasibility = broker_rules.check_plan_feasible(draft, rules=rules)
        feasibility["plan_source"] = _mapping(payload.get("execution_plan")).get("source")
        feasibility["plan_approved"] = bool(
            _mapping(payload.get("execution_plan")).get("approved"))
    return {"broker_rules": context, "plan_feasibility": feasibility}


# ── 집행 기억(TCA) 근거 ────────────────────────────────────────────────────
def _records(payload: Mapping[str, Any]) -> tuple[list[Any], str]:
    raw = payload.get("tca_records")
    if raw:
        records = [r if isinstance(r, tca_memory.ExecutionRecord)
                   else tca_memory.ExecutionRecord(**dict(r)) for r in raw]
        return records, "payload.tca_records"
    mapping = payload.get("philosophy_by_strategy")
    if mapping:
        records, _unmapped = tca_memory.fetch_records(philosophy_by_strategy=dict(mapping))
        return records, "execution.tca_results"
    return [], ""


def tca_evidence(payload: Mapping[str, Any], plan: RAGPlan) -> dict[str, Any]:
    """과거 유사 집행의 실현 슬리피지. **Paper 근거는 시뮬레이션으로 표시된다.**"""
    if not allows(plan, "structured_filter"):
        return {"tca_memory": route_denied("desk-runner-worker", plan, "structured_filter")}
    try:
        records, source = _records(payload)
    except (TcaMemoryError, TypeError) as exc:
        return {"tca_memory": {"checked": False, "error": type(exc).__name__,
                               "detail": str(exc)[:200]}}
    if not records:
        return {"tca_memory": {"checked": False, "groups": {},
                               "reason": "집행 기억 없음 — tca_records 도 DATABASE_URL 매핑도 없다"}}

    settings = _tca_settings()
    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        bucket = tca_memory.notional_bucket(record.notional, settings)
        key = f"{record.philosophy}/{record.side}/{bucket}/{record.adapter}"
        slot = groups.setdefault(key, {"samples": 0, "adapter": record.adapter,
                                       "simulated": record.adapter in tca_memory.SIMULATED_ADAPTERS})
        slot["samples"] += 1
    for slot in groups.values():
        slot["sufficient"] = slot["samples"] >= settings.min_samples
    top = dict(sorted(groups.items(), key=lambda kv: -kv[1]["samples"])[:_MAX_TCA_GROUPS])

    adapter = str(_mapping(payload.get("execution_plan")).get("adapter") or "paper")
    try:
        proposals = tca_memory.propose_adjustments(records, settings=settings, adapter=adapter)
    except (TcaMemoryError, KeyError) as exc:
        proposals = {"proposals": [], "error": type(exc).__name__}
    return {"tca_memory": {
        "checked": True, "source": source, "groups": top,
        "min_samples": settings.min_samples,
        # 이 근거가 시뮬레이션인지가 신뢰도를 가른다. 최상단에 올린다.
        "evidence_is_simulated": bool(proposals.get("evidence_is_simulated")),
        "proposals": proposals.get("proposals", []),
        "authoritative": False,
        "rule": ("tca: 인용은 위 groups 키 중 sufficient=true 인 것만 가능합니다. "
                 "제안은 제안일 뿐이며 한도 반영은 리스크본부 권한입니다."),
    }}


# ── Certification 근거 ─────────────────────────────────────────────────────
def certification_evidence(payload: Mapping[str, Any], plan: RAGPlan) -> dict[str, Any]:
    """파생 접수 게이트. **없는 인증을 있는 것으로 만들지 않는다.**"""
    profile = payload.get("capability_profile")
    certified = set(getattr(profile, "certified_by", None)
                    or _mapping(profile).get("certified_by") or [])
    asset_class = str(_mapping(payload.get("derivatives")).get("asset_class")
                      or _mapping(payload.get("order_intent")).get("asset_class") or "").upper()
    body: dict[str, Any] = {
        "certification_required_assets": sorted(CERTIFICATION_REQUIRED),
        "required_certifiers": sorted(DERIVATIVE_CERTIFIERS),
        "asset_class": asset_class or None,
        "certified_by": sorted(certified),
        "missing": sorted(DERIVATIVE_CERTIFIERS - certified),
        "decided_by": "deterministic",
        "rule": ("cert: 인용은 위 certified_by / missing 안에서만 가능합니다. "
                 "서명이 없으면 구조를 제안하지 말고 차단 사유만 서술하십시오."),
    }
    if profile is None:
        # 프로필이 없으면 "허용"으로 떨어뜨리지 않는다 (개발 원칙 9).
        body["verdict_unknown"] = True
        body["reason"] = "capability_profile 부재 — 인증 여부를 알 수 없다"
    else:
        body["blocked"] = bool(asset_class in CERTIFICATION_REQUIRED and body["missing"])
    return {"certification": body}


# ── OrderIntent 제안 현황 근거 ─────────────────────────────────────────────
def proposal_evidence(payload: Mapping[str, Any], _plan: RAGPlan) -> dict[str, Any]:
    """제안이 있는지·왜 없는지. **검색이 아니라 상태 조회라 경로 검사가 없다.**

    `propose_intent()` 가 이미 결정론으로 만든 결과를 옮기기만 한다 - 여기서 수량·가격을
    다시 계산하면 같은 규칙이 두 곳에 생긴다.
    """
    proposal = (_mapping(payload.get("order_intent_proposal"))
                or _mapping(_mapping(payload.get("debate")).get("order_intent_proposal")))
    if not proposal:
        return {"order_intent_proposal": {
            "checked": False,
            "reason": "제안 블록이 없다 — 토론이 propose_intent 전에 끝났거나 호출자가 안 줬다"}}
    return {"order_intent_proposal": {
        "checked": True,
        **{k: proposal.get(k) for k in ("available", "reason", "detail",
                                        "submittable", "risk_gate_required")},
        # 제안은 주문이 아니다. 러너가 옮겨도 이 사실은 안 바뀐다.
        "decided_by": "deterministic",
        "rule": "제안은 Risk 판정 전이며 그 자체로 제출 권한이 아니다",
    }}


PROVIDERS: dict[str, tuple[EvidenceProvider, ...]] = {
    "bull-thesis-worker": (bull_debate_evidence,),
    "bear-thesis-worker": (bear_debate_evidence,),
}

# desk-runner(결정론 잡무)가 한 번에 도는 provider. **정책표가 아니라 실행 목록이다.**
# 이 목록이 곧 "러너가 무엇을 볼 수 있는가"이며 payload 로 바뀌지 않는다 - 그래서
# rag_router 의 정책표에 항목을 두지 않아도 감사가 된다.
DESK_RUNNER_PROVIDERS: tuple[EvidenceProvider, ...] = (
    proposal_evidence,        # 구 trade-proposal-worker
    state_machine_evidence,   # 구 order-constraint-worker
    broker_rules_evidence,    # 구 execution-planning-worker
    tca_evidence,             # 구 execution-planning / venue-cost-worker
    certification_evidence,   # 구 derivatives-structure-worker
)


def _safe(provider: EvidenceProvider, payload: Mapping[str, Any], plan: RAGPlan) -> dict[str, Any]:
    """provider 예외를 흡수한다. 근거 하나가 죽어도 직원 그래프는 안 죽고,
    대신 checked=False 로 남아 조용히 통과하지 않는다."""
    try:
        return dict(provider(payload, plan))
    except (BrokerRuleError, TcaMemoryError, Exception) as exc:  # noqa: BLE001 - 근거 경계
        return {f"{provider.__name__}_error": {
            "checked": False, "error": type(exc).__name__, "detail": str(exc)[:200]}}


def desk_runner_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """desk-runner 가 모으는 사실 전체. **LLM 을 부르지 않는다.**

    payload 로 경로를 못 바꾼다 - `DETERMINISTIC_PLAN` 고정이라 `choose_rag_route()` 를
    거치지 않는다. provider 하나가 죽어도 `_safe` 가 `checked: False` 로 남기므로
    러너 보고가 통째로 사라지지 않는다.
    """
    evidence: dict[str, Any] = {"rag_plan": DETERMINISTIC_PLAN.as_dict(),
                                "decided_by": "deterministic", "authoritative": False}
    for provider in DESK_RUNNER_PROVIDERS:
        evidence.update(_safe(provider, payload, DETERMINISTIC_PLAN))
    return evidence


def grounded_tool(base, worker_id: str, *,
                  capture: MutableMapping[str, dict] | None = None):
    """기존 read-only tool 을 감싸 근거와 RAG 플랜을 evidence 에 얹는다.

    capture 는 인용 검증기가 "이 직원이 실제로 뭘 받았나"를 알아야 해서 있다.
    직원마다 자기 키만 쓰므로 async fan-out 에서도 안전하다.
    """
    providers = PROVIDERS.get(worker_id, ())

    def read_context(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        evidence = dict(base(payload))
        plan = choose_rag_route(payload, worker_id=worker_id)
        evidence["rag_plan"] = plan.as_dict()
        for provider in providers:
            evidence.update(_safe(provider, payload, plan))
        if capture is not None:
            capture[worker_id] = evidence
        return evidence

    return read_context


if __name__ == "__main__":
    import json
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    BULL_TEXT = "고정가 상승이 이익 레버리지로 이어진다"
    BEAR_TEXT = "가격 상승분이 이미 주가에 반영됐다"
    DEBATE = {
        "claims": {"fact:0": "DRAM 고정가 상승", "fact:1": "컨센서스 상향",
                   "invalid:0": "고정가 반전"},
        "bull": {"bull_case": BULL_TEXT, "claim_refs": ["fact:0", "fact:1"]},
        "bear": {"bear_case": BEAR_TEXT, "claim_refs": ["fact:0", "invalid:0"]},
        "contested": {"contested_refs": ["fact:0"], "bull_only_refs": ["fact:1"],
                      "bear_only_refs": ["invalid:0"], "untouched_refs": []},
        "grounded": True,
        "order_intent_proposal": {"available": True, "submittable": False,
                                  "risk_gate_required": True, "reason": None},
    }

    def plan_for(worker_id: str) -> RAGPlan:
        return choose_rag_route({}, worker_id=worker_id)

    RUNNER = DETERMINISTIC_PLAN   # desk-runner 는 정책표가 아니라 고정 플랜을 쓴다

    # 1. **Bull evidence 에 Bear 원문이 없다** — 이 파일의 존재 이유
    bull = bull_debate_evidence({"debate": DEBATE}, plan_for("bull-thesis-worker"))
    dumped = json.dumps(bull, ensure_ascii=False)
    assert BEAR_TEXT not in dumped, "Bull 근거에 Bear 원문이 샜다"
    assert BULL_TEXT in dumped, "자기 쪽 논지가 없다"
    assert bull["debate"]["opponent_refs"] == ["invalid:0"], bull["debate"]
    assert bull["debate"]["claims"] == ["fact:0", "fact:1", "invalid:0"]
    bear = bear_debate_evidence({"debate": DEBATE}, plan_for("bear-thesis-worker"))
    assert BULL_TEXT not in json.dumps(bear, ensure_ascii=False), "Bear 근거에 Bull 원문이 샜다"
    assert bear["debate"]["opponent_refs"] == ["fact:1"]
    # 토론이 없으면 없다고 적는다
    assert bull_debate_evidence({}, plan_for("bull-thesis-worker"))["debate"]["checked"] is False
    print("  상대 원문 차단 (편향)       OK")

    # 2. 상태 전이 — 현재 상태에서 나가는 간선만
    state = state_machine_evidence(
        {"order_intent": {"intent_status": "APPROVED"}}, RUNNER)["state_transitions"]
    assert state["intent"]["current_state"] == "APPROVED"
    assert "APPROVED->READY_TO_SUBMIT" in state["intent"]["outgoing"]
    assert "DRAFT->RISK_PENDING" not in state["intent"]["outgoing"], "무관한 간선이 실렸다"
    assert state["risk_mapping"]["entry_allowed_trading_states"] == ["ENABLED"]
    # 종단 상태는 나가는 간선이 없다
    dead = state_machine_evidence({"order_intent": {"intent_status": "REJECTED"}}, RUNNER)
    assert dead["state_transitions"]["intent"]["outgoing"] == []
    # 상태 미상이면 전체 표를 주되 current_state 를 null 로 명시한다
    unknown = state_machine_evidence({}, RUNNER)
    assert unknown["state_transitions"]["intent"]["current_state"] is None
    assert unknown["state_transitions"]["intent"]["all_transitions"]
    print("  상태 전이 근거              OK")

    # 3. **라우터에 복종한다** — NO_RAG 직원에게 검색 provider 를 붙여도 안 부른다
    denied = broker_rules_evidence({}, plan_for("bull-thesis-worker"))
    assert denied["broker_rules"]["checked"] is False
    assert denied["broker_rules"]["reason"] == "route_denied:NO_RAG"
    assert tca_evidence({}, plan_for("bull-thesis-worker"))["tca_memory"]["checked"] is False
    assert state_machine_evidence({}, plan_for("bear-thesis-worker"))[
        "state_transitions"]["reason"] == "route_denied:NO_RAG"
    print("  라우터 복종 (route_denied)  OK")

    # 4. 브로커 규칙 + 분할 판정 — 프리셋 초안이 실제 판정을 낸다
    exec_plan = {"slices": 40, "window_minutes": 0.1667, "replaces_per_slice": 2,
                 "adapter": "ls-live", "source": "philosophies.yaml:momentum",
                 "approved": False}
    rules_ev = broker_rules_evidence({"execution_plan": exec_plan}, RUNNER)
    assert "ls:CSPAT00701" in rules_ev["broker_rules"]
    feasible = rules_ev["plan_feasibility"]
    assert feasible["feasible"] is False and feasible["min_window_seconds"] == 40.0
    assert feasible["plan_source"] == "philosophies.yaml:momentum"
    assert feasible["plan_approved"] is False, "검사용 초안이 승인된 계획으로 보인다"
    # 계획이 없으면 없다고 적는다
    bare = broker_rules_evidence({}, RUNNER)
    assert bare["plan_feasibility"]["checked"] is False
    print("  브로커 규칙 + 분할 판정     OK")

    # 5. 집행 기억 — 그룹과 시뮬레이션 표시
    def record(**over):
        kw = {"order_id": "o", "philosophy": "momentum", "side": "BUY", "adapter": "ls-live",
              "notional": Decimal("20000000"), "slippage_bps": Decimal("60"), "slices": 5,
              "participation_rate": None, "calculated_at": now - timedelta(days=1)}
        kw.update(over)
        return tca_memory.ExecutionRecord(**kw)

    live = tca_evidence({"tca_records": [record(order_id=f"o{i}") for i in range(25)],
                         "execution_plan": {"adapter": "ls-live"}}, RUNNER)["tca_memory"]
    assert live["checked"] is True and live["source"] == "payload.tca_records"
    key = "momentum/BUY/mid/ls-live"
    assert live["groups"][key]["samples"] == 25 and live["groups"][key]["sufficient"] is True
    assert live["evidence_is_simulated"] is False and live["proposals"]
    # 표본이 적으면 sufficient 가 아니다 -> tca: 인용이 막힌다
    thin = tca_evidence({"tca_records": [record(order_id=f"t{i}") for i in range(3)]},
                        RUNNER)["tca_memory"]
    assert thin["groups"][key]["sufficient"] is False
    # Paper 근거는 시뮬레이션으로 표시된다
    sim = tca_evidence({"tca_records": [record(order_id=f"p{i}", adapter="paper")
                                        for i in range(25)],
                        "execution_plan": {"adapter": "paper"}}, RUNNER)["tca_memory"]
    assert sim["evidence_is_simulated"] is True
    assert sim["groups"]["momentum/BUY/mid/paper"]["simulated"] is True
    assert tca_evidence({}, RUNNER)["tca_memory"]["checked"] is False
    print("  집행 기억 그룹 + 시뮬 표시  OK")

    # 6. Certification — 프로필 없으면 허용으로 안 떨어진다
    none_profile = certification_evidence({"derivatives": {"asset_class": "FUTURE"}}, RUNNER)
    assert none_profile["certification"]["verdict_unknown"] is True
    assert none_profile["certification"]["missing"] == ["accounting", "broker", "risk"]

    from uuid import uuid4

    from derivatives import CapabilityProfile

    partial = CapabilityProfile(capability_profile_id=uuid4(), profile_code="P", version=1,
                                certified_by=frozenset({"broker"}))
    blocked = certification_evidence({"capability_profile": partial,
                                      "derivatives": {"asset_class": "FUTURE"}},
                                     RUNNER)["certification"]
    assert blocked["blocked"] is True and blocked["certified_by"] == ["broker"]
    assert blocked["missing"] == ["accounting", "risk"]
    full = CapabilityProfile(capability_profile_id=uuid4(), profile_code="P", version=1,
                             certified_by=frozenset(DERIVATIVE_CERTIFIERS))
    allowed = certification_evidence({"capability_profile": full,
                                      "derivatives": {"asset_class": "FUTURE"}},
                                     RUNNER)["certification"]
    assert allowed["blocked"] is False and allowed["missing"] == []
    print("  Certification 게이트        OK")

    # 7. provider 예외가 밖으로 안 나간다
    def boom(_payload, _plan):
        raise RuntimeError("근거 소스 장애")

    boom.__name__ = "boom"
    caught = _safe(boom, {}, RUNNER)
    assert caught["boom_error"]["checked"] is False
    assert caught["boom_error"]["error"] == "RuntimeError"
    print("  provider 예외 흡수          OK")

    # 8. grounded_tool 배선 — RAG 플랜과 capture (LLM 직원 전용)
    def base(payload):
        return {"worker_id": "bull-thesis-worker", "evidence": dict(payload)}

    capture: dict = {}
    tool = grounded_tool(base, "bull-thesis-worker", capture=capture)
    out = tool({"debate": DEBATE})
    assert out["rag_plan"]["route"] == "NO_RAG"
    assert "debate" in out and "broker_rules" not in out, "LLM 직원에 검색 근거가 붙었다"
    assert capture["bull-thesis-worker"] is out, "capture 가 evidence 를 안 담았다"
    print("  grounded_tool 배선          OK")

    # 8b. **desk-runner 는 다섯 근거를 한 번에 모은다** — 조건부 직원 4명의 대체물
    runner_ev = desk_runner_evidence({
        "debate": DEBATE, "execution_plan": exec_plan,
        "order_intent": {"intent_status": "APPROVED", "asset_class": "EQUITY"},
        "tca_records": [record(order_id=f"r{i}") for i in range(25)],
        "derivatives": {"asset_class": "FUTURE"},
    })
    for key_name in ("order_intent_proposal", "state_transitions", "broker_rules",
                     "plan_feasibility", "tca_memory", "certification"):
        assert key_name in runner_ev, f"러너 근거에 {key_name} 이 없다"
    assert runner_ev["order_intent_proposal"]["available"] is True
    assert runner_ev["state_transitions"]["intent"]["current_state"] == "APPROVED"
    assert runner_ev["tca_memory"]["checked"] is True
    assert runner_ev["certification"]["verdict_unknown"] is True   # 프로필 없음 -> 허용 아님
    assert runner_ev["rag_plan"]["route"] == DETERMINISTIC_PLAN.route
    assert runner_ev["authoritative"] is False
    # **상대 원문이 아니라 아예 토론 서술이 안 들어간다** - 러너는 사실만 옮긴다
    dumped_runner = json.dumps(runner_ev, ensure_ascii=False, default=str)
    assert BULL_TEXT not in dumped_runner and BEAR_TEXT not in dumped_runner
    # 제안이 없으면 없다고 적는다
    assert desk_runner_evidence({})["order_intent_proposal"]["checked"] is False
    print("  desk-runner 근거 5종        OK")

    # 9. 프롬프트 폭증 방지 — provider 가 얹는 근거의 크기 상한.
    #    공용 런타임이 evidence JSON 을 8000자에서 자르므로 상한이 없으면 뒤에 붙은
    #    근거가 조용히 잘려나간다. 실제 base 는 input_fields 만 노출하므로(원본 payload 를
    #    통째로 되싣지 않는다) 여기서도 같은 조건으로 잰다.
    big = desk_runner_evidence({
        "execution_plan": exec_plan,
        "order_intent": {"intent_status": "APPROVED"},
        "market_snapshot": {"bid": "69900", "ask": "70100"},
        "tca_records": [record(order_id=f"x{i}", philosophy=p, side=s)
                        for i in range(200)
                        for p, s in [("momentum", "BUY")]],
    })
    size = len(json.dumps(big, ensure_ascii=False, default=str))
    assert size < 8000, f"evidence 가 너무 크다: {size}자"
    assert len(big["tca_memory"]["groups"]) <= _MAX_TCA_GROUPS
    print(f"  evidence 크기 상한 ({size}자)  OK")

    print("ok - 직원 근거 주입 10개 영역 점검 통과 "
          f"(LLM 직원 {len(PROVIDERS)}명 + desk-runner provider "
          f"{len(DESK_RUNNER_PROVIDERS)}개, 상대 원문 차단, 라우터 복종)")
