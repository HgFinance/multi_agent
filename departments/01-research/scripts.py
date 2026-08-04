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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, ClassVar, Optional, TypedDict

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))  # evidence 패키지 - 임포트 실행에서도 찾도록
sys.path.insert(0, str(_BASE / "collectors"))
sys.path.insert(0, str(_BASE / "agents"))

from evidence.forecast import (
    falsification_note,
    probability_for_claim,
)
from evidence.highlights import pick_highlights
from evidence.highlights import render_line as render_highlights
from evidence.llm_client import chat as llm_chat
from langgraph.graph import END, StateGraph

PIPELINE_VERSION = "research-department-pipeline-v2"  # v2: 분석가 3인 통합 + 수치 가드
KST = timezone(timedelta(hours=9))

# ▶ .env 를 프로세스 환경으로 올린다 (2026-08-03)
#   실측: .env 에 RESEARCH_SUPERVISOR_BASE/MODEL 을 넣어도 **무시됐다.**
#   이 모듈은 os.environ 만 봤고, .env 는 수집기(source_registry.load_project_env)
#   에서만 읽혔다. 그래서 총괄이 Claude 로 도는 줄 알았는데 실제로는 로컬
#   qwen3:14b 였고, 창작·영어 서술·스키마 이탈이 전부 그 모델에서 났다.
#   **설정이 있는데 조용히 안 먹는 것이 가장 나쁘다** - 무엇이 도는지 착각하게 된다.
#   이미 프로세스에 있는 값은 덮지 않는다(컨테이너 주입이 우선).
try:
    from source_registry import load_project_env as _load_env

    for _k, _v in (_load_env() or {}).items():
        os.environ.setdefault(_k, _v)
except Exception:  # noqa: BLE001, S110 - .env 가 없어도 기본값으로 돈다
    pass

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
# ▶ 총괄은 별도 엔드포인트를 쓸 수 있다 (2026-08-03)
#   실측: pipeline_runs 19회 중 10회가 총괄 스키마 이탈로 실패했다
#   (evidence_quality 에 문장을 넣거나, 키를 통째로 빠뜨리거나, 한 겹 싸거나).
#   qwen3:14b 가 6인 분석가 readout 을 종합하는 이 자리에서 반복적으로 무너진다.
#   RESEARCH_SUPERVISOR_BASE 를 Claude Code 프록시로 돌리면 그 자리만 올릴 수
#   있다 - 분석가 6인은 그대로 로컬 무료다(비용·한도 소모가 예측 가능하다).
SUPERVISOR_MODEL = os.environ.get("RESEARCH_SUPERVISOR_MODEL", "qwen3:14b")
SUPERVISOR_BASE = os.environ.get("RESEARCH_SUPERVISOR_BASE", OLLAMA_BASE).rstrip("/")
MARKET_API = os.environ.get("MARKET_API_URL", "http://127.0.0.1:8036")
RESEARCH_API = os.environ.get("RESEARCH_API_URL", "http://127.0.0.1:8035")


class ResearchState(TypedDict, total=False):
    symbol: str
    universe: dict          # 결정론 판정 결과
    data_quality: dict      # RES-02 수집 품질 게이트 (PASS/WARN/FAIL/UNKNOWN)
    evidence: dict          # Evidence Bundle (API 수집분)
    sentiment: dict         # 검증된 sentiment 요약
    technical: dict         # RES-04 기술적 소견 (결정론 readout + 검증된 서술)
    fundamental: dict       # RES-05 펀더멘털 소견 (결정론 readout + 검증된 서술)
    regime: dict            # RES-07 시장 레짐 (라벨은 코드, 서술만 LLM)
    geopolitical: dict      # RES-09 지정학 국면 (라벨·driver 는 코드, 서술만 LLM)
    microstructure: dict    # RES-03 미시구조 (판정은 코드, 서술만 LLM)
    packet: dict            # 최종 Research Packet
    insights: list          # 교차 해석 (반증 조건 필수)
    insight_rejected: list  # 거부된 해석 + 사유
    insight_claims: list    # 해석의 채점 행
    insight_note: str
    revisions: int          # 반박 -> 재해석 횟수 (상한 MAX_REVISIONS)
    gap_fills: int          # 근거 부족 -> 재수집 횟수 (상한 MAX_GAP_FILLS)
    evidence_gaps: dict     # 분석가별 미확인 항목
    halted: str             # 중단 사유 (거래 불가 등)


def _get(url: str):
    """파이프라인 공용 조회 - Evidence 조립 경로가 쓴다.

    페르소나는 큐레이터(RES-08)다. 뉴스·공시를 모아 분석가에게 넘기는
    큐레이션이 그 직무이고, 총괄은 허용목록에서 "자체 조회 없음"으로
    선언돼 있다(evidence/bundle.py 와 같은 판단).
    """
    sys.path.insert(0, str(_BASE / "evidence"))
    from api_client import get_json

    return get_json(url, persona="rag-librarian-evidence-curator", timeout=30)


# ── 노드 1: Universe 판정 (결정론 직원) ────────────────────────────────────
def check_universe(state: ResearchState) -> dict:
    from universe_manager import run as universe_run

    d = universe_run(basket=(state["symbol"],))
    if state["symbol"] in d.excluded:
        return {"universe": {"tradable": False, "reason": d.excluded[state["symbol"]]},
                "halted": f"거래 불가({d.excluded[state['symbol']]}) - 분석 중단"}
    return {"universe": {"tradable": True, "as_of": d.as_of.isoformat()}}


# ── 노드 1.5: 데이터 품질 게이트 (RES-02 Market Data Steward) ──────────────
# ▶ **P0 페르소나인데 파이프라인 어디에도 없었다.** 감사 구현
#   (collectors/market_data_steward.py)은 있고 market.data_quality_windows 에
#   판정을 쓰는데, 그걸 노출하는 엔드포인트가 없어 아무도 못 읽었다 - Agent 는
#   DB Credential 을 안 받으므로 API 가 없으면 존재하지 않는 것과 같다.
#   그 결과 **데이터 품질 게이트가 Packet 생성 앞에 서 있지 않았다.**
DQ_FAIL_HALTS = True          # FAIL 이면 분석을 안 한다. 나쁜 데이터로 낸 결론은
                              # 없는 결론보다 나쁘다 - 사람이 그걸 믿기 때문이다

# ▶ **분석이 실제로 쓰는 스트림만 막는다.** 첫 실측에서 derivatives 스트림
#   FAIL 때문에 주식 종목(006800) 분석이 통째로 멈췄다 - 우리 분석가 6인 중
#   파생 데이터를 쓰는 사람은 없다. 무관한 장애로 막으면 게이트가 과하고,
#   과한 게이트는 사람이 곧 꺼버린다(가드 오탐과 같은 실패 방식).
#   관련 없는 스트림의 FAIL 은 기록하되 통과시킨다.
EQUITY_STREAMS = ("ticks", "quotes", "bars", "breadth", "index")

# 일봉이 며칠 비면 분석을 멈추는가. 1 은 수집 지연일 수 있으나 2 이상이면
# 배선이 끊긴 것이다 - 그 위에서 낸 판단은 쓸 수 없다.
BAR_STALE_HALT_SESSIONS = 2


def check_data_quality(state: ResearchState) -> dict:
    """수집 품질을 확인하고 나쁘면 막는다. **행 0건은 PASS 가 아니다.**"""
    try:
        rows = _get(f"{MARKET_API}/dq/windows?hours=24")
    except Exception as e:
        # 조회 실패를 통과로 위장하지 않는다. 다만 품질 API 장애가 리서치를
        # 통째로 세우는 것도 과하므로, 미확인으로 남기고 진행한다.
        return {"data_quality": {"status": "UNKNOWN",
                                 "reason": f"품질 조회 실패: {type(e).__name__}"}}
    rows = rows if isinstance(rows, list) else []
    if not rows:
        # 감사가 안 돌았다는 뜻이다. 정상 0 과 구분한다.
        return {"data_quality": {"status": "UNKNOWN",
                                 "reason": "최근 24시간 품질 감사 기록 0건"}}

    def _relevant(r) -> bool:
        st = str(r.get("stream_type") or "").lower()
        return any(k in st for k in EQUITY_STREAMS)

    failed = [r for r in rows if str(r.get("quality_status")) == "FAIL"]
    bad = [r for r in failed if _relevant(r)]          # 우리가 쓰는 스트림만
    unrelated = sorted({str(r.get("stream_type")) for r in failed
                        if not _relevant(r)})
    warn = [r for r in rows if str(r.get("quality_status")) == "WARN"]
    streams = sorted({str(r.get("stream_type")) for r in bad})
    dq = {
        "status": "FAIL" if bad else ("WARN" if warn else "PASS"),
        "windows": len(rows),
        "failed_streams": streams,
        "warned_streams": sorted({str(r.get("stream_type")) for r in warn}),
        # 무관한 스트림 장애도 **숨기지 않는다** - 막지 않을 뿐이다
        "failed_unrelated_streams": unrelated,
        "reason": "; ".join(
            str((r.get("metrics") or {}).get("reasons") or "")[:80] for r in bad[:3]),
    }
    # ▶ **봉 신선도.** 틱 품질만 보면 이게 안 보인다 - 실측(2026-08-04)에서
    #   일봉이 7/31 에 멈춰 있었는데 리포트는 그 종가를 "최신" 으로 인용했고
    #   품질 게이트는 통과시켰다. 분석 전체가 나흘 낡은 가격 위에 서 있었다.
    # ▶ **두 DB 를 건너 센다.** 봉은 TimescaleDB, 거래일 달력은 Supabase 다.
    #   한 쿼리로 join 하려다 UndefinedTable 로 죽었다 - 각자 자기 DB 만 보고
    #   여기서 합친다.
    fr: dict = {"ok": None, "reason": "봉 신선도 미확인"}
    try:
        bars = _get(f"{MARKET_API}/dq/bar_freshness?interval=1D")
        if isinstance(bars, dict) and bars.get("last_bar_date"):
            last = str(bars["last_bar_date"])[:10]
            cal = _get(f"{RESEARCH_API}/calendar/sessions_since?since={last}")
            fr = {"last_bar_date": last, "symbols": bars.get("symbols")}
            if isinstance(cal, dict) and cal.get("sessions") is not None:
                fr["missing_sessions"] = int(cal["sessions"])
                fr["ok"] = fr["missing_sessions"] == 0
            else:
                # 달력을 못 읽으면 **날짜 차이로 대신 세지 않는다** - 주말을
                # 지연으로 세면 월요일마다 오탐이 나고 사람이 가드를 무시한다
                fr["reason"] = "거래일 달력을 읽지 못해 지연 일수를 셀 수 없다"
        elif isinstance(bars, dict):
            fr = dict(bars, missing_sessions=None)
    except Exception as e:
        fr = {"ok": None, "reason": f"봉 신선도 조회 실패: {type(e).__name__}"}
    dq["bar_freshness"] = fr
    missing = fr.get("missing_sessions")
    if isinstance(missing, int) and missing >= BAR_STALE_HALT_SESSIONS:
        # 하루 빠진 것과 나흘 빠진 것은 다르다. 하루는 수집 지연일 수 있으나
        # 여러 날이면 배선이 끊긴 것이고, 그 위에서 낸 판단은 쓸 수 없다.
        return {"data_quality": dict(dq, status="FAIL"),
                "halted": f"일봉이 {fr.get('last_bar_date')} 이후 "
                          f"거래일 {missing}일치 비었다 - 낡은 가격으로 판단하지 않는다"}
    if isinstance(missing, int) and missing > 0:
        dq["status"] = "WARN" if dq["status"] == "PASS" else dq["status"]

    if bad and DQ_FAIL_HALTS:
        return {"data_quality": dq,
                "halted": f"데이터 품질 FAIL - 스트림 {', '.join(streams)} "
                          f"(RES-02 감사). 나쁜 데이터로 낸 판단은 내지 않는다"}
    return {"data_quality": dq}


# ── 노드 2: Evidence Bundle 조립 (결정론 - evidence/bundle.py) ─────────────
def assemble_evidence(state: ResearchState) -> dict:
    # 수집·등락률 계산은 전부 모듈이 한다 - 노드는 배선만 (계약: 기존 키 유지)
    from evidence.bundle import assemble_bundle

    ev = assemble_bundle(state["symbol"], market_api=MARKET_API,
                         research_api=RESEARCH_API, get=_get)
    # RES-08 사서의 공시 원문 발췌(결정론 - 종목 링크 확인 문서의 머리 청크만).
    # 사서 조회 실패는 Packet 전체를 죽일 일이 아니다 - 미확인으로 명시한다.
    try:
        from rag_librarian import recent_excerpts_for_symbol

        ev["disclosure_excerpts"] = recent_excerpts_for_symbol(state["symbol"])
    except Exception as e:  # noqa: BLE001 - intentional fallback boundary
        ev["disclosure_excerpts"] = {"status": "UNAVAILABLE",
                                     "reason": f"{type(e).__name__}: {e}"[:120]}
    return {"evidence": ev}


# ── 노드 3: Sentiment (검증된 LLM 직원) ────────────────────────────────────
def analyze_sentiment(state: ResearchState) -> dict:
    from news_sentiment_analyst import run as senti_run

    # ▶ 본문을 읽는다 (2026-08-03, 재일님 지시 "본문 링크 타고 분석")
    #   article_reader 가 robots.txt 를 지키고 열람 예산·실패를 세며, **본문을
    #   저장하지 않는다**(라이선스). 판정에만 쓰고 파생 점수만 남는다.
    #   as_of 재현(백테스트)에서는 열람 자체를 건너뛴다 - 지금의 웹페이지는
    #   그때의 지면이 아니므로 PIT 가 깨진다(news_sentiment_analyst 가 판정).
    #   창을 48시간으로 넓혔다 - 24시간은 주말·연휴에 재료가 얇아진다.
    r = senti_run(state["symbol"], hours=48.0, read_bodies=True)
    # ▶ 인용을 버리지 않는다 (2026-08-03, RQF-1)
    #   RES-06 은 이미 document_id 단위로 인용하고 환각 인용을 버린다
    #   (verify_and_aggregate). 그런데 파이프라인이 verdict·score 만 들고 와서
    #   **그 인용이 여기서 끊겼다** - Packet 의 주장이 근거를 못 갖는 원인이었다.
    #   원문·본문은 싣지 않는다(라이선스). document_id 와 제목까지다.
    cited = tuple(dict.fromkeys(
        str(e["document_id"]) for e in (r.evidence or ()) if e.get("document_id")))
    base = {"verdict": r.verdict, "score": r.score,
            "articles": r.articles_used, "dropped": r.articles_dropped,
            "cited_evidence_ids": cited,
            "readout": {"articles_used": float(r.articles_used),
                        "articles_dropped": float(r.articles_dropped),
                        **({"score": float(r.score)} if r.score is not None else {})}}
    try:
        from enrich import enrich as _enrich

        base = _enrich("sentiment", base, symbol=state["symbol"])
    except Exception as e:  # noqa: BLE001
        print(f"⚠ sentiment 도구 보강 실패(분석은 유지): "
              f"{type(e).__name__}: {e}"[:150], flush=True)
    return {"sentiment": {**base,
                          "evidence": tuple(
                              {k: e.get(k) for k in
                               ("document_id", "title", "sentiment", "salience")}
                              for e in (r.evidence or ()))}}


# ── 노드 3b/3c: 기술적·펀더멘털 분석가 (검증된 LLM 직원 + 결정론 계산기) ──
def _norm_note(note):
    """분석가 note 가 pydantic 모델일 수 있다 - 프롬프트 직렬화 가능하게 통일."""
    return note.model_dump() if hasattr(note, "model_dump") else note


def _analyst_state(r: dict, *, node: str = "", symbol: str = "") -> dict:
    """분석가 반환 -> state 조각. **반환 모양이 두 갈래인 것을 여기서 흡수한다.**

    ▶ 왜 필요한가 (실측 2026-08-03, 이 세션에서 발견)
      RES-04 기술·RES-07 레짐·RES-03 미시구조는 `summary`/`used_metrics`/
      `cautions`/`dropped` 를 **최상위 평평한 키**로 낸다(technical_analyst.py:361).
      RES-05 펀더멘털만 `note` 객체를 낸다.
      그런데 파이프라인 노드는 전부 `r.get("note")` 만 읽었다. 그래서 세 분석가의
      **검증을 통과한 서술이 매 실행 100% 폐기**됐고, 리포트에는
      `_서술 없음 (LLM 미응답)_` 이 찍혔다 - LLM 은 정상 응답했는데도.
      **거짓 라벨이 3개월치 리포트를 얕아 보이게 만든 진짜 원인이다.**

      서술이 없으면 총괄 프롬프트(_digest)도 그것을 못 보므로, thesis 가 분석가
      근거 위에 서지 못하고 모델의 사전지식으로 흘렀다 - 005380 창작 사고의
      배경이기도 하다.

    두 모양을 다 받아 하나로 낸다. 없는 것을 지어내지는 않는다.
    """
    note = _norm_note(r.get("note")) or {}
    if not isinstance(note, dict):
        note = {}

    def pick(key):
        # note 안이 우선, 없으면 최상위 평평한 키
        v = note.get(key)
        return v if v not in (None, "", [], {}) else r.get(key)

    # ▶ 도구 보강 (2026-08-03). 계획이 있는 노드만 - 없으면 그대로 통과한다.
    #   실패해도 원래 결과를 건드리지 않는다(enrich 가 보장).
    if node and symbol:
        try:
            from enrich import enrich as _enrich

            r = _enrich(node, r, symbol=symbol)
        except Exception as e:  # noqa: BLE001 - 보강 실패가 분석을 죽이지 않는다
            print(f"⚠ {node} 도구 보강 실패(분석은 유지): "
                  f"{type(e).__name__}: {e}"[:150], flush=True)

    return {
        "verdict": r.get("verdict"),
        "readout": r.get("readout"),
        "cited_evidence_ids": tuple(r.get("cited_evidence_ids") or ()),
        "tool_trace": r.get("tool_trace"),
        "note": note or None,
        "summary": pick("summary"),
        "used_metrics": pick("used_metrics") or [],
        "cautions": pick("cautions") or [],
        "dropped": pick("dropped") or [],
        # llm_status 는 서술 유무를 라벨링하는 근거다 - 없으면 추측하지 않는다
        "llm_status": r.get("llm_status"),
        "reason": r.get("reason"),
    }


def analyze_technical(state: ResearchState) -> dict:
    from technical_analyst import analyze as tech_analyze

    r = tech_analyze(state["symbol"], market_api=MARKET_API)
    return {"technical": _analyst_state(r, node="technical", symbol=state["symbol"])}


def analyze_fundamental(state: ResearchState) -> dict:
    from fundamental_analyst import analyze as fund_analyze

    r = fund_analyze(state["symbol"])
    return {"fundamental": {**_analyst_state(r, node="fundamental", symbol=state["symbol"]),
                            "verification": r.get("verification")}}


def analyze_regime(state: ResearchState) -> dict:
    """시장 단면 레짐 - 종목이 아니라 시장 전체의 맥락 (RES-07)."""
    from sector_regime_analyst import analyze as regime_analyze

    r = regime_analyze(market_api=MARKET_API)
    return {"regime": _analyst_state(r, node="regime", symbol=state["symbol"])}


def analyze_geopolitical(state: ResearchState) -> dict:
    """지정학 국면 - 종목도 시장 단면도 아닌 **바깥 환경** (RES-09).

    레짐(RES-07)이 국내 시장 내부 단면이라면 이쪽은 외생 충격이다. 둘 다
    종목 무관이라 결과가 종목별로 달라지지 않는다 - 그래도 매 Packet 마다
    부르는 이유는 as_of 시점 국면이 Packet 의 맥락이기 때문이다.
    """
    from geopolitical_analyst import analyze as geo_analyze

    r = geo_analyze(research_api=RESEARCH_API)
    return {"geopolitical": _analyst_state(r, node="geopolitical", symbol=state["symbol"])}


def analyze_microstructure(state: ResearchState) -> dict:
    from microstructure_analyst import analyze as micro_analyze

    r = micro_analyze(state["symbol"], market_api=MARKET_API)
    return {"microstructure": _analyst_state(r, node="microstructure", symbol=state["symbol"])}


PACKET_REQUIRED_KEYS = ("thesis", "facts", "interpretation",
                        "invalidation", "evidence_quality")


# 총괄이 자주 내는 근접 키 이름 -> 계약 키. **뜻이 명백한 것만** 넣는다 -
# 추측으로 매핑하면 다른 내용을 엉뚱한 필드에 넣게 된다.
# 실측 2026-08-03: invalidation_conditions, key_facts, thesis_statement 로 냈다가
# "missing keys" 로 거부돼 분석가 6인을 돌린 2~3분이 통째로 버려졌다.
_KEY_ALIASES = {
    "invalidation_conditions": "invalidation",
    "invalidations": "invalidation",
    "key_facts": "facts",
    "facts_observed": "facts",
    "thesis_statement": "thesis",
    "interpretations": "interpretation",
    "catalyst": "catalysts",
    "upcoming_catalysts": "catalysts",
    "evidence_quality_assessment": "evidence_quality",
    "data_quality": "evidence_quality",
    "thesis_summary": "thesis",
}


def normalize_packet_keys(obj):
    """근접 키 이름을 계약 키로 바꾼다. **이미 계약 키가 있으면 건드리지 않는다.**"""
    if not isinstance(obj, dict):
        return obj
    out = dict(obj)
    for alias, real in _KEY_ALIASES.items():
        if alias in out and real not in out:
            out[real] = out.pop(alias)
    return out


def unwrap_packet(obj):
    """총괄이 Packet 을 한 겹 감싸 냈으면 벗긴다.

    실측 2026-08-02·08-03: 두 실행이 `missing keys: [전부]` 로 죽었다. 키가
    하나도 없다는 것은 형태가 조금 어긋난 게 아니라 **다른 층을 봤다**는
    뜻이고, 소형·중형 모델의 가장 흔한 이탈이 {"packet": {...}} 처럼 한 겹
    싸는 것이다. 거부하면 분석가 6인을 돌린 2~3분이 통째로 버려지므로,
    확실히 벗길 수 있을 때만 벗긴다 - **추측으로 구조를 바꾸지 않는다.**

    벗기는 조건: 값이 dict 이고 그 안에 필수 키가 **전부** 있을 때만.
    (일부만 있으면 그건 다른 문제이므로 건드리지 않는다.)
    """
    if not isinstance(obj, dict):
        return obj
    obj = normalize_packet_keys(obj)
    if all(k in obj for k in PACKET_REQUIRED_KEYS):
        return obj

    # ▶ 페르소나가 공식 산출물을 **전부** 낸 경우 (실측 2026-08-03, 005380)
    #   RES-00 프롬프트가 "Official outputs: Research Assignment, Research Packet,
    #   Dossier Update, Data Quality Warning" 이라고 4종을 나열한다. Claude 가
    #   그 지시를 성실히 따라 4종을 한 객체로 냈고, 아래 일반 탐색은 그중 어느
    #   것을 골라야 할지 몰라 실패했다(2회 연속 거부).
    #   **이름으로 Packet 을 먼저 찾는다** - 추측이 아니라 프롬프트가 그렇게
    #   부르라고 시킨 이름이다.
    for key in ("research_packet", "Research Packet", "researchPacket", "packet"):
        v = normalize_packet_keys(obj.get(key))
        if isinstance(v, dict) and all(k in v for k in PACKET_REQUIRED_KEYS):
            return v

    for raw in obj.values():
        v = normalize_packet_keys(raw)
        if isinstance(v, dict) and all(k in v for k in PACKET_REQUIRED_KEYS):
            return v
    return obj


def flatten_to_str_list(v) -> list[str] | None:
    """총괄이 낸 값을 문자열 목록으로 평탄화. 불가능하면 None(재시도).

    실측 2026-08-02: 관통 4회가 모두 총괄 스키마 이탈로 죽었고 매번 모양이
    달랐다. 거부만 하면 분석가 6인을 돌린 2~3분이 버려지므로, **모양은 코드가
    바로잡고 내용 검증은 그대로 돌린다**(수치 재대조·라벨 가드는 평탄화된
    문자열에 그대로 걸리므로 오히려 검사가 더 잘 보게 된다).

    지어내지 않는 것이 원칙이라 값이 없으면 빈 목록이 아니라 None 을 낸다 -
    빈 목록은 '내용이 없다'는 사실을 만들어내는 것이다.
    """
    def one(x) -> str:
        if isinstance(x, str):
            return x.strip()
        if isinstance(x, dict):
            # {"claim": "...", "source": "..."} -> "claim: ... | source: ..."
            return " | ".join(f"{k}: {one(val)}" for k, val in x.items())
        if isinstance(x, (list, tuple)):
            return " ; ".join(one(i) for i in x)
        return str(x)

    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else None
    if isinstance(v, dict):
        out = [f"{k}: {one(val)}" for k, val in v.items()]
    elif isinstance(v, (list, tuple)):
        out = [one(x) for x in v]
    else:
        return None
    out = [s for s in (x.strip() for x in out) if s]
    return out or None


# ── 노드 4: Packet 초안 (supervisor 페르소나 - config.yaml 원문) ───────────
def _supervisor_persona() -> str:
    """RES-00 페르소나 + **이 호출의 범위**를 명시한다.

    ▶ 왜 덧붙이는가 (실측 2026-08-03)
      페르소나가 "Official outputs: Research Assignment, Research Packet,
      Dossier Update, Data Quality Warning" 이라고 4종을 나열한다. Claude 는 그
      지시를 성실히 따라 **매번 4종을 한 객체로** 낸다. 그런데 그 안의
      research_packet 은 {facts, interpretations} 처럼 반쪽이라 언래퍼로도 못
      살린다 - 모델이 산출물 하나에 쏟을 토큰을 넷으로 나눠 쓰기 때문이다.
      실측 4회 연속 거부.

      **페르소나를 고치지 않는다.** 그건 부서의 직무 정의이고 다른 호출(헤르메스
      경유)에서도 쓰인다. 대신 이 호출이 4종 중 무엇을 요구하는지 여기서 좁힌다.
    """
    cfg = (_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8")
    persona = re.search(r'research-supervisor: "(.*?)"\n', cfg, re.DOTALL).group(1)
    return persona + (
        "\n\nSCOPE OF THIS CALL: you are producing the Research Packet ONLY. "
        "Do NOT emit Research Assignment, Dossier Update or Data Quality Warning "
        "in this reply, and do NOT nest the packet under a wrapper key. "
        "Return ONE flat JSON object whose top-level keys are exactly the schema "
        "keys requested below. Spend your whole answer on that single object.")


# 서술이 한국어인지 판정하는 하한. 숫자·티커·[n1] 같은 ref 가 섞이므로 100% 를
# 요구할 수 없고, 너무 낮으면 영어 문장에 한글 단어 하나 섞인 것을 통과시킨다.
# 실측: 정상 한국어 서술은 0.35~0.55, 영어 서술은 0.00~0.03 이라 0.15 면 갈린다.
MIN_KOREAN_RATIO = 0.15

_HANGUL_RE = re.compile(r"[가-힣]")
_LETTER_RE = re.compile(r"[가-힣A-Za-z]")

# 언어를 판정할 **서술** 필드. evidence_quality 같은 토큰 필드는 제외한다 -
# 그건 영어여야 맞다.
NARRATIVE_KEYS = ("thesis", "facts", "interpretation", "catalysts", "invalidation")


def korean_ratio(packet: dict) -> float:
    """서술 필드의 한글 비율. 글자가 없으면 1.0(판정 보류 - 막지 않는다).

    숫자·기호는 세지 않는다. 한글 대 (한글+로마자) 다 - 그래야 "Global sales
    decreased 5.1%" 같은 문장이 0 에 가깝게 나온다.
    """
    buf = []
    for k in NARRATIVE_KEYS:
        v = (packet or {}).get(k)
        if isinstance(v, str):
            buf.append(v)
        elif isinstance(v, (list, tuple)):
            buf += [str(x) for x in v]
    text = " ".join(buf)
    # 인용 ref([n1], [d2])와 필드 접두사(sales_data:)는 언어가 아니다.
    # 세면 한국어 문장도 비율이 깎이고, 무엇보다 숫자만 있는 서술이 영어로
    # 오판된다 - 가드가 오탐하면 재시도만 늘고 결국 무시된다.
    text = re.sub(r"\[[a-zA-Z]\d+\]", " ", text)
    text = re.sub(r"^\s*[a-z_]+\s*:", " ", text, flags=re.MULTILINE)
    letters = _LETTER_RE.findall(text)
    if not letters:
        return 1.0
    return len(_HANGUL_RE.findall(text)) / len(letters)


def _citable(bundle: dict) -> list[dict]:
    """총괄이 인용할 수 있는 근거 목록. **여기 없는 ref 는 창작이다.**

    Bundle 이 ref + evidence_id 를 싣게 된 뒤(evidence/bundle.py 2026-08-03)
    비로소 가능해진 계약이다. 예전 프롬프트는 "reference ids like news:3" 이라고
    **존재하지 않는 형식**을 지시했고, 그러니 LLM 이 형식을 지어냈다.
    """
    out = []
    for key in ("news_headlines", "disclosures_7d"):
        for item in (bundle or {}).get(key) or []:
            if isinstance(item, dict) and item.get("ref"):
                out.append({"ref": item["ref"],
                            "title": str(item.get("title", ""))[:80],
                            "kind": "news" if key.startswith("news") else "disclosure"})
    return out


def draft_packet(state: ResearchState, *, llm=None) -> dict:
    symbol = state["symbol"]
    # 시점 가드의 기준. state 에 없으면 지금이다 - Evidence 를 방금 모았으므로
    # '지금' 이 곧 컷오프다(as_known_at = started 와 같은 뜻).
    as_known = state.get("as_known_at") or datetime.now(timezone.utc)
    if isinstance(as_known, str):
        as_known = datetime.fromisoformat(as_known)
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
        "market_regime": state.get("regime") or {"status": "NOT_RUN"},
        "geopolitical": state.get("geopolitical") or {"status": "NOT_RUN"},
        "microstructure": state.get("microstructure") or {"status": "NOT_RUN"},
        # ▶ 감성이 확정치 풀에서 빠져 있었다 (2026-08-03 실측)
        #   6인 중 5인만 넣어서 RES-06 의 articles_used·score 를 인용하면
        #   창작으로 몰렸다 - 실측 불일치 [200, 127] 이 정확히 이것이다.
        "sentiment": state.get("sentiment") or {"status": "NOT_RUN"},
    }

    # 프롬프트에는 압축 다이제스트만 - 실측: 원자료(중첩 dict)를 통째로 주면
    # 모델이 출력 구조까지 dict 로 따라가 스키마(list[str])를 어긴다.
    # 수치 재대조(confirmed)는 아래에서 원본 analysts 로 한다 - 상위집합 허용.
    def _digest(a: dict) -> dict:
        """분석가 결과 -> 총괄 프롬프트용 압축본. **상한이 있는 것이 핵심이다.**

        실측 2026-08-02(분석가 6인 확장): 다이제스트가 커지자 qwen3 의 <think>
        가 길어져 JSON 끝이 잘렸다("missing keys: ['invalidation']" - 마지막
        키만 사라지는 전형적 절단). 분석가를 늘릴 때마다 총괄이 깨지면 확장이
        불가능하므로, 분석가 수와 무관하게 프롬프트 크기가 상한을 갖게 한다.
        버려지는 원문은 리포트의 '분석가 소견 원문'에 그대로 남는다.
        """
        if not a or a.get("status") == "NOT_RUN":
            return {"status": "NOT_RUN"}
        # 분석가마다 서술 위치가 다르다 - note.summary(기술·펀더멘털·레짐·
        # 미시구조) 와 최상위 summary(지정학). 한쪽만 보면 RES-09 다이제스트가
        # 통째로 null 이 되어 총괄이 맥락 없이 쓴다(2026-08-02 실측).
        note = a.get("note") or {}
        summary = (note.get("summary") or a.get("summary") or "")[:DIGEST_SUMMARY_CHARS]
        cautions = [str(c)[:DIGEST_CAUTION_CHARS]
                    for c in (note.get("cautions") or a.get("cautions") or [])
                    ][:DIGEST_MAX_CAUTIONS]
        # ▶ 최상위만 세면 재료의 절반을 놓친다 (2026-08-03)
        #   readout 은 평평하지 않다 - fields/ratios/liquidity/macro_overlay 같은
        #   컨테이너 한 겹 안에 실제 수치가 있다. 최상위 dict 만 훑으면 펀더멘털의
        #   재무 12개, 레짐의 스타일 비율이 통째로 빠지고, 총괄은 그만큼 얇은
        #   재료로 쓰다가 사전지식으로 채운다(005380 창작 사고의 배경).
        #   highlights 가 이미 푼 문제이므로 같은 함수를 쓴다 - 중요도 정렬
        #   (임계 위반 우선, |값| 큰 순)까지 따라온다.
        h = pick_highlights(a.get("readout"), top_n=DIGEST_MAX_NUMBERS,
                            flags=(a.get("readout") or {}).get("flags") or [])
        nums = {i["key"]: i["value"] for i in h["items"]}
        return {"verdict": a.get("verdict"), "summary": summary,
                "cautions": cautions, "key_numbers": nums,
                # 몇 개 중 몇 개를 보여주는지 숨기지 않는다 - 총괄이 "이게 전부"
                # 라고 오해하면 없는 것을 채우려 든다
                "numbers_shown_of_total": [len(nums), h["total_metrics"]],
                "unknown_metrics": h["unknown"]}

    analysts_digest = {k: _digest(v) for k, v in analysts.items()}
    task = f"""Using ONLY the evidence below, draft a compact Research Packet in Korean.
Schema (JSON only):
{{"thesis": "1-2 sentences in Korean, facts first",
 "facts": ["one fact per string. EVERY fact must end with its evidence ref in
            brackets, e.g. [n1] for a news item or [d2] for a disclosure. If you
            cannot point at a ref, it is not a fact - move it to interpretation."],
 "interpretation": ["clearly separated from facts"],
 "catalysts": ["upcoming checkpoints"],
 "invalidation": ["conditions that would kill the thesis"],
 "evidence_quality": "sufficient | partial | insufficient_evidence"}}
LANGUAGE - non-negotiable, checked by code:
- thesis, facts, interpretation, catalysts, invalidation MUST be written in
  KOREAN (한국어). A reply in English is rejected automatically and costs a
  retry. Field NAMES stay English; every VALUE is Korean. Numbers, tickers and
  refs like [n1] stay as-is.

STRUCTURE RULES - non-negotiable:
- facts, interpretation, catalysts, invalidation MUST each be a FLAT JSON
  array of plain strings. No nested objects, no key-value maps.
- evidence_quality is exactly one of the three lowercase tokens above.
- Write all narrative text in Korean.

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
{json.dumps(analysts_digest, ensure_ascii=False, indent=1)}
Rules for analyst findings - non-negotiable:
- Quote their verdicts and numbers verbatim; do not invent new figures.
- If analysts DISAGREE (e.g., sentiment positive but technical BEARISH),
  preserve BOTH views side by side in interpretation. Never delete dissent,
  never average it away - state the conflict explicitly.
- NOT_RUN / INSUFFICIENT_DATA / null means 미확인 - never fill the gap.
- key_numbers is a SELECTION, not everything - numbers_shown_of_total tells you
  how many were withheld. Do not invent the missing ones.

CITABLE EVIDENCE (these refs, and only these, may appear in facts):
{json.dumps(_citable(bundle), ensure_ascii=False, indent=1)}
Rules for citation - non-negotiable:
- A statement in "facts" MUST carry a ref from the list above, like "... [n1]".
- You may NOT cite a ref that is not in the list. Inventing an id is worse than
  omitting the claim.
- Anything you cannot attach a ref to belongs in "interpretation", not "facts".
- Never write a company name, price, date or source that is absent from the
  evidence above. If the evidence does not mention it, you do not know it.

Evidence:
{json.dumps(bundle, ensure_ascii=False, indent=1)}

마지막 지시 (가장 중요, 코드가 검사한다):
thesis · facts · interpretation · catalysts · invalidation 의 **값을 전부
한국어 문장으로** 쓴다. 영어로 쓰면 코드가 거부하고 재시도를 소모한다.
키 이름은 영어 그대로 두고, 숫자·종목코드·[n1] 같은 근거 표시도 그대로 둔다.
JSON 객체 하나만 반환한다 - 설명 문장이나 코드펜스를 앞뒤에 붙이지 않는다."""

    call = llm or _ollama_chat
    # symbol 은 뺀다 - 코드가 덮어쓰기로 정했으므로(종목 정체성은 LLM 에게 묻지
    # 않는다) 필수로 요구하면 실패 표면만 넓어진다. 실측 2026-08-03: 4/6 키를
    # 맞춘 응답이 symbol 하나 때문에 버려졌다.
    REQUIRED = ("thesis", "facts", "interpretation", "invalidation",
                "evidence_quality")
    packet = None
    last_err = None
    repair = ""      # 실패 유형별 교정 지시 - 일반 문구만으로는 같은 실수를 반복한다
    for attempt in (1, 2, 3, 4, 5):   # 파싱·스키마·언어 위반 재시도 - 실측: 토큰 이탈(high)이 2회 연속도 나온다
        extra = "" if attempt == 1 else (
            f"\n\nYour previous reply was rejected ({last_err}). Return ONLY the "
            f"JSON object with ALL required keys: {', '.join(REQUIRED)}.{repair}")
        out = re.sub(r"<think>.*?</think>", "",
                     call(_supervisor_persona(), task + extra), flags=re.DOTALL)
        s, e = out.find("{"), out.rfind("}")
        if s < 0 or e < s:
            last_err = "no JSON object in reply"
            continue
        try:
            candidate = json.loads(out[s:e + 1])
        except json.JSONDecodeError as err:
            last_err = f"invalid JSON: {err}"
            continue
        candidate = unwrap_packet(candidate)
        missing = [k for k in REQUIRED if k not in candidate]
        if missing:
            last_err = f"missing keys: {missing}"
            # **원문을 남긴다.** 예전에는 사유만 남아 다음 실패 때 무엇이
            # 왔는지 알 방법이 없었다(실측: 08-02 01:22 과 08-03 00:10 이 같은
            # 문구로 죽었는데 둘 다 원인을 못 봤다). 분석가 6인을 2~3분 돌린
            # 뒤에 죽는 자리라 재현 비용이 비싸다 - 그때 본 것을 그때 남긴다.
            _last_raw = out[s:e + 1][:600]
            # 래핑된 경우 **안쪽 키도** 찍는다 - 바깥 키만 보면 왜 못 벗겼는지
            # 알 수 없다(실측 2026-08-03: 'Research Packet' 을 이름으로 찾는데도
            # 계속 거부돼 안쪽을 못 봤다).
            try:
                for _k, _v in (candidate or {}).items():
                    if isinstance(_v, dict):
                        print(f"   └ {_k!r} 안쪽 키: {sorted(_v)[:10]}", flush=True)
            except Exception:  # noqa: BLE001, S110 - intentional fallback boundary
                pass
            print(f"⚠ 총괄 스키마 이탈({attempt + 1}회) - 받은 최상위 키: "
                  f"{sorted(candidate)[:12]} / 원문 앞부분: {_last_raw[:220]}",
                  file=sys.stderr, flush=True)
            repair = ('\nReturn a SINGLE flat JSON object whose TOP-LEVEL keys are '
                      'exactly: symbol, thesis, facts, interpretation, invalidation, '
                      'evidence_quality. Do NOT wrap it in another object such as '
                      '{"packet": {...}} or {"result": {...}}.')
            continue
        # 타입 - **거부가 아니라 교정한다.** 실측 2026-08-02: 관통 실행 4회가
        # 전부 여기서 죽었는데 매번 이탈 모양이 달랐다(facts 가 dict, 키 누락,
        # 토큰 이탈). 그때마다 **분석가 6인을 돌린 2~3분이 통째로 버려진다.**
        # 중첩을 거부한 원래 이유는 "항목 안에 창작 수치가 숨어 검사를 우회"
        # 였는데, 평탄화하면 오히려 수치 검사가 전부 보게 되므로 교정이 더
        # 안전하다. 무엇을 고쳤는지는 Packet 에 남겨 사람이 볼 수 있게 한다.
        coerced: list[str] = []
        bad_key = None
        for k in ("facts", "interpretation", "invalidation"):
            v = candidate.get(k)
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                continue
            flat = flatten_to_str_list(v)
            if flat is None:
                bad_key = k
                break
            candidate[k] = flat
            coerced.append(k)
        if bad_key is not None:
            last_err = f"{bad_key} 를 문자열 목록으로 바꿀 수 없다"
            repair = (f'\n"{bad_key}" must be a FLAT array of plain Korean '
                      f'sentences, e.g. ["첫 문장.", "둘째 문장."] - never an '
                      f'object, never nested.')
            continue
        candidate["_schema_coerced"] = coerced
        eq = str(candidate.get("evidence_quality", "")).lower().strip()
        if eq not in ("sufficient", "partial", "insufficient_evidence"):
            last_err = f"evidence_quality must be one of the 3 tokens, got {eq[:40]!r}"
            # 실측 2026-08-01·08-02: qwen3 이 'high'/'good' 같은 제 어휘를 3회
            # 연속 낸다. 일반 문구로는 안 고쳐진다 - 무엇을 냈고 무엇으로
            # 바꿔야 하는지 지목하고, 뜻이 가까운 토큰까지 알려준다.
            near = ("sufficient" if eq in ("high", "good", "strong", "ok")
                    else "insufficient_evidence" if eq in ("low", "poor", "none")
                    else "partial")
            repair = (f'\n"evidence_quality" must be EXACTLY one of these three '
                      f'lowercase strings: "sufficient", "partial", '
                      f'"insufficient_evidence". You wrote {eq[:20]!r}, which is '
                      f'not allowed. If you meant that, write "{near}".')
            continue
        candidate["evidence_quality"] = eq

        # ▶ 한국어 검사 (2026-08-03)
        #   프롬프트가 "Write all narrative text in Korean" 이라고 세 군데서
        #   지시하는데 총괄이 영어로 쓴다. **지시만 하고 검사하지 않으면 지켜지지
        #   않는다** - 스키마·수치는 검사하면서 언어만 신뢰한 것이 구멍이었다.
        #   재시도 루프가 이미 있으므로 같은 자리에 넣는다.
        ratio = korean_ratio(candidate)
        if ratio < MIN_KOREAN_RATIO:
            last_err = f"narrative is not Korean (한글 비율 {ratio:.0%})"
            repair = (
                '\nCRITICAL: thesis, facts, interpretation, catalysts and '
                'invalidation MUST be written in KOREAN (한국어). Your previous '
                'reply was in English and was rejected. Keep field NAMES in '
                'English but write every VALUE in Korean. Numbers, tickers and '
                'evidence refs like [n1] stay as-is.')
            continue

        # ▶ 종목 정체성은 LLM 에게 묻지 않는다 (2026-08-03 사고)
        #   005380(현대차)을 요청했는데 총괄이 symbol 을 "KOSPI" 로 쓰고 지수
        #   서사를 만들었다. symbol 은 **우리가 인자로 넘긴 값**이다 - 코드가
        #   아는 사실을 LLM 출력으로 받는 것 자체가 설계 오류였다.
        #   다르게 썼다는 사실은 지우지 않고 남긴다(무엇이 어긋났는지 봐야 한다).
        said = str(candidate.get("symbol", "")).strip()
        if said != symbol:
            candidate["_symbol_said"] = said
            print(f"⚠ 총괄이 종목을 다르게 썼다: {said!r} -> {symbol} 로 정정",
                  flush=True)
        candidate["symbol"] = symbol

        packet = candidate
        break
    if packet is None:
        # 결정론 가드: 두 번 다 스키마를 못 지키면 초안을 거부한다 - 통과 위장 금지
        raise ValueError(f"Packet 초안 거부 - {last_err}")
    # 결정론 가드 2: 서술 속 % 수치가 확정치(가격+분석가 readout) 밖이면 창작이다.
    # 대상은 **사실 서술**(thesis/facts/interpretation)만 - invalidation·catalysts
    # 는 모델이 제안하는 미래 조건("성장률 10% 미달 시")이라 확정치에 없는 것이
    # 정상이다(실측 2026-08-01: 제안 임계값 오탐). 중첩 우회 방지로 해당 키들을
    # 통째로 직렬화한다.
    from evidence.bundle import verify_narrative_dates, verify_narrative_numbers

    narrative = json.dumps({k: packet.get(k) for k in
                            ("thesis", "facts", "interpretation")},
                           ensure_ascii=False)
    packet["numeric_check"] = verify_narrative_numbers(
        narrative,
        # ▶ 우리가 준 근거의 수치도 확정치다 (2026-08-03)
        #   Opus 전환 후 불일치가 23건 중 9건으로 늘었는데, 확인해 보니 대부분이
        #   **우리가 프롬프트에 넣은 뉴스 제목의 숫자**였다("7월 판매 5.1% 감소
        #   [n6]"). 우리가 준 것을 인용했는데 창작으로 몰면 가드가 거짓말을 하고,
        #   그러면 사람이 가드를 무시한다. 근거 제목을 풀에 넣는다 -
        #   **정당하게 준 것이 화이트리스트에 들어간다**는 도구 계층과 같은 원칙이다.
        {"price": price_ctx, "analysts": analysts,
         "evidence_titles": [i.get("title") for k in
                             ("news_headlines", "disclosures_7d")
                             for i in (bundle.get(k) or []) if isinstance(i, dict)]})

    # 결정론 가드 3: 사실 서술에 확정치 밖 수치가 있으면 evidence_quality 를
    # **강등**한다. 실측 2026-08-02: 총괄이 매출 +18.3%·영업이익 +12.7% 를
    # 지어내 facts 에 출처까지 달아 넣었는데(분석가 실제값은 46.76%/101.16%)
    # 검사는 불일치를 표시만 하고 evidence_quality 는 sufficient 로 나갔다.
    # 표시만 하는 가드는 하류(트레이딩)에서 읽히지 않는다 - 등급을 낮춰야
    # 계약이 된다. 낮추기만 하고 올리지는 않는다(위험 방향 fail-closed).
    if not packet["numeric_check"].get("ok"):
        order = ("insufficient_evidence", "partial", "sufficient")
        cur = packet["evidence_quality"]
        if order.index(cur) > order.index("partial"):
            packet["evidence_quality"] = "partial"
            packet["numeric_check"]["downgraded_from"] = cur

    # ▶ 결정론 가드 4: 시점 창작 (2026-08-03 사고)
    #   005380 실행에서 총괄이 facts 4건을 **2023-11-02/03** 날짜로 썼다.
    #   출처("연합뉴스", "코리아타임스")까지 달았는데 우리는 그런 기사를 준 적이
    #   없다. 완전한 창작인데 **숫자 가드를 통과했다** - % 수치가 아니라 날짜라서다.
    #   Evidence 가 담고 있는 시점 밖의 날짜를 사실 서술에 쓰면 창작으로 본다.
    packet["date_check"] = verify_narrative_dates(narrative, as_known_at=as_known)
    if not packet["date_check"].get("ok"):
        order = ("insufficient_evidence", "partial", "sufficient")
        cur = packet["evidence_quality"]
        if order.index(cur) > order.index("partial"):
            packet["evidence_quality"] = "partial"
            packet["date_check"]["downgraded_from"] = cur

    # ▶ 결정론 가드 5: 창작 문장을 **격리한다** (2026-08-03)
    #   지금까지는 등급만 낮추고 창작 문장은 facts 에 그대로 남았다. 트레이딩본부가
    #   읽는 것은 facts 이지 evidence_quality 가 아니다 - 등급을 낮춰도 "삼성전자
    #   7% 하락(2023-10-15, Bloomberg)" 이 사실 목록에 그대로 있으면 하류는 그것을
    #   사실로 받는다. **표시로 끝내지 않고 빼낸다.**
    packet = quarantine_fabrications(packet, as_known_at=as_known)
    return {"packet": packet}


def quarantine_fabrications(packet: dict, *, as_known_at) -> dict:
    """facts 에서 창작이 확인된 문장을 빼내 _quarantined 로 옮긴다.

    ▶ 문장 단위로 판정한다. Packet 전체를 강등하는 것과 다르다 - 한 문장이
      창작이라고 나머지 정상 사실까지 버릴 이유가 없다.

    ▶ 판정 기준은 **시점**뿐이다. 숫자 불일치는 문장 단위로 귀속시키기 어렵다
      (한 문장에 여러 수치가 있고 일부만 어긋날 수 있다) - 그건 전체 강등으로
      남긴다. 시점은 그 문장 안에 있으므로 귀속이 확실하다.
      **확실하지 않은 것은 빼지 않는다** - 정상 사실을 지우는 것이 더 나쁘다.

    ▶ facts 가 비면 insufficient_evidence 다. 강등이 아니라 사실 진술이다 -
      근거 있는 사실이 하나도 없는 Packet 은 근거가 불충분한 것이 맞다.
    """
    from evidence.bundle import verify_narrative_dates

    p = dict(packet or {})
    facts = list(p.get("facts") or [])
    if not facts:
        return p

    kept, quarantined = [], []
    for f in facts:
        s = str(f)
        r = verify_narrative_dates(s, as_known_at=as_known_at)
        if r.get("ok"):
            kept.append(f)
        else:
            bad = (r.get("too_old_years") or []) + (r.get("future_years") or [])
            quarantined.append({"text": s, "reason": f"Evidence 창 밖 연도 {bad}"})

    if not quarantined:
        return p
    p["facts"] = kept
    p["_quarantined"] = list(p.get("_quarantined") or []) + quarantined
    print(f"⚠ 창작 의심 사실 {len(quarantined)}건을 facts 에서 격리했다", flush=True)
    if not kept:
        # 근거 있는 사실이 0건이면 등급을 사실대로 적는다
        p["evidence_quality"] = "insufficient_evidence"
        p["_quarantine_emptied_facts"] = True
    return p


SUPERVISOR_TEMPERATURE = 0.2   # 총괄은 분석가(0.1)보다 조금 느슨하게 종합한다
SUPERVISOR_TIMEOUT = 600       # 14b + <think> - 분석가보다 훨씬 길다
# 실측: 기본값에서 JSON 이 잘려 스키마 위반 -> 4096 -> 분석가 6인 확장 후
# <think> 가 길어져 또 잘림(마지막 키 invalidation 소실)이라 8192.
# 입력 상한(_digest)과 함께 쓴다 - 한쪽만으로는 확장할 때마다 다시 깨진다.
SUPERVISOR_MAX_TOKENS = 8192


def _ollama_chat(system: str, user: str) -> str:
    """호출 모양은 evidence/llm_client 가 단일 출처다. 여기 남는 것은 총괄의
    설정뿐 - 이 셋만 분석가와 다를 이유가 실제로 있다."""
    return llm_chat(system, user, base=SUPERVISOR_BASE, model=SUPERVISOR_MODEL,
                    timeout=SUPERVISOR_TIMEOUT,
                    temperature=SUPERVISOR_TEMPERATURE,
                    max_tokens=SUPERVISOR_MAX_TOKENS)



# ── 노드 6: Skeptic (분석가 대화) ──────────────────────────────────────────
def challenge_packet(state: ResearchState) -> dict:
    """총괄 초안을 **반박한다**. framework 6.1 9단계(Challenge).

    지금까지 분석가 6인은 병렬로 각자 답하고 총괄이 fan-in 할 뿐이었다 -
    서로의 판정을 보지 않고 모순이 있어도 총괄이 매끄럽게 뭉갰다.
    여기가 유일하게 **대화가 일어나는 자리**다.

    갈등은 코드가 찾고(detect_disagreements), LLM 은 찾아진 갈등에 대해서만
    대안 설명을 쓴다. LLM 에게 "누가 어긋나나"를 물으면 없는 갈등을 지어낸다.

    실패해도 Packet 을 죽이지 않는다 - 반박이 없는 것과 Packet 이 없는 것은
    다르다. 다만 **반박을 못 했다는 사실은 남긴다**.
    """
    from evidence.llm_client import extract_json
    from skeptic import (
        CHALLENGE_SYSTEM,
        apply_challenge,
        build_challenge_prompt,
        detect_disagreements,
        verify_challenge,
    )

    packet = state.get("packet") or {}
    analysts = {k: state.get(k) for k in
                ("technical", "fundamental", "regime", "geopolitical",
                 "microstructure", "sentiment")}
    analysts = {k: v for k, v in analysts.items() if isinstance(v, dict)}
    disagreements = detect_disagreements(analysts)

    confirmed = {"price": (state.get("evidence") or {}).get("price_context"),
                 "analysts": {k: v.get("readout") for k, v in analysts.items()}}

    challenge = verification = None
    try:
        raw = _ollama_chat(CHALLENGE_SYSTEM, build_challenge_prompt(
            thesis=str(packet.get("thesis", "")), analysts=analysts,
            disagreements=disagreements, confirmed=confirmed))
        challenge = json.loads(extract_json(raw))
        # dict 로 온 반박도 문장으로 펴서 검증한다 - str(dict) 를 검사하면
        # 따옴표·중괄호에 가려 수치 검출이 헐거워진다(실측 2026-08-03).
        from skeptic import _as_sentence

        verification = verify_challenge(
            " ".join(_as_sentence(x) for x in (
                (challenge.get("alternative_explanations") or [])
                + [challenge.get("weakest_claim", ""),
                   challenge.get("what_would_overturn_it", "")])),
            confirmed)
    except Exception as e:  # noqa: BLE001
        # 반박 실패는 Packet 실패가 아니다. 그러나 침묵하지 않는다.
        verification = {"ok": None,
                        "reason": f"반박 미실행: {type(e).__name__}: {e}"[:200]}
        print(f"⚠ Skeptic 실패(Packet 은 유지): {type(e).__name__}: {e}", flush=True)

    return {"packet": apply_challenge(packet, disagreements=disagreements,
                                      challenge=challenge,
                                      verification=verification)}


# ── Evidence Gap Loop ──────────────────────────────────────────────────────
# ▶ 분석가가 INSUFFICIENT_DATA 를 내면 지금은 **그냥 다음으로 갔다.** 근거가
#   부족하다고 스스로 말했는데 아무도 메우려 하지 않는다 - 도구를 쥐여준
#   의미가 여기서 사라졌다. framework 의 Evidence Gap Loop 가 이 자리다.
#
#   되돌아가는 곳은 assemble_evidence 다. 분석가 개별 재실행이 아니라 근거를
#   더 모아 **전원이 다시 보게** 한다 - 한 분석가만 다시 돌리면 다른 분석가는
#   옛 근거로 판단한 채 남아 Packet 안에서 시점이 갈라진다.
MAX_GAP_FILLS = 1        # 한 번만. 못 메우면 부족한 채로 내되 그 사실을 남긴다


def gap_report(state: ResearchState) -> dict:
    """어느 분석가가 무엇이 없다고 했는가. 순수 함수."""
    gaps: dict[str, list[str]] = {}
    for node in ("technical", "fundamental", "regime", "geopolitical",
                 "microstructure", "sentiment"):
        st = state.get(node)
        if not isinstance(st, dict):
            continue
        ro = st.get("readout") or {}
        missing = [str(x) for x in (ro.get("unavailable") or [])][:8]
        if str(st.get("verdict") or ro.get("verdict")) == "INSUFFICIENT_DATA":
            missing = missing or ["(사유 미기재)"]
        if missing:
            gaps[node] = missing
    return gaps


def needs_gap_fill(state: ResearchState) -> str:
    """근거를 더 모을 가치가 있는가. **판정은 결정론이다.**

    빈 구멍이 있다고 무조건 되돌아가지 않는다 - 어떤 지표는 그 종목에
    원래 없다(비상장 파생, 미상장 기간). 무한히 다시 모아도 안 채워진다.
    **분석가가 INSUFFICIENT_DATA 로 판정 자체를 못 낸 경우**만 되돌아간다.
    """
    if int(state.get("gap_fills") or 0) >= MAX_GAP_FILLS:
        return "done"
    for node in ("technical", "fundamental", "regime", "geopolitical",
                 "microstructure", "sentiment"):
        st = state.get(node)
        if not isinstance(st, dict):
            continue
        if str(st.get("verdict") or (st.get("readout") or {}).get("verdict"))                 == "INSUFFICIENT_DATA":
            return "fill"
    return "done"


def bump_gap_fill(state: ResearchState) -> dict:
    """재수집 횟수를 올리고 무엇이 비었는지 남긴다.

    **메우려 했다는 사실과 무엇이 비었는지가 남아야** 다음에 수집기를 고친다.
    조용히 다시 돌면 같은 구멍이 영원히 반복된다.
    """
    gaps = gap_report(state)
    return {"gap_fills": int(state.get("gap_fills") or 0) + 1,
            "evidence_gaps": gaps}


# ── 노드 10.5: 해석 (인사이트) ─────────────────────────────────────────────
# ▶ **인사이트가 나올 자리가 없었다.** 분석가는 compute -> narrate -> verify 로
#   돌고 라벨은 코드가 정한다. LLM 은 그 라벨을 문장으로 옮길 뿐이라 여섯 축을
#   가로질러 "그래서 무슨 뜻인가" 를 말하는 자리가 통째로 비어 있었다.
#
#   마스터 플랜이 결정론에 묶은 것은 **규칙 판정**(PIT·인용검증·한도)이다.
#   "이 세 신호가 같이 나타난다는 게 무슨 뜻인가" 는 규칙 판정이 아니다 -
#   둘을 같은 것으로 묶어 해석까지 결정론에 넘긴 것이 지금까지의 한계였다.
MAX_REVISIONS = 1        # 반박 -> 재해석은 한 번만. 무한 순환은 비용만 태운다
MAX_INSIGHTS = 5

_INTERPRET_SYSTEM = """너는 리서치본부 총괄(RES-00)이다.
분석가 여섯의 판정을 **가로질러** 해석한다. 숫자를 다시 쓰는 것이 아니라
서로 다른 축이 함께 무엇을 가리키는지 말한다.

반드시 지켜라:
- 각 해석은 **서로 다른 분석가 2인 이상**의 Fact 를 참조한다.
  하나만 가리키면 그건 해석이 아니라 그 Fact 의 재진술이다.
- 각 해석에 **반증 조건**을 쓴다: 무엇이 관측되면 이 해석이 틀리는가.
  "없다", "알 수 없음" 같은 회피는 거부된다. 틀릴 수 없는 문장은 분석이 아니다.
- 판정 지평(일)을 정한다. 언제 맞았는지 볼 수 있어야 한다.
- 새 수치를 만들지 않는다. 주어진 Fact 를 가리키기만 한다.
- **[주의] 로 시작하는 Fact 를 반드시 존중한다.** 어떤 지표가 이 업종에
  적용되지 않는다고 적혀 있으면 그 지표를 근거로 삼지 않는다.
- source_nodes 에는 **분석가 이름**을 넣는다(주어진 analysts 목록에서 고른다).
  claim 문장 안에 적지 말고 반드시 이 필드에 넣어라.

kind 는 다음 중 하나: CROSS_SIGNAL, CAUSAL_HYPOTHESIS, REGIME_READ,
RISK_ASYMMETRY, DIVERGENCE

JSON 만 낸다: {"insights": [{"kind": "...", "claim": "...",
"supporting_fact_ids": ["..."], "source_nodes": ["..."], "falsifier": "...",
"horizon_days": 5, "confidence": "MEDIUM"}]}"""


def _fact_index(packet: dict) -> dict:
    """Packet facts -> {id: fact}. id 가 없으면 순번으로 만든다."""
    out = {}
    for i, f in enumerate(packet.get("facts") or []):
        if not isinstance(f, dict):
            continue
        fid = str(f.get("id") or f.get("fact_id") or f"f{i + 1}")
        out[fid] = f
    return out


def interpret(state: ResearchState) -> dict:
    """여섯 축을 가로질러 해석을 만든다. **수치 대조가 아니라 반증 가능성으로
    검증한다** - 수치 대조를 걸면 다시 숫자 나열로 수렴한다."""
    sys.path.insert(0, str(_BASE / "contracts"))
    from evidence.llm_client import extract_json
    from insight import to_claims, validate_insights

    packet = state.get("packet") or {}
    nodes = {k for k in ("technical", "fundamental", "regime", "geopolitical",
                         "microstructure", "sentiment")
             if isinstance(state.get(k), dict)}
    # ▶ **가로지를 재료는 분석가 readout 이다.** 처음엔 Packet 의 facts 만
    #   줬는데, 그건 공시·뉴스 기반(d1, n1)이라 **분석가 귀속이 없다** -
    #   그래서 source_nodes 를 채울 수가 없었고 좋은 해석이 전부 거부됐다.
    #   교차 해석은 "어느 분석가가 무엇을 봤나" 가 있어야 성립한다.
    facts = _fact_index(packet)
    for node in sorted(nodes):
        ro = (state.get(node) or {}).get("readout") or {}
        hi = pick_highlights(ro, flags=(ro.get("flags") or []))
        for it in (hi.get("items") or [])[:6]:
            k = str(it.get("key") or "")
            if not k:
                continue
            unit = str(it.get("unit") or "")
            facts[f"{node}.{k}"] = {
                "claim": f"{k} = {it.get('value')}{unit}", "source_node": node}
        for fl in (hi.get("flags") or [])[:3]:
            facts[f"{node}.flag.{fl}"] = {"claim": f"플래그: {fl}",
                                          "source_node": node}
        note = ((state.get(node) or {}).get("note") or {}).get("summary")             or (state.get(node) or {}).get("summary")
        if note:
            facts[f"{node}.소견"] = {"claim": str(note)[:200],
                                    "source_node": node}
        # ▶ **주의사항도 재료다.** 실측(006800): RES-05 가 "부채비율은 금융업에
        #   적용되지 않는다" 를 cautions 에 실었고 Skeptic 은 그것을 읽어 약한
        #   주장을 지목했는데, 해석 노드는 수치만 받아 같은 오독을 반복했다 -
        #   경고가 닿지 않는 층은 경고가 없는 것과 같다.
        for ci, c in enumerate((ro.get("cautions")
                                or (state.get(node) or {}).get("cautions")
                                or [])[:4]):
            facts[f"{node}.주의{ci + 1}"] = {"claim": f"[주의] {str(c)[:200]}",
                                            "source_node": node}
    revision = int(state.get("revisions") or 0)
    if not facts or len(nodes) < 2:
        # 가로지를 것이 없으면 해석도 없다. 억지로 만들면 재진술이 된다.
        return {"insights": [], "insight_rejected": [],
                "insight_note": f"Fact {len(facts)}건 / 분석가 {len(nodes)}인 - "
                                f"교차 해석 불가"}

    # ▶ Fact -> 분석가 대응은 **코드가 안다.** 이것을 LLM 에게 물었더니 이름을
    #   claim 안에 적고 필드는 비워 냈고, 그래서 실측에서 좋은 해석 4건이
    #   전부 거부됐다(source_nodes=[]). 코드가 아는 것을 LLM 에게 묻지 않는다.
    fact_node = {fid: str(f.get("source_node") or f.get("node") or "")
                 for fid, f in facts.items()}
    brief = {fid: {"claim": str(f.get("claim") or f.get("text") or "")[:180],
                   "분석가": fact_node.get(fid) or "(미상)"}
             for fid, f in list(facts.items())[:48]}
    # 재해석이면 반박 내용을 같이 준다 - 그것이 이 순환의 이유다
    chal = (packet.get("challenge") or {}) if revision else {}
    user = json.dumps({
        "facts": brief,
        "analysts": sorted(nodes),
        "thesis": str(packet.get("thesis", ""))[:600],
        "skeptic_반박": {k: chal.get(k) for k in
                        ("alternative_explanations", "weakest_claim",
                         "what_would_overturn_it")} if chal else None,
        "지시": ("반박을 반영해 해석을 다시 하라" if revision
                else f"최대 {MAX_INSIGHTS}개"),
    }, ensure_ascii=False)

    try:
        raw = _ollama_chat(_INTERPRET_SYSTEM, user)
        parsed = json.loads(extract_json(raw))
    except Exception as e:  # noqa: BLE001
        # 해석 실패가 Packet 실패는 아니다. 다만 침묵하지 않는다.
        return {"insights": [], "insight_rejected": [],
                "insight_note": f"해석 미실행: {type(e).__name__}"}

    raw_items = (parsed.get("insights") or [])[:MAX_INSIGHTS]
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        # 참조한 Fact 로부터 분석가를 채운다. LLM 이 채웠으면 그것도 합치되
        # **없는 분석가는 넣지 않는다** - 참조 무결성은 계약이 다시 본다.
        derived = {fact_node.get(str(f)) for f in
                   (item.get("supporting_fact_ids") or [])}
        given = {str(n) for n in (item.get("source_nodes") or [])}
        merged = sorted((derived | given) & nodes)
        if merged:
            item["source_nodes"] = merged

    ok, rejected = validate_insights(raw_items, fact_ids=set(facts),
                                     known_nodes=nodes)
    return {
        "insights": [i.model_dump() for i in ok],
        # ▶ 거부를 조용히 버리지 않는다 - 왜 인사이트가 안 나오는지 보여야
        #   다음에 프롬프트를 고칠 수 있다
        "insight_rejected": rejected,
        "insight_claims": to_claims(ok, symbol=str(state.get("symbol") or "")),
        "insight_note": f"채택 {len(ok)} / 거부 {len(rejected)}"
                        + (f" (재해석 {revision}회)" if revision else ""),
    }


def needs_revision(state: ResearchState) -> str:
    """반박이 실질적인데 해석이 그것을 안 다뤘으면 되돌아간다.

    ▶ **LangGraph 가 처음으로 값을 하는 자리다.** 지금까지 그래프는 선형
      간선뿐이라 함수를 순서대로 부르는 것과 다를 게 없었다. 되돌아가는 경로가
      있어야 심의(deliberation)가 되고, 심의가 있어야 판단이 깊어진다.

    판정은 결정론이다. "다시 할까?" 를 LLM 에게 물으면 매번 다르게 답하고
    재현이 깨진다.
    """
    if int(state.get("revisions") or 0) >= MAX_REVISIONS:
        return "done"
    packet = state.get("packet") or {}
    conflicts = (packet.get("disagreements") or
                 (packet.get("challenge") or {}).get("disagreements") or [])
    if not conflicts:
        return "done"
    # 반박이 지목한 분석가를 해석이 실제로 가로질렀는가
    flagged = set()
    for c in conflicts:
        if isinstance(c, dict):
            flagged |= {str(x) for x in (c.get("nodes") or []) if x}
    covered = set()
    for ins in (state.get("insights") or []):
        covered |= {str(n) for n in (ins.get("source_nodes") or [])}
    return "revise" if (flagged and not (flagged & covered)) else "done"


def bump_revision(state: ResearchState) -> dict:
    """재해석 횟수를 올린다. **상한이 없으면 순환이 멈추지 않는다.**"""
    return {"revisions": int(state.get("revisions") or 0) + 1}


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def build_pipeline():
    g = StateGraph(ResearchState)
    g.add_node("check_universe", check_universe)
    g.add_node("check_data_quality", check_data_quality)
    g.add_node("assemble_evidence", assemble_evidence)
    g.add_node("analyze_sentiment", analyze_sentiment)
    g.add_node("analyze_technical", analyze_technical)
    g.add_node("analyze_fundamental", analyze_fundamental)
    g.add_node("analyze_regime", analyze_regime)
    g.add_node("analyze_geopolitical", analyze_geopolitical)
    g.add_node("analyze_microstructure", analyze_microstructure)
    g.add_node("draft_packet", draft_packet)
    g.add_node("challenge_packet", challenge_packet)
    g.add_node("interpret", interpret)
    g.add_node("bump_revision", bump_revision)
    g.add_node("bump_gap_fill", bump_gap_fill)
    g.set_entry_point("check_universe")
    # 거래 불가면 즉시 종료 - 죽은 종목에 분석 비용을 쓰지 않는다
    g.add_conditional_edges("check_universe",
                            lambda s: "END" if s.get("halted") else "go",
                            {"END": END, "go": "check_data_quality"})
    # RES-02 품질 게이트가 Evidence 조립 **앞**에 선다. 뒤에 두면 이미 나쁜
    # 데이터로 계산을 다 한 뒤에 막는 셈이라 게이트가 아니라 사후 라벨이다.
    g.add_conditional_edges("check_data_quality",
                            lambda s: "END" if s.get("halted") else "go",
                            {"END": END, "go": "assemble_evidence"})
    # 분석가 6인은 순차다 - GPU 하나에 모델 하나(agent-research 공유)라
    # LLM 호출은 어차피 직렬화된다. 형태만 병렬로 꾸미지 않는다.
    g.add_edge("assemble_evidence", "analyze_sentiment")
    g.add_edge("analyze_sentiment", "analyze_technical")
    g.add_edge("analyze_technical", "analyze_fundamental")
    g.add_edge("analyze_fundamental", "analyze_regime")
    # 레짐(국내 단면) 다음에 지정학(외생 환경) - 안에서 밖으로 넓히는 순서
    g.add_edge("analyze_regime", "analyze_geopolitical")
    g.add_edge("analyze_geopolitical", "analyze_microstructure")
    # Evidence Gap Loop - 분석가가 판정을 못 냈으면 근거를 더 모아 전원이
    # 다시 본다. 한 분석가만 다시 돌리면 Packet 안에서 시점이 갈라진다.
    g.add_conditional_edges("analyze_microstructure", needs_gap_fill,
                            {"fill": "bump_gap_fill", "done": "draft_packet"})
    g.add_edge("bump_gap_fill", "assemble_evidence")
    g.add_edge("draft_packet", "interpret")
    # 해석 -> 반박 -> (필요하면) 재해석. **이 순환이 LangGraph 를 쓰는 이유다.**
    # 지금까지 그래프는 선형 간선뿐이라 함수를 순서대로 부르는 것과 다를 게
    # 없었다. 되돌아가는 경로가 있어야 심의가 되고, 심의가 있어야 판단이 깊어진다.
    g.add_edge("interpret", "challenge_packet")
    g.add_conditional_edges("challenge_packet", needs_revision,
                            {"revise": "bump_revision", "done": END})
    g.add_edge("bump_revision", "interpret")
    return g.compile()


def _node_models() -> dict:
    """노드별 실사용 모델 - 각 에이전트 모듈의 MODEL 상수에서 직접 읽는다
    (env 기본값을 여기 복제하면 배정 변경 시 어긋난다)."""
    models = {"supervisor": SUPERVISOR_MODEL}
    for node, mod in (("sentiment", "news_sentiment_analyst"),
                      ("technical", "technical_analyst"),
                      ("fundamental", "fundamental_analyst"),
                      ("regime", "sector_regime_analyst"),
                      ("geopolitical", "geopolitical_analyst"),
                      ("microstructure", "microstructure_analyst")):
        try:
            models[node] = getattr(__import__(mod), "MODEL", None)
        except Exception:  # noqa: BLE001 - intentional fallback boundary
            models[node] = None
    return models


# ── 선순환 1단: 반증 가능한 주장 발행 (코드가 만든다) ─────────────────────
# 재일님 지시 2026-08-01 "실제 투자 판단 -> 결과에 영향을 줄 때 선순환".
# Packet 의 산문 무효화 조건("연계성 약화 가능성")은 사람도 판정이 갈려
# 채점되지 않는다. 채점 가능한 주장은 코드가 결정론으로 발행한다 -
# LLM 에게 자기 채점 기준을 쓰게 두면 맞히기 쉬운 조건 쪽으로 굽는다.
# 총괄 프롬프트 상한 - 분석가를 늘려도 총괄이 깨지지 않게 하는 방어선
DIGEST_SUMMARY_CHARS = 240
DIGEST_CAUTION_CHARS = 140
DIGEST_MAX_CAUTIONS = 2
DIGEST_MAX_NUMBERS = 10

CLAIM_HORIZONS = (5, 20)          # 거래일. 5일=단기 반응, 20일=한 달 검증
DRAWDOWN_PCT = 10.0               # 기준가 대비 하락 발동선
RALLY_PCT = 10.0


def build_packet_claims(state: dict, packet: dict) -> list[dict]:
    """Packet 시점 상태 -> 기계가 채점할 수 있는 주장 목록 (순수 함수).

    기준가가 없으면 가격 주장을 만들지 않는다 - 기준 없는 채점은 무의미하고,
    없는 기준을 추정해 넣으면 통계 전체가 오염된다.
    """
    claims: list[dict] = []
    price = ((state.get("evidence") or {}).get("price_context") or {})
    # 키 이름은 bundle.compute_price_context 계약이다("last_close"). 실측
    # 2026-08-02: "close" 로 잘못 읽어 가격 주장이 통째로 발행되지 않았고,
    # 그런데도 조용히 넘어갔다 - 그래서 아래 자체점검이 이 키를 못 박는다.
    base = price.get("last_close")
    try:
        base = float(base) if base is not None else None
    except (TypeError, ValueError):
        base = None

    if base and base > 0:
        for h in CLAIM_HORIZONS:
            claims.append({"kind": "PRICE_DRAWDOWN", "metric": "close", "op": "<=",
                           "threshold": round(base * (1 - DRAWDOWN_PCT / 100), 4),
                           "baseline": base, "horizon_days": h,
                           "source_node": "price_context"})
            claims.append({"kind": "PRICE_RALLY", "metric": "close", "op": ">=",
                           "threshold": round(base * (1 + RALLY_PCT / 100), 4),
                           "baseline": base, "horizon_days": h,
                           "source_node": "price_context"})

    regime = (state.get("regime") or {}).get("verdict")
    if regime and regime != "INSUFFICIENT_DATA":
        claims.append({"kind": "REGIME_FLIP", "metric": "regime_label", "op": "!=",
                       "threshold_text": regime, "baseline_text": regime,
                       "horizon_days": 20, "source_node": "regime"})

    geo = (state.get("geopolitical") or {}).get("verdict")
    if geo and geo not in ("INSUFFICIENT_DATA", "SHOCK"):
        # 이미 SHOCK 이면 "SHOCK 으로 악화"는 발동 불가라 주장이 되지 않는다
        claims.append({"kind": "GEO_ESCALATION", "metric": "geo_risk_label",
                       "op": "==", "threshold_text": "SHOCK",
                       "baseline_text": geo, "horizon_days": 20,
                       "source_node": "geopolitical"})
    # ▶ 사전 확률·반증 조건을 붙인다 (2026-08-03, P0)
    #   확률이 없으면 Brier Score·Calibration Error 를 원리적으로 못 센다 -
    #   발동 여부만 세면 과신하는 분석가와 소심한 분석가가 구분되지 않는다.
    #   **코드가 낸다** - LLM 이 자기 확률을 쓰면 맞히기 쉬운 쪽으로 굽는다
    #   (origin='code' 와 같은 이유).
    # 주장 -> 방법 귀속. 분석가 readout 의 method_keys 가 출처다.
    # **안 쓴 기법을 썼다고 기록하지 않는다** - 귀속이 거짓이면 그걸로 계산한
    # validated 도 거짓이 된다. 여러 방법이 기여했으면 첫 것을 대표로 쓰고,
    # 전체는 나중에 다대다로 넓힌다(지금은 열이 하나다).
    by_node = {}
    for node in ("regime", "geopolitical", "microstructure", "fundamental",
                 "technical"):
        keys = ((state.get(node) or {}).get("readout") or {}).get("method_keys")
        if keys:
            by_node[node] = list(keys)

    closes = price.get("closes") or []
    for c in claims:
        node_methods = by_node.get(c.get("source_node") or "")
        if node_methods:
            c["method_key"] = node_methods[0]
        prob, method = probability_for_claim(c, closes)
        if prob is not None:
            c["probability"] = prob
            c["probability_method"] = method
            c["method_key"] = method
        c["falsification_note"] = falsification_note(c)
    return claims


def _record_packet_claims(*, trace_id: str, symbol: str, claims: list[dict],
                          conn=None, as_known_at=None) -> int:
    """research.packet_claims 적재. 기록 실패가 Packet 을 죽이지 않는다.

    as_known_at 은 이 주장을 발행할 때의 증거 컷오프다 - 사후 채점이
    hindsight 로 오염되지 않았는지 확인하는 기준이라 주장과 함께 남긴다.
    """
    if not claims:
        return 0
    try:
        own = conn is None
        if own:
            import psycopg2
            from source_registry import load_project_env

            conn = psycopg2.connect(load_project_env()["DATABASE_URL"],
                                    connect_timeout=10)
        with conn.cursor() as cur:
            for c in claims:
                cur.execute(
                    """
                    insert into research.packet_claims
                      (trace_id, symbol, kind, metric, op, threshold,
                       threshold_text, baseline, baseline_text, horizon_days,
                       source_node, as_known_at, probability,
                       probability_method, method_key, falsification_note)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s)
                    on conflict (trace_id, kind, metric, horizon_days) do nothing
                    """,
                    (trace_id, symbol, c["kind"], c["metric"], c["op"],
                     c.get("threshold"), c.get("threshold_text"),
                     c.get("baseline"), c.get("baseline_text"),
                     c["horizon_days"], c.get("source_node"), as_known_at,
                     c.get("probability"), c.get("probability_method"),
                     c.get("method_key"), c.get("falsification_note")))
        conn.commit()
        if own:
            conn.close()
        return len(claims)
    except Exception as e:  # noqa: BLE001 - intentional fallback boundary
        print(f"⚠ packet_claims 기록 실패(Packet 은 정상): {type(e).__name__}: {e}",
              file=sys.stderr)
        return 0


def _record_pipeline_run(*, symbol: str, trace_id: str, started, ended,
                         status: str, packet: dict | None = None,
                         halt_reason: str | None = None, conn=None,
                         analyst_verdicts: dict | None = None) -> str | None:
    """research.pipeline_runs 기록 - 기록 실패가 Packet 을 죽이면 안 된다.

    audit.agent_runs 는 workforce 프로필 FK(HR 권한)가 선행이라 우리 소유
    스키마에 기록한다(마이그레이션 001300 주석). 좌표·지문만 남긴다.
    """
    import hashlib

    models = _node_models()
    input_hash = hashlib.sha256(
        f"{symbol}|{PIPELINE_VERSION}|{json.dumps(models, sort_keys=True)}".encode()
    ).hexdigest()
    packet_hash = None if packet is None else hashlib.sha256(
        json.dumps(packet, sort_keys=True, ensure_ascii=False,
                   default=str).encode()).hexdigest()
    nc = (packet or {}).get("numeric_check") or {}
    dc = (packet or {}).get("date_check") or {}
    try:
        own = conn is None
        if own:
            import psycopg2
            from source_registry import load_project_env

            conn = psycopg2.connect(load_project_env()["DATABASE_URL"],
                                    connect_timeout=10)
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into research.pipeline_runs
                  (trace_id, pipeline_version, symbol, input_hash, node_models,
                   packet_hash, evidence_quality, numeric_check_ok, status,
                   halt_reason, started_at, ended_at, as_known_at, metadata)
                values (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s::jsonb)
                returning pipeline_run_id
                """,
                (trace_id, PIPELINE_VERSION, symbol, input_hash,
                 json.dumps(models), packet_hash,
                 (packet or {}).get("evidence_quality"),
                 nc.get("ok") if nc else None, status, halt_reason,
                 started, ended,
                 # as_known_at = 증거 컷오프. 이 실행이 "무엇까지 알 수 있었나"
                 # 의 경계다. 지금은 진입 시각(started)이 그 경계다 - 분석가
                 # 일부가 아직 as_of 를 안 받아 최신을 보기 때문이다.
                 # **기록이 먼저다**: 기록이 없으면 무엇이 파라미터화됐는지조차
                 # 확인할 수 없고, 사후 채점이 hindsight 로 오염됐는지도 모른다.
                 started,
                 # 판정 스냅샷 - 사후 채점(packet_outcome_scorer)이 "이 분석가가
                 # 그때 뭐라고 했는지"를 여기서 읽는다. 없으면 되먹임 통계
                 # (analyst_calibration)가 통째로 비어 선순환이 끊긴다.
                 # date_check 를 함께 남긴다 - 리포트에만 찍고 DB 에 없으면
                 # "그때 시점 가드가 뭐라고 했나" 를 사후에 물을 수 없다.
                 json.dumps({"analyst_verdicts": analyst_verdicts or {},
                             "date_check": dc},
                            ensure_ascii=False)))
            run_id = str(cur.fetchone()[0])
        conn.commit()
        if own:
            conn.close()
        return run_id
    except Exception as e:  # noqa: BLE001 - intentional fallback boundary
        print(f"⚠ pipeline_runs 기록 실패(Packet 은 정상): {type(e).__name__}: {e}",
              file=sys.stderr)
        return None


# ── 학습 계층 1단: 실행 중 사건을 남긴다 ─────────────────────────────────────
# 우리 파이프라인은 인터페이스·추론·실행·메모리는 있는데 **학습이 없었다** -
# 같은 결함을 겪어도 다음 실행이 그걸 모른다. 사건을 남겨야 반복이 보이고,
# 반복이 보여야 부서가 절차로 굳힐 수 있다(agents/skill_forge.py).
#
# 무엇을 남길지는 **코드가 정한다.** "오늘 뭐가 아쉬웠어?" 를 LLM 에게 물으면
# 매번 다른 답이 나와 셀 수 없다 - 셀 수 없으면 반복도 없다.
_OCCURRENCE_LOG = Path(__file__).resolve().parent / "var" / "occurrences.jsonl"


def collect_occurrences(out: dict, *, run_id: str, symbol: str,
                        at: str) -> list[dict]:
    """실행 상태 -> 사건 목록. 순수 함수."""
    ev: list[dict] = []

    def _add(kind: str, detail: str) -> None:
        ev.append({"kind": kind, "detail": detail[:180],
                   "run_id": run_id, "symbol": symbol, "at": at})

    for node in ("sentiment", "technical", "fundamental", "regime",
                 "geopolitical", "microstructure"):
        st = out.get(node) or {}
        if not st:
            _add("분석가 미실행", f"{node} 노드가 상태를 내지 않았다")
            continue
        # 서술이 비면 리포트에서 그 분석가가 통째로 사라진다
        if not (((st.get("note") or {}).get("summary")) or st.get("summary")):
            _add("분석가 서술 폐기", f"{node} 가 서술 없이 끝났다")
        ro = st.get("readout") or {}
        for name in (ro.get("unavailable") or [])[:6]:
            # ▶ 미확인 지표는 **지표별로** 센다. "무언가 없었다" 로 뭉치면
            #   어느 배관이 막혔는지 영원히 안 보인다
            _add(f"지표 미확인:{name}", f"{node} 의 {name} 를 계산하지 못했다")
    for t in (out.get("tool_results") or []):
        if isinstance(t, dict) and t.get("ok") is False:
            _add(f"도구 실패:{t.get('tool')}", str(t.get("reason") or "")[:120])
    q = (out.get("packet") or {}).get("_quote_quality") or {}
    for m in (q.get("mismatched") or [])[:6]:
        _add("수치 불일치", f"확정치 풀에 없는 수치 {m}")
    return ev


def append_occurrences(events: list[dict], *,
                path: Path | None = None) -> int:
    """사건을 누적한다. **실패해도 파이프라인을 죽이지 않는다** - 학습 기록이
    없다고 오늘 리포트를 못 내는 것은 우선순위가 뒤집힌 것이다."""
    if not events:
        return 0
    tgt = path or _OCCURRENCE_LOG
    try:
        tgt.parent.mkdir(parents=True, exist_ok=True)
        with tgt.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + chr(10))
        return len(events)
    except OSError:
        return 0


def fetch_method_performance(used_keys: list[str], *,
                             get: Optional[Callable] = None) -> dict:
    """방법별 사후 성과를 되읽어 이번 근거의 성적을 붙인다 (학습 계층 2단).

    ▶ 이 한 칸이 비어 자가 발전이 안 됐다. 주장 발행(packet_claims.method_key),
      사후 채점(packet_outcome_scorer), 집계(research.method_calibration)까지
      다 있었는데 **그 숫자를 다음 실행이 읽지 않았다** - 적중률 30% 짜리와
      70% 짜리를 리포트가 똑같은 어조로 인용했다.

    실패는 비치명이다. 성적을 못 읽었다고 오늘 리포트를 못 내면 안 된다 -
    다만 **못 읽었다는 사실을 남긴다.** 조용히 빈 값을 주면 "성적이 없는 것"과
    "성적이 나쁜 것"이 구분되지 않는다.
    """
    from evidence.method_performance import grade_all, performance_note

    if not used_keys:
        return {"available": False, "reason": "이번 실행이 쓴 method_key 가 없다"}
    try:
        rows = (get or _get)(f"{RESEARCH_API}/methods/performance?min_scored=1")
    except Exception as e:
        return {"available": False,
                "reason": f"성과 조회 실패: {type(e).__name__}"}
    rows = rows if isinstance(rows, list) else []
    note = performance_note(grade_all(rows), used_keys=used_keys)
    note["available"] = True
    return note


def run_research_department(symbol: str) -> dict:
    """본부 단독 실행 - QA 부서의 run_qa_department 와 같은 외부 인터페이스."""
    import uuid

    trace_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc)
    try:
        out = build_pipeline().invoke({"symbol": symbol})
    except Exception as e:
        _record_pipeline_run(symbol=symbol, trace_id=trace_id, started=started,
                             ended=datetime.now(timezone.utc), status="FAILED",
                             halt_reason=f"{type(e).__name__}: {e}"[:200])
        raise
    ended = datetime.now(timezone.utc)
    if out.get("halted"):
        _record_pipeline_run(symbol=symbol, trace_id=trace_id, started=started,
                             ended=ended, status="HALTED",
                             halt_reason=str(out["halted"])[:200])
        return {"symbol": symbol, "verdict": "HALTED", "reason": out["halted"],
                "trace_id": trace_id}
    packet = out["packet"]
    packet["trace_id"] = trace_id
    # 증거 컷오프. 진입 시각을 그대로 쓴다 - 분석가 일부가 아직 as_of 를 안 받아
    # 최신을 보므로 '이 시각 이후 관측은 안 썼다'가 아니라 '이 시각에 실행했다'가
    # 지금의 정확한 뜻이다. 그래도 기록해야 나중에 무엇이 파라미터화됐는지
    # 확인할 수 있고, 사후 채점이 hindsight 인지 아닌지도 이 값으로 따진다.
    as_known_at = started
    packet["as_known_at"] = as_known_at.isoformat()
    packet["_analyst_verdicts"] = {          # 리포트용 메타 (Packet 본문과 분리)
        "sentiment": (out.get("sentiment") or {}).get("verdict"),
        "technical": (out.get("technical") or {}).get("verdict"),
        "fundamental": (out.get("fundamental") or {}).get("verdict"),
        "regime": (out.get("regime") or {}).get("verdict"),
        "geopolitical": (out.get("geopolitical") or {}).get("verdict"),
        "microstructure": (out.get("microstructure") or {}).get("verdict"),
    }
    _record_pipeline_run(
        symbol=symbol, trace_id=trace_id, started=started, ended=ended,
        status="COMPLETED", packet=packet,
        analyst_verdicts={k: v for k, v in packet["_analyst_verdicts"].items() if v})
    # 분석가 서술 - 리포트를 풍성하게. 검증(환각·수치·라벨)을 통과한 문장만 온다
    # ▶ 핵심 수치는 **코드가** 싣는다 (2026-08-03 실측 개선)
    #   계측 결과: 미시구조 재료 25개 중 LLM 이 3개만 인용(12%), 지정학 18개 중
    #   3개(18%). 서술이 "2~3문장 600자"로 묶여 있어 애초에 다 못 쓴다 -
    #   모델을 탓할 문제가 아니다. 그런데 Packet 에는 summary 만 실리므로
    #   나머지 22개는 계산해놓고 버려졌다. 무엇이 중요한 재료인지도 판정의
    #   일부이므로 코드가 골라 그대로 싣는다(판정은 코드, 서술만 LLM).
    packet["_analyst_notes"] = {
        node: {"summary": ((out.get(key) or {}).get("note") or {}).get("summary")
                          or (out.get(key) or {}).get("summary"),
               "cautions": ((out.get(key) or {}).get("note") or {}).get("cautions")
                           or (out.get(key) or {}).get("cautions") or [],
               "highlights": pick_highlights(
                   (out.get(key) or {}).get("readout"),
                   flags=((out.get(key) or {}).get("readout") or {}).get("flags") or [])}
        for node, key in (("technical", "technical"),
                          ("fundamental", "fundamental"),
                          ("regime", "regime"),
                          ("geopolitical", "geopolitical"),
                          ("microstructure", "microstructure"))
    }
    # 선순환 1단: 반증 가능한 주장을 발행해 남긴다. 채점은 지평이 지난 뒤
    # collectors/packet_outcome_scorer.py 가 시세로 대조한다.
    claims = build_packet_claims(out, packet)
    # ▶ **해석도 채점 대상이다.** 반증 조건과 지평이 있으므로 같은 경로로
    #   발행한다 - 자가 발전의 대상이 지표에서 판단으로 넓어지는 지점이다.
    #   생성만 하고 발행을 안 하면 인사이트는 영원히 성적이 안 매겨진다.
    insight_claims = out.get("insight_claims") or []
    claims = claims + insight_claims
    packet["_insights"] = out.get("insights") or []
    packet["_insight_rejected"] = out.get("insight_rejected") or []
    packet["_insight_note"] = out.get("insight_note")
    packet["_revisions"] = int(out.get("revisions") or 0)
    packet["_claims"] = claims
    packet["_claims_recorded"] = _record_packet_claims(
        trace_id=trace_id, symbol=symbol, claims=claims,
        as_known_at=as_known_at)
    # 선순환 2단: 이번 실행에서 무엇이 막혔는지 남긴다. 같은 것이 서로 다른
    # 실행에서 3번 반복되면 skill_forge 가 절차로 굳힌다.
    packet["_occurrences_logged"] = append_occurrences(collect_occurrences(
        out, run_id=trace_id, symbol=symbol, at=started.isoformat()))
    # 선순환 3단: 이번에 쓴 방법들의 **과거 성적**을 붙인다. 리포트가 스스로
    # "이 신호는 최근 성적이 나쁘다" 고 말하면 그것만으로 판단의 질이 오른다.
    packet["_method_performance"] = fetch_method_performance(
        sorted({c["method_key"] for c in claims if c.get("method_key")}))
    return packet


def _render_packet_md(packet: dict, *, now: str | None = None) -> str:
    """Packet 을 그대로 옮겨 적는 순수 함수 - 동규님 리스크본부 리포트 패턴
    (departments/03-risk/scripts.py _render_report_md, 2026-08-01) 채택.
    LLM 이 리포트 구조·내용을 창작하지 않는다. now 주입은 자체점검 결정성용."""
    _mp = packet.get("_method_performance") or {}
    nc = packet.get("numeric_check") or {}
    dc = packet.get("date_check") or {}
    qq = (nc.get("quote_quality") or {})
    verdicts = packet.get("_analyst_verdicts") or {}
    lines = [
        "# 리서치본부 — Research Packet (결정론적 생성, LLM 자유 서술 아님)",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| **symbol** | `{packet.get('symbol')}` |",
        f"| **evidence_quality** | **{packet.get('evidence_quality')}** |",
        (f"| **수치 재대조** | {'통과' if nc.get('ok') else '⚠ 불일치 ' + str(nc.get('unmatched', []) + nc.get('unmatched_counts', []))} "
        f"(% {nc.get('checked', 0)}건 / 셈단위 {nc.get('checked_counts', 0)}건) |"),
        # ▶ 인용 품질 - **통과가 검증인지 우연인지**를 드러낸다 (2026-08-03)
        #   지표 확장(10 -> 31)으로 확정치 풀이 넓어지면 창작 수치가 우연히 맞는
        #   일이 생긴다. "불일치 0" 만 보면 좋아 보이지만, 그중 몇이 여러 지표에
        #   동시에 걸린 모호한 인용인지가 진짜 품질이다.
        (f"| **인용 품질** | 확정치 풀 {nc.get('pool_size', 0)}개 · 인용 "
        f"{qq.get('quoted', 0)}건 중 {qq.get('matched', 0)}건 매칭"
        f"{', 모호 ' + str(qq.get('ambiguous', 0)) + '건(' + str(round(qq.get('ambiguity_ratio', 0) * 100)) + '%)' if qq.get('ambiguous') else ''} |"),
        # 시점 재대조를 리포트에 싣는다 - **표시 없는 가드는 없는 가드다**
        # (2026-08-03: 가드가 돌고도 리포트·DB 어디에도 안 남아 못 봤다)
        (f"| **시점 재대조** | {'통과' if dc.get('ok', True) else '⚠ Evidence 창 밖 연도 ' + str(dc.get('too_old_years', []) + dc.get('future_years', []))}"
        f" (창 {dc.get('window', '-')}) |"),
        "| **분석가 판정** | " + " · ".join(
            f"{k}={v}" for k, v in verdicts.items() if v) + " |",
        # ▶ **이번 근거의 과거 성적** (학습 계층 2단). 적중률 30% 짜리와 70%
        #   짜리를 같은 어조로 인용하던 것을 드러낸다. 성적이 '없는' 것과
        #   '나쁜' 것을 구분해 적는다 - 새 지표가 표본 없다는 이유로 나쁜
        #   기법처럼 읽히면 아무도 새 지표를 안 넣는다.
        (f"| **근거 성적** | "
         + (("우수 " + ", ".join(_mp["strong_methods"]) + " · ") if _mp.get("strong_methods") else "")
         + (("⚠ 저조 " + ", ".join(_mp["weak_methods"]) + " · ") if _mp.get("weak_methods") else "")
         + (f"미채점 {len(_mp['unscored_methods'])}종" if _mp.get("unscored_methods") else "")
         + " |") if _mp.get("available") else
        f"| **근거 성적** | 미확인 ({_mp.get('reason', '조회 안 함')}) |",
        f"| **생성** | {PIPELINE_VERSION}, {now or datetime.now(timezone.utc).isoformat()} |",
        "",
        "## Thesis", "", str(packet.get("thesis", "")), "",
    ]
    # ── 해석 (인사이트) ─────────────────────────────────────────────────────
    # 수치 나열과 분리해 싣는다. 검증 방식이 다르므로 읽는 사람도 다르게
    # 읽어야 한다 - Fact 는 확정치 대조를 통과한 것이고, 해석은 **반증 가능한
    # 가설**이다. 섞어 놓으면 가설이 사실처럼 읽힌다.
    _ins = packet.get("_insights") or []
    if _ins:
        lines += ["## 인사이트 (교차 해석)", "",
                  f"_{len(_ins)}건. 각 해석은 서로 다른 분석가 2인 이상의 "
                  f"근거를 가로지르며 **반증 조건**을 함께 낸다 - 틀릴 수 있어야 "
                  f"분석이다._", ""]
        for k, ins in enumerate(_ins, 1):
            lines += [
                f"**{k}. [{ins.get('kind')}]** {ins.get('claim')}",
                f"- 근거: {', '.join(ins.get('source_nodes') or [])} "
                f"({len(ins.get('supporting_fact_ids') or [])}개 Fact)",
                f"- **반증 조건**: {ins.get('falsifier')}",
                f"- 지평 {ins.get('horizon_days')}일 · 신뢰도 {ins.get('confidence')}",
                "",
            ]
    _rej = packet.get("_insight_rejected") or []
    if _rej:
        # ▶ 거부를 숨기지 않는다. 왜 인사이트가 적은지 보여야 프롬프트를 고친다
        lines += [f"_거부된 해석 {len(_rej)}건: "
                  + "; ".join(str(r.get("reason", ""))[:70] for r in _rej[:3])
                  + "_", ""]
    if packet.get("_revisions"):
        lines += [f"_반박을 받아 해석을 {packet['_revisions']}회 재작성했다._", ""]
    lines += [
    ]
    for title, key in (("사실 (facts)", "facts"),
                       ("해석 (interpretation)", "interpretation"),
                       ("촉매 (catalysts)", "catalysts"),
                       ("무효화 조건 (invalidation)", "invalidation")):
        lines += [f"## {title}", ""]
        lines += [f"- {x}" for x in (packet.get(key) or [])] or ["- (없음)"]
        lines += [""]

    # ▶ 격리된 창작 의심 문장. **지우지 않고 드러낸다** - 무엇이 왜 빠졌는지
    #   보여야 사람이 판단할 수 있고, 조용히 사라지면 가드를 신뢰할 수 없다.
    qz = packet.get("_quarantined") or []
    if qz:
        lines += ["## 격리된 문장 (창작 의심 — facts 에서 제외)", ""]
        lines += [f"- ~~{q.get('text', '')}~~ — {q.get('reason', '')}" for q in qz]
        if packet.get("_quarantine_emptied_facts"):
            lines += ["", ("> ⚠ 근거 있는 사실이 0건이라 evidence_quality 를 "
                          "insufficient_evidence 로 적었다")]
        lines += [""]

    # ▶ 반박 (Skeptic). **내용이 핵심이다** - 갈등 건수만 세는 것은 대화가 아니다.
    #   총괄이 동의하든 안 하든 지우지 못한다(마스터플랜: 반대 의견을 삭제하지 않는다).
    dg = packet.get("disagreements") or {}
    dissent = packet.get("dissent") or []
    cv = packet.get("_challenge_verification") or {}
    lines += ["## 반박 (Skeptic — 분석가 간 대화)", ""]
    if dg:
        lines += [(f"- 코드가 찾은 갈등 **{dg.get('count', 0)}건** "
                  f"(정반대 {dg.get('opposite', 0)} / 신호↔맥락 "
                  f"{dg.get('signal_vs_context', 0)})")]
        lines += [f"  - {x}" for x in (dg.get("lines") or [])]
    if dissent:
        lines += ["", "**대안 설명과 반대 근거**", ""]
        lines += [f"- {x}" for x in dissent]
    else:
        lines += ["", "- (반박 없음)"]
    if cv.get("ok") is False:
        lines += ["", f"> ⚠ 반박 서술에 확정치 밖 수치: {cv.get('unmatched')}"]
    elif cv.get("ok") is None and cv.get("reason"):
        lines += ["", f"> ⚠ {cv['reason']}"]
    if packet.get("_downgraded_by_challenge"):
        lines += ["", (f"> 치명적 반증으로 evidence_quality 강등: "
                      f"{packet['_downgraded_by_challenge']} → "
                      f"{packet.get('evidence_quality')}")]
    lines += [""]

    # 분석가 원문 소견 - 총괄이 압축하며 버린 맥락이 여기 남는다. 상충하는
    # 소견을 지우지 않는 것이 본부 원칙이라, 요약본 옆에 원문을 같이 싣는다.
    # 서술이 없어도(LLM 실패·침묵) **핵심 수치가 있으면 싣는다.** 예전에는
    # summary 가 없으면 그 분석가를 통째로 뺐는데, 그러면 결정론 계산 결과까지
    # 같이 사라진다 - LLM 이 죽었다고 코드가 만든 재료를 버릴 이유가 없다
    # (실측 2026-08-03: 5인 중 3인이 이 필터에 걸려 리포트에서 사라졌다).
    notes = {k: v for k, v in (packet.get("_analyst_notes") or {}).items()
             if v.get("summary") or (v.get("highlights") or {}).get("items")}
    if notes:
        lines += ["## 분석가 소견 원문 (검증 통과분)", ""]
        for node, n in notes.items():
            verdict = verdicts.get(node)
            lines += [f"### {node}" + (f" — `{verdict}`" if verdict else ""), ""]
            lines += ([str(n["summary"]), ""] if n.get("summary")
                      else ["_서술 없음 (LLM 미응답) - 아래 수치는 코드 계산이다_", ""])
            # 코드가 고른 핵심 수치 - LLM 이 문장에 못 담은 재료가 여기로 온다.
            # 실측(2026-08-03): 서술만 실을 때 미시구조는 계산한 25개 중 3개만
            # Packet 에 도달했다. 나머지가 버려지는 것을 막는다.
            h = n.get("highlights") or {}
            if h.get("items") or h.get("flags"):
                if h.get("flags"):
                    lines += [f"- **임계 위반**: {', '.join(h['flags'])}"]
                lines += [f"- **핵심 수치**: {render_highlights(h)}", ""]
            for c in n.get("cautions") or []:
                lines += [f"> ⚠ {c}"]
            if n.get("cautions"):
                lines += [""]

    # 선순환 - 나중에 기계가 채점할 주장. 산문 무효화 조건과 달리 판정이
    # 갈리지 않는다(코드가 발행하고 코드가 채점한다).
    claims = packet.get("_claims") or []
    if claims:
        lines += ["## 검증 예정 주장 (코드 발행 · 사후 자동 채점)", "",
                  "| 종류 | 조건 | 지평 | 사전확률 | 출처 |",
                  "|---|---|---|---|---|"]
        for c in claims:
            tgt = c.get("threshold")
            cond = (f"`{c['metric']} {c['op']} "
                    f"{tgt if tgt is not None else c.get('threshold_text')}`")
            if c.get("baseline") is not None:
                cond += f" (기준 {c['baseline']:,})"
            elif c.get("baseline_text"):
                cond += f" (기준 {c['baseline_text']})"
            # 확률은 코드가 낸 사전 확률(evidence/forecast). 없으면 '-' -
            # 라벨 주장은 변동성 모형의 대상이 아니라 확률을 내지 않는다.
            pr = c.get("probability")
            lines += [(f"| {c['kind']} | {cond} | {c['horizon_days']}거래일 | "
                      f"{f'{pr:.0%}' if pr is not None else '-'} | "
                      f"{c.get('source_node') or '-'} |")]
        notes_f = [c for c in claims if c.get("falsification_note")]
        if notes_f:
            lines += ["", "**반증 조건** (발행 시점 고정 - 사후 해석 방지)", ""]
            lines += [f"- {c['falsification_note']}" for c in notes_f]
        lines += ["",
                  (f"기록 {packet.get('_claims_recorded', 0)}건 — 채점은 "
                  f"`collectors/packet_outcome_scorer.py`, 누적 성과는 "
                  f"`research.analyst_calibration`."), ""]
    return "\n".join(lines)


# ── 자체 점검 (LLM·API 없음) ───────────────────────────────────────────────
def _check_graph_shape():
    p = build_pipeline()
    assert p is not None
    print("  그래프 컴파일            OK")


def _check_halt_short_circuit():
    # 거래 불가 종목이면 Evidence·LLM 을 부르지 않고 끝나는지 - conditional edge
    import universe_manager as um

    class _D:
        excluded: ClassVar[dict[str, str]] = {"999999": "HALTED"}
        def __init__(self):
            self.as_of = datetime.now(timezone.utc)
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
        return json.dumps({"symbol": "X", "thesis": "시험용 논지",
                           "facts": [],
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


def _check_packet_report_renderer():
    """리포트 렌더러 순수성 - 같은 Packet 이면 같은 MD, 수치 불일치는 ⚠ 로 드러난다."""
    pk = {"symbol": "000660", "evidence_quality": "sufficient",
          "thesis": "테스트 논지", "facts": ["사실 1"], "interpretation": ["해석 1"],
          "catalysts": [], "invalidation": ["조건 1"],
          "numeric_check": {"ok": True, "checked": 3, "checked_counts": 1,
                            "unmatched": [], "unmatched_counts": []},
          "_analyst_verdicts": {"sentiment": "SCORED", "technical": "BEARISH"}}
    a = _render_packet_md(pk, now="2026-08-01T00:00:00+00:00")
    b = _render_packet_md(pk, now="2026-08-01T00:00:00+00:00")
    assert a == b, "같은 입력이 다른 리포트를 냈다 - 순수성 위반"
    assert "000660" in a and "테스트 논지" in a and "- 사실 1" in a
    assert "통과" in a and "sentiment=SCORED" in a
    assert "- (없음)" in a                      # catalysts 빈 목록 표기
    bad = dict(pk, numeric_check={"ok": False, "checked": 2, "checked_counts": 0,
                                  "unmatched": [55.5], "unmatched_counts": []})
    assert "⚠ 불일치" in _render_packet_md(bad, now="2026-08-01T00:00:00+00:00")
    print("  Packet 리포트 렌더러     OK")


def _check_run_recorder():
    """실행 기록 - 지문·상태가 정확히 남고, 기록 실패는 Packet 을 죽이지 않는다."""
    captured = {}

    class _Cur:
        def execute(self, sql, params):
            captured["params"] = params
        def fetchone(self):
            return ("11111111-1111-1111-1111-111111111111",)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()
        def commit(self):
            captured["committed"] = True

    ts = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    pk = {"symbol": "X", "evidence_quality": "partial",
          "numeric_check": {"ok": True}}
    rid = _record_pipeline_run(symbol="X", trace_id="t-1", started=ts, ended=ts,
                               status="COMPLETED", packet=pk, conn=_Conn())
    assert rid and captured["committed"]
    p = captured["params"]
    assert p[8] == "COMPLETED" and p[6] == "partial" and p[7] is True
    assert len(p[3]) == 64 and len(p[5]) == 64      # input/packet sha256
    assert "supervisor" in json.loads(p[4])          # node_models 기록
    # 같은 packet 이면 같은 지문 (재현성)
    _record_pipeline_run(symbol="X", trace_id="t-2", started=ts, ended=ts,
                         status="COMPLETED", packet=pk, conn=_Conn())
    h1 = p[5]
    assert captured["params"][5] == h1

    class _Broken:
        def cursor(self):
            raise RuntimeError("DB down")
    assert _record_pipeline_run(symbol="X", trace_id="t-3", started=ts, ended=ts,
                                status="HALTED", conn=_Broken()) is None  # 예외 없음
    print("  실행 기록(pipeline_runs) OK")


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


def _check_schema_coercion():
    """모양 이탈은 교정하고 내용 검증은 그대로 - 분석가 6인 실행을 버리지 않는다."""
    assert flatten_to_str_list(["a", "b"]) == ["a", "b"]
    assert flatten_to_str_list("한 문장") == ["한 문장"]
    # 실측 이탈 모양: dict 항목이 섞인 리스트
    got = flatten_to_str_list([{"claim": "매출 46.76% 증가", "source": "disclosure:5"}])
    assert got == ["claim: 매출 46.76% 증가 | source: disclosure:5"], got
    # 통째로 dict
    assert flatten_to_str_list({"a": "1", "b": "2"}) == ["a: 1", "b: 2"]
    # 값이 없으면 빈 목록이 아니라 None - 없는 내용을 만들어내지 않는다
    for empty in (None, "", "   ", [], {}, ["", "  "]):
        assert flatten_to_str_list(empty) is None, empty

    # 평탄화된 뒤에도 수치 검사가 창작을 잡는지 - 교정이 가드를 무디게 하면 안 된다
    def liar(system, user):
        return json.dumps({
            "symbol": "X", "thesis": "성장 지속",
            "facts": {"revenue": "전년 대비 18.3% 증가"},   # dict 이탈 + 창작 수치
            "interpretation": [], "catalysts": [], "invalidation": [],
            "evidence_quality": "sufficient"})

    out = draft_packet(
        {"symbol": "X", "universe": {}, "sentiment": {"verdict": "SCORED"},
         "fundamental": {"verdict": "NOTED", "readout": {"revenue_yoy_pct": 46.76}},
         "evidence": {"price_context": {"status": "OK"}}}, llm=liar)["packet"]
    assert out["facts"] == ["revenue: 전년 대비 18.3% 증가"], out["facts"]
    assert out["_schema_coerced"] == ["facts"], out.get("_schema_coerced")
    assert not out["numeric_check"]["ok"], "평탄화 후 창작 수치를 놓쳤다"
    assert out["evidence_quality"] == "partial", "강등이 안 걸렸다"
    # 한 겹 싸서 온 Packet 은 벗긴다 - 실측 2회가 '키 전부 누락'으로 죽었다
    inner = {k: "x" for k in PACKET_REQUIRED_KEYS}
    # 근접 키 정규화가 새 dict 를 만드므로 **값**으로 비교한다(is 가 아니라)
    assert unwrap_packet({"packet": inner}) == inner
    assert unwrap_packet({"result": {"data": inner}}) != inner, \
        "두 겹까지 파고들지 않는다 - 추측으로 구조를 바꾸지 않는다"
    assert unwrap_packet(inner) == inner, "이미 평평하면 그대로 둔다"
    # 일부 키만 있는 dict 는 건드리지 않는다(다른 문제이므로 사유가 보여야 한다)
    partial = {"wrap": {"symbol": "x", "thesis": "y"}}
    assert unwrap_packet(partial) == partial

    # ▶ 근접 키 이름 정규화 (실측 2026-08-03: 이것 때문에 2회 연속 거부됐다)
    alias = {"symbol": "X", "thesis": "논지", "key_facts": ["f"],
             "interpretation": [], "invalidation_conditions": ["조건"],
             "evidence_quality": "partial"}
    fixed = unwrap_packet(alias)
    assert fixed["facts"] == ["f"] and fixed["invalidation"] == ["조건"], fixed
    # 계약 키가 이미 있으면 별칭이 덮지 않는다
    assert unwrap_packet(dict(alias, facts=["원본"]))["facts"] == ["원본"]
    assert unwrap_packet(["not", "a", "dict"]) == ["not", "a", "dict"]
    print("  스키마 교정·가드 유지    OK")


def _check_numeric_failure_downgrades_quality():
    """창작 수치 적발이 **등급까지 낮추는지** - 표시만 하면 하류가 안 읽는다.

    실측 2026-08-02: 총괄이 매출 +18.3%(실제 46.76%)를 facts 에 출처까지
    달아 넣었는데 evidence_quality 는 sufficient 로 나갔다.
    """
    def liar(system, user):
        return json.dumps({
            "symbol": "X", "thesis": "매출이 전년 대비 18.3% 늘었다",
            "facts": ["매출 성장률 18.3% (disclosure:5)"],
            "interpretation": [], "catalysts": [], "invalidation": [],
            "evidence_quality": "sufficient"})

    out = draft_packet(
        {"symbol": "X", "universe": {}, "sentiment": {"verdict": "SCORED"},
         "fundamental": {"verdict": "NOTED", "readout": {"revenue_yoy_pct": 46.76}},
         "evidence": {"price_context": {"status": "OK"}}}, llm=liar)
    p = out["packet"]
    assert not p["numeric_check"]["ok"]
    assert p["evidence_quality"] == "partial", \
        f"창작 수치가 있는데 등급이 {p['evidence_quality']} 로 남았다"
    assert p["numeric_check"]["downgraded_from"] == "sufficient"

    # 정직한 Packet 은 건드리지 않는다 - 강등은 낮추기만, 올리진 않는다
    def honest(system, user):
        return json.dumps({
            "symbol": "X", "thesis": "매출이 46.76% 늘었다",
            "facts": ["매출 성장률 46.76%"], "interpretation": [],
            "catalysts": [], "invalidation": [], "evidence_quality": "sufficient"})

    ok = draft_packet(
        {"symbol": "X", "universe": {}, "sentiment": {"verdict": "SCORED"},
         "fundamental": {"verdict": "NOTED", "readout": {"revenue_yoy_pct": 46.76}},
         "evidence": {"price_context": {"status": "OK"}}}, llm=honest)["packet"]
    assert ok["numeric_check"]["ok"] and ok["evidence_quality"] == "sufficient"
    print("  수치 불일치 = 등급 강등  OK")


def _check_claims_use_bundle_key():
    """가격 주장이 실제로 발행되는지 - 키 이름이 어긋나면 조용히 0건이 된다.

    실측 2026-08-02: price_context 의 종가 키는 'last_close' 인데 'close' 로
    읽어 가격 주장이 통째로 빠졌고 아무도 몰랐다(리포트에 레짐 1건만).
    """
    from evidence.bundle import compute_price_context

    ctx = compute_price_context([
        {"bucket_time": f"2026-07-{d:02d}T00:00:00+09:00", "close": 1000.0 + d}
        for d in range(1, 26)])
    assert "last_close" in ctx, "bundle 계약(last_close)이 바뀌었다"

    claims = build_packet_claims(
        {"evidence": {"price_context": ctx},
         "regime": {"verdict": "RISK_ON"},
         "geopolitical": {"verdict": "ELEVATED"}}, {})
    kinds = {c["kind"] for c in claims}
    assert {"PRICE_DRAWDOWN", "PRICE_RALLY", "REGIME_FLIP",
            "GEO_ESCALATION"} == kinds, kinds
    price_claims = [c for c in claims if c["kind"].startswith("PRICE_")]
    assert len(price_claims) == len(CLAIM_HORIZONS) * 2, price_claims
    base = float(ctx["last_close"])
    dd = next(c for c in price_claims if c["kind"] == "PRICE_DRAWDOWN")
    assert abs(dd["threshold"] - base * 0.9) < 1e-6, dd

    # 이미 SHOCK 이면 'SHOCK 으로 악화'는 발동 불가 - 주장이 되지 않는다
    no_geo = build_packet_claims({"evidence": {"price_context": ctx},
                                  "geopolitical": {"verdict": "SHOCK"}}, {})
    assert not any(c["kind"] == "GEO_ESCALATION" for c in no_geo)
    # 기준가가 없으면 가격 주장을 만들지 않는다
    none_ctx = build_packet_claims(
        {"evidence": {"price_context": {"status": "UNAVAILABLE"}}}, {})
    assert not any(c["kind"].startswith("PRICE_") for c in none_ctx)
    print("  주장 발행·기준가 계약    OK")


def _check_occurrence_collection():
    """사건 수집이 **지표 단위로** 세는지. 뭉치면 어느 배관인지 안 보인다."""
    out = {
        "technical": {"note": {"summary": "정상"},
                      "readout": {"unavailable": ["amihud", "kyle_lambda"]}},
        "regime": {"readout": {}},          # 서술 없음
        "tool_results": [{"tool": "breadth_history", "ok": False, "reason": "0건"}],
        "packet": {"_quote_quality": {"mismatched": [4444.4]}},
    }
    ev = collect_occurrences(out, run_id="r1", symbol="005380", at="2026-08-03")
    kinds = {e["kind"] for e in ev}
    assert "지표 미확인:amihud" in kinds and "지표 미확인:kyle_lambda" in kinds, kinds
    assert "분석가 서술 폐기" in kinds, kinds
    assert "도구 실패:breadth_history" in kinds, kinds
    assert "수치 불일치" in kinds, kinds
    assert sum(1 for e in ev if e["kind"] == "분석가 미실행") == 4, ev
    assert all(e["run_id"] == "r1" for e in ev)


def _check_occurrence_log_failure_is_not_fatal():
    """기록 실패가 리포트를 죽이면 우선순위가 뒤집힌 것이다."""
    # ▶ **파일을 부모로 삼는다.** 전에는 "/nonexistent-root-xyz/..." 를 썼는데
    #   Windows 에서 그건 현재 드라이브 루트로 해석돼 mkdir 이 성공했다 -
    #   실패를 검사하는 테스트가 플랫폼에 따라 통과 조건이 달랐다.
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".notadir", delete=False) as f:
        blocker = Path(f.name)
    try:
        bad = blocker / "var" / "occ.jsonl"     # 파일 아래에는 디렉터리를 못 만든다
        assert append_occurrences([{"kind": "k"}], path=bad) == 0
        assert append_occurrences([], path=bad) == 0
    finally:
        blocker.unlink(missing_ok=True)


def _check_method_performance_readback():
    """성과 되읽기가 **없는 것과 나쁜 것을 구분**하는가 (학습 계층 2단).

    조용히 빈 값을 주면 성적이 없는 기법과 나쁜 기법이 같아 보인다. 조회
    실패도 마찬가지 - 못 읽었으면 못 읽었다고 남아야 사람이 판단한다.
    """
    rows = [{"method_key": "momentum_20d", "horizon_days": 20, "scored": 40,
             "trigger_rate": 0.20, "brier_score": None},
            {"method_key": "adx_trend", "horizon_days": 20, "scored": 60,
             "trigger_rate": 0.75, "brier_score": None},
            {"method_key": "신규", "horizon_days": 20, "scored": 2,
             "trigger_rate": 1.0, "brier_score": None}]
    r = fetch_method_performance(["momentum_20d", "adx_trend", "신규", "미채점"],
                                 get=lambda url: rows)
    assert r["available"] is True, r
    assert r["weak_methods"] == ["momentum_20d"], r
    assert r["strong_methods"] == ["adx_trend"], r
    # 표본 2건짜리는 INSUFFICIENT 라 약체가 아니다 - 새 지표를 죽이면 안 된다
    assert "신규" not in r["weak_methods"], r
    # 아예 집계에 없는 것은 이름을 남긴다
    assert "미채점" in r["unscored_methods"], r
    assert "momentum_20d" in (r["caution"] or ""), r

    # 조회 실패는 비치명이되 **사유가 남는다**
    def boom(url):
        raise OSError("연결 거부")
    bad = fetch_method_performance(["x"], get=boom)
    assert bad["available"] is False and "실패" in bad["reason"], bad
    # 쓴 기법이 없으면 그것도 사유로 남는다
    none = fetch_method_performance([], get=lambda url: rows)
    assert none["available"] is False, none


def _check_data_quality_gate():
    """품질 게이트가 **Evidence 조립 앞**에 서고, 행 0을 PASS 로 안 보는가.

    RES-02 는 P0 페르소나인데 감사 결과를 노출하는 엔드포인트가 없어
    파이프라인 어디에도 배선돼 있지 않았다. 게이트를 뒤에 두면 이미 나쁜
    데이터로 계산을 다 한 뒤에 막는 셈이라 게이트가 아니라 사후 라벨이다.
    """
    # 조회를 가짜로 바꿔 네 경우를 본다
    orig = globals()["_get"]
    try:
        def _fake(windows, fresh_missing=0):
            def g(url):
                if "bar_freshness" in url:
                    return {"ok": True, "last_bar_date": "2026-07-31",
                            "symbols": 350}
                if "sessions_since" in url:
                    return {"since": "2026-07-31", "sessions": fresh_missing}
                return windows
            return g

        globals()["_get"] = _fake([
            {"stream_type": "ticks", "quality_status": "FAIL",
             "metrics": {"reasons": ["중복 12%"]}}])
        out = check_data_quality({"symbol": "x"})
        assert out["data_quality"]["status"] == "FAIL", out
        assert out.get("halted"), "FAIL 인데 안 막았다"

        # ▶ **무관한 스트림 장애로 막지 않는다.** 실측: derivatives FAIL 이
        #   주식 종목 분석을 통째로 멈췄다 - 분석가 6인 중 파생을 쓰는
        #   사람이 없다. 과한 게이트는 사람이 곧 꺼버린다.
        globals()["_get"] = _fake([
            {"stream_type": "derivatives", "quality_status": "FAIL",
             "metrics": {"reasons": ["지연"]}}])
        out = check_data_quality({"symbol": "x"})
        assert not out.get("halted"), "무관한 스트림으로 막았다"
        # 다만 **숨기지도 않는다**
        assert out["data_quality"]["failed_unrelated_streams"] == ["derivatives"], out

        globals()["_get"] = _fake([
            {"stream_type": "ticks", "quality_status": "WARN", "metrics": {}}])
        out = check_data_quality({"symbol": "x"})
        assert out["data_quality"]["status"] == "WARN" and not out.get("halted"), out

        # ▶ 행 0 은 PASS 가 아니다 - 감사가 안 돌았다는 뜻이다
        globals()["_get"] = _fake([])
        out = check_data_quality({"symbol": "x"})
        assert out["data_quality"]["status"] == "UNKNOWN", out
        assert "0건" in out["data_quality"]["reason"], out

        # ▶ **봉이 낡으면 막는다.** 틱 품질이 멀쩡해도 일봉이 며칠 비면
        #   분석 전체가 낡은 가격 위에 선다(실측 2026-08-04).
        globals()["_get"] = _fake(
            [{"stream_type": "ticks", "quality_status": "PASS", "metrics": {}}],
            fresh_missing=2)
        out = check_data_quality({"symbol": "x"})
        assert out.get("halted") and "일봉" in out["halted"], out
        # 하루는 수집 지연일 수 있다 - 막지 않되 WARN 으로 남긴다
        globals()["_get"] = _fake(
            [{"stream_type": "ticks", "quality_status": "PASS", "metrics": {}}],
            fresh_missing=1)
        out = check_data_quality({"symbol": "x"})
        assert not out.get("halted") and out["data_quality"]["status"] == "WARN", out

        # 조회 실패도 통과가 아니다
        def boom(url):
            raise OSError("거부")
        globals()["_get"] = boom
        out = check_data_quality({"symbol": "x"})
        assert out["data_quality"]["status"] == "UNKNOWN", out
    finally:
        globals()["_get"] = orig

    # 그래프에서 게이트가 assemble_evidence 앞에 있는가
    src = __import__("pathlib").Path(__file__).read_text(encoding="utf-8")
    i_gate = src.index('g.add_node("check_data_quality"')
    i_asm = src.index('"go": "assemble_evidence"')
    assert i_gate < i_asm, "게이트가 Evidence 조립 뒤에 있다"


def _check_revision_cycle_terminates():
    """**순환이 반드시 끝나는가.** 무한 반복은 최악의 실패다 - 비용을 태우고
    사람이 개입할 때까지 안 멈춘다.

    그리고 되돌아가는 조건이 결정론인지 본다. "다시 할까?" 를 LLM 에게 물으면
    매번 다르게 답하고 재현이 깨진다.
    """
    conflict = {"packet": {"disagreements": [{"nodes": ["technical", "regime"]}]}}

    # 갈등이 있고 해석이 그 축을 안 다뤘으면 되돌아간다
    st = dict(conflict, insights=[{"source_nodes": ["fundamental", "sentiment"]}])
    assert needs_revision(st) == "revise", st

    # 해석이 그 축을 다뤘으면 안 돌아간다 - 이미 답했는데 또 시키면 낭비다
    st2 = dict(conflict, insights=[{"source_nodes": ["technical", "regime"]}])
    assert needs_revision(st2) == "done", st2

    # 갈등이 없으면 안 돌아간다
    assert needs_revision({"packet": {"disagreements": []}}) == "done"

    # ▶ 상한에 닿으면 **무조건** 끝난다. 갈등이 남아 있어도 끝낸다 -
    #   해결 못 한 갈등은 Packet 에 남아 사람이 본다
    st3 = dict(conflict, insights=[], revisions=MAX_REVISIONS)
    assert needs_revision(st3) == "done", st3
    assert bump_revision({"revisions": 0})["revisions"] == 1

    # 실제로 유한한가 - 상한까지 돌리고 멈추는지 시뮬레이션
    state = dict(conflict, insights=[{"source_nodes": ["x", "y"]}], revisions=0)
    steps = 0
    while needs_revision(state) == "revise":
        state["revisions"] = bump_revision(state)["revisions"]
        steps += 1
        assert steps <= MAX_REVISIONS + 1, "순환이 안 멈춘다"
    assert steps == MAX_REVISIONS, steps

    # 그래프에 순환 간선이 실제로 있는가 (선형으로 되돌아가면 의미가 없다)
    src = __import__("pathlib").Path(__file__).read_text(encoding="utf-8")
    assert 'g.add_edge("bump_revision", "interpret")' in src, "순환 간선이 없다"
    assert '"revise": "bump_revision"' in src, "조건부 되돌림이 없다"


def _check_interpret_refuses_thin_input():
    """가로지를 것이 없으면 해석을 만들지 않는다.

    Fact 가 없거나 분석가가 1인이면 '교차 해석' 이 성립하지 않는다. 억지로
    만들면 그 Fact 의 재진술이 되고, 재진술을 인사이트라 부르면 리포트가
    길어지기만 한다.
    """
    r = interpret({"packet": {"facts": []}, "technical": {}, "regime": {}})
    assert r["insights"] == [] and "불가" in r["insight_note"], r
    # 분석가 1인
    r2 = interpret({"packet": {"facts": [{"id": "f1", "claim": "x"}]},
                    "technical": {}})
    assert r2["insights"] == [], r2


def _check_insight_render_and_publish():
    """인사이트가 **리포트에 실리고 채점 행으로 발행되는가.**

    생성만 하고 발행을 안 하면 영원히 성적이 안 매겨진다 - 자가 발전의
    대상이 지표에 머문다. 렌더링도 마찬가지로, 안 보이면 없는 것과 같다.
    """
    sys.path.insert(0, str(_BASE / "contracts"))
    from insight import Insight, to_claims

    ins = {"kind": "DIVERGENCE",
           "claim": "추세는 섰는데 폭이 안 따라와 저변이 좁다",
           "source_nodes": ["technical", "regime"],
           "supporting_fact_ids": ["f1", "f2"],
           "falsifier": "5거래일 내 상승비율이 55% 를 넘으면 틀렸다",
           "horizon_days": 5, "confidence": "MEDIUM"}
    md = _render_packet_md(
        {"symbol": "005380", "thesis": "t", "facts": [], "_insights": [ins],
         "_insight_rejected": [{"reason": "없는 Fact 참조"}], "_revisions": 1},
        now="2026-08-04T00:00:00Z")
    assert "## 인사이트 (교차 해석)" in md, "인사이트 절이 없다"
    # ▶ 절 이름이 기존 interpretation 절과 겹치면 읽는 사람이 헷갈린다
    assert md.count("## 해석 (교차") == 0, "절 이름이 interpretation 과 겹친다"
    assert "반증 조건" in md, "반증 조건이 안 보인다 - 이게 인사이트의 핵심이다"
    assert "거부된 해석" in md, "거부를 숨기면 왜 적은지 모른다"
    assert "재작성" in md, "재해석 횟수가 안 보인다"

    # 발행 행이 반증 조건을 싣는가 (채점의 기준이다)
    claims = to_claims([Insight(**ins)], symbol="005380")
    assert claims[0]["falsification_note"], "반증 조건이 채점 행에 없다"
    assert claims[0]["kind"] == "INSIGHT_DIVERGENCE"


def _check_gap_loop_terminates_and_is_selective():
    """근거 재수집 순환이 **끝나고, 아무 때나 돌지 않는가.**

    빈 구멍이 있다고 무조건 되돌아가면 안 된다 - 어떤 지표는 그 종목에
    원래 없어서 몇 번을 다시 모아도 안 채워진다. 그러면 순환이 낭비가 되고,
    낭비하는 순환은 곧 꺼진다.
    """
    # 판정을 못 낸 분석가가 있으면 되돌아간다
    st = {"technical": {"verdict": "INSUFFICIENT_DATA"}}
    assert needs_gap_fill(st) == "fill", st

    # ▶ 미확인 항목이 있어도 **판정은 냈으면** 되돌아가지 않는다
    st2 = {"technical": {"verdict": "POSITIVE",
                         "readout": {"unavailable": ["amihud", "vpin"]}}}
    assert needs_gap_fill(st2) == "done", st2

    # 상한에 닿으면 무조건 끝난다 - 못 메우면 부족한 채로 내되 사실을 남긴다
    st3 = dict(st, gap_fills=MAX_GAP_FILLS)
    assert needs_gap_fill(st3) == "done", st3

    # 유한성 - 실제로 돌려본다
    state = dict(st, gap_fills=0)
    steps = 0
    while needs_gap_fill(state) == "fill":
        state.update(bump_gap_fill(state))
        steps += 1
        assert steps <= MAX_GAP_FILLS + 1, "재수집 순환이 안 멈춘다"
    assert steps == MAX_GAP_FILLS, steps

    # 무엇이 비었는지 남는가 - 안 남기면 같은 구멍이 영원히 반복된다
    g = bump_gap_fill({"technical": {"verdict": "INSUFFICIENT_DATA",
                                     "readout": {"unavailable": ["amihud"]}},
                       "regime": {"verdict": "OK",
                                  "readout": {"unavailable": ["vkospi_52w"]}}})
    assert g["evidence_gaps"]["technical"] == ["amihud"], g
    assert g["evidence_gaps"]["regime"] == ["vkospi_52w"], g

    # 그래프에 순환 간선이 있는가
    src = __import__("pathlib").Path(__file__).read_text(encoding="utf-8")
    assert 'g.add_edge("bump_gap_fill", "assemble_evidence")' in src
    assert '"fill": "bump_gap_fill"' in src


def _check_korean_guard():
    """서술이 한국어인가 - **지시만 하고 검사하지 않으면 지켜지지 않는다.**"""
    # 실제 사고 문장(005380, 2026-08-03)
    eng = {"thesis": "Hyundai's global sales performance and strategic investment "
                     "in Yeongnam region may signal broader market positioning.",
           "facts": ["sales_data: Global sales decreased 5.1% year-over-year"]}
    assert korean_ratio(eng) < MIN_KOREAN_RATIO, korean_ratio(eng)

    kor = {"thesis": "7월 글로벌 판매가 전년 대비 5.1% 감소했다.",
           "facts": ["현대차 7월 글로벌 판매 31만8천대 [n1]", "부채비율 188.95% [d1]"]}
    assert korean_ratio(kor) >= MIN_KOREAN_RATIO, korean_ratio(kor)

    # 숫자·ref 만 있으면 판정을 보류한다 - 막지 않는다(모르는 것을 단정하지 않는다)
    assert korean_ratio({"thesis": "5.1%", "facts": ["188.95", "[n1]"]}) == 1.0
    assert korean_ratio({}) == 1.0

    # 토큰 필드(evidence_quality)는 언어 판정 대상이 아니다 - 영어여야 맞다
    assert "evidence_quality" not in NARRATIVE_KEYS
    # 영어 문장에 한글 단어 하나 섞은 것을 통과시키지 않는다
    mixed = {"thesis": "Hyundai global sales decreased significantly in July 현대차",
             "facts": ["Global sales down year over year across all regions"]}
    assert korean_ratio(mixed) < MIN_KOREAN_RATIO, korean_ratio(mixed)
    print("  총괄 한국어 가드         OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        sym = sys.argv[sys.argv.index("--run") + 1]
        print(f"{PIPELINE_VERSION} 실행: {sym}")
        packet = run_research_department(sym)
        print(json.dumps(packet, ensure_ascii=False, indent=1))
        # 결정론 MD 리포트 저장 (동규님 리스크본부 패턴 채택, 2026-08-01)
        rep_dir = _BASE / "reports"
        rep_dir.mkdir(exist_ok=True)
        rp = rep_dir / f"research_packet_{sym}_{datetime.now(KST):%Y%m%d_%H%M%S}.md"
        rp.write_text(_render_packet_md(packet), encoding="utf-8")
        print(f"리포트 저장: {rp.relative_to(_BASE)}")

        # Notion 은 **구속력 없는 Projection** 이다 - 실패해도 Packet 은 그대로다
        # (동규님 리스크 Reporter 와 같은 규약). 미설정이면 조용히 생략한다.
        try:
            from notion_reporter import upload_packet
            nr = upload_packet(packet, symbol=sym,
                               report_md=rp.read_text(encoding="utf-8"))
            print("Notion:", nr.get("url") or nr.get("reason") or nr)
        except Exception as e:  # noqa: BLE001
            print(f"Notion 업로드 예외(무시): {type(e).__name__}: {e}")
        raise SystemExit(0)

    print(f"{PIPELINE_VERSION} 자체 점검 (LLM·API 없음)")
    _check_graph_shape()
    _check_korean_guard()
    _check_occurrence_collection()
    _check_method_performance_readback()
    _check_data_quality_gate()
    _check_revision_cycle_terminates()
    _check_interpret_refuses_thin_input()
    _check_insight_render_and_publish()
    _check_gap_loop_terminates_and_is_selective()
    _check_occurrence_log_failure_is_not_fatal()
    _check_halt_short_circuit()
    _check_packet_guard()
    _check_price_context_injection()
    _check_evidence_module()
    _check_analyst_conflict_and_numeric_guard()
    _check_schema_coercion()
    _check_numeric_failure_downgrades_quality()
    _check_claims_use_bundle_key()
    _check_packet_report_renderer()
    _check_run_recorder()
    print("본부 파이프라인 12개 영역 통과. 실행은 --run <종목>")
