#!/usr/bin/env python3
"""스타일·안전자산 비율 - "그래서 돈이 어디로 갔나"의 결정론 계산.

소유: 재일 (리서치본부)
근거: capability_audit 발견분(국채선물·스타일 지수) 도입, 우선순위 3.
      방법론 등재: methods.py `flight_to_quality`, `style_rotation_ratio`,
      `low_volatility_preference`

▶ 왜 수준이 아니라 비율인가
  고배당지수가 3% 올랐다는 사실 하나로는 아무 말도 못 한다 - 시장이 3%
  올랐으면 아무 일도 없었던 것이다. 스타일은 **상대**로만 존재한다.
  그래서 모든 계열을 KOSPI200 으로 나눈 뒤에야 지표로 취급한다.

▶ 같은 날끼리만 나눈다
  분자가 금요일 종가고 분모가 목요일 종가면 그 비율은 스타일이 아니라
  하루치 시장 등락이다. 날짜가 짝이 맞는 관측만 비율이 된다.

▶ 라벨은 여기서 정하고 LLM 은 서술만 한다
  기존 분석가 3단(compute -> narrate -> verify)과 같은 규율이다.

실행: python evidence/style_ratios.py     # 자체 점검(네트워크·DB 없음)
"""
from __future__ import annotations

import sys
from datetime import date

MODULE_VERSION = "research-style-ratios-v1"

BENCHMARK_CODE = "KOSPI200"
VOL_CODE = "VKOSPI"
# 비율 계열의 표시 이름 - 라벨 사유를 사람이 읽을 수 있게 한다
STYLE_LABELS = {
    "BOND_FUT": "국채선물(안전자산)",
    "MIN_VOL": "최소변동성(방어)",
    "DIV_50": "고배당50(배당·가치)",
    "DIV_GROWTH": "배당성장50",
    "EX_MEGA": "초대형주제외(중소형)",
}
# 어느 계열이 앞서면 어떤 국면인가 - 코드가 라벨을 정하므로 표로 고정한다
TILT_BY_CODE = {
    "BOND_FUT": "SAFE_HAVEN",
    "MIN_VOL": "DEFENSIVE",
    "DIV_50": "DIVIDEND_VALUE",
    "DIV_GROWTH": "DIVIDEND_VALUE",
    "EX_MEGA": "SMALL_MID",
}

LOOKBACK = 20              # 상대강도 목표 창(거래일)
MIN_LOOKBACK = 5           # 이보다 짧으면 상대강도라 부르지 않는다
MIN_HISTORY = 60           # 백분위를 말하려면 최소 이만큼은 있어야 한다
TILT_MIN_CHG_PCT = 1.0     # 상대강도가 이 미만이면 쏠림이라 부르지 않는다

# ▶ 창이 짧아도 기준(1.0%)은 낮추지 않는다
#   LS 로 지수 과거 시세를 받을 수 없어(2026-08-02 실측: t8419 /indtp/chart 는
#   헤더만 오고 시계열 0행) 이력은 수집 시작일부터 하루씩 쌓인다. 20일을 다
#   기다리면 한 달간 이 축이 죽는다.
#   그래서 창은 가진 만큼(5~20일) 쓰되 **문턱은 고정한다** - 짧은 창에서
#   1.0% 를 넘기는 게 더 어려우므로 판정은 보수적으로만 기운다(개발원칙 9).
#   대신 실제 쓴 창을 lookback_used 로 항상 함께 낸다.
# ▶ 계열끼리는 같은 창으로만 비교한다
#   A 는 20일 +2%, B 는 5일 +3% 를 나란히 놓고 "B 가 앞선다"고 하면 그건
#   비교가 아니다. 공통 창(가장 짧은 계열에 맞춘 창) 하나로 전부 다시 잰다.

TILT_RULES = {
    "판정순서": "계산 가능한 비율이 2개 미만 -> UNDETERMINED, "
              "공통 창 상대강도 최대값 < 1.0% -> BROAD_MARKET, 그 외 최대 계열의 축",
    "상대강도": "(오늘 비율 / 공통창 거래일 전 비율 - 1) * 100 (%). "
              f"공통 창은 {MIN_LOOKBACK}~{LOOKBACK}거래일, 실제값은 lookback_used",
    "공통창": "모든 비율 계열이 함께 가진 길이로 맞춘다 - 서로 다른 창의 "
            "상대강도를 나란히 비교하지 않는다",
    "비율": "스타일지수 종가 / KOSPI200 종가 (같은 날짜끼리만)",
    "백분위": f"보유 이력 안에서의 순위(%). 이력 {MIN_HISTORY}일 미만이면 None",
    "UNDETERMINED": "비율 2개 미만이거나 공통 창이 "
                    f"{MIN_LOOKBACK}거래일 미만 - 비교 불가",
    "BROAD_MARKET": "어느 스타일도 시장을 1% 이상 앞서지 않음",
}


class StyleRatioError(ValueError):
    """입력이 계산 규율을 어겼다. 조용히 빈 값으로 바꾸지 않는다."""


# ---------------------------------------------------------------------------
# 순수 계산
# ---------------------------------------------------------------------------

def _to_date(raw) -> date | None:
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def group_by_code(rows) -> dict[str, dict[date, float]]:
    """관측 행 -> {계열코드: {날짜: 값}}.

    날짜·값이 깨진 행은 버리되 **버렸다는 사실이 남게** 개수를 세어 돌려준다
    (호출부가 coverage 로 보고한다). 같은 날 중복은 나중 행이 이긴다 -
    재수집분이 우선이라는 macro 규약과 같다.
    """
    out: dict[str, dict[date, float]] = {}
    for r in rows or []:
        code = str(r.get("code") or "").strip()
        d = _to_date(r.get("period"))
        if not code or d is None:
            continue
        try:
            v = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        if v <= 0:
            # 지수는 양수다. 0 이나 음수는 분모가 되면 폭발하고 분자면 무의미.
            continue
        out.setdefault(code, {})[d] = v
    return out


def ratio_series(numer: dict[date, float],
                 denom: dict[date, float]) -> dict[date, float]:
    """같은 날짜에 둘 다 있는 날만 비율을 만든다 - 날짜가 어긋난 비율은
    스타일이 아니라 그냥 하루치 등락이다."""
    return {d: numer[d] / denom[d] for d in sorted(set(numer) & set(denom))}


def percentile_of_last(series: dict[date, float],
                       *, min_history: int = MIN_HISTORY) -> float | None:
    """보유 이력 안에서 최신값의 순위(%). 이력이 짧으면 None - '오늘이 1년
    최고'라고 말하려면 1년이 있어야 한다."""
    if len(series) < min_history:
        return None
    vals = [series[d] for d in sorted(series)]
    last = vals[-1]
    below = sum(1 for v in vals if v < last)
    ties = sum(1 for v in vals if v == last)
    # 동률은 절반만 인정(중간 순위) - 계단식 지수에서 100% 가 남발되지 않는다
    return round((below + ties / 2) / len(vals) * 100, 1)


def change_pct(series: dict[date, float], *, lookback: int = LOOKBACK) -> float | None:
    """최신 대비 lookback 거래일 전 변화율(%). 관측이 모자라면 None."""
    days = sorted(series)
    if lookback < 1 or len(days) < lookback + 1:
        return None
    old = series[days[-1 - lookback]]
    if old <= 0:
        return None
    return round((series[days[-1]] / old - 1) * 100, 2)


def common_lookback(series_list, *, target: int = LOOKBACK,
                    minimum: int = MIN_LOOKBACK) -> int | None:
    """여러 계열이 **함께** 가진 창 길이. 없으면 None(비교 불가).

    가장 짧은 계열에 맞춘다 - 긴 계열만 20일로 재고 짧은 계열은 5일로 재면
    그 둘의 비교는 상대강도가 아니라 창 길이의 차이를 보는 것이다.
    """
    lengths = [len(s) for s in series_list if s]
    if not lengths:
        return None
    window = min(min(lengths) - 1, target)
    return window if window >= minimum else None


def compute_style_overlay(rows, *, benchmark: str = BENCHMARK_CODE,
                          vol_code: str = VOL_CODE,
                          min_history: int = MIN_HISTORY) -> dict:
    """macro 관측 행 -> 스타일 overlay + 결정론 tilt 라벨.

    순수 함수(네트워크·시계 없음). 분모가 없으면 비율을 하나도 못 만들므로
    ratios 는 빈 dict 이고 tilt 는 UNDETERMINED 다 - **빈 결과를 성공으로
    위장하지 않고 사유를 tilt_reason 에 적는다.**
    """
    by_code = group_by_code(rows)
    bench = by_code.get(benchmark) or {}

    overlay: dict = {
        "benchmark_code": benchmark,
        "benchmark_level": None,
        "as_of": None,
        "volatility": None,
        "ratios": {},
        "tilt": "UNDETERMINED",
        "tilt_reason": None,
        "tilt_rules": TILT_RULES,
    }

    if bench:
        last_day = max(bench)
        overlay["as_of"] = last_day.isoformat()
        overlay["benchmark_level"] = round(bench[last_day], 2)

    vol = by_code.get(vol_code) or {}
    if vol:
        vd = max(vol)
        vw = common_lookback([vol])
        overlay["volatility"] = {
            "code": vol_code,
            "level": round(vol[vd], 2),
            "as_of": vd.isoformat(),
            "percentile": percentile_of_last(vol, min_history=min_history),
            "change_pct": None if vw is None else change_pct(vol, lookback=vw),
            "lookback_used": vw,
            "days": len(vol),
        }

    if not bench:
        overlay["tilt_reason"] = (
            f"분모 {benchmark} 관측이 없다 - 스타일은 상대로만 존재하므로 "
            f"비율을 만들 수 없다")
        return overlay

    ratio_by_code: dict[str, dict[date, float]] = {}
    for code, series in sorted(by_code.items()):
        if code in (benchmark, vol_code):
            continue
        rs = ratio_series(series, bench)
        if rs:
            ratio_by_code[code] = rs

    # 창은 전 계열 공통으로 하나만 정한다 - 계열마다 다른 창을 쓰면 비교가 아니다
    window = common_lookback(list(ratio_by_code.values()))
    overlay["lookback_used"] = window

    for code, rs in ratio_by_code.items():
        rd = max(rs)
        overlay["ratios"][code] = {
            "name": STYLE_LABELS.get(code, code),
            "ratio": round(rs[rd], 6),
            "as_of": rd.isoformat(),
            "change_pct": None if window is None else change_pct(rs, lookback=window),
            "percentile": percentile_of_last(rs, min_history=min_history),
            "days": len(rs),
        }

    if len(overlay["ratios"]) < 2:
        overlay["tilt_reason"] = (
            f"비율이 {len(overlay['ratios'])}개뿐이라 스타일을 비교할 대상이 없다")
        return overlay
    if window is None:
        overlay["tilt_reason"] = (
            f"전 계열 공통 이력이 {MIN_LOOKBACK}거래일 미만 - 상대강도 미확인. "
            f"지수 과거 시세를 소급할 수 없어 수집 시작일부터 하루씩 쌓인다")
        return overlay

    ranked = sorted(((c, m["change_pct"]) for c, m in overlay["ratios"].items()
                     if m["change_pct"] is not None), key=lambda kv: -kv[1])
    if not ranked:
        overlay["tilt_reason"] = f"공통 창 {window}거래일에서 계산된 상대강도가 없다"
        return overlay

    top_code, top_chg = ranked[0]
    if top_chg < TILT_MIN_CHG_PCT:
        overlay["tilt"] = "BROAD_MARKET"
        overlay["tilt_reason"] = (
            f"최대 상대강도가 {overlay['ratios'][top_code]['name']} {top_chg}% "
            f"({window}거래일)로 기준 {TILT_MIN_CHG_PCT}% 미만 - 특정 스타일 "
            f"쏠림이라 부르지 않는다")
        return overlay

    overlay["tilt"] = TILT_BY_CODE.get(top_code, "OTHER_STYLE")
    overlay["tilt_leader"] = top_code
    overlay["tilt_reason"] = (
        f"{overlay['ratios'][top_code]['name']} 가 {window}거래일 동안 "
        f"KOSPI200 대비 {top_chg}% - 계산된 {len(ranked)}개 중 최대"
        + ("" if window >= LOOKBACK else
           f" (목표 창 {LOOKBACK}일 미달 - 이력이 쌓이면 자동으로 넓어진다)"))
    return overlay


def overlay_numbers(overlay) -> list[float]:
    """overlay 안의 모든 수치(중첩 포함) - 서술 검증의 숫자 풀에 넣는다.
    여기서 빠뜨리면 **정상 인용이 환각으로 오판된다.**"""
    pool: list[float] = []

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            pool.append(float(node))

    walk(overlay)
    return pool


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _rows(code: str, values: list[float], *, end=date(2026, 8, 3), step=1):
    from datetime import timedelta
    n = len(values)
    return [{"code": code,
             "period": (end - timedelta(days=(n - 1 - i) * step)).isoformat(),
             "value": v} for i, v in enumerate(values)]


def _check_grouping():
    rows = _rows("A", [1.0, 2.0]) + [
        {"code": "", "period": "2026-08-03", "value": 1},        # 코드 없음
        {"code": "B", "period": "쓰레기", "value": 1},            # 날짜 깨짐
        {"code": "B", "period": "2026-08-03", "value": "숫자아님"},  # 값 깨짐
        {"code": "B", "period": "2026-08-02", "value": 0},        # 0 은 분모 폭발
        {"code": "B", "period": "2026-08-01", "value": -5},       # 음수 지수 없음
    ]
    g = group_by_code(rows)
    assert set(g) == {"A"}, g
    assert len(g["A"]) == 2
    # 같은 날 중복은 나중 행이 이긴다
    g2 = group_by_code([{"code": "A", "period": "2026-08-03", "value": 1},
                        {"code": "A", "period": "2026-08-03", "value": 9}])
    assert g2["A"][date(2026, 8, 3)] == 9.0
    print("  행 그룹핑·불량행 배제    OK")


def _check_ratio_date_alignment():
    """날짜가 어긋난 관측으로 비율을 만들면 안 된다 - 스타일이 아니라 등락이 된다."""
    num = {date(2026, 8, 3): 100.0, date(2026, 8, 1): 90.0}
    den = {date(2026, 8, 3): 50.0, date(2026, 8, 2): 45.0}
    rs = ratio_series(num, den)
    assert list(rs) == [date(2026, 8, 3)], rs
    assert rs[date(2026, 8, 3)] == 2.0
    assert ratio_series(num, {}) == {}
    print("  같은 날짜끼리만 비율     OK")


def _check_percentile_and_change():
    from datetime import timedelta
    d0 = date(2026, 6, 1)

    s = {d0 + timedelta(days=i): float(i) for i in range(60)}  # 60개 증가 계열
    p = percentile_of_last(s)
    assert p is not None and p > 99, p
    # 이력이 짧으면 백분위를 말하지 않는다
    assert percentile_of_last({date(2026, 8, 3): 1.0}) is None
    # 동률 절반 인정 - 전부 같은 값이면 50%
    flat = {d0 + timedelta(days=i): 5.0 for i in range(60)}
    assert percentile_of_last(flat) == 50.0, percentile_of_last(flat)
    # 20거래일 변화율: 21개 필요
    up = {d0 + timedelta(days=i): 100.0 + i for i in range(21)}
    assert change_pct(up) == 20.0, change_pct(up)
    assert change_pct({date(2026, 8, 3): 1.0}) is None
    print("  백분위·상대강도 계산     OK")


def _check_tilt_rules():
    # 국채선물만 20일간 +5%, 나머지는 제자리 -> SAFE_HAVEN
    bench = _rows(BENCHMARK_CODE, [100.0] * 21)
    bond = _rows("BOND_FUT", [100.0 + i * 0.25 for i in range(21)])   # +5%
    flat = _rows("DIV_50", [100.0] * 21)
    o = compute_style_overlay(bench + bond + flat)
    assert o["tilt"] == "SAFE_HAVEN", o["tilt_reason"]
    assert o["tilt_leader"] == "BOND_FUT"
    assert o["ratios"]["BOND_FUT"]["change_pct"] == 5.0
    assert o["lookback_used"] == 20
    assert o["ratios"]["DIV_50"]["change_pct"] == 0.0
    assert o["benchmark_level"] == 100.0 and o["as_of"] == "2026-08-03"

    # 아무도 1% 를 못 넘기면 쏠림이라 부르지 않는다
    weak = compute_style_overlay(
        bench + _rows("BOND_FUT", [100.0 + i * 0.02 for i in range(21)]) + flat)
    assert weak["tilt"] == "BROAD_MARKET", weak
    assert "미만" in weak["tilt_reason"]

    # 분모가 없으면 UNDETERMINED + 사유
    none_bench = compute_style_overlay(bond + flat)
    assert none_bench["tilt"] == "UNDETERMINED" and none_bench["ratios"] == {}
    assert BENCHMARK_CODE in none_bench["tilt_reason"]

    # 비율이 1개뿐이어도 비교 불가
    one = compute_style_overlay(bench + bond)
    assert one["tilt"] == "UNDETERMINED" and len(one["ratios"]) == 1
    print("  tilt 라벨 4갈래          OK")


def _check_adaptive_window():
    """지수 과거 시세를 소급할 수 없으므로 이력은 하루씩 쌓인다. 창은 가진
    만큼 쓰되 문턱(1.0%)은 낮추지 않고, 계열끼리는 같은 창으로만 비교한다."""
    # 6일치뿐 -> 공통 창 5일. 5일간 +2% 면 문턱을 넘으므로 tilt 가 선다
    short = compute_style_overlay(
        _rows(BENCHMARK_CODE, [100.0] * 6)
        + _rows("BOND_FUT", [100.0 + i * 0.4 for i in range(6)])   # 5일 +2%
        + _rows("DIV_50", [100.0] * 6))
    assert short["lookback_used"] == 5, short["lookback_used"]
    assert short["tilt"] == "SAFE_HAVEN", short["tilt_reason"]
    assert "목표 창" in short["tilt_reason"], "짧은 창을 쓴 사실이 드러나야 한다"

    # 문턱은 창이 짧다고 낮아지지 않는다 - 5일 +0.5% 는 여전히 쏠림이 아니다
    weak = compute_style_overlay(
        _rows(BENCHMARK_CODE, [100.0] * 6)
        + _rows("BOND_FUT", [100.0 + i * 0.1 for i in range(6)])
        + _rows("DIV_50", [100.0] * 6))
    assert weak["tilt"] == "BROAD_MARKET", weak["tilt_reason"]

    # 4일치면 창이 안 서고, 그 사유가 남는다 (0 이나 임의값으로 채우지 않는다)
    tiny = compute_style_overlay(
        _rows(BENCHMARK_CODE, [100.0] * 4)
        + _rows("BOND_FUT", [100.0 + i for i in range(4)])
        + _rows("DIV_50", [100.0] * 4))
    assert tiny["lookback_used"] is None and tiny["tilt"] == "UNDETERMINED"
    assert "소급" in tiny["tilt_reason"], tiny["tilt_reason"]
    assert tiny["ratios"]["BOND_FUT"]["change_pct"] is None

    # 한 계열만 길어도 공통 창은 짧은 쪽에 맞춘다 - 다른 창끼리 비교 금지
    mixed = compute_style_overlay(
        _rows(BENCHMARK_CODE, [100.0] * 21)
        + _rows("BOND_FUT", [100.0 + i * 0.25 for i in range(21)])
        + _rows("DIV_50", [100.0] * 8))
    assert mixed["lookback_used"] == 7, mixed["lookback_used"]
    print("  적응 창·문턱 고정        OK")


def _check_volatility_block():
    bench = _rows(BENCHMARK_CODE, [100.0] * 21)
    vk = _rows(VOL_CODE, [20.0 + i for i in range(21)])
    o = compute_style_overlay(bench + vk)
    assert o["volatility"]["level"] == 40.0
    assert o["volatility"]["change_pct"] == 100.0
    # VKOSPI 는 지수 대비 비율을 만들지 않는다 (단위가 다르다)
    assert VOL_CODE not in o["ratios"], "변동성지수를 스타일 비율에 섞으면 안 된다"
    # 이력 60일 미만이면 백분위는 None
    assert o["volatility"]["percentile"] is None
    assert compute_style_overlay(bench)["volatility"] is None
    print("  변동성 블록 분리         OK")


def _check_number_pool():
    o = compute_style_overlay(
        _rows(BENCHMARK_CODE, [100.0] * 21)
        + _rows("BOND_FUT", [100.0 + i * 0.25 for i in range(21)])
        + _rows("DIV_50", [100.0] * 21))
    pool = overlay_numbers(o)
    # 중첩 안의 수치가 전부 들어와야 정상 인용이 환각으로 오판되지 않는다
    assert 5.0 in pool and 100.0 in pool          # 상대강도·기준지수 수준
    assert all(isinstance(x, float) for x in pool)
    # 두 계열의 ratio/change/days + benchmark_level + lookback_used = 8.
    # None(백분위)은 안 담는다
    assert len(pool) == 8, (len(pool), pool)
    assert o["ratios"]["BOND_FUT"]["ratio"] in pool, "중첩 2단이 누락되면 오판한다"
    print("  숫자 풀 중첩 수집        OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_grouping()
    _check_ratio_date_alignment()
    _check_percentile_and_change()
    _check_tilt_rules()
    _check_adaptive_window()
    _check_volatility_block()
    _check_number_pool()
    print("스타일 비율 7개 영역 통과.")
