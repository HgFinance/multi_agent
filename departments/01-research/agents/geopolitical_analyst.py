#!/usr/bin/env python3
"""geopolitical-analyst (RES-09) - 리서치본부 지정학 분석가 실행 실체.

담당: 재일 (리서치/퀀트)
근거: 재일님 지시 2026-08-01 "리서치 본부 에이전트에 지정학 분석가 하나
      추가해" (앞선 대화: "국제정치 관련해서 시장이 들썩이는 경우가 있어서
      - 이란을 폭격한다니 마니 이런 것들")
      collectors/geopolitical_collector.py 가 적재한 GPR·GDELT 계열
      agents/sector_regime_analyst.py 의 compute(순수) -> narrate(LLM) ->
       verify(결정론) 3단 분리·라벨 복원 패턴을 그대로 따른다

설계 결정
  1. **라벨은 코드가 정한다.** compute_geo_readout 이 결정론 규칙으로
     risk_label 과 driver 를 확정하고 판정 기준(geo_rules)을 readout 에
     같이 실어 출력만으로 재검산되게 한다. LLM 은 서술만 한다.
  2. **driver 를 따로 낸다** - THREAT_LED(위협·언사 주도) vs ACT_LED(실제
     사건 주도). GPR 지수가 GPRD_THREAT / GPRD_ACT 를 분리 제공하므로
     "폭격한다니 만다니"와 실제 타격을 코드가 구분한다. 같은 리스크 수준
     이어도 시장 반응이 다른 축이라 라벨 하나로 뭉개지 않는다.
  3. **신선도를 숨기지 않는다.** GPR 은 게시가 4~5일 늦다(수집기가 보수적
     으로 period+7일 기록). readout 에 gpr_lag_days 를 반드시 실어 "오늘
     값"으로 오독되지 않게 한다. 지연이 GPR_STALE_MAX_DAYS 를 넘으면
     라벨을 GPR 없이(GDELT 만으로) 판정하고 사유를 남긴다.
  4. 데이터 접근은 research-api 뿐이다(DB Credential 없음):
     GET /macro/observations (PIT: published_at<=as_of, 최신 vintage).
  5. 판단 불가는 판단 불가로: 계열이 전무하면 INSUFFICIENT_DATA. LLM 불가
     여도 라벨은 코드 산출물이라 verdict 를 유지한다(RES-07 과 동일).

이 출력은 **Agent Decision 이 아니다.** Research Packet 의 입력 재료다.
지정학 국면 하나로 주문을 지시하지 않는다 - 전달 경로(유가·환율·수출)를
거쳐야 종목 함의가 되며, 그 판단은 총괄·리스크의 몫이다.

실행
  python agents/geopolitical_analyst.py        # 자체 점검 (네트워크 없음)
  python agents/geopolitical_analyst.py --run  # 실측 (research-api + Ollama)
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evidence"))
from narrative_guard import audit_narrative  # noqa: E402

AGENT_VERSION = "research-geopolitical-analyst-v1"
KST = timezone(timedelta(hours=9))

RESEARCH_API = os.environ.get("RESEARCH_API_URL", "http://127.0.0.1:8035")
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get(
    "GEOPOLITICAL_MODEL",
    # 정형 판정·서술 역할이라 J4 실측에서 통과한 EXAONE 을 기본으로 둔다
    # (감성처럼 다중 인용을 요구하지 않아 qwen 을 쓸 이유가 없다).
    "exaone3.5:7.8b")
LLM_TIMEOUT = float(os.environ.get("GEO_LLM_TIMEOUT", "120"))

WINDOW_DAYS = 120           # 백분위·중앙값 기준 창
GPR_CODES = ("GPRD", "GPRD_THREAT", "GPRD_ACT")
GPR_STALE_MAX_DAYS = 21     # 이보다 오래되면 GPR 을 라벨 판정에서 뺀다
MIN_POINTS = 20             # 백분위·중앙값이 의미를 갖는 최소 관측 수

# 라벨 임계값 - geo_rules 에 문장으로도 실어 출력만으로 재검산 가능하게
SHOCK_PCTL = 95.0           # GPR 백분위 이 이상
SHOCK_RATIO = 2.5           # GDELT 테마 최근/중앙 배율 이 이상
ELEVATED_PCTL = 80.0
ELEVATED_RATIO = 1.8
CALM_PCTL = 20.0
CALM_RATIO = 1.3
DRIVER_MARGIN = 1.15        # 위협/실제 비가 이 배 넘으면 한쪽 주도로 본다

GEO_RULES = {
    "판정순서": "SHOCK -> ELEVATED -> CALM -> NEUTRAL",
    "SHOCK": f"GPR 백분위>={SHOCK_PCTL} 또는 테마 배율>={SHOCK_RATIO}",
    "ELEVATED": f"GPR 백분위>={ELEVATED_PCTL} 또는 테마 배율>={ELEVATED_RATIO}",
    "CALM": f"GPR 백분위<={CALM_PCTL} 이고 모든 테마 배율<{CALM_RATIO}",
    "NEUTRAL": "위 어느 규칙에도 해당 없음",
    "INSUFFICIENT_DATA": "GPR·GDELT 계열 모두 관측 부족",
    "driver": f"위협/실제 비가 {DRIVER_MARGIN} 배 초과면 THREAT_LED, "
              f"역이면 ACT_LED, 그 사이는 BALANCED",
    "배율정의": "테마 보도 점유율의 최근값 / 창 중앙값",
    "GPR지연": f"게시 지연 {GPR_STALE_MAX_DAYS}일 초과면 GPR 을 판정에서 제외",
}

_NON_METRIC = ("risk_label", "driver", "geo_rules", "latest_gpr_date",
               "gpr_excluded_reason", "label_reason")


class GeoNote(BaseModel):
    risk_label: Literal["CALM", "NEUTRAL", "ELEVATED", "SHOCK", "INSUFFICIENT_DATA"]
    driver: Literal["THREAT_LED", "ACT_LED", "BALANCED", "UNKNOWN"]
    summary: str = Field(max_length=700, description="한국어 2~4문장 지정학 국면 서술")
    transmission: list[str] = Field(
        default_factory=list,
        description="한국 시장 전달 경로 가설 (유가·환율·수출 등). 단정 금지")
    used_metrics: list[str] = Field(description="서술이 실제 인용한 readout 키만")
    cautions: list[str] = Field(default_factory=list)

    @field_validator("risk_label", "driver", mode="before")
    @classmethod
    def _norm(cls, v):
        return str(v).strip().upper()

    @field_validator("used_metrics", "cautions", "transmission", mode="before")
    @classmethod
    def _coerce_str_list(cls, v):
        return [str(x).strip() for x in v] if isinstance(v, list) else v


# ---------------------------------------------------------------------------
# 1. compute - 결정론 계산 (순수 함수, LLM·네트워크 무관)
# ---------------------------------------------------------------------------

def _r2(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 2)


def _to_date(v) -> Optional[date]:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def group_series(rows: list[dict]) -> dict[str, list[tuple[date, float]]]:
    """API 행 -> {code: [(period, value)]} 오름차순. 잡값은 버린다."""
    out: dict[str, list[tuple[date, float]]] = {}
    for r in rows or []:
        code = str(r.get("code") or "").strip()
        d = _to_date(r.get("period"))
        try:
            v = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        if not code or d is None:
            continue
        out.setdefault(code, []).append((d, v))
    for code in out:
        out[code].sort(key=lambda t: t[0])
    return out


def percentile_of(value: float, pool: list[float]) -> Optional[float]:
    """pool 안에서 value 의 백분위(이하 비율 %). pool 이 얇으면 None."""
    if len(pool) < MIN_POINTS:
        return None
    n_le = sum(1 for v in pool if v <= value)
    return round(100.0 * n_le / len(pool), 1)


def shock_ratio(pool: list[float], latest: float) -> Optional[float]:
    """최근값 / 중앙값. 중앙값이 0 이면 배율이 정의되지 않는다(None)."""
    if len(pool) < MIN_POINTS:
        return None
    m = median(pool)
    if m <= 0:
        return None
    return round(latest / m, 2)


def compute_geo_readout(rows: list[dict], *, as_of: date) -> dict:
    """API 행 -> 결정론 readout + risk_label + driver.

    GPR 이 오래됐으면(GPR_STALE_MAX_DAYS 초과) 라벨 판정에서 빼고 사유를
    남긴다 - 낡은 값을 현재 국면으로 쓰는 것이 가장 위험하다.
    """
    series = group_series(rows)
    out: dict = {"window_days": WINDOW_DAYS, "geo_rules": GEO_RULES}

    # --- GPR ---
    gprd = series.get("GPRD") or []
    latest_gpr_date = gprd[-1][0] if gprd else None
    lag = (as_of - latest_gpr_date).days if latest_gpr_date else None
    out["latest_gpr_date"] = latest_gpr_date.isoformat() if latest_gpr_date else None
    out["gpr_lag_days"] = lag
    out["gpr_points"] = len(gprd)

    gpr_usable = bool(gprd) and lag is not None and lag <= GPR_STALE_MAX_DAYS
    out["gpr_excluded_reason"] = None
    if gprd and not gpr_usable:
        out["gpr_excluded_reason"] = (
            f"GPR 최신 관측이 {lag}일 전 - 판정 기준 {GPR_STALE_MAX_DAYS}일 초과라 "
            f"라벨 판정에서 제외(수치는 참고로만 남긴다)")

    if gprd:
        vals = [v for _, v in gprd]
        out["gpr_latest"] = _r2(vals[-1])
        out["gpr_percentile"] = percentile_of(vals[-1], vals)
        out["gpr_5d_avg"] = _r2(sum(vals[-5:]) / len(vals[-5:]))
        out["gpr_window_median"] = _r2(median(vals)) if len(vals) >= MIN_POINTS else None
    else:
        out["gpr_latest"] = out["gpr_percentile"] = None
        out["gpr_5d_avg"] = out["gpr_window_median"] = None

    threat = series.get("GPRD_THREAT") or []
    act = series.get("GPRD_ACT") or []
    out["gpr_threat_latest"] = _r2(threat[-1][1]) if threat else None
    out["gpr_act_latest"] = _r2(act[-1][1]) if act else None
    ratio = None
    if threat and act and act[-1][1] > 0:
        ratio = round(threat[-1][1] / act[-1][1], 2)
    out["threat_act_ratio"] = ratio

    if ratio is None or not gpr_usable:
        driver = "UNKNOWN"
    elif ratio > DRIVER_MARGIN:
        driver = "THREAT_LED"
    elif ratio < 1.0 / DRIVER_MARGIN:
        driver = "ACT_LED"
    else:
        driver = "BALANCED"

    # --- GDELT 테마 ---
    themes: dict[str, dict] = {}
    for code, pts in sorted(series.items()):
        if not code.startswith("GDELT_") or code.endswith("_TONE"):
            continue
        vals = [v for _, v in pts]
        tone = series.get(f"{code}_TONE") or []
        themes[code] = {
            "latest": round(vals[-1], 4),
            "median": round(median(vals), 4) if len(vals) >= MIN_POINTS else None,
            "shock_ratio": shock_ratio(vals, vals[-1]),
            "tone_latest": _r2(tone[-1][1]) if tone else None,
            "points": len(vals),
            "latest_date": pts[-1][0].isoformat(),
        }
    out["themes"] = themes
    ratios = [t["shock_ratio"] for t in themes.values() if t["shock_ratio"] is not None]
    out["max_theme_ratio"] = max(ratios) if ratios else None
    out["hot_themes"] = sorted(
        (c for c, t in themes.items()
         if t["shock_ratio"] is not None and t["shock_ratio"] >= ELEVATED_RATIO),
        key=lambda c: -themes[c]["shock_ratio"])

    # --- 라벨 ---
    pctl = out["gpr_percentile"] if gpr_usable else None
    mx = out["max_theme_ratio"]
    if pctl is None and mx is None:
        label = "INSUFFICIENT_DATA"
    elif (pctl is not None and pctl >= SHOCK_PCTL) or (mx is not None and mx >= SHOCK_RATIO):
        label = "SHOCK"
    elif (pctl is not None and pctl >= ELEVATED_PCTL) or \
         (mx is not None and mx >= ELEVATED_RATIO):
        label = "ELEVATED"
    elif (pctl is None or pctl <= CALM_PCTL) and (mx is None or mx < CALM_RATIO):
        label = "CALM"
    else:
        label = "NEUTRAL"
    out["risk_label"] = label
    out["driver"] = driver if label != "INSUFFICIENT_DATA" else "UNKNOWN"

    # 라벨의 **근거**도 코드가 만든다. 실측 2026-08-01: 라벨만 주면 EXAONE 이
    # 근거를 지어내 GPR 에 귀속시켰다("183.64 백분위가 95 이상이라 SHOCK") -
    # 실제로는 백분위 43 이고 SHOCK 은 북한 미사일 테마 배율 3.5 에서 나왔다.
    # 모델과 싸우는 대신 베낄 문장을 준다.
    reasons: list[str] = []
    if label in ("SHOCK", "ELEVATED"):
        trg = SHOCK_PCTL if label == "SHOCK" else ELEVATED_PCTL
        rat = SHOCK_RATIO if label == "SHOCK" else ELEVATED_RATIO
        if pctl is not None and pctl >= trg:
            reasons.append(f"GPR 백분위 {pctl} >= {trg}")
        if mx is not None and mx >= rat:
            hot = ", ".join(out["hot_themes"]) or "테마"
            reasons.append(f"테마 배율 {mx} >= {rat} ({hot})")
    elif label == "CALM":
        reasons.append(f"GPR 백분위 {pctl} <= {CALM_PCTL}" if pctl is not None
                       else "GPR 판정 제외")
        reasons.append(f"모든 테마 배율 < {CALM_RATIO}")
    elif label == "NEUTRAL":
        reasons.append(f"GPR 백분위 {pctl} 와 최대 테마 배율 {mx} 가 "
                       f"어느 임계도 넘지 않음")
    else:
        reasons.append("GPR·GDELT 관측 부족")
    if out["gpr_excluded_reason"]:
        reasons.append("GPR 은 지연으로 판정에서 제외됨")
    out["label_reason"] = "; ".join(reasons)
    return out


# ---------------------------------------------------------------------------
# 2. narrate - LLM 서술 (판정 아님)
# ---------------------------------------------------------------------------

# 실측 2026-08-01: 필드를 산문으로만 설명했더니 EXAONE 이 제 키를 만들어
# ('geopolitical_state' 등) 두 번 연속 Schema 를 어겼다. 총괄 노드(draft_packet)
# 에서 통한 방식 - **정확한 JSON 골격을 그대로 보여주고 키를 고정**한다.
_SYSTEM = (
    "You are the geopolitical analyst of a Korean equity research desk.\n"
    "Return ONLY this JSON object - these five keys exactly, no others, "
    "no renaming, no nesting:\n"
    '{"risk_label": "<copy verbatim>", "driver": "<copy verbatim>", '
    '"summary": "<2-4 sentences in Korean>", '
    '"transmission": ["<hypothesis>", "..."], '
    '"used_metrics": ["<readout key>", "..."], "cautions": ["..."]}\n'
    "RULES - non-negotiable:\n"
    "- risk_label and driver are ALREADY DECIDED by deterministic code. Copy "
    "them character for character. Never change them.\n"
    "- summary is a single Korean string (NOT a list, NOT an object) using "
    "ONLY numbers present in the readout. Never invent a number.\n"
    # 실측: 라벨 근거를 스스로 지어내 엉뚱한 지표에 귀속시켰다(백분위 43 인데
    # "95 이상이라 SHOCK"). 근거는 코드가 계산해 주므로 그대로 쓰게 한다.
    "- summary MUST state the label reason exactly as given. Never attribute "
    "the label to a metric that the given reason does not name, and never "
    "quote a threshold number that is not in the readout.\n"
    "- transmission, used_metrics, cautions are FLAT arrays of plain strings.\n"
    "- transmission holds at most 3 hypotheses for how this could reach the "
    "Korean market (oil import cost, USD/KRW, export demand, defense names). "
    "Hedge them - they are hypotheses, never facts - and never recommend "
    "buying or selling anything.\n"
    "- used_metrics lists only keys that actually appear in the readout.\n"
    # 실측 2026-08-01: gpr_excluded_reason 이 null 이면 EXAONE 이 "지연이 적용되지
    # 않아 최신 데이터"라고 썼다 - 실제로는 7일 지난 값이다. 제외되지 않았을 뿐
    # 지연은 늘 있다. 날짜와 지연일을 반드시 쓰게 해 "오늘 값" 오독을 막는다.
    "- summary MUST state the GPR observation date (latest_gpr_date) and how "
    "many days old it is (gpr_lag_days). The index ALWAYS lags - never call it "
    "current, latest, or real-time, and never say there is no lag. If "
    "gpr_excluded_reason is set, also say the index was excluded from the label."
)


def narrate(readout: dict, llm: Optional[Callable] = None) -> GeoNote:
    allowed = [k for k in readout if k not in _NON_METRIC]
    prompt = (f"Deterministic risk label (copy verbatim): {readout['risk_label']}\n"
              f"Deterministic driver (copy verbatim): {readout['driver']}\n"
              f"Why that label (code-computed - state THIS reason, do not "
              f"invent your own): {readout.get('label_reason')}\n"
              f"Allowed metric keys: {json.dumps(allowed)}\n"
              f"Geopolitical readout (code-computed, do not alter):\n"
              + json.dumps(readout, ensure_ascii=False, indent=1))
    call = llm or _ollama_call
    last_err = None
    for attempt in range(2):
        text = call(_SYSTEM, prompt if attempt == 0 else
                    prompt + f"\n\nYour previous output failed validation: {last_err}. "
                             f"Return ONLY valid JSON for the schema.")
        try:
            start, end = text.find("{"), text.rfind("}")
            return GeoNote.model_validate_json(text[start:end + 1])
        except (ValidationError, ValueError) as e:
            last_err = str(e)[:200]
    raise RuntimeError(f"LLM 서술이 Schema 를 두 번 어겼다: {last_err}")


def _ollama_call(system: str, user: str) -> str:
    req = urllib.request.Request(
        OLLAMA_BASE + "/v1/chat/completions", method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
        data=json.dumps({
            "model": MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }).encode(),
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
        out = json.loads(r.read())
    return out["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# 3. verify - 결정론 검증
# ---------------------------------------------------------------------------

_PCT_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*%")
_RATIO_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*배")
# 지정학 서술이 넘지 말아야 할 선 - 매매 지시는 RES-09 의 권한이 아니다
_ACTION_WORDS = ("매수", "매도", "비중 확대", "비중 축소", "손절", "진입", "청산")


def _number_pool(readout: dict) -> list[float]:
    pool = [float(v) for k, v in readout.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and k not in ("window_days", "gpr_points")]
    for t in (readout.get("themes") or {}).values():
        pool.extend(float(v) for v in t.values()
                    if isinstance(v, (int, float)) and not isinstance(v, bool))
    return pool


def verify(note: GeoNote, readout: dict) -> tuple[GeoNote, dict]:
    """(1) 환각 인용 키 제거, (2) %·배 수치 ±0.1 대조, (3) 라벨·driver 복원,
    (4) 매매 지시 어휘 적발. LLM 문장 자체는 고치지 않고 cautions 에 남긴다."""
    dropped: dict = {"hallucinated_metrics": [], "flagged_numbers": [],
                     "label_restored": None, "driver_restored": None,
                     "action_words": []}

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

    label = note.risk_label
    code_label = readout.get("risk_label")
    if code_label and label != code_label:
        reason = (f"LLM 이 risk_label 을 {label} 로 적었다 - 결정론 라벨 "
                  f"{code_label} 로 복원 (라벨 권한은 코드에 있다)")
        dropped["label_restored"] = reason
        cautions.append("[복원] " + reason)
        label = code_label

    driver = note.driver
    code_driver = readout.get("driver")
    if code_driver and driver != code_driver:
        reason = (f"LLM 이 driver 를 {driver} 로 적었다 - 결정론 값 "
                  f"{code_driver} 로 복원")
        dropped["driver_restored"] = reason
        cautions.append("[복원] " + reason)
        driver = code_driver

    blob = note.summary + " " + " ".join(note.transmission)
    for w in _ACTION_WORDS:
        if w in blob:
            dropped["action_words"].append(w)
    if dropped["action_words"]:
        cautions.append(
            f"[권한] 매매 지시 어휘 {dropped['action_words']} 감지 - 지정학 분석은 "
            f"주문을 지시하지 않는다(RES-09 금지). 해당 문장은 근거로만 취급")

    dropped["label_flags"] = audit_narrative(note.summary, readout)
    for lf in dropped["label_flags"]:
        if lf["check"] == "percentile_literal":
            cautions.append(f"[라벨검증] summary 의 {lf['value']} - {lf['reason']}")
        else:
            cautions.append(
                f"[라벨검증] summary 의 {lf['value']} 는 {lf['metric']} 수치인데 "
                f"같은 문장에 다른 지표 라벨 '{lf['forbidden_hit']}' - 오서술 의심")

    return (note.model_copy(update={"risk_label": label, "driver": driver,
                                    "used_metrics": kept, "cautions": cautions}),
            dropped)


# ---------------------------------------------------------------------------
# 4. analyze
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 25):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _theme_codes(path: Optional[Path] = None) -> list[str]:
    """수집기와 같은 워치리스트를 읽어 계열 코드를 만든다 - 목록이 두 곳에서
    갈라지지 않게 한 곳(config/geopolitical_themes.txt)만 본다."""
    p = path or (Path(__file__).resolve().parent.parent / "config"
                 / "geopolitical_themes.txt")
    codes: list[str] = []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return codes
    for line in text.splitlines():
        body = line.split("#", 1)[0].strip()
        if "|" not in body:
            continue
        code = body.split("|", 1)[0].strip()
        if code and code not in codes:
            codes.append(code)
            codes.append(f"{code}_TONE")
    return codes


def analyze(*, research_api: Optional[str] = None, llm: Optional[Callable] = None,
            get: Callable = _http_get) -> dict:
    base = (research_api or RESEARCH_API).rstrip("/")
    now = datetime.now(timezone.utc)
    out = {"agent": AGENT_VERSION, "as_of": now.isoformat(), "model": MODEL}

    codes = list(GPR_CODES) + _theme_codes()
    if not codes:
        return {**out, "verdict": "INSUFFICIENT_DATA", "readout": None,
                "reason": "조회할 계열 코드가 없다 (config/geopolitical_themes.txt 확인)"}
    url = (f"{base}/macro/observations?codes={urllib.parse.quote(','.join(codes))}"
           f"&days={WINDOW_DAYS}")
    try:
        rows = get(url)
    except Exception as e:  # noqa: BLE001 - 미가동을 통과로 위장하지 않는다
        return {**out, "verdict": "INSUFFICIENT_DATA", "readout": None,
                "reason": f"research-api /macro/observations 호출 실패: "
                          f"{type(e).__name__}: {e}"}

    readout = compute_geo_readout(rows, as_of=now.date())
    if readout["risk_label"] == "INSUFFICIENT_DATA":
        return {**out, "verdict": "INSUFFICIENT_DATA", "readout": readout,
                "reason": f"지정학 계열 관측 부족 (GPR {readout['gpr_points']}건, "
                          f"테마 {len(readout['themes'])}개)"}

    try:
        note = narrate(readout, llm=llm)
    except Exception as e:  # noqa: BLE001
        return {**out, "verdict": readout["risk_label"], "driver": readout["driver"],
                "llm_status": "LLM_UNAVAILABLE", "readout": readout,
                "reason": f"{type(e).__name__}: {str(e)[:300]}"}

    note, dropped = verify(note, readout)
    return {**out, "verdict": note.risk_label, "driver": note.driver,
            "llm_status": "OK", "readout": readout, "summary": note.summary,
            "transmission": note.transmission, "used_metrics": note.used_metrics,
            "cautions": note.cautions, "dropped": dropped}


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음
# ---------------------------------------------------------------------------

def _rows(code: str, values: list[float], *, end: date) -> list[dict]:
    n = len(values)
    return [{"code": code, "period": (end - timedelta(days=n - 1 - i)).isoformat(),
             "value": v} for i, v in enumerate(values)]


def _check_grouping_and_stats():
    today = date(2026, 8, 1)
    rows = _rows("GPRD", [100.0] * 30, end=today)
    rows += [{"code": "GPRD", "period": "쓰레기", "value": 1},
             {"code": "GPRD", "period": "2026-07-01", "value": "숫자아님"},
             {"code": "", "period": "2026-07-01", "value": 1}]
    g = group_series(rows)
    assert len(g["GPRD"]) == 30, g["GPRD"][:3]
    assert g["GPRD"][0][0] < g["GPRD"][-1][0], "오름차순 정렬 안 됨"
    # 백분위·배율은 관측이 얇으면 None (지어내지 않는다)
    assert percentile_of(5.0, [1.0] * (MIN_POINTS - 1)) is None
    assert percentile_of(100.0, [100.0] * MIN_POINTS) == 100.0
    assert shock_ratio([0.0] * MIN_POINTS, 1.0) is None, "중앙값 0 배율은 정의 불가"
    assert shock_ratio([2.0] * MIN_POINTS, 5.0) == 2.5
    print("  그룹핑·통계 방어         OK")


def _check_labels_and_driver():
    today = date(2026, 8, 1)
    # 평온: GPR 최근값이 창 하위 + 테마 조용
    calm = _rows("GPRD", [100.0] * 29 + [50.0], end=today - timedelta(days=1))
    calm += _rows("GPRD_THREAT", [100.0] * 30, end=today - timedelta(days=1))
    calm += _rows("GPRD_ACT", [100.0] * 30, end=today - timedelta(days=1))
    calm += _rows("GDELT_X", [1.0] * 30, end=today - timedelta(days=1))
    r = compute_geo_readout(calm, as_of=today)
    assert r["risk_label"] == "CALM", r["risk_label"]
    assert r["driver"] == "BALANCED", r["driver"]

    # 충격: 테마만 3배. GPR 은 최신값이 창 하위(백분위 3.3)라 발동하지 않는다
    # - 평탄한 계열([100]*30)로 두면 최신값 백분위가 100 이 되어 GPR 조건까지
    #   같이 켜진다(실측으로 확인). 근거 문구 검사는 한쪽만 켜야 의미가 있다.
    shock = _rows("GPRD", [100.0] * 29 + [50.0], end=today - timedelta(days=1))
    shock += _rows("GDELT_X", [1.0] * 29 + [3.0], end=today - timedelta(days=1))
    r = compute_geo_readout(shock, as_of=today)
    assert r["risk_label"] == "SHOCK", r["risk_label"]
    assert r["max_theme_ratio"] == 3.0 and r["hot_themes"] == ["GDELT_X"]
    # 근거는 실제 발동 조건만 말해야 한다 - GPR 백분위는 SHOCK 을 만들지 않았다
    assert "테마 배율 3.0" in r["label_reason"], r["label_reason"]
    assert "백분위" not in r["label_reason"], \
        f"발동하지 않은 조건이 근거에 섞였다: {r['label_reason']}"
    # 둘 다 켜지면 둘 다 적는다 - 근거는 사실의 전부여야 한다
    both = _rows("GPRD", [100.0] * 30, end=today - timedelta(days=1))
    both += _rows("GDELT_X", [1.0] * 29 + [3.0], end=today - timedelta(days=1))
    rb = compute_geo_readout(both, as_of=today)
    assert "백분위" in rb["label_reason"] and "테마 배율" in rb["label_reason"], \
        rb["label_reason"]

    # 위협 주도 - "폭격한다니 만다니"가 실제 사건보다 큰 국면
    th = _rows("GPRD", [100.0] * 30, end=today - timedelta(days=1))
    th += _rows("GPRD_THREAT", [100.0] * 29 + [300.0], end=today - timedelta(days=1))
    th += _rows("GPRD_ACT", [100.0] * 29 + [100.0], end=today - timedelta(days=1))
    r = compute_geo_readout(th, as_of=today)
    assert r["driver"] == "THREAT_LED", r
    assert r["threat_act_ratio"] == 3.0

    # 실제 사건 주도
    ac = _rows("GPRD", [100.0] * 30, end=today - timedelta(days=1))
    ac += _rows("GPRD_THREAT", [100.0] * 30, end=today - timedelta(days=1))
    ac += _rows("GPRD_ACT", [100.0] * 29 + [400.0], end=today - timedelta(days=1))
    assert compute_geo_readout(ac, as_of=today)["driver"] == "ACT_LED"

    assert compute_geo_readout([], as_of=today)["risk_label"] == "INSUFFICIENT_DATA"
    print("  라벨·driver 판정         OK")


def _check_stale_gpr_excluded():
    """낡은 GPR 을 현재 국면으로 쓰지 않는다 - 가장 위험한 오독이다."""
    today = date(2026, 8, 1)
    old_end = today - timedelta(days=40)          # 40일 전이 최신 = 낡음
    rows = _rows("GPRD", [100.0] * 29 + [999.0], end=old_end)   # 백분위 100
    rows += _rows("GDELT_X", [1.0] * 30, end=today - timedelta(days=1))
    r = compute_geo_readout(rows, as_of=today)
    assert r["gpr_lag_days"] == 40
    assert r["gpr_excluded_reason"] is not None
    assert r["gpr_percentile"] == 100.0, "수치는 참고로 남긴다"
    assert r["risk_label"] == "CALM", f"낡은 GPR 이 라벨을 끌고 갔다: {r['risk_label']}"
    assert r["driver"] == "UNKNOWN"
    print("  낡은 GPR 판정 제외       OK")


def _check_verify_restores_and_flags():
    readout = {"risk_label": "ELEVATED", "driver": "THREAT_LED",
               "gpr_latest": 195.9, "gpr_percentile": 82.0,
               "threat_act_ratio": 1.4, "window_days": 120, "gpr_points": 116,
               "themes": {"GDELT_NK": {"shock_ratio": 1.9, "latest": 0.08}},
               "geo_rules": GEO_RULES}
    note = GeoNote(risk_label="CALM", driver="ACT_LED",
                   summary="지정학 지수는 82.0% 수준이고 위협/실제 비는 9.9배다. "
                           "지금은 매수 기회로 보인다.",
                   transmission=["유가 상승 시 수입 부담이 커질 수 있다"],
                   used_metrics=["gpr_percentile", "없는키", "risk_label"],
                   cautions=[])
    got, dropped = verify(note, readout)
    assert got.risk_label == "ELEVATED", "라벨 복원 실패"
    assert got.driver == "THREAT_LED", "driver 복원 실패"
    assert dropped["hallucinated_metrics"] == ["없는키", "risk_label"], dropped
    assert got.used_metrics == ["gpr_percentile"]
    assert "9.9배" in dropped["flagged_numbers"], dropped["flagged_numbers"]
    assert "매수" in dropped["action_words"], "매매 지시 어휘를 놓쳤다"
    assert any("[권한]" in c for c in got.cautions)
    print("  검증(복원·숫자·권한)     OK")


def _check_narrate_roundtrip():
    readout = compute_geo_readout(
        _rows("GPRD", [100.0] * 30, end=date(2026, 7, 31))
        + _rows("GDELT_X", [1.0] * 29 + [3.0], end=date(2026, 7, 31)),
        as_of=date(2026, 8, 1))

    def fake_llm(system, user):
        assert "copy verbatim" in system.lower() or "Copy them verbatim" in system
        assert readout["risk_label"] in user
        return ('그럴듯한 프리앰블 {"risk_label": "SHOCK", "driver": "UNKNOWN", '
                '"summary": "테마 보도량이 평소의 3.0배로 뛰었다.", '
                '"transmission": ["유가 경로 가설"], '
                '"used_metrics": ["max_theme_ratio"], "cautions": []} 꼬리')

    note = narrate(readout, llm=fake_llm)
    note, dropped = verify(note, readout)
    assert note.risk_label == "SHOCK"
    assert not dropped["flagged_numbers"], dropped["flagged_numbers"]
    assert not dropped["action_words"]

    calls = {"n": 0}

    def bad_llm(system, user):
        calls["n"] += 1
        return "{}" if calls["n"] == 1 else (
            '{"risk_label": "SHOCK", "driver": "UNKNOWN", "summary": "s", '
            '"transmission": [], "used_metrics": [], "cautions": []}')

    assert narrate(readout, llm=bad_llm).risk_label == "SHOCK"
    assert calls["n"] == 2, "Schema 위반 재시도가 없었다"

    def always_bad(system, user):
        return "{}"
    try:
        narrate(readout, llm=always_bad)
        raise AssertionError("두 번 어겨도 통과했다 - 서술을 지어내면 안 된다")
    except RuntimeError:
        pass
    print("  narrate 왕복·재시도      OK")


def _check_analyze_api_down():
    def boom(url, timeout=25):
        raise ConnectionError("연결 거부")
    r = analyze(get=boom, llm=lambda s, u: "{}")
    assert r["verdict"] == "INSUFFICIENT_DATA"
    assert "호출 실패" in r["reason"], r["reason"]

    rows = (_rows("GPRD", [100.0] * 30, end=date.today() - timedelta(days=1))
            + _rows("GDELT_X", [1.0] * 29 + [3.0],
                    end=date.today() - timedelta(days=1)))

    def ok_get(url, timeout=25):
        assert "/macro/observations" in url and "codes=" in url
        return rows

    def dead_llm(system, user):
        raise TimeoutError("Ollama 없음")
    r = analyze(get=ok_get, llm=dead_llm)
    assert r["verdict"] == "SHOCK", r["verdict"]        # 라벨은 코드 것이라 남는다
    assert r["llm_status"] == "LLM_UNAVAILABLE"
    print("  analyze 실패 경로        OK")


def _check_theme_codes_from_config():
    codes = _theme_codes()
    assert codes, "config/geopolitical_themes.txt 를 못 읽었다"
    assert "GDELT_NK_MISSILE" in codes and "GDELT_NK_MISSILE_TONE" in codes, codes[:6]
    assert len(codes) == len(set(codes)), "코드 중복"
    print("  테마 코드 단일 출처      OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        print(json.dumps(analyze(), ensure_ascii=False, indent=2))
        raise SystemExit(0)

    print(f"{AGENT_VERSION} 자체 점검 (네트워크 없음)")
    _check_grouping_and_stats()
    _check_labels_and_driver()
    _check_stale_gpr_excluded()
    _check_verify_restores_and_flags()
    _check_narrate_roundtrip()
    _check_analyze_api_down()
    _check_theme_codes_from_config()
    print("지정학 분석가 7개 영역 통과. 실측은 --run")
