"""국면 분해 - 이 전략이 특정 장세에서만 되는가.

담당: 재일 (퀀트·백테스트본부 QNT)
근거: contracts/quant_v2.ExperimentCardV1.regime_breakdown
      (SUBMIT_TO_QA 는 이것이 비어 있으면 계약이 거부한다 - 7.3절 P0)

▶ 무엇을 보는가
  창별 성과를 **그 창의 시장 상태**로 묶는다. 상승장에서만 벌고 하락장에서
  전부 토해내는 전략은 총수익이 좋아도 운용할 수 없다 - 총계 하나로는
  그 사실이 안 보인다.

▶ 국면은 전략이 아니라 시장이 정한다
  같은 기간의 **벤치마크(동일가중 매수보유)** 수익률로 나눈다. 전략 성과로
  나누면 "잘된 창" 과 "상승장" 이 같은 말이 되어 아무것도 못 가린다 -
  자기 결과로 자기를 설명하는 순환이다.

▶ 표본 1개짜리 국면은 통계가 아니다
  한 창만 있는 국면은 값을 내되 **n=1 임을 함께 싣는다.** 평균이라고
  부르면 안 된다. 국면이 하나뿐이면(전 구간이 같은 장세) 분해가 성립하지
  않으므로 그 사실을 낸다 - 억지로 쪼개지 않는다.

▶ 경계값은 임의다, 그러니 고정하고 적는다
  ±5% 반기 수익률로 나눈다. 반기 ±5% 는 연 ±10% 수준으로, 한국 시장에서
  "방향이 있었다" 고 말할 만한 최소선이다. 임의지만 **실험마다 바뀌지 않게**
  고정하고 카드에 남긴다 - 사후에 경계를 옮기면 원하는 그림을 만들 수 있다.

자체 점검: python departments/04-quant-backtest/pipeline/regime_breakdown.py
"""

from __future__ import annotations

import sys

MODULE_VERSION = "quant-regime-breakdown-v1"

# 반기 벤치마크 수익률 경계. **사후에 옮기지 않는다** - 옮기면 원하는 그림이
# 나온다. 카드에 함께 실어 어느 기준으로 나눴는지 남긴다.
BULL_THRESHOLD = 0.05
BEAR_THRESHOLD = -0.05

# 국면 이름. 계약이 dict[str, dict[str, float]] 이므로 키는 문자열이다.
BULL, BEAR, FLAT = "BULL", "BEAR", "SIDEWAYS"

# 이 창수 미만인 국면은 "평균" 이라 부르지 않는다(값은 낸다).
MIN_WINDOWS_FOR_MEAN = 2


def classify(benchmark_return: float | None) -> str | None:
    """시장 수익률 -> 국면. **모르면 None 이다**(0 을 SIDEWAYS 로 읽지 않는다).

    벤치마크가 없는 창을 SIDEWAYS 로 채우면 "횡보장에서도 됐다" 는 근거가
    데이터 없이 생긴다.
    """
    if benchmark_return is None:
        return None
    if benchmark_return >= BULL_THRESHOLD:
        return BULL
    if benchmark_return <= BEAR_THRESHOLD:
        return BEAR
    return FLAT


def build(window_metrics: list, benchmarks: dict) -> dict:
    """창별 지표 + 창별 벤치마크 수익률 -> 국면 분해. **순수 함수.**

    window_metrics: [(label, {"total_return":…, "sharpe_rf0":…, "max_drawdown":…}), …]
    benchmarks:     {label: benchmark_total_return}
    """
    buckets: dict = {}
    unclassified = []
    for label, m in window_metrics:
        r = classify(benchmarks.get(label))
        if r is None:
            # 조용히 버리지 않는다 - 몇 창을 못 나눴는지 남는다
            unclassified.append(label)
            continue
        buckets.setdefault(r, []).append((label, m))

    out: dict = {}
    for name, items in buckets.items():
        rets = [m["total_return"] for _, m in items]
        excess = [m["total_return"] - benchmarks[l] for l, m in items]
        row = {
            "n_windows": float(len(items)),
            "total_return": round(sum(rets) / len(rets), 6),
            "excess_return": round(sum(excess) / len(excess), 6),
            "worst_return": round(min(rets), 6),
            "win_ratio": round(sum(1 for r in rets if r > 0) / len(rets), 4),
            "benchmark_return": round(
                sum(benchmarks[l] for l, _ in items) / len(items), 6),
        }
        # ▶ 표본 1개는 평균이 아니다. 값은 내되 사실을 함께 싣는다.
        row["is_single_sample"] = 0.0 if len(items) >= MIN_WINDOWS_FOR_MEAN else 1.0
        out[name] = row

    meta = {
        "n_regimes": float(len(out)),
        "n_unclassified_windows": float(len(unclassified)),
        "bull_threshold": BULL_THRESHOLD,
        "bear_threshold": BEAR_THRESHOLD,
    }
    return {"regime_breakdown": out, "meta": meta,
            "unclassified_windows": unclassified}


def concerns(breakdown: dict) -> list:
    """분해가 말해주는 것. **비어 있으면 문제없다는 뜻이 아니다** - 국면이
    하나뿐이면 애초에 비교가 없었다는 뜻이다."""
    out = []
    if len(breakdown) < 2:
        out.append(
            f"국면이 {len(breakdown)}개뿐이다 - 장세를 가로질러 검증되지 "
            f"않았다(전 구간이 같은 장세였거나 창이 부족하다)")
        return out
    if BEAR in breakdown and breakdown[BEAR]["total_return"] < 0:
        b = breakdown[BEAR]
        out.append(
            f"하락장 평균 수익률 {b['total_return']:.1%} - 상승장에서 벌고 "
            f"하락장에서 토해내는 형태인지 확인해야 한다"
            + (" (하락장 표본 1개)" if b["is_single_sample"] else ""))
    if BULL in breakdown and breakdown[BULL]["excess_return"] < 0:
        out.append(
            f"상승장 초과수익 {breakdown[BULL]['excess_return']:.1%} - "
            f"상승장에서 시장을 못 이겼다면 그냥 지수를 사는 편이 낫다")
    singles = [k for k, v in breakdown.items() if v["is_single_sample"]]
    if singles:
        out.append(f"표본 1창짜리 국면: {sorted(singles)} - 평균으로 읽지 않는다")
    return out


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _wm(**kw):
    return [(l, {"total_return": r, "sharpe_rf0": 1.0, "max_drawdown": -0.1})
            for l, r in kw.items()]


def _check_market_defines_regime_not_strategy():
    """**전략 성과로 국면을 나누면 순환이다** - "잘된 창" = "상승장" 이 된다.

    전략이 크게 번 창이라도 시장이 빠졌으면 BEAR 다.
    """
    wm = _wm(w1=0.40)                       # 전략 +40%
    r = build(wm, {"w1": -0.20})            # 시장 -20%
    assert BEAR in r["regime_breakdown"], r
    assert BULL not in r["regime_breakdown"], r


def _check_unknown_benchmark_is_not_sideways():
    """**벤치마크 없는 창을 SIDEWAYS 로 채우지 않는다.**

    채우면 "횡보장에서도 됐다" 는 근거가 데이터 없이 생긴다.
    """
    assert classify(None) is None
    r = build(_wm(w1=0.1, w2=0.2), {"w1": 0.10})
    assert r["unclassified_windows"] == ["w2"], r
    assert r["meta"]["n_unclassified_windows"] == 1.0, r
    assert FLAT not in r["regime_breakdown"], r


def _check_single_sample_is_flagged():
    """표본 1개를 평균이라 부르지 않는다."""
    r = build(_wm(w1=0.3, w2=0.2, w3=-0.1),
              {"w1": 0.10, "w2": 0.08, "w3": -0.30})
    bd = r["regime_breakdown"]
    assert bd[BULL]["is_single_sample"] == 0.0, bd   # 2창
    assert bd[BEAR]["is_single_sample"] == 1.0, bd   # 1창
    assert any("표본 1창" in c for c in concerns(bd)), concerns(bd)


def _check_bear_loss_is_surfaced():
    """**상승장에서 벌고 하락장에서 토해내는 형태**를 총계가 숨긴다."""
    r = build(_wm(w1=0.50, w2=0.45, w3=-0.40, w4=-0.35),
              {"w1": 0.10, "w2": 0.12, "w3": -0.20, "w4": -0.25})
    bd = r["regime_breakdown"]
    assert bd[BEAR]["total_return"] < 0, bd
    assert any("하락장" in c for c in concerns(bd)), concerns(bd)


def _check_one_regime_means_no_comparison():
    """**국면이 하나뿐이면 비교가 없었다** - 빈 우려 목록이 안전을 뜻하지 않는다."""
    r = build(_wm(w1=0.2, w2=0.3), {"w1": 0.10, "w2": 0.11})
    c = concerns(r["regime_breakdown"])
    assert len(r["regime_breakdown"]) == 1, r
    assert any("장세를 가로질러 검증되지" in x for x in c), c


def _check_thresholds_are_recorded():
    """**사후에 경계를 옮기면 원하는 그림이 나온다** - 카드에 남긴다."""
    m = build(_wm(w1=0.1), {"w1": 0.2})["meta"]
    assert m["bull_threshold"] == BULL_THRESHOLD
    assert m["bear_threshold"] == BEAR_THRESHOLD


def _check_values_are_floats():
    """계약이 dict[str, dict[str, float]] 이다 - bool/str 이 섞이면 카드가 죽는다."""
    bd = build(_wm(w1=0.3, w2=-0.4), {"w1": 0.1, "w2": -0.3})["regime_breakdown"]
    for row in bd.values():
        for k, v in row.items():
            assert isinstance(v, float) and not isinstance(v, bool), (k, v)


def _check_excess_is_vs_same_window_benchmark():
    """초과수익은 **같은 창의** 시장 대비다 - 전 구간 평균 대비가 아니다."""
    bd = build(_wm(w1=0.30), {"w1": 0.10})["regime_breakdown"]
    assert abs(bd[BULL]["excess_return"] - 0.20) < 1e-9, bd


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_market_defines_regime_not_strategy(); print("  시장이 국면 정의        OK")
    _check_unknown_benchmark_is_not_sideways();  print("  미상 != 횡보            OK")
    _check_single_sample_is_flagged();           print("  표본 1개 표시           OK")
    _check_bear_loss_is_surfaced();              print("  하락장 손실 표면화       OK")
    _check_one_regime_means_no_comparison();     print("  국면 1개 = 비교 없음    OK")
    _check_thresholds_are_recorded();            print("  경계 기록               OK")
    _check_values_are_floats();                  print("  float 계약              OK")
    _check_excess_is_vs_same_window_benchmark(); print("  동일 창 대비 초과       OK")
    print("국면 분해 8개 영역 통과.")
