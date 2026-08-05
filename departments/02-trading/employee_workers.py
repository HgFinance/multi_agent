"""Trading employee Worker registry: proposals and execution plans, never order submission.

execution-planning-worker / venue-cost-worker 는 다른 직원과 달리 **거래소·브로커 규칙
근거를 주입받는다** (2026-08-05). 두 가지를 한다:

  1. 근거 주입 - 두 직원의 tool evidence 에 `execution/broker_rules.py` 가 검색한 규칙
     표와 결정론적 실현가능성 판정을 넣는다. 규칙 숫자를 기억에서 꺼내지 않게 한다.
  2. 인용 검증 - 직원이 낸 `evidence_refs` 중 규칙 색인에 없는 rule_id 는 날조다.
     하나라도 있으면 그 직원 보고는 escalate 된다(승인 방향으로 fallback 하지 않는다).

**초당 한도를 넘는 분할 설계는 서술로 통과할 수 없다.** 판정은 LLM 이 아니라
`check_plan_feasible()` 이 하고, 결과가 evidence 에 먼저 들어간 채로 직원이 서술한다.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_BASE = Path(__file__).resolve().parent
# 직접 실행(자체 점검)일 때도 departments 패키지를 찾게 한다. import 로 쓰일 때는 이미 잡혀 있다.
for _p in (str(_BASE.parents[1]), str(_BASE.parent)):
    if _p not in sys.path:
        sys.path.append(_p)

try:
    from departments.employee_worker_runtime import (
        WorkerLLM,
        WorkerSpec,
        run_worker_registry,
        tools_for_specs,
    )
except ModuleNotFoundError:
    from employee_worker_runtime import (
        WorkerLLM,
        WorkerSpec,
        run_worker_registry,
        tools_for_specs,
    )

sys.path.insert(0, str(Path(__file__).resolve().parent / "execution"))

from broker_rules import (  # noqa: E402 - sys.path 조정 뒤라야 import 된다
    BrokerRuleError,
    ExecutionPlanDraft,
    check_plan_feasible,
    rule_context,
    verify_citations,
)

WORKER_SPECS = (
    WorkerSpec("market-thesis-worker", "Bull and bear market-thesis debate analyst", ("trading.research_packet.read",), "always", ("research_packet", "market_snapshot")),
    WorkerSpec("trade-proposal-worker", "Trade proposal and OrderIntent analyst", ("trading.portfolio_state.read",), "always", ("research_packet", "portfolio_snapshot", "strategy_bundle")),
    WorkerSpec("order-constraint-worker", "Risk and compliance constraint mapping analyst", ("trading.risk_decision.read",), "risk_decision", ("risk_decision", "order_constraints")),
    WorkerSpec("execution-planning-worker", "Risk-approved execution planning analyst", ("trading.execution_constraints.read",), "approved_risk", ("risk_decision", "order_constraints", "market_snapshot", "execution_plan")),
    WorkerSpec("venue-cost-worker", "Broker venue, slippage and transaction-cost analyst", ("trading.venue_cost.read",), "execution_request", ("order_intent", "market_snapshot", "venue_costs", "execution_plan")),
    WorkerSpec("derivatives-structure-worker", "Derivatives structure and margin planning analyst", ("trading.derivatives.read",), "derivatives_signal", ("derivatives", "risk_decision")),
)

# 규칙 근거를 받는 직원. 나머지 직원은 브로커 한도를 다루지 않으므로 주입하지 않는다.
RULE_GROUNDED_WORKERS = frozenset({"execution-planning-worker", "venue-cost-worker"})

_PLAN_FIELDS = ("slices", "window_minutes", "replaces_per_slice", "cancels",
                "account_polls_per_minute", "adapter")


def _plan_draft(payload: Mapping[str, Any]) -> ExecutionPlanDraft | None:
    """payload 의 execution_plan 을 검사 가능한 초안으로 읽는다. 없는 값은 채우지 않는다."""
    plan = payload.get("execution_plan")
    if not isinstance(plan, Mapping):
        return None
    if plan.get("slices") is None or plan.get("window_minutes") is None:
        return None
    kwargs = {k: plan[k] for k in _PLAN_FIELDS if plan.get(k) is not None}
    kwargs["slices"] = int(kwargs["slices"])
    kwargs["window_minutes"] = float(kwargs["window_minutes"])
    return ExecutionPlanDraft(**kwargs)


def _rule_query(payload: Mapping[str, Any]) -> str:
    """어떤 규칙을 뽑을지. 집행 계획은 주문·정정·취소·잔고 TR 을 항상 함께 본다."""
    adapter = (payload.get("execution_plan") or {}).get("adapter", "") if isinstance(
        payload.get("execution_plan"), Mapping) else ""
    return ("CSPAT00601 현물주문 CSPAT00701 현물정정주문 CSPAT00801 현물취소주문 "
            f"t0424 주식잔고 CSPAQ12200 예수금 /stock/order /stock/accno {adapter}")


def _grounded_tool(base):
    """기존 read-only tool 을 감싸 규칙 근거와 결정론 판정을 evidence 에 얹는다."""

    def read_context(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        evidence = dict(base(payload))
        try:
            evidence["broker_rules"] = rule_context(_rule_query(payload), k=6)
            draft = _plan_draft(payload)
            evidence["plan_feasibility"] = (
                check_plan_feasible(draft) if draft is not None
                else {"checked": False,
                      "reason": "execution_plan 에 slices/window_minutes 가 없어 검사하지 않았다"})
        except BrokerRuleError as exc:
            # 규칙을 못 읽으면 근거 없이 서술하게 두지 않는다 - 막는 쪽으로 떨어진다.
            evidence["broker_rules"] = f"규칙을 읽을 수 없습니다: {exc}"
            evidence["plan_feasibility"] = {"checked": False, "feasible": False,
                                            "reason": type(exc).__name__}
        return evidence

    return read_context


def trading_tools() -> dict[str, Any]:
    tools = dict(tools_for_specs(WORKER_SPECS))
    for worker_id in RULE_GROUNDED_WORKERS:
        tools[worker_id] = _grounded_tool(tools[worker_id])
    return tools


def _apply_rule_citation_checks(result: dict[str, Any]) -> dict[str, Any]:
    """규칙 근거를 받은 직원의 인용을 검증한다. 날조가 있으면 escalate 한다."""
    for report in result.get("workers", []):
        if report.get("worker_id") not in RULE_GROUNDED_WORKERS:
            continue
        output = report.get("output") or {}
        refs = [r for r in (output.get("evidence_refs") or []) if str(r).startswith("ls:")]
        try:
            checked = verify_citations(refs)
        except BrokerRuleError as exc:
            checked = {"refs": refs, "unknown_refs": [], "uncited": not refs,
                       "grounded": False, "error": type(exc).__name__, "detail": str(exc)}
        report["rule_citations"] = checked
        if checked["unknown_refs"] or checked.get("error"):
            output["escalate"] = True
            report["status"] = "DEGRADED"
            result["degraded"] = True
            if report["worker_id"] not in result.get("failed", []):
                result.setdefault("failed", []).append(report["worker_id"])
            if report["worker_id"] in result.get("executed", []):
                result["executed"].remove(report["worker_id"])
    return result


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    result = run_worker_registry(WORKER_SPECS, payload, tools=trading_tools(), llm=llm)
    return _apply_rule_citation_checks(result)


if __name__ == "__main__":
    import json

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # 초당 3건 한도를 넘기는 분할 계획. 직원이 서술하기 **전에** 판정이 끝나 있어야 한다.
    payload = {
        "approved_risk": True, "execution_request": True,
        "risk_decision": {"verdict": "APPROVE"}, "order_intent": {"quantity": "300"},
        "market_snapshot": {"bid": "69900", "ask": "70100"},
        "execution_plan": {"slices": 40, "window_minutes": 0.1667,
                           "replaces_per_slice": 2, "adapter": "ls-live"},
    }

    tools = trading_tools()
    evidence = tools["execution-planning-worker"](payload)
    assert "ls:CSPAT00701" in evidence["broker_rules"], evidence["broker_rules"]
    assert "만들어 쓰지" in evidence["broker_rules"], "규칙 밖 숫자 금지 문구가 없다"
    verdict = evidence["plan_feasibility"]
    assert verdict["feasible"] is False, verdict
    assert verdict["min_window_seconds"] == 40.0, verdict
    assert verdict["decided_by"] == "deterministic", "판정을 LLM 이 하게 뒀다"
    print("  규칙 근거 주입 + 한도 판정   OK")

    # 계획이 없으면 없다고 적는다 - 없는 계획을 가능하다고 하지 않는다
    bare = tools["venue-cost-worker"]({"execution_request": True})
    assert bare["plan_feasibility"]["checked"] is False, bare["plan_feasibility"]
    print("  계획 부재 시 미검사 표기     OK")

    # 규칙 근거를 안 받는 직원은 그대로다 (주입 대상이 아닌 직원까지 바꾸지 않는다)
    other = tools["trade-proposal-worker"]({"research_packet": {"symbol": "005930"}})
    assert "broker_rules" not in other, other
    print("  비대상 직원 불변             OK")

    # 색인 밖 rule_id 를 인용하면 그 직원 보고가 escalate 된다
    fake = {"workers": [{"worker_id": "venue-cost-worker", "status": "COMPLETED",
                         "output": {"summary": "s", "evidence_refs": ["ls:CSPAT99999"],
                                    "escalate": False}}],
            "executed": ["venue-cost-worker"], "failed": [], "degraded": False}
    checked = _apply_rule_citation_checks(fake)
    assert checked["workers"][0]["rule_citations"]["unknown_refs"] == ["ls:CSPAT99999"]
    assert checked["workers"][0]["output"]["escalate"] is True
    assert checked["degraded"] is True and checked["executed"] == []
    assert checked["failed"] == ["venue-cost-worker"]

    good = {"workers": [{"worker_id": "venue-cost-worker", "status": "COMPLETED",
                         "output": {"summary": "s", "evidence_refs": ["ls:CSPAT00701"],
                                    "escalate": False}}],
            "executed": ["venue-cost-worker"], "failed": [], "degraded": False}
    ok = _apply_rule_citation_checks(good)
    assert ok["workers"][0]["rule_citations"]["grounded"] is True
    assert ok["degraded"] is False and ok["executed"] == ["venue-cost-worker"]
    print("  인용 날조 -> escalate        OK")

    print("ok - 트레이딩 직원 규칙 근거 4개 영역 점검 통과 "
          "(집행계획·거래비용 직원만 주입, 한도 판정은 결정론)")
    print(json.dumps({"grounded_workers": sorted(RULE_GROUNDED_WORKERS)}, ensure_ascii=False))
