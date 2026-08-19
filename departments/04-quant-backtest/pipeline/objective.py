#!/usr/bin/env python3
"""공장의 **목표까지 남은 격차**를 계열별로 잰다.

▶ 목표는 하나다
  `release_gate` 를 통과하는 전략 계열을 **하나** 만든다(= 전략 1호).
  그것이 이 회사가 지금 하려는 일 전부다. 나머지 지표는 거기까지의 거리다.

▶ 임계를 **여기서 정하지 않는다** (가장 중요한 규칙)
  기준은 `release_gate.CRITERIA` 와 `walk_forward.FRAGILITY_RULES` 가 이미
  갖고 있다. 목적함수가 자기 임계를 따로 쓰면 **관문이 둘**이 되고, 둘은
  반드시 갈린다 - 2026-08-12 하루에 같은 것을 두 곳에서 정의해 갈라진 결함이
  열두 건 났다. 여기서는 **거리만** 잰다.

▶ 왜 필요한가 (2026-08-12 실측)
  발주는 `order by h.created_at` - 도착 순서다. 그래서 이런 일이 났다:

      계열              실험  Sharpe    DSR   초과%p
      fam_42663e9f        1   1.276  0.976   157.51   ← 최고인데 한 번 돌고 버려짐
      fam_65a4c7b6        7   0.602  0.135        -   ← 최하인데 7번

  **무엇이 목표에 가까운지 아무도 안 봤다.** 볼 재료가 없었기 때문이다.
  이 모듈이 그 재료를 만든다. 고르는 것은 배분자가 한다.

사용:
    quant-py objective.py               # 계열별 격차표
    quant-py objective.py --json
    quant-py objective.py --check
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_universe import governed_stock_evidence_sql  # noqa: E402

OBJECTIVE_VERSION = "quant-objective-v1"

# ▶ 조항을 **무엇으로 닫을 수 있는가.** 배분자가 "이 계열은 손잡이 하나 거리"
#   인지 "엣지를 바꿔야 하는 거리" 인지 가르는 데 쓴다. 둘은 비용이 완전히
#   다르다 - 앞은 재실험 1회, 뒤는 리서치 한 주기다.
#
#   `knob`   : 실행면 손잡이로 닫는다 (재실험으로 해결)
#   `sample` : 표본이 쌓이면 닫힌다 (기다리거나 데이터셋을 넓힌다)
#   `edge`   : 신호 자체가 약하다 (새 기획안이 필요하다)
CLOSABLE_BY = {
    "max_drawdown": "knob",        # 변동성 타게팅·낙폭 정지
    "turnover": "knob",            # 리밸런스 빈도·top_n
    "fragility": "knob",           # 창별 낙폭·부호 일관성이 대부분 위험관리다
    "pbo": "sample",               # 계열 변형이 4개 이상이어야 잰다
    "deflated_sharpe": "sample",   # 표본·시도 수에 달렸다
    "bootstrap_ci": "sample",
    "excess_return": "edge",       # 벤치마크를 못 이기는 건 손잡이로 못 고친다
    "information_ratio": "edge",
    "trial_pressure": "edge",      # 예산 소진 - 이 계열은 접는 게 맞다
}

_GAMMA = 0.5772156649015329        # 오일러-마스케로니


def expected_max_sharpe(trials: int) -> float:
    """알파가 0인 `trials` 번 시도에서 기대되는 **최고** Sharpe.

    `overfit_stats.deflated_sharpe` 가 쓰는 것과 **같은 식**이다. 시도를 한 번
    더 하면 이 기준선이 올라가고, 그만큼 같은 성적의 DSR 이 깎인다.
    배분자는 그 상승분을 **비용**으로 쓴다.
    """
    from overfit_stats import _norm_ppf  # noqa: PLC0415

    t = max(1, int(trials))
    if t == 1:
        return 0.0
    e1 = _norm_ppf(1.0 - 1.0 / t)
    e2 = _norm_ppf(1.0 - 1.0 / (t * math.e))
    return (1 - _GAMMA) * e1 + _GAMMA * e2


def deflation_cost(trials_now: int) -> float:
    """시도를 하나 더 했을 때 **기준선이 얼마나 올라가나**(Sharpe 단위).

    이것이 그 시도의 진짜 비용이다. 성적이 그대로여도 기준선이 올라가면
    DSR 은 떨어진다 - 같은 계열을 많이 돌릴수록 통과가 어려워진다.
    **욕심쟁이 배분은 이 시스템에서 자기 발등을 찍는다.**
    """
    return expected_max_sharpe(trials_now + 1) - expected_max_sharpe(trials_now)


@dataclass
class Gap:
    """조항 하나까지 남은 거리."""

    clause: str
    needed: float | None
    actual: float | None
    shortfall: float | None        # native 단위. None 이면 미측정
    closable: str                  # knob | sample | edge
    measured: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class FamilyState:
    """계열 하나가 목표에서 얼마나 떨어져 있나. **전부 원장에서 온 값이다.**"""

    family_id: str
    trials: int
    evidence_experiment_id: str = ""
    passed: list = field(default_factory=list)
    gaps: list = field(default_factory=list)      # Gap
    best_sharpe: float | None = None
    dsr: float | None = None
    reached: bool = False

    @property
    def n_clauses(self) -> int:
        return len(self.passed) + len(self.gaps)

    @property
    def knob_only(self) -> bool:
        """**손잡이만 돌리면 되는 계열.** 재실험 한 번 거리다.

        ▶ 미측정 조항을 빼고 세면 안 된다 (자체 점검이 이걸 잡았다)
          `if g.measured` 로 거르면, 남은 게 미측정뿐일 때 빈 목록에 `all()` 이
          True 를 돌려줘 **"손잡이 거리"로 오판**한다. 안 잰 것은 손잡이로
          닫히지 않는다 - 재는 것이 답이다.
        """
        return bool(self.gaps) and all(g.closable == "knob" for g in self.gaps)

    @property
    def rerun_closes(self) -> bool:
        """**같은 계열을 한 번 더 돌리면 닫히는가.**

        손잡이(knob)와 표본(sample)은 둘 다 재실험으로 움직인다 - 변형을 하나
        더 만들면 PBO 가 재지고(4변형 필요) 손잡이 값도 바뀐다. **한 번의
        실험이 둘을 같이 푼다.** 엣지(edge)가 섞이면 재실험으로는 안 된다.
        """
        return bool(self.gaps) and all(
            g.closable in ("knob", "sample") for g in self.gaps)

    @property
    def blocking_kinds(self) -> set:
        return {g.closable for g in self.gaps}

    def as_dict(self) -> dict:
        d = asdict(self)
        d["gaps"] = [g.as_dict() if isinstance(g, Gap) else g for g in self.gaps]
        d["knob_only"] = self.knob_only
        d["n_clauses"] = self.n_clauses
        return d


def _shortfall(clause: str, needed, actual) -> float | None:
    """모자란 양. **native 단위 그대로** 돌려준다.

    단위가 다른 것을 억지로 한 수로 합치지 않는다 - 없는 정밀도가 생긴다.
    비교가 필요하면 부르는 쪽이 조항을 골라 본다.
    """
    if needed is None or actual is None:
        return None
    return abs(float(needed) - float(actual))


def evaluate_family(metrics: dict, *, fragility: str, trials: int,
                    family_id: str = "",
                    evidence_experiment_id: str = "") -> FamilyState:
    """지표 -> 목표까지의 거리. **판정은 관문이 하고 여기서는 재기만 한다.**"""
    from release_gate import CRITERIA
    from release_gate import evaluate as gate_evaluate

    d = gate_evaluate(metrics, fragility=fragility)
    unmeasured = set(d.unmeasured or ())

    # 조항 -> (기준값, 실측값). 이름·방향은 관문이 소유한다.
    _spec = {
        "excess_return": ("excess_return_pct", CRITERIA["min_excess_return_pct"]),
        "information_ratio": ("information_ratio", CRITERIA["min_information_ratio"]),
        "max_drawdown": ("max_drawdown_pct", CRITERIA["max_drawdown_pct"]),
        "turnover": ("turnover", CRITERIA["max_turnover"]),
        "deflated_sharpe": ("deflated_sharpe", CRITERIA["min_deflated_sharpe"]),
        "pbo": ("pbo", CRITERIA["max_pbo"]),
        "bootstrap_ci": ("bootstrap_ci_low", 0.0),
    }
    gaps: list[Gap] = []
    for clause in d.failed:
        mkey, needed = _spec.get(clause, (None, None))
        actual = metrics.get(mkey) if mkey else None
        gaps.append(Gap(
            clause=clause, needed=needed, actual=actual,
            shortfall=_shortfall(clause, needed, actual),
            closable=CLOSABLE_BY.get(clause, "edge"),
            measured=clause not in unmeasured))

    return FamilyState(
        family_id=family_id, trials=int(trials),
        evidence_experiment_id=str(evidence_experiment_id or ""),
        passed=list(d.passed), gaps=gaps,
        best_sharpe=metrics.get("sharpe"),
        dsr=metrics.get("deflated_sharpe"),
        reached=(d.decision == "SUBMIT_TO_QA"))


# ── 원장에서 읽기 ────────────────────────────────────────────────────────────

_GOVERNED_PERFORMANCE_EVIDENCE = governed_stock_evidence_sql(
    experiment_alias="e", dataset_alias="manifest", hypothesis_alias="h")

_SQL_FAMILY_BEST = f"""
with pressure as (
  -- Every exposed formula still pays the multiple-testing cost.  Eligibility
  -- below chooses reusable performance evidence; it never erases a trial.
  select trial_family_id fam, max(trial_number) n_trials
    from quant.experiments
   where trial_family_id is not null
   group by trial_family_id
), eligible as (
  select e.trial_family_id fam, e.experiment_id eid, e.trial_number tn,
         max(case when m.metric='deflated_sharpe' then m.value end) dsr
    from quant.experiments e
    join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
    join quant.dataset_manifests manifest on manifest.dataset_id = e.dataset_id
    left join quant.experiment_metrics m
           on m.experiment_id = e.experiment_id
   where e.trial_family_id is not null and e.status = 'COMPLETED'
     and {_GOVERNED_PERFORMANCE_EVIDENCE}
   group by e.trial_family_id, e.experiment_id, e.trial_number),
best as (
  select eligible.fam, eligible.eid, eligible.tn, eligible.dsr,
         row_number() over (partition by fam order by dsr desc nulls last, tn desc) rk,
         -- ▶ **시도 수는 `trial_number` 의 최대값이다. 실험 행 수가 아니다.**
         --   (2026-08-12 실측) fam_42663e9f 는 실험이 2건인데 trial_number 가
         --   9였다 - `trial_family.family_ids_for` 가 동의어 계열의 옛 카드까지
         --   세기 때문이다. 행 수로 세면 배분자가 예산이 남은 줄 알고 계속
         --   배분하고, 그 사이 DSR 기준선은 9회 기준으로 깎인다.
         --   **관문이 보는 분모와 배분자가 보는 분모가 달라진다.**
         pressure.n_trials
    from eligible
    join pressure using (fam))
select fam, eid::text, n_trials from best where rk = 1
"""


def survey(conn) -> list[FamilyState]:
    """계열별 **최고 성적 기준** 목표까지의 거리. 못 읽으면 빈 목록."""
    from release_gate import SQL_GATE_METRICS, metrics_from_rows

    out: list[FamilyState] = []
    with conn.cursor() as cur:
        cur.execute(_SQL_FAMILY_BEST)
        rows = cur.fetchall()
        for fam, eid, n_trials in rows:
            cur.execute(SQL_GATE_METRICS, (eid,))
            metrics = metrics_from_rows(cur.fetchall())
            # 취약성은 창별 낙폭에서 다시 읽는다 - 관문과 같은 재료를 본다
            cur.execute("""select min(value) from quant.experiment_metrics
                            where experiment_id = %s and metric = 'max_drawdown'
                              and dimensions->>'window' is not null""", (eid,))
            got = cur.fetchone()
            worst = float(got[0]) if got and got[0] is not None else None
            frag = _fragility_from_worst(worst)
            st = evaluate_family(metrics, fragility=frag, trials=int(n_trials or 1),
                                 family_id=str(fam),
                                 evidence_experiment_id=str(eid))
            out.append(st)
    return sorted(out, key=lambda s: (-(s.dsr or 0.0), len(s.gaps)))


def _fragility_from_worst(worst_window_mdd) -> str:
    """창별 최악 낙폭 -> 취약성. **임계는 walk_forward 가 갖고 있다.**"""
    if worst_window_mdd is None:
        return ""                       # 미측정. 관문이 fail-closed 로 받는다
    from walk_forward import FRAGILITY_RULES  # noqa: PLC0415

    return ("FRAGILE" if float(worst_window_mdd)
            < FRAGILITY_RULES["max_worst_window_mdd"] else "ROBUST")


# ── 사람이 읽는 표 ───────────────────────────────────────────────────────────

def render(states: list[FamilyState]) -> str:
    if not states:
        return "계열이 없다. (지어내지 않았다 - 원장에 완주한 실험이 없다)"
    lines = ["목표: release_gate 를 통과하는 계열 하나 (= 전략 1호)", ""]
    lines.append("%-18s %5s %6s %7s  %s" % ("계열", "시도", "통과", "DSR", "남은 조항"))
    lines.append("-" * 96)
    for s in states:
        kinds = "".join(sorted({g.closable[0] for g in s.gaps}))
        remain = "; ".join(_gap_words(g) for g in s.gaps[:3])
        lines.append("%-18s %5d %3d/%-2d %7s  %s"
                     % (s.family_id[:18], s.trials, len(s.passed), s.n_clauses,
                        ("%.3f" % s.dsr) if s.dsr is not None else "-",
                        remain[:52] or "없음"))
        if s.knob_only:
            lines.append("      ▶ **손잡이만 돌리면 된다** - 재실험 한 번 거리다")
        elif kinds:
            lines.append(f"      막는 종류: {_kind_words(s.blocking_kinds)}"
                         f" | 다음 시도의 DSR 비용 {deflation_cost(s.trials):+.3f}")
    return "\n".join(lines)


def _gap_words(g: Gap) -> str:
    """조항 하나를 한 마디로. **미측정과 '거리를 못 재는 조항'을 가른다.**

    ▶ 첫 판이 `shortfall is None` 을 전부 "미측정" 으로 찍었다 (2026-08-12).
      `fragility` 는 수치 임계가 없어 shortfall 이 없을 뿐 **측정은 됐다** -
      FRAGILE 로 판정이 나 있었다. 그걸 "미측정" 으로 적으면 다음 사람이
      "재면 되겠네" 로 읽는다. 재는 것과 고치는 것은 다른 일이다.
      오늘 하루 종일 고친 `미측정 ≠ 0` 이 내 새 코드에서 다시 났다.
    """
    if not g.measured:
        return f"{g.clause}(미측정)"
    if g.shortfall is None:
        return f"{g.clause}(위반)"
    return f"{g.clause}({g.shortfall:.2f} 모자람)"


def _kind_words(kinds: set) -> str:
    w = {"knob": "손잡이", "sample": "표본", "edge": "엣지"}
    return ", ".join(w.get(k, k) for k in sorted(kinds))


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _check_thresholds_are_not_redefined():
    """**목적함수가 자기 임계를 갖지 않는다.** 관문이 둘이 되면 반드시 갈린다.

    (2026-08-12: 같은 것을 두 곳에서 정의해 갈라진 결함이 하루에 열두 건 났다.
    다른 세션 제안은 `DSR>0.95 ∧ fragility≠FRAGILE ∧ 초과>0` 3조항짜리
    목적함수였는데, 배포된 관문은 8조항이다 - 그대로 넣었으면 관문을
    우회하는 두 번째 기준이 생겼을 것이다.)
    """
    import ast  # noqa: PLC0415

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    # 모듈 최상위에 임계처럼 보이는 상수가 없어야 한다
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                name = getattr(t, "id", "")
                assert not any(k in name.lower() for k in
                               ("min_", "max_", "threshold", "criteria", "limit")), \
                    f"목적함수가 자기 임계를 만들었다: {name}"
    from release_gate import CRITERIA  # noqa: PLC0415

    # 관문의 모든 조항이 닫는 방법 표에 있어야 한다 - 새 조항이 생기면 여기서 걸린다
    covered = set(CLOSABLE_BY)
    for c in ("excess_return", "information_ratio", "max_drawdown", "turnover",
              "deflated_sharpe", "pbo", "bootstrap_ci", "fragility"):
        assert c in covered, f"{c} 를 무엇으로 닫는지 안 적혀 있다"
    assert CRITERIA["min_deflated_sharpe"] == 0.95   # 재정의가 아니라 참조 확인
    print("  임계 재정의 없음         OK")


def _check_deflation_cost_is_real():
    """**시도를 늘리면 기준선이 오른다.** 배분자가 이걸 비용으로 써야 한다."""
    assert expected_max_sharpe(1) == 0.0
    c1 = deflation_cost(1)
    c5 = deflation_cost(5)
    c20 = deflation_cost(20)
    assert c1 > 0 and c5 > 0 and c20 > 0, (c1, c5, c20)
    # 초반 한 번이 가장 비싸다 - 1회에서 2회로 갈 때 기준선이 크게 뛴다
    assert c1 > c5 > c20, (c1, c5, c20)
    # overfit_stats 와 **같은 값**이어야 한다. 다르면 배분자가 딴 세상을 본다
    from overfit_stats import _norm_ppf  # noqa: PLC0415
    e1, e2 = _norm_ppf(1 - 1 / 5), _norm_ppf(1 - 1 / (5 * math.e))
    assert abs(expected_max_sharpe(5) - ((1 - _GAMMA) * e1 + _GAMMA * e2)) < 1e-12
    print(f"  시도 비용 실재           OK (1->2: {c1:+.3f}, 20->21: {c20:+.3f})")


def _check_knob_only_is_detected():
    """**손잡이 하나 거리인 계열을 골라낸다.** (2026-08-12 momentum 원문)"""
    m = {"excess_return_pct": 157.51, "information_ratio": 1.2552,
         "deflated_sharpe": 0.9762, "bootstrap_ci_low": 0.05,
         "max_drawdown_pct": -50.52, "turnover": 114.87, "pbo": 0.3}
    s = evaluate_family(m, fragility="FRAGILE", trials=1, family_id="fam_test")
    clauses = {g.clause for g in s.gaps}
    assert clauses == {"max_drawdown", "fragility"}, clauses
    assert s.knob_only, "손잡이만으로 닫히는 계열을 못 알아봤다"
    mdd = next(g for g in s.gaps if g.clause == "max_drawdown")
    assert abs(mdd.shortfall - 15.52) < 0.01, mdd.shortfall
    assert not s.reached

    # 엣지가 약한 계열은 손잡이 거리가 아니다 - 이걸 섞으면 배분자가 헛돈다
    bad = dict(m, excess_return_pct=-168.77, information_ratio=-1.4)
    s2 = evaluate_family(bad, fragility="FRAGILE", trials=1)
    assert not s2.knob_only, "엣지가 없는 계열을 손잡이 거리로 봤다"
    assert not s2.rerun_closes, "엣지가 없는데 재실험으로 닫힌다고 봤다"
    assert "edge" in s2.blocking_kinds
    print("  손잡이 거리 판별         OK")


def _check_unmeasured_is_not_a_distance():
    """**미측정은 거리가 아니다.** 안 잰 것을 '조금 모자람' 으로 적으면 안 된다."""
    m = {"excess_return_pct": 157.51, "information_ratio": 1.26,
         "deflated_sharpe": 0.976, "bootstrap_ci_low": 0.05,
         "max_drawdown_pct": -20.0, "turnover": 100.0}      # pbo 없음
    s = evaluate_family(m, fragility="ROBUST", trials=1)
    pbo = next(g for g in s.gaps if g.clause == "pbo")
    assert pbo.shortfall is None and not pbo.measured, pbo
    assert not s.reached, "미측정인데 목표 도달로 봤다"
    # 미측정만 남았으면 손잡이 거리가 **아니다** - 재는 것이 답이다.
    # (첫 판이 `if g.measured` 로 걸러서 빈 목록에 all()=True 가 나왔다)
    assert not s.knob_only, "미측정만 남았는데 손잡이 거리로 봤다"
    # 다만 **재실험으로는 닫힌다** - 변형을 하나 더 만들면 PBO 가 재진다
    assert s.rerun_closes, "재실험으로 닫히는 것을 못 알아봤다"
    assert _gap_words(next(g for g in s.gaps if g.clause == "pbo")) == "pbo(미측정)"

    # ▶ **'거리를 못 재는 조항' 을 미측정으로 찍지 않는다.** `fragility` 는
    #   수치 임계가 없어 shortfall 이 없을 뿐 측정은 됐다 - 첫 판이 이걸
    #   "미측정" 으로 적었고, 그러면 다음 사람이 "재면 되겠네" 로 읽는다.
    m2 = {"excess_return_pct": 157.51, "information_ratio": 1.26,
          "deflated_sharpe": 0.976, "bootstrap_ci_low": 0.05,
          "max_drawdown_pct": -20.0, "turnover": 100.0, "pbo": 0.2}
    s2 = evaluate_family(m2, fragility="FRAGILE", trials=1)
    fg = next(g for g in s2.gaps if g.clause == "fragility")
    assert fg.measured and fg.shortfall is None, fg
    assert _gap_words(fg) == "fragility(위반)", _gap_words(fg)
    print("  미측정 != 거리           OK")


def _check_reached_matches_the_gate():
    """**도달 판정은 관문 것을 그대로 쓴다.** 여기서 새로 판정하지 않는다."""
    from release_gate import evaluate as gate  # noqa: PLC0415

    good = {"excess_return_pct": 156.37, "information_ratio": 1.2495,
            "max_drawdown_pct": -20.0, "turnover": 100.0,
            "deflated_sharpe": 0.99, "bootstrap_ci_low": 0.2, "pbo": 0.2}
    s = evaluate_family(good, fragility="ROBUST", trials=1)
    assert s.reached is (gate(good, fragility="ROBUST").decision == "SUBMIT_TO_QA")
    assert s.reached and not s.gaps, s.gaps
    print("  도달 판정 = 관문 판정    OK")


def _check_trials_come_from_trial_number():
    """**시도 수는 `trial_number` 의 최대값이다.** 실험 행 수로 세면 예산이 샌다.

    (2026-08-12 실측) fam_42663e9f 는 실험 2건인데 `trial_number` 가 9였다 -
    동의어 계열의 옛 카드까지 세기 때문이다. 행 수로 세면 배분자는 "시도 2회,
    예산 남음" 으로 읽고 계속 배분하는데, DSR 기준선은 9회 기준으로 깎여 있다.
    관문이 보는 분모와 배분자가 보는 분모가 갈리면 배분이 통째로 틀린다.
    """
    import re as _re

    sql = _SQL_FAMILY_BEST
    assert "max(trial_number) n_trials" in sql, \
        "시도 수를 trial_number 최대값으로 안 센다"
    assert not _re.search(r"count\(\*\)\s*over\s*\(partition by fam\)\s*n_trials", sql), \
        "실험 행 수로 세고 있다 - 예산이 샌다"
    print("  시도 수 = trial_number   OK")
    assert "join pressure using (fam)" in sql
    assert "evaluation_identity_complete" in sql and "FULL_60" in sql


def _selfcheck() -> int:
    print(f"{OBJECTIVE_VERSION} 자체 점검 (DB 없음)")
    _check_trials_come_from_trial_number()
    _check_thresholds_are_not_redefined()
    _check_deflation_cost_is_real()
    _check_knob_only_is_detected()
    _check_unmeasured_is_not_a_distance()
    _check_reached_matches_the_gate()
    print("목적함수 6개 영역 통과.")
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="목표까지 남은 격차")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return _selfcheck()

    import psycopg2

    # 다른 파이프라인 모듈과 같은 방식으로 수집기 경로를 붙인다
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                           / "01-research" / "collectors"))
    from source_registry import load_project_env
    conn = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)
    try:
        states = survey(conn)
    finally:
        conn.close()
    if a.json:
        print(json.dumps([s.as_dict() for s in states], ensure_ascii=False,
                         indent=2, default=str))
        return 0
    print(render(states))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
