#!/usr/bin/env python3
"""리스크본부 LangGraph 파이프라인 - run_risk_department(order_intent, context) -> Case Decision.

담당: 동규 (리스크/QA)
근거: QA 부서 패턴(departments/06-ai-qa-audit/scripts.py)을 재일님이 리서치본부에
      적용한 개선판(departments/01-research/scripts.py, 2026-07-31)을 리스크본부에 적용.

QA 원안과의 차이 - **노드가 프롬프트가 아니라 config.yaml의 실제 페르소나 + 이미 구현된
결정론적 서비스다** (research 패턴을 그대로 따름):
  check_trading_state  market-liquidity-risk-agent (결정론 - Redis 최신 Trading State)
  pre_trade_check      pre-trade-risk-analyst (결정론 - RiskEngine.check_order, P0 Gate)
  counterparty_check   operational-counterparty-risk-agent (조건부 - counterparty_health 체크가
                        DOWN/DEGRADED일 때만, risk_engine.py의 실제 CheckOutcome을 근거로 호출)
  compliance_check     compliance-policy-agent (skills/agentic-rag 기존 baseline 재사용)
  supervise            risk-supervisor 페르소나 (hermes/config.yaml 원문 사용, 종합·서술만)
  notion_report        Reporter Node (결정론 - notion_reporter.upload_case, LLM 아님) - 최종 Case를
                        Notion Risk DB(NOTION_RISK_DB)에 Projection으로 올린다. 실패해도 RiskState의
                        바인딩 판정은 못 바꾼다 - notion_upload 필드에 성공/실패만 기록(2026-08-02 추가)

derivatives-margin-risk-agent는 여전히 뺐다 - config.yaml의 not_started 항목대로 Greeks·margin
전용 API가 없는 것과 별개로, OrderIntent/RiskContext 어디에도 파생상품 여부를 나타내는 필드가
없다(연구본부가 microstructure/technical/fundamental/sector-regime 4개 페르소나를 같은 이유로
뺀 것과 동일) - 트리거할 실제 신호가 없는 노드를 조건부로 "연결"하면 조건이 항상 거짓이거나
날조한 기준이 된다. operational-counterparty-risk-agent는 다르다 - risk_engine.py 10단계 중
counterparty_health 체크가 이미 실측(ctx.counterparty.health)을 CheckOutcome으로 남기므로,
그 실제 결과를 조건으로 conditional edge를 태울 수 있다.

원칙 (전 노드 공통, CLAUDE.md):
  - 바인딩 verdict는 RiskEngine.check_order 결과에서만 온다. risk-supervisor LLM은
    그 verdict를 절대 못 바꾸고, 서술(narrative)만 만든다.
  - REJECT면 compliance_check(비용이 큰 Agentic RAG 루프)를 건너뛰고 바로 종합한다 -
    이미 막힌 주문에 정책 검색을 더 태울 이유가 없다 (개발 원칙 9번).
  - Redis(REDIS_URL)·Compliance(OPENAI_API_KEY, agentic-rag 내부)는 환경변수다 - 특정 PC 주소를
    하드코딩하지 않는다.
  - 부서장(Hermes AIAgent)은 config.yaml의 model.default를 그대로 쓴다 - 부서별 env var로 바꾸지 않는다
    (CLAUDE.md "model은 8개 파일 모두 동일, 바꾸려면 8개를 함께 바꾼다"). QA 패턴(departments/06-ai-qa-audit/
    scripts.py)과 동일 - risk라서 Hermes를 뺄 이유는 없다.
  - run_agent(Hermes Runtime) import는 모듈 최상단이 아니라 _hermes_chat 함수 안에서 한다(Lazy Import) -
    이 패키지는 프로젝트 .venv가 아니라 별도 Hermes 설치 venv(예: ~/claude)에만 있으므로, 최상단에서
    부르면 --run 없이 자체 점검만 하려는 경우에도 ModuleNotFoundError로 모듈 전체가 죽는다.

실행:
  python scripts.py               # 자체 점검 (Redis·Hermes 없음, run_agent 안 불러도 됨 - Lazy Import)

  --run (REDIS_URL, OPENAI_API_KEY, Hermes 필요) 은 .env(프로젝트 루트)를 셸에 먼저 로드해야 한다.
  departments/03-risk 안에서 바로 실행하면 .env를 못 찾아 KeyError: 'REDIS_URL' 이 난다:
    
    cd /Users/baiohelseu/Desktop/Project/multi_agent
    set -a && source .env && set +a 
    source ~/claude/bin/activate      
    cd departments/03-risk
    python scripts.py --run

  reports/risk_case_report_<risk_request_id>.md 로 결정론적 MD 리포트도 저장한다 - _render_report_md 는
  순수 함수(run_risk_department 반환값을 그대로 옮김) 이고 LLM은 narrative/compliance answer 필드에만
  관여한다 (QA 패턴과 동일).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

_BASE = Path(__file__).resolve().parent
_REPO_ROOT = _BASE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_ENGINE_DIR = _BASE / "engine"
_API_DIR = _BASE / "api"
_CONTRACTS_DIR = _BASE.parent / "02-trading" / "contracts"
_AGENTIC_RAG_DIR = _BASE.parent.parent / "skills" / "agentic-rag"
for _p in (_ENGINE_DIR, _API_DIR, _CONTRACTS_DIR, _AGENTIC_RAG_DIR):
    sys.path.insert(0, str(_p))

from langgraph.graph import END, StateGraph
from reporting import (
    evaluation_metrics,
    json_cell,
    langsmith_handoff,
    md_cell,
)

from apps.observability.risk_qa import get_runtime_telemetry, observe_pipeline

PIPELINE_VERSION = "risk-department-pipeline-v1"
RISK_TELEMETRY = get_runtime_telemetry("risk")
from src.resilience import register_circuit_breaker_observer

register_circuit_breaker_observer(RISK_TELEMETRY.record_circuit_breaker)


_JOURNAL_MODULE = None


def _journal_module():
    """Load this department's journal without colliding with QA's package."""

    global _JOURNAL_MODULE
    if _JOURNAL_MODULE is None:
        journal_path = _BASE / "harness" / "journal.py"
        spec = importlib.util.spec_from_file_location("risk_execution_journal", journal_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Risk journal을 로드할 수 없습니다: {journal_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _JOURNAL_MODULE = module
    return _JOURNAL_MODULE


class RiskState(TypedDict, total=False):
    order_intent: dict     # OrderIntent JSON
    context: dict           # RiskContextIn JSON (trading_state 필드는 아래서 실측으로 덮어씀)
    scope: str               # trading_state_store 조회 scope (예: "fund:<uuid>")
    trading_state: str      # market-liquidity-risk-agent 결과
    assessment: dict        # pre-trade-risk-analyst 결과 (RiskAssessment)
    counterparty: dict | None  # operational-counterparty-risk-agent 결과 (플래그 안 되면 생략)
    compliance: dict | None  # compliance-policy-agent 결과 (REJECT면 생략)
    verdict: str             # 최종 case-level 값 - 항상 assessment 에서만 옴
    narrative: str
    escalate: bool
    notion_upload: dict | None  # Reporter Node 결과 ({"ok": bool, "url"|"reason"|"error": ...})
    fallbacks: list[dict]
    observability: dict
    report_markdown: str
    evaluation: dict


def _fallback(stage: str, exc: Exception) -> dict:
    return {"stage": stage, "error": type(exc).__name__, "action": "ESCALATE"}


def _fallback_narrative(stage: str, exc: Exception) -> str:
    return (f"{stage} Agent를 사용할 수 없어 결정론적 결과만 유지했습니다 "
            f"({type(exc).__name__}). 수동 검토와 후속 상태 확인이 필요합니다.")


def _fallback_assessment(order_intent: dict, context: dict, exc: Exception) -> dict:
    payload = json.dumps({"order_intent": order_intent, "context": context},
                         ensure_ascii=False, sort_keys=True, default=str)
    return {"risk_request_id": f"fallback-{hashlib.sha256(payload.encode()).hexdigest()[:16]}",
            "verdict": "reject", "approved_quantity": None, "reason_codes": ["pipeline_fallback"],
            "check_results": [], "calculation_version": "risk-pipeline-fallback-v1",
            "input_hash": hashlib.sha256(payload.encode()).hexdigest(),
            "trading_state": "HALTED"}


# ── 노드 1: Trading State 실측 (결정론 직원) ────────────────────────────────
def check_trading_state(state: RiskState) -> dict:
    try:
        import redis as redis_lib
        from trading_state_store import RedisTradingStateStore

        store = RedisTradingStateStore(redis_lib.Redis.from_url(os.environ["REDIS_URL"]))
        ts = store.get_state_fail_closed(state.get("scope", "fund:default"))
        return {"trading_state": ts.value}
    except Exception as exc:
        # Redis failure is a state-uncertainty condition, never an ENABLED fallback.
        return {"trading_state": "HALTED", "fallbacks": [_fallback("trading_state", exc)]}


# ── 노드 2: Pre-trade Gate (결정론 직원 - RiskEngine) ───────────────────────
def pre_trade_check(state: RiskState) -> dict:
    from app import RiskContextIn  # api/app.py 의 JSON->RiskContext 변환을 재사용
    from contracts import OrderIntent
    from risk_engine import RiskEngine

    intent = OrderIntent(**state["order_intent"])
    ctx = RiskContextIn(**{**state["context"], "trading_state": state["trading_state"]}).to_context()
    result = RiskEngine().check_order(intent, ctx)
    d = result.decision
    return {"assessment": {
        "risk_request_id": str(result.risk_request_id),
        "verdict": d.verdict.value,
        "approved_quantity": str(d.approved_quantity) if d.approved_quantity is not None else None,
        "reason_codes": [r.value for r in result.reason_codes],
        "calculation_version": result.calculation_version, "input_hash": result.input_hash,
        "check_results": [{"name": c.check_name, "passed": c.passed, "detail": c.detail}
                          for c in result.check_results],
    }}


# ── 노드 3: Counterparty (조건부 - operational-counterparty-risk-agent) ────
def _counterparty_flagged(assessment: dict) -> bool:
    """pre_trade_check(RiskEngine) 가 이미 실측한 counterparty_health CheckOutcome 만 본다 -
    새로 데이터를 만들지 않는다. DOWN(reject)이면 passed=False, DEGRADED면 detail이 채워진다
    (risk_engine.py 10단계, "" if OK else "DEGRADED 상태 - 통과하되 기록")."""
    for c in assessment.get("check_results", []):
        if c.get("name") == "counterparty_health":
            return not c.get("passed", True) or bool(c.get("detail"))
    return "counterparty_unhealthy" in assessment.get("reason_codes", [])


def counterparty_check(state: RiskState, *, chat=None) -> dict:
    a = state["assessment"]
    cp = next((c for c in a["check_results"] if c["name"] == "counterparty_health"), None)
    task = f"""Using ONLY the evidence below, write a 1-2 sentence Korean note on Broker/Counterparty
status for this case. Prioritize status confirmation and Reconciliation over new orders when the
Broker state is not healthy - you cannot change the deterministic verdict "{a['verdict']}", only
explain the counterparty angle of it.
Schema (JSON only):
{{"counterparty_narrative": "1-2 sentences", "escalate": true or false}}

Evidence:
{json.dumps({"order_intent": state["order_intent"], "counterparty_health_check": cp,
             "reason_codes": a["reason_codes"], "verdict": a["verdict"]}, ensure_ascii=False, indent=1)}"""

    try:
        call = chat or _hermes_chat
        out = call(_persona("operational-counterparty-risk-agent"), task)
        s, e = out.find("{"), out.rfind("}")
        note = json.loads(out[s:e + 1])
        for k in ("counterparty_narrative", "escalate"):
            if k not in note:
                raise ValueError(f"Counterparty 점검 결과에 {k} 가 없다 - 초안 거부")
        return {"counterparty": note}
    except Exception as exc:
        if chat is not None:
            raise
        return {
            "counterparty": {"counterparty_narrative": _fallback_narrative("Counterparty", exc),
                              "escalate": True},
            "fallbacks": [_fallback("counterparty", exc)],
        }


# ── 노드 4: Compliance (skills/agentic-rag 기존 baseline 재사용) ───────────
def compliance_check(state: RiskState) -> dict:
    try:
        from src.graph import run_compliance_check

        oi = state["order_intent"]
        query = (f"Can we execute a {oi['side']} order for {oi['quantity']} units of "
                 f"instrument {oi['instrument_id']} under fund {oi['fund_id']} today?")
        as_of_date = state["context"]["as_of"][:10]  # PIT 필터는 date-only(YYYY-MM-DD)를 요구한다
        return {"compliance": run_compliance_check(query, as_of_date, persona="compliance-policy-agent")}
    except Exception as exc:
        return {
            "compliance": {"answer": {"verdict": "ambiguous", "cited_documents": [],
                                        "rationale": _fallback_narrative("Compliance", exc),
                                        "confidence": 0.0, "escalate": True},
                            "grounded": False, "fallback": True, "attempts": 0,
                            "fallback_reason": type(exc).__name__, "relevant_documents": []},
            "fallbacks": [_fallback("compliance", exc)],
        }


# ── 노드 5: 종합 (risk-supervisor 페르소나 - Hermes AIAgent) ───────────────
def _persona(name: str) -> str:
    cfg = (_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8")
    return re.search(rf'{re.escape(name)}: "(.*?)"\n', cfg, re.DOTALL).group(1)


def _hermes_chat(persona: str, task: str) -> str:
    from run_agent import (
        AIAgent,  # Lazy Import - Hermes 없는 환경에서도 모듈 자체는 항상 import 가능해야 한다
    )

    agent = AIAgent(model="poolside/laguna-s-2.1:free", quiet_mode=True,
                     ephemeral_system_prompt=persona)
    return agent.chat(task)


def supervise(state: RiskState, *, chat=None) -> dict:
    a = state["assessment"]
    bundle = {"order_intent": state["order_intent"], "trading_state": state["trading_state"],
              "assessment": a, "compliance": state.get("compliance"),
              "counterparty": state.get("counterparty")}
    task = f"""Using ONLY the evidence below, write a case-level risk narrative in Korean for
CEO/Audit review. The binding verdict is "{a['verdict']}" from the deterministic Risk Engine -
you cannot change it, only cite and explain it. If "counterparty" evidence is present, weave its
counterparty_narrative into the case narrative.
Schema (JSON only):
{{"narrative": "2-4 sentences, cite check names and the compliance/counterparty results",
 "escalate": true or false, "cited_checks": ["check names referenced"]}}

Evidence:
{json.dumps(bundle, ensure_ascii=False, indent=1)}"""

    try:
        call = chat or _hermes_chat
        out = call(_persona("risk-supervisor"), task)
        s, e = out.find("{"), out.rfind("}")
        note = json.loads(out[s:e + 1])
        for k in ("narrative", "escalate", "cited_checks"):
            if k not in note:
                raise ValueError(f"Supervisor 종합 결과에 {k} 가 없다 - 초안 거부")
        # 바인딩 verdict 는 LLM 출력이 아니라 assessment 에서 그대로 가져온다
        return {"verdict": a["verdict"], "narrative": note["narrative"], "escalate": note["escalate"]}
    except Exception as exc:
        if chat is not None:
            raise
        return {"verdict": a["verdict"], "narrative": _fallback_narrative("Risk Supervisor", exc),
                "escalate": True, "fallbacks": [_fallback("supervisor", exc)]}


def _assemble_out(state: RiskState) -> dict:
    """RiskState -> run_risk_department()/notion_report 공용 결과 dict. 필드 목록을 한 곳에서만
    유지한다 - 그래프 노드(notion_report)와 외부 인터페이스(run_risk_department)가 따로 베끼면
    한쪽만 필드를 늘렸을 때 드리프트가 생긴다."""
    a = state["assessment"]
    trace_id = (state.get("context") or {}).get("trace_id") or (state.get("order_intent") or {}).get("trace_id")
    trace_id = trace_id or f"{PIPELINE_VERSION}:{a['risk_request_id']}"
    executed_agents = []
    if a.get("check_results"):
        executed_agents += ["market-liquidity-risk-agent", "pre-trade-risk-analyst"]
    if state.get("counterparty"):
        executed_agents.append("operational-counterparty-risk-agent")
    if state.get("compliance"):
        executed_agents.append("compliance-policy-agent")
    if state.get("narrative"):
        executed_agents.append("risk-supervisor")
    risk_agents = ["risk-supervisor", "market-liquidity-risk-agent", "derivatives-margin-risk-agent",
                   "compliance-policy-agent", "pre-trade-risk-analyst", "operational-counterparty-risk-agent"]
    return {"risk_request_id": a["risk_request_id"], "verdict": state["verdict"],
            "approved_quantity": a["approved_quantity"], "reason_codes": a["reason_codes"],
            "check_results": a["check_results"], "calculation_version": a["calculation_version"],
            "input_hash": a["input_hash"], "trading_state": state["trading_state"],
            "counterparty": state.get("counterparty"), "compliance": state.get("compliance"),
            "narrative": state["narrative"], "escalate": state["escalate"],
            "fallbacks": state.get("fallbacks", []), "observability": langsmith_handoff(str(trace_id)),
        "agent_execution": {"executed": executed_agents,
        "not_executed": [agent for agent in risk_agents if agent not in executed_agents]},
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
    """Record the Hermes/LangGraph boundary without changing the Risk verdict."""

    try:
        journal_module = _journal_module()
        sink = journal_module.jsonl_sink(log_path) if log_path else None
        journal = journal_module.RunJournal(
            department="risk-management",
            hermes_profile="risk-management",
            sink=sink,
        )
        input_event = journal.input_snapshot(
            run_id=run_id,
            trace_id=trace_id,
            employee_profile="risk-supervisor",
            payload=payload,
            schema_id="risk.department.input.v1",
            as_of=as_of,
            asset=asset,
            model_version=PIPELINE_VERSION,
            prompt_version="risk-hermes-profile-v1",
            parameter_version="risk-engine-contract-v1",
        )
        execution = result.get("agent_execution") or {}
        executed = list(execution.get("executed") or [])
        not_executed = list(execution.get("not_executed") or [])
        fallbacks = list(result.get("fallbacks") or [])
        compliance = result.get("compliance") or {}
        degraded = bool(fallbacks or result.get("escalate") or compliance.get("grounded") is False)
        schema_valid = bool(result.get("assessment")) and not bool(fallbacks)
        domain_valid = result.get("verdict") in {"approve", "resize", "reject"} and not degraded
        agents_for_log = executed or ["risk-pipeline-fallback"]
        for employee in agents_for_log:
            journal.agent_output(
                run_id=run_id,
                trace_id=trace_id,
                employee_profile=employee,
                output={
                    "employee_profile": employee,
                    "verdict": result.get("verdict"),
                    "evidence_refs": ("compliance-policy-agent",) if compliance else (),
                },
                inputs_hash=input_event.inputs_hash,
                schema_id="risk.agent-output.v1",
                schema_valid=schema_valid,
                domain_valid=domain_valid,
                failed_rule=(result.get("reason_codes") or [None])[0],
                fallback_reason=(fallbacks[0].get("stage") if fallbacks else None),
                retry_count=max((item.get("attempt", 0) for item in fallbacks), default=0),
                as_of=as_of,
                asset=asset,
                model_version=PIPELINE_VERSION,
                prompt_version="risk-hermes-profile-v1",
                parameter_version="risk-engine-contract-v1",
            )
        failed_rule = (result.get("reason_codes") or [None])[0]
        if fallbacks and not failed_rule:
            failed_rule = fallbacks[0].get("stage") or "pipeline_fallback"
        journal.validation(
            run_id=run_id,
            trace_id=trace_id,
            employee_profile="risk-supervisor",
            inputs_hash=input_event.inputs_hash,
            schema_id="risk.department.output.v1",
            schema_valid=schema_valid,
            domain_valid=domain_valid,
            failed_rule=failed_rule,
            fallback_reason=(fallbacks[0].get("reason") if fallbacks else None),
            retry_count=max((item.get("attempt", 0) for item in fallbacks), default=0),
            raw={"executed": executed, "not_executed": not_executed},
            as_of=as_of,
            asset=asset,
        )
        safe_action = "HOLD" if degraded else result.get("verdict", "HOLD")
        journal.decision(
            run_id=run_id,
            trace_id=trace_id,
            employee_profile="risk-supervisor",
            inputs_hash=input_event.inputs_hash,
            output={
                "decision": result.get("verdict"),
                "safe_action": safe_action,
                "binding": False,
            },
            schema_id="risk.department.decision.v1",
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
            "hermes_profile": "risk-management",
            "employees_executed": executed,
            "employees_not_executed": not_executed,
            "retry_policy": {"max_retries": 2, "max_attempts": 3},
            "external_dependencies": {
                "redis": "DEGRADED" if any(item.get("stage") == "trading_state" for item in fallbacks) else "NOT_OBSERVED",
                "compliance_rag": "EXECUTED" if compliance else "NOT_EXECUTED",
                "canonical_db": "CONFIGURED" if os.environ.get("DATABASE_URL") else "NOT_CONFIGURED",
                "portfolio_market_snapshot": "SUPPLIED_UNVERIFIED" if payload.get("context", {}).get("portfolio") else "MISSING",
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
            "candidate_verdict": result.get("verdict", "reject"),
            "safe_action": "HOLD",
            "binding": False,
            "journal_error": type(exc).__name__,
            "journal_path": str(log_path) if log_path else None,
        }


# ── 노드 6: Notion 업로드 (Reporter Node - 결정론, LLM 아님) ────────────────
def notion_report(state: RiskState, *, uploader=None) -> dict:
    from notion_reporter import upload_case

    out = _assemble_out(state)
    report_md = _render_report_md(state["order_intent"], state["context"], out)
    evaluation = evaluation_metrics(out, report_md)
    report_md = _render_report_md(state["order_intent"], state["context"],
                                   {**out, "evaluation": evaluation})
    evaluation = evaluation_metrics(out, report_md)
    upload = uploader or upload_case
    try:
        notion_upload = upload(state["order_intent"], state["context"], out, report_md=report_md)
    except Exception as exc:
        notion_upload = {"ok": False, "reason": f"Reporter 예외: {type(exc).__name__}"}
        evaluation["fallback_count"] += 1
    evaluation = evaluation_metrics({**out, "notion_upload": notion_upload}, report_md)
    return {"notion_upload": notion_upload, "report_markdown": report_md, "evaluation": evaluation}


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def _route_after_pretrade(state: RiskState) -> str:
    # counterparty_health 가 실측으로 플래그됐으면(DOWN/DEGRADED) verdict 와 무관하게 먼저 태운다 -
    # REJECT 사유가 counterparty 든 아니든, 실측 신호가 있으면 그 신호부터 반영한다.
    if _counterparty_flagged(state["assessment"]):
        return "counterparty"
    # 그 외 REJECT 면 정책 검색(Agentic RAG)을 태우지 않고 바로 종합한다 - 이미 막힌 주문이다
    return "supervise" if state["assessment"]["verdict"] == "reject" else "compliance"


def _route_after_counterparty(state: RiskState) -> str:
    # counterparty_check 를 거친 뒤에도 REJECT short-circuit 규칙은 그대로 적용한다
    return "supervise" if state["assessment"]["verdict"] == "reject" else "compliance"


def build_pipeline():
    g = StateGraph(RiskState)
    g.add_node("check_trading_state", check_trading_state)
    g.add_node("pre_trade_check", pre_trade_check)
    g.add_node("counterparty_check", counterparty_check)
    g.add_node("compliance_check", compliance_check)
    g.add_node("supervise", supervise)
    g.add_node("notion_report", notion_report)
    g.set_entry_point("check_trading_state")
    g.add_edge("check_trading_state", "pre_trade_check")
    g.add_conditional_edges("pre_trade_check", _route_after_pretrade,
                            {"counterparty": "counterparty_check",
                             "supervise": "supervise", "compliance": "compliance_check"})
    g.add_conditional_edges("counterparty_check", _route_after_counterparty,
                            {"supervise": "supervise", "compliance": "compliance_check"})
    g.add_edge("compliance_check", "supervise")
    g.add_edge("supervise", "notion_report")
    g.add_edge("notion_report", END)
    return g.compile()


@observe_pipeline(RISK_TELEMETRY, "pipeline")
def run_risk_department(
    order_intent: dict,
    context: dict,
    scope: str = "fund:default",
    *,
    run_id: str | None = None,
    log_path: str | Path | None = None,
) -> dict:
    """본부 단독 실행 - QA/연구본부의 run_<dept>_department 와 같은 외부 인터페이스."""
    execution_run_id = run_id or str(uuid4())
    trace_id = str(context.get("trace_id") or order_intent.get("trace_id") or f"risk:{execution_run_id}")
    payload = {"order_intent": order_intent, "context": context, "scope": scope}
    as_of = str(context.get("as_of") or order_intent.get("snapshot", {}).get("as_of") or "") or None
    asset = str(order_intent.get("instrument_id")) if order_intent.get("instrument_id") else None
    try:
        out = build_pipeline().invoke({"order_intent": order_intent, "context": context, "scope": scope})
    except Exception as exc:
        out = {"order_intent": order_intent, "context": context, "scope": scope,
               "assessment": _fallback_assessment(order_intent, context, exc),
               "trading_state": "HALTED", "verdict": "reject",
               "narrative": _fallback_narrative("Risk pipeline", exc), "escalate": True,
               "fallbacks": [_fallback("pipeline", exc)]}
    result = _assemble_out(out)
    out["execution_evidence"] = _record_execution_evidence(
        payload,
        result,
        run_id=execution_run_id,
        trace_id=trace_id,
        as_of=as_of,
        asset=asset,
        log_path=log_path,
    )
    out.update(notion_report(out))
    result = _assemble_out(out)
    result["execution_evidence"] = out["execution_evidence"]
    result["notion_upload"] = out.get("notion_upload")
    result["report_markdown"] = out.get("report_markdown", "")
    result["evaluation"] = out.get("evaluation", evaluation_metrics(result))
    return result


def _render_report_md(order_intent: dict, context: dict, out: dict) -> str:
    """out(run_risk_department 반환값)을 그대로 옮겨 적는 순수 함수 - LLM이 리포트 구조나
    내용을 창작하지 않는다(QA 패턴, departments/06-ai-qa-audit/scripts.py 동일)."""
    checks = out.get("check_results", [])
    compliance = out.get("compliance")

    lines = [
        "# 리스크본부 — Case 심사 보고서 (결정론적 생성, LLM 자유 서술 아님)",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| **risk_request_id** | `{out['risk_request_id']}` |",
        f"| **판정 (verdict)** | **{out['verdict']}** |",
        f"| **승인 수량** | {out['approved_quantity']} |",
        f"| **판정 엔진** | departments/03-risk/engine/risk_engine.py (`{out['calculation_version']}`) |",
        f"| **input_hash** | `{out['input_hash']}` (같은 OrderIntent·Context면 재현 가능) |",
        f"| **trading_state** | {md_cell(out['trading_state'])} |",
        (f"| **주문** | {md_cell(order_intent.get('side'))} {md_cell(order_intent.get('quantity'))} x "
         f"{md_cell(order_intent.get('instrument_id'))} (fund {md_cell(order_intent.get('fund_id'))}) |"),
        f"| **escalate** | {md_cell(out['escalate'])} |",
        f"| **생성** | {PIPELINE_VERSION}, {datetime.now(timezone.utc).isoformat()} |",
        "",
        "---",
        "",
        "## Pre-trade 검사 결과",
        "",
        "| Check | 통과 | 상세 |",
        "|---|---|---|",
    ]
    lines += [f"| {md_cell(c.get('name'))} | {md_cell(c.get('passed'))} | {md_cell(c.get('detail'))} |" for c in checks]
    if not checks:
        lines.append("| — | — | (check_results 없음) |")

    lines += ["", "## Counterparty / Broker 점검 (operational-counterparty-risk-agent)", ""]
    counterparty = out.get("counterparty")
    if counterparty:
        lines += [md_cell(counterparty.get("counterparty_narrative")),
                  f"(escalate: {md_cell(counterparty.get('escalate'))})"]
    else:
        lines.append("counterparty_health 미플래그 - 조건부 노드 미호출")

    lines += ["", "## Reason Codes", "",
              ", ".join(f"`{r}`" for r in out["reason_codes"]) if out["reason_codes"] else "없음",
              "", "## Compliance (compliance-policy-agent, Agentic RAG)", ""]
    if compliance:
        lines += ["| 필드 | 값 |", "|---|---|",
                  f"| grounded | {md_cell(compliance.get('grounded'))} |",
                  f"| attempts | {md_cell(compliance.get('attempts'))} |",
                  f"| answer | {json_cell(compliance.get('answer'))} |", ""]
        docs = compliance.get("relevant_documents") or []
        if docs:
            lines += ["| 참조 문서 | version | score |", "|---|---|---|"]
            lines += [f"| {md_cell(d.get('title'))} (`{md_cell(d.get('document_id'))}`) | "
                      f"{md_cell(d.get('version'))} | {md_cell(d.get('score'))} |" for d in docs]
    else:
        lines.append("REJECT 조기 종료 - compliance_check 생략됨")

    lines += [
        "", "## 종합 서술 (risk-supervisor, Hermes)", "",
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
        lines += [f"| {md_cell(item.get('stage'))} | {md_cell(item.get('error'))} | {md_cell(item.get('action'))} |"
                  for item in fallbacks]

    notion = out.get("notion_upload")
    if notion is not None:
        lines += ["", "## Notion 업로드 (Reporter Node)", ""]
        lines.append(f"업로드 성공: {notion['url']}" if notion.get("ok")
                     else f"업로드 생략/실패: {notion.get('reason') or notion.get('error')}")

    lines += [
        "", "---",
        "> 이 문서는 risk_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로",
        "> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.",
    ]
    return "\n".join(lines)


# ── 자체 점검 (Redis·OpenAI 없음) ──────────────────────────────────────────
def _check_graph_shape():
    p = build_pipeline()
    assert p is not None
    print("  그래프 컴파일            OK")


def _check_reject_short_circuit():
    # REJECT 면 compliance_check(비싼 Agentic RAG 루프) 를 부르지 않고 바로 종합하는지 -
    # supervise 는 REJECT 여도 여전히 불린다(감사용 서술은 필요) 라서 Hermes 콜도 같이 스텁한다.
    # notion_report 도 스텁한다 - 안 그러면 ai-office/.dev.vars 에 실제 토큰이 있을 때
    # 자체 점검(네트워크 없음이 원칙) 이 진짜 Notion에 네트워크 호출을 낸다.
    global check_trading_state, pre_trade_check, compliance_check, _hermes_chat, notion_report
    orig_ts, orig_pt, orig_cc, orig_chat, orig_nr = (
        check_trading_state, pre_trade_check, compliance_check, _hermes_chat, notion_report)
    check_trading_state = lambda s: {"trading_state": "HALTED"}
    pre_trade_check = lambda s: {"assessment": {
        "risk_request_id": "r1", "verdict": "reject", "approved_quantity": None,
        "reason_codes": ["trading_state_blocked"], "check_results": []}}
    compliance_check = lambda s: (_ for _ in ()).throw(AssertionError("REJECT인데 compliance_check 가 불렸다"))
    _hermes_chat = lambda persona, task: '{"narrative": "차단됨", "escalate": true, "cited_checks": []}'
    notion_report = lambda s: {"notion_upload": {"ok": False, "reason": "self-check stub"}}
    try:
        out = build_pipeline().invoke({"order_intent": {}, "context": {}})
        assert out["verdict"] == "reject"
        assert "compliance" not in out
    finally:
        check_trading_state, pre_trade_check, compliance_check, _hermes_chat, notion_report = (
            orig_ts, orig_pt, orig_cc, orig_chat, orig_nr)
    print("  REJECT 조기 종료          OK")


def _check_supervisor_guard():
    a = {"verdict": "approve", "reason_codes": [], "check_results": []}
    bad_chat = lambda persona, task: '{"narrative": "n"}'  # escalate/cited_checks 누락
    try:
        supervise({"order_intent": {}, "trading_state": "ENABLED", "assessment": a}, chat=bad_chat)
        raise AssertionError("불완전 종합 결과가 통과했다")
    except ValueError:
        pass
    print("  Supervisor 스키마 가드    OK")


def _check_counterparty_routing():
    # counterparty_health가 실측으로 플래그된 케이스(DEGRADED 통과, DOWN 거부) 는 verdict 와
    # 무관하게 counterparty_check 로 먼저 가고, 플래그가 없으면 기존 REJECT short-circuit 그대로다
    degraded = {"assessment": {"verdict": "approve",
                "check_results": [{"name": "counterparty_health", "passed": True, "detail": "DEGRADED 상태 - 통과하되 기록"}],
                "reason_codes": []}}
    assert _route_after_pretrade(degraded) == "counterparty"
    assert _route_after_counterparty(degraded) == "compliance"

    down_reject = {"assessment": {"verdict": "reject",
                   "check_results": [{"name": "counterparty_health", "passed": False, "detail": "paper 브로커 상태가 DOWN입니다"}],
                   "reason_codes": ["counterparty_unhealthy"]}}
    assert _route_after_pretrade(down_reject) == "counterparty"
    assert _route_after_counterparty(down_reject) == "supervise"

    healthy = {"assessment": {"verdict": "approve",
               "check_results": [{"name": "counterparty_health", "passed": True, "detail": ""}], "reason_codes": []}}
    assert _route_after_pretrade(healthy) == "compliance"

    other_reject = {"assessment": {"verdict": "reject", "check_results": [], "reason_codes": ["turnover_limit"]}}
    assert _route_after_pretrade(other_reject) == "supervise"
    print("  Counterparty 조건부 라우팅  OK")


def _check_counterparty_schema_guard():
    a = {"verdict": "reject", "check_results": [{"name": "counterparty_health", "passed": False, "detail": "d"}],
         "reason_codes": ["counterparty_unhealthy"]}
    bad_chat = lambda persona, task: '{"counterparty_narrative": "n"}'  # escalate 누락
    try:
        counterparty_check({"order_intent": {}, "assessment": a}, chat=bad_chat)
        raise AssertionError("불완전 Counterparty 결과가 통과했다")
    except ValueError:
        pass
    print("  Counterparty 스키마 가드    OK")


def _check_counterparty_full_invoke():
    # DEGRADED(비거부) 케이스로 전체 그래프를 실제로 돌려 conditional edge 매핑이 오타 없이
    # counterparty_check -> compliance_check -> supervise -> notion_report 순으로 이어지는지 확인한다.
    # notion_report 도 called 리스트에 남기되 실제 업로드는 스텁한다(네트워크 없음 원칙).
    global check_trading_state, pre_trade_check, counterparty_check, compliance_check, _hermes_chat, notion_report
    orig = (check_trading_state, pre_trade_check, counterparty_check, compliance_check, _hermes_chat, notion_report)
    called = []
    check_trading_state = lambda s: {"trading_state": "ENABLED"}
    pre_trade_check = lambda s: {"assessment": {
        "risk_request_id": "r1", "verdict": "approve", "approved_quantity": "1",
        "reason_codes": [], "calculation_version": "v", "input_hash": "h",
        "check_results": [{"name": "counterparty_health", "passed": True, "detail": "DEGRADED 상태 - 통과하되 기록"}]}}
    counterparty_check = lambda s: called.append("counterparty") or {"counterparty": {"counterparty_narrative": "d", "escalate": False}}
    compliance_check = lambda s: called.append("compliance") or {"compliance": None}
    _hermes_chat = lambda persona, task: '{"narrative": "n", "escalate": false, "cited_checks": []}'
    notion_report = lambda s: called.append("notion") or {"notion_upload": {"ok": False, "reason": "self-check stub"}}
    try:
        out = build_pipeline().invoke({"order_intent": {}, "context": {}})
        assert called == ["counterparty", "compliance", "notion"], called
        assert out["counterparty"]["counterparty_narrative"] == "d"
        assert out["notion_upload"] == {"ok": False, "reason": "self-check stub"}
    finally:
        (check_trading_state, pre_trade_check, counterparty_check, compliance_check,
         _hermes_chat, notion_report) = orig
    print("  Counterparty 조건부 호출    OK")


def _check_notion_report_node():
    captured = {}

    def stub_uploader(order_intent, context, out, *, report_md=""):
        captured["out"], captured["report_md"] = out, report_md
        return {"ok": True, "url": "https://notion.so/fake"}

    state = {"order_intent": {"trade_case_id": "t1"}, "context": {},
             "trading_state": "ENABLED", "verdict": "approve", "narrative": "n", "escalate": False,
             "assessment": {"risk_request_id": "r1", "verdict": "approve", "approved_quantity": "1",
                            "reason_codes": [], "check_results": [], "calculation_version": "v",
                            "input_hash": "h"}}
    result = notion_report(state, uploader=stub_uploader)
    assert result["notion_upload"] == {"ok": True, "url": "https://notion.so/fake"}
    assert result["report_markdown"]
    assert captured["out"]["risk_request_id"] == "r1"
    assert "risk_request_id" in captured["report_md"]  # _render_report_md 가 실제로 불렸는지
    print("  Notion Reporter 노드        OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        from datetime import timedelta
        from uuid import uuid4

        now = datetime.now(timezone.utc)
        fund, book, strategy, aapl = (str(uuid4()) for _ in range(4))
        demo_intent = {
            "trade_case_id": str(uuid4()), "fund_id": fund, "book_id": book,
            "strategy_id": strategy, "instrument_id": aapl, "side": "BUY",
            "order_type": "LIMIT", "quantity": "100", "limit_price": "70000",
            "time_in_force": "DAY", "valid_until": (now + timedelta(hours=1)).isoformat(),
            "snapshot": {"market_snapshot_id": "s1", "as_of": now.isoformat(),
                        "bid": "69900", "ask": "70000"},
            "idempotency_key": "idem_scripts_001", "created_by": "trader-pm-agent", "trace_id": "t1",
        }
        demo_context = {
            "mandate": {"fund_id": fund, "allowed_instrument_ids": None,
                       "min_order_notional": "100000", "max_order_notional": "50000000"},
            "limits": {"soft_single_issuer_pct": "0.20", "hard_single_issuer_pct": "0.30",
                      "max_daily_turnover_notional": "100000000", "max_daily_order_count": 50,
                      "max_daily_loss": "10000000", "max_drawdown_pct": "0.20"},
            "restricted_items": [],
            "portfolio": {"fund_id": fund, "cash": "100000000", "buying_power": "100000000",
                         "gross_exposure": "100000000", "peak_equity": "1000000000", "equity": "1000000000"},
            "market_status": {"tradable": "--reject" not in sys.argv},
            "counterparty": {"broker_adapter": "paper", "health": "ok"},
            "trading_state": "ENABLED", "as_of": now.isoformat(),
        }
        print(f"{PIPELINE_VERSION} 실행 (데모 주문{' - REJECT 유도' if '--reject' in sys.argv else ''})")
        log_path = Path(os.environ.get(
            "RISK_RUN_LOG_PATH",
            _BASE / "reports" / "runs" / f"risk_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl",
        ))
        if "--log-path" in sys.argv:
            log_index = sys.argv.index("--log-path") + 1
            if log_index >= len(sys.argv):
                raise SystemExit("--log-path requires a file path")
            log_path = Path(sys.argv[log_index])
        out = run_risk_department(demo_intent, demo_context, scope=f"fund:{fund}", log_path=log_path)
        print(json.dumps(out, ensure_ascii=False, indent=1))

        report_dir = _BASE / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"risk_case_report_{out['risk_request_id']}.md"
        report_path.write_text(_render_report_md(demo_intent, demo_context, out), encoding="utf-8")
        print(f"결정론적 MD 리포트 저장: {report_path}")
        raise SystemExit(0)

    print(f"{PIPELINE_VERSION} 자체 점검 (Redis·Hermes 없음)")
    _check_graph_shape()
    _check_reject_short_circuit()
    _check_supervisor_guard()
    _check_counterparty_routing()
    _check_counterparty_schema_guard()
    _check_counterparty_full_invoke()
    _check_notion_report_node()
    print("본부 파이프라인 7개 영역 통과. 실행은 --run (REDIS_URL, OPENAI_API_KEY, Hermes 필요)")
