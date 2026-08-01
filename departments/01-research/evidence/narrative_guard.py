#!/usr/bin/env python3
"""의미 오서술 가드 v1 (narrative-guard) - 라벨-수치 결합의 결정론 검사.

담당: 재일 (리서치/퀀트)
근거: 리서치본부 실측 사고 3건 - 수치는 readout 그대로인데 **라벨을 바꿔
      부르는** 서술이 기존 수치 재대조(±0.1)를 전부 통과했다:
  사례 1. Packet 이 range_position_20d_pct(20일 레인지 내 위치) 35.9 를
          "20일 이동평균선 기준 35.9% 상승"으로 서술 - 지표의 의미가 바뀌었다.
  사례 2. 레짐 분석가가 above_sma20_pct 5일 평균 22.11 을 "5일 평균 A/D
          비율"이라 서술 - SMA20 상회 비율이 등락 비율로 둔갑했다.
  사례 3. EXAONE 이 spread_bp_p50/p90 의 키 이름 속 50/90(백분위 표기)을
          %수치처럼 서술 - readout 어디에도 없는 50%/90% 가 문장에 등장했다.

왜 별도 가드인가 - 수치 재대조가 못 잡는 마지막 구멍:
  기존 verify 의 숫자 대조는 "summary 의 수치가 readout 에 존재하는가"만
  본다. 위 사례들은 수치 자체가 readout 에 있으므로(35.9, 22.11) 전부
  통과한다. 오서술은 수치가 아니라 **수치 옆에 붙은 라벨**에서 생긴다.
  그래서 이 가드는 라벨 어구와 수치의 결합(같은 문장)을 결정론으로 검사한다.
  LLM 없음 - LLM 의 오서술을 LLM 으로 재검하면 같은 구멍이 또 생긴다.

판정 규칙 (v1 - 오탐 위험을 낮게 시작, 표시만):
  1. summary 의 수치마다 readout 에서 ±tolerance 로 매칭되는 키를 전부 찾는다
     (부호 반전 서술 허용 - 기존 숫자검증과 같은 관례).
  2. **매칭 키 전부**가 "같은 문장에 그 키의 forbidden 어구가 있고, allowed
     어구가 forbidden 보다 수치에 더 가깝지도 않다"일 때만 위반이다.
     매칭 키 중 하나라도 무죄 설명이 가능하면(사전에 없는 키, forbidden
     미출현, allowed 가 더 근접) 위반이 아니다 - 과잉 차단 방지.
  3. allowed 는 참고 필드다 - allowed 가 없다고 위반이 아니다. 단, 한 문장에
     두 지표를 같이 인용하는 정상 서술("상회 60% ... A/D 5.0배")을 오탐하지
     않기 위해 allowed 가 forbidden 보다 수치에 근접하면 정상 인용으로 본다.
  4. spread_bp_p50/p90 은 forbidden 어구 대신 백분위 리터럴 규칙: readout 에
     그 키가 있는데 summary 의 "50%"/"90%" 가 readout 어느 수치와도(±0.1)
     안 맞으면 키 이름의 p50/p90 을 %로 읽은 것이다(사례 3).
  판정 강등 없음 - 배선부(technical/sector_regime verify)는 cautions 표시와
  dropped.label_flags 카운트만 남긴다. 강등은 오탐률 실측 후 v2 에서 검토.

한계 (v1):
  - 사전(METRIC_LEXICON)에 있는 키만 라벨 검사한다 - 커버리지는 실측 사례
    중심이고, 사전 밖 키가 수치에 같이 매칭되면 보수적으로 통과시킨다.
  - 문장 분리는 마침표·물음표 단순 규칙이라 쉼표 run-on 문장에서는 다른
    절의 라벨이 같은 문장으로 잡힐 수 있다(근접 규칙이 대부분 걸러낸다).
  - readout 최상위 수치만 매칭 풀로 쓴다(중첩 dict 제외).

실행: python evidence/narrative_guard.py   # 자체 점검 (네트워크·LLM 없음)
"""
from __future__ import annotations

import re
import sys
from typing import Optional

GUARD_VERSION = "narrative-guard-v1"

# readout 키 -> 그 키의 수치 옆에 와야 자연스러운 라벨(allowed) / 다른 지표의
# 라벨이라 이 키의 수치 옆에 나오면 오서술인 어구(forbidden).
# 커버리지는 실측 3사례 + 명백한 혼동쌍만 - 넓히는 건 오탐 실측 후에.
METRIC_LEXICON: dict[str, dict[str, list[str]]] = {
    # RES-04 technical readout: 레인지 위치 vs 이동평균 괴리 (사례 1의 혼동쌍)
    "range_position_20d_pct": {
        "allowed": ["레인지", "범위 내 위치"],
        "forbidden": ["이동평균", "이동 평균"],
    },
    "price_vs_sma20_pct": {
        "allowed": ["이동평균", "이동 평균", "SMA"],
        "forbidden": ["레인지"],
    },
    "price_vs_sma60_pct": {
        "allowed": ["이동평균", "이동 평균", "SMA"],
        "forbidden": ["레인지"],
    },
    # RES-07 regime readout: A/D 비율 vs SMA20 상회 비율 (사례 2의 혼동쌍)
    "ad_ratio": {
        "allowed": ["A/D", "등락 비", "상승/하락 비"],
        # v2(2026-08-01): 실측 미탐 - ad_ratio 5.80 을 "상승 종목 비율"이라 서술
        "forbidden": ["상회", "상승 종목 비율"],
    },
    "ad_ratio_5d_avg": {
        "allowed": ["A/D", "등락 비", "상승/하락 비"],
        "forbidden": ["상회", "상승 종목 비율"],
    },
    "ad_ratio_20d_avg": {
        "allowed": ["A/D", "등락 비", "상승/하락 비"],
        "forbidden": ["상회"],
    },
    "ad_ratio_prev5_avg": {
        "allowed": ["A/D", "등락 비", "상승/하락 비"],
        "forbidden": ["상회"],
    },
    "above_sma20_pct": {
        "allowed": ["상회", "SMA20 위"],
        "forbidden": ["A/D", "등락 비", "상승 종목 비율"],
    },
    "above_sma20_pct_5d_avg": {
        "allowed": ["상회", "SMA20 위"],
        "forbidden": ["A/D", "등락 비", "상승 종목 비율"],
    },
    "above_sma20_pct_20d_avg": {
        "allowed": ["상회", "SMA20 위"],
        "forbidden": ["A/D", "등락 비", "상승 종목 비율"],
    },
    # RES-06 microstructure readout: forbidden 없음 - bp 지표의 오서술은 어구가
    # 아니라 키 이름의 p50/p90 리터럴(사례 3) - check_percentile_literal 담당
    "spread_bp_p50": {"allowed": ["스프레드", "bp"], "forbidden": []},
    "spread_bp_p90": {"allowed": ["스프레드", "bp"], "forbidden": []},
}

# 수치 매칭 풀에서 빼는 bookkeeping 키 - 기존 verify 의 숫자검증과 같은 관례
# (봉/행 개수는 인용 수치가 아니다)
_NOT_QUOTABLE = ("bars_used", "days_used")

# 영문자·숫자·밑줄·소수점 뒤에서 시작하는 숫자는 식별자 일부다(sma20, p50)
_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])[-+]?\d+(?:\.\d+)?")

# 숫자 바로 뒤에 붙으면 "지표 수치 인용"이 아니라 개수·기간 표현인 접미사
# (예: "5일 평균", "20일", "70봉") - 이런 숫자를 매칭하면 오탐만 는다
_COUNTER_SUFFIXES = ("일", "개월", "개", "봉", "번", "회", "년", "월", "주",
                     "건", "행", "종목", "명")

# 백분위 리터럴: 정확히 50%/90% (150% 나 p50 은 제외)
_PCTL_RES = {
    "spread_bp_p50": (50.0, re.compile(r"(?<![A-Za-z0-9_.])50(?:\.0+)?\s*%")),
    "spread_bp_p90": (90.0, re.compile(r"(?<![A-Za-z0-9_.])90(?:\.0+)?\s*%")),
}


def _split_sentences(text: str) -> list[str]:
    # 소수점(35.9)의 '.' 은 문장 경계가 아니다 - 뒤에 숫자가 오면 자르지 않는다
    return [s.strip() for s in re.split(r"[.?](?!\d)", text or "") if s.strip()]


def _metric_values(readout: dict) -> dict[str, float]:
    return {k: float(v) for k, v in (readout or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and k not in _NOT_QUOTABLE}


def _nearest_phrase(sent: str, span: tuple[int, int],
                    phrases: list[str]) -> tuple[float, Optional[str]]:
    """수치 span 과 가장 가까운 어구 출현의 (간격, 어구). 없으면 (inf, None).
    casefold 로 SMA/sma 표기 차이를 흡수한다."""
    low = sent.casefold()
    best: tuple[float, Optional[str]] = (float("inf"), None)
    for p in phrases:
        pl = p.casefold()
        start = 0
        while True:
            i = low.find(pl, start)
            if i < 0:
                break
            j = i + len(pl)
            if j <= span[0]:
                d = span[0] - j
            elif i >= span[1]:
                d = i - span[1]
            else:
                d = 0
            if d < best[0]:
                best = (float(d), p)
            start = i + 1
    return best


def check_label_consistency(summary: str, readout: dict,
                            tolerance: float = 0.1) -> list[dict]:
    """summary 수치마다 readout 매칭 키를 찾아 라벨-수치 결합을 검사한다.

    위반 dict: {"check","value","metric","forbidden_hit","allowed_present",
    "sentence"}. allowed_present 는 참고 필드다(없다고 위반 아님)."""
    violations: list[dict] = []
    values = _metric_values(readout)
    if not values or not summary:
        return violations

    for sent in _split_sentences(summary):
        for m in _NUM_RE.finditer(sent):
            tail = sent[m.end():m.end() + 2]
            if any(tail.startswith(s) for s in _COUNTER_SUFFIXES):
                continue
            x = float(m.group(0))
            # 부호 반전 서술 허용은 기존 숫자검증(±0.1)과 같은 관례
            matched = [k for k, v in values.items()
                       if abs(x - v) <= tolerance or abs(x + v) <= tolerance]
            if not matched:
                continue

            # 매칭 키 전부가 유죄일 때만 위반 - 하나라도 무죄 설명이 가능하면
            # (사전 밖 키, forbidden 미출현, allowed 가 더 근접) 통과시킨다
            guilty: list[tuple[str, str, bool]] = []
            innocent = False
            for k in matched:
                lex = METRIC_LEXICON.get(k)
                if not lex or not lex["forbidden"]:
                    innocent = True
                    break
                f_dist, f_hit = _nearest_phrase(sent, m.span(), lex["forbidden"])
                if f_hit is None:
                    innocent = True
                    break
                a_dist, a_hit = _nearest_phrase(sent, m.span(), lex["allowed"])
                if a_hit is not None and a_dist <= f_dist:
                    # 한 문장에 두 지표를 같이 인용하는 정상 서술 보호:
                    # 제 라벨이 더 가까이 붙어 있으면 올바른 인용으로 본다
                    innocent = True
                    break
                guilty.append((k, f_hit, a_hit is not None))
            if innocent or not guilty:
                continue

            unit = "%" if tail.startswith("%") else ("배" if tail.startswith("배") else "")
            for k, f_hit, a_present in guilty:
                violations.append({
                    "check": "label_consistency",
                    "value": m.group(0) + unit,
                    "metric": k,
                    "forbidden_hit": f_hit,
                    "allowed_present": a_present,
                    "sentence": sent,
                })
    return violations


def check_percentile_literal(summary: str, readout: dict,
                             tolerance: float = 0.1) -> list[dict]:
    """spread_bp_p50/p90 키가 있을 때 summary 의 50%/90% 리터럴을 검사한다.

    그 리터럴이 readout 어느 수치와도(±tolerance) 안 맞으면 키 이름의
    백분위 표기를 %수치로 읽은 오서술이다(실측 사례 3)."""
    violations: list[dict] = []
    if not summary or not readout:
        return violations
    pool = list(_metric_values(readout).values())
    for key, (lit, rx) in _PCTL_RES.items():
        if key not in readout:
            continue
        m = rx.search(summary)
        if m is None:
            continue
        if any(abs(lit - v) <= tolerance for v in pool):
            continue  # readout 에 실제 50/90 수치가 있다 - 정당 인용 가능
        violations.append({
            "check": "percentile_literal",
            "value": m.group(0),
            "metric": key,
            "forbidden_hit": None,
            "reason": (f"readout 어느 수치와도 안 맞는 {lit:g}% - {key} 키 이름의 "
                       f"백분위 표기 p{lit:g} 를 %수치로 읽었을 가능성"),
        })
    return violations


def audit_narrative(summary: str, readout: dict,
                    tolerance: float = 0.1) -> list[dict]:
    """라벨-수치 결합 검사 + 백분위 리터럴 검사를 합친 위반 목록."""
    return (check_label_consistency(summary, readout, tolerance)
            + check_percentile_literal(summary, readout, tolerance))


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·LLM 없음. 실측 사례 3건을 그대로 재현한다
# ---------------------------------------------------------------------------

def _check_case1_range_as_sma():
    """실측 사례 1: 레인지 내 위치 35.9 를 이동평균 대비로 서술 - 위반."""
    readout = {"range_position_20d_pct": 35.9, "price_vs_sma20_pct": 4.2,
               "momentum_20d_pct": 12.3, "bars_used": 70}
    v = audit_narrative("주가는 20일 이동평균선 기준 35.9% 상승했다.", readout)
    assert len(v) == 1, v
    assert v[0]["metric"] == "range_position_20d_pct" and v[0]["value"] == "35.9%"
    assert v[0]["forbidden_hit"] == "이동평균" and v[0]["allowed_present"] is False
    # 띄어쓴 "이동 평균" 표기도 같은 오서술이다
    v2 = audit_narrative("20일 이동 평균 대비 35.9% 위다.", readout)
    assert len(v2) == 1 and v2[0]["forbidden_hit"] == "이동 평균", v2
    print("  사례1 레인지->이동평균 오서술 위반   OK")


def _check_case2_above_as_ad():
    """실측 사례 2: SMA20 상회 5일 평균 22.11 을 A/D 비율로 서술 - 위반."""
    readout = {"above_sma20_pct_5d_avg": 22.11, "ad_ratio_5d_avg": 0.8,
               "above_sma20_pct": 25.4, "days_used": 25}
    v = audit_narrative("5일 평균 A/D 비율은 22.11 로 하락 우위다.", readout)
    assert len(v) == 1, v
    assert v[0]["metric"] == "above_sma20_pct_5d_avg" and v[0]["forbidden_hit"] == "A/D"
    # "5일"의 5는 기간 표현(접미사 '일')이라 수치 매칭 대상이 아니다
    assert all(x["value"] != "5" for x in v)
    print("  사례2 상회평균->A/D 오서술 위반      OK")


def _check_case3_percentile_literal():
    """실측 사례 3: spread_bp_p50/p90 의 50/90 을 %수치처럼 서술 - 위반."""
    readout = {"spread_bp_p50": 8.5, "spread_bp_p90": 15.2, "quotes": 3000}
    v = audit_narrative("호가 스프레드의 50% 는 8.5bp 수준이다.", readout)
    assert len(v) == 1, v
    assert v[0]["check"] == "percentile_literal" and v[0]["metric"] == "spread_bp_p50"
    v90 = audit_narrative("스프레드 상위 90% 구간이 넓다.", readout)
    assert len(v90) == 1 and v90[0]["metric"] == "spread_bp_p90", v90
    # "p50" 표기 그대로나 실제 수치 인용(8.5bp)은 위반이 아니다
    assert audit_narrative("스프레드 p50 은 8.5bp, p90 은 15.2bp 다.", readout) == []
    # readout 에 진짜 50 에 해당하는 수치가 있으면 리터럴도 정당 인용이다
    ok = {"spread_bp_p50": 50.0, "spread_bp_p90": 90.0}
    assert audit_narrative("스프레드 중앙값 50% ... 아니 50.0bp 다.", ok) == []
    print("  사례3 p50/p90 백분위 리터럴 위반     OK")


def _check_normal_narration_passes():
    """정상 서술 통과: 라벨이 제 지표에 붙어 있으면 한 문장에 두 지표가
    섞여도(allowed 근접 규칙) 위반이 아니다 - 과잉 차단 방지."""
    readout = {"range_position_20d_pct": 35.9, "price_vs_sma20_pct": 4.2,
               "bars_used": 70}
    assert audit_narrative(
        "sma20 대비 +4.2% 이고 20일 레인지 내 위치 35.9% 다.", readout) == []
    # allowed 어구가 아예 없어도 forbidden 이 없으면 위반이 아니다
    assert audit_narrative("현재 위치는 35.9% 수준이다.", readout) == []
    # 사전 밖 키에도 같이 매칭되는 수치는 보수적으로 통과 (오탐 방지 우선 -
    # 어느 지표를 인용했는지 단정할 수 없다)
    amb = {"range_position_20d_pct": 35.9,
           "volatility_20d_annualized_pct": 35.85}
    assert audit_narrative("20일 이동평균선 기준 35.9% 상승했다.", amb) == []
    print("  정상 서술·모호 수치 통과             OK")


def _check_no_number_passes():
    """수치 없는 문장은 검사 대상이 없다 - 위반 0."""
    readout = {"range_position_20d_pct": 35.9, "ad_ratio": 0.8}
    assert audit_narrative("이동평균 아래에서 A/D 도 약세다.", readout) == []
    assert audit_narrative("", readout) == []
    assert audit_narrative("시장이 조용하다.", {}) == []
    print("  수치 없는 문장 통과                  OK")


def _check_same_sentence_rule():
    """같은 문장 판정: forbidden 어구가 다른 문장에 있으면 위반이 아니고,
    같은 문장으로 합치면 위반이다. 소수점은 문장 경계로 자르지 않는다."""
    readout = {"range_position_20d_pct": 35.9, "price_vs_sma20_pct": 4.2}
    apart = "20일 이동평균선이 꺾였다. 레인지 내 위치는 35.9% 다."
    assert audit_narrative(apart, readout) == []
    together = "20일 이동평균선이 꺾였고 위치는 35.9% 다."
    v = audit_narrative(together, readout)
    assert len(v) == 1 and v[0]["metric"] == "range_position_20d_pct", v
    # 문장 분리가 35.9 의 '.' 을 경계로 오인하면 수치가 쪼개져 못 잡는다
    assert v[0]["value"] == "35.9%", v
    print("  같은 문장 판정·소수점 분리 규칙      OK")


def _check_v2_observed_misses():
    """v2 사전 확장 - 실측 미탐 2건(상회·A/D 를 '상승 종목 비율'로) 재현 적발."""
    r = {"above_sma20_pct_5d_avg": 22.11, "ad_ratio": 5.8039}
    v = audit_narrative("직전 5일 평균 상승 종목 비율은 22.11%입니다.", r)
    assert any(x["metric"] == "above_sma20_pct_5d_avg" and
               x["forbidden_hit"] == "상승 종목 비율" for x in v), v
    v2 = audit_narrative("상승 종목 비율 5.80배를 기록했습니다.", r)
    assert any(x["metric"] == "ad_ratio" for x in v2), v2
    # 정당한 사용은 통과 - advancer_share 계열이 있으면 그 키에 매칭돼 구제
    ok = audit_narrative("SMA20 상회 비율은 22.11%입니다.", r)
    assert not ok, ok
    print("  v2 확장(실측 미탐 재현)  OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"{GUARD_VERSION} 자체 점검 (결정론, 네트워크·LLM 없음)")
    _check_case1_range_as_sma()
    _check_case2_above_as_ad()
    _check_case3_percentile_literal()
    _check_normal_narration_passes()
    _check_no_number_passes()
    _check_same_sentence_rule()
    _check_v2_observed_misses()
    print("narrative-guard 7개 영역 통과. 배선: technical/sector_regime verify()")
