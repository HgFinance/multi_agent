#!/usr/bin/env python3
"""막힌 조항 -> **다음에 돌릴 구체적 변형.** 결정론이다, LLM 이 없어도 돈다.

▶ 왜 필요한가 (2026-08-12 실측)
  지금은 기각되면 리서치가 **완전히 새 기획안**을 쓴다. 그래서 이런 일이 났다:

      fam_42663e9f (momentum)  Sharpe 1.2765 · DSR 0.976 · 초과 +157.51%p
        → FRAGILE 로 기각 → 아무도 다시 안 건드림 → breakout(-0.958)을 새로 시작

  찾은 알파를 버리고 새로 찾는 것보다 **막은 조항을 겨냥해 같은 엣지를 다시
  거는 쪽이 훨씬 싸다.** 그런데 그 번역을 아무도 안 했다.

▶ 값을 어디서 가져오나 - **관측에서 유도한다, 지어내지 않는다**
  낙폭이 -50.52% 인데 기준이 -35% 면 필요한 감쇄비는 35/50.52 = 0.693 이다.
  그 비율을 노출 상한·목표변동성에 그대로 쓴다. 1차 근사이고, 맞는지는
  **실험이 판정한다** - 그게 이 변형의 가설이다.

▶ 변형을 많이 만들지 않는다
  시도 1->2 에서 DSR 기준선이 +0.520 뛴다(`objective.deflation_cost`).
  아무 값이나 여러 개 던지면 그 최고치는 실력과 운을 못 가린다.
  **근거가 있는 것만, 적게.**

사용:
    quant-py variant_generator.py --check
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

GENERATOR_VERSION = "quant-variant-generator-v1"

# 이보다 아래로는 노출을 줄이지 않는다. 90% 를 현금으로 두면 그건 그 전략이
# 아니라 다른 전략이다 - 무엇을 검정하는지가 흐려진다.
MIN_SCALE = 0.35
# 낙폭 정지는 기준보다 **여유 있게** 건다. 기준선에 딱 맞추면 한 틱만 넘어도
# 관문을 못 넘는다. 0.8 = 허용 낙폭의 80% 에서 멈춘다.
STOP_MARGIN = 0.8


@dataclass
class Variant:
    """다음에 돌릴 변형 하나. **무엇을 노리는지 반드시 적는다.**"""

    kind: str                       # 무엇을 바꾸나
    targets: list                   # 어느 조항을 노리나
    config_delta: dict = field(default_factory=dict)
    dataset: str = ""               # 바꿀 데이터셋 (빈 문자열이면 그대로)
    rationale: str = ""             # 왜 이 값인가 - 관측에서 유도한 근거

    def as_dict(self) -> dict:
        return asdict(self)


def _scale_for(actual, bar) -> float | None:
    """기준까지 줄이려면 노출을 얼마로? **관측 비율에서 나온다.**

    낙폭은 노출에 대략 비례한다(1차 근사). -50.52% 를 -35% 로 만들려면
    35/50.52 = 0.693 배. 정확한 관계가 아니라는 것을 알고 쓴다 - 실험이 판정한다.
    """
    try:
        a, b = abs(float(actual)), abs(float(bar))
    except (TypeError, ValueError):
        return None
    if a <= 0 or b <= 0 or a <= b:
        return None
    return max(MIN_SCALE, round(b / a, 3))


def propose(state, metrics: dict, *, wider_dataset: str = "",
            current_dataset: str = "") -> list[Variant]:
    """계열 상태 -> 변형 후보. **막는 조항이 없으면 빈 목록.**

    `state` 는 `objective.FamilyState`. 순서가 곧 우선순위다.
    """
    from release_gate import CRITERIA
    from walk_forward import FRAGILITY_RULES

    blocked = {g.clause for g in state.gaps}
    unmeasured = {g.clause for g in state.gaps if not g.measured}
    out: list[Variant] = []

    # ── ① 표본을 넓힌다. **가장 싸고 가장 크다** ────────────────────────────
    #   2026-08-12: momentum 이 626일(v2, 350종목)로 돌았는데 v3 는 10년·3,924
    #   종목이었다. 표본만 626->2,600일로 늘리면 시도 2회에서 DSR 0.880->0.992.
    #   **엣지를 안 건드리고 통계만 좋아진다** - 다른 어떤 변형보다 먼저다.
    # ▶ **이미 그 데이터셋으로 돌았으면 넓힐 게 없다** (2026-08-12 실측)
    #   파이프라인을 돌려 보니 배분자가 `v3` 로 이미 3회 돈 계열에 계속
    #   "v3 로 넓혀라" 를 냈다. 제목이 `표본확대 … · 표본확대 …` 로 두 겹이
    #   된 가설이 4건, 낭비된 실험이 6건이었다. **넓힘은 넓어질 때만 넓힘이다.**
    _already = (current_dataset and wider_dataset
                and str(current_dataset).strip() == str(wider_dataset).strip())
    if wider_dataset and not _already and (
            unmeasured or {"deflated_sharpe", "bootstrap_ci"} & blocked
            or state.dsr is not None):
        out.append(Variant(
            kind="표본 확대", targets=["deflated_sharpe", "bootstrap_ci", "pbo"],
            dataset=wider_dataset,
            rationale=(f"같은 엣지를 더 긴 표본({wider_dataset})으로 다시 잰다. "
                       f"엣지를 안 바꾸므로 성적이 유지되면 통계만 좋아진다")))

    # ── ② 낙폭. 오늘 연 손잡이가 정확히 이걸 위한 것이다 ────────────────────
    mdd = metrics.get("max_drawdown_pct")
    bar = CRITERIA["max_drawdown_pct"]
    if "max_drawdown" in blocked and mdd is not None:
        scale = _scale_for(mdd, bar)
        if scale is not None:
            stop = round(bar / 100.0 * STOP_MARGIN, 3)     # -35% -> -0.28
            out.append(Variant(
                kind="낙폭 정지", targets=["max_drawdown", "fragility"],
                config_delta={"max_drawdown_stop": stop},
                rationale=(f"전기간 낙폭 {mdd:.2f}% 가 기준 {bar}% 를 "
                           f"{abs(mdd - bar):.2f}%p 넘는다. 허용치의 "
                           f"{STOP_MARGIN:.0%} 지점에서 전량 현금화한다")))
            av = metrics.get("ann_vol")
            if av is not None:
                out.append(Variant(
                    kind="변동성 타게팅", targets=["max_drawdown", "fragility"],
                    config_delta={"vol_target_annual": round(float(av) * scale, 4)},
                    rationale=(f"낙폭을 {scale:.3f}배로 줄이려면 노출도 그만큼. "
                               f"실현변동성 {float(av):.4f} x {scale:.3f} 를 "
                               f"목표로 둔다(낙폭~노출 1차 근사)")))

    # ── ③ 창별 취약성만 남았을 때 ──────────────────────────────────────────
    worst = metrics.get("worst_window_mdd")
    wbar = FRAGILITY_RULES["max_worst_window_mdd"]
    if "fragility" in blocked and "max_drawdown" not in blocked and worst is not None:
        s = _scale_for(worst, wbar)
        if s is not None:
            out.append(Variant(
                kind="창 낙폭 정지", targets=["fragility"],
                config_delta={"max_drawdown_stop": round(wbar * STOP_MARGIN, 3)},
                rationale=(f"최악 창 낙폭 {float(worst):.4f} 가 취약성 기준 "
                           f"{wbar} 를 넘는다. 전기간 낙폭은 이미 통과했으므로 "
                           f"창 수준만 막는다")))

    # ── ④ 회전율 ───────────────────────────────────────────────────────────
    to = metrics.get("turnover")
    tbar = CRITERIA["max_turnover"]
    if "turnover" in blocked and to is not None:
        out.append(Variant(
            kind="회전 억제", targets=["turnover"],
            config_delta={"rebalance": "MONTH_FIRST_TRADING_DAY"},
            rationale=(f"회전 {float(to):.1f}x 가 허용 {tbar}x 를 넘는다. "
                       f"비용 가정 오차가 결과를 좌우한다 - 리밸런스를 늦춘다")))

    # ── 엣지가 막는 계열에는 변형을 내지 않는다 ─────────────────────────────
    #   초과수익·정보비율이 음수면 손잡이로 못 고친다. **여기서 변형을 만들면
    #   못 고칠 것에 시도 예산과 DSR 을 태운다.**
    if {"excess_return", "information_ratio"} & blocked:
        out = [v for v in out if v.kind == "표본 확대"]
    return out


def summary(variants: list[Variant]) -> str:
    if not variants:
        return "낼 변형이 없다. (지어내지 않았다 - 손잡이로 닿는 조항이 없다)"
    lines = []
    for i, v in enumerate(variants, 1):
        what = v.dataset or ", ".join(f"{k}={val}" for k, val in v.config_delta.items())
        lines.append(f"{i}. [{v.kind}] {what}")
        lines.append(f"   노리는 조항: {', '.join(v.targets)}")
        lines.append(f"   근거: {v.rationale}")
    return "\n".join(lines)


# ── 자체 점검 ────────────────────────────────────────────────────────────────

_MOMENTUM = {                      # 2026-08-12 실측 원문 (75a6d09e)
    "excess_return_pct": 157.51, "information_ratio": 1.2552,
    "deflated_sharpe": 0.9762, "bootstrap_ci_low": -0.0029,
    "max_drawdown_pct": -50.52, "turnover": 114.87,
    "ann_vol": 0.3421, "sharpe": 1.2765, "worst_window_mdd": -0.2544,
}


def _state(metrics, *, fragility="FRAGILE", trials=1):
    from objective import evaluate_family
    return evaluate_family(metrics, fragility=fragility, trials=trials)


def _check_momentum_gets_risk_variants():
    """**막은 조항을 겨냥한 변형이 나온다.** (2026-08-12 사고 원문)

    momentum 은 알파가 있는데 낙폭에서 죽었다. 그때 아무도 이 번역을 안 해서
    리서치가 새 엣지를 설계했고, 새 엣지도 같은 자리에서 죽었다.
    """
    vs = propose(_state(_MOMENTUM), _MOMENTUM, wider_dataset="krx-basket-daily/v3")
    kinds = [v.kind for v in vs]
    assert "표본 확대" in kinds, kinds
    assert "낙폭 정지" in kinds and "변동성 타게팅" in kinds, kinds
    # **표본 확대가 먼저다** - 엣지를 안 건드리고 통계만 좋아지므로 가장 싸다
    assert kinds[0] == "표본 확대", kinds

    stop = next(v for v in vs if v.kind == "낙폭 정지")
    assert stop.config_delta == {"max_drawdown_stop": -0.28}, stop.config_delta
    vt = next(v for v in vs if v.kind == "변동성 타게팅")
    # 35/50.52 = 0.693 -> 0.3421 * 0.693
    assert abs(vt.config_delta["vol_target_annual"] - 0.2371) < 0.002, vt.config_delta

    # 손잡이는 **실제로 바인더가 받는 이름**이어야 한다. 아니면 조용히 무시된다
    from config_binding import EDGE_KEYS
    for v in vs:
        for k in v.config_delta:
            assert k in EDGE_KEYS, f"바인더가 안 받는 손잡이: {k}"
    print("  막은 조항 -> 변형        OK")


def _check_no_variants_when_edge_is_dead():
    """**엣지가 죽은 계열에 손잡이 변형을 내지 않는다.**

    초과수익 -168%p 짜리에 낙폭 정지를 걸어 봐야 여전히 진다. 시도 예산과
    DSR 만 태운다 - 시도 1->2 에 DSR 기준선이 +0.520 뛴다.
    """
    dead = dict(_MOMENTUM, excess_return_pct=-168.77, information_ratio=-1.3959,
                deflated_sharpe=0.0011)
    vs = propose(_state(dead), dead, wider_dataset="krx-basket-daily/v3")
    assert all(v.kind == "표본 확대" for v in vs), [v.kind for v in vs]
    # 표본 확대조차 없다면(넓힐 데이터가 없으면) 아무것도 내지 않는다
    assert propose(_state(dead), dead) == []
    print("  엣지 죽으면 변형 없음    OK")


def _check_values_come_from_observation():
    """**값이 관측에서 나온다.** 상수를 박으면 다른 계열에 그대로 틀린다."""
    a = dict(_MOMENTUM, max_drawdown_pct=-50.52, ann_vol=0.3421)
    b = dict(_MOMENTUM, max_drawdown_pct=-70.00, ann_vol=0.5000)
    va = next(v for v in propose(_state(a), a) if v.kind == "변동성 타게팅")
    vb = next(v for v in propose(_state(b), b) if v.kind == "변동성 타게팅")
    assert va.config_delta != vb.config_delta, "관측이 달라도 같은 값을 냈다"
    # 더 나쁜 낙폭에는 더 낮은 목표 - 방향이 맞아야 한다
    assert vb.config_delta["vol_target_annual"] < 0.5, vb.config_delta
    # 바닥이 있다 - 90% 를 현금으로 두면 그건 그 전략이 아니다
    ext = dict(_MOMENTUM, max_drawdown_pct=-95.0, ann_vol=0.30)
    ve = next(v for v in propose(_state(ext), ext) if v.kind == "변동성 타게팅")
    assert ve.config_delta["vol_target_annual"] >= 0.30 * MIN_SCALE - 1e-9
    print("  값이 관측에서 유도됨     OK")


def _check_no_widening_when_already_wide():
    """**이미 그 데이터셋이면 넓힐 게 없다.** (2026-08-12 파이프라인 실측)

    배분자가 `v3` 로 이미 3회 돈 계열에 계속 "v3 로 넓혀라" 를 냈다.
    제목이 `표본확대 … · 표본확대 …` 로 두 겹인 가설이 4건 만들어졌고
    실험 6건이 낭비됐다. **넓힘은 넓어질 때만 넓힘이다.**
    """
    st = _state(_MOMENTUM)
    # 다른 데이터셋이면 낸다
    wide = propose(st, _MOMENTUM, wider_dataset="krx-basket-daily/v3",
                   current_dataset="krx-basket-daily/v2")
    assert any(v.kind == "표본 확대" for v in wide), [v.kind for v in wide]

    # **같은 데이터셋이면 안 낸다**
    same = propose(st, _MOMENTUM, wider_dataset="krx-basket-daily/v3",
                   current_dataset="krx-basket-daily/v3")
    assert not any(v.kind == "표본 확대" for v in same), [v.kind for v in same]
    # 다만 손잡이 변형은 남아 있어야 한다 - 넓힐 게 없다고 할 일이 없는 건 아니다
    assert any(v.kind == "낙폭 정지" for v in same), [v.kind for v in same]

    # 공백·대소문자 차이로 새어 나가지 않는다
    spaced = propose(st, _MOMENTUM, wider_dataset=" krx-basket-daily/v3 ",
                     current_dataset="krx-basket-daily/v3")
    assert not any(v.kind == "표본 확대" for v in spaced)

    # 현재 데이터셋을 모르면 예전처럼 낸다 - 모르는 것과 같은 것은 다르다
    unknown = propose(st, _MOMENTUM, wider_dataset="krx-basket-daily/v3")
    assert any(v.kind == "표본 확대" for v in unknown)
    print("  이미 넓으면 안 넓힌다     OK")


def _check_clean_family_gets_nothing():
    """통과한 계열에는 변형을 내지 않는다. **할 일이 없으면 만들지 않는다.**"""
    good = {"excess_return_pct": 156.37, "information_ratio": 1.2495,
            "max_drawdown_pct": -20.0, "turnover": 100.0,
            "deflated_sharpe": 0.99, "bootstrap_ci_low": 0.2, "pbo": 0.2,
            "ann_vol": 0.2}
    st = _state(good, fragility="ROBUST")
    assert st.reached and not st.gaps
    assert propose(st, good) == []
    assert "지어내지 않았다" in summary([])
    print("  통과 계열엔 변형 없음    OK")


def _selfcheck() -> int:
    print(f"{GENERATOR_VERSION} 자체 점검 (DB 없음)")
    _check_momentum_gets_risk_variants()
    _check_no_variants_when_edge_is_dead()
    _check_values_come_from_observation()
    _check_no_widening_when_already_wide()
    _check_clean_family_gets_nothing()
    print("변형 생성기 5개 영역 통과.")
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="막힌 조항 -> 변형 후보")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--demo", action="store_true", help="momentum 실측으로 시연")
    a = ap.parse_args(argv)
    if a.check:
        return _selfcheck()
    if a.demo:
        vs = propose(_state(_MOMENTUM), _MOMENTUM,
                     wider_dataset="krx-basket-daily/v3")
        print("fam_42663e9f (momentum) 에 낼 변형:\n")
        print(summary(vs))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
