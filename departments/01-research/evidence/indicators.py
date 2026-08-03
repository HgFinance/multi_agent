#!/usr/bin/env python3
"""추세·변동성·모멘텀 지표 - 근거를 풍부하게 만드는 결정론 계산.

담당: 재일 (리서치본부)
근거: 재일님 지시 2026-08-03 "인용하는 지표가 좀 적어서 근거가 부실한 느낌.
      현재 이동평균선 MA 정도만 기용하고 있는 느낌인데 추세 관련 지표들도 많고
      변동성 관련된 지표도 있을 것이고 그 외 다양한 지표 추가"
      RESEARCH_OUTPUT_ADVANCEMENT_STRATEGY.md 4.1절(Evidence before Narrative)

▶ 지표를 늘리면 검증도 같이 는다
  number_guard 의 확정치 풀은 readout 에서 나온다. 지표가 늘면 풀이 넓어지고,
  분석가가 쓸 수 있는 문장도 넓어진다 - **자율성과 검증이 같이 큰다.**
  그래서 "가드가 서술을 막는다" 는 문제의 답은 가드 완화가 아니라 지표 확장이다.

▶ 새 의존을 들이지 않는다
  pandas·TA-Lib 없이 순수 Python 으로 쓴다. 이 지표들은 전부 한 줄 수식이고,
  라이브러리를 들이는 기준(기존 스택으로 못 푸는 문제 + 제거 기준)을 못 넘는다.
  numpy 는 이미 있지만 종가 100~250개 계산에 굳이 필요 없다.

▶ 계산 못 하면 None 이다. 0 이 아니다.
  봉이 부족하면 그 지표는 None 이고 unavailable 목록에 이름이 남는다.
  0 으로 채우면 "지표가 0" 과 "계산 못 함" 이 섞이고, 그 순간 판정이 오염된다.

▶ 입력 규약
  closes/highs/lows/volumes 는 **과거 -> 최신** 순이다. 호출자가 정렬해서 준다
  (market-api /bars 는 최신순으로 오므로 뒤집어야 한다 - 실측 사고 있었다).

실행: python evidence/indicators.py     # 자체 점검 (네트워크 없음)
"""
from __future__ import annotations

import math
import sys

INDICATORS_VERSION = "research-indicators-v1"


def _f(xs) -> list[float]:
    return [float(x) for x in (xs or []) if x is not None]


def sma(xs: list[float], n: int) -> float | None:
    xs = _f(xs)
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema(xs: list[float], n: int) -> float | None:
    """지수이동평균. SMA 로 초기화한다 - 첫 값을 종가로 잡으면 초반이 왜곡된다."""
    xs = _f(xs)
    if len(xs) < n:
        return None
    k = 2.0 / (n + 1)
    cur = sum(xs[:n]) / n
    for x in xs[n:]:
        cur = x * k + cur * (1 - k)
    return cur


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder RSI. 과매수/과매도가 아니라 **모멘텀의 강도**를 본다.

    Wilder(1978). 상승/하락 평균을 지수평활한다 - 단순평균을 쓰면 값이 튄다.
    """
    c = _f(closes)
    if len(c) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def macd(closes: list[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> dict:
    """MACD 선·신호선·히스토그램. 추세 전환의 **속도**를 본다.

    히스토그램 부호가 바뀌는 것이 전환 신호이고, 크기가 그 강도다.
    """
    c = _f(closes)
    if len(c) < slow + signal:
        return {"macd": None, "signal": None, "hist": None}
    # 각 시점의 MACD 선을 만들어야 신호선(그 EMA)을 낼 수 있다
    line = []
    for i in range(slow, len(c) + 1):
        f, s = ema(c[:i], fast), ema(c[:i], slow)
        if f is None or s is None:
            continue
        line.append(f - s)
    if len(line) < signal:
        return {"macd": round(line[-1], 4) if line else None,
                "signal": None, "hist": None}
    sig = ema(line, signal)
    m = line[-1]
    return {"macd": round(m, 4), "signal": round(sig, 4),
            "hist": round(m - sig, 4)}


def atr(highs, lows, closes, n: int = 14) -> float | None:
    """Average True Range. **변동성을 가격 단위로** 본다 - % 변동성과 다르다.

    Wilder(1978). 갭을 포함하므로 장중 범위만 보는 것보다 정직하다.
    """
    h, low, c = _f(highs), _f(lows), _f(closes)
    if min(len(h), len(low), len(c)) < n + 1:
        return None
    trs = []
    for i in range(1, min(len(h), len(low), len(c))):
        trs.append(max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1])))
    if len(trs) < n:
        return None
    cur = sum(trs[:n]) / n
    for t in trs[n:]:
        cur = (cur * (n - 1) + t) / n
    return round(cur, 4)


def bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> dict:
    """볼린저 밴드. %B 는 밴드 안 위치, 밴드폭은 **변동성 압축/확장**을 본다.

    밴드폭이 좁아진 뒤 확장하는 것이 국면 전환의 흔한 형태다.
    """
    c = _f(closes)
    if len(c) < n:
        return {"pct_b": None, "bandwidth_pct": None, "mid": None}
    win = c[-n:]
    mid = sum(win) / n
    var = sum((x - mid) ** 2 for x in win) / n
    sd = math.sqrt(var)
    up, lo = mid + k * sd, mid - k * sd
    return {
        "mid": round(mid, 4),
        "pct_b": round((c[-1] - lo) / (up - lo) * 100.0, 2) if up != lo else None,
        "bandwidth_pct": round((up - lo) / mid * 100.0, 2) if mid else None,
    }


def adx(highs, lows, closes, n: int = 14) -> dict:
    """ADX/DMI. **추세의 존재와 방향**을 나눠 본다.

    Wilder(1978). ADX 는 방향이 아니라 강도다 - 25 위면 추세, 아래면 횡보로
    보는 것이 통상이나 임계는 시장마다 다르므로 값만 내고 판정은 호출자가 한다.
    """
    h, low, c = _f(highs), _f(lows), _f(closes)
    m = min(len(h), len(low), len(c))
    if m < 2 * n:
        return {"adx": None, "plus_di": None, "minus_di": None}
    plus, minus, trs = [], [], []
    for i in range(1, m):
        up, dn = h[i] - h[i - 1], low[i - 1] - low[i]
        plus.append(up if (up > dn and up > 0) else 0.0)
        minus.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1])))

    def _smooth(xs):
        cur = sum(xs[:n])
        out = [cur]
        for x in xs[n:]:
            cur = cur - cur / n + x
            out.append(cur)
        return out

    if len(trs) < n:
        return {"adx": None, "plus_di": None, "minus_di": None}
    str_, sp, sm = _smooth(trs), _smooth(plus), _smooth(minus)
    dis = []
    for t, p, mi in zip(str_, sp, sm):
        if t == 0:
            continue
        pdi, mdi = 100.0 * p / t, 100.0 * mi / t
        dis.append((pdi, mdi, 100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0))
    if len(dis) < n:
        return {"adx": None,
                "plus_di": round(dis[-1][0], 2) if dis else None,
                "minus_di": round(dis[-1][1], 2) if dis else None}
    dx = [d[2] for d in dis]
    cur = sum(dx[:n]) / n
    for x in dx[n:]:
        cur = (cur * (n - 1) + x) / n
    return {"adx": round(cur, 2), "plus_di": round(dis[-1][0], 2),
            "minus_di": round(dis[-1][1], 2)}


def trend_slope_pct(closes: list[float], n: int = 20) -> float | None:
    """최소제곱 추세 기울기를 **일평균 %**로. 방향과 속도를 한 숫자로 본다."""
    c = _f(closes)[-n:]
    if len(c) < max(3, n // 2):
        return None
    k = len(c)
    xs = list(range(k))
    mx, my = sum(xs) / k, sum(c) / k
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, c)) / den
    return round(slope / my * 100.0, 4) if my else None


def donchian_position_pct(closes, highs=None, lows=None, n: int = 52) -> float | None:
    """N일 레인지 안 위치(%). 52주 신고가/신저가 대비 어디인가."""
    c = _f(closes)
    if n <= 0 or len(c) < n:
        return None
    # highs/lows 가 짧으면 종가로 대체한다 - 빈 목록에 max() 를 부르면 죽는다
    h, low = _f(highs)[-n:], _f(lows)[-n:]
    hi = max(h) if len(h) == n else max(c[-n:])
    lo = min(low) if len(low) == n else min(c[-n:])
    return round((c[-1] - lo) / (hi - lo) * 100.0, 1) if hi != lo else None


def realized_vol_pct(closes: list[float], n: int = 20,
                     annualize: int = 252) -> float | None:
    """로그수익률 실현변동성(연율화 %). 종가 기반이라 장중 변동은 못 본다."""
    c = _f(closes)
    if len(c) < n + 1:
        return None
    rets = [math.log(c[i] / c[i - 1]) for i in range(len(c) - n, len(c))
            if c[i - 1] > 0 and c[i] > 0]
    if len(rets) < 2:
        return None
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(var * annualize) * 100.0, 2)


def parkinson_vol_pct(highs, lows, n: int = 20,
                      annualize: int = 252) -> float | None:
    """Parkinson(1980) 고가-저가 변동성. 종가 변동성보다 **효율적**이다.

    같은 표본에서 분산 추정 오차가 작다 - 장중 범위 정보를 쓰기 때문이다.
    종가 변동성과 함께 보면 갭 위주인지 장중 위주인지 갈린다.
    """
    h, low = _f(highs)[-n:], _f(lows)[-n:]
    if len(h) < n or len(low) < n:
        return None
    acc = [math.log(a / b) ** 2 for a, b in zip(h, low) if a > 0 and b > 0]
    if not acc:
        return None
    var = sum(acc) / (4.0 * math.log(2.0) * len(acc))
    return round(math.sqrt(var * annualize) * 100.0, 2)


def volume_trend(volumes: list[float], short: int = 5,
                 long: int = 20) -> dict:
    """거래량 추세. 가격 움직임에 **참여가 따라오는가**를 본다."""
    v = _f(volumes)
    s, ln = sma(v, short), sma(v, long)
    return {"vol_ratio_s_over_l": round(s / ln, 4) if s and ln else None,
            "vol_zscore": _zscore(v, long)}


def _zscore(xs: list[float], n: int) -> float | None:
    x = _f(xs)
    if len(x) < n + 1:
        return None
    win = x[-n:]
    mu = sum(win) / n
    sd = math.sqrt(sum((y - mu) ** 2 for y in win) / n)
    return round((x[-1] - mu) / sd, 2) if sd else None


def compute_trend_pack(closes, highs=None, lows=None, volumes=None) -> dict:
    """추세·변동성·모멘텀을 한 번에. **못 구한 것은 unavailable 에 이름을 남긴다.**

    0 으로 채우지 않는다 - "지표가 0" 과 "계산 못 함" 이 섞이면 판정이 오염된다.
    """
    c = _f(closes)
    out: dict = {}
    out["rsi_14"] = rsi(c, 14)
    out.update({f"macd_{k}": v for k, v in macd(c).items()})
    out.update({f"bb_{k}": v for k, v in bollinger(c).items()})
    out["trend_slope_20d_pct"] = trend_slope_pct(c, 20)
    out["trend_slope_60d_pct"] = trend_slope_pct(c, 60)
    out["range_position_52w_pct"] = donchian_position_pct(c, highs, lows, 252) \
        if len(c) >= 252 else donchian_position_pct(c, highs, lows, min(len(c), 60))
    out["realized_vol_20d_pct"] = realized_vol_pct(c, 20)
    out["realized_vol_60d_pct"] = realized_vol_pct(c, 60)
    if highs and lows:
        out.update({f"adx_{k}": v for k, v in adx(highs, lows, c).items()})
        out["atr_14"] = atr(highs, lows, c, 14)
        out["parkinson_vol_20d_pct"] = parkinson_vol_pct(highs, lows, 20)
    if volumes:
        out.update(volume_trend(volumes))
    # 변동성 국면 - 단기가 장기보다 크면 확장, 작으면 압축
    s, l = out.get("realized_vol_20d_pct"), out.get("realized_vol_60d_pct")
    if s and l:
        out["vol_regime_ratio"] = round(s / l, 3)

    unavailable = sorted(k for k, v in out.items() if v is None)
    for k in unavailable:
        out.pop(k)
    out["unavailable"] = unavailable
    out["bars_used"] = len(c)
    return out


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음
# ---------------------------------------------------------------------------

def _series(n=300, start=100.0, step=0.5):
    return [start + step * i for i in range(n)]


def _check_insufficient_is_none():
    """봉이 부족하면 None 이다. **0 이 아니다.**"""
    short = [100.0, 101.0]
    assert rsi(short) is None and atr([1, 2], [0, 1], short) is None
    assert bollinger(short)["pct_b"] is None
    assert adx([1, 2], [0, 1], short)["adx"] is None
    assert trend_slope_pct(short, 20) is None
    pack = compute_trend_pack(short)
    # 못 구한 것은 키에서 빠지고 이름이 남는다
    assert "rsi_14" not in pack and "rsi_14" in pack["unavailable"]
    assert pack["bars_used"] == 2
    print("  부족 = None(0 아님)      OK")


def _check_rsi_bounds_and_direction():
    up = _series(60)
    down = list(reversed(up))
    r_up, r_dn = rsi(up), rsi(down)
    assert 0 <= r_dn < 50 < r_up <= 100, (r_up, r_dn)
    assert r_up == 100.0, "단조 상승이면 100 이다"
    # 횡보는 중간
    flat = [100.0] * 60
    assert rsi(flat) == 50.0, rsi(flat)
    print("  RSI 범위·방향            OK")


def _check_macd_sign_follows_trend():
    up, down = _series(120), list(reversed(_series(120)))
    assert macd(up)["hist"] is not None and macd(up)["macd"] > 0
    assert macd(down)["macd"] < 0
    print("  MACD 부호                OK")


def _check_bollinger_and_vol_regime():
    flat = [100.0] * 40
    b = bollinger(flat)
    # 변동이 없으면 밴드폭 0, %B 는 정의 불가(None) - 0 으로 채우지 않는다
    assert b["bandwidth_pct"] == 0.0 and b["pct_b"] is None, b
    noisy = [100.0 + (5 if i % 2 else -5) for i in range(40)]
    assert bollinger(noisy)["bandwidth_pct"] > 0
    # 변동성 확대 국면이면 ratio > 1
    calm_then_wild = [100.0] * 60 + [100.0 + (8 if i % 2 else -8) for i in range(30)]
    p = compute_trend_pack(calm_then_wild)
    assert p.get("vol_regime_ratio", 0) > 1.0, p.get("vol_regime_ratio")
    print("  볼린저·변동성 국면       OK")


def _check_adx_detects_trend():
    n = 80
    hi = [100.0 + i for i in range(n)]
    lo = [99.0 + i for i in range(n)]
    cl = [99.5 + i for i in range(n)]
    a = adx(hi, lo, cl)
    assert a["adx"] is not None and a["plus_di"] > a["minus_di"], a
    # 횡보는 +DI 와 -DI 가 비슷하다
    flat_h = [100.5] * n
    flat_l = [99.5] * n
    flat_c = [100.0] * n
    f = adx(flat_h, flat_l, flat_c)
    assert f["plus_di"] == f["minus_di"] == 0.0, f
    print("  ADX 추세 탐지            OK")


def _check_slope_and_position():
    up = _series(60, 100.0, 1.0)
    s = trend_slope_pct(up, 20)
    assert s and s > 0, s
    assert trend_slope_pct(list(reversed(up)), 20) < 0
    # 최신이 최고가면 위치 100
    assert donchian_position_pct(up, n=60) == 100.0
    assert donchian_position_pct(list(reversed(up)), n=60) == 0.0
    # 빈 입력·짧은 highs 에 죽지 않는다 (실측: 자체점검이 잡았다)
    assert donchian_position_pct([], n=5) is None
    assert donchian_position_pct(up, highs=[], lows=[], n=60) == 100.0
    assert compute_trend_pack([])["bars_used"] == 0
    print("  기울기·레인지 위치       OK")


def _check_parkinson_vs_close_vol():
    """장중 범위가 넓은데 종가가 같으면 Parkinson 이 더 크다 - 갭/장중 구분."""
    n = 40
    cl = [100.0] * n
    hi = [105.0] * n
    lo = [95.0] * n
    pk = parkinson_vol_pct(hi, lo, 20)
    cv = realized_vol_pct(cl, 20)
    assert pk and pk > 0 and cv == 0.0, (pk, cv)
    print("  Parkinson vs 종가변동성  OK")


def _check_pack_is_deterministic():
    c = _series(300)
    h = [x + 1 for x in c]
    lo = [x - 1 for x in c]
    v = [1000.0 + i for i in range(300)]
    a = compute_trend_pack(c, h, lo, v)
    b = compute_trend_pack(c, h, lo, v)
    assert a == b, "같은 입력에 다른 출력"
    # 실제로 지표가 늘었는지 - 확정치 풀이 넓어져야 의미가 있다
    nums = [k for k, x in a.items() if isinstance(x, (int, float))]
    assert len(nums) >= 15, f"지표가 {len(nums)}개뿐이다: {sorted(nums)}"
    assert a["unavailable"] == [], a["unavailable"]
    print(f"  결정론·지표수({len(nums)})       OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{INDICATORS_VERSION} 자체 점검 (네트워크 없음)")
    _check_insufficient_is_none()
    _check_rsi_bounds_and_direction()
    _check_macd_sign_follows_trend()
    _check_bollinger_and_vol_regime()
    _check_adx_detects_trend()
    _check_slope_and_position()
    _check_parkinson_vs_close_vol()
    _check_pack_is_deterministic()
    print("지표 모듈 8개 영역 통과.")
