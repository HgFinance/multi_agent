"""종목 채점 코어 - 순수 함수, I/O 없음.

트레이딩 보조형 포트폴리오 추천의 1층이다. 유니버스 전 종목에 대해 축별
점수(-1..+1)를 매기고, 축을 합성해 종합 확신도와 가격 계획(지지·저항·목표·
손절)을 낸다. 같은 입력이면 항상 같은 답이 나온다 - 외부 조회는 호출부가
하고 여기에는 데이터만 들어온다.

## 왜 이렇게 나눴나

CLAUDE.md "LLM은 관련성 판단·서술에만 쓴다. PIT 필터·인용 검증·한도 검사는
결정론적 Python" 을 이 파일이 지킨다. 목표가·손절가는 **숫자**라서 LLM이
지어내면 안 된다 - 여기서 봉 데이터로 산출하고, LLM은 그 숫자를 설명만 한다.

## 기권(ABSTAINED)이 0점이 아닌 이유

축 하나가 죽었을 때 그 축을 0점(중립)으로 채우면, 데이터가 없는 종목과 진짜
중립인 종목이 같은 점수를 받는다. virattt/ai-hedge-fund 의 blend_signals 가
같은 문제를 이렇게 적어 놨다 - "'no opinion' must not masquerade as
'opinion: neutral'". 그래서 기권 축은 분자와 분모 **둘 다에서** 빠지고,
남은 유효 가중치(effective_weight)가 문턱 아래면 종합점수를 내는 대신
INSUFFICIENT 로 떨어진다.

실측 2026-08-24 기준 우리 축 상태: 차트 LIVE, 수급 LIVE(단 LS 2,000회/일 ·
초당 1건이라 전 종목 불가), 공매도 LIVE, 밸류/업종 LIVE, 뉴스 LIVE,
공시 LIVE, 테마 소스 없음. 테마가 상시 NO_SOURCE 라 이 장치가 장식이 아니다.
"""

from __future__ import annotations

import math
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# price_levels 는 컨테이너로 나란히 복사되거나(배치 실행기), 저장소 경로에
# 있거나(호스트) 둘 중 하나다. 둘 다 없으면 조용히 넘어가지 않고 죽는다.
# 실행 위치가 셋이다 - 저장소(호스트), /tmp(컨테이너로 복사된 배치 실행기),
# 이미지에 구워진 리서치 경로. 셋 다 뒤진다.
for _cand in (os.path.dirname(os.path.abspath(__file__)),
              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "..", "..", "01-research", "evidence"),
              "/app/departments/01-research/evidence"):
    _cand = os.path.normpath(_cand)
    if os.path.isfile(os.path.join(_cand, "price_levels.py")) and _cand not in sys.path:
        sys.path.insert(0, _cand)

# 축 정본. 가중치는 합이 1일 필요 없다 - 기권 축을 빼고 정규화하기 때문이다.
# 값의 근거: 한국투자증권 추천종목이 "정량평가(성장성·안정성·모멘텀)로 선정 →
# 수급·투자심리로 추천강도" 순서를 쓰고, TipRanks 가 뉴스감정과 수급성 지표를
# 별도 축으로 둔다. 우리는 트레이딩 보조라 모멘텀·수급을 앞에 둔다.
DEFAULT_AXIS_WEIGHTS: dict[str, float] = {
    "momentum": 0.28,   # 차트 - 추세·상대강도·거래대금
    "flow": 0.26,       # 수급 - 외국인·기관 연속 순매수와 강도
    "short": 0.10,      # 공매도 - 비중 급증은 감점
    "valuation": 0.10,  # 밸류/업종 상대
    "news": 0.14,       # 뉴스 호재/악재
    "disclosure": 0.08, # 공시 호재/악재
    "theme": 0.04,      # 테마 편입
}

STATUS_OK = "OK"
STATUS_ABSTAINED = "ABSTAINED"   # 소스는 있는데 이번엔 값이 없다(장중 미집계 등)
STATUS_NO_SOURCE = "NO_SOURCE"   # 소스 자체가 없다(테마)

# 유효 가중치가 이 아래면 종합점수를 내지 않는다. 0.5 는 "축 절반은 살아
# 있어야 한다"는 뜻 - 차트(0.28)+수급(0.26)만으로 0.54 라 겨우 넘는다.
MIN_EFFECTIVE_WEIGHT = 0.50


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _tanh(value: float, scale: float = 1.0) -> float:
    """무한 범위를 (-1, +1) 로 접는다. scale 이 클수록 민감해진다."""
    return math.tanh(value * scale)


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


@dataclass(frozen=True)
class AxisScore:
    """축 하나의 판정. status 가 OK 가 아니면 value 는 반드시 None 이다."""

    axis: str
    status: str
    value: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status == STATUS_OK:
            if self.value is None:
                raise ValueError(f"{self.axis}: OK 인데 value 가 없다")
            if not -1.0 <= self.value <= 1.0:
                raise ValueError(f"{self.axis}: value 가 [-1,1] 밖이다 ({self.value})")
        elif self.value is not None:
            # 기권인데 값이 남아 있으면 호출부가 그 값을 쓸 위험이 있다.
            raise ValueError(f"{self.axis}: {self.status} 인데 value 가 있다")


def abstain(axis: str, reason: str, *, no_source: bool = False) -> AxisScore:
    return AxisScore(
        axis=axis,
        status=STATUS_NO_SOURCE if no_source else STATUS_ABSTAINED,
        reason=reason,
    )


# ────────────────────────────────────────────────────────────────────────────
# 축 1 - 모멘텀/추세 (일봉만 필요 = 전 종목에 돌릴 수 있는 유일한 축)
# ────────────────────────────────────────────────────────────────────────────

def momentum_features(bars: Sequence[Mapping[str, Any]]) -> dict[str, float] | None:
    """일봉에서 횡단면 비교 전 원시 지표를 뽑는다. 최신 봉이 마지막이다.

    None 을 돌려주면 '이 종목은 채점 불가'라는 뜻이다 - 봉이 모자라면 억지로
    짧은 창으로 계산하지 않는다(60일 수익률을 20일로 대체하면 조용히 다른
    지표가 된다).
    """
    if len(bars) < 60:
        return None
    closes = [_safe(b.get("close")) for b in bars]
    if any(c is None or c <= 0 for c in closes):
        return None
    notionals = [_safe(b.get("notional"), 0.0) or 0.0 for b in bars]

    last = closes[-1]
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60

    # 거래대금 팽창 - 최근 5일 평균 대비 60일 평균. 0 나눗셈은 팽창 없음으로 본다.
    recent_turnover = sum(notionals[-5:]) / 5
    base_turnover = sum(notionals[-60:]) / 60
    turnover_ratio = (recent_turnover / base_turnover) if base_turnover > 0 else 1.0

    # 변동성 - 일간 로그수익률 표준편차(20일). 목표가·손절 폭에도 쓰인다.
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 20, len(closes))]
    mean_ret = sum(rets) / len(rets)
    vol20 = math.sqrt(sum((r - mean_ret) ** 2 for r in rets) / len(rets))

    return {
        "ret_20": last / closes[-21] - 1.0 if len(closes) > 20 else 0.0,
        "ret_60": last / closes[-61] - 1.0 if len(closes) > 60 else last / closes[0] - 1.0,
        "ma20_gap": last / ma20 - 1.0,
        "ma_stack": 1.0 if last > ma20 > ma60 else (-1.0 if last < ma20 < ma60 else 0.0),
        "above_ma20_ratio": sum(1 for c in closes[-20:] if c > ma20) / 20,
        "turnover_ratio": turnover_ratio,
        "vol20": vol20,
        "last_close": last,
    }


def _percentile(value: float, sorted_values: Sequence[float]) -> float:
    """정렬된 표본 안에서 value 의 백분위(0..1). 표본이 비면 중앙값으로 본다."""
    n = len(sorted_values)
    if n == 0:
        return 0.5
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_values[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    return lo / n


def momentum_axis_batch(
    features_by_symbol: Mapping[str, Mapping[str, float]],
) -> dict[str, AxisScore]:
    """모멘텀 축을 **유니버스 횡단면**으로 채점한다.

    수익률을 절대값으로 자르면 장 전체가 오른 날 전 종목이 만점을 받는다.
    Seeking Alpha 가 "같은 섹터 내 상대비교"를 쓰는 이유와 같다 - 여기서는
    섹터 대신 유니버스 전체를 분모로 쓴다(섹터 매핑은 t3320 을 종목마다
    불러야 해서 전 종목에는 못 쓴다. 후보 단계에서 valuation 축이 그 일을 한다).
    """
    if not features_by_symbol:
        return {}
    ret20_sorted = sorted(f["ret_20"] for f in features_by_symbol.values())
    ret60_sorted = sorted(f["ret_60"] for f in features_by_symbol.values())

    out: dict[str, AxisScore] = {}
    for symbol, f in features_by_symbol.items():
        p20 = _percentile(f["ret_20"], ret20_sorted)
        p60 = _percentile(f["ret_60"], ret60_sorted)
        # 백분위 0..1 -> -1..+1
        comp_ret20 = p20 * 2 - 1
        comp_ret60 = p60 * 2 - 1
        comp_stack = f["ma_stack"]
        comp_persist = f["above_ma20_ratio"] * 2 - 1
        # 거래대금 팽창은 방향이 없다 - 추세가 살아 있을 때만 가점으로 본다.
        comp_turnover = _tanh(math.log(max(f["turnover_ratio"], 1e-6)), 0.8)
        if comp_ret20 < 0:
            comp_turnover = -abs(comp_turnover)

        value = _clamp(
            0.30 * comp_ret20
            + 0.25 * comp_ret60
            + 0.20 * comp_stack
            + 0.15 * comp_persist
            + 0.10 * comp_turnover
        )
        out[symbol] = AxisScore(
            axis="momentum",
            status=STATUS_OK,
            value=value,
            detail={
                "ret_20": round(f["ret_20"], 4),
                "ret_20_pct": round(p20, 3),
                "ret_60": round(f["ret_60"], 4),
                "ret_60_pct": round(p60, 3),
                "ma_stack": f["ma_stack"],
                "above_ma20_ratio": round(f["above_ma20_ratio"], 2),
                "turnover_ratio": round(f["turnover_ratio"], 2),
                "universe_size": len(features_by_symbol),
            },
            evidence_refs=("market.market_bars:1D",),
        )
    return out


# ────────────────────────────────────────────────────────────────────────────
# 축 2 - 수급 (LS t1717). 후보 축소 후에만 부를 수 있다.
# ────────────────────────────────────────────────────────────────────────────

def _streak(values: Sequence[float | None], positive: bool) -> int:
    """최신부터 세어 부호가 유지되는 일수. None(미집계)을 만나면 거기서 끊는다."""
    count = 0
    for v in values:
        if v is None:
            break
        if (v > 0) if positive else (v < 0):
            count += 1
        else:
            break
    return count


def flow_axis(
    rows: Sequence[Mapping[str, Any]],
    *,
    avg_daily_volume: float | None = None,
) -> AxisScore:
    """외국인·기관 순매수 추세를 채점한다. rows 는 최신일이 앞이다(t1717 순서).

    t1717 은 장중에 투자자별 집계를 0 으로 준다. ls_mcp_server.investor_flow 가
    그 날을 '집계상태' 키로 표시하고 수치 키를 빼므로, 여기서는 키가 없으면
    None 으로 읽어 연속일수 계산을 **거기서 끊는다**. 0 으로 읽으면 "순매수
    끊김"으로 오판한다 - 그건 사실이 아니라 미집계다.
    """
    if not rows:
        return abstain("flow", "t1717 응답에 행이 없다")

    foreign = [_safe(r.get("외인계")) for r in rows]
    inst = [_safe(r.get("기관계")) for r in rows]
    retail = [_safe(r.get("개인")) for r in rows]

    usable = [i for i, (f, k) in enumerate(zip(foreign, inst)) if f is not None and k is not None]
    if not usable:
        return abstain("flow", "조회 구간 전체가 장중 미집계")
    if usable[0] != 0:
        # 최신일이 미집계면 연속일수의 기준일이 흔들린다. 하루는 봐주되
        # 그 사실을 detail 에 남긴다.
        pass

    f_buy_streak = _streak(foreign, True)
    f_sell_streak = _streak(foreign, False)
    i_buy_streak = _streak(inst, True)
    i_sell_streak = _streak(inst, False)

    # 연속일수 -> 점수. 20일 연속이면 사실상 만점이 되도록 스케일을 잡는다
    # (사용자가 예로 든 기준이 "20일 연속 외국인 OR 기관 순매수").
    def streak_score(buy: int, sell: int) -> float:
        return _tanh(buy / 6.0) - _tanh(sell / 6.0)

    comp_foreign_streak = streak_score(f_buy_streak, f_sell_streak)
    comp_inst_streak = streak_score(i_buy_streak, i_sell_streak)

    # 강도 - 최근 20일 누적 순매수를 평균 거래량으로 나눈다. 연속일수만 보면
    # '20일 내내 100주씩 산' 종목이 만점을 받는다.
    window = [v for v in foreign[:20] if v is not None]
    inst_window = [v for v in inst[:20] if v is not None]
    comp_intensity = 0.0
    intensity_detail: dict[str, Any] = {}
    if avg_daily_volume and avg_daily_volume > 0 and window:
        f_ratio = sum(window) / (avg_daily_volume * len(window))
        i_ratio = sum(inst_window) / (avg_daily_volume * len(inst_window)) if inst_window else 0.0
        comp_intensity = _clamp(_tanh((f_ratio + i_ratio) * 6.0))
        intensity_detail = {
            "foreign_net_20d_per_adv": round(f_ratio, 4),
            "inst_net_20d_per_adv": round(i_ratio, 4),
        }

    # 개인이 반대편이면 소폭 가점 - 수급 주체가 갈린 상태를 선호한다.
    comp_retail = 0.0
    retail_window = [v for v in retail[:20] if v is not None]
    if retail_window:
        comp_retail = -_tanh(sum(retail_window) / (abs(sum(retail_window)) + 1e-9) * 0.3)

    value = _clamp(
        0.35 * comp_foreign_streak
        + 0.30 * comp_inst_streak
        + 0.30 * comp_intensity
        + 0.05 * comp_retail
    )
    unaggregated = [r.get("date") for r in rows if r.get("집계상태")]
    return AxisScore(
        axis="flow",
        status=STATUS_OK,
        value=value,
        detail={
            "foreign_buy_streak": f_buy_streak,
            "foreign_sell_streak": f_sell_streak,
            "inst_buy_streak": i_buy_streak,
            "inst_sell_streak": i_sell_streak,
            "days_observed": len(rows),
            "unaggregated_dates": unaggregated,
            **intensity_detail,
        },
        evidence_refs=("ls:t1717",),
    )


# ────────────────────────────────────────────────────────────────────────────
# 축 3 - 공매도
# ────────────────────────────────────────────────────────────────────────────

def short_axis(rows: Sequence[Mapping[str, Any]]) -> AxisScore:
    """공매도 비중의 **평소 대비 급증**을 감점한다. 절대 비중이 아니다.

    비중 10% 가 늘 10% 인 종목과, 5% 이던 게 어제 11% 가 된 종목은 다른 상태다.
    후자가 신호다.
    """
    ratios = [_safe(r.get("공매도비중_pct")) for r in rows]
    ratios = [r for r in ratios if r is not None]
    if len(ratios) < 5:
        return abstain("short", f"공매도 이력 부족({len(ratios)}일)")

    latest = ratios[0]
    baseline = sorted(ratios[1:])[len(ratios[1:]) // 2]  # 중앙값 - 하루 스파이크에 안 흔들린다
    if baseline <= 0:
        return abstain("short", "기준 공매도 비중이 0")
    surge = latest / baseline
    value = _clamp(-_tanh(math.log(max(surge, 1e-6)), 1.2))
    return AxisScore(
        axis="short",
        status=STATUS_OK,
        value=value,
        detail={
            "latest_pct": round(latest, 2),
            "baseline_median_pct": round(baseline, 2),
            "surge_ratio": round(surge, 2),
            "days_observed": len(ratios),
        },
        evidence_refs=("ls:t1927",),
    )


# ────────────────────────────────────────────────────────────────────────────
# 합성
# ────────────────────────────────────────────────────────────────────────────

COMPOSITE_OK = "OK"
COMPOSITE_INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True)
class CompositeScore:
    status: str
    value: float | None            # -1..+1
    display: int | None            # 0..100, UI 용
    effective_weight: float        # 살아 있는 축 가중치 비율
    axes: tuple[AxisScore, ...]
    contributions: dict[str, float]
    unreported: tuple[str, ...] = ()
    reason: str = ""


def blend_axes(
    axes: Sequence[AxisScore],
    weights: Mapping[str, float] | None = None,
    *,
    min_effective_weight: float = MIN_EFFECTIVE_WEIGHT,
) -> CompositeScore:
    """기권을 뺀 가중평균. 유효 가중치가 문턱 아래면 점수를 내지 않는다.

    분모는 **가중치표 전체**이지 전달된 축이 아니다. 전달분만 분모로 쓰면
    축을 아예 빼먹은 호출부가 조용히 만점을 받는다 - 기권을 0 으로 채우는
    것과 같은 실패를 뒷문으로 다시 들이는 셈이다. 표에 있는데 보고되지 않은
    축은 unreported 로 남기고 분모에는 그대로 센다.
    """
    weights = dict(weights or DEFAULT_AXIS_WEIGHTS)
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return CompositeScore(
            COMPOSITE_INSUFFICIENT, None, None, 0.0, tuple(axes), {}, (),
            "가중치표가 비어 있다")

    seen = {a.axis for a in axes}
    unknown = sorted(seen - set(weights))
    if unknown:
        # 오타 난 축 이름이 조용히 무시되면 그 축은 영원히 반영되지 않는다.
        raise ValueError(f"가중치표에 없는 축: {unknown}")
    unreported = tuple(sorted(set(weights) - seen))

    live = [a for a in axes if a.status == STATUS_OK]
    live_weight = sum(weights[a.axis] for a in live)
    effective = live_weight / total_weight

    if effective < min_effective_weight:
        dead = sorted(a.axis for a in axes if a.status != STATUS_OK)
        detail = f"죽은 축: {', '.join(dead) or '없음'}"
        if unreported:
            detail += f" / 미보고 축: {', '.join(unreported)}"
        return CompositeScore(
            COMPOSITE_INSUFFICIENT, None, None, round(effective, 3),
            tuple(axes), {}, unreported,
            f"유효 축 가중치 {effective:.0%} < 최소 {min_effective_weight:.0%} ({detail})")

    contributions = {
        a.axis: round(weights[a.axis] * a.value / live_weight, 4) for a in live
    }
    value = _clamp(sum(contributions.values()))
    return CompositeScore(
        status=COMPOSITE_OK,
        value=value,
        display=round((value + 1) / 2 * 100),
        effective_weight=round(effective, 3),
        axes=tuple(axes),
        contributions=contributions,
        unreported=unreported,
    )


# ────────────────────────────────────────────────────────────────────────────
# 가격 계획 - 정본은 리서치 소유 모듈이다
#
# 같은 계산이 배치 선별과 요청 시점 종목 질의(market-api /levels) 양쪽에서
# 필요하고, 둘이 다른 숫자를 내면 "화면과 리포트가 다르다"가 된다. 시세
# 파생물은 리서치 소유이므로 거기 두고 여기서 가져다 쓴다.
# ────────────────────────────────────────────────────────────────────────────

from price_levels import (  # noqa: E402
    BASE_LOOKBACK_BARS,
    BREAKOUT_TARGET_ATR,
    CAVEAT as PRICE_PLAN_CAVEAT,
    MAX_RISK_ATR,
    MAX_RISK_PCT,
    MIN_REWARD_RISK,
    PLAN_OK,
    PLAN_REJECTED,
    PLAN_UNAVAILABLE,
    Level,
    PricePlan,
    price_plan,
)

# ────────────────────────────────────────────────────────────────────────────
# 자체 점검 - 저장소 관례상 pytest 대신 __main__ assert
# ────────────────────────────────────────────────────────────────────────────

def _synthetic_bars(n: int, start: float, drift: float, amp: float) -> list[dict]:
    bars = []
    price = start
    for i in range(n):
        price = price * (1 + drift) + amp * math.sin(i / 5.0)
        bars.append({
            "close": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "notional": 1_000_000 + 50_000 * (i % 7),
        })
    return bars


if __name__ == "__main__":
    # 기권 축은 값을 가질 수 없다
    try:
        AxisScore(axis="news", status=STATUS_ABSTAINED, value=0.0)
        raise AssertionError("기권인데 value 를 받아들였다")
    except ValueError:
        pass

    # 기권 축이 분모에서 빠진다 - 살아 있는 축만으로 정규화
    axes = [
        AxisScore("momentum", STATUS_OK, 1.0),
        AxisScore("flow", STATUS_OK, 1.0),
        abstain("news", "자격 없음"),
        abstain("disclosure", "자격 없음"),
        abstain("theme", "소스 없음", no_source=True),
    ]
    blended = blend_axes(axes)
    assert blended.status == COMPOSITE_OK, blended.reason
    assert abs(blended.value - 1.0) < 1e-9, blended.value
    assert blended.display == 100, blended.display
    # momentum 0.28 + flow 0.26 = 0.54, 분모는 가중치표 전체 1.00
    assert abs(blended.effective_weight - 0.54) < 1e-9, blended.effective_weight
    # short/valuation 은 아예 보고되지 않았다 - 분모에 남고 이름이 드러나야 한다
    assert blended.unreported == ("short", "valuation"), blended.unreported

    # 축을 빼먹어도 유효 가중치가 부풀지 않는다(전달분을 분모로 쓰면 1.0 이 된다)
    omitted = blend_axes([AxisScore("momentum", STATUS_OK, 1.0),
                          AxisScore("flow", STATUS_OK, 1.0)])
    assert abs(omitted.effective_weight - 0.54) < 1e-9, omitted.effective_weight
    assert len(omitted.unreported) == 5, omitted.unreported

    # 가중치표에 없는 축 이름은 조용히 무시하지 않는다
    try:
        blend_axes([AxisScore("momentom", STATUS_OK, 1.0)])
        raise AssertionError("오타 난 축 이름을 통과시켰다")
    except ValueError as exc:
        assert "momentom" in str(exc)

    # 유효 가중치가 문턱 아래면 점수를 내지 않는다
    thin = blend_axes([
        AxisScore("momentum", STATUS_OK, 1.0),
        abstain("flow", "LS 예산 소진"),
        abstain("short", "이력 부족"),
        abstain("valuation", "미조회"),
        abstain("news", "미조회"),
        abstain("disclosure", "미조회"),
        abstain("theme", "소스 없음", no_source=True),
    ])
    assert thin.status == COMPOSITE_INSUFFICIENT, thin
    assert thin.value is None and thin.display is None
    assert "유효 축 가중치" in thin.reason

    # 기권을 0 으로 채우는 것과 다르다는 것을 명시적으로 확인한다
    naive = 0.28 * 1.0 + 0.26 * 1.0 + 0.10 * 0 + 0.10 * 0 + 0.14 * 0 + 0.08 * 0 + 0.04 * 0
    assert abs(naive - 0.54) < 1e-9
    assert abs(blended.value - naive) > 0.4, "기권 처리가 0 채움과 같아졌다"

    # 수급 - 미집계(None)에서 연속일수가 끊긴다
    rows_live = [{"외인계": 100, "기관계": 50, "개인": -150} for _ in range(20)]
    live_axis = flow_axis(rows_live, avg_daily_volume=1000)
    assert live_axis.status == STATUS_OK
    assert live_axis.detail["foreign_buy_streak"] == 20, live_axis.detail
    assert live_axis.value > 0.5, live_axis.value

    rows_gap = [{"집계상태": "장중_미집계", "date": "20260824"}] + rows_live
    gap_axis = flow_axis(rows_gap, avg_daily_volume=1000)
    assert gap_axis.detail["foreign_buy_streak"] == 0, "미집계를 순매수 끊김으로 읽었다"
    assert gap_axis.detail["unaggregated_dates"] == ["20260824"]

    # 전 구간 미집계면 기권
    assert flow_axis([{"집계상태": "장중_미집계"}] * 3).status == STATUS_ABSTAINED

    # 공매도 - 급증이 감점
    surge = short_axis([{"공매도비중_pct": 11.34}] + [{"공매도비중_pct": 5.0}] * 9)
    assert surge.status == STATUS_OK and surge.value < -0.5, surge
    flat = short_axis([{"공매도비중_pct": 5.0}] * 10)
    assert abs(flat.value) < 0.05, flat

    # 모멘텀 - 봉이 모자라면 채점하지 않는다
    assert momentum_features(_synthetic_bars(30, 1000, 0.001, 1)) is None

    # 상승 종목이 하락 종목보다 높은 점수를 받는다
    up = momentum_features(_synthetic_bars(120, 1000, 0.004, 5))
    down = momentum_features(_synthetic_bars(120, 1000, -0.004, 5))
    assert up and down
    scores = momentum_axis_batch({"UP": up, "DOWN": down})
    assert scores["UP"].value > scores["DOWN"].value, scores

    # 가격 계획 - 손익비 미달은 REJECTED
    plan = price_plan(_synthetic_bars(120, 1000, 0.004, 25))
    assert plan.status in {PLAN_OK, PLAN_REJECTED}, plan
    assert plan.atr and plan.atr > 0
    if plan.status == PLAN_OK:
        assert plan.stop < plan.entry_high < plan.target
        assert plan.reward_risk >= MIN_REWARD_RISK
    assert price_plan(_synthetic_bars(30, 1000, 0.004, 5)).status == PLAN_UNAVAILABLE

    print("instrument_scoring self-check OK")
