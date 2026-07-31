#!/usr/bin/env python3
"""리스크본부 LangGraph 파이프라인 - run_risk_department(order_intent, context) -> Case Decision.

담당: 동규 (리스크/QA)
근거: QA 부서 패턴(departments/06-ai-qa-audit/scripts.py)을 재일님이 리서치본부에
      적용한 개선판(departments/01-research/scripts.py, 2026-07-31)을 리스크본부에 적용.

QA 원안과의 차이 - **노드가 프롬프트가 아니라 config.yaml의 실제 페르소나 + 이미 구현된
결정론적 서비스다** (research 패턴을 그대로 따름):
  check_trading_state  market-liquidity-risk-agent (결정론 - Redis 최신 Trading State)
  pre_trade_check      pre-trade-risk-analyst (결정론 - RiskEngine.check_order, P0 Gate)
  compliance_check     compliance-policy-agent (skills/agentic-rag 기존 baseline 재사용)
  supervise            risk-supervisor 페르소나 (hermes/config.yaml 원문 사용, 종합·서술만)

derivatives-margin-risk-agent/operational-counterparty-risk-agent는 뺐다 - config.yaml의
not_started 항목대로 Greeks·counterparty aggregation 전용 API가 아직 없다(연구본부가
microstructure/technical/fundamental/sector-regime 4개 페르소나를 같은 이유로 뺀 것과 동일).

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
    set -a && source .env && set +a  # REDIS_URL, OPENAI_API_KEY 등을 셸 환경변수로 로드
    source ~/claude/bin/activate      # run_agent(Hermes Runtime)가 이 venv에만 설치돼 있다 - 프로젝트 .venv엔 없음
    cd departments/03-risk
    python scripts.py --run           # 데모 주문으로 실제 실행

  reports/risk_case_report_<risk_request_id>.md 로 결정론적 MD 리포트도 저장한다 - _render_report_md 는
  순수 함수(run_risk_department 반환값을 그대로 옮김) 이고 LLM은 narrative/compliance answer 필드에만
  관여한다 (QA 패턴과 동일).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TypedDict

_BASE = Path(__file__).resolve().parent
_ENGINE_DIR = _BASE / "engine"
_API_DIR = _BASE / "api"
_CONTRACTS_DIR = _BASE.parent / "02-trading" / "contracts"
_AGENTIC_RAG_DIR = _BASE.parent.parent / "skills" / "agentic-rag"
for _p in (_ENGINE_DIR, _API_DIR, _CONTRACTS_DIR, _AGENTIC_RAG_DIR):
    sys.path.insert(0, str(_p))

from langgraph.graph import END, StateGraph  # noqa: E402

PIPELINE_VERSION = "risk-department-pipeline-v1"


class RiskState(TypedDict, total=False):
    order_intent: dict     # OrderIntent JSON
    context: dict           # RiskContextIn JSON (trading_state 필드는 아래서 실측으로 덮어씀)
    scope: str               # trading_state_store 조회 scope (예: "fund:<uuid>")
    trading_state: str      # market-liquidity-risk-agent 결과
    assessment: dict        # pre-trade-risk-analyst 결과 (RiskAssessment)
    compliance: Optional[dict]  # compliance-policy-agent 결과 (REJECT면 생략)
    verdict: str             # 최종 case-level 값 - 항상 assessment 에서만 옴
    narrative: str
    escalate: bool


# ── 노드 1: Trading State 실측 (결정론 직원) ────────────────────────────────
def check_trading_state(state: RiskState) -> dict:
    import redis as redis_lib
    from trading_state_store import RedisTradingStateStore

    store = RedisTradingStateStore(redis_lib.Redis.from_url(os.environ["REDIS_URL"]))
    ts = store.get_state_fail_closed(state.get("scope", "fund:default"))
    return {"trading_state": ts.value}


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


# ── 노드 3: Compliance (skills/agentic-rag 기존 baseline 재사용) ───────────
def compliance_check(state: RiskState) -> dict:
    from src.graph import run_compliance_check

    oi = state["order_intent"]
    query = (f"Can we execute a {oi['side']} order for {oi['quantity']} units of "
             f"instrument {oi['instrument_id']} under fund {oi['fund_id']} today?")
    as_of_date = state["context"]["as_of"][:10]  # PIT 필터는 date-only(YYYY-MM-DD)를 요구한다
    return {"compliance": run_compliance_check(query, as_of_date, persona="compliance-policy-agent")}


# ── 노드 4: 종합 (risk-supervisor 페르소나 - Hermes AIAgent) ───────────────
def _supervisor_persona() -> str:
    cfg = (_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8")
    return re.search(r'risk-supervisor: "(.*?)"\n', cfg, re.S).group(1)


def _hermes_chat(persona: str, task: str) -> str:
    from run_agent import AIAgent  # Lazy Import - Hermes 없는 환경에서도 모듈 자체는 항상 import 가능해야 한다

    agent = AIAgent(model="poolside/laguna-s-2.1:free", quiet_mode=True,
                     ephemeral_system_prompt=persona)
    return agent.chat(task)


def supervise(state: RiskState, *, chat=None) -> dict:
    a = state["assessment"]
    bundle = {"order_intent": state["order_intent"], "trading_state": state["trading_state"],
              "assessment": a, "compliance": state.get("compliance")}
    task = f"""Using ONLY the evidence below, write a case-level risk narrative in Korean for
CEO/Audit review. The binding verdict is "{a['verdict']}" from the deterministic Risk Engine -
you cannot change it, only cite and explain it.
Schema (JSON only):
{{"narrative": "2-4 sentences, cite check names and the compliance result",
 "escalate": true or false, "cited_checks": ["check names referenced"]}}

Evidence:
{json.dumps(bundle, ensure_ascii=False, indent=1)}"""

    call = chat or _hermes_chat
    out = call(_supervisor_persona(), task)
    s, e = out.find("{"), out.rfind("}")
    note = json.loads(out[s:e + 1])
    for k in ("narrative", "escalate", "cited_checks"):
        if k not in note:
            raise ValueError(f"Supervisor 종합 결과에 {k} 가 없다 - 초안 거부")
    # 바인딩 verdict 는 LLM 출력이 아니라 assessment 에서 그대로 가져온다
    return {"verdict": a["verdict"], "narrative": note["narrative"], "escalate": note["escalate"]}


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def build_pipeline():
    g = StateGraph(RiskState)
    g.add_node("check_trading_state", check_trading_state)
    g.add_node("pre_trade_check", pre_trade_check)
    g.add_node("compliance_check", compliance_check)
    g.add_node("supervise", supervise)
    g.set_entry_point("check_trading_state")
    g.add_edge("check_trading_state", "pre_trade_check")
    # REJECT 면 정책 검색(Agentic RAG)을 태우지 않고 바로 종합한다 - 이미 막힌 주문이다
    g.add_conditional_edges("pre_trade_check",
                            lambda s: "skip" if s["assessment"]["verdict"] == "reject" else "go",
                            {"skip": "supervise", "go": "compliance_check"})
    g.add_edge("compliance_check", "supervise")
    g.add_edge("supervise", END)
    return g.compile()


def run_risk_department(order_intent: dict, context: dict, scope: str = "fund:default") -> dict:
    """본부 단독 실행 - QA/연구본부의 run_<dept>_department 와 같은 외부 인터페이스."""
    out = build_pipeline().invoke({"order_intent": order_intent, "context": context, "scope": scope})
    a = out["assessment"]
    return {"risk_request_id": a["risk_request_id"], "verdict": out["verdict"],
            "approved_quantity": a["approved_quantity"], "reason_codes": a["reason_codes"],
            "check_results": a["check_results"], "calculation_version": a["calculation_version"],
            "input_hash": a["input_hash"], "trading_state": out["trading_state"],
            "compliance": out.get("compliance"), "narrative": out["narrative"], "escalate": out["escalate"]}


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
        f"| **trading_state** | {out['trading_state']} |",
        f"| **주문** | {order_intent.get('side')} {order_intent.get('quantity')} x "
        f"{order_intent.get('instrument_id')} (fund {order_intent.get('fund_id')}) |",
        f"| **escalate** | {out['escalate']} |",
        f"| **생성** | {PIPELINE_VERSION}, {datetime.now(timezone.utc).isoformat()} |",
        "",
        "---",
        "",
        "## Pre-trade 검사 결과",
        "",
        "| Check | 통과 | 상세 |",
        "|---|---|---|",
    ]
    lines += [f"| {c['name']} | {c['passed']} | {c['detail']} |" for c in checks]
    if not checks:
        lines.append("| — | — | (check_results 없음) |")

    lines += ["", "## Reason Codes", "",
              ", ".join(f"`{r}`" for r in out["reason_codes"]) if out["reason_codes"] else "없음",
              "", "## Compliance (compliance-policy-agent, Agentic RAG)", ""]
    if compliance:
        lines += ["| 필드 | 값 |", "|---|---|",
                  f"| grounded | {compliance.get('grounded')} |",
                  f"| attempts | {compliance.get('attempts')} |",
                  f"| answer | {compliance.get('answer')} |", ""]
        docs = compliance.get("relevant_documents") or []
        if docs:
            lines += ["| 참조 문서 | version | score |", "|---|---|---|"]
            lines += [f"| {d['title']} (`{d['document_id']}`) | {d['version']} | {d['score']} |" for d in docs]
    else:
        lines.append("REJECT 조기 종료 - compliance_check 생략됨")

    lines += [
        "", "## 종합 서술 (risk-supervisor, Hermes)", "",
        out["narrative"],
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
    # supervise 는 REJECT 여도 여전히 불린다(감사용 서술은 필요) 라서 Hermes 콜도 같이 스텁한다
    global check_trading_state, pre_trade_check, compliance_check, _hermes_chat
    orig_ts, orig_pt, orig_cc, orig_chat = check_trading_state, pre_trade_check, compliance_check, _hermes_chat
    check_trading_state = lambda s: {"trading_state": "HALTED"}
    pre_trade_check = lambda s: {"assessment": {
        "risk_request_id": "r1", "verdict": "reject", "approved_quantity": None,
        "reason_codes": ["trading_state_blocked"], "check_results": []}}
    compliance_check = lambda s: (_ for _ in ()).throw(AssertionError("REJECT인데 compliance_check 가 불렸다"))
    _hermes_chat = lambda persona, task: '{"narrative": "차단됨", "escalate": true, "cited_checks": []}'
    try:
        out = build_pipeline().invoke({"order_intent": {}, "context": {}})
        assert out["verdict"] == "reject"
        assert "compliance" not in out
    finally:
        check_trading_state, pre_trade_check, compliance_check, _hermes_chat = orig_ts, orig_pt, orig_cc, orig_chat
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
            "market_status": {"tradable": True},
            "counterparty": {"broker_adapter": "paper", "health": "ok"},
            "trading_state": "ENABLED", "as_of": now.isoformat(),
        }
        print(f"{PIPELINE_VERSION} 실행 (데모 주문)")
        out = run_risk_department(demo_intent, demo_context, scope=f"fund:{fund}")
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
    print("본부 파이프라인 3개 영역 통과. 실행은 --run (REDIS_URL, OPENAI_API_KEY, Hermes 필요)")
