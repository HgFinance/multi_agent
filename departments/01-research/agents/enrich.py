#!/usr/bin/env python3
"""분석가 보강 계층 - 도구를 불러 readout 을 넓히고 근거를 붙인다.

담당: 재일 (리서치본부)
근거: RESEARCH_QUANT_AGENTIC_FRAMEWORK.md 6.1절 3단계(Query Plan)·6.2절,
      RESEARCH_OUTPUT_ADVANCEMENT_STRATEGY.md 4.1절
      재일님 지시 2026-08-03 "도구 배선도 진행"

▶ 왜 분석가마다 고치지 않는가
  6인 각자에 도구 루프를 넣으면 같은 코드가 6벌이 되고, 그중 하나만 고쳐지는
  날이 반드시 온다(scripts.py 의 note/summary 분기가 정확히 그렇게 갈라져
  서술이 100% 폐기됐다). 보강은 **한 곳**에서 한다.

▶ 무엇을 하는가
  1. 페르소나의 도구 상자를 만든다(권한은 config.yaml 이 정본)
  2. **코드가 정한 계획**대로 부른다 - 무엇을 부를지 LLM 에게 묻지 않는다.
     각 분석가의 도구는 1~2개이고 "언제 부르는가"가 이미 결정론이다.
     LLM 에게 물으면 도구 선택이 비결정적이 되고 재현이 깨진다.
  3. 가져온 수치를 readout 에 **접두사와 함께** 합친다(tool.키). 접두사가
     있어야 코드 계산과 도구 조회를 나중에 구분할 수 있다.
  4. 근거 ID 와 호출 흔적을 남긴다 - 무엇을 부르고 무엇을 안 불렀는지.

▶ 실패는 분석을 죽이지 않는다
  도구가 실패해도 원래 readout 은 그대로다. 보강은 더하기이지 대체가 아니다.
  다만 실패 사실은 tool_trace 에 남는다 - 조용히 비면 도구가 도는 줄 안다.

실행: python agents/enrich.py     # 자체 점검 (네트워크 없음)
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "evidence"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

ENRICH_VERSION = "research-enrich-v1"

# 분석가 -> (페르소나, [(도구, 인자 만드는 법)])
# **코드가 정한 계획이다.** 무엇을 부를지 LLM 에게 묻지 않는다 - 각 분석가의
# 도구는 1~2개이고 언제 부르는지가 이미 결정론이라, 물으면 재현만 깨진다.
PLANS: dict[str, tuple[str, tuple[tuple[str, dict], ...]]] = {
    "fundamental": ("fundamental-analyst", (
        # 한 시점 비교로는 추세를 말할 수 없다
        ("financial_history", {"limit": 24}),
        # 재무제표만으로 설명 안 되는 변화의 배경
        ("disclosure_detail", {"days": 30}),
    )),
    "sentiment": ("news-sentiment-analyst", (
        # 단일 매체를 사실로 올리지 않기 위해
        ("story_cluster", {"hours": 48.0}),
    )),
    "technical": ("technical-analyst", (
        # 20일 창으로는 장기 추세와의 충돌을 못 본다
        ("price_history", {"limit": 120}),
    )),
    "regime": ("sector-regime-analyst", (
        # 하루치 값은 레짐이 아니다
        ("breadth_history", {"days": 60}),
    )),
    "microstructure": ("microstructure-analyst", (
        # 비교 기준 없는 STRESSED 는 약한 근거다
        ("micro_history", {}),
    )),
}

# 종목 인자가 필요한 도구. 없는 도구(breadth_history)에 symbol 을 넘기면 죽는다.
_NEEDS_SYMBOL = {"financial_history", "disclosure_detail", "story_cluster",
                 "news_window", "price_history", "micro_history"}


def enrich(node: str, result: dict, *, symbol: str, run_mode: str = "LIVE",
           runners: dict | None = None, max_rounds: int = 2) -> dict:
    """분석가 결과에 도구 조회를 더한다. **원래 결과는 보존한다.**

    반환은 result 의 얕은 복사 + readout 확장 + tool_evidence/tool_trace.
    """
    plan = PLANS.get(node)
    if not plan:
        return result
    persona, steps = plan

    if runners is None:
        from tool_runners import ALL_RUNNERS

        runners = ALL_RUNNERS

    from analyst_toolbox import BudgetExceeded, build_toolbox

    box = build_toolbox(persona, runners, max_rounds=max_rounds)
    if not box.specs:
        # 권한이 없거나 구현이 없다 - 조용히 넘어가지 않고 흔적을 남긴다
        out = dict(result)
        out["tool_trace"] = {"persona": persona, "available": [], "called": [],
                             "reason": "허용된 도구가 없다(config.yaml 또는 러너 부재)"}
        return out

    for label, kwargs in steps:
        kw = dict(kwargs)
        if label in _NEEDS_SYMBOL:
            kw["symbol"] = symbol
        kw["run_mode"] = run_mode
        try:
            box.call(label, **kw)
        except BudgetExceeded:
            break          # 예산은 한계이지 오류가 아니다
        except Exception:  # noqa: BLE001, S112 - 권한·PIT 위반은 trace 에 남는다
            continue

    out = dict(result)
    gained = box.retrieved_numbers()
    if gained:
        # readout 을 **덮지 않고 더한다.** 접두사(tool.키)가 있어 코드 계산과
        # 도구 조회를 나중에 구분할 수 있다 - 같은 이름이 겹쳐도 안 섞인다.
        out["readout"] = {**(result.get("readout") or {}), **gained}
    refs = box.retrieved_refs()
    if refs:
        # 이미 있는 인용을 지우지 않는다(RES-06 은 자기 인용을 갖고 온다)
        prev = tuple(result.get("cited_evidence_ids") or ())
        out["cited_evidence_ids"] = tuple(dict.fromkeys(prev + refs))
    out["tool_trace"] = box.trace()
    return out


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음
# ---------------------------------------------------------------------------

def _runner(label, *, ok=True, nums=None, refs=()):
    from analyst_toolbox import ToolResult

    def run(**kw):
        return ToolResult(tool=label, ok=ok, data={"kw": kw},
                          numbers=nums or {}, evidence_refs=refs,
                          reason="" if ok else "시험 실패")
    return run


_ALLOW = {
    "fundamental-analyst": ("research.evidence.financials.read",
                            "research.evidence.disclosures.read"),
    "news-sentiment-analyst": ("research.evidence.stories.read",),
    "technical-analyst": ("market.bars.read",),
}


def _check_plans_match_toolbox():
    """계획이 요구하는 도구가 페르소나에게 실제로 배정돼 있는가."""
    from analyst_toolbox import PERSONA_TOOLS

    bad = []
    for node, (persona, steps) in PLANS.items():
        labels = {lbl for _, lbl, *_ in PERSONA_TOOLS.get(persona, ())}
        for label, _ in steps:
            if label not in labels:
                bad.append(f"{node}({persona}) -> {label}")
    assert not bad, f"계획이 없는 도구를 부른다: {bad}"
    print(f"  계획 <-> 배정 일치({len(PLANS)}) OK")


def _check_readout_is_extended_not_replaced():
    r = {"verdict": "NOTED", "readout": {"부채비율_pct": 188.95}}
    out = enrich("fundamental", r, symbol="005380", runners={
        "financial_history": _runner("financial_history",
                                     nums={"매출액_기간변화_pct": 12.3}),
        "disclosure_detail": _runner("disclosure_detail", nums={"공시건수": 4.0},
                                     refs=("d1", "d2")),
    })
    # 원래 수치가 살아 있다
    assert out["readout"]["부채비율_pct"] == 188.95
    # 도구 수치가 접두사와 함께 들어왔다
    assert out["readout"]["financial_history.매출액_기간변화_pct"] == 12.3, out["readout"]
    assert out["readout"]["disclosure_detail.공시건수"] == 4.0
    assert out["cited_evidence_ids"] == ("d1", "d2")
    # 원본은 안 바뀐다
    assert "financial_history.매출액_기간변화_pct" not in r["readout"]
    print("  readout 확장(보존)       OK")


def _check_existing_citations_kept():
    """RES-06 은 자기 인용을 갖고 온다 - 지우지 않는다."""
    r = {"verdict": "SCORED", "readout": {}, "cited_evidence_ids": ("own-1",)}
    out = enrich("sentiment", r, symbol="005380", runners={
        "story_cluster": _runner("story_cluster", nums={"스토리수": 22.0},
                                 refs=("own-1", "new-1"))})
    assert out["cited_evidence_ids"] == ("own-1", "new-1"), out["cited_evidence_ids"]
    print("  기존 인용 보존           OK")


def _check_failure_does_not_kill_analysis():
    r = {"verdict": "NEUTRAL", "readout": {"rsi": 62.1}}
    out = enrich("technical", r, symbol="005380", runners={
        "price_history": _runner("price_history", ok=False)})
    assert out["readout"] == {"rsi": 62.1}, "실패가 원래 결과를 건드렸다"
    assert out["tool_trace"]["failures"], "실패가 흔적 없이 사라졌다"
    print("  실패가 분석을 안 죽인다  OK")


def _check_no_tools_leaves_trace():
    """도구가 없어도 조용히 넘어가지 않는다 - 도는 줄 알면 안 된다."""
    r = {"verdict": "NOTED", "readout": {}}
    out = enrich("fundamental", r, symbol="005380", runners={})
    assert out["tool_trace"]["called"] == []
    assert "도구가 없다" in out["tool_trace"]["reason"], out["tool_trace"]
    print("  도구 부재도 기록         OK")


def _check_unknown_node_untouched():
    r = {"verdict": "ELEVATED", "readout": {"gpr": 195.9}}
    assert enrich("geopolitical", r, symbol="005380", runners={}) is not None
    # 계획에 없는 노드는 그대로 반환한다(지어내지 않는다)
    assert enrich("없는노드", r, symbol="005380") is r
    print("  계획 없는 노드 무변경    OK")


def _check_symbol_only_where_needed():
    """symbol 을 안 받는 도구에 넘기면 죽는다 - 인자를 구분한다."""
    seen = {}

    def run(**kw):
        from analyst_toolbox import ToolResult
        seen.update(kw)
        return ToolResult(tool="breadth_history", ok=True, numbers={"x": 1.0})

    enrich("regime", {"readout": {}}, symbol="005380",
           runners={"breadth_history": run})
    assert "symbol" not in seen, seen
    assert seen.get("days") == 60 and seen.get("run_mode") == "LIVE"
    print("  인자 구분(symbol)        OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{ENRICH_VERSION} 자체 점검 (네트워크 없음)")
    _check_plans_match_toolbox()
    _check_readout_is_extended_not_replaced()
    _check_existing_citations_kept()
    _check_failure_does_not_kill_analysis()
    _check_no_tools_leaves_trace()
    _check_unknown_node_untouched()
    _check_symbol_only_where_needed()
    print("분석가 보강 계층 7개 영역 통과.")
