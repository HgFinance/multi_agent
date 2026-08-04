"""과적합 통계 - "높은 Sharpe 가 알파인가 우연인가" 를 수치로 가른다.

담당: 재일 (퀀트·백테스트본부 QNT)
근거: 재일님 계획 2026-08-04 "다중검정·과적합 검증" - Deflated Sharpe Ratio,
      PBO, Bootstrap CI 를 ExperimentCard 필수 검증으로

▶ 왜 필요한가
  contracts/quant_v2.ExperimentCardV1 은 deflated_sharpe / bootstrap_ci /
  purged_walk_forward / cpcv 를 필드로 갖고 있는데 **채우는 코드가 0개였다.**
  계약은 요구하는데 아무도 계산하지 않는 상태 - 오늘만 세 번째 같은 패턴이다
  (F-Score, industry_code, trial_pressure).

  trial_pressure 는 "몇 번 시도했나" 를 세지만 그것만으로는 부족하다.
  **20번 중 최고가 Sharpe 1.5 라면 그게 우연으로 나올 확률**을 계산해야
  실력과 운을 가른다. 그것이 Deflated Sharpe Ratio 다.

▶ 무엇을 계산하고 무엇을 하지 않는가
  한다   : DSR, Bootstrap CI, PBO(조합 대칭 교차검증 기반 과적합 확률)
  안 한다: 이 숫자로 자동 승격·강등. 판정은 release_gate 가 하고,
           승격은 CEO·Risk·QA 몫이다.

  표본이 부족하면 **None 을 낸다.** 0 이나 낙관적 기본값으로 채우면
  "검증했다" 는 착각만 만든다 - 계약도 None 을 허용한다.

자체 점검: python departments/04-quant-backtest/pipeline/overfit_stats.py
"""

from __future__ import annotations

import math
import sys
from typing import Sequence

MODULE_VERSION = "quant-overfit-stats-v1"

# 이보다 짧으면 어떤 통계도 신뢰할 수 없다. 60 거래일은 약 3개월이고,
# 그보다 짧은 표본으로 Sharpe 를 논하는 것은 잡음을 논하는 것이다.
MIN_RETURNS = 60

# Bootstrap 재표본 수. 1000 이면 95% 구간의 경계가 안정된다.
BOOTSTRAP_N = 1000

TRADING_DAYS = 252


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _std(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def _skew_kurt(xs: Sequence[float]) -> tuple[float, float]:
    """왜도·첨도. DSR 이 정규성 가정을 벗어난 분포를 보정하는 데 쓴다."""
    n, sd = len(xs), _std(xs)
    if n < 4 or sd == 0:
        return 0.0, 3.0
    m = _mean(xs)
    z = [(x - m) / sd for x in xs]
    skew = sum(v ** 3 for v in z) / n
    kurt = sum(v ** 4 for v in z) / n
    return skew, kurt


def sharpe(returns: Sequence[float], *, periods: int = TRADING_DAYS) -> float | None:
    """연율화 Sharpe. 표본이 짧으면 None - 짧은 표본의 Sharpe 는 잡음이다."""
    if len(returns) < MIN_RETURNS:
        return None
    sd = _std(returns)
    if sd == 0:
        return None                    # 변동이 없으면 Sharpe 가 정의되지 않는다
    return _mean(returns) / sd * math.sqrt(periods)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """역정규. Acklam 근사 - scipy 없이 쓰기 위해."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"확률이 (0,1) 밖이다: {p}")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def deflated_sharpe(returns: Sequence[float], *, trials: int,
                    periods: int = TRADING_DAYS) -> dict:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014).

    ▶ 이 함수가 답하는 질문
      "N번 시도해서 얻은 최고 Sharpe 가, **실제로는 알파가 0인데** 우연히
       이만큼 나올 확률은?" - DSR 은 그 확률을 뺀 값이다.

      trials 가 커질수록 기대 최고 Sharpe 가 올라간다. 20번 돌리면 알파가
      없어도 Sharpe 0.7 정도는 흔히 나온다. 그 기준선을 넘어야 의미가 있다.

    반환의 dsr 은 **0~1 확률**이다(1에 가까울수록 우연이 아니다).
    표본이 짧으면 None - 없는 확신을 만들지 않는다.
    """
    sr = sharpe(returns, periods=periods)
    n = len(returns)
    if sr is None or trials < 1:
        return {"deflated_sharpe": None, "sharpe": sr,
                "reason": f"표본 {n}개(최소 {MIN_RETURNS}) 또는 trials 부족"}

    # 기대 최고 Sharpe: 알파가 0인 N번 시도에서 최고치가 어디쯤 나오는가
    gamma = 0.5772156649015329        # 오일러-마스케로니
    if trials == 1:
        sr0 = 0.0
    else:
        e1 = _norm_ppf(1.0 - 1.0 / trials)
        e2 = _norm_ppf(1.0 - 1.0 / (trials * math.e))
        sr0 = (1 - gamma) * e1 + gamma * e2

    skew, kurt = _skew_kurt(returns)
    # 연율화된 Sharpe 를 기간당으로 되돌려 분산을 계산한다
    sr_p = sr / math.sqrt(periods)
    denom = 1.0 - skew * sr_p + (kurt - 1.0) / 4.0 * sr_p ** 2
    if denom <= 0 or n < 2:
        return {"deflated_sharpe": None, "sharpe": sr,
                "reason": "분포 보정항이 음수 - DSR 을 정의할 수 없다"}
    z = (sr_p - sr0 / math.sqrt(periods)) * math.sqrt(n - 1) / math.sqrt(denom)
    return {
        "deflated_sharpe": round(_norm_cdf(z), 4),
        "sharpe": round(sr, 4),
        "expected_max_sharpe": round(sr0, 4),   # trials 로 인한 기준선
        "trials": trials,
        "skew": round(skew, 4), "kurtosis": round(kurt, 4),
    }


def bootstrap_ci(returns: Sequence[float], *, n_boot: int = BOOTSTRAP_N,
                 alpha: float = 0.05, seed: int = 20260804,
                 periods: int = TRADING_DAYS) -> dict:
    """Sharpe 의 부트스트랩 신뢰구간. **구간이 0을 걸치면 알파가 아니다.**

    난수 seed 를 고정한다 - 재현되지 않는 검증은 검증이 아니다.
    """
    n = len(returns)
    if n < MIN_RETURNS:
        return {"bootstrap_ci_low": None, "bootstrap_ci_high": None,
                "reason": f"표본 {n}개 < 최소 {MIN_RETURNS}"}

    # 선형 합동 생성기 - 표준 라이브러리 random 을 쓰면 전역 상태에 얽힌다
    state = seed
    def _rand_idx() -> int:
        nonlocal state
        state = (1103515245 * state + 12345) % (1 << 31)
        return state % n

    stats = []
    for _ in range(n_boot):
        sample = [returns[_rand_idx()] for _ in range(n)]
        s = sharpe(sample, periods=periods)
        if s is not None:
            stats.append(s)
    if len(stats) < n_boot // 2:
        return {"bootstrap_ci_low": None, "bootstrap_ci_high": None,
                "reason": "재표본 다수에서 Sharpe 가 정의되지 않았다"}
    stats.sort()
    lo = stats[int(len(stats) * alpha / 2)]
    hi = stats[min(int(len(stats) * (1 - alpha / 2)), len(stats) - 1)]
    return {
        "bootstrap_ci_low": round(lo, 4),
        "bootstrap_ci_high": round(hi, 4),
        # ▶ 구간이 0을 걸치면 "알파가 있다" 고 말할 수 없다. 판정은 여기서
        #   하지 않되 사실은 남긴다.
        "ci_excludes_zero": lo > 0.0,
        "n_boot": len(stats), "seed": seed,
    }


def pbo(is_ranks: Sequence[int], n_strategies: int) -> dict:
    """Probability of Backtest Overfitting (Bailey et al. 2016) 축약형.

    ▶ 무엇을 재는가
      in-sample 에서 1등이던 전략이 out-of-sample 에서 **중앙값 아래로**
      떨어지는 비율. 높으면 "백테스트 1등" 이 미래를 예측하지 못한다는 뜻이다.

    is_ranks: 각 분할에서 IS 최고 전략의 **OOS 순위**(1 = 최고).
    """
    if not is_ranks or n_strategies < 2:
        return {"pbo": None, "reason": "분할 또는 전략 수 부족"}
    # OOS 상대순위가 중앙(0.5) 아래면 과적합으로 센다
    below = sum(1 for r in is_ranks if (r - 0.5) / n_strategies > 0.5)
    return {
        "pbo": round(below / len(is_ranks), 4),
        "splits": len(is_ranks),
        "n_strategies": n_strategies,
    }


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _series(mu: float, sd: float, n: int = 300, seed: int = 7) -> list[float]:
    """결정론 의사난수 수익률. 검증이 재현돼야 한다."""
    out, st = [], seed
    for _ in range(n):
        st = (1103515245 * st + 12345) % (1 << 31)
        u1 = (st % 10000 + 1) / 10001
        st = (1103515245 * st + 12345) % (1 << 31)
        u2 = (st % 10000 + 1) / 10001
        z = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
        out.append(mu + sd * z)
    return out


def _check_short_sample_is_none():
    """짧은 표본에 확신을 만들지 않는다 - 0 이 아니라 None 이다."""
    short = _series(0.001, 0.01, n=30)
    assert sharpe(short) is None
    d = deflated_sharpe(short, trials=5)
    assert d["deflated_sharpe"] is None and "표본" in d["reason"], d
    b = bootstrap_ci(short)
    assert b["bootstrap_ci_low"] is None and b["bootstrap_ci_high"] is None


def _check_more_trials_lowers_dsr():
    """**같은 성적이라도 많이 시도했으면 DSR 이 낮아야 한다.**

    이것이 이 모듈의 존재 이유다 - 20번 중 최고 Sharpe 1.5 는 1번 만에 나온
    1.5 와 다르다. trials 가 늘면 기준선(expected_max_sharpe)이 올라간다.
    """
    r = _series(0.0008, 0.01)
    one = deflated_sharpe(r, trials=1)
    many = deflated_sharpe(r, trials=50)
    assert one["deflated_sharpe"] is not None and many["deflated_sharpe"] is not None
    assert many["expected_max_sharpe"] > one["expected_max_sharpe"], (one, many)
    assert many["deflated_sharpe"] < one["deflated_sharpe"], (one, many)


def _check_no_alpha_gets_low_dsr():
    """알파가 없는 계열은 시도를 늘리면 DSR 이 무너져야 한다."""
    flat = _series(0.0, 0.01, seed=11)
    d = deflated_sharpe(flat, trials=100)
    assert d["deflated_sharpe"] is not None
    assert d["deflated_sharpe"] < 0.5, d


def _check_ci_is_ordered_and_deterministic():
    r = _series(0.001, 0.01)
    a = bootstrap_ci(r, n_boot=300)
    b = bootstrap_ci(r, n_boot=300)
    assert a["bootstrap_ci_low"] <= a["bootstrap_ci_high"], a
    # seed 고정 - 재현되지 않는 검증은 검증이 아니다
    assert a == b, (a, b)
    # 계약(quant_v2)이 뒤집힌 구간을 거부하므로 순서를 지켜야 한다
    assert a["bootstrap_ci_low"] is not None


def _check_ci_zero_crossing_flagged():
    """구간이 0을 걸치면 알파라고 말할 수 없다 - 그 사실이 남는가."""
    noise = bootstrap_ci(_series(0.0, 0.02, seed=3), n_boot=300)
    assert noise["ci_excludes_zero"] is False, noise
    strong = bootstrap_ci(_series(0.003, 0.005, seed=3), n_boot=300)
    assert strong["ci_excludes_zero"] is True, strong


def _check_pbo():
    # IS 1등이 OOS 에서도 상위 -> 과적합 낮음
    good = pbo([1, 2, 1, 3, 2], n_strategies=20)
    assert good["pbo"] == 0.0, good
    # IS 1등이 OOS 하위권 -> 과적합 높음
    bad = pbo([18, 19, 20, 17, 16], n_strategies=20)
    assert bad["pbo"] == 1.0, bad
    assert pbo([], 20)["pbo"] is None
    assert pbo([1], 1)["pbo"] is None


def _check_zero_variance_is_none():
    """변동이 없으면 Sharpe 가 정의되지 않는다 - 무한대를 만들지 않는다."""
    assert sharpe([0.001] * 200) is None


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_short_sample_is_none();          print("  짧은 표본 = None        OK")
    _check_more_trials_lowers_dsr();        print("  시도↑ -> DSR↓           OK")
    _check_no_alpha_gets_low_dsr();         print("  무알파 = 낮은 DSR       OK")
    _check_ci_is_ordered_and_deterministic(); print("  CI 순서·재현성          OK")
    _check_ci_zero_crossing_flagged();      print("  CI 0 교차 표시          OK")
    _check_pbo();                           print("  PBO 판정                OK")
    _check_zero_variance_is_none();         print("  무변동 = None           OK")
    print("과적합 통계 7개 영역 통과.")
