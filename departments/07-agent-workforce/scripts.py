#!/usr/bin/env python3
"""Agent Workforce 인사팀 LangGraph 파이프라인 - run_hr_department(...) -> F19 Candidate Report.

담당: 영주 (Agent Workforce 인사팀)
근거: departments/{01-research,03-risk,06-ai-qa-audit}/scripts.py 패턴(LangGraph 실무진 +
      Hermes 부서장 정제 + Notion Reporter Node)을 인사팀에 적용.
      결정론 절반은 이미 있는 improvements/{candidate,workflow}.py를 그대로 쓴다.

2026-08-03 실측 변경: 처음엔 scorecard/cost.py의 build_department_scorecard를 Notion에
올리려 했지만, 실제 NOTION_HR_DB에 연결해보니 이미 만들어진 DB가
docs/06-integrations/notion/NOTION_DEPARTMENT_DB_DESIGN.md 5절 "채용 후보" 스키마
그대로였다(후보 role_code + CEO 승인/IAM 생성/QA 독립검증 체크박스). Scorecard 데이터를
그 스키마에 억지로 밀어넣으면 속성이 안 맞아 업로드가 400으로 거부된다 - 그래서 이 파이프라인은
F19 ImprovementCandidate를 대상으로 바꿨다. Scorecard 자체는 여전히 유효하고
api/app.py의 /workforce/v1/departments/{code}/scorecard로 남아있다(Notion 연동만 없음).

Risk/QA와의 차이 - **인사팀에게는 새 판정 엔진이 없다.** 이미 있는
improvements/candidate.py(Pydantic 계약)·workflow.py(상태 머신 + 자기승인 차단)가 전부다.
그래서 노드가 3개뿐이다:
  submit_candidate   ImprovementCandidate 생성 + (선택) 상태 전이(결정론 - candidate.py/
                     workflow.py). 자기승인·근거 없는 승인은 여기서 이미 막힌다
  narrate            agent-workforce-supervisor 페르소나(hermes/config.yaml 원문) - 후보
                     내용을 서술만 한다. QA독립검증·CEO승인 여부는 LLM이 못 정한다
                     (notion_report 노드가 candidate/events에서 결정론으로만 뽑는다)
  notion_report      Reporter Node(결정론, LLM 아님) - Notion HR DB(NOTION_HR_DB)에 후보
                     상태를 Projection으로 올린다. 실패해도 candidate 상태 머신은 못 바꾼다

원칙 (전 노드 공통, CLAUDE.md):
  - candidate.status·QA독립검증·CEO승인 체크박스는 workflow.py 전이 결과에서만 온다.
    LLM은 그 값을 못 바꾸고 서술만 만든다.
  - LLM 주소는 Hermes AIAgent(run_agent) Lazy Import다.

실행:
  python scripts.py               # 자체 점검 (Hermes·Notion 없음)
  python scripts.py --run         # 데모 후보 제출까지 실제 실행 (Hermes 필요)
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import TypedDict

_BASE = Path(__file__).resolve().parent
_REPO_ROOT = _BASE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_IMPROVEMENTS_DIR = _BASE / "improvements"
sys.path.insert(0, str(_IMPROVEMENTS_DIR))

from langgraph.graph import END, StateGraph  # noqa: E402

from candidate import ImprovementCandidate  # noqa: E402
from reporting import evaluation_metrics, json_cell, langsmith_handoff, md_cell  # noqa: E402
from workflow import Approval, CandidateStatus, ImprovementWorkflow  # noqa: E402

PIPELINE_VERSION = "agent-workforce-pipeline-v2"

_workflow = ImprovementWorkflow()


class HRState(TypedDict, total=False):
    candidate_input: dict          # ImprovementCandidate 생성자 필드
    transition: dict | None        # {to_status, actor, reason, at, approver?, qa_eval_run_id?}
    trace_id: str
    candidate: ImprovementCandidate
    events: list
    narrative: str
    escalate: bool
    notion_upload: dict | None
    report_markdown: str
    evaluation: dict
    observability: dict


# ── 노드 1: 후보 제출 + (선택) 전이 (결정론 - candidate.py/workflow.py) ─────
def submit_candidate(state: HRState) -> dict:
    candidate = ImprovementCandidate(**state["candidate_input"])
    tr = state.get("transition")
    if tr:
        approval = None
        if tr.get("approver"):
            approval = Approval(approver=tr["approver"], qa_eval_run_id=tr.get("qa_eval_run_id") or "",
                                 reason=tr.get("reason", ""))
        candidate = _workflow.transition(
            candidate, CandidateStatus(tr["to_status"]), actor=tr["actor"], reason=tr.get("reason", ""),
            at=datetime.fromisoformat(tr["at"]), approval=approval,
        )
    events = _workflow.events_for(candidate.candidate_id)
    return {"candidate": candidate, "events": events}


# ── 노드 2: 서술 (agent-workforce-supervisor 페르소나 - Hermes AIAgent) ────
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

    agent = AIAgent(model="poolside/laguna-s-2.1:free", quiet_mode=True,
                     ephemeral_system_prompt=persona)
    return agent.chat(task)


def narrate(state: HRState, *, chat=None) -> dict:
    c = state["candidate"]
    bundle = {
        "target_type": c.target_type.value, "target_ref": c.target_ref,
        "target_current_version": c.target_current_version, "risk_class": c.risk_class.value,
        "evidence_ids": c.evidence_ids, "expected_effect": c.expected_effect,
        "status": c.status.value, "rollback_target_version": c.rollback_target_version,
    }
    task = f"""Using ONLY the evidence below, write a 2-4 sentence Korean summary of this Agent
self-improvement candidate for CEO/HR review. You cannot decide or claim whether QA verified it
or CEO approved it - that is determined by deterministic code, not you. Just describe what is
being proposed, its risk class and evidence.
Schema (JSON only):
{{"narrative": "2-4 sentences in Korean", "escalate": true or false}}

Evidence:
{json.dumps(bundle, ensure_ascii=False, indent=1)}"""

    call = chat or _hermes_chat
    out = call(_persona("agent-workforce-supervisor"), task)
    s, e = out.find("{"), out.rfind("}")
    note = json.loads(out[s:e + 1])
    for k in ("narrative", "escalate"):
        if k not in note:
            raise ValueError(f"인사팀 서술 결과에 {k} 가 없다 - 초안 거부")
    return {"narrative": note["narrative"], "escalate": note["escalate"]}


# ── 노드 3: Notion 업로드 (Reporter Node - 결정론, LLM 아님) ────────────────
def _assemble_out(state: HRState) -> dict:
    c = state["candidate"]
    trace_id = state.get("trace_id") or f"{PIPELINE_VERSION}:{c.candidate_id}"
    return {
        "candidate_id": c.candidate_id, "target_ref": c.target_ref, "target_type": c.target_type.value,
        "status": c.status.value, "risk_class": c.risk_class.value,
        "narrative": state["narrative"], "escalate": state["escalate"],
        "qa_verified": any(e.qa_eval_run_id for e in state["events"]),
        "ceo_approved": c.status.value in {"APPROVED", "DEPLOYED", "OBSERVING", "KEPT", "ROLLED_BACK", "RETIRED"},
        "observability": langsmith_handoff(str(trace_id)),
    }


def notion_report(state: HRState, *, uploader=None) -> dict:
    from notion_reporter import upload_candidate

    out = _assemble_out(state)
    report_md = _render_report_md(out)
    evaluation = evaluation_metrics(out, report_md)
    upload = uploader or upload_candidate
    try:
        notion_upload = upload(state["candidate"], state["events"], report_md=report_md)
    except Exception as exc:  # noqa: BLE001 - self-check preserves fail-closed behavior.
        notion_upload = {"ok": False, "reason": f"Reporter 예외: {type(exc).__name__}"}
        evaluation["fallback_count"] = evaluation.get("fallback_count", 0) + 1
    evaluation = evaluation_metrics({**out, "notion_upload": notion_upload}, report_md)
    return {"notion_upload": notion_upload, "report_markdown": report_md, "evaluation": evaluation}


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def build_pipeline():
    g = StateGraph(HRState)
    g.add_node("submit_candidate", submit_candidate)
    g.add_node("narrate", narrate)
    g.add_node("notion_report", notion_report)
    g.set_entry_point("submit_candidate")
    g.add_edge("submit_candidate", "narrate")
    g.add_edge("narrate", "notion_report")
    g.add_edge("notion_report", END)
    return g.compile()


def run_hr_department(*, candidate_input: dict, transition: dict | None = None, trace_id: str) -> dict:
    """본부 단독 실행 - Risk/QA/Research의 run_<dept>_department와 같은 외부 인터페이스."""
    out = build_pipeline().invoke({
        "candidate_input": candidate_input, "transition": transition, "trace_id": trace_id,
    })
    result = _assemble_out(out)
    result["notion_upload"] = out.get("notion_upload")
    result["report_markdown"] = out.get("report_markdown", "")
    result["evaluation"] = out.get("evaluation", evaluation_metrics(result))
    return result


# ── MD 리포트 렌더 (결정론 - LLM 미개입, out을 그대로 옮기기만 한다) ─────────
def _render_report_md(out: dict) -> str:
    lines = [
        "# Agent Workforce 인사팀 — F19 개선 후보 (결정론적 생성, LLM 자유 서술 아님)",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| **candidate_id** | `{out.get('candidate_id')}` |",
        f"| **target_ref** | {md_cell(out.get('target_ref'))} |",
        f"| **target_type** | {md_cell(out.get('target_type'))} |",
        f"| **판정 (status)** | **{out.get('status')}** |",
        f"| **risk_class** | {md_cell(out.get('risk_class'))} |",
        f"| **QA 독립검증** | {md_cell(out.get('qa_verified'))} |",
        f"| **CEO 승인** | {md_cell(out.get('ceo_approved'))} |",
        f"| **escalate** | {md_cell(out.get('escalate'))} |",
        f"| **생성** | {PIPELINE_VERSION}, {datetime.now().isoformat()} |",
        "",
        "---",
        "",
        "## 종합 서술 (agent-workforce-supervisor, Hermes)",
        "",
        md_cell(out.get("narrative")),
    ]

    evaluation = out.get("evaluation") or {}
    if evaluation:
        lines += ["", "## 평가 지표", "", "| 지표 | 값 |", "|---|---|"]
        lines += [f"| {md_cell(key)} | {json_cell(value)} |" for key, value in evaluation.items()]

    notion = out.get("notion_upload")
    if notion is not None:
        lines += ["", "## Notion 업로드 (Reporter Node)", ""]
        lines.append(f"업로드 성공: {notion['url']}" if notion.get("ok")
                     else f"업로드 생략/실패: {notion.get('reason') or notion.get('error')}")

    lines += [
        "", "---",
        "> 이 문서는 candidate.py/workflow.py의 결정론적 상태와 스키마 검증된 LLM 서술을",
        "> Python이 그대로 옮긴 것이다 - LLM이 QA검증·CEO승인 여부를 창작하지 않았다.",
    ]
    return "\n".join(lines)


# ── 자체 점검 (Hermes·Notion 없음) ──────────────────────────────────────────
def _check_graph_shape():
    p = build_pipeline()
    assert p is not None
    print("  그래프 컴파일              OK")


def _demo_candidate_input(**over) -> dict:
    base = dict(
        candidate_id="ic-demo-1", author="qa-department-hermes", target_type="PROFILE",
        target_ref="agent-citation-checker", target_current_version=3,
        evidence_ids=["finding-101"], expected_effect="인용 누락 오탐 감소",
        risk_class="MEDIUM", rollback_target_version=3,
    )
    base.update(over)
    return base


def _check_submit_without_transition():
    # 그래프 전체가 아니라 노드 함수만 직접 부른다 - 전체 invoke는 narrate까지 진행돼
    # Hermes(run_agent)가 필요해진다. 이 테스트는 submit_candidate 자체만 본다.
    out = submit_candidate({
        "candidate_input": _demo_candidate_input(candidate_id="ic-1"), "transition": None,
    })
    assert out["candidate"].status == CandidateStatus.PROPOSED
    assert out["events"] == []
    print("  전이 없는 제출              OK")


def _check_illegal_transition_blocked():
    from workflow import IllegalTransition

    try:
        build_pipeline().invoke({
            "candidate_input": _demo_candidate_input(candidate_id="ic-3"),
            "transition": {"to_status": "APPROVED", "actor": "x", "reason": "x",
                           "at": "2026-08-03T00:00:00+00:00",
                           "approver": "qa-department-hermes", "qa_eval_run_id": "e1"},
            "trace_id": "t3",
        })
        raise AssertionError("PROPOSED -> APPROVED 직행이 통과했다(불법 전이)")
    except IllegalTransition:
        pass
    print("  상태머신 게이트가 파이프라인에서도 걸림  OK")


def _check_narrate_schema_guard():
    bad_chat = lambda persona, task: '{"narrative": "n"}'  # escalate 누락
    c = ImprovementCandidate(**_demo_candidate_input(candidate_id="ic-4"))
    try:
        narrate({"candidate": c}, chat=bad_chat)
        raise AssertionError("불완전 서술 결과가 통과했다")
    except ValueError:
        pass
    print("  narrate 스키마 가드          OK")


def _check_notion_report_node():
    captured = {}

    def stub_uploader(candidate, events, *, report_md=""):
        captured["candidate"], captured["report_md"] = candidate, report_md
        return {"ok": True, "url": "https://notion.so/fake"}

    c = ImprovementCandidate(**_demo_candidate_input(candidate_id="ic-5"))
    state = {"candidate": c, "events": [], "narrative": "n", "escalate": False, "trace_id": "t5"}
    result = notion_report(state, uploader=stub_uploader)
    assert result["notion_upload"] == {"ok": True, "url": "https://notion.so/fake"}
    assert result["report_markdown"]
    assert captured["candidate"].candidate_id == "ic-5"
    print("  Notion Reporter 노드          OK")


def _check_full_pipeline_invoke():
    called = []
    fake_chat = lambda persona, task: called.append("narrate") or json.dumps(
        {"narrative": "citation-checker Profile 개선을 제안합니다.", "escalate": False})
    fake_uploader = lambda candidate, events, *, report_md="": called.append("notion") or {
        "ok": False, "reason": "self-check stub"}

    global narrate, notion_report
    orig_narrate, orig_notion = narrate, notion_report
    narrate = lambda s: orig_narrate(s, chat=fake_chat)
    notion_report = lambda s: orig_notion(s, uploader=fake_uploader)
    try:
        out = build_pipeline().invoke({
            "candidate_input": _demo_candidate_input(candidate_id="ic-6"), "transition": None, "trace_id": "t6",
        })
        assert called == ["narrate", "notion"], called
        assert out["candidate"].status == CandidateStatus.PROPOSED
        assert out["notion_upload"] == {"ok": False, "reason": "self-check stub"}
    finally:
        narrate, notion_report = orig_narrate, orig_notion
    print("  전체 파이프라인 배선          OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        print(f"{PIPELINE_VERSION} 실행 (데모 후보 제출)")
        out = run_hr_department(
            candidate_input=_demo_candidate_input(candidate_id=f"ic-run-{datetime.now():%Y%m%d%H%M%S}"),
            transition=None, trace_id=f"{PIPELINE_VERSION}-run",
        )
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        report_dir = _BASE / "reports"
        report_dir.mkdir(exist_ok=True)
        rp = report_dir / f"hr_candidate_{out['candidate_id']}.md"
        rp.write_text(_render_report_md(out), encoding="utf-8")
        print(f"결정론적 MD 리포트 저장: {rp}")
        raise SystemExit(0)

    print(f"{PIPELINE_VERSION} 자체 점검 (Hermes·Notion 없음)")
    _check_graph_shape()
    _check_submit_without_transition()
    _check_illegal_transition_blocked()
    _check_narrate_schema_guard()
    _check_notion_report_node()
    _check_full_pipeline_invoke()
    print("본부 파이프라인 6개 영역 통과. 실행은 --run (Hermes 필요)")
