#!/usr/bin/env python3
"""microstructure-analyst (RES-03) - 리서치본부 미시구조 분석가 실행 실체.

담당: 재일 (리서치/퀀트)
근거: departments/01-research/hermes/config.yaml 의 microstructure-analyst 페르소나
      ("Numeric features come from the streaming service" - LLM 은 해석만)
      docs/04-organization/AGENT_EMPLOYEE_PROFILES.md RES-03 직무기술서
      agents/technical_analyst.py 의 compute→narrate→verify 단계 분리·자체점검 패턴
      api/market_api.py GET /microstructure/{symbol} (RES-03 의 결정론 재료)
      contracts/market_events.py depth_imbalance=(bid-ask)/(bid+ask) 부호 규약

설계 결정 (판정은 코드, 서술만 LLM):
  1. 지표 계산은 전부 결정론 Python(compute_micro_readout, 순수 함수).
     체결강도 proxy(매수/매도 체결량 비), 매수 체결 비중 %, VWAP 대비 종가
     위치 %, 스프레드 bp p50/p90, 심도 불균형 평균까지 코드가 책임진다.
  2. 특이 플래그도 결정론: SPREAD_WIDE(p50>=20bp),
     ONE_SIDED_FLOW(매수 비중>=65% 또는 <=35%), DEPTH_SKEW(|불균형|>=0.3).
     assessment 는 플래그 조합으로 코드가 선판정한다 -
     STRESSED(SPREAD_WIDE 또는 DEPTH_SKEW) > ONE_SIDED(ONE_SIDED_FLOW) >
     ORDERLY, ticks·quotes 둘 다 없으면 INSUFFICIENT_DATA. 기준·부호 규약은
     readout dict 에 그대로 기록해 서술과 검증이 같은 근거를 본다.
  3. LLM 은 narrate 에서 받아 적기만 한다. verify 가 (a) readout 에 없는
     인용 키 제거, (b) summary 의 %·bp 수치를 readout 수치와 ±0.1 대조,
     (c) LLM assessment 가 코드 판정과 다르면 코드 판정으로 복원한다.
     LLM 이 죽어도 판정은 코드 것이 그대로 남는다(LLM_UNAVAILABLE 명시).
  4. 데이터 접근은 market-api 뿐이다(GET /microstructure/{symbol} +
     선택적 GET /bars 최신 1봉의 종가). DB Credential 을 받지 않는다
     (통합 계획 6.2). /bars 실패는 비치명 - VWAP 대비 위치만 미확인이 된다.
  5. 판단 불가는 판단 불가로: null 인 쪽 지표는 None(=미확인) + 부분 판정
     (data_coverage 로 명시), 둘 다 null 이면 INSUFFICIENT_DATA. 수치·판정을
     지어내지 않는다.

흐름 (technical_analyst 와 같은 단계 분리):
  fetch(market-api) -> compute(결정론) -> narrate(LLM) -> verify(결정론)

이 출력은 **Agent Decision 이 아니다.** Research Packet 의 입력 재료
(Microstructure Note)다 (CLAUDE.md: Agent Decision != Signal != Order).

실행:
  python agents/microstructure_analyst.py              # 자체 점검 (네트워크 없음)
  python agents/microstructure_analyst.py --run 000660 # 실측 (market-api + Ollama)
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# LLM 호출·서술 재시도의 단일 출처 - agents/ 를 스크립트로 실행하면 본부 루트가
# sys.path 에 없어 evidence/ 를 직접 넣는다(다른 분석가와 같은 관례)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evidence"))
from liquidity import compute_liquidity
from llm_client import chat as llm_chat
from llm_client import narrate as llm_narrate
from narrative_guard import audit_narrative, label_caution_lines
from number_guard import caution_lines, flag_unmatched

PERSONA = "microstructure-analyst"   # 부서 허용목록 키
AGENT_VERSION = "research-microstructure-analyst-v1"

MARKET_API = os.environ.get("MARKET_API_URL", "http://127.0.0.1:8036")
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("MICRO_ANALYST_MODEL", "agent-research")
LLM_TIMEOUT = float(os.environ.get("MICRO_LLM_TIMEOUT", "120"))  # 로컬 14b 지연 감안
# 유동성 지표(Amihud·Roll)의 관측 창 20일 + 차분·결측 여유
LIQUIDITY_BARS = 30

# 특이 플래그 기준 (결정론 - readout 에도 문자열로 기록한다)
SPREAD_WIDE_BP = 20.0     # 스프레드 p50 이 이 이상이면 SPREAD_WIDE
ONE_SIDED_HI_PCT = 65.0   # 매수 체결 비중이 이 이상이면 ONE_SIDED_FLOW
ONE_SIDED_LO_PCT = 35.0   # 매수 체결 비중이 이 이하이면 ONE_SIDED_FLOW
DEPTH_SKEW_ABS = 0.3      # |심도 불균형 평균| 이 이상이면 DEPTH_SKEW

# 서술이 인용할 수 없는 메타 키 (판정 규칙·부호 규약·날짜 문자열)
META_KEYS = frozenset({
    "assessment", "assessment_rule", "flag_criteria",
    "depth_imbalance_convention", "ticks_date", "quotes_date",
    "last_close_date", "first_trade", "last_trade",
})

# verify 의 %·bp 수치 대조 풀 - 비율·bp 지표만 (체결량 같은 절대 수량 제외)
NUMERIC_POOL_KEYS = (
    "buy_over_sell_ratio", "buy_volume_ratio_pct", "close_vs_vwap_pct",
    "spread_bp_p50", "spread_bp_p90", "depth_imbalance_avg",
)


# ---------------------------------------------------------------------------
# LLM 출력 계약 (개발 원칙 2: 항상 Pydantic 으로 검증)
# ---------------------------------------------------------------------------

class MicroNote(BaseModel):
    assessment: Literal["ORDERLY", "STRESSED", "ONE_SIDED", "INSUFFICIENT_DATA"]
    summary: str = Field(max_length=600, description="한국어 2~3문장 미시구조 서술")
    used_metrics: list[str] = Field(description="서술이 실제 인용한 readout 키만")
    cautions: list[str] = Field(default_factory=list)

    @field_validator("assessment", mode="before")
    @classmethod
    def _norm_assessment(cls, v):
        return str(v).strip().upper()

    @field_validator("used_metrics", "cautions", mode="before")
    @classmethod
    def _coerce_str_list(cls, v):
        return [str(x).strip() for x in v] if isinstance(v, list) else v


# ---------------------------------------------------------------------------
# 1. compute - 결정론 지표 계산 (순수 함수, LLM·네트워크·시계 무관)
# ---------------------------------------------------------------------------

def _r4(v: float | None) -> float | None:
    return None if v is None else round(float(v), 4)


def _f(v) -> float | None:
    """market-api 는 Decimal 을 문자열로 줄 수 있다 - None 안전 float 코어션."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    f = _f(v)
    return None if f is None else int(f)


def compute_micro_readout(payload: dict, last_close: float | None = None) -> dict:
    """market-api /microstructure 응답(payload) -> 미시구조 readout.

    순수 함수 - 입력만으로 계산한다(자체 점검 대상).
    ticks/quotes 어느 쪽이 null 이면 그쪽 지표는 전부 None(=미확인)이고
    data_coverage 로 부분 판정임을 드러낸다. 둘 다 null 이면 assessment 는
    INSUFFICIENT_DATA 다. last_close 가 없으면 VWAP 대비 위치는 None.

    매수 체결 비중 % 의 분모는 buy+sell (side 분류된 체결량)이다 - side=0
    미분류 체결은 total volume 에는 있어도 비중 계산에 넣지 않는다.
    """
    ticks = payload.get("ticks")
    quotes = payload.get("quotes")

    # --- 체결(ticks) 지표 -------------------------------------------------
    trades = volume = vwap = buy = sell = None
    ticks_date = first_trade = last_trade = None
    if ticks:
        ticks_date = str(ticks.get("trade_date"))[:10] if ticks.get("trade_date") else None
        first_trade = str(ticks["first_trade"]) if ticks.get("first_trade") else None
        last_trade = str(ticks["last_trade"]) if ticks.get("last_trade") else None
        trades = _i(ticks.get("trades"))
        volume = _f(ticks.get("volume"))
        vwap = _f(ticks.get("vwap"))
        buy = _f(ticks.get("buy_volume"))
        sell = _f(ticks.get("sell_volume"))

    # 체결강도 proxy: 매수/매도 체결량 비. 매도 0(또는 결측)이면 None - 무한대를
    # 수치로 위장하지 않는다. 매수 체결 비중 % 는 buy/(buy+sell).
    ratio = None
    if buy is not None and sell is not None and sell > 0:
        ratio = _r4(buy / sell)
    buy_pct = None
    if buy is not None and sell is not None and (buy + sell) > 0:
        buy_pct = _r4(buy / (buy + sell) * 100.0)

    # VWAP 대비 종가 위치 %: last_close 가 주어질 때만 (없으면 미확인)
    lc = _f(last_close)
    close_vs_vwap = None
    if lc is not None and vwap is not None and vwap > 0:
        close_vs_vwap = _r4((lc - vwap) / vwap * 100.0)

    # --- 호가(quotes) 지표 ------------------------------------------------
    n_quotes = p50 = p90 = imb = None
    quotes_date = None
    if quotes:
        quotes_date = str(quotes.get("trade_date"))[:10] if quotes.get("trade_date") else None
        n_quotes = _i(quotes.get("quotes"))
        p50 = _r4(_f(quotes.get("spread_bp_p50")))
        p90 = _r4(_f(quotes.get("spread_bp_p90")))
        imb = _r4(_f(quotes.get("depth_imbalance_avg")))

    # --- 특이 플래그 (결정론 - 기준을 readout 에 같이 기록) ---------------
    flags: list[str] = []
    if p50 is not None and p50 >= SPREAD_WIDE_BP:
        flags.append("SPREAD_WIDE")
    if buy_pct is not None and (buy_pct >= ONE_SIDED_HI_PCT or buy_pct <= ONE_SIDED_LO_PCT):
        flags.append("ONE_SIDED_FLOW")
    if imb is not None and abs(imb) >= DEPTH_SKEW_ABS:
        flags.append("DEPTH_SKEW")

    coverage = ("FULL" if ticks and quotes else
                "TICKS_ONLY" if ticks else
                "QUOTES_ONLY" if quotes else "NONE")

    # --- 판정 (코드가 내린다 - LLM 은 받아 적는다) ------------------------
    if coverage == "NONE":
        assessment = "INSUFFICIENT_DATA"
    elif "SPREAD_WIDE" in flags or "DEPTH_SKEW" in flags:
        assessment = "STRESSED"
    elif "ONE_SIDED_FLOW" in flags:
        assessment = "ONE_SIDED"
    else:
        assessment = "ORDERLY"

    return {
        # 체결
        "ticks_date": ticks_date,
        "trades": trades,
        "volume": _r4(volume),
        "vwap": _r4(vwap),
        "buy_volume": _r4(buy),
        "sell_volume": _r4(sell),
        "buy_over_sell_ratio": ratio,
        "buy_volume_ratio_pct": buy_pct,
        "first_trade": first_trade,
        "last_trade": last_trade,
        # 종가 vs VWAP (last_close 없으면 미확인)
        "last_close": _r4(lc),
        "close_vs_vwap_pct": close_vs_vwap,
        # 호가
        "quotes_date": quotes_date,
        "quotes": n_quotes,
        "spread_bp_p50": p50,
        "spread_bp_p90": p90,
        "depth_imbalance_avg": imb,
        "depth_imbalance_convention":
            "(bid-ask)/(bid+ask): 음수=매도(ask) 심도 우위(매도벽), 양수=매수벽",
        # 플래그·판정 (기준 문자열까지 기록 - 서술·검증이 같은 근거를 본다)
        "flags": flags,
        "flag_criteria": {
            "SPREAD_WIDE": f"spread_bp_p50 >= {SPREAD_WIDE_BP}bp",
            "ONE_SIDED_FLOW": (f"buy_volume_ratio_pct >= {ONE_SIDED_HI_PCT}% "
                               f"또는 <= {ONE_SIDED_LO_PCT}%"),
            "DEPTH_SKEW": f"|depth_imbalance_avg| >= {DEPTH_SKEW_ABS}",
        },
        "data_coverage": coverage,
        "assessment": assessment,
        "assessment_rule": ("STRESSED=SPREAD_WIDE|DEPTH_SKEW > "
                            "ONE_SIDED=ONE_SIDED_FLOW > ORDERLY; "
                            "ticks·quotes 둘 다 없으면 INSUFFICIENT_DATA"),
    }


# ---------------------------------------------------------------------------
# 2. narrate - LLM 은 여기서만 쓴다 (서술만, 판정·계산 없음)
# ---------------------------------------------------------------------------

_SYSTEM = """You are the Microstructure Analyst (RES-03) of a research department.
You are given a code-computed microstructure readout (JSON) for one trading
day of one Korean stock (tick/quote aggregates). Your ONLY job is to narrate
it. Rules:
- NEVER recompute, invent or extrapolate numbers. Quote values EXACTLY as
  given in the readout. Cite ONLY the metric keys that exist in it.
- A null metric means that data was unavailable ("미확인") - do not guess its
  value, and mention the limitation in cautions instead.
- assessment: copy readout["assessment"] EXACTLY. It is code-determined from
  deterministic flags (rule given in readout["assessment_rule"]). You never
  re-judge it.
- summary: 2~3 Korean sentences on order flow (buy/sell intensity), price vs
  VWAP, and liquidity (spread bp p50/p90, depth imbalance), quoting the given
  values. depth_imbalance_avg sign: negative = ask-side (sell wall) depth
  dominance, per readout["depth_imbalance_convention"].
- used_metrics: ONLY the readout keys your summary actually relied on.
- cautions: short Korean phrases for risks/limits (null metrics, partial
  data_coverage, wide spread, one-sided flow).
Output JSON only, exactly this shape, no other text:
{"assessment":"ORDERLY","summary":"...","used_metrics":["spread_bp_p50"],
 "cautions":["..."]}"""


def narrate(readout: dict, symbol: str, llm: Callable | None = None) -> MicroNote:
    """readout 을 LLM 에 주고 MicroNote 를 받는다. Schema 불합격이면 한 번
    고쳐 부르고, 또 실패하면 예외다 - 서술을 지어내지 않는다(호출부가
    LLM_UNAVAILABLE 처리하되 판정은 코드 것을 유지). llm 주입은 자체 점검용."""
    allowed = [k for k in readout if k not in META_KEYS]
    prompt = (f"Stock: {symbol}\n"
              f"Allowed metric keys: {json.dumps(allowed)}\n"
              f"Code-determined assessment (copy exactly): {readout['assessment']}\n"
              f"Microstructure readout (code-computed, do not alter):\n"
              + json.dumps(readout, ensure_ascii=False, indent=1))

    return llm_narrate(_SYSTEM, prompt, MicroNote, llm or _ollama_call)


def _ollama_call(system: str, user: str) -> str:
    """로컬/팀 Ollama. 호출 모양은 evidence/llm_client 가 단일 출처다.

    여기 남는 것은 **이 분석가의 설정**뿐이다(모델·타임아웃). 예전에는 요청
    조립까지 7곳에 복붙돼 timeout 이 20/30/LLM_TIMEOUT 으로 갈라져 있었다.
    """
    return llm_chat(system, user, base=OLLAMA_BASE, model=MODEL,
                    timeout=LLM_TIMEOUT)


# ---------------------------------------------------------------------------
# 3. verify - 결정론적 검증. LLM 출력이 코드 계산을 벗어나면 여기서 걸린다
# ---------------------------------------------------------------------------

# summary 의 %·bp 수치만 대조한다 (체결량 같은 절대 수량은 단위가 없다)
_NUM_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*(%|bp)", re.IGNORECASE)


def verify(note: MicroNote, readout: dict) -> tuple[MicroNote, dict]:
    """(1) used_metrics 중 readout 에 없는(또는 메타) 키 제거(환각 인용 - 카운트),
    (2) summary 의 %·bp 수치가 readout 비율·bp 수치(±0.1, 부호 반전 서술 허용)
        밖이면 플래그해 cautions 에 남긴다,
    (3) LLM assessment 가 코드 판정과 다르면 코드 판정으로 복원하고 사유 기록.
    복원이지 조작이 아니다 - summary 원문은 고치지 않는다."""
    dropped: dict = {"hallucinated_metrics": [], "flagged_numbers": [],
                     "assessment_restored": None}

    kept: list[str] = []
    for k in note.used_metrics:
        if k in readout and k not in META_KEYS:
            if k not in kept:
                kept.append(k)
        else:
            dropped["hallucinated_metrics"].append(k)

    # 숫자 대조 규칙은 evidence/number_guard 가 단일 출처다. 풀은 여기서
    # 고른다 - 이 분석가는 비율·bp 지표만 인용 대상이고 체결량 같은 절대
    # 수량은 뺀다(NUMERIC_POOL_KEYS).
    pool = [float(readout[k]) for k in NUMERIC_POOL_KEYS
            if isinstance(readout.get(k), (int, float))
            and not isinstance(readout.get(k), bool)]
    dropped["flagged_numbers"] = flag_unmatched(note.summary, pool)
    cautions = list(note.cautions) + caution_lines(dropped["flagged_numbers"])

    assessment = note.assessment
    code_assessment = readout.get("assessment")
    if code_assessment and assessment != code_assessment:
        reason = (f"LLM assessment={assessment} 이 코드 판정 {code_assessment} 과 "
                  f"불일치 - 코드 판정으로 복원 (판정은 코드가 내린다)")
        dropped["assessment_restored"] = reason
        cautions.append("[복원] " + reason)
        assessment = code_assessment

    # 의미 오서술 가드 - **이 분석가에는 없었다**(2026-08-02 배선). 사전의
    # spread_bp_p50/p90 백분위 리터럴 규칙은 애초에 이 분석가의 실측 사고
    # (사례 3: 키 이름 속 50/90 을 %로 서술)에서 나왔는데, 정작 여기에는
    # 안 붙어 있었다. 숫자 대조는 수치가 readout 에 있으면 통과시키므로
    # 라벨을 바꿔 부르는 오서술은 이 가드만 잡는다. v1 은 표시만 한다.
    dropped["label_flags"] = audit_narrative(note.summary, readout)
    cautions += label_caution_lines(dropped["label_flags"])

    return (note.model_copy(update={"assessment": assessment, "used_metrics": kept,
                                    "cautions": cautions}),
            dropped)


# ---------------------------------------------------------------------------
# 4. analyze - fetch -> compute -> narrate -> verify 전 과정
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 20):
    """조회면 호출 - **페르소나를 밝힌다**(Tool Gateway 이행, 2026-08-02).
    헤더가 없으면 게이트웨이가 익명으로 보고 강제 모드에서 403 이다."""
    from api_client import get_json

    return get_json(url, persona=PERSONA, timeout=timeout)


def analyze(symbol: str, *, market_api: str | None = None,
            llm: Callable | None = None, get: Callable = _http_get) -> dict:
    """market-api 호출 -> 결정론 계산 -> LLM 서술 -> 결정론 검증.

    /microstructure 불가·둘 다 null -> verdict INSUFFICIENT_DATA (사유 명시).
    /bars(최신 종가) 실패는 비치명 - VWAP 대비 위치만 미확인.
    LLM 불가 -> 판정은 코드 것 유지, llm_status=LLM_UNAVAILABLE 명시.
    """
    base = (market_api or MARKET_API).rstrip("/")
    ts = datetime.now(timezone.utc)
    out = {"agent": AGENT_VERSION, "symbol": symbol, "as_of": ts.isoformat(),
           "model": MODEL}
    try:
        payload = get(f"{base}/microstructure/{symbol}")
    except Exception as e:  # noqa: BLE001 - 미가동을 통과로 위장하지 않는다
        return {**out, "verdict": "INSUFFICIENT_DATA", "readout": None,
                "reason": f"market-api /microstructure 호출 실패: "
                          f"{type(e).__name__}: {e}"}

    # 일봉 - 최신 종가(VWAP 대비 위치)와 유동성 지표(Amihud·Roll)에 함께 쓴다.
    # 예전에는 1봉만 받았는데, 유동성은 창이 필요해 LIQUIDITY_BARS 로 넓혔다.
    # 실패해도 분석은 계속한다 - 둘 다 비치명 보조다.
    last_close = None
    last_close_date = None
    liquidity = None
    try:
        bars = get(f"{base}/bars/{symbol}?interval=1D"
                   f"&limit={LIQUIDITY_BARS}&source=ls_chart")
        if bars:
            last_close = _f(bars[0].get("close"))     # API 는 최신순
            last_close_date = str(bars[0].get("bucket_time"))[:10]
            liquidity = compute_liquidity(bars)
    except Exception:  # noqa: BLE001, S110 - 비치명: 종가 위치·유동성만 미확인이 된다
        pass

    readout = compute_micro_readout(payload, last_close=last_close)
    readout["last_close_date"] = last_close_date
    # 체결·호가 단면이 '오늘 하루'라면 이 둘은 '최근 20일의 구조적 비용'이다.
    # 못 받았으면 None(미확인) - 빈 dict 로 위장하지 않는다.
    readout["liquidity"] = liquidity

    if readout["assessment"] == "INSUFFICIENT_DATA":
        return {**out, "verdict": "INSUFFICIENT_DATA", "readout": readout,
                "reason": payload.get("note") or "ticks·quotes 둘 다 없음"}

    try:
        note = narrate(readout, symbol, llm=llm)
    except Exception as e:  # noqa: BLE001 - Ollama 다운/Schema 반복 위반 등
        # 판정은 코드가 이미 내렸다 - LLM 불가는 서술 부재일 뿐이다
        return {**out, "verdict": readout["assessment"],
                "llm_status": "LLM_UNAVAILABLE", "readout": readout,
                "reason": f"{type(e).__name__}: {str(e)[:300]}"}

    note, dropped = verify(note, readout)
    return {**out, "verdict": note.assessment, "llm_status": "OK",
            "readout": readout, "summary": note.summary,
            "used_metrics": note.used_metrics, "cautions": note.cautions,
            "dropped": dropped}


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음 (합성 payload + 가짜 LLM 주입)
# ---------------------------------------------------------------------------

def _ticks_row(buy, sell, vwap=100.0, trades=1000, volume=None,
               date="2026-07-31") -> dict:
    b = _f(buy) or 0.0
    s = _f(sell) or 0.0
    return {"trade_date": date, "trades": trades,
            "volume": volume if volume is not None else b + s,
            "vwap": vwap, "buy_volume": buy, "sell_volume": sell,
            "first_trade": f"{date}T09:00:01+09:00",
            "last_trade": f"{date}T15:30:00+09:00"}


def _quotes_row(p50, p90=None, imb=0.0, n=5000, date="2026-07-31") -> dict:
    return {"trade_date": date, "quotes": n, "spread_bp_p50": p50,
            "spread_bp_p90": p90 if p90 is not None else p50 * 2,
            "depth_imbalance_avg": imb}


def _payload(ticks=None, quotes=None) -> dict:
    return {"symbol": "TEST", "ticks": ticks, "quotes": quotes,
            "note": None if (ticks or quotes) else "해당 일자 데이터 없음"}


def _check_intensity_math():
    """강도·비중·VWAP 수작업 대조. buy=650, sell=350 (문자열 - Decimal 직렬화
    모사): ratio=650/350=1.8571, 비중=650/1000=65.0%, vwap=100 종가=103
    -> +3.0%. 매도 0 이면 ratio None(무한대 위장 금지), 비중 100%."""
    r = compute_micro_readout(
        _payload(_ticks_row("650", "350", vwap="100.0", volume="1000"),
                 _quotes_row(8.5, 15.2, 0.12)),
        last_close=103.0)
    assert r["buy_over_sell_ratio"] == 1.8571, r["buy_over_sell_ratio"]
    assert r["buy_volume_ratio_pct"] == 65.0, r["buy_volume_ratio_pct"]
    assert r["close_vs_vwap_pct"] == 3.0, r["close_vs_vwap_pct"]
    assert r["vwap"] == 100.0 and r["volume"] == 1000.0 and r["trades"] == 1000
    assert r["spread_bp_p50"] == 8.5 and r["spread_bp_p90"] == 15.2
    assert r["depth_imbalance_avg"] == 0.12
    assert r["data_coverage"] == "FULL"

    # 매도 0 - ratio 는 None, 비중은 100%
    r0 = compute_micro_readout(_payload(_ticks_row(100.0, 0.0)))
    assert r0["buy_over_sell_ratio"] is None and r0["buy_volume_ratio_pct"] == 100.0

    # side 집계 자체가 결측 - 둘 다 None, ONE_SIDED_FLOW 미발화
    rn = compute_micro_readout(_payload(_ticks_row(None, None)))
    assert rn["buy_over_sell_ratio"] is None and rn["buy_volume_ratio_pct"] is None
    assert "ONE_SIDED_FLOW" not in rn["flags"]

    # last_close 없으면 VWAP 대비 위치 미확인, vwap 결측이어도 미확인
    assert compute_micro_readout(
        _payload(_ticks_row(1.0, 1.0)))["close_vs_vwap_pct"] is None
    tk = _ticks_row(1.0, 1.0)
    tk["vwap"] = None
    assert compute_micro_readout(
        _payload(tk), last_close=100.0)["close_vs_vwap_pct"] is None
    print("  강도·비중·VWAP 수작업 대조  OK")


def _check_flag_spread():
    # p50=20.0 은 발화(>=), 19.99 는 비발화 - 경계 확인
    r = compute_micro_readout(_payload(quotes=_quotes_row(20.0)))
    assert "SPREAD_WIDE" in r["flags"] and r["assessment"] == "STRESSED"
    r = compute_micro_readout(_payload(quotes=_quotes_row(19.99)))
    assert "SPREAD_WIDE" not in r["flags"] and r["assessment"] == "ORDERLY"
    print("  SPREAD_WIDE 경계(20bp)      OK")


def _check_flag_one_sided():
    # 비중 65.0% 발화 / 64.99% 비발화, 35.0% 발화 / 35.01% 비발화
    r = compute_micro_readout(_payload(_ticks_row(6500.0, 3500.0)))
    assert "ONE_SIDED_FLOW" in r["flags"] and r["assessment"] == "ONE_SIDED"
    r = compute_micro_readout(_payload(_ticks_row(6499.0, 3501.0)))
    assert "ONE_SIDED_FLOW" not in r["flags"] and r["assessment"] == "ORDERLY"
    r = compute_micro_readout(_payload(_ticks_row(3500.0, 6500.0)))
    assert "ONE_SIDED_FLOW" in r["flags"], r["flags"]
    r = compute_micro_readout(_payload(_ticks_row(3501.0, 6499.0)))
    assert "ONE_SIDED_FLOW" not in r["flags"], r["flags"]
    print("  ONE_SIDED_FLOW 경계(65/35)  OK")


def _check_flag_depth():
    # |불균형|>=0.3: -0.3 발화(매도벽 우위), +0.3 발화, -0.2999 비발화
    r = compute_micro_readout(_payload(quotes=_quotes_row(5.0, imb=-0.3)))
    assert "DEPTH_SKEW" in r["flags"] and r["assessment"] == "STRESSED"
    r = compute_micro_readout(_payload(quotes=_quotes_row(5.0, imb=0.3)))
    assert "DEPTH_SKEW" in r["flags"]
    r = compute_micro_readout(_payload(quotes=_quotes_row(5.0, imb=-0.2999)))
    assert "DEPTH_SKEW" not in r["flags"] and r["assessment"] == "ORDERLY"
    # 부호 규약이 dict 에 기록돼 있다 (음수=매도벽 우위)
    assert "음수=매도(ask) 심도 우위" in r["depth_imbalance_convention"]
    print("  DEPTH_SKEW 경계(|0.3|)      OK")


def _check_assessment_precedence():
    # 세 플래그 동시 발화 - STRESSED 가 ONE_SIDED 보다 우선한다 (규칙 기록 확인)
    r = compute_micro_readout(
        _payload(_ticks_row(9000.0, 1000.0), _quotes_row(25.0, imb=0.5)))
    assert set(r["flags"]) == {"SPREAD_WIDE", "ONE_SIDED_FLOW", "DEPTH_SKEW"}
    assert r["assessment"] == "STRESSED"
    assert "STRESSED=SPREAD_WIDE|DEPTH_SKEW" in r["assessment_rule"]
    assert "SPREAD_WIDE" in r["flag_criteria"]
    # 실측 모사 (000660 07-31 기대치): 매수 1.97M/매도 2.28M, p50 6.03,
    # 불균형 -0.336 - ratio 0.864, 비중 46.3529%, DEPTH_SKEW 만 발화 - STRESSED
    r = compute_micro_readout(
        _payload(_ticks_row(1970000.0, 2280000.0, trades=77351),
                 _quotes_row(6.03, 10.5, -0.336)))
    assert r["buy_over_sell_ratio"] == 0.864, r["buy_over_sell_ratio"]
    assert r["buy_volume_ratio_pct"] == 46.3529, r["buy_volume_ratio_pct"]
    assert r["flags"] == ["DEPTH_SKEW"] and r["assessment"] == "STRESSED"
    print("  판정 우선순위·실측 모사     OK")


def _check_partial_null():
    # ticks null - 체결 지표 전부 None, 호가 플래그만으로 부분 판정
    r = compute_micro_readout(_payload(quotes=_quotes_row(25.0)))
    for k in ("trades", "volume", "vwap", "buy_volume", "sell_volume",
              "buy_over_sell_ratio", "buy_volume_ratio_pct", "close_vs_vwap_pct"):
        assert r[k] is None, f"ticks null 인데 {k} 가 계산됐다"
    assert r["data_coverage"] == "QUOTES_ONLY" and r["assessment"] == "STRESSED"
    # quotes null - 호가 지표 전부 None, 체결 플래그만으로 부분 판정
    r = compute_micro_readout(_payload(_ticks_row(8000.0, 2000.0)))
    for k in ("quotes", "spread_bp_p50", "spread_bp_p90", "depth_imbalance_avg"):
        assert r[k] is None, f"quotes null 인데 {k} 가 계산됐다"
    assert r["data_coverage"] == "TICKS_ONLY" and r["assessment"] == "ONE_SIDED"
    # 둘 다 null - INSUFFICIENT_DATA, 플래그 없음
    r = compute_micro_readout(_payload())
    assert r["data_coverage"] == "NONE" and r["assessment"] == "INSUFFICIENT_DATA"
    assert r["flags"] == []
    print("  ticks/quotes null 부분판정  OK")


def _check_verify_hallucination():
    r = compute_micro_readout(
        _payload(_ticks_row(650.0, 350.0), _quotes_row(8.5, 15.2, 0.12)))
    note = MicroNote(assessment="ONE_SIDED", summary="매수 비중 65.0% 로 쏠림.",
                     used_metrics=["buy_volume_ratio_pct", "ofi_1min",
                                   "assessment_rule", "buy_volume_ratio_pct"],
                     cautions=[])
    v, dropped = verify(note, r)
    # 없는 키(ofi_1min)·메타 키(assessment_rule)는 버리고, 중복은 1개로
    assert v.used_metrics == ["buy_volume_ratio_pct"], v.used_metrics
    assert dropped["hallucinated_metrics"] == ["ofi_1min", "assessment_rule"]
    assert dropped["assessment_restored"] is None and v.assessment == "ONE_SIDED"
    print("  환각 키 제거·카운트         OK")


def _check_verify_number_flag():
    r = compute_micro_readout(
        _payload(_ticks_row(650.0, 350.0, vwap=100.0), _quotes_row(8.5, 15.2, 0.12)),
        last_close=103.0)
    note = MicroNote(
        assessment="ONE_SIDED",
        # 65.0%·8.5bp 는 readout 그대로(통과), -3.0% 는 부호 반전 서술(허용),
        # 42.0bp 와 99.9% 는 어느 수치에도 없다(플래그)
        summary="매수 비중 65.0%, 스프레드 8.5bp, VWAP 대비 -3.0%. "
                "체감 스프레드는 42.0bp 이고 승률은 99.9% 다.",
        used_metrics=["buy_volume_ratio_pct"], cautions=[])
    v, dropped = verify(note, r)
    assert dropped["flagged_numbers"] == ["42.0bp", "99.9%"], dropped
    assert sum("신뢰 금지" in c for c in v.cautions) == 2, v.cautions
    print("  summary %·bp 수치 대조      OK")


def _check_verify_assessment_restore():
    # 코드 판정 STRESSED (DEPTH_SKEW) 인데 LLM 이 ORDERLY 라 우기면 복원한다
    r = compute_micro_readout(_payload(quotes=_quotes_row(6.03, imb=-0.336)))
    assert r["assessment"] == "STRESSED"
    note = MicroNote(assessment="ORDERLY", summary="질서정연한 흐름이다.",
                     used_metrics=["spread_bp_p50"], cautions=[])
    v, dropped = verify(note, r)
    assert v.assessment == "STRESSED", "코드 판정이 복원되지 않았다"
    assert dropped["assessment_restored"] and "복원" in dropped["assessment_restored"]
    assert any("[복원]" in c for c in v.cautions)
    # 일치하면 건드리지 않는다
    note2 = MicroNote(assessment="STRESSED", summary="심도 불균형 -0.336.",
                      used_metrics=["depth_imbalance_avg"], cautions=[])
    v2, d2 = verify(note2, r)
    assert v2.assessment == "STRESSED" and d2["assessment_restored"] is None
    print("  판정 불일치 코드 복원       OK")


def _check_narrate_roundtrip():
    r = compute_micro_readout(
        _payload(_ticks_row(650.0, 350.0), _quotes_row(8.5, 15.2, 0.12)))

    def fake_llm(system, user):
        assert "NEVER recompute" in system and "copy readout" in system
        assert "spread_bp_p50" in user and "ONE_SIDED" in user
        return json.dumps({"assessment": "one_sided",  # 소문자도 정규화되는지
                           "summary": "매수 비중 65.0% 로 매수 쏠림이다.",
                           "used_metrics": ["buy_volume_ratio_pct"],
                           "cautions": ["쏠림 지속성 미확인"]}, ensure_ascii=False)

    note = narrate(r, "TEST", llm=fake_llm)
    assert note.assessment == "ONE_SIDED"
    assert note.used_metrics == ["buy_volume_ratio_pct"]

    calls = {"n": 0}

    def flaky_llm(_s, _u):
        calls["n"] += 1
        if calls["n"] == 1:
            return "죄송하지만 JSON 이 아닙니다"
        return json.dumps({"assessment": "ORDERLY", "summary": "무난한 흐름.",
                           "used_metrics": [], "cautions": []})

    note = narrate(r, "TEST", llm=flaky_llm)
    assert calls["n"] == 2 and note.assessment == "ORDERLY"

    try:
        narrate(r, "TEST", llm=lambda s, u: "no json ever")
        raise AssertionError("Schema 위반이 두 번인데 통과했다")
    except RuntimeError:
        pass
    print("  가짜 LLM narrate 왕복       OK")


def _check_analyze_pipeline():
    payload = _payload(_ticks_row(1970000.0, 2280000.0, trades=77351,
                                  vwap=100.0),
                       _quotes_row(6.03, 10.5, -0.336))

    def fake_get(url):
        if "/microstructure/000000" in url:
            return payload
        if "/bars/000000" in url:
            assert f"limit={LIQUIDITY_BARS}" in url and "source=ls_chart" in url, url
            # 최신순(desc). 최신 종가 103.0 은 그대로 두고, 유동성 계산용으로
            # 나머지 날짜를 채운다 - 톱니라 Roll 모형이 성립한다.
            rows = [{"bucket_time": "2026-07-31T00:00:00+09:00",
                     "close": "103.0", "notional": 1e9}]
            rows += [{"bucket_time": f"2026-07-{31 - i:02d}T00:00:00+09:00",
                      "close": str(100.0 + (i % 2)), "notional": 1e9}
                     for i in range(1, 25)]
            return rows
        raise AssertionError(f"예상 밖 URL: {url}")

    def fake_llm(_s, _u):  # 환각 키 + 창작 수치 + 판정 불일치 전부 탑재
        return json.dumps({"assessment": "ORDERLY",
                           "summary": "스프레드 42.0bp 로 널널하다.",
                           "used_metrics": ["spread_bp_p50", "ofi_1min"],
                           "cautions": []}, ensure_ascii=False)

    out = analyze("000000", llm=fake_llm, get=fake_get)
    assert out["verdict"] == "STRESSED", out          # 판정 불일치 - 복원됐다
    assert out["dropped"]["hallucinated_metrics"] == ["ofi_1min"]
    assert out["dropped"]["flagged_numbers"] == ["42.0bp"]
    assert out["dropped"]["assessment_restored"] and out["llm_status"] == "OK"
    assert out["readout"]["close_vs_vwap_pct"] == 3.0   # bars 종가가 관통됐다
    assert out["readout"]["last_close_date"] == "2026-07-31"

    # /bars 만 죽음 - 비치명: 분석은 계속, 종가 위치만 미확인
    def bars_down(url):
        if "/bars/" in url:
            raise OSError("bars down")
        return payload

    def ok_llm(_s, _u):
        return json.dumps({"assessment": "STRESSED", "summary": "심도 매도 우위.",
                           "used_metrics": ["depth_imbalance_avg"], "cautions": []})

    out2 = analyze("000000", llm=ok_llm, get=bars_down)
    assert out2["verdict"] == "STRESSED" and out2["llm_status"] == "OK"
    assert out2["readout"]["last_close"] is None
    assert out2["readout"]["close_vs_vwap_pct"] is None

    # 둘 다 null - LLM 을 부르지 않고 INSUFFICIENT_DATA
    out3 = analyze("000000", get=lambda u: _payload() if "/micro" in u else [],
                   llm=lambda s, u: (_ for _ in ()).throw(
                       AssertionError("데이터 없음인데 LLM 이 불렸다")))
    assert out3["verdict"] == "INSUFFICIENT_DATA"
    assert out3["reason"] == "해당 일자 데이터 없음"

    # LLM 다운 - 판정은 코드 것 유지 + LLM_UNAVAILABLE 명시 (서술만 부재)
    def dead_llm(_s, _u):
        raise OSError("connection refused")

    out4 = analyze("000000", get=fake_get, llm=dead_llm)
    assert out4["verdict"] == "STRESSED", "LLM 없다고 코드 판정까지 버렸다"
    assert out4["llm_status"] == "LLM_UNAVAILABLE" and "refused" in out4["reason"]
    print("  analyze 파이프라인          OK")


def _check_api_down_fail_closed():
    def down(_u):
        raise OSError("connection refused")

    out = analyze("000660", get=down)
    assert out["verdict"] == "INSUFFICIENT_DATA" and out["readout"] is None
    assert "호출 실패" in out["reason"] and "connection refused" in out["reason"]
    print("  API 장애 fail-closed        OK")


# ---------------------------------------------------------------------------

_PRINT_KEYS = (
    "ticks_date", "trades", "volume", "vwap", "buy_volume", "sell_volume",
    "buy_over_sell_ratio", "buy_volume_ratio_pct",
    "last_close", "close_vs_vwap_pct",
    "quotes_date", "quotes", "spread_bp_p50", "spread_bp_p90",
    "depth_imbalance_avg",
)


def _print_result(out: dict) -> None:
    print(f"  verdict={out.get('verdict')} "
          f"(llm={out.get('llm_status', '-')}, model={out.get('model')})")
    if out.get("reason"):
        print(f"  reason: {out['reason']}")
    r = out.get("readout")
    if r:
        print(f"  readout (coverage={r['data_coverage']}, "
              f"판정근거 flags={r['flags'] or '없음'}):")
        for k in _PRINT_KEYS:
            v = r.get(k)
            print(f"    {k:26} {'미확인' if v is None else v}")
        print(f"    {'assessment':26} {r['assessment']} <- {r['assessment_rule']}")
    if out.get("summary"):
        print(f"  summary: {out['summary']}")
        print(f"  used_metrics: {out['used_metrics']}")
        for c in out.get("cautions", []):
            print(f"  caution: {c}")
        d = out["dropped"]
        restored = d["assessment_restored"] or "없음"
        print(f"  dropped: 환각 키 {len(d['hallucinated_metrics'])}"
              f"{d['hallucinated_metrics'] or ''} / "
              f"수치 플래그 {len(d['flagged_numbers'])}"
              f"{d['flagged_numbers'] or ''} / 판정 복원 {restored}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        a = sys.argv
        sym = a[a.index("--run") + 1] if a.index("--run") + 1 < len(a) else "000660"
        print(f"{AGENT_VERSION} 실측: {sym} (market-api + Ollama {MODEL})")
        _print_result(analyze(sym))
        raise SystemExit(0)

    print(f"{AGENT_VERSION} 자체 점검 (네트워크 없음, 합성 payload)")
    _check_intensity_math()
    _check_flag_spread()
    _check_flag_one_sided()
    _check_flag_depth()
    _check_assessment_precedence()
    _check_partial_null()
    _check_verify_hallucination()
    _check_verify_number_flag()
    _check_verify_assessment_restore()
    _check_narrate_roundtrip()
    _check_analyze_pipeline()
    _check_api_down_fail_closed()
    print("microstructure-analyst 12개 영역 통과. 실측은 --run <종목코드>")
