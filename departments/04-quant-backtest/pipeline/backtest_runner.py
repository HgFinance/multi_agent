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

import gc
import hashlib
import json
import math
import sys
import uuid
from collections import deque
import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pit_dataset import DATA_ROOT, content_hash, load_partition, resolve_object_path

RUNNER_VERSION = "quant-backtest-runner-v1"
KST = timezone(timedelta(hours=9))

# v1 비용 가정 - 근거를 값 옆에 남긴다. 바꾸면 cost_model_version 을 올린다.
from strategy_templates import (          # noqa: E402  (같은 디렉터리 모듈)
    TEMPLATES,
    pit_view_for,
    resolve,
    signal_scores,
)

COST_MODEL = {
    "version": "krx-cost-v2",
    "commission_bps": 1.5,   # 위탁수수료 왕복의 절반 (매수·매도 각각 부과)
    "sell_tax_bps": 15.0,    # 증권거래세+농특세 매도측 0.15% 가정 (2026 코스피 기준)
    # ▶ **실측으로 교체** (2026-08-04). v1 의 5.0bps 는 가정이었고 실제보다
    #   낮았다. market.market_quotes 2,000,000 표본:
    #     스프레드 평균 16.38bps · 중앙값 14.40bps · 상위10% 27.47bps
    #   체결 시 부담하는 것은 반쪽이므로 중앙값 기준 7.20bps 다.
    #   회전율 344배 전략에서 이 2.2bps 차이가 연 7%p 넘게 갈린다 - 가정
    #   하나로 결론이 뒤집히는 자리라 실측이 아니면 결과를 믿을 수 없다.
    "slippage_bps": 7.2,
    "slippage_source": "market.market_quotes 중앙값 스프레드/2 (2026-08-04 실측)",
    # 종목별 스프레드가 오면 그것을 쓴다. 없으면 위 기본값(보수적 중앙값).
    "slippage_p90_bps": 13.7,   # 상위10% 스프레드/2 - 스트레스 시나리오용
}


# ── 유동성 계층별 슬리피지 ──────────────────────────────────────────────────
# ▶ 왜 계층인가 (2026-08-04 실측)
#   종목별 실측 스프레드를 쓰고 싶었지만 **호가가 4거래일치뿐**이다
#   (market_quotes 2026-07-30~08-04). 백테스트는 2024-01 부터인데 오늘 호가로
#   2024 년 비용을 매기면 미래 유동성을 아는 셈이라 look-ahead 다.
#   일봉 고저가 추정(Corwin-Schultz)도 시도했으나 이 데이터에서 중앙값이
#   -3.57bps 로 나와(절반 이상 음수) 비용 입력으로 쓸 수 없었다.
#
#   대신 **거래대금은 2024 년부터 있다.** 실측 호가로 확인한 관계:
#     거래대금 5분위   평균거래대금        스프레드 중앙값
#       1 (하위)            1,825              18.59 bps
#       2                   4,990              16.88
#       3                  12,805              13.51
#       4                  38,519              13.34
#       5 (상위)          454,539              12.81
#   거래대금이 250배 차이나는데 스프레드는 1.45배 - 완만하지만 단조롭다.
#
# ▶ 이 방식의 한계를 숨기지 않는다
#   계층↔스프레드 **수준**은 2026-08 표본으로 보정한 것이라 2024 년의 절대
#   수준과 다를 수 있다. 다만 "얇을수록 넓다" 는 **관계**는 구조적이므로
#   전 종목 단일값보다 낫다. 호가 이력이 쌓이면 시점별로 교체한다.
LIQUIDITY_TIERS = (
    # (거래대금 상한, 왕복 스프레드 bps) - 상한 미만이면 이 계층
    (3_000, 18.59),
    (8_000, 16.88),
    (25_000, 13.51),
    (100_000, 13.34),
    (float("inf"), 12.81),
)


def slippage_bps_for(symbol: str | None = None,
                     spreads: dict | None = None,
                     adv: float | None = None) -> float:
    """체결 슬리피지(bps). 우선순위: **종목 실측 > 유동성 계층 > 시장 중앙값.**

    유동성이 얇은 종목일수록 스프레드가 넓은데 전 종목에 같은 값을 물리면
    소형주 전략이 부당하게 유리해진다. 반대로 없는 값을 지어내지도 않는다.
    """
    if spreads and symbol:
        v = spreads.get(str(symbol))
        if v is not None and v > 0:
            return float(v) / 2.0          # 왕복 스프레드의 절반이 편도 비용
    if adv is not None and adv > 0:
        for cap, bps in LIQUIDITY_TIERS:
            if adv < cap:
                return bps / 2.0
    return float(COST_MODEL["slippage_bps"])
DEFAULT_CONFIG = {
    "strategy": "MOM-20-SMOKE",
    "lookback_days": 20,
    "top_n": 20,
    "rebalance": "MONTH_FIRST_TRADING_DAY",
    "initial_capital": 100_000_000.0,
}

# 전략 카탈로그. **실체는 strategy_templates.TEMPLATES 이고 여기는 파생 뷰다** -
# 시그널·순위·최소 히스토리는 그쪽이 소유하고 자체 점검도 그쪽에 있다.
# 여기 없는(=resolve 가 못 찾는) strategy 문자열은 실행을 거부한다.
STRATEGIES = {
    f"{t.template_id}": {"rank": t.rank, "edge_type": t.edge_type, "note": t.note}
    for t in TEMPLATES.values()
}
# 과거 실험이 참조하는 명시 ID - 재현을 위해 유지한다(접두가 같아 resolve 가 찾는다).
LEGACY_STRATEGY_IDS = ("MOM-20-SMOKE", "REV-5-SMOKE")
REV_CONFIG = {
    "strategy": "REV-5-SMOKE",
    "lookback_days": 5,
    "top_n": 20,
    "rebalance": "EVERY_5_TRADING_DAYS",   # 가설 horizon 5일에 맞춘 주기
    "initial_capital": 100_000_000.0,
}


# 실험 결과를 정하는 모듈들. **한 파일만 세면 지문이 거짓말을 한다.**
#   backtest_runner   - 체결·비용·회계
#   strategy_templates - 시그널과 PIT 뷰(유동성 필터 포함). 전략 카탈로그의 실체
#   pit_dataset        - 파티션 적재 경로
#   overfit_stats      - 부트스트랩·DSR (run_backtest 안에서 부른다)
_CODE_FILES = ("backtest_runner.py", "strategy_templates.py",
               "pit_dataset.py", "overfit_stats.py")


def _code_digest(parts) -> str:
    """(이름, 내용) 목록 -> 지문. 이름도 섞는다 - 파일이 바뀌든 갈리든 달라진다."""
    h = hashlib.sha256()
    for name, blob in parts:
        h.update(name.encode())
        h.update(b"\0")
        h.update(blob)
        h.update(b"\0")
    return h.hexdigest()[:12]


def code_version() -> str:
    """결과를 정하는 코드의 해시 - 코드가 바뀌면 실험이 달라진다는 사실을 강제한다.

    ▶ 한 파일만 세면 지문이 거짓말을 한다 (2026-08-14 실측)
      예전에는 `Path(__file__)` 하나만 해시했다. 그런데 시그널과 PIT 뷰는
      `strategy_templates` 에 있다 - 이 러너가 거기서 import 하고, 전략
      카탈로그도 거기서 파생된다. 즉 **결과를 가장 크게 정하는 파일이 지문
      밖에** 있었다.

      그래서 두 방향으로 깨졌다:
        · 거래대금 단위 버그(백만원 vs 원)를 `strategy_templates` 에서 고쳤는데
          지문이 그대로라, 유니버스가 비어 **체결 0** 으로 끝난 실험
          `9354f7fd` 가 그 지문을 계속 쥐고 있었다. 고친 코드로 다시 돌리려던
          주문 2건이 "중복 실험" 으로 죽었다 - **버그가 자기 자리를 봉인했다.**
        · 반대로 **같은 지문의 두 실험이 다른 결과를 낼 수 있다.** "같은 입력 =
          같은 해시 = 같은 결과" 라는 재현성 계약이 그만큼 거짓이었다.

      지문이 넓어지므로 **과거 지문은 더 이상 새 실행을 막지 않는다.** 그건
      의도한 것이다 - 그 결과들은 실제로 다른 코드가 낸 것이다. 과거 행은
      자기 code_version 을 그대로 달고 원장에 남는다.
    """
    here = Path(__file__).resolve().parent
    parts = []
    for name in _CODE_FILES:
        p = here / name
        # 없는 파일도 결정적으로 센다 - 빠졌다는 사실 자체가 지문의 일부다
        parts.append((name, p.read_bytes() if p.exists() else b"<missing>"))
    return f"{RUNNER_VERSION}+{_code_digest(parts)}"


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
    notionals: dict[tuple[date, str], float] = field(default_factory=dict)
    # ▶ **미시구조 일별 집계** (2026-08-14). (날짜, 종목) -> 피처 dict.
    #   `krx-microstructure-daily` 가 제공하는 spread_bps·depth_imbalance·
    #   order_flow_imbalance·trade_intensity·realized_volatility 를 담는다.
    #   **비어 있는 것이 정상**이다 - 그 데이터셋을 요구하지 않은 실험은 이
    #   필드가 빈 채로 돌고, 예전과 완전히 같은 결과를 낸다.
    #
    #   ⚠ 필드를 늘렸으면 `walk_forward.slice_market` 도 같이 늘려야 한다.
    #     `notionals` 를 안 넘겨 창 21개가 전부 0 이 된 사고가 오늘 있었다
    #     (실험은 COMPLETED 인데 강건성 근거만 백지). 그쪽 자체점검이 이
    #     필드도 함께 검사한다.
    micro: dict[tuple[date, str], dict] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: list[dict]) -> Market:
        dates = sorted({r["trade_date"] for r in rows})
        opens, closes, notionals, symbols = {}, {}, {}, set()
        for r in rows:
            iid = str(r["instrument_id"])
            symbols.add(iid)
            opens[(r["trade_date"], iid)] = float(r["open"])
            closes[(r["trade_date"], iid)] = float(r["close"])
            nv = r.get("notional")
            if nv is not None:
                notionals[(r["trade_date"], iid)] = float(nv)
        return cls(dates=dates, opens=opens, closes=closes,
                   symbols=sorted(symbols), notionals=notionals)

    def micro_until(self, until: date, feature: str,
                    window: int = 1) -> dict[str, float]:
        """until **까지의** 미시구조 피처 평균. 미래를 보지 않는다.

        `window=1` 이면 그날 값 하나다. 호가·체결은 일별로도 잡음이 크므로
        창을 주면 평균한다 - 그 판단은 신호가 한다(기본은 평균 안 함).
        """
        idx = [d for d in self.dates if d <= until][-window:]
        out: dict[str, float] = {}
        for s_ in self.symbols:
            vals = []
            for d in idx:
                m = self.micro.get((d, s_))
                if m is None:
                    continue
                v = m.get(feature)
                if isinstance(v, (int, float)):
                    vals.append(float(v))
            if vals:
                out[s_] = sum(vals) / len(vals)
        return out

    def adv_until(self, until: date, window: int = 20) -> dict[str, float]:
        """until **까지의** 평균 거래대금. 미래 거래대금을 보지 않는다.

        유동성 계층을 이 값으로 정하므로 여기서 PIT 가 깨지면 비용 전체가
        미래 정보가 된다.
        """
        idx = [d for d in self.dates if d <= until][-window:]
        if not idx:
            return {}
        out: dict[str, float] = {}
        for s_ in self.symbols:
            vals = [self.notionals[(d, s_)] for d in idx
                    if self.notionals.get((d, s_))]
            if vals:
                out[s_] = sum(vals) / len(vals)
        return out

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


# 러너가 아는 리밸런스 정책. **`config_binding.REBALANCE_BY_HORIZON` 과 같은
# 집합이어야 한다** - 바인더가 만드는 값을 러너가 모르면 그 가설은 실행 단계에서
# 죽는다(2026-08-14 실측: horizon_days=2 -> `EVERY_TRADING_DAY` 로 사상됐는데
# 러너 어휘에 없어 `ValueError` 로 실험이 통째로 실패했다). 자체점검이 대조한다.
REBALANCE_POLICIES = ("MONTH_FIRST_TRADING_DAY", "EVERY_5_TRADING_DAYS",
                      "EVERY_TRADING_DAY")


def rebalance_days(dates: list[date], config: dict) -> set[date]:
    """리밸런스 일자 집합 - 정책 문자열이 모르는 값이면 거부한다."""
    policy = config.get("rebalance", "MONTH_FIRST_TRADING_DAY")
    if policy == "MONTH_FIRST_TRADING_DAY":
        return month_first_trading_days(dates)
    if policy == "EVERY_5_TRADING_DAYS":
        return set(dates[::5])
    if policy == "EVERY_TRADING_DAY":
        # ▶ 매일 리밸런스. 단기 지평(<=3일) 가설이 여기로 온다 - 미시구조처럼
        #   신호 수명이 며칠인 전략은 월초 리밸런스로는 그 지평을 검증할 수
        #   없다. 회전율이 커지는 것은 **사실이고 비용 모델이 그대로 문다** -
        #   회전을 숨기지 않고 성적에 반영시키는 것이 맞다.
        return set(dates)
    raise ValueError(
        f"알 수 없는 rebalance 정책: {policy!r} - 사용 가능: "
        f"{list(REBALANCE_POLICIES)}")


def select_targets(market: Market, i: int, config: dict) -> list[str]:
    """시그널일 t-1 종가까지로 대상 선정.

    ▶ 2026-08-10: 시그널을 여기서 직접 계산하지 않고 strategy_templates 레지스트리에
      위임한다. 이전에는 `market.momentum` 하나만 호출해서, 전략이 무엇이든 실제로
      도는 시그널은 모멘텀 하나였다 - 순위 방향만 뒤집는 것이 전략 유형의 전부였다.

    ▶ 실측 버그 수정: `config_binding` 이 가설 파라미터를 ID 에 새겨 `REV-5-20` 을
      만드는데 카탈로그에는 그 문자열이 없어 **가설이 반영되는 순간 ValueError 로
      실행이 거부**됐다(= 파라미터가 기본값과 같을 때만 실험이 돌았다).
      `resolve()` 가 접두로 템플릿을 찾아 이 구멍을 막는다.
    """
    # ▶ **알파 수식(AST)이 있으면 그것이 신호다** (2026-08-14)
    #   완성된 템플릿 14종은 그대로 두고 **병행**으로 연다 - 기존 실험의
    #   재현이 깨지지 않는다. 수식이 있는 실험만 이 경로로 간다.
    #
    #   AST 에는 TOP/BOTTOM 이 없다. **항상 큰 값이 좋다**로 해석한다 -
    #   방향을 뒤집고 싶으면 수식이 `neg` 를 쓰면 되고, 그러면 방향까지
    #   지문에 들어가 "무엇을 시험했는지" 가 트리 하나로 완결된다.
    expr = config.get("signal_expr")
    if expr:
        from alpha_ast import evaluate as _eval_expr  # noqa: PLC0415
        from alpha_ast import parse as _parse_expr

        _scores = _eval_expr(_parse_expr(expr),
                             pit_view_for(market, market.dates[i - 1], config))
        if not _scores:
            return []
        # 동점 순서는 종목 코드로 고정 - 결정론을 유지한다
        _ranked = sorted(_scores, key=lambda s: (-_scores[s], s))
        return _construct(_ranked, config)

    strat = config["strategy"]
    tpl = resolve(strat)
    if tpl is None:
        raise ValueError(
            f"카탈로그에 없는 전략: {strat!r} - 실행 거부 "
            f"(사용 가능: {sorted(TEMPLATES)})")
    scores = signal_scores(market, market.dates[i - 1], template=tpl,
                           params=config)
    if not scores:
        return []
    # 동점 시 순서는 market.symbols(정렬됨) 순서를 따른다 - 결정론 유지.
    # `rank` 를 반영했으므로 이 목록은 **언제나 좋은 것부터**다.
    ranked = sorted(scores, key=scores.get, reverse=tpl.rank == "TOP")
    return _construct(ranked, config)


# ── 포트폴리오 구성 방식 ────────────────────────────────────────────────────
#
# ▶ 왜 방식을 열었나 (2026-08-14 분위 곡선 실측)
#   `signal_composite` 를 체결가능 유니버스(ADV 1억, 1,689종목)에서 10분위로
#   갈라 재보니 알파가 **상위가 아니라 하위**에 있었다:
#     Q1 -0.72 · Q4 +1.00 · Q7 +0.79 · Q10 +0.35 (%/월, 비용 전)
#     롱온리 초과(Q10-평균) +0.06%p  vs  숏다리 몫(평균-Q1) +1.00%p
#   상위 분위가 평균과 구별되지 않으므로 **상위를 고르는 구성으로는 못 먹는다** -
#   top_n 이나 보유기간을 흔들어도 곡선 모양은 그대로다.
#
#   그런데 우리 어휘에는 "상위 N개를 산다" 밖에 없었다. 그래서 공장은 같은 벽에
#   반복해 부딪혔다 - 병목 센서스 실측으로 `BASELINE_NOT_BEATEN` 이 **계열
#   14개**에서 되풀이됐다("개별 설계 문제가 아니다"). 벽을 넘을 수단 자체가
#   없으면 가설을 아무리 더 만들어도 결과는 같다.
#
# ▶ 왜 섞지 않고 배타로 두나
#   `top_n=20` 은 DEFAULT_CONFIG 에 늘 있어서 "명시했는지" 를 구분할 수 없다.
#   두 손잡이를 동시에 허용하면 어떤 구성을 잰 실험인지 사후에 알 수 없고,
#   사전등록도 모호해진다. 방식을 **한 단어로 선언**하게 한다.
CONSTRUCTIONS = ("TOP_N", "EXCLUDE_BOTTOM")


def _construct(ranked: list[str], config: dict) -> list[str]:
    """좋은 것부터 정렬된 목록 -> 실제 보유 종목.

    `EXCLUDE_BOTTOM` 은 하위 `exclude_bottom_pct`% 를 빼고 **나머지 전체**를
    동일가중으로 든다. 상위를 고르는 것이 아니라 나쁜 것을 피하는 구성이라,
    숏 다리를 못 쓰는 롱온리에서 하위 분위의 음(-)수익을 회피하는 몫만 취한다.
    """
    mode = str(config.get("portfolio_construction") or "TOP_N").upper()
    if mode == "TOP_N":
        return ranked[:int(config["top_n"])]
    if mode == "EXCLUDE_BOTTOM":
        q = float(config.get("exclude_bottom_pct", 10.0))
        if not 0.0 < q < 100.0:
            raise ValueError(
                f"exclude_bottom_pct 는 0 초과 100 미만이어야 한다: {q!r}")
        keep = int(len(ranked) * (1.0 - q / 100.0))
        # 한 종목도 안 남으면 그건 구성이 아니라 사고다 - 빈 포트폴리오는
        # 거래 0 이 되고, 그것을 성과로 판정하면 가설이 억울하게 죽는다.
        if keep < 1:
            raise ValueError(
                f"하위 {q}% 를 빼니 남는 종목이 없다(후보 {len(ranked)}개) - "
                f"유니버스나 비율을 고친다")
        return ranked[:keep]
    raise ValueError(
        f"모르는 구성 방식: {mode!r} - 사용 가능: {list(CONSTRUCTIONS)}")


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


def _apply_costs(side: str, notional: float,
                 slip_bps: float | None = None, mult: float = 1.0) -> float:
    """체결 비용. `mult` 는 **반증용 스트레스 배수**다.

    ▶ 왜 배수를 config 로 받나 (2026-08-12)
      에이전트가 쓴 반증 시험 49건 중 **10건이 "비용 차감 후 초과수익 소멸"**
      이었다. `COST_UNACCOUNTED` 는 가장 흔한 경쟁 설명 코드이기도 하다.
      그런데 비용 모델이 모듈 상수라 그 시험을 돌릴 방법이 없었다 -
      **에이전트가 설계한 반증을 아무도 집행하지 않았다.**

      전역을 바꾸면 병렬 실행이 서로를 오염시킨다. config 로 받아 실행마다
      독립시킨다. **기본 1.0 이라 안 켜면 예전과 한 비트도 안 다르다.**

    ▶ `EDGE_KEYS` 에는 넣지 않는다 - 이건 반증 도구지 가설 파라미터가 아니다.
      기획안이 비용을 낮춰 성적을 좋게 만드는 길을 열면 안 된다.
    """
    bps = COST_MODEL["commission_bps"] + (
        COST_MODEL["slippage_bps"] if slip_bps is None else float(slip_bps))
    if side == "SELL":
        bps += COST_MODEL["sell_tax_bps"]
    return notional * bps / 1e4 * float(mult)


def cost_multiplier(config: dict) -> float:
    """config -> 비용 배수. 없으면 1.0(꺼짐)."""
    v = (config or {}).get("cost_stress")
    try:
        m = float(v) if v is not None else 1.0
    except (TypeError, ValueError):
        return 1.0
    # 비용을 **낮추는** 방향은 막는다 - 반증 도구로 성적을 꾸미면 안 된다
    return m if m >= 1.0 else 1.0



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
        fee = _apply_costs("BUY", per, mult=cost_multiplier(config))
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
    diffs, rs, rb = [], [], []
    prev_s = prev_b = None
    for d, sv in strategy_equity:
        bv = bmap.get(d)
        if bv is None or not sv or not bv:
            continue
        if prev_s and prev_b:
            r_s = sv / prev_s - 1.0
            r_b = bv / prev_b - 1.0
            diffs.append(r_s - r_b)
            rs.append(r_s)
            rb.append(r_b)
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

    # ── 위험조정 비교 계측 (2026-08-14, 2층 관문 결재 1단계) ──────────────
    # ▶ 왜 여기인가: 전략·벤치마크 **일별 수익이 날짜 정렬된 채로 함께 있는
    #   유일한 지점**이다. 벤치마크 곡선은 이 함수를 나가면 버려지고(관문은
    #   DB 스칼라만 재조회한다), 원장에 수익률 시계열 테이블이 없다.
    # ▶ 왜 필요한가 (실측): vol 타게팅 15% 전략을 ~30% vol 완전투자 벤치마크와
    #   **명목 초과**로 비교해 −90~−103%p 가 나왔다. 위험을 줄일수록 관문이
    #   처벌하는 구조이고, 문헌은 이를 leverage bias 라 부른다. 정석은 M²
    #   (벤치마크 vol 로 스케일 후 비교)·회귀 alpha·appraisal ratio 다.
    # ▶ **판정은 바꾸지 않는다.** release_gate.evaluate 는 이름 지정된 8개
    #   키만 읽으므로 여기 무엇을 더해도 passed/failed 는 불변이다. 이 값들은
    #   관문 재설계 결재를 위한 근거이지 그 자체로 합격선이 아니다.
    # ▶ 못 재면 키를 안 만든다(0 을 채우지 않는다). rf=0 가정은 이 저장소의
    #   sharpe_rf0 관례와 같다.
    if len(rs) > 20:
        n = len(rs)
        ann = 252 ** 0.5
        m_s, m_b = sum(rs) / n, sum(rb) / n
        sd_s = (sum((x - m_s) ** 2 for x in rs) / (n - 1)) ** 0.5
        sd_b = (sum((x - m_b) ** 2 for x in rb) / (n - 1)) ** 0.5
        if sd_s > 0:
            out["strategy_ann_vol_pct"] = round(sd_s * ann * 100.0, 2)
        if sd_b > 0:
            out["benchmark_ann_vol_pct"] = round(sd_b * ann * 100.0, 2)
        if sd_s > 0 and sd_b > 0:
            # M²: 전략을 벤치마크와 같은 변동성으로 스케일한 뒤의 초과(연율 %p).
            #   레버리지로 위험을 맞춘 뒤 비교하는 것이 위험조정 비교의 정의다.
            m2 = ((m_s / sd_s) * sd_b - m_b) * 252.0 * 100.0
            out["m2_excess_ann_pct"] = round(m2, 2)
            cov = sum((rs[i] - m_s) * (rb[i] - m_b) for i in range(n)) / (n - 1)
            out["corr_vs_benchmark"] = round(cov / (sd_s * sd_b), 4)
            beta = cov / (sd_b ** 2)
            out["beta_vs_benchmark"] = round(beta, 4)
            alpha_d = m_s - beta * m_b
            out["alpha_ann_pct"] = round(alpha_d * 252.0 * 100.0, 2)
            resid = [rs[i] - beta * rb[i] for i in range(n)]
            m_r = sum(resid) / n
            sd_r = (sum((x - m_r) ** 2 for x in resid) / (n - 1)) ** 0.5
            if sd_r > 0:
                out["residual_ann_vol_pct"] = round(sd_r * ann * 100.0, 2)
                # appraisal ratio = 연율 alpha / 연율 잔차위험. vol 관리 전략
                # 평가의 표준 지표(Moreira-Muir 가 성과를 이것으로 보고한다).
                out["appraisal_ratio"] = round(alpha_d * 252.0 / (sd_r * ann), 4)
    return out


# ── 위험관리 손잡이 ────────────────────────────────────────────────────────
# ▶ 왜 이게 있어야 하나 (2026-08-12 실측)
#   momentum 이 초과 +157.51%p · IR 1.26 · DSR 0.976 을 냈다. 다중검정을
#   감안해도 우연이 아닌 성적이다. 그런데 **전기간 낙폭이 -50.52%** 라
#   릴리스 관문(-35% 허용)을 못 넘었다.
#
#   이때 손잡이가 `strategy/lookback_days/top_n/rebalance/initial_capital`
#   다섯 개뿐이었다. **완전투자 동일가중 롱온리 말고는 표현할 수가 없었다** -
#   에이전트가 "낙폭을 줄여 보자" 고 판단해도 돌릴 방법이 없는 상태였다.
#   그래서 새 엣지만 계속 설계했고, 새 엣지도 같은 자리에서 같이 죽었다.
#
#   **기본값은 전부 꺼짐이다.** 안 켜면 이전과 완전히 같은 결과가 나온다 -
#   기존 실험의 input_hash 가 흔들리면 사전등록이 무너진다.
#
#   값은 우리가 정하지 않는다. 에이전트가 config 로 준다.
RISK_KEYS = ("vol_target_annual", "max_drawdown_stop", "vol_lookback_days",
             "max_exposure", "trend_filter_days", "trend_filter_exposure")
_VOL_MIN_OBS = 20              # 이보다 짧은 표본으로 변동성을 추정하지 않는다
# 추세 판정에 필요한 최소 종목 수. 이보다 적으면 시장을 대표하지 못한다.
_TREND_MIN_NAMES = 30


def risk_enabled(config: dict) -> bool:
    """위험관리 손잡이가 하나라도 켜져 있나. **꺼져 있으면 예전 그대로다.**"""
    return any(config.get(k) is not None for k in RISK_KEYS
               if k not in ("vol_lookback_days", "trend_filter_exposure"))


# ── 시장 추세 필터 ──────────────────────────────────────────────────────────
#
# ▶ 왜 이 수단이 필요한가 (2026-08-14 조사)
#   원장 실측: 손잡이를 끄면 momentum 이 IR 1.255·초과 +157.5%p 를 내지만
#   낙폭 -50.5% 로 강건성 관문에 걸리고, 손잡이(변동성 타게팅)를 켜면 낙폭은
#   잡히는데 IR 이 -0.45 로 무너진다. **양쪽 다 막힌다.**
#
#   문헌이 빠진 수단을 지목한다 - 이동평균 같은 타이밍 요소는 "장기 하락장에서
#   노출을 줄이는 데 매우 효과적" 이고, 변동성 타게팅과 달리 **강세장 참여를
#   덜 희생**한다(변동성 타게팅은 추세가 강할 때 오히려 디레버리지한다).
#   우리 어휘에는 그 수단이 없었다.
#
# ▶ 왜 지수가 아니라 수익률 중앙값인가
#   단일 지수를 만들려면 구성·가중·리밸런스 규칙을 또 정해야 하고, 종목이
#   드나들 때 평균 종가는 왜곡된다. 유니버스의 `days` 일 수익률 **중앙값**은
#   그 왜곡이 없고 "시장이 오르는 중인가" 에 직접 답한다. 이동평균 위/아래
#   판정과 같은 것을 재면서 구성 규칙을 하나 덜 만든다.
#
# ▶ PIT
#   호출부가 `t-1` 까지의 날짜를 넘긴다. 오늘 수익률을 보고 오늘 노출을
#   정하면 그건 위험관리가 아니라 미래 정보다.


def market_trend_up(market: Market, until: date, days: int) -> bool | None:
    """유니버스의 `days` 일 수익률 중앙값이 양(+)인가.

    반환 `None` = **판정하지 않음**(표본 부족). 못 잰 것을 "하락" 으로 몰면
    이력이 짧은 구간에서 전략이 통째로 현금이 된다 - 그건 필터가 아니라 사고다.
    """
    if days <= 0:
        return None
    rets = market.momentum(until, days)
    vals = [v for v in rets.values() if v is not None]
    if len(vals) < _TREND_MIN_NAMES:
        return None
    vals.sort()
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    return med > 0.0


def risk_exposure(config: dict, equity: list,
                  market_up: bool | None = None) -> tuple[float, str]:
    """오늘의 목표 익스포저. `1.0` = 완전투자, `0.0` = 전량 현금.

    **어제까지의 자기 자산곡선만 본다** - `equity` 는 오늘 평가 전이므로
    구조적으로 PIT 다. 미래를 보고 위험을 줄이면 그건 위험관리가 아니다.

    `market_up` 은 호출부가 `t-1` 까지로 판정한 시장 추세다(`None` = 미판정).
    """
    vt = config.get("vol_target_annual")
    dd_stop = config.get("max_drawdown_stop")
    cap_raw = config.get("max_exposure")
    trend_days = config.get("trend_filter_days")
    cap = float(cap_raw) if cap_raw is not None else 1.0
    if vt is None and dd_stop is None and cap_raw is None and trend_days is None:
        return 1.0, ""                       # 손잡이 꺼짐 - 예전 그대로

    # ▶ **추세 필터를 먼저 본다.** 시장이 내려가는 구간에서는 자기 곡선이
    #   아직 안 꺾였어도 노출을 줄인다 - 낙폭 정지(자기 곡선)는 이미 맞은
    #   뒤에 반응하고, 이것은 그 전에 반응하라는 수단이다.
    #   미판정(None)이면 **적용하지 않는다** - 못 잰 것을 하락으로 몰지 않는다.
    if trend_days is not None and market_up is False:
        raw = config.get("trend_filter_exposure")
        # 기본은 전량 현금. 값은 우리가 정하지 않는다 - 기획안이 준다.
        te = 0.0 if raw is None else float(raw)
        return max(0.0, min(te, cap)), "추세이탈"

    vals = [v for _, v in equity]
    if len(vals) < 2:
        return min(1.0, cap), "이력없음"

    # ① 낙폭 정지. **고점 대비**로 본다 - 시작가 대비로 재면 오래 번 전략이
    #    영원히 안 멈춘다. 리밸런스일에만 평가된다(그 사이엔 안 판다).
    if dd_stop is not None:
        peak = max(vals)
        if peak > 0 and (vals[-1] / peak - 1.0) <= float(dd_stop):
            return 0.0, "낙폭정지"

    # ② 변동성 타게팅. 실현변동성이 목표보다 크면 그만큼 줄인다.
    exposure = cap
    if vt is not None:
        lb = int(config.get("vol_lookback_days") or 60)
        w = vals[-(lb + 1):]
        rets = [w[k] / w[k - 1] - 1.0 for k in range(1, len(w)) if w[k - 1] > 0]
        if len(rets) < _VOL_MIN_OBS:
            # **표본이 모자라면 추정하지 않는다** - 짧은 표본의 변동성으로
            # 레버리지를 정하면 초반에 과대 노출된다. 상한만 지킨다.
            return min(1.0, cap), "표본부족"
        mu = sum(rets) / len(rets)
        var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
        rv = math.sqrt(var) * math.sqrt(252)
        exposure = min(cap, float(vt) / rv) if rv > 1e-9 else cap
    return max(0.0, exposure), ""


def run_backtest(market: Market, config: dict, *,
                 trials: int = 1) -> BacktestResult:
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
    exposures: list[float] = []              # 리밸런스일별 목표 익스포저
    risk_notes: list[str] = []
    _risk_on = risk_enabled(config)
    # 벤치마크도 **같은 배수**를 쓴다 - 전략만 비용을 물면 비교가 거짓이다
    _cost_mult = cost_multiplier(config)
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
            # ▶ **오늘까지의** 거래대금으로 유동성 계층을 정한다. 미래
            #   거래대금을 보면 비용 전체가 미래 정보가 된다.
            adv = market.adv_until(market.dates[i - 1])
            if ranked:
                tradable = [s for s in ranked if (d, s) in market.opens]
                port_value = cash + sum(
                    q * last_close.get(s, 0.0) for s, q in shares.items())
                # ▶ 위험관리. 기본 1.0 이라 손잡이를 안 켜면 예전과 동일하다.
                #   `equity` 는 오늘 평가 전이므로 어제까지만 담겨 있다(PIT).
                exposure = 1.0
                if _risk_on:
                    # 시장 추세는 **t-1 까지**로 판정한다(PIT). 손잡이를 안
                    # 켰으면 계산하지 않는다 - 켠 실험만 비용을 문다.
                    _mkt_up = None
                    _tfd = config.get("trend_filter_days")
                    if _tfd is not None:
                        _mkt_up = market_trend_up(market, market.dates[i - 1],
                                                  int(_tfd))
                    exposure, _why = risk_exposure(config, equity,
                                                   market_up=_mkt_up)
                    exposures.append(exposure)
                    if _why and _why not in risk_notes:
                        risk_notes.append(_why)
                target_value = ((port_value * exposure / len(tradable))
                                if tradable else 0.0)

                # 매도 먼저 (현금 확보) - 대상에서 빠졌거나 초과 보유분
                for s in list(shares):
                    px = market.opens.get((d, s))
                    if px is None:
                        continue            # 오늘 시세 없음 - 보유 유지 (아래 노트)
                    want = target_value / px if s in tradable else 0.0
                    diff = shares[s] - want
                    if diff * px > 1.0:     # 1원 미만 잔차는 무시
                        sb = slippage_bps_for(s, adv=adv.get(s))
                        exec_px = px * (1 - sb / 1e4)
                        notional = diff * exec_px
                        fee = _apply_costs("SELL", notional, sb, _cost_mult)
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
                    sb = slippage_bps_for(s, adv=adv.get(s))
                    exec_px = px * (1 + sb / 1e4)
                    want = target_value / px
                    diff = want - shares.get(s, 0.0)
                    if diff * px > 1.0:
                        notional = diff * exec_px
                        fee = _apply_costs("BUY", notional, sb, _cost_mult)
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
    # ▶ 위험관리를 **켰을 때만** 지표를 붙인다. 안 켰는데 `avg_exposure=1.0`
    #   을 적으면 "위험관리를 했는데 1.0 이었다" 로 읽힌다 - 안 한 것과 다르다.
    if exposures:
        metrics["avg_exposure"] = round(sum(exposures) / len(exposures), 4)
        metrics["min_exposure"] = round(min(exposures), 4)
        metrics["days_de_risked"] = sum(1 for e in exposures if e < 0.999)
        if risk_notes:
            notes.append("위험관리: " + ", ".join(risk_notes))
    # ▶ 벤치마크 대비를 **항상** 붙인다. 절대수익만 보면 시장이 오른 것을
    #   전략의 실력으로 착각한다.
    # ── 과적합 통계 (계약 quant_v2 가 요구하는 필드) ────────────────────────
    # ▶ 계산과 판정을 **같이** 붙인다. 계약에 필드만 있고 채우는 코드가
    #   없던 상태가 오늘만 세 번째였다(F-Score, industry_code, trial_pressure).
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from overfit_stats import bootstrap_ci, deflated_sharpe

        eq = [v for _, v in equity]
        rets = [eq[i] / eq[i - 1] - 1.0
                for i in range(1, len(eq)) if eq[i - 1]]
        # ▶ trials 는 **config 가 아니라 인자로** 받는다. config 는 통째로
        #   input_hash 에 들어가므로, 여기 넣으면 같은 실험이 5번째냐 6번째냐에
        #   따라 해시가 달라져 중복 가드가 무력해진다. 시도 횟수는 백테스트의
        #   입력이 아니라 결과 Sharpe 에 거는 사후 보정이다.
        n_trials = max(1, int(trials or 1))
        for src in (deflated_sharpe(rets, trials=n_trials), bootstrap_ci(rets)):
            for k, v in src.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    metrics[k] = v          # metrics 는 numeric 전용이다
    except Exception as e:  # noqa: BLE001
        notes.append(f"과적합 통계 실패: {type(e).__name__}")

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
    # ▶ **손익이 몇 종목에서 나왔나** (2026-08-12 개방)
    #   `low_volatility` 가 10년에서 초과 +855.92%p 를 냈는데, 상위 3종목이
    #   실현손익의 63%, 한 종목의 한 체결이 34% 였다. 원인은 액면분할 미조정
    #   이었다(×10.0000·×5.0000 같은 정확한 정수배가 원천에 남아 있다).
    #
    #   **저변동성 선택은 이 결함에 구조적으로 노출된다** - 정체 가격 종목이
    #   측정 변동성이 낮아 계속 뽑히고, 그 종목이 바로 분할 점프를 갖는다.
    #   빈도는 0.002% 인데 성과의 34% 를 만들었다 - 빈도로는 안 보인다.
    #
    #   판정하지 않고 **재기만 한다.** 임계는 관문이 정할 일이고, 집중이
    #   반드시 결함인 것도 아니다(진짜 소수 종목 전략이 있다). 다만 이 수를
    #   안 남기면 아무도 못 본다.
    _by_sym: dict[str, float] = {}
    for f in sells:
        _by_sym[f.instrument_id] = (_by_sym.get(f.instrument_id, 0.0)
                                    + float(f.realized_pnl))
    _pos = sorted((v for v in _by_sym.values() if v > 0), reverse=True)
    _gross = sum(_pos)
    top1 = round(_pos[0] / _gross, 4) if _gross > 0 and _pos else None
    top3 = round(sum(_pos[:3]) / _gross, 4) if _gross > 0 and _pos else None
    return {
        "total_return": round(total, 6), "cagr": round(cagr, 6),
        "ann_vol": round(ann_vol, 6), "sharpe_rf0": round(sharpe, 4),
        "max_drawdown": round(mdd, 6),
        "turnover_total": round(traded_notional / capital, 4),
        "n_fills": len(fills), "n_sells": len(sells),
        "sell_win_rate": round(wins / len(sells), 4) if sells else None,
        "total_fees": round(total_fees, 2),
        "final_equity": round(values[-1], 2),
        # 못 재면 키를 안 넣는다 - 0 으로 채우면 "집중이 없었다" 로 읽힌다
        **({"pnl_top1_share": top1} if top1 is not None else {}),
        **({"pnl_top3_share": top3} if top3 is not None else {}),
        "n_pnl_symbols": len(_by_sym) or None,
    }


# ---------------------------------------------------------------------------
# Dataset 로드 + 무결성 재검증
# ---------------------------------------------------------------------------

# ── 미시구조 적재 ───────────────────────────────────────────────────────────
#
# ▶ 왜 별도 적재인가 (2026-08-14)
#   호가·체결은 일봉과 **다른 데이터셋**이다(`krx-microstructure-daily`).
#   실험은 dataset_id 하나를 쓰므로, 일봉을 본체로 두고 미시구조는 **보조로
#   덧붙인다.** 커버리지도 다르다 - 일봉은 2016~, 미시구조는 2026-05-18~
#   (61 거래일 · 2,489종목). 창이 짧아 백테스트 관문은 어렵지만 횡단면
#   2,489 × 61기간이면 **신호 IC 는 잰다.**
#
# ▶ 왜 신호가 요구를 선언하나
#   가설이 `micro_dataset=...` 같은 키를 따로 적게 하면 그 키를 빠뜨린 가설은
#   조용히 미시구조 없이 돈다(오늘 `trend_filter_days` 가 그렇게 무시된 사고가
#   있었다). 대신 **템플릿이 `needs_micro` 로 선언**하면 신호를 고르는 순간
#   요구가 따라온다 - 빠뜨릴 자리가 없다.
#
# ▶ 품질 상태는 지우지 않는다
#   `quality_status`(PASS/WARN)를 값과 함께 담는다. 여기서 WARN 을 버리면
#   "못 잰 것" 과 "재긴 쟀는데 표본이 얇은 것" 이 같아진다 - 거르는 판단은
#   신호가 하고, 적재는 사실을 그대로 남긴다.
MICRO_FEATURES = ("spread_bps", "depth_imbalance", "order_flow_imbalance",
                  "trade_intensity", "realized_volatility",
                  # 거래대금·체결수량 (2026-08-14, 데이터셋 v2). `traded_value`
                  # 단위는 백만원 - 일봉 `notional` 과 같은 눈금이다.
                  "traded_value", "traded_volume",
                  # 일중 구간 피처 (2026-08-14, 데이터셋 v3). 여전히 일별 한
                  # 행이라 실행면 규약은 그대로다 - 하루를 평균으로 뭉개지
                  # 않을 뿐이다.
                  "ofi_close", "ofi_open", "ofi_intraday_std",
                  "close_vs_vwap", "spread_close_ratio")

MICRO_DATASET = ("krx-microstructure-daily", "v3")


def attach_micro(market: Market, rows: list[dict]) -> int:
    """미시구조 일별 집계를 `Market.micro` 에 붙인다. 반환: 실린 (날짜,종목) 수."""
    n = 0
    for r in rows:
        d = r.get("trade_date")
        iid = str(r.get("instrument_id") or "")
        if not d or not iid:
            continue
        vals: dict = {}
        for k in MICRO_FEATURES:
            v = r.get(k)
            if v in (None, "", "None"):
                continue
            try:
                vals[k] = float(v)
            except (TypeError, ValueError):
                continue          # 숫자가 아니면 안 담는다(지어내지 않는다)
        if not vals:
            continue
        qs = r.get("quality_status")
        if qs not in (None, "", "None"):
            vals["quality_status"] = str(qs)
        market.micro[(d, iid)] = vals
        n += 1
    return n


def attach_micro_if_needed(market: Market, config: dict, conn) -> int:
    """전략이 미시구조를 요구하면 붙인다. 요구가 없으면 **아무것도 안 한다**.

    반환: 실린 (날짜,종목) 수. 0 이면 요구가 없었거나 데이터가 없었다는 뜻이라
    호출부가 로그로 구분해 준다 - 조용히 비면 신호가 전 종목 미산출로 죽는데
    그 이유가 안 보인다.
    """
    from strategy_templates import resolve  # noqa: PLC0415

    # ▶ 수식(AST)이면 **트리가 자기 요구를 말한다** - 가설이 따로 안 적어도
    #   미시구조 필드를 읽으면 자동으로 실린다(alpha_ast.needs_micro).
    expr = config.get("signal_expr")
    if expr:
        from alpha_ast import needs_micro as _expr_needs_micro  # noqa: PLC0415
        from alpha_ast import parse as _parse_expr

        if not _expr_needs_micro(_parse_expr(expr)):
            return 0
    else:
        tpl = resolve(str(config.get("strategy") or ""))
        if tpl is None or not getattr(tpl, "needs_micro", False):
            return 0
    name, ver = MICRO_DATASET
    *_rest, mrows = load_dataset(conn, name, ver)
    n = attach_micro(market, mrows)
    # ▶ **붙인 즉시 표본을 그 커버리지로 자른다** - 아래 함수 주석 참조.
    #   붙이기와 자르기를 갈라 두면 한쪽만 부르는 자리가 생긴다(오늘 EDGE_KEYS
    #   를 넣고 `_take_risk` 목록을 빠뜨려 손잡이가 조용히 무시된 사고가 있었다).
    dropped, lo, hi = clip_to_micro_coverage(market)
    if dropped:
        print(f"  표본을 미시구조 커버리지로 잘랐다: {lo}~{hi} "
              f"({len(market.dates):,}일 유지 / {dropped:,}일 제외)", flush=True)
    return n


def clip_to_micro_coverage(market: Market) -> tuple[int, date | None, date | None]:
    """`Market` 을 미시구조가 실제로 있는 기간으로 **제자리 축소**한다.

    ▶ 왜 (2026-08-14 실측, 첫 수식형 알파 `332fdec9`)
      일봉은 2016-01-03~2026-08-09(2,599거래일)인데 미시구조는
      2026-05-17~08-13(61거래일)뿐이다. 그런데 실험은 전 기간을 돌았다.
      미시구조가 없는 97.7% 구간에서는 수식이 값을 못 내 거래가 없었고,
      **그 현금 구간이 그대로 성적에 들어갔다.**

        초과 -102.39%p · Sharpe -0.3185 · IR -0.4839
        walk-forward 20창 중 18창(2016H2~2025H2)이 전부 정확히 0

      -102%p 는 신호가 나쁘다는 증거가 아니라 **벤치마크가 10년간 +82.86%
      오르는 동안 우리가 현금이었다**는 뜻이다. 신호에 대한 정보가 0인데
      숫자는 참혹해서, 이대로면 환류가 "미시구조는 안 된다" 를 가르친다.

      거래 0 을 성과로 세지 않는 규율은 이미 있었지만(`experiment_did_not_trade`,
      `lessons_from` 의 UNDERPOWERED_DATA) **전체 단위**라 통과했다 - 실험
      전체로는 12,835건 체결했기 때문이다. 진짜 문제는 표본 기간이었다.

    ▶ 왜 여기서 자르나
      벤치마크는 `buy_and_hold_equity(market, config)` 로 **같은 Market** 에서
      나온다. 그래서 Market 을 자르면 전략과 벤치마크가 같은 구간을 보게 되고
      초과·IR·낙폭이 전부 같은 기간의 것이 된다. 밖에서 따로 자르면 둘이
      어긋난다.

      표본이 61거래일로 줄면 walk-forward 도 `_short_sample_windows()` 규격
      (SHORT_SAMPLE_MAX_DAYS=120)으로 떨어진다 - 반기 창 20개를 만들어 18개가
      0 이 되는 일이 구조적으로 사라진다.

    반환: (제외한 거래일 수, 커버리지 시작, 끝)
    """
    if not market.micro:
        return 0, None, None
    days = {d for (d, _s) in market.micro}
    lo, hi = min(days), max(days)
    keep = [d for d in market.dates if lo <= d <= hi]
    dropped = len(market.dates) - len(keep)
    if not dropped:
        return 0, lo, hi
    if not keep:                    # 겹치는 날이 없다 - 자르면 표본이 사라진다
        return 0, lo, hi            # 그대로 두고 상위가 거래 0 으로 판정하게 한다
    ks = set(keep)
    market.dates = keep
    # ▶ 필드를 손으로 세지 않는다. `Market` 에 필드가 늘 때 여기를 같이 안
    #   고치면 옛 구간이 남아 신호가 미래를 본다(walk_forward.slice_market 이
    #   `notionals` 를 빠뜨려 창 21개가 백지가 된 사고가 오늘 있었다).
    for f in dataclasses.fields(market):
        v = getattr(market, f.name)
        if isinstance(v, dict) and v and isinstance(next(iter(v)), tuple):
            setattr(market, f.name, {k: x for k, x in v.items() if k[0] in ks})
    return dropped, lo, hi


# ── 단위 선언 대조 ──────────────────────────────────────────────────────────
#
# ▶ 왜 필요한가 (2026-08-14 실측 사고)
#   `krx-basket-daily/v3` 의 `notional` 은 **백만원 단위**인데 매니페스트의
#   `schema_definition` 에는 열 이름만 있고 단위가 없었다. 실행면은 코드에
#   박힌 상수(`strategy_templates.NOTIONAL_UNIT_KRW`)로 그것을 가정한다.
#   가정과 실물이 어긋나자 체결가능 유니버스가 **0종목**이 됐고, 자체점검
#   12개가 전부 통과하는 채로 실험 6건이 빈 유니버스를 돌았다.
#
# ▶ 왜 환산이 아니라 대조인가
#   로더에서 원 단위로 환산하면 비용 모델(`LIQUIDITY_TIERS` 임계 3,000 =
#   30억원)까지 연쇄로 바꿔야 하고, 그 연쇄가 바로 오늘 사고를 낸 자리다.
#   지금 필요한 것은 "조용히 배수가 틀어지는 것" 을 막는 일이므로, **선언과
#   가정을 대조해 어긋나면 멈춘다.** 환산 통일은 별도 작업으로 남긴다.
#
# ▶ 선언이 없으면 막지 않는다(아직)
#   기존 매니페스트 4건에 단위가 없다. 여기서 바로 막으면 공장이 선다.
#   경고를 남겨 채우게 하고, 채워진 뒤에는 어긋남을 확실히 잡는다 -
#   **"없음" 과 "틀림" 을 같은 등급으로 두지 않는다.**
UNIT_FACTOR_KRW = {
    "KRW": 1,
    "KRW_THOUSAND": 1_000,
    "KRW_MILLION": 1_000_000,
    "KRW_BILLION": 1_000_000_000,
}


def assert_declared_units(name: str, version: str,
                          schema_def: dict | None,
                          notional_unit: str | None = None) -> str | None:
    """매니페스트가 선언한 거래대금 단위가 실행면 가정과 같은가.

    ▶ **전용 컬럼이 정본이다.** `quant.dataset_manifests.notional_unit` 이
      선언 여부를 스키마로 강제하고(준비도 진단이 그 컬럼 존재로 판정한다),
      `schema_definition.units` 는 검산 근거 같은 서술을 담는 보조다.
      컬럼이 비어 있으면 jsonb 로 물러난다 - 옛 매니페스트 호환.

    반환: 선언된 단위 문자열(없으면 None). 어긋나면 `RuntimeError`.
    """
    from strategy_templates import NOTIONAL_UNIT_KRW  # noqa: PLC0415

    units = ((schema_def or {}).get("units") or {})
    declared = notional_unit or units.get("notional")
    if not declared:
        # ▶ **없는 열의 단위는 묻지 않는다** (2026-08-14).
        #   미시구조 데이터셋은 스프레드·불균형·체결강도만 담고 거래대금 열이
        #   아예 없는데도 이 경고가 매번 떴다. 소음은 그냥 시끄러운 게 아니라
        #   **진짜 경고를 가린다** - 같은 실행 로그에서 표본이 2,599일이라는
        #   훨씬 중대한 사실이 이 줄들 사이에 묻혀 있었다.
        cols = [str(c).lower() for c in ((schema_def or {}).get("columns") or [])]
        if cols and not any("notional" in c or "value" in c or "amount" in c
                            for c in cols):
            return None
        print(f"  ⚠ {name}/{version} 매니페스트에 거래대금 단위 선언이 없다 - "
              f"실행면은 1 단위 = {NOTIONAL_UNIT_KRW:,}원으로 가정하고 진행한다 "
              f"(schema_definition.units.notional 을 채우면 대조한다)", flush=True)
        return None
    key = str(declared).strip().upper()
    factor = UNIT_FACTOR_KRW.get(key)
    if factor is None:
        raise RuntimeError(
            f"{name}/{version} 이 모르는 거래대금 단위를 선언했다: {declared!r} - "
            f"사용 가능: {sorted(UNIT_FACTOR_KRW)}. 모르는 단위를 추측해서 "
            f"쓰면 그 순간 모든 유동성 판정이 틀어진다")
    if factor != NOTIONAL_UNIT_KRW:
        raise RuntimeError(
            f"{name}/{version} 의 거래대금 단위가 실행면 가정과 다르다: "
            f"매니페스트 {key}(={factor:,}원) vs 실행면 {NOTIONAL_UNIT_KRW:,}원. "
            f"1e{len(str(factor // NOTIONAL_UNIT_KRW or 1)) - 1}배 어긋난 채 돌면 "
            f"유동성 필터가 전 종목을 버리거나 전부 통과시킨다(2026-08-14 실측: "
            f"유니버스 0종목). 데이터나 상수 중 하나를 고친 뒤 다시 돌린다")
    return key


def load_dataset(conn, name: str, version: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            select dataset_id, universe_version_id, content_hash, row_count,
                   schema_definition, notional_unit
            from quant.dataset_manifests where name = %s and version = %s
            """, (name, version))
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"Manifest 없음: {name}/{version} - 먼저 --build")
        dataset_id, universe_id, chash, row_count, schema_def, notional_unit = row
        # 로더 경계에서 단위를 대조한다 - 아래 함수의 주석에 근거가 있다.
        # 파티션을 읽기 **전에** 막는다: 어긋난 단위로는 읽어도 쓸 수 없다.
        assert_declared_units(name, version, schema_def, notional_unit)
        cur.execute(
            """
            select partition_key, object_path, content_hash
            from quant.dataset_partitions where dataset_id = %s
            order by partition_key
            """, (dataset_id,))
        parts = cur.fetchall()

    # ▶ 명세가 있는 데이터셋은 **그 명세의 리더·해시**를 쓴다 (2026-08-12)
    #   `load_partition`/`content_hash` 는 일봉 스키마(open/high/low/close)에
    #   묶여 있어, 마이크로구조처럼 열이 다른 데이터셋에는 못 쓴다. 이걸 안
    #   가르면 매니페스트는 등재되는데 로드에서 죽는다 - 실제로 그 상태를
    #   만들어 놓고 "사상됐다" 고 보고했다.
    #
    #   일봉은 예전 경로 그대로 간다. 이미 해시 검증을 통과하며 도는 것을
    #   형식을 바꾸자고 다시 만들 이유가 없다.
    try:
        from dataset_spec import spec_for                 # noqa: PLC0415
        from spec_dataset_builder import (                # noqa: PLC0415
            content_hash as spec_hash, read_partition)
        # ▶ **스탬프를 아는 조회를 쓴다** - `SPECS.get` 은 `v1-20260812` 를 못
        #   찾고, 못 찾으면 아래 일봉 gzip 경로로 조용히 떨어진다(2026-08-12
        #   실측: `BadGzipFile: Not a gzipped file (b'PA')`).
        spec = spec_for(name, version)
    except ImportError:
        spec = None

    # ▶ **형식이 안 맞으면 다른 리더로 흘리지 않는다.** 명세를 못 찾았는데
    #   파일이 Parquet 이면 그건 "일봉이다" 가 아니라 "조회가 빗나갔다" 이다.
    #   조용히 다음 리더로 떨어지면 사고 원인이 형식 오류로 위장된다.
    if spec is None and parts:
        _first = str(parts[0][1] or "")
        if _first.endswith(".parquet"):
            raise RuntimeError(
                f"{name}/{version} 은 Parquet 인데 명세 등록부에 없다 - "
                f"dataset_spec.SPECS 에 등재하거나 버전을 확인하라 "
                f"(일봉 gzip 리더로 읽으면 BadGzipFile 로 죽는다)")
    if spec is not None:
        rows_s: list[dict] = []
        for key, path, phash in parts:
            chunk = read_partition(spec, resolve_object_path(path))
            got = spec_hash(spec, chunk)
            if got != phash:
                raise RuntimeError(
                    f"Partition {key} 해시 불일치 - 파일 변조/유실 "
                    f"(manifest {phash[:12]}… vs 파일 {got[:12]}…)")
            rows_s.extend(chunk)
        if spec_hash(spec, rows_s) != chash or len(rows_s) != row_count:
            raise RuntimeError("Dataset 전체 해시/행수 불일치 - 재생성할 것")
        return str(dataset_id), str(universe_id), chash, rows_s

    rows: list[dict] = []
    for key, path, phash in parts:
        # 원장의 object_path 는 빌드한 OS 의 구분자를 그대로 갖고 있다 - 정규화한다.
        chunk = load_partition(resolve_object_path(path))
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

# 좀비 판정 최소 나이(분). 한 실험의 전체 오케스트레이션이 수 분이므로 10분이면
# 살아 있는 실행을 뺏지 않는다 - 다음 재발주는 15분 주기라 첫 재시도에서 걸린다.
ZOMBIE_RECLAIM_MIN = 10.0


def zombie_experiment(status: str, ended: bool, n_runs: int,
                      n_outcomes: int, age_min: float) -> bool:
    """미완(좀비) 실험인가 - **판정이 있으면 절대 아니다.** (순수 함수)

    좀비 서명 두 가지:
      RUNNING · 종료 없음 · backtest_runs 0 · 판정 0 · 10분 경과
      CANCELLED · backtest_runs 0 · 판정 0 (나이 무관)
    FAILED 는 좀비가 아니다 - 그건 실패라는 **기록**이고, 기록은 보호한다.

    ▶ CANCELLED 조항 (2026-08-13 실측, 추적 감시가 잡음)
      워커 스톨 회수가 버려진 RUNNING 행을 CANCELLED 로 닫게 되자, 같은 코드
      지문의 재발주가 중복 가드에서 "중복 실험 - 기존 판정을 수동 확인" 으로
      죽었다. 그 행에는 판정이 없다 - **거짓 사유**였고, 그 지문의 실험이
      영구 봉인되는 구조였다. 정보 0 인 CANCELLED 는 회수(같은 experiment_id
      로 완주) 대상이다. 나이 조건이 필요 없는 이유: 이미 명시적으로 닫힌
      행이라 살아 있는 실행을 뺏을 가능성이 없다.
    """
    if int(n_runs) != 0 or int(n_outcomes) != 0:
        return False
    if str(status) == "CANCELLED":
        return True
    return (str(status) == "RUNNING" and not ended
            and float(age_min) >= ZOMBIE_RECLAIM_MIN)


def register_and_run(name: str, version: str, *, seed: int = 0,
                     config: dict | None = None,
                     hypothesis_id: str | None = None,
                     trials: int = 1) -> dict:
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
        # ▶ trials 는 해시에 **안 들어간다**(중복 가드 보존).
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
                # ▶ **좀비와 판정을 가른다** (2026-08-13 실측, 무인 발주 1호)
                #   백테스트가 다 돈 뒤 결과 INSERT 가 일시적 읽기전용
                #   (서버 복구 창)에 걸려 죽었다. experiments 행은 RUNNING 인
                #   채 남았고 backtest_runs 도 판정도 없다. input_hash 유니크
                #   때문에 재시도는 전부 "중복 실험" 이 된다 - **인프라 깜빡임
                #   한 번이 그 지문의 실험을 영구 봉인하는 구조**였다.
                #
                #   중복 가드의 목적은 "판정을 다시 사지 않는 것"(재판정은 결과
                #   조작 여지)이다. 판정이 **없는** 미완 실험을 되살리는 것은
                #   재판정이 아니라 완주다. 그래서 좀비 서명(RUNNING · 종료
                #   없음 · run 기록 0 · 판정 0 · 10분 경과)일 때만 같은
                #   experiment_id 를 이어서 돌린다 - 새 행을 만들지 않으므로
                #   사전등록 정체성(input_hash = 실험 하나)은 그대로다.
                cur2 = conn.cursor()
                cur2.execute("""
                    select e.experiment_id::text, e.status,
                           e.ended_at is null,
                           extract(epoch from (now()-e.started_at))/60,
                           (select count(*) from quant.backtest_runs r
                             where r.experiment_id = e.experiment_id),
                           (select count(*) from research.experiment_outcomes o
                             where o.experiment_id = e.experiment_id::text)
                      from quant.experiments e where e.input_hash=%s""",
                             (ihash,))
                prev = cur2.fetchone()
                if prev and zombie_experiment(prev[1], not prev[2],
                                              int(prev[4]), int(prev[5]),
                                              float(prev[3] or 0)):
                    exp_id = prev[0]
                    print(f"미완 실험 회수({exp_id[:8]}…): {prev[1]} 인 채 "
                          f"{float(prev[3]):.0f}분 - run 기록 0 · 판정 0 이므로 "
                          f"재판정이 아니라 **완주**다. 같은 experiment_id 로 "
                          f"이어서 돈다", flush=True)
                    # status·ended_at 도 되돌린다 - CANCELLED 좀비를 회수할 때
                    # started_at 만 갱신하면 ended_at < started_at 이 되어
                    # experiments_check 제약이 터진다(2026-08-13).
                    cur2.execute("update quant.experiments set status='RUNNING',"
                                 " ended_at=null, started_at=now(),"
                                 " trace_id=%s where experiment_id=%s::uuid",
                                 (trace, exp_id))
                    conn.commit()
                else:
                    print(f"같은 input_hash 의 실험이 이미 있다({ihash[:16]}…) - "
                          f"재실행은 같은 결과라 등록하지 않는다 (재현성 계약)",
                          flush=True)
                    return {"status": 0, "duplicate": True, "input_hash": ihash,
                            "experiment_id": str(prev[0]) if prev else None}
            else:
                exp_id = str(got[0])
        conn.commit()

        market = Market.from_rows(rows)
        # ▶ **원본 행을 붙들지 않는다** (2026-08-14 실측)
        #   `load_dataset` 이 돌려준 dict 리스트(v3 = 725만 행)와 `Market`
        #   이 동시에 살아 있으면 같은 데이터를 두 벌 든다. 미시구조까지
        #   더 실으면 그 자리에서 `OSError: Cannot allocate memory` 로
        #   죽는다 - 실제로 OFI 첫 실험이 그렇게 실패했다. 위 두 줄 이후
        #   `rows` 를 읽는 곳이 없으므로 즉시 놓는다.
        del rows
        gc.collect()
        # ▶ 신호가 요구하면 미시구조를 붙인다(2026-08-14). 요구가 없으면
        #   아무 일도 일어나지 않아 예전 실험의 재현이 그대로다.
        _micro_n = attach_micro_if_needed(market, config, conn)
        if _micro_n:
            print(f"  미시구조 적재 {_micro_n:,}건 (호가·체결 일별 집계)",
                  flush=True)
        run_row_id = None
        try:
            result = run_backtest(market, config, trials=trials)
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
    # ▶ **기대값을 하드코딩한다** - COST_MODEL 에서 다시 계산하면 비용이
    #   바뀌어도 검사가 따라 바뀌어 아무것도 못 지킨다. 실제로 슬리피지를
    #   5.0 -> 7.2bps(실측)로 올렸을 때 이 검사가 먼저 걸렸다.
    #   매수 1.5 + 7.2 = 8.7bps / 매도 + 세금 15.0 = 23.7bps
    buy = _apply_costs("BUY", 1_000_000)
    sell = _apply_costs("SELL", 1_000_000)
    assert abs(buy - 870.0) < 1e-9, buy
    assert abs(sell - 2370.0) < 1e-9, sell

    # 종목별 실측이 있으면 그것을 쓰고, 없으면 시장 중앙값으로 떨어진다
    assert slippage_bps_for("005930", {"005930": 4.0}) == 2.0      # 왕복/2
    assert slippage_bps_for("005930", {}) == COST_MODEL["slippage_bps"]
    assert slippage_bps_for(None, {"005930": 4.0}) == COST_MODEL["slippage_bps"]
    # 0 이나 음수는 실측이 아니다 - 기본값으로 떨어진다
    assert slippage_bps_for("X", {"X": 0}) == COST_MODEL["slippage_bps"]

    # ▶ **유동성 계층이 단조로운가.** 얇을수록 비싸야 한다 - 뒤집히면
    #   소형주 전략이 부당하게 유리해지고 백테스트가 거짓말을 한다.
    tiers = [slippage_bps_for(adv=a) for a in (1_000, 5_000, 15_000, 50_000, 1e6)]
    assert tiers == sorted(tiers, reverse=True), tiers
    assert tiers[0] > tiers[-1], tiers
    # 실측 있으면 계층보다 우선한다
    assert slippage_bps_for("A", {"A": 4.0}, adv=1_000) == 2.0
    # 거래대금이 없으면 계층을 못 쓴다 - 기본값(지어내지 않는다)
    assert slippage_bps_for("A", None, adv=None) == COST_MODEL["slippage_bps"]
    assert slippage_bps_for("A", None, adv=0) == COST_MODEL["slippage_bps"]
    print("  FIFO 손익·비용 산식      OK")


def _check_risk_adjusted_metrics_are_numeric_and_honest():
    """**위험조정 계측이 수치만 담고, 못 재면 키를 안 만든다** (2026-08-14).

    metrics 는 numeric 컬럼에 그대로 적재되므로 문자열·불리언·NaN 이 하나라도
    섞이면 insert 가 DatatypeMismatch 로 죽어 실험이 종결되지 못한다(이 파일이
    같은 사고를 두 번 기록하고 있다). 그리고 vol 이 0 이면 M² 는 정의되지
    않는데, 그때 0 을 채우면 관문·환류가 "쟀는데 0" 으로 읽는다.
    """
    from datetime import timedelta

    d0 = date(2025, 1, 1)
    days = [d0 + timedelta(days=i) for i in range(120)]
    # 전략: 저변동 완만 상승 / 벤치마크: 고변동 큰 상승 (실측 구도 그대로)
    se = [(d, 100.0 * (1.0 + 0.0004 * i + 0.001 * ((i % 5) - 2)))
          for i, d in enumerate(days)]
    be = [(d, 100.0 * (1.0 + 0.0009 * i + 0.004 * ((i % 7) - 3)))
          for i, d in enumerate(days)]
    out = excess_metrics(se, be)
    for k in ("m2_excess_ann_pct", "alpha_ann_pct", "appraisal_ratio",
              "strategy_ann_vol_pct", "benchmark_ann_vol_pct",
              "beta_vs_benchmark", "corr_vs_benchmark"):
        assert k in out, f"{k} 가 안 나왔다: {sorted(out)}"
    for k, v in out.items():
        assert isinstance(v, (int, float)) and v == v, f"{k}={v!r} 는 수치가 아니다"
    # 벤치마크가 완전 평탄(vol 0)이면 M²·beta 는 정의 불가 - 키가 없어야 한다
    flat = [(d, 100.0) for d in days]
    out2 = excess_metrics(se, flat)
    for k in ("m2_excess_ann_pct", "beta_vs_benchmark", "appraisal_ratio"):
        assert k not in out2, f"vol 0 인데 {k} 를 지어냈다: {out2.get(k)}"
    # 표본이 짧으면 위험조정 계측을 아예 안 낸다(21일 미만)
    short = excess_metrics(se[:10], be[:10])
    assert "m2_excess_ann_pct" not in short, short
    print("  위험조정 계측 정직        OK")


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


def _crash_market(n: int = 400):
    """오르다 크게 빠지고 회복하는 시장. **낙폭 제어가 의미를 갖는 모양이다.**"""
    px, series = 100.0, []
    for i in range(n):
        if i < n * 0.5:
            px *= 1.004                      # 상승장
        elif i < n * 0.65:
            px *= 0.975                      # 급락 (약 -45%)
        else:
            px *= 1.003                      # 회복
        series.append(round(px, 4))
    # 두 종목이 같은 모양이면 순위가 무의미하다 - 약간 어긋나게 둔다
    other = [round(v * (1 + 0.02 * ((i % 11) - 5) / 100), 4)
             for i, v in enumerate(series)]
    return _mk_market({"AAA": series, "BBB": other})


def _check_pnl_concentration_is_measured():
    """**손익이 몇 종목에서 나왔는지 잰다.** (2026-08-12 사고 원문)

    `low_volatility` 가 10년 초과 +855.92%p 를 냈는데 한 종목의 한 체결이
    34% 였다 - 원천의 액면분할 미조정(×10.0000)을 먹은 것이었다. 그 수치가
    지표에 없어서 관문도 사람도 볼 수가 없었다.
    """
    from datetime import date as _d

    def _f(sym, pnl):
        return Fill(_d(2026, 1, 5), sym, "SELL", 1.0, 100.0, 0.0, pnl)

    eq = [(_d(2026, 1, 5), 100.0), (_d(2026, 1, 6), 110.0)]
    # 한 종목이 이익의 대부분을 만든 경우
    m = compute_metrics(eq, [_f("A", 900.0), _f("B", 50.0), _f("C", 50.0)],
                        100.0, 10.0)
    assert abs(m["pnl_top1_share"] - 0.9) < 1e-9, m["pnl_top1_share"]
    assert abs(m["pnl_top3_share"] - 1.0) < 1e-9, m["pnl_top3_share"]
    assert m["n_pnl_symbols"] == 3, m

    # 고르게 난 경우 - 같은 총이익이어도 집중도가 다르다
    m2 = compute_metrics(eq, [_f(s, 100.0) for s in "ABCDEFGHIJ"], 100.0, 10.0)
    assert m2["pnl_top1_share"] < 0.15, m2["pnl_top1_share"]

    # ▶ **못 재면 키를 안 넣는다.** 0 으로 채우면 "집중이 없었다" 로 읽힌다
    m3 = compute_metrics(eq, [], 100.0, 0.0)
    assert "pnl_top1_share" not in m3, m3
    # 손실만 난 경우도 분모가 없다 - 억지로 만들지 않는다
    m4 = compute_metrics(eq, [_f("A", -10.0)], 100.0, 1.0)
    assert "pnl_top1_share" not in m4, m4
    print("  손익 집중도 측정         OK")


def _check_risk_knobs_off_change_nothing():
    """**손잡이를 안 켜면 한 비트도 안 바뀐다.** (사전등록 무결성)

    러너에 손잡이를 더하면서 기존 실험 결과가 흔들리면 `input_hash` 가 걸어 둔
    사전등록이 무너진다. 안 켠 경우는 예전 경로 그대로여야 한다.
    """
    m = _crash_market()
    base = run_backtest(m, dict(DEFAULT_CONFIG, top_n=2))
    same = run_backtest(m, dict(DEFAULT_CONFIG, top_n=2,
                                vol_target_annual=None, max_drawdown_stop=None,
                                max_exposure=None))
    assert base.metrics == same.metrics, "손잡이를 껐는데 결과가 달라졌다"
    # 안 켰으면 위험관리 지표를 **적지 않는다** - 1.0 을 적으면 한 것처럼 읽힌다
    assert "avg_exposure" not in base.metrics, base.metrics.keys()
    assert not risk_enabled(dict(DEFAULT_CONFIG))
    assert risk_enabled(dict(DEFAULT_CONFIG, max_drawdown_stop=-0.2))
    # vol_lookback_days 만으로는 켜지지 않는다 (그건 보조 손잡이다)
    assert not risk_enabled(dict(DEFAULT_CONFIG, vol_lookback_days=40))
    print("  손잡이 꺼짐=이전과 동일  OK")


def _check_drawdown_stop_actually_cuts_drawdown():
    """**켜면 실제로 낙폭이 준다.** 손잡이가 이름만 있으면 없는 것과 같다."""
    m = _crash_market()
    cfg = dict(DEFAULT_CONFIG, top_n=2)
    base = run_backtest(m, cfg)
    stopped = run_backtest(m, dict(cfg, max_drawdown_stop=-0.15))

    b_mdd = base.metrics["max_drawdown"]
    s_mdd = stopped.metrics["max_drawdown"]
    assert b_mdd < -0.25, f"시장이 충분히 안 빠졌다 - 검사가 의미 없다: {b_mdd}"
    assert s_mdd > b_mdd, f"낙폭 정지를 켰는데 낙폭이 안 줄었다: {b_mdd} -> {s_mdd}"
    # 실제로 위험을 줄인 날이 있어야 한다 - 지표가 그것을 증언한다
    assert stopped.metrics["min_exposure"] == 0.0, stopped.metrics
    assert stopped.metrics["days_de_risked"] >= 1, stopped.metrics
    print(f"  낙폭정지 효과 확인       OK ({b_mdd:.1%} -> {s_mdd:.1%})")


def _check_risk_is_pit():
    """**미래를 보고 위험을 줄이지 않는다.**

    폭락 뒤 구간을 잘라내도 그 이전 판단이 같아야 한다 - 뒤를 봤다면 달라진다.
    """
    full = _crash_market(400)
    cut = _crash_market(400)
    cut_dates = set(cut.dates[:230])         # 폭락 직후까지만
    cut.dates = [d for d in cut.dates if d in cut_dates]

    cfg = dict(DEFAULT_CONFIG, top_n=2, max_drawdown_stop=-0.15,
               vol_target_annual=0.15)
    a = run_backtest(full, cfg)
    b = run_backtest(cut, cfg)
    # 겹치는 구간의 자산곡선이 같아야 한다
    n = len(b.equity)
    assert n > 60, n
    for (d1, v1), (d2, v2) in zip(a.equity[:n], b.equity):
        assert d1 == d2 and abs(v1 - v2) < 1e-6, (
            f"뒤를 보고 판단이 바뀌었다: {d1} {v1} vs {v2}")
    print("  위험관리도 PIT          OK")


def _check_fingerprint_covers_every_module_that_moves_results():
    """**지문은 결과를 정하는 코드 전부를 세야 한다** (2026-08-14 실측).

    예전에는 이 파일 하나만 해시했다. 시그널과 PIT 뷰는 `strategy_templates`
    에 있는데도 지문 밖이었고, 그래서 거래대금 단위 버그를 고쳐도 지문이 안
    바뀌었다 - 체결 0 으로 끝난 `9354f7fd` 가 그 지문을 계속 쥐어 재실행
    주문 2건이 "중복 실험" 으로 죽었다.

    여기서 고정하는 것 두 가지:
      ① 어느 부분이 바뀌어도 지문이 바뀐다(내용도, 파일 이름도).
      ② **같은 디렉터리에서 top-level import 하는 모듈은 전부 지문에 든다.**
         새 모듈을 얹으면서 `_CODE_FILES` 를 안 고치면 여기서 걸린다 -
         "다음 사람이 기억한다" 에 기대지 않는다.
    """
    import re as _re

    base = [("a.py", b"one"), ("b.py", b"two")]
    d0 = _code_digest(base)
    assert d0 == _code_digest(list(base)), "같은 입력인데 지문이 흔들린다"
    assert d0 != _code_digest([("a.py", b"one!"), ("b.py", b"two")]), \
        "내용이 바뀌었는데 지문이 그대로다"
    assert d0 != _code_digest([("z.py", b"one"), ("b.py", b"two")]), \
        "파일 이름이 바뀌었는데 지문이 그대로다"
    # 구분자가 없으면 ('ab','') 와 ('a','b') 가 같은 지문이 된다
    assert _code_digest([("a.py", b"xy")]) != _code_digest([("a.pyx", b"y")]), \
        "이름과 내용의 경계가 없다 - 다른 조합이 같은 지문이 된다"

    here = Path(__file__).resolve().parent
    src = Path(__file__).read_text(encoding="utf-8")
    # top-level(들여쓰기 없는) import 만 본다 - 함수 안 지연 import 는 선택적
    # 경로라 여기서 강제하지 않는다(overfit_stats 처럼 필요하면 손으로 넣는다).
    imported = set()
    for m in _re.finditer(r"^(?:from|import)\s+([A-Za-z_][\w]*)", src, _re.M):
        mod = m.group(1)
        if (here / f"{mod}.py").exists():
            imported.add(f"{mod}.py")
    missing = sorted(imported - set(_CODE_FILES))
    assert not missing, (
        f"이 러너가 쓰는데 지문이 안 세는 모듈: {missing} - "
        f"_CODE_FILES 에 넣어라. 안 넣으면 그 파일을 고쳐도 지문이 안 바뀌어 "
        f"고친 실험이 '중복' 으로 막히고, 같은 지문이 다른 결과를 낸다")
    print("  지문이 결과 모듈 전부를 셈 OK "
          f"({len(_CODE_FILES)}개: {', '.join(_CODE_FILES)})")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        a = sys.argv
        def opt(n, d=None):
            return a[a.index(n) + 1] if n in a else d
        _r = register_and_run(
            opt("--dataset", "krx-basket-daily"),
            # v2 부터 notional(유동성 계층 슬리피지 재료)을 담는다.
            # v1 파티션 파일은 v2 빌드가 같은 경로에 덮어써
            # 매니페스트와 해시가 어긋난다 - 해시 가드가 실제로
            # 그것을 잡았다(재현성이 깨진 채 돌지 않는다).
            opt("--dataset-version", "v2"),
            seed=int(opt("--seed", "0")),
            config=REV_CONFIG if "--strategy-rev5" in a else None)
        raise SystemExit(int(_r.get("status", 1)))

    def _check_zombie_reclaim_never_touches_verdicts():
        """**좀비 회수가 판정을 절대 건드리지 않는다.** (2026-08-13 실측)

        무인 발주 1호가 백테스트를 다 돌고 결과 쓰기에서 일시 읽기전용에
        죽어 RUNNING 좀비가 됐다. input_hash 유니크라 재시도가 전부 "중복
        실험" - 인프라 깜빡임 한 번이 그 지문을 영구 봉인하는 구조였다.
        회수는 판정 없는 미완에만 열린다 - 기록·판정은 종류 불문 보호.
        """
        assert zombie_experiment("RUNNING", False, 0, 0, 11.0)   # 그 사고 그대로
        assert not zombie_experiment("RUNNING", False, 0, 0, 3.0)   # 살아있는 실행
        assert not zombie_experiment("RUNNING", False, 1, 0, 99.0)  # run 기록 있음
        assert not zombie_experiment("RUNNING", False, 0, 1, 99.0)  # **판정 있음**
        assert not zombie_experiment("COMPLETED", True, 1, 1, 99.0)
        assert not zombie_experiment("FAILED", True, 1, 0, 99.0)    # 실패도 기록이다
        # ▶ 스톨 회수가 닫은 CANCELLED (2026-08-13 실측: 재발주가 "중복 실험 -
        #   기존 판정을 수동 확인" 이라는 **거짓 사유**로 죽었다. 판정이 없는데)
        assert zombie_experiment("CANCELLED", True, 0, 0, 0.0)      # 나이 무관 회수
        assert not zombie_experiment("CANCELLED", True, 1, 0, 99.0)  # run 기록 보호
        assert not zombie_experiment("CANCELLED", True, 0, 1, 99.0)  # 판정 불가침
        print("  좀비 회수 != 재판정      OK")

    def _check_rebalance_vocab_matches_binder():
        """**바인더가 만드는 정책을 러너가 다 알아야 한다** (2026-08-14 실측).

        `horizon_days=2` 가 `EVERY_TRADING_DAY` 로 사상됐는데 러너 어휘에 없어
        실험이 `ValueError` 로 통째로 실패했다. 두 표가 갈리면 그 조합의 가설은
        접수·바인딩을 다 통과하고 **실행 단계에서만** 죽는다 - 가장 늦게 발각되는
        자리다. 여기서 집합을 대조해 늘릴 때 같이 늘어나게 한다.
        """
        from config_binding import REBALANCE_BY_HORIZON, _rebalance_for

        made = set(REBALANCE_BY_HORIZON.values())
        # 바인더는 표에 없는 지평도 규칙으로 사상한다 - 경계값을 훑어 실제 산출을 모은다
        for h in (1, 2, 3, 4, 5, 6, 10, 11, 20, 21, 60, 120):
            made.add(_rebalance_for(h))
        unknown = sorted(made - set(REBALANCE_POLICIES))
        assert not unknown, (
            f"바인더가 만드는데 러너가 모르는 리밸런스 정책: {unknown} - "
            f"그 지평의 가설은 실행 단계에서만 죽는다")
        # 실제로 동작하는지 - 이름만 맞고 산출이 비면 거래가 0 이 된다
        days = [date(2026, 1, d) for d in range(1, 21)]
        assert len(rebalance_days(days, {"rebalance": "EVERY_TRADING_DAY"})) == 20
        assert len(rebalance_days(days, {"rebalance": "EVERY_5_TRADING_DAYS"})) == 4
        try:
            rebalance_days(days, {"rebalance": "NOPE"})
        except ValueError as e:
            assert "사용 가능" in str(e), e
        else:
            raise AssertionError("모르는 정책이 통과했다")
        print("  리밸런스 어휘 일치       OK")

    def _check_ast_signal_path():
        """**수식(AST)이 신호가 되고, 기존 경로는 그대로다** (2026-08-14).

        표현을 열어도 옛 실험이 흔들리면 안 된다 - 그래서 `signal_expr` 가
        있는 실험만 새 경로로 가고, 없으면 예전 코드가 한 줄도 안 바뀐 채 돈다.
        """
        from alpha_ast import parse as _pe

        d0 = date(2026, 1, 5)
        days = [d0 + timedelta(days=k) for k in range(40)]
        closes, opens = {}, {}
        for j, s in enumerate(("A", "B", "C")):
            for k, d in enumerate(days):
                closes[(d, s)] = 100.0 + j * 10 + k * (j + 1)   # 기울기가 다르다
                opens[(d, s)] = closes[(d, s)]
        m = Market(dates=days, opens=opens, closes=closes, symbols=["A", "B", "C"])

        # ① 수식 경로 - 20일 수익률 상위를 산다(기울기 큰 C 가 위)
        cfg = {"strategy": "MOM-20-20", "top_n": 2, "lookback_days": 20,
               "signal_expr": _pe({"op": "ts_return", "field": "close", "n": 21})}
        got = select_targets(m, len(days) - 1, cfg)
        assert got == ["C", "B"], got

        # ② `neg` 로 방향을 뒤집으면 순서가 뒤집힌다 - 방향이 수식 안에 있다
        cfg_neg = dict(cfg, signal_expr=_pe(
            {"op": "neg", "arg": {"op": "ts_return", "field": "close", "n": 21}}))
        assert select_targets(m, len(days) - 1, cfg_neg) == ["A", "B"], \
            select_targets(m, len(days) - 1, cfg_neg)

        # ③ 수식이 없으면 **예전 경로** - 템플릿이 신호를 만든다
        plain = {"strategy": "MOM-20-20", "top_n": 2, "lookback_days": 20}
        assert select_targets(m, len(days) - 1, plain) == ["C", "B"]

        # ④ 결정론 - 같은 입력이면 같은 선택
        assert select_targets(m, len(days) - 1, cfg) == got

        # ⑤ 미시구조 요구를 트리가 말한다 - 가설이 안 적어도 실린다
        class _Boom:
            def cursor(self):
                raise AssertionError("요구하지 않았는데 데이터셋을 읽었다")
        assert attach_micro_if_needed(m, cfg, _Boom()) == 0     # 가격만 읽는 수식
        micro_cfg = dict(cfg, signal_expr=_pe(
            {"op": "ts_mean", "field": "order_flow_imbalance", "n": 3}))
        try:
            attach_micro_if_needed(m, micro_cfg, _Boom())
        except AssertionError as e:
            assert "요구하지 않았는데" in str(e)      # 읽으려 했다 = 요구를 인식했다
        else:
            raise AssertionError("미시구조 수식인데 데이터셋을 안 읽었다")
        print("  AST 신호 경로(병행)      OK")

    def _check_micro_attach():
        """**미시구조는 요구한 신호에만 붙고, 없는 값은 지어내지 않는다** (2026-08-14).

        호가·체결은 일봉과 다른 데이터셋이고 커버리지도 짧다(2026-05-18~).
        요구하지 않은 실험에 붙으면 그 실험의 재현이 깨지고, 없는 구간을 0 으로
        채우면 "호가가 없었다" 가 "불균형이 중립이었다" 로 둔갑한다.
        """
        from strategy_templates import TEMPLATES

        d0 = date(2026, 6, 1)
        m = Market(dates=[d0], opens={}, closes={(d0, "A"): 100.0},
                   symbols=["A"])
        rows = [
            {"trade_date": d0, "instrument_id": "A", "spread_bps": 12.5,
             "order_flow_imbalance": 0.31, "quality_status": "PASS"},
            # 숫자가 아닌 값·빈 값은 담지 않는다(지어내지 않는다)
            {"trade_date": d0, "instrument_id": "B", "spread_bps": "n/a",
             "order_flow_imbalance": None},
            # 키가 없으면 행 자체를 버린다 - 정체 없는 값은 붙일 자리가 없다
            {"instrument_id": "C", "spread_bps": 3.0},
        ]
        n = attach_micro(m, rows)
        assert n == 1, (n, m.micro)
        got = m.micro[(d0, "A")]
        assert got["order_flow_imbalance"] == 0.31 and got["spread_bps"] == 12.5
        assert got["quality_status"] == "PASS", "품질 상태를 버리면 WARN 을 못 가린다"
        assert ("B" not in [k[1] for k in m.micro]), "숫자가 아닌 값을 담았다"

        # 요구하지 않은 전략에는 **붙지 않는다** - conn 을 건드리면 그 자체로 실패
        class _Boom:
            def cursor(self):
                raise AssertionError("요구하지 않았는데 데이터셋을 읽었다")
        assert attach_micro_if_needed(m, {"strategy": "MOM-20-20"}, _Boom()) == 0
        # 템플릿이 요구를 선언한다 - 어휘에 미시구조 신호가 실제로 있는지 고정
        need = [t.edge_type for t in TEMPLATES.values()
                if getattr(t, "needs_micro", False)]
        assert "order_flow_imbalance" in need, need
        assert "signal_composite" in need, need
        try:
            attach_micro_if_needed(m, {"strategy": "COMBO"}, _Boom())
        except AssertionError:
            pass
        else:
            raise AssertionError("COMBO did not request the microstructure dataset")
        print("  미시구조 적재(요구한 것만) OK")

    def _check_combo_short_sample_is_inconclusive():
        """The current 61-day micro sample must not manufacture a WF verdict."""
        from strategy_templates import resolve
        from walk_forward import make_windows

        combo = resolve("COMBO")
        assert combo is not None
        warmup = combo.min_history(dict(DEFAULT_CONFIG, strategy="COMBO"))
        assert warmup == 60, warmup
        days = [date(2026, 5, 18) + timedelta(days=i) for i in range(61)]
        assert make_windows(days, warmup, embargo_days=20) == [], \
            "61 days with a 60-day warmup cannot support walk-forward evidence"
        print("  COMBO 61-day sample -> insufficient OK")

    def _check_sample_is_clipped_to_micro_coverage():
        """**미시구조를 읽으면 표본도 그 기간이어야 한다** (2026-08-14 실측).

        첫 수식형 알파 `332fdec9` 가 일봉 2,599거래일 전 구간을 돌았다.
        미시구조는 61거래일뿐이라 나머지 97.7% 는 신호가 없어 현금이었고,
        그 구간이 성적에 그대로 들어갔다 - 초과 -102.39%p, walk-forward
        20창 중 18창이 정확히 0. 신호에 대한 정보는 0 인데 숫자만 참혹해서,
        이대로면 환류가 "미시구조는 안 된다" 를 가르친다.
        """
        days = [date(2026, 1, 5) + timedelta(days=k) for k in range(40)]
        m = Market(dates=list(days), symbols=["A"],
                   opens={(d, "A"): 100.0 for d in days},
                   closes={(d, "A"): 100.0 for d in days},
                   notionals={(d, "A"): 300.0 for d in days})
        # 미시구조는 끝의 5일만 있다
        cov = days[-5:]
        m.micro = {(d, "A"): {"spread_bps": 10.0} for d in cov}

        dropped, lo, hi = clip_to_micro_coverage(m)
        assert (lo, hi) == (cov[0], cov[-1]), (lo, hi)
        assert dropped == 35 and m.dates == cov, (dropped, len(m.dates))
        # **모든 격자 필드가 같이 잘린다.** 하나라도 남으면 그 구간의 값을
        # 신호가 보게 되고, 그것은 표본 밖을 보는 것이다.
        for f in dataclasses.fields(m):
            v = getattr(m, f.name)
            if isinstance(v, dict) and v:
                stale = [k for k in v if k[0] not in set(cov)]
                assert not stale, (f.name, stale[:3])

        # 미시구조를 안 쓰면 **아무것도 안 한다** - 기존 실험의 재현이 그대로다
        m2 = Market(dates=list(days), symbols=["A"], opens={}, closes={})
        assert clip_to_micro_coverage(m2) == (0, None, None)
        assert m2.dates == days

        # 겹치는 날이 하나도 없으면 자르지 않는다 - 표본을 0 으로 만드는 대신
        # 상위가 "거래 0" 으로 판정하게 둔다(빈 표본은 오류 메시지가 안 나온다)
        m3 = Market(dates=list(days), symbols=["A"], opens={}, closes={})
        m3.micro = {(date(2020, 1, 6), "A"): {"spread_bps": 1.0}}
        assert clip_to_micro_coverage(m3)[0] == 0 and m3.dates == days

        # 자른 표본은 짧은 표본 규격으로 떨어진다 - 반기 창 20개를 만들어
        # 18개가 0 이 되는 일이 구조적으로 사라진다
        from walk_forward import SHORT_SAMPLE_MAX_DAYS
        assert len(cov) <= SHORT_SAMPLE_MAX_DAYS, (len(cov), SHORT_SAMPLE_MAX_DAYS)
        print("  표본=미시구조 커버리지   OK")

    def _check_trend_filter():
        """**시장이 내려갈 때만 줄이고, 못 재면 건드리지 않는다** (2026-08-14).

        원장 실측이 양쪽으로 막혀 있었다 - 손잡이를 끄면 IR 1.255/낙폭 -50.5%
        로 강건성 탈락, 변동성 타게팅을 켜면 낙폭은 잡히고 IR 이 -0.45 로
        무너진다. 이 필터는 그 사이를 노리는 **세 번째 수단**이라, 기존 두
        손잡이의 동작을 바꾸지 않는 것이 전제다.
        """
        eq = [(date(2024, 1, 1) + timedelta(days=k), 100.0 + k)
              for k in range(30)]

        # ① 꺼져 있으면 예전 그대로 - 새 손잡이가 기존 재현을 깨면 안 된다
        assert risk_exposure({}, eq) == (1.0, "")
        assert risk_exposure({}, eq, market_up=False) == (1.0, "")

        cfg = {"trend_filter_days": 200}
        # ② 하락 판정이면 줄인다. 기본은 전량 현금.
        exp, why = risk_exposure(cfg, eq, market_up=False)
        assert (exp, why) == (0.0, "추세이탈"), (exp, why)
        # ③ 상승이면 이 필터는 관여하지 않는다
        assert risk_exposure(cfg, eq, market_up=True)[0] == 1.0
        # ④ **미판정이면 적용하지 않는다** - 못 잰 것을 하락으로 몰면 이력이
        #    짧은 구간에서 전략이 통째로 현금이 된다(필터가 아니라 사고다)
        assert risk_exposure(cfg, eq, market_up=None)[0] == 1.0
        # ⑤ 부분 노출도 표현된다 - 값은 기획안이 정한다
        assert risk_exposure(dict(cfg, trend_filter_exposure=0.3),
                             eq, market_up=False)[0] == 0.3
        # ⑥ 상한(max_exposure)을 넘지 못한다
        assert risk_exposure(dict(cfg, trend_filter_exposure=0.8,
                                  max_exposure=0.5), eq, market_up=False)[0] == 0.5

        # ⑦ 추세 판정: 표본이 모자라면 None(지어내지 않는다)
        d0 = date(2024, 1, 2)
        days = [d0 + timedelta(days=k) for k in range(300)]
        few = Market(dates=days, opens={}, closes={
            (d, f"S{j}"): 100.0 + i for i, d in enumerate(days) for j in range(5)},
            symbols=[f"S{j}" for j in range(5)])
        assert market_trend_up(few, days[-1], 200) is None, "5종목으로 시장을 판정했다"
        # 오르는 시장 / 내리는 시장
        up = Market(dates=days, opens={}, closes={
            (d, f"S{j}"): 100.0 + i for i, d in enumerate(days)
            for j in range(60)}, symbols=[f"S{j}" for j in range(60)])
        down = Market(dates=days, opens={}, closes={
            (d, f"S{j}"): 400.0 - i for i, d in enumerate(days)
            for j in range(60)}, symbols=[f"S{j}" for j in range(60)])
        assert market_trend_up(up, days[-1], 200) is True
        assert market_trend_up(down, days[-1], 200) is False
        assert market_trend_up(up, days[-1], 0) is None, "창 0을 판정으로 쓰면 안 된다"

        # ⑧ 호출부가 **t-1** 로 판정하는지 - 오늘을 보면 그건 미래 정보다
        import ast
        import pathlib
        src = pathlib.Path(__file__).read_text(encoding="utf-8")
        body = ast.get_source_segment(src, next(
            n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == "run_backtest")) or ""
        assert "market_trend_up(market, market.dates[i - 1]" in body, \
            "추세 판정이 t-1 이 아니다 - 오늘 수익률로 오늘 노출을 정하면 누출이다"
        print("  추세 필터(세 번째 수단)  OK")

    def _check_declared_units_are_compared():
        """**선언한 단위와 실행면 가정이 어긋나면 멈춘다** (2026-08-14 실측).

        매니페스트에 단위가 없어 실행면 상수가 유일한 근거였고, 그 가정이
        실물(백만원)과 어긋나자 체결가능 유니버스가 0종목이 됐다. 자체점검
        12개가 전부 통과한 채로 실험 6건이 빈 유니버스를 돌았다.

        "없음" 과 "틀림" 을 가른다 - 미선언은 경고로 지나가고(기존 매니페스트
        4건이 그 상태다), 선언이 어긋나면 확실히 막는다.
        """
        from strategy_templates import NOTIONAL_UNIT_KRW

        # 맞는 선언은 그대로 통과하고 그 값을 돌려준다
        ok = {"columns": ["notional"], "units": {"notional": "KRW_MILLION"}}
        assert NOTIONAL_UNIT_KRW == 1_000_000, NOTIONAL_UNIT_KRW
        assert assert_declared_units("d", "v3", ok) == "KRW_MILLION"
        # 소문자·공백도 같은 선언이다 - 표기 차이로 막으면 아무도 안 채운다
        assert assert_declared_units(
            "d", "v3", {"units": {"notional": " krw_million "}}) == "KRW_MILLION"

        # 어긋난 선언은 막는다 - 이것이 오늘 사고를 미리 잡았을 자리다
        for bad_unit in ("KRW", "KRW_THOUSAND", "KRW_BILLION"):
            try:
                assert_declared_units("d", "v3", {"units": {"notional": bad_unit}})
            except RuntimeError as e:
                assert "실행면 가정과 다르다" in str(e), e
            else:
                raise AssertionError(f"{bad_unit} 어긋남이 통과했다")

        # 모르는 단위를 추측해서 쓰지 않는다
        try:
            assert_declared_units("d", "v3", {"units": {"notional": "USD"}})
        except RuntimeError as e:
            assert "모르는 거래대금 단위" in str(e), e
        else:
            raise AssertionError("모르는 단위가 통과했다")

        # 미선언은 막지 않는다(아직) - 여기서 막으면 기존 4건이 전부 선다
        assert assert_declared_units("d", "v3", None) is None
        assert assert_declared_units("d", "v3", {"columns": ["notional"]}) is None
        assert assert_declared_units("d", "v3", {"units": {}}) is None

        # ▶ **없는 열의 단위는 묻지 않는다** (2026-08-14). 미시구조는 스프레드·
        #   불균형만 담는데 매 실행마다 거래대금 경고가 떴다. 소음은 그냥
        #   시끄러운 게 아니라 **진짜 경고를 가린다** - 같은 로그에서 표본이
        #   2,599일이라는 훨씬 중대한 사실이 그 줄들 사이에 묻혀 있었다.
        import io as _io
        import contextlib as _cl
        micro_schema = {"columns": ["instrument_id", "trade_date", "spread_bps",
                                    "depth_imbalance", "order_flow_imbalance"]}
        _buf = _io.StringIO()
        with _cl.redirect_stdout(_buf):
            assert assert_declared_units("m", "v1", micro_schema) is None
        assert _buf.getvalue() == "", f"없는 열에 경고를 냈다: {_buf.getvalue()}"
        # 거래대금을 담는 데이터셋에는 **여전히** 경고한다 - 위 완화가 진짜
        # 미선언을 덮으면 안 된다
        _buf2 = _io.StringIO()
        with _cl.redirect_stdout(_buf2):
            assert_declared_units("d", "v3", {"columns": ["close", "notional"]})
        assert "거래대금 단위 선언이 없다" in _buf2.getvalue(), _buf2.getvalue()
        # 열 목록 자체가 없으면 모르는 것이니 묻는다(침묵이 기본값이 되면 안 된다)
        _buf3 = _io.StringIO()
        with _cl.redirect_stdout(_buf3):
            assert_declared_units("d", "v3", {"units": {}})
        assert "거래대금 단위 선언이 없다" in _buf3.getvalue(), _buf3.getvalue()

        # ▶ **전용 컬럼이 정본이다.** 준비도 진단이 컬럼 존재로 판정하므로,
        #   컬럼 값이 오면 그것을 먼저 본다. jsonb 는 옛 매니페스트 폴백.
        assert assert_declared_units("d", "v3", None, "KRW_MILLION") == "KRW_MILLION"
        try:
            assert_declared_units("d", "v3", ok, "KRW")   # 컬럼이 이기는지
        except RuntimeError as e:
            assert "실행면 가정과 다르다" in str(e), e
        else:
            raise AssertionError("jsonb 가 컬럼을 덮었다 - 정본이 흔들린다")

        # 로더가 실제로 이 대조를 부르는지 - 안 부르면 이 함수는 장식이다
        import ast
        import pathlib
        src = pathlib.Path(__file__).read_text(encoding="utf-8")
        body = ast.get_source_segment(src, next(
            n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == "load_dataset")) or ""
        assert "assert_declared_units(" in body, "로더가 단위를 대조하지 않는다"
        assert "notional_unit" in body, "로더가 단위 컬럼을 안 읽는다"
        print("  단위 선언 대조           OK")

    def _check_construction_modes():
        """**하위 배제 구성이 실제로 하위를 뺀다** (2026-08-14 분위 곡선 처방).

        기본(TOP_N)은 한 톨도 바뀌지 않아야 한다 - 새 손잡이가 기존 실험의
        재현을 깨면 과거 결과와 비교가 불가능해진다.
        """
        ranked = [f"S{i:02d}" for i in range(100)]      # 좋은 것부터

        # 기본은 예전 그대로
        assert _construct(ranked, {"top_n": 20}) == ranked[:20]
        assert _construct(ranked, {"top_n": 20,
                                   "portfolio_construction": "TOP_N"}) == ranked[:20]

        # 하위 배제: 남는 것은 상위 쪽이고, top_n 은 관여하지 않는다
        cfg = {"top_n": 20, "portfolio_construction": "EXCLUDE_BOTTOM",
               "exclude_bottom_pct": 10.0}
        kept = _construct(ranked, cfg)
        assert len(kept) == 90, len(kept)
        assert kept[0] == "S00" and kept[-1] == "S89", (kept[0], kept[-1])
        assert "S99" not in kept, "하위가 안 빠졌다"
        # 소문자로 선언해도 같은 구성이어야 한다(기획안이 자유 텍스트로 준다)
        assert _construct(ranked, dict(cfg, portfolio_construction="exclude_bottom")) == kept

        # 범위 밖은 거부한다 - 조용히 기본값으로 흘리면 무엇을 잰지 모른다
        for bad in (0.0, 100.0, -5.0, 150.0):
            try:
                _construct(ranked, dict(cfg, exclude_bottom_pct=bad))
            except ValueError:
                pass
            else:
                raise AssertionError(f"exclude_bottom_pct={bad} 가 통과했다")

        # 다 빼서 빈 포트폴리오가 되면 사고다 - 거래 0 은 판정할 수 없다
        try:
            _construct(["A", "B"], {"portfolio_construction": "EXCLUDE_BOTTOM",
                                    "exclude_bottom_pct": 90.0})
        except ValueError as e:
            assert "남는 종목이 없다" in str(e), e
        else:
            raise AssertionError("빈 포트폴리오가 통과했다")

        # 모르는 방식을 기본값으로 흘리지 않는다
        try:
            _construct(ranked, {"top_n": 20, "portfolio_construction": "MAGIC"})
        except ValueError as e:
            assert "모르는 구성 방식" in str(e), e
        else:
            raise AssertionError("모르는 구성 방식이 통과했다")
        print("  구성 방식(빼기 포함)     OK")

    print(f"{RUNNER_VERSION} 자체 점검 (DB 없음)")
    _check_zombie_reclaim_never_touches_verdicts()
    _check_no_lookahead()
    _check_fifo_and_costs()
    _check_risk_adjusted_metrics_are_numeric_and_honest()
    _check_metrics_and_determinism()
    _check_cash_never_negative()
    _check_strategy_catalog()
    _check_risk_knobs_off_change_nothing()
    _check_drawdown_stop_actually_cuts_drawdown()
    _check_risk_is_pit()
    _check_pnl_concentration_is_measured()
    _check_construction_modes()
    _check_fingerprint_covers_every_module_that_moves_results()
    _check_declared_units_are_compared()
    _check_trend_filter()
    _check_rebalance_vocab_matches_binder()
    _check_ast_signal_path()
    _check_micro_attach()
    _check_combo_short_sample_is_inconclusive()
    _check_sample_is_clipped_to_micro_coverage()
    print("Backtest Runner 19개 영역 통과. 실행은 --run")
