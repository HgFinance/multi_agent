"""가격 레벨 - 일봉에서 지지·저항·진입·목표·손절을 낸다. 순수 함수, I/O 없음.

**여기가 정본이다.** 같은 계산이 두 곳에서 필요하다:
  - 배치 선별(`departments/04-quant-backtest/pipeline/recommendation/`)
  - 요청 시점 종목 질의(market-api `/levels/{symbol}` -> gather_holdings_evidence)
둘이 다른 숫자를 내면 "화면과 리포트가 다르다"가 된다. 시세 파생물은 리서치
소유이므로 여기 두고, 퀀트 쪽이 이걸 import 한다.

## 검증 상태 - 이 숫자들은 재현되지만 수익을 보장하지 않는다

2026-08-25 실측(개발 2016-2022, n=5,674): **목표 선도달 31.0% vs 손절 선도달
62.1%.** 계산 규칙은 결정론이라 재현되지만, 그 규칙대로 하면 손절이 먼저 닿을
확률이 두 배다. 답변에 실을 때 `CAVEAT` 를 같이 실어야 한다.

## 계수의 출처

- `BREAKOUT_TARGET_ATR = 2.55` 는 신고가권 표본(n=2,548)의 최대유리이동
  **중앙값**이다. 수익률을 목적함수로 쓰지 않고 "절반이 실제 도달하는 거리"로
  사전등록 재보정했다(원래 3.2 는 내가 RR 관문을 통과시키려고 올린 값이었다).
- 나머지 계수는 **측정된 적이 없다.** 손절 `MAX_RISK_ATR = 2.0` 은 같은 표본의
  최대불리이동 중앙값 2.52 ATR 보다 좁아서, 절반 이상이 손절에 먼저 닿는
  구조적 원인이다. 고치려면 새 사전등록이 필요하다.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# 답변·리포트에 이 계획을 실을 때 반드시 같이 나가야 하는 문구.
CAVEAT = ("과거 검증에서 목표 선도달 31.0%, 손절 선도달 62.1%였다"
          "(개발구간 2016-2022, n=5,674). 계산은 재현되지만 수익 예측이 아니다.")

MIN_REWARD_RISK = 1.5
# 손절폭의 절대 한도(진입가 대비). ATR 배수만으로는 급등주에서 -26% 손절이
# 나온다 - 트레이딩 계획으로 쓸 수 없는 값이다.
MAX_RISK_PCT = 0.12
# 손절은 진입가에서 이보다 멀어지지 않는다. 최근 스윙이 격했던 종목은 가장
# 가까운 지지 클러스터도 10%+ 아래에 있어서, 지지만 보고 손절을 잡으면 손익비가
# 구조적으로 성립하지 않는다(실측 2026-08-24: 상위 8종목 전부 이 이유로 기각).
# 지지 기반 손절과 ATR 기반 손절 중 **더 가까운 쪽**을 쓴다.
MAX_RISK_ATR = 2.0
# 신고가권(머리 위 저항 없음) 목표 배수.
#
# ▶ 2026-08-24 사전등록 재보정: 3.2 -> 2.55.
#   3.2 는 내가 "2.5 로 두면 신고가 종목이 RR 1.32 로 전부 기각된다"는 이유로
#   올린 값이었다 - 내가 정한 관문을 통과하도록 내가 분자를 키운 것이라
#   자기충족적이었다. 개발 구간(2016-07~2022-12) 신고가권 표본 n=2,548 의
#   최대유리이동 중앙값이 **2.55 ATR** 이고 3.2 도달 비율은 40.9% 였다.
#   수익률을 목적함수로 쓰지 않고 "절반이 실제 도달하는 거리"로 정했다.
#
# ⚠ 이 재보정으로 손익비가 나아지지 않는다. 같은 표본의 **최대불리이동
#   중앙값이 2.52 ATR** 이라 MAX_RISK_ATR = 2.0 손절은 절반 이상이 먼저 닿는다
#   (실측 손절 선도달 62.1% vs 목표 선도달 31.0%). 손절 폭은 아직 손대지
#   않았다 - 사전등록에서 이번 라운드 재보정 대상은 이 상수 하나뿐이었다.
BREAKOUT_TARGET_ATR = 2.55
# 손절 후보로 쓰는 '최근 바닥' 구간. 추세 종목의 실질 지지선.
BASE_LOOKBACK_BARS = 10



def _safe(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f

# ────────────────────────────────────────────────────────────────────────────
# 가격 계획 - 지지/저항/목표가/손절가
# ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Level:
    price: float
    touches: int
    strength: float      # 0..1
    basis: str           # 사람이 읽는 근거


@dataclass(frozen=True)
class PricePlan:
    status: str
    last_close: float
    supports: tuple[Level, ...] = ()
    resistances: tuple[Level, ...] = ()
    entry_low: float | None = None
    entry_high: float | None = None
    target: float | None = None
    stop: float | None = None
    reward_risk: float | None = None
    risk_pct: float | None = None
    atr: float | None = None
    target_basis: str = ""
    stop_basis: str = ""
    reason: str = ""


PLAN_OK = "OK"
PLAN_REJECTED = "REJECTED"       # 손익비가 안 나온다
PLAN_UNAVAILABLE = "UNAVAILABLE" # 봉이 모자란다

MIN_REWARD_RISK = 1.5
# 손절폭의 절대 한도(진입가 대비). ATR 배수만으로는 급등주에서 -26% 손절이
# 나온다 - 트레이딩 계획으로 쓸 수 없는 값이다.
MAX_RISK_PCT = 0.12
# 손절은 진입가에서 이보다 멀어지지 않는다. 최근 스윙이 격했던 종목은 가장
# 가까운 지지 클러스터도 10%+ 아래에 있어서, 지지만 보고 손절을 잡으면 손익비가
# 구조적으로 성립하지 않는다(실측 2026-08-24: 상위 8종목 전부 이 이유로 기각).
# 지지 기반 손절과 ATR 기반 손절 중 **더 가까운 쪽**을 쓴다.
MAX_RISK_ATR = 2.0
# 신고가권(머리 위 저항 없음) 목표 배수.
#
# ▶ 2026-08-24 사전등록 재보정: 3.2 -> 2.55.
#   3.2 는 내가 "2.5 로 두면 신고가 종목이 RR 1.32 로 전부 기각된다"는 이유로
#   올린 값이었다 - 내가 정한 관문을 통과하도록 내가 분자를 키운 것이라
#   자기충족적이었다. 개발 구간(2016-07~2022-12) 신고가권 표본 n=2,548 의
#   최대유리이동 중앙값이 **2.55 ATR** 이고 3.2 도달 비율은 40.9% 였다.
#   수익률을 목적함수로 쓰지 않고 "절반이 실제 도달하는 거리"로 정했다.
#
# ⚠ 이 재보정으로 손익비가 나아지지 않는다. 같은 표본의 **최대불리이동
#   중앙값이 2.52 ATR** 이라 MAX_RISK_ATR = 2.0 손절은 절반 이상이 먼저 닿는다
#   (실측 손절 선도달 62.1% vs 목표 선도달 31.0%). 손절 폭은 아직 손대지
#   않았다 - 사전등록에서 이번 라운드 재보정 대상은 이 상수 하나뿐이었다.
BREAKOUT_TARGET_ATR = 2.55
# 손절 후보로 쓰는 '최근 바닥' 구간. 추세 종목의 실질 지지선.
BASE_LOOKBACK_BARS = 10


def _atr(bars: Sequence[Mapping[str, Any]], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(len(bars) - period, len(bars)):
        high = _safe(bars[i].get("high"))
        low = _safe(bars[i].get("low"))
        prev_close = _safe(bars[i - 1].get("close"))
        if high is None or low is None or prev_close is None:
            return None
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs) / len(trs)


def _swing_points(bars: Sequence[Mapping[str, Any]], k: int) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """좌우 k봉보다 극단인 지점. (가격, 거래대금) 쌍으로 돌려준다."""
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        h = _safe(bars[i].get("high"))
        low = _safe(bars[i].get("low"))
        notional = _safe(bars[i].get("notional"), 0.0) or 0.0
        if h is None or low is None:
            continue
        window = bars[i - k:i + k + 1]
        window_highs = [_safe(b.get("high")) for b in window]
        window_lows = [_safe(b.get("low")) for b in window]
        if any(v is None for v in window_highs + window_lows):
            continue
        if h >= max(window_highs):
            highs.append((h, notional))
        if low <= min(window_lows):
            lows.append((low, notional))
    return highs, lows


def _cluster(points: Sequence[tuple[float, float]], tol: float) -> list[Level]:
    """가격이 tol(상대) 안에 모인 스윙을 하나의 레벨로 묶는다."""
    if not points:
        return []
    ordered = sorted(points, key=lambda p: p[0])
    groups: list[list[tuple[float, float]]] = [[ordered[0]]]
    for price, notional in ordered[1:]:
        anchor = groups[-1][0][0]
        if abs(price - anchor) / anchor <= tol:
            groups[-1].append((price, notional))
        else:
            groups.append([(price, notional)])

    max_notional = max(sum(n for _, n in g) for g in groups) or 1.0
    max_touches = max(len(g) for g in groups)
    levels = []
    for g in groups:
        # 거래대금 가중 평균가 - 많이 거래된 지점 쪽으로 레벨을 당긴다.
        weight_total = sum(n for _, n in g)
        if weight_total > 0:
            price = sum(p * n for p, n in g) / weight_total
        else:
            price = sum(p for p, _ in g) / len(g)
        strength = 0.5 * (len(g) / max_touches) + 0.5 * (weight_total / max_notional)
        levels.append(Level(
            price=round(price, 1),
            touches=len(g),
            strength=round(strength, 3),
            basis=f"스윙 {len(g)}회 접점, 거래대금 가중",
        ))
    return levels


def price_plan(
    bars: Sequence[Mapping[str, Any]],
    *,
    swing_k: int = 3,
    cluster_tol: float = 0.02,
    atr_period: int = 14,
    min_reward_risk: float = MIN_REWARD_RISK,
    max_risk_pct: float = MAX_RISK_PCT,
) -> PricePlan:
    """봉에서 지지·저항을 뽑고 진입·목표·손절을 낸다.

    목표가는 **저항 클러스터**에서 나온다. 저항이 없으면(신고가 구간) ATR
    배수로 대체하되 그 사실을 basis 에 남긴다 - 애널리스트 목표가처럼 미래
    실적을 가정해 지어내지 않는다.

    손익비가 min_reward_risk 미만이면 PLAN_REJECTED 다. 점수가 아무리 높아도
    가격 자리가 나쁘면 추천하지 않는다 - 트레이딩 보조에서는 '무엇을'만큼
    '어디서'가 중요하다.
    """
    if len(bars) < max(60, atr_period + 1, swing_k * 2 + 1):
        return PricePlan(PLAN_UNAVAILABLE, 0.0, reason=f"봉 부족({len(bars)})")
    last = _safe(bars[-1].get("close"))
    atr = _atr(bars, atr_period)
    if last is None or last <= 0 or atr is None or atr <= 0:
        return PricePlan(PLAN_UNAVAILABLE, last or 0.0, reason="종가 또는 ATR 산출 불가")

    highs, lows = _swing_points(bars, swing_k)
    resistances = [lv for lv in _cluster(highs, cluster_tol) if lv.price > last * 1.005]
    supports = [lv for lv in _cluster(lows, cluster_tol) if lv.price < last * 0.995]
    resistances.sort(key=lambda lv: lv.price)
    supports.sort(key=lambda lv: -lv.price)

    # 진입 - 현재가 아래 ATR 0.5 구간. 추격매수를 강요하지 않는다.
    entry_low = round(last - 0.5 * atr, 1)
    entry_high = round(last + 0.2 * atr, 1)
    # 손익비는 기대 체결가(구간 중앙)로 잰다. 상단으로 재면 실제로는 성립하는
    # 자리도 전부 기각된다.
    entry_ref = (entry_low + entry_high) / 2

    # 목표 - 첫 저항이 너무 가까우면(ATR 1배 미만) 그 다음 저항을 본다.
    target_basis = ""
    target = None
    for lv in resistances:
        if lv.price - last >= atr:
            target, target_basis = lv.price, f"저항 {lv.price:,.0f} ({lv.basis})"
            break
    if target is None:
        target = round(last + BREAKOUT_TARGET_ATR * atr, 1)
        target_basis = f"저항 없음(신고가권) - ATR×{BREAKOUT_TARGET_ATR}"

    # 손절 - 후보 셋 중 **진입가에 가장 가까운(=가장 높은)** 자리를 쓴다.
    #  (1) 스윙 지지 클러스터 하단
    #  (2) 최근 10일 저가 - 추세가 살아 있는 종목의 실질 바닥. 이게 없으면
    #      신고가권 종목은 스윙 지지가 저 아래뿐이라 손절이 늘 ATR 상한으로
    #      밀리고, 목표도 ATR 배수라 손익비가 **상수로 굳어 전부 기각된다**
    #      (실측 2026-08-24: 신고가 종목 전부 RR 1.32 로 동일 기각).
    #  (3) ATR 상한 - 리스크의 절대 한도
    candidates: list[tuple[float, str]] = []
    if supports:
        candidates.append((supports[0].price - 0.5 * atr,
                           f"지지 {supports[0].price:,.0f} 하단 - ATR×0.5"))
    recent_lows = [_safe(b.get("low")) for b in bars[-BASE_LOOKBACK_BARS:]]
    recent_lows = [v for v in recent_lows if v is not None]
    if recent_lows:
        base_low = min(recent_lows)
        candidates.append((base_low - 0.5 * atr,
                           f"최근 {BASE_LOOKBACK_BARS}일 저가 {base_low:,.0f} 하단 - ATR×0.5"))
    atr_floor = entry_ref - MAX_RISK_ATR * atr
    candidates.append((atr_floor, f"ATR×{MAX_RISK_ATR} 리스크 한도"))

    # 진입가 위로 올라온 후보는 손절이 될 수 없다. ATR 한도보다 아래인 후보도 뺀다.
    usable = [(p, b) for p, b in candidates if atr_floor <= p < entry_ref]
    if usable:
        stop, stop_basis = max(usable, key=lambda pb: pb[0])
    else:
        stop, stop_basis = atr_floor, f"유효 지지 없음 - ATR×{MAX_RISK_ATR}"
    stop = round(stop, 1)

    risk = entry_ref - stop
    reward = target - entry_ref
    risk_pct = risk / entry_ref if entry_ref > 0 else None

    def _plan(status: str, rr: float | None, reason: str) -> PricePlan:
        return PricePlan(
            status=status,
            last_close=last,
            supports=tuple(supports[:3]),
            resistances=tuple(resistances[:3]),
            entry_low=entry_low,
            entry_high=entry_high,
            target=target,
            stop=stop,
            reward_risk=rr,
            risk_pct=round(risk_pct, 4) if risk_pct is not None else None,
            atr=round(atr, 1),
            target_basis=target_basis,
            stop_basis=stop_basis,
            reason=reason,
        )

    if risk <= 0 or reward <= 0:
        return _plan(PLAN_REJECTED, None,
                     f"진입 {entry_ref:,.0f} 기준 목표/손절 배치가 성립하지 않는다")

    # 변동성 과다 - ATR 배수로는 제한이 걸려도 절대 손실폭이 감당 불가일 수 있다.
    # 실측 2026-08-24: 20일 +237% 급등주가 ATR×2 손절 = 진입가 대비 -26% 로
    # 나왔다. 손절을 %로 억지로 당기면 잡음에 그냥 털리는 자리가 되므로,
    # 폭을 줄이는 대신 **이 종목은 계획이 성립하지 않는다**고 말한다.
    if risk_pct is not None and risk_pct > max_risk_pct:
        return _plan(PLAN_REJECTED, round(reward / risk, 2),
                     f"변동성 과다 - 손절폭이 진입가 대비 {risk_pct:.1%} "
                     f"(한도 {max_risk_pct:.0%}), {stop_basis}")

    rr = round(reward / risk, 2)
    if rr < min_reward_risk:
        return _plan(PLAN_REJECTED, rr,
                     f"손익비 {rr:.2f} < 최소 {min_reward_risk} "
                     f"(목표 +{reward:,.0f} / 손절 -{risk:,.0f}) - {target_basis}, {stop_basis}")
    return _plan(PLAN_OK, rr, "")




# ── 자체 점검 ────────────────────────────────────────────────────────────

def _synthetic(n: int, start: float, drift: float, amp: float) -> list[dict]:
    bars, price = [], start
    for i in range(n):
        price = price * (1 + drift) + amp * math.sin(i / 5.0)
        bars.append({"close": price, "high": price * 1.01, "low": price * 0.99,
                     "notional": 1_000_000 + 50_000 * (i % 7)})
    return bars


if __name__ == "__main__":
    plan = price_plan(_synthetic(120, 1000, 0.004, 25))
    assert plan.status in {PLAN_OK, PLAN_REJECTED}, plan
    assert plan.atr and plan.atr > 0
    if plan.status == PLAN_OK:
        assert plan.stop < plan.entry_high < plan.target
        assert plan.reward_risk >= MIN_REWARD_RISK
        assert plan.risk_pct is not None and plan.risk_pct <= MAX_RISK_PCT
        assert plan.target_basis and plan.stop_basis

    # 봉이 모자라면 계산하지 않는다
    assert price_plan(_synthetic(30, 1000, 0.004, 5)).status == PLAN_UNAVAILABLE

    # 변동성 과다는 손절 폭을 억지로 줄이지 않고 기각한다
    wild = price_plan(_synthetic(120, 1000, 0.02, 120))
    if wild.status == PLAN_REJECTED and wild.risk_pct:
        assert wild.risk_pct > MAX_RISK_PCT or (wild.reward_risk or 0) < MIN_REWARD_RISK

    # 재보정된 상수가 사전등록 값 그대로인지 - 조용히 바뀌면 안 된다
    assert BREAKOUT_TARGET_ATR == 2.55, BREAKOUT_TARGET_ATR
    assert MAX_RISK_ATR == 2.0 and MAX_RISK_PCT == 0.12
    assert "31.0%" in CAVEAT and "62.1%" in CAVEAT

    print("price_levels self-check OK")
