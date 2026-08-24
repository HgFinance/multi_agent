"""1층 선별 - 유니버스 전 종목을 일봉만으로 채점한다.

LS 수급 조회는 초당 1건 · 하루 2,000회라 2,694종목에는 못 쓴다(45분 + 캡 초과).
그래서 전 종목에 돌릴 수 있는 축은 일봉 기반 모멘텀뿐이고, 수급·공매도·밸류는
여기서 좁힌 후보에만 붙인다. 이 파일은 그 1층만 한다.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2

from instrument_scoring import (
    PLAN_OK,
    momentum_axis_batch,
    momentum_features,
    price_plan,
)

LOOKBACK_BARS = 130
# 60일 평균 일거래대금 하한. market_bars.notional 단위는 **백만원**이다
# (실측: 삼성전자 8/20 = 7,703,214 = 7.7조원, 유니버스 중앙값 574 = 5.7억원).
# 원으로 착각하고 500,000 을 넣었더니 2,694 중 2,674 가 잘렸다.
MIN_TURNOVER = 1_000          # 10억원/일
TOP_N = 25

BARS_SQL = """
SELECT b.instrument_id::text, b.bucket_time, b.open, b.high, b.low, b.close,
       b.volume, b.notional
FROM market.market_bars AS b
WHERE b.interval_code = '1D'
  AND b.bucket_time > now() - interval '260 days'
ORDER BY b.instrument_id, b.bucket_time
"""

SYMBOL_SQL = "SELECT instrument_id::text, symbol FROM market.symbol_map"

# 회사명은 시세 DB 에 없다(market.symbol_map 은 symbol+instrument_id 뿐).
# 2층 뉴스·공시 조회가 종목코드로는 안 되고 회사명이 필요해서 control DB 에서
# 따로 가져온다. 실패해도 1층은 그대로 돈다 - 이름은 2층에서만 쓴다.
NAME_SQL = """
SELECT s.symbol, i.display_name
FROM reference.instruments AS i
JOIN reference.instrument_symbols AS s
  ON s.instrument_id = i.instrument_id AND s.is_primary = TRUE
WHERE i.display_name IS NOT NULL
"""


def load_company_names(dsn: str) -> dict[str, str]:
    try:
        with psycopg2.connect(dsn) as c, c.cursor() as cur:
            cur.execute(NAME_SQL)
            return {r[0].strip(): r[1].strip() for r in cur if r[0] and r[1]}
    except Exception as exc:  # noqa: BLE001
        print(f"warn: 회사명 조회 실패 ({type(exc).__name__}) - 2층에서 이름이 빈다")
        return {}


def main() -> int:
    dsn = os.environ.get("TIMESCALE_DATABASE_URL") or os.environ["DATABASE_URL"]
    started = time.time()
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    cur.execute(SYMBOL_SQL)
    names = {r[0]: r[1] for r in cur.fetchall()}
    company_names = load_company_names(
        os.environ.get("CONTROL_DATABASE_URL")
        or dsn.rsplit("/", 1)[0] + "/control")

    cur.execute(BARS_SQL)
    by_instrument: dict[str, list[dict]] = defaultdict(list)
    for iid, bucket, o, h, low, c, vol, notional in cur:
        by_instrument[iid].append({
            "bucket_time": bucket, "open": o, "high": h, "low": low,
            "close": c, "volume": vol, "notional": notional,
        })
    fetch_secs = time.time() - started
    print(f"instruments with 1D bars: {len(by_instrument)}  (fetch {fetch_secs:.1f}s)")

    features: dict[str, dict] = {}
    bars_by_symbol: dict[str, list[dict]] = {}
    rejected = {"no_symbol": 0, "not_common_share": 0, "short_history": 0, "illiquid": 0}

    for iid, bars in by_instrument.items():
        symbol = (names.get(iid) or "").strip()
        if not symbol:
            rejected["no_symbol"] += 1
            continue
        # 보통주만 추천 대상으로 둔다. KRX 코드에 문자가 섞인 것은 우선주·신형
        # 우선주·신주인수권이다(예: 0015N0). 추천 자체로도 부적절하고, 하류의
        # ls_mcp_server._shcode_of 가 코드를 `\d{6}` 로만 인식해 이런 코드는
        # DART 기업명 조회로 새는데 그 색인 다운로드가 멈춰 있다(실측: 4분+ 무응답).
        if not (len(symbol) == 6 and symbol.isdigit()):
            rejected["not_common_share"] += 1
            continue
        bars = bars[-LOOKBACK_BARS:]
        f = momentum_features(bars)
        if f is None:
            rejected["short_history"] += 1
            continue
        avg_turnover = sum(float(b["notional"] or 0) for b in bars[-60:]) / 60
        if avg_turnover < MIN_TURNOVER:
            rejected["illiquid"] += 1
            continue
        f["avg_turnover"] = avg_turnover
        features[symbol] = f
        bars_by_symbol[symbol] = bars

    print(f"scored universe: {len(features)}   rejected: {rejected}")
    if not features:
        return 1

    scores = momentum_axis_batch(features)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1].value)

    print(f"\n{'rank':>4} {'symbol':>8} {'mom':>6} {'ret20':>8} {'ret60':>8} "
          f"{'stack':>5} {'turn':>5}  plan")
    print("-" * 100)
    shown = 0
    for rank, (symbol, axis) in enumerate(ranked, 1):
        if shown >= TOP_N:
            break
        d = axis.detail
        plan = price_plan(bars_by_symbol[symbol])
        if plan.status == PLAN_OK:
            plan_txt = (f"OK  진입~{plan.entry_high:,.0f} 목표 {plan.target:,.0f} "
                        f"손절 {plan.stop:,.0f} RR {plan.reward_risk}")
        else:
            plan_txt = f"{plan.status}: {plan.reason[:52]}"
        print(f"{rank:>4} {symbol:>8} {axis.value:>6.3f} "
              f"{d['ret_20']:>8.3f} {d['ret_60']:>8.3f} {d['ma_stack']:>5.0f} "
              f"{d['turnover_ratio']:>5.2f}  {plan_txt}")
        shown += 1

    # 최종 후보 = 점수 순위 안에서 가격계획이 성립하는 것만. "확신은 요청하고
    # 리스크가 처분한다"(ai-hedge-fund) - 점수가 높아도 자리가 나쁘면 뺀다.
    survivors, skipped = [], []
    for symbol, axis in ranked:
        plan = price_plan(bars_by_symbol[symbol])
        if plan.status == PLAN_OK:
            survivors.append((symbol, axis, plan))
        else:
            skipped.append({"symbol": symbol, "momentum": round(axis.value, 3),
                            "reason": plan.reason})
        if len(survivors) >= TOP_N:
            break

    payload = {
        "as_of": max(b["bucket_time"] for bl in bars_by_symbol.values()
                     for b in bl[-1:]).isoformat(),
        "universe_total": len(by_instrument),
        "universe_scored": len(features),
        "screen_rejected": rejected,
        "candidates": [
            {
                "symbol": s,
                "company": company_names.get(s, ""),
                "momentum": round(a.value, 4),
                "momentum_detail": a.detail,
                "last_close": p.last_close,
                "atr": p.atr,
                "avg_turnover_mkrw": round(features[s]["avg_turnover"]),
                "plan": {
                    "entry_low": p.entry_low, "entry_high": p.entry_high,
                    "target": p.target, "stop": p.stop,
                    "reward_risk": p.reward_risk, "risk_pct": p.risk_pct,
                    "target_basis": p.target_basis, "stop_basis": p.stop_basis,
                    "supports": [{"price": lv.price, "touches": lv.touches,
                                  "strength": lv.strength} for lv in p.supports],
                    "resistances": [{"price": lv.price, "touches": lv.touches,
                                     "strength": lv.strength} for lv in p.resistances],
                },
            }
            for s, a, p in survivors
        ],
        "skipped_before_quota": skipped,
        "company_names_loaded": len(company_names),
    }
    out_path = os.environ.get("SCREEN_OUT", "/tmp/candidates.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, default=str)

    print(f"\n최종 후보 {len(survivors)}개 "
          f"(가격계획 기각으로 건너뛴 상위 종목 {len(skipped)}개) -> {out_path}")
    print(f"총 소요 {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
