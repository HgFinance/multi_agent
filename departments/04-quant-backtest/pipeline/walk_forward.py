#!/usr/bin/env python3
"""Walk-Forward Validator v1 - MOM-20 스모크의 창별 강건성 판정 (QNT-04).

소유: 재일 (퀀트/백테스트본부, QNT-04 Robustness Validator 직무의 결정론 부분)
근거: pipeline/backtest_runner.py (Market/run_backtest/load_dataset/input_hash 패턴,
      DEFAULT_CONFIG=MOM-20, COST_MODEL),
      pipeline/pit_dataset.py (Dataset Manifest·content_hash·load_partition),
      supabase/migrations/20260729000300 (quant.hypotheses/experiments/
      experiment_metrics - split 'WALK_FORWARD' 허용, dimensions jsonb 로 창 구분)

▶ 설계 결정과 이유
  - **창 = 달력 반기(6개월) 롤링, 무겹침.** MOM-20 이 월초 리밸런스라 반기 경계
    (1/1, 7/1)가 항상 리밸런스 날과 일치한다 - 창 경계가 포지션 수명 중간을
    자르지 않는다. 시험창끼리 겹치면 같은 구간을 두 번 세는 것이라 금지.
  - **웜업 30거래일 = lookback 20 + 여유 10.** 웜업은 시그널 이력 전용이고 체결은
    금지다. 웜업 안의 월초(시험창 직전 달 첫 거래일)는 슬라이스 위치가
    30 - 그달 거래일수(19~23) = 7~11 < lookback+1 이라 momentum() 이 빈 dict 를
    돌려주므로 체결이 구조적으로 없다. 웜업을 40 이상으로 늘리면 이 보장이
    깨진다 - run_window() 의 단언(웜업 체결 0건)이 자체점검·실전 양쪽에서 잡는다.
  - **선견 금지는 run_backtest 구조(시그널 t-1 종가 / 체결 t 시가)를 그대로 쓴다.**
    창 자르기가 새로 보태는 위험은 "슬라이스에 시험창 밖 미래가 섞이는 것"뿐이라
    slice_market 이 [warmup_start, test_end] 밖을 물리적으로 제거하고 자체점검이
    단언한다.
  - **지표는 시험창만 계산한다.** 웜업 구간은 전액 현금(자본 불변)임을 단언한 뒤
    equity 를 마지막 웜업일부터 잘라 compute_metrics 에 넘긴다 - 창 수익률의
    기준선이 항상 초기자본이라 창끼리 비교 가능하다.
  - **backtest_runs 는 쓰지 않는다.** 이 모듈의 산출 단위는 "런"이 아니라
    "검증"이다 - 창별 숫자는 experiment_metrics(split='WALK_FORWARD',
    dimensions={"window": ...})로, 요약은 dimensions={"window":"SUMMARY"} 로 남긴다.
  - **판정은 결정론 규칙(LLM 없음)이고 hypotheses.status 를 바꾸지 않는다.**
    QNT-04 는 강건성 근거를 만들 뿐 가설 승인/기각 권한이 없다(승인은
    CEO·Risk·QA 체인). 판정은 stdout + SUMMARY 지표로만 남긴다.
  - **멱등**: input_hash = dataset content_hash + 창 구성 + config + code + cost.
    experiments.input_hash unique 라 같은 검증의 재등록은 DB 가 거부하고 exit 0.

사용
  python pipeline/walk_forward.py                    # 자체 점검 (DB 없음)
  python pipeline/walk_forward.py --run \
      --dataset krx-basket-daily --dataset-version v1   # 실제 검증 + 등록
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import itertools

from backtest_runner import (
    COST_MODEL,
    DEFAULT_CONFIG,
    Market,
    compute_metrics,
    load_dataset,
    run_backtest,
)

WF_VERSION = "quant-walk-forward-v1"
SEED = 0
WARMUP_TRADING_DAYS = 30          # lookback 20 + 여유 10 (위 docstring 의 보장 조건)

# 판정 규칙 - 결정론. 값을 바꾸면 판정 기준이 바뀌는 것이므로 근거를 함께 고친다.
FRAGILITY_RULES = {
    "min_positive_window_ratio": 0.6,   # 5창 기준 3창 이상 양수여야 부호 일관
    "max_worst_window_mdd": -0.25,      # 어느 한 창이라도 MDD 25% 초과면 취약
    "max_sharpe_std": 1.5,              # 창간 Sharpe 산포가 이보다 크면 불안정
}


def wf_code_version() -> str:
    """이 파일 + backtest_runner 해시 - 창 결과는 시뮬레이터 코드에도 의존한다."""
    here = Path(__file__).resolve()
    me = hashlib.sha256(here.read_bytes()).hexdigest()[:12]
    runner = hashlib.sha256((here.parent / "backtest_runner.py").read_bytes()).hexdigest()[:12]
    return f"{WF_VERSION}+{me}+runner:{runner}"


# ---------------------------------------------------------------------------
# 창 분할 - 순수 함수 (거래일 목록 -> 반기 시험창 + 웜업)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WFWindow:
    label: str            # "2024H2" 형태 - experiment_metrics.dimensions 키
    warmup_start: date    # 시그널 웜업 시작 (체결 금지 구간)
    test_start: date      # 시험창 첫 거래일 (반기 첫 거래일 = 월초 리밸런스 날)
    test_end: date        # 시험창 마지막 거래일
    n_warmup_days: int
    n_test_days: int
    partial: bool         # 반기 달력 끝이 데이터 끝보다 뒤 = 미완 반기
    # ▶ **embargo** - 시험창 앞쪽에서 잘라내는 거래일 수.
    #   웜업 마지막 날의 시그널이 보유 지평만큼 미래로 이어지므로, 그 구간을
    #   그대로 평가하면 **직전 구간 정보가 성적에 섞인다**(López de Prado
    #   purging/embargo). 창이 붙어 있는 walk-forward 에서 특히 그렇다.
    embargo_days: int = 0
    embargoed_start: date | None = None   # embargo 적용 후 실제 평가 시작일


def half_label(d: date) -> str:
    return f"{d.year}H{1 if d.month <= 6 else 2}"


def half_calendar_end(label: str) -> date:
    year = int(label[:4])
    return date(year, 6, 30) if label.endswith("H1") else date(year, 12, 31)


def make_windows(dates: list[date], warmup_days: int,
                 embargo_days: int = 0) -> list[WFWindow]:
    """오름차순 거래일 -> 무겹침 반기 시험창. 웜업을 확보 못 하는 앞 반기는 제외.

    순수 함수다 - 같은 (dates, warmup_days)는 언제나 같은 창 목록이고,
    이 목록이 input_hash 의 일부가 된다.
    """
    assert dates == sorted(dates) and len(set(dates)) == len(dates), "거래일이 정렬·유일해야 한다"
    index = {d: i for i, d in enumerate(dates)}
    groups: dict[str, list[date]] = {}
    for d in dates:
        groups.setdefault(half_label(d), []).append(d)

    out: list[WFWindow] = []
    for label in sorted(groups):
        ds = groups[label]
        i0 = index[ds[0]]
        if i0 < warmup_days:
            continue    # 데이터 맨 앞 반기는 웜업 전용으로만 쓰인다 - 창이 될 수 없다
        # embargo 만큼 앞을 잘라낸다. 잘라낸 뒤 남는 날이 없으면 그 창은
        # 평가할 수 없다 - 억지로 0일짜리 창을 만들지 않는다.
        emb = max(0, int(embargo_days))
        if emb >= len(ds):
            continue
        eff = ds[emb:]
        out.append(WFWindow(
            label=label, warmup_start=dates[i0 - warmup_days],
            test_start=ds[0], test_end=ds[-1],
            n_warmup_days=warmup_days, n_test_days=len(eff),
            embargo_days=emb,
            embargoed_start=eff[0],
            partial=half_calendar_end(label) > dates[-1]))
    return out


def slice_market(market: Market, w: WFWindow) -> Market:
    """[warmup_start, test_end] 밖을 물리적으로 제거 - 창 밖 미래가 못 들어온다."""
    # ▶ 웜업은 그대로 두고(시그널 계산에 필요하다) 평가만 embargo 뒤부터
    #   시작한다. 웜업까지 자르면 시그널이 아예 안 나온다.
    keep = [d for d in market.dates if w.warmup_start <= d <= w.test_end]
    kset = set(keep)
    opens = {k: v for k, v in market.opens.items() if k[0] in kset}
    closes = {k: v for k, v in market.closes.items() if k[0] in kset}
    symbols = sorted({s for (_, s) in closes})
    return Market(dates=keep, opens=opens, closes=closes, symbols=symbols)


def run_window(sub: Market, w: WFWindow, config: dict) -> dict:
    """창 하나 실행 + 창 독립성 단언 + 시험창만의 지표 계산.

    단언이 자체점검 전용이 아니라 실전(--run)에서도 돈다 - 웜업 상수를 잘못
    키우거나 달력이 예상과 다르면 조용히 오염되는 대신 여기서 죽는다.
    """
    assert sub.dates, f"{w.label}: 슬라이스가 비었다"
    assert sub.dates[0] >= w.warmup_start and sub.dates[-1] <= w.test_end, \
        f"{w.label}: 슬라이스에 창 밖 날짜가 있다"
    warmup_len = sum(1 for d in sub.dates if d < w.test_start)
    assert warmup_len == w.n_warmup_days, \
        f"{w.label}: 웜업 {warmup_len}일 != 기대 {w.n_warmup_days}일"

    # 웜업 중 무거래를 **구조로** 강제한다. MOM-20 은 월초 리밸런스+룩백 20이
    # 우연히 웜업 체결을 피했지만, REV-5(5일 리밸런스)에서 137건이 웜업에
    # 체결되며 아래 단언이 실측 발화했다(2026-08-01) - 운이 아니라 계약으로.
    # ▶ **거래는 test_start 부터, 평가는 embargo 뒤부터.** 둘을 같게 두면
    #   웜업 마지막 시그널의 보유 지평이 성적에 그대로 섞인다 - 직전 구간
    #   정보가 새는 자리다.
    result = run_backtest(sub, dict(config,
                                    no_trade_before=w.test_start.isoformat()))

    early = [f for f in result.fills if f.trade_date < w.test_start]
    assert not early, f"{w.label}: 웜업 중 체결 {len(early)}건 - 창 독립성 위반"
    capital = float(config["initial_capital"])
    # 웜업 전 구간이 전액 현금이면 마지막 웜업일 equity == 초기자본이다
    base_val = result.equity[warmup_len - 1][1]
    assert abs(base_val - capital) <= 1e-6 * capital, \
        f"{w.label}: 웜업 말 자본 {base_val} != 초기자본 {capital}"

    # embargo 만큼 평가 시작을 미룬다. 기준점은 그 직전 자산이다 -
    # 초기자본으로 두면 embargo 구간 손익이 성적에 남는다.
    emb = int(getattr(w, "embargo_days", 0) or 0)
    start_i = max(warmup_len - 1, warmup_len - 1 + emb)
    if start_i >= len(result.equity) - 1:
        # 잘라내고 나면 평가할 구간이 없다 - 0 으로 채우지 않고 알린다
        return {"label": w.label, "usable": False,
                "reason": f"embargo {emb}일 적용 후 평가 구간이 없다"}
    equity_test = result.equity[start_i:]
    traded = sum(abs(f.quantity * f.price) for f in result.fills)
    m = compute_metrics(equity_test, result.fills, capital, traded)
    # 판정 제외 규칙(fragility_summary min_test_days)의 재료 - 창 크기를 싣는다
    m["test_days"] = w.n_test_days
    m["partial_window"] = w.partial

    # ▶ **같은 창의 시장 수익률**을 함께 낸다. 국면 분해(regime_breakdown)가
    #   "이 전략이 상승장에서만 되는가" 를 보려면 창마다 시장이 어땠는지가
    #   있어야 하는데, 지금까지 전략 성과만 냈다. 전략 성과로 국면을 나누면
    #   "잘된 창" 과 "상승장" 이 같은 말이 되어 아무것도 못 가린다.
    try:
        from backtest_runner import buy_and_hold_equity

        bh = buy_and_hold_equity(sub, config)
        bh_test = [v for d, v in bh if d >= equity_test[0][0]]
        if len(bh_test) >= 2 and bh_test[0]:
            m["benchmark_return"] = bh_test[-1] / bh_test[0] - 1.0
    except Exception:
        pass          # 못 구하면 안 싣는다 - 없는 벤치마크를 0 으로 두지 않는다

    # ▶ 창의 **일별 수익률**을 함께 돌려준다. 과적합 통계(DSR/부트스트랩)는
    #   수익률 계열이 60개 이상 필요한데 창 요약은 5개뿐이라, 지금까지 이
    #   경로에서는 계산 자체가 불가능했다(카드 validation 이 전부 null 이었다).
    eq = [v for _, v in equity_test]
    m["_daily_returns"] = [eq[i] / eq[i - 1] - 1.0
                           for i in range(1, len(eq)) if eq[i - 1]]
    return m


# ---------------------------------------------------------------------------
# Fragility 판정 - 결정론 (LLM 없음)
# ---------------------------------------------------------------------------

# 판정에 넣을 최소 시험 거래일. 실측(2026-08-01): 2026H2 부분창(20거래일,
# -46.71%)이 5창 요약을 지배해 FRAGILE 판정을 사실상 혼자 결정했다 - 표본
# 미달 창은 **기록은 남기되 판정에서 제외**한다. 값 40 = 정상 반기(~120
# 거래일)의 1/3, 월초 리밸런스 2회 이상이 보장되는 최소 구간.
MIN_JUDGE_TEST_DAYS = 40


def fragility_summary(window_metrics: list[tuple[str, dict]],
                      *, min_test_days: int = MIN_JUDGE_TEST_DAYS
                      ) -> tuple[dict, list[str], str]:
    """창별 지표 -> (요약 통계, 위반 플래그, 판정). 통계만 DB 에 남는다.

    metrics 에 test_days 가 있으면 min_test_days 미달 창을 판정에서 뺀다
    (없으면 구버전 호환으로 포함). 제외 수는 n_excluded_short 로 남는다 -
    조용한 절단 금지.
    """
    assert window_metrics, "창이 0개면 판정할 수 없다"
    judgeable = [(l, m) for l, m in window_metrics
                 if m.get("test_days") is None or m["test_days"] >= min_test_days]
    excluded = [l for l, m in window_metrics
                if not (m.get("test_days") is None or m["test_days"] >= min_test_days)]
    assert judgeable, (f"판정 가능한 창이 없다 (전부 {min_test_days}일 미만) - "
                       f"표본 없이 판정하지 않는다")
    rets = [m["total_return"] for _, m in judgeable]
    sharpes = [m["sharpe_rf0"] for _, m in judgeable]
    mdds = [m["max_drawdown"] for _, m in judgeable]
    n = len(rets)
    pos_ratio = sum(1 for r in rets if r > 0) / n
    worst_mdd = min(mdds)
    if n >= 2:
        mu = sum(sharpes) / n
        sharpe_std = math.sqrt(sum((s - mu) ** 2 for s in sharpes) / (n - 1))
    else:
        sharpe_std = 0.0
    stats = {
        "n_windows": n,
        "n_excluded_short": len(excluded),   # 표본 미달로 판정 제외된 창 수
        "positive_window_ratio": round(pos_ratio, 4),
        "worst_window_return": round(min(rets), 6),
        "mean_window_return": round(sum(rets) / n, 6),
        "worst_window_mdd": round(worst_mdd, 6),
        "sharpe_std": round(sharpe_std, 4),
    }
    flags = []
    if pos_ratio < FRAGILITY_RULES["min_positive_window_ratio"]:
        flags.append("SIGN_INCONSISTENT")
    if worst_mdd < FRAGILITY_RULES["max_worst_window_mdd"]:
        flags.append("DEEP_WINDOW_MDD")
    if sharpe_std > FRAGILITY_RULES["max_sharpe_std"]:
        flags.append("SHARPE_UNSTABLE")
    return stats, flags, ("FRAGILE" if flags else "ROBUST")


def wf_input_hash(dataset_hash: str, windows: list[WFWindow],
                  config: dict, code_ver: str, seed: int) -> str:
    """창 구성이 해시에 들어간다 - 웜업/창 규칙이 바뀌면 다른 검증이다."""
    payload = json.dumps({
        "dataset": dataset_hash,
        "windows": [{"window": w.label, "warmup_start": str(w.warmup_start),
                     "test_start": str(w.test_start), "test_end": str(w.test_end),
                     "warmup_days": w.n_warmup_days} for w in windows],
        "config": config, "code": code_ver, "seed": seed, "cost": COST_MODEL,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# 실행 + 등록 (hypothesis -> experiment -> 창별 metrics -> SUMMARY)
# ---------------------------------------------------------------------------

def register_and_validate(name: str, version: str, *,
                          config: dict | None = None,
                          edge: dict | None = None,
                          title: str | None = None) -> int:
    # ▶ config/edge 를 인자로 연다. 예전엔 둘 다 하드코딩(DEFAULT_CONFIG,
    #   type='none')이라 **같은 Family 안 변형을 만들 수 없었고**, 그래서
    #   PBO 가 요구하는 "변형 4개 이상" 을 영원히 못 채웠다. 실측 17건 중
    #   11건이 edge type='none' 이라 Family 미사상이었던 것도 이 때문이다.
    import psycopg2

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "01-research" / "collectors"))
    from source_registry import load_project_env

    env = load_project_env()
    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    config = dict(config or DEFAULT_CONFIG)
    code_ver = wf_code_version()
    trace = str(uuid.uuid4())
    try:
        dataset_id, _universe_id, dhash, rows = load_dataset(conn, name, version)
        market = Market.from_rows(rows)
        windows = make_windows(market.dates, WARMUP_TRADING_DAYS)
        assert windows, "walk-forward 창이 0개다 - 데이터 구간 확인"
        ihash = wf_input_hash(dhash, windows, config, code_ver, SEED)

        split_policy = {
            "policy": "walk-forward-rolling-6m",
            "warmup_trading_days": WARMUP_TRADING_DAYS,
            "windows": [{"window": w.label, "warmup_start": str(w.warmup_start),
                         "test_start": str(w.test_start), "test_end": str(w.test_end),
                         "n_test_days": w.n_test_days, "partial": w.partial}
                        for w in windows],
            "note": "시험창 무겹침 - 웜업은 시그널 전용(체결 0건 단언)",
        }
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into quant.hypotheses
                  (title, rationale, expected_edge, falsification_criteria,
                   required_data_products, status, created_by, trace_id)
                values (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, 'TESTING', %s, %s)
                returning hypothesis_id
                """,
                (title or "[QNT-04] MOM-20 walk-forward 강건성 검증",
                 ("MOM-20 스모크의 강건성 판정 - 전략 승인 목적이 아니다. "
                 "창별 성과의 부호 일관성·최악 창 MDD·창간 Sharpe 산포를 결정론 "
                 "규칙으로 요약해 QNT-03 스모크 결과가 특정 구간의 우연인지 "
                 "가려낸다. 판정으로 hypotheses.status 를 바꾸지 않는다(승인 "
                 "권한은 CEO·Risk·QA 체인)."),
                 json.dumps(edge or {"type": "none",
                                     "note": "robustness check - edge 주장 없음"}),
                 json.dumps({"fragility_rules": FRAGILITY_RULES,
                             "note": "규칙 위반 플래그가 하나라도 있으면 FRAGILE"}),
                 json.dumps([f"{name}/{version}"]), WF_VERSION, trace))
            hyp_id = str(cur.fetchone()[0])

            # ▶ **사전등록을 건너뛰지 않는다.** 이 경로는 오케스트레이터를
            #   안 타므로 material_fingerprint 가 비어 있었고, 그래서 여기서
            #   나온 실험은 ExperimentCard 를 만들 수 없었다(지문이 없으면
            #   결과를 보고 설정을 바꿨는지 확인할 방법이 없다).
            #   관문을 우회하는 실행 경로를 남겨두면 규율이 선택사항이 된다.
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from preregistration import preregister

            spec = {
                "strategy_family": (edge or {}).get("type") or "robustness",
                "universe_version": f"{name}/{version}",
                "holding_horizon": config.get("lookback_days"),
                "decision_frequency": config.get("rebalance"),
                "cost_model_version": COST_MODEL["version"],
                "preregistered_splits": [w.label for w in windows],
                "baseline": "equal_weight_buy_and_hold",
                "entry_exit_rules": {k: config[k] for k in sorted(config)},
                "falsification_tests": FRAGILITY_RULES,
                "label": "forward_return",
            }
            pre = preregister(spec)
            if not pre.ok:
                # 고정할 것이 없으면 실험하지 않는다 - 등록한 척만 하는 실험은
                # 나중에 무엇을 바꿔도 지문이 같아 검사가 무력하다
                raise RuntimeError(f"사전등록 불가: {pre.reason}")
            cur.execute(
                """update quant.hypotheses
                      set material_fingerprint=%s, preregistered_at=now()
                    where hypothesis_id=%s""", (pre.fingerprint, hyp_id))

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
                (hyp_id, dataset_id, code_ver, json.dumps(config), SEED,
                 json.dumps(split_policy, ensure_ascii=False),
                 COST_MODEL["version"], ihash, trace))
            got = cur.fetchone()
            if got is None:
                conn.rollback()     # 가설 insert 도 함께 되돌린다 - 고아 가설 금지
                print(f"같은 input_hash 의 검증이 이미 있다({ihash[:16]}…) - "
                      f"재실행은 같은 결과라 등록하지 않는다 (재현성 계약)", flush=True)
                return 0
            exp_id = str(got[0])
        conn.commit()

        try:
            per_window: list[tuple[str, dict]] = []
            for w in windows:
                per_window.append((w.label, run_window(slice_market(market, w), w, config)))
            stats, flags, verdict = fragility_summary(per_window)

            # ▶ **과적합 통계.** 창 요약은 5개뿐이라 계산이 불가능했는데,
            #   창별 일별 수익률을 이어 붙이면 OOS 계열이 된다. 웜업은
            #   빠져 있고 창은 겹치지 않으므로 이어 붙여도 미래가 안 샌다.
            from overfit_stats import bootstrap_ci, deflated_sharpe

            oos_rets = [r for _, m in per_window
                        for r in (m.get("_daily_returns") or [])]
            n_variants = 1
            try:
                with conn.cursor() as c2:
                    c2.execute(
                        """select count(distinct e.experiment_id)
                             from quant.experiments e
                             join quant.hypotheses h
                               on h.hypothesis_id = e.hypothesis_id
                            where h.expected_edge->>'type' = %s""",
                        (str((edge or {}).get("type") or ""),))
                    n_variants = max(1, int((c2.fetchone() or [1])[0]))
            except Exception:
                n_variants = 1        # 못 세면 1 - 없는 압력을 지어내지 않는다
            for src in (deflated_sharpe(oos_rets, trials=n_variants),
                        bootstrap_ci(oos_rets)):
                for k, v in src.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        stats[k] = v

            # ▶ **국면 분해.** 창별 시장 수익률로 묶는다 - 상승장에서만 되는
            #   전략을 총계 하나가 숨긴다. 계약이 SUBMIT_TO_QA 에 이걸 요구한다.
            from regime_breakdown import build as regime_build

            bench = {l: m["benchmark_return"] for l, m in per_window
                     if m.get("benchmark_return") is not None}
            reg = regime_build([(l, m) for l, m in per_window
                                if l in bench], bench)
            for k, v in reg["meta"].items():
                stats[f"regime_{k}"] = v
        except Exception:
            # 실패도 기록한다 - 성공만 남기는 것이 p-hacking 의 시작이다
            with conn.cursor() as cur:
                cur.execute("update quant.experiments set status='FAILED', ended_at=now() "
                            "where experiment_id=%s", (exp_id,))
            conn.commit()
            raise

        with conn.cursor() as cur:
            # 국면별 지표를 dimensions={"regime": …} 로 남긴다 - 카드가
            # regime_breakdown 을 여기서 읽는다
            for rname, row in reg["regime_breakdown"].items():
                for k, v in row.items():
                    cur.execute(
                        """
                        insert into quant.experiment_metrics
                          (experiment_id, split, metric, value, dimensions,
                           cost_model_version)
                        values (%s, 'WALK_FORWARD', %s, %s, %s::jsonb, %s)
                        on conflict (experiment_id, split, metric, dimensions)
                        do update set value = excluded.value
                        """,
                        (exp_id, k, v, json.dumps({"regime": rname}),
                         COST_MODEL["version"]))
            for label, m in per_window + [("SUMMARY", stats)]:
                for k, v in m.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and v is not None:  # bool 은 int 서브클래스 - DB numeric 에 못 들어간다 (실측)
                        cur.execute(
                            """
                            insert into quant.experiment_metrics
                              (experiment_id, split, metric, value, dimensions,
                               cost_model_version)
                            values (%s, 'WALK_FORWARD', %s, %s, %s::jsonb, %s)
                            on conflict (experiment_id, split, metric, dimensions) do nothing
                            """,
                            (exp_id, k, v, json.dumps({"window": label}),
                             COST_MODEL["version"]))
            cur.execute("update quant.experiments set status='COMPLETED', ended_at=now() "
                        "where experiment_id=%s", (exp_id,))
        conn.commit()

        print(f"{WF_VERSION}: {name}/{version} MOM-20 walk-forward 완료 "
              f"(experiment {exp_id[:8]}…)", flush=True)
        print(f"  창 {len(windows)}개 | 웜업 {WARMUP_TRADING_DAYS}거래일 | "
              f"{len(market.symbols)}종목 | hypothesis {hyp_id[:8]}… (status 는 "
              f"TESTING 유지 - 판정 권한 없음)", flush=True)
        by_label = {w.label: w for w in windows}
        for label, m in per_window:
            w = by_label[label]
            part = " (부분)" if w.partial else ""
            print(f"  {label}{part} {w.test_start}~{w.test_end} | "
                  f"수익률 {m['total_return']:+.2%} | Sharpe {m['sharpe_rf0']} | "
                  f"MDD {m['max_drawdown']:.2%} | 체결 {m['n_fills']}", flush=True)
        print(f"  Fragility: 양수 창 비율 {stats['positive_window_ratio']:.2f} | "
              f"최악 창 수익률 {stats['worst_window_return']:+.2%} | "
              f"최악 창 MDD {stats['worst_window_mdd']:.2%} | "
              f"Sharpe 표준편차 {stats['sharpe_std']}", flush=True)
        print(f"  판정 {verdict}{' ' + str(flags) if flags else ''} "
              f"(결정론 규칙 - 전략 승인 아님)", flush=True)
        print(f"  input_hash {ihash[:16]}… (같은 입력 = 같은 해시 = 중복 등록 차단)",
              flush=True)
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 자체 점검 - DB 없음, 합성 데이터
# ---------------------------------------------------------------------------

def _weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _synth_market(start: date, end: date, series: dict) -> Market:
    """주중=거래일 가정의 합성 시장. series: 심볼 -> f(i, date) -> 가격."""
    days = _weekdays(start, end)
    rows = []
    for s, fn in series.items():
        for i, d in enumerate(days):
            px = float(fn(i, d))
            rows.append({"instrument_id": s, "trade_date": d,
                         "open": px, "high": px, "low": px, "close": px,
                         "volume": 1000,
                         "observed_at": datetime(2026, 7, 31, tzinfo=timezone.utc)})
    return Market.from_rows(rows)


def _check_windows_pure():
    days = _weekdays(date(2024, 1, 2), date(2026, 7, 30))
    ws = make_windows(days, WARMUP_TRADING_DAYS)
    assert [w.label for w in ws] == ["2024H2", "2025H1", "2025H2", "2026H1", "2026H2"], \
        [w.label for w in ws]
    assert ws == make_windows(days, WARMUP_TRADING_DAYS)      # 순수 함수 - 재호출 동일
    for a, b in itertools.pairwise(ws):
        assert a.test_end < b.test_start, f"시험창 겹침: {a.label}/{b.label}"
    for w in ws:
        assert w.warmup_start < w.test_start <= w.test_end
        n_warm = sum(1 for d in days if w.warmup_start <= d < w.test_start)
        assert n_warm == WARMUP_TRADING_DAYS, f"{w.label}: 웜업 {n_warm}일"
        assert half_label(w.test_start) == w.label == half_label(w.test_end)
    assert [w.partial for w in ws] == [False, False, False, False, True]
    # 데이터가 반기 중간에 시작하면 그 반기는 창이 아니라 웜업 전용이다
    ws2 = make_windows(_weekdays(date(2024, 7, 1), date(2025, 12, 31)),
                       WARMUP_TRADING_DAYS)
    assert [w.label for w in ws2] == ["2025H1", "2025H2"]
    assert not ws2[-1].partial
    print("  창 분할(무겹침·웜업 보장) OK")


def _check_slice_no_future():
    m = _synth_market(date(2024, 1, 2), date(2024, 12, 31),
                      {"A": lambda i, d: 100.0, "B": lambda i, d: 200.0})
    (w,) = make_windows(m.dates, WARMUP_TRADING_DAYS)
    sub = slice_market(m, w)
    assert all(w.warmup_start <= d <= w.test_end for d in sub.dates), "창 밖 날짜 유입"
    assert all(w.warmup_start <= k[0] <= w.test_end for k in sub.opens), "opens 에 창 밖 미래"
    assert all(w.warmup_start <= k[0] <= w.test_end for k in sub.closes), "closes 에 창 밖 미래"
    warm = [d for d in sub.dates if d < w.test_start]
    assert len(warm) == WARMUP_TRADING_DAYS and max(warm) < w.test_start
    assert sub.dates[-1] == w.test_end
    print("  슬라이스(창 밖 미래 차단) OK")


def _check_no_lookahead_through_window():
    """창 첫 리밸런스가 시험창 당일 정보를 쓰면 안 된다 - 시그널은 t-1 까지."""
    start, end = date(2025, 1, 2), date(2025, 12, 31)
    ws = make_windows(_weekdays(start, end), WARMUP_TRADING_DAYS)
    (w,) = [x for x in ws if x.label == "2025H2"]
    series = {
        "UP": lambda i, d: 100.0 * (1.002 ** i),                          # 꾸준한 상승
        "SPIKE_T0": lambda i, d: 300.0 if d >= w.test_start else 100.0,   # 시험창 당일 급등
        "SPIKE_MID": lambda i, d: 300.0 if d >= date(2025, 7, 15) else 100.0,
        "FLAT": lambda i, d: 100.0,
    }
    sub = slice_market(_synth_market(start, end, series), w)
    cfg = dict(DEFAULT_CONFIG, top_n=1)
    result = run_backtest(sub, cfg)
    buys = [f for f in result.fills if f.side == "BUY"]
    assert buys and buys[0].trade_date == w.test_start
    assert buys[0].instrument_id == "UP", \
        "첫 리밸런스가 당일 급등(SPIKE_T0)을 골랐다 - 선견 유입"
    # 급등이 t-1 lookback 이력에 들어온 다음 리밸런스부터는 고르는 게 정상이다
    assert any(f.instrument_id == "SPIKE_MID" for f in buys[1:]), \
        "웜업 경계가 시험창 안 정보까지 막았다"
    assert all(f.trade_date >= w.test_start for f in result.fills), "웜업 중 체결"
    warmup_len = sum(1 for d in sub.dates if d < w.test_start)
    cap = float(cfg["initial_capital"])
    assert all(abs(v - cap) < 1e-9 for _, v in result.equity[:warmup_len]), \
        "웜업 구간 equity 가 초기자본이 아니다 - 체결 유입 의심"
    run_window(sub, w, cfg)     # 실전 경로의 단언들도 같은 입력으로 통과해야 한다
    print("  선견 차단(창 경유 t-1)  OK")


def _check_embargo_removes_leading_days():
    """**embargo 가 시험창 앞을 실제로 잘라내는가.**

    창이 붙어 있는 walk-forward 에서 웜업 마지막 시그널은 보유 지평만큼
    미래로 이어진다. 그 구간을 그대로 평가하면 직전 구간 정보가 성적에
    섞인다 - 이 모듈이 내는 fragility 도, 그 위에서 계산한 DSR·PBO 도
    같이 오염된다.
    """
    days = _weekdays(date(2024, 1, 1), date(2026, 6, 30))
    base = make_windows(days, WARMUP_TRADING_DAYS)
    emb = make_windows(days, WARMUP_TRADING_DAYS, embargo_days=5)
    assert base and emb, (len(base), len(emb))
    b0, e0 = base[0], emb[0]
    # 시험창 경계는 그대로다 - 잘라내는 것은 **평가 시작**이다
    assert e0.test_start == b0.test_start and e0.test_end == b0.test_end
    assert e0.embargo_days == 5 and b0.embargo_days == 0
    assert e0.n_test_days == b0.n_test_days - 5, (b0.n_test_days, e0.n_test_days)
    assert e0.embargoed_start > b0.test_start, (b0.test_start, e0.embargoed_start)

    # ▶ 잘라내면 남는 날이 없는 창은 **만들지 않는다** - 0일짜리 창을
    #   억지로 만들면 그 창의 지표가 전부 무의미한 값이 된다
    huge = make_windows(days, WARMUP_TRADING_DAYS, embargo_days=10_000)
    assert huge == [], huge
    # 음수는 0 으로 본다(자르지 않음) - 미래를 당겨오지 않는다
    assert make_windows(days, WARMUP_TRADING_DAYS, embargo_days=-5)[0].embargo_days == 0


def _check_window_metrics_determinism():
    start, end = date(2025, 1, 2), date(2025, 12, 31)
    m = _synth_market(start, end, {"A": lambda i, d: 100.0 * (1.001 ** i),
                                   "B": lambda i, d: 100.0})
    ws = make_windows(m.dates, WARMUP_TRADING_DAYS)
    (w,) = [x for x in ws if x.label == "2025H2"]
    cfg = dict(DEFAULT_CONFIG, top_n=1)
    m1 = run_window(slice_market(m, w), w, cfg)
    m2 = run_window(slice_market(m, w), w, cfg)
    assert m1 == m2, "같은 창이 다른 지표를 냈다 - 비결정성"
    assert m1["total_return"] > 0 and m1["max_drawdown"] <= 0
    # 기준선 = 초기자본: 시험창만의 수익이지 웜업 포함 수익이 아니다
    # (compute_metrics 가 total_return 을 6자리 반올림하므로 그 수준까지만 본다)
    assert abs(m1["final_equity"] / cfg["initial_capital"] - 1.0
               - m1["total_return"]) < 1e-6
    print("  창 지표(결정성·기준선)  OK")


def _check_fragility_rules():
    good = [(f"W{i}", {"total_return": 0.05, "sharpe_rf0": 1.0, "max_drawdown": -0.05})
            for i in range(5)]
    stats, flags, verdict = fragility_summary(good)
    assert verdict == "ROBUST" and not flags
    assert stats["positive_window_ratio"] == 1.0 and stats["sharpe_std"] == 0.0

    bad = [("W1", {"total_return": 0.30, "sharpe_rf0": 3.0, "max_drawdown": -0.05}),
           ("W2", {"total_return": -0.10, "sharpe_rf0": -1.0, "max_drawdown": -0.30}),
           ("W3", {"total_return": -0.05, "sharpe_rf0": -0.5, "max_drawdown": -0.10})]
    stats2, flags2, verdict2 = fragility_summary(bad)
    assert verdict2 == "FRAGILE"
    assert set(flags2) == {"SIGN_INCONSISTENT", "DEEP_WINDOW_MDD", "SHARPE_UNSTABLE"}, flags2
    assert stats2["worst_window_mdd"] == -0.30 and stats2["n_windows"] == 3
    mu = (3.0 - 1.0 - 0.5) / 3
    exp_std = math.sqrt(sum((s - mu) ** 2 for s in (3.0, -1.0, -0.5)) / 2)
    assert abs(stats2["sharpe_std"] - round(exp_std, 4)) < 1e-9

    # 표본 미달 창 제외 (2026H2 실측: 20거래일 부분창이 판정을 지배했다)
    mixed = [("A", {"total_return": 0.05, "sharpe_rf0": 1.0,
                    "max_drawdown": -0.05, "test_days": 120}),
             ("B", {"total_return": 0.06, "sharpe_rf0": 1.1,
                    "max_drawdown": -0.06, "test_days": 118}),
             ("SHORT", {"total_return": -0.47, "sharpe_rf0": -7.8,
                        "max_drawdown": -0.47, "test_days": 20})]
    s3, _f3, v3 = fragility_summary(mixed)
    assert v3 == "ROBUST" and s3["n_windows"] == 2 and s3["n_excluded_short"] == 1
    assert s3["worst_window_mdd"] == -0.06      # 부분창 수치가 판정에 안 들어감
    try:
        fragility_summary([("S", {"total_return": 0.0, "sharpe_rf0": 0.0,
                                  "max_drawdown": 0.0, "test_days": 5})])
        raise AssertionError("전부 표본 미달인데 판정이 나왔다")
    except AssertionError as e:
        if "판정이 나왔다" in str(e):
            raise
    # test_days 없는 구버전 입력은 전부 판정 대상 (호환)
    assert fragility_summary(good)[0]["n_excluded_short"] == 0
    print("  Fragility 판정(결정론)  OK")


def _check_input_hash():
    days = _weekdays(date(2024, 1, 2), date(2026, 7, 30))
    ws = make_windows(days, WARMUP_TRADING_DAYS)
    code = wf_code_version()
    h = wf_input_hash("d", ws, DEFAULT_CONFIG, code, SEED)
    assert h == wf_input_hash("d", list(ws), dict(DEFAULT_CONFIG), code, SEED)
    assert h != wf_input_hash("d2", ws, DEFAULT_CONFIG, code, SEED)
    assert h != wf_input_hash("d", ws[:-1], DEFAULT_CONFIG, code, SEED)     # 창 구성 변경
    assert h != wf_input_hash("d", ws, dict(DEFAULT_CONFIG, top_n=10), code, SEED)
    print("  input_hash(창 구성 포함) OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        a = sys.argv
        def opt(n, d=None):
            return a[a.index(n) + 1] if n in a else d
        raise SystemExit(register_and_validate(
            opt("--dataset", "krx-basket-daily"),
            opt("--dataset-version", "v1")))

    print(f"{WF_VERSION} 자체 점검 (DB 없음)")
    _check_embargo_removes_leading_days()
    print("  embargo 적용            OK")
    _check_windows_pure()
    _check_slice_no_future()
    _check_no_lookahead_through_window()
    _check_window_metrics_determinism()
    _check_fragility_rules()
    _check_input_hash()
    print("Walk-Forward 7개 영역 통과. 실행은 --run")
