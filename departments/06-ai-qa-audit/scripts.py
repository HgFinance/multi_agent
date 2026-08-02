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
  hallucination_review    hallucination-critic (skills/agentic-rag 기존 baseline 재사용, evidence-qa-agent
                           코퍼스 그대로 씀) - UNSUPPORTED/CONTRADICTED로 이미 플래그된 claim만 대상,
                           그 결과를 뒤집지 않고 유형 분류·인용 근거만 덧붙인다(2026-08-02 추가)
  notion_report           Reporter Node (결정론 - notion_reporter.upload_case, LLM 아님) - 최종 감사
                           결과를 Notion QA DB(NOTION_QA_DB)에 Projection으로 올린다. 실패해도
                           QAState의 바인딩 판정은 못 바꾼다(2026-08-02 추가, 03-risk와 동일 패턴)

model-risk-agent/internal-audit-agent는 뺐다 - config.yaml의 not_started 항목대로 대응 결정론
서비스가 아직 없다 (research가 나머지 페르소나를 같은 이유로 뺀 것과 동일).

원칙 (전 노드 공통, CLAUDE.md):
  - 바인딩 decision은 EvidenceQaEngine.check_artifact 결과에서만 온다. 워커 LLM도 qa-audit-supervisor도
    그 결과를 절대 못 바꾸고 서술(narrative)만 만든다 - config.yaml의 evidence-qa-agent 페르소나 자체가
    "you interpret ... you do not judge citations from memory"라고 명시한다.
  - 워커 LLM 주소는 팀 공용 GPU에 올린 Ollama 서버다. 환경변수로 바꾸지 않는다 - 동규님이 의도적으로
    고정한 팀 공유 인프라 주소다.
  - 부서장(Hermes AIAgent)은 config.yaml의 model.default를 그대로 쓴다 - 부서별 env var로 바꾸지 않는다
    (CLAUDE.md "model은 8개 파일 모두 동일, 바꾸려면 8개를 함께 바꾼다").
  - run_agent(Hermes Runtime) import는 모듈 최상단이 아니라 _hermes_chat 함수 안에서 한다(Lazy Import) -
    이 패키지는 프로젝트 .venv가 아니라 별도 Hermes 설치 venv(예: ~/claude)에만 있으므로, 최상단에서
    부르면 --run 없이 자체 점검만 하려는 경우에도 ModuleNotFoundError로 모듈 전체가 죽는다.

실행:
  source ~/claude/bin/activate    # run_agent(Hermes Runtime)가 이 venv에만 설치돼 있다 - 프로젝트 .venv엔 없음
  python scripts.py               # 자체 점검 (Ollama·Hermes 없음, run_agent 안 불러도 됨 - Lazy Import)
  python scripts.py --run         # 데모 Artifact로 실제 실행 (워커 Ollama, Hermes 필요) -
                                   # reports/qa_audit_report_<qa_decision_id>.md 도 같이 저장한다.
                                   # _render_report_md는 run_qa_department() 반환값을 그대로 옮기기만
                                   # 하는 순수 함수다 - LLM이 리포트 형식·내용을 자유 창작하지 않는다.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict
from uuid import UUID, uuid4

from langgraph.graph import END, StateGraph
from langsmith.wrappers import wrap_openai
from openai import OpenAI

_BASE = Path(__file__).resolve().parent
_REPO_ROOT = _BASE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_AGENTIC_RAG_DIR = _BASE.parent.parent / "skills" / "agentic-rag"
for _p in (_BASE / "evidence", _AGENTIC_RAG_DIR):
    sys.path.insert(0, str(_p))

from reporting import (
    evaluation_metrics,
    json_cell,
    langsmith_handoff,
    md_cell,
)

PIPELINE_VERSION = "qa-department-pipeline-v1"
from apps.observability.risk_qa import get_runtime_telemetry, observe_pipeline

QA_TELEMETRY = get_runtime_telemetry("qa")
from src.resilience import register_circuit_breaker_observer

register_circuit_breaker_observer(QA_TELEMETRY.record_circuit_breaker)


_JOURNAL_MODULE = None


def _journal_module():
    """Load this department's journal without colliding with Risk's package."""

    global _JOURNAL_MODULE
    if _JOURNAL_MODULE is None:
        journal_path = _BASE / "harness" / "journal.py"
        spec = importlib.util.spec_from_file_location("qa_execution_journal", journal_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"QA journal을 로드할 수 없습니다: {journal_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _JOURNAL_MODULE = module
    return _JOURNAL_MODULE

# 워커(직원) LLM 백엔드 - 동규님 GPU에 올린 팀 공용 Ollama 서버. 환경변수로 옮기지 않는다.
# 192.168.25.25
# 172.31.99.238
# wrap_openai는 LANGSMITH_TRACING이 꺼져 있으면 그대로 통과한다 - 켜졌을 때만 토큰량 추적
internal_llm = wrap_openai(OpenAI(base_url="http://192.168.25.25:11434/v1", api_key="ollama"))


def _call_internal_llm(prompt: str) -> str:
    res = internal_llm.chat.completions.create(
        model="agent-qa",
        messages=[{"role": "user", "content": prompt}],
    )
    return res.choices[0].message.content


def _persona(name: str) -> str:
    cfg = (_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8")
    m = re.search(rf'{re.escape(name)}: "(.*?)"\n', cfg, re.DOTALL)
    if not m:
        raise ValueError(f"{name} 페르소나를 config.yaml에서 찾을 수 없다")
    return m.group(1)


class QAState(TypedDict, total=False):
    artifact: dict          # Artifact JSON (Claim 포함) - 검토 대상
    evidence_store: dict    # {evidence_id: EvidenceChunk 필드} - QA는 근거를 직접 수집하지 않는다
    decision_time: str      # PIT 기준 시각 (ISO8601)
    assessment: dict        # evidence-qa-agent 결정론 결과 (QaAssessment)
    claim_narrative: str    # evidence-qa-agent LLM 결과 (grounded 서술)
    hallucination_reviews: list  # hallucination-critic 결과 (UNSUPPORTED/CONTRADICTED claim만, 비었으면 전부 SUPPORTED/PARTIAL)
    verdict: str            # 최종 값 - 항상 assessment 에서만 옴
    narrative: str
    escalate: bool
    notion_upload: dict     # Reporter Node 결과 ({"ok": bool, "url"|"reason"|"error": ...})
    fallbacks: list[dict]
    observability: dict
    report_markdown: str
    evaluation: dict


def _fallback(stage: str, exc: Exception) -> dict:
    return {"stage": stage, "error": type(exc).__name__, "action": "ESCALATE"}


def _fallback_narrative(stage: str, exc: Exception) -> str:
    return (f"{stage} Agent를 사용할 수 없어 결정론적 QA 판정만 유지했습니다 "
            f"({type(exc).__name__}). 원본 부서와 독립 검토자에게 수동 확인을 요청합니다.")


def _fallback_assessment(state: QAState, exc: Exception) -> dict:
    payload = json.dumps(state.get("artifact", {}), ensure_ascii=False, sort_keys=True, default=str)
    input_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {"assessment": {
        "qa_decision_id": str(uuid4()), "decision": "FAIL", "reason_codes": ["pipeline_fallback"],
        "calculation_version": "qa-pipeline-fallback-v1", "input_hash": input_hash,
        "claim_checks": [], "findings": [{"finding_id": str(uuid4()), "finding_type": "pipeline_failure",
                                             "severity": "CRITICAL", "description": _fallback_narrative("Evidence QA", exc)}],
    }, "fallbacks": [_fallback("evidence_check", exc)]}


# ── 노드 1: Evidence 검사 (결정론 직원 - EvidenceQaEngine) ─────────────────
def check_evidence(state: QAState) -> dict:
    from evidence_qa_engine import (
        Artifact,
        EvidenceChunk,
        EvidenceQaEngine,
        EvidenceStore,
        QaContext,
    )

    chunks = {UUID(eid): EvidenceChunk(evidence_id=UUID(eid), **fields)
              for eid, fields in state["evidence_store"].items()}
    ctx = QaContext(evidence_store=EvidenceStore(chunks=chunks),
                     decision_time=datetime.fromisoformat(state["decision_time"]))
    result = EvidenceQaEngine().check_artifact(Artifact(**state["artifact"]), ctx)
    return {"assessment": {
        "qa_decision_id": str(result.qa_decision_id), "decision": result.decision.value,
        "reason_codes": [r.value for r in result.reason_codes],
        "calculation_version": result.calculation_version, "input_hash": result.input_hash,
        "claim_checks": [{"claim_index": c.claim_index, "claim": c.claim,
                          "result": c.result.value, "reason": c.reason}
                         for c in result.claim_checks],
        "findings": [{"finding_id": str(f.finding_id), "finding_type": f.finding_type,
                     "severity": f.severity.value, "description": f.description}
                    for f in result.findings],
    }}


# ── 노드 1.5: Hallucination Critic (skills/agentic-rag, evidence-qa-agent 코퍼스 재사용) ──
def hallucination_review(state: QAState) -> dict:
    from src.graph import run_compliance_check

    a = state["assessment"]
    flagged = [c for c in a["claim_checks"] if c["result"] in ("UNSUPPORTED", "CONTRADICTED")]
    if not flagged:
        # config.yaml 페르소나 프롬프트 원칙 - 이미 나온 근거를 우선하고, 불필요한 외부 호출은 피한다
        return {"hallucination_reviews": []}

    corpus_dir = _AGENTIC_RAG_DIR / "corpus" / "evidence"
    as_of_date = state["decision_time"][:10]
    return {"hallucination_reviews": [
        {"claim_index": c["claim_index"],
         **run_compliance_check(c["claim"], as_of_date, corpus_dir=corpus_dir, persona="hallucination-critic")}
        for c in flagged
    ]}


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
    from run_agent import (
        AIAgent,  # Lazy Import - Hermes 없는 환경에서도 모듈 자체는 항상 import 가능해야 한다
    )

    agent = AIAgent(model="poolside/laguna-s-2.1:free", quiet_mode=True,
                     ephemeral_system_prompt=persona)
    return agent.chat(task)


def supervise(state: QAState, *, chat=None) -> dict:
    a = state["assessment"]
    bundle = {"decision": a["decision"], "reason_codes": a["reason_codes"],
              "claim_checks": a["claim_checks"], "findings": a["findings"],
              "claim_narrative": state["claim_narrative"],
              "hallucination_reviews": state.get("hallucination_reviews", [])}
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


def _assemble_out(state: QAState) -> dict:
    """QAState -> run_qa_department()/notion_report 공용 결과 dict. 필드 목록을 한 곳에서만
    유지한다(03-risk/scripts.py와 동일 패턴) - 그래프 노드와 외부 인터페이스가 따로 베끼면
    한쪽만 필드를 늘렸을 때 드리프트가 생긴다."""
    a = state["assessment"]
    return {"qa_decision_id": a["qa_decision_id"], "verdict": state["verdict"],
            "reason_codes": a["reason_codes"], "claim_checks": a["claim_checks"], "findings": a["findings"],
            "calculation_version": a["calculation_version"], "input_hash": a["input_hash"],
            "claim_narrative": state["claim_narrative"],
            "hallucination_reviews": state.get("hallucination_reviews", []),
        "narrative": state["narrative"], "escalate": state["escalate"],
        "execution_evidence": state.get("execution_evidence")}


def _record_execution_evidence(
    payload: dict,
    result: dict,
    *,
    run_id: str,
    trace_id: str,
    as_of: str | None,
    asset: str | None,
    log_path: str | Path | None,
) -> dict:
    """Record the Hermes/LangGraph boundary without changing the QA verdict."""

    try:
        journal_module = _journal_module()
        sink = journal_module.jsonl_sink(log_path) if log_path else None
        journal = journal_module.RunJournal(
            department="qa-department",
            hermes_profile="qa-department",
            sink=sink,
        )
        input_event = journal.input_snapshot(
            run_id=run_id,
            trace_id=trace_id,
            employee_profile="qa-audit-supervisor",
            payload=payload,
            schema_id="qa.department.input.v1",
            as_of=as_of,
            asset=asset,
            model_version=PIPELINE_VERSION,
            prompt_version="qa-hermes-profile-v1",
            parameter_version="evidence-qa-engine-v1",
        )
        execution = result.get("agent_execution") or {}
        executed = list(execution.get("executed") or [])
        not_executed = list(execution.get("not_executed") or [])
        fallbacks = list(result.get("fallbacks") or [])
        degraded = bool(fallbacks or result.get("escalate"))
        schema_valid = bool(result.get("qa_decision_id")) and not bool(fallbacks)
        domain_valid = result.get("verdict") in {"PASS", "WARN", "FAIL"} and not degraded
        agents_for_log = executed or ["qa-pipeline-fallback"]
        for employee in agents_for_log:
            journal.agent_output(
                run_id=run_id,
                trace_id=trace_id,
                employee_profile=employee,
                output={
                    "employee_profile": employee,
                    "decision": result.get("verdict"),
                    "evidence_refs": tuple(
                        str(check.get("claim_index"))
                        for check in (result.get("claim_checks") or [])
                        if check.get("claim_index") is not None
                    ),
                },
                inputs_hash=input_event.inputs_hash,
                schema_id="qa.agent-output.v1",
                schema_valid=schema_valid,
                domain_valid=domain_valid,
                failed_rule=(result.get("reason_codes") or [None])[0],
                fallback_reason=(fallbacks[0].get("stage") if fallbacks else None),
                retry_count=max((item.get("attempt", 0) for item in fallbacks), default=0),
                as_of=as_of,
                asset=asset,
                model_version=PIPELINE_VERSION,
                prompt_version="qa-hermes-profile-v1",
                parameter_version="evidence-qa-engine-v1",
            )
        failed_rule = (result.get("reason_codes") or [None])[0]
        if fallbacks and not failed_rule:
            failed_rule = fallbacks[0].get("stage") or "pipeline_fallback"
        journal.validation(
            run_id=run_id,
            trace_id=trace_id,
            employee_profile="qa-audit-supervisor",
            inputs_hash=input_event.inputs_hash,
            schema_id="qa.department.output.v1",
            schema_valid=schema_valid,
            domain_valid=domain_valid,
            failed_rule=failed_rule,
            fallback_reason=(fallbacks[0].get("reason") if fallbacks else None),
            retry_count=max((item.get("attempt", 0) for item in fallbacks), default=0),
            raw={"executed": executed, "not_executed": not_executed},
            as_of=as_of,
            asset=asset,
        )
        safe_action = "ESCALATE" if degraded else result.get("verdict", "FAIL")
        journal.decision(
            run_id=run_id,
            trace_id=trace_id,
            employee_profile="qa-audit-supervisor",
            inputs_hash=input_event.inputs_hash,
            output={
                "decision": result.get("verdict"),
                "safe_action": safe_action,
                "binding": False,
            },
            schema_id="qa.department.decision.v1",
            schema_valid=schema_valid,
            domain_valid=domain_valid,
            failed_rule=failed_rule,
            fallback_reason=(fallbacks[0].get("reason") if fallbacks else None),
            retry_count=max((item.get("attempt", 0) for item in fallbacks), default=0),
            as_of=as_of,
            asset=asset,
        )
        review = journal.review(run_id)
        return {
            "run_id": run_id,
            "trace_id": trace_id,
            "input_hash": input_event.inputs_hash,
            "pipeline_status": "DEGRADED" if degraded else "COMPLETED",
            "candidate_verdict": result.get("verdict"),
            "safe_action": safe_action,
            "binding": False,
            "hermes_profile": "qa-department",
            "employees_executed": executed,
            "employees_not_executed": not_executed,
            "retry_policy": {"max_retries": 2, "max_attempts": 3},
            "external_dependencies": {
                "policy_corpus": "PLACEHOLDER_OR_UNVERIFIED",
                "canonical_db": "CONFIGURED" if os.environ.get("DATABASE_URL") else "NOT_CONFIGURED",
                "agent_runs_tool_calls": "CONFIGURED" if os.environ.get("DATABASE_URL") else "NOT_CONFIGURED",
            },
            "event_types": [event.event_type.value for event in journal.events_for_run(run_id)],
            "replay_ready": review["replay_ready"],
            "journal_event_count": len(journal.events_for_run(run_id)),
            "journal_path": str(log_path) if log_path else None,
            "review": review,
        }
    except Exception as exc:  # logging must never turn a safe decision into approval
        return {
            "run_id": run_id,
            "trace_id": trace_id,
            "pipeline_status": "DEGRADED",
            "candidate_verdict": result.get("verdict", "FAIL"),
            "safe_action": "ESCALATE",
            "binding": False,
            "journal_error": type(exc).__name__,
            "journal_path": str(log_path) if log_path else None,
        }


# ── 노드 4: Notion 업로드 (Reporter Node - 결정론, LLM 아님) ────────────────
def notion_report(state: QAState, *, uploader=None) -> dict:
    from notion_reporter import upload_case

    out = _assemble_out(state)
    report_md = _render_report_md(state["artifact"], state["decision_time"], out)
    upload = uploader or upload_case
    return {"notion_upload": upload(state["artifact"], state["decision_time"], out, report_md=report_md)}


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def build_pipeline():
    g = StateGraph(QAState)
    g.add_node("check_evidence", check_evidence)
    g.add_node("hallucination_review", hallucination_review)
    g.add_node("draft_claim_narrative", draft_claim_narrative)
    g.add_node("supervise", supervise)
    g.add_node("notion_report", notion_report)
    g.set_entry_point("check_evidence")
    g.add_edge("check_evidence", "hallucination_review")
    g.add_edge("hallucination_review", "draft_claim_narrative")
    g.add_edge("draft_claim_narrative", "supervise")
    g.add_edge("supervise", "notion_report")
    g.add_edge("notion_report", END)
    return g.compile()


@observe_pipeline(QA_TELEMETRY, "pipeline")
def run_qa_department(artifact: dict, evidence_store: dict, decision_time: str) -> dict:
    """본부 단독 실행 - Risk/Research 의 run_<dept>_department 와 같은 외부 인터페이스."""
    out = build_pipeline().invoke({
        "artifact": artifact, "evidence_store": evidence_store, "decision_time": decision_time,
    })
    result = _assemble_out(out)
    result["notion_upload"] = out.get("notion_upload")
    return result


# ── 자체 점검 (Ollama·Hermes 없음) ──────────────────────────────────────────
def _check_graph_shape():
    p = build_pipeline()
    assert p is not None
    print("  그래프 컴파일              OK")


def _check_fail_still_narrates():
    # FAIL 판정이어도 워커·부서장 둘 다 계속 불려 서술이 남는지, decision 값 자체는
    # 그대로 유지되는지 - Ollama·Hermes·Agentic RAG 콜은 전부 스텁으로 대체한다. notion_report 도
    # 스텁한다 - 안 그러면 ai-office/.dev.vars 에 실제 토큰이 있을 때 자체 점검이 진짜 Notion에
    # 네트워크 호출을 낸다(03-risk/scripts.py와 동일 이유).
    global check_evidence, hallucination_review, _call_internal_llm, _hermes_chat, notion_report
    orig = (check_evidence, hallucination_review, _call_internal_llm, _hermes_chat, notion_report)
    check_evidence = lambda s: {"assessment": {
        "qa_decision_id": "d1", "decision": "FAIL",
        "reason_codes": ["fact_without_evidence"],
        "claim_checks": [{"claim_index": 0, "claim": "x", "result": "UNSUPPORTED", "reason": "근거 없음"}],
        "findings": [{"finding_type": "unsupported_claim", "severity": "HIGH", "description": "d"}],
    }}
    hallucination_review = lambda s: {"hallucination_reviews": [
        {"claim_index": 0, "answer": {"verdict": "HALLUCINATION"}, "grounded": True}]}
    _call_internal_llm = lambda prompt: "요약: 근거 없는 주장 1건"
    _hermes_chat = lambda persona, task: '{"narrative": "차단됨", "escalate": true, "cited_checks": ["0"]}'
    notion_report = lambda s: {"notion_upload": {"ok": False, "reason": "self-check stub"}}
    try:
        out = build_pipeline().invoke({"artifact": {}, "evidence_store": {}, "decision_time": "x"})
        assert out["assessment"]["decision"] == "FAIL"
        assert out["verdict"] == "FAIL"
        assert out["escalate"] is True
        assert out["hallucination_reviews"][0]["answer"]["verdict"] == "HALLUCINATION"
    finally:
        check_evidence, hallucination_review, _call_internal_llm, _hermes_chat, notion_report = orig
    print("  FAIL 판정에도 서술 계속됨   OK")


def _check_hallucination_review_conditional():
    # SUPPORTED/PARTIAL 뿐이면 Agentic RAG 콜 자체를 안 하는지(비용 절감), UNSUPPORTED/CONTRADICTED가
    # 있으면 그 claim만 골라 hallucination-critic 페르소나로 넘기는지 확인한다 - run_compliance_check을
    # src.graph 모듈에서 직접 스텁해 네트워크 없이 점검한다(hallucination_review는 함수 안에서 Lazy Import).
    import src.graph as agentic_graph

    clean = {"assessment": {"claim_checks": [
        {"claim_index": 0, "claim": "x", "result": "SUPPORTED", "reason": "ok"}]}}
    flagged = {"assessment": {"claim_checks": [
        {"claim_index": 0, "claim": "ok", "result": "SUPPORTED", "reason": "ok"},
        {"claim_index": 1, "claim": "bad", "result": "UNSUPPORTED", "reason": "근거 없음"}]},
        "decision_time": "2026-08-02T00:00:00+00:00"}
    calls = []

    def stub(query, as_of, corpus_dir=None, persona="compliance-policy-agent"):
        calls.append((query, persona, corpus_dir.name if corpus_dir else None))
        return {"answer": {"verdict": "HALLUCINATION"}, "grounded": True}

    orig = agentic_graph.run_compliance_check
    agentic_graph.run_compliance_check = stub
    try:
        assert hallucination_review(clean) == {"hallucination_reviews": []}
        out = hallucination_review(flagged)
        assert calls == [("bad", "hallucination-critic", "evidence")]
        assert out["hallucination_reviews"][0]["claim_index"] == 1
    finally:
        agentic_graph.run_compliance_check = orig
    print("  Hallucination Critic 조건부 호출  OK")


def _check_supervisor_guard():
    a = {"decision": "PASS", "reason_codes": [], "claim_checks": [], "findings": []}
    bad_chat = lambda persona, task: '{"narrative": "n"}'  # escalate/cited_checks 누락
    try:
        supervise({"assessment": a, "claim_narrative": "n"}, chat=bad_chat)
        raise AssertionError("불완전 종합 결과가 통과했다")
    except ValueError:
        pass
    print("  Supervisor 스키마 가드      OK")


# ── MD 리포트 렌더 (결정론 - LLM 미개입, run_qa_department 반환값을 그대로 옮기기만 한다) ──
def _render_report_md(artifact: dict, decision_time: str, out: dict) -> str:
    checks = out.get("claim_checks", [])
    findings = out.get("findings", [])

    lines = [
        "# AI QA/감사본부 — 감사 보고서 (결정론적 생성, LLM 자유 서술 아님)",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| **qa_decision_id** | `{out['qa_decision_id']}` |",
        f"| **판정 (verdict)** | **{out['verdict']}** |",
        f"| **판정 엔진** | departments/06-ai-qa-audit/evidence/evidence_qa_engine.py (`{out['calculation_version']}`) |",
        f"| **input_hash** | `{out['input_hash']}` (같은 Artifact·Context면 재현 가능) |",
        f"| **decision_time (PIT)** | {md_cell(decision_time)} |",
        f"| **escalate** | {md_cell(out['escalate'])} |",
        f"| **생성** | {PIPELINE_VERSION}, {datetime.now(timezone.utc).isoformat()} |",
        "",
        "---",
        "",
        "## Claim별 검사 결과",
        "",
        "| # | Claim | 검사 결과 | 사유 |",
        "|---|---|---|---|",
    ]
    lines += [f"| {md_cell(c.get('claim_index'))} | {md_cell(c.get('claim'))} | "
              f"{md_cell(c.get('result'))} | {md_cell(c.get('reason'))} |" for c in checks]
    if not checks:
        lines.append("| — | (claim_checks 없음) | — | — |")

    lines += ["", "## Hallucination Critic (hallucination-critic, Agentic RAG)", ""]
    reviews = out.get("hallucination_reviews") or []
    if reviews:
        for r in reviews:
            answer = r.get("answer") or {}
            lines += [f"### Claim #{r['claim_index']}", "",
                      "| 필드 | 값 |", "|---|---|",
                      f"| verdict | {md_cell(answer.get('verdict'))} |",
                      f"| grounded | {md_cell(r.get('grounded'))} |",
                      f"| rationale | {md_cell(answer.get('rationale'))} |", ""]
    else:
        lines.append("UNSUPPORTED/CONTRADICTED claim 없음 - 조건부 노드 미호출")

    lines += ["", "## Reason Codes", "",
              ", ".join(f"`{r}`" for r in out["reason_codes"]) if out["reason_codes"] else "없음",
              "", "## Findings", ""]
    if findings:
        for f in findings:
            lines += [f"### `{md_cell(f.get('finding_id'))}`", "",
                      "| 필드 | 값 |", "|---|---|",
                      f"| 유형 | `{md_cell(f.get('finding_type'))}` |",
                      f"| 심각도 | {md_cell(f.get('severity'))} |",
                      f"| 설명 | {md_cell(f.get('description'))} |", ""]
    else:
        lines.append("없음")

    lines += [
        "", "## Claim 서술 (evidence-qa-agent, 내부 Ollama - 판정 재해석 없이 결과만 풀어씀)", "",
        md_cell(out["claim_narrative"]),
        "", "## 종합 서술 (qa-audit-supervisor, Hermes)", "",
        md_cell(out["narrative"]),
    ]

    evaluation = out.get("evaluation") or {}
    if evaluation:
        lines += ["", "## 평가 지표", "", "| 지표 | 값 |", "|---|---|"]
        lines += [f"| {md_cell(key)} | {json_cell(value)} |" for key, value in evaluation.items()]

    observability = out.get("observability") or {}
    if observability:
        lines += ["", "## LangSmith / HR 관측성 전달", "", "| 필드 | 값 |", "|---|---|",
                  f"| trace_id | `{md_cell(observability.get('trace_id'))}` |",
                  f"| LangSmith | {json_cell(observability.get('langsmith'))} |"]

    agent_execution = out.get("agent_execution") or {}
    if agent_execution:
        lines += ["", "## Agent 실행 매니페스트", "", "| 구분 | Agent |", "|---|---|"]
        lines += [f"| 실행 | {md_cell(agent)} |" for agent in agent_execution.get("executed", [])]
        lines += [f"| 미실행/조건부 | {md_cell(agent)} |" for agent in agent_execution.get("not_executed", [])]

    fallbacks = out.get("fallbacks") or []
    if fallbacks:
        lines += ["", "## Fallback / Escalation", "", "| 단계 | 오류 | 조치 |", "|---|---|---|"]
        lines += [f"| {md_cell(item.get('stage'))} | {md_cell(item.get('error'))} | "
                  f"{md_cell(item.get('action'))} |" for item in fallbacks]

    notion = out.get("notion_upload")
    if notion is not None:
        lines += ["", "## Notion 업로드 (Reporter Node)", ""]
        lines.append(f"업로드 성공: {notion['url']}" if notion.get("ok")
                     else f"업로드 생략/실패: {notion.get('reason') or notion.get('error')}")

    lines += [
        "", "---",
        "> 이 문서는 evidence_qa_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로",
        "> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.",
    ]
    return "\n".join(lines)


def _check_notion_report_node():
    captured = {}

    def stub_uploader(artifact, decision_time, out, *, report_md=""):
        captured["out"], captured["report_md"] = out, report_md
        return {"ok": True, "url": "https://notion.so/fake"}

    state = {"artifact": {"trace_id": "t1"}, "decision_time": "2026-08-01", "narrative": "n", "escalate": False,
             "claim_narrative": "cn", "verdict": "PASS",
             "assessment": {"qa_decision_id": "d1", "decision": "PASS", "reason_codes": [],
                            "claim_checks": [], "findings": [], "calculation_version": "v", "input_hash": "h"}}
    result = notion_report(state, uploader=stub_uploader)
    assert result["notion_upload"] == {"ok": True, "url": "https://notion.so/fake"}
    assert result["report_markdown"]
    assert captured["out"]["qa_decision_id"] == "d1"
    assert "qa_decision_id" in captured["report_md"]  # _render_report_md 가 실제로 불렸는지
    print("  Notion Reporter 노드        OK")


_RAW_CHECK_EVIDENCE = check_evidence
_RAW_HALLUCINATION_REVIEW = hallucination_review
_RAW_DRAFT_CLAIM_NARRATIVE = draft_claim_narrative
_RAW_SUPERVISE = supervise


def check_evidence(state: QAState) -> dict:
    try:
        return _RAW_CHECK_EVIDENCE(state)
    except Exception as exc:
        return _fallback_assessment(state, exc)


def hallucination_review(state: QAState) -> dict:
    try:
        return _RAW_HALLUCINATION_REVIEW(state)
    except Exception as exc:
        flagged = [c for c in state.get("assessment", {}).get("claim_checks", [])
                   if c.get("result") in ("UNSUPPORTED", "CONTRADICTED")]
        return {"hallucination_reviews": [{"claim_index": c.get("claim_index"), "answer": {
                    "verdict": "HALLUCINATION", "cited_documents": [],
                    "rationale": _fallback_narrative("Hallucination Critic", exc),
                    "confidence": 0.0, "escalate": True}, "grounded": False,
                    "fallback": True, "fallback_reason": type(exc).__name__} for c in flagged],
                "fallbacks": [_fallback("hallucination_review", exc)]}


def draft_claim_narrative(state: QAState) -> dict:
    try:
        return _RAW_DRAFT_CLAIM_NARRATIVE(state)
    except Exception as exc:
        checks = state.get("assessment", {}).get("claim_checks", [])
        summary = "; ".join(f"Claim {c.get('claim_index')}: {c.get('result')} — {c.get('reason')}"
                            for c in checks) or "검사된 Claim 없음"
        return {"claim_narrative": f"결정론적 Evidence QA 결과를 전달합니다. {summary}",
                "fallbacks": [_fallback("claim_narrative", exc)]}


def supervise(state: QAState, *, chat=None) -> dict:
    try:
        return _RAW_SUPERVISE(state, chat=chat)
    except Exception as exc:
        if chat is not None:
            raise
        decision = state.get("assessment", {}).get("decision", "FAIL")
        return {"verdict": decision, "narrative": _fallback_narrative("QA Supervisor", exc),
                "escalate": True, "fallbacks": [_fallback("supervisor", exc)]}


_RAW_ASSEMBLE_OUT = _assemble_out


def _assemble_out(state: QAState) -> dict:
    out = _RAW_ASSEMBLE_OUT(state)
    trace_id = (state.get("artifact") or {}).get("trace_id")
    trace_id = trace_id or f"{PIPELINE_VERSION}:{out['qa_decision_id']}"
    out["fallbacks"] = state.get("fallbacks", [])
    out["observability"] = langsmith_handoff(str(trace_id))
    executed_agents = []
    if out.get("claim_checks") is not None:
        executed_agents.append("evidence-qa-agent")
    if out.get("hallucination_reviews"):
        executed_agents.append("hallucination-critic")
    if out.get("claim_narrative"):
        executed_agents.append("evidence-qa-agent")
    if out.get("narrative"):
        executed_agents.append("qa-audit-supervisor")
    qa_agents = ["qa-audit-supervisor", "evidence-qa-agent", "hallucination-critic", "model-risk-agent",
                 "internal-audit-agent", "agent-ops-monitor", "tool-permission-security-reviewer",
                 "incident-postmortem-agent"]
    out["agent_execution"] = {"executed": list(dict.fromkeys(executed_agents)),
                               "not_executed": [agent for agent in qa_agents if agent not in executed_agents]}
    return out


_RAW_NOTION_REPORT = notion_report


def notion_report(state: QAState, *, uploader=None) -> dict:
    out = _assemble_out(state)
    report_md = _render_report_md(state["artifact"], state["decision_time"], out)
    evaluation = evaluation_metrics(out, report_md)
    report_md = _render_report_md(state["artifact"], state["decision_time"],
                                   {**out, "evaluation": evaluation})
    evaluation = evaluation_metrics(out, report_md)
    from notion_reporter import upload_case
    try:
        upload = uploader or upload_case
        notion_upload = upload(state["artifact"], state["decision_time"], out, report_md=report_md)
    except Exception as exc:
        notion_upload = {"ok": False, "reason": f"Reporter 예외: {type(exc).__name__}"}
        evaluation["fallback_count"] += 1
    evaluation = evaluation_metrics({**out, "notion_upload": notion_upload}, report_md)
    return {"notion_upload": notion_upload, "report_markdown": report_md, "evaluation": evaluation}


_RAW_RUN_QA_DEPARTMENT = run_qa_department


def run_qa_department(artifact: dict, evidence_store: dict, decision_time: str) -> dict:
    try:
        out = build_pipeline().invoke({"artifact": artifact, "evidence_store": evidence_store,
                                       "decision_time": decision_time})
    except Exception as exc:
        out = {"artifact": artifact, "evidence_store": evidence_store, "decision_time": decision_time,
               **_fallback_assessment({"artifact": artifact}, exc),
               "claim_narrative": _fallback_narrative("QA pipeline", exc),
               "hallucination_reviews": [], "verdict": "FAIL",
               "narrative": _fallback_narrative("QA Supervisor", exc), "escalate": True}
        out.update(notion_report(out))
    result = _assemble_out(out)
    result["notion_upload"] = out.get("notion_upload")
    result["report_markdown"] = out.get("report_markdown", "")
    result["evaluation"] = out.get("evaluation", evaluation_metrics(result))
    return result


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
                       "evidence_ids": [] if "--fail" in sys.argv else [eid]}],
        }
        demo_evidence_store = {eid: {"source": "market-api", "published_at": now_iso,
                                     "observed_at": now_iso, "excerpt": "종가 70000원",
                                     "numeric_value": "70000", "unit": "KRW"}}
        print(f"{PIPELINE_VERSION} 실행 (데모 Artifact{' - FAIL 유도' if '--fail' in sys.argv else ''})")
        out = run_qa_department(demo_artifact, demo_evidence_store, now_iso)
        print(json.dumps(out, ensure_ascii=False, indent=1))

        report_dir = _BASE / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"qa_audit_report_{out['qa_decision_id']}.md"
        report_path.write_text(_render_report_md(demo_artifact, now_iso, out), encoding="utf-8")
        print(f"결정론적 MD 리포트 저장: {report_path}")
        raise SystemExit(0)

    print(f"{PIPELINE_VERSION} 자체 점검 (Ollama·Hermes·Agentic RAG 없음)")
    _check_graph_shape()
    _check_fail_still_narrates()
    _check_hallucination_review_conditional()
    _check_supervisor_guard()
    _check_notion_report_node()
    print("본부 파이프라인 5개 영역 통과. 실행은 --run (내부 Ollama·Hermes·OPENAI_API_KEY 필요)")
