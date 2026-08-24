"""반전 신호의 **손익**을 비용 포함으로 잰다. IC 가 손익이 아니기 때문이다.

IC -0.08 은 순위 상관일 뿐이다. 실제로 돈이 되려면 (1) 벤치마크를 이겨야 하고
(2) 비용을 넘겨야 하고 (3) 낙폭이 견딜 만해야 한다. 셋 다 IC 로는 안 보인다.

## 사전등록 - 돌리기 전에 전부 박는다

**신호**: contrarian = -(60일 수익률, 최근 5일 skip). 부호·형성기간·skip 은
`ic_surface`/`ic_stability` 에서 이미 고정된 값이고 여기서 다시 고르지 않는다.

**유니버스**: 60일 평균 거래대금 상위 200(T1). 폐지 위험이 낮아 생존편향이
가장 작은 계층이다.

**포트폴리오**: contrarian 점수 상위 20종목 동일가중 롱온리. 20세션마다 전량
교체(보유 20세션). 롱온리인 이유 - 한국은 공매도 제약이 상시 변수라
숏 다리를 가정하면 체결 가능성을 과장한다.

**벤치마크**: 같은 날 T1 유니버스 **동일가중 수익률**. 0 이 아니라 이걸 이겨야
한다 - 시장이 오른 구간에서 아무 20종목이나 사도 플러스가 나온다.

**비용**: 왕복 30bp 기본(KRX 매도세 20bp + 수수료·슬리피지). 60bp 도 같이 낸다.

**판정 (개발 구간)**
  - 초과수익 평균 > 0 이고 t >= 3.5
**판정 (홀드아웃) - 이번엔 미리 적는다**
  - 초과수익 평균 > 0 이고 t >= 2.0 (표본이 절반 이하라 문턱을 낮춰 잡는다)
  - 부호가 개발과 같을 것
  - 이 둘을 다 만족해야 "재현"이고, 하나라도 실패하면 기각이다.

**금지**: 이 실행 결과로 종목 수·보유기간·유니버스 크기를 바꾸지 않는다.
바꾸고 싶으면 새 사전등록이고 개발 구간이 그만큼 닳는다.
"""

from __future__ import annotations

import math
import os

import numpy as np
import psycopg2

F, SKIP, HOLD = 60, 5, 20
TIER_N = 200
N_HOLD = 20
REBALANCE_EVERY = 20
MIN_TURNOVER = 1_000
COSTS = (0.003, 0.006)
DEV_END = "2022-12-31"
SPLIT = os.environ.get("BACKTEST_SPLIT", "dev").strip().lower()
PASS_T_DEV, PASS_T_HOLDOUT = 3.5, 2.0


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

    warm = F + SKIP + 5
    dstr = np.array([d.isoformat() for d in dates])
    dev_ix = int(np.searchsorted(dstr, DEV_END, side="right"))
    last = len(dates) - HOLD - 1
    lo, hi = ((warm, min(dev_ix, last)) if SPLIT == "dev"
              else (max(warm, dev_ix), last))

    port, bench, picks_log = [], [], []
    for t in range(lo, hi, REBALANCE_EVERY):
        tw = np.nanmean(turn[:, t-59:t+1], axis=1)
        valid = np.isfinite(close[:, t]) & np.isfinite(close[:, t-warm])
        elig = np.where(valid & (tw >= MIN_TURNOVER))[0]
        if len(elig) < 100:
            continue
        idx = elig[np.argsort(-tw[elig])][:TIER_N]
        past = close[idx, t-SKIP] / close[idx, t-SKIP-F] - 1.0
        fwd = close[idx, t+HOLD] / close[idx, t] - 1.0
        ok = np.isfinite(past) & np.isfinite(fwd)
        idx, past, fwd = idx[ok], past[ok], fwd[ok]
        if len(idx) < 50:
            continue
        # contrarian = 과거 수익률이 가장 낮은 N 종목
        pick = np.argsort(past)[:N_HOLD]
        port.append(float(fwd[pick].mean()))
        bench.append(float(fwd.mean()))
        picks_log.append((dates[t], float(past[pick].mean()), float(fwd[pick].mean())))

    p, b = np.array(port), np.array(bench)
    print(f"[{SPLIT.upper()}] 리밸런스 {len(p)}회  "
          f"({picks_log[0][0]} ~ {picks_log[-1][0]})")
    print(f"사전등록: 신호 -(F={F},skip={SKIP}) · T1 상위{TIER_N} · "
          f"상위{N_HOLD}종목 동일가중 롱온리 · 보유 {HOLD}세션")
    print(f"판정 문턱: dev t>={PASS_T_DEV}, holdout t>={PASS_T_HOLDOUT}\n")

    print(f"{'비용':>6} {'전략':>10} {'벤치':>10} {'초과':>10} {'t':>7} "
          f"{'승률':>7} {'최대낙폭':>10}")
    verdicts = {}
    for cost in COSTS:
        net = p - cost                       # 매 리밸런스 전량 교체 = 왕복 1회
        ex = net - b                         # 벤치는 매수후보유로 본다(보수적)
        t_ex = ex.mean() / (ex.std(ddof=1) / math.sqrt(len(ex))) if ex.std(ddof=1) > 0 else 0.0
        eq = np.cumprod(1 + net)
        mdd = float((eq / np.maximum.accumulate(eq) - 1).min())
        print(f"{cost*1e4:>5.0f}bp {net.mean()*100:>9.2f}% {b.mean()*100:>9.2f}% "
              f"{ex.mean()*100:>+9.2f}% {t_ex:>+7.2f} {(ex>0).mean():>6.0%} "
              f"{mdd*100:>9.1f}%")
        verdicts[cost] = (ex.mean(), t_ex)

    print(f"\n연환산 참고 (리밸런스 {HOLD}세션 ≈ 연 {250/HOLD:.1f}회)")
    for cost in COSTS:
        net = p - cost
        ann = (1 + net.mean()) ** (250 / HOLD) - 1
        ann_b = (1 + b.mean()) ** (250 / HOLD) - 1
        print(f"  {cost*1e4:.0f}bp: 전략 {ann*100:+.1f}%  벤치 {ann_b*100:+.1f}%")

    base_ex, base_t = verdicts[COSTS[0]]
    thr = PASS_T_DEV if SPLIT == "dev" else PASS_T_HOLDOUT
    ok = base_ex > 0 and base_t >= thr
    print(f"\n사전등록 판정 ({COSTS[0]*1e4:.0f}bp 기준, 문턱 t>={thr})")
    print(f"  초과수익 {base_ex*100:+.2f}%  t={base_t:+.2f}  =>  "
          f"{'통과' if ok else '불합격'}")
    print("\n※ 유니버스 생존편향 100%. 다만 T1(대형)은 폐지가 드물어 영향이 작다.")
    print("※ 벤치마크는 같은 T1 유니버스 동일가중이라 시장 상승분은 이미 빠져 있다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
