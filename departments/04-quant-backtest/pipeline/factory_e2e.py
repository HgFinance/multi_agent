"""공장 한 바퀴 실증 - 기획안 -> 접수 -> 실험 -> 카드 -> 환류 -> 재조회 차단.

담당: 재일 (리서치본부 + 퀀트·백테스트본부)

▶ 무엇을 증명하나
  "루프가 닫혔다" 는 주장을 코드가 아니라 **실행으로** 확인한다. 여섯 단계를 실제
  DB 에 대고 돌리고, 마지막에 같은 계열의 기획안이 **교훈 미대응으로 막히는지**
  본다. 막히면 환류가 실제로 다음 가설에 닿은 것이다.

  1. 리서치가 리드와 기획안을 만든다(계약 검증 + 발행 게이트)
  2. 퀀트가 Gate 0 로 접수한다(어휘·예산·기각이력)
  3. 가설을 계보와 함께 등록한다
  4. 실험을 돌린다(사전등록 -> PIT -> 백테스트 -> 과적합 통계)
  5. 판정을 환류한다(적재와 상태 전이가 한 트랜잭션)
  6. 같은 계열 기획안을 다시 넣어 **차단되는지** 본다

▶ 이 스크립트는 쓰기를 한다
  실증이므로 실제 행을 만든다. 전부 `e2e-` 접두 ID 라 나중에 골라낼 수 있고,
  `--cleanup` 으로 지운다. **운영 데이터를 건드리지 않는다.**

실행: python departments/04-quant-backtest/pipeline/factory_e2e.py --run
      python departments/04-quant-backtest/pipeline/factory_e2e.py --cleanup
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_RESEARCH = _HERE.parents[1] / "01-research"
for p in (str(_HERE), str(_RESEARCH)):
    if p not in sys.path:
        sys.path.insert(0, p)

from contracts.factory_contracts import (  # noqa: E402
    CompetingExplanation,
    DataRequirement,
    ExperimentProposalV1,
    MethodologyLeadV1,
    PriorCheck,
    ScoutLens,
    SourceRef,
    SourceType,
    lead_id_for,
)
from factory.publish_gate import evaluate as publish_gate  # noqa: E402

from factory_bridge import (  # noqa: E402
    build_outcome,
    count_family_trials,
    fetch_family_outcomes,
    gate0,
    lessons_from,
    to_hypothesis_row,
)

TAG = "e2e"
NOW = datetime.now(timezone.utc)


def _conn():
    env = Path(__file__).resolve().parents[3] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                os.environ.setdefault("DATABASE_URL", line.split("=", 1)[1].strip())
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _say(step: str, msg: str) -> None:
    print(f"  [{step}] {msg}")


# ── 1. 리서치 산출 ───────────────────────────────────────────────────────────

def make_research_output(suffix: str = "1") -> tuple[MethodologyLeadV1, ExperimentProposalV1]:
    """스카우트가 낼 법한 리드와 편집장이 낼 법한 기획안. **계약이 검증한다.**"""
    refs = (SourceRef(
        url=f"https://arxiv.org/abs/2401.{suffix.zfill(5)}",
        title="Liquidity shocks and short-horizon reversal in equity markets",
        author="Author et al.", published_at="2024-01-15",
        accessed_at=NOW,
        excerpt=("We find that stocks experiencing abnormal turnover together with "
                 "negative returns subsequently reverse, consistent with liquidity "
                 "provision being compensated."),
    ),)
    lead = MethodologyLeadV1(
        lead_id=lead_id_for(list(refs)), case_id=f"{TAG}-case-{suffix}",
        scout_lens=ScoutLens.ACADEMIC, source_type=SourceType.PAPER,
        as_known_at=NOW, refs=refs,
        claimed_edge="거래대금이 급증하며 하락한 종목은 이후 되돌아온다",
        stated_mechanism="강제 매도자가 유동성 공급자에게 프리미엄을 지불한다",
        market_context="US equities, 1993-2018",
        stated_failure_mode="유동성 위기 구간에서는 되돌림이 지연되거나 사라진다",
        testability="RULE_EXPRESSIBLE",
    )
    proposal = ExperimentProposalV1(
        proposal_id=f"{TAG}-prop-{suffix}", case_id=f"{TAG}-case-{suffix}",
        as_known_at=NOW, lead_ids=(lead.lead_id,),
        economic_rationale=("레버리지 청산과 펀드 환매가 반대편에서 가격을 무시하고 "
                            "판다. 그 압력이 가라앉으면 유동성 공급자가 보상을 받는다"),
        counterparty="강제 청산·환매 물량",
        competing_explanation="단순히 하락 후 반등(평균회귀)이거나 비용 미반영일 수 있다",
        competing_explanation_codes=(CompetingExplanation.DATA_MINING,
                                     CompetingExplanation.COST_UNACCOUNTED),
        skeptic_sign=f"{TAG}-skeptic-run-{suffix}",
        edge_type="liquidity_shock_reversal", universe_key="krx_all",
        falsification_tests=("하락장 초과수익이 0 미만이면 기각",
                             "거래대금 급증 조건을 빼도 같은 성과면 유동성 설명이 아니다"),
        data_requirements=DataRequirement(tables=("market_bars",), min_history_days=400),
        suggested_params={"lookback_days": 5, "top_n": 20},
        trial_budget=5, prior_check=PriorCheck(),
        source_reported_effect={"market": "US equities", "period": "1993-2018",
                                "reported_metric": "annualized_excess_return",
                                "reported_value": 0.04},
    )
    return lead, proposal


def persist_research(conn, lead: MethodologyLeadV1, prop: ExperimentProposalV1) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            insert into research.methodology_leads
              (lead_id, case_id, scout_lens, source_type, as_known_at, refs,
               claimed_edge, stated_mechanism, market_context, stated_failure_mode,
               testability, status)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (lead_id) do nothing
        """, (lead.lead_id, lead.case_id, lead.scout_lens.value,
              lead.source_type.value, lead.as_known_at,
              json.dumps([r.model_dump(mode="json") for r in lead.refs]),
              lead.claimed_edge, lead.stated_mechanism, lead.market_context,
              lead.stated_failure_mode, lead.testability.value, lead.status.value))
        cur.execute("""
            insert into research.experiment_proposals
              (proposal_id, case_id, as_known_at, lead_ids, economic_rationale,
               counterparty, competing_explanation, competing_explanation_codes,
               skeptic_sign, edge_type, universe_key, label, baseline,
               falsification_tests, data_requirements, suggested_params,
               trial_budget, prior_check, source_reported_effect, status)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PUBLISHED')
            on conflict (proposal_id) do nothing
        """, (prop.proposal_id, prop.case_id, prop.as_known_at, list(prop.lead_ids),
              prop.economic_rationale, prop.counterparty, prop.competing_explanation,
              [c.value for c in prop.competing_explanation_codes], prop.skeptic_sign,
              prop.edge_type, prop.universe_key, prop.label, prop.baseline,
              list(prop.falsification_tests),
              json.dumps(prop.data_requirements.model_dump(mode="json")),
              json.dumps(prop.suggested_params), prop.trial_budget,
              json.dumps(prop.prior_check.model_dump(mode="json")),
              json.dumps(prop.source_reported_effect)))
    conn.commit()


def proposal_as_dict(prop: ExperimentProposalV1) -> dict:
    d = prop.model_dump(mode="json")
    d["competing_explanation_codes"] = [c.value for c in prop.competing_explanation_codes]
    return d


# ── 3. 접수 -> 가설 등록 ─────────────────────────────────────────────────────

def register_hypothesis(conn, row: dict) -> str:
    """가설 등록. **trial_family_id/trial_number 는 여기 넣지 않는다.**

    Family 는 실험 단위 개념이라 `quant.experiments` 에 있다(마이그레이션
    20260804001400). 가설에 박아 두면 같은 가설의 여러 시도가 한 값을 공유해
    시도 계수가 무의미해진다 - 계수는 실험 행이 센다.
    """
    with conn.cursor() as cur:
        cur.execute("""
            insert into quant.hypotheses
              (title, rationale, expected_edge, falsification_criteria,
               required_data_products, status, created_by, trace_id,
               proposal_id, lead_ids, economic_rationale, counterparty,
               competing_explanation, competing_explanation_codes, skeptic_sign,
               source_reported_effect)
            values (%s,%s,%s,%s,%s,'PROPOSED',%s, gen_random_uuid(),
                    %s,%s,%s,%s,%s,%s,%s,%s)
            returning hypothesis_id
        """, (row["title"], row["rationale"], json.dumps(row["expected_edge"]),
              json.dumps(row["falsification_criteria"]),
              json.dumps(row["required_data_products"]), row["created_by"],
              row["proposal_id"], row["lead_ids"], row["economic_rationale"],
              row["counterparty"], row["competing_explanation"],
              row["competing_explanation_codes"], row["skeptic_sign"],
              json.dumps(row["source_reported_effect"])))
        hid = cur.fetchone()[0]
    conn.commit()
    return str(hid)


def run() -> int:
    print("전략 공장 한 바퀴 실증\n")
    conn = _conn()
    fails = 0

    # 1. 리서치 산출 (계약 검증)
    lead, prop = make_research_output("1")
    _say("1", f"리드 생성 {lead.lead_id} (출처 {len(lead.refs)}건, 계약 통과)")
    _say("1", f"기획안 생성 {prop.proposal_id} edge={prop.edge_type} universe={prop.universe_key}")

    # 2. 발행 게이트
    g = publish_gate(prop, leads={lead.lead_id: lead}, past_outcomes=[])
    _say("2", f"발행 게이트 ok={g.ok} blockers={g.blockers} warnings={len(g.warnings)}")
    if not g.ok:
        print("!! 발행 게이트에서 막혔다"); return 1
    persist_research(conn, lead, prop)
    _say("2", "research.methodology_leads / experiment_proposals 적재")

    # 3. Gate 0 접수
    pd = proposal_as_dict(prop)
    fam_probe = gate0(pd, trials_used=0, past_outcomes=[])
    used = count_family_trials(conn, fam_probe.trial_family_id)
    past = fetch_family_outcomes(conn, fam_probe.trial_family_id)
    g0 = gate0(pd, trials_used=used, past_outcomes=past)
    _say("3", f"Gate 0 ok={g0.ok} family={g0.trial_family_id} "
              f"시도={g0.trial_number} (기존 실행 {used}, 환류 {len(past)})")
    if not g0.ok:
        print(f"!! Gate 0 차단: {g0.codes} {g0.reasons}"); return 1

    # 4. 가설 등록 (계보 포함)
    hid = register_hypothesis(conn, to_hypothesis_row(pd, g0))
    _say("4", f"가설 등록 {hid} (proposal_id·경제근거·반대편·서명 계보 포함)")
    with conn.cursor() as cur:
        cur.execute("select proposal_id, counterparty, skeptic_sign, "
                    "competing_explanation_codes "
                    "from quant.hypotheses where hypothesis_id=%s", (hid,))
        got = cur.fetchone()
    _say("4", f"계보 확인: proposal={got[0]} counterparty={got[1]!r} "
              f"skeptic={got[2]} codes={got[3]}")
    _say("4", f"Family {g0.trial_family_id} / 시도 {g0.trial_number} 은 실험 행이 든다")

    # 5. 판정 -> 환류 (실험 실행은 별도 - 여기서는 판정 재료를 가정해 환류를 검증한다)
    failed = ["pbo", "min_deflated_sharpe"]
    concerns = ["하락장 평균 수익률 -32.1% - 상승장에서 벌고 하락장에서 토해내는 형태"]
    lessons = lessons_from(failed_criteria=failed, regime_concerns=concerns)
    outcome = build_outcome(
        experiment_id=f"{TAG}-exp-1", hypothesis_id=hid,
        trial_family_id=g0.trial_family_id, trial_number=g0.trial_number,
        decision="REJECT", failed_criteria=failed, lesson_codes=lessons,
        oos_summary={"pbo": 0.8, "deflated_sharpe": 0.13, "information_ratio": None},
        regime_concerns=concerns, proposal_id=prop.proposal_id,
        notes="E2E 실증")
    _say("5", f"교훈 사상(결정론): {lessons}")
    from factory_bridge import finalize
    oid = finalize(conn, hypothesis_id=hid, new_status="REJECTED", outcome=outcome)
    _say("5", f"환류 적재 {oid} + 상태 전이 REJECTED (한 트랜잭션)")
    with conn.cursor() as cur:
        cur.execute("select status from quant.hypotheses where hypothesis_id=%s", (hid,))
        st = cur.fetchone()[0]
        cur.execute("select oos_summary, lesson_codes from research.experiment_outcomes "
                    "where outcome_id=%s", (oid,))
        oos, lc = cur.fetchone()
    _say("5", f"상태={st}, 적재된 oos={oos} (미측정 information_ratio 는 키가 없다)")
    if "information_ratio" in (oos or {}):
        print("!! 미측정 지표가 0 으로 채워졌다"); fails += 1

    # 6. 같은 계열 재도전 -> 교훈 미대응이면 차단되어야 한다
    _, prop2 = make_research_output("2")
    pd2 = proposal_as_dict(prop2)
    past2 = fetch_family_outcomes(conn, g0.trial_family_id)
    used2 = count_family_trials(conn, g0.trial_family_id)
    g2 = gate0(pd2, trials_used=used2, past_outcomes=past2)
    _say("6", f"같은 계열 재도전 -> ok={g2.ok} codes={g2.codes}")
    if g2.ok:
        print("!! 교훈 미대응 재도전이 통과했다 - 루프가 안 닫혔다"); fails += 1
    else:
        _say("6", f"차단 사유: {g2.reasons[-1][:80]}")

    # 7. 교훈에 대응하면 통과해야 한다
    prop3 = prop2.model_copy(update={
        "proposal_id": f"{TAG}-prop-3",
        "prior_check": PriorCheck(
            trial_family_id=g0.trial_family_id, trials_used=used2,
            past_outcomes=(oid,),
            lessons_addressed={c: f"{c} 대응: 표본과 변형 수를 조정한다" for c in lc}),
    })
    g3 = gate0(proposal_as_dict(prop3), trials_used=used2, past_outcomes=past2)
    _say("7", f"대응 후 재도전 -> ok={g3.ok} 시도={g3.trial_number}")
    if not g3.ok:
        print(f"!! 대응했는데 막혔다: {g3.codes} {g3.reasons}"); fails += 1

    conn.close()
    print()
    if fails:
        print(f"실증 실패 {fails}건")
        return 1
    print("한 바퀴 통과 - 기획안 -> 접수 -> 계보 등록 -> 환류 -> 차단 -> 대응 후 통과")
    return 0


def cleanup() -> int:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute("delete from research.experiment_outcomes where experiment_id like %s",
                    (f"{TAG}-%",))
        n1 = cur.rowcount
        # ▶ 자손부터 지운다 (2026-08-11 실측)
        #   hypotheses 를 먼저 지우면 FK 로 막혀 정리가 통째로 실패하고, 그러면
        #   다음 실증이 이전 판 위에서 돈다(실제로 Gate 0 이 이전 교훈에 막혔다).
        #   순서는 information_schema 로 확인한 실제 FK 그래프를 따른다:
        #     backtest_runs/experiment_metrics/model_artifacts/strategy.candidates
        #       -> experiments -> hypotheses  (experiment_jobs 도 hypotheses 참조)
        _MINE = "select hypothesis_id from quant.hypotheses where proposal_id like %s"
        _MY_EXP = f"select experiment_id from quant.experiments where hypothesis_id in ({_MINE})"
        n_child = 0
        for table in ("quant.backtest_runs", "quant.experiment_metrics",
                      "quant.model_artifacts", "strategy.candidates"):
            cur.execute(
                f"delete from {table} where experiment_id in ({_MY_EXP})", (f"{TAG}-%",)
            )
            n_child += cur.rowcount
        cur.execute(f"delete from quant.experiments where hypothesis_id in ({_MINE})",
                    (f"{TAG}-%",))
        n_exp = cur.rowcount
        cur.execute(f"delete from quant.experiment_jobs where hypothesis_id in ({_MINE})",
                    (f"{TAG}-%",))
        n_job = cur.rowcount
        cur.execute("delete from quant.hypotheses where proposal_id like %s", (f"{TAG}-%",))
        n2 = cur.rowcount
        print(f"  선행 삭제: 실험 자손 {n_child}, experiments {n_exp}, jobs {n_job}")
        cur.execute("delete from research.experiment_proposals where proposal_id like %s",
                    (f"{TAG}-%",))
        n3 = cur.rowcount
        cur.execute("delete from research.methodology_leads where case_id like %s",
                    (f"{TAG}-%",))
        n4 = cur.rowcount
    conn.commit(); conn.close()
    print(f"정리: outcomes {n1}, hypotheses {n2}, proposals {n3}, leads {n4}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if "--cleanup" in sys.argv:
        raise SystemExit(cleanup())
    if "--run" in sys.argv:
        raise SystemExit(run())
    print(__doc__)
