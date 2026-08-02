#!/usr/bin/env python3
"""sector-regime-analyst (RES-07) - 리서치본부 섹터·레짐 분석가 실행 실체.

담당: 재일 (리서치/퀀트)
근거: departments/01-research/hermes/config.yaml 의 sector-regime-analyst 페르소나
      ("Working from deterministic regime features, you explain risk-on/off ..."
       - 레짐 피처를 만드는 게 아니라 설명하는 역할)
      docs/04-organization/AGENT_EMPLOYEE_PROFILES.md RES-07 직무기술서
      api/market_api.py /regime/daily 주석 ("RES-07 의 결정론 재료 ...
       계산은 전부 이 SQL - LLM 은 이 결과를 서술만 한다")
      agents/technical_analyst.py 의 compute(순수) -> narrate(LLM) ->
       verify(결정론) 단계 분리·환각 인용 검증 패턴

설계 결정:
  1. **레짐 라벨은 코드가 정한다. LLM 은 서술만 한다.** compute_regime_readout
     이 결정론 규칙으로 regime_label 을 확정하고, 라벨 기준 자체(regime_rules)
     를 readout 에 같이 기록한다 - 라벨이 왜 그렇게 나왔는지 출력만 봐도
     재검산할 수 있게. LLM 에게는 "라벨을 그대로 받아 적고 수치를 서술하라"
     고만 지시한다.
  2. verify 는 LLM 이 라벨을 바꿨으면 결정론 라벨로 **복원**한다. RES-04 의
     stance 강등과 다르다 - stance 는 LLM 의 판단이라 모순 시 NEUTRAL 로
     보수화(강등)하지만, 레짐 라벨은 애초에 LLM 산출물이 아니므로 LLM 의
     변경은 무효이고 원 라벨로 되돌린 뒤 사유만 남긴다(라벨 권한은 코드).
  3. 환각 인용 키 제거·카운트, summary 의 %·배 수치 ±0.1 대조는 RES-04 와
     같은 규율이다. LLM 문장은 고치지 않고 cautions 에 검증 결과를 덧붙인다.
  4. 데이터 접근은 market-api 뿐이다(통합 계획 6.2, DB Credential 없음):
     GET /regime/daily (일별 등락·SMA20 상회 단면 - 주 재료),
     GET /breadth (거래소 공식 등락 - 보조. 실패해도 분석은 계속하되 해당
     필드는 None="미확인"으로 드러낸다).
  5. 판단 불가는 판단 불가로: 행이 모자란 지표는 None, 핵심 지표 전부 불가면
     INSUFFICIENT_DATA. LLM 불가면 결정론 readout+라벨만 내고 LLM_UNAVAILABLE
     명시 - RES-04 와 달리 verdict(라벨)는 유지한다. 라벨은 코드 것이므로
     LLM 이 죽어도 사라질 이유가 없다.

흐름: fetch(market-api) -> compute(결정론) -> narrate(LLM) -> verify(결정론)

이 출력은 **Agent Decision 이 아니다.** Regime Brief 의 입력 재료다
(CLAUDE.md: Agent Decision != Signal != Order). 거시 전망 하나로 주문을
지시하지 않는다(RES-07 금지 조항).

실행:
  python agents/sector_regime_analyst.py        # 자체 점검 (네트워크 없음)
  python agents/sector_regime_analyst.py --run  # 실측 (market-api + Ollama)
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

# 의미 오서술 가드(라벨-수치 결합 검사) - agents/ 를 스크립트로 실행하면
# 본부 루트가 sys.path 에 없어 evidence/ 를 직접 넣는다(collectors 와 동일 관례)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evidence"))
from narrative_guard import audit_narrative  # noqa: E402

PERSONA = "sector-regime-analyst"   # 부서 허용목록 키
AGENT_VERSION = "research-sector-regime-analyst-v1"
KST = timezone(timedelta(hours=9))

MARKET_API = os.environ.get("MARKET_API_URL", "http://127.0.0.1:8036")
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get(
    "SECTOR_REGIME_MODEL",
    # 2026-08-01 역할 분담 실측: 3.1초, 환각 0·라벨 복원 없음. 근거: 가이드 J4.
    "exaone3.5:7.8b")
LLM_TIMEOUT = float(os.environ.get("REGIME_LLM_TIMEOUT", "120"))  # 로컬 14b 지연 감안

REGIME_DAYS = 40          # 20일 지표 + 휴장/결측 여유
BREADTH_MARKETS = ("KOSPI", "KOSDAQ")

# 라벨 임계값 - regime_rules 에 문장으로도 같이 실어 출력만으로 재검산 가능하게
THRUST_AD_MIN = 4.0       # 최신 A/D 이 이상
THRUST_PREV5_MAX = 1.0    # 직전 5일 평균 A/D 이 미만(하락 우위였다가 급반전)
CAPITULATION_DA_MIN = 4.0  # 최신 D/A 이 이상
RISK_ON_PCT = 60.0        # SMA20 상회 비율 이 이상
RISK_OFF_PCT = 30.0       # SMA20 상회 비율 이 이하

REGIME_RULES = {
    "판정순서": "BREADTH_THRUST -> CAPITULATION -> RISK_ON -> RISK_OFF -> NEUTRAL",
    "BREADTH_THRUST": "최신 A/D>=4 이고 직전 5일 평균 A/D<1 "
                      "(decliners=0 이면 A/D 는 무한대로 본다)",
    "CAPITULATION": "최신 D/A>=4 (advancers=0 이면 D/A 는 무한대로 본다)",
    "RISK_ON": "최신 SMA20 상회 비율>=60%",
    "RISK_OFF": "최신 SMA20 상회 비율<=30%",
    "NEUTRAL": "위 어느 규칙에도 해당 없음",
    "INSUFFICIENT_DATA": "핵심 지표 전부 계산 불가(행 부족)",
}

# 이 지표가 전부 None 이면 레짐 판단 자체가 불가하다 (INSUFFICIENT_DATA)
CORE_METRICS = (
    "ad_ratio", "ad_ratio_5d_avg", "ad_ratio_20d_avg", "ad_ratio_prev5_avg",
    "above_sma20_pct", "above_sma20_pct_5d_avg", "above_sma20_pct_20d_avg",
    "net_advances_5d_sum", "up_days_in_20d",
)

# 인용(used_metrics) 대상이 아닌 bookkeeping 키 - 라벨·규칙·날짜는 지표가 아니다
_NON_METRIC = ("regime_label", "regime_rules", "latest_trade_date")


# ---------------------------------------------------------------------------
# LLM 출력 계약 (개발 원칙 2: 항상 Pydantic 으로 검증)
# ---------------------------------------------------------------------------

class RegimeNote(BaseModel):
    regime: Literal["RISK_ON", "RISK_OFF", "BREADTH_THRUST", "CAPITULATION",
                    "NEUTRAL", "INSUFFICIENT_DATA"]
    summary: str = Field(max_length=600, description="한국어 2~3문장 레짐 서술")
    used_metrics: list[str] = Field(description="서술이 실제 인용한 readout 키만")
    cautions: list[str] = Field(default_factory=list)

    @field_validator("regime", mode="before")
    @classmethod
    def _norm_regime(cls, v):
        return str(v).strip().upper()

    @field_validator("used_metrics", "cautions", mode="before")
    @classmethod
    def _coerce_str_list(cls, v):
        return [str(x).strip() for x in v] if isinstance(v, list) else v


# ---------------------------------------------------------------------------
# 1. compute - 결정론 레짐 계산 (순수 함수, LLM 무관)
# ---------------------------------------------------------------------------

def _r4(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 4)


def _avg(vals: list[Optional[float]]) -> Optional[float]:
    # 창 안에 계산 불가한 날이 하나라도 끼면 평균도 미확인이다 - 부분 평균으로
    # 위장하지 않는다
    if not vals or any(v is None for v in vals):
        return None
    return _r4(sum(vals) / len(vals))


def _ad_ratio(adv: int, dec: int) -> Optional[float]:
    # 하락 0 은 비율로 표현 불가(무한대) - 수치는 None, 라벨 규칙은 원 카운트로
    # 별도 판정한다
    return None if dec == 0 else _r4(adv / dec)


def _clean_rows(regime_rows: Optional[list[dict]]) -> list[dict]:
    """날짜 오름차순 정렬 + 같은 거래일 중복은 나중 것(재수집 우선)으로 정리.
    등락 카운트가 없는 행은 지표를 오염시키므로 버린다."""
    dedup: dict[str, dict] = {}
    for r in sorted((r for r in (regime_rows or [])
                     if r.get("trade_date") is not None
                     and r.get("advancers") is not None
                     and r.get("decliners") is not None),
                    key=lambda r: str(r["trade_date"])[:10]):
        dedup[str(r["trade_date"])[:10]] = r
    return [dedup[k] for k in sorted(dedup)]


def compute_regime_readout(regime_rows: list[dict],
                           breadth_rows: Optional[list[dict]] = None) -> dict:
    """market-api /regime/daily 행 목록 -> 레짐 readout + 결정론 라벨.

    순수 함수 - 네트워크·시계 없이 입력만으로 계산한다(자체 점검 대상).
    /regime/daily 는 최신순(desc)으로 주므로 여기서 오름차순 정렬한다.
    각 지표는 행 충분성을 검사해 부족하면 None(=미확인)이고, 핵심 지표가
    전부 None 이면 regime_label 은 INSUFFICIENT_DATA 다.
    breadth_rows(거래소 공식 등락)는 보조 필드 - 없으면 None(미확인)이다.
    """
    rows = _clean_rows(regime_rows)
    n = len(rows)

    advs = [int(r["advancers"]) for r in rows]
    decs = [int(r["decliners"]) for r in rows]
    ratios = [_ad_ratio(a, d) for a, d in zip(advs, decs)]

    def above_pct(r: dict) -> Optional[float]:
        cov = int(r.get("sma20_coverage") or 0)
        # 분모(20봉 완비 종목)가 0 이면 비율이 존재하지 않는다 - 미확인
        return None if cov == 0 else _r4(int(r.get("above_sma20") or 0) / cov * 100.0)

    pcts = [above_pct(r) for r in rows]

    ad_latest = ratios[-1] if n else None
    ad_5 = _avg(ratios[-5:]) if n >= 5 else None
    ad_20 = _avg(ratios[-20:]) if n >= 20 else None
    ad_prev5 = _avg(ratios[-6:-1]) if n >= 6 else None  # 최신일 제외 직전 5일

    pct_latest = pcts[-1] if n else None
    pct_5 = _avg(pcts[-5:]) if n >= 5 else None
    pct_20 = _avg(pcts[-20:]) if n >= 20 else None

    net_5 = sum(a - d for a, d in zip(advs[-5:], decs[-5:])) if n >= 5 else None
    up_20 = sum(1 for a, d in zip(advs[-20:], decs[-20:]) if a > d) if n >= 20 else None

    readout = {
        "latest_trade_date": str(rows[-1]["trade_date"])[:10] if rows else None,
        "days_used": n,
        "latest_advancers": advs[-1] if n else None,
        "latest_decliners": decs[-1] if n else None,
        "latest_unchanged": int(rows[-1].get("unchanged") or 0) if n else None,
        "latest_above_sma20": int(rows[-1].get("above_sma20") or 0) if n else None,
        "latest_sma20_coverage": int(rows[-1].get("sma20_coverage") or 0) if n else None,
        "latest_symbols": int(rows[-1].get("symbols") or 0) if n else None,
        "ad_ratio": ad_latest,
        "ad_ratio_5d_avg": ad_5,
        "ad_ratio_20d_avg": ad_20,
        "ad_ratio_prev5_avg": ad_prev5,
        "above_sma20_pct": pct_latest,
        "above_sma20_pct_5d_avg": pct_5,
        "above_sma20_pct_20d_avg": pct_20,
        "net_advances_5d_sum": net_5,
        "up_days_in_20d": up_20,
        "breadth_by_market": _breadth_by_market(breadth_rows),
    }

    # 라벨 결정 - 비율이 무한대인 날(하락 0/상승 0)도 규칙이 성립하도록
    # 나눗셈 대신 원 카운트 부등식(a>=4d, d>=4a)으로 판정한다
    if all(readout[k] is None for k in CORE_METRICS):
        label = "INSUFFICIENT_DATA"
    else:
        a, d = advs[-1], decs[-1]
        if a > 0 and a >= THRUST_AD_MIN * d \
                and ad_prev5 is not None and ad_prev5 < THRUST_PREV5_MAX:
            label = "BREADTH_THRUST"
        elif d > 0 and d >= CAPITULATION_DA_MIN * a:
            label = "CAPITULATION"
        elif pct_latest is not None and pct_latest >= RISK_ON_PCT:
            label = "RISK_ON"
        elif pct_latest is not None and pct_latest <= RISK_OFF_PCT:
            label = "RISK_OFF"
        else:
            label = "NEUTRAL"

    readout["regime_label"] = label
    readout["regime_rules"] = REGIME_RULES
    return readout


def _breadth_by_market(breadth_rows: Optional[list[dict]]) -> Optional[dict]:
    """시장별 최신 공식 등락(보조 필드). 못 받았으면 None = 미확인."""
    if not breadth_rows:
        return None
    latest: dict[str, dict] = {}
    for r in breadth_rows:
        m = r.get("market")
        if m is None or r.get("event_time") is None:
            continue
        # desc 순서를 신뢰하지 않고 event_time 으로 직접 최신을 고른다
        if m not in latest or str(r["event_time"]) > str(latest[m]["event_time"]):
            latest[m] = r
    if not latest:
        return None
    return {m: {"event_time": str(r["event_time"]),
                "advancers": None if r.get("advancers") is None else int(r["advancers"]),
                "decliners": None if r.get("decliners") is None else int(r["decliners"]),
                "unchanged": None if r.get("unchanged") is None else int(r["unchanged"])}
            for m, r in sorted(latest.items())}


# ---------------------------------------------------------------------------
# 2. narrate - LLM 은 여기서만 쓴다 (서술만. 라벨은 이미 코드가 정했다)
# ---------------------------------------------------------------------------

_SYSTEM = """You are the Sector & Regime Analyst (RES-07) of a research department.
You are given a code-computed market regime readout (JSON) for the Korean market.
The regime label was ALREADY decided by deterministic code rules - it is in
readout.regime_label and the exact rules are in readout.regime_rules.
Your ONLY job is to narrate it. Rules:
- "regime" MUST be readout.regime_label copied EXACTLY as given. You have NO
  authority to change the label - the code does. If you disagree, write your
  concern in cautions, but "regime" must still equal regime_label.
- NEVER recompute, invent or extrapolate numbers. Quote metric values EXACTLY
  as given in the readout. Cite ONLY metric keys that exist in it.
- A null metric means insufficient rows ("미확인") - do not guess its value,
  and mention the limitation in cautions instead.
- summary: 2~3 Korean sentences describing the market regime, quoting the
  given values (% for *_pct keys, 배 for A/D ratio keys where natural).
- used_metrics: ONLY the readout keys your summary actually relied on.
- cautions: short Korean phrases for risks/limits (null metrics, sma20
  coverage, breadth 미확인 etc).
Output JSON only, exactly this shape, no other text:
{"regime":"NEUTRAL","summary":"...","used_metrics":["ad_ratio"],
 "cautions":["..."]}"""


def narrate(readout: dict, llm: Optional[Callable] = None) -> RegimeNote:
    """readout 을 LLM 에 주고 RegimeNote 를 받는다. Schema 불합격이면 한 번
    고쳐 부르고, 또 실패하면 예외다 - 서술을 지어내지 않는다(호출부가
    LLM_UNAVAILABLE 처리). llm 주입은 자체 점검용."""
    allowed = [k for k in readout if k not in _NON_METRIC]
    prompt = (f"Deterministic regime label (copy verbatim): {readout['regime_label']}\n"
              f"Allowed metric keys: {json.dumps(allowed)}\n"
              f"Regime readout (code-computed, do not alter):\n"
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
            return RegimeNote.model_validate_json(text[start:end + 1])
        except (ValidationError, ValueError) as e:
            last_err = str(e)[:200]
    raise RuntimeError(f"LLM 서술이 Schema 를 두 번 어겼다: {last_err}")


def _ollama_call(system: str, user: str) -> str:
    """로컬/팀 Ollama (OpenAI 호환). technical_analyst._ollama_call 과 동일 패턴.
    qwen3 계열의 <think> 프리앰블은 호출부 파서가 첫 '{'~마지막 '}' 만
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
_RATIO_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*배")


def _number_pool(readout: dict) -> list[float]:
    pool = [float(v) for k, v in readout.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and k != "days_used"]
    for m in (readout.get("breadth_by_market") or {}).values():
        pool.extend(float(v) for v in m.values()
                    if isinstance(v, (int, float)) and not isinstance(v, bool))
    return pool


def verify(note: RegimeNote, readout: dict) -> tuple[RegimeNote, dict]:
    """(1) used_metrics 중 readout 에 없는/인용 불가한 키 제거(환각 인용 - 카운트),
    (2) summary 의 %·배 수치가 readout 수치(±0.1, 부호 반전 서술 허용) 밖이면
        플래그해 cautions 에 남긴다,
    (3) LLM 이 regime 라벨을 바꿨으면 결정론 라벨로 **복원**하고 사유 기록 -
        강등이 아니라 복원이다. 라벨 권한은 코드에 있고 LLM 변경은 무효다.
    LLM 이 쓴 summary 원문은 고치지 않는다."""
    dropped: dict = {"hallucinated_metrics": [], "flagged_numbers": [],
                     "label_restored": None}

    kept: list[str] = []
    for k in note.used_metrics:
        if k in readout and k not in _NON_METRIC:
            if k not in kept:
                kept.append(k)
        else:
            dropped["hallucinated_metrics"].append(k)

    pool = _number_pool(readout)
    for rx in (_PCT_RE, _RATIO_RE):
        for m in rx.finditer(note.summary):
            x = float(m.group(1))
            if not any(abs(x - v) <= 0.1 or abs(x + v) <= 0.1 for v in pool):
                dropped["flagged_numbers"].append(m.group(0))

    cautions = list(note.cautions)
    for f in dropped["flagged_numbers"]:
        cautions.append(f"[숫자검증] summary 의 {f} 는 readout 수치와 불일치"
                        f"(±0.1 밖) - 해당 문장 신뢰 금지")

    regime = note.regime
    code_label = readout.get("regime_label")
    if code_label and regime != code_label:
        reason = (f"LLM 이 regime 을 {regime} 로 적었다 - 결정론 라벨 "
                  f"{code_label} 로 복원 (라벨 권한은 코드에 있다)")
        dropped["label_restored"] = reason
        cautions.append("[복원] " + reason)
        regime = code_label

    # 의미 오서술 가드 v1(evidence/narrative_guard): 수치는 readout 그대로인데
    # 라벨을 바꿔 부르는 서술(실측: 상회 5일 평균 22.11 을 "A/D 비율")은 위
    # 숫자 대조가 못 잡는다 - 라벨-수치 결합을 같은 문장 단위로 검사한다.
    # v1 은 표시만 한다(레짐 라벨·verdict 불변) - 오탐 위험을 낮게 시작.
    dropped["label_flags"] = audit_narrative(note.summary, readout)
    for lf in dropped["label_flags"]:
        if lf["check"] == "percentile_literal":
            cautions.append(f"[라벨검증] summary 의 {lf['value']} - {lf['reason']}")
        else:
            cautions.append(
                f"[라벨검증] summary 의 {lf['value']} 는 {lf['metric']} 수치인데 "
                f"같은 문장에 다른 지표 라벨 '{lf['forbidden_hit']}' - 오서술 의심")

    return (note.model_copy(update={"regime": regime, "used_metrics": kept,
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


def analyze(*, market_api: Optional[str] = None, llm: Optional[Callable] = None,
            get: Callable = _http_get) -> dict:
    """market-api 호출 -> 결정론 계산·라벨 -> LLM 서술 -> 결정론 검증.

    /regime/daily 불가/행 전무 -> verdict INSUFFICIENT_DATA (사유 명시).
    /breadth 불가 -> breadth_by_market None(미확인)으로 계속한다 - 보조 재료다.
    LLM 불가 -> 결정론 readout+라벨은 그대로, llm_status=LLM_UNAVAILABLE.
    """
    base = (market_api or MARKET_API).rstrip("/")
    ts = datetime.now(timezone.utc)
    out = {"agent": AGENT_VERSION, "as_of": ts.isoformat(), "model": MODEL}
    try:
        regime_rows = get(f"{base}/regime/daily?days={REGIME_DAYS}")
    except Exception as e:  # noqa: BLE001 - 미가동을 통과로 위장하지 않는다
        return {**out, "verdict": "INSUFFICIENT_DATA", "readout": None,
                "reason": f"market-api /regime/daily 호출 실패: {type(e).__name__}: {e}"}

    breadth_rows: list[dict] = []
    for mkt in BREADTH_MARKETS:
        try:
            breadth_rows.extend(get(f"{base}/breadth?market={mkt}&limit=1"))
        except Exception:  # noqa: BLE001 - 보조 재료 - 실패는 미확인으로 드러난다
            pass

    readout = compute_regime_readout(regime_rows, breadth_rows or None)
    if readout["regime_label"] == "INSUFFICIENT_DATA":
        return {**out, "verdict": "INSUFFICIENT_DATA", "readout": readout,
                "reason": f"레짐 지표 전부 계산 불가 (행 {readout['days_used']}개)"}

    try:
        note = narrate(readout, llm=llm)
    except Exception as e:  # noqa: BLE001 - Ollama 다운/Schema 반복 위반 등
        # 라벨은 코드 산출물이므로 LLM 이 죽어도 verdict 는 남긴다 (설계 결정 5)
        return {**out, "verdict": readout["regime_label"],
                "llm_status": "LLM_UNAVAILABLE", "readout": readout,
                "reason": f"{type(e).__name__}: {str(e)[:300]}"}

    note, dropped = verify(note, readout)
    return {**out, "verdict": note.regime, "llm_status": "OK",
            "readout": readout, "summary": note.summary,
            "used_metrics": note.used_metrics, "cautions": note.cautions,
            "dropped": dropped}


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음 (합성 행 + 가짜 LLM 주입)
# ---------------------------------------------------------------------------

def _rows(specs: list[tuple]) -> list[dict]:
    """specs: (advancers, decliners, unchanged, above_sma20, sma20_coverage)."""
    base = datetime(2026, 1, 1)
    out = []
    for i, (adv, dec, unch, above, cov) in enumerate(specs):
        out.append({
            "trade_date": (base + timedelta(days=i)).strftime("%Y-%m-%d"),
            "advancers": adv, "decliners": dec, "unchanged": unch,
            "above_sma20": above, "sma20_coverage": cov,
            "symbols": adv + dec + unch,
        })
    return out


def _check_readout_math():
    """수작업 계산과 대조. 19일 (40/80, 상회 30%) + 5일 (60/120, 상회 20%)
    + 최신일 (250/50, 상회 60%):
    ad_ratio=5.0, prev5=0.5, 5d=(0.5*4+5)/5=1.4, 20d=(0.5*19+5)/20=0.725,
    above%=60, 5d=(20*4+60)/5=28, 20d=(30*14+20*5+60)/20=29,
    net5=(-60)*4+200=-40, up20=1. 라벨: A/D 5>=4, prev5 0.5<1 -> THRUST."""
    rows = _rows([(40, 80, 10, 30, 100)] * 19 + [(60, 120, 10, 20, 100)] * 5
                 + [(250, 50, 10, 60, 100)])
    rows.reverse()  # market-api 최신순(desc) 그대로 - 정렬은 함수 책임
    r = compute_regime_readout(rows)
    exp = {"days_used": 25, "latest_trade_date": "2026-01-25",
           "latest_advancers": 250, "latest_decliners": 50,
           "latest_unchanged": 10, "latest_above_sma20": 60,
           "latest_sma20_coverage": 100, "latest_symbols": 310,
           "ad_ratio": 5.0, "ad_ratio_5d_avg": 1.4, "ad_ratio_20d_avg": 0.725,
           "ad_ratio_prev5_avg": 0.5, "above_sma20_pct": 60.0,
           "above_sma20_pct_5d_avg": 28.0, "above_sma20_pct_20d_avg": 29.0,
           "net_advances_5d_sum": -40, "up_days_in_20d": 1,
           "regime_label": "BREADTH_THRUST"}
    for k, v in exp.items():
        got = r[k]
        if isinstance(v, float):
            assert got is not None and abs(got - v) < 1e-6, f"{k}: {got} != {v}"
        else:
            assert got == v, f"{k}: {got} != {v}"
    assert r["regime_rules"] == REGIME_RULES  # 라벨 기준이 출력에 실려 있다

    # 같은 거래일 중복은 나중 것(재수집)이 이긴다
    rows2 = _rows([(40, 80, 10, 30, 100)] * 6)
    dup = dict(rows2[-1])
    dup.update(advancers=100, decliners=50)
    r2 = compute_regime_readout(rows2 + [dup])
    assert r2["days_used"] == 6 and r2["latest_advancers"] == 100
    print("  결정론 A/D·상회비율 대조 OK")


def _check_label_rules():
    """5개 라벨 규칙이 각각 발화하는 최소 케이스 + 억제 케이스."""
    # BREADTH_THRUST: 하락 우위 5일(0.25) 뒤 A/D 5.0 급반전
    r = compute_regime_readout(_rows([(10, 40, 0, 50, 100)] * 5
                                     + [(200, 40, 0, 50, 100)]))
    assert r["regime_label"] == "BREADTH_THRUST", r["regime_label"]
    # 직전이 상승 우위(prev5 2.0>=1)면 같은 A/D 5.0 이라도 THRUST 가 아니다
    r = compute_regime_readout(_rows([(80, 40, 0, 50, 100)] * 5
                                     + [(200, 40, 0, 50, 100)]))
    assert r["regime_label"] == "NEUTRAL", r["regime_label"]
    # 직전 5일 미확인(행 1개)이면 THRUST 를 지어내지 않는다
    r = compute_regime_readout(_rows([(200, 40, 0, 50, 100)]))
    assert r["regime_label"] == "NEUTRAL", r["regime_label"]
    # decliners=0 (A/D 무한대)도 원 카운트 판정으로 THRUST 성립
    r = compute_regime_readout(_rows([(10, 40, 0, 50, 100)] * 5
                                     + [(50, 0, 0, 50, 100)]))
    assert r["regime_label"] == "BREADTH_THRUST" and r["ad_ratio"] is None
    # CAPITULATION: D/A=10, advancers=0(무한대)도 성립
    r = compute_regime_readout(_rows([(30, 300, 0, 45, 100)]))
    assert r["regime_label"] == "CAPITULATION", r["regime_label"]
    r = compute_regime_readout(_rows([(0, 100, 0, 45, 100)]))
    assert r["regime_label"] == "CAPITULATION", r["regime_label"]
    # RISK_ON: 상회 70% / 경계 60% 도 포함
    r = compute_regime_readout(_rows([(120, 100, 0, 70, 100)]))
    assert r["regime_label"] == "RISK_ON", r["regime_label"]
    r = compute_regime_readout(_rows([(100, 100, 0, 60, 100)]))
    assert r["regime_label"] == "RISK_ON", r["regime_label"]
    # RISK_OFF: 경계 30% 포함
    r = compute_regime_readout(_rows([(90, 110, 0, 30, 100)]))
    assert r["regime_label"] == "RISK_OFF", r["regime_label"]
    # NEUTRAL: 어느 규칙에도 안 걸림
    r = compute_regime_readout(_rows([(100, 110, 0, 45, 100)]))
    assert r["regime_label"] == "NEUTRAL", r["regime_label"]
    # INSUFFICIENT_DATA: 행 전무
    r = compute_regime_readout([])
    assert r["regime_label"] == "INSUFFICIENT_DATA" and r["days_used"] == 0
    print("  라벨 규칙 5종 발화       OK")


def _check_insufficient_rows():
    # 3행: 최신일 지표만 나오고 5일·20일 지표는 전부 None (부분 미확인)
    r = compute_regime_readout(_rows([(40, 80, 10, 45, 100)] * 3))
    assert r["ad_ratio"] == 0.5 and r["above_sma20_pct"] == 45.0
    for k in ("ad_ratio_5d_avg", "ad_ratio_20d_avg", "ad_ratio_prev5_avg",
              "above_sma20_pct_5d_avg", "above_sma20_pct_20d_avg",
              "net_advances_5d_sum", "up_days_in_20d"):
        assert r[k] is None, f"{k} 가 행 3개로 계산됐다"
    assert r["regime_label"] == "NEUTRAL"  # 판단 가능한 지표는 있다
    # 5행: 5일 지표는 나오고 prev5(6행 필요)·20일 지표만 None
    r5 = compute_regime_readout(_rows([(40, 80, 10, 45, 100)] * 5))
    assert r5["ad_ratio_5d_avg"] == 0.5 and r5["net_advances_5d_sum"] == -200
    assert r5["ad_ratio_prev5_avg"] is None and r5["up_days_in_20d"] is None
    # sma20_coverage=0 이면 상회 비율은 미확인 - 0% 로 위장하지 않는다
    rc = compute_regime_readout(_rows([(40, 80, 10, 0, 0)]))
    assert rc["above_sma20_pct"] is None and rc["regime_label"] == "NEUTRAL"
    print("  행 부족 None 처리        OK")


def _check_breadth_aux():
    rows = _rows([(100, 110, 0, 45, 100)])
    breadth = [
        {"event_time": "2026-07-31T15:30:00+09:00", "market": "KOSPI",
         "advancers": 296, "decliners": 51, "unchanged": 3},
        {"event_time": "2026-07-31T15:30:00+09:00", "market": "KOSDAQ",
         "advancers": 100, "decliners": 40, "unchanged": 5},
        # 더 오래된 KOSPI 행 - 최신 선택이 순서가 아니라 event_time 기준인지
        {"event_time": "2026-07-30T15:30:00+09:00", "market": "KOSPI",
         "advancers": 10, "decliners": 200, "unchanged": 1},
    ]
    r = compute_regime_readout(rows, breadth)
    bm = r["breadth_by_market"]
    assert bm["KOSPI"]["advancers"] == 296 and bm["KOSPI"]["decliners"] == 51
    assert bm["KOSDAQ"]["advancers"] == 100
    # breadth 없음 = None(미확인) - 빈 dict 로 위장하지 않는다
    assert compute_regime_readout(rows)["breadth_by_market"] is None
    assert compute_regime_readout(rows, [])["breadth_by_market"] is None
    print("  breadth 보조 필드        OK")


def _check_verify_hallucination():
    readout = compute_regime_readout(_rows([(100, 110, 0, 45, 100)] * 6))
    note = RegimeNote(regime="NEUTRAL", summary="상회 비율 45.0% 로 중립이다.",
                      used_metrics=["above_sma20_pct", "vix", "regime_rules",
                                    "above_sma20_pct"],
                      cautions=[])
    v, dropped = verify(note, readout)
    # 없는 키(vix)·인용 불가 키(regime_rules)는 버리고, 중복은 1개로
    assert v.used_metrics == ["above_sma20_pct"], v.used_metrics
    assert dropped["hallucinated_metrics"] == ["vix", "regime_rules"]
    assert dropped["label_restored"] is None and v.regime == "NEUTRAL"
    print("  환각 키 제거·카운트      OK")


def _check_verify_number_flag():
    readout = compute_regime_readout(_rows([(40, 80, 10, 30, 100)] * 19
                                           + [(60, 120, 10, 20, 100)] * 5
                                           + [(250, 50, 10, 60, 100)]))
    note = RegimeNote(
        regime="BREADTH_THRUST",
        # 60.0%·5.0배 는 readout 그대로(통과), -28.0% 는 부호 반전 서술(허용),
        # 88.8% 와 7.7배 는 어느 수치에도(±0.1) 없다(플래그)
        summary="상회 비율 60.0% 회복에 A/D 5.0배 급반등, 5일 평균 대비 -28.0% "
                "이나 승률 88.8% 나 7.7배 확대는 아니다.",
        used_metrics=["above_sma20_pct", "ad_ratio"], cautions=[])
    v, dropped = verify(note, readout)
    assert dropped["flagged_numbers"] == ["88.8%", "7.7배"], dropped
    assert sum("신뢰 금지" in c for c in v.cautions) == 2, v.cautions
    # narrative_guard v1: 제 라벨이 붙은 인용(상회 60.0%, A/D 5.0배)은 allowed
    # 근접 규칙으로 통과하고, 쉼표 run-on 절의 -28.0(상회 5일 평균의 부호
    # 반전)만 'A/D' 가 더 가까워 오서술 의심 플래그를 받는다 - v1 은 표시만
    # 하므로 여기서 개수·대상을 고정해 가드 회귀를 감지한다
    assert [f["value"] for f in dropped["label_flags"]] == ["-28.0%"], \
        dropped["label_flags"]
    assert dropped["label_flags"][0]["metric"] == "above_sma20_pct_5d_avg"
    assert sum(c.startswith("[라벨검증]") for c in v.cautions) == 1, v.cautions
    print("  summary 수치 대조 플래그 OK")


def _check_label_restore():
    readout = compute_regime_readout(_rows([(30, 300, 0, 25, 100)]))
    assert readout["regime_label"] == "CAPITULATION"
    # LLM 이 라벨을 멋대로 NEUTRAL 로 바꿨다 - 결정론 라벨로 복원된다
    note = RegimeNote(regime="NEUTRAL", summary="투매가 나왔다.",
                      used_metrics=["ad_ratio"], cautions=[])
    v, dropped = verify(note, readout)
    assert v.regime == "CAPITULATION", "라벨 권한이 LLM 에 넘어갔다"
    assert dropped["label_restored"] and "복원" in dropped["label_restored"]
    assert any(c.startswith("[복원]") for c in v.cautions), v.cautions
    # 라벨을 그대로 받아 적었으면 건드리지 않는다
    note2 = RegimeNote(regime="CAPITULATION", summary="투매가 나왔다.",
                       used_metrics=["ad_ratio"], cautions=[])
    v2, d2 = verify(note2, readout)
    assert v2.regime == "CAPITULATION" and d2["label_restored"] is None
    print("  라벨 불일치 복원         OK")


def _check_narrate_roundtrip():
    readout = compute_regime_readout(_rows([(120, 100, 0, 70, 100)]))
    assert readout["regime_label"] == "RISK_ON"

    def fake_llm(system, user):
        assert "regime_label" in system and "EXACTLY" in system  # 라벨 권한 지시
        assert "copy verbatim): RISK_ON" in user and "ad_ratio" in user
        return json.dumps({"regime": "risk_on",  # 소문자도 정규화되는지
                           "summary": "SMA20 상회 70.0% 로 위험 선호 국면이다.",
                           "used_metrics": ["above_sma20_pct"],
                           "cautions": ["표본 1일"]}, ensure_ascii=False)

    note = narrate(readout, llm=fake_llm)
    assert note.regime == "RISK_ON" and note.used_metrics == ["above_sma20_pct"]

    calls = {"n": 0}

    def flaky_llm(_s, _u):
        calls["n"] += 1
        if calls["n"] == 1:
            return "죄송하지만 JSON 이 아닙니다"
        return json.dumps({"regime": "RISK_ON", "summary": "위험 선호.",
                           "used_metrics": [], "cautions": []})

    note = narrate(readout, llm=flaky_llm)
    assert calls["n"] == 2 and note.regime == "RISK_ON"

    try:
        narrate(readout, llm=lambda s, u: "no json ever")
        raise AssertionError("Schema 위반이 두 번인데 통과했다")
    except RuntimeError:
        pass
    print("  가짜 LLM narrate 왕복    OK")


def _check_analyze_pipeline():
    regime = _rows([(40, 80, 10, 30, 100)] * 19 + [(60, 120, 10, 20, 100)] * 5
                   + [(250, 50, 10, 60, 100)])
    regime.reverse()  # API 최신순 그대로
    breadth = {"KOSPI": [{"event_time": "2026-07-31T15:30:00+09:00",
                          "market": "KOSPI", "advancers": 296, "decliners": 51,
                          "unchanged": 3}],
               "KOSDAQ": [{"event_time": "2026-07-31T15:30:00+09:00",
                           "market": "KOSDAQ", "advancers": 900, "decliners": 700,
                           "unchanged": 100}]}

    def fake_get(url):
        if "/regime/daily" in url:
            assert f"days={REGIME_DAYS}" in url, url
            return regime
        for m, rows in breadth.items():
            if f"market={m}" in url:
                return rows
        raise AssertionError(f"예상 밖 URL: {url}")

    def fake_llm(_s, _u):  # 라벨 변조 + 환각 키 + 창작 수치 전부 탑재
        return json.dumps({"regime": "RISK_OFF",
                           "summary": "하락 지속 확률 88.8% 로 본다.",
                           "used_metrics": ["ad_ratio", "vix"],
                           "cautions": []}, ensure_ascii=False)

    out = analyze(llm=fake_llm, get=fake_get)
    assert out["verdict"] == "BREADTH_THRUST", out    # 변조가 복원됐다
    assert out["dropped"]["label_restored"] is not None
    assert out["dropped"]["hallucinated_metrics"] == ["vix"]
    assert out["dropped"]["flagged_numbers"] == ["88.8%"]
    # narrative_guard v1 배선 확인: 위반 0 이어도 카운트 키는 항상 노출된다
    assert out["dropped"]["label_flags"] == []
    assert out["llm_status"] == "OK" and out["readout"]["ad_ratio"] == 5.0
    assert out["readout"]["breadth_by_market"]["KOSPI"]["advancers"] == 296

    # 행 전무 - LLM 을 부르지 않고 INSUFFICIENT_DATA
    def empty_get(url):
        return [] if "/regime/daily" in url else breadth["KOSPI"]

    out2 = analyze(get=empty_get,
                   llm=lambda s, u: (_ for _ in ()).throw(
                       AssertionError("행 전무인데 LLM 이 불렸다")))
    assert out2["verdict"] == "INSUFFICIENT_DATA" and "행 0개" in out2["reason"]

    # LLM 다운 - 라벨은 코드 것이므로 verdict 는 남는다 (RES-04 와 다른 지점)
    def dead_llm(_s, _u):
        raise OSError("connection refused")

    out3 = analyze(get=fake_get, llm=dead_llm)
    assert out3["verdict"] == "BREADTH_THRUST", out3
    assert out3["llm_status"] == "LLM_UNAVAILABLE" and "summary" not in out3

    # breadth 만 죽으면 미확인으로 계속한다 - 보조 재료가 본 분석을 못 막는다
    def breadth_down(url):
        if "/breadth" in url:
            raise OSError("connection refused")
        return regime

    out4 = analyze(get=breadth_down, llm=dead_llm)
    assert out4["verdict"] == "BREADTH_THRUST"
    assert out4["readout"]["breadth_by_market"] is None
    print("  analyze 파이프라인       OK")


def _check_api_down_fail_closed():
    def down(_u):
        raise OSError("connection refused")

    out = analyze(get=down)
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
        print(f"  readout (행 {r['days_used']}개, 최종 거래일 {r['latest_trade_date']}, "
              f"라벨 {r['regime_label']}):")
        for k in ("latest_advancers", "latest_decliners", "latest_unchanged",
                  "latest_above_sma20", "latest_sma20_coverage", "latest_symbols",
                  *CORE_METRICS):
            v = r.get(k)
            print(f"    {k:28} {'미확인' if v is None else v}")
        bm = r.get("breadth_by_market")
        if bm is None:
            print("    breadth_by_market            미확인")
        else:
            for mkt, b in bm.items():
                print(f"    breadth[{mkt}]  상승 {b['advancers']} / 하락 "
                      f"{b['decliners']} / 보합 {b['unchanged']} ({b['event_time']})")
    if out.get("summary"):
        print(f"  summary: {out['summary']}")
        print(f"  used_metrics: {out['used_metrics']}")
        for c in out.get("cautions", []):
            print(f"  caution: {c}")
        d = out["dropped"]
        restored = d["label_restored"] or "없음"
        labels = d.get("label_flags", [])
        print(f"  dropped: 환각 키 {len(d['hallucinated_metrics'])}"
              f"{d['hallucinated_metrics'] or ''} / "
              f"수치 플래그 {len(d['flagged_numbers'])}"
              f"{d['flagged_numbers'] or ''} / "
              f"라벨 플래그 {len(labels)}"
              f"{[f['value'] + '->' + f['metric'] for f in labels] or ''} / "
              f"라벨 복원 {restored}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        print(f"{AGENT_VERSION} 실측 (market-api {MARKET_API} + Ollama {MODEL})")
        _print_result(analyze())
        raise SystemExit(0)

    print(f"{AGENT_VERSION} 자체 점검 (네트워크 없음, 합성 행)")
    _check_readout_math()
    _check_label_rules()
    _check_insufficient_rows()
    _check_breadth_aux()
    _check_verify_hallucination()
    _check_verify_number_flag()
    _check_label_restore()
    _check_narrate_roundtrip()
    _check_analyze_pipeline()
    _check_api_down_fail_closed()
    print("sector-regime-analyst 10개 영역 통과. 실측은 --run")
