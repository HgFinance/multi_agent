#!/usr/bin/env python3
"""QA본부 LangGraph 파이프라인 - run_qa_department(artifact, evidence_store, decision_time) -> QA Assessment.

담당: 동규 (리스크/QA)
근거: 이 부서의 원안(LangGraph 실무진 + Hermes 부서장 정제)을 재일님이 리서치본부에,
      동규님이 리스크본부에 적용하며 실구현 직원 패턴으로 개선했다
      (departments/01-research/scripts.py, departments/03-risk/scripts.py, 2026-07-31).
      원안 자신은 그 개선을 반영하지 않은 채 남아있어 같은 패턴으로 재작성한다(2026-08-01).

원안과의 차이 - **워커가 자유 판단 프롬프트가 아니라 이미 구현된 결정론 엔진 + 그 결과를
grounded 서술만 하는 LLM이다** (research/risk 패턴을 QA 자신에게 역적용):
  check_evidence          evidence-qa-agent 결정론 절반 (evidence_qa_engine.EvidenceQaEngine.check_artifact -
                           8단계 검사, PASS/WARN/FAIL/CONDITIONAL, input_hash 재현성)
  draft_claim_narrative   evidence-qa-agent LLM 절반 (내부 Ollama 워커 - 엔진 결과를 grounded
                           서술만 한다, 새 판정 금지)
  supervise               qa-audit-supervisor 페르소나 (hermes/config.yaml 원문, Hermes AIAgent 호출 -
                           종합·Escalation만, 판정 자체는 못 바꾼다)

hallucination-critic/model-risk-agent/internal-audit-agent는 뺐다 - config.yaml의 not_started
항목대로 대응 결정론 서비스가 아직 없다 (research가 4개 페르소나를 같은 이유로 뺀 것과 동일).

원칙 (전 노드 공통, CLAUDE.md):
  - 바인딩 decision은 EvidenceQaEngine.check_artifact 결과에서만 온다. 워커 LLM도 qa-audit-supervisor도
    그 결과를 절대 못 바꾸고 서술(narrative)만 만든다 - config.yaml의 evidence-qa-agent 페르소나 자체가
    "you interpret ... you do not judge citations from memory"라고 명시한다.
  - 워커 LLM 주소는 팀 공용 GPU에 올린 Ollama 서버다. 환경변수로 바꾸지 않는다 - 동규님이 의도적으로
    고정한 팀 공유 인프라 주소다.
  - 부서장(Hermes AIAgent)은 config.yaml의 model.default를 그대로 쓴다 - 부서별 env var로 바꾸지 않는다
    (CLAUDE.md "model은 8개 파일 모두 동일, 바꾸려면 8개를 함께 바꾼다").

실행:
  python scripts.py               # 자체 점검 (Ollama·Hermes 없음)
  python scripts.py --run         # 데모 Artifact로 실제 실행 (워커 Ollama, Hermes 필요)
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict
from uuid import UUID

from langgraph.graph import END, StateGraph
from openai import OpenAI
from run_agent import AIAgent

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE / "evidence"))

PIPELINE_VERSION = "qa-department-pipeline-v1"

# 워커(직원) LLM 백엔드 - 동규님 GPU에 올린 팀 공용 Ollama 서버. 환경변수로 옮기지 않는다.
internal_llm = OpenAI(base_url="http://172.31.99.238:11434/v1", api_key="ollama")


def _call_internal_llm(prompt: str) -> str:
    res = internal_llm.chat.completions.create(
        model="agent-qa",
        messages=[{"role": "user", "content": prompt}],
    )
    return res.choices[0].message.content


def _persona(name: str) -> str:
    cfg = (_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8")
    m = re.search(rf'{re.escape(name)}: "(.*?)"\n', cfg, re.S)
    if not m:
        raise ValueError(f"{name} 페르소나를 config.yaml에서 찾을 수 없다")
    return m.group(1)


class QAState(TypedDict, total=False):
    artifact: dict          # Artifact JSON (Claim 포함) - 검토 대상
    evidence_store: dict    # {evidence_id: EvidenceChunk 필드} - QA는 근거를 직접 수집하지 않는다
    decision_time: str      # PIT 기준 시각 (ISO8601)
    assessment: dict        # evidence-qa-agent 결정론 결과 (QaAssessment)
    claim_narrative: str    # evidence-qa-agent LLM 결과 (grounded 서술)
    verdict: str            # 최종 값 - 항상 assessment 에서만 옴
    narrative: str
    escalate: bool


# ── 노드 1: Evidence 검사 (결정론 직원 - EvidenceQaEngine) ─────────────────
def check_evidence(state: QAState) -> dict:
    from evidence_qa_engine import Artifact, EvidenceChunk, EvidenceQaEngine, EvidenceStore, QaContext

    chunks = {UUID(eid): EvidenceChunk(evidence_id=UUID(eid), **fields)
              for eid, fields in state["evidence_store"].items()}
    ctx = QaContext(evidence_store=EvidenceStore(chunks=chunks),
                     decision_time=datetime.fromisoformat(state["decision_time"]))
    result = EvidenceQaEngine().check_artifact(Artifact(**state["artifact"]), ctx)
    return {"assessment": {
        "qa_decision_id": str(result.qa_decision_id), "decision": result.decision.value,
        "reason_codes": [r.value for r in result.reason_codes],
        "claim_checks": [{"claim_index": c.claim_index, "claim": c.claim,
                          "result": c.result.value, "reason": c.reason}
                         for c in result.claim_checks],
        "findings": [{"finding_type": f.finding_type, "severity": f.severity.value,
                     "description": f.description} for f in result.findings],
    }}


# ── 노드 2: Claim 서술 (LLM 직원 - 내부 Ollama, grounded 서술만) ───────────
def draft_claim_narrative(state: QAState) -> dict:
    a = state["assessment"]
    prompt = f"""{_persona('evidence-qa-agent')}

아래는 결정론적 Evidence QA Engine이 이미 계산한 Claim별 검사 결과다(전체 판정: {a['decision']}).
새 판정을 내리거나 근거를 임의로 재해석하지 말고, 각 결과를 그대로 풀어 쓴 한국어 요약만 작성하라:
{json.dumps(a['claim_checks'], ensure_ascii=False, indent=1)}"""
    return {"claim_narrative": _call_internal_llm(prompt)}


# ── 노드 3: 종합 (qa-audit-supervisor 페르소나 - Hermes AIAgent) ───────────
def _hermes_chat(persona: str, task: str) -> str:
    agent = AIAgent(model="poolside/laguna-s-2.1:free", quiet_mode=True,
                     ephemeral_system_prompt=persona)
    return agent.chat(task)


def supervise(state: QAState, *, chat=None) -> dict:
    a = state["assessment"]
    bundle = {"decision": a["decision"], "reason_codes": a["reason_codes"],
              "claim_checks": a["claim_checks"], "findings": a["findings"],
              "claim_narrative": state["claim_narrative"]}
    task = f"""Using ONLY the evidence below, write a case-level QA audit narrative in Korean for
CEO/department review. The binding decision is "{a['decision']}" from the deterministic Evidence QA
Engine - you cannot change it, only interpret and escalate it.
Schema (JSON only):
{{"narrative": "2-4 sentences, cite claim_checks/findings",
 "escalate": true or false, "cited_checks": ["claim indices or finding types referenced"]}}

Evidence:
{json.dumps(bundle, ensure_ascii=False, indent=1)}"""

    call = chat or _hermes_chat
    out = call(_persona("qa-audit-supervisor"), task)
    s, e = out.find("{"), out.rfind("}")
    note = json.loads(out[s:e + 1])
    for k in ("narrative", "escalate", "cited_checks"):
        if k not in note:
            raise ValueError(f"Supervisor 종합 결과에 {k} 가 없다 - 초안 거부")
    # 바인딩 decision 은 LLM 출력이 아니라 assessment 에서 그대로 가져온다
    return {"verdict": a["decision"], "narrative": note["narrative"], "escalate": note["escalate"]}


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def build_pipeline():
    g = StateGraph(QAState)
    g.add_node("check_evidence", check_evidence)
    g.add_node("draft_claim_narrative", draft_claim_narrative)
    g.add_node("supervise", supervise)
    g.set_entry_point("check_evidence")
    g.add_edge("check_evidence", "draft_claim_narrative")
    g.add_edge("draft_claim_narrative", "supervise")
    g.add_edge("supervise", END)
    return g.compile()


def run_qa_department(artifact: dict, evidence_store: dict, decision_time: str) -> dict:
    """본부 단독 실행 - Risk/Research 의 run_<dept>_department 와 같은 외부 인터페이스."""
    out = build_pipeline().invoke({
        "artifact": artifact, "evidence_store": evidence_store, "decision_time": decision_time,
    })
    a = out["assessment"]
    return {"qa_decision_id": a["qa_decision_id"], "verdict": out["verdict"],
            "reason_codes": a["reason_codes"], "findings": a["findings"],
            "claim_narrative": out["claim_narrative"], "narrative": out["narrative"],
            "escalate": out["escalate"]}


# ── 자체 점검 (Ollama·Hermes 없음) ──────────────────────────────────────────
def _check_graph_shape():
    p = build_pipeline()
    assert p is not None
    print("  그래프 컴파일              OK")


def _check_fail_still_narrates():
    # FAIL 판정이어도 워커·부서장 둘 다 계속 불려 서술이 남는지, decision 값 자체는
    # 그대로 유지되는지 - Ollama·Hermes 콜은 스텁으로 대체한다
    global check_evidence, _call_internal_llm, _hermes_chat
    orig_ce, orig_llm, orig_chat = check_evidence, _call_internal_llm, _hermes_chat
    check_evidence = lambda s: {"assessment": {
        "qa_decision_id": "d1", "decision": "FAIL",
        "reason_codes": ["fact_without_evidence"],
        "claim_checks": [{"claim_index": 0, "claim": "x", "result": "UNSUPPORTED", "reason": "근거 없음"}],
        "findings": [{"finding_type": "unsupported_claim", "severity": "HIGH", "description": "d"}],
    }}
    _call_internal_llm = lambda prompt: "요약: 근거 없는 주장 1건"
    _hermes_chat = lambda persona, task: '{"narrative": "차단됨", "escalate": true, "cited_checks": ["0"]}'
    try:
        out = build_pipeline().invoke({"artifact": {}, "evidence_store": {}, "decision_time": "x"})
        assert out["assessment"]["decision"] == "FAIL"
        assert out["verdict"] == "FAIL"
        assert out["escalate"] is True
    finally:
        check_evidence, _call_internal_llm, _hermes_chat = orig_ce, orig_llm, orig_chat
    print("  FAIL 판정에도 서술 계속됨   OK")


def _check_supervisor_guard():
    a = {"decision": "PASS", "reason_codes": [], "claim_checks": [], "findings": []}
    bad_chat = lambda persona, task: '{"narrative": "n"}'  # escalate/cited_checks 누락
    try:
        supervise({"assessment": a, "claim_narrative": "n"}, chat=bad_chat)
        raise AssertionError("불완전 종합 결과가 통과했다")
    except ValueError:
        pass
    print("  Supervisor 스키마 가드      OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        from uuid import uuid4

        now_iso = datetime.now(timezone.utc).isoformat()
        eid = str(uuid4())
        demo_artifact = {
            "artifact_version_id": str(uuid4()), "artifact_type": "research_packet",
            "producer": "research-supervisor", "fund_id": str(uuid4()), "trace_id": str(uuid4()),
            "claims": [{"claim_index": 0, "text": "AAPL 종가는 70000원", "kind": "fact",
                       "subject": "AAPL", "numeric_value": "70000", "unit": "KRW",
                       "evidence_ids": [eid]}],
        }
        demo_evidence_store = {eid: {"source": "market-api", "published_at": now_iso,
                                     "observed_at": now_iso, "excerpt": "종가 70000원",
                                     "numeric_value": "70000", "unit": "KRW"}}
        print(f"{PIPELINE_VERSION} 실행 (데모 Artifact)")
        print(json.dumps(run_qa_department(demo_artifact, demo_evidence_store, now_iso),
                         ensure_ascii=False, indent=1))
        raise SystemExit(0)

    print(f"{PIPELINE_VERSION} 자체 점검 (Ollama·Hermes 없음)")
    _check_graph_shape()
    _check_fail_still_narrates()
    _check_supervisor_guard()
    print("본부 파이프라인 3개 영역 통과. 실행은 --run (내부 Ollama·Hermes 필요)")
