#!/usr/bin/env python3
"""**다음에 무엇을 돌릴 것인가**를 고른다. 도착 순서가 아니라 목표까지의 거리로.

▶ 무엇이 문제였나 (2026-08-12 실측)
  발주는 `order by h.created_at` - 문자 그대로 도착 순서였다. 결과:

      계열              실험  Sharpe    DSR    돌린 데이터
      fam_42663e9f        1   1.276  0.976    v2 (350종목·2년·626일)   ← 최고
      fam_65a4c7b6        7   0.602  0.135    -                        ← 최하인데 7번
      fam_24aa2641        1  -0.958  0.001    v3 (3,924종목·10년)      ← 최악인데 최고 데이터

  **최고 전략이 최악의 데이터로 돌았고, 최악 전략이 최고의 데이터를 썼다.**
  무엇이 유망한지 아무도 안 봤기 때문이다.

▶ 욕심쟁이는 이 시스템에서 자기 발등을 찍는다
  같은 계열을 한 번 더 돌리면 DSR 기준선이 오른다(시도 1->2 에 +0.520).
  성적이 그대로여도 DSR 이 0.976 -> 0.880 으로 떨어져 관문을 못 넘는다.
  **점수만 보고 1등 계열에 예산을 다 쓰면 그 계열을 스스로 죽인다.**

  그래서 이 배분자는 "다음 시도 뒤의 DSR" 을 미리 계산해 **자멸적인 수를
  걸러낸다.** 예외가 하나 있다 - 표본을 넓히는 수는 표본이 커지는 만큼
  z 가 올라가므로 기준선 상승을 이긴다. 실측으로 626->2,600일이면 시도
  2회에서 DSR 0.880 -> 0.992 다. **그게 지금 유일하게 공짜인 수다.**

사용:
    quant-py allocator.py               # 다음 수 순위
    quant-py allocator.py --json
    quant-py allocator.py --check
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ALLOCATOR_VERSION = "quant-allocator-v1"

# 시도 예산. `trial_family.pressure` 가 쓰는 것과 같은 뜻이다 - 이 수를 넘겨
# 고른 최고치는 실력과 운을 못 가린다.
DEFAULT_TRIAL_BUDGET = 5


@dataclass
class Move:
    """다음에 돌릴 한 수. **점수의 근거를 반드시 들고 다닌다.**"""

    family_id: str
    kind: str
    targets: list = field(default_factory=list)
    config_delta: dict = field(default_factory=dict)
    dataset: str = ""
    trials_now: int = 0
    dsr_now: float | None = None
    dsr_after: float | None = None     # 이 수를 둔 뒤의 예상 DSR
    self_defeating: bool = False       # 두면 DSR 이 관문 아래로 떨어지나
    score: float = 0.0
    why: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def projected_dsr(metrics: dict, *, trials_after: int,
                  n_days_after: int | None = None) -> float | None:
    """이 수를 둔 뒤의 DSR. **`overfit_stats` 와 같은 식을 쓴다.**

    성적(Sharpe·왜도·첨도)이 유지된다는 가정이다 - 낙관도 비관도 아니고,
    "표본과 시도만 달라졌을 때" 의 산수다. 실제 성적은 실험이 정한다.
    """
    from objective import expected_max_sharpe
    from overfit_stats import TRADING_DAYS, _norm_cdf

    sr = metrics.get("sharpe")
    skew = metrics.get("skew")
    kurt = metrics.get("kurtosis")
    n = n_days_after or metrics.get("excess_days_used")
    if sr is None or skew is None or kurt is None or not n or int(n) < 2:
        return None                      # 못 재면 안 적는다
    sr_p = float(sr) / math.sqrt(TRADING_DAYS)
    denom = 1.0 - float(skew) * sr_p + (float(kurt) - 1.0) / 4.0 * sr_p ** 2
    if denom <= 0:
        return None
    sr0 = expected_max_sharpe(int(trials_after)) / math.sqrt(TRADING_DAYS)
    z = (sr_p - sr0) * math.sqrt(int(n) - 1) / math.sqrt(denom)
    return round(_norm_cdf(z), 4)


# ▶ 무익 중단 문턱. I-SPY 2 는 "확증시험 성공 예측확률 10% 미만이면 그 팔을
#   중단" 한다. 우리도 같은 자리가 필요하다 - 지금은 **포기 규칙이 아예 없어서**
#   `fam_65a4c7b6` 가 성적 한 번 못 내고 7번 돌았다(예산 5 초과).
#
#   우리 판 예측확률: "다음 시도가 관문을 넘을 수 있나" 를 **지금 알고 있는
#   것만으로** 본다. 낙관하지 않는다 - 성적이 그대로라는 가정이다.
FUTILITY_DSR = 0.10          # 다음 시도 뒤 예상 DSR 이 이보다 낮으면 무익
FUTILITY_EXCESS_PCT = -20.0  # 벤치마크에 이만큼 지면 손잡이로 못 뒤집는다


def futility(state, metrics: dict, *, budget: int = DEFAULT_TRIAL_BUDGET,
             trials_after: int | None = None) -> str:
    """**이 계열을 접어야 하나.** 접을 사유 문자열, 아니면 빈 문자열.

    ▶ 왜 필요한가 (2026-08-12 실측)
      배분자는 점수만 매기고 "그만해라" 를 말하지 않았다. 그래서 성적을 한 번도
      못 낸 계열이 7번 돌았고, 그 7번이 다른 계열의 DSR 예산과 실행 시간을
      먹었다. **포기 규칙이 없는 탐색은 자원을 태우는 것과 같다.**

      임계는 우리가 새로 만들지 않는다 - I-SPY 2 의 10% 무익 중단을 그대로
      옮기고, 초과수익 하한만 우리 관문(-10%p 여유의 두 배)에서 유도한다.
    """
    if state.trials >= budget:
        return f"시도 예산 {budget}회 소진 - 더 돌려도 증거가 안 된다"

    ex = metrics.get("excess_return_pct")
    if ex is not None and float(ex) <= FUTILITY_EXCESS_PCT:
        return (f"초과수익 {float(ex):+.1f}%p - 벤치마크에 크게 지고 있다. "
                f"손잡이로는 못 뒤집는다(엣지 문제)")

    nxt = int(trials_after if trials_after is not None else state.trials + 1)
    proj = projected_dsr(metrics, trials_after=nxt)
    if proj is not None and proj < FUTILITY_DSR:
        return (f"다음 시도 뒤 예상 DSR {proj:.3f} < {FUTILITY_DSR} - "
                f"이 계열은 시도를 더 해도 우연과 구분되지 않는다")
    return ""


def score_move(state, metrics: dict, variant, *,
               budget: int = DEFAULT_TRIAL_BUDGET,
               wider_days: int | None = None) -> Move:
    """변형 하나에 점수를 매긴다. **자멸적인 수는 점수가 아니라 표시로 거른다.**"""
    from release_gate import CRITERIA

    blocked = {g.clause for g in state.gaps}
    closes = [t for t in variant.targets if t in blocked]
    trials_after = state.trials + 1
    n_after = wider_days if variant.dataset else None
    dsr_after = projected_dsr(metrics, trials_after=trials_after,
                              n_days_after=n_after)
    bar = CRITERIA["min_deflated_sharpe"]
    # DSR 이 이미 안 재지는 계열은 자멸 판정을 못 한다 - 모른다고 적는다
    defeating = bool(dsr_after is not None and dsr_after < bar
                     and (state.dsr or 0) >= bar)

    # ── 점수 ─────────────────────────────────────────────────────────────
    #   ① 닫는 조항 수 - 많이 닫을수록 목표에 가깝다
    #   ② 남은 조항이 적은 계열 우선 - 5/8 통과가 0/8 보다 가깝다
    #   ③ 자멸적이면 크게 깎는다. 0 으로 만들지는 않는다 - 다른 수가 전부
    #      자멸적이면 그중 덜 나쁜 것을 알아야 한다
    #   ④ 예산이 없으면 0. 예산을 넘겨 고른 최고치는 증거가 아니다
    # ▶ **무익하면 점수가 0이다.** 표본 확대라도 예외가 아니다 - 벤치마크에
    #   크게 지는 계열을 더 넓은 표본으로 돌려 봐야 더 크게 질 뿐이다.
    stop = futility(state, metrics, budget=budget, trials_after=trials_after)
    if stop:
        return Move(family_id=state.family_id, kind=variant.kind,
                    targets=list(variant.targets),
                    config_delta=dict(variant.config_delta),
                    dataset=variant.dataset, trials_now=state.trials,
                    dsr_now=state.dsr, dsr_after=dsr_after, score=0.0,
                    why=f"무익 중단: {stop}")

    progress = len(state.passed) / max(1, state.n_clauses)
    score = (len(closes) + 1.0) * (0.5 + progress)
    if defeating:
        score *= 0.15
    if not closes:
        score *= 0.5                    # 막힌 조항을 안 건드리는 수는 뒤로

    # ▶ **엣지가 죽은 계열은 뒤로 보낸다** (2026-08-12)
    #   표본을 넓히면 표본 부족 교훈은 해소된다 - 그래서 생성기가 그 수를
    #   남겨 둔다. 그런데 벤치마크에 168%p 지는 계열은 표본을 늘려도 진다.
    #   조항 수만 세면 그런 계열이 살아 있는 계열과 같은 줄에 선다.
    #   **예산은 유한하다.** 진 계열보다 이긴 계열을 먼저 본다.
    ex = metrics.get("excess_return_pct")
    if ex is not None and float(ex) < 0:
        score *= 0.25 if float(ex) > -50 else 0.1
        why_edge = f" | 초과 {float(ex):+.1f}%p - 엣지가 지고 있다"
    else:
        why_edge = ""

    why = variant.rationale + why_edge
    if defeating:
        why += (f" | ⚠ 이 수를 두면 DSR {state.dsr:.3f} -> {dsr_after:.3f} 로 "
                f"관문({bar}) 아래로 떨어진다 - 계열을 스스로 죽인다")
    elif dsr_after is not None and state.dsr is not None:
        why += f" | DSR {state.dsr:.3f} -> {dsr_after:.3f} 예상"

    return Move(family_id=state.family_id, kind=variant.kind,
                targets=list(variant.targets),
                config_delta=dict(variant.config_delta),
                dataset=variant.dataset, trials_now=state.trials,
                dsr_now=state.dsr, dsr_after=dsr_after,
                self_defeating=defeating, score=round(score, 4), why=why)


def plan(conn, *, budget: int = DEFAULT_TRIAL_BUDGET,
         wider_dataset: str = "", wider_days: int | None = None) -> list[Move]:
    """원장 -> 다음 수 순위. 못 읽으면 빈 목록(지어내지 않는다)."""
    from objective import survey
    from release_gate import SQL_GATE_METRICS, metrics_from_rows
    from variant_generator import propose

    moves: list[Move] = []
    states = survey(conn)
    with conn.cursor() as cur:
        for st in states:
            if st.reached:
                continue                 # 이미 도달한 계열은 더 안 돌린다
            cur.execute("""select experiment_id::text from quant.experiments
                            where trial_family_id = %s and status='COMPLETED'
                            order by trial_number desc limit 1""", (st.family_id,))
            got = cur.fetchone()
            if not got:
                continue
            cur.execute(SQL_GATE_METRICS, (got[0],))
            metrics = metrics_from_rows(cur.fetchall())
            cur.execute("""select min(value) from quant.experiment_metrics
                            where experiment_id = %s and metric='max_drawdown'
                              and dimensions->>'window' is not null""", (got[0],))
            w = cur.fetchone()
            if w and w[0] is not None:
                metrics["worst_window_mdd"] = float(w[0])
            # **이 계열이 지금 무슨 데이터셋을 쓰고 있나** - 같은 것으로
            # 넓히자고 하면 낭비다(실측: 그 낭비가 6건이었다)
            cur.execute("""select m.name||'/'||m.version
                             from quant.experiments e
                             join quant.dataset_manifests m
                               on m.dataset_id = e.dataset_id
                            where e.trial_family_id = %s
                            order by e.trial_number desc limit 1""",
                        (st.family_id,))
            _ds = cur.fetchone()
            current_ds = str(_ds[0]) if _ds and _ds[0] else ""
            for v in propose(st, metrics, wider_dataset=wider_dataset,
                             current_dataset=current_ds):
                moves.append(score_move(st, metrics, v, budget=budget,
                                        wider_days=wider_days))
    return sorted(moves, key=lambda m: (-m.score, m.family_id))


_SQL_PARENT = """
select h.hypothesis_id::text, h.title, h.rationale, h.expected_edge,
       h.falsification_criteria, h.required_data_products,
       h.proposal_id, h.lead_ids, h.research_packet_ids, h.claim_ids,
       h.economic_rationale, h.counterparty, h.competing_explanation,
       h.competing_explanation_codes, h.skeptic_sign, h.source_reported_effect
  from quant.experiments e
  join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
 where e.trial_family_id = %s
 order by e.trial_number desc limit 1
"""

_SQL_VARIANT_EXISTS = """
select hypothesis_id::text from quant.hypotheses
 where created_by = %s and title = %s
   and status in ('PROPOSED','PREREGISTERED','RUNNING','TESTING')
 limit 1
"""

_SQL_INSERT_VARIANT = """
insert into quant.hypotheses
  (title, rationale, expected_edge, falsification_criteria,
   required_data_products, status, created_by, trace_id,
   proposal_id, lead_ids, research_packet_ids, claim_ids,
   economic_rationale, counterparty, competing_explanation,
   competing_explanation_codes, skeptic_sign, source_reported_effect)
values (%s,%s,%s,%s,%s,'PROPOSED',%s, gen_random_uuid(),
        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
returning hypothesis_id::text
"""

CREATED_BY = "factory-allocator"

# `expected_edge` `falsification_criteria` `required_data_products`
# `source_reported_effect` 가 jsonb 다. dict/list 를 그대로 넘기면
# `ProgrammingError: can't adapt type 'dict'` 로 발주가 통째로 죽는다(실측).
_JSONB_COLUMNS = ("expected_edge", "falsification_criteria",
                  "required_data_products", "source_reported_effect")


def _jb(value):
    """jsonb 열에 넣을 값을 감싼다. psycopg2 가 없으면 원값 그대로(자체 점검용)."""
    try:
        from psycopg2.extras import Json  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return value
    return Json(value)


def _unwrap(value):
    """`_jb` 로 감싼 값을 되돌린다. 자체 점검이 내용을 대조할 때 쓴다."""
    return getattr(value, "adapted", value)


def register_variant(conn, move: Move) -> tuple[str, str]:
    """배분자가 고른 수를 **가설로 등록한다.** 반환: (hypothesis_id, 사유).

    ▶ 왜 새 가설을 만드나 - 부모를 고치지 않기 위해서다
      사전등록된 가설의 `required_data_products` 를 바꾸면 **등록한 것과 실행한
      것이 달라진다.** 그래서 부모는 그대로 두고, 근거·반증기준·리드를 전부
      물려받은 변형을 새로 등록한다. 계열 키는 데이터셋을 안 보므로
      (`trial_family.FAMILY_FIELDS`) 자동으로 같은 계열의 trial N+1 이 된다.

    ▶ **판단이 필요 없는 수만 등록한다**
      "같은 엣지를 더 넓은 표본으로 다시 잰다" 는 결정론이다 - 엣지도
      파라미터도 안 바꾼다. 손잡이 값을 고르는 것은 판단이라 에이전트 몫이다.
      여기서 그것까지 하면 공장이 사람 없이 가설을 지어내는 것이 된다.

    ▶ 안 하는 것: 자멸적인 수, 예산 소진, 이미 같은 변형이 대기 중인 경우
    """
    if move.self_defeating:
        return "", "자멸적인 수 - 두면 DSR 이 관문 아래로 떨어진다"
    if move.score <= 0:
        return "", "점수 0 - 예산 소진이거나 닫는 조항이 없다"
    if move.kind != "표본 확대" or not move.dataset:
        return "", f"'{move.kind}' 는 판단이 필요한 수 - 에이전트가 고른다"

    with conn.cursor() as cur:
        cur.execute(_SQL_PARENT, (move.family_id,))
        row = cur.fetchone()
        if not row:
            return "", "부모 가설을 못 찾았다"
        (pid, title, rationale, edge, falsify, _req, proposal, leads,
         packets, claims, econ, cp, comp, comp_codes, sign, src) = row

        # ▶ **못 도는 부모의 변형은 못 돈다** (2026-08-12 파이프라인 실측)
        #   `774f3b75` 가 `observation_refs, universe` 로 발주 게이트에 막혀
        #   있는데, 배분자가 그 변형(`deb25128`)을 만들었다. 변형은
        #   `expected_edge` 를 그대로 물려받으므로 **태어날 때부터 막혀 있다.**
        #   실측으로 그런 가설이 2건 생겼다.
        #
        #   여기서 나쁜 키를 떼어 내지 않는다 - 그건 가설을 바꾸는 일이고
        #   우리 권한이 아니다. **부모를 고치는 것이 답이고, 그건 리서치 몫**
        #   이다(브리핑의 [막혀 있는 가설] 이 이미 그렇게 말한다).
        try:
            from config_binding import EDGE_KEYS  # noqa: PLC0415
            bad = sorted(k for k in (edge or {}) if k not in EDGE_KEYS)
        except Exception:  # noqa: BLE001 - 못 읽으면 막지 않는다
            bad = []
        if bad:
            return "", (f"부모가 실행면이 안 읽는 파라미터를 갖고 있다: "
                        f"{', '.join(bad)} - 변형도 똑같이 막힌다. "
                        f"리서치가 부모를 다시 등록해야 한다")

        # ▶ **부모가 이미 그 데이터셋이면 넓힐 게 없다** (2026-08-12 실측)
        #   파이프라인을 돌려 보니 `표본확대 … · 표본확대 …` 로 제목이 두 겹인
        #   가설이 4건 만들어졌고 실험 6건이 낭비됐다. 배분자 쪽에서도 막지만
        #   여기서도 막는다 - 두 곳 다 뚫려야 낭비가 나온다.
        if move.dataset in [str(x) for x in (_req or [])]:
            return "", f"부모가 이미 {move.dataset} 다 - 넓힐 게 없다"

        # 제목을 **부모의 원래 제목**에서 만든다. 부모가 이미 변형이면 그
        # 꼬리를 떼어 낸다 - 안 그러면 세대마다 제목이 길어진다.
        base = str(title).split(" · 표본확대 ")[0]
        new_title = f"{base[:44]} · 표본확대 {move.dataset}"
        cur.execute(_SQL_VARIANT_EXISTS, (CREATED_BY, new_title))
        if cur.fetchone():
            return "", "같은 변형이 이미 대기 중이다"

        note = (f"[배분자 자동 등록] 부모 {pid[:8]} 의 표본 확대 변형이다. "
                f"엣지·파라미터를 바꾸지 않고 데이터셋만 {move.dataset} 로 넓힌다. "
                f"근거: {move.why[:300]}")
        new_rationale = f"{note}\n\n--- 부모 가설의 근거 ---\n{str(rationale or '')}"

        # ▶ **`proposal_id` 는 물려받지 않는다** (2026-08-12 실측)
        #   `quant_hypotheses_proposal_id_uniq` 가 막는다 - 한 기획안당 가설
        #   하나다. 옳은 제약이다: 이 변형은 리서치가 낸 기획안이 아니라
        #   **공장이 만든 같은 계열의 다음 시도**다. 남의 기획안 번호를 달면
        #   그 기획안이 두 번 접수된 것처럼 보인다.
        #   혈통은 rationale 에 부모 id 로 남는다.
        cur.execute(_SQL_INSERT_VARIANT,
                    (new_title, new_rationale, _jb(edge or {}),
                     _jb(falsify), _jb([move.dataset]), CREATED_BY, None,
                     leads, packets, claims, econ, cp, comp, comp_codes,
                     sign, _jb(src)))
        hid = cur.fetchone()[0]
    conn.commit()
    return hid, f"{move.family_id[:12]} 를 {move.dataset} 로 다시 잰다"


def render(moves: list[Move], *, top: int = 6) -> str:
    if not moves:
        return "둘 수 있는 수가 없다. (지어내지 않았다)"
    lines = ["다음에 무엇을 돌릴 것인가 - **목표까지의 거리 순**", ""]
    for i, m in enumerate(moves[:top], 1):
        what = m.dataset or ", ".join(f"{k}={v}" for k, v in m.config_delta.items())
        mark = "  ⚠자멸" if m.self_defeating else ""
        lines.append(f"{i}. [{m.score:5.2f}]{mark} {m.family_id[:18]}  "
                     f"{m.kind}: {what}")
        lines.append(f"        {m.why[:118]}")
    n_bad = sum(1 for m in moves if m.self_defeating)
    if n_bad:
        lines.append(f"\n⚠ 자멸적인 수 {n_bad}건 - 두면 DSR 이 관문 아래로 떨어진다.")
        lines.append("  같은 계열을 그냥 다시 돌리는 것은 그 계열을 죽이는 일이다.")
    return "\n".join(lines)


# ── 자체 점검 ────────────────────────────────────────────────────────────────

_MOM = {"excess_return_pct": 157.51, "information_ratio": 1.2552,
        "deflated_sharpe": 0.9762, "bootstrap_ci_low": -0.0029,
        "max_drawdown_pct": -50.52, "turnover": 114.87, "ann_vol": 0.3421,
        "sharpe": 1.2765, "skew": -0.2154, "kurtosis": 8.2483,
        "excess_days_used": 626, "worst_window_mdd": -0.2544}


def _state(metrics, *, fragility="FRAGILE", trials=1, fam="fam_test"):
    from objective import evaluate_family
    return evaluate_family(metrics, fragility=fragility, trials=trials,
                           family_id=fam)


def _check_projection_matches_recorded_dsr():
    """**투영식이 원장의 DSR 을 재현한다.** 아니면 배분자가 딴 세상을 본다."""
    got = projected_dsr(_MOM, trials_after=1)
    assert got is not None and abs(got - _MOM["deflated_sharpe"]) < 0.002, got
    # 시도가 늘면 떨어진다 - 실측: 626일에서 시도 2회면 0.880
    d2 = projected_dsr(_MOM, trials_after=2)
    assert abs(d2 - 0.880) < 0.005, d2
    # 표본이 늘면 오른다 - 실측: 2,600일에서 시도 2회면 0.992
    d2w = projected_dsr(_MOM, trials_after=2, n_days_after=2600)
    assert abs(d2w - 0.992) < 0.005, d2w
    assert d2w > d2, "표본 확대가 DSR 을 못 올린다"
    print(f"  투영 = 원장 DSR          OK (1회 {got}, 2회 {d2}, 2회+표본 {d2w})")


def _check_self_defeating_is_flagged():
    """**같은 표본으로 한 번 더 돌리는 것은 자멸이다.** 이걸 못 잡으면 배분자가
    최고 계열을 스스로 죽인다."""
    from variant_generator import propose

    st = _state(_MOM)
    vs = propose(st, _MOM, wider_dataset="krx-basket-daily/v3")
    moves = [score_move(st, _MOM, v, wider_days=2600) for v in vs]
    by = {m.kind: m for m in moves}

    assert not by["표본 확대"].self_defeating, "표본 확대를 자멸로 봤다"
    assert by["낙폭 정지"].self_defeating, "같은 표본 재실험을 자멸로 안 봤다"
    assert "관문" in by["낙폭 정지"].why and "죽인다" in by["낙폭 정지"].why
    # **자멸적이지 않은 수가 위로 와야 한다**
    assert max(moves, key=lambda m: m.score).kind == "표본 확대", \
        [(m.kind, m.score) for m in moves]
    print("  자멸적인 수 식별         OK")


def _check_budget_stops_allocation():
    """예산을 소진한 계열에는 0점. **더 돌려도 증거가 안 된다.**"""
    from variant_generator import propose

    st = _state(_MOM, trials=5)
    v = propose(st, _MOM, wider_dataset="x/v3")[0]
    m = score_move(st, _MOM, v, budget=5)
    assert m.score == 0.0 and "예산" in m.why, m
    print("  예산 소진 -> 배분 중지   OK")


def _check_futility_stops_hopeless_families():
    """**희망 없는 계열을 접는다.** (2026-08-12 실측 - I-SPY 2 의 10% 무익 중단)

    지금까지 포기 규칙이 아예 없어서 `fam_65a4c7b6` 이 성적을 한 번도 못 내고
    7번 돌았다. 그 7번이 다른 계열의 시간과 DSR 예산을 먹었다.
    """
    # ① 벤치마크에 크게 지는 계열 - 손잡이로 못 뒤집는다
    dead = dict(_MOM, excess_return_pct=-175.03, information_ratio=-0.66,
                sharpe=-0.355, deflated_sharpe=0.0)
    why = futility(_state(dead, fam="fam_dead"), dead)
    assert why and "벤치마크에 크게 지" in why, why

    # ② 다음 시도 뒤 DSR 이 바닥이면 무익 - 시도를 더 해도 우연과 구분 안 됨
    weak = dict(_MOM, sharpe=0.30, deflated_sharpe=0.12,
                excess_return_pct=5.0)
    why2 = futility(_state(weak, trials=6, fam="fam_weak"), weak, budget=99)
    assert why2 and "우연과 구분되지 않는다" in why2, why2

    # ③ **살아 있는 계열은 안 접는다** - 오탐은 좋은 계열을 죽인다
    assert futility(_state(_MOM), _MOM) == "", futility(_state(_MOM), _MOM)

    # ④ 무익하면 표본 확대라도 0점 - 더 넓게 봐야 더 크게 질 뿐이다
    from variant_generator import propose
    st = _state(dead, fam="fam_dead")
    vs = propose(st, dead, wider_dataset="krx-basket-daily/v3")
    assert vs, "표본 확대 변형이 안 나왔다 - 검사가 무의미해졌다"
    mv = score_move(st, dead, vs[0], wider_days=2600)
    assert mv.score == 0.0 and "무익 중단" in mv.why, mv.why

    # ⑤ 재료가 없으면 접지 않는다 - 모르는 것과 나쁜 것은 다르다
    assert futility(_state({}, fam="f"), {}) == ""
    print("  무익 중단                OK")


def _check_closer_family_ranks_higher():
    """**목표에 가까운 계열이 위로.** 도착 순서가 아니라 거리다."""
    from variant_generator import propose

    near = _state(_MOM, fam="fam_near")                        # 4/8 통과
    dead = dict(_MOM, excess_return_pct=-168.77, information_ratio=-1.3959,
                deflated_sharpe=0.0011, sharpe=-0.958)
    far = _state(dead, fam="fam_far")                          # 0/8
    moves = []
    for st, mt in ((near, _MOM), (far, dead)):
        for v in propose(st, mt, wider_dataset="krx-basket-daily/v3"):
            moves.append(score_move(st, mt, v, wider_days=2600))
    moves.sort(key=lambda m: -m.score)
    assert moves[0].family_id == "fam_near", [(m.family_id, m.kind, m.score)
                                              for m in moves]
    # ▶ **지고 있는 계열이 이기는 계열보다 위로 오면 안 된다.** 조항 수만
    #   세면 그런 일이 난다 - 실측에서 초과 -168%p 계열이 순위에 올라왔다.
    best_far = max((m.score for m in moves if m.family_id == "fam_far"), default=0)
    best_near = max(m.score for m in moves if m.family_id == "fam_near")
    assert best_far < best_near * 0.5, (best_far, best_near)
    # 지고 있다는 사실이 어떤 형태로든 적혀야 한다. 무익 중단이 먼저 걸리면
    # 그 사유가, 아니면 점수 감가 사유가 적힌다 - 둘 다 없으면 침묵이다.
    far_whys = " ".join(m.why for m in moves if m.family_id == "fam_far")
    assert ("지고 있다" in far_whys or "무익 중단" in far_whys), \
        f"지고 있다는 사실이 안 적혔다: {far_whys[:120]}"
    print("  가까운 계열이 위로       OK")


def _check_nothing_is_invented():
    """재료가 없으면 **아무 수도 내지 않는다.**"""
    assert projected_dsr({}, trials_after=2) is None
    assert projected_dsr({"sharpe": 1.0}, trials_after=2) is None
    assert render([]) == "둘 수 있는 수가 없다. (지어내지 않았다)"
    print("  재료 없으면 수도 없음    OK")


def _check_only_deterministic_moves_are_auto_registered():
    """**판단이 필요한 수는 공장이 스스로 등록하지 않는다.**

    "같은 엣지를 더 넓은 표본으로" 는 결정론이다 - 엣지도 파라미터도 안 바꾼다.
    손잡이 값을 고르는 것은 판단이고, 그것까지 자동으로 하면 공장이 사람 없이
    가설을 지어내는 것이 된다. 사전등록의 뜻이 사라진다.
    """
    class _Conn:
        def cursor(self): raise AssertionError("DB 를 건드리면 안 되는 경로다")

    knob = Move(family_id="fam_x", kind="낙폭 정지", score=3.0,
                config_delta={"max_drawdown_stop": -0.28})
    hid, why = register_variant(_Conn(), knob)
    assert hid == "" and "판단이 필요한" in why, why

    bad = Move(family_id="fam_x", kind="표본 확대", dataset="a/v3",
               score=3.0, self_defeating=True)
    hid, why = register_variant(_Conn(), bad)
    assert hid == "" and "자멸" in why, why

    spent = Move(family_id="fam_x", kind="표본 확대", dataset="a/v3", score=0.0)
    hid, why = register_variant(_Conn(), spent)
    assert hid == "" and "점수 0" in why, why
    print("  결정론적 수만 자동등록   OK")


def _check_variant_inherits_provenance():
    """**변형은 부모의 근거를 물려받는다.** 근거 없는 가설을 새로 만들지 않는다."""
    parent = ("11111111-2222-3333-4444-555555555555", "[momentum] krx_all",
              "부모 근거 본문", {"type": "momentum"}, ["기준A"],
              ["krx-basket-daily/v2"], "prop_1", ["lead_1"], ["pkt_1"],
              ["claim_1"], "경제적 근거", "상대방", "경쟁 설명", ["CODE_A"],
              "sign", "0.4")

    class _Cur:
        def __init__(self): self.inserted = None
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            self._sql, self._p = sql, params
            if "insert into quant.hypotheses" in sql:
                self.inserted = params
        def fetchone(self):
            if "order by e.trial_number" in self._sql:
                return parent
            if "insert into" in self._sql:
                return ("new-hyp-id",)
            return None                         # 중복 없음

    cur = _Cur()

    class _Conn:
        def cursor(self): return cur
        def commit(self): pass

    mv = Move(family_id="fam_42663e9f", kind="표본 확대",
              dataset="krx-basket-daily/v3", score=3.0,
              why="표본만 넓힌다 | DSR 0.976 -> 0.993 예상")
    hid, why = register_variant(_Conn(), mv)
    assert hid == "new-hyp-id", (hid, why)
    p = cur.inserted
    assert "표본확대" in p[0] and "krx-basket-daily/v3" in p[0], p[0]
    # **데이터셋만 바뀐다** - 엣지·반증기준은 그대로다
    assert _unwrap(p[4]) == ["krx-basket-daily/v3"], p[4]
    assert _unwrap(p[2]) == {"type": "momentum"}, p[2]
    assert _unwrap(p[3]) == ["기준A"], p[3]

    # ▶ **jsonb 열은 감싸서 넘긴다** (2026-08-12 실측)
    #   dict/list 를 그대로 넘겼다가 `ProgrammingError: can't adapt type 'dict'`
    #   로 발주가 통째로 죽었다. 감싸는 것을 빠뜨리면 다시 난다.
    try:
        import psycopg2.extras  # noqa: PLC0415

        for idx, col in ((2, "expected_edge"), (3, "falsification_criteria"),
                         (4, "required_data_products"),
                         (15, "source_reported_effect")):
            assert isinstance(p[idx], psycopg2.extras.Json), \
                f"{col} 을 안 감쌌다 - can't adapt type 으로 죽는다"
    except ImportError:
        pass                                  # 호스트에 psycopg2 가 없으면 건너뛴다
    # 근거·리드·주장이 전부 따라와야 한다 - 안 따라오면 Gate 0 가 막는다
    for want in ("lead_1", "pkt_1", "claim_1", "경제적 근거", "상대방"):
        assert want in str(p), f"{want} 가 안 따라왔다"
    assert "부모 근거 본문" in p[1] and "배분자 자동 등록" in p[1]
    assert p[5] == CREATED_BY, "누가 만들었는지 안 남았다"

    # ▶ **기획안 번호는 물려받지 않는다** - 유니크 제약이 막고, 그게 옳다.
    #   이 변형은 리서치 기획안이 아니라 공장이 만든 다음 시도다.
    assert p[6] is None, "부모의 proposal_id 를 물려받았다 - 유니크 제약에 죽는다"
    assert "11111111" in p[1], "부모 혈통이 근거에 안 남았다"

    # ▶ **못 도는 부모의 변형은 만들지 않는다** (2026-08-12 파이프라인 실측)
    #   `774f3b75` 가 `observation_refs, universe` 로 막혀 있는데 그 변형
    #   `deb25128` 이 만들어져 태어날 때부터 막혔다. 실측 2건.
    broken = list(parent)
    broken[3] = {"type": "momentum", "observation_refs": ["x"], "universe": "krx"}
    cur2 = _Cur()
    cur2.fetchone = lambda: tuple(broken)          # noqa: ARG005

    class _C2:
        def cursor(self): return cur2
        def commit(self): pass

    hid2, why2 = register_variant(_C2(), mv)
    assert hid2 == "" and "실행면이 안 읽는" in why2, why2
    assert "observation_refs" in why2 and "universe" in why2, why2
    assert cur2.inserted is None, "막힌 부모인데 변형을 등록했다"
    print("  변형이 근거를 물려받음   OK")


def _selfcheck() -> int:
    print(f"{ALLOCATOR_VERSION} 자체 점검 (DB 없음)")
    _check_projection_matches_recorded_dsr()
    _check_self_defeating_is_flagged()
    _check_budget_stops_allocation()
    _check_futility_stops_hopeless_families()
    _check_closer_family_ranks_higher()
    _check_nothing_is_invented()
    _check_only_deterministic_moves_are_auto_registered()
    _check_variant_inherits_provenance()
    print("배분자 8개 영역 통과.")
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="다음 수 배분")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--wider", default="", help="넓은 데이터셋 <이름>/<버전>")
    ap.add_argument("--wider-days", type=int, default=None)
    a = ap.parse_args(argv)
    if a.check:
        return _selfcheck()

    import psycopg2

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                           / "01-research" / "collectors"))
    from source_registry import load_project_env
    conn = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)
    try:
        moves = plan(conn, wider_dataset=a.wider, wider_days=a.wider_days)
    finally:
        conn.close()
    if a.json:
        print(json.dumps([m.as_dict() for m in moves], ensure_ascii=False, indent=2))
        return 0
    print(render(moves))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
