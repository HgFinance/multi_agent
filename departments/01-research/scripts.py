#!/usr/bin/env python3
"""리서치본부 LangGraph 파이프라인 - run_research_department(symbol) -> Research Packet.

담당: 재일 (리서치/퀀트)
근거: 동규님의 QA 부서 패턴(departments/06-ai-qa-audit/scripts.py - LangGraph
      실무진 + 부서장 정제)을 우리 본부에 적용 (재일님 지시 2026-07-31).

QA 패턴과의 차이 - **노드가 프롬프트가 아니라 실구현 직원이다**:
  check_universe      universe_manager (결정론 - LS 공식 목록, 실전 검증 347/3)
  assemble_evidence   evidence/bundle.py 조립기 (읽기 전용 API 2종 + 결정론
                      price_context - qwen 이 +27% 급등을 하락으로 서술한 실측
                      사고 후 등락률 계산을 코드로 이관)
  analyze_sentiment   news_sentiment_analyst (로컬 LLM + 환각 검증, 10/10 실측)
  draft_packet        research-supervisor 페르소나 (hermes/config.yaml 원문 사용)

원칙 (전 노드 공통):
  - LLM 은 판단·서술만. 수치·필터·검증은 코드가 한다.
  - 에이전트는 DB 를 모른다 - research-api/market-api 만 호출한다.
  - 근거 부족은 insufficient_evidence 로 끝낸다. 지어내지 않는다.
  - LLM 주소는 환경변수(OLLAMA_BASE_URL)다 - 특정 PC 주소를 하드코딩하지 않는다.

실행:
  python scripts.py               # 자체 점검 (LLM·API 없음)
  python scripts.py --run 005490  # 실제 Packet 생성
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import timedelta, timezone
from pathlib import Path
from typing import Optional, TypedDict

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))  # evidence 패키지 - 임포트 실행에서도 찾도록
sys.path.insert(0, str(_BASE / "collectors"))
sys.path.insert(0, str(_BASE / "agents"))

from langgraph.graph import END, StateGraph  # noqa: E402

PIPELINE_VERSION = "research-department-pipeline-v2"  # v2: 분석가 3인 통합 + 수치 가드
KST = timezone(timedelta(hours=9))

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
SUPERVISOR_MODEL = os.environ.get("RESEARCH_SUPERVISOR_MODEL", "qwen3:14b")
MARKET_API = os.environ.get("MARKET_API_URL", "http://127.0.0.1:8036")
RESEARCH_API = os.environ.get("RESEARCH_API_URL", "http://127.0.0.1:8035")


class ResearchState(TypedDict, total=False):
    symbol: str
    universe: dict          # 결정론 판정 결과
    evidence: dict          # Evidence Bundle (API 수집분)
    sentiment: dict         # 검증된 sentiment 요약
    technical: dict         # RES-04 기술적 소견 (결정론 readout + 검증된 서술)
    fundamental: dict       # RES-05 펀더멘털 소견 (결정론 readout + 검증된 서술)
    packet: dict            # 최종 Research Packet
    halted: str             # 중단 사유 (거래 불가 등)


def _get(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


# ── 노드 1: Universe 판정 (결정론 직원) ────────────────────────────────────
def check_universe(state: ResearchState) -> dict:
    from universe_manager import run as universe_run

    d = universe_run(basket=(state["symbol"],))
    if state["symbol"] in d.excluded:
        return {"universe": {"tradable": False, "reason": d.excluded[state["symbol"]]},
                "halted": f"거래 불가({d.excluded[state['symbol']]}) - 분석 중단"}
    return {"universe": {"tradable": True, "as_of": d.as_of.isoformat()}}


# ── 노드 2: Evidence Bundle 조립 (결정론 - evidence/bundle.py) ─────────────
def assemble_evidence(state: ResearchState) -> dict:
    # 수집·등락률 계산은 전부 모듈이 한다 - 노드는 배선만 (계약: 기존 키 유지)
    from evidence.bundle import assemble_bundle

    return {"evidence": assemble_bundle(state["symbol"], market_api=MARKET_API,
                                        research_api=RESEARCH_API, get=_get)}


# ── 노드 3: Sentiment (검증된 LLM 직원) ────────────────────────────────────
def analyze_sentiment(state: ResearchState) -> dict:
    from news_sentiment_analyst import run as senti_run

    r = senti_run(state["symbol"], hours=24.0, read_bodies=False)
    return {"sentiment": {"verdict": r.verdict, "score": r.score,
                          "articles": r.articles_used,
                          "dropped": r.articles_dropped}}


# ── 노드 3b/3c: 기술적·펀더멘털 분석가 (검증된 LLM 직원 + 결정론 계산기) ──
def _norm_note(note):
    """분석가 note 가 pydantic 모델일 수 있다 - 프롬프트 직렬화 가능하게 통일."""
    return note.model_dump() if hasattr(note, "model_dump") else note


def analyze_technical(state: ResearchState) -> dict:
    from technical_analyst import analyze as tech_analyze

    r = tech_analyze(state["symbol"], market_api=MARKET_API)
    return {"technical": {"verdict": r.get("verdict"),
                          "readout": r.get("readout"),
                          "note": _norm_note(r.get("note")),
                          "llm_status": r.get("llm_status"),
                          "reason": r.get("reason")}}


def analyze_fundamental(state: ResearchState) -> dict:
    from fundamental_analyst import analyze as fund_analyze

    r = fund_analyze(state["symbol"])
    return {"fundamental": {"verdict": r.get("verdict"),
                            "readout": r.get("readout"),
                            "note": _norm_note(r.get("note")),
                            "verification": r.get("verification")}}


# ── 노드 4: Packet 초안 (supervisor 페르소나 - config.yaml 원문) ───────────
def _supervisor_persona() -> str:
    cfg = (_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8")
    return re.search(r'research-supervisor: "(.*?)"\n', cfg, re.S).group(1)


def draft_packet(state: ResearchState, *, llm=None) -> dict:
    evidence = dict(state["evidence"])
    # 가격 수치는 코드가 계산한 확정값 - qwen 이 +27% 급등을 하락으로 서술한
    # 실측 사고 재발 방지로, LLM 이 재계산하지 못하게 별도 블록으로 분리 주입
    price_ctx = evidence.pop("price_context", None) or {
        "status": "UNAVAILABLE", "reason": "evidence 에 price_context 가 없다"}
    bundle = {"symbol": state["symbol"], "universe": state["universe"],
              "sentiment": state["sentiment"], **evidence}
    # 분석가 소견 - 각자의 결정론 readout 과 검증 통과한 서술만 온다
    analysts = {
        "technical": state.get("technical") or {"status": "NOT_RUN"},
        "fundamental": state.get("fundamental") or {"status": "NOT_RUN"},
    }
    task = f"""Using ONLY the evidence below, draft a compact Research Packet in Korean.
Schema (JSON only):
{{"symbol": "...", "thesis": "1-2 sentences, facts first",
 "facts": ["cited facts only - reference ids like news:3"],
 "interpretation": ["clearly separated from facts"],
 "catalysts": ["upcoming checkpoints"],
 "invalidation": ["conditions that would kill the thesis"],
 "evidence_quality": "sufficient | partial | insufficient_evidence"}}

CONFIRMED PRICE FIGURES (deterministic, computed by code - NOT by you):
{json.dumps(price_ctx, ensure_ascii=False, indent=1)}
Rules for these figures - non-negotiable:
- Quote them verbatim. Do NOT recompute returns or infer price direction
  yourself from headlines or any other source.
- change_1d_pct > 0 means the price ROSE (상승); < 0 means it FELL (하락).
  Follow the sign exactly.
- If status is UNAVAILABLE or a field is null, write "미확인" - never guess.

CONFIRMED ANALYST FINDINGS (readouts computed by code; narratives already
hallucination-checked - NOT yours to recompute):
{json.dumps(analysts, ensure_ascii=False, indent=1)}
Rules for analyst findings - non-negotiable:
- Quote their verdicts and numbers verbatim; do not invent new figures.
- If analysts DISAGREE (e.g., sentiment positive but technical BEARISH),
  preserve BOTH views side by side in interpretation. Never delete dissent,
  never average it away - state the conflict explicitly.
- NOT_RUN / INSUFFICIENT_DATA / null means 미확인 - never fill the gap.

Evidence:
{json.dumps(bundle, ensure_ascii=False, indent=1)}"""

    call = llm or _ollama_chat
    out = call(_supervisor_persona(), task)
    s, e = out.find("{"), out.rfind("}")
    packet = json.loads(out[s:e + 1])
    # 결정론 가드: 스키마 필수 키와 사실/해석 분리 존재를 코드로 확인한다
    for k in ("symbol", "thesis", "facts", "interpretation", "invalidation",
              "evidence_quality"):
        if k not in packet:
            raise ValueError(f"Packet 에 {k} 가 없다 - 초안 거부")
    packet["evidence_quality"] = str(packet["evidence_quality"]).lower()  # 표기 정규화
    # 결정론 가드 2: 서술 속 % 수치가 확정치(가격+분석가 readout) 밖이면 창작이다
    from evidence.bundle import verify_narrative_numbers

    narrative = " ".join([str(packet.get("thesis", "")),
                          *map(str, packet.get("facts") or []),
                          *map(str, packet.get("interpretation") or [])])
    packet["numeric_check"] = verify_narrative_numbers(
        narrative, {"price": price_ctx, "analysts": analysts})
    return {"packet": packet}


def _ollama_chat(system: str, user: str) -> str:
    req = urllib.request.Request(
        OLLAMA_BASE + "/v1/chat/completions", method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
        data=json.dumps({"model": SUPERVISOR_MODEL, "temperature": 0.2,
                         "messages": [{"role": "system", "content": system},
                                      {"role": "user", "content": user}]}).encode())
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def build_pipeline():
    g = StateGraph(ResearchState)
    g.add_node("check_universe", check_universe)
    g.add_node("assemble_evidence", assemble_evidence)
    g.add_node("analyze_sentiment", analyze_sentiment)
    g.add_node("analyze_technical", analyze_technical)
    g.add_node("analyze_fundamental", analyze_fundamental)
    g.add_node("draft_packet", draft_packet)
    g.set_entry_point("check_universe")
    # 거래 불가면 즉시 종료 - 죽은 종목에 분석 비용을 쓰지 않는다
    g.add_conditional_edges("check_universe",
                            lambda s: "END" if s.get("halted") else "go",
                            {"END": END, "go": "assemble_evidence"})
    # 분석가 3인은 순차다 - GPU 하나에 모델 하나(agent-research 공유)라
    # LLM 호출은 어차피 직렬화된다. 형태만 병렬로 꾸미지 않는다.
    g.add_edge("assemble_evidence", "analyze_sentiment")
    g.add_edge("analyze_sentiment", "analyze_technical")
    g.add_edge("analyze_technical", "analyze_fundamental")
    g.add_edge("analyze_fundamental", "draft_packet")
    g.add_edge("draft_packet", END)
    return g.compile()


def run_research_department(symbol: str) -> dict:
    """본부 단독 실행 - QA 부서의 run_qa_department 와 같은 외부 인터페이스."""
    out = build_pipeline().invoke({"symbol": symbol})
    if out.get("halted"):
        return {"symbol": symbol, "verdict": "HALTED", "reason": out["halted"]}
    return out["packet"]


# ── 자체 점검 (LLM·API 없음) ───────────────────────────────────────────────
def _check_graph_shape():
    p = build_pipeline()
    assert p is not None
    print("  그래프 컴파일            OK")


def _check_halt_short_circuit():
    # 거래 불가 종목이면 Evidence·LLM 을 부르지 않고 끝나는지 - conditional edge
    import universe_manager as um

    class _D:
        excluded = {"999999": "HALTED"}
        def __init__(self): from datetime import datetime; self.as_of = datetime.now(timezone.utc)
    orig = um.run
    um.run = lambda basket=(): _D()
    try:
        out = build_pipeline().invoke({"symbol": "999999"})
        assert out.get("halted"), "거래 불가가 계속 진행됐다"
        assert "packet" not in out
    finally:
        um.run = orig
    print("  거래불가 조기 종료       OK")


def _check_packet_guard():
    bad_llm = lambda s, u: '{"symbol":"X","thesis":"t"}'  # 필수 키 누락
    try:
        draft_packet({"symbol": "X", "universe": {}, "sentiment": {},
                      "evidence": {}}, llm=bad_llm)
        raise AssertionError("불완전 Packet 이 통과했다")
    except ValueError:
        pass
    print("  Packet 스키마 가드       OK")


def _check_price_context_injection():
    # +27% 사고 재발 방지 - 확정 수치 블록과 "재계산 금지" 지시가 프롬프트에
    # 실제로 들어가는지, evidence 에 price_context 가 없으면 UNAVAILABLE 로
    # 명시되는지 코드로 확인한다
    captured = {}

    def fake_llm(system, user):
        captured["user"] = user
        return json.dumps({"symbol": "X", "thesis": "t", "facts": [],
                           "interpretation": [], "catalysts": [],
                           "invalidation": [], "evidence_quality": "partial"})

    draft_packet({"symbol": "X", "universe": {}, "sentiment": {},
                  "evidence": {"price_context": {
                      "status": "OK", "change_1d_pct": 27.0,
                      "direction_1d": "상승"}}}, llm=fake_llm)
    u = captured["user"]
    assert "CONFIRMED PRICE FIGURES" in u and "27.0" in u and "상승" in u
    assert "Do NOT recompute" in u

    draft_packet({"symbol": "X", "universe": {}, "sentiment": {},
                  "evidence": {}}, llm=fake_llm)
    assert '"UNAVAILABLE"' in captured["user"]
    print("  가격 컨텍스트 주입       OK")


def _check_evidence_module():
    # 조립기 모듈 자체 점검을 파이프라인 점검에 포함 - 계약이 함께 지켜지는지
    from evidence import bundle as eb

    eb._check_surge_plus27()
    eb._check_bundle_contract()


def _check_analyst_conflict_and_numeric_guard():
    """상충 보존 지시가 프롬프트에 실제로 들어가고, 서술 속 창작 수치가
    결정론으로 적발되는지 - 다각 분석 통합의 두 가드."""
    captured = {}

    def fake_llm(system, user):
        captured["user"] = user
        return json.dumps({
            "symbol": "X", "thesis": "감성 긍정 대 기술 BEARISH 상충",
            "facts": ["20일 모멘텀 -21.44%"],
            "interpretation": ["창작 수치 +55.5% 상승"],
            "catalysts": [], "invalidation": [], "evidence_quality": "partial"})

    out = draft_packet(
        {"symbol": "X", "universe": {},
         "sentiment": {"verdict": "SCORED", "score": 0.7},
         "technical": {"verdict": "BEARISH",
                       "readout": {"momentum_20d_pct": -21.4449}},
         "fundamental": {"status": "NOT_RUN"},
         "evidence": {"price_context": {"status": "OK", "change_1d_pct": 29.95}}},
        llm=fake_llm)
    u = captured["user"]
    assert "CONFIRMED ANALYST FINDINGS" in u and "BEARISH" in u
    assert "preserve BOTH" in u, "상충 보존 지시가 프롬프트에 없다"
    nc = out["packet"]["numeric_check"]
    assert nc["checked"] == 2 and not nc["ok"], nc
    assert nc["unmatched"] == [55.5], "창작 수치(55.5%)가 적발되지 않았다"
    print("  상충 보존·수치 창작 적발 OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        sym = sys.argv[sys.argv.index("--run") + 1]
        print(f"{PIPELINE_VERSION} 실행: {sym}")
        packet = run_research_department(sym)
        print(json.dumps(packet, ensure_ascii=False, indent=1))
        raise SystemExit(0)

    print(f"{PIPELINE_VERSION} 자체 점검 (LLM·API 없음)")
    _check_graph_shape()
    _check_halt_short_circuit()
    _check_packet_guard()
    _check_price_context_injection()
    _check_evidence_module()
    _check_analyst_conflict_and_numeric_guard()
    print("본부 파이프라인 7개 영역 통과. 실행은 --run <종목>")
