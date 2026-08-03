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


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크 없음)")
    _check_units_are_union()
    _check_tolerance_and_sign()
    _check_empty_pool_flags_everything()
    _check_pool_excludes_counts()
    _check_caution_wording()
    print("숫자 가드 5개 영역 통과.")
