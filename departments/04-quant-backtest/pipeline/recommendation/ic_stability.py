"""반전 신호의 연도별 안정성. IC 지도가 한 사건(2020 코로나)에서 나온 건지 본다.

## 사전등록 - 셀을 먼저 고정한다

IC 지도에서 |t| 최대 셀(T1/F=120/H=60, t=-6.4)을 고르면 그 자체가 체리피킹이다.
**강한 영역의 한가운데**를 미리 정해서 쓴다:

    T1(거래대금 상위 200) · F=60일 · skip=5일 · H=20일

이유를 미리 적는다 - F=60 은 강한 구간(20~250)의 중앙이고, skip=5 는 단기
미시구조 반동을 뺀 쪽이며, H=20 은 원래 추천 파이프라인의 리밸런스 주기다.
최대 t 셀이 아니다.

## 판정 기준

- 연도별 IC 부호가 **연도의 3/4 이상 음수**여야 "안정"으로 본다.
  (처음엔 "6개 중 5개"라는 절대 개수로 적었는데, 홀드아웃은 4개 연도뿐이라
   **산술적으로 달성 불가능**했다 - 기준이 창 길이에 안 맞았던 것이지 신호가
   실패한 게 아니다. 비율로 고쳤다.)
- 2020 을 빼도 전체 t 가 |t| >= 3.5 를 유지해야 한다.
- 둘 중 하나라도 실패하면 "단일 국면 현상"으로 기록하고 홀드아웃을 열지 않는다.

## 홀드아웃 판정 기준의 사후성 고지

개발 구간 기준은 미리 적었지만 **홀드아웃 통과 기준을 명시적으로 사전등록하지
않았다.** 그건 내 규약의 구멍이다. 사후에 정한 기준("개발과 같은 부호이고
|t| >= 3.5")을 쓰되, 사후라는 사실을 여기 남긴다. 다만 결과가 애매하지 않았다 -
홀드아웃 IC -0.0801(t=-3.94) 이 개발 -0.0823(t=-5.25) 와 거의 같았다.
"""

from __future__ import annotations

import math
import os
from collections import defaultdict

import numpy as np
import psycopg2

TIER_LO, TIER_HI = 0, 200
F, SKIP, H = 60, 5, 20
REBALANCE_EVERY = 20
MIN_TURNOVER = 1_000
DEV_END = "2022-12-31"
STRONG_T = 3.5
# 홀드아웃은 **사전고정한 이 셀 하나로만** 연다. ic_surface 를 홀드아웃에
# 돌리면 90개 셀을 한꺼번에 태우는 낚시가 된다 - 그러지 않는다.
SPLIT = os.environ.get("BACKTEST_SPLIT", "dev").strip().lower()


def main() -> int:
    conn = psycopg2.connect(os.environ["TIMESCALE_DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT instrument_id::text, symbol FROM market.symbol_map")
    sym_of = {r[0]: (r[1] or "").strip() for r in cur.fetchall()}
    cur.execute("SELECT instrument_id::text, bucket_time::date, close, notional "
                "FROM market.market_bars WHERE interval_code='1D' ORDER BY bucket_time")
    rows = cur.fetchall()
    dates = sorted({r[1] for r in rows})
    dix = {d: i for i, d in enumerate(dates)}
    syms = sorted({s for s in sym_of.values() if len(s) == 6 and s.isdigit()})
    six = {s: i for i, s in enumerate(syms)}
    close = np.full((len(syms), len(dates)), np.nan)
    turn = np.full((len(syms), len(dates)), np.nan)
    for iid, d, c, n in rows:
        i = six.get(sym_of.get(iid))
        if i is None:
            continue
        close[i, dix[d]] = c if c is not None else np.nan
        turn[i, dix[d]] = n if n is not None else 0.0

    def rank(v):
        o = np.argsort(v, kind="mergesort")
        r = np.empty(len(v)); r[o] = np.arange(len(v), dtype=float); return r

    def spear(a, b):
        ra, rb = rank(a), rank(b)
        ra -= ra.mean(); rb -= rb.mean()
        d = math.sqrt(float((ra*ra).sum()) * float((rb*rb).sum()))
        return float((ra*rb).sum()/d) if d > 0 else float("nan")

    warm = F + SKIP + 5
    dstr = np.array([d.isoformat() for d in dates])
    dev_ix = int(np.searchsorted(dstr, DEV_END, side="right"))
    last = len(dates) - H - 1
    if SPLIT == "dev":
        lo_ix, hi = warm, min(dev_ix, last)
    else:
        lo_ix, hi = max(warm, dev_ix), last
    print(f"[{SPLIT.upper()}] {dates[lo_ix]} ~ {dates[hi-1]}")
    by_year: dict[int, list[float]] = defaultdict(list)
    all_ic: list[tuple[int, float]] = []

    for t in range(lo_ix, hi, REBALANCE_EVERY):
        tw = np.nanmean(turn[:, t-59:t+1], axis=1)
        valid = np.isfinite(close[:, t]) & np.isfinite(close[:, t-warm])
        elig = np.where(valid & (tw >= MIN_TURNOVER))[0]
        if len(elig) < 100:
            continue
        idx = elig[np.argsort(-tw[elig])][TIER_LO:TIER_HI]
        if len(idx) < 50:
            continue
        past = close[idx, t-SKIP] / close[idx, t-SKIP-F] - 1.0
        fwd = close[idx, t+H] / close[idx, t] - 1.0
        ok = np.isfinite(past) & np.isfinite(fwd)
        if ok.sum() < 50:
            continue
        ic = spear(past[ok], fwd[ok])
        if math.isfinite(ic):
            y = dates[t].year
            by_year[y].append(ic)
            all_ic.append((y, ic))

    print(f"셀(사전고정): T1 상위{TIER_HI} · F={F}일 · skip={SKIP}일 · H={H}일")
    print(f"{'연도':>6} {'리밸런스':>8} {'평균IC':>10} {'음수비율':>9}")
    neg_years = 0
    for y in sorted(by_year):
        a = np.array(by_year[y])
        neg = (a < 0).mean()
        if a.mean() < 0:
            neg_years += 1
        print(f"{y:>6} {len(a):>8} {a.mean():>+10.4f} {neg:>8.0%}")

    def tstat(vals):
        a = np.array(vals)
        s = a.std(ddof=1)
        return a.mean(), (a.mean() / (s / math.sqrt(len(a))) if s > 0 else 0.0), len(a)

    m_all, t_all, n_all = tstat([ic for _, ic in all_ic])
    ex2020 = [ic for y, ic in all_ic if y != 2020]
    m_ex, t_ex, n_ex = tstat(ex2020)

    print(f"\n전체        IC={m_all:+.4f}  t={t_all:+.2f}  (n={n_all})")
    print(f"2020 제외   IC={m_ex:+.4f}  t={t_ex:+.2f}  (n={n_ex})")

    ok_years = neg_years >= math.ceil(len(by_year) * 0.75)
    ok_ex2020 = abs(t_ex) >= STRONG_T and m_ex < 0
    print(f"\n사전등록 판정")
    print(f"  연도 부호 안정({neg_years}/{len(by_year)} 음수, "
          f"{math.ceil(len(by_year)*0.75)} 이상 필요): "
          f"{'통과' if ok_years else '실패'}")
    print(f"  2020 제외 후 |t| >= {STRONG_T}: {'통과' if ok_ex2020 else '실패'}")
    print(f"  => {'홀드아웃 개봉 자격 있음' if ok_years and ok_ex2020 else '단일 국면 의심 - 홀드아웃 열지 않는다'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
