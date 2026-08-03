#!/usr/bin/env python3
"""CEO Office LangGraph 파이프라인 - run_ceo_department(...) -> Daily Report Case.

담당: 영주 (CEO Office)
근거: departments/{01-research,03-risk,06-ai-qa-audit}/scripts.py 패턴(LangGraph 실무진 +
      Hermes 부서장 정제 + Notion Reporter Node)을 CEO Office에 적용.
      결정론 절반은 이미 있는 src/reporting/daily_report.py(DailyReportAssembler)를 그대로 쓴다 -
      이 파일은 그 위에 LangGraph 배선과 서술·Notion 업로드만 얹는다.

Risk/QA와의 차이 - **CEO에게는 RiskEngine/EvidenceQaEngine 같은 판정 엔진이 없다.** CEO는 다른
본부 숫자를 재계산하지 않고 Snapshot ID만 조립한다(팀 가이드 3.1). 그래서 노드가 2개뿐이다:
  assemble_report   DailyReportAssembler(결정론 - Section 완결성 게이트, content_hash idempotency)
  narrate           executive-orchestrator 페르소나(hermes/config.yaml 원문) - status나
                    source_snapshot_ids를 못 바꾸고 서술만 만든다. status=FAILED면 LLM을 아예
                    안 부른다(Risk의 REJECT short-circuit과 동일 이유 - 이미 실패한 조립에
                    비싼 호출을 얹지 않는다)
  notion_report     Reporter Node(결정론, LLM 아님) - governance.report_runs 계약을 Notion CEO
                    DB(NOTION_CEO_DB)에 Projection으로 올린다. 실패해도 status는 못 바꾼다

이번 버전에는 없는 것(의도적 축소, 후속 작업):
  - harness/journal.py 급 실행 증거 기록(Risk/QA는 있음) - Notion 업로드까지가 이번 목표다.
  - asyncpg Repository - DailyReportAssembler는 여전히 InMemoryReportRunRepository를 쓴다
    (PR 대화 기준 GOV-01 선행 작업, 이 파일 범위 밖).
  - portfolio/risk/research/strategy-registry/audit Snapshot 조회 API 실 연동 - sections_input을
    호출자가 직접 채운다(config.yaml not_started와 동일 한계).

원칙 (전 노드 공통, CLAUDE.md):
  - status·source_snapshot_ids는 DailyReportAssembler 결과에서만 온다. LLM은 그 값을 못 바꾸고
    서술만 만든다.
  - LLM 주소는 Hermes AIAgent(run_agent) Lazy Import다 - Hermes 없는 환경에서도 모듈 자체는
    항상 import 가능해야 한다(자체 점검이 이 경로를 검증한다).

실행:
  python scripts.py               # 자체 점검 (Hermes·Notion 없음)
  python scripts.py --run         # 데모 Section으로 실제 실행 (Hermes 필요, --run-network 로 Notion도)
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TypedDict

_BASE = Path(__file__).resolve().parent
_REPO_ROOT = _BASE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_REPORTING_DIR = _BASE / "src" / "reporting"
sys.path.insert(0, str(_REPORTING_DIR))

from langgraph.graph import END, StateGraph  # noqa: E402

from daily_report import (  # noqa: E402
    DailyReportAssembler,
    DailyReportSections,
    InMemoryReportRunRepository,
    SnapshotRef,
)
from reporting import evaluation_metrics, json_cell, langsmith_handoff, md_cell  # noqa: E402

PIPELINE_VERSION = "ceo-office-pipeline-v1"
ALL_SECTION_FIELDS = ("portfolio", "risk", "research", "execution", "strategy", "qa")


class CEOState(TypedDict, total=False):
    fund_id: str
    as_of: str                      # ISO date "YYYY-MM-DD"
    template_version: str
    trace_id: str
    sections_input: dict            # {"portfolio": {"snapshot_id":.., "as_of":..} | None, ...,
                                     #  "pending_user_action_case_ids": [...]}
    status: str                     # 'QUEUED' | 'FAILED' - DailyReportAssembler 결과에서만 옴
    source_snapshot_ids: list[str]
    missing_required: list[str]
    content_hash: str
    present_sections: list[str]
    narrative: str
    escalate: bool
    notion_upload: dict | None
    report_markdown: str
    evaluation: dict
    observability: dict


def _ref(d: dict | None) -> SnapshotRef | None:
    if d is None:
        return None
    return SnapshotRef(snapshot_id=d["snapshot_id"], as_of=datetime.fromisoformat(d["as_of"]))


# ── 노드 1: Report 조립 (결정론 직원 - DailyReportAssembler) ───────────────
def assemble_report(state: CEOState) -> dict:
    raw = state.get("sections_input") or {}
    sections = DailyReportSections(
        portfolio=_ref(raw.get("portfolio")),
        risk=_ref(raw.get("risk")),
        research=_ref(raw.get("research")),
        execution=_ref(raw.get("execution")),
        strategy=_ref(raw.get("strategy")),
        qa=_ref(raw.get("qa")),
        pending_user_action_case_ids=tuple(raw.get("pending_user_action_case_ids") or ()),
    )
    assembler = DailyReportAssembler(InMemoryReportRunRepository())
    result = assembler.assemble(
        fund_id=state["fund_id"], as_of=date.fromisoformat(state["as_of"]),
        template_version=state["template_version"], sections=sections, trace_id=state["trace_id"],
    )
    present = [f for f in ALL_SECTION_FIELDS if getattr(sections, f) is not None]
    return {
        "status": result.row.status,
        "source_snapshot_ids": list(result.row.source_snapshot_ids),
        "missing_required": list(result.missing_required),
        "content_hash": result.row.content_hash,
        "present_sections": present,
    }


# ── 노드 2: 서술 (executive-orchestrator 페르소나 - Hermes AIAgent) ────────
def _persona(name: str) -> str:
    cfg = (_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8")
    m = re.search(rf'{re.escape(name)}: "(.*?)"\n', cfg, re.DOTALL)
    if not m:
        raise ValueError(f"{name} 페르소나를 config.yaml에서 찾을 수 없다")
    return m.group(1)


def _hermes_chat(persona: str, task: str) -> str:
    from run_agent import (
        AIAgent,  # Lazy Import - Hermes 없는 환경에서도 모듈 자체는 항상 import 가능해야 한다
    )

 agent = AIAgent(model=_configured_head_model(), quiet_mode=True,
                     ephemeral_system_prompt=persona)
    return agent.chat(task)


def _configured_head_model() -> str:
    """Read the current Hermes Head model instead of a legacy alias."""

    config = (_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8")
    match = re.search(r"^\s+default:\s*([^\s#]+)", config, re.MULTILINE)
    if not match:
        raise ValueError("Hermes Head model is missing from config.yaml")
    return match.group(1)


def _deterministic_failed_narrative(state: CEOState) -> str:
    missing = ", ".join(state.get("missing_required") or []) or "unknown"
    return (f"필수 Section({missing})이 없어 Report를 조립하지 못했습니다. "
            "본부 공식 Snapshot을 다시 확인한 뒤 재시도해야 합니다.")


def narrate(state: CEOState, *, chat=None) -> dict:
    # status=FAILED면 서술할 근거 자체가 없다 - 비싼 호출 없이 바로 결정론 서술로 끝낸다
    # (Risk의 REJECT short-circuit과 동일 원칙, 개발 원칙 9).
    if state["status"] == "FAILED" and chat is None:
        return {"narrative": _deterministic_failed_narrative(state), "escalate": True}

    bundle = {"status": state["status"], "present_sections": state["present_sections"],
              "missing_required": state["missing_required"],
              "source_snapshot_ids": state["source_snapshot_ids"]}
    task = f"""Using ONLY the evidence below, write a 2-4 sentence Korean summary of today's
company-wide Daily Report for the user. You cannot change "status" or invent any numeric
figures - you only have Snapshot IDs and section presence, no PnL/NAV values themselves.
Schema (JSON only):
{{"narrative": "2-4 sentences in Korean", "escalate": true or false}}

Evidence:
{json.dumps(bundle, ensure_ascii=False, indent=1)}"""

    call = chat or _hermes_chat
    out = call(_persona("executive-orchestrator"), task)
    s, e = out.find("{"), out.rfind("}")
    note = json.loads(out[s:e + 1])
    for k in ("narrative", "escalate"):
        if k not in note:
            raise ValueError(f"CEO 서술 결과에 {k} 가 없다 - 초안 거부")
    return {"narrative": note["narrative"], "escalate": note["escalate"]}


# ── 노드 3: Notion 업로드 (Reporter Node - 결정론, LLM 아님) ────────────────
def _assemble_out(state: CEOState) -> dict:
    trace_id = state.get("trace_id") or f"{PIPELINE_VERSION}:{state.get('content_hash', '')}"
    return {
        "fund_id": state["fund_id"], "report_type": "DAILY", "as_of": state["as_of"],
        "status": state["status"], "source_snapshot_ids": state["source_snapshot_ids"],
        "missing_required": state["missing_required"], "template_version": state["template_version"],
        "content_hash": state["content_hash"], "trace_id": state["trace_id"],
        "narrative": state["narrative"], "escalate": state["escalate"],
        "present_sections": state["present_sections"],
        "observability": langsmith_handoff(str(trace_id)),
    }


def notion_report(state: CEOState, *, uploader=None) -> dict:
    from notion_reporter import upload_report

    out = _assemble_out(state)
    report_md = _render_report_md(out)
    evaluation = evaluation_metrics(out, report_md)
    upload = uploader or upload_report
    try:
        notion_upload = upload(out, report_md=report_md)
    except Exception as exc:  # noqa: BLE001 - self-check preserves fail-closed behavior.
        notion_upload = {"ok": False, "reason": f"Reporter 예외: {type(exc).__name__}"}
        evaluation["fallback_count"] = evaluation.get("fallback_count", 0) + 1
    evaluation = evaluation_metrics({**out, "notion_upload": notion_upload}, report_md)
    return {"notion_upload": notion_upload, "report_markdown": report_md, "evaluation": evaluation}


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def build_pipeline():
    g = StateGraph(CEOState)
    g.add_node("assemble_report", assemble_report)
    g.add_node("narrate", narrate)
    g.add_node("notion_report", notion_report)
    g.set_entry_point("assemble_report")
    g.add_edge("assemble_report", "narrate")
    g.add_edge("narrate", "notion_report")
    g.add_edge("notion_report", END)
    return g.compile()


def run_ceo_department(
    *, fund_id: str, as_of: str, template_version: str, sections_input: dict, trace_id: str,
) -> dict:
    """본부 단독 실행 - Risk/QA/Research의 run_<dept>_department와 같은 외부 인터페이스."""
    out = build_pipeline().invoke({
        "fund_id": fund_id, "as_of": as_of, "template_version": template_version,
        "sections_input": sections_input, "trace_id": trace_id,
    })
    result = _assemble_out(out)
    result["notion_upload"] = out.get("notion_upload")
    result["report_markdown"] = out.get("report_markdown", "")
    result["evaluation"] = out.get("evaluation", evaluation_metrics(result))
    return result


# ── MD 리포트 렌더 (결정론 - LLM 미개입, out을 그대로 옮기기만 한다) ─────────
def _render_report_md(out: dict) -> str:
    lines = [
        "# CEO Office — Daily Report (결정론적 생성, LLM 자유 서술 아님)",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| **fund_id** | `{out.get('fund_id')}` |",
        f"| **as_of** | {md_cell(out.get('as_of'))} |",
        f"| **판정 (status)** | **{out.get('status')}** |",
        f"| **template_version** | {md_cell(out.get('template_version'))} |",
        f"| **content_hash** | `{out.get('content_hash')}` |",
        f"| **escalate** | {md_cell(out.get('escalate'))} |",
        f"| **생성** | {PIPELINE_VERSION}, {datetime.now(timezone.utc).isoformat()} |",
        "",
        "---",
        "",
        "## 포함된 Section",
        "",
        ", ".join(f"`{s}`" for s in out.get("present_sections", [])) or "없음",
        "",
        "## 누락된 필수 Section",
        "",
        ", ".join(f"`{m}`" for m in out.get("missing_required", [])) or "없음",
        "",
        "## Source Snapshot IDs",
        "",
    ]
    lines += [f"- `{sid}`" for sid in out.get("source_snapshot_ids", [])] or ["- (없음)"]
    lines += ["", "## 종합 서술 (executive-orchestrator, Hermes)", "", md_cell(out.get("narrative"))]

    evaluation = out.get("evaluation") or {}
    if evaluation:
        lines += ["", "## 평가 지표", "", "| 지표 | 값 |", "|---|---|"]
        lines += [f"| {md_cell(key)} | {json_cell(value)} |" for key, value in evaluation.items()]

    observability = out.get("observability") or {}
    if observability:
        lines += ["", "## LangSmith 전달", "", "| 필드 | 값 |", "|---|---|",
                  f"| trace_id | `{md_cell(observability.get('trace_id'))}` |",
                  f"| LangSmith | {json_cell(observability.get('langsmith'))} |"]

    notion = out.get("notion_upload")
    if notion is not None:
        lines += ["", "## Notion 업로드 (Reporter Node)", ""]
        lines.append(f"업로드 성공: {notion['url']}" if notion.get("ok")
                     else f"업로드 생략/실패: {notion.get('reason') or notion.get('error')}")

    lines += [
        "", "---",
        "> 이 문서는 DailyReportAssembler의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이",
        "> 그대로 옮긴 것이다 - LLM이 status나 Snapshot ID를 창작하지 않았다.",
    ]
    return "\n".join(lines)


# ── 자체 점검 (Hermes·Notion 없음) ──────────────────────────────────────────
def _check_graph_shape():
    p = build_pipeline()
    assert p is not None
    print("  그래프 컴파일              OK")


def _demo_sections(*, full: bool) -> dict:
    ref = {"snapshot_id": "s-portfolio", "as_of": "2026-08-01T00:00:00+00:00"}
    d = {"portfolio": ref, "risk": {"snapshot_id": "s-risk", "as_of": ref["as_of"]}}
    if full:
        d.update({
            "research": {"snapshot_id": "s-research", "as_of": ref["as_of"]},
            "execution": {"snapshot_id": "s-execution", "as_of": ref["as_of"]},
            "strategy": {"snapshot_id": "s-strategy", "as_of": ref["as_of"]},
            "qa": {"snapshot_id": "s-qa", "as_of": ref["as_of"]},
        })
    d["pending_user_action_case_ids"] = ["case-1"]
    return d


def _check_failed_short_circuit():
    # 필수 Section 누락(status=FAILED)이면 narrate가 LLM을 안 부르고 결정론 서술로 끝나는지 -
    # 기본 호출(build_pipeline().invoke)은 항상 chat=None으로 narrate를 부르므로 별도 스텁 없이도
    # LLM 경로를 안 타는지 확인할 수 있다.
    out = build_pipeline().invoke({
        "fund_id": "f1", "as_of": "2026-08-01", "template_version": "v1",
        "sections_input": {"pending_user_action_case_ids": []},  # portfolio/risk 둘 다 없음
        "trace_id": "t1",
    })
    assert out["status"] == "FAILED"
    assert "portfolio" in out["missing_required"] and "risk" in out["missing_required"]
    assert out["escalate"] is True
    assert "필수 Section" in out["narrative"]
    print("  FAILED 조기 종료           OK")


def _check_narrate_schema_guard():
    bad_chat = lambda persona, task: '{"narrative": "n"}'  # escalate 누락
    state = {"status": "QUEUED", "present_sections": ["portfolio", "risk"],
              "missing_required": [], "source_snapshot_ids": ["s1", "s2"]}
    try:
        narrate(state, chat=bad_chat)
        raise AssertionError("불완전 서술 결과가 통과했다")
    except ValueError:
        pass
    print("  narrate 스키마 가드         OK")


def _check_notion_report_node():
    captured = {}

    def stub_uploader(out, *, report_md=""):
        captured["out"], captured["report_md"] = out, report_md
        return {"ok": True, "url": "https://notion.so/fake"}

    state = {"fund_id": "f1", "as_of": "2026-08-01", "status": "QUEUED",
              "source_snapshot_ids": ["s1"], "missing_required": [], "template_version": "v1",
              "content_hash": "h1", "trace_id": "t1", "narrative": "n", "escalate": False,
              "present_sections": ["portfolio", "risk"]}
    result = notion_report(state, uploader=stub_uploader)
    assert result["notion_upload"] == {"ok": True, "url": "https://notion.so/fake"}
    assert result["report_markdown"]
    assert captured["out"]["fund_id"] == "f1"
    assert "content_hash" in captured["report_md"]
    print("  Notion Reporter 노드        OK")


def _check_report_renderer_purity():
    out = {"fund_id": "f1", "as_of": "2026-08-01", "status": "QUEUED", "template_version": "v1",
           "content_hash": "h1", "escalate": False, "present_sections": ["portfolio", "risk"],
           "missing_required": [], "source_snapshot_ids": ["s1", "s2"], "narrative": "요약"}
    a = _render_report_md(out)
    b = _render_report_md(out)
    assert a == b, "같은 입력이 다른 리포트를 냈다 - 순수성 위반"
    assert "f1" in a and "요약" in a and "`s1`" in a
    print("  리포트 렌더러 순수성       OK")


def _check_full_pipeline_invoke():
    # 전체 Section이 있는 QUEUED 케이스로 그래프를 실제로 돌려 배선이 맞는지 확인한다.
    called = []
    fake_chat = lambda persona, task: called.append("narrate") or json.dumps(
        {"narrative": "오늘 회사 상태는 정상입니다.", "escalate": False})
    fake_uploader = lambda out, *, report_md="": called.append("notion") or {
        "ok": False, "reason": "self-check stub"}

    global narrate, notion_report
    orig_narrate, orig_notion = narrate, notion_report
    narrate = lambda s: orig_narrate(s, chat=fake_chat)
    notion_report = lambda s: orig_notion(s, uploader=fake_uploader)
    try:
        out = build_pipeline().invoke({
            "fund_id": "f1", "as_of": "2026-08-01", "template_version": "v1",
            "sections_input": _demo_sections(full=True), "trace_id": "t1",
        })
        assert called == ["narrate", "notion"], called
        assert out["status"] == "QUEUED"
        assert set(out["present_sections"]) == set(ALL_SECTION_FIELDS)
        assert out["notion_upload"] == {"ok": False, "reason": "self-check stub"}
    finally:
        narrate, notion_report = orig_narrate, orig_notion
    print("  전체 파이프라인 배선       OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        print(f"{PIPELINE_VERSION} 실행 (데모 Section)")
        out = run_ceo_department(
            fund_id="demo-fund", as_of=date.today().isoformat(), template_version="v1",
            sections_input=_demo_sections(full="--full" in sys.argv),
            trace_id=hashlib.sha256(b"ceo-demo").hexdigest()[:16],
        )
        print(json.dumps(out, ensure_ascii=False, indent=1))
        report_dir = _BASE / "reports"
        report_dir.mkdir(exist_ok=True)
        rp = report_dir / f"ceo_daily_report_{out['content_hash'][:12]}.md"
        rp.write_text(_render_report_md(out), encoding="utf-8")
        print(f"결정론적 MD 리포트 저장: {rp}")
        raise SystemExit(0)

    print(f"{PIPELINE_VERSION} 자체 점검 (Hermes·Notion 없음)")
    _check_graph_shape()
    _check_failed_short_circuit()
    _check_narrate_schema_guard()
    _check_notion_report_node()
    _check_report_renderer_purity()
    _check_full_pipeline_invoke()
    print("본부 파이프라인 6개 영역 통과. 실행은 --run (Hermes 필요, 전체 Section은 --full)")
