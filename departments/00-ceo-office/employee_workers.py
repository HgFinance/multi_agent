"""CEO Office employee Worker registry.

직원 2명. **그중 LLM 을 쓰는 것은 1명뿐이다**(2026-08-11).

  executive-briefing-worker  부서 결과 서술 종합 — LLM
  ceo-runner                 부서 판정·미완료 단계 조회 — **LLM 없음.** 옮기기만 한다

CEO 는 Trading·Risk·QA·Accounting 네 부서가 2026-08-06~07 에 거친 "LLM 이 판단하지
않는 일" 분리를 한 번도 거치지 않은 유일한 부서였다. 그 결과가 head 페르소나
(config.yaml) 한 문단에 Mandate 해석 + 6본부 라우팅 + 4개 위원회 소집 +
Chief-of-Staff 8개 업무가 전부 들어 있던 상태다.

**러너는 새 판정을 만들지 않는다.** 이미 다른 부서 결정론 엔진이 확정한 판정을
옮기기만 한다 — Risk verdict 는 `RiskEngine.check_order()` 가, QA decision 은
`EvidenceQaEngine` 이 이미 정했다. `desk_runner()`/`back_office_runner()` 와 같은 일이다.

**`missing_inputs` 가 이 러너의 핵심이다.** CEO 의 workflow 상 임무는 "각 결과를
통합해 사용자 설명과 **미완료 상태**를 보고"(orchestration/workflows/investment-case.yaml:84)
인데, 그 "미완료"는 판단이 아니라 조회다. `back_office_runner()` 의
`missing_blocks`("없는 것을 없다고 적는다")와 같은 원칙을 그대로 쓴다.

미완료는 blocker 가 아니다 — `escalate` 는 Risk/QA 가 실제로 막았을 때만 True 다.
CEO 는 4개 workflow(investment-case / strategy-research / workforce-management /
agent-evolution)에 등장하고 흐름마다 도달한 단계가 다르므로, 안 온 단계를 전부
escalate 로 올리면 그 신호는 곧 의미를 잃는다.

지침: docs/02-engineering/CEO_RUNNER_SPEC.md
근거: docs/02-engineering/WORKER_ROLE_BOUNDARIES.md §"결정론 Worker(러너)를 두는 부서"

자체 점검: python departments/00-ceo-office/employee_workers.py
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import datetime, timezone
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
except ModuleNotFoundError:  # direct department-local execution
    from employee_worker_runtime import (
        WorkerLLM,
        WorkerSpec,
        run_worker_registry,
        tools_for_specs,
    )

# 각 부서가 CEO 로 넘기는 산출물 계약. workflow 의 output_contract 이름 그대로다.
# strategy_report 는 아직 어느 workflow 도 만들지 않는다 - 그래서 늘 missing 으로
# 잡히고, 그것이 이 필드가 있어야 할 자리라는 사실을 계속 드러낸다.
STAGE_INPUTS = (
    "research_packet",
    "order_intent",
    "risk_decision",
    "qa_assessment",
    "accounting_snapshot",
    "strategy_report",
)

WORKER_SPECS = (
    WorkerSpec("executive-briefing-worker", "Executive briefing and cross-department handoff analyst", ("ceo.department_reports.read",), "always", STAGE_INPUTS),
)

# ── ceo-runner: 부서 판정·미완료 단계 조회 (LLM 없음) ──────────────────────
RUNNER_ID = "ceo-runner"
RUNNER_ROLE = "CEO 사무 러너 — 부서 판정·미완료 단계 조회(결정론, LLM 없음)"

# 감사에서 "이 직원이 무엇을 읽었나"가 남아야 한다. CEO Worker 와 같은 도구로
# 시작한다 - 러너는 부서 보고서 봉투 밖으로 나가지 않는다.
RUNNER_TOOLS = ("ceo.department_reports.read",)


def _artifact_sources(payload: Mapping[str, Any]) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """단계 산출물이 실제로 실려 오는 자리 3곳. **짐작이 아니라 호출부 확인이다.**

    - `payload` 최상위: `orchestration/employee_dispatch.py` 를 직접 부르는 경로와
      tests/test_worker_architecture.py 픽스처가 이 모양이다.
    - `payload["artifacts"]`: investment-case 의 CEO 단계
      (`orchestration/adapters/paper_pipeline.py` `PaperPipelineAdapter.ceo`).
    - `payload["workflow_context"]["artifacts"]`: strategy-research /
      workforce-management / agent-evolution 의 CEO 단계(같은 파일 `auxiliary`).

    셋 다 `_store()` 가 계약 이름(`risk_decision` 등)을 키로 쓰므로 이름은 같다.
    앞에 있는 자리가 이긴다 - 직접 넘긴 값이 봉투 안 사본보다 가깝다.
    """
    sources: list[tuple[str, Mapping[str, Any]]] = [("payload", payload)]
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, Mapping):
        sources.append(("artifacts", artifacts))
    workflow_context = payload.get("workflow_context")
    if isinstance(workflow_context, Mapping):
        nested = workflow_context.get("artifacts")
        if isinstance(nested, Mapping):
            sources.append(("workflow_context.artifacts", nested))
    return tuple(sources)


def _resolve_stages(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """(값, 어디서 왔는지, 없는 것). **없는 것을 없다고 적는다.**"""
    resolved: dict[str, Any] = {}
    origin: dict[str, str] = {}
    missing: list[str] = []
    sources = _artifact_sources(payload)
    for field in STAGE_INPUTS:
        for name, source in sources:
            value = source.get(field)
            if value is not None:
                resolved[field] = value
                origin[field] = name
                break
        else:
            # 빈 dict 로 두면 "확인했고 비어 있다"로 읽힌다 - 안 온 것과 구분한다.
            missing.append(field)
    return resolved, origin, missing


def _parse_instant(value: Any) -> datetime | None:
    """ISO 문자열/`datetime` 을 tz-aware 로. 못 읽으면 None 이다(0 으로 보지 않는다)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _risk_facts(decision: Any, at: datetime, blockers: list[str]) -> dict[str, Any]:
    """`RiskEngine` 이 확정한 verdict/만료를 옮긴다. **다시 판정하지 않는다.**

    판정 어휘는 `RiskVerdict`(departments/02-trading/contracts/contracts.py:96)이며
    값은 소문자다 - 대소문자로 비교가 갈리지 않게 정규화만 한다.
    """
    if not isinstance(decision, Mapping):
        blockers.append("risk_decision_unreadable")
        return {"verdict": None, "expiry_checked": False,
                "expiry_reason": "risk_decision 이 매핑이 아니다"}

    facts: dict[str, Any] = {}
    verdict = decision.get("verdict", decision.get("candidate_verdict"))
    facts["verdict"] = verdict
    if verdict is None:
        blockers.append("risk_verdict_missing")
    elif str(verdict).upper() != "APPROVE":
        blockers.append(f"risk_verdict_{str(verdict).lower()}")

    # `RiskDecision.expires_at`(contracts.py:251)은 계약에 있지만 CEO 로 오는 봉투에는
    # 실려 있지 않다 - departments/03-risk/scripts.py 가 만드는 assessment dict 에
    # 그 필드가 없다. 없을 때 "기한 안"이라고 말하지 않고, blocker 로도 올리지 않는다
    # (매 케이스 걸리면 escalate 가 곧 의미를 잃는다). **확인 못 했다고 적는다.**
    if "expires_at" not in decision:
        facts["expiry_checked"] = False
        facts["expiry_reason"] = "risk_decision 에 expires_at 이 실려 있지 않다"
        return facts

    expires_at = _parse_instant(decision["expires_at"])
    facts["expires_at"] = decision["expires_at"]
    if expires_at is None:
        facts["expiry_checked"] = False
        facts["expiry_reason"] = "expires_at 을 해석할 수 없다"
        blockers.append("risk_decision_expiry_unreadable")
        return facts

    facts["expiry_checked"] = True
    facts["expired"] = expires_at <= at
    if facts["expired"]:
        blockers.append("risk_decision_expired")
    return facts


def _qa_facts(assessment: Any, blockers: list[str]) -> dict[str, Any]:
    """`EvidenceQaEngine` 이 확정한 판정을 옮긴다. **다시 판정하지 않는다.**

    필드 이름이 둘이다 - 엔진 레코드는 `decision`
    (departments/06-ai-qa-audit/evidence/evidence_qa_engine.py:262), 파이프라인이
    CEO 로 넘기는 봉투는 `verdict`(paper_pipeline.py `oms_fill_gate` 가 읽는 이름)다.
    하나만 보면 나머지 경로에서 눈이 먼다.
    """
    if not isinstance(assessment, Mapping):
        blockers.append("qa_assessment_unreadable")
        return {"decision": None}

    decision = assessment.get("decision", assessment.get("verdict"))
    if decision is None:
        blockers.append("qa_decision_missing")
    elif str(decision).upper() == "FAIL":
        blockers.append("qa_decision_fail")
    return {"decision": decision}


def ceo_runner(payload: Mapping[str, Any], *, at: datetime | None = None) -> dict[str, Any]:
    """부서 판정 집계와 미완료 단계 조회. **모델을 부르지 않는다.**

    Risk/QA 가 이미 확정한 판정을 blocker 로 옮기고, 안 온 단계를 그대로 적는다 —
    `summary` 필드가 없는 것이 이 직원의 요지다. 문장을 만들 자리가 없으면 그
    자리에서 환각도 생기지 않는다. 서술 종합은 executive-briefing-worker 몫이다.
    """
    at = at or datetime.now(timezone.utc)
    resolved, origin, missing = _resolve_stages(payload)

    blockers: list[str] = []
    facts: dict[str, Any] = {"stage_origin": origin}
    if "risk_decision" in resolved:
        facts["risk"] = _risk_facts(resolved["risk_decision"], at, blockers)
    if "qa_assessment" in resolved:
        facts["qa"] = _qa_facts(resolved["qa_assessment"], blockers)

    return {
        "worker_id": RUNNER_ID,
        "role": RUNNER_ROLE,
        "tools": list(RUNNER_TOOLS),
        "status": "COMPLETED",
        "attempts": 1,
        "llm": False,  # ← 이 직원은 모델을 안 부른다. 계약으로 박는다
        "output": {
            "worker_id": RUNNER_ID,
            "facts": facts,
            "missing_inputs": missing,
            "blockers": blockers,
            # 미완료는 escalate 사유가 아니다 - Risk/QA 가 막았을 때만 올린다.
            "escalate": bool(blockers),
            "decided_by": "deterministic",
            "authoritative": False,  # 판정은 Risk/QA/Accounting 이 한다
        },
        "error": None,
        "output_contract": "ceo.ceo-runner.v1",
    }


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    # HR 유휴 리포트는 6개 투자본부만 조회하므로 ceo 이벤트를 지금 읽는 곳은 없다.
    # 그래도 stage 를 준다 - 나중에 범위를 넓힐 때 과거 데이터가 비어 있지 않게.
    result = run_worker_registry(WORKER_SPECS, payload, tools=tools_for_specs(WORKER_SPECS),
                                 llm=llm, stage="ceo")

    # ceo-runner 는 레지스트리 밖이다. 공용 런타임은 그래프마다 LLM 을 부르므로
    # 거기 넣으면 "LLM 없음"이 프롬프트 문장이 되고 실행 경로로는 뚫린다.
    # 네 부서 러너(desk/risk/qa/back-office) 전부 이 방식이다.
    result["workers"].append(ceo_runner(payload))
    result["executed"].append(RUNNER_ID)
    result["llm_worker_count"] = len(WORKER_SPECS)
    return result


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    import json
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    payload = {
        "research_packet": {"status": "COMPLETED"},
        "order_intent": {"side": "BUY", "quantity": 1},
        "risk_decision": {"verdict": "approve", "expires_at": (now + timedelta(minutes=5)).isoformat()},
        "qa_assessment": {"decision": "PASS"},
        "accounting_snapshot": {"status": "PAPER_NOT_POSTED"},
    }

    # 1. LLM 직원 1 + 러너 1. 러너는 레지스트리 밖이다
    ids = [s.worker_id for s in WORKER_SPECS]
    assert ids == ["executive-briefing-worker"], ids
    assert RUNNER_ID not in ids, "러너가 레지스트리 안에 들어갔다 - LLM 경로로 뚫린다"
    assert all(s.trigger == "always" for s in WORKER_SPECS)
    print("  LLM 직원 1 + 러너 1        OK")

    # 2. 승인·통과 케이스는 blocker 가 없다. **서술 필드도 없다**
    ok = ceo_runner(payload, at=now)
    assert ok["llm"] is False and "summary" not in ok["output"]
    assert ok["output"]["blockers"] == [], ok["output"]["blockers"]
    assert ok["output"]["escalate"] is False
    assert ok["output"]["decided_by"] == "deterministic"
    assert ok["output"]["authoritative"] is False
    assert ok["output"]["facts"]["risk"]["expired"] is False
    assert ok["output"]["facts"]["qa"]["decision"] == "PASS"
    print("  승인 케이스 무통과         OK")

    # 3. 안 온 단계는 없다고 적되 escalate 하지 않는다
    assert ok["output"]["missing_inputs"] == ["strategy_report"], ok["output"]["missing_inputs"]
    bare = ceo_runner({}, at=now)
    assert bare["output"]["missing_inputs"] == list(STAGE_INPUTS)
    assert bare["output"]["blockers"] == [] and bare["output"]["escalate"] is False
    assert "risk" not in bare["output"]["facts"] and "qa" not in bare["output"]["facts"]
    print("  미완료 표기 / escalate 안 함 OK")

    # 4. Risk/QA 판정을 **옮긴다**. 새로 만들지 않는다
    rejected = ceo_runner({**payload, "risk_decision": {"verdict": "reject"}}, at=now)
    assert rejected["output"]["blockers"] == ["risk_verdict_reject"]
    assert rejected["output"]["escalate"] is True
    failed = ceo_runner({**payload, "qa_assessment": {"decision": "FAIL"}}, at=now)
    assert failed["output"]["blockers"] == ["qa_decision_fail"]
    # 파이프라인 봉투는 `verdict` 로 온다 - 이름 하나만 보면 눈이 먼다
    piped = ceo_runner({**payload, "qa_assessment": {"verdict": "FAIL"}}, at=now)
    assert piped["output"]["blockers"] == ["qa_decision_fail"]
    warned = ceo_runner({**payload, "qa_assessment": {"verdict": "WARN"}}, at=now)
    assert warned["output"]["blockers"] == [], "WARN 은 FAIL 이 아니다"
    print("  Risk/QA 판정 이관          OK")

    # 5. 만료 - 지났으면 지났다고, 못 재면 못 쟀다고 적는다
    stale = ceo_runner(
        {**payload, "risk_decision": {"verdict": "approve",
                                      "expires_at": (now - timedelta(seconds=1)).isoformat()}},
        at=now)
    assert stale["output"]["blockers"] == ["risk_decision_expired"]
    unreadable = ceo_runner(
        {**payload, "risk_decision": {"verdict": "approve", "expires_at": "n/a"}}, at=now)
    assert unreadable["output"]["blockers"] == ["risk_decision_expiry_unreadable"]
    assert unreadable["output"]["facts"]["risk"]["expiry_checked"] is False
    # 필드 자체가 없으면 "기한 안"이라고 말하지 않는다. blocker 로도 올리지 않는다
    no_expiry = ceo_runner({**payload, "risk_decision": {"verdict": "approve"}}, at=now)
    assert no_expiry["output"]["facts"]["risk"]["expiry_checked"] is False
    assert no_expiry["output"]["blockers"] == [], no_expiry["output"]["blockers"]
    print("  만료 판정 / 미확인 구분    OK")

    # 6. 봉투 안 사본에서도 찾는다 - CEO 는 4개 workflow 에서 모양이 다르게 온다
    nested = ceo_runner({"case_request": {"case_id": "c1"}, "artifacts": payload}, at=now)
    assert nested["output"]["missing_inputs"] == ["strategy_report"]
    assert nested["output"]["facts"]["stage_origin"]["risk_decision"] == "artifacts"
    auxiliary = ceo_runner(
        {"input_contract": "strategy_qa_assessment", "workflow_context": {"artifacts": payload}},
        at=now)
    assert auxiliary["output"]["facts"]["stage_origin"]["qa_assessment"] == "workflow_context.artifacts"
    # 직접 넘긴 값이 봉투 안 사본을 이긴다
    both = ceo_runner({**payload, "artifacts": {"qa_assessment": {"decision": "FAIL"}}}, at=now)
    assert both["output"]["facts"]["stage_origin"]["qa_assessment"] == "payload"
    assert both["output"]["blockers"] == []
    print("  4개 workflow 봉투 해석     OK")

    # 7. 레지스트리 실행 - 스텁 LLM 은 러너까지 불리지 않는다
    calls: list[str] = []

    def stub(_system: str, prompt: str) -> str:
        worker_id = next((line.split(":", 1)[1].strip() for line in prompt.splitlines()
                          if line.startswith("Worker id:")), "?")
        calls.append(worker_id)
        return json.dumps({"summary": f"ctx for {worker_id}", "confidence": 0.8,
                           "evidence_refs": ["test:evidence"], "escalate": False})

    out = run_employee_workers(payload, llm=stub)
    assert out["degraded"] is False, out.get("failed")
    assert set(out["executed"]) == {"executive-briefing-worker", RUNNER_ID}, out["executed"]
    assert out["llm_worker_count"] == 1
    assert calls == ["executive-briefing-worker"], f"LLM 이 러너까지 불렸다: {calls}"
    assert out["binding"] is False
    print("  2명 실행 / LLM 은 1회      OK")

    # 8. 서술 직원이 죽어도 러너 보고는 살아 있다
    def failing(_system: str, _prompt: str) -> str:
        raise TimeoutError("synthetic worker timeout")

    dirty = run_employee_workers(payload, llm=failing)
    assert dirty["failed"] == ["executive-briefing-worker"], dirty["failed"]
    assert dirty["executed"] == [RUNNER_ID], dirty["executed"]
    runner_report = next(w for w in dirty["workers"] if w["worker_id"] == RUNNER_ID)
    assert runner_report["status"] == "COMPLETED" and runner_report["llm"] is False
    print("  LLM 실패 -> 러너는 생존    OK")

    print(f"ok - CEO 직원 레지스트리 8개 영역 점검 통과 "
          f"(LLM {len(WORKER_SPECS)}명 + 러너 1명, 러너는 모델을 부르지 않는다)")
