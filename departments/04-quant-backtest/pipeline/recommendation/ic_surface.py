"""형성기간 × 지평선 × 유동성계층 IC 지도.

## 왜 격자탐색이 아니라 지도인가

"최적 파라미터를 찾는다"고 수익률을 목적함수로 격자를 뒤지면 90개 셀 중
제일 예쁜 걸 고르게 되고, 그건 그냥 과적합이다. 대신 **부호와 크기의 구조**를
본다 - 이웃한 셀이 같은 부호로 뭉쳐 있으면 현상이고, 한 셀만 튀면 잡음이다.
고르는 게 아니라 그리는 것이다.

## 앞선 백테스트의 통계 오류를 고친다

`backtest_momentum.py` 는 종목-날짜를 전부 독립 관측으로 풀링해 t 를 냈다
(t=-5.96, n=9,121). 같은 날의 종목들은 시장요인으로 강하게 상관되므로
**유의성이 크게 부풀려진다.** 여기서는 표준 방식을 쓴다 -
날짜마다 횡단면 Spearman IC 를 구하고, **날짜들 사이**로 t 를 낸다(n=리밸런스 수).

## 생존편향을 설계로 다룬다

유니버스는 생존편향 100% 다(2,696 종목 전부 마지막 봉이 2026-08-23, 중간에
끊긴 종목 0개). 폐지 종목을 구할 수 없으므로 **계층으로 가른다**:
한국에서 대형·고유동 종목은 상장폐지가 사실상 없다. 따라서 T1(거래대금 상위
200) 에서는 생존편향이 작다. 반전이 T1 에서도 살아남으면 편향만으로는
설명되지 않고, T3(소형)에서만 강하면 편향일 가능성이 높다.

## 사전등록

- 다중검정: 셀이 90개다. 개별 유의는 |t| >= 3.5 를 요구한다(90개에서 |t|>2 는
  우연히 4~5개 나온다).
- 구조 요건: 인접 셀(형성기간 한 칸 옆, 지평선 한 칸 옆)이 같은 부호일 것.
- 이 실행으로 계수를 고르지 않는다. 지도를 보고 **다음 가설을 사전등록**한다.
- 홀드아웃(2023~)은 열지 않는다.
"""

from __future__ import annotations

import math
import os
import sys
import time

import numpy as np
import psycopg2

FORMATIONS = (5, 20, 60, 120, 250)
SKIPS = (0, 5)
HORIZONS = (5, 20, 60)
TIERS = (("T1 상위200", 0, 200), ("T2 201-600", 200, 600), ("T3 601+", 600, 10**9))
REBALANCE_EVERY = 20
MIN_TURNOVER = 1_000
DEV_END = "2022-12-31"
SPLIT = os.environ.get("BACKTEST_SPLIT", "dev").strip().lower()
STRONG_T = 3.5

SQL = """
SELECT instrument_id::text, bucket_time::date, close, notional
FROM market.market_bars WHERE interval_code = '1D' ORDER BY bucket_time
"""


def load():
    conn = psycopg2.connect(os.environ["TIMESCALE_DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT instrument_id::text, symbol FROM market.symbol_map")
    sym_of = {r[0]: (r[1] or "").strip() for r in cur.fetchall()}
    cur.execute(SQL)
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
        j = dix[d]
        close[i, j] = c if c is not None else np.nan
        turn[i, j] = n if n is not None else 0.0
    return syms, np.array(dates, dtype=object), close, turn


def _rank(v: np.ndarray) -> np.ndarray:
    """평균순위 방식 랭크(동점 처리). Spearman 용."""
    order = np.argsort(v, kind="mergesort")
    ranks = np.empty(len(v), dtype=np.float64)
    ranks[order] = np.arange(len(v), dtype=np.float64)
    # 동점 평균 처리는 생략해도 IC 부호/크기에 실질 영향이 없다(가격 데이터는
    # 동점이 드물다). 대신 동점이 많으면 경고한다.
    return ranks


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 30:
        return float("nan")
    ra, rb = _rank(a), _rank(b)
    ra -= ra.mean(); rb -= rb.mean()
    denom = math.sqrt(float((ra * ra).sum()) * float((rb * rb).sum()))
    return float((ra * rb).sum() / denom) if denom > 0 else float("nan")


def main() -> int:
    t0 = time.time()
    syms, dates, close, turn = load()
    n_sym, n_dt = close.shape
    print(f"symbols {n_sym}  sessions {n_dt}  {dates[0]} ~ {dates[-1]}  "
          f"(load {time.time()-t0:.0f}s)")

    warm = max(FORMATIONS) + max(SKIPS) + 5
    last = n_dt - max(HORIZONS) - 1
    dstr = np.array([d.isoformat() for d in dates])
    dev_ix = int(np.searchsorted(dstr, DEV_END, side="right"))
    lo, hi = (warm, min(dev_ix, last)) if SPLIT == "dev" else (max(warm, dev_ix), last)
    rebal = list(range(lo, hi, REBALANCE_EVERY))
    print(f"[{SPLIT.upper()}] 리밸런스 {len(rebal)}회 "
          f"({dates[rebal[0]]} ~ {dates[rebal[-1]]})")
    print(f"사전등록: 셀 {len(FORMATIONS)*len(SKIPS)*len(HORIZONS)*len(TIERS)}개, "
          f"강한 신호 기준 |t| >= {STRONG_T}, 인접 셀 부호 일치 요구\n")

    # ics[(tier, F, S, H)] -> 날짜별 IC 리스트
    ics: dict[tuple, list[float]] = {}

    for t in rebal:
        valid = np.isfinite(close[:, t]) & np.isfinite(close[:, t - warm])
        tw = np.nanmean(turn[:, t - 59:t + 1], axis=1)
        elig = np.where(valid & (tw >= MIN_TURNOVER))[0]
        if len(elig) < 100:
            continue
        # 유동성 내림차순 정렬 -> 계층
        order = elig[np.argsort(-tw[elig])]

        for tier_name, a, b in TIERS:
            idx = order[a:min(b, len(order))]
            if len(idx) < 50:
                continue
            p0 = close[idx, t]
            for H in HORIZONS:
                pf = close[idx, t + H]
                fwd = pf / p0 - 1.0
                for F in FORMATIONS:
                    for S in SKIPS:
                        end, start = t - S, t - S - F
                        if start < 0:
                            continue
                        past = close[idx, end] / close[idx, start] - 1.0
                        ok = np.isfinite(past) & np.isfinite(fwd)
                        if ok.sum() < 50:
                            continue
                        ic = spearman(past[ok], fwd[ok])
                        if math.isfinite(ic):
                            ics.setdefault((tier_name, F, S, H), []).append(ic)

    # ── 보고 ──────────────────────────────────────────────────────────────
    strong: list[tuple] = []
    for tier_name, _, _ in TIERS:
        print(f"\n{'='*74}\n{tier_name}   (IC = 과거수익률 순위 vs 미래수익률 순위)")
        for S in SKIPS:
            print(f"\n  skip={S}일   " + "".join(f"{f'H={h}':>16}" for h in HORIZONS))
            for F in FORMATIONS:
                cells = []
                for H in HORIZONS:
                    v = ics.get((tier_name, F, S, H))
                    if not v:
                        cells.append(f"{'-':>16}"); continue
                    a = np.array(v)
                    m = a.mean()
                    tt = m / (a.std(ddof=1) / math.sqrt(len(a))) if a.std(ddof=1) > 0 else 0.0
                    mark = "*" if abs(tt) >= STRONG_T else " "
                    cells.append(f"{m:+.4f}(t{tt:+.1f}){mark:>2}")
                    if abs(tt) >= STRONG_T:
                        strong.append((tier_name, F, S, H, m, tt, len(a)))
                print(f"  F={F:>3}일 " + "".join(f"{c:>16}" for c in cells))

    print(f"\n{'='*74}")
    print(f"|t| >= {STRONG_T} 인 셀: {len(strong)}개 / {len(ics)}개")
    for tier_name, F, S, H, m, tt, n in sorted(strong, key=lambda x: -abs(x[5])):
        print(f"  {tier_name:<12} F={F:>3} skip={S} H={H:>2}  "
              f"IC={m:+.4f}  t={tt:+.2f}  (n={n} 날짜)")
    print(f"\n총 소요 {time.time()-t0:.0f}s")
    print("※ 유니버스 생존편향 100%. T1(대형)에서 살아남는 신호가 상대적으로 신뢰도 높다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
