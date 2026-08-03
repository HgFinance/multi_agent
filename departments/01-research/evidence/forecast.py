#!/usr/bin/env python3
"""주장의 사전 확률 - 결정론 계산.

소유: 재일 (리서치본부)
근거: WHOLE_SYSTEM_ADVANCEMENT_ROADMAP.md 6.2 P0 5항(Forecast) 및 핵심지표
      (Brier Score, Calibration Error).
      방법론 등재: evidence/methods.py `barrier_touch_probability`

▶ 왜 확률이 필요한가
  지금 주장은 "5거래일 안에 -10% 를 터치하나" 같은 이진 조건이다. 발동
  여부만 세면 분석가가 **과신하는지 소심한지** 알 수 없다. 10번 중 1번
  맞힌 사람과 9번 맞힌 사람이 둘 다 "터치한다"고만 말했다면 구분이 안 된다.
  확률을 붙여야 Brier Score 와 Calibration Error 가 성립한다.

▶ 왜 LLM 이 아니라 코드가 내는가
  packet_claims.origin 이 'code' 로 못박힌 것과 같은 이유다. LLM 이 자기
  채점 기준과 확률을 함께 쓰면 **맞히기 쉬운 쪽으로 굽는다.** 확률은
  관측된 변동성에서 결정론으로 나온다.

▶ 방법: 기하 브라운 운동의 배리어 터치 확률
  Harrison(1985) 의 반사원리(reflection principle). 드리프트 0 을 가정하면
  최저가가 배리어 아래로 내려갈 확률은 닫힌 식으로 나온다:
      P(터치) = 2 * Phi( -|ln(B/S)| / (sigma * sqrt(T)) )
  드리프트를 0 으로 두는 것은 보수적 선택이다 - 추세를 넣으면 최근 방향에
  확률이 끌려가고, 그건 예측이 아니라 외삽이다.

▶ 한계를 숨기지 않는다
  실현변동성이 미래 변동성과 같다는 가정, 로그정규 가정, 드리프트 0 가정이
  전부 근사다. 그래서 이 확률은 '정답'이 아니라 **비교 가능한 기준선**이다.
  분석가가 이 기준선보다 잘 맞히는지가 calibration 이 답할 질문이다.

실행: python evidence/forecast.py     # 자체 점검(네트워크 없음)
"""
from __future__ import annotations

import math
import sys

MODULE_VERSION = "research-forecast-v1"
METHOD_KEY = "barrier_touch_probability"

TRADING_DAYS = 252
MIN_RETURNS = 10        # 이보다 적으면 변동성을 말하지 않는다
# 확률을 이 범위로 자른다. 0 이나 1 을 내면 Brier 가 극단으로 벌어지고,
# 무엇보다 **확실하다고 말할 근거가 없다**(모형이 근사이므로).
P_FLOOR, P_CEIL = 0.01, 0.99


def _norm_cdf(x: float) -> float:
    """표준정규 누적분포. scipy 없이 - 수집기 컨테이너에 numpy/scipy 가 없다."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def realized_vol(closes, *, annualize: bool = False) -> float | None:
    """일별 로그수익률의 표본표준편차. 관측이 모자라면 None.

    표본 표준편차(ddof=1)를 쓴다 - 모집단 공식은 적은 표본에서 변동성을
    체계적으로 과소평가한다.
    """
    c = [float(x) for x in (closes or []) if x is not None and float(x) > 0]
    if len(c) < MIN_RETURNS + 1:
        return None
    rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    return sd * math.sqrt(TRADING_DAYS) if annualize else sd


def touch_probability(baseline: float, threshold: float, horizon_days: int,
                      daily_vol: float) -> float | None:
    """지평 안에 배리어를 **한 번이라도** 건드릴 확률.

    baseline 에서 threshold 방향으로 얼마나 떨어져 있는지를 변동성 단위로
    재고, 반사원리로 터치 확률을 낸다. 방향(위/아래)은 대칭이라 절대값을 쓴다.

    입력이 말이 안 되면 None - 0 이나 0.5 로 채우면 그 값이 채점에 들어간다.
    """
    if not (baseline and threshold) or baseline <= 0 or threshold <= 0:
        return None
    if horizon_days <= 0 or not daily_vol or daily_vol <= 0:
        return None
    dist = abs(math.log(threshold / baseline))
    if dist == 0:
        return P_CEIL            # 이미 배리어 위 - 사실상 확실
    z = dist / (daily_vol * math.sqrt(horizon_days))
    p = 2.0 * _norm_cdf(-z)
    return round(min(max(p, P_FLOOR), P_CEIL), 4)


def probability_for_claim(claim: dict, closes) -> tuple[float | None, str | None]:
    """주장 하나 -> (확률, 방법 key). 계산 불가면 (None, None).

    가격 주장(수치 임계값)만 확률을 낸다. 라벨 주장(REGIME_FLIP 등)은
    변동성 모형으로 다룰 대상이 아니다 - **모르는 것에 숫자를 붙이지 않는다.**
    라벨 주장의 확률은 daily_labels 이력의 전이빈도로 내야 하고, 그건 표본이
    쌓인 뒤 별도 방법으로 등재한다(백로그).
    """
    base = claim.get("baseline")
    thr = claim.get("threshold")
    if base is None or thr is None:
        return None, None
    vol = realized_vol(closes)
    if vol is None:
        return None, None
    p = touch_probability(float(base), float(thr),
                          int(claim.get("horizon_days") or 0), vol)
    return (p, METHOD_KEY) if p is not None else (None, None)


def falsification_note(claim: dict) -> str:
    """무엇이 관측되면 이 주장이 틀린 것인가 - 사람이 읽는 한 줄.

    반증 조건을 미리 적어두지 않으면 사후에 해석이 늘어난다("그 정도면
    맞은 셈이다"). 조건을 발행 시점에 고정하는 것이 목적이다.
    """
    kind = claim.get("kind", "")
    h = claim.get("horizon_days")
    op, thr = claim.get("op"), claim.get("threshold")
    txt = claim.get("threshold_text")
    if thr is not None:
        return (f"{h}거래일 안에 {claim.get('metric')} 이 {op} {thr} 를 "
                f"한 번도 건드리지 않으면 이 주장은 틀린 것이다")
    return (f"{h}거래일 안에 {claim.get('metric')} 이 {op} {txt} 가 "
            f"한 번도 되지 않으면 이 주장은 틀린 것이다")


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음
# ---------------------------------------------------------------------------

def _check_norm_cdf():
    assert abs(_norm_cdf(0.0) - 0.5) < 1e-9
    assert abs(_norm_cdf(1.96) - 0.975) < 1e-3
    assert abs(_norm_cdf(-1.96) - 0.025) < 1e-3
    print("  정규 누적분포            OK")


def _check_realized_vol():
    # 일정 비율로 오르는 계열은 로그수익률이 상수 -> 표준편차 0
    steady = [100.0 * (1.01 ** i) for i in range(30)]
    assert abs(realized_vol(steady)) < 1e-12, realized_vol(steady)
    # 톱니는 변동성이 크다
    saw = [100.0 * (1.05 if i % 2 else 0.95) for i in range(30)]
    assert realized_vol(saw) > 0.04, realized_vol(saw)
    # 표본이 모자라면 None - 0 으로 채우면 확률이 1 로 튄다
    assert realized_vol([100.0, 101.0]) is None
    assert realized_vol([]) is None
    # 연율화는 sqrt(252) 배
    v, va = realized_vol(saw), realized_vol(saw, annualize=True)
    assert abs(va - v * math.sqrt(252)) < 1e-9
    print("  실현변동성              OK")


def _check_touch_probability():
    # 멀수록 확률이 낮고, 지평이 길수록 높고, 변동성이 크면 높다 - 단조성
    p_near = touch_probability(100.0, 95.0, 5, 0.02)
    p_far = touch_probability(100.0, 80.0, 5, 0.02)
    assert p_near > p_far, (p_near, p_far)
    assert touch_probability(100.0, 95.0, 20, 0.02) > p_near
    assert touch_probability(100.0, 95.0, 5, 0.05) > p_near

    # 위·아래 대칭 (같은 로그거리면 같은 확률)
    up = touch_probability(100.0, 105.0, 5, 0.02)
    down = touch_probability(100.0, 100.0 / 1.05, 5, 0.02)
    assert abs(up - down) < 1e-6, (up, down)

    # 상·하한으로 자른다 - 0 이나 1 은 '확실하다'는 말인데 근거가 없다
    assert touch_probability(100.0, 1.0, 1, 0.001) == P_FLOOR
    assert touch_probability(100.0, 100.0, 5, 0.02) == P_CEIL

    # 말이 안 되는 입력은 None (0 이나 0.5 로 채우지 않는다)
    for bad in ((0.0, 95.0, 5, 0.02), (100.0, 0.0, 5, 0.02),
                (100.0, 95.0, 0, 0.02), (100.0, 95.0, 5, 0.0),
                (100.0, 95.0, 5, None)):
        assert touch_probability(*bad) is None, bad
    print("  배리어 터치 확률         OK")


def _check_claim_probability():
    closes = [100.0 * (1.0 + (0.01 if i % 2 else -0.01)) for i in range(40)]
    price_claim = {"kind": "PRICE_DRAWDOWN", "metric": "close", "op": "<=",
                   "baseline": 100.0, "threshold": 92.0, "horizon_days": 5}
    p, key = probability_for_claim(price_claim, closes)
    assert p is not None and 0 < p < 1 and key == METHOD_KEY, (p, key)

    # 라벨 주장은 확률을 내지 않는다 - 변동성 모형의 대상이 아니다
    label_claim = {"kind": "REGIME_FLIP", "metric": "regime_label", "op": "!=",
                   "threshold_text": "RISK_ON", "horizon_days": 5}
    assert probability_for_claim(label_claim, closes) == (None, None)

    # 시세가 모자라면 확률 없음 - 지어내지 않는다
    assert probability_for_claim(price_claim, [100.0, 101.0]) == (None, None)
    print("  주장별 확률 배정         OK")


def _check_falsification():
    n = falsification_note({"kind": "PRICE_DRAWDOWN", "metric": "close",
                            "op": "<=", "threshold": 92.0, "horizon_days": 5})
    assert "5거래일" in n and "92.0" in n and "틀린 것" in n, n
    n2 = falsification_note({"kind": "REGIME_FLIP", "metric": "regime_label",
                             "op": "!=", "threshold_text": "RISK_ON",
                             "horizon_days": 20})
    assert "RISK_ON" in n2 and "20거래일" in n2, n2
    print("  반증 조건 문구           OK")


def _check_registry():
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    from methods import METHODS

    m = {x.key: x for x in METHODS}
    assert METHOD_KEY in m, f"{METHOD_KEY} 가 레지스트리에 없다"
    assert m[METHOD_KEY].status == "ADOPTED"
    assert not m[METHOD_KEY].validated, "표본 전에 validated 를 켤 수 없다"
    print("  방법론 등재              OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크 없음)")
    _check_norm_cdf()
    _check_realized_vol()
    _check_touch_probability()
    _check_claim_probability()
    _check_falsification()
    _check_registry()
    print("Forecast 6개 영역 통과.")
