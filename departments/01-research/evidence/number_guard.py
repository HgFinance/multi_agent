#!/usr/bin/env python3
"""서술 속 수치가 코드 계산과 맞는지 - 숫자 대조의 단일 출처.

소유: 재일 (리서치본부)
근거: 2026-08-02 중복 조사. 같은 검증이 분석가 4곳에 복붙됐는데 **정규식이
      서로 갈라져 있었다** - 그리고 그게 곧 검증 구멍이었다:

        RES-04 기술      %          만 검사
        RES-03 미시구조  % bp       검사
        RES-07 레짐      % 배       검사
        RES-09 지정학    % 배       검사
        RES-05 펀더멘털  %          검사(게다가 부호 반전 허용도 없다)
        RES-06 감성      검사 없음

      즉 기술 분석가가 "3.5배"를 지어내도 아무도 안 잡았다. 복붙은 처음엔
      같지만 갈라지고, 갈라진 자리가 정확히 뚫린 자리가 된다.

▶ 단위를 넓히는 것이 안전한 방향이다
  검사 단위를 합집합으로 통일한다. 어떤 분석가가 자기 풀에 없는 단위의 수치를
  쓰면 그건 잡혀야 하는 것이지 봐줄 것이 아니다. 좁히면 구멍, 넓히면 소음 -
  소음은 cautions 한 줄이고 구멍은 틀린 서술이 그대로 나가는 것이다.

▶ 부호 반전을 허용하는 이유
  코드가 -3.2% 를 냈는데 서술이 "3.2% 하락"이라고 쓰는 것은 정상이다.
  그래서 |x-v| 와 |x+v| 둘 다 본다.

실행: python evidence/number_guard.py     # 자체 점검(네트워크 없음)
"""
from __future__ import annotations

import re
import sys

MODULE_VERSION = "research-number-guard-v1"

TOLERANCE = 0.1        # ±0.1 - 반올림 자릿수 차이는 봐준다
# 검사 대상 단위. 우리 분석가들이 실제로 쓰는 표기의 합집합이다.
#   %      비율·증감률          bp   스프레드
#   배     A/D·체결강도 비율    p/포인트  지수 포인트
UNIT_PATTERN = r"(?:%|bp|배|포인트|p(?![a-zA-Z]))"
_NUM_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*" + UNIT_PATTERN, re.IGNORECASE)


def quoted_numbers(text: str) -> list[tuple[float, str]]:
    """서술에서 (값, 원문조각) 목록. 단위가 붙은 수치만 본다.

    단위 없는 맨 숫자는 검사하지 않는다 - 종목코드·건수·연도가 전부 걸려
    소음이 실제 위반을 덮는다.
    """
    return [(float(m.group(1)), m.group(0)) for m in _NUM_RE.finditer(text or "")]


# 지표가 아닌 bookkeeping - 인용 풀에 넣지 않는다. 카운트가 풀에 있으면
# LLM 이 그 숫자를 단위와 함께 지어내도 통과한다(numeric_pool 주석과 같은 이유).
_NOT_QUOTABLE = frozenset({
    "bars_used", "days_used", "n", "count", "limit", "window_days",
    "articles", "articles_used", "articles_dropped", "gpr_points",
    "lookback_used", "window_requested",
})


def keyed_pool(readout: dict, *, exclude: tuple[str, ...] = (),
               prefix: str = "") -> dict[str, float]:
    """readout -> {키: 수치}. numeric_pool 과 달리 **키를 잃지 않는다.**

    컨테이너 한 겹(fields/ratios 등)은 펼친다 - 지표 확장 후 재무·스타일 수치가
    거기 들어가는데, 최상위만 보면 풀의 절반을 놓친다.
    """
    out: dict[str, float] = {}
    for k, v in (readout or {}).items():
        if k in exclude or k in _NOT_QUOTABLE:
            continue
        key = f"{prefix}{k}"
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out[key] = float(v)
        elif isinstance(v, dict):
            for ik, iv in v.items():
                if ik in exclude or ik in _NOT_QUOTABLE:
                    continue
                val = iv.get("value") if isinstance(iv, dict) and "value" in iv else iv
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    out[f"{key}.{ik}" if k not in ("fields",) else f"{prefix}{ik}"] = \
                        float(val)
    return out


def trace_quoted(text: str, pool: dict[str, float], *,
                 tolerance: float = TOLERANCE,
                 allow_sign_flip: bool = True) -> list[dict]:
    """인용 수치마다 **어느 키에 맞았는지**를 남긴다.

    ▶ 왜 필요한가 (2026-08-03, 지표 확장이 드러낸 문제)
      지표를 10개에서 31개로 늘리자 확정치 풀이 넓어졌고, 그만큼 **우연히 맞는
      수치**가 생겼다. 실제로 자체점검 픽스처의 '창작 수치 99.9%' 가 단조 상승
      구간의 rsi=adx=100.0 에 허용오차로 걸려 통과했다.

      flag_unmatched 는 통과/불통과만 낸다. 그래서 **"통과가 검증인지 우연인지"
      를 구분할 수 없다.** 어느 키에 몇 개나 맞았는지를 남기면 그것이 보인다:
        matched_keys 가 1개  -> 그 지표를 인용한 것이다(검증)
        matched_keys 가 여럿 -> 어느 것을 뜻하는지 모른다(모호)
      모호 비율(ambiguity)이 높아지면 가드가 헐거워졌다는 뜻이다.

    **판정하지 않는다.** 통과 여부는 flag_unmatched 가 그대로 맡는다 - 여기서
    또 판정하면 두 곳이 갈라진다. 이 함수는 근거를 드러내기만 한다.
    """
    out: list[dict] = []
    for x, raw in quoted_numbers(text):
        hits = [k for k, v in (pool or {}).items()
                if abs(x - v) <= tolerance
                or (allow_sign_flip and abs(x + v) <= tolerance)]
        out.append({"raw": raw, "value": x, "matched_keys": sorted(hits),
                    "matched": bool(hits), "ambiguous": len(hits) > 1})
    return out


def quote_quality(traces: list[dict]) -> dict:
    """인용 품질 요약. **모호 비율을 숨기지 않는다.**"""
    n = len(traces or [])
    if not n:
        return {"quoted": 0, "matched": 0, "ambiguous": 0,
                "ambiguity_ratio": 0.0, "unmatched": []}
    matched = sum(1 for t in traces if t["matched"])
    amb = sum(1 for t in traces if t["ambiguous"])
    return {
        "quoted": n,
        "matched": matched,
        "ambiguous": amb,
        # 맞은 것 중 몇이 모호한가 - 이 값이 높으면 "통과" 가 우연일 수 있다
        "ambiguity_ratio": round(amb / matched, 3) if matched else 0.0,
        "unmatched": [t["raw"] for t in traces if not t["matched"]],
    }


def flag_unmatched(text: str, pool, *, tolerance: float = TOLERANCE,
                   allow_sign_flip: bool = True) -> list[str]:
    """pool 의 어떤 값과도 안 맞는 인용 수치의 원문 조각 목록.

    pool 이 비어 있으면 **모든 인용 수치가 걸린다.** 그것이 맞다 - 코드가
    아무 수치도 안 냈는데 서술에 수치가 있으면 그건 전부 지어낸 것이다.
    """
    vals = [float(v) for v in pool]
    out: list[str] = []
    for x, raw in quoted_numbers(text):
        ok = any(abs(x - v) <= tolerance or
                 (allow_sign_flip and abs(x + v) <= tolerance) for v in vals)
        if not ok:
            out.append(raw)
    return out


def numeric_pool(readout: dict, *, exclude: tuple[str, ...] = ()) -> list[float]:
    """readout 최상위의 인용 가능한 수치.

    exclude 에는 **개수·길이 같은 bookkeeping 키**를 넣는다(bars_used,
    days_used, ...). 카운트가 풀에 있으면 LLM 이 그 숫자를 단위와 함께
    지어내도 통과한다 - 검증이 헐거워지는 전형적인 경로다.
    """
    return [float(v) for k, v in (readout or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and k not in exclude]


def caution_lines(flags: list[str]) -> list[str]:
    """플래그 -> cautions 문장. 문구도 한 곳에서만 만든다."""
    return [f"[숫자검증] summary 의 {f} 는 readout 수치와 불일치"
            f"(±{TOLERANCE} 밖) - 해당 문장 신뢰 금지" for f in flags]


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음
# ---------------------------------------------------------------------------

def _check_units_are_union():
    """갈라져 있던 단위가 전부 잡히는가 - 이게 이 모듈의 존재 이유다."""
    got = {raw for _v, raw in quoted_numbers(
        "상회 27.4%, 스프레드 12bp, A/D 5.8배, 지수 1209.3포인트")}
    assert got == {"27.4%", "12bp", "5.8배", "1209.3포인트"}, got
    # 예전에는 기술 분석가가 '배'를 못 잡았다 - 이제 잡힌다
    assert flag_unmatched("A/D 가 3.5배다", [1.0, 2.0]) == ["3.5배"]
    # 단위 없는 맨 숫자는 검사 대상이 아니다(종목코드·건수·연도 소음 방지)
    assert quoted_numbers("종목 000660 을 2026년에 12건") == []
    print("  단위 합집합 검사         OK")


def _check_tolerance_and_sign():
    pool = [27.4286, -3.2]
    # 반올림 자릿수 차이는 봐준다 (27.4 - 27.4286 = 0.029)
    assert flag_unmatched("27.4%", pool) == []
    assert flag_unmatched("27.5%", pool) == [], "0.07 차이는 허용오차 안이다"
    # 경계 밖은 잡는다 (27.6 - 27.4286 = 0.171)
    assert flag_unmatched("27.6%", pool) == ["27.6%"]
    # 부호 반전 허용 - 코드가 -3.2 인데 서술이 "3.2% 하락"은 정상이다
    assert flag_unmatched("3.2% 하락", pool) == []
    assert flag_unmatched("3.2% 하락", pool, allow_sign_flip=False) == ["3.2%"]
    # 음수 표기도 읽는다
    assert flag_unmatched("-3.2%", pool) == []
    print("  허용오차·부호 반전       OK")


def _check_empty_pool_flags_everything():
    """코드가 수치를 안 냈는데 서술에 수치가 있으면 전부 지어낸 것이다."""
    assert flag_unmatched("15.0% 올랐다", []) == ["15.0%"]
    assert flag_unmatched("수치 없는 서술", []) == []
    print("  빈 풀 = 전부 위반        OK")


def _check_pool_excludes_counts():
    readout = {"ad_ratio": 5.8039, "above_sma20_pct": 27.4286,
               "days_used": 40, "label": "RISK_ON", "ok": True, "none": None}
    pool = numeric_pool(readout, exclude=("days_used",))
    assert 40.0 not in pool, "카운트가 풀에 있으면 '40%' 를 지어내도 통과한다"
    assert 5.8039 in pool and 27.4286 in pool
    assert True not in pool and 1.0 not in pool, "bool 은 수치가 아니다"
    # 실제로 40% 가 걸리는가
    assert flag_unmatched("40% 상회", pool) == ["40%"]
    print("  카운트 제외 풀           OK")


def _check_caution_wording():
    lines = caution_lines(["88.8%"])
    assert len(lines) == 1 and "88.8%" in lines[0] and "신뢰 금지" in lines[0]
    assert caution_lines([]) == []
    print("  cautions 문구 단일화     OK")


def _check_trace_reveals_ambiguity():
    """**이 모듈의 새 존재 이유** - 통과가 검증인지 우연인지 가른다.

    지표 확장(10 -> 31)으로 풀이 넓어지자 창작 수치가 우연히 맞는 일이 생겼다.
    실제 사례: 단조 상승 구간에서 rsi=adx=100.0 이라 '99.9%' 가 통과했다.
    """
    pool = keyed_pool({"rsi_14": 100.0, "adx_adx": 100.0,
                       "momentum_20d_pct": 40.0, "bars_used": 120})
    # bookkeeping 은 풀에 안 들어간다
    assert "bars_used" not in pool, pool
    tr = trace_quoted("모멘텀 40.0% 상승, 승률 99.9%, 창작 4444.4%", pool)
    by = {t["raw"]: t for t in tr}
    # 한 키에만 맞음 = 그 지표를 인용한 것이다(검증)
    assert by["40.0%"]["matched_keys"] == ["momentum_20d_pct"]
    assert by["40.0%"]["ambiguous"] is False
    # 두 키에 맞음 = 어느 것을 뜻하는지 모른다(모호) - 이것이 드러나야 한다
    assert by["99.9%"]["matched"] is True and by["99.9%"]["ambiguous"] is True
    assert by["99.9%"]["matched_keys"] == ["adx_adx", "rsi_14"]
    # 아무데도 안 맞음 = 창작
    assert by["4444.4%"]["matched"] is False

    q = quote_quality(tr)
    assert q == {"quoted": 3, "matched": 2, "ambiguous": 1,
                 "ambiguity_ratio": 0.5, "unmatched": ["4444.4%"]}, q

    # **기존 계약을 깨지 않는다** - flag_unmatched 와 결과가 같아야 한다
    vals = list(pool.values())
    assert flag_unmatched("모멘텀 40.0% 상승, 승률 99.9%, 창작 4444.4%", vals) ==         [t["raw"] for t in tr if not t["matched"]]

    # keyed_pool 은 컨테이너 한 겹을 펼친다 - 최상위만 보면 절반을 놓친다
    deep = keyed_pool({"fields": {"매출액": {"value": 186.3}},
                       "ratios": {"BOND_FUT": 1.05}})
    assert deep == {"매출액": 186.3, "ratios.BOND_FUT": 1.05}, deep
    print("  인용 귀속·모호 탐지      OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크 없음)")
    _check_units_are_union()
    _check_tolerance_and_sign()
    _check_empty_pool_flags_everything()
    _check_pool_excludes_counts()
    _check_trace_reveals_ambiguity()
    _check_caution_wording()
    print("숫자 가드 6개 영역 통과.")
