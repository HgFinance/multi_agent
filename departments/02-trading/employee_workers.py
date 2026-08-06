"""Trading employee Worker registry: proposals and execution plans, never order submission.

직원 3명. **그중 LLM 을 쓰는 것은 2명뿐이다**(2026-08-06).

  bull-thesis-worker   독립 강세 논지 — LLM
  bear-thesis-worker   독립 약세 논지 — LLM
  desk-runner          데스크 잡무 — **LLM 없음.** 결정론 모듈 출력을 그대로 옮긴다

기존 5명(trade-proposal / order-constraint / execution-planning / venue-cost /
derivatives-structure)을 `desk-runner` 하나로 합쳤다. 다섯 다 **답이 하나로 정해지는
일**이었다 — 주문 제안은 `scripts.propose_intent()` 가 `intent_builder` 로 이미 만들고,
제약 매핑은 `contracts` 전이표 조회, 집행 계획은 `philosophies.yaml` 프리셋 +
`broker_rules.check_plan_feasible()`, 비용은 산수, Certification 은 서명 조회다.
그 위에 `qwen3:1.7b` 를 얹어봐야 이미 나온 답을 한국어로 다시 쓰는 것뿐이고, 그
과정에서 회계·집행 수치가 LLM 문장을 거치는 경로만 생긴다(CLAUDE.local.md 원칙 5).

Bull/Bear 만 남긴 이유는 하나다. **같은 근거에서 서로 다르게 나와야 하는 것이 산출물**이고,
둘의 분리 자체가 통제 장치다 — `worker_evidence` 가 상대 원문을 차단하고 `scripts.py` 의
`debate_merge` 가 문장 복제를 세어 위반이면 `grounded=false` 로 떨군다.

Skill/RAG 경계는 `skills/` 패키지가 소유한다:

  rag_router       직원별 RAG 경로 정책 — **LLM 직원 2명만.** desk-runner 는 항목이 없다
  trigger_payload  조건부 트리거 파생 — 승인은 risk_gate 만이 만든다
  worker_evidence  근거 주입 — Bull/Bear 는 상대 원문을 절대 안 받는다
  citations        네임스페이스 인용 검증 — 색인 밖 인용은 escalate

**paper 경로에서 승인 근거는 여전히 안 온다.** `orchestration/workflows/investment-case.yaml`
이 trading=sequence 2, risk=sequence 3 이라 그 시점에 `risk_decision` 이 없다. 없는 승인을
만들어 넣는 것이 가짜 승인이므로 그렇게 하지 않고 `trigger_provenance` 에 사유를 남긴다.
desk-runner 는 그때 상태 전이·Certification 을 `checked: False` 로 보고한다 — 침묵하지 않는다.

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
from trading_worker_skills.rag_router import RAGPlan  # noqa: E402
from trading_worker_skills.trigger_payload import enrich_payload  # noqa: E402
from trading_worker_skills.worker_evidence import (  # noqa: E402
    PROVIDERS,
    broker_rules_evidence,
    certification_evidence,
    grounded_tool,
    state_machine_evidence,
    tca_evidence,
)

# LLM 직원. Bull 과 Bear 는 **별개 직원**이다 — 같은 Research Packet 을 받되 서로의
# 출력을 절대 입력으로 받지 않는다(worker_evidence 가 상대 원문을 제외한다).
WORKER_SPECS = (
    WorkerSpec("bull-thesis-worker", "Independent bull-case thesis analyst", ("trading.research_packet.read",), "always", ("research_packet", "market_snapshot")),
    WorkerSpec("bear-thesis-worker", "Independent bear-case thesis analyst", ("trading.research_packet.read",), "always", ("research_packet", "market_snapshot")),
)

# 근거를 주입받는 직원. worker_evidence.PROVIDERS 가 정본이고 여기서 베끼지 않는다.
GROUNDED_WORKERS = frozenset(PROVIDERS)

# ── desk-runner: 데스크 잡무 (LLM 없음) ────────────────────────────────────
RUNNER_ID = "desk-runner"
RUNNER_ROLE = "Trading desk runner — 결정론 잡무(제약 매핑·집행 실현가능성·비용·인증 조회)"

# 흡수한 5명의 도구 허용목록 합집합. 감사에서 "이 직원이 무엇을 읽었나"가 남아야 한다.
RUNNER_TOOLS = (
    "trading.portfolio_state.read",
    "trading.risk_decision.read",
    "trading.execution_constraints.read",
    "trading.venue_cost.read",
    "trading.derivatives.read",
)

# desk-runner 가 쓰는 근거 수단. **정책표(rag_router)에 항목을 두지 않는다** —
# 라우터는 *LLM 이 무엇을 볼 수 있나*를 제한하는 장치고 이 직원은 LLM 이 없다.
# 서술이 없으니 검색 결과가 편향시킬 서술도 없다. 항목을 비워두면
# `rag_policy_for_worker(RUNNER_ID)` 가 ValueError 를 내는데, 그게 옳은 fail-closed 다:
# 누군가 나중에 desk-runner 를 LLM 경로에 물리면 조용히 열리는 대신 즉시 죽는다.
RUNNER_PLAN = RAGPlan(
    route="HYBRID",
    methods=("lexical", "structured_filter", "graph_context"),
    max_chunks=16, max_hops=2,
    reason="desk-runner 는 LLM 이 없다 — 결정론 모듈 출력을 그대로 옮긴다",
)

_RUNNER_PROVIDERS = (state_machine_evidence, broker_rules_evidence,
                     tca_evidence, certification_evidence)


def desk_runner(payload: Mapping[str, Any]) -> dict[str, Any]:
    """데스크 잡무 담당. **모델을 부르지 않는다.**

    합쳐진 5명이 하던 일을 그대로 한다. 다만 서술을 만들지 않고 결정론 모듈의
    판정을 그대로 옮긴다 — `summary` 필드가 없는 것이 이 직원의 요지다.
    """
    facts: dict[str, Any] = {}
    errors: list[str] = []
    for provider in _RUNNER_PROVIDERS:
        try:
            facts.update(provider(payload, RUNNER_PLAN))
        except Exception as exc:  # noqa: BLE001 - 근거 하나가 죽어도 나머지는 남긴다
            errors.append(f"{provider.__name__}:{type(exc).__name__}")
    # 규칙 원문은 LLM 이 인용하라고 붙였던 것이다. 낭독할 LLM 이 없으니 뺀다.
    facts.pop("broker_rules", None)
    # 주문 제안은 이미 propose_intent() 가 결정론으로 만들었다. 다시 만들지 않고 옮긴다.
    proposal = payload.get("debate")
    facts["order_intent_proposal"] = (
        dict(proposal).get("order_intent_proposal") if isinstance(proposal, Mapping)
        else payload.get("order_intent_proposal"))

    # fail-closed (개발 원칙 9). 막힌 것은 막힌 대로 올린다.
    feasibility = facts.get("plan_feasibility") or {}
    cert = facts.get("certification") or {}
    blockers = []
    if feasibility.get("feasible") is False:
        blockers.append("plan_infeasible")
    if cert.get("blocked"):
        blockers.append("certification_blocked")
    if cert.get("verdict_unknown") and payload.get("derivatives_signal"):
        blockers.append("certification_unknown")
    if errors:
        blockers.append("evidence_error")

    return {
        "worker_id": RUNNER_ID,
        "role": RUNNER_ROLE,
        "tools": list(RUNNER_TOOLS),
        "status": "COMPLETED",
        "attempts": 1,
        "llm": False,               # ← 이 직원은 모델을 안 부른다. 계약으로 박는다
        "output": {
            "worker_id": RUNNER_ID,
            "facts": facts,
            "blockers": blockers,
            "escalate": bool(blockers),
            "decided_by": "deterministic",
            "authoritative": False,   # 판정은 OMS·Risk 가 한다. 이건 옮긴 것일 뿐이다
        },
        "error": ";".join(errors) or None,
        "output_contract": "trading.desk-runner.v1",
    }


def trading_tools(capture: dict[str, dict] | None = None) -> dict[str, Any]:
    """LLM 직원별 read-only tool. 근거·RAG 플랜은 `grounded_tool` 이 얹는다."""
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

    # desk-runner 는 레지스트리 밖이다. 공용 런타임은 그래프마다 LLM 을 부르므로
    # 거기 넣으면 "LLM 없음"이 프롬프트 문장이 되고 실행 경로로는 뚫린다.
    runner = desk_runner(enriched)
    result["workers"].append(runner)
    result["executed"].append(RUNNER_ID)
    result["llm_worker_count"] = len(WORKER_SPECS)
    # 왜 안 켰는지를 결과에 실어 보낸다 - 침묵하는 not_executed 를 없앤다.
    result["trigger_provenance"] = enriched.get("trigger_provenance", {})
    return result


if __name__ == "__main__":
    import json
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    from trading_worker_skills.rag_router import rag_policy_for_worker

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

    # 1. LLM 직원은 Bull/Bear 둘뿐이고 나머지는 desk-runner 하나로 합쳐졌다
    ids = [s.worker_id for s in WORKER_SPECS]
    assert ids == ["bull-thesis-worker", "bear-thesis-worker"], ids
    for gone in ("market-thesis-worker", "trade-proposal-worker", "order-constraint-worker",
                 "execution-planning-worker", "venue-cost-worker",
                 "derivatives-structure-worker"):
        assert gone not in ids, f"합쳐진 직원이 남아 있다: {gone}"
    assert all(s.trigger == "always" for s in WORKER_SPECS), "조건부 LLM 직원이 남았다"
    print("  LLM 직원 2 + 잡무 1        OK")

    # 2. **Bull evidence 에 Bear 원문이 없다** - 확증편향 차단의 배선 검사
    tools = trading_tools()
    bull_ev = tools["bull-thesis-worker"](PAYLOAD)
    bear_ev = tools["bear-thesis-worker"](PAYLOAD)
    assert BEAR_TEXT not in json.dumps(bull_ev, ensure_ascii=False), "Bull 에 Bear 원문이 샜다"
    assert BULL_TEXT not in json.dumps(bear_ev, ensure_ascii=False), "Bear 에 Bull 원문이 샜다"
    assert BULL_TEXT in json.dumps(bull_ev, ensure_ascii=False)
    assert bull_ev["rag_plan"]["route"] == bear_ev["rag_plan"]["route"] == "NO_RAG"
    print("  Bull/Bear 상대 원문 차단   OK")

    # 3. **desk-runner 는 LLM 경로에 못 들어간다** - 정책이 없으므로 fail-closed
    try:
        rag_policy_for_worker(RUNNER_ID)
        raise AssertionError("desk-runner 가 RAG 정책을 얻었다 - LLM 경로가 열린다")
    except ValueError:
        pass
    assert RUNNER_ID not in {s.worker_id for s in WORKER_SPECS}
    assert RUNNER_ID not in PROVIDERS
    print("  desk-runner LLM 경로 차단  OK")

    # 4. desk-runner 가 흡수한 5명의 판정을 실제로 만든다
    enriched = enrich_payload(PAYLOAD)
    runner = desk_runner(enriched)
    facts = runner["output"]["facts"]
    assert runner["llm"] is False and "summary" not in runner["output"]
    assert "state_transitions" in facts          # order-constraint 몫
    assert "plan_feasibility" in facts           # execution-planning 몫
    assert "tca_memory" in facts                 # venue-cost 몫
    assert "certification" in facts              # derivatives-structure 몫
    assert facts["order_intent_proposal"]["available"] is True   # trade-proposal 몫
    assert facts["order_intent_proposal"]["submittable"] is False
    assert runner["output"]["decided_by"] == "deterministic"
    assert facts["plan_feasibility"]["plan_source"] == "philosophies.yaml:momentum"
    print("  잡무 5종 결정론 산출       OK")

    # 5. 막힌 것은 막힌 대로 올린다 (fail-closed)
    blocked = desk_runner(enrich_payload({
        **PAYLOAD, "derivatives": {"asset_class": "FUTURE"},
        "execution_plan": {"slices": 500, "window_minutes": 1, "adapter": "paper"}}))
    assert blocked["output"]["escalate"] is True
    assert "plan_infeasible" in blocked["output"]["blockers"], blocked["output"]["blockers"]
    # capability_profile 이 없으면 "허용"으로 떨어지지 않는다
    assert "certification_unknown" in blocked["output"]["blockers"]
    print("  실현불가·미인증 escalate   OK")

    # 6. 트리거 파생은 그대로 산다 - desk-runner 가 무엇을 보고할지를 가른다
    assert enriched["execution_request"] is True     # 제안 + 시세
    assert enriched["approved_risk"] is False        # risk_decision 이 없다
    assert enriched["derivatives_signal"] is False   # 현물
    prov = enriched["trigger_provenance"]
    assert "sequence 2" in prov["approved_risk"]["reason"], prov["approved_risk"]
    assert enrich_payload({**PAYLOAD, "risk_decision": approval()})["approved_risk"] is True
    print("  트리거 파생 + 사유 기록    OK")

    # 7. 실제 레지스트리 실행 - 스텁 LLM 은 2번만 불린다
    calls: list[str] = []

    def stub(_system: str, prompt: str) -> str:
        worker_id = next((line.split(":", 1)[1].strip() for line in prompt.splitlines()
                          if line.startswith("Worker id:")), "?")
        calls.append(worker_id)
        return json.dumps({"summary": f"ctx for {worker_id}", "confidence": 0.8,
                           "evidence_refs": [], "escalate": False})

    full = {**PAYLOAD, "risk_decision": approval(),
            "derivatives": {"asset_class": "FUTURE"}}
    out = run_employee_workers(full, llm=stub)
    assert out["binding"] is False and out["degraded"] is False, out.get("failed")
    assert set(out["executed"]) == {*ids, RUNNER_ID}, sorted(out["executed"])
    assert out["not_executed"] == []
    assert out["llm_worker_count"] == 2
    assert set(calls) == set(ids), f"LLM 이 잡무 직원까지 불렸다: {calls}"
    assert RUNNER_ID not in calls
    assert out["trigger_provenance"], "provenance 가 결과에 안 실렸다"
    print("  3명 실행 / LLM 은 2회      OK")

    # 8. 색인 밖 인용은 그 직원만 escalate
    def faker(_system: str, prompt: str) -> str:
        worker_id = next((line.split(":", 1)[1].strip() for line in prompt.splitlines()
                          if line.startswith("Worker id:")), "?")
        refs = ["claim:fact:99"] if worker_id == "bear-thesis-worker" else []
        return json.dumps({"summary": "s", "confidence": 0.5,
                           "evidence_refs": refs, "escalate": False})

    dirty = run_employee_workers(full, llm=faker)
    assert dirty["failed"] == ["bear-thesis-worker"], dirty["failed"]
    assert "bear-thesis-worker" not in dirty["executed"]
    assert RUNNER_ID in dirty["executed"], "잡무 직원이 남의 날조에 휩쓸렸다"
    assert dirty["degraded"] is True
    print("  날조 인용 -> 해당 직원만   OK")

    # 9. **모르는 접두사는 escalate 하지 않는다** - 공용 계약 테스트 호환
    def generic(_system: str, _prompt: str) -> str:
        return json.dumps({"summary": "s", "confidence": 0.8,
                           "evidence_refs": ["test:evidence"], "escalate": False})

    clean = run_employee_workers(full, llm=generic)
    assert clean["failed"] == [] and clean["degraded"] is False, clean["failed"]
    print("  모르는 접두사 통과         OK")

    print("ok - 트레이딩 직원 레지스트리 9개 영역 점검 통과 "
          f"(LLM {len(WORKER_SPECS)}명 + 잡무 1명, 잡무는 모델을 부르지 않는다)")
