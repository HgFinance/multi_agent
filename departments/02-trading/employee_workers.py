"""Trading employee Worker registry: proposals and execution plans, never order submission.

직원 7명. **Bull 과 Bear 를 별도 직원으로 나눴다**(2026-08-05) — 하나가 양쪽 논지를
다 만들면 그게 확증편향이다. 두 직원은 `scripts.py` 의 `bull_researcher`/`bear_researcher`
노드와 1:1 대응하고, 페르소나도 `config.yaml` 의 TRD-01/TRD-02 원문을 공유한다.

Skill/RAG 경계는 `skills/` 패키지가 소유한다:

  rag_router       직원별 RAG 경로 정책 — **7명 전원 forced.** payload 로 못 바꾼다
  trigger_payload  조건부 직원 트리거 파생 — 승인은 risk_gate 만이 만든다
  worker_evidence  직원별 근거 주입 — Bull/Bear 는 상대 원문을 절대 안 받는다
  citations        네임스페이스 인용 검증 — 색인 밖 인용은 escalate

**paper 경로에서 order-constraint / execution-planning 은 여전히 안 돈다.**
`orchestration/workflows/investment-case.yaml` 이 trading=sequence 2, risk=sequence 3
이라 그 시점에 `risk_decision` 이 없다. 없는 승인을 만들어 넣는 것이 가짜 승인이므로
그렇게 하지 않고 `trigger_provenance` 에 사유를 남긴다.

자체 점검: python departments/02-trading/employee_workers.py
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_BASE = Path(__file__).resolve().parent
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


def _load_skill_package() -> None:
    """`skills/` 를 고유 이름으로 등록한다.

    저장소 루트에도 `skills/`(agentic-rag)가 있고 `__init__.py` 가 없어 namespace
    package 로 잡힌다. flat `import skills` 로 두면 sys.path 순서에 따라 어느 쪽이
    잡힐지 갈리므로, 리스크본부와 같은 방식으로 고유 이름을 붙여 로드한다.
    """
    package_name = "trading_worker_skills"
    if package_name in sys.modules:
        return
    package_dir = _BASE / "skills"
    spec = importlib.util.spec_from_file_location(
        package_name, package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("트레이딩 Skill 패키지를 로드할 수 없습니다")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)


_load_skill_package()

from trading_worker_skills.citations import apply_citation_checks  # noqa: E402
from trading_worker_skills.trigger_payload import enrich_payload  # noqa: E402
from trading_worker_skills.worker_evidence import PROVIDERS, grounded_tool  # noqa: E402

WORKER_SPECS = (
    # Bull 과 Bear 는 **별개 직원**이다. 같은 Research Packet 을 받되 서로의 출력을
    # 절대 입력으로 받지 않는다(worker_evidence 가 상대 원문을 제외한다).
    WorkerSpec("bull-thesis-worker", "Independent bull-case thesis analyst", ("trading.research_packet.read",), "always", ("research_packet", "market_snapshot")),
    WorkerSpec("bear-thesis-worker", "Independent bear-case thesis analyst", ("trading.research_packet.read",), "always", ("research_packet", "market_snapshot")),
    WorkerSpec("trade-proposal-worker", "Trade proposal and OrderIntent analyst", ("trading.portfolio_state.read",), "always", ("research_packet", "portfolio_snapshot", "strategy_bundle")),
    WorkerSpec("order-constraint-worker", "Risk and compliance constraint mapping analyst", ("trading.risk_decision.read",), "risk_decision", ("risk_decision", "order_constraints", "order_intent", "broker_order")),
    WorkerSpec("execution-planning-worker", "Risk-approved execution planning analyst", ("trading.execution_constraints.read",), "approved_risk", ("risk_decision", "order_constraints", "market_snapshot", "execution_plan")),
    WorkerSpec("venue-cost-worker", "Broker venue, slippage and transaction-cost analyst", ("trading.venue_cost.read",), "execution_request", ("order_intent", "market_snapshot", "venue_costs", "execution_plan")),
    WorkerSpec("derivatives-structure-worker", "Derivatives structure and margin planning analyst", ("trading.derivatives.read",), "derivatives_signal", ("derivatives", "risk_decision")),
)

# 근거를 주입받는 직원. worker_evidence.PROVIDERS 가 정본이고 여기서 베끼지 않는다.
GROUNDED_WORKERS = frozenset(PROVIDERS)


def trading_tools(capture: dict[str, dict] | None = None) -> dict[str, Any]:
    """직원별 read-only tool. 근거·RAG 플랜은 `grounded_tool` 이 얹는다."""
    base = tools_for_specs(WORKER_SPECS)
    return {spec.worker_id: grounded_tool(base[spec.worker_id], spec.worker_id,
                                          capture=capture)
            for spec in WORKER_SPECS}


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    """직원 레지스트리 진입점. **트레이딩에 들어오는 유일한 문이다.**

    `orchestration/employee_dispatch.run_department_workers()` 가 여기만 부르므로
    payload 보강도 인용 검증도 여기서 한 번만 일어난다.
    """
    enriched = enrich_payload(payload)
    captured: dict[str, dict] = {}
    result = run_worker_registry(WORKER_SPECS, enriched,
                                 tools=trading_tools(capture=captured), llm=llm)
    result = apply_citation_checks(result, evidence_by_worker=captured)
    # 왜 안 켰는지를 결과에 실어 보낸다 - 침묵하는 not_executed 를 없앤다.
    result["trigger_provenance"] = enriched.get("trigger_provenance", {})
    return result


if __name__ == "__main__":
    import json
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    now = datetime.now(timezone.utc)
    BULL_TEXT = "고정가 상승이 이익 레버리지로 이어진다"
    BEAR_TEXT = "가격 상승분이 이미 주가에 반영됐다"
    DEBATE = {
        "claims": {"fact:0": "DRAM 고정가 상승", "fact:1": "컨센서스 상향"},
        "bull": {"bull_case": BULL_TEXT, "claim_refs": ["fact:0", "fact:1"]},
        "bear": {"bear_case": BEAR_TEXT, "claim_refs": ["fact:0"]},
        "contested": {"contested_refs": ["fact:0"], "bull_only_refs": ["fact:1"],
                      "bear_only_refs": [], "untouched_refs": []},
        "grounded": True,
        "order_intent_proposal": {"available": True, "submittable": False,
                                  "risk_gate_required": True},
    }
    PAYLOAD = {
        "research_packet": {"symbol": "005930"},
        "market_snapshot": {"bid": "69900", "ask": "70100"},
        "debate": DEBATE,
        "philosophy": "momentum",
    }

    def approval() -> dict:
        return {"risk_decision_id": str(uuid4()), "decision": {"verdict": "APPROVE"},
                "trading_state": "ENABLED", "approved_quantity": "100",
                "calculation_version": "risk-engine-v1", "reason_codes": ["LIMIT_OK"],
                "expires_at": (now + timedelta(hours=1)).isoformat()}

    # 1. 직원 7명, Bull/Bear 가 별개 직원이다
    ids = [s.worker_id for s in WORKER_SPECS]
    assert len(WORKER_SPECS) == 7 and len(set(ids)) == 7, ids
    assert "bull-thesis-worker" in ids and "bear-thesis-worker" in ids
    assert "market-thesis-worker" not in ids, "합쳐진 직원이 남아 있다"
    always = [s.worker_id for s in WORKER_SPECS if s.trigger == "always"]
    assert len(always) == 3 and len(WORKER_SPECS) - len(always) == 4
    print("  직원 7명 (Bull/Bear 분리)   OK")

    # 2. **Bull evidence 에 Bear 원문이 없다** - 확증편향 차단의 배선 검사
    tools = trading_tools()
    bull_ev = tools["bull-thesis-worker"](PAYLOAD)
    bear_ev = tools["bear-thesis-worker"](PAYLOAD)
    assert BEAR_TEXT not in json.dumps(bull_ev, ensure_ascii=False), "Bull 에 Bear 원문이 샜다"
    assert BULL_TEXT not in json.dumps(bear_ev, ensure_ascii=False), "Bear 에 Bull 원문이 샜다"
    assert BULL_TEXT in json.dumps(bull_ev, ensure_ascii=False)
    assert bull_ev["rag_plan"]["route"] == bear_ev["rag_plan"]["route"] == "NO_RAG"
    print("  Bull/Bear 상대 원문 차단    OK")

    # 3. 트리거 파생이 실제로 직원을 켠다
    enriched = enrich_payload(PAYLOAD)
    assert enriched["execution_request"] is True     # 제안 + 시세
    assert enriched["approved_risk"] is False        # risk_decision 이 없다
    assert enriched["derivatives_signal"] is False   # 현물
    assert enriched["execution_plan"]["source"] == "philosophies.yaml:momentum"
    prov = enriched["trigger_provenance"]
    assert "sequence 2" in prov["approved_risk"]["reason"], prov["approved_risk"]
    print("  트리거 파생 + 사유 기록     OK")

    # 4. 승인이 있으면 집행 직원이 켜진다
    assert enrich_payload({**PAYLOAD, "risk_decision": approval()})["approved_risk"] is True
    print("  승인 시 집행 직원 활성      OK")

    # 5. 실제 레지스트리 실행 - 스텁 LLM 으로 7명 전원
    def stub(_system: str, prompt: str) -> str:
        worker_id = next((line.split(":", 1)[1].strip() for line in prompt.splitlines()
                          if line.startswith("Worker id:")), "?")
        return json.dumps({"summary": f"ctx for {worker_id}", "confidence": 0.8,
                           "evidence_refs": [], "escalate": False})

    full = {**PAYLOAD, "risk_decision": approval(),
            "derivatives": {"asset_class": "FUTURE"}}
    out = run_employee_workers(full, llm=stub)
    assert out["binding"] is False and out["degraded"] is False, out.get("failed")
    assert set(out["executed"]) == set(ids), sorted(out["executed"])
    assert out["not_executed"] == []
    assert out["trigger_provenance"], "provenance 가 결과에 안 실렸다"
    print("  7명 전원 실행 + provenance  OK")

    # 6. 색인 밖 인용은 그 직원만 escalate
    def faker(_system: str, prompt: str) -> str:
        worker_id = next((line.split(":", 1)[1].strip() for line in prompt.splitlines()
                          if line.startswith("Worker id:")), "?")
        refs = ["ls:CSPAT99999"] if worker_id == "venue-cost-worker" else []
        return json.dumps({"summary": "s", "confidence": 0.5,
                           "evidence_refs": refs, "escalate": False})

    dirty = run_employee_workers(full, llm=faker)
    assert dirty["failed"] == ["venue-cost-worker"], dirty["failed"]
    assert "venue-cost-worker" not in dirty["executed"]
    assert dirty["degraded"] is True
    print("  날조 인용 -> 해당 직원만    OK")

    # 7. **모르는 접두사는 escalate 하지 않는다** - 공용 계약 테스트 호환
    def generic(_system: str, _prompt: str) -> str:
        return json.dumps({"summary": "s", "confidence": 0.8,
                           "evidence_refs": ["test:evidence"], "escalate": False})

    clean = run_employee_workers(full, llm=generic)
    assert clean["failed"] == [] and clean["degraded"] is False, clean["failed"]
    print("  모르는 접두사 통과          OK")

    # 8. 근거 대상 직원과 아닌 직원
    assert GROUNDED_WORKERS == set(PROVIDERS)
    assert "trade-proposal-worker" not in GROUNDED_WORKERS
    plain = tools["trade-proposal-worker"]({"research_packet": {"symbol": "005930"}})
    assert "broker_rules" not in plain and "debate" not in plain
    assert plain["rag_plan"]["route"] == "NO_RAG", "RAG 미배정 직원이 열려 있다"
    print("  근거 대상 직원 구분         OK")

    print("ok - 트레이딩 직원 레지스트리 8개 영역 점검 통과 "
          f"(직원 {len(WORKER_SPECS)}명, 근거 주입 {len(GROUNDED_WORKERS)}명)")
