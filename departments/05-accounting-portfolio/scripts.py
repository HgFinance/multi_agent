#!/usr/bin/env python3
"""회계/포트폴리오본부 LangGraph 파이프라인 - run_accounting_close(...) -> Close Bundle.

담당: 도현 (회계/포트폴리오본부) — 트레이딩본부는 별도 스크립트를 가진다. 이 파일에
      OrderIntent·OMS·Broker 를 끌어오지 않는다(부서 경계, CLAUDE.md 권한 분리).
형식 근거: 리스크본부(departments/03-risk/scripts.py)·QA본부의 파이프라인 형식 —
      결정론 노드 + 서술만 하는 LLM, _guard_node fail-closed 경계, 순수 함수 MD 리포트,
      input_hash 재현성, 자체 점검, Notion Reporter 노드.
내용 근거: config.yaml portfolio-control-supervisor 페르소나가 규정한 마감 순서
      "Reconciliation, Valuation, Accrual, PnL, then NAV close" (마스터플랜 12.4).
      F15(Portfolio와 PnL) 완료 조건 3개를 노드로 강제한다.

**이 본부는 다른 본부와 다르다 — 결정론 엔진이 이미 다 있다.**
리스크·트레이딩은 판정을 새로 만들어야 했지만 여기는 ledger/portfolio/reconciliation/
daily_report 가 이미 숫자를 만든다. 그래서 이 파이프라인은 숫자를 **만들지 않고**
정해진 순서로 엔진을 부르고, 어긋나면 멈추고, LLM 은 그 결과를 서술만 한다.

  validate_inputs   결정론 - Fund/Book 일치, as_of, 필수 입력
  reconcile         ACC-02 결정론 - reconcile_fills / positions / cash
  narrate_breaks    ACC-02 LLM (조건부 - Break 가 있을 때만). 심각도는 못 바꾼다
  verify_projection ACC-01 결정론 - rebuild 재현성 + 저장 projection 대조 + 차대 균형
  value             ACC-04 결정론 - value_portfolio (Mark 없거나 낡으면 NAV 자체를 안 만든다)
  daily_report      결정론 - build_daily_report, NAV 항등식 검산
  supervise         ACC-00 LLM - 마감 서술만. 수치를 다시 쓰지 않는다
  notion_report     Reporter (결정론) - Notion Accounting DB Projection

**NAV 는 절대 OFFICIAL 로 나가지 않는다.** config.yaml fund-accounting-agent 페르소나:
"Preliminary and Official NAV are different things — never present the former as the
latter, and Official NAV requires independent approval you do not hold."
반환값의 nav_status 는 PRELIMINARY 또는 BLOCKED 뿐이고 is_official 은 항상 False 다.
트레이딩의 produces_order_intent=False 와 같은 자리의 계약이다.

우리 본부라서 있는 것:
  - **Material Break -> 평가·보고 차단.** 브로커와 내부 포지션이 어긋난 상태로 NAV 를
    계산하면 "틀린 줄 아는 숫자"가 NAV 모양으로 나온다. 마스터플랜 11.2 가 이 상황을
    Kill Switch 대상으로 규정한다(reconcile_positions docstring). 개발 원칙 9번.
  - **projection vs rebuild 대조.** config.yaml 백로그 1번 "어긋난 채로 하루가 지나면
    NAV 가 틀린 값으로 확정된다"를 마감 절차에 넣었다. F15 완료 조건 1·3.
  - **Mark 신선도.** value_portfolio 가 ValuationError 를 던지면 NAV 를 만들지 않는다.
    D3 가 market-api 종가 대기 중이라 지금은 이 경로가 자주 탄다 - 그게 정상이다.
  - **NAV 항등식 검산.** nav == cash + securities_value. F15 완료 조건 2.

원칙 (CLAUDE.md / 팀 가이드):
  - 회계 수치를 LLM 문장에서 추출해 확정하지 않는다. LLM 노드 2개는 서술만 만들고
    nav/pnl/break severity 중 어느 것도 바꾸지 못한다.
  - Posted Journal 은 수정하지 않는다 - 이 파이프라인에 post/reverse 경로가 없다.
    읽기만 한다.
  - 실패는 통과가 아니다. 어느 노드가 죽어도 nav_status 는 BLOCKED 로 떨어지고
    escalate=True 가 된다.
  - run_agent(Hermes) import 는 _hermes_chat 안에서 한다(Lazy Import).

실행:
  python departments/05-accounting-portfolio/scripts.py          # 자체 점검 (Hermes·네트워크 없이)
  python departments/05-accounting-portfolio/scripts.py --run    # 실제 Hermes + Notion

  --run 은 .env 로드와 Hermes 경로가 필요하다:
    set -a && source .env && set +a
    PYTHONPATH=<hermes-agent 루트> python departments/05-accounting-portfolio/scripts.py --run
"""
from __future__ import annotations

import hashlib
import json
import operator
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, TypedDict
from uuid import UUID, uuid4

_BASE = Path(__file__).resolve().parent
_REPO_ROOT = _BASE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
for _sub in ("ledger", "portfolio", "reconciliation", "reporting", "corporate_actions"):
    _p = str(_BASE / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from daily_report import DailyReport, ReportError, build_daily_report
from langgraph.graph import END, StateGraph
from langsmith import tracing_context
from ledger import Ledger
from portfolio import (
    MarkPrice,
    PortfolioSnapshot,
    ValuationError,
    value_portfolio,
)
from reconciliation import (
    Break,
    ReconResult,
    reconcile_cash,
    reconcile_fills,
    reconcile_positions,
)

PIPELINE_VERSION = "accounting-close-pipeline-v1"

# NAV 는 이 파이프라인에서 절대 OFFICIAL 이 되지 않는다 (모듈 상단 참고).
NAV_PRELIMINARY = "PRELIMINARY"
NAV_BLOCKED = "BLOCKED"


class CloseState(TypedDict, total=False):
    # 입력 (호출자가 준다 - 이 파이프라인은 원장에 쓰지 않는다)
    ledger: Any                      # Ledger - 읽기 전용으로만 쓴다
    marks: dict                      # {instrument_id: MarkPrice} - market-api 산출물
    as_of: Any                       # datetime
    accounting_date: Any             # date
    external: dict                   # 브로커 명세서 {fills, positions, cash}
    opening_snapshot: Any            # PortfolioSnapshot | None
    stored_projection: dict | None   # {"positions": {...}, "cash": Decimal} - DB Read Model
    strategy_of: dict | None

    # 산출
    close_id: str
    input_hash: str
    trace_id: str
    fund_id: str
    book_id: str
    recon: dict                      # 대사 결과 요약
    breaks: list                     # Break 목록 (dict 화)
    break_narrative: str | None      # ACC-02 LLM (서술만)
    projection_check: dict           # rebuild 재현성 + 저장본 대조 + 차대 균형
    snapshot: dict | None            # 평가 결과 (금액은 문자열)
    # PortfolioSnapshot 원본. daily_report 가 dict 가 아니라 객체를 받는다.
    # **State 에 선언하지 않으면 LangGraph 가 조용히 버린다** - 선언 안 했더니 report 가
    # 계속 null 로 나왔고 fallback 도 안 남아서 자체 점검을 그냥 통과했다(2026-08-03 실측).
    _snapshot_obj: Any
    nav_status: str                  # PRELIMINARY | BLOCKED
    nav_identity: dict               # NAV 항등식 검산
    report: dict | None              # DailyReport.to_dict()
    narrative: str
    escalate: bool
    notion_upload: dict
    report_markdown: str
    fallbacks: Annotated[list[dict], operator.add]


class ClosePipelineNodeError(RuntimeError):
    """실패한 LangGraph 노드를 예외에 붙인다 - 원인은 그대로 둔다."""

    def __init__(self, node: str, cause: Exception) -> None:
        self.failed_node = node
        self.cause = cause
        super().__init__(str(cause))


def _guard_node(node: str, handler):
    def guarded(state: CloseState) -> dict:
        try:
            return handler(state)
        except ClosePipelineNodeError:
            raise
        except Exception as exc:
            raise ClosePipelineNodeError(node, exc) from exc

    guarded.__name__ = f"{node}_guarded"
    return guarded


def _sanitize(exc: Exception) -> str:
    """진단은 남기되 자격증명이 fallback 기록으로 새지 않게 한다 - 이 메시지는 MD 리포트와
    Notion 까지 흘러간다."""
    message = " ".join(str(exc).split()) or "no_exception_message"
    message = re.sub(r"(?i)\b(?:rediss?|postgres(?:ql)?|https?)://[^\s]+", "[REDACTED]", message)
    message = re.sub(r"\b(?:sk|ntn|lsv2|ghp|xox[baprs])_[A-Za-z0-9._-]+\b", "[REDACTED]", message)
    return message[:240]


def _fallback(stage: str, exc: Exception) -> dict:
    # safe_action 은 회계 마감이 실패했을 때의 안전 방향이다. 승인·확정 방향으로 가지 않는다.
    return {"stage": stage, "error": type(exc).__name__, "error_message": _sanitize(exc),
            "safe_action": "NAV_NOT_CONFIRMED", "decision_origin": "FALLBACK"}


def _d(value) -> str | None:
    """Decimal -> 문자열. JSON number 는 IEEE754 double 이라 Decimal 이 깨진다
    (daily_report.to_dict / ui_read_model 과 같은 규칙)."""
    return None if value is None else str(value)


_CONFIG: dict | None = None


def _config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        import yaml

        _CONFIG = yaml.safe_load((_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8"))
    return _CONFIG


def _model_version() -> str:
    return str((_config().get("model") or {}).get("default", "unknown"))


def _prompt_version(persona_name: str) -> str:
    """페르소나 본문 해시 - 프롬프트를 한 글자만 고쳐도 값이 바뀐다."""
    return f"{persona_name}@{hashlib.sha256(_persona(persona_name).encode()).hexdigest()[:12]}"


# ── LangSmith 관측성 ──────────────────────────────────────────────────────
# 기본은 꺼져 있다 (LANGSMITH_TRACING=false). 켜면 이 그래프의 노드·LLM 호출이
# 외부(LangSmith)로 나간다 - 회계 Trace 에는 NAV·Position·현금이 그대로 담기므로
# 금융 데이터 외부 전송 정책 검토 전까지 로컬 개발에서만 켠다.
def _ls_project() -> str:
    """부서별 Project 로 격리한다. 한 Project 에 8개 부서를 섞으면 회계 Trace 를
    다른 본부가 그대로 열람하게 된다 - 부서 경계가 관측성에서만 무너진다."""
    return f"{os.environ.get('LANGSMITH_PROJECT') or 'hedgefund'}-05-accounting"


def _langsmith_handoff(trace_id: str) -> dict[str, Any]:
    """리포트·소비자에게 넘기는 것은 Trace 원문이 아니라 이 좌표뿐이다."""
    flag = os.environ.get("LANGCHAIN_TRACING_V2", os.environ.get("LANGSMITH_TRACING", ""))
    enabled = flag.casefold() in {"1", "true", "yes", "on"}
    return {
        "trace_id": str(trace_id),
        "langsmith": {
            "enabled": enabled,
            "project": _ls_project() if enabled else None,
            "run_id": os.environ.get("LANGSMITH_RUN_ID"),
            "handoff_status": "configured" if enabled else "not_configured",
        },
    }


def _max_staleness() -> timedelta:
    """Mark 신선도 한도. 튜닝 대상 숫자라 config.yaml 이 소유한다."""
    minutes = int((_config().get("config") or {}).get("mark_max_staleness_minutes", 30))
    return timedelta(minutes=minutes)


# ── 노드 1: 입력 검증 (결정론) ─────────────────────────────────────────────
def validate_inputs(state: CloseState) -> dict:
    ledger, as_of = state.get("ledger"), state.get("as_of")
    if ledger is None:
        raise ValueError("ledger 가 없다 - 마감할 원장이 있어야 한다")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        # naive 시각을 UTC 로 가정하면 KST 와 9시간 어긋나 회계일이 하루 밀린다.
        raise ValueError("as_of 는 timezone 이 있는 datetime 이어야 한다")
    if not isinstance(state.get("accounting_date"), date):
        raise TypeError("accounting_date 가 없다")

    # 분개 UUID 는 posting 마다 새로 나므로 해시에 넣지 않는다 - 넣으면 경제적으로 같은
    # 원장인데 해시가 매번 달라져 재현성 계약이 무의미해진다. 원천 이벤트 ID 와 시산표
    # (계정별 잔액)가 "이 마감이 무엇을 근거로 했는가"의 실체다.
    payload = {
        "fund_id": str(ledger.fund_id), "book_id": str(ledger.book_id),
        "accounting_date": state["accounting_date"].isoformat(),
        "sources": sorted(f"{j.event_type}:{j.source_event_id}" for j in ledger.journals),
        "trial_balance": {k: str(v) for k, v in sorted(ledger.trial_balance().items())},
        "marks": sorted(f"{k}:{v.price}@{v.as_of.isoformat()}"
                        for k, v in (state.get("marks") or {}).items()),
    }
    ihash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"input_hash": ihash, "close_id": f"cls-{ihash[:16]}",
            "trace_id": str(state.get("trace_id") or f"{PIPELINE_VERSION}:{ihash[:16]}"),
            "fund_id": str(ledger.fund_id), "book_id": str(ledger.book_id)}


# ── 노드 2: 대사 (ACC-02 결정론) ───────────────────────────────────────────
def _break_dict(b: Break) -> dict:
    return {"break_id": str(b.break_id), "kind": b.kind, "severity": str(b.severity),
            "detail": b.detail, "internal_ref": b.internal_ref,
            "external_ref": b.external_ref, "status": b.status, "escalates": b.escalates}


def _recon_summary(name: str, res: ReconResult) -> dict:
    return {"recon_type": res.recon_type, "rule_version": res.rule_version,
            "result": res.result, "item_count": len(res.items),
            "break_count": len(res.breaks), "material_count": len(res.material_breaks)}


def reconcile(state: CloseState) -> dict:
    """브로커 명세서와 내부 기록을 대사한다. 판정은 전부 reconciliation.py 가 한다."""
    ledger, as_of = state["ledger"], state["as_of"]
    ext = state.get("external") or {}
    positions, cash = ledger.rebuild()

    results: dict[str, dict] = {}
    breaks: list[Break] = []

    if ext.get("fills") is not None:
        res = reconcile_fills(list(ext.get("internal_fills") or []),
                              list(ext["fills"]), as_of=as_of)
        results["fill"] = _recon_summary("fill", res)
        breaks += res.breaks
    if ext.get("positions") is not None:
        res = reconcile_positions({k: v.quantity for k, v in positions.items()},
                                  dict(ext["positions"]), as_of=as_of)
        results["position"] = _recon_summary("position", res)
        breaks += res.breaks
    if ext.get("cash") is not None:
        res = reconcile_cash(cash, Decimal(str(ext["cash"])), as_of=as_of)
        results["cash"] = _recon_summary("cash", res)
        breaks += res.breaks

    if not results:
        # 브로커 명세서가 없으면 대사를 "통과"로 기록하지 않는다 - 안 한 것이다.
        results["_none"] = {"result": "not_reconciled",
                            "detail": "브로커 명세서가 없어 대사를 수행하지 않았다"}
    return {"recon": results, "breaks": [_break_dict(b) for b in breaks]}


# ── 노드 3: Break 서술 (ACC-02 LLM - 조건부, 서술만) ───────────────────────
def _persona(name: str) -> str:
    cfg = (_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8")
    m = re.search(rf'{re.escape(name)}: "(.*?)"\n', cfg, re.DOTALL)
    if not m:
        raise ValueError(f"{name} 페르소나를 config.yaml 에서 찾을 수 없다")
    return m.group(1)


def _hermes_chat(persona: str, task: str) -> str:
    from run_agent import (
        AIAgent,  # Lazy Import - Hermes 없는 환경에서도 모듈 import 는 항상 되어야 한다
    )

    # enabled_toolsets=[] 로 도구를 0 개로 만든다. 회계 직원이 파일을 쓰거나 셸을 실행할
    # 이유가 없고, 트레이딩본부에서 이걸 안 막았더니 분석가가 남의 본부 디렉터리에
    # 파일을 만든 실측 사고가 있었다(2026-08-03).
    agent = AIAgent(model=_model_version(), quiet_mode=True,
                    ephemeral_system_prompt=persona, enabled_toolsets=[])
    return agent.chat(task)


def _parse_json_block(out: str, required: tuple[str, ...], who: str) -> dict:
    s, e = out.find("{"), out.rfind("}")
    if s < 0 or e <= s:
        raise ValueError(f"{who} 응답에 JSON 이 없다 - 초안 거부")
    note = json.loads(out[s:e + 1])
    for k in required:
        if k not in note:
            raise ValueError(f"{who} 결과에 {k} 가 없다 - 초안 거부")
    return note


def narrate_breaks(state: CloseState, *, chat=None) -> dict:
    """Break 를 사람이 읽을 문장으로. **심각도와 건수는 못 바꾼다** - 이미 확정된 값이다."""
    breaks = state.get("breaks") or []
    if not breaks:
        return {}
    task = f"""Using ONLY the reconciliation breaks below, write a Korean narrative for the
Portfolio Control close pack. You cannot change any severity or count — they are already
determined by deterministic code. Describe what each break means operationally and who owns it.
Never confirm a fuzzy candidate and never state a position or cash figure you computed yourself.
Schema (JSON only):
{{"break_narrative": "2-4 sentences in Korean",
 "owner_hint": ["who should resolve each break"]}}

Breaks:
{json.dumps(breaks, ensure_ascii=False, indent=1)}"""
    try:
        call = chat or _hermes_chat
        note = _parse_json_block(call(_persona("reconciliation-agent"), task),
                                 ("break_narrative", "owner_hint"), "Reconciliation")
        return {"break_narrative": note["break_narrative"]}
    except Exception as exc:  # noqa: BLE001 - intentional fallback boundary
        # 서술 실패가 대사 결과를 지우지 않는다. Break 는 그대로 남는다.
        return {"break_narrative": None, "fallbacks": [_fallback("narrate_breaks", exc)]}


# ── 노드 4: Projection 대조 (ACC-01 결정론) ────────────────────────────────
def verify_projection(state: CloseState) -> dict:
    """F15 완료 조건 1·3 + config.yaml 백로그 1번.

    분개에서 두 번 재구축해 같은 값이 나오는지(Online == Replay), 그리고 저장된
    projection 과 어긋나지 않는지 본다. 어긋나면 분개가 사실이고 차이는 Break 다
    (portfolio-controller 페르소나 원문).
    """
    ledger = state["ledger"]
    first_pos, first_cash = ledger.rebuild()
    second_pos, second_cash = ledger.rebuild()

    deterministic = (first_cash == second_cash and
                     {k: v.quantity for k, v in first_pos.items()} ==
                     {k: v.quantity for k, v in second_pos.items()})

    tb = ledger.trial_balance()
    balanced = sum(tb.values()) == Decimal(0)

    drift: list[dict] = []
    stored = state.get("stored_projection")
    if stored:
        for iid, pos in first_pos.items():
            want = stored.get("positions", {}).get(iid)
            if want is not None and Decimal(str(want)) != pos.quantity:
                drift.append({"instrument_id": str(iid), "rebuilt": str(pos.quantity),
                              "stored": str(want)})
        if stored.get("cash") is not None and Decimal(str(stored["cash"])) != first_cash:
            drift.append({"instrument_id": "CASH", "rebuilt": str(first_cash),
                          "stored": str(stored["cash"])})

    return {"projection_check": {
        "rebuild_deterministic": deterministic,
        "trial_balance_balanced": balanced,
        "trial_balance_sum": _d(sum(tb.values())),
        "stored_projection_compared": bool(stored),
        "drift": drift,
        "position_count": len(first_pos),
        "cash": _d(first_cash),
    }}


# ── 노드 5: 평가 (ACC-04 결정론) ───────────────────────────────────────────
def _snapshot_dict(s: PortfolioSnapshot) -> dict:
    return {"as_of": s.as_of.isoformat(), "fund_id": str(s.fund_id), "book_id": str(s.book_id),
            "cash": _d(s.cash), "securities_value": _d(s.securities_value),
            "unrealized_pnl": _d(s.unrealized_pnl), "nav": _d(s.nav),
            "gross_exposure": _d(s.gross_exposure), "net_exposure": _d(s.net_exposure),
            "position_count": len(s.positions)}


def value(state: CloseState) -> dict:
    """value_portfolio 는 Mark 가 하나라도 없거나 낡으면 ValuationError 를 던진다.
    그때 NAV 를 만들지 않는다 - 일부만 평가한 NAV 는 틀린 NAV 다(portfolio.py docstring)."""
    try:
        snap = value_portfolio(state["ledger"], state.get("marks") or {},
                               state["as_of"], _max_staleness())
    except ValuationError as exc:
        return {"snapshot": None, "nav_status": NAV_BLOCKED,
                "nav_identity": {"checked": False, "reason": "평가 불가로 NAV 미산출"},
                "fallbacks": [_fallback("value", exc)]}

    # F15 완료 조건 2: "Cash와 Position Value가 Portfolio NAV와 일치한다"
    identity_ok = snap.nav == snap.cash + snap.securities_value
    return {"snapshot": _snapshot_dict(snap), "_snapshot_obj": snap,
            "nav_status": NAV_PRELIMINARY if identity_ok else NAV_BLOCKED,
            "nav_identity": {"checked": True, "ok": identity_ok,
                             "nav": _d(snap.nav), "cash": _d(snap.cash),
                             "securities_value": _d(snap.securities_value),
                             "difference": _d(snap.nav - snap.cash - snap.securities_value)}}


# ── 노드 6: 일일 보고 (결정론) ─────────────────────────────────────────────
def daily_report_node(state: CloseState) -> dict:
    snap = state.get("_snapshot_obj")
    opening = state.get("opening_snapshot")
    if snap is None or opening is None:
        # 기초 스냅샷이 없으면 Drawdown·기간 손익을 만들 수 없다. 지어내지 않는다.
        return {"report": None}
    try:
        rep: DailyReport = build_daily_report(
            snapshots=[opening, snap], ledger=state["ledger"],
            accounting_date=state["accounting_date"],
            breaks=[], strategy_of=state.get("strategy_of"))
    except ReportError as exc:
        return {"report": None, "fallbacks": [_fallback("daily_report", exc)]}
    return {"report": rep.to_dict()}


# ── 노드 7: 마감 종합 (ACC-00 LLM - 서술만) ────────────────────────────────
def _deterministic_close_note(state: CloseState) -> dict:
    """LLM 없이도 마감 서술이 나온다. 차단된 마감에 LLM 비용을 더 쓰지 않는다."""
    status = state.get("nav_status", NAV_BLOCKED)
    materials = [b for b in (state.get("breaks") or []) if b.get("escalates")]
    reasons = []
    if materials:
        reasons.append(f"Material Break {len(materials)}건")
    if not (state.get("projection_check") or {}).get("trial_balance_balanced", True):
        reasons.append("차대 불균형")
    if (state.get("projection_check") or {}).get("drift"):
        reasons.append("저장 projection 과 분개 재구축 불일치")
    if state.get("snapshot") is None:
        reasons.append("평가 불가(Mark 없음/낡음)")
    detail = ", ".join(reasons) or "차단 사유 기록 없음"
    return {"narrative": (f"NAV 를 확정하지 않았다 ({status}). 사유: {detail}. "
                          "이 결과는 Preliminary 이며 Official NAV 는 독립 승인이 필요하다."),
            "supervisor_llm_called": False}


def supervise(state: CloseState, *, chat=None) -> dict:
    """마감 서술. **수치를 다시 쓰지 않는다** - nav/pnl 은 이미 확정된 값이다."""
    if state.get("nav_status") == NAV_BLOCKED and chat is None:
        return _deterministic_close_note(state)

    bundle = {"recon": state.get("recon"), "breaks": state.get("breaks"),
              "break_narrative": state.get("break_narrative"),
              "projection_check": state.get("projection_check"),
              "snapshot": state.get("snapshot"), "nav_identity": state.get("nav_identity"),
              "nav_status": state.get("nav_status"), "report": state.get("report")}
    task = f"""Using ONLY the close evidence below, write a Korean close narrative for CEO and
Audit review. Every figure is already final — quote them verbatim and never recompute or restate
a NAV, PnL or position number. This NAV is PRELIMINARY; Official NAV requires an independent
approval you do not hold, so never describe it as official or confirmed.
Schema (JSON only):
{{"narrative": "3-5 sentences in Korean covering reconciliation, valuation and NAV status",
 "escalate": true or false,
 "cited_figures": ["which figures you referenced"]}}

Evidence:
{json.dumps(bundle, ensure_ascii=False, indent=1)}"""
    try:
        call = chat or _hermes_chat
        note = _parse_json_block(call(_persona("portfolio-control-supervisor"), task),
                                 ("narrative", "escalate", "cited_figures"), "Supervisor")
        return {"narrative": note["narrative"], "supervisor_llm_called": True,
                "_llm_escalate": bool(note["escalate"])}
    except Exception as exc:  # noqa: BLE001 - intentional fallback boundary
        # 서술 실패가 이미 확정된 수치를 지우지 않는다. 결정론 서술로 대체하고 fallback 만 남긴다.
        return {"narrative": _deterministic_close_note(state)["narrative"],
                "supervisor_llm_called": True, "fallbacks": [_fallback("supervise", exc)]}


# ── 노드 8: Notion 업로드 (Reporter - 결정론) ──────────────────────────────
def notion_report(state: CloseState, *, uploader=None) -> dict:
    from notion_reporter import upload_close

    out = _assemble_out(state)
    report_md = _render_report_md(out)
    upload = uploader or upload_close
    try:
        result = upload(out, report_md=report_md)
    except Exception as exc:  # noqa: BLE001 - intentional fallback boundary
        result = {"ok": False, "reason": f"Reporter 예외: {type(exc).__name__}"}
    return {"notion_upload": result, "report_markdown": report_md}


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def _has_material_break(state: CloseState) -> bool:
    return any(b.get("escalates") for b in (state.get("breaks") or []))


def _route_after_reconcile(state: CloseState):
    # Break 가 있으면 먼저 서술을 붙인다(있는 것만 - 없으면 LLM 을 부르지 않는다).
    return "narrate_breaks" if state.get("breaks") else "verify_projection"


def _route_after_breaks(state: CloseState) -> str:
    # Material Break 는 평가·보고를 건너뛴다. 브로커와 내부가 어긋난 포지션으로 만든 NAV 는
    # 틀린 줄 알면서 내보내는 숫자다 (마스터플랜 11.2, 개발 원칙 9번).
    return "supervise" if _has_material_break(state) else "verify_projection"


def build_pipeline(chat=None, uploader=None):
    """chat / uploader 를 주입받아 자체 점검이 Hermes·Notion 없이 돌게 한다."""
    g = StateGraph(CloseState)
    for name, handler in (
        ("validate_inputs", validate_inputs),
        ("reconcile", reconcile),
        ("narrate_breaks", lambda s: narrate_breaks(s, chat=chat)),
        ("verify_projection", verify_projection),
        ("value", value),
        ("daily_report", daily_report_node),
        ("supervise", lambda s: supervise(s, chat=chat)),
        ("notion_report", lambda s: notion_report(s, uploader=uploader)),
    ):
        g.add_node(name, _guard_node(name, handler))
    g.set_entry_point("validate_inputs")
    g.add_edge("validate_inputs", "reconcile")
    g.add_conditional_edges("reconcile", _route_after_reconcile)
    g.add_conditional_edges("narrate_breaks", _route_after_breaks)
    g.add_edge("verify_projection", "value")
    g.add_edge("value", "daily_report")
    g.add_edge("daily_report", "supervise")
    g.add_edge("supervise", "notion_report")
    g.add_edge("notion_report", END)
    return g.compile()


def _assemble_out(state: CloseState) -> dict:
    """CloseState -> 외부 결과 dict. 필드 목록을 한 곳에서만 유지한다."""
    breaks = state.get("breaks") or []
    projection = state.get("projection_check") or {}
    materials = [b for b in breaks if b.get("escalates")]

    # NAV 확정을 막는 사유는 전부 결정론이다. LLM 의 escalate 판단은 이 값을 못 바꾼다.
    blocked = (bool(materials)
               or state.get("snapshot") is None
               or state.get("nav_status") == NAV_BLOCKED
               or projection.get("trial_balance_balanced") is False
               or projection.get("rebuild_deterministic") is False
               or bool(projection.get("drift")))
    return {
        "pipeline_version": PIPELINE_VERSION,
        "close_id": state.get("close_id"),
        "input_hash": state.get("input_hash"),
        "trace_id": state.get("trace_id"),
        "fund_id": state.get("fund_id"), "book_id": state.get("book_id"),
        "accounting_date": (state["accounting_date"].isoformat()
                            if isinstance(state.get("accounting_date"), date) else None),
        "recon": state.get("recon", {}),
        "breaks": breaks,
        "material_break_count": len(materials),
        "break_narrative": state.get("break_narrative"),
        "projection_check": projection,
        "snapshot": state.get("snapshot"),
        "nav_identity": state.get("nav_identity", {}),
        "nav_status": NAV_BLOCKED if blocked else NAV_PRELIMINARY,
        "report": state.get("report"),
        "narrative": state.get("narrative", ""),
        "escalate": bool(blocked or state.get("fallbacks")),
        "fallbacks": state.get("fallbacks", []),
        "observability": _langsmith_handoff(state.get("trace_id") or ""),
        "agent_versions": {
            "model": _model_version(),
            "supervisor_prompt": _prompt_version("portfolio-control-supervisor"),
            "reconciliation_prompt": _prompt_version("reconciliation-agent"),
        },
        # 이 파이프라인은 NAV 를 확정하지 않는다 - 소비자가 착각하지 않게 계약으로 박는다.
        "is_official": False,
        "official_nav_requires": "독립 승인 (이 파이프라인에 권한 없음)",
    }


def run_accounting_close(
    *,
    ledger: Ledger,
    as_of: datetime,
    accounting_date: date,
    marks: dict | None = None,
    external: dict | None = None,
    opening_snapshot: PortfolioSnapshot | None = None,
    stored_projection: dict | None = None,
    strategy_of: dict | None = None,
    trace_id: str | None = None,
    chat=None,
    uploader=None,
) -> dict:
    """본부 단독 실행 - 다른 본부의 run_<dept>_department 와 같은 외부 인터페이스."""
    initial: dict = {"ledger": ledger, "as_of": as_of, "accounting_date": accounting_date,
                     "marks": marks or {}, "external": external or {},
                     "opening_snapshot": opening_snapshot,
                     "stored_projection": stored_projection, "strategy_of": strategy_of,
                     "trace_id": trace_id, "fallbacks": []}
    try:
        # tracing_context 는 enabled 를 건드리지 않는다 - LANGSMITH_TRACING 이 꺼져 있으면
        # 그대로 꺼진 채고, 켜져 있을 때만 회계본부 Project 로 보낸다.
        with tracing_context(project_name=_ls_project()):
            state = build_pipeline(chat=chat, uploader=uploader).invoke(initial)
    except Exception as exc:  # noqa: BLE001 - intentional fallback boundary
        state = {**initial, "nav_status": NAV_BLOCKED, "breaks": [], "recon": {},
                 "narrative": "", "fallbacks": [_fallback("pipeline", exc)]}
    out = _assemble_out(state)
    out["report_markdown"] = state.get("report_markdown") or _render_report_md(out)
    out["notion_upload"] = state.get("notion_upload") or {
        "ok": False, "reason": "파이프라인이 Reporter 전에 실패해 업로드하지 않았다"}
    return out


# ── 결정론적 MD 리포트 (순수 함수) ─────────────────────────────────────────
def _md_cell(value: Any) -> str:
    """표 셀 한 칸. 줄바꿈은 공백으로 접는다 - <br> 은 departments/notion_markdown.py 가
    HTML 을 해석하지 않아 Notion 블록에서 문자 그대로 보인다(트레이딩본부 실측)."""
    if value is None:
        return "—"
    return " ".join(str(value).replace("|", "\\|").split())


def _md_lines(title: str, value) -> list[str]:
    lines = [f"### {title}", ""]
    if isinstance(value, (list, tuple)):
        lines += [f"- {_md_cell(v)}" for v in value] or ["—"]
    else:
        lines.append(_md_cell(value) if value is not None else "—")
    lines.append("")
    return lines


def _render_report_md(out: dict) -> str:
    """out(run_accounting_close 반환값)을 그대로 옮겨 적는다 - LLM 이 리포트 구조나 수치를
    창작하지 않는다."""
    snap = out.get("snapshot") or {}
    ident = out.get("nav_identity") or {}
    proj = out.get("projection_check") or {}
    blocked = out.get("nav_status") == NAV_BLOCKED
    lines = [
        "# 회계/포트폴리오본부 — 마감 팩 (결정론적 생성, LLM 자유 서술 아님)",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| **close_id** | `{_md_cell(out.get('close_id'))}` |",
        f"| **회계일** | {_md_cell(out.get('accounting_date'))} |",
        f"| **NAV 상태** | **{_md_cell(out.get('nav_status'))}** |",
        "| **공식 여부** | **비공식 (is_official=false)** |",
        f"| **escalate** | {'**예**' if out.get('escalate') else '아니오'} |",
        f"| Material Break | {_md_cell(out.get('material_break_count', 0))}건 |",
        f"| fund / book | `{_md_cell(out.get('fund_id'))}` / `{_md_cell(out.get('book_id'))}` |",
        f"| pipeline_version | `{_md_cell(out.get('pipeline_version'))}` |",
        f"| input_hash | `{_md_cell(out.get('input_hash'))}` |",
        f"| trace_id | `{_md_cell(out.get('trace_id'))}` |",
        f"| LangSmith | {_md_cell(json.dumps((out.get('observability') or {}).get('langsmith'), ensure_ascii=False, sort_keys=True))} |",
        "",
        "> **이 NAV 는 Preliminary 다.** Official NAV 는 독립 승인이 필요하며 이 파이프라인은",
        "> 그 권한을 갖지 않는다. 원장 수정·NAV 확정 권한은 CEO 에게도 없다.",
        "",
        "## 1. 대사 (Reconciliation)",
        "",
        "| 종류 | 결과 | 항목 | Break | Material |",
        "|---|---|---|---|---|",
    ]
    for name, r in (out.get("recon") or {}).items():
        lines.append(f"| {_md_cell(name)} | {_md_cell(r.get('result'))} | "
                     f"{_md_cell(r.get('item_count'))} | {_md_cell(r.get('break_count'))} | "
                     f"{_md_cell(r.get('material_count'))} |")

    if out.get("breaks"):
        lines += ["", "### Break 목록", "", "| 종류 | 심각도 | 내용 | 상태 |", "|---|---|---|---|"]
        lines += [f"| {_md_cell(b.get('kind'))} | {_md_cell(b.get('severity'))} | "
                  f"{_md_cell(b.get('detail'))} | {_md_cell(b.get('status'))} |"
                  for b in out["breaks"]]
    if out.get("break_narrative"):
        lines += _md_lines("Break 서술 (ACC-02, 비바인딩)", out["break_narrative"])

    lines += [
        "", "## 2. Projection 대조 (F15 완료 조건 1·3)", "",
        "| 검사 | 결과 |",
        "|---|---|",
        f"| rebuild 재현성 (Online == Replay) | {'통과' if proj.get('rebuild_deterministic') else '**실패**'} |",
        f"| 차대 균형 | {'통과' if proj.get('trial_balance_balanced') else '**실패**'} (합계 {_md_cell(proj.get('trial_balance_sum'))}) |",
        f"| 저장 projection 대조 | {'수행' if proj.get('stored_projection_compared') else '미수행 (저장본 없음)'} |",
        f"| 불일치 건수 | {_md_cell(len(proj.get('drift') or []))} |",
    ]
    if proj.get("drift"):
        lines += ["", "| 종목 | 재구축 | 저장본 |", "|---|---|---|"]
        lines += [f"| {_md_cell(d.get('instrument_id'))} | {_md_cell(d.get('rebuilt'))} | "
                  f"{_md_cell(d.get('stored'))} |" for d in proj["drift"]]

    lines += ["", "## 3. 평가와 NAV (F15 완료 조건 2)", ""]
    if snap:
        lines += [
            "| 항목 | 금액 |",
            "|---|---|",
            f"| 현금 | {_md_cell(snap.get('cash'))} |",
            f"| 증권평가액 | {_md_cell(snap.get('securities_value'))} |",
            f"| 미실현손익 | {_md_cell(snap.get('unrealized_pnl'))} |",
            f"| **NAV (Preliminary)** | **{_md_cell(snap.get('nav'))}** |",
            f"| Gross / Net Exposure | {_md_cell(snap.get('gross_exposure'))} / {_md_cell(snap.get('net_exposure'))} |",
            "",
            (f"NAV 항등식 (NAV == 현금 + 증권평가액): "
            f"**{'일치' if ident.get('ok') else '불일치'}** (차이 {_md_cell(ident.get('difference'))})"),
        ]
    else:
        lines.append(f"평가하지 않았다 — {_md_cell(ident.get('reason') or '사유 기록 없음')}. "
                     "Mark 가 하나라도 없거나 낡으면 NAV 를 만들지 않는다.")

    if out.get("report"):
        rep = out["report"]
        lines += ["", "## 4. 일일 보고", "",
                  f"- NAV 변동: {_md_cell((rep.get('nav') or {}).get('change'))}",
                  f"- 순손익: {_md_cell((rep.get('pnl') or {}).get('net'))}",
                  f"- 공식 여부: {_md_cell(rep.get('is_official'))}"]

    lines += _md_lines("마감 서술 (ACC-00, 비바인딩)", out.get("narrative") or "—")

    if out.get("fallbacks"):
        lines += ["## Fallback", "", "| 단계 | 오류 | 내용 | 안전 조치 |", "|---|---|---|---|"]
        lines += [f"| {_md_cell(f.get('stage'))} | {_md_cell(f.get('error'))} | "
                  f"{_md_cell(f.get('error_message'))} | {_md_cell(f.get('safe_action'))} |"
                  for f in out["fallbacks"]]
    if blocked:
        lines += ["", "> NAV 가 **차단**됐다. 위 사유를 해소하기 전에는 Preliminary NAV 도",
                  "> 다음 단계로 넘기지 않는다."]
    return "\n".join(lines) + "\n"


# ── 자체 점검 (Hermes 없음, Notion 없음, 네트워크 없음) ────────────────────
from dataclasses import dataclass, field

from contracts import Side
from ledger import Position

_NOW = datetime(2026, 8, 3, 6, 0, tzinfo=timezone.utc)
_DATE = date(2026, 8, 3)
_FUND, _BOOK = UUID(int=1), UUID(int=2)
_AAA = UUID(int=10)


@dataclass
class _Fill:
    quantity: Decimal
    price: Decimal
    fee: Decimal
    tax: Decimal
    event_time: datetime
    broker_fill_id: str
    fill_id: UUID = field(default_factory=uuid4)


def _ledger_with_position() -> Ledger:
    """자본 1억 + 100주 매수. 엔진의 실제 API 로만 세운다 - 분개를 손으로 만들지 않는다."""
    led = Ledger(fund_id=_FUND, book_id=_BOOK)
    led.post_capital(Decimal(100000000), _NOW, "cap_1")
    led.post_fill(_Fill(Decimal(100), Decimal(70000), Decimal(1050),
                        Decimal(0), _NOW, "bf_1"),
                  Side.BUY, _AAA, Position(_AAA))
    return led


def _marks(price="70000", minutes_old=0) -> dict:
    return {_AAA: MarkPrice(_AAA, Decimal(price), _NOW - timedelta(minutes=minutes_old))}


def _no_upload(out, *, report_md=""):
    """자체 점검 전용 Reporter 스텁 - .env 에 실 NOTION_TOKEN 이 있어 스텁이 없으면
    자체 점검이 진짜 Notion 에 페이지를 만든다."""
    return {"ok": False, "reason": "self-check stub - 네트워크 없음"}


def _run(**kw):
    kw.setdefault("uploader", _no_upload)
    kw.setdefault("ledger", _ledger_with_position())
    kw.setdefault("as_of", _NOW)
    kw.setdefault("accounting_date", _DATE)
    kw.setdefault("marks", _marks())
    return run_accounting_close(**kw)


def _stub(supervisor=None, recon=None, capture=None):
    sup = supervisor or {"narrative": "마감 정상", "escalate": False, "cited_figures": ["nav"]}
    rec = recon or {"break_narrative": "브레이크 서술", "owner_hint": ["운영"]}

    def chat(persona, task):
        who = "recon" if "Reconciliation Agent" in persona else "supervisor"
        if capture is not None:
            capture[who] = task
        payload = rec if who == "recon" else sup
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload, ensure_ascii=False)
    return chat


def _check_graph_shape():
    assert build_pipeline() is not None
    print("  그래프 컴파일              OK")


def _check_persona_lookup():
    for name in ("portfolio-control-supervisor", "reconciliation-agent"):
        text = _persona(name)
        assert text.startswith("You are the"), name
    assert "never generate a trading signal" in _persona("portfolio-control-supervisor")
    assert "never confirm a fuzzy candidate" in _persona("reconciliation-agent")
    try:
        _persona("nonexistent-agent")
        raise AssertionError("없는 페르소나가 조회됐다")
    except ValueError:
        pass
    print("  페르소나 조회 + 실패 가드  OK")


def _check_agent_has_no_tools():
    """회계 직원에게 도구가 붙지 않는지. 트레이딩본부에서 분석가가 남의 본부 디렉터리에
    파일을 쓴 실측 사고(2026-08-03)의 재발 방지."""
    import types

    captured: dict = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured.update(kw)

        def chat(self, task):
            return "{}"

    fake = types.ModuleType("run_agent")
    fake.AIAgent = FakeAgent
    saved = sys.modules.get("run_agent")
    sys.modules["run_agent"] = fake
    try:
        _hermes_chat("persona", "task")
    finally:
        sys.modules.pop("run_agent", None)
        if saved is not None:
            sys.modules["run_agent"] = saved
    assert captured.get("enabled_toolsets") == [], captured.get("enabled_toolsets")
    assert captured.get("model") == _model_version()
    print("  직원 도구 0개 (경계)       OK")


def _check_happy_close():
    out = _run(chat=_stub())
    assert out["nav_status"] == NAV_PRELIMINARY, out["nav_status"]
    assert out["is_official"] is False
    assert out["escalate"] is False, out["fallbacks"]
    # 자본 1억 - 수수료 1050, 체결가로 평가하면 미실현 0
    assert out["snapshot"]["nav"] == "99998950", out["snapshot"]
    assert out["nav_identity"]["ok"] is True          # F15 완료 조건 2
    assert out["projection_check"]["rebuild_deterministic"] is True   # F15 완료 조건 3
    assert out["projection_check"]["trial_balance_balanced"] is True
    print("  정상 마감 (Preliminary)    OK")


def _check_nav_never_official():
    # 이 본부의 핵심 계약. LLM 이 뭐라 하든 OFFICIAL 이 되지 않는다.
    liar = {"narrative": "Official NAV 확정", "escalate": False, "cited_figures": []}
    out = _run(chat=_stub(supervisor=liar))
    assert out["is_official"] is False
    assert out["nav_status"] in (NAV_PRELIMINARY, NAV_BLOCKED)
    assert "OFFICIAL" != out["nav_status"]
    assert out["official_nav_requires"]
    assert "Preliminary" in out["report_markdown"]
    print("  NAV OFFICIAL 불가 계약     OK")


def _check_missing_mark_blocks_nav():
    # Mark 가 없으면 NAV 를 만들지 않는다. 일부만 평가한 NAV 는 틀린 NAV 다.
    out = _run(marks={}, chat=_stub())
    assert out["snapshot"] is None
    assert out["nav_status"] == NAV_BLOCKED and out["escalate"] is True
    assert out["fallbacks"][0]["stage"] == "value"
    assert out["fallbacks"][0]["safe_action"] == "NAV_NOT_CONFIRMED"
    # 낡은 Mark 도 같다
    stale = _run(marks=_marks(minutes_old=999), chat=_stub())
    assert stale["nav_status"] == NAV_BLOCKED, stale["nav_identity"]
    print("  Mark 없음/낡음 -> NAV 차단  OK")


def _check_material_break_blocks_valuation():
    # 브로커 잔고가 내부와 다르면 평가·보고를 건너뛴다 (마스터플랜 11.2).
    called: list = []

    def watch(persona, task):
        called.append("recon" if "Reconciliation Agent" in persona else "supervisor")
        return json.dumps({"break_narrative": "수량 불일치", "owner_hint": ["운영"],
                           "narrative": "차단", "escalate": True, "cited_figures": []})

    out = _run(external={"positions": {_AAA: Decimal(50)}}, chat=watch)
    assert out["material_break_count"] >= 1, out["breaks"]
    assert out["nav_status"] == NAV_BLOCKED and out["escalate"] is True
    assert out["snapshot"] is None, "Material Break 인데 평가했다"
    assert out["report"] is None
    assert "recon" in called, "Break 가 있는데 ACC-02 서술을 안 불렀다"
    print("  Material Break -> 평가 차단 OK")


def _check_clean_recon_passes():
    # 대사가 맞으면 평가까지 간다 - 게이트가 항상 막기만 하는 게 아니다.
    out = _run(external={"positions": {_AAA: Decimal(100)},
                         "cash": Decimal(92998950)}, chat=_stub())
    assert out["material_break_count"] == 0, out["breaks"]
    assert out["snapshot"] is not None and out["nav_status"] == NAV_PRELIMINARY
    assert out["recon"]["position"]["result"] == "matched"
    print("  대사 일치 -> 평가 진행      OK")


def _check_projection_drift_blocks():
    # 저장 projection 과 분개 재구축이 어긋나면 분개가 사실이고 차이는 Break 다.
    out = _run(stored_projection={"positions": {_AAA: Decimal(999)}}, chat=_stub())
    assert out["projection_check"]["drift"], "불일치를 못 잡았다"
    assert out["nav_status"] == NAV_BLOCKED and out["escalate"] is True
    clean = _run(stored_projection={"positions": {_AAA: Decimal(100)},
                                    "cash": Decimal(92998950)}, chat=_stub())
    assert clean["projection_check"]["drift"] == [], clean["projection_check"]
    assert clean["nav_status"] == NAV_PRELIMINARY
    print("  projection 대조 (백로그 1)  OK")


def _check_daily_report_produced():
    """기초 스냅샷을 주면 일일 보고가 실제로 나온다.

    이 검사가 없어서 _snapshot_obj 를 State 에 선언 안 한 버그가 통과했다 - report 가 계속
    null 인데 fallback 도 없어서 아무도 안 죽었다(2026-08-03 실측).
    """
    opening_ledger = Ledger(fund_id=_FUND, book_id=_BOOK)
    opening_ledger.post_capital(Decimal(100000000), _NOW - timedelta(hours=1), "cap_1")
    opening = value_portfolio(opening_ledger, {}, _NOW - timedelta(hours=1))

    out = _run(marks=_marks("77000"), opening_snapshot=opening, chat=_stub())
    assert out["report"] is not None, "기초 스냅샷을 줬는데 일일 보고가 없다"
    rep = out["report"]
    assert rep["is_official"] is False, "일일 보고가 공식으로 나왔다"
    # 기초 NAV 1억 -> 기말 100,698,950 (미실현 70만 - 수수료 1,050)
    assert rep["nav"]["close"] == "100698950", rep["nav"]
    assert rep["nav"]["change"] == "698950", rep["nav"]
    assert "일일 보고" in out["report_markdown"]

    # 기초 스냅샷이 없으면 보고를 지어내지 않는다
    without = _run(marks=_marks("77000"), chat=_stub())
    assert without["report"] is None
    assert without["nav_status"] == NAV_PRELIMINARY, "보고 없음이 NAV 를 막으면 안 된다"
    print("  일일 보고 산출             OK")


def _check_no_recon_is_not_pass():
    # 브로커 명세서가 없으면 "대사 통과"로 기록하지 않는다.
    out = _run(chat=_stub())
    assert out["recon"]["_none"]["result"] == "not_reconciled", out["recon"]
    print("  미수행 대사 != 통과         OK")


def _check_llm_cannot_change_numbers():
    # supervisor 가 escalate=False 라 해도 결정론 차단 사유가 있으면 escalate 는 True 다.
    out = _run(marks={}, chat=_stub(supervisor={"narrative": "문제 없음", "escalate": False,
                                                 "cited_figures": []}))
    assert out["escalate"] is True, "LLM 이 escalate 를 뒤집었다"
    assert out["nav_status"] == NAV_BLOCKED
    # 서술 실패도 수치를 지우지 않는다
    broken = _run(chat=_stub(supervisor=RuntimeError("hermes down")))
    assert broken["snapshot"] is not None, "서술 실패가 평가 결과를 지웠다"
    assert broken["fallbacks"][0]["stage"] == "supervise"
    print("  LLM 이 수치를 못 바꾼다     OK")


def _check_reproducibility():
    a = _run(chat=_stub())
    b = _run(chat=_stub())
    assert a["input_hash"] == b["input_hash"], "같은 원장인데 해시가 다르다"
    assert a["close_id"] == b["close_id"]
    c = _run(marks=_marks("77000"), chat=_stub())
    assert c["input_hash"] != a["input_hash"], "Mark 가 바뀌었는데 해시가 같다"
    print("  재현성 (input_hash)        OK")


def _check_malformed_input():
    naive = run_accounting_close(ledger=_ledger_with_position(),
                                  as_of=datetime(2026, 8, 3, 6, 0),   # noqa: DTZ001 - intentionally invalid input
                                 accounting_date=_DATE, marks=_marks(),
                                 chat=_stub(), uploader=_no_upload)
    assert naive["nav_status"] == NAV_BLOCKED and naive["escalate"] is True
    assert naive["fallbacks"][0]["stage"] == "pipeline"
    assert naive["report_markdown"], "실패해도 리포트는 나와야 한다"
    print("  잘못된 입력 fail-closed     OK")


def _check_report_renders():
    out = _run(chat=_stub())
    md = out["report_markdown"]
    assert _render_report_md(out) == md, "리포트가 호출마다 달라진다"
    assert "<br>" not in md, "MD 에 <br> 가 있다 - Notion 블록에서 문자로 보인다"
    for must in (out["close_id"], out["input_hash"], "Preliminary", "99998950"):
        assert must in md, f"리포트에 {must!r} 가 없다"

    from departments.notion_markdown import markdown_to_notion_blocks

    blocks = markdown_to_notion_blocks(md)
    kinds = {b["type"] for b in blocks}
    for need in ("heading_1", "heading_2", "table", "quote"):
        assert need in kinds, f"렌더링에 {need} 가 없다: {sorted(kinds)}"

    def _plain(b):
        d = b.get(b["type"], {})
        return "".join(x.get("text", {}).get("content", "") for x in (d.get("rich_text") or []))

    rendered = " ".join(_plain(b) for b in blocks)
    assert "<br>" not in rendered and "**" not in rendered, "마크업이 본문으로 샜다"
    print("  결정론 MD 리포트 + 렌더링  OK")


def _check_notion_report_node():
    seen: dict = {}

    def uploader(out, *, report_md=""):
        seen["out"], seen["report_md"] = out, report_md
        return {"ok": True, "url": "https://notion.so/fake"}

    out = _run(chat=_stub(), uploader=uploader)
    assert out["notion_upload"] == {"ok": True, "url": "https://notion.so/fake"}
    assert seen["out"]["close_id"] == out["close_id"]
    assert seen["out"]["is_official"] is False
    assert "Preliminary" in seen["report_md"]

    def dead(out, *, report_md=""):
        raise RuntimeError("notion down")

    broken = _run(chat=_stub(), uploader=dead)
    assert broken["notion_upload"]["ok"] is False
    assert broken["nav_status"] == NAV_PRELIMINARY, "업로드 실패가 NAV 상태를 바꿨다"
    print("  Notion Reporter 노드       OK")


def _run_live() -> dict:
    """--run 용 고정 Fixture. 기초 스냅샷(자본만)과 기말(매수 후)을 만들어 일일 보고까지 낸다.

    ponytail: 원장·Mark 는 Fixture 다. 실제 Supabase 원장과 market-api 종가로 바꾸는 것은
    D3(Valuation/PnL/NAV) 착수 조건이며 지금은 market-api bulk 종가 조회면 대기 중이다.
    """
    opening_ledger = Ledger(fund_id=_FUND, book_id=_BOOK)
    opening_ledger.post_capital(Decimal(100000000), _NOW - timedelta(hours=1), "cap_1")
    opening = value_portfolio(opening_ledger, {}, _NOW - timedelta(hours=1))
    return run_accounting_close(
        ledger=_ledger_with_position(), as_of=_NOW, accounting_date=_DATE,
        marks=_marks("77000"),
        external={"positions": {_AAA: Decimal(100)}, "cash": Decimal(92998950)},
        opening_snapshot=opening)


def _check_langsmith_observability():
    """기본은 꺼짐이고, Project 는 회계본부로 격리된다.
    실제 그래프를 켠 채로 돌리지 않는다 - 이 점검은 네트워크를 타면 안 된다."""
    saved = {k: os.environ.get(k) for k in
             ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_PROJECT")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        off = _langsmith_handoff("t1")["langsmith"]
        assert off["enabled"] is False and off["handoff_status"] == "not_configured", off
        assert off["project"] is None, off

        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_PROJECT"] = "First"
        on = _langsmith_handoff("t1")["langsmith"]
        assert on == {"enabled": True, "project": "First-05-accounting",
                      "run_id": None, "handoff_status": "configured"}, on
        # 다른 부서 Project 를 그대로 쓰지 않는다 (부서 경계).
        assert _ls_project().endswith("-05-accounting"), _ls_project()
        os.environ.pop("LANGSMITH_PROJECT")
        assert _ls_project() == "hedgefund-05-accounting", _ls_project()
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    md = _render_report_md({"observability": _langsmith_handoff("t1"), "recon": {}})
    assert "| LangSmith |" in md, md[:400]
    print("  LangSmith 관측성           OK")


def _check_secret_redaction():
    leaked = _fallback("value", RuntimeError("connect https://x.notion.com/t ntn_abc123DEF"))
    assert "ntn_abc123DEF" not in leaked["error_message"], leaked
    assert "https://" not in leaked["error_message"], leaked
    print("  자격증명 마스킹            OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        print(f"{PIPELINE_VERSION} 실행 (고정 Fixture 원장 - 실 Hermes + Notion)")
        result = _run_live()
        print(json.dumps({k: v for k, v in result.items() if k != "report_markdown"},
                         ensure_ascii=False, indent=1))
        report_dir = _BASE / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = report_dir / f"accounting_close_{result['close_id']}_{stamp}.md"
        path.write_text(result["report_markdown"], encoding="utf-8")
        print(f"결정론적 MD 리포트 저장: {path}")
        raise SystemExit(0 if result["nav_status"] == NAV_PRELIMINARY else 1)

    print(f"{PIPELINE_VERSION} 자체 점검 (Hermes·Notion 없음, 네트워크 없음)")
    _check_graph_shape()
    _check_persona_lookup()
    _check_agent_has_no_tools()
    _check_happy_close()
    _check_nav_never_official()
    _check_missing_mark_blocks_nav()
    _check_material_break_blocks_valuation()
    _check_clean_recon_passes()
    _check_projection_drift_blocks()
    _check_daily_report_produced()
    _check_no_recon_is_not_pass()
    _check_llm_cannot_change_numbers()
    _check_reproducibility()
    _check_malformed_input()
    _check_report_renders()
    _check_notion_report_node()
    _check_secret_redaction()
    _check_langsmith_observability()
    print("회계본부 마감 파이프라인 18개 영역 통과. 실행은 --run (Hermes + Notion 필요)")
