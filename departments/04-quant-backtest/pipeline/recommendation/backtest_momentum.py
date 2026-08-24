"""모멘텀 축과 가격계획을 10.6년 일봉으로 측정한다.

**이건 "이미 아는 것의 재확인"이 아니다.** 재료(ATR·이동평균·스윙 지지저항)는
표준이지만, instrument_scoring 이 쓰는 계수 30여 개는 2026-08-24 오후에 정해진
값이고 한 번도 측정된 적이 없다. 특히 `BREAKOUT_TARGET_ATR` 은 2.5 로 뒀을 때
신고가 종목이 전부 기각되길래 3.2 로 올린 값이다 - 내가 정한 관문(RR 1.5)을
통과하도록 내가 분자를 키운 것이라, 자기충족적으로 좋아 보일 수 있다.

세 가지를 답한다:
  Q1. 모멘텀 축 순위가 forward return 을 가르는가 (십분위 스프레드)
  Q2. 지지/저항 기반 목표·손절이 실제로 도달하는가 (목표 선도달 vs 손절 선도달)
  Q3. BREAKOUT_TARGET_ATR = 3.2 가 정당한가 (신고가권 실제 MFE 분포)

## 편향에 대한 정직한 고지

유니버스에 **상장폐지가 없다**(10.6년 폐지 0건 - 오늘 상장된 목록을 과거로
역채운 데이터다). 따라서 아래 수익률은 **생존편향으로 위쪽으로 치우쳐 있다.**
망한 종목이 표본에서 빠졌기 때문이고, 이 데이터로는 보정할 방법이 없다.
Q1 의 십분위 **스프레드**는 편향이 양쪽 십분위에 함께 걸려 절대수익보다는
덜 오염되지만, 그래도 깨끗하지 않다.

거래비용은 왕복 30bp 로 가정한다(2026 KRX 매도세 20bp + 수수료·슬리피지).

## 사전등록 규약 - 돌리기 전에 박는다

이 저장소는 개발 데이터를 소진한 전력이 있다(급등페이드: 소진 37세션 LCB +36.6
-> 홀드아웃 8세션 기각). 결과를 보고 계수를 고쳐 다시 돌리면 백테스트만 예뻐진다.
그래서:

1. **표본을 가른다.** 개발 구간 2016~2022, **홀드아웃 2023~현재는 보지 않는다.**
   홀드아웃은 `BACKTEST_SPLIT=holdout` 를 명시해야 열리고, 그 순간 1회 소모다.
2. **판정 기준을 미리 정한다.**
   - Q1 합격: 개발 구간 +20d D10-D1 스프레드가 **양수이고 t >= 2.0**.
     불합격이면 계수를 만지는 게 아니라 "이 모멘텀 축은 이대로는 안 된다"가 결론이다.
   - Q2 합격: 왕복비용 차감 후 평균 손익 > 0 이고 t >= 2.0.
   - Q3 는 합격/불합격이 아니라 **기술통계**다(아래 3번).
3. **`BREAKOUT_TARGET_ATR` 만 예외로 재보정한다.** 단 수익률을 목적함수로 쓰지
   않는다 - 신고가권 표본의 MFE **중앙값**으로 정한다. "손익이 좋아지는 값"이
   아니라 "실제로 절반이 도달하는 거리"다. 이 규칙을 지금 못 박고, 나온 숫자를
   그대로 쓴다(마음에 안 든다고 p60·p70 으로 갈아타지 않는다).
4. **그 외 29개 계수는 이번 라운드에서 건드리지 않는다.** 고치고 싶으면 근거가
   백테스트 숫자가 아니라 **메커니즘**이어야 하고, 고칠 때마다 개발 구간이 닳는다.
"""

from __future__ import annotations

import math
import os
import sys
import time
from collections import defaultdict

import numpy as np
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from instrument_scoring import (
    PLAN_OK,
    momentum_axis_batch,
    momentum_features,
    price_plan,
)

LOOKBACK = 130          # 피처 창 (screen_universe 와 동일)
REBALANCE_EVERY = 20    # 약 한 달
HORIZONS = (5, 10, 20)
PLAN_MAX_HOLD = 30      # 목표/손절 도달을 몇 세션까지 기다리나
MIN_TURNOVER = 1_000    # 백만원. screen_universe 와 동일
ROUND_TRIP_COST = 0.003 # 30bp
N_DECILES = 10
PLAN_TOP_N = 150        # 매 리밸런스에서 가격계획을 재는 상위 종목 수

# 표본 분할. 홀드아웃은 명시해야 열린다 - 실수로 열리면 안 된다.
DEV_END = "2022-12-31"
SPLIT = os.environ.get("BACKTEST_SPLIT", "dev").strip().lower()
if SPLIT not in {"dev", "holdout"}:
    raise SystemExit("BACKTEST_SPLIT 은 dev 또는 holdout")

# 사전등록 합격선
PASS_T = 2.0

SQL = """
SELECT b.instrument_id::text, b.bucket_time::date, b.high, b.low, b.close, b.notional
FROM market.market_bars AS b
WHERE b.interval_code = '1D'
ORDER BY b.bucket_time
"""


def load() -> tuple[list[str], np.ndarray, dict[str, np.ndarray]]:
    dsn = os.environ["TIMESCALE_DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SELECT instrument_id::text, symbol FROM market.symbol_map")
    sym_of = {r[0]: (r[1] or "").strip() for r in cur.fetchall()}

    cur.execute(SQL)
    rows = cur.fetchall()
    print(f"rows: {len(rows):,}")

    dates_sorted = sorted({r[1] for r in rows})
    date_ix = {d: i for i, d in enumerate(dates_sorted)}
    symbols = sorted({s for s in sym_of.values()
                      if len(s) == 6 and s.isdigit()})
    sym_ix = {s: i for i, s in enumerate(symbols)}

    shape = (len(symbols), len(dates_sorted))
    arr = {k: np.full(shape, np.nan, dtype=np.float64)
           for k in ("high", "low", "close", "notional")}
    for iid, d, h, lo, c, n in rows:
        s = sym_of.get(iid)
        si = sym_ix.get(s)
        if si is None:
            continue
        di = date_ix[d]
        arr["high"][si, di] = h if h is not None else np.nan
        arr["low"][si, di] = lo if lo is not None else np.nan
        arr["close"][si, di] = c if c is not None else np.nan
        arr["notional"][si, di] = n if n is not None else 0.0
    return symbols, np.array(dates_sorted, dtype=object), arr


def _bars(arr, si, lo, hi):
    """[lo, hi) 구간을 instrument_scoring 이 먹는 dict 리스트로."""
    c = arr["close"][si, lo:hi]
    h = arr["high"][si, lo:hi]
    l = arr["low"][si, lo:hi]
    n = arr["notional"][si, lo:hi]
    return [{"close": c[k], "high": h[k], "low": l[k], "notional": n[k]}
            for k in range(len(c))]


def main() -> int:
    started = time.time()
    symbols, dates, arr = load()
    n_sym, n_dt = arr["close"].shape
    print(f"symbols(보통주): {n_sym}  sessions: {n_dt}  "
          f"{dates[0]} ~ {dates[-1]}  (load {time.time()-started:.0f}s)")

    close, high, low, notional = arr["close"], arr["high"], arr["low"], arr["notional"]
    finite_close = np.isfinite(close)

    # 리밸런스 시점 - 워밍업(LOOKBACK) 이후, 가장 긴 지평선만큼 여유를 둔다.
    last_usable = n_dt - max(max(HORIZONS), PLAN_MAX_HOLD) - 1
    date_strs = np.array([d.isoformat() for d in dates])
    dev_end_ix = int(np.searchsorted(date_strs, DEV_END, side="right"))
    if SPLIT == "dev":
        lo_ix, hi_ix = LOOKBACK, min(dev_end_ix, last_usable)
    else:
        lo_ix, hi_ix = max(LOOKBACK, dev_end_ix), last_usable
    rebal = list(range(lo_ix, hi_ix, REBALANCE_EVERY))
    if not rebal:
        raise SystemExit(f"{SPLIT} 구간에 리밸런스 시점이 없다")
    print(f"[{SPLIT.upper()}] 리밸런스 {len(rebal)}회 "
          f"({dates[rebal[0]]} ~ {dates[rebal[-1]]})")
    print(f"사전등록: Q1/Q2 합격선 t >= {PASS_T}, "
          f"BREAKOUT_TARGET_ATR 는 MFE 중앙값으로만 재보정")
    print()

    # ── 누적기 ───────────────────────────────────────────────────────────
    decile_rets = {h: defaultdict(list) for h in HORIZONS}
    plan_outcome = {"target_first": 0, "stop_first": 0, "neither": 0}
    plan_pnl: list[float] = []
    breakout_mfe: list[float] = []      # ATR 단위 최대유리이동
    breakout_mae: list[float] = []
    level_target_mfe: list[float] = []
    n_planned = 0

    for t in rebal:
        window_lo = t - LOOKBACK + 1
        # 창 전체가 유효하고 유동성 문턱을 넘는 종목만
        window_ok = finite_close[:, window_lo:t + 1].all(axis=1)
        turnover = np.nanmean(notional[:, t - 59:t + 1], axis=1)
        eligible = np.where(window_ok & (turnover >= MIN_TURNOVER))[0]
        if len(eligible) < 100:
            continue

        feats = {}
        bars_cache = {}
        for si in eligible:
            b = _bars(arr, si, window_lo, t + 1)
            f = momentum_features(b)
            if f is None:
                continue
            feats[si] = f
            bars_cache[si] = b
        if len(feats) < 100:
            continue

        scores = momentum_axis_batch(feats)
        ranked = sorted(scores.items(), key=lambda kv: kv[1].value)
        n = len(ranked)

        # ── Q1: 십분위별 forward return ─────────────────────────────────
        for rank, (si, ax) in enumerate(ranked):
            d = min(N_DECILES - 1, rank * N_DECILES // n)
            p0 = close[si, t]
            for h in HORIZONS:
                p1 = close[si, t + h]
                if np.isfinite(p0) and np.isfinite(p1) and p0 > 0:
                    decile_rets[h][d].append(p1 / p0 - 1.0)

        # ── Q2/Q3: 가격계획 - 상위 PLAN_TOP_N 만 ────────────────────────
        for si, ax in ranked[::-1][:PLAN_TOP_N]:
            plan = price_plan(bars_cache[si])
            if plan.status != PLAN_OK:
                continue
            n_planned += 1
            entry = (plan.entry_low + plan.entry_high) / 2
            fwd_hi = high[si, t + 1:t + 1 + PLAN_MAX_HOLD]
            fwd_lo = low[si, t + 1:t + 1 + PLAN_MAX_HOLD]
            if not np.isfinite(fwd_hi).any():
                continue

            hit_t = np.where(fwd_hi >= plan.target)[0]
            hit_s = np.where(fwd_lo <= plan.stop)[0]
            first_t = hit_t[0] if len(hit_t) else 10**9
            first_s = hit_s[0] if len(hit_s) else 10**9
            if first_t < first_s:
                plan_outcome["target_first"] += 1
                plan_pnl.append(plan.target / entry - 1.0 - ROUND_TRIP_COST)
            elif first_s < first_t:
                plan_outcome["stop_first"] += 1
                plan_pnl.append(plan.stop / entry - 1.0 - ROUND_TRIP_COST)
            else:
                plan_outcome["neither"] += 1
                last = close[si, t + PLAN_MAX_HOLD]
                if np.isfinite(last):
                    plan_pnl.append(last / entry - 1.0 - ROUND_TRIP_COST)

            # MFE/MAE 를 ATR 단위로 - 목표 배수가 정당한지 보는 자료
            mfe = (np.nanmax(fwd_hi) - entry) / plan.atr
            mae = (entry - np.nanmin(fwd_lo)) / plan.atr
            if "저항 없음" in plan.target_basis:
                breakout_mfe.append(mfe)
                breakout_mae.append(mae)
            else:
                level_target_mfe.append(mfe)

    # ── 보고 ─────────────────────────────────────────────────────────────
    print("Q1. 모멘텀 십분위별 forward return (D1=최하위 … D10=최상위)")
    print(f"{'십분위':>6} {'표본':>8} " + " ".join(f"{f'+{h}d':>9}" for h in HORIZONS))
    for d in range(N_DECILES):
        cells = []
        for h in HORIZONS:
            v = decile_rets[h][d]
            cells.append(f"{np.mean(v)*100:>8.2f}%" if v else "       -")
        n0 = len(decile_rets[HORIZONS[0]][d])
        print(f"{'D'+str(d+1):>6} {n0:>8,} " + " ".join(cells))

    print()
    for h in HORIZONS:
        top, bot = decile_rets[h][N_DECILES - 1], decile_rets[h][0]
        if not top or not bot:
            continue
        spread = np.mean(top) - np.mean(bot)
        se = math.sqrt(np.var(top, ddof=1) / len(top) + np.var(bot, ddof=1) / len(bot))
        t_stat = spread / se if se > 0 else float("nan")
        verdict = ""
        if h == 20:
            verdict = ("  <- 사전등록 합격" if spread > 0 and t_stat >= PASS_T
                       else "  <- 사전등록 불합격")
        print(f"  +{h:>2}d  D10-D1 스프레드 {spread*100:+.2f}%p   t={t_stat:+.2f}   "
              f"(n={len(top):,}/{len(bot):,}){verdict}")

    print(f"\nQ2. 가격계획 결과 (최대 {PLAN_MAX_HOLD}세션 보유, 왕복비용 {ROUND_TRIP_COST*1e4:.0f}bp)")
    total = sum(plan_outcome.values())
    if total:
        for k, v in plan_outcome.items():
            print(f"  {k:<14} {v:>7,}  ({v/total:>5.1%})")
        arr_pnl = np.array(plan_pnl)
        print(f"  계획 수립 {n_planned:,}건, 평가 {len(arr_pnl):,}건")
        print(f"  평균 손익 {arr_pnl.mean()*100:+.2f}%   중앙값 {np.median(arr_pnl)*100:+.2f}%"
              f"   승률 {(arr_pnl > 0).mean():.1%}")
        se = arr_pnl.std(ddof=1) / math.sqrt(len(arr_pnl))
        t_plan = arr_pnl.mean() / se
        print(f"  t={t_plan:+.2f}  <- 사전등록 "
              f"{'합격' if arr_pnl.mean() > 0 and t_plan >= PASS_T else '불합격'}")
    else:
        print("  평가 가능한 계획이 없다")

    print(f"\nQ3. 목표 배수 정당성 - 진입 후 {PLAN_MAX_HOLD}세션 최대유리이동(ATR 단위)")
    for label, data in (("신고가권(ATR×3.2 목표)", breakout_mfe),
                        ("저항 기반 목표", level_target_mfe)):
        if not data:
            continue
        a = np.array(data)
        qs = np.percentile(a, [25, 50, 75, 90])
        print(f"  {label:<22} n={len(a):>6,}  중앙 {qs[1]:>5.2f}  "
              f"p25 {qs[0]:>5.2f}  p75 {qs[2]:>5.2f}  p90 {qs[3]:>5.2f}")
    if breakout_mfe:
        a = np.array(breakout_mfe)
        reach = (a >= 3.2).mean()
        med = float(np.median(a))
        print(f"  → 신고가권에서 MFE 가 현재값 3.2 ATR 이상인 비율: {reach:.1%}")
        m = np.array(breakout_mae)
        print(f"  → 같은 표본의 최대불리이동 중앙값: {np.median(m):.2f} ATR")
        print(f"  → 사전등록 재보정값(MFE 중앙값): BREAKOUT_TARGET_ATR = {med:.2f}")
        print("     (수익률을 목적함수로 쓰지 않았다 - 절반이 실제 도달하는 거리다)")

    print(f"\n총 소요 {time.time()-started:.0f}s")
    print("※ 유니버스에 상장폐지가 없어 수익률은 생존편향으로 위쪽에 치우쳐 있다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
