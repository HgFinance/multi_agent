#!/usr/bin/env python3
"""QNT-00 실험 오케스트레이터 - 가설을 실험 체인에 태우는 결정론 상태 머신.

소유: 재일 (퀀트/백테스트본부, QNT-00 supervisor 의 결정론 부분)
근거: quant.hypotheses (strategy_hypothesis_agent 가 PROPOSED 등록),
      pipeline/{pit_dataset,backtest_runner,walk_forward}.py (실험 체인),
      QNT-00 페르소나 계약("실패를 포함한 모든 실험을 Registry 에 기록,
      Production 승격은 직접 하지 않는다")

▶ 설계 - 오케스트레이션은 판단이 아니라 게이트다
  1. 실험 가능성 게이트(결정론):
     - required_data_products 가 quant.dataset_manifests 에 실재하는가
     - expected_edge.type 이 **구현된 전략 카탈로그**에 있는가
     둘 중 하나라도 없으면 NOT_RUNNABLE - 가설은 PROPOSED 로 남고(가설이
     틀린 게 아니라 실험 수단이 없는 것), 부족분이 백로그로 보고된다.
     없는 전략을 비슷한 구현으로 대충 돌리는 것이 이 게이트가 막는 거짓이다.
  2. 실행 가능하면: PROPOSED→TESTING 전이 후 백테스트+강건성 검증을 돌리고,
     강건성 판정(FRAGILE 여부)으로 TESTING→REJECTED/SUPPORTED 를 전이한다.
     전이는 실험 증거(experiment_id) 없이는 일어나지 않는다.
  3. 승격은 없다 - SUPPORTED 조차 Candidate 제출 자격일 뿐, Production
     결정은 CEO·Risk·QA 게이트 몫(권한 분리).

사용
  python pipeline/experiment_orchestrator.py            # 자체 점검 (DB 없음)
  python pipeline/experiment_orchestrator.py --run      # 최신 PROPOSED 가설 처리
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ORCH_VERSION = "quant-experiment-orchestrator-v1"

# 구현된 전략 카탈로그 - 여기 없는 edge type 은 실험 불가가 사실이다.
# 새 전략을 구현하면 한 줄 추가한다 (구현 없이 추가하는 것이 금지 사항).
STRATEGY_CATALOG: dict[str, dict] = {
    "momentum": {
        "strategy_code": "MOM-20-SMOKE",
        "impl": "pipeline/backtest_runner.py + walk_forward.py",
        "note": "20일 모멘텀 상위 N 균등, 월초 리밸런스",
    },
    "mean_reversion": {
        "strategy_code": "REV-5-SMOKE",
        "impl": "pipeline/backtest_runner.py (STRATEGIES) + walk_forward 조각",
        "note": "5일 낙폭 하위 N 균등, 5거래일 리밸런스 (2026-08-01 구현 - "
                "QNT-01 첫 가설의 백로그를 이행)",
    },
}
# v2 부터 notional 을 담는다(유동성 계층 슬리피지 재료). v1 파티션 파일은
# v2 빌드가 같은 경로에 덮어써 매니페스트와 해시가 어긋난다 - 해시 가드가
# 실제로 그것을 잡았다("재현성이 깨진 채 돌지 않는다").
DATASET_NAME, DATASET_VERSION = "krx-basket-daily", "v2"


@dataclass
class OrchestratorReport:
    hypothesis_id: str
    title: str
    verdict: str                    # RUNNABLE / NOT_RUNNABLE / NO_HYPOTHESIS
    missing: list = field(default_factory=list)
    transitions: list = field(default_factory=list)
    experiment_refs: dict = field(default_factory=dict)
    backlog: list = field(default_factory=list)
    trial_pressure: dict = field(default_factory=dict)   # 몇 번째 시도인가
    release: dict = field(default_factory=dict)          # QNT-07 관문 판정
    preregistration: dict = field(default_factory=dict)  # 사전등록 지문·검증
    lifecycle: dict = field(default_factory=dict)        # 전략 생명주기 요청
    # 2026-08-10: 리서치 환류. **비어 있으면 루프가 안 닫힌 것**이다 -
    # 종결됐는데 이 값이 없으면 그 교훈은 다음 기획안에 닿지 못한다.
    feedback: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 게이트 (순수 함수 - 자체점검 대상)
# ---------------------------------------------------------------------------

def feasibility(hypothesis: dict, existing_datasets: set,
                catalog: dict | None = None) -> tuple[bool, list, list]:
    """(실행 가능?, 부족 목록, 백로그 제안). 판단이 아니라 존재 확인이다."""
    catalog = STRATEGY_CATALOG if catalog is None else catalog
    missing: list = []
    backlog: list = []

    needed = hypothesis.get("required_data_products") or []
    for d in needed:
        if d not in existing_datasets:
            missing.append(f"dataset:{d}")
            backlog.append(f"Dataset '{d}' 구축 (pit_dataset.py --build)")
    if not needed:
        missing.append("dataset:(미지정)")
        backlog.append("가설에 required_data_products 가 없다 - QNT-01 스펙 보강")

    edge = ((hypothesis.get("expected_edge") or {}).get("type") or "").strip().lower()
    if not edge:
        missing.append("edge_type:(미지정)")
    elif edge not in catalog:
        missing.append(f"strategy_impl:{edge}")
        backlog.append(f"'{edge}' 전략 구현 (STRATEGY_CATALOG 등재 조건)")
    return (not missing), missing, backlog


# ▶ **상태 머신이 세 갈래로 갈라져 있다** (2026-08-04 실측)
#     계약 quant_v2.HypothesisStatus : INTAKE -> PREREGISTERED ->
#                                      DATASET_CERTIFIED -> RUNNING ->
#                                      ROBUSTNESS_REVIEW -> SUPPORTED/REJECTED
#     DB 제약 quant.hypotheses        : PROPOSED/APPROVED/TESTING/SUPPORTED/
#                                      REJECTED/INCONCLUSIVE/ARCHIVED
#     이 실행부                       : PROPOSED -> TESTING -> {SUPPORTED,
#                                      REJECTED, INCONCLUSIVE}
#
#   계약의 7단계는 사전등록(PREREGISTERED)과 데이터셋 인증(DATASET_CERTIFIED)을
#   별도 관문으로 두는데 **호출처가 0개**다 - 지금은 그 두 관문 없이 바로
#   TESTING 으로 간다. 결과를 본 뒤 설정을 바꾸는 것을 막는 장치가 그 자리인데
#   비어 있다는 뜻이다(계획 2번 "사전등록 강화" 가 이것이다).
#
#   지금 갈라진 채로 두는 이유: 상태를 계약 쪽으로 옮기려면 DB 제약·기존 14행·
#   실행부를 같이 바꿔야 하고, 그 사이 어느 하나만 먼저 바뀌면 UPDATE 가 죽는다.
#   실제로 오늘 INCONCLUSIVE 를 코드에만 넣었다가 DB 제약에 없어 예산 초과가
#   나는 순간 죽을 뻔했다(마이그레이션 20260804001000 으로 메웠다).
#   **다음 작업에서 세 갈래를 하나로 합친다.**
def robustness_to_status(fragility_verdict: str,
                         pressure: dict | None = None) -> str:
    """강건성 판정 -> 가설 상태. SUPPORTED 도 승격이 아니라 후보 자격일 뿐.

    ▶ **시도 횟수를 같이 본다** (재일님 2026-08-04)
      웹에서 컨셉을 빌려 우리가 구현하면 파라미터 자유도가 전부 우리 것이 된다.
      한 컨셉으로 변형 20개를 돌려 제일 좋은 것을 고르면 그 성적은 실력이
      아니라 **다중검정**이다 - 12번째 시도의 Sharpe 1.5 는 1번째와 다르다.

      contracts/quant_v2.trial_pressure() 가 이 계산을 진작 하고 있었는데
      **호출처가 0개였다.** 예산을 넘긴 Family 의 ROBUST 는 SUPPORTED 로
      올리지 않고 INCONCLUSIVE 로 둔다 - 틀렸다는 뜻이 아니라 이 표본으로는
      실력과 운을 못 가린다는 뜻이다.
    """
    v = (fragility_verdict or "").strip().upper()
    if v == "FRAGILE":
        return "REJECTED"
    if v == "ROBUST":
        if (pressure or {}).get("over_budget"):
            return "INCONCLUSIVE"
        return "SUPPORTED"
    raise ValueError(f"알 수 없는 강건성 판정: {fragility_verdict!r} - "
                     f"모르는 값을 상태 전이로 옮기지 않는다")


# 한 컨셉(edge type)에 허용하는 변형 시도 수. 넘으면 ROBUST 라도 SUPPORTED 로
# 올리지 않는다 - 많이 돌려 고른 최고치는 실력과 운을 못 가린다.
TRIAL_BUDGET_DEFAULT = 5


# ---------------------------------------------------------------------------
# 오케스트레이션 본체
# ---------------------------------------------------------------------------

# ▶ **상태를 바꾸는 UPDATE 는 status_changed_at 을 함께 쓴다.** 빠뜨리면 그
#   실험은 /jobs/stuck 에서 영원히 멈춘 것으로 보인다 - 자체점검이 소스에서
#   status= 를 쓰는 UPDATE 마다 그 컬럼이 있는지 확인한다.
# 실행부 상태 -> 환류 판정. SUPPORTED 는 승격이 아니라 **제출 자격**이다.
_STATUS_TO_DECISION = {
    "REJECTED": "REJECT",
    "SUPPORTED": "SUBMIT_TO_QA",
    "INCONCLUSIVE": "GATE_HOLD",
}

# 환류 oos_summary 로 옮길 지표. **없는 것은 넣지 않는다**(미측정과 0 을 구분).
_OOS_KEYS = ("excess_return_pct", "information_ratio", "max_drawdown_pct",
             "deflated_sharpe", "pbo", "bootstrap_ci_low", "bootstrap_ci_high")


def _finalize_with_feedback(conn, *, report, hid: str, new_status: str,
                            experiment_id, failed_criteria=None,
                            lesson_codes=None, fragility: str = "",
                            notes: str = "") -> None:
    """상태 전이를 **환류와 함께** 커밋한다.

    ▶ 왜 여기서 실패를 삼키지 않나
      환류를 못 적재하면 그 실험의 교훈은 영영 Gate 0 에 닿지 않고, 회사는 같은
      실험을 다시 산다. 조용히 상태만 바꾸느니 실험을 미종결로 두고 사람이 보는
      편이 낫다 - 그래서 예외를 잡지 않는다(fail-closed).
    """
    from factory_bridge import build_outcome, finalize, lessons_from

    tp = report.trial_pressure or {}
    exp_id = str(experiment_id or "")
    oos: dict = {}
    if exp_id:
        # 이 파일의 관례를 따라 컨텍스트 매니저를 쓰지 않는다(자체 점검의 가짜 커서 호환)
        cur = conn.cursor()
        cur.execute("""select metric, value from quant.experiment_metrics
                        where experiment_id = %s
                          and coalesce(dimensions->>'window','') in ('','SUMMARY')
                          and dimensions->>'regime' is null""", (exp_id,))
        # (metric, value) 2-튜플이 아닌 행은 지표 행이 아니다 - 조용히 건너뛴다
        found = {r[0]: float(r[1]) for r in (cur.fetchall() or [])
                 if isinstance(r, (list, tuple)) and len(r) == 2}
        oos = {k: found[k] for k in _OOS_KEYS if k in found}
        # 카드와 이름을 맞춘다 - 두 곳이 다른 이름을 쓰면 대조가 안 된다
        for src, dst in (("bootstrap_ci_low", "ci_low"),
                         ("bootstrap_ci_high", "ci_high")):
            if src in oos:
                oos[dst] = oos.pop(src)

    failed = list(failed_criteria or [])
    if not failed and new_status == "REJECTED" and fragility:
        failed = [f"fragility_{str(fragility).lower()}"]
    lessons = list(lesson_codes or []) or lessons_from(
        failed_criteria=failed,
        regime_concerns=report.notes if hasattr(report, "notes") else (),
        fragility=fragility)
    decision = _STATUS_TO_DECISION.get(new_status, "GATE_HOLD")

    outcome = build_outcome(
        experiment_id=exp_id or f"unknown-{hid}", hypothesis_id=str(hid),
        trial_family_id=str(tp.get("trial_family_id") or ""),
        trial_number=int(tp.get("trial_number") or 1),
        decision=decision, failed_criteria=failed, lesson_codes=lessons,
        oos_summary=oos, notes=notes)
    oid = finalize(conn, hypothesis_id=str(hid), new_status=new_status,
                   outcome=outcome)
    report.feedback = {"outcome_id": oid, "decision": decision,
                       "lesson_codes": lessons, "oos_keys": sorted(oos)}


def _norm_data_products(v) -> list:
    """가설의 required_data_products 를 이름 리스트로 정규화한다."""
    if v is None or v == "":
        return []
    if isinstance(v, str):
        v = json.loads(v)
    if isinstance(v, dict):
        # DataRequirement {tables:[...], min_history_days:N} - 이름만 꺼낸다
        return list(v.get("tables") or v.get("data_products") or [])
    return list(v)


def orchestrate(hypothesis_id: str | None = None, *, conn=None,
                run_chain=None) -> OrchestratorReport:
    """가설 하나를 게이트에 태운다. conn/run_chain 주입은 자체점검용."""
    own_conn = conn is None
    if own_conn:
        import psycopg2

        sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                               / "01-research" / "collectors"))
        from source_registry import load_project_env

        conn = psycopg2.connect(load_project_env()["DATABASE_URL"],
                                connect_timeout=20)
    try:
        cur = conn.cursor()
        if hypothesis_id:
            cur.execute("""
                select hypothesis_id, title, expected_edge, required_data_products,
                       status from quant.hypotheses where hypothesis_id = %s
            """, (hypothesis_id,))
        else:
            cur.execute("""
                select hypothesis_id, title, expected_edge, required_data_products,
                       status from quant.hypotheses
                -- ▶ 계약 상태로 옮기면서 여기를 안 고쳐 대기 가설을 못
                --   찾고 있었다(NO_HYPOTHESIS). 옛 값도 함께 본다 -
                --   이행기 동안 둘 다 존재한다.
                where status in ('INTAKE', 'PROPOSED')
                order by created_at desc limit 1
            """)
        row = cur.fetchone()
        if row is None:
            return OrchestratorReport(hypothesis_id="-", title="-",
                                      verdict="NO_HYPOTHESIS")
        hid, title, edge, data_products, _status = row
        hyp = {"expected_edge": edge if isinstance(edge, dict) else json.loads(edge or "{}"),
               # ▶ 세 모양을 다 받는다: 리스트(구 형식), dict(기획안의
               #   DataRequirement {tables, min_history_days}), JSON 문자열.
               #   기획안 경로가 dict 를 넣는데 리스트만 처리해 TypeError 로
               #   실험이 죽었다(2026-08-10 실측).
               "required_data_products": _norm_data_products(data_products),
               # ▶ status 를 언패킹만 하고 dict 에 안 넣어서 사전등록 관문이
               #   빈 문자열을 읽고 "순서를 건너뛴다" 로 막았다. 조회한 값을
               #   쓰지 않으면 조회하지 않은 것과 같다.
               "status": _status}

        cur.execute("select distinct name || '/' || version from quant.dataset_manifests")
        datasets = {r[0] for r in cur.fetchall()}

        ok, missing, backlog = feasibility(hyp, datasets)
        report = OrchestratorReport(hypothesis_id=str(hid), title=title,
                                    verdict="RUNNABLE" if ok else "NOT_RUNNABLE",
                                    missing=missing, backlog=backlog)
        if not ok:
            return report          # PROPOSED 유지 - 수단 부족은 가설의 죄가 아니다

        # 실행 가능 - TESTING 전이 후 체인 실행 (전이는 증거와 함께만 전진)
        # ── 사전등록 관문 ────────────────────────────────────────────────
        # ▶ **결과를 보기 전에 실질 내용을 고정한다.** trial_pressure 는
        #   "몇 번 시도했나" 를 세고, 이것은 "같은 실험인 척 설정을 바꿨나" 를
        #   잡는다 - 둘은 다른 부정을 막는다.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from preregistration import can_transition, normalize_status
        from preregistration import preregister, verify as prereg_verify

        spec_row = {k: hyp.get(k) for k in (
            "features", "label", "preregistered_splits", "cost_model_version",
            "falsification_tests", "universe_version", "decision_frequency",
            "holding_horizon", "baseline", "entry_exit_rules",
            "strategy_family") if hyp.get(k) is not None}
        # edge 로부터 실질 필드를 채운다(가설 발행기가 아직 평평한 스키마다)
        edge = hyp.get("expected_edge") or {}
        spec_row.setdefault("strategy_family", edge.get("type"))
        spec_row.setdefault("holding_horizon", edge.get("horizon_days"))
        spec_row.setdefault("cost_model_version", "krx-cost-v2")
        spec_row.setdefault("universe_version", "krx-basket-daily/v2")
        spec_row.setdefault("falsification_tests",
                            hyp.get("falsification_tests"))

        cur_status = normalize_status(str(hyp.get("status") or ""))
        pre = preregister(spec_row)
        report.preregistration = {"ok": pre.ok, "reason": pre.reason,
                                  "fingerprint": pre.fingerprint}
        if not pre.ok:
            # 고정할 것이 없으면 실험하지 않는다 - 등록한 척만 하는 실험은
            # 나중에 무엇을 바꿔도 지문이 같아 검사가 무력하다
            report.verdict = "NOT_PREREGISTERABLE"
            report.backlog.append(f"사전등록 불가: {pre.reason}")
            return report
        if not can_transition(cur_status, "PREREGISTERED"):
            report.verdict = "BAD_TRANSITION"
            report.backlog.append(
                f"{cur_status} -> PREREGISTERED 는 계약 순서를 건너뛴다")
            return report

        cur.execute(
            """update quant.hypotheses
               set status='PREREGISTERED', preregistered_at=now(),
                   status_changed_at=now(), material_fingerprint=%s
               where hypothesis_id=%s""", (pre.fingerprint, hid))
        conn.commit()
        report.transitions.append(f"{cur_status}->PREREGISTERED")

        cur.execute("update quant.hypotheses set status='RUNNING', "
                    "status_changed_at=now() where hypothesis_id=%s", (hid,))
        conn.commit()
        report.transitions.append("PREREGISTERED->RUNNING")

        # ▶ 같은 Family 에서 몇 번째 시도인지 세어 상태 전이에 반영한다.
        #   가설의 edge type + universe 를 Family 로 본다(같은 컨셉의 변형들).
        # ▶ **Family 로 센다**(edge type 이 아니라). 같은 type 이라도 컨셉이
        #   다르면 다른 Family 다 - "SMA20 이탈 후 회귀" 와 "거래대금 급감 후
        #   회귀" 는 둘 다 mean_reversion 이지만 다른 아이디어이고, 하나가
        #   예산을 다 썼다고 다른 하나를 막으면 안 된다.
        #   반대로 파라미터만 바꾼 변형은 같은 Family 다 - 그게 우리가 세려는
        #   다중검정이다.
        from trial_family import family_id, pressure as fam_pressure

        fam = family_id(hyp)
        cards: list[dict] = []
        try:
            # ▶ **기록된 배정을 읽는다**(다시 계산하지 않는다). Family 는
            #   유니버스 서술을 통제 어휘로 사상해 만들므로, 어휘를 늘리면
            #   과거 실험의 배정이 소급 변경되어 "12번째 시도" 가 어제와 오늘
            #   다른 값이 된다. 시도 압력은 기록된 사실이어야 한다.
            cur.execute(
                """select trial_family_id from quant.experiments
                   where trial_family_id is not null""")
            cards = [{"trial_family_id": r[0]} for r in cur.fetchall()]
        except Exception:
            cards = []                   # 못 세면 0 - 없는 압력을 지어내지 않는다
        budget = int(hyp.get("trial_budget") or TRIAL_BUDGET_DEFAULT)
        report.trial_pressure = fam_pressure(fam, cards, budget=budget)
        # ▶ **백테스트에 시도 횟수를 넘긴다.** 안 넘기면 DSR 이 trials=1
        #   로 계산돼 전혀 감가되지 않는다 - 20번 시도해 고른 Sharpe 를
        #   첫 시도와 같은 값으로 읽게 된다(계약만 있고 실행부가 안 따라간
        #   같은 결함이다).
        hyp = dict(hyp)
        hyp['_trials'] = int(report.trial_pressure['trial_number'])


        chain = run_chain or _default_chain
        result = chain(hyp, str(hid))   # {"experiment_id", "fragility": FRAGILE|ROBUST}
        report.experiment_refs = {k: result[k] for k in ("experiment_id", "fragility")
                                  if k in result}
        # ▶ 이 실험의 Family 배정을 남긴다. **한쪽만 쓰지 않는다** - Family 가
        #   없으면 순번도 없다(DB 제약이 강제한다). 없는 순번을 지어내면
        #   다음 실험의 계수가 틀어진다.
        if fam and result.get("experiment_id"):
            cur.execute(
                """update quant.experiments
                      set trial_family_id=%s, trial_number=%s
                    where experiment_id=%s and trial_family_id is null""",
                (fam, int(report.trial_pressure["trial_number"]),
                 result["experiment_id"]))
            conn.commit()

            # ▶ PBO - **고르는 행위 자체가 작동하는가.** trial_pressure 는
            #   몇 번 시도했나를 세고 DSR 은 그만큼 Sharpe 를 깎는다. PBO 는
            #   다른 것을 본다: IS 1등이 OOS 에서도 1등인가. 셋은 서로를
            #   대체하지 않는다.
            #   방금 실험의 창까지 쌓인 뒤에 센다 - 자기 자신을 빼면 안 된다.
            try:
                from pbo_cscv import compute as pbo_compute
                from pbo_cscv import load_family_performance

                pres = pbo_compute(load_family_performance(conn, fam))
                report.trial_pressure.update(pres)
                # ▶ **지표로도 적재한다.** 릴리스 관문은 report 가 아니라
                #   experiment_metrics 에서 pbo 를 읽는다 - 여기 안 넣으면
                #   관문이 늘 None 을 보고 fail-closed 로 전부 HOLD 한다.
                #   (계약·계산은 됐는데 소비처에 안 닿는 같은 결함이다.)
                pv = pres.get("probability_of_backtest_overfitting")
                if pv is not None:
                    # cost_model_version 은 NOT NULL 이다 - 빠뜨리면 적재가
                    # 통째로 죽고 관문은 다시 None 을 본다(실측으로 걸렸다)
                    from backtest_runner import COST_MODEL

                    cur.execute(
                        """insert into quant.experiment_metrics
                             (experiment_id, split, metric, value, dimensions,
                              cost_model_version)
                           values (%s, 'WALK_FORWARD', 'pbo', %s, '{}'::jsonb, %s)
                           on conflict (experiment_id, split, metric, dimensions)
                           do update set value = excluded.value""",
                        (result["experiment_id"], pv, COST_MODEL["version"]))
                    # 표본 수를 함께 남긴다 - 변형 2개짜리 PBO 0.0 을 변형
                    # 20개짜리와 같은 무게로 읽으면 안 된다
                    for k in ("n_variants", "n_splits", "n_windows"):
                        if pres.get(k) is not None:
                            cur.execute(
                                """insert into quant.experiment_metrics
                                     (experiment_id, split, metric, value,
                                      dimensions, cost_model_version)
                                   values (%s, 'WALK_FORWARD', %s, %s,
                                           '{"stat":"pbo"}'::jsonb, %s)
                                   on conflict (experiment_id, split, metric,
                                                dimensions)
                                   do update set value = excluded.value""",
                                (result["experiment_id"], f"pbo_{k}", pres[k],
                                 COST_MODEL["version"]))
                    conn.commit()
            except Exception as e:  # noqa: BLE001
                # 못 재면 None 이다. **0 을 내지 않는다** - 0 은 "과적합 없음"
                # 으로 읽히는데 "안 재봤다" 와 정반대다.
                report.trial_pressure["probability_of_backtest_overfitting"] = None
                report.trial_pressure["pbo_error"] = f"{type(e).__name__}: {e}"[:200]
        # ▶ **실험 후 대조.** 등록 시점과 실질 내용이 다르면 결과를 보고
        #   설정을 바꾼 것이고, 그 실험은 무효다.
        cur.execute("select material_fingerprint from quant.hypotheses "
                    "where hypothesis_id=%s", (hid,))
        registered = (cur.fetchone() or [None])[0]
        vr = prereg_verify(spec_row, registered)
        report.preregistration["verified"] = vr.ok
        if not vr.ok:
            report.preregistration["violation"] = vr.reason
            report.preregistration["changed_fields"] = list(vr.changed_fields)
            # ▶ 환류와 함께 종결한다. 사전등록 위반은 가장 중요한 교훈이라
            #   조용히 상태만 바꾸면 다음 기획안이 같은 실수를 반복한다.
            _finalize_with_feedback(
                conn, report=report, hid=hid, new_status="REJECTED",
                experiment_id=result.get("experiment_id"),
                failed_criteria=["preregistration_violation"],
                lesson_codes=["LEAKAGE_SUSPECT"],
                notes=f"사전등록 위반: {vr.reason}"[:400])
            report.transitions.append("RUNNING->REJECTED (사전등록 위반)")
            return report

        new_status = robustness_to_status(result["fragility"],
                                          report.trial_pressure)
        # ▶ **환류 적재와 상태 전이를 한 트랜잭션으로.** 적재가 실패하면 상태도
        #   안 바뀐다 - 조용히 종결되고 교훈만 사라지는 경로를 없앤다.
        _finalize_with_feedback(
            conn, report=report, hid=hid, new_status=new_status,
            experiment_id=result.get("experiment_id"),
            fragility=result.get("fragility", ""))
        report.transitions.append(f"RUNNING->{new_status}")

        # ▶ QNT-07 릴리스 관문. SUPPORTED 까지 온 것만 본다.
        #   **승격이 아니라 제출 판정이다** - Production 은 CEO·Risk·QA 몫이다.
        if new_status == "SUPPORTED":
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from release_gate import evaluate as gate_evaluate

                cur.execute(
                    """select metric, value from quant.experiment_metrics
                       where experiment_id = %s""",
                    (result.get("experiment_id"),))
                metrics = {m: float(v) for m, v in cur.fetchall()}
                # walk_forward 가 쓰는 이름과 맞춘다
                metrics.setdefault("max_drawdown_pct", metrics.get("mdd_pct"))
                d = gate_evaluate(metrics, fragility=result["fragility"],
                                  trial_pressure=report.trial_pressure)
                report.release = d.as_dict()

                # ▶ 관문 다음 칸. **승격이 아니라 요청이다** - 좋은 백테스트가
                #   바로 운영 전략이 되지 않게 Shadow 부터 밟는다.
                from strategy_lifecycle import evaluate_promotion

                lc = evaluate_promotion(
                    str(hyp.get("title") or hid)[:40],
                    current_state="RESEARCH",
                    gate_decision=d.decision)
                report.lifecycle = lc.as_dict()
            except Exception as e:  # noqa: BLE001
                # 관문 실패를 통과로 위장하지 않는다 - HOLD 가 안전한 기본값이다
                report.release = {"decision": "HOLD",
                                  "reasons": [f"관문 실행 실패: {type(e).__name__}"]}
        return report
    finally:
        if own_conn:
            conn.close()


def _default_chain(hyp: dict, hypothesis_id: str | None = None) -> dict:
    """실전 체인: 백테스트(가설 바인딩) + walk-forward 강건성 -> 판정.

    edge type -> 전략 config 매핑은 카탈로그가 정하고, 강건성 지표는
    walk_forward 의 조각(make_windows/run_window/fragility_summary)을
    같은 config 로 재사용한다 - 검증 규칙을 두 벌 만들지 않는다.
    """
    import psycopg2
    from backtest_runner import (
        DEFAULT_CONFIG,
        REV_CONFIG,
        Market,
        load_dataset,
        register_and_run,
    )
    from source_registry import load_project_env
    from walk_forward import (
        WARMUP_TRADING_DAYS,
        fragility_summary,
        make_windows,
        run_window,
        slice_market,
    )

    edge = ((hyp.get("expected_edge") or {}).get("type") or "").lower()
    # ▶ **가설을 config 에 바인딩한다.** 예전엔 edge type -> 고정 config 라
    #   가설이 무엇이든 같은 실험이 됐고(input_hash 가 config 를 포함한다),
    #   두 번째부터 전부 "중복 실험" 으로 막혔다. 그러면 사전등록 지문에
    #   holding_horizon 을 넣어도 실제 백테스트가 그 값을 안 써서 **고정한
    #   것과 실행한 것이 달라진다** - 관문이 형식만 남는다.
    from config_binding import bind

    base = {"momentum": DEFAULT_CONFIG, "mean_reversion": REV_CONFIG}[edge]
    binding = bind(hyp, base)
    if not binding.ok:
        # 범위 밖 값을 잘라 쓰지 않는다 - 자르면 등록한 것과 실행한 것이
        # 달라져 같은 문제가 다시 생긴다
        raise RuntimeError("가설 파라미터 거부: " + "; ".join(binding.rejected))
    config = binding.config

    print(f"  config 바인딩: 가설 {binding.from_hypothesis or '-'} / "
          f"기본값 {binding.from_default or '-'}", flush=True)
    # ▶ 시도 횟수를 넘긴다 - DSR 이 감가하려면 알아야 한다. **config 가 아니라
    #   인자로** 넘긴다(config 는 input_hash 에 들어가므로 넣으면 중복 가드가
    #   무력해진다).
    bt = register_and_run(DATASET_NAME, DATASET_VERSION,
                          config=config, hypothesis_id=hypothesis_id,
                          trials=int(hyp.get("_trials") or 1))
    if bt.get("duplicate"):
        # 같은 (가설, 데이터, 코드) 실험이 이미 있다 - 다시 돌리지 않고 기존
        # 실험의 강건성 판정을 찾아 쓴다. 여기 없으면 판정 불가로 끊는다.
        raise RuntimeError(f"중복 실험({bt.get('experiment_id')}) - 기존 판정을 "
                           f"수동 확인할 것 (자동 재판정은 결과 조작 여지가 있다)")

    # 강건성: 같은 config 로 창별 재실행 (walk_forward 조각 재사용)
    conn = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)
    try:
        _, _, _, rows = load_dataset(conn, DATASET_NAME, DATASET_VERSION)
        market = Market.from_rows(rows)
        # ▶ **embargo = 보유 지평.** 웜업 마지막 시그널이 그만큼 미래로
        #   이어지므로 그 구간을 평가에서 뺀다 - 안 빼면 직전 구간 정보가
        #   성적에 섞인다.
        windows = make_windows(market.dates, WARMUP_TRADING_DAYS,
                               embargo_days=int(config.get("lookback_days") or 0))
        wm = [(w.label, run_window(slice_market(market, w), w, dict(config)))
              for w in windows]
        _summary, flags, verdict = fragility_summary(wm)
        with conn.cursor() as cur:
            for label, metrics in wm:
                for k in ("total_return", "sharpe_rf0", "max_drawdown"):
                    if isinstance(metrics.get(k), (int, float)):
                        cur.execute("""
                            insert into quant.experiment_metrics
                              (experiment_id, split, metric, value,
                               dimensions, cost_model_version)
                            values (%s, 'WALK_FORWARD', %s, %s, %s::jsonb, %s)
                            on conflict do nothing
                        """, (bt["experiment_id"], k, metrics[k],
                              json.dumps({"window": label, "chain": ORCH_VERSION}),
                              "krx-cost-v1"))
        conn.commit()
    finally:
        conn.close()

    return {"experiment_id": bt["experiment_id"], "fragility": verdict,
            "fragility_flags": flags, "windows": len(wm),
            "backtest_metrics": bt.get("metrics")}


def _print_report(r: OrchestratorReport) -> None:
    print(f"{ORCH_VERSION}: {r.verdict}")
    print(f"  가설: {r.title[:60]} ({r.hypothesis_id[:8]}…)")
    if r.missing:
        print(f"  부족: {', '.join(r.missing)}")
    for b in r.backlog:
        print(f"  백로그: {b}")
    if r.lifecycle:
        lc = r.lifecycle
        print(f"  생명주기: {lc.get('from_state')} -> "
              f"{lc.get('to_state') or '-'} 요청 "
              f"(승인: {lc.get('needs_approval_from') or '-'})")
    if r.release:
        print(f"  릴리스: {r.release.get('decision')} "
              f"(미달 {r.release.get('failed') or '-'})")
        for msg in (r.release.get("reasons") or [])[:3]:
            print(f"    · {msg}")
    for t in r.transitions:
        print(f"  전이: {t}")
    if r.experiment_refs:
        print(f"  실험: {r.experiment_refs}")


# ---------------------------------------------------------------------------
# 자체 점검 - DB 없음 (가짜 커서·체인 주입)
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, hypothesis_row, datasets):
        self._row = hypothesis_row
        self._datasets = datasets
        self.updates: list = []

    def execute(self, sql, params=()):
        self._last = (sql, params)
        if "update quant.hypotheses" in sql:
            self.updates.append(params)
            # ▶ 지문을 기억한다. 실제 DB 는 UPDATE 한 값을 되읽을 수 있으므로
            #   가짜도 그래야 사전등록 대조가 진짜 경로를 검사한다 - 안 그러면
            #   늘 "위반" 이 나와 검사가 가드를 잘못 고발한다.
            if "material_fingerprint" in sql and params:
                self._fingerprint = params[0]

    def fetchone(self):
        if "material_fingerprint from quant.hypotheses" in getattr(
                self, "_last", ("", ()))[0]:
            return (getattr(self, "_fingerprint", None),)
        return self._row

    def fetchall(self):
        return [(d,) for d in self._datasets]


class _FakeConn:
    def __init__(self, cursor):
        self._cur = cursor
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1


def _check_every_status_update_touches_timestamp():
    """**상태를 바꾸면서 전이 시각을 안 쓰면 그 실험은 영원히 멈춘 것으로 보인다.**

    /jobs/stuck 이 status_changed_at 으로 멈춘 작업을 찾으므로, 갱신을
    빠뜨린 경로가 하나라도 있으면 그 상태로 들어간 실험은 계속 경보를 낸다.
    """
    import re

    src = __import__("pathlib").Path(__file__).read_text(encoding="utf-8")
    # 검사 자신의 정규식 리터럴은 제외하고 실제 SQL 문자열만 본다
    for m in re.finditer(r'"update quant\.hypotheses[^"]*"(?:\s*"[^"]*")*', src):
        stmt = m.group(0)
        if "status=" not in stmt and "set status" not in stmt:
            continue
        assert "status_changed_at" in stmt,             f"전이 시각을 안 쓰는 UPDATE: {stmt[:80]}"


def _check_feasibility_gate():
    ds = {"krx-basket-daily/v1"}
    ok, missing, _ = feasibility(
        {"expected_edge": {"type": "momentum"},
         "required_data_products": ["krx-basket-daily/v1"]}, ds)
    assert ok and not missing
    # mean_reversion 은 2026-08-01 REV-5 구현으로 RUNNABLE 이 됐다 (백로그 이행)
    ok_rev, _, _ = feasibility(
        {"expected_edge": {"type": "mean_reversion"},
         "required_data_products": ["krx-basket-daily/v1"]}, ds)
    assert ok_rev, "REV-5 구현 후에도 mean_reversion 이 막혀 있다"
    # 미구현 전략 -> NOT_RUNNABLE (카탈로그에 없는 가상 전략으로 검증)
    ok2, missing2, backlog2 = feasibility(
        {"expected_edge": {"type": "pairs_trading"},
         "required_data_products": ["krx-basket-daily/v1"]}, ds)
    assert not ok2 and "strategy_impl:pairs_trading" in missing2
    assert any("pairs_trading" in b for b in backlog2)
    # 없는 데이터셋 -> NOT_RUNNABLE
    ok3, missing3, _ = feasibility(
        {"expected_edge": {"type": "momentum"},
         "required_data_products": ["us-daily/v1"]}, ds)
    assert not ok3 and "dataset:us-daily/v1" in missing3
    # 스펙 자체가 비면 둘 다 잡힌다
    ok4, missing4, _ = feasibility({}, ds)
    assert not ok4 and "dataset:(미지정)" in missing4 and "edge_type:(미지정)" in missing4
    print("  실험 가능성 게이트       OK")


def _check_status_mapping():
    assert robustness_to_status("FRAGILE") == "REJECTED"
    assert robustness_to_status("ROBUST") == "SUPPORTED"
    # ▶ **예산을 넘긴 Family 의 ROBUST 는 SUPPORTED 가 아니다.**
    #   틀렸다는 뜻이 아니라 이 표본으로는 실력과 운을 못 가린다는 뜻이다.
    over = {"trials_used": 12, "trial_budget": 5, "over_budget": True}
    assert robustness_to_status("ROBUST", over) == "INCONCLUSIVE"
    # FRAGILE 은 시도 수와 무관하게 REJECTED (많이 돌렸다고 구제되지 않는다)
    assert robustness_to_status("FRAGILE", over) == "REJECTED"
    ok = {"trials_used": 2, "trial_budget": 5, "over_budget": False}
    assert robustness_to_status("ROBUST", ok) == "SUPPORTED"
    for bad in ("", "MAYBE", None):
        try:
            robustness_to_status(bad)
            raise AssertionError(f"{bad!r} 가 상태로 옮겨졌다")
        except ValueError:
            pass
    print("  강건성->상태 매핑        OK")


def _check_orchestrate_paths():
    row = ("h-1", "미구현 엣지 가설", {"type": "pairs_trading", "horizon_days": 5},
           ["krx-basket-daily/v1"], "PROPOSED")
    cur = _FakeCursor(row, ["krx-basket-daily/v1"])
    r = orchestrate("h-1", conn=_FakeConn(cur))
    assert r.verdict == "NOT_RUNNABLE" and not cur.updates, \
        "NOT_RUNNABLE 인데 상태를 건드렸다"

    row2 = ("h-2", "모멘텀 가설", {"type": "momentum"},
            ["krx-basket-daily/v1"], "PROPOSED")
    cur2 = _FakeCursor(row2, ["krx-basket-daily/v1"])
    r2 = orchestrate("h-2", conn=_FakeConn(cur2),
                     run_chain=lambda h, hid: {"experiment_id": "e-1",
                                               "fragility": "FRAGILE"})
    assert r2.verdict == "RUNNABLE", (r2.verdict, r2.backlog)
    # ▶ **사전등록이 전이에 끼어든다.** 결과를 보기 전에 실질 내용을 고정하고
    #   실험 뒤 대조한다 - 그 두 단계가 안 보이면 관문이 없는 것이다.
    assert r2.transitions == ["INTAKE->PREREGISTERED",
                              "PREREGISTERED->RUNNING",
                              "RUNNING->REJECTED"], r2.transitions
    assert r2.preregistration["ok"] is True, r2.preregistration
    assert r2.preregistration["fingerprint"], r2.preregistration
    assert r2.preregistration["verified"] is True, r2.preregistration

    cur3 = _FakeCursor(row2, ["krx-basket-daily/v1"])
    r3 = orchestrate("h-2", conn=_FakeConn(cur3),
                     run_chain=lambda h, hid: {"experiment_id": "e-2",
                                               "fragility": "ROBUST"})
    assert r3.transitions[-1] == "RUNNING->SUPPORTED", r3.transitions

    cur4 = _FakeCursor(None, [])
    assert orchestrate("none", conn=_FakeConn(cur4)).verdict == "NO_HYPOTHESIS"
    print("  오케스트레이션 경로       OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        a = sys.argv
        hid = a[a.index("--hypothesis") + 1] if "--hypothesis" in a else None
        _print_report(orchestrate(hid))
        raise SystemExit(0)

    print(f"{ORCH_VERSION} 자체 점검 (DB 없음)")
    _check_every_status_update_touches_timestamp()
    print("  전이 시각 누락 없음      OK")
    _check_feasibility_gate()
    _check_status_mapping()
    _check_orchestrate_paths()
    print("오케스트레이터 3개 영역 통과. 실행은 --run [--hypothesis <id>]")
