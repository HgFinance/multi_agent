#!/usr/bin/env python3
"""리서치본부 LangGraph 파이프라인 - run_research_department(symbol) -> Research Packet.

담당: 재일 (리서치/퀀트)
근거: 동규님의 QA 부서 패턴(departments/06-ai-qa-audit/scripts.py - LangGraph
      실무진 + 부서장 정제)을 우리 본부에 적용 (재일님 지시 2026-07-31).

QA 패턴과의 차이 - **노드가 프롬프트가 아니라 실구현 직원이다**:
  check_universe      universe_manager (결정론 - LS 공식 목록, 실전 검증 347/3)
  assemble_evidence   Evidence Bundle 조립기 (읽기 전용 API 2종에서 수집 - 결정론)
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
sys.path.insert(0, str(_BASE / "collectors"))
sys.path.insert(0, str(_BASE / "agents"))

from langgraph.graph import END, StateGraph  # noqa: E402

PIPELINE_VERSION = "research-department-pipeline-v1"
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


# ── 노드 2: Evidence Bundle 조립 (결정론 - API 만) ─────────────────────────
def assemble_evidence(state: ResearchState) -> dict:
    sym = state["symbol"]
    bars = _get(f"{MARKET_API}/bars/{sym}?interval=1D&limit=5")
    snap = _get(f"{MARKET_API}/snapshot/{sym}")
    news = _get(f"{RESEARCH_API}/evidence/news?symbol={sym}&hours=24&limit=8")
    disc = _get(f"{RESEARCH_API}/evidence/disclosures?symbol={sym}&days=7&limit=5")
    return {"evidence": {
        "daily_closes_recent": [(b["bucket_time"][:10], float(b["close"])) for b in bars],
        "last_trade": snap.get("last_trade"),
        "news_headlines": [{"id": i + 1, "title": n["title"],
                            "relation": n["relation_type"]}
                           for i, n in enumerate(news)],
        "disclosures_7d": [d_["title"] for d_ in disc],
    }}


# ── 노드 3: Sentiment (검증된 LLM 직원) ────────────────────────────────────
def analyze_sentiment(state: ResearchState) -> dict:
    from news_sentiment_analyst import run as senti_run

    r = senti_run(state["symbol"], hours=24.0, read_bodies=False)
    return {"sentiment": {"verdict": r.verdict, "score": r.score,
                          "articles": r.articles_used,
                          "dropped": r.articles_dropped}}


# ── 노드 4: Packet 초안 (supervisor 페르소나 - config.yaml 원문) ───────────
def _supervisor_persona() -> str:
    cfg = (_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8")
    return re.search(r'research-supervisor: "(.*?)"\n', cfg, re.S).group(1)


def draft_packet(state: ResearchState, *, llm=None) -> dict:
    bundle = {"symbol": state["symbol"], "universe": state["universe"],
              "sentiment": state["sentiment"], **state["evidence"]}
    task = f"""Using ONLY the evidence below, draft a compact Research Packet in Korean.
Schema (JSON only):
{{"symbol": "...", "thesis": "1-2 sentences, facts first",
 "facts": ["cited facts only - reference ids like news:3"],
 "interpretation": ["clearly separated from facts"],
 "catalysts": ["upcoming checkpoints"],
 "invalidation": ["conditions that would kill the thesis"],
 "evidence_quality": "sufficient | partial | insufficient_evidence"}}

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
    g.add_node("draft_packet", draft_packet)
    g.set_entry_point("check_universe")
    # 거래 불가면 즉시 종료 - 죽은 종목에 분석 비용을 쓰지 않는다
    g.add_conditional_edges("check_universe",
                            lambda s: "END" if s.get("halted") else "go",
                            {"END": END, "go": "assemble_evidence"})
    g.add_edge("assemble_evidence", "analyze_sentiment")
    g.add_edge("analyze_sentiment", "draft_packet")
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
    print("본부 파이프라인 3개 영역 통과. 실행은 --run <종목>")
