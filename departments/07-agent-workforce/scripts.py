#!/usr/bin/env python3
"""Agent Workforce 인사팀 LangGraph 파이프라인 - run_hr_department(...) -> Weekly Scorecard Case.

담당: 영주 (Agent Workforce 인사팀)
근거: departments/{01-research,03-risk,06-ai-qa-audit}/scripts.py 패턴(LangGraph 실무진 +
      Hermes 부서장 정제 + Notion Reporter Node)을 인사팀에 적용.
      결정론 절반은 이미 있는 scorecard/cost.py(build_department_scorecard)를 그대로 쓴다.

Risk/QA와의 차이 - **인사팀에게는 채용 판정 엔진이 없다.** Notion 설계서(docs/06-integrations/
notion/NOTION_DEPARTMENT_DB_DESIGN.md) 5절은 07번 DB를 "채용 후보" 스키마로 스케치했지만,
hiring_requests/candidates는 스키마만 있고 채용 판정 로직이 아직 없다(config.yaml not_started).
그래서 이미 구현된 F27 Workforce Scorecard를 대신 올린다 - 그래프는 2개 노드뿐이다:
  score_department   build_department_scorecard(결정론 - Cost/Capacity/Quality Snapshot 집계.
                      Snapshot 없으면 0이 아니라 None으로 남긴다 - cost.py 불변식 3)
  narrate            agent-workforce-supervisor 페르소나(hermes/config.yaml 원문) - Scorecard
                      수치를 못 바꾸고 서술만 만든다
  notion_report       Reporter Node(결정론, LLM 아님) - Scorecard를 Notion HR DB(NOTION_HR_DB)에
                      Projection으로 올린다. 실패해도 Scorecard 값은 못 바꾼다

build_department_scorecard()에는 department 단위 budget 판정(OK/WARNING/EXCEEDED)이 없다 -
그건 assess_budget()이 Agent 단위로만 낸다(cost.py 참고). 여기서 department 단위 판정을 새로
만들지 않는다(Notion 설계서 원칙: 코드에 없는 판정 값을 만들지 않는다).

이번 버전에는 없는 것(의도적 축소, 후속 작업):
  - harness/journal.py 급 실행 증거 기록(Risk/QA는 있음).
  - asyncpg Repository - capacity_snapshots/cost_snapshots 조회는 여전히 호출자가 채운다
    (workforce-api 미구현, PROJECT_IMPLEMENTATION_STATUS.md HR-01 선행 작업).
  - 채용 후보(hiring_requests/candidates) Notion 연동 - 판정 엔진이 생긴 뒤에 붙인다(YAGNI).

원칙 (전 노드 공통, CLAUDE.md):
  - cost/capacity/quality 수치는 build_department_scorecard 결과에서만 온다. LLM은 그 값을
    못 바꾸고 서술만 만든다.
  - LLM 주소는 Hermes AIAgent(run_agent) Lazy Import다.

실행:
  python scripts.py               # 자체 점검 (Hermes·Notion 없음)
  python scripts.py --run         # 데모 Snapshot으로 실제 실행 (Hermes 필요)
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

_BASE = Path(__file__).resolve().parent
_REPO_ROOT = _BASE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_SCORECARD_DIR = _BASE / "scorecard"
sys.path.insert(0, str(_SCORECARD_DIR))

from langgraph.graph import END, StateGraph  # noqa: E402

from cost import CapacitySnapshot, CostSnapshot, build_department_scorecard  # noqa: E402
from reporting import evaluation_metrics, json_cell, langsmith_handoff, md_cell  # noqa: E402

PIPELINE_VERSION = "agent-workforce-pipeline-v1"


class HRState(TypedDict, total=False):
    department_code: str
    window_start: str               # ISO datetime
    window_end: str
    capacity_input: dict | None      # CapacitySnapshot 필드 dict | None
    cost_snapshots_input: list[dict]  # CostSnapshot 필드 dict 목록
    finding_count: int | None
    rework_rate: str | None          # Decimal 문자열
    trace_id: str
    scorecard: dict                  # build_department_scorecard 반환값
    narrative: str
    escalate: bool
    notion_upload: dict | None
    report_markdown: str
    evaluation: dict
    observability: dict


def _capacity(d: dict | None) -> CapacitySnapshot | None:
    if d is None:
        return None
    return CapacitySnapshot(
        window_start=datetime.fromisoformat(d["window_start"]),
        window_end=datetime.fromisoformat(d["window_end"]),
        arrivals=d.get("arrivals", 0),
        queue_p95_ms=_dec(d.get("queue_p95_ms")),
        duration_p95_ms=_dec(d.get("duration_p95_ms")),
        retry_rate=_dec(d.get("retry_rate")),
        error_rate=_dec(d.get("error_rate")),
        utilization=_dec(d.get("utilization")),
        department_id=d.get("department_id"),
        agent_id=d.get("agent_id"),
    )


def _dec(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _cost_snapshot(d: dict) -> CostSnapshot:
    return CostSnapshot(
        agent_id=d["agent_id"], profile_version_id=d["profile_version_id"],
        window_start=datetime.fromisoformat(d["window_start"]),
        window_end=datetime.fromisoformat(d["window_end"]),
        input_tokens=d.get("input_tokens", 0), output_tokens=d.get("output_tokens", 0),
        model_cost=Decimal(str(d.get("model_cost", "0"))), tool_cost=Decimal(str(d.get("tool_cost", "0"))),
        infra_cost=Decimal(str(d.get("infra_cost", "0"))), case_count=d.get("case_count", 0),
        currency=d.get("currency", "USD"),
    )


# ── 노드 1: Scorecard 집계 (결정론 직원 - build_department_scorecard) ──────
def score_department(state: HRState) -> dict:
    capacity = _capacity(state.get("capacity_input"))
    cost_snapshots = [_cost_snapshot(d) for d in state.get("cost_snapshots_input") or []]
    rework = state.get("rework_rate")
    scorecard = build_department_scorecard(
        department_code=state["department_code"],
        window_start=datetime.fromisoformat(state["window_start"]),
        window_end=datetime.fromisoformat(state["window_end"]),
        capacity=capacity, cost_snapshots=cost_snapshots,
        finding_count=state.get("finding_count"),
        rework_rate=_dec(rework),
    )
    return {"scorecard": scorecard}


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
    sc = state["scorecard"]
    # Snapshot이 전부 없으면(cost=capacity=None) 서술할 근거 자체가 없다 - 비싼 호출 없이
    # 바로 결정론 서술로 끝낸다(cost.py 불변식 3: 데이터 없음을 0으로 채우지 않는다는 원칙의 연장).
    if sc.get("cost") is None and sc.get("capacity") is None and chat is None:
        return {"narrative": (f"{state['department_code']}의 이번 창에는 Cost·Capacity Snapshot이 "
                              "없습니다. 측정 누락인지 실제로 활동이 없었는지 확인이 필요합니다."),
                "escalate": True}

    task = f"""Using ONLY the evidence below, write a 2-4 sentence Korean summary of this
department's weekly Workforce Scorecard for the CEO/HR review. You cannot change any of the
numbers - you only interpret and flag concerns (e.g. missing snapshots, high finding_count).
Schema (JSON only):
{{"narrative": "2-4 sentences in Korean", "escalate": true or false}}

Evidence:
{json.dumps(sc, ensure_ascii=False, indent=1, default=str)}"""

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
    trace_id = state.get("trace_id") or f"{PIPELINE_VERSION}:{state['department_code']}"
    sc = state["scorecard"]
    return {
        "department_code": state["department_code"], "window": sc["window"],
        "capacity": sc.get("capacity"), "cost": sc.get("cost"), "quality": sc.get("quality"),
        "trace_id": state["trace_id"], "narrative": state["narrative"], "escalate": state["escalate"],
        "observability": langsmith_handoff(str(trace_id)),
    }


def notion_report(state: HRState, *, uploader=None) -> dict:
    from notion_reporter import upload_scorecard

    out = _assemble_out(state)
    report_md = _render_report_md(out)
    evaluation = evaluation_metrics(out, report_md)
    upload = uploader or upload_scorecard
    try:
        notion_upload = upload(out, report_md=report_md)
    except Exception as exc:  # noqa: BLE001 - self-check preserves fail-closed behavior.
        notion_upload = {"ok": False, "reason": f"Reporter 예외: {type(exc).__name__}"}
        evaluation["fallback_count"] = evaluation.get("fallback_count", 0) + 1
    evaluation = evaluation_metrics({**out, "notion_upload": notion_upload}, report_md)
    return {"notion_upload": notion_upload, "report_markdown": report_md, "evaluation": evaluation}


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def build_pipeline():
    g = StateGraph(HRState)
    g.add_node("score_department", score_department)
    g.add_node("narrate", narrate)
    g.add_node("notion_report", notion_report)
    g.set_entry_point("score_department")
    g.add_edge("score_department", "narrate")
    g.add_edge("narrate", "notion_report")
    g.add_edge("notion_report", END)
    return g.compile()


def run_hr_department(
    *, department_code: str, window_start: str, window_end: str,
    capacity_input: dict | None = None, cost_snapshots_input: list[dict] | None = None,
    finding_count: int | None = None, rework_rate: str | None = None, trace_id: str,
) -> dict:
    """본부 단독 실행 - Risk/QA/Research의 run_<dept>_department와 같은 외부 인터페이스."""
    out = build_pipeline().invoke({
        "department_code": department_code, "window_start": window_start, "window_end": window_end,
        "capacity_input": capacity_input, "cost_snapshots_input": cost_snapshots_input or [],
        "finding_count": finding_count, "rework_rate": rework_rate, "trace_id": trace_id,
    })
    result = _assemble_out(out)
    result["notion_upload"] = out.get("notion_upload")
    result["report_markdown"] = out.get("report_markdown", "")
    result["evaluation"] = out.get("evaluation", evaluation_metrics(result))
    return result


# ── MD 리포트 렌더 (결정론 - LLM 미개입, out을 그대로 옮기기만 한다) ─────────
def _render_report_md(out: dict) -> str:
    cost = out.get("cost") or {}
    capacity = out.get("capacity") or {}
    quality = out.get("quality") or {}
    lines = [
        "# Agent Workforce 인사팀 — Weekly Scorecard (결정론적 생성, LLM 자유 서술 아님)",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| **department_code** | `{out.get('department_code')}` |",
        f"| **window** | {md_cell((out.get('window') or {}).get('window_start'))} ~ "
        f"{md_cell((out.get('window') or {}).get('window_end'))} |",
        f"| **escalate** | {md_cell(out.get('escalate'))} |",
        f"| **생성** | {PIPELINE_VERSION}, {datetime.now(timezone.utc).isoformat()} |",
        "",
        "---",
        "",
        "## Cost",
        "",
    ]
    if cost:
        lines += ["| 필드 | 값 |", "|---|---|"]
        lines += [f"| {md_cell(k)} | {json_cell(v)} |" for k, v in cost.items()]
    else:
        lines.append("Cost Snapshot 없음 (측정 누락일 수 있음 - 0이 아니다)")

    lines += ["", "## Capacity", ""]
    if capacity:
        lines += ["| 필드 | 값 |", "|---|---|"]
        lines += [f"| {md_cell(k)} | {json_cell(v)} |" for k, v in capacity.items()]
    else:
        lines.append("Capacity Snapshot 없음")

    lines += ["", "## Quality", ""]
    lines += ["| 필드 | 값 |", "|---|---|"]
    lines += [f"| {md_cell(k)} | {json_cell(v)} |" for k, v in quality.items()]

    lines += ["", "## 종합 서술 (agent-workforce-supervisor, Hermes)", "", md_cell(out.get("narrative"))]

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
        "> 이 문서는 build_department_scorecard의 결정론적 집계와 스키마 검증된 LLM 서술을",
        "> Python이 그대로 옮긴 것이다 - LLM이 수치를 창작하지 않았다.",
    ]
    return "\n".join(lines)


# ── 자체 점검 (Hermes·Notion 없음) ──────────────────────────────────────────
def _check_graph_shape():
    p = build_pipeline()
    assert p is not None
    print("  그래프 컴파일              OK")


def _demo_cost_snapshot(**over) -> dict:
    base = dict(agent_id="a1", profile_version_id="pv1", window_start="2026-07-24T00:00:00+00:00",
                window_end="2026-07-31T00:00:00+00:00", input_tokens=200, output_tokens=200,
                model_cost="1.5", case_count=5, currency="USD")
    base.update(over)
    return base


def _check_missing_snapshots_short_circuit():
    # Cost·Capacity 둘 다 없으면 narrate가 LLM을 안 부르고 결정론 서술로 끝나는지.
    out = build_pipeline().invoke({
        "department_code": "07-agent-workforce", "window_start": "2026-07-24T00:00:00+00:00",
        "window_end": "2026-07-31T00:00:00+00:00", "capacity_input": None,
        "cost_snapshots_input": [], "trace_id": "t1",
    })
    assert out["scorecard"]["cost"] is None and out["scorecard"]["capacity"] is None
    assert out["escalate"] is True
    assert "Snapshot" in out["narrative"]
    print("  Snapshot 없음 조기 종료     OK")


def _check_narrate_schema_guard():
    bad_chat = lambda persona, task: '{"narrative": "n"}'  # escalate 누락
    state = {"department_code": "07-agent-workforce",
              "scorecard": {"department_code": "07-agent-workforce", "window": {},
                            "capacity": None, "cost": {"case_count": 1}, "quality": {}}}
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

    state = {"department_code": "07-agent-workforce", "trace_id": "t1", "narrative": "n",
              "escalate": False,
              "scorecard": {"department_code": "07-agent-workforce",
                            "window": {"window_start": "2026-07-24T00:00:00Z",
                                      "window_end": "2026-07-31T00:00:00Z"},
                            "capacity": None, "cost": {"case_count": 5}, "quality": {}}}
    result = notion_report(state, uploader=stub_uploader)
    assert result["notion_upload"] == {"ok": True, "url": "https://notion.so/fake"}
    assert result["report_markdown"]
    assert captured["out"]["department_code"] == "07-agent-workforce"
    print("  Notion Reporter 노드        OK")


def _check_report_renderer_purity():
    out = {"department_code": "07-agent-workforce",
           "window": {"window_start": "2026-07-24T00:00:00Z", "window_end": "2026-07-31T00:00:00Z"},
           "cost": {"case_count": 5}, "capacity": None, "quality": {"finding_count": 0},
           "escalate": False, "narrative": "요약"}
    a = _render_report_md(out)
    b = _render_report_md(out)
    assert a == b, "같은 입력이 다른 리포트를 냈다 - 순수성 위반"
    assert "07-agent-workforce" in a and "요약" in a
    print("  리포트 렌더러 순수성       OK")


def _check_full_pipeline_invoke():
    called = []
    fake_chat = lambda persona, task: called.append("narrate") or json.dumps(
        {"narrative": "이번 주 인사팀 지표는 정상입니다.", "escalate": False})
    fake_uploader = lambda out, *, report_md="": called.append("notion") or {
        "ok": False, "reason": "self-check stub"}

    global narrate, notion_report
    orig_narrate, orig_notion = narrate, notion_report
    narrate = lambda s: orig_narrate(s, chat=fake_chat)
    notion_report = lambda s: orig_notion(s, uploader=fake_uploader)
    try:
        out = build_pipeline().invoke({
            "department_code": "07-agent-workforce", "window_start": "2026-07-24T00:00:00+00:00",
            "window_end": "2026-07-31T00:00:00+00:00", "capacity_input": None,
            "cost_snapshots_input": [_demo_cost_snapshot()], "trace_id": "t1",
        })
        assert called == ["narrate", "notion"], called
        assert out["scorecard"]["cost"]["case_count"] == 5
        assert out["notion_upload"] == {"ok": False, "reason": "self-check stub"}
    finally:
        narrate, notion_report = orig_narrate, orig_notion
    print("  전체 파이프라인 배선       OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        print(f"{PIPELINE_VERSION} 실행 (데모 Snapshot)")
        out = run_hr_department(
            department_code="07-agent-workforce", window_start="2026-07-24T00:00:00+00:00",
            window_end="2026-07-31T00:00:00+00:00", capacity_input=None,
            cost_snapshots_input=[_demo_cost_snapshot()],
            trace_id=hashlib.sha256(b"hr-demo").hexdigest()[:16],
        )
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        report_dir = _BASE / "reports"
        report_dir.mkdir(exist_ok=True)
        rp = report_dir / f"hr_scorecard_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.md"
        rp.write_text(_render_report_md(out), encoding="utf-8")
        print(f"결정론적 MD 리포트 저장: {rp}")
        raise SystemExit(0)

    print(f"{PIPELINE_VERSION} 자체 점검 (Hermes·Notion 없음)")
    _check_graph_shape()
    _check_missing_snapshots_short_circuit()
    _check_narrate_schema_guard()
    _check_notion_report_node()
    _check_report_renderer_purity()
    _check_full_pipeline_invoke()
    print("본부 파이프라인 6개 영역 통과. 실행은 --run (Hermes 필요)")
