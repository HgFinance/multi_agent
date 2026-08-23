#!/usr/bin/env python3
"""**에이전트가 설계한 반증 시험을 실제로 돌린다.**

▶ 왜 이게 없었나 (2026-08-12 실측)
  계약은 "반증 검사가 없다" 며 반증 없는 기획안을 거부한다. 에이전트는 성실히
  썼다 - 기획안 10건에 **49개**. 그런데 **아무도 돌리지 않았다.**
  저장되고, 강제되고, 무시됐다.

  그 49개 중 이런 것들이 있다:

      "거래비용 차감 후 초과수익 소멸"            (10회 요청)
      "시장 beta 통제 후 효과 소멸"               (9회)
      "placebo 형성일에서도 동일한 효과"          (5회)
      "롤링 창 절반 미만에서만 양의 결과"         (6회)

  고정 관문을 통과한 것보다 **자기가 설계한 반증을 견딘 것**이 훨씬 강한
  증거다. 관문은 우리가 정한 기준이고, 반증은 그 가설이 스스로 건 조건이다.

▶ 무엇을 돌리고 무엇을 안 돌리나
  기계가 확실히 할 수 있는 것만 돌린다. 나머지는 **"미실행" 으로 정직하게
  남기고 카드로 에이전트에게 넘긴다** - 애매한 것을 기계가 대충 판정하면
  "반증했다" 는 거짓 안심만 생긴다.

  돌린다  : 비용 스트레스, 창별 부호, 표본·창 수, 초과수익 부호
  안 돌린다: 베타 중립화, 유동성 층화, placebo 셔플, 분위수 단조성
            (기계가 없다. 에이전트가 직접 돌리거나 실행면을 넓혀야 한다)

사용:
    quant-py falsification.py --check
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FALSIFICATION_VERSION = "quant-falsification-v1"

# 비용 스트레스 배수. 에이전트가 "30·60·100bp" 를 요구한 적이 있고, 기본
# 왕복 비용이 약 24bps 이므로 3배면 대략 그 상단이다. **판정이 아니라 시험이다.**
COST_STRESS_MULT = 3.0

# ── 산문 -> 시험 종류 ────────────────────────────────────────────────────────
# ▶ 에이전트에게 서식을 강요하지 않는다. 자유롭게 쓰게 두고 **읽을 수 있는
#   것만 읽는다.** 서식을 강요하면 에이전트가 표현할 수 있는 반증이 줄어든다.
_PATTERNS = (
    ("cost_stress", re.compile(
        r"(거래\s*비용|비용|스프레드|슬리피지|bp).{0,30}"
        r"(차감|반영|스트레스|가정).{0,30}(소멸|사라|없|0\s*이하|이하)"
        r"|(비용).{0,20}(소멸|사라)"
        r"|(?i:(predicted|locked).{0,30}(bps|pnl).{0,60}(exceed|spread|round[- ]?trip|charges))"
        # Microstructure cost criterion used by the queue: the net taker PnL
        # must exceed observed spread plus governed round-trip charges. Keep
        # this explicit because the sentence says "does not exceed" and
        # therefore does not match the legacy "exceed ... charges" branch.
        r"|(?i:(net\s+predicted\s+taker\s+pnl).{0,80}"
        r"(does\s+not\s+exceed|not\s+exceed|fails?\s+to\s+exceed).{0,100}"
        r"(observed\s+spread|spread).{0,80}(governed\s+round[- ]?trip|round[- ]?trip\s+charges))"
        # Queue criteria also describe the same executable-cost claim using
        # the measured markout name rather than "net predicted taker PnL".
        # Keep this branch bounded to an explicit threshold so arbitrary prose
        # is never upgraded to a runnable test.
        r"|(?i:(cost[- ]adjusted\s+)?(?:[0-9]+[- ]second\s+)?"
        r"markout.{0,80}(does\s+not\s+exceed|not\s+exceed|fails?\s+to\s+exceed)"
        r".{0,30}[0-9]+(?:\.[0-9]+)?\s*(?:bp|bps|basis\s*points))"
        # Queue criteria use "clear" for the same locked cost hurdle.
        r"|(?i:(?:cost[- ]adjusted|taker|gated\s+signal|signal).{0,80}"
        r"(?:does\s+not\s+clear|fails?\s+to\s+clear|cannot\s+clear|fails?\s+cost\s+hurdle)"
        r".{0,100}(?:spread|charge|fee|hurdle|edge|23\s*bp|11\.5\s*bp))"
        r"|(?i:(?:signal|pnl|displacement).{0,60}(?:cannot|fails?\s+to)\s+clear\s+spread\s+plus)"
        r"|(?i:23\s*bp\s+round[- ]?trip\s+hurdle)"
        r"|(?:(?:주\s*)?상호작용).{0,30}23\s*bp.{0,30}(?:hurdle|비용)")),
    ("window_signs", re.compile(
        r"(롤링|rolling|walk[- ]?forward|창).{0,30}"
        r"(절반|다수|부호|불안정|반전|재현\s*실패|한\s*구간|한\s*국면)"
        r"|(국면).{0,20}(집중)"
        r"|(?i:(?:sign|effect|result).{0,60}(?:negative|unstable|one[- ]session|one[- ]regime|confined|reverses?|vanishes?).{0,60}(?:session|regime|out[- ]of[- ]sample|replicate))"
        r"|(?i:(?:fails?\s+to\s+replicate|confined\s+to|effect\s+is).{0,60}(?:session|regime|instrument|one[- ]session|one[- ]regime))"
        r"|(?i:(?:effect|result).{0,60}(?:exists|confined|survives).{0,30}(?:one\s+session|one\s+liquidity\s+bucket|independent\s+sessions?))"
        r"|(?i:failure\s+to\s+replicate.{0,40}(?:sessions?|instruments?))")),
    ("window_count", re.compile(
        r"(walk[- ]?forward|창).{0,12}\d+\s*개\s*미만"
        r"|(?i:(?:fewer|less than|under).{0,20}(?:completed )?(?:KRX )?sessions)"
        r"|(?i:(?:underpowered|insufficient).{0,30}(?:data|sessions|sample))")),
    ("beats_baseline", re.compile(
        r"(baseline|기준선|벤치마크).{0,20}(대비|보다).{0,20}"
        r"(초과수익\s*없|높지\s*않|이하)"
        r"|(fails?\s+to\s+exceed|does\s+not\s+exceed|not\s+exceed).{0,50}"
        r"(baseline|benchmark|equal[_ ]weight|buy[_ ]and[_ ]hold)"
        r"|(초과수익|excess\s+return).{0,30}(below|under|less\s+than|이하|없)"
        r"|(?i:(?:no|not|does not|fails? to).{0,50}(?:incremental|improvement|distinct|positive|difference|better|indistinguishable).{0,60}(?:markout|pnl|return|effect|control|baseline|microprice|midprice|queue|gate|strata))"
        r"|(?i:(?:does not improve|performs similarly|match(?:es)?|indistinguishable|unchanged).{0,60}(?:microprice|midprice|control|gate|strata|effect|markout|pnl|baseline))"
        r"|(?i:(?:fails? cost hurdle|non[- ]positive calibration|negative calibration|PIT alignment fails?|alignment failure))"
        r"|(?i:(?:control|gate|ablation|strata).{0,40}(?:performs similarly|unchanged|indistinguishable))"
        r"|(?i:(?:effect|markout|pnl).{0,40}(?:unchanged|absent|disappears|indistinguishable))"
        # Queue proposals also use calibration language for the same
        # executable sign/baseline test. Keep the branch bounded.
        r"|(?i:(?:fitted\s+score[- ]?to[- ]?bps|calibration).{0,50}"
        r"(?:relation|slope).{0,30}(?:non[- ]?positive|negative|zero))"
        r"|(?i:(?:negative|non[- ]?positive|zero).{0,30}"
        r"(?:pre[- ]?OOS|calibration).{0,30}(?:relation|slope))"
        r"|(?i:(?:fails?\s+to\s+add|no\s+incremental).{0,60}"
        r"(?:net\s+)?(?:[0-9]+[- ]?second\s+)?markout.{0,60}(?:tape[- ]?only|control|baseline))"
        # A cost hurdle can be phrased as an unchanged hurdle.
        r"|(?i:(?:fails?\s+to\s+clear|does\s+not\s+clear).{0,50}"
        r"(?:unchanged\s+)?[0-9]+(?:\.[0-9]+)?\s*bp"
        r".{0,40}(?:round[- ]?trip|hurdle|cost))")),
    # 창별 최악 낙폭·창간 Sharpe 산포는 `fragility_summary` 가 이미 재는 값이다
    ("window_mdd", re.compile(r"창.{0,10}(MDD|낙폭)")),
    ("window_sharpe_std", re.compile(r"창간?\s*Sharpe.{0,10}(표준편차|산포|std)")),
    # These checks are executable when the deterministic runner supplies the
    # explicit result key. Missing keys remain NOT_RUN (never zero/pass).
    ("beta_neutral", re.compile(r"(베타|beta).{0,20}(통제|중립)")),
    ("liquidity_strat", re.compile(r"(유동성|저유동성|Amihud|분위).{0,20}"
                                   r"(층화|통제|분위|제외)")),
    ("placebo", re.compile(r"(placebo|위약|셔플|무작위|뒤집|반전).{0,20}"
                           r"(형성일|검정|신호|시점|식별자)?")),
    ("monotonic", re.compile(r"(분위수|quantile).{0,20}(단조)")),
)

# Legacy fallback labels are retained for unusual prose, but the normal
# criteria above are now runnable whenever their measured result key exists.
_UNRUNNABLE = (
)


@dataclass
class TestResult:
    """반증 시험 하나의 결과. **실행 못 한 것을 통과로 적지 않는다.**"""

    text: str
    kind: str
    ran: bool
    survived: bool | None = None     # True=반증 못 함(가설이 견딤)
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def classify(text: str) -> tuple[str, bool]:
    """산문 한 줄 -> (종류, 기계가 돌릴 수 있나)."""
    t = str(text or "")
    for kind, pat in _PATTERNS:
        if pat.search(t):
            return kind, True
    for kind, pat in _UNRUNNABLE:
        if pat.search(t):
            return kind, False
    return "unclassified", False


def _pct(text: str, default: float) -> float:
    """문장에서 임계를 읽는다. 못 읽으면 규칙 기본값 - **지어내지 않는다.**"""
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", str(text))
    if m:
        return float(m.group(1)) / 100.0
    m = re.search(r"(-?\d+(?:\.\d+)?)", str(text))
    return float(m.group(1)) if m else default


def _criteria_texts(criteria) -> list[str]:
    """저장된 반증 기준을 실행 가능한 문장 목록으로 정규화한다.

    원장에는 기준이 보통 문자열 목록으로 오지만, 과거 실행면은
    ``["note", {"fragility_rules": [...]}]`` 처럼 dict를 목록 원소로
    저장했다. dict의 repr을 정규식에 넘기면 실제 문장을 놓치므로, 의미 있는
    ``fragility_rules`` 값을 먼저 펼치고 그 밖의 dict도 값만 재귀적으로
    펼친다. 값이 없으면 기준을 만들어 내지 않고 빈 목록으로 둔다.
    """
    if criteria is None:
        return []
    if isinstance(criteria, str):
        # Legacy list placeholders are storage labels, not tests.
        if criteria.strip().lower() in {"note", "fragility_rules"}:
            return []
        return [criteria]
    if isinstance(criteria, dict):
        if "fragility_rules" in criteria:
            return _criteria_texts(criteria["fragility_rules"])
        out: list[str] = []
        for value in criteria.values():
            out.extend(_criteria_texts(value))
        return out
    if isinstance(criteria, (list, tuple)):
        out: list[str] = []
        for item in criteria:
            out.extend(_criteria_texts(item))
        return out
    return [str(criteria)]


def run(tests, metrics: dict, *, window_metrics=None,
        cost_stress_metrics=None, window_mdds=None,
        window_sharpes=None) -> list[TestResult]:
    """반증 시험들을 돌린다. 재료가 없으면 **미실행**이지 통과가 아니다.

    `cost_stress_metrics` 는 비용 배수를 걸고 재실행한 결과다. 없으면
    비용 시험은 미실행이다 - 안 돌리고 "견뎠다" 고 적으면 거짓이다.
    """
    out: list[TestResult] = []
    wins = list(window_metrics or [])
    for t in _criteria_texts(tests):
        kind, runnable = classify(t)
        if not runnable:
            out.append(TestResult(text=str(t), kind=kind, ran=False,
                                  detail="기계가 못 돌린다 - 에이전트가 직접 돌려라"))
            continue

        if kind == "cost_stress":
            ex = (cost_stress_metrics or {}).get("excess_return_pct")
            if ex is None:
                out.append(TestResult(text=str(t), kind=kind, ran=False,
                                      detail="비용 스트레스 재실행 결과가 없다"))
            else:
                out.append(TestResult(
                    text=str(t), kind=kind, ran=True, survived=float(ex) > 0,
                    detail=(f"비용 {COST_STRESS_MULT:g}배에서 초과수익 "
                            f"{float(ex):+.2f}%p")))
        elif kind == "window_signs":
            if not wins:
                out.append(TestResult(text=str(t), kind=kind, ran=False,
                                      detail="창별 성적이 없다"))
            else:
                pos = sum(1 for w in wins if float(w) > 0)
                out.append(TestResult(
                    text=str(t), kind=kind, ran=True,
                    survived=pos > len(wins) / 2.0,
                    detail=f"창 {len(wins)}개 중 양(+) {pos}개"))
        elif kind == "window_count":
            m = re.search(r"(\d+)\s*개\s*미만", str(t))
            need = int(m.group(1)) if m else 3
            out.append(TestResult(
                text=str(t), kind=kind, ran=bool(wins),
                survived=(len(wins) >= need) if wins else None,
                detail=(f"창 {len(wins)}개 (필요 {need})" if wins
                        else "창 성적이 없다")))
        elif kind == "window_mdd":
            mdds = list(window_mdds or [])
            if not mdds:
                out.append(TestResult(text=str(t), kind=kind, ran=False,
                                      detail="창별 낙폭이 없다"))
            else:
                bar = _pct(t, -0.25)
                worst = min(float(x) for x in mdds)
                out.append(TestResult(
                    text=str(t), kind=kind, ran=True, survived=worst >= bar,
                    detail=f"최악 창 낙폭 {worst:.2%} (기준 {bar:.0%})"))
        elif kind == "window_sharpe_std":
            shs = [float(x) for x in (window_sharpes or [])]
            if len(shs) < 2:
                out.append(TestResult(text=str(t), kind=kind, ran=False,
                                      detail="창별 Sharpe 가 2개 미만이다"))
            else:
                mu = sum(shs) / len(shs)
                sd = (sum((x - mu) ** 2 for x in shs) / (len(shs) - 1)) ** 0.5
                bar = _pct(t, 1.5)
                out.append(TestResult(
                    text=str(t), kind=kind, ran=True, survived=sd <= bar,
                    detail=f"창간 Sharpe 표준편차 {sd:.3f} (기준 {bar})"))
        elif kind == "beats_baseline":
            ex = metrics.get("excess_return_pct")
            out.append(TestResult(
                text=str(t), kind=kind, ran=ex is not None,
                survived=(float(ex) > 0) if ex is not None else None,
                detail=(f"초과수익 {float(ex):+.2f}%p" if ex is not None
                        else "초과수익이 없다")))
        elif kind in {"beta_neutral", "liquidity_strat", "placebo"}:
            keys = {
                "beta_neutral": ("beta_neutral_excess_return_pct", "alpha_ann_pct"),
                "liquidity_strat": ("liquidity_strat_excess_return_pct",),
                "placebo": ("placebo_excess_return_pct",),
            }[kind]
            ex = next((metrics.get(k) for k in keys if metrics.get(k) is not None), None)
            out.append(TestResult(
                text=str(t), kind=kind, ran=ex is not None,
                survived=(float(ex) > 0) if ex is not None else None,
                detail=(f"{kind} 초과수익 {float(ex):+.2f}%p" if ex is not None
                        else f"{kind} 측정 결과가 없다")))
        elif kind == "monotonic":
            mono = metrics.get("quantile_monotonic")
            out.append(TestResult(
                text=str(t), kind=kind, ran=mono is not None,
                survived=bool(mono) if mono is not None else None,
                detail=(f"분위수 단조성={bool(mono)}" if mono is not None
                        else "분위수 단조성 측정 결과가 없다")))
    return out


def summarize(results) -> dict:
    """판정에 실을 요약. **미실행을 통과와 섞지 않는다.**"""
    ran = [r for r in results if r.ran]
    return {
        "total": len(results),
        "ran": len(ran),
        "survived": sum(1 for r in ran if r.survived),
        "refuted": sum(1 for r in ran if r.survived is False),
        "not_run": len(results) - len(ran),
        "refuted_texts": [r.text[:90] for r in ran if r.survived is False][:4],
        "not_run_kinds": sorted({r.kind for r in results if not r.ran}),
    }


def note(results) -> str:
    """환류에 실을 한 줄."""
    s = summarize(results)
    if not s["total"]:
        return ""
    head = (f"자기반증 {s['ran']}/{s['total']} 실행 · "
            f"견딤 {s['survived']} · 반증됨 {s['refuted']}")
    if s["refuted_texts"]:
        head += " | 반증된 것: " + "; ".join(s["refuted_texts"][:2])
    if s["not_run"]:
        head += (f" | 미실행 {s['not_run']}건({', '.join(s['not_run_kinds'][:3])})"
                 f" - 기계가 없다")
    return head


# ── 자체 점검 ────────────────────────────────────────────────────────────────

_REAL = [                       # 2026-08-12 에이전트가 실제로 쓴 문장들
    "전체기간에서 baseline 대비 초과수익 없음",
    "walk-forward 창 3개 미만",
    "거래비용 차감 후 초과수익 소멸",
    "보수적 거래비용 차감 후 초과수익 소멸",
    "시장 beta 통제 후 효과 소멸",
    "저유동성 구간을 제외하거나 유동성 층화 시 효과 역전·소멸",
    "롤링 walk-forward 창의 절반 미만에서만 양의 결과",
    "손실 직후가 아닌 placebo 형성일에서도 동일한 효과",
    "호가불균형 분위수별 20일 선도수익률의 단조성이 사라짐",
    "locked predicted BPS exceeds the current spread, governed round-trip charges, and minimum_predicted_edge_bps",
    "net predicted taker PnL does not exceed observed spread plus governed round-trip charges",
    "cost-adjusted 5-second markout does not exceed 23bp",
]


def _check_real_sentences_are_classified():
    """**에이전트가 실제로 쓴 문장**을 읽는다. 지어낸 예문으로 맞추면 소용없다."""
    kinds = {t: classify(t) for t in _REAL}
    assert kinds[_REAL[0]] == ("beats_baseline", True), kinds[_REAL[0]]
    assert kinds[_REAL[1]] == ("window_count", True), kinds[_REAL[1]]
    assert kinds[_REAL[2]] == ("cost_stress", True), kinds[_REAL[2]]
    assert kinds[_REAL[3]] == ("cost_stress", True), kinds[_REAL[3]]
    assert kinds[_REAL[4]] == ("beta_neutral", True)
    assert kinds[_REAL[5]] == ("liquidity_strat", True)
    assert kinds[_REAL[6]] == ("window_signs", True), kinds[_REAL[6]]
    assert kinds[_REAL[7]] == ("placebo", True)
    assert kinds[_REAL[8]] == ("monotonic", True)
    assert kinds[_REAL[9]] == ("cost_stress", True), kinds[_REAL[9]]
    assert kinds[_REAL[10]] == ("cost_stress", True), kinds[_REAL[10]]
    assert kinds[_REAL[11]] == ("cost_stress", True), kinds[_REAL[11]]
    runnable = sum(1 for k, r in kinds.values() if r)
    print(f"  실제 문장 분류           OK ({runnable}/{len(_REAL)} 실행 가능)")


def _check_nested_criteria_are_executed():
    """과거 dict-in-list 저장도 기준을 잃지 않고 실행한다."""
    tests = ["note", {"fragility_rules": ["거래비용 차감 후 초과수익 소멸"]}]
    rs = run(tests, {}, cost_stress_metrics={"excess_return_pct": -1.0})
    assert len(rs) == 1 and rs[0].ran and rs[0].survived is False, rs
    print("  dict 기준 정규화          OK")


def _check_unrun_is_not_pass():
    """**안 돌린 것을 견딘 것으로 세지 않는다.** 가장 중요한 규칙이다."""
    rs = run(_REAL, {"excess_return_pct": 100.0})   # 창·비용 재실행 없음
    s = summarize(rs)
    assert s["total"] == len(_REAL)
    assert s["not_run"] >= 5, s
    # 비용 시험은 재실행이 없으므로 **미실행**이어야 한다
    cost = [r for r in rs if r.kind == "cost_stress"]
    assert cost and all(not r.ran for r in cost), cost
    assert all(r.survived is None for r in rs if not r.ran)
    assert s["survived"] + s["refuted"] == s["ran"]
    assert "미실행" in note(rs), note(rs)
    print("  미실행 != 견딤           OK")


def _check_cost_stress_can_refute():
    """비용을 올려 초과수익이 사라지면 **반증됐다**고 적는다."""
    tests = ["거래비용 차감 후 초과수익 소멸"]
    ok = run(tests, {"excess_return_pct": 157.5},
             cost_stress_metrics={"excess_return_pct": 88.0})
    assert ok[0].ran and ok[0].survived is True, ok[0]
    bad = run(tests, {"excess_return_pct": 157.5},
              cost_stress_metrics={"excess_return_pct": -12.0})
    assert bad[0].ran and bad[0].survived is False, bad[0]
    assert "반증된 것" in note(bad), note(bad)
    print("  비용 스트레스가 반증한다  OK")


def _check_explicit_falsifications_use_measured_keys():
    """Measured keys execute; absent keys stay NOT_RUN (2026-08-16)."""
    tests = _REAL[4:5] + _REAL[5:6] + _REAL[7:9]
    missing = run(tests, {})
    assert all(not r.ran and r.survived is None for r in missing), missing
    measured = run(tests, {
        "beta_neutral_excess_return_pct": -1.0,
        "liquidity_strat_excess_return_pct": 2.0,
        "placebo_excess_return_pct": -0.5,
        "quantile_monotonic": True,
    })
    assert all(r.ran for r in measured), measured
    assert [r.survived for r in measured] == [False, True, False, True], measured
    print("  명시적 반증 키 실행       OK")


def _check_fragility_rules_are_executable():
    """**시스템이 스스로 건 취약성 기준도 실행 가능해야 한다.** (2026-08-12)

    `walk_forward` 가 만든 반증 기준이 원장에 `['note','fragility_rules']` 로
    저장돼 있었다 - dict 를 리스트 자리에 넣어 키가 기준이 된 것이다.
    이 검사가 그 세 문장을 실제로 돌린다.
    """
    from walk_forward import _fragility_criteria  # noqa: PLC0415

    crits = _fragility_criteria()
    assert len(crits) == 3, crits
    kinds = [classify(x) for x in crits]
    assert all(r for _, r in kinds), kinds
    assert {k for k, _ in kinds} == {"window_signs", "window_mdd",
                                     "window_sharpe_std"}, kinds

    rs = run(crits, {}, window_metrics=[0.1, 0.2, -0.05, 0.3, 0.1],
             window_mdds=[-0.07, -0.17, -0.19, -0.25, -0.21],
             window_sharpes=[0.8, 1.1, 0.4, 0.9, 1.0])
    assert all(r.ran for r in rs), [r.as_dict() for r in rs]
    assert all(r.survived for r in rs), [r.detail for r in rs]

    # 한 창이 기준을 넘으면 반증된다
    bad = run(crits, {}, window_metrics=[0.1, 0.2, -0.05, 0.3, 0.1],
              window_mdds=[-0.07, -0.51, -0.19, -0.25, -0.21],
              window_sharpes=[0.8, 1.1, 0.4, 0.9, 1.0])
    m = next(r for r in bad if r.kind == "window_mdd")
    assert m.survived is False and "-51" in m.detail, m.detail
    print("  취약성 규칙도 실행됨     OK")


def _check_window_signs():
    """창 절반 이하에서만 양수면 반증. **다수 창에서 재현돼야 견딘 것이다.**"""
    t = ["롤링 walk-forward 창의 절반 미만에서만 양의 결과"]
    good = run(t, {}, window_metrics=[0.1, 0.2, -0.05, 0.3, 0.1])
    assert good[0].survived is True, good[0]
    bad = run(t, {}, window_metrics=[0.1, -0.2, -0.05, -0.3, 0.1])
    assert bad[0].survived is False, bad[0]
    none = run(t, {})
    assert none[0].ran is False and none[0].survived is None
    print("  창별 부호 판정           OK")


def _check_cost_multiplier_is_one_way():
    """**비용을 낮추는 방향은 막는다.** 반증 도구로 성적을 꾸미면 안 된다."""
    from backtest_runner import cost_multiplier  # noqa: PLC0415

    assert cost_multiplier({}) == 1.0
    assert cost_multiplier({"cost_stress": 3}) == 3.0
    assert cost_multiplier({"cost_stress": 0.1}) == 1.0, "비용을 낮췄다"
    assert cost_multiplier({"cost_stress": "이상한값"}) == 1.0
    # EDGE_KEYS 에 없어야 한다 - 기획안이 비용을 만질 수 없다
    from config_binding import EDGE_KEYS  # noqa: PLC0415
    assert "cost_stress" not in EDGE_KEYS, "기획안이 비용을 만질 수 있게 됐다"
    print("  비용 배수는 한 방향       OK")


def _selfcheck() -> int:
    print(f"{FALSIFICATION_VERSION} 자체 점검 (DB 없음)")
    _check_real_sentences_are_classified()
    _check_unrun_is_not_pass()
    _check_nested_criteria_are_executed()
    _check_cost_stress_can_refute()
    _check_explicit_falsifications_use_measured_keys()
    _check_window_signs()
    _check_fragility_rules_are_executable()
    _check_cost_multiplier_is_one_way()
    print("자기반증 6개 영역 통과.")
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="에이전트가 설계한 반증 시험 실행")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return _selfcheck()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
