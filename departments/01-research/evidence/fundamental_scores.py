#!/usr/bin/env python3
"""펀더멘털 정량 점수 - Piotroski F-Score(부분) · Altman Z-Score.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-02 "분석에 기반이 되는 논문·방법론을 찾아 도입".
      등재는 evidence/methods.py (인용·부분구현 사유의 단일 출처).

▶ 이 파일이 지키는 것
  1. **없는 것을 만들지 않는다.** 2026-08-02 현금흐름표 수집(2019020)으로
     CFO>0·발생액 2신호가 가능해져 8/9 가 됐다. 남은 1개(매출총이익률)는
     DART 어느 API 에도 매출총이익이 표준 계정으로 없어 여전히 불가다.
     9점 척도인 척하지 않는다 - 점수는 available 분모와 함께만 의미가 있다.
  2. **결측은 0점이 아니다.** 어떤 신호의 재료가 없으면 그 신호는 채점에서
     빠진다(분모 감소). 결측을 0점으로 처리하면 자료가 부실한 회사가
     자동으로 나쁜 회사가 된다 - 흔하고 치명적인 오류다.
  3. 전기 대비 신호는 전기 값이 있어야만 계산한다(prior_fact 없으면 제외).

실행: python evidence/fundamental_scores.py   # 자체 점검 (네트워크·DB 없음)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

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

# 영업활동현금흐름(CFO). DART 전체 재무제표는 계정명을 표준화하지 않아 회사마다
# 다르게 쓴다(실측 2026-08-02: 16개사에서 10가지 변형). **총액만 골라야 한다** -
# "영업활동에서 창출된 현금"은 이자·법인세 차감 전이고 "자산·부채의 변동"은
# 운전자본 변동이라 둘 다 CFO 가 아니다. 이걸 섞으면 F-Score 가 조용히 틀린다.
CFO_REQUIRE = ("영업활동",)
CFO_EXCLUDE = ("창출", "변동", "자산", "부채", "차감", "법인세", "이자")

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


def is_cfo_account(account_name: str) -> bool:
    """이 계정명이 영업활동현금흐름 **총액**인가.

    포함어와 제외어를 함께 본다 - 포함만 보면 하위 항목(창출된 현금, 자산·부채
    변동)이 딸려 들어와 CFO 를 잘못 잡는다.
    """
    nm = " ".join(str(account_name or "").split())
    if not nm.startswith("CF:") and "CF:" in nm:
        nm = nm.split("CF:", 1)[1]
    nm = nm.replace("CF:", "")
    if not all(k in nm for k in CFO_REQUIRE):
        return False
    if any(k in nm for k in CFO_EXCLUDE):
        return False
    return "현금흐름" in nm or "현금" in nm


def find_cfo(facts: dict):
    """{계정코드: 값} 에서 CFO 를 찾는다. 없으면 None(0 으로 채우지 않는다).

    후보가 여럿이면 **절대값이 가장 큰 것**을 취한다 - 총액이 하위 항목보다
    크다는 경험칙이고, 애초에 하위 항목은 위 제외어로 대부분 걸러진다.
    """
    cands = [(k, v) for k, v in facts.items()
             if k.startswith("CF:") and is_cfo_account(k) and v is not None]
    if not cands:
        return None
    try:
        return max(cands, key=lambda kv: abs(float(kv[1])))[1]
    except (TypeError, ValueError):
        return None


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

    # --- 현금흐름 신호 (2026-08-02 opendart_cashflow 수집으로 가능해짐) ---
    cfo = find_cfo(cur)
    sigs.append(Signal("CFO_POSITIVE", None if cfo is None else float(cfo) > 0,
                       f"CFO={float(cfo):,.0f}" if cfo is not None else "CFO 없음"))
    # 발생액 신호: CFO/자산 > ROA (현금이 이익을 뒷받침하는가)
    cfo_ta = _div(cfo, cur.get(A_ASSETS))
    sigs.append(Signal(
        "ACCRUAL(CFO>ROA)",
        None if (cfo_ta is None or roa is None) else cfo_ta > roa,
        f"CFO/자산={cfo_ta:.4f} vs ROA={roa:.4f}"
        if (cfo_ta is not None and roa is not None) else "재료 없음"))

    scored = [s for s in sigs if s.passed is not None]
    return FScore(
        score=sum(1 for s in scored if s.passed),
        available=len(scored),
        signals=tuple(sigs),
        # 매출총이익은 DART 주요계정·전체재무제표 어디에도 표준 계정으로 없다
        unavailable=("GROSS_MARGIN_UP",),
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
         A_RETAINED: 300, A_OPERATING: 120, A_EQUITY: 700, A_LIABILITIES: 300,
         # CFO 150 > 순이익 100 -> CFO>0 과 발생액 둘 다 통과
         "CF:영업활동현금흐름": 150}
_PRIOR = {A_NET_INCOME: 50, A_ASSETS: 1000, A_CUR_ASSETS: 300, A_CUR_LIAB: 200,
          A_NONCUR_LIAB: 150, A_REVENUE: 700, A_CAPITAL_STOCK: 50}


def _check_cfo_detection():
    """CFO 총액만 고르고 하위 항목은 배제한다 - 섞이면 F-Score 가 조용히 틀린다.

    실측 2026-08-02: 16개사에서 계정명 10가지 변형이 나왔다.
    """
    for nm in ("CF:영업활동현금흐름", "CF:영업활동으로 인한 현금흐름",
               "CF:영업활동순현금흐름", "CF:영업활동으로 인한 순현금흐름",
               "CF:영업활동 현금흐름", "CF:영업활동으로부터의 현금흐름"):
        assert is_cfo_account(nm), nm
    # 총액이 아닌 것들 - 이자·법인세 차감 전이거나 운전자본 변동이다
    for nm in ("CF:영업활동에서 창출된 현금흐름", "CF:영업활동에서 창출된 현금",
               "CF:영업활동으로 인한 자산·부채의 변동",
               "CF:영업활동으로 인한 자산부채의 변동",
               "CF:투자활동현금흐름", "CF:재무활동현금흐름", "IS:영업이익", ""):
        assert not is_cfo_account(nm), nm

    # 후보가 여럿이면 절대값이 큰 총액을 취한다
    facts = {"CF:영업활동현금흐름": 1000, "CF:투자활동현금흐름": -5000,
             "BS:자산총계": 9999}
    assert find_cfo(facts) == 1000
    assert find_cfo({"BS:자산총계": 100}) is None, "없으면 None(0 아님)"
    assert find_cfo({"CF:영업활동현금흐름": None}) is None
    print("  CFO 계정 판별            OK")


def _check_f_all_pass():
    f = f_score(_GOOD, _PRIOR)
    assert f.available == 8 and f.score == 8, (f.score, f.available)
    assert f.label == "STRONG"
    assert f.unavailable == ("GROSS_MARGIN_UP",), f.unavailable
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
                           A_CAPITAL_STOCK: 80, "CF:영업활동현금흐름": -20})
    f = f_score(worse, _PRIOR)
    got = {s.key: s.passed for s in f.signals}
    assert got["ROA_POSITIVE"] is False and got["ROA_IMPROVED"] is False
    assert got["LEVERAGE_DOWN"] is False and got["LIQUIDITY_UP"] is False
    assert got["NO_NEW_EQUITY(proxy)"] is False, "자본금 증가를 놓쳤다"
    assert got["ASSET_TURNOVER_UP"] is False
    assert got["CFO_POSITIVE"] is False, "적자 현금흐름을 놓쳤다"
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
    assert f.available == 8, "레지스트리 문구와 구현 분모가 어긋난다"
    print("  레지스트리-구현 일치     OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"{SCORES_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_cfo_detection()
    _check_f_all_pass()
    _check_missing_is_not_zero()
    _check_f_direction()
    _check_altman()
    _check_registry_consistency()
    print("펀더멘털 점수 6개 영역 통과")
