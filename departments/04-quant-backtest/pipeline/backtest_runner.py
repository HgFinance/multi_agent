#!/usr/bin/env python3
"""Backtest Runner v1 - PIT Dataset Manifest 로부터 재현 가능한 백테스트.

소유: 재일 (퀀트/백테스트본부, QNT-03 Backtest Engineer 직무의 결정론 부분)
근거: supabase/migrations/20260729000300 (quant.hypotheses/experiments/
      backtest_runs/backtest_trades/experiment_metrics),
      pipeline/pit_dataset.py (Manifest·Partition·content_hash),
      TEAM_JAEIL 수용 기준 "Backtest가 PIT Dataset Manifest로 재현된다",
      "Strategy Candidate가 Dataset·Code·Metric·Cost Model과 연결된다"

▶ 이 파일이 지키는 QNT-03 계약
  - **같은 (Dataset content_hash, Config, Code, Seed) -> 같은 input_hash.**
    experiments.input_hash 가 unique 라 같은 실험의 중복 등록이 DB 에서 막힌다.
  - **선견 차단이 구조다**: 시그널은 t-1 종가까지로 계산하고 체결은 t 시가다.
    같은 날 데이터로 같은 날 체결하는 경로가 코드에 없다.
  - **실패한 런도 등록한다** (status=FAILED). 성공만 남기는 것이 p-hacking 의
    시작이라 등록 후 실행, 종료 시 상태 갱신 순서를 지킨다.
  - Dataset 은 파일에서 읽되 **Partition 해시를 Manifest 와 재대조**한다 -
    파일이 변조·유실되면 실행을 거부한다(재현성이 깨진 채 돌지 않는다).
  - 비용 없는 백테스트는 없다. cost_model 이 결과 요약과 Metric 에 항상 붙는다.

▶ v1 전략: MOM-20 스모크 (모멘텀 20일 상위 N 균등, 월초 리밸런스)
  가설 검증용이 아니라 **파이프라인 관통 검증용**이다 - hypothesis 제목에
  SMOKE 라고 박는다. 진짜 가설은 QNT-01 이 Experiment Spec 으로 가져온다.

사용
  python pipeline/backtest_runner.py                    # 자체 점검 (DB 없음)
  python pipeline/backtest_runner.py --run \
      --dataset krx-basket-daily --dataset-version v1  # 실행 + 등록
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pit_dataset import DATA_ROOT, content_hash, load_partition

RUNNER_VERSION = "quant-backtest-runner-v1"
KST = timezone(timedelta(hours=9))

# v1 비용 가정 - 근거를 값 옆에 남긴다. 바꾸면 cost_model_version 을 올린다.
COST_MODEL = {
    "version": "krx-cost-v1",
    "commission_bps": 1.5,   # 위탁수수료 왕복의 절반 (매수·매도 각각 부과)
    "sell_tax_bps": 15.0,    # 증권거래세+농특세 매도측 0.15% 가정 (2026 코스피 기준)
    "slippage_bps": 5.0,     # 시가 체결 가정에 대한 보수적 슬리피지
}
DEFAULT_CONFIG = {
    "strategy": "MOM-20-SMOKE",
    "lookback_days": 20,
    "top_n": 20,
    "rebalance": "MONTH_FIRST_TRADING_DAY",
    "initial_capital": 100_000_000.0,
}

# 전략 카탈로그 - 시그널 순위 방향만 다르고 실행·비용·가드는 공유한다.
# 여기 없는 strategy 문자열은 실행을 거부한다(비슷한 걸로 대충 돌리지 않는다).
STRATEGIES = {
    "MOM-20-SMOKE": {"rank": "TOP", "note": "N일 수익률 상위 N 균등 (모멘텀)"},
    "REV-5-SMOKE": {"rank": "BOTTOM", "note": "N일 수익률 하위 N 균등 (평균회귀 롱)"},
}
REV_CONFIG = {
    "strategy": "REV-5-SMOKE",
    "lookback_days": 5,
    "top_n": 20,
    "rebalance": "EVERY_5_TRADING_DAYS",   # 가설 horizon 5일에 맞춘 주기
    "initial_capital": 100_000_000.0,
}


def code_version() -> str:
    """이 파일 자신의 해시 - 코드가 바뀌면 실험이 달라진다는 사실을 강제한다."""
    src = Path(__file__).read_bytes()
    return f"{RUNNER_VERSION}+{hashlib.sha256(src).hexdigest()[:12]}"


def input_hash(dataset_hash: str, config: dict, code_ver: str, seed: int) -> str:
    payload = json.dumps({"dataset": dataset_hash, "config": config,
                          "code": code_ver, "seed": seed,
                          "cost": COST_MODEL}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# 시장 데이터 뷰 (Dataset 행 -> 날짜/종목 격자)
# ---------------------------------------------------------------------------

@dataclass
class Market:
    dates: list[date]                       # 오름차순 거래일
    opens: dict[tuple[date, str], float]
    closes: dict[tuple[date, str], float]
    symbols: list[str]

    @classmethod
    def from_rows(cls, rows: list[dict]) -> Market:
        dates = sorted({r["trade_date"] for r in rows})
        opens, closes, symbols = {}, {}, set()
        for r in rows:
            iid = str(r["instrument_id"])
            symbols.add(iid)
            opens[(r["trade_date"], iid)] = float(r["open"])
            closes[(r["trade_date"], iid)] = float(r["close"])
        return cls(dates=dates, opens=opens, closes=closes, symbols=sorted(symbols))

    def momentum(self, until: date, lookback: int) -> dict[str, float]:
        """until **포함까지의 종가**로 lookback 수익률. 호출부가 t-1 을 넘긴다."""
        idx = [d for d in self.dates if d <= until]
        if len(idx) < lookback + 1:
            return {}
        d_now, d_then = idx[-1], idx[-1 - lookback]
        out = {}
        for s in self.symbols:
            a, b = self.closes.get((d_then, s)), self.closes.get((d_now, s))
            if a and b and a > 0:
                out[s] = b / a - 1.0
        return out


def month_first_trading_days(dates: list[date]) -> set[date]:
    seen, out = set(), set()
    for d in dates:
        if (d.year, d.month) not in seen:
            seen.add((d.year, d.month))
            out.add(d)
    return out


def rebalance_days(dates: list[date], config: dict) -> set[date]:
    """리밸런스 일자 집합 - 정책 문자열이 모르는 값이면 거부한다."""
    policy = config.get("rebalance", "MONTH_FIRST_TRADING_DAY")
    if policy == "MONTH_FIRST_TRADING_DAY":
        return month_first_trading_days(dates)
    if policy == "EVERY_5_TRADING_DAYS":
        return set(dates[::5])
    raise ValueError(f"알 수 없는 rebalance 정책: {policy!r}")


def select_targets(market: Market, i: int, config: dict) -> list[str]:
    """시그널일 t-1 종가까지로 대상 선정 - 전략은 순위 방향만 다르다."""
    strat = config["strategy"]
    if strat not in STRATEGIES:
        raise ValueError(f"카탈로그에 없는 전략: {strat!r} - 실행 거부")
    signal = market.momentum(market.dates[i - 1], int(config["lookback_days"]))
    if not signal:
        return []
    top = STRATEGIES[strat]["rank"] == "TOP"
    return sorted(signal, key=signal.get, reverse=top)[:int(config["top_n"])]


# ---------------------------------------------------------------------------
# 시뮬레이션 - 시그널 t-1 마감 / 체결 t 시가
# ---------------------------------------------------------------------------

@dataclass
class Fill:
    trade_date: date
    instrument_id: str
    side: str            # BUY / SELL
    quantity: float
    price: float         # 슬리피지 반영 체결가
    fees: float
    realized_pnl: float | None = None   # SELL 만 (FIFO)


@dataclass
class BacktestResult:
    equity: list[tuple[date, float]]
    fills: list[Fill]
    metrics: dict
    config: dict
    notes: list[str] = field(default_factory=list)


def _apply_costs(side: str, notional: float) -> float:
    bps = COST_MODEL["commission_bps"] + COST_MODEL["slippage_bps"]
    if side == "SELL":
        bps += COST_MODEL["sell_tax_bps"]
    return notional * bps / 1e4



def buy_and_hold_equity(market: Market, config: dict) -> list[tuple[date, float]]:
    """동일가중 매수 후 보유 벤치마크. **같은 PIT 데이터·같은 비용 모델.**

    ▶ 왜 필요한가 (2026-08-04 실측에서 드러난 공백)
      백테스트가 "+26.30%, Sharpe 0.4371" 을 냈는데 **그게 좋은 건지 나쁜 건지
      판단할 기준이 없었다.** 같은 기간 시장이 +40% 였으면 그 전략은 가치를
      파괴한 것이다. 리서치본부가 이미 지키는 원칙과 같다 -
      "비교 기준 없는 단일 값은 판정이 아니다".

    ▶ 공정하게 비교하기 위한 규칙
      · 같은 Market(같은 PIT Manifest) 을 쓴다 - 다른 데이터로 비교하면 무의미
      · **진입 비용을 벤치마크에도 물린다** - 무비용 벤치마크와 비교하면
        전략이 부당하게 불리해진다
      · 첫날 상장돼 있던 종목만 동일가중으로 산다. 중간 신규 상장을 사후에
        편입하면 그게 look-ahead 다
      · 리밸런싱 없음 - 그것이 buy-and-hold 의 정의다(회전율 0)
    """
    if not market.dates:
        return []
    d0 = market.dates[0]
    # Market 은 (date, symbol) -> price 평면 dict 다. 첫날 종가가 있는 종목만.
    day0 = {s_: market.closes[(d0, s_)] for s_ in market.symbols
            if market.closes.get((d0, s_))}
    names = sorted(k for k, v in day0.items() if v and v > 0)
    if not names:
        return []

    capital = float(config["initial_capital"])
    per = capital / len(names)
    shares: dict[str, float] = {}
    spent = 0.0
    for s_ in names:
        px = day0[s_]
        # 비용을 먼저 떼고 남은 돈으로 산다(전략과 같은 방식)
        fee = _apply_costs("BUY", per)
        q = max((per - fee) / px, 0.0)
        shares[s_] = q
        spent += q * px + fee
    cash = capital - spent

    out: list[tuple[date, float]] = []
    last: dict[str, float] = dict(day0)
    for d in market.dates:
        for k in shares:
            v = market.closes.get((d, k))
            if v and v > 0:
                last[k] = v
        out.append((d, cash + sum(q * last.get(k, 0.0) for k, q in shares.items())))
    return out


def excess_metrics(strategy_equity: list[tuple[date, float]],
                   bench_equity: list[tuple[date, float]]) -> dict:
    """전략 - 벤치마크. **초과가 없으면 그 전략은 존재 이유가 없다.**

    행이 비면 0 이 아니라 None 이다 - 계산 못 한 것과 초과가 0 인 것은 다르다.
    """
    if len(strategy_equity) < 2 or len(bench_equity) < 2:
        return {}          # 계산 못 했으면 **키를 안 만든다** (0 이 아니다)

    def _total(eq):
        a, b = eq[0][1], eq[-1][1]
        return None if not a else (b / a - 1.0)

    st, bt = _total(strategy_equity), _total(bench_equity)
    if st is None or bt is None:
        return {}          # 기준 자산이 0 - 판정 불가지 초과 0 이 아니다

    # 일별 초과수익의 변동성으로 정보비율. 날짜를 맞춰 곱셈오차를 막는다
    bmap = dict(bench_equity)
    diffs = []
    prev_s = prev_b = None
    for d, sv in strategy_equity:
        bv = bmap.get(d)
        if bv is None or not sv or not bv:
            continue
        if prev_s and prev_b:
            diffs.append((sv / prev_s - 1.0) - (bv / prev_b - 1.0))
        prev_s, prev_b = sv, bv

    ir = None
    if len(diffs) > 20:
        mu = sum(diffs) / len(diffs)
        var = sum((x - mu) ** 2 for x in diffs) / (len(diffs) - 1)
        sd = var ** 0.5
        if sd > 0:
            ir = round(mu / sd * (252 ** 0.5), 4)

    # ▶ **수치만 담는다.** metrics 는 numeric 컬럼에 그대로 적재되므로
    #   불리언·문자열을 섞으면 DatatypeMismatch 로 실행이 죽는다(실측).
    #   판정은 excess_return_pct 부호가 이미 말한다 - 별도 플래그를 수치
    #   테이블에 억지로 넣지 않는다.
    out = {
        "benchmark_total_return_pct": round(bt * 100.0, 2),
        # 초과가 음수면 숨기지 않는다 - 절대수익이 양수여도 시장을 못 이기면
        # 그 전략은 채택할 이유가 없다
        "excess_return_pct": round((st - bt) * 100.0, 2),
        "excess_days_used": float(len(diffs)),
    }
    if ir is not None:
        out["information_ratio"] = ir       # 표본 부족이면 키를 아예 안 넣는다
    return out


def run_backtest(market: Market, config: dict) -> BacktestResult:
    _lookback = int(config["lookback_days"])
    _top_n = int(config["top_n"])
    capital = float(config["initial_capital"])

    cash = capital
    shares: dict[str, float] = {}
    lots: dict[str, deque] = {}             # FIFO (qty, price+수수료 반영 안 한 체결가)
    last_close: dict[str, float] = {}
    fills: list[Fill] = []
    equity: list[tuple[date, float]] = []
    notes: list[str] = []
    rebal_days = rebalance_days(market.dates, config)
    # 웜업 무거래 계약 (walk-forward 창 독립성) - 이 날짜 전에는 리밸런스 금지
    ntb = config.get("no_trade_before")
    if isinstance(ntb, str):
        ntb = date.fromisoformat(ntb)
    traded_notional = 0.0

    for i, d in enumerate(market.dates):
        # ── 리밸런스: 시그널은 어제까지, 체결은 오늘 시가 ──
        if d in rebal_days and i > 0 and (ntb is None or d >= ntb):
            ranked = select_targets(market, i, config)
            if ranked:
                tradable = [s for s in ranked if (d, s) in market.opens]
                port_value = cash + sum(
                    q * last_close.get(s, 0.0) for s, q in shares.items())
                target_value = (port_value / len(tradable)) if tradable else 0.0

                # 매도 먼저 (현금 확보) - 대상에서 빠졌거나 초과 보유분
                for s in list(shares):
                    px = market.opens.get((d, s))
                    if px is None:
                        continue            # 오늘 시세 없음 - 보유 유지 (아래 노트)
                    want = target_value / px if s in tradable else 0.0
                    diff = shares[s] - want
                    if diff * px > 1.0:     # 1원 미만 잔차는 무시
                        exec_px = px * (1 - COST_MODEL["slippage_bps"] / 1e4)
                        notional = diff * exec_px
                        fee = _apply_costs("SELL", notional)
                        pnl = _fifo_sell(lots.setdefault(s, deque()), diff, exec_px) - fee
                        cash += notional - fee
                        shares[s] -= diff
                        if shares[s] <= 1e-9:
                            del shares[s]
                        fills.append(Fill(d, s, "SELL", diff, exec_px, fee, pnl))
                        traded_notional += abs(notional)
                # 매수
                for s in tradable:
                    px = market.opens[(d, s)]
                    exec_px = px * (1 + COST_MODEL["slippage_bps"] / 1e4)
                    want = target_value / px
                    diff = want - shares.get(s, 0.0)
                    if diff * px > 1.0:
                        notional = diff * exec_px
                        fee = _apply_costs("BUY", notional)
                        if notional + fee > cash:
                            diff = max((cash - fee) / exec_px, 0.0)
                            notional = diff * exec_px
                        if diff > 0:
                            cash -= notional + fee
                            shares[s] = shares.get(s, 0.0) + diff
                            lots.setdefault(s, deque()).append((diff, exec_px))
                            fills.append(Fill(d, s, "BUY", diff, exec_px, fee))
                            traded_notional += abs(notional)

        # ── 평가 (종가, 시세 없으면 직전 종가 유지) ──
        for s in shares:
            c = market.closes.get((d, s))
            if c is not None:
                last_close[s] = c
        equity.append((d, cash + sum(q * last_close.get(s, 0.0)
                                     for s, q in shares.items())))

    held_wo_price = [s for s in shares if (market.dates[-1], s) not in market.closes]
    if held_wo_price:
        notes.append(f"기말에 시세 없는 보유 {len(held_wo_price)}종목 - 직전 종가 평가")

    metrics = compute_metrics(equity, fills, capital, traded_notional)
    # ▶ 벤치마크 대비를 **항상** 붙인다. 절대수익만 보면 시장이 오른 것을
    #   전략의 실력으로 착각한다.
    bench_note = "benchmark=equal_weight_buy_and_hold (동일 PIT·동일 비용)"
    try:
        ex = excess_metrics(equity, buy_and_hold_equity(market, config))
        metrics.update(ex)
        if not ex:
            bench_note = "benchmark 계산 불가 - 곡선 부족 또는 기준 자산 0"
    except Exception as e:  # noqa: BLE001
        bench_note = f"benchmark 계산 실패: {type(e).__name__}"
    notes.append(bench_note)
    return BacktestResult(equity=equity, fills=fills, metrics=metrics,
                          config=config, notes=notes)


def _fifo_sell(q: deque, qty: float, price: float) -> float:
    """FIFO 실현손익 (수수료 제외분). 랏이 모자라면 있는 만큼만 - 남기지 않는다."""
    pnl, remain = 0.0, qty
    while remain > 1e-12 and q:
        lot_qty, lot_px = q[0]
        take = min(remain, lot_qty)
        pnl += take * (price - lot_px)
        remain -= take
        if take >= lot_qty - 1e-12:
            q.popleft()
        else:
            q[0] = (lot_qty - take, lot_px)
    return pnl


def compute_metrics(equity: list[tuple[date, float]], fills: list[Fill],
                    capital: float, traded_notional: float) -> dict:
    values = [v for _, v in equity]
    if len(values) < 2:
        return {"error": "구간이 너무 짧다"}
    rets = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values))]
    years = max((equity[-1][0] - equity[0][0]).days / 365.25, 1e-9)
    total = values[-1] / values[0] - 1.0
    cagr = (values[-1] / values[0]) ** (1 / years) - 1.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
    ann_vol = math.sqrt(var) * math.sqrt(252)
    sharpe = (mean * 252) / ann_vol if ann_vol > 1e-12 else 0.0
    peak, mdd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    sells = [f for f in fills if f.side == "SELL" and f.realized_pnl is not None]
    wins = sum(1 for f in sells if f.realized_pnl > 0)
    total_fees = sum(f.fees for f in fills)
    return {
        "total_return": round(total, 6), "cagr": round(cagr, 6),
        "ann_vol": round(ann_vol, 6), "sharpe_rf0": round(sharpe, 4),
        "max_drawdown": round(mdd, 6),
        "turnover_total": round(traded_notional / capital, 4),
        "n_fills": len(fills), "n_sells": len(sells),
        "sell_win_rate": round(wins / len(sells), 4) if sells else None,
        "total_fees": round(total_fees, 2),
        "final_equity": round(values[-1], 2),
    }


# ---------------------------------------------------------------------------
# Dataset 로드 + 무결성 재검증
# ---------------------------------------------------------------------------

def load_dataset(conn, name: str, version: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            select dataset_id, universe_version_id, content_hash, row_count
            from quant.dataset_manifests where name = %s and version = %s
            """, (name, version))
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"Manifest 없음: {name}/{version} - 먼저 --build")
        dataset_id, universe_id, chash, row_count = row
        cur.execute(
            """
            select partition_key, object_path, content_hash
            from quant.dataset_partitions where dataset_id = %s
            order by partition_key
            """, (dataset_id,))
        parts = cur.fetchall()

    rows: list[dict] = []
    for key, path, phash in parts:
        chunk = load_partition(DATA_ROOT.parent / path)
        got = content_hash(chunk)
        if got != phash:
            raise RuntimeError(
                f"Partition {key} 해시 불일치 - 파일 변조/유실. 재현성이 깨진 채 "
                f"돌지 않는다 (manifest {phash[:12]}… vs 파일 {got[:12]}…)")
        rows.extend(chunk)
    if content_hash(rows) != chash or len(rows) != row_count:
        raise RuntimeError("Dataset 전체 해시/행수 불일치 - --build 로 재생성할 것")
    return str(dataset_id), str(universe_id), chash, rows


# ---------------------------------------------------------------------------
# 등록 (hypothesis -> experiment -> run -> trades/metrics)
# ---------------------------------------------------------------------------

def register_and_run(name: str, version: str, *, seed: int = 0,
                     config: dict | None = None,
                     hypothesis_id: str | None = None) -> dict:
    """백테스트 등록·실행. hypothesis_id 를 주면 그 가설에 실험을 묶는다
    (오케스트레이터 경로) - 없으면 SMOKE 가설을 만든다(단독 실행 경로).

    반환: {"status": 0|중복0, "experiment_id", "backtest_run_id", "metrics",
           "input_hash", "duplicate": bool}
    """
    import psycopg2

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "01-research" / "collectors"))
    from source_registry import load_project_env

    env = load_project_env()
    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    config = dict(config or DEFAULT_CONFIG)
    code_ver = code_version()
    trace = str(uuid.uuid4())
    try:
        dataset_id, universe_id, dhash, rows = load_dataset(conn, name, version)
        ihash = input_hash(dhash, config, code_ver, seed)

        with conn.cursor() as cur:
            if hypothesis_id is None:
                # SMOKE 가설 - 파이프라인 관통 검증용임을 제목에 박는다
                cur.execute(
                    """
                    insert into quant.hypotheses
                      (title, rationale, expected_edge, falsification_criteria,
                       required_data_products, status, created_by, trace_id)
                    values (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, 'TESTING', %s, %s)
                    returning hypothesis_id
                    """,
                    ("[SMOKE] MOM-20 파이프라인 관통 검증",
                     ("전략 가설이 아니라 Dataset->Experiment->Run->Ledger 체인의 "
                     "재현성 검증이 목적이다. 결과 수치로 전략 판단을 하지 않는다."),
                     json.dumps({"type": "none", "note": "smoke"}),
                     json.dumps({"note": "해시 재검증 실패 또는 비결정성 발견 시 기각"}),
                     json.dumps([f"{name}/{version}"]), RUNNER_VERSION, trace))
                hyp_id = str(cur.fetchone()[0])
            else:
                hyp_id = hypothesis_id

            cur.execute(
                """
                insert into quant.experiments
                  (hypothesis_id, dataset_id, code_version, config, seed,
                   split_policy, cost_model_version, status, input_hash, trace_id,
                   started_at)
                values (%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, 'RUNNING', %s, %s, now())
                on conflict (input_hash) do nothing
                returning experiment_id
                """,
                (hyp_id, dataset_id, code_ver, json.dumps(config), seed,
                 json.dumps({"policy": "single-window", "note": "v1 스모크 - "
                             "walk-forward 는 QNT-04 몫으로 후속"}),
                 COST_MODEL["version"], ihash, trace))
            got = cur.fetchone()
            if got is None:
                conn.rollback()
                print(f"같은 input_hash 의 실험이 이미 있다({ihash[:16]}…) - "
                      f"재실행은 같은 결과라 등록하지 않는다 (재현성 계약)", flush=True)
                cur2 = conn.cursor()
                cur2.execute("select experiment_id from quant.experiments "
                             "where input_hash=%s", (ihash,))
                prev = cur2.fetchone()
                return {"status": 0, "duplicate": True, "input_hash": ihash,
                        "experiment_id": str(prev[0]) if prev else None}
            exp_id = str(got[0])
        conn.commit()

        market = Market.from_rows(rows)
        run_row_id = None
        try:
            result = run_backtest(market, config)
            status = "COMPLETED"
        except Exception as e:
            # 실패한 런도 등록한다 - 성공만 남기지 않는다
            with conn.cursor() as cur:
                cur.execute("update quant.experiments set status='FAILED', ended_at=now() "
                            "where experiment_id=%s", (exp_id,))
                cur.execute(
                    """
                    insert into quant.backtest_runs
                      (experiment_id, dataset_id, universe_version_id, start_date,
                       end_date, initial_capital, cost_model, risk_model,
                       result_summary, code_version, input_hash, status)
                    values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,'FAILED')
                    """,
                    (exp_id, dataset_id, universe_id, market.dates[0], market.dates[-1],
                     config["initial_capital"], json.dumps(COST_MODEL),
                     json.dumps({"note": "v1 - 리스크 모델 없음(롱온리 균등)"}),
                     json.dumps({"error": str(e)[:400]}), code_ver, ihash))
            conn.commit()
            raise

        summary = {
            "metrics": result.metrics,
            "notes": result.notes,
            "reproduce": f"python pipeline/backtest_runner.py --run --dataset {name} "
                         f"--dataset-version {version}",
            "dataset_content_hash": dhash,
        }
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into quant.backtest_runs
                  (experiment_id, dataset_id, universe_version_id, start_date,
                   end_date, initial_capital, cost_model, risk_model,
                   result_summary, code_version, input_hash, status)
                values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s)
                returning backtest_run_id
                """,
                (exp_id, dataset_id, universe_id, market.dates[0], market.dates[-1],
                 config["initial_capital"], json.dumps(COST_MODEL),
                 json.dumps({"note": "v1 - 리스크 모델 없음(롱온리 균등)"}),
                 json.dumps(summary, ensure_ascii=False), code_ver, ihash, status))
            run_row_id = str(cur.fetchone()[0])

            for f in result.fills:
                cur.execute(
                    """
                    insert into quant.backtest_trades
                      (backtest_run_id, instrument_id, opened_at, side, quantity,
                       open_price, fees, realized_pnl, signal_ref)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    """,
                    (run_row_id, f.instrument_id,
                     datetime.combine(f.trade_date, datetime.min.time(), tzinfo=KST),
                     f.side, round(f.quantity, 6), round(f.price, 4),
                     round(f.fees, 4),
                     None if f.realized_pnl is None else round(f.realized_pnl, 4),
                     json.dumps({"strategy": config["strategy"],
                                 "signal_date": "t-1 close", "exec": "t open"})))
            for k, v in result.metrics.items():
                if isinstance(v, (int, float)) and v is not None:
                    cur.execute(
                        """
                        insert into quant.experiment_metrics
                          (experiment_id, split, metric, value, cost_model_version)
                        values (%s, 'TEST', %s, %s, %s)
                        on conflict (experiment_id, split, metric, dimensions) do nothing
                        """, (exp_id, k, v, COST_MODEL["version"]))
            cur.execute("update quant.experiments set status='COMPLETED', ended_at=now() "
                        "where experiment_id=%s", (exp_id,))
        conn.commit()

        m = result.metrics
        print(f"{RUNNER_VERSION}: {name}/{version} 완료 (run {run_row_id[:8]}…)", flush=True)
        print(f"  {market.dates[0]} ~ {market.dates[-1]} | {len(market.symbols)}종목 | "
              f"체결 {m['n_fills']} | 수수료 {m['total_fees']:,.0f}원", flush=True)
        print(f"  수익률 {m['total_return']:+.2%} (CAGR {m['cagr']:+.2%}) | "
              f"변동성 {m['ann_vol']:.2%} | Sharpe {m['sharpe_rf0']} | "
              f"MDD {m['max_drawdown']:.2%} | 회전 {m['turnover_total']}x", flush=True)
        if result.notes:
            for n in result.notes:
                print(f"  ⚠ {n}", flush=True)
        print(f"  input_hash {ihash[:16]}… (같은 입력 = 같은 해시 = 중복 등록 차단)",
              flush=True)
        return {"status": 0, "duplicate": False, "experiment_id": exp_id,
                "backtest_run_id": run_row_id, "metrics": m, "input_hash": ihash,
                "dataset_id": dataset_id, "dataset_hash": dhash}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 자체 점검 - DB 없음
# ---------------------------------------------------------------------------

def _mk_market(prices: dict[str, list[float]], start=date(2026, 1, 5)) -> Market:
    """영업일 연속 가정의 합성 시장. open = 전일 close 유지가 아니라 당일 close 와 동일."""
    n = len(next(iter(prices.values())))
    dates = []
    d = start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    rows = []
    for s, series in prices.items():
        for i, px in enumerate(series):
            rows.append({"instrument_id": s, "trade_date": dates[i],
                         "open": px, "high": px, "low": px, "close": px,
                         "volume": 1000, "observed_at": datetime.now(timezone.utc)})
    return Market.from_rows(rows)


def _check_no_lookahead():
    """t 당일 급등 정보로 t 에 선택하면 안 된다 - 시그널은 t-1 까지."""
    n = 25
    flat = [100.0] * n
    spike = [100.0] * (n - 1) + [200.0]      # 마지막 날에만 급등
    m = _mk_market({"FLAT": flat, "SPIKE": spike})
    sig = m.momentum(m.dates[-2], 20)         # t-1 까지: 급등 미반영
    assert abs(sig["SPIKE"]) < 1e-9, "t-1 시그널에 t 정보가 새어 들어왔다"
    sig2 = m.momentum(m.dates[-1], 20)        # t 까지 보면 반영 (함수 자체 검증)
    assert sig2["SPIKE"] > 0.9
    print("  선견 차단(t-1 시그널)    OK")


def _check_fifo_and_costs():
    q = deque([(10.0, 100.0), (5.0, 110.0)])
    pnl = _fifo_sell(q, 12.0, 120.0)          # 10@100 + 2@110 매도
    assert abs(pnl - (10 * 20 + 2 * 10)) < 1e-9
    assert len(q) == 1 and abs(q[0][0] - 3.0) < 1e-9
    buy = _apply_costs("BUY", 1_000_000)      # 1.5 + 5 bps
    sell = _apply_costs("SELL", 1_000_000)    # + 세금 15 bps
    assert abs(buy - 650.0) < 1e-9 and abs(sell - 2150.0) < 1e-9
    print("  FIFO 손익·비용 산식      OK")


def _check_metrics_and_determinism():
    up = {"A": [100 * 1.001 ** i for i in range(300)],
          "B": [100.0] * 300}
    m = _mk_market(up)
    r1 = run_backtest(m, dict(DEFAULT_CONFIG, top_n=1, initial_capital=1e8))
    r2 = run_backtest(m, dict(DEFAULT_CONFIG, top_n=1, initial_capital=1e8))
    assert r1.metrics == r2.metrics, "같은 입력이 다른 결과를 냈다 - 비결정성"
    assert r1.metrics["total_return"] > 0    # 상승 종목 하나를 고르는 구성
    assert r1.metrics["max_drawdown"] <= 0
    h1 = input_hash("d", DEFAULT_CONFIG, "c", 0)
    assert h1 == input_hash("d", dict(DEFAULT_CONFIG), "c", 0)
    assert h1 != input_hash("d", dict(DEFAULT_CONFIG, top_n=10), "c", 0)
    print("  결정성·Metric·input_hash OK")


def _check_strategy_catalog():
    """REV-5(평균회귀)가 하락 종목을 고르고, 모르는 전략·정책은 거부되는지."""
    n = 40
    m = _mk_market({"UP": [100 * 1.01 ** i for i in range(n)],
                    "DOWN": [100 * 0.99 ** i for i in range(n)]})
    i = len(m.dates) - 1
    assert select_targets(m, i, dict(REV_CONFIG, top_n=1)) == ["DOWN"]
    assert select_targets(m, i, dict(DEFAULT_CONFIG, top_n=1)) == ["UP"]
    try:
        select_targets(m, i, dict(DEFAULT_CONFIG, strategy="XXX"))
        raise AssertionError("카탈로그 밖 전략이 실행됐다")
    except ValueError:
        pass
    try:
        rebalance_days(m.dates, {"rebalance": "EVERY_FULL_MOON"})
        raise AssertionError("모르는 리밸런스 정책이 통과했다")
    except ValueError:
        pass
    assert rebalance_days(m.dates[:8], {"rebalance": "EVERY_5_TRADING_DAYS"}) \
        == {m.dates[0], m.dates[5]}
    r1 = run_backtest(m, dict(REV_CONFIG, top_n=1, initial_capital=1e6))
    r2 = run_backtest(m, dict(REV_CONFIG, top_n=1, initial_capital=1e6))
    assert r1.metrics == r2.metrics, "REV-5 가 비결정적이다"
    # no_trade_before(웜업 무거래 계약): 그 전 체결 0 - WF 실측 137건 재발 방지
    cut = m.dates[20]
    r3 = run_backtest(m, dict(REV_CONFIG, top_n=1, initial_capital=1e6,
                              no_trade_before=cut.isoformat()))
    assert r1.fills and r1.fills[0].trade_date < cut      # 기본은 cut 전에 체결 시작
    assert r3.fills and all(f.trade_date >= cut for f in r3.fills), \
        "no_trade_before 위반 - WF 웜업 체결 137건 실측의 재발 방지"
    print("  전략 카탈로그(REV-5)     OK")


def _check_cash_never_negative():
    prices = {s: [100.0 + (i % 7) for i in range(60)] for s in ("A", "B", "C")}
    m = _mk_market(prices)
    r = run_backtest(m, dict(DEFAULT_CONFIG, top_n=3, initial_capital=1_000_000))
    # 현금 부족 시 축소 매수 경로가 작동해 자본이 음수로 안 간다
    assert all(v > 0 for _, v in r.equity)
    print("  현금 제약(축소 매수)     OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        a = sys.argv
        def opt(n, d=None):
            return a[a.index(n) + 1] if n in a else d
        _r = register_and_run(
            opt("--dataset", "krx-basket-daily"),
            opt("--dataset-version", "v1"),
            seed=int(opt("--seed", "0")),
            config=REV_CONFIG if "--strategy-rev5" in a else None)
        raise SystemExit(int(_r.get("status", 1)))

    print(f"{RUNNER_VERSION} 자체 점검 (DB 없음)")
    _check_no_lookahead()
    _check_fifo_and_costs()
    _check_metrics_and_determinism()
    _check_cash_never_negative()
    _check_strategy_catalog()
    print("Backtest Runner 5개 영역 통과. 실행은 --run")
