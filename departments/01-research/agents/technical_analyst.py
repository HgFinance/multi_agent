#!/usr/bin/env python3
"""technical-analyst (RES-04) - 리서치본부 기술적 분석가 실행 실체.

담당: 재일 (리서치/퀀트)
근거: departments/01-research/hermes/config.yaml 의 technical-analyst 페르소나
      ("you never compute feature values yourself" - LLM 은 해석만)
      docs/04-organization/AGENT_EMPLOYEE_PROFILES.md RES-04 직무기술서
      agents/news_sentiment_analyst.py 의 _ollama_call·Pydantic 검증·verify 패턴
      evidence/bundle.py 의 market-api /bars 호출 규약(결정론 가격 컨텍스트)

설계 결정:
  1. 지표 계산은 전부 결정론 Python(compute_technical_readout, 순수 함수).
     LLM 은 서술(narrate)만 한다 - 수치를 만들거나 방향을 재계산하는 경로가
     없다. 봉 정렬·중복 제거·반올림(소수 4자리)까지 코드가 책임진다.
  2. 인용 규율: LLM 응답 스키마에 used_metrics 를 강제하고, verify 가
     readout 에 없는 키를 버린다(환각 인용 - 조용히 넘기지 않고 카운트).
     summary 의 % 수치가 readout 수치(±0.1 허용)와 불일치하면 플래그한다.
  3. stance 와 20일 모멘텀 부호가 정면 모순이면(예: +15% 인데 BEARISH)
     NEUTRAL 로 강등하고 사유를 남긴다 - 강등이지 조작이 아니다. LLM 이 쓴
     문장은 고치지 않고 cautions 에 검증 결과를 덧붙인다.
  4. 데이터 접근은 market-api(GET /bars/{symbol}?interval=1D&source=ls_chart)
     뿐이다. DB Credential 을 받지 않는다(통합 계획 6.2).
  5. 판단 불가는 판단 불가로: 봉이 부족한 지표는 None(=미확인), 전부 불가면
     verdict INSUFFICIENT_DATA, LLM 불가면 stance 없이 LLM_UNAVAILABLE 명시.
     점수·방향을 지어내지 않는다.

흐름 (news_sentiment_analyst 와 같은 단계 분리):
  fetch(market-api) -> compute(결정론) -> narrate(LLM) -> verify(결정론)

이 출력은 **Agent Decision 이 아니다.** Research Packet 의 입력 재료다
(CLAUDE.md: Agent Decision != Signal != Order).

실행:
  python agents/technical_analyst.py              # 자체 점검 (네트워크 없음)
  python agents/technical_analyst.py --run 000660 # 실측 (market-api + Ollama)
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

AGENT_VERSION = "research-technical-analyst-v1"
KST = timezone(timedelta(hours=9))

MARKET_API = os.environ.get("MARKET_API_URL", "http://127.0.0.1:8036")
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("TECH_ANALYST_MODEL", "agent-research")
LLM_TIMEOUT = float(os.environ.get("TECH_LLM_TIMEOUT", "120"))  # 로컬 14b 지연 감안

BARS_LIMIT = 120          # sma60+cross(10일 lookback)에 70봉 필요 - 여유 포함
ANNUALIZE_DAYS = 252      # 한국 거래일 연율화 관례
CONTRADICTION_PCT = 5.0   # 이 이상 모멘텀과 반대 stance 면 "정면 모순"으로 본다

# 이 8개가 전부 None 이면 판단 자체가 불가하다 (INSUFFICIENT_DATA)
CORE_METRICS = (
    "price_vs_sma20_pct", "price_vs_sma60_pct",
    "momentum_20d_pct", "momentum_60d_pct",
    "volatility_20d_annualized_pct", "volume_ratio_5d_over_20d",
    "range_position_20d_pct", "cross_sma20_sma60_recent_10d",
)


# ---------------------------------------------------------------------------
# LLM 출력 계약 (개발 원칙 2: 항상 Pydantic 으로 검증)
# ---------------------------------------------------------------------------

class TechnicalNote(BaseModel):
    stance: Literal["BULLISH", "BEARISH", "NEUTRAL", "INSUFFICIENT_DATA"]
    summary: str = Field(max_length=600, description="한국어 2~3문장 기술적 상태 서술")
    used_metrics: list[str] = Field(description="서술이 실제 인용한 readout 키만")
    cautions: list[str] = Field(default_factory=list)

    @field_validator("stance", mode="before")
    @classmethod
    def _norm_stance(cls, v):
        return str(v).strip().upper()

    @field_validator("used_metrics", "cautions", mode="before")
    @classmethod
    def _coerce_str_list(cls, v):
        return [str(x).strip() for x in v] if isinstance(v, list) else v


# ---------------------------------------------------------------------------
# 1. compute - 결정론 지표 계산 (순수 함수, LLM 무관)
# ---------------------------------------------------------------------------

def _r4(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 4)


def _pct(cur: Optional[float], base: Optional[float]) -> Optional[float]:
    # 기준가 0/결측은 데이터 오류다 - 수치로 위장하지 않고 None 으로 남긴다
    if cur is None or base is None or base == 0:
        return None
    return _r4((cur - base) / base * 100.0)


def compute_technical_readout(bars: list[dict]) -> dict:
    """일봉 dict 목록(bucket_time·close 필수, 정렬 무관) -> 기술적 readout.

    순수 함수 - 네트워크·시계 없이 입력만으로 계산한다(자체 점검 대상).
    market-api /bars 는 최신순(desc)으로 주므로 여기서 오름차순 정렬하고,
    같은 bucket_time 중복은 마지막 것(재수집 우선)으로 정리한다.
    각 지표는 데이터 충분성을 검사해 부족하면 None(=미확인)이다.
    """
    dedup: dict[str, dict] = {}
    for b in sorted((b for b in bars if b.get("close") is not None),
                    key=lambda b: str(b["bucket_time"])):
        dedup[str(b["bucket_time"])] = b
    rows = [dedup[k] for k in sorted(dedup)]
    closes = [float(b["close"]) for b in rows]
    n = len(closes)
    last = closes[-1] if n else None

    sma20 = sum(closes[-20:]) / 20.0 if n >= 20 else None
    sma60 = sum(closes[-60:]) / 60.0 if n >= 60 else None
    mom20 = _pct(last, closes[-21]) if n >= 21 else None
    mom60 = _pct(last, closes[-61]) if n >= 61 else None

    # 20일 변동성: 최근 20개 일수익률 표본표준편차(ddof=1) x sqrt(252), %
    vol20 = None
    if n >= 21:
        w = closes[-21:]
        if all(c != 0 for c in w[:-1]):
            rets = [b / a - 1.0 for a, b in zip(w, w[1:])]
            m = sum(rets) / len(rets)
            var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
            vol20 = _r4(math.sqrt(var) * math.sqrt(ANNUALIZE_DAYS) * 100.0)

    # 거래량: 최근 5일 평균 / 최근 20일 평균 (배율). 결측 거래량이 끼면 미확인
    volu = None
    if n >= 20:
        v20 = [b.get("volume") for b in rows[-20:]]
        if all(v is not None for v in v20):
            v20 = [float(v) for v in v20]
            avg20 = sum(v20) / 20.0
            if avg20 > 0:
                volu = _r4((sum(v20[-5:]) / 5.0) / avg20)

    # 20일 고저 레인지 내 위치. high/low 결측 봉은 close 로 보수적 대체
    rng = None
    if n >= 20:
        w = rows[-20:]
        hi = max(float(b["high"]) if b.get("high") is not None else float(b["close"])
                 for b in w)
        lo = min(float(b["low"]) if b.get("low") is not None else float(b["close"])
                 for b in w)
        if hi > lo:
            rng = _r4((last - lo) / (hi - lo) * 100.0)

    # 최근 10일 내 골든/데드 크로스 (sma20 vs sma60). sma60 시계열이 2점
    # 이상 있어야 교차를 말할 수 있다 - 아니면 None(미확인)
    cross = None
    if n >= 61:
        diffs = []
        for i in range(59, n):
            s20 = sum(closes[i - 19:i + 1]) / 20.0
            s60 = sum(closes[i - 59:i + 1]) / 60.0
            diffs.append(s20 - s60)
        cross = "NONE"
        # 최근 10개 전이(k=len-10..len-1)만 본다 - 오래된 크로스는 NONE
        for k in range(len(diffs) - 1, max(0, len(diffs) - 11), -1):
            if diffs[k - 1] <= 0.0 < diffs[k]:
                cross = "GOLDEN"
                break
            if diffs[k - 1] >= 0.0 > diffs[k]:
                cross = "DEAD"
                break

    return {
        "last_close": _r4(last),
        "last_close_date": str(rows[-1]["bucket_time"])[:10] if rows else None,
        "bars_used": n,
        "sma20": _r4(sma20),
        "sma60": _r4(sma60),
        "price_vs_sma20_pct": _pct(last, sma20),
        "price_vs_sma60_pct": _pct(last, sma60),
        "momentum_20d_pct": mom20,
        "momentum_60d_pct": mom60,
        "volatility_20d_annualized_pct": vol20,
        "volume_ratio_5d_over_20d": volu,
        "range_position_20d_pct": rng,
        "cross_sma20_sma60_recent_10d": cross,
    }


def _all_core_none(readout: dict) -> bool:
    return all(readout.get(k) is None for k in CORE_METRICS)


# ---------------------------------------------------------------------------
# 2. narrate - LLM 은 여기서만 쓴다 (서술만, 계산 없음)
# ---------------------------------------------------------------------------

_SYSTEM = """You are the Technical Analyst (RES-04) of a research department.
You are given a code-computed technical readout (JSON) for one Korean stock.
Your ONLY job is to narrate it. Rules:
- NEVER recompute, invent or extrapolate numbers. Quote metric values EXACTLY
  as given in the readout. Cite ONLY the metric keys that exist in it.
- A null metric means insufficient bars ("미확인") - do not guess its value,
  and mention the limitation in cautions instead.
- stance: BULLISH | BEARISH | NEUTRAL, or INSUFFICIENT_DATA if the readout is
  mostly null. The stance must follow the given numbers, not your imagination.
- summary: 2~3 Korean sentences describing the technical state, quoting the
  given values (with % where the key name says pct).
- used_metrics: ONLY the readout keys your summary actually relied on.
- cautions: short Korean phrases for risks/limits (null metrics, volatility,
  volume anomalies).
Output JSON only, exactly this shape, no other text:
{"stance":"NEUTRAL","summary":"...","used_metrics":["momentum_20d_pct"],
 "cautions":["..."]}"""


def narrate(readout: dict, symbol: str, llm: Optional[Callable] = None) -> TechnicalNote:
    """readout 을 LLM 에 주고 TechnicalNote 를 받는다. Schema 불합격이면 한 번
    고쳐 부르고, 또 실패하면 예외다 - 서술을 지어내지 않는다(호출부가
    LLM_UNAVAILABLE 처리). llm 주입은 자체 점검용."""
    allowed = [k for k in readout if k != "last_close_date"]
    prompt = (f"Stock: {symbol}\n"
              f"Allowed metric keys: {json.dumps(allowed)}\n"
              f"Technical readout (code-computed, do not alter):\n"
              + json.dumps(readout, ensure_ascii=False, indent=1))

    call = llm or _ollama_call
    last_err = None
    for attempt in range(2):
        text = call(_SYSTEM, prompt if attempt == 0 else
                    prompt + f"\n\nYour previous output failed validation: {last_err}. "
                             f"Return ONLY valid JSON for the schema.")
        try:
            start = text.find("{")
            end = text.rfind("}")
            return TechnicalNote.model_validate_json(text[start:end + 1])
        except (ValidationError, ValueError) as e:
            last_err = str(e)[:200]
    raise RuntimeError(f"LLM 서술이 Schema 를 두 번 어겼다: {last_err}")


def _ollama_call(system: str, user: str) -> str:
    """로컬/팀 Ollama (OpenAI 호환). news_sentiment_analyst._ollama_call 과 동일
    패턴. qwen3 계열의 <think> 프리앰블은 호출부 파서가 첫 '{'~마지막 '}' 만
    취하므로 그대로 견딘다."""
    req = urllib.request.Request(
        OLLAMA_BASE + "/v1/chat/completions", method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
        data=json.dumps({
            "model": MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.1,
            # 소형 모델의 JSON 규율 - 지원 안 하는 서버는 무시한다
            "response_format": {"type": "json_object"},
        }).encode(),
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
        out = json.loads(r.read())
    return out["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# 3. verify - 결정론적 검증. LLM 출력이 코드 계산을 벗어나면 여기서 걸린다
# ---------------------------------------------------------------------------

_PCT_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*%")


def verify(note: TechnicalNote, readout: dict) -> tuple[TechnicalNote, dict]:
    """(1) used_metrics 중 readout 에 없는 키 제거(환각 인용 - 카운트),
    (2) summary 의 % 수치가 readout 수치(±0.1, 부호 반전 서술 허용) 밖이면
        해당 수치를 플래그해 cautions 에 남긴다,
    (3) stance 와 20일 모멘텀이 정면 모순이면 NEUTRAL 로 강등하고 사유 기록.
    강등이지 조작이 아니다 - summary 원문은 고치지 않는다."""
    dropped: dict = {"hallucinated_metrics": [], "flagged_numbers": [],
                     "demoted": None}

    kept: list[str] = []
    for k in note.used_metrics:
        if k in readout and k != "last_close_date":
            if k not in kept:
                kept.append(k)
        else:
            dropped["hallucinated_metrics"].append(k)

    # summary 숫자 대조: readout 의 수치 풀(지표+참조값, bars_used 제외)과 비교
    pool = [float(v) for k, v in readout.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and k != "bars_used"]
    for m in _PCT_RE.finditer(note.summary):
        x = float(m.group(1))
        if not any(abs(x - v) <= 0.1 or abs(x + v) <= 0.1 for v in pool):
            dropped["flagged_numbers"].append(m.group(0))

    cautions = list(note.cautions)
    for f in dropped["flagged_numbers"]:
        cautions.append(f"[숫자검증] summary 의 {f} 는 readout 수치와 불일치"
                        f"(±0.1 밖) - 해당 문장 신뢰 금지")

    stance = note.stance
    mom = readout.get("momentum_20d_pct")
    if isinstance(mom, (int, float)) and stance in ("BULLISH", "BEARISH"):
        reason = None
        if stance == "BEARISH" and mom >= CONTRADICTION_PCT:
            reason = (f"stance=BEARISH 인데 momentum_20d_pct={mom:+.2f}% - "
                      f"정면 모순, NEUTRAL 강등")
        elif stance == "BULLISH" and mom <= -CONTRADICTION_PCT:
            reason = (f"stance=BULLISH 인데 momentum_20d_pct={mom:+.2f}% - "
                      f"정면 모순, NEUTRAL 강등")
        if reason:
            dropped["demoted"] = reason
            cautions.append("[강등] " + reason)
            stance = "NEUTRAL"

    return (note.model_copy(update={"stance": stance, "used_metrics": kept,
                                    "cautions": cautions}),
            dropped)


# ---------------------------------------------------------------------------
# 4. analyze - fetch -> compute -> narrate -> verify 전 과정
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def analyze(symbol: str, *, market_api: Optional[str] = None,
            llm: Optional[Callable] = None, get: Callable = _http_get) -> dict:
    """market-api 호출 -> 결정론 계산 -> LLM 서술 -> 결정론 검증.

    market-api 불가/봉 전무 -> verdict INSUFFICIENT_DATA (사유 명시).
    LLM 불가 -> 결정론 readout 만, stance 없이 llm_status=LLM_UNAVAILABLE.
    """
    base = (market_api or MARKET_API).rstrip("/")
    ts = datetime.now(timezone.utc)
    out = {"agent": AGENT_VERSION, "symbol": symbol, "as_of": ts.isoformat(),
           "model": MODEL}
    try:
        bars = get(f"{base}/bars/{symbol}?interval=1D&limit={BARS_LIMIT}"
                   f"&source=ls_chart")
    except Exception as e:  # noqa: BLE001 - 미가동을 통과로 위장하지 않는다
        return {**out, "verdict": "INSUFFICIENT_DATA", "readout": None,
                "reason": f"market-api /bars 호출 실패: {type(e).__name__}: {e}"}

    readout = compute_technical_readout(bars)
    if _all_core_none(readout):
        return {**out, "verdict": "INSUFFICIENT_DATA", "readout": readout,
                "reason": f"지표 전부 계산 불가 (봉 {readout['bars_used']}개)"}

    try:
        note = narrate(readout, symbol, llm=llm)
    except Exception as e:  # noqa: BLE001 - Ollama 다운/Schema 반복 위반 등
        return {**out, "verdict": None, "llm_status": "LLM_UNAVAILABLE",
                "readout": readout,
                "reason": f"{type(e).__name__}: {str(e)[:300]}"}

    note, dropped = verify(note, readout)
    return {**out, "verdict": note.stance, "llm_status": "OK",
            "readout": readout, "summary": note.summary,
            "used_metrics": note.used_metrics, "cautions": note.cautions,
            "dropped": dropped}


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음 (합성 봉 + 가짜 LLM 주입)
# ---------------------------------------------------------------------------

def _bars(closes: list[float], vols: Optional[list[float]] = None,
          hl_pad: float = 0.0) -> list[dict]:
    base = datetime(2026, 1, 1)
    out = []
    for i, c in enumerate(closes):
        out.append({
            "bucket_time": (base + timedelta(days=i)).strftime("%Y-%m-%d"),
            "open": str(c), "high": str(c + hl_pad), "low": str(c - hl_pad),
            "close": str(c),  # market-api 는 Decimal 을 문자열로 준다 - 실측 동일
            "volume": (vols[i] if vols else 1000.0), "source": "ls_chart",
            "is_final": True,
        })
    return out


def _check_readout_math():
    """수작업 계산과 대조. closes=1..70, 거래량 65x1000+5x2000, high/low=±1.
    sma20=mean(51..70)=60.5, sma60=mean(11..70)=40.5,
    price_vs_sma20=9.5/60.5=15.7025%, price_vs_sma60=29.5/40.5=72.8395%,
    mom20=(70-50)/50=40%, mom60=(70-10)/10=600%,
    volume=2000/((15*1000+5*2000)/20=1250)=1.6,
    range=(70-50)/(71-50)=95.2381% (hi=70+1, lo=51-1)."""
    bars = _bars([float(x) for x in range(1, 71)],
                 vols=[1000.0] * 65 + [2000.0] * 5, hl_pad=1.0)
    bars.reverse()  # market-api 최신순(desc) 그대로 - 정렬은 함수 책임
    r = compute_technical_readout(bars)
    exp = {"last_close": 70.0, "bars_used": 70, "sma20": 60.5, "sma60": 40.5,
           "price_vs_sma20_pct": 15.7025, "price_vs_sma60_pct": 72.8395,
           "momentum_20d_pct": 40.0, "momentum_60d_pct": 600.0,
           "volume_ratio_5d_over_20d": 1.6, "range_position_20d_pct": 95.2381,
           "cross_sma20_sma60_recent_10d": "NONE"}
    for k, v in exp.items():
        got = r[k]
        if isinstance(v, float):
            assert got is not None and abs(got - v) < 1e-6, f"{k}: {got} != {v}"
        else:
            assert got == v, f"{k}: {got} != {v}"
    assert r["last_close_date"] == "2026-03-11"  # 2026-01-01 + 69일
    print("  결정론 지표 계산         OK")


def _check_volatility():
    # (a) 일정 성장률 1%/일 - 수익률 표준편차 0 - 변동성 0
    r = compute_technical_readout(_bars([100.0 * 1.01 ** k for k in range(21)]))
    assert r["volatility_20d_annualized_pct"] == 0.0, r
    # (b) 100 x20 후 110: 수익률 19x0 + 0.10, mean=0.005,
    #     var=(19*0.005^2+0.095^2)/19=0.0005, std=0.0223607,
    #     x sqrt(252) x100 = 35.4965% (수작업 계산)
    r = compute_technical_readout(_bars([100.0] * 20 + [110.0]))
    v = r["volatility_20d_annualized_pct"]
    assert v is not None and abs(v - 35.4965) < 1e-3, v
    assert r["momentum_20d_pct"] == 10.0
    print("  변동성 수작업 대조       OK")


def _check_insufficient_bars():
    # 5봉: 20일 이상 필요한 지표 전부 None - 전체 판단 불가
    r = compute_technical_readout(_bars([100.0, 101.0, 102.0, 103.0, 104.0]))
    for k in CORE_METRICS:
        assert r[k] is None, f"{k} 가 봉 5개로 계산됐다"
    assert _all_core_none(r)
    # 0봉도 죽지 않고 전부 None
    r0 = compute_technical_readout([])
    assert r0["bars_used"] == 0 and _all_core_none(r0)
    # 25봉: sma20 계열은 나오고 sma60 계열만 None (부분 미확인)
    r25 = compute_technical_readout(_bars([float(x) for x in range(1, 26)]))
    assert r25["price_vs_sma20_pct"] is not None
    assert r25["momentum_20d_pct"] is not None
    assert r25["sma60"] is None and r25["momentum_60d_pct"] is None
    assert r25["cross_sma20_sma60_recent_10d"] is None
    assert not _all_core_none(r25)
    print("  봉 부족 None 처리        OK")


def _check_cross_detection():
    # 65일 횡보 후 5일 상승 - sma20 이 sma60 을 상향 돌파(최근 10일 내)
    r = compute_technical_readout(
        _bars([100.0] * 65 + [101.0, 102.0, 103.0, 104.0, 105.0]))
    assert r["cross_sma20_sma60_recent_10d"] == "GOLDEN", r
    # 65일 횡보 후 5일 하락 - 데드 크로스
    r = compute_technical_readout(
        _bars([100.0] * 65 + [99.0, 98.0, 97.0, 96.0, 95.0]))
    assert r["cross_sma20_sma60_recent_10d"] == "DEAD", r
    # 크로스가 15일 전(10일 창 밖)이면 NONE - 오래된 크로스를 최근으로 안 판다
    r = compute_technical_readout(
        _bars([100.0] * 60 + [100.0 + k for k in range(1, 16)]))
    assert r["cross_sma20_sma60_recent_10d"] == "NONE", r
    print("  골든/데드 크로스 판정    OK")


def _check_verify_hallucination():
    readout = compute_technical_readout(_bars([float(x) for x in range(1, 71)]))
    note = TechnicalNote(stance="BULLISH", summary="20일 모멘텀 40.0% 상승 추세다.",
                         used_metrics=["momentum_20d_pct", "rsi_14",
                                       "last_close_date", "momentum_20d_pct"],
                         cautions=[])
    v, dropped = verify(note, readout)
    # 없는 키(rsi_14)·비수치 키(last_close_date)는 버리고, 중복은 1개로
    assert v.used_metrics == ["momentum_20d_pct"], v.used_metrics
    assert dropped["hallucinated_metrics"] == ["rsi_14", "last_close_date"]
    assert dropped["demoted"] is None and v.stance == "BULLISH"
    print("  환각 키 제거·카운트      OK")


def _check_verify_number_flag():
    readout = compute_technical_readout(
        _bars([float(x) for x in range(1, 71)], hl_pad=1.0))
    note = TechnicalNote(
        stance="BULLISH",
        # 40.0% 는 readout 그대로(통과), -15.7% 는 부호 반전 서술(±0.1 허용),
        # 99.9% 는 어느 수치에도 없다(플래그)
        summary="모멘텀 40.0% 에 sma20 대비 -15.7% 괴리, 승률은 99.9% 다.",
        used_metrics=["momentum_20d_pct"], cautions=[])
    v, dropped = verify(note, readout)
    assert dropped["flagged_numbers"] == ["99.9%"], dropped
    assert any("99.9%" in c for c in v.cautions), v.cautions
    print("  summary 수치 대조 플래그 OK")


def _check_verify_contradiction():
    readout = compute_technical_readout(_bars([float(x) for x in range(1, 71)]))
    assert readout["momentum_20d_pct"] == 40.0
    note = TechnicalNote(stance="BEARISH", summary="추세가 꺾일 것 같다.",
                         used_metrics=["momentum_20d_pct"], cautions=[])
    v, dropped = verify(note, readout)
    assert v.stance == "NEUTRAL", "모멘텀 +40% 인데 BEARISH 가 통과했다"
    assert dropped["demoted"] and "정면 모순" in dropped["demoted"]
    assert any("[강등]" in c for c in v.cautions)
    # 반대로 하락 추세 + BULLISH 도 강등
    down = compute_technical_readout(_bars([float(x) for x in range(70, 0, -1)]))
    assert down["momentum_20d_pct"] is not None and down["momentum_20d_pct"] < -5
    note2 = TechnicalNote(stance="BULLISH", summary="반등이 기대된다.",
                          used_metrics=["momentum_20d_pct"], cautions=[])
    v2, d2 = verify(note2, down)
    assert v2.stance == "NEUTRAL" and d2["demoted"]
    # 모멘텀과 부합하는 stance 는 건드리지 않는다 (강등이지 조작이 아니다)
    note3 = TechnicalNote(stance="BULLISH", summary="상승 추세다.",
                          used_metrics=["momentum_20d_pct"], cautions=[])
    v3, d3 = verify(note3, readout)
    assert v3.stance == "BULLISH" and d3["demoted"] is None
    print("  모순 stance NEUTRAL 강등 OK")


def _check_narrate_roundtrip():
    readout = compute_technical_readout(_bars([float(x) for x in range(1, 71)]))

    def fake_llm(system, user):
        assert "NEVER recompute" in system and "momentum_20d_pct" in user
        return json.dumps({"stance": "bullish",  # 소문자도 정규화되는지
                           "summary": "20일 모멘텀 40.0% 로 상승 추세다.",
                           "used_metrics": ["momentum_20d_pct"],
                           "cautions": ["단기 과열 주의"]}, ensure_ascii=False)

    note = narrate(readout, "TEST", llm=fake_llm)
    assert note.stance == "BULLISH" and note.used_metrics == ["momentum_20d_pct"]

    calls = {"n": 0}

    def flaky_llm(_s, _u):
        calls["n"] += 1
        if calls["n"] == 1:
            return "죄송하지만 JSON 이 아닙니다"
        return json.dumps({"stance": "NEUTRAL", "summary": "판단 유보.",
                           "used_metrics": [], "cautions": []})

    note = narrate(readout, "TEST", llm=flaky_llm)
    assert calls["n"] == 2 and note.stance == "NEUTRAL"

    try:
        narrate(readout, "TEST", llm=lambda s, u: "no json ever")
        raise AssertionError("Schema 위반이 두 번인데 통과했다")
    except RuntimeError:
        pass
    print("  가짜 LLM narrate 왕복    OK")


def _check_analyze_pipeline():
    bars = _bars([float(x) for x in range(1, 71)],
                 vols=[1000.0] * 65 + [2000.0] * 5, hl_pad=1.0)
    bars.reverse()  # API 최신순 그대로

    def fake_get(url):
        assert "/bars/000000" in url and "source=ls_chart" in url, url
        return bars

    def fake_llm(_s, _u):  # 환각 키 + 창작 수치 + 모순 stance 전부 탑재
        return json.dumps({"stance": "BEARISH",
                           "summary": "하락 반전 확률 99.9% 로 본다.",
                           "used_metrics": ["momentum_20d_pct", "rsi_14"],
                           "cautions": []}, ensure_ascii=False)

    out = analyze("000000", llm=fake_llm, get=fake_get)
    assert out["verdict"] == "NEUTRAL", out          # 모순 - 강등됐다
    assert out["dropped"]["hallucinated_metrics"] == ["rsi_14"]
    assert out["dropped"]["flagged_numbers"] == ["99.9%"]
    assert out["dropped"]["demoted"] and out["llm_status"] == "OK"
    assert out["readout"]["momentum_20d_pct"] == 40.0

    # 봉 부족 - LLM 을 부르지 않고 INSUFFICIENT_DATA
    out2 = analyze("000000", get=lambda u: _bars([100.0] * 5),
                   llm=lambda s, u: (_ for _ in ()).throw(
                       AssertionError("봉 부족인데 LLM 이 불렸다")))
    assert out2["verdict"] == "INSUFFICIENT_DATA" and "봉 5개" in out2["reason"]

    # LLM 다운 - readout 은 있되 stance 없이 LLM_UNAVAILABLE
    def dead_llm(_s, _u):
        raise OSError("connection refused")

    out3 = analyze("000000", get=fake_get, llm=dead_llm)
    assert out3["verdict"] is None and out3["llm_status"] == "LLM_UNAVAILABLE"
    assert out3["readout"]["momentum_20d_pct"] == 40.0
    print("  analyze 파이프라인       OK")


def _check_api_down_fail_closed():
    def down(_u):
        raise OSError("connection refused")

    out = analyze("000660", get=down)
    assert out["verdict"] == "INSUFFICIENT_DATA" and out["readout"] is None
    assert "호출 실패" in out["reason"] and "connection refused" in out["reason"]
    print("  API 장애 fail-closed     OK")


# ---------------------------------------------------------------------------

def _print_result(out: dict) -> None:
    print(f"  verdict={out.get('verdict')} "
          f"(llm={out.get('llm_status', '-')}, model={out.get('model')})")
    if out.get("reason"):
        print(f"  reason: {out['reason']}")
    r = out.get("readout")
    if r:
        print(f"  readout ({r['bars_used']}봉, 최종 {r['last_close_date']}):")
        for k in ("last_close", "sma20", "sma60", *CORE_METRICS):
            v = r.get(k)
            print(f"    {k:32} {'미확인' if v is None else v}")
    if out.get("summary"):
        print(f"  summary: {out['summary']}")
        print(f"  used_metrics: {out['used_metrics']}")
        for c in out.get("cautions", []):
            print(f"  caution: {c}")
        d = out["dropped"]
        demote = d["demoted"] or "없음"
        print(f"  dropped: 환각 키 {len(d['hallucinated_metrics'])}"
              f"{d['hallucinated_metrics'] or ''} / "
              f"수치 플래그 {len(d['flagged_numbers'])}"
              f"{d['flagged_numbers'] or ''} / 강등 {demote}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        a = sys.argv
        sym = a[a.index("--run") + 1] if a.index("--run") + 1 < len(a) else "000660"
        print(f"{AGENT_VERSION} 실측: {sym} (market-api + Ollama {MODEL})")
        _print_result(analyze(sym))
        raise SystemExit(0)

    print(f"{AGENT_VERSION} 자체 점검 (네트워크 없음, 합성 봉)")
    _check_readout_math()
    _check_volatility()
    _check_insufficient_bars()
    _check_cross_detection()
    _check_verify_hallucination()
    _check_verify_number_flag()
    _check_verify_contradiction()
    _check_narrate_roundtrip()
    _check_analyze_pipeline()
    _check_api_down_fail_closed()
    print("technical-analyst 10개 영역 통과. 실측은 --run <종목코드>")
