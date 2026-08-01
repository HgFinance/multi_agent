#!/usr/bin/env python3
"""펀더멘털 정량 점수 - Piotroski F-Score(부분) · Altman Z-Score.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-02 "분석에 기반이 되는 논문·방법론을 찾아 도입".
      등재는 evidence/methods.py (인용·부분구현 사유의 단일 출처).

▶ 이 파일이 지키는 것
  1. **없는 것을 만들지 않는다.** DART 주요계정에는 현금흐름표가 없다.
     그래서 F-Score 9신호 중 3개(CFO>0, 발생액, 매출총이익률)는 계산하지
     않고 'unavailable' 로 보고한다. 9점 척도인 척하지 않는다 - 점수는
     available 분모와 함께만 의미가 있다.
  2. **결측은 0점이 아니다.** 어떤 신호의 재료가 없으면 그 신호는 채점에서
     빠진다(분모 감소). 결측을 0점으로 처리하면 자료가 부실한 회사가
     자동으로 나쁜 회사가 된다 - 흔하고 치명적인 오류다.
  3. 전기 대비 신호는 전기 값이 있어야만 계산한다(prior_fact 없으면 제외).

실행: python evidence/fundamental_scores.py   # 자체 점검 (네트워크·DB 없음)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

SCORES_VERSION = "research-fundamental-scores-v1"

# DART 주요계정 명칭 (research.financial_facts.account_code 의 scheme)
A_NET_INCOME = "IS:당기순이익(손실)"
A_ASSETS = "BS:자산총계"
A_LIABILITIES = "BS:부채총계"
A_EQUITY = "BS:자본총계"
A_CUR_ASSETS = "BS:유동자산"
A_CUR_LIAB = "BS:유동부채"
A_NONCUR_LIAB = "BS:비유동부채"
A_REVENUE = "IS:매출액"
A_OPERATING = "IS:영업이익"
A_RETAINED = "BS:이익잉여금"
A_CAPITAL_STOCK = "BS:자본금"

# Altman(1968) 판정 구간. 우리는 X4 를 장부가로 대용하므로 구간을 그대로
# 결론으로 쓰지 않고 라벨에 (참고) 를 붙인다 - methods.py partial_reason 참고
Z_SAFE = 2.99
Z_DISTRESS = 1.81


@dataclass(frozen=True)
class Signal:
    key: str
    passed: bool | None      # None = 재료 없음 (채점 제외)
    detail: str


@dataclass(frozen=True)
class FScore:
    score: int
    available: int
    signals: tuple[Signal, ...]
    unavailable: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """분모를 감춘 라벨은 만들지 않는다."""
        if self.available == 0:
            return "INSUFFICIENT_DATA"
        r = self.score / self.available
        if r >= 0.8:
            return "STRONG"
        if r >= 0.5:
            return "MIXED"
        return "WEAK"

    def as_dict(self) -> dict:
        return {"f_score": self.score, "f_available": self.available,
                "f_label": self.label,
                "f_signals": {s.key: s.passed for s in self.signals},
                "f_unavailable": list(self.unavailable)}


def _div(a, b):
    """0 나눗셈·결측은 None. 0 으로 채우면 비율이 거짓말을 한다."""
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    return None if b == 0 else a / b


def f_score(cur: dict, prior: dict | None) -> FScore:
    """Piotroski F-Score 중 **계산 가능한 6신호**.

    cur/prior: {account_code: value}. prior 가 없으면 변화 신호 4개가 빠져
    available 이 2 로 줄어든다 - 그것이 사실이다.
    """
    sigs: list[Signal] = []
    prior = prior or {}

    roa = _div(cur.get(A_NET_INCOME), cur.get(A_ASSETS))
    sigs.append(Signal("ROA_POSITIVE", None if roa is None else roa > 0,
                       f"ROA={roa:.4f}" if roa is not None else "재료 없음"))

    roa_prev = _div(prior.get(A_NET_INCOME), prior.get(A_ASSETS))
    sigs.append(Signal(
        "ROA_IMPROVED",
        None if (roa is None or roa_prev is None) else roa > roa_prev,
        f"{roa_prev:.4f} -> {roa:.4f}" if (roa is not None and roa_prev is not None)
        else "전기 없음"))

    lev = _div(cur.get(A_NONCUR_LIAB), cur.get(A_ASSETS))
    lev_prev = _div(prior.get(A_NONCUR_LIAB), prior.get(A_ASSETS))
    sigs.append(Signal(
        "LEVERAGE_DOWN",
        None if (lev is None or lev_prev is None) else lev < lev_prev,
        f"{lev_prev:.4f} -> {lev:.4f}" if (lev is not None and lev_prev is not None)
        else "재료 없음"))

    liq = _div(cur.get(A_CUR_ASSETS), cur.get(A_CUR_LIAB))
    liq_prev = _div(prior.get(A_CUR_ASSETS), prior.get(A_CUR_LIAB))
    sigs.append(Signal(
        "LIQUIDITY_UP",
        None if (liq is None or liq_prev is None) else liq > liq_prev,
        f"{liq_prev:.4f} -> {liq:.4f}" if (liq is not None and liq_prev is not None)
        else "재료 없음"))

    # EQ_OFFER 대용: 자본금이 늘지 않았으면 통과. 무상증자·액면분할이 섞이는
    # 근사라 methods.py 에 대용임을 명시했다.
    cap, cap_prev = cur.get(A_CAPITAL_STOCK), prior.get(A_CAPITAL_STOCK)
    try:
        no_issue = float(cap) <= float(cap_prev)
        detail = f"자본금 {float(cap_prev):,.0f} -> {float(cap):,.0f}"
    except (TypeError, ValueError):
        no_issue, detail = None, "재료 없음"
    sigs.append(Signal("NO_NEW_EQUITY(proxy)", no_issue, detail))

    turn = _div(cur.get(A_REVENUE), cur.get(A_ASSETS))
    turn_prev = _div(prior.get(A_REVENUE), prior.get(A_ASSETS))
    sigs.append(Signal(
        "ASSET_TURNOVER_UP",
        None if (turn is None or turn_prev is None) else turn > turn_prev,
        f"{turn_prev:.4f} -> {turn:.4f}" if (turn is not None and turn_prev is not None)
        else "재료 없음"))

    scored = [s for s in sigs if s.passed is not None]
    return FScore(
        score=sum(1 for s in scored if s.passed),
        available=len(scored),
        signals=tuple(sigs),
        unavailable=("CFO_POSITIVE", "ACCRUAL(CFO>ROA)", "GROSS_MARGIN_UP"),
    )


@dataclass(frozen=True)
class ZScore:
    z: float | None
    components: dict
    label: str
    proxy_used: bool = True

    def as_dict(self) -> dict:
        return {"altman_z": None if self.z is None else round(self.z, 3),
                "altman_label": self.label,
                "altman_components": {k: (None if v is None else round(v, 4))
                                      for k, v in self.components.items()},
                "altman_x4_is_proxy": self.proxy_used}


def altman_z(cur: dict) -> ZScore:
    """Altman(1968) Z-Score. X4 는 시가총액이 없어 자본총계/부채총계로 대용.

    구성요소 하나라도 없으면 z 를 만들지 않는다 - 부분 합으로 점수를 내면
    구간 판정이 통째로 어긋난다.
    """
    assets = cur.get(A_ASSETS)
    x1 = _div((float(cur[A_CUR_ASSETS]) - float(cur[A_CUR_LIAB]))
              if (cur.get(A_CUR_ASSETS) is not None
                  and cur.get(A_CUR_LIAB) is not None) else None, assets)
    x2 = _div(cur.get(A_RETAINED), assets)
    x3 = _div(cur.get(A_OPERATING), assets)          # EBIT 대용 = 영업이익
    x4 = _div(cur.get(A_EQUITY), cur.get(A_LIABILITIES))
    x5 = _div(cur.get(A_REVENUE), assets)
    comp = {"X1_운전자본/자산": x1, "X2_이익잉여금/자산": x2,
            "X3_영업이익/자산": x3, "X4_자본/부채(대용)": x4, "X5_매출/자산": x5}
    if any(v is None for v in comp.values()):
        return ZScore(None, comp, "INSUFFICIENT_DATA")
    z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5
    label = "SAFE(참고)" if z > Z_SAFE else (
        "DISTRESS(참고)" if z < Z_DISTRESS else "GREY(참고)")
    return ZScore(z, comp, label)


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

_GOOD = {A_NET_INCOME: 100, A_ASSETS: 1000, A_CUR_ASSETS: 400, A_CUR_LIAB: 200,
         A_NONCUR_LIAB: 100, A_REVENUE: 800, A_CAPITAL_STOCK: 50,
         A_RETAINED: 300, A_OPERATING: 120, A_EQUITY: 700, A_LIABILITIES: 300}
_PRIOR = {A_NET_INCOME: 50, A_ASSETS: 1000, A_CUR_ASSETS: 300, A_CUR_LIAB: 200,
          A_NONCUR_LIAB: 150, A_REVENUE: 700, A_CAPITAL_STOCK: 50}


def _check_f_all_pass():
    f = f_score(_GOOD, _PRIOR)
    assert f.available == 6 and f.score == 6, (f.score, f.available)
    assert f.label == "STRONG"
    assert len(f.unavailable) == 3, "계산 불가 신호를 감췄다"
    print("  F-Score 전신호 통과      OK")


def _check_missing_is_not_zero():
    """결측은 0점이 아니라 채점 제외다 - 자료 부실 = 나쁜 회사가 되면 안 된다."""
    thin = {A_NET_INCOME: 100, A_ASSETS: 1000}          # 전기·나머지 없음
    f = f_score(thin, None)
    assert f.available == 1 and f.score == 1, (f.score, f.available)
    assert f.label == "STRONG", "분모가 1이면 1/1 은 STRONG 이 맞다"
    empty = f_score({}, None)
    assert empty.available == 0 and empty.label == "INSUFFICIENT_DATA"
    print("  결측=제외(0점 아님)      OK")


def _check_f_direction():
    worse = dict(_GOOD, **{A_NET_INCOME: -10, A_NONCUR_LIAB: 300,
                           A_CUR_ASSETS: 100, A_REVENUE: 500,
                           A_CAPITAL_STOCK: 80})
    f = f_score(worse, _PRIOR)
    got = {s.key: s.passed for s in f.signals}
    assert got["ROA_POSITIVE"] is False and got["ROA_IMPROVED"] is False
    assert got["LEVERAGE_DOWN"] is False and got["LIQUIDITY_UP"] is False
    assert got["NO_NEW_EQUITY(proxy)"] is False, "자본금 증가를 놓쳤다"
    assert got["ASSET_TURNOVER_UP"] is False
    assert f.score == 0 and f.label == "WEAK"
    print("  F-Score 악화 방향        OK")


def _check_altman():
    z = altman_z(_GOOD)
    # 1.2*0.2 + 1.4*0.3 + 3.3*0.12 + 0.6*(700/300) + 1.0*0.8 = 3.256
    assert z.z is not None and abs(z.z - 3.256) < 1e-6, z.z
    assert z.label == "SAFE(참고)" and z.proxy_used
    # 구성요소 하나라도 없으면 점수를 만들지 않는다
    bad = dict(_GOOD); bad.pop(A_RETAINED)
    assert altman_z(bad).z is None
    assert altman_z(bad).label == "INSUFFICIENT_DATA"
    # 0 나눗셈 방어
    zero = dict(_GOOD, **{A_LIABILITIES: 0})
    assert altman_z(zero).z is None
    print("  Altman Z·결측 방어       OK")


def _check_registry_consistency():
    """레지스트리와 구현이 어긋나면 둘 중 하나는 거짓말이다."""
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    from methods import METHODS, STATUS_ADOPTED

    got = {m.key: m for m in METHODS}
    for key in ("piotroski_f_score", "altman_z_score"):
        assert got[key].status == STATUS_ADOPTED, key
        assert "fundamental_scores.py" in (got[key].module or ""), key
    f = f_score(_GOOD, _PRIOR)
    assert "9신호 중 6개" in got["piotroski_f_score"].partial_reason
    assert f.available == 6, "레지스트리는 6개라는데 구현이 다르다"
    print("  레지스트리-구현 일치     OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"{SCORES_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_f_all_pass()
    _check_missing_is_not_zero()
    _check_f_direction()
    _check_altman()
    _check_registry_consistency()
    print("펀더멘털 점수 5개 영역 통과")
