#!/usr/bin/env python3
"""**신호 자체를 검정한다** - 포트폴리오를 만들기 전에.

▶ 왜 필요한가 (2026-08-12 실측)
  지금 우리는 신호를 **포트폴리오 백테스트로만** 검정한다. 그러면 두 가지가
  뒤섞인다:

      신호 품질   이 점수가 미래 수익률을 예측하나
      구성 방식   상위 20종목 · 동일가중 · 완전투자 · 위험관리 없음

  momentum 이 정확히 그 함정에 빠졌다. 10년 표본에서 초과 -175%p 로 기각됐는데,
  그게 **신호가 죽어서인지 구성이 나빠서인지 아무도 모른다.** 상위 20종목만
  보고 나머지 3,904종목이 무슨 말을 하는지는 버렸다.

  IC 는 매 기간 **전 종목**에 대해 신호 점수와 forward return 의 순위상관을
  잰다. 3,924개 중 20개가 아니라 3,924개를 다 쓴다.

▶ 그리고 싸다
  체결·수수료·슬리피지·현금제약 시뮬레이션이 없다. 자율 공장이 하루 수백 건을
  돌리려면 **싼 1차 관문**이 반드시 먼저 있어야 한다.

▶ 문턱은 t > 3.0 이다, 2.0 이 아니다
  업계 전체가 같은 데이터를 파고 있으므로 관행적 2.0 은 너무 느슨하다는 것이
  다중검정 문헌의 결론이다. **우리가 정하는 게 아니라 문헌이 정한 값**이고,
  근거를 값 옆에 적는다.

▶ 순위상관(Spearman)을 쓴다
  피어슨은 이상치 하나에 끌려간다. 오늘 액면분할 미조정으로 ×10 점프가 있는
  것을 확인했으므로 더욱 그렇다 - 순위는 그 종목을 "1등" 으로 볼 뿐
  "10배" 로 보지 않는다.

사용:
    quant-py signal_ic.py --check
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

IC_VERSION = "quant-signal-ic-v3"

# ▶ 신규 팩터 유의 문턱. 관행 2.0 이 아니라 3.0 이다 - 같은 데이터를 업계
#   전체가 파고 있어 2.0 은 다중검정을 감안하면 너무 느슨하다는 것이
#   다중검정 문헌의 결론이다(Harvey-Liu-Zhu 계열).
MIN_T_STAT = 3.0
# 이보다 종목이 적은 기간은 횡단면이라 부를 수 없다 - 그 기간은 버린다.
MIN_NAMES = 30
# 이보다 기간이 적으면 IC 평균의 표준오차를 못 믿는다.
MIN_PERIODS = 12


@dataclass
class ICResult:
    """신호 검정 결과. **못 잰 것은 키를 안 넣는다.**"""

    periods: int = 0
    mean_ic: float | None = None
    std_ic: float | None = None
    t_stat: float | None = None
    ic_ir: float | None = None          # mean/std - 기간당 정보비율
    hit_rate: float | None = None       # IC > 0 인 기간 비율
    median_breadth: int = 0             # 기간당 중앙값 종목 수
    reason: str = ""

    @property
    def significant(self) -> bool:
        """**단측이다.** 신호가 주장한 방향으로 예측력이 있나.

        ▶ 첫 판이 `abs(t)` 를 썼다 (2026-08-12 실측이 잡음)
          방향을 정규화한 신호의 음의 t 를 "통과" 로 찍으면 **"이 신호를 쓸
          수 있나" 의 답으로는 틀렸다** - 신호가 반대로 간다는 뜻이다.
          뒤집으면 쓸 수 있지만 그건 **다른 가설**이고,
          결과를 보고 방향을 뒤집는 것은 사전등록 위반이다.
        """
        return self.t_stat is not None and self.t_stat >= MIN_T_STAT

    @property
    def inverted(self) -> bool:
        """**신호가 반대 방향으로 유의하다.** 발견이지만 이 가설의 증거는 아니다.

        다음 기획안이 방향을 뒤집어 **새로 사전등록**하면 검정할 수 있다.
        지금 실험의 결과로 쓰면 결과를 보고 가설을 바꾼 것이다.
        """
        return self.t_stat is not None and self.t_stat <= -MIN_T_STAT

    def as_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if v is not None}
        d["significant"] = self.significant
        d["inverted"] = self.inverted
        d["min_t_stat"] = MIN_T_STAT
        return d


def _ranks(values: list[float]) -> list[float]:
    """평균 순위(동점은 평균). 순위상관의 재료."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """순위상관. **피어슨을 안 쓴다** - 이상치 하나가 끌고 가면 안 된다."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx <= 0 or dy <= 0:
        return None                     # 전부 동점 - 상관을 정의할 수 없다
    return num / (dx * dy)


def ic_series(market, config: dict, *, horizon: int,
              sample_dates=None, min_names: int = MIN_NAMES) -> list:
    """기간별 `(날짜, IC, 종목수)`.

    **PIT 는 구조로 지킨다** - 신호는 `PITView(market, d)` 가 주는 것만 보고,
    forward return 은 `d` 이후를 본다. 둘이 겹치지 않는다.

    ▶ **기본은 비중첩 표본이다** (2026-08-12, 자체 점검이 잡았다)
      일별로 재면서 5일 forward return 을 쓰면 연속 IC 가 4일을 공유한다.
      그러면 t 검정의 독립 가정이 깨져 **유의성이 부풀어 오른다** - 순수
      임의보행에서 t = +3.72 가 나왔다. 독립 관측은 65개가 아니라 ~13개였다.

      `horizon` 간격으로 건너뛰면 forward 구간이 안 겹친다. 표본 수가 줄지만
      **줄어든 그 수가 진짜 수**다. 중첩을 쓰려면 HAC 보정이 필요한데,
      그건 없는 정밀도를 만드는 쪽이라 안 쓴다.
    """
    from strategy_templates import (path_crosses_unadjusted_adjustment_gap,
                                    pit_view_for, resolve)

    # The portfolio backtester gives an explicit AST precedence over the legacy
    # template.  IC must measure the *same signal*, otherwise an AST experiment
    # quietly receives MOM/REV's IC and the factory learns from the wrong formula.
    expr = config.get("signal_expr")
    parsed_expr = None
    tpl = None
    if expr:
        from alpha_ast import parse as _parse_expr  # noqa: PLC0415

        parsed_expr = _parse_expr(expr)
    else:
        tpl = resolve(str(config.get("strategy") or ""))
        if tpl is None:
            return []
    dates = list(market.dates)
    idx = {d: i for i, d in enumerate(dates)}
    if sample_dates is not None:
        targets = list(sample_dates)
    else:
        step = max(1, int(horizon))
        targets = dates[::step]
    out = []
    for d in targets:
        i = idx.get(d)
        if i is None or i + horizon >= len(dates):
            continue                    # 앞을 못 보면 그 기간은 없는 것이다
        fwd = dates[i + horizon]
        try:
            view = pit_view_for(market, d, dict(config))
            if parsed_expr is not None:
                from alpha_ast import evaluate as _eval_expr  # noqa: PLC0415

                scores = _eval_expr(parsed_expr, view)
            else:
                scores = tpl.signal(view, dict(config))
                # Template scores are raw features.  For BOTTOM templates (for
                # example low volatility), smaller raw values are the bullish
                # signal.  IC uses one convention -- larger means better -- so
                # orient them exactly as select_targets does before correlation.
                if tpl.rank == "BOTTOM":
                    scores = {sym: -float(value)
                              for sym, value in (scores or {}).items()
                              if value is not None}
        except Exception:  # noqa: BLE001 - 한 기간이 죽어도 나머지는 잰다
            continue
        xs, ys = [], []
        for sym, sc in (scores or {}).items():
            p0 = market.closes.get((d, sym))
            p1 = market.closes.get((fwd, sym))
            # **없는 값을 채우지 않는다.** 결측 종목은 그 기간에서 뺀다.
            if p0 is None or p1 is None or p0 <= 0 or sc is None:
                continue
            # The label owns the whole (d, fwd] return path, not only its two
            # endpoints.  A 2x split followed by an ordinary price move need
            # not leave an integer endpoint ratio, so inspect every observed
            # close inside the label window and leave that label unmeasured.
            price_path = [market.closes[(session, sym)]
                          for session in dates[i:i + horizon + 1]
                          if (session, sym) in market.closes]
            if path_crosses_unadjusted_adjustment_gap(price_path):
                continue
            xs.append(float(sc))
            ys.append(float(p1) / float(p0) - 1.0)
        if len(xs) < min_names:
            continue
        ic = spearman(xs, ys)
        if ic is not None:
            out.append((d, ic, len(xs)))
    return out


def summarize(series) -> ICResult:
    """IC 계열 -> 검정 결과. **표본이 모자라면 판정하지 않는다.**"""
    ics = [x[1] for x in (series or ())]
    n = len(ics)
    if n < MIN_PERIODS:
        return ICResult(periods=n,
                        reason=f"기간 {n}개 < 최소 {MIN_PERIODS} - 판정하지 않는다")
    mean = sum(ics) / n
    var = sum((x - mean) ** 2 for x in ics) / (n - 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return ICResult(periods=n, mean_ic=round(mean, 6),
                        reason="IC 산포가 0 - t 를 정의할 수 없다")
    breadths = sorted(x[2] for x in series)
    return ICResult(
        periods=n,
        mean_ic=round(mean, 6),
        std_ic=round(sd, 6),
        t_stat=round(mean / sd * math.sqrt(n), 4),
        ic_ir=round(mean / sd, 4),
        hit_rate=round(sum(1 for x in ics if x > 0) / n, 4),
        median_breadth=breadths[len(breadths) // 2],
    )


def expected_ir(res: ICResult, *, effective_breadth: int | None = None):
    """`IR ≈ IC × √breadth` (운용의 기본법칙). **상한이지 예측이 아니다.**

    ▶ 첫 판이 종목 수를 그대로 폭으로 썼다 (2026-08-12 실측이 잡음)
      `breakout` 에 기대 IR +1.911 이 찍혔다. 3,924종목을 **독립 베팅 3,924개**
      로 센 것인데, 한국 주식은 시장·섹터로 묶여 있어 실제 독립 베팅 수는
      그보다 훨씬 적다. 기본법칙의 breadth 는 **자산 수가 아니라 독립 베팅
      수**다.

      독립 베팅 수를 제대로 추정하려면 수익률 상관행렬을 클러스터링해야 한다
      (López de Prado 의 ONC). 그게 없으므로 **여기서는 상한만 말한다** -
      "이보다 좋을 수는 없다". 지어낸 추정치를 내놓는 것보다 낫다.

      `effective_breadth` 를 주면 그것을 쓴다. 없으면 종목 수(상한)를 쓰되
      호출부가 상한임을 알아야 한다 - `render` 가 그렇게 표시한다.
    """
    if res.mean_ic is None:
        return None
    b = effective_breadth if effective_breadth else res.median_breadth
    if not b or b < 1:
        return None
    return round(res.mean_ic * math.sqrt(b), 4)


def render(res: ICResult) -> str:
    if res.mean_ic is None:
        return f"IC 미측정: {res.reason or '재료 없음'}"
    eir = expected_ir(res)
    if res.significant:
        mark = "통과"
    elif res.inverted:
        mark = "**역방향** (뒤집으면 신호일 수 있으나 새 사전등록이 필요하다)"
    else:
        mark = "**미달**"
    return (f"IC {res.mean_ic:+.4f} · t {res.t_stat:+.2f} (기준 {MIN_T_STAT}) {mark}"
            f" · 적중 {res.hit_rate:.1%} · 기간 {res.periods} · 종목 {res.median_breadth}"
            # **상한**이라고 반드시 적는다 - 종목 수를 독립 베팅 수로 쓴 값이다
            + (f" · IR 상한 {eir:+.2f}" if eir is not None else ""))


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _mk(prices: dict, start=None):
    from datetime import date, timedelta

    from backtest_runner import Market
    start = start or date(2026, 1, 5)
    n = len(next(iter(prices.values())))
    dates, d = [], start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    rows = []
    for s, ser in prices.items():
        for i, px in enumerate(ser):
            rows.append({"instrument_id": s, "trade_date": dates[i], "open": px,
                         "high": px, "low": px, "close": px, "volume": 1000,
                         "observed_at": None})
    return Market.from_rows(rows)


def _check_spearman_resists_outliers():
    """**순위상관은 ×10 점프에 안 끌려간다.** 오늘 그 데이터를 확인했다."""
    xs = list(range(1, 11))
    ys = [float(v) for v in xs]
    assert abs(spearman(xs, ys) - 1.0) < 1e-9
    # 한 점을 10배로 튀겨도 순위가 그대로면 상관도 그대로다
    ys_out = list(ys); ys_out[-1] = 1000.0
    assert abs(spearman(xs, ys_out) - 1.0) < 1e-9, "순위상관이 이상치에 끌렸다"
    # 완전 역순
    assert abs(spearman(xs, list(reversed(ys))) + 1.0) < 1e-9
    # 전부 동점이면 정의할 수 없다 - 0 으로 위장하지 않는다
    assert spearman(xs, [5.0] * 10) is None
    assert spearman([1.0, 2.0], [1.0, 2.0]) is None      # 표본 부족
    print("  순위상관 이상치 내성      OK")


def _check_ic_detects_a_real_signal():
    """**진짜 신호가 있으면 IC 가 잡는다.** 안 잡으면 검정이 아니다."""
    import random
    rnd = random.Random(20260812)
    n_days, n_sym = 90, 60
    prices = {}
    # 종목마다 고정 드리프트를 주고, 모멘텀 신호가 그걸 집어내는지 본다
    for s in range(n_sym):
        drift = (s - n_sym / 2) / n_sym * 0.004      # -0.2%~+0.2% /일
        px, ser = 100.0, []
        for _ in range(n_days):
            px *= (1 + drift + rnd.gauss(0, 0.004))
            ser.append(round(px, 4))
        prices[f"S{s:03d}"] = ser
    m = _mk(prices)
    cfg = {"strategy": "MOM", "lookback_days": 20, "top_n": 10}
    ser = ic_series(m, cfg, horizon=5, min_names=30)
    res = summarize(ser)
    assert res.mean_ic is not None, res.reason
    assert res.mean_ic > 0.1, f"있는 신호를 못 잡았다: {res.mean_ic}"
    assert res.significant, render(res)
    print(f"  진짜 신호를 잡는다        OK ({render(res)[:52]})")


def _check_ic_rejects_noise():
    """**신호가 없으면 유의하지 않아야 한다.** 아무거나 통과시키면 관문이 아니다.

    ▶ 첫 판이 여기서 t = -44.69 를 냈다 (2026-08-12)
      "잡음" 이라고 만든 픽스처가 실은 **강한 평균회귀**였다 - 매 시점마다
      난수를 새로 뽑아 합쳐서 연속 가격 사이에 공통 성분이 없었다. 진짜
      임의보행이 아니라 매일 독립인 계열이었고, 그러면 오른 것은 다음에
      내린다. **검정기가 옳았고 픽스처가 틀렸다.**

      임의보행은 **누적**이어야 한다 - 어제 가격에 오늘 충격을 곱한다.
    """
    import random
    rnd = random.Random(7)
    prices = {}
    for s in range(60):
        px, ser = 100.0, []
        for _ in range(90):
            px *= math.exp(rnd.gauss(0, 0.01))   # 누적 - 진짜 임의보행
            ser.append(round(px, 4))
        prices[f"S{s:03d}"] = ser
    m = _mk(prices)
    cfg = {"strategy": "MOM", "lookback_days": 20, "top_n": 10}
    res = summarize(ic_series(m, cfg, horizon=5, min_names=30))
    assert res.mean_ic is not None
    assert not res.significant, f"잡음을 유의하다고 했다: {render(res)}"

    # ▶ **중첩 표본은 유의성을 부풀린다** - 이 검사가 그걸 잡았다.
    #   같은 데이터를 일별로(중첩) 재면 t 가 커진다. 기본이 비중첩이어야 하는
    #   이유이고, 누가 기본값을 되돌리면 여기서 실패한다.
    overlapped = summarize(ic_series(m, cfg, horizon=5, min_names=30,
                                     sample_dates=m.dates))
    assert overlapped.periods > res.periods * 3, (overlapped.periods, res.periods)
    assert abs(overlapped.t_stat) > abs(res.t_stat), \
        "중첩이 t 를 안 부풀렸다 - 이 검사가 무의미해졌다"
    print(f"  잡음은 기각한다           OK (비중첩 t {res.t_stat:+.2f} vs "
          f"중첩 t {overlapped.t_stat:+.2f} - 중첩은 거품)")


def _check_pit_is_structural():
    """**미래를 보고 신호를 만들 수 없다.** 구조로 막혀야 한다.

    신호는 `PITView(market, d)` 만 보고, forward return 은 `d` 다음을 본다.
    d 이후 가격이 바뀌어도 그날의 IC 계산에 쓰인 **신호 점수**는 같아야 한다.
    """
    from datetime import date as _d

    from strategy_templates import PITView, pit_view_for, resolve
    base = {f"S{s}": [100.0 + s + i * 0.1 for i in range(40)] for s in range(40)}
    m1 = _mk(base)
    future = {k: v[:] for k, v in base.items()}
    for k in future:                       # 마지막 5일을 통째로 바꾼다
        for i in range(35, 40):
            future[k][i] *= 3.0
    m2 = _mk(future)
    cfg = {"strategy": "MOM", "lookback_days": 20, "top_n": 10}
    tpl = resolve("MOM")
    d = m1.dates[30]                       # 바뀐 구간보다 앞
    s1 = tpl.signal(PITView(m1, d), cfg)
    s2 = tpl.signal(PITView(m2, d), cfg)
    assert s1 == s2, "미래 가격이 과거 시점 신호를 바꿨다 - PIT 파손"
    print("  PIT 가 구조로 지켜짐      OK")


def _check_ast_ic_measures_the_ast():
    """AST 실험의 IC 는 기본 템플릿이 아니라 후보 수식을 재야 한다."""
    # 종목별 추세가 강하므로 MOM 은 양의 IC 다. 같은 가격 입력을 neg 로 뒤집은
    # AST 는 음의 IC 여야 한다. 두 값이 같은 방향이면 IC 경로가 AST 를 무시한 것.
    prices = {}
    for s in range(40):
        drift = (s + 1) / 40 * 0.004
        px, ser = 100.0, []
        for _ in range(60):
            px *= 1.0 + drift
            ser.append(px)
        prices[f"S{s:03d}"] = ser
    market = _mk(prices)
    base = {"strategy": "MOM", "lookback_days": 10, "top_n": 10}
    mom = summarize(ic_series(market, base, horizon=2, min_names=30))
    ast = dict(base, signal_expr={
        "op": "neg",
        "arg": {"op": "ts_return", "field": "close", "n": 10},
    })
    inverted = summarize(ic_series(market, ast, horizon=2, min_names=30))
    assert mom.mean_ic is not None and mom.mean_ic > 0.9, mom
    assert inverted.mean_ic is not None and inverted.mean_ic < -0.9, inverted
    print("  AST IC 가 후보 수식을 측정  OK")


def _check_bottom_signal_is_oriented():
    """BOTTOM 템플릿도 '큰 IC = 좋은 방향'이라는 한 계약을 지킨다."""
    # 변동성이 낮은 종목일수록 다음 수익률이 높도록 만든다. LOWVOL 의 원시
    # 점수(변동성)는 낮을수록 좋으므로 방향을 안 뒤집으면 음의 IC 가 나온다.
    prices = {}
    for s in range(40):
        amp = 0.0002 + s * 0.00008
        drift = 0.004 - s * 0.00008
        px, ser = 100.0, []
        for i in range(90):
            px *= 1.0 + drift + (amp if i % 2 else -amp)
            ser.append(px)
        prices[f"S{s:03d}"] = ser
    market = _mk(prices)
    cfg = {"strategy": "LOWVOL", "lookback_days": 20, "top_n": 10}
    result = summarize(ic_series(market, cfg, horizon=2, min_names=30))
    assert result.mean_ic is not None and result.mean_ic > 0.8, result
    print("  BOTTOM 신호 방향 정규화    OK")


def _check_unmeasured_is_not_zero():
    """**못 잰 것을 0으로 적지 않는다.** 기간이 모자라면 판정 자체를 안 한다."""
    r = summarize([])
    assert r.mean_ic is None and r.t_stat is None and "판정하지 않는다" in r.reason
    assert not r.significant
    assert "mean_ic" not in r.as_dict(), r.as_dict()
    short = [(None, 0.5, 100)] * (MIN_PERIODS - 1)
    assert summarize(short).mean_ic is None, "기간이 모자란데 판정했다"
    # 결측 종목은 그 기간에서 빠진다 - 0 으로 채우지 않는다
    m = _mk({f"S{s}": [100.0 + i for i in range(40)] for s in range(5)})
    assert ic_series(m, {"strategy": "MOM", "lookback_days": 20},
                     horizon=5, min_names=30) == [], "종목이 모자란데 쟀다"
    print("  미측정 != 0              OK")


def _check_inverted_is_not_pass():
    """**역방향 신호를 통과로 찍지 않는다.** (2026-08-12 실측 원문)

    방향이 이미 '큰 값이 좋다'로 정규화된 신호가 t=-7.09 라면 첫 판의
    `abs(t)` 는 그것을 "통과" 로 찍었다. 신호가 **반대로 간다**는 뜻인데
    통과라고 하면 다음 사람이 그대로 쓴다. 뒤집으면 신호일 수 있지만 그건
    다른 가설이고, 결과를 보고 방향을 바꾸는 것은 사전등록 위반이다.
    """
    inv = ICResult(periods=128, mean_ic=-0.1103, std_ic=0.1857,
                   t_stat=-7.09, ic_ir=-0.594, hit_rate=0.242,
                   median_breadth=2640)
    assert not inv.significant, "역방향을 통과로 찍었다"
    assert inv.inverted, "역방향이라는 사실을 안 남겼다"
    assert "역방향" in render(inv) and "사전등록" in render(inv), render(inv)

    ok = ICResult(periods=128, mean_ic=0.0372, std_ic=0.178, t_stat=3.5,
                  hit_rate=0.602, median_breadth=2640)
    assert ok.significant and not ok.inverted
    # 문턱 아래는 둘 다 아니다 - 애매한 것을 어느 쪽으로도 밀지 않는다
    mid = ICResult(periods=128, mean_ic=0.0372, t_stat=2.36, std_ic=0.178,
                   hit_rate=0.602, median_breadth=2640)
    assert not mid.significant and not mid.inverted
    assert "미달" in render(mid)
    print("  역방향 != 통과            OK")


def _check_expected_ir_is_marked_as_upper_bound():
    """**종목 수를 독립 베팅 수로 쓰면 안 된다.** (2026-08-12 실측이 잡음)

    `breakout` 에 기대 IR +1.911 이 찍혔다. 3,924종목을 독립 베팅 3,924개로
    센 것인데 한국 주식은 시장·섹터로 묶여 있다. 지어낸 정밀도라 **상한**
    이라고 반드시 표시해야 한다.
    """
    r = ICResult(periods=128, mean_ic=0.0372, std_ic=0.178, t_stat=2.36,
                 hit_rate=0.602, median_breadth=2640)
    assert abs(expected_ir(r) - 0.0372 * math.sqrt(2640)) < 1e-4
    assert "IR 상한" in render(r), render(r)
    # 유효 폭을 주면 그것을 쓴다 - 나중에 클러스터링이 생기면 여기로 들어온다
    assert abs(expected_ir(r, effective_breadth=20)
               - 0.0372 * math.sqrt(20)) < 1e-4
    assert expected_ir(ICResult(periods=20)) is None      # IC 없으면 못 낸다
    print("  기대 IR 은 상한 표시      OK")


def _selfcheck() -> int:
    print(f"{IC_VERSION} 자체 점검 (DB 없음)")
    _check_inverted_is_not_pass()
    _check_expected_ir_is_marked_as_upper_bound()
    _check_spearman_resists_outliers()
    _check_ic_detects_a_real_signal()
    _check_ic_rejects_noise()
    _check_pit_is_structural()
    _check_ast_ic_measures_the_ast()
    _check_bottom_signal_is_oriented()
    _check_unmeasured_is_not_zero()
    print("신호 IC 9개 영역 통과.")
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="신호 자체를 검정한다(IC)")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return _selfcheck()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
