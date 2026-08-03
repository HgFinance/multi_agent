#!/usr/bin/env python3
"""시장 심리·국면 지표 - 시황을 숫자로 만든다.

담당: 재일 (리서치본부)
근거: 재일님 지시 2026-08-03 "거시경제에서의 국면 변화뿐만 아니라 현재 시장의
      국면변화·최근 시황·현재 종목의 국면변화를 레짐쪽이나 거시경제 맞는
      에이전트가 서술하는게 좀 아쉬움", "현재 시장의 심리를 반영한 분석도 좀
      들어갔으면 - 시황 + 시장 심리 지수"
      RESEARCH_OUTPUT_ADVANCEMENT_STRATEGY.md 6.3절(OutlookByHorizonV1)

▶ 세 층의 국면을 구분한다
  거시 사이클  금리·환율·유가가 어느 국면인가 (분기~년 단위)
  시장 국면    지금 시장이 위험선호인가 회피인가 (주~월 단위)
  종목 국면    이 종목이 시장 대비 어디에 있는가 (일~주 단위)
  셋을 섞으면 "시장은 좋은데 종목은 나쁘다" 를 말할 수 없다 - 그게 판단의 핵심인데.

▶ 심리는 지어내지 않는다
  "투자자들이 불안해한다" 같은 문장은 근거가 없다. 우리가 가진 것으로만 만든다:
    등락 확산(A/D), 20일선 위 비율, VKOSPI 수준·백분위, 스타일 회전(고배당 vs 성장),
    거래량 집중. 없는 것(풋콜 비율, 신용잔고)은 **없다고 적는다.**

▶ 국면 전환은 지속을 요구한다
  하루 값이 임계를 넘었다고 "국면이 바뀌었다" 고 하지 않는다. 연속 일수를
  요구하고, 되돌림이 있으면 전환을 취소한다 - 그러지 않으면 매일 국면이 바뀐다.

실행: python evidence/market_psych.py     # 자체 점검 (네트워크 없음)
"""
from __future__ import annotations

import sys

PSYCH_VERSION = "research-market-psych-v1"

# 국면 전환에 요구하는 연속 일수. 1 이면 매일 국면이 바뀐다.
PERSIST_DAYS = 3


def _f(xs) -> list[float]:
    return [float(x) for x in (xs or []) if x is not None]


def percentile_of(value: float | None, history: list[float]) -> float | None:
    """이 값이 과거 대비 몇 번째 백분위인가. **수준보다 상대 위치가 정보다.**

    VKOSPI 20 이 높은지 낮은지는 절대값으로 알 수 없다 - 과거 분포가 있어야 한다.
    """
    h = _f(history)
    if value is None or len(h) < 10:
        return None
    below = sum(1 for x in h if x < value)
    return round(below / len(h) * 100.0, 1)


def breadth_sentiment(ad_ratios: list[float], above_ma_pcts: list[float]) -> dict:
    """등락 확산으로 본 심리. 지수가 아니라 **참여의 넓이**를 본다.

    지수가 올라도 소수 종목만 오르면 그건 강세가 아니다.
    """
    ad, above = _f(ad_ratios), _f(above_ma_pcts)
    out: dict = {}
    if ad:
        out["ad_ratio_latest"] = round(ad[-1], 4)
        if len(ad) >= 5:
            out["ad_ratio_5d_avg"] = round(sum(ad[-5:]) / 5, 4)
        if len(ad) >= 20:
            out["ad_ratio_20d_avg"] = round(sum(ad[-20:]) / 20, 4)
            out["ad_ratio_percentile"] = percentile_of(ad[-1], ad[-60:])
    if above:
        out["above_ma_pct_latest"] = round(above[-1], 2)
        if len(above) >= 20:
            out["above_ma_pct_20d_avg"] = round(sum(above[-20:]) / 20, 2)
            # 참여 확대/축소 - 넓이가 늘고 있나
            out["participation_trend"] = round(
                above[-1] - sum(above[-20:]) / 20, 2)
    return out


def fear_gauge(vkospi: list[float]) -> dict:
    """변동성 지수로 본 공포. 수준과 **백분위**를 함께 낸다.

    VKOSPI 20 이 높은지 낮은지는 절대값으로 말할 수 없다.
    """
    v = _f(vkospi)
    if not v:
        return {"vkospi": None, "note": "VKOSPI 미확인"}
    out = {"vkospi": round(v[-1], 2)}
    if len(v) >= 20:
        out["vkospi_20d_avg"] = round(sum(v[-20:]) / 20, 2)
        out["vkospi_vs_20d_pct"] = round(
            (v[-1] - sum(v[-20:]) / 20) / (sum(v[-20:]) / 20) * 100.0, 2)
    if len(v) >= 60:
        out["vkospi_percentile_60d"] = percentile_of(v[-1], v[-60:])
    return out


def style_rotation(defensive: list[float], growth: list[float]) -> dict:
    """스타일 회전 - **돈이 어디로 가는가**. 방어(고배당·국채) vs 성장.

    지수 수준보다 이 비율의 방향이 위험선호를 더 직접 말한다.
    """
    d, g = _f(defensive), _f(growth)
    if len(d) < 2 or len(g) < 2 or len(d) != len(g):
        return {"style_ratio": None, "note": "스타일 계열 부족"}
    ratio = [a / b for a, b in zip(d, g) if b]
    if len(ratio) < 2:
        return {"style_ratio": None, "note": "스타일 비율 계산 불가"}
    out = {"defensive_over_growth": round(ratio[-1], 4)}
    if len(ratio) >= 20:
        base = sum(ratio[-20:]) / 20
        out["style_rotation_20d_pct"] = round((ratio[-1] - base) / base * 100.0, 2) \
            if base else None
        # 방어 쪽으로 돈이 가면 위험회피다
        out["risk_appetite"] = ("RISK_OFF" if out["style_rotation_20d_pct"] and
                                out["style_rotation_20d_pct"] > 2.0 else
                                "RISK_ON" if out["style_rotation_20d_pct"] and
                                out["style_rotation_20d_pct"] < -2.0 else "NEUTRAL")
    return out


def relative_strength(symbol_closes: list[float],
                      index_closes: list[float], n: int = 20) -> dict:
    """**종목 국면** - 시장 대비 어디에 있는가.

    시장이 오를 때 같이 오르는 것과 시장을 이기는 것은 다르다. 이 구분이
    없으면 "시장은 좋은데 종목은 나쁘다" 를 말할 수 없다.
    """
    s, i = _f(symbol_closes), _f(index_closes)
    if len(s) < n + 1 or len(i) < n + 1:
        return {"relative_strength_20d_pct": None, "note": "시계열 부족"}
    s_ret = (s[-1] - s[-n - 1]) / abs(s[-n - 1]) * 100.0 if s[-n - 1] else None
    i_ret = (i[-1] - i[-n - 1]) / abs(i[-n - 1]) * 100.0 if i[-n - 1] else None
    if s_ret is None or i_ret is None:
        return {"relative_strength_20d_pct": None, "note": "기준가 0"}
    rs = s_ret - i_ret
    return {
        "symbol_return_20d_pct": round(s_ret, 2),
        "index_return_20d_pct": round(i_ret, 2),
        "relative_strength_20d_pct": round(rs, 2),
        # 라벨은 방향 두 개의 조합이다 - 하나만 보면 오독한다
        "symbol_vs_market": (
            "LEADING" if rs > 2.0 else "LAGGING" if rs < -2.0 else "INLINE"),
        "both_direction": (
            "동반상승" if s_ret > 0 and i_ret > 0 else
            "동반하락" if s_ret < 0 and i_ret < 0 else
            "역행"),
    }


def detect_transition(series: list[float], threshold: float, *,
                      above: bool = True,
                      persist: int = PERSIST_DAYS) -> dict:
    """국면 전환 판정. **지속을 요구한다** - 하루 넘었다고 바뀐 게 아니다.

    persist 일 연속으로 임계를 넘어야 전환으로 본다. 그 전에는 '접근 중' 이다.
    """
    x = _f(series)
    if len(x) < persist + 1:
        return {"state": "UNKNOWN", "reason": f"관측 {len(x)}일 < 필요 {persist + 1}일"}

    def _over(v):
        return v > threshold if above else v < threshold

    recent = x[-persist:]
    prior = x[-persist - 1]
    if all(_over(v) for v in recent) and not _over(prior):
        return {"state": "TRANSITIONED", "days": persist,
                "reason": f"{persist}일 연속 임계 {threshold} "
                          f"{'초과' if above else '미만'} (직전은 아님)"}
    if all(_over(v) for v in recent):
        # 이미 그 국면에 있다 - 전환이 아니라 지속이다
        run = 0
        for v in reversed(x):
            if _over(v):
                run += 1
            else:
                break
        return {"state": "SUSTAINED", "days": run,
                "reason": f"{run}일째 같은 국면"}
    if _over(x[-1]):
        return {"state": "APPROACHING", "days": 1,
                "reason": "임계를 넘었으나 지속일수 미달 - 전환이라 부르지 않는다"}
    return {"state": "NORMAL", "days": 0, "reason": "임계 밖"}


def compute_psych_pack(*, ad_ratios=None, above_ma_pcts=None, vkospi=None,
                       defensive=None, growth=None,
                       symbol_closes=None, index_closes=None) -> dict:
    """시장 심리 + 종목 국면. **없는 것은 없다고 적는다.**"""
    out: dict = {}
    out.update(breadth_sentiment(ad_ratios or [], above_ma_pcts or []))
    fg = fear_gauge(vkospi or [])
    out.update({k: v for k, v in fg.items() if v is not None})
    sr = style_rotation(defensive or [], growth or [])
    out.update({k: v for k, v in sr.items() if v is not None})
    rs = relative_strength(symbol_closes or [], index_closes or [])
    out.update({k: v for k, v in rs.items() if v is not None})

    missing = []
    if not vkospi:
        missing.append("VKOSPI")
    if not (defensive and growth):
        missing.append("스타일 계열")
    if not (symbol_closes and index_closes):
        missing.append("종목/지수 시계열")
    # 우리가 아예 못 만드는 것 - 지어내지 않고 이름을 남긴다
    missing.append("풋콜비율·신용잔고(미수집)")
    out["psych_unavailable"] = missing
    return out


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음
# ---------------------------------------------------------------------------

def _check_percentile():
    hist = [float(i) for i in range(100)]
    assert percentile_of(50.0, hist) == 50.0
    assert percentile_of(0.0, hist) == 0.0
    # 표본이 적으면 백분위를 말하지 않는다 - 10개로 낸 백분위는 거짓말이다
    assert percentile_of(5.0, [1.0, 2.0, 3.0]) is None
    assert percentile_of(None, hist) is None
    print("  백분위(표본 부족 None)   OK")


def _check_breadth_and_participation():
    ad = [1.0] * 19 + [5.0]
    above = [20.0] * 19 + [35.0]
    b = breadth_sentiment(ad, above)
    assert b["ad_ratio_latest"] == 5.0
    assert b["ad_ratio_20d_avg"] < 5.0, "최신이 평균보다 높아야 한다"
    # 참여 확대가 양수로 드러난다
    assert b["participation_trend"] > 0, b
    print("  등락 확산·참여 추세      OK")


def _check_fear_gauge_needs_history():
    assert fear_gauge([])["vkospi"] is None
    v = [20.0] * 59 + [30.0]
    f = fear_gauge(v)
    assert f["vkospi"] == 30.0 and f["vkospi_vs_20d_pct"] > 0
    assert f["vkospi_percentile_60d"] is not None
    print("  공포 지수(수준+백분위)   OK")


def _check_style_rotation_direction():
    # 방어가 성장 대비 강해지면 RISK_OFF
    d = [100.0] * 19 + [120.0]
    g = [100.0] * 20
    s = style_rotation(d, g)
    assert s["risk_appetite"] == "RISK_OFF", s
    s2 = style_rotation([100.0] * 20, [100.0] * 19 + [120.0])
    assert s2["risk_appetite"] == "RISK_ON", s2
    # 길이가 다르면 계산하지 않는다
    assert style_rotation([1.0], [1.0, 2.0])["style_ratio"] is None
    print("  스타일 회전 방향         OK")


def _check_relative_strength_separates_market():
    """**종목 국면과 시장 국면을 가른다** - 이게 없으면 오독한다."""
    n = 25
    # 시장 +10%, 종목 +2% -> 동반상승이지만 종목은 뒤처진다
    idx = [100.0 + i * 0.4 for i in range(n)]
    sym = [100.0 + i * 0.08 for i in range(n)]
    r = relative_strength(sym, idx)
    assert r["both_direction"] == "동반상승"
    assert r["symbol_vs_market"] == "LAGGING", r
    assert r["relative_strength_20d_pct"] < 0
    # 시장은 내리는데 종목은 오르면 역행
    r2 = relative_strength(sym, list(reversed(idx)))
    assert r2["both_direction"] == "역행", r2
    print("  종목 vs 시장 국면        OK")


def _check_transition_requires_persistence():
    """하루 넘었다고 국면이 바뀐 게 아니다 - 매일 바뀌면 판정이 아니다."""
    # 하루만 초과 -> 전환이 아니라 접근
    one = [1.0] * 10 + [9.0]
    assert detect_transition(one, 5.0)["state"] == "APPROACHING"
    # 3일 연속 + 직전은 아님 -> 전환
    three = [1.0] * 10 + [9.0, 9.0, 9.0]
    t = detect_transition(three, 5.0)
    assert t["state"] == "TRANSITIONED" and t["days"] == 3, t
    # 계속 그 상태면 전환이 아니라 지속
    long = [9.0] * 15
    su = detect_transition(long, 5.0)
    assert su["state"] == "SUSTAINED" and su["days"] == 15, su
    # 관측이 부족하면 모른다고 한다
    assert detect_transition([9.0], 5.0)["state"] == "UNKNOWN"
    print("  전환은 지속을 요구한다   OK")


def _check_pack_reports_missing():
    """없는 것은 없다고 적는다 - 지어내지 않는다."""
    p = compute_psych_pack(ad_ratios=[1.0] * 20, above_ma_pcts=[20.0] * 20)
    assert "VKOSPI" in p["psych_unavailable"]
    assert "스타일 계열" in p["psych_unavailable"]
    # 우리가 아예 못 만드는 것도 명시한다
    assert any("풋콜" in x for x in p["psych_unavailable"]), p["psych_unavailable"]
    # 있는 것은 계산된다
    assert p["ad_ratio_latest"] == 1.0
    print("  미확인 명시              OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{PSYCH_VERSION} 자체 점검 (네트워크 없음)")
    _check_percentile()
    _check_breadth_and_participation()
    _check_fear_gauge_needs_history()
    _check_style_rotation_direction()
    _check_relative_strength_separates_market()
    _check_transition_requires_persistence()
    _check_pack_reports_missing()
    print("시장 심리·국면 7개 영역 통과.")
