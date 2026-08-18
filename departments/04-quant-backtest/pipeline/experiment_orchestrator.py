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

import gc
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Research's shared source_registry lives in the canonical /app/repo mirror in
# production; the old parents[2]/01-research path only works in a different
# checkout layout. Keep both layouts deterministic so the execution surface can
# actually reach the source contract before attempting the experiment.
for _research_collectors in (
    Path("/app/repo/departments/01-research/collectors"),
    Path(__file__).resolve().parents[2] / "01-research" / "collectors",
):
    if _research_collectors.is_dir():
        sys.path.insert(0, str(_research_collectors))

ORCH_VERSION = "quant-experiment-orchestrator-v2"
TRIAL_RESERVATION_KEY = "_trial_family_reservation_v1"

from strategy_templates import (           # noqa: E402  (같은 디렉터리 모듈)
    NOT_IMPLEMENTED,
    TEMPLATES,
    resolve,
    template_for_edge,
)

# 구현된 전략 카탈로그. **실체는 strategy_templates.TEMPLATES 이고 여기는 파생
# 뷰다** - backtest_runner.STRATEGIES 가 이미 같은 방식으로 파생돼 있다.
#
# ▶ 왜 파생으로 바꿨나 (2026-08-12 실측)
#   여기는 원래 `momentum`·`mean_reversion` 둘만 적힌 손글씨 표였다. 그런데
#   실행면(TEMPLATES)에는 그때 이미 8개가 구현돼 있었다 - LOWVOL·RAMOM·LIQREV·
#   BRK·TREND·ILLIQ 까지. 그래서 `low_volatility` 가설이 **구현이 있는데도**
#   `'low_volatility' 전략 구현 (STRATEGY_CATALOG 등재 조건)` 으로 반려됐다.
#   2026-08-11 이후 백테스트가 한 건도 안 돈 이유의 절반이 이것이다.
#
#   같은 부서 안에서 표가 둘로 갈리면 접수·판정·실행이 서로 다른 것을 본다
#   (trial_family 이름공간 사고와 같은 계열). 손글씨 표를 지우고 실행면 하나만
#   남긴다 - 전략을 구현하면 카탈로그는 저절로 따라온다.
STRATEGY_CATALOG: dict[str, dict] = {
    t.edge_type: {
        "strategy_code": t.template_id,
        "impl": "pipeline/strategy_templates.py (TEMPLATES) + backtest_runner.py",
        "note": t.note,
        "claimed_edge": t.claimed_edge,
    }
    for t in TEMPLATES.values()
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
    regime_evidence: list = field(default_factory=list)
    data_feasibility: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 게이트 (순수 함수 - 자체점검 대상)
# ---------------------------------------------------------------------------

def base_config_for(edge: str, default_config: dict, rev_config: dict) -> dict:
    """edge_type -> 백테스트 기본 config. 가설 파라미터는 bind() 가 이 위에 얹는다.

    ▶ 왜 함수인가 (2026-08-12 실측)
      여기는 `{"momentum": …, "mean_reversion": …}[edge]` 였다. 카탈로그를
      TEMPLATES 파생으로 바꿔 `low_volatility` 가 게이트를 통과하자, 그 다음
      줄에서 **`KeyError: 'low_volatility'`** 로 죽었다 - 같은 부서 안에
      edge_type 으로 색인하는 표가 하나 더 있었던 것이다. 관문을 넓히면
      **그 뒤의 모든 표가 같이 넓어져야 한다.**

    ▶ 알려진 둘은 값을 그대로 둔다
      config 는 `input_hash` 에 들어간다. momentum/mean_reversion 의 기본값을
      지금 손보면 8월 4~10일 실험과 지문이 갈려 중복 판정이 어긋난다.
      과거를 재현할 수 있어야 하므로 새 edge 만 템플릿에서 유도한다.
    """
    known = {"momentum": default_config, "mean_reversion": rev_config}.get(edge)
    if known is not None:
        return known

    tpl = template_for_edge(edge)
    if tpl is None:
        why = NOT_IMPLEMENTED.get(edge)
        raise RuntimeError(
            f"'{edge}' 에 해당하는 시그널 템플릿이 없다"
            + (f" - {why}" if why else f" (사용 가능: {sorted(STRATEGY_CATALOG)})"))

    # 리밸런스는 여기서 정하지 않는다 - config_binding.REBALANCE_BY_HORIZON 이
    # 가설의 horizon_days 로 덮는다. 여기 값은 horizon 이 없을 때의 바닥이다.
    lookback = 20
    return {
        "strategy": f"{tpl.template_id}-{lookback}-SMOKE",
        "lookback_days": lookback,
        "top_n": 20,
        "rebalance": "MONTH_FIRST_TRADING_DAY",
        "initial_capital": 100_000_000.0,
    }


def signal_horizon(config: dict) -> int:
    """IC forward horizon: 사전등록 지평을 형성창보다 우선한다.

    `lookback_days` is the feature formation window.  Short-lived microstructure
    hypotheses can carry their forecast horizon separately; when both keys are
    present, using the lookback silently tests a different claim and can leave too
    few non-overlapping observations to measure.
    """
    for key in ("horizon_days", "holding_horizon", "lookback_days"):
        raw = config.get(key)
        if raw is None:
            continue
        try:
            horizon = int(raw)
        except (TypeError, ValueError):
            continue
        if horizon >= 1:
            return horizon
    return 20


def _verified_frozen_daily_windows(*, frozen_plan: dict,
                                   dataset_content_hash: str,
                                   dates: list,
                                   warmup_days: int,
                                   embargo_days: int,
                                   cost_model: dict):
    """Decode only a preregistered plan after an independent recalculation.

    The second construction is a drift detector, not the plan used for metric
    writes.  Exact equality means the immutable dataset/calendar, code, cost,
    purge, and stock-scope facts still produce the plan inserted before the
    backtest.  Metrics always iterate the separately decoded frozen windows.
    """

    from walk_forward import (  # noqa: PLC0415
        freeze_daily_evaluation_plan,
        windows_from_frozen_daily_plan,
    )

    if not isinstance(frozen_plan, dict):
        raise RuntimeError("daily experiment lacks a frozen evaluation plan")
    frozen_windows = windows_from_frozen_daily_plan(dict(frozen_plan))
    recalculated = freeze_daily_evaluation_plan(
        dataset_content_hash=dataset_content_hash,
        dates=list(dates),
        warmup_days=int(warmup_days),
        embargo_days=int(embargo_days),
        cost_model=dict(cost_model),
    )
    if (recalculated.get("evaluation_plan_fingerprint") !=
            frozen_plan.get("evaluation_plan_fingerprint")):
        raise RuntimeError(
            "daily evaluation plan drift: independent fingerprint mismatch")
    if recalculated != frozen_plan:
        raise RuntimeError(
            "daily evaluation plan drift: canonical plan mismatch")
    return frozen_windows

def dataset_of(hyp: dict) -> tuple[str, str]:
    """가설이 쓸 (데이터셋 이름, 버전). **상수가 아니라 사상 결과에서 나온다.**

    `orchestrate` 가 `data_resolution.resolve` 결과를 `required_data_products`
    에 넣어 두므로 여기서는 그것을 읽기만 한다. 값이 없거나 모양이 이상하면
    모듈 상수로 떨어진다 - **조용히 다른 데이터로 돌지 않기 위해서**다.
    """
    datasets: list[tuple[str, str]] = []
    for d in (hyp.get("required_data_products") or []):
        s = str(d)
        if "/" in s:
            name, _, ver = s.rpartition("/")
            if name and ver:
                datasets.append((name, ver))
    # The backtester always builds its execution market from daily OHLCV.
    # Feature-only datasets (for example microstructure) are attached later.
    # Prefer the price dataset even if resolution returned feature data first.
    for name, ver in datasets:
        if name == DATASET_NAME:
            return name, ver
    if datasets:
        return datasets[0]
    return DATASET_NAME, DATASET_VERSION


def execution_data_products(products) -> list:
    """Add the daily price primitive required by every executable strategy."""
    out = list(products or [])
    has_bars = any(
        str(product) == "market_bars"
        or str(product).startswith(f"{DATASET_NAME}/")
        for product in out
    )
    return out if has_bars else ["market_bars", *out]


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
        # 왜 없는지를 구분해 적는다. `NOT_IMPLEMENTED` 는 "요청은 있으나 실행면에
        # 없다"를 사유와 함께 관리하는 표다 - 그 사유를 그대로 실어야 기획자가
        # 같은 이름을 다시 내지 않는다. 표에도 없으면 어휘 자체가 틀린 것이라
        # 쓸 수 있는 목록을 보여 준다(없는 이름을 지어낸 쪽을 고쳐야 한다).
        why = NOT_IMPLEMENTED.get(edge)
        backlog.append(
            f"'{edge}' 미구현: {why}" if why
            else f"'{edge}' 는 어휘에 없다 - 사용 가능: {sorted(catalog)}"
        )
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
def walk_forward_efficiency(window_metrics: list, full_sharpe) -> float | None:
    """**성적이 전 구간에 고르게 있었나, 한쪽에 몰렸나** (2026-08-14 조사).

    ▶ 무엇을 재나
      업계 관례의 Walk-Forward Efficiency 는 `OOS Sharpe / IS Sharpe` 이고
      0.5~0.7 이 현실적 목표, 1.0 초과는 오히려 의심 신호다. 그런데 우리는
      창마다 파라미터를 **최적화하지 않고** 고정값으로 돌리므로 그대로는 못
      쓴다. 같은 질문에 답하는 우리 판은 이것이다:

          창별 Sharpe 평균 / 전기간 Sharpe

      전기간이 좋은데 창별 평균이 나쁘면 그 성적은 **특정 구간에 몰린 것**
      이고, 둘이 비슷하면 구간을 가로질러 살아 있다는 뜻이다.

    ▶ 왜 관문이 아니라 계측인가
      지금 강건성은 `positive_window_ratio` 하나로 FRAGILE 만 찍고 **왜**
      취약한지는 말하지 못한다(실측: 전체 판정의 87%가 fragility). 임계를
      바꾸는 것은 합격선 변경이라 사전등록·결재 사안이고, 여기서는 다음
      기획이 방향을 잡도록 **이유를 재서 환류에 실을 뿐**이다.

    ▶ 못 재면 안 적는다
      전기간 Sharpe 가 0 근처면 비율이 폭발한다 - 그건 진단이 아니라 잡음이다.
      분모가 작으면 `None` 을 돌려주고 원장에 키를 만들지 않는다.
    """
    try:
        denom = float(full_sharpe)
    except (TypeError, ValueError):
        return None
    if not (abs(denom) >= 0.1):
        return None
    vals = [m.get("sharpe_rf0") for _, m in (window_metrics or [])]
    nums = [float(v) for v in vals if isinstance(v, (int, float))]
    if len(nums) < 2:
        return None
    return round((sum(nums) / len(nums)) / denom, 4)


def experiment_did_not_trade(metrics: dict | None) -> bool:
    """**한 주도 못 샀으면 전략을 잰 것이 아니다** (2026-08-14 실측).

    ▶ 무엇이 있었나
      `min_adv_krw` 단위가 어긋나(원 vs 백만원) 체결가능 유니버스가 통째로
      비었다. 백테스트는 죽지 않고 완주했고 지표는 전부 0 - total_return 0,
      turnover_total 0, MDD 0 - 인데 벤치마크만 올라 초과가 -82.86%p 로
      찍혔다. 관문은 이것을 **REJECT** 로 판정했다. 즉 "실험이 성립하지
      않았다" 를 "전략이 나쁘다" 로 기록한 것이다.

      단위는 고쳤지만 **같은 증상은 다른 원인으로도 온다** - 데이터 결측,
      필터 과다, 신호가 한 종목도 못 고른 경우. 그때마다 가설이 억울하게
      기각되고, 그 기각은 계열 예산(trial pressure)까지 태운다.

    ▶ 판정 불가는 판정 결과다
      `fragility_summary` 가 창 0개를 INSUFFICIENT 로 돌려주는 것과 같은
      원칙이다. 여기서도 기각이 아니라 INCONCLUSIVE 로 보내고
      UNDERPOWERED_DATA 를 리서치에 돌려준다.

    ▶ 못 잰 것과 0 을 구분한다
      `turnover_total` 이 아예 없으면(구버전 지표) 판단하지 않는다 -
      없는 것을 사고로 몰면 옛 실험이 무더기로 무효가 된다.
    """
    m = metrics or {}
    turnover = m.get("turnover_total")
    if turnover is None:
        return False
    try:
        if float(turnover) != 0.0:
            return False
    except (TypeError, ValueError):
        return False
    ret = m.get("total_return")
    if ret is None:
        return True
    try:
        return float(ret) == 0.0
    except (TypeError, ValueError):
        return True


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
    if v == "INSUFFICIENT":
        # 창이 0개면 강건성을 잰 것이 아니다. 기각도 지지도 아니고 **판정 불가**다 -
        # 여기서 REJECTED 로 밀면 데이터가 모자란 것을 가설의 죄로 기록하게 된다.
        return "INCONCLUSIVE"
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
def release_to_status(provisional_status: str, decision: str | None, *,
                      failed=(), unmeasured=()) -> str:
    """Overlay the deterministic release gate on a robustness verdict.

    Robustness is necessary but never sufficient for a durable QA submission.
    A measured release failure rejects the hypothesis; missing measurements or
    a gate execution failure keep it inconclusive.  Only a clean, explicit
    ``SUBMIT_TO_QA`` may retain ``SUPPORTED``.
    """

    provisional = str(provisional_status or "").upper()
    if provisional != "SUPPORTED":
        return provisional

    failed_set = {str(item) for item in (failed or ())}
    unmeasured_set = {str(item) for item in (unmeasured or ())}
    measured_failures = failed_set - unmeasured_set
    if measured_failures:
        return "REJECTED"
    if (str(decision or "").upper() == "SUBMIT_TO_QA"
            and not failed_set and not unmeasured_set):
        return "SUPPORTED"
    return "INCONCLUSIVE"


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
#
# ▶ **화이트리스트가 계측을 잘라 왔다** (2026-08-14 배선 조사)
#   랭크-IC(signal_ic*)와 회전율(turnover_total)은 이미 quant.experiment_metrics
#   에 적재되고 환류 SQL 도 그 행을 읽어 오는데, 이 튜플에 이름이 없어서
#   **outcomes 에 한 번도 실린 적이 없었다.** 측정은 되는데 원장에 안 남으면
#   다음 기획안은 그 사실을 못 본다 - "측정되는데 저장이 죽으면 그 측정은
#   없는 것" 과 같은 사고다.
#
# ▶ 여기 이름을 더해도 **판정은 바뀌지 않는다**: release_gate.evaluate 는
#   이름 지정된 8개 키만 검사하고, 이 튜플은 환류(기록) 전용이다. 다만
#   factory_bridge.lessons_from 이 excess_return_pct·information_ratio 두
#   이름을 읽으므로 신규 키는 반드시 새 이름을 쓴다.
#
# ▶ 두 적재 경로가 이 한 튜플을 공유한다(orphan_finalizer 가 import) -
#   정상 종결과 고아 완주가 같은 계측을 남긴다.
_OOS_KEYS = (
    # 관문이 보는 지표 (기존)
    "excess_return_pct", "information_ratio", "max_drawdown_pct",
    "deflated_sharpe", "pbo", "bootstrap_ci_low", "bootstrap_ci_high",
    # 위험조정 비교 계측 (2026-08-14) - 명목 초과가 vol 차이에 오염되는 것을
    # 원장이 스스로 보이게 한다(leverage bias). backtest_runner.excess_metrics.
    "m2_excess_ann_pct", "alpha_ann_pct", "appraisal_ratio",
    "strategy_ann_vol_pct", "benchmark_ann_vol_pct",
    "beta_vs_benchmark", "corr_vs_benchmark", "residual_ann_vol_pct",
    # 부품 채점표 (2026-08-14) - 단일 신호를 부품으로 채점할 때 쓰는 축.
    # 전부 이미 적재돼 있던 값이다(새 계산 없음).
    "turnover_total", "turnover",
    "signal_ic", "signal_ic_t", "signal_ic_hit_rate", "signal_ic_periods",
    "signal_ic_breadth", "pnl_top1_share", "pnl_top3_share",
    # 강건성 **이유** (2026-08-14) - 전체 판정의 87%가 fragility 인데 왜
    # 취약한지는 원장이 말하지 못했다. 아래 넷은 판정 재료가 아니라 진단이다.
    #   walk_forward_efficiency: 창별 Sharpe 평균 / 전기간 Sharpe (0.5~0.7 정상)
    #   positive_window_ratio·worst_window_mdd·sharpe_std: 관문이 실제로 보는 값
    "walk_forward_efficiency", "positive_window_ratio",
    "worst_window_mdd", "sharpe_std",
    # Intraday event-time lane.  Session counts, not overlapping ticks, are the
    # independent evidence unit.
    "mean_mid_markout_bps", "mean_implementation_drag_bps",
    "mean_net_bps_per_opportunity", "fill_rate", "sessions", "instruments",
    "positive_fold_ratio", "session_net_ci_low_bps", "session_net_ci_high_bps",
    "mean_capacity_shares_l1", "p10_capacity_shares_l1",
    "max_concurrent_opportunities",
)


def _gate_note(decision, metrics: dict) -> str:
    """관문 판정 -> 다음 기획안이 읽을 한 줄. **거리를 적는다.**

    ▶ 왜 "REJECTED" 만으로는 부족한가 (2026-08-12)
      momentum 이 초과 +157.51%p · IR 1.26 · DSR 0.976 을 내고 기각됐다.
      환류에 남은 건 `fragility_fragile` 뿐이라, 리서치는 **엣지가 없었다**고
      읽고 다른 엣지를 설계했다. 실제로는 엣지가 있었고 낙폭이 문제였다.
      "6개 통과, MDD 하나 남음" 과 "기각" 은 다음 설계를 완전히 다르게 만든다.
    """
    from release_gate import CRITERIA

    passed = list(getattr(decision, "passed", ()) or ())
    failed = list(getattr(decision, "failed", ()) or ())
    total = len(passed) + len(failed)
    if not total:
        return ""

    # 조항별 거리. 미확인은 거리가 아니라 **측정 부재**로 적는다 - 둘을 섞으면
    # "조금 모자랐다" 와 "재보지도 않았다" 가 같은 문장이 된다.
    gaps: list[str] = []
    _spec = (
        ("excess_return", "excess_return_pct", "min_excess_return_pct", "%p", 1),
        ("information_ratio", "information_ratio", "min_information_ratio", "", 1),
        ("max_drawdown", "max_drawdown_pct", "max_drawdown_pct", "%", -1),
        ("turnover", "turnover", "max_turnover", "x", -1),
        ("deflated_sharpe", "deflated_sharpe", "min_deflated_sharpe", "", 1),
        ("pbo", "pbo", "max_pbo", "", -1),
    )
    for name, mkey, ckey, unit, sign in _spec:
        if name not in failed:
            continue
        v = metrics.get(mkey)
        if v is None:
            # ▶ **못 재는 이유가 곧 다음 할 일이다.** PBO 는 계열에 변형이
            #   둘 이상이어야 정의된다("IS 1등이 OOS 에서도 1등인가" 이므로
            #   고를 것이 하나면 물음 자체가 성립하지 않는다). 같은 계열의
            #   두 번째 변형을 돌리면 그때 잰다 - 위험관리를 붙인 변형이
            #   낙폭과 PBO 를 **한 번에** 푼다.
            gaps.append("pbo 미측정 (계열 변형 1개 - 같은 계열 두 번째 변형이 "
                        "돌면 잰다)" if name == "pbo" else f"{name} 미측정")
            continue
        lim = CRITERIA[ckey]
        gaps.append(f"{name} {v:.2f}{unit} (기준 {lim}{unit}, "
                    f"{abs(v - lim):.2f}{unit} 모자람)")
    for name in ("fragility", "bootstrap_ci", "trial_pressure"):
        if name in failed:
            if name == "bootstrap_ci":
                lo = metrics.get("bootstrap_ci_low")
                gaps.append(f"bootstrap_ci 하한 {lo}" if lo is not None
                            else "bootstrap_ci 미측정")
            else:
                gaps.append(name)

    # ▶ 계측 꼬리 (2026-08-14). **판정에 관여하지 않는다** - 다음 기획안이
    #   "명목 초과가 나빴는데 위험조정으로는 어땠나" 를 볼 수 있게 하는 사실
    #   줄이다. vol 타게팅을 단 실험이 명목 초과 −100%p 로 죽는 동안 M² 는
    #   전혀 다른 이야기를 할 수 있고, 그 차이가 곧 관문 재설계의 근거다.
    extra = []
    for label, key, unit, digits in (
            ("M²", "m2_excess_ann_pct", "%p", 1),
            ("α", "alpha_ann_pct", "%p", 1),
            ("AR", "appraisal_ratio", "", 2),
            ("전략vol", "strategy_ann_vol_pct", "%", 0),
            ("벤치vol", "benchmark_ann_vol_pct", "%", 0),
            ("IC t", "signal_ic_t", "", 2),
            ("회전", "turnover_total", "x", 1)):
        v = metrics.get(key)
        if v is None:
            continue
        try:
            extra.append(f"{label} {float(v):.{digits}f}{unit}")
        except (TypeError, ValueError):
            continue
    tail = (" || 계측: " + " · ".join(extra)) if extra else ""

    head = f"관문 {len(passed)}/{total} 통과"
    if not failed:
        return head + " - 남은 조항 없음" + tail
    return f"{head}. 남은 조항: " + "; ".join(gaps[:6]) + tail


def _finalize_with_feedback(conn, *, report, hid: str, new_status: str,
                            experiment_id, failed_criteria=None,
                            lesson_codes=None, fragility: str = "",
                            gate_failed=None, regime_evidence=None,
                            notes: str = "") -> None:
    """상태 전이를 **환류와 함께** 커밋한다.

    ▶ 왜 여기서 실패를 삼키지 않나
      환류를 못 적재하면 그 실험의 교훈은 영영 Gate 0 에 닿지 않고, 회사는 같은
      실험을 다시 산다. 조용히 상태만 바꾸느니 실험을 미종결로 두고 사람이 보는
      편이 낫다 - 그래서 예외를 잡지 않는다(fail-closed).
    """
    from factory_bridge import (
        assert_governed_stock_experiment,
        build_outcome,
        finalize,
        lessons_from,
    )

    tp = report.trial_pressure or {}
    exp_id = str(experiment_id or "")
    oos: dict = {}
    if exp_id:
        # 이 파일의 관례를 따라 컨텍스트 매니저를 쓰지 않는다(자체 점검의 가짜 커서 호환)
        cur = conn.cursor()
        cur.execute("""select metric, value from quant.experiment_metrics
                        where experiment_id = %s
                          and coalesce(dimensions->>'window','') in ('','SUMMARY')
                          and dimensions->>'regime' is null
                          and dimensions->>'screening_candidate' is null""",
                    (exp_id,))
        # (metric, value) 2-튜플이 아닌 행은 지표 행이 아니다 - 조용히 건너뛴다
        found = {r[0]: float(r[1]) for r in (cur.fetchall() or [])
                 if isinstance(r, (list, tuple)) and len(r) == 2}
        # ▶ 저장 이름과 관문 어휘를 여기서도 맞춘다 (2026-08-12).
        #   `max_drawdown_pct` 는 저장된 적이 없는 이름이라 **환류 요약에서
        #   낙폭이 통째로 빠져 있었다** - 관문과 똑같은 결함이 두 번째 자리에
        #   있었다. 사상표는 관문이 소유하고 여기는 빌려 쓴다.
        try:
            from release_gate import STORED_ALIASES

            for want, (stored, scale) in STORED_ALIASES.items():
                if want not in found and stored in found:
                    found[want] = found[stored] * scale
        except Exception:  # noqa: BLE001  - 관문을 못 불러도 환류는 적재한다
            pass
        oos = {k: found[k] for k in _OOS_KEYS if k in found}
        # 카드와 이름을 맞춘다 - 두 곳이 다른 이름을 쓰면 대조가 안 된다
        for src, dst in (("bootstrap_ci_low", "ci_low"),
                         ("bootstrap_ci_high", "ci_high")):
            if src in oos:
                oos[dst] = oos.pop(src)

    failed = list(failed_criteria or [])
    if not failed and new_status == "REJECTED" and fragility:
        failed = [f"fragility_{str(fragility).lower()}"]
    # ▶ 관문이 막은 조항을 **합친다**(덮지 않는다). `lessons_from` 은 이미
    #   조항 이름을 교훈 어휘로 사상한다 - drawdown->BEAR_FRAGILE,
    #   turnover->COST_SENSITIVE. 설계는 처음부터 이걸 기대하고 있었는데
    #   관문이 안 돌아서 재료가 온 적이 없었다.
    for g in (gate_failed or ()):
        if g not in failed:
            failed.append(str(g))
    lessons = list(lesson_codes or []) or lessons_from(
        failed_criteria=failed,
        regime_concerns=regime_evidence if regime_evidence is not None else
                        getattr(report, "regime_evidence", ()),
        fragility=fragility, oos_summary=oos)
    decision = _STATUS_TO_DECISION.get(new_status, "GATE_HOLD")

    outcome = build_outcome(
        experiment_id=exp_id or f"unknown-{hid}", hypothesis_id=str(hid),
        trial_family_id=str(tp.get("trial_family_id") or ""),
        trial_number=int(tp.get("trial_number") or 1),
        decision=decision, failed_criteria=failed, lesson_codes=lessons,
        oos_summary=oos, notes=notes)
    if ({str(new_status or "").upper(), str(decision or "").upper()}
            & {"SUPPORTED", "SUBMIT_TO_QA", "PROMOTED"}):
        # Do not trust an injected evaluator's experiment_id.  Re-read the
        # durable evidence at this terminal boundary; factory_bridge.finalize
        # repeats the check so direct bridge callers cannot bypass it either.
        assert_governed_stock_experiment(
            conn, experiment_id=exp_id, hypothesis_id=str(hid))
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


def _reservation_payload(value) -> dict:
    """Return a validated durable reservation or fail the pressure gate."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid trial reservation JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("trial reservation must be a JSON object")
    family = str(value.get("trial_family_id") or "")
    try:
        number = int(value.get("trial_number"))
    except (TypeError, ValueError) as exc:
        raise ValueError("trial reservation number is invalid") from exc
    if not family or number < 1:
        raise ValueError("trial reservation family/number is invalid")
    return dict(value)


def _reserve_trial_family(conn, cur, *, hypothesis_id: str, hyp: dict,
                          families: tuple[str, ...], budget: int,
                          pressure_fn) -> dict:
    """Atomically consume one family trial before an evaluator can run.

    ``quant.experiments`` is registered inside the concrete runner, so it does
    not exist when the orchestrator must authorize first evidence access.  The
    hypothesis JSON therefore carries the durable reservation.  It is written
    under a family advisory transaction lock, then copied to every experiment
    row created for that hypothesis.  A retry reuses the same reservation; a
    worker crash cannot make an exposed failed/cancelled experiment disappear
    from future family pressure.

    Query/decoding errors deliberately propagate.  Treating an unavailable
    pressure ledger as zero would authorize an uncorrected first trial.
    """
    canonical = families[0] if families else ""
    if not canonical:
        # An unclassifiable family retains the existing conservative DSR
        # contract, but there is no shared family counter to reserve.
        return pressure_fn(families, [], budget=budget)

    cur.execute(
        "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (canonical,))
    cur.execute(
        f"""select hypothesis_id::text,
                   expected_edge->'{TRIAL_RESERVATION_KEY}'
              from quant.hypotheses
             where expected_edge->'{TRIAL_RESERVATION_KEY}'
                       ->>'trial_family_id' = any(%s)""",
        (list(families),))
    reservation_rows = cur.fetchall()

    reservations: dict[str, dict] = {}
    for row_hid, raw in reservation_rows:
        payload = _reservation_payload(raw)
        if payload and payload["trial_family_id"] in families:
            reservations[str(row_hid)] = payload

    cur.execute(
        """select experiment_id::text, hypothesis_id::text,
                  trial_family_id, trial_number
             from quant.experiments
            where trial_family_id = any(%s)""",
        (list(families),))
    experiment_rows = cur.fetchall()

    # One hypothesis reservation represents one immutable evaluator input.
    # Legacy rows without a reservation remain countable, while experiments
    # already backed by a reservation are not double counted.
    cards = [
        {"trial_family_id": payload["trial_family_id"]}
        for payload in reservations.values()
    ]
    for _experiment_id, row_hid, family_id, _trial_number in experiment_rows:
        if str(row_hid) not in reservations:
            cards.append({"trial_family_id": str(family_id or "")})

    current = reservations.get(str(hypothesis_id))
    if current is None:
        # Backfill a legacy already-assigned experiment without burning a new
        # number.  This is the idempotent upgrade path for in-flight retries.
        legacy = next((row for row in experiment_rows
                       if str(row[1]) == str(hypothesis_id)
                       and str(row[2] or "") in families), None)
        if legacy is not None:
            current = {
                "reservation_id": f"legacy-experiment:{legacy[0]}",
                "trial_family_id": str(legacy[2]),
                "trial_number": int(legacy[3]),
                "trial_budget": int(budget),
                "orchestrator_version": ORCH_VERSION,
            }
        else:
            calculated = pressure_fn(families, cards, budget=budget)
            current = {
                "reservation_id": str(uuid.uuid4()),
                "trial_family_id": canonical,
                "trial_number": int(calculated["trial_number"]),
                "trial_budget": int(budget),
                "orchestrator_version": ORCH_VERSION,
            }
            if len(families) > 1:
                current["counted_aliases"] = list(families[1:])

        cur.execute(
            f"""update quant.hypotheses
                   set expected_edge=jsonb_set(
                         coalesce(expected_edge, '{{}}'::jsonb),
                         '{{{TRIAL_RESERVATION_KEY}}}', %s::jsonb, true)
                 where hypothesis_id=%s""",
            (json.dumps(current, sort_keys=True, separators=(",", ":")),
             hypothesis_id))
    # End the family transaction lock before expensive evaluation.  Both the
    # newly written reservation and an idempotently reused one are now fixed.
    conn.commit()

    # The immutable assigned number, not today's possibly larger population,
    # controls replay.  This prevents an idempotent retry from rewriting DSR.
    number = int(current["trial_number"])
    pressure = {
        "trial_family_id": str(current["trial_family_id"]),
        "trials_used": number - 1,
        "trial_number": number,
        "trial_budget": int(current.get("trial_budget") or budget),
        "over_budget": number > int(current.get("trial_budget") or budget),
        "reservation_id": str(current.get("reservation_id") or ""),
        "reserved_before_evaluation": True,
    }
    if current.get("counted_aliases"):
        pressure["counted_aliases"] = list(current["counted_aliases"])
    edge = dict(hyp.get("expected_edge") or {})
    edge[TRIAL_RESERVATION_KEY] = dict(current)
    hyp["expected_edge"] = edge
    return pressure


def _attach_trial_reservation(cur, *, hypothesis_id: str,
                              pressure: dict,
                              experiment_id: str | None = None) -> None:
    """Copy a prior reservation to completed, failed, or cancelled rows."""
    family = str(pressure.get("trial_family_id") or "")
    if not family:
        return
    params: list = [family, int(pressure["trial_number"]), hypothesis_id]
    predicate = ""
    if experiment_id:
        predicate = " and experiment_id=%s"
        params.append(experiment_id)
    cur.execute(
        """update quant.experiments
              set trial_family_id=%s, trial_number=%s
            where hypothesis_id=%s and trial_family_id is null"""
        + predicate,
        tuple(params))


def orchestrate(hypothesis_id: str | None = None, *, conn=None,
                market_conn=None, run_chain=None) -> OrchestratorReport:
    """가설 하나를 게이트에 태운다. conn/market_conn/run_chain 주입은 자체점검용.

    ▶ 두 DB 를 걸친다: 메타(가설·매니페스트)는 `DATABASE_URL`, 시장 데이터는
      `TIMESCALE_DATABASE_URL`. 데이터 요구를 **실제로 재려면** 후자가 있어야 하고,
      없으면 `NOT_VERIFIED` 로 막힌다 - 못 잰 것을 통과로 세지 않는다.
    """
    own_conn = conn is None
    # ▶ **두 연결은 따로 판단한다** (2026-08-12)
    #   예전에는 시장 연결을 `if own_conn:` 안에서만 열었다. 그래서 메타 연결을
    #   주입하는 쪽(experiment_worker)이 부르면 시장 연결이 **통째로 건너뛰어졌고**,
    #   커버리지를 못 재 전건이 `NOT_RUNNABLE` 로 떨어졌다. 실측: 워커가 집은
    #   실험 3건이 전부 "시장 DB 연결이 없어 커버리지를 재지 못했다"로 실패.
    #
    #   증상이 고약한 이유는 **막는 쪽이 옳게 동작했기 때문**이다 - 못 잰 것을
    #   통과로 세지 않는다는 규칙은 제대로 지켜졌고, 그래서 로그만 보면 데이터가
    #   모자란 것처럼 보인다. 실제로는 연결을 안 준 쪽이 문제였다.
    own_market = market_conn is None
    if own_conn or own_market:
        import psycopg2

        sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                               / "01-research" / "collectors"))
        from source_registry import load_project_env

        env = load_project_env()
        if own_conn:
            from db_writer import connect as connect_writer

            conn = connect_writer(env["DATABASE_URL"], connect_timeout=20)
        if own_market and env.get("TIMESCALE_DATABASE_URL"):
            market_conn = psycopg2.connect(env["TIMESCALE_DATABASE_URL"],
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
        execution_products = execution_data_products(
            _norm_data_products(data_products))
        hyp = {"expected_edge": edge if isinstance(edge, dict) else json.loads(edge or "{}"),
               # ▶ 세 모양을 다 받는다: 리스트(구 형식), dict(기획안의
               #   DataRequirement {tables, min_history_days}), JSON 문자열.
               #   기획안 경로가 dict 를 넣는데 리스트만 처리해 TypeError 로
               #   실험이 죽었다(2026-08-10 실측).
               "required_data_products": execution_products,
               # ▶ status 를 언패킹만 하고 dict 에 안 넣어서 사전등록 관문이
               #   빈 문자열을 읽고 "순서를 건너뛴다" 로 막았다. 조회한 값을
               #   쓰지 않으면 조회하지 않은 것과 같다.
               "status": _status}

        # ── 데이터 요구 사상 + 실측 ──────────────────────────────────────
        # ▶ 여기가 매니페스트 **이름만 대조**하던 자리다. 리서치는 원천 이름으로
        #   말하고(`market_bars`) 실행면은 매니페스트 이름으로 물어서
        #   (`krx-basket-daily/v2`) 공장을 거친 가설이 전부 NOT_RUNNABLE 로
        #   떨어져 PROPOSED 에 영구 정체했다(2026-08-10 실측). 게다가 이름이
        #   있으면 통과시키는 것은 fail-open 이었다 - 매니페스트는 빌드 시점의
        #   주장이고 원천이 그 뒤 비었는지는 말해 주지 않는다.
        #   이제 사상은 source_versions 에서 유도하고 커버리지는 로컬에서 잰다.
        from data_resolution import resolve as resolve_data

        res = resolve_data(
            execution_products, meta_conn=conn, market_conn=market_conn,
            research_lane=str((hyp.get("expected_edge") or {}).get(
                "research_lane") or ""))
        if not res.ok:
            return OrchestratorReport(
                hypothesis_id=str(hid), title=title, verdict="NOT_RUNNABLE",
                missing=[f"data:{res.verdict}"] + [f"source:{u}" for u in res.unmapped],
                backlog=list(res.notes))
        # 이후 단계는 실행면 이름만 쓴다 - 원천 이름이 더 내려가면 안 된다.
        hyp["required_data_products"] = list(res.datasets)

        ok, missing, backlog = feasibility(hyp, set(res.datasets))
        report = OrchestratorReport(hypothesis_id=str(hid), title=title,
                                    verdict="RUNNABLE" if ok else "NOT_RUNNABLE",
                                    missing=missing, backlog=backlog)
        if not ok:
            return report          # PROPOSED 유지 - 수단 부족은 가설의 죄가 아니다

        # Intraday source coverage is a pre-trial concern. Probe and persist it
        # before preregistration, experiment registration, or family pressure is
        # calculated.  The governed 6/20/60 funnel requires 60 evaluation
        # sessions plus at least one strictly prior calibration session; shorter
        # history is retried later without polluting DSR/PBO trial accounting.
        edge = hyp.get("expected_edge") or {}
        intraday_lane = (
            str(edge.get("research_lane") or "").upper() == "INTRADAY_EVENT")
        if intraday_lane:
            if market_conn is None:
                report.verdict = "NOT_RUNNABLE"
                report.missing.append("data:TIMESCALE_CONNECTION")
                return report
            from intraday_experiment_runner import prepare as prepare_intraday
            from intraday_experiment_runner import record_data_feasibility

            prepared = prepare_intraday(
                hyp, market_conn=market_conn, meta_conn=conn)
            report.data_feasibility = record_data_feasibility(
                conn, str(hid), prepared)
            if report.data_feasibility["status"] != "PASS":
                selected = prepared["selected"]
                report.verdict = "NEEDS_DATA"
                report.missing.append(f"intraday_slice:{selected.get('status')}")
                report.backlog.append(
                    "Wait for at least 61 causal sessions and two STOCK instruments; "
                    "the factory will recheck coverage without consuming a trial.")
                return report
            hyp["_intraday_preflight"] = prepared

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
        intraday_lane = str(edge.get("research_lane") or "").upper() == "INTRADAY_EVENT"
        if intraday_lane:
            spec_row.update({
                "features": [edge.get("intraday_signal_expr")],
                "label": (edge.get("semantic_plan") or {}).get("output"),
                "decision_frequency": f"{edge.get('sample_interval_seconds', 5)}s",
                "holding_horizon": f"{edge.get('horizon_seconds')}s",
                "entry_exit_rules": {
                    "execution": edge.get("execution"),
                    "latency_ms": edge.get("order_latency_ms", 250),
                    "threshold": edge.get("threshold", 0.0),
                },
                "cost_model_version": "krx-intraday-execution-v3",
                "universe_version": "krx-intraday-events/v1",
            })
        else:
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
        from trial_family import family_ids_for, pressure as fam_pressure

        # ▶ **접수(Gate 0)와 같은 함수로 계산한다**(2026-08-11 실측). 예전엔
        #   family_id(hyp) 를 그대로 불러 label/baseline 이 빈 문자열로 해시됐고,
        #   Gate 0 은 기본값을 넣어 해시했다. 같은 기획안이
        #     접수 fam_42663e9f4b0f8233 / 실행 fam_65a4c7b6f4c75999
        #   두 값을 가졌다 - 실험은 실행면 값으로 각인되는데 Gate 0 은 접수 값으로
        #   세니 **count_family_trials 가 영원히 0** 이었고, 시도 예산·DSR 감가·
        #   기각 교훈 대응이 전부 안 걸렸다.
        # ▶ **동의어를 전부 세고, 찍는 값은 정본 하나** (2026-08-12)
        #   계열 ID 를 만드는 길이 둘이라 같은 개념이 두 값을 가졌고,
        #   그래서 시도 카운터가 1부터 다시 셌다(momentum/krx_all 이 7건
        #   있는데 오늘 것이 시도1). 세는 쪽이 둘 다 인정하게 한다.
        fams = family_ids_for(hyp)
        fam = fams[0] if fams else ""
        budget = int(hyp.get("trial_budget") or TRIAL_BUDGET_DEFAULT)
        try:
            # ▶ **기록된 배정을 읽는다**(다시 계산하지 않는다). Family 는
            #   유니버스 서술을 통제 어휘로 사상해 만들므로, 어휘를 늘리면
            #   과거 실험의 배정이 소급 변경되어 "12번째 시도" 가 어제와 오늘
            #   다른 값이 된다. 시도 압력은 기록된 사실이어야 한다.
            report.trial_pressure = _reserve_trial_family(
                conn, cur, hypothesis_id=str(hid), hyp=hyp,
                families=fams, budget=budget, pressure_fn=fam_pressure)
        except Exception as exc:  # noqa: BLE001 - governance boundary
            # An unavailable pressure ledger is not evidence of zero trials.
            # Never enter the evaluator with an uncorrected trial count.
            if hasattr(conn, "rollback"):
                conn.rollback()
            cur.execute(
                "update quant.hypotheses set status='PROPOSED', "
                "status_changed_at=now() where hypothesis_id=%s "
                "and status='RUNNING'", (hid,))
            conn.commit()
            report.verdict = "TRIAL_PRESSURE_UNAVAILABLE"
            report.trial_pressure = {
                "status": "UNAVAILABLE",
                "error": type(exc).__name__,
                "fail_closed": True,
            }
            report.backlog.append(
                "Trial-family pressure could not be loaded; evaluation was "
                f"blocked fail-closed ({type(exc).__name__}).")
            report.transitions.append(
                "RUNNING->PROPOSED (trial pressure unavailable)")
            return report
        # ▶ **백테스트에 시도 횟수를 넘긴다.** 안 넘기면 DSR 이 trials=1
        #   로 계산돼 전혀 감가되지 않는다 - 20번 시도해 고른 Sharpe 를
        #   첫 시도와 같은 값으로 읽게 된다(계약만 있고 실행부가 안 따라간
        #   같은 결함이다).
        hyp = dict(hyp)
        hyp['_trials'] = int(report.trial_pressure['trial_number'])


        if run_chain is not None:
            chain = run_chain
        elif intraday_lane:
            from intraday_experiment_runner import run as run_intraday

            chain = lambda h, i: run_intraday(  # noqa: E731 - injected DB handles
                h, i, meta_conn=conn, market_conn=market_conn)
        else:
            chain = _default_chain
        try:
            result = chain(hyp, str(hid))   # {"experiment_id", "fragility": FRAGILE|ROBUST}
        except Exception:
            # ▶ 실패한 체인이 가설을 RUNNING 에 가두지 않는다 (2026-08-13 실측)
            #   퀀트 카드가 CLI 로 orchestrate 를 직접 부르는 경로는 작업 큐가
            #   없어서, 체인이 죽으면 실패가 어디에도 안 남고 가설만 RUNNING 에
            #   갇혔다 - e2379857 이 job 0건·실험 행 0건·"사유 기록 없음" 으로
            #   하루 3회 스톨 회수됐다. 30분 스톨 대기는 회수가 아니라 낭비다:
            #   실패 즉시 PROPOSED 로 되돌리고 예외는 그대로 올린다(작업 큐
            #   경로에서는 호출부가 failure_reason 을 기록한다).
            # The runner may already have registered/read evidence before it
            # failed.  Bind every such row to the pre-evaluation reservation;
            # FAILED/CANCELLED status never refunds multiple-testing pressure.
            _attach_trial_reservation(
                cur, hypothesis_id=str(hid), pressure=report.trial_pressure)
            cur.execute("update quant.hypotheses set status='PROPOSED', "
                        "status_changed_at=now() where hypothesis_id=%s "
                        "and status='RUNNING'", (hid,))
            conn.commit()
            raise
        report.regime_evidence = list(result.get("regime_evidence") or ())
        report.experiment_refs = {k: result[k] for k in ("experiment_id", "fragility")
                                  if k in result}
        # ▶ 이 실험의 Family 배정을 남긴다. **한쪽만 쓰지 않는다** - Family 가
        #   없으면 순번도 없다(DB 제약이 강제한다). 없는 순번을 지어내면
        #   다음 실험의 계수가 틀어진다.
        family_pbo = None
        if fam and result.get("experiment_id"):
            _attach_trial_reservation(
                cur, hypothesis_id=str(hid), pressure=report.trial_pressure,
                experiment_id=str(result["experiment_id"]))
            conn.commit()

            # ▶ PBO - **고르는 행위 자체가 작동하는가.** trial_pressure 는
            #   몇 번 시도했나를 세고 DSR 은 그만큼 Sharpe 를 깎는다. PBO 는
            #   다른 것을 본다: IS 1등이 OOS 에서도 1등인가. 셋은 서로를
            #   대체하지 않는다.
            #   방금 실험의 창까지 쌓인 뒤에 센다 - 자기 자신을 빼면 안 된다.
            try:
                from pbo_cscv import compute as pbo_compute
                from pbo_cscv import load_family_performance

                # A worker retry must replay the decision made for this immutable
                # input, not recompute it using family variants discovered later.
                # Otherwise merely retrying could rewrite history and create a
                # second final-gate row with different dimensions.
                if intraday_lane and result.get("duplicate"):
                    replay_pbo = (result.get("intraday_report", {}).get("summary", {})
                                  .get("pbo"))
                    pres = {
                        "probability_of_backtest_overfitting": replay_pbo,
                        "idempotent_replay": True,
                    }
                else:
                    performance = load_family_performance(
                        conn, fam,
                        reference_experiment_id=str(result["experiment_id"]),
                        evaluation_scope=(
                            "FULL_60" if intraday_lane
                            else "DAILY_WALK_FORWARD"),
                    )
                    pres = pbo_compute(performance)
                report.trial_pressure.update(pres)
                # ▶ **지표로도 적재한다.** 릴리스 관문은 report 가 아니라
                #   experiment_metrics 에서 pbo 를 읽는다 - 여기 안 넣으면
                #   관문이 늘 None 을 보고 fail-closed 로 전부 HOLD 한다.
                #   (계약·계산은 됐는데 소비처에 안 닿는 같은 결함이다.)
                pv = pres.get("probability_of_backtest_overfitting")
                family_pbo = pv
                if pv is not None:
                    # cost_model_version 은 NOT NULL 이다 - 빠뜨리면 적재가
                    # 통째로 죽고 관문은 다시 None 을 본다(실측으로 걸렸다)
                    if intraday_lane:
                        from intraday_experiment_runner import COST_MODEL_VERSION
                        metric_cost_version = COST_MODEL_VERSION
                    else:
                        from backtest_runner import COST_MODEL
                        metric_cost_version = COST_MODEL["version"]

                    cur.execute(
                        """insert into quant.experiment_metrics
                             (experiment_id, split, metric, value, dimensions,
                              cost_model_version)
                           values (%s, 'WALK_FORWARD', 'pbo', %s, '{}'::jsonb, %s)
                           on conflict (experiment_id, split, metric, dimensions)
                           do update set value = excluded.value""",
                        (result["experiment_id"], pv, metric_cost_version))
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
                                 metric_cost_version))
                    conn.commit()
            except Exception as e:  # noqa: BLE001
                # 못 재면 None 이다. **0 을 내지 않는다** - 0 은 "과적합 없음"
                # 으로 읽히는데 "안 재봤다" 와 정반대다.
                report.trial_pressure["probability_of_backtest_overfitting"] = None
                report.trial_pressure["pbo_error"] = f"{type(e).__name__}: {e}"[:200]

        if result.get("research_lane") == "INTRADAY_EVENT":
            from intraday_candidate import apply_family_pbo
            from intraday_experiment_runner import persist_final_gate

            result["intraday_report"] = apply_family_pbo(
                result["intraday_report"], family_pbo)
            persist_final_gate(conn, result["experiment_id"],
                               result["intraday_report"])
            decision = result["intraday_report"]["decision"]
            pending_forward = (
                "INDEPENDENT_FORWARD_CONFIRMATION_PENDING" in
                (result["intraday_report"].get("failed_criteria") or []))
            result["fragility"] = (
                "ROBUST" if decision == "SUBMIT_TO_QA" else
                "INSUFFICIENT" if decision == "NO_EVIDENCE" or pending_forward
                else "FRAGILE")
            report.experiment_refs = {
                "experiment_id": result["experiment_id"],
                "fragility": result["fragility"],
                "idempotent_replay": bool(result.get("duplicate")),
            }
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

        if result.get("research_lane") == "INTRADAY_EVENT":
            intraday_report = result["intraday_report"]
            failed = list(intraday_report.get("failed_criteria") or [])
            decision = intraday_report.get("decision")
            underpowered = any(item in {
                "NO_EXECUTABLE_OBSERVATIONS", "SESSIONS_BELOW_MINIMUM",
                "INSTRUMENTS_BELOW_MINIMUM", "OPPORTUNITIES_BELOW_MINIMUM",
                "PBO_UNMEASURED", "INDEPENDENT_FORWARD_CONFIRMATION_PENDING",
            } for item in failed)
            new_status = ("SUPPORTED" if decision == "SUBMIT_TO_QA" else
                          "INCONCLUSIVE" if decision == "NO_EVIDENCE" or underpowered
                          else "REJECTED")
            report.release = {
                "decision": decision,
                "failed": failed,
                "summary": intraday_report.get("summary") or {},
                "not_a_promotion": intraday_report.get("not_a_promotion"),
            }
            gate_note = json.dumps({
                "lane": "INTRADAY_EVENT", "decision": decision,
                "failed": failed, "summary": intraday_report.get("summary") or {},
            }, ensure_ascii=False, default=str)[:4000]
            _finalize_with_feedback(
                conn, report=report, hid=hid, new_status=new_status,
                experiment_id=result.get("experiment_id"),
                fragility=result.get("fragility", ""), gate_failed=failed,
                notes=gate_note)
            report.transitions.append(f"RUNNING->{new_status}")
            if new_status == "SUPPORTED":
                try:
                    from strategy_lifecycle import evaluate_promotion
                    lc = evaluate_promotion(
                        str(title or hid)[:40], current_state="RESEARCH",
                        gate_decision="SUBMIT_TO_QA")
                    report.lifecycle = lc.as_dict()
                except Exception as exc:  # noqa: BLE001
                    report.lifecycle = {"error": f"lifecycle evaluation failed: {type(exc).__name__}"}
            return report

        # ▶ **실험이 성립했는지 먼저 본다** - 거래가 0이면 강건성을 논할
        #   대상 자체가 없다. 기각으로 밀면 데이터·필터 사고가 가설의 죄로
        #   기록되고 계열 예산까지 탄다(2026-08-14 실측, 위 함수 참조).
        no_trade = experiment_did_not_trade(result.get("backtest_metrics"))
        if no_trade:
            new_status = "INCONCLUSIVE"
            report.backlog.append(
                "거래 0건 - 체결가능 유니버스가 비었거나 신호가 한 종목도 "
                "고르지 못했다. 판정이 아니라 판정 불가로 종결한다")
        else:
            new_status = robustness_to_status(result["fragility"],
                                              report.trial_pressure)
        # ▶ **환류 적재와 상태 전이를 한 트랜잭션으로.** 적재가 실패하면 상태도
        #   안 바뀐다 - 조용히 종결되고 교훈만 사라지는 경로를 없앤다.
        # ▶ QNT-07 릴리스 관문. **환류보다 먼저 돌린다** - 판정을 환류에 실어야
        #   다음 기획안이 "어느 조항에서 몇 만큼 모자랐는지" 를 읽는다.
        #   **승격이 아니라 제출 판정이다** - Production 은 CEO·Risk·QA 몫이다.
        #
        # ▶ 왜 REJECTED 에도 돌리나 (2026-08-12)
        #   예전엔 SUPPORTED 일 때만 돌렸다. 그런데 SUPPORTED 가 한 번도 없어서
        #   관문 336줄이 **한 번도 실행된 적이 없었다.** 그 사이 momentum 은
        #   초과 +157.51%p · IR 1.26 · DSR 0.976 을 내고도 "REJECTED" 한 줄만
        #   남겼다 - 어느 조항이 막았는지 아무도 몰랐고, 리서치는 같은 자리에서
        #   죽을 새 엣지를 계속 설계했다. 관문은 순수 함수다. 늘 돌려서
        #   **거리를 알려주는 편이 옳다.** 제출은 여전히 SUPPORTED 만 한다.
        gate_failed: list[str] = []
        gate_note = ""
        release_gate_error = False
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from release_gate import SQL_GATE_METRICS, metrics_from_rows
            from release_gate import evaluate as gate_evaluate

            cur.execute(SQL_GATE_METRICS, (result.get("experiment_id"),))
            # 이름·단위·split 사상은 **관문이 소유한다** - 읽는 곳마다 각자
            # 맞추면 한 곳을 고쳐도 다른 곳이 계속 눈을 감는다(실제로 그랬다).
            metrics = metrics_from_rows(cur.fetchall())
            d = gate_evaluate(metrics, fragility=result.get("fragility", ""),
                              trial_pressure=report.trial_pressure)
            report.release = d.as_dict()
            # ▶ **못 잰 조항은 교훈 재료가 아니다.** `lessons_from` 은 조항
            #   이름만 보고 사상하므로, PBO 미측정이 그대로 들어가면 환류에
            #   "과적합됐다(OVERFIT_PBO)" 는 **없는 사실**이 적힌다. 차단은
            #   그대로 하되(관문 판정은 HOLD), 교훈은 잰 것으로만 만든다.
            _unmeasured = set(d.unmeasured or ())
            gate_failed = [str(x) for x in (d.failed or []) if x not in _unmeasured]
            gate_note = _gate_note(d, metrics)
            # ▶ **자기반증 결과를 환류에 같이 싣는다.** 관문은 우리가 정한
            #   기준이고 반증은 그 가설이 스스로 건 조건이다 - 후자를 견딘
            #   것이 훨씬 강한 증거이므로 다음 기획안이 반드시 봐야 한다.
            try:
                import falsification as _fx  # noqa: PLC0415

                _fr = [_fx.TestResult(**r) for r in (result.get("falsification") or [])]
                _n = _fx.note(_fr)
                if _n:
                    gate_note = (gate_note + " || " + _n) if gate_note else _n
                report.release["falsification"] = _fx.summarize(_fr)
            except Exception:  # noqa: BLE001 - 못 실으면 안 싣는다
                pass
        except Exception as e:  # noqa: BLE001
            # 관문 실패를 통과로 위장하지 않는다 - HOLD 가 안전한 기본값이다
            release_gate_error = True
            report.release = {"decision": "HOLD",
                              "reasons": [f"관문 실행 실패: {type(e).__name__}: {e}"]}

        provisional_status = new_status
        new_status = release_to_status(
            new_status,
            report.release.get("decision"),
            failed=report.release.get("failed") or (),
            unmeasured=report.release.get("unmeasured") or (),
        )
        if provisional_status == "SUPPORTED" and new_status == "INCONCLUSIVE":
            hold_reason = (
                "release_gate_unavailable" if release_gate_error else
                "release_gate_unmeasured" if report.release.get("unmeasured") else
                "release_gate_hold"
            )
            if hold_reason not in gate_failed:
                gate_failed.append(hold_reason)

        # ▶ **환류 적재와 상태 전이를 한 트랜잭션으로.** 적재가 실패하면 상태도
        #   안 바뀐다 - 조용히 종결되고 교훈만 사라지는 경로를 없앤다.
        _finalize_with_feedback(
            conn, report=report, hid=hid, new_status=new_status,
            experiment_id=result.get("experiment_id"),
            fragility=result.get("fragility", ""),
            gate_failed=gate_failed, regime_evidence=report.regime_evidence,
            notes=gate_note)
        report.transitions.append(f"RUNNING->{new_status}")

        # ▶ 관문 다음 칸. **승격이 아니라 요청이다** - 좋은 백테스트가 바로
        #   운영 전략이 되지 않게 Shadow 부터 밟는다. 여기만 SUPPORTED 전용이다.
        if (new_status == "SUPPORTED"
                and report.release.get("decision") == "SUBMIT_TO_QA"):
            try:
                from strategy_lifecycle import evaluate_promotion

                lc = evaluate_promotion(
                    str(hyp.get("title") or hid)[:40],
                    current_state="RESEARCH",
                    gate_decision=str(report.release.get("decision")))
                report.lifecycle = lc.as_dict()
            except Exception as e:  # noqa: BLE001
                report.lifecycle = {"error": f"생명주기 판정 실패: {type(e).__name__}"}
        return report
    finally:
        # **우리가 연 것만 닫는다.** 주입받은 연결을 닫으면 호출부의 다음 작업이
        # 죽는다(워커는 한 연결로 여러 주문을 돈다). 여는 조건과 닫는 조건이
        # 다르면 언젠가 한쪽이 새거나 남의 것을 닫는다 - 같은 깃발을 쓴다.
        if own_conn:
            conn.close()
        if own_market and market_conn is not None:
            market_conn.close()


def _default_chain(hyp: dict, hypothesis_id: str | None = None) -> dict:
    """실전 체인: 백테스트(가설 바인딩) + walk-forward 강건성 -> 판정.

    edge type -> 전략 config 매핑은 카탈로그가 정하고, 강건성 지표는
    walk_forward 의 조각(make_windows/run_window/fragility_summary)을
    같은 config 로 재사용한다 - 검증 규칙을 두 벌 만들지 않는다.
    """
    import psycopg2
    from backtest_runner import (
        COST_MODEL,
        DEFAULT_CONFIG,
        REV_CONFIG,
        Market,
        buy_and_hold_equity,
        load_dataset,
        register_and_run,
        required_warmup_days,
        run_backtest,
    )
    from source_registry import load_project_env
    from walk_forward import (
        SHORT_MIN_TEST_DAYS,
        SHORT_SAMPLE_MAX_DAYS,
        WARMUP_TRADING_DAYS,
        fragility_summary,
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

    base = base_config_for(edge, DEFAULT_CONFIG, REV_CONFIG)
    binding = bind(hyp, base)
    if not binding.ok:
        # 범위 밖 값을 잘라 쓰지 않는다 - 자르면 등록한 것과 실행한 것이
        # 달라져 같은 문제가 다시 생긴다
        raise RuntimeError("가설 파라미터 거부: " + "; ".join(binding.rejected))
    config = binding.config

    print(f"  config 바인딩: 가설 {binding.from_hypothesis or '-'} / "
          f"기본값 {binding.from_default or '-'}", flush=True)
    # ▶ **받았지만 안 읽은 값은 실행 로그에도 남긴다** (2026-08-14, t_e9534028).
    #   `walk_forward_window_days` 처럼 사전등록에만 남고 실험은 기본 창으로
    #   도는 값이 있다. 거부가 아니라 조용한 무시라서, 여기서 안 찍으면
    #   "등록한 가설과 실행한 실험이 다르다" 가 로그에 흔적을 안 남긴다.
    if binding.ignored:
        print(f"  ! 실행면이 안 읽은 값: {binding.ignored}", flush=True)
    # ▶ 시도 횟수를 넘긴다 - DSR 이 감가하려면 알아야 한다. **config 가 아니라
    #   인자로** 넘긴다(config 는 input_hash 에 들어가므로 넣으면 중복 가드가
    #   무력해진다).
    # ▶ **데이터셋은 가설이 정한다 - 상수가 아니다** (2026-08-12)
    #   여기는 `DATASET_NAME, DATASET_VERSION = "krx-basket-daily", "v2"` 라는
    #   모듈 상수를 썼다. 그런데 위(orchestrate)에서 `data_resolution.resolve`
    #   가 **이미 사상 결과를 계산해 `hyp["required_data_products"]` 에 넣어
    #   둔다.** 계산해 놓고 버리고 상수를 쓰고 있었다 - 그래서 v3 를 만들어도
    #   실험은 영원히 v2 로 돌았다.
    #
    #   `resolve` 는 같은 이름이면 **최신 버전**을 고르므로, 상수를 걷어내는
    #   것만으로 새 데이터셋이 저절로 쓰인다. 데이터셋 해시는 `input_hash` 에
    #   들어가므로 v2/v3 는 서로 다른 실험이 된다 - 과거 결과는 그대로 남는다.
    ds_name, ds_ver = dataset_of(hyp)
    print(f"  데이터셋 {ds_name}/{ds_ver} (가설 요구에서 사상)", flush=True)
    warmup = required_warmup_days(
        config, legacy_floor=WARMUP_TRADING_DAYS)
    embargo = signal_horizon(config)
    bt = register_and_run(ds_name, ds_ver,
                          config=config, hypothesis_id=hypothesis_id,
                          trials=int(hyp.get("_trials") or 1),
                          evaluation_mode="DAILY_WALK_FORWARD",
                          evaluation_warmup_days=warmup,
                          evaluation_embargo_days=embargo)
    if bt.get("duplicate"):
        # 같은 (가설, 데이터, 코드) 실험이 이미 있다 - 다시 돌리지 않고 기존
        # 실험의 강건성 판정을 찾아 쓴다. 여기 없으면 판정 불가로 끊는다.
        raise RuntimeError(f"중복 실험({bt.get('experiment_id')}) - 기존 판정을 "
                           f"수동 확인할 것 (자동 재판정은 결과 조작 여지가 있다)")

    # 강건성: 같은 config 로 창별 재실행 (walk_forward 조각 재사용)
    from db_writer import connect as connect_writer

    conn = connect_writer(load_project_env()["DATABASE_URL"], connect_timeout=20)
    try:
        dataset_id, universe_version_id, dataset_hash, rows = load_dataset(
            conn, ds_name, ds_ver)
        market = Market.from_rows(rows)
        # ▶ **원본 행을 붙들지 않는다** (2026-08-14 실측)
        #   `load_dataset` 이 돌려준 dict 리스트(v3 = 725만 행)와 `Market`
        #   이 동시에 살아 있으면 같은 데이터를 두 벌 든다. 미시구조까지
        #   더 실으면 그 자리에서 `OSError: Cannot allocate memory` 로
        #   죽는다 - 실제로 OFI 첫 실험이 그렇게 실패했다. 위 두 줄 이후
        #   `rows` 를 읽는 곳이 없으므로 즉시 놓는다.
        del rows
        gc.collect()
        # ▶ **신호가 요구하면 미시구조를 붙인다** (2026-08-14)
        #   호가·체결은 일봉과 다른 데이터셋이라 따로 실어야 한다. 요구는
        #   가설이 아니라 **템플릿**이 선언하므로(`needs_micro`) 빠뜨릴 자리가
        #   없다. 요구가 없으면 아무것도 안 하고 예전 경로 그대로다.
        try:
            from backtest_runner import attach_micro_if_needed  # noqa: PLC0415

            _mn = attach_micro_if_needed(market, config, conn)
            if _mn:
                print(f"  미시구조 적재 {_mn:,}건 (호가·체결 일별 집계)",
                      flush=True)
        except Exception as e:  # noqa: BLE001 - 못 붙여도 실험은 돈다(신호가 빈다)
            print(f"  ⚠ 미시구조 적재 실패: {type(e).__name__}: {str(e)[:110]}",
                  flush=True)
        # ▶ **embargo = 보유 지평.** 웜업 마지막 시그널이 그만큼 미래로
        #   이어지므로 그 구간을 평가에서 뺀다 - 안 빼면 직전 구간 정보가
        #   성적에 섞인다.
        # ▶ **웜업은 전략이 선언한 최소 히스토리를 따른다** (2026-08-12)
        #   여기는 `WARMUP_TRADING_DAYS`(=30, 주석에 "lookback 20 + 여유 10")를
        #   그대로 썼다. 20일 모멘텀에 맞춘 상수인데 **모든 전략에 같이 걸렸다.**
        #   126일 형성창 전략도 30일치 히스토리만 받으니 시그널이 계산되지 않고,
        #   창은 만들어져도 산출이 비어 `강건성을 재지 못했다` 로 끝났다
        #   (`667f0a45` 의 교훈이 그것이다).
        #
        #   템플릿뿐 아니라 AST와 켜진 위험관리 창도 실제 이력을 요구한다.
        #   이 계산은 러너의 단일 함수가 소유한다. 특히 짧은 미시구조 표본에는
        #   가격전략용 30일 관례를 강제로 붙이지 않고 수식의 실제 요구량을 쓴다.
        print(f"  웜업 {warmup}일 (신호·AST·위험관리 선언 기준)", flush=True)
        windows = _verified_frozen_daily_windows(
            frozen_plan=bt.get("evaluation_plan"),
            dataset_content_hash=dataset_hash,
            dates=market.dates,
            warmup_days=warmup,
            embargo_days=embargo,
            cost_model=COST_MODEL,
        )
        from stock_universe import build_stock_evaluation_identity

        window_boundaries = [{
            "window": w.label,
            "start_session": str(w.test_start),
            "end_session": str(w.test_end),
        } for w in windows]
        evaluation_identity = build_stock_evaluation_identity(
            dataset_id=dataset_id,
            dataset_content_hash=dataset_hash,
            universe_version_id=universe_version_id,
            instrument_ids=market.symbols,
            windows=window_boundaries,
            cost_model_version=COST_MODEL["version"],
            evaluation_scope="DAILY_WALK_FORWARD",
            evaluation_plan_fingerprint=
                bt["evaluation_plan"]["evaluation_plan_fingerprint"],
        )
        # ▶ **창마다 바로 적재한다** (2026-08-14 실측)
        #   예전엔 창 21개를 전부 계산한 **뒤에** 한 번에 저장했다. 이 구간은
        #   백테스트가 끝난 뒤에도 13분을 더 도는데, 그 사이 워커가 재시작되면
        #   진행분이 **통째로** 사라진다 - 실측으로 최근 한 시간에 워커가 4번
        #   기동했고(다른 세션의 `docker restart`), 그때마다 창 지표가 0행으로
        #   남았다. 실험은 COMPLETED 인데 강건성 근거만 없는 상태가 그것이다.
        #
        #   창 하나는 그 자체로 완결된 사실이므로 계산 즉시 커밋한다. 중단돼도
        #   센 만큼은 남고, 짧은 트랜잭션이라 풀러가 idle 로 끊을 일도 준다.
        #   요약(fragility)은 전 창이 모여야 의미가 있으므로 그대로 마지막에
        #   한 번 쓴다 - 부분 요약은 판정을 오도한다.
        wm = []
        for w in windows:
            metrics = run_window(slice_market(market, w), w, dict(config))
            wm.append((w.label, metrics))
            with conn.cursor() as cur:
                for k in ("total_return", "sharpe_rf0", "max_drawdown"):
                    if isinstance(metrics.get(k), (int, float)):
                        cur.execute("""
                            insert into quant.experiment_metrics
                              (experiment_id, split, metric, value,
                               dimensions, cost_model_version)
                            values (%s, 'WALK_FORWARD', %s, %s, %s::jsonb, %s)
                            on conflict do nothing
                        """, (bt["experiment_id"], k, metrics[k],
                              json.dumps({
                                  **evaluation_identity,
                                  "window": w.label,
                                  "start_session": str(w.test_start),
                                  "end_session": str(w.test_end),
                                  "chain": ORCH_VERSION,
                              }), COST_MODEL["version"]))
            conn.commit()
        # Short-sample windows are deliberately 10+ days.  Applying the legacy
        # 40-day half-year filter to them discards every window and makes the
        # adaptive construction self-contradictory.  This only enables the
        # fragility diagnostic; DSR/bootstrap/release thresholds stay intact.
        _judge_days = (SHORT_MIN_TEST_DAYS
                       if len(market.dates) < SHORT_SAMPLE_MAX_DAYS else None)
        summary, flags, verdict = fragility_summary(
            wm, **({"min_test_days": _judge_days} if _judge_days else {}))
        # ▶ **왜 취약한지를 같이 잰다** (2026-08-14). 판정(verdict)은 위에서
        #   이미 끝났고 여기서 바뀌지 않는다 - 합격선을 건드리지 않고 이유만
        #   원장에 남긴다. 실측으로 전체 판정의 87%가 fragility 인데, 그중
        #   무엇이 구간 편중이고 무엇이 진짜 불안정인지 지금은 구분이 없다.
        _wfe = walk_forward_efficiency(wm, (bt.get("metrics") or {}).get("sharpe_rf0"))
        if _wfe is not None:
            summary = dict(summary, walk_forward_efficiency=_wfe)
            print(f"  창 효율(WFE) {_wfe:+.3f} "
                  f"(창별 Sharpe 평균 / 전기간 Sharpe · 0.5~0.7 이 정상대)",
                  flush=True)
        with conn.cursor() as cur:
            # ▶ **판정 근거도 같이 남긴다** (2026-08-14 실측)
            #   이 자리에서 요약을 `_summary` 로 버리고 있었다. 그래서 창별
            #   수익·MDD 는 314행 남는데 `positive_window_ratio`·
            #   `worst_window_mdd` 는 이 체인으로 돈 실험 22건 전부 **0행**
            #   이었다 - 환류에는 `fragility` 가 실패 조항으로 찍히는데
            #   **왜 취약한지는 원장 어디에도 없는** 상태다.
            #   근거 없는 판정은 재현도 반박도 못 하고, 임계를 논의하려면
            #   먼저 분포가 있어야 한다. flags 는 숫자가 아니므로
            #   dimensions 에 실어 어느 조항이 걸렸는지까지 남긴다.
            dims = json.dumps({"chain": ORCH_VERSION, "verdict": verdict,
                               "flags": flags})
            for k, v in summary.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    cur.execute("""
                        insert into quant.experiment_metrics
                          (experiment_id, split, metric, value,
                           dimensions, cost_model_version)
                        values (%s, 'WALK_FORWARD', %s, %s, %s::jsonb, %s)
                        on conflict do nothing
                    """, (bt["experiment_id"], k, float(v), dims, "krx-cost-v1"))
        conn.commit()
    finally:
        conn.close()

    # ▶ **신호 자체를 잰다** (2026-08-12) - 포트폴리오와 분리해서.
    #   백테스트는 신호 품질과 구성 방식을 섞는다. IC 는 전 종목 횡단면으로
    #   신호만 본다. 실측: breakout 이 IC +0.0372(t +2.36, 적중 60.2%)로
    #   신호는 살아 있는데 백테스트 초과는 -168.77%p 였다 - **구성이 죽였다.**
    #   `low_volatility` 의 백테스트 초과 +855.92%p 는 액면분할 미조정 한
    #   종목이 만든 수익이었다. IC 만으로 집중 사고를 잡을 수는 없으므로
    #   pnl 집중도 관문과 함께 본다. v2 는 템플릿 TOP/BOTTOM 방향도
    #   '큰 값이 좋다'로 정규화한다.
    #   2~8초면 끝나므로 매 실험에 붙인다.
    try:
        import signal_ic as _ic  # noqa: PLC0415

        _ic_horizon = signal_horizon(config)
        _res = _ic.summarize(_ic.ic_series(
            market, dict(config), horizon=_ic_horizon))
        if _res.mean_ic is not None:
            print(f"  신호 {_ic.render(_res)}", flush=True)
        else:
            print(f"  신호 {_ic.render(_res)}", flush=True)
        # ▶ **자기 연결로 쓴다** (2026-08-13 실측). 여기서 `conn` 을 재사용
        #   했더니 앞 구간(강건성)이 이미 닫은 연결이라 매 실험
        #   `InterfaceError: connection already closed` 로 죽었다 - IC 는
        #   로그에만 찍히고 원장에는 0건이었다. **측정은 되는데 저장이
        #   죽어 있으면 그 측정은 없는 것과 같다** - IC 사전검정을 탐색층
        #   관문으로 승격할지 판정하는 자격 검사(IC↔판정 순위상관)가
        #   짝 0건으로 영영 미측정에 머문다. 짧은 단문 트랜잭션이라
        #   세션풀 부담도 없다.
        #
        #   평균 IC 를 못 낸 경우에도 기간·breadth 는 남긴다. 그래야 공장이
        #   "알파 0" 과 "표본 부족" 을 구분해 다음 실험 지평을 고칠 수 있다.
        _ic_dims = json.dumps({
            "signal_ic_version": _ic.IC_VERSION,
            "horizon_days": _ic_horizon,
            "signal_source": "AST" if config.get("signal_expr") else "TEMPLATE",
        })
        _c2 = connect_writer(load_project_env()["DATABASE_URL"],
                             connect_timeout=20)
        try:
            with _c2.cursor() as cur:
                for name, val in (("signal_ic", _res.mean_ic),
                                  ("signal_ic_t", _res.t_stat),
                                  ("signal_ic_hit_rate", _res.hit_rate),
                                  ("signal_ic_periods", _res.periods),
                                  ("signal_ic_breadth", _res.median_breadth)):
                    if val is None:
                        continue        # 못 잰 값 자체는 안 적는다
                    cur.execute("""
                        insert into quant.experiment_metrics
                          (experiment_id, split, metric, value, dimensions,
                           cost_model_version)
                        values (%s, 'TEST', %s, %s, %s::jsonb, %s)
                        on conflict do nothing""",
                        (bt["experiment_id"], name, float(val), _ic_dims,
                         "krx-cost-v1"))
            _c2.commit()
        finally:
            _c2.close()
    except Exception as e:  # noqa: BLE001 - IC 를 못 재도 실험은 종결한다
        print(f"  ⚠ 신호 IC 실패: {type(e).__name__}: {str(e)[:110]}", flush=True)

    # ▶ **가설이 스스로 건 반증 시험을 돌린다** (2026-08-12)
    #   계약은 "반증 없는 가설은 미완성" 이라며 강제하는데, 정작 그 반증을
    #   아무도 돌리지 않았다 - 기획안 10건에 49개가 저장만 돼 있었다.
    #   고정 관문을 넘은 것보다 **자기가 건 조건을 견딘 것**이 강한 증거다.
    #
    #   비용 스트레스는 **실험으로 등록하지 않는다.** 반증 검사가 시도 수를
    #   올리면 DSR 이 깎여, 검증을 열심히 할수록 통과가 어려워진다.
    falsif: list = []
    try:
        import falsification as _fx  # noqa: PLC0415

        tests = list(hyp.get("falsification_criteria") or [])
        need_cost = any(_fx.classify(t)[0] == "cost_stress" for t in tests)
        cost_metrics = None
        if need_cost:
            stressed = run_backtest(market, dict(config,
                                                 cost_stress=_fx.COST_STRESS_MULT))
            bench = buy_and_hold_equity(market, dict(
                config, cost_stress=_fx.COST_STRESS_MULT))
            b0, b1 = bench[0][1], bench[-1][1]
            e0, e1 = stressed.equity[0][1], stressed.equity[-1][1]
            cost_metrics = {"excess_return_pct": round(
                (e1 / e0 - 1.0) * 100.0 - (b1 / b0 - 1.0) * 100.0, 4)}
            print(f"  비용 {_fx.COST_STRESS_MULT:g}배 스트레스: 초과수익 "
                  f"{cost_metrics['excess_return_pct']:+.2f}%p", flush=True)
        def _col(key):
            return [m[key] for _, m in wm if isinstance(m.get(key), (int, float))]

        falsif = _fx.run(tests, bt.get("metrics") or {},
                         window_metrics=_col("total_return"),
                         window_mdds=_col("max_drawdown"),
                         window_sharpes=_col("sharpe_rf0"),
                         cost_stress_metrics=cost_metrics)
        if falsif:
            print("  " + _fx.note(falsif), flush=True)
    except Exception as e:  # noqa: BLE001 - 반증을 못 돌려도 실험은 종결한다
        print(f"  ⚠ 자기반증 실행 실패: {type(e).__name__}: {str(e)[:110]}",
              flush=True)

    return {"experiment_id": bt["experiment_id"], "fragility": verdict,
            "fragility_flags": flags, "windows": len(wm),
            "falsification": [r.as_dict() for r in falsif],
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
    def __init__(self, hypothesis_row, datasets, *,
                 governed_stock_evidence=True):
        self._row = hypothesis_row
        self._datasets = datasets
        self._governed_stock_evidence = governed_stock_evidence
        self.updates: list = []
        self.update_sqls: list = []   # 어떤 문장이었는지도 본다(복귀 검사용)

    def execute(self, sql, params=()):
        self._last = (sql, params)
        if "update quant.hypotheses" in sql:
            self.updates.append(params)
            self.update_sqls.append(" ".join(str(sql).split()))
            # ▶ 지문을 기억한다. 실제 DB 는 UPDATE 한 값을 되읽을 수 있으므로
            #   가짜도 그래야 사전등록 대조가 진짜 경로를 검사한다 - 안 그러면
            #   늘 "위반" 이 나와 검사가 가드를 잘못 고발한다.
            if "material_fingerprint" in sql and params:
                self._fingerprint = params[0]

    def fetchone(self):
        if ("select exists" in getattr(self, "_last", ("", ()))[0].lower()
                and "quant.current_krx_stock_instrument_identity"
                in self._last[0]):
            return (self._governed_stock_evidence,)
        if "material_fingerprint from quant.hypotheses" in getattr(
                self, "_last", ("", ()))[0]:
            return (getattr(self, "_fingerprint", None),)
        return self._row

    def fetchall(self):
        # 매니페스트 조회는 (이름, 버전, source_versions) 3열이다. 데이터 사상이
        # 이 열들에서 유도되므로 가짜도 같은 모양이어야 진짜 경로를 검사한다.
        if "dataset_manifests" in getattr(self, "_last", ("", ()))[0]:
            out = []
            for d in self._datasets:
                name, _, ver = str(d).rpartition("/")
                out.append((name or str(d), ver or "v1",
                            {"market_bars": "ls_chart/1D"}))
            return out
        sql = getattr(self, "_last", ("", ()))[0]
        if (TRIAL_RESERVATION_KEY in sql
                or "from quant.experiments" in sql):
            return []
        return [(d,) for d in self._datasets]


class _FakeMarketCursor:
    """로컬 시장 DB 흉내. 커버리지 1열 5칸을 돌려준다."""

    def __init__(self, row): self._row = row
    def execute(self, sql, params=()): pass
    def fetchone(self): return self._row


class _FakeMarket:
    def __init__(self, row=(218985, "2024-01-01", "2026-08-09", 640, 350)):
        self._row = row

    def cursor(self):
        return _FakeMarketCursor(self._row)


class _FakeConn:
    def __init__(self, cursor):
        self._cur = cursor
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


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
    assert release_to_status(
        "SUPPORTED", "SUBMIT_TO_QA", failed=(), unmeasured=()) == "SUPPORTED"
    assert release_to_status(
        "SUPPORTED", "HOLD", failed=("pbo",),
        unmeasured=("pbo",)) == "INCONCLUSIVE"
    assert release_to_status(
        "SUPPORTED", "HOLD", failed=("excess_return",),
        unmeasured=()) == "REJECTED"
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
    r = orchestrate("h-1", conn=_FakeConn(cur), market_conn=_FakeMarket())
    assert r.verdict == "NOT_RUNNABLE" and not cur.updates, \
        "NOT_RUNNABLE 인데 상태를 건드렸다"

    row2 = ("h-2", "모멘텀 가설", {"type": "momentum"},
            ["krx-basket-daily/v1"], "PROPOSED")
    cur2 = _FakeCursor(row2, ["krx-basket-daily/v1"])
    r2 = orchestrate("h-2", conn=_FakeConn(cur2), market_conn=_FakeMarket(),
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
    r3 = orchestrate("h-2", conn=_FakeConn(cur3), market_conn=_FakeMarket(),
                     run_chain=lambda h, hid: {"experiment_id": "e-2",
                                               "fragility": "ROBUST"})
    assert r3.transitions[-1] == "RUNNING->INCONCLUSIVE", r3.transitions
    assert r3.release.get("decision") == "HOLD", r3.release

    cur4 = _FakeCursor(None, [])
    assert orchestrate("none", conn=_FakeConn(cur4), market_conn=_FakeMarket()).verdict == "NO_HYPOTHESIS"
    print("  오케스트레이션 경로       OK")
    _check_dataset_comes_from_hypothesis()
    print("  데이터셋=사상 결과       OK")
    _check_chain_failure_releases_hypothesis()
    print("  실패 체인 즉시 복귀       OK")
    _check_metrics_are_recorded_without_changing_verdict()
    print("  계측 확장·판정 불변       OK")
    _check_fragility_summary_is_recordable()
    print("  강건성 근거 원장 적재     OK")
    _check_no_trade_is_not_a_rejection()
    print("  거래 0 = 판정 불가        OK")
    _check_windows_are_persisted_incrementally()
    print("  창마다 적재(중단 견딤)    OK")
    _check_wfe_measures_without_changing_the_gate()
    print("  창 효율 계측(판정 불변)   OK")


def _check_wfe_measures_without_changing_the_gate():
    """**이유를 재되 합격선은 건드리지 않는다** (2026-08-14 조사).

    업계는 WFE(0.5~0.7 정상대)와 파라미터 안정성으로 "왜 무너졌나" 를 가르는데
    우리는 `positive_window_ratio` 하나로 FRAGILE 만 찍었다(전체 판정의 87%).
    임계 변경은 사전등록·결재 사안이므로 여기서는 **계측만** 늘린다.
    """
    wm = [("2024H1", {"sharpe_rf0": 0.4}), ("2024H2", {"sharpe_rf0": 0.6}),
          ("2025H1", {"sharpe_rf0": 0.5})]
    # 창 평균 0.5 / 전기간 1.0 = 0.5 (정상대 하단)
    assert walk_forward_efficiency(wm, 1.0) == 0.5
    # 전기간이 좋은데 창별이 나쁘면 낮게 나온다 = 구간 편중
    assert walk_forward_efficiency(wm, 2.5) == 0.2

    # ▶ **못 재면 안 적는다.** 분모가 0 근처면 비율이 폭발한다 - 진단이 아니라
    #   잡음이고, 그 잡음이 환류에 실리면 다음 기획이 엉뚱한 곳을 고친다.
    for bad in (0.0, 0.05, -0.09, None, "n/a"):
        assert walk_forward_efficiency(wm, bad) is None, bad
    # 창이 한 개뿐이면 평균이 그 자신이라 아무것도 말하지 않는다
    assert walk_forward_efficiency([("2024H1", {"sharpe_rf0": 0.4})], 1.0) is None
    assert walk_forward_efficiency([], 1.0) is None
    # 숫자가 아닌 창 값에 죽지 않는다(지어내지도 않는다)
    assert walk_forward_efficiency(
        [("a", {"sharpe_rf0": None}), ("b", {"sharpe_rf0": "x"})], 1.0) is None

    # 관문은 이 값을 보지 않는다 - 계측이 합격선을 흔들면 사전등록이 무너진다
    from walk_forward import FRAGILITY_RULES
    assert "walk_forward_efficiency" not in FRAGILITY_RULES, FRAGILITY_RULES
    assert "walk_forward_efficiency" in _OOS_KEYS, "환류에 안 실리면 아무도 못 본다"


def _check_windows_are_persisted_incrementally():
    """**창마다 적재한다 - 중단돼도 센 만큼은 남는다** (2026-08-14 실측).

    이 구간은 백테스트가 끝난 뒤에도 13분을 더 돈다. 창 21개를 전부 계산한
    뒤에 한 번에 저장하면 그 사이 워커가 재시작될 때 **통째로** 사라진다 -
    실측으로 한 시간에 워커가 4번 기동했고(다른 세션의 재시작), 그때마다
    실험은 COMPLETED 인데 창 지표만 0행인 상태가 남았다. 그 0행이
    `positive_window_ratio` 를 못 만들어 강건성 판정 근거가 통째로 비었다.

    요약(fragility)은 전 창이 모여야 의미가 있으므로 마지막에 한 번 쓴다 -
    부분 요약은 판정을 오도하므로 여기서 함께 고정한다.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_default_chain")
    body = ast.get_source_segment(src, fn) or ""
    assert "for w in windows:" in body, "창 루프가 사라졌다"
    assert "wm = [(w.label, run_window(" not in body, \
        "창을 전부 계산한 뒤 저장하는 형태로 돌아갔다 - 중단되면 통째로 잃는다"
    head = body.index("for w in windows:")
    tail = body.index("fragility_summary(")
    assert head < tail, "요약이 창 루프보다 먼저 온다"
    assert "conn.commit()" in body[head:tail], \
        "창 루프 안에서 커밋하지 않는다 - 중단되면 진행분이 사라진다"
    # 요약은 루프 **밖**에서 한 번만 - 창마다 요약을 쓰면 부분 판정이 남는다
    assert body.count("fragility_summary(") == 1, "요약을 여러 번 쓴다"


def _check_no_trade_is_not_a_rejection():
    """**실험이 성립 안 한 것을 가설의 죄로 적지 않는다** (2026-08-14 실측).

    유니버스가 비어 한 주도 못 산 실험 2건이 REJECT 로 굳었다(지표 전부 0,
    초과 -82.86%p). 원인이던 단위는 고쳤지만 같은 증상은 결측·필터·신호
    미선택으로도 온다 - 그때마다 기각되면 계열 예산까지 탄다.
    """
    # 실측 그대로: 거래도 수익도 0
    assert experiment_did_not_trade(
        {"turnover_total": 0.0, "total_return": 0.0, "max_drawdown": 0.0})
    # 회전율만 0 이고 수익이 있으면 그건 다른 사고다 - 여기서 삼키지 않는다
    assert not experiment_did_not_trade(
        {"turnover_total": 0.0, "total_return": 0.12})
    # 정상 실험은 건드리지 않는다
    assert not experiment_did_not_trade(
        {"turnover_total": 3.4, "total_return": -0.08})
    # ▶ **못 잰 것과 0 을 구분한다.** 구버전 실험엔 회전율이 없다 -
    #   없는 것을 사고로 몰면 옛 실험이 무더기로 무효가 된다.
    assert not experiment_did_not_trade({"total_return": 0.0})
    assert not experiment_did_not_trade({})
    assert not experiment_did_not_trade(None)
    # 숫자가 아닌 값에 죽지 않는다(지어내지도 않는다)
    assert not experiment_did_not_trade({"turnover_total": "n/a"})


def _check_fragility_summary_is_recordable():
    """**판정 근거가 원장에 남을 모양인가** (2026-08-14 실측).

    이 체인은 `fragility_summary` 를 부르고 요약을 `_summary` 로 버렸다.
    그 결과 창별 수익·MDD 는 314행 쌓이는데 관문이 실제로 보는 세 지표
    (`positive_window_ratio`·`worst_window_mdd`·`sharpe_std`)는 **0행**이었고,
    환류에는 `fragility` 실패만 찍혔다 - 왜 취약한지는 아무 데도 없었다.

    그래서 여기서 고정하는 것은 저장 SQL 이 아니라 **저장 가능성**이다:
    관문이 읽는 키가 요약에 숫자로 들어 있어야 insert 루프가 집는다. 키
    이름이 바뀌거나 문자열로 바뀌면 이 검사가 먼저 깨진다.
    """
    from walk_forward import FRAGILITY_RULES, fragility_summary  # noqa: PLC0415

    wm = [(lbl, {"total_return": r, "sharpe_rf0": s, "max_drawdown": m,
                 "test_days": 120})
          for lbl, r, s, m in [("2024H1", 0.10, 1.0, -0.10),
                               ("2024H2", -0.05, -0.5, -0.30),
                               ("2025H1", 0.20, 1.5, -0.08),
                               ("2025H2", 0.03, 0.2, -0.15)]]
    stats, flags, verdict = fragility_summary(wm)
    numeric = {k for k, v in stats.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
    for key in ("n_windows", "positive_window_ratio", "worst_window_mdd",
                "sharpe_std"):
        assert key in numeric, f"관문이 읽는 {key} 가 숫자로 안 남는다: {stats}"
    # 위 픽스처는 한 창이 MDD -30% 로 임계(-25%)를 넘는다 - 플래그가 서야
    # 하고, 그 이유가 flags 로 나와야 dimensions 에 실린다.
    assert verdict == "FRAGILE", (verdict, flags)
    assert "DEEP_WINDOW_MDD" in flags, flags
    assert FRAGILITY_RULES["max_worst_window_mdd"] == -0.25, \
        "임계를 바꿨다면 근거와 CEO 결재를 함께 남긴다"
    # 판정 불가는 판정 결과다 - 창이 없어도 죽지 않고, 요약도 숫자로 남는다.
    empty_stats, empty_flags, empty_verdict = fragility_summary([])
    assert empty_verdict == "INSUFFICIENT", empty_verdict
    assert empty_flags == ["NO_WINDOWS"], empty_flags
    assert isinstance(empty_stats.get("n_windows"), int), empty_stats
    # 짧은 미시구조 창은 10일 자로 만들었으므로 같은 자로 판정한다. 최종
    # 승격의 DSR/부트스트랩 관문을 낮추는 것이 아니라 탐색 피드백을 보존한다.
    short = [(f"W{i:02d}", {"total_return": 0.01, "sharpe_rf0": 0.2,
                              "max_drawdown": -0.05, "test_days": 10})
             for i in range(1, 5)]
    short_stats, _, short_verdict = fragility_summary(short, min_test_days=10)
    assert short_stats["n_windows"] == 4 and short_verdict != "INSUFFICIENT", \
        (short_stats, short_verdict)


def _check_metrics_are_recorded_without_changing_verdict():
    """**계측을 늘려도 판정은 그대로다** (2026-08-14, 2층 관문 결재 1단계).

    랭크-IC·회전율은 이미 원장에 있었는데 _OOS_KEYS 화이트리스트에서 잘려
    outcomes 에 한 번도 안 실렸다. 위험조정 계측(M²·alpha·appraisal)도 같은
    경로로 싣는다. 두 가지를 함께 고정한다 - (a) 새 지표가 환류에 실린다,
    (b) 관문 판정 함수는 이 지표들을 보지 않는다(합격선 변경 아님).
    """
    from release_gate import CRITERIA, evaluate

    need = {"m2_excess_ann_pct", "alpha_ann_pct", "appraisal_ratio",
            "turnover_total", "signal_ic", "signal_ic_t"}
    assert need <= set(_OOS_KEYS), sorted(need - set(_OOS_KEYS))
    # 판정 부산물(교훈)이 읽는 두 이름과 겹치지 않아야 '판정 불변' 이 성립한다
    assert "excess_return_pct" in _OOS_KEYS and "information_ratio" in _OOS_KEYS

    # 같은 지표 dict 에 신규 계측을 넣어도 evaluate 결과가 동일한가
    base = {"excess_return_pct": 130.3, "information_ratio": 0.17,
            "max_drawdown_pct": -21.3, "turnover": 12.0,
            "deflated_sharpe": 0.9968, "bootstrap_ci_low": 0.78,
            "bootstrap_ci_high": 1.41}
    rich = dict(base, m2_excess_ann_pct=-4.2, alpha_ann_pct=3.1,
                appraisal_ratio=0.22, signal_ic=0.035, signal_ic_t=3.16,
                strategy_ann_vol_pct=15.0, benchmark_ann_vol_pct=31.0)
    a = evaluate(base, fragility="ROBUST")
    b = evaluate(rich, fragility="ROBUST")
    assert (sorted(a.passed), sorted(a.failed)) == (sorted(b.passed), sorted(b.failed)), \
        f"계측 추가가 판정을 바꿨다: {a.failed} vs {b.failed}"
    assert CRITERIA["min_information_ratio"] == 0.5, "관문 임계가 바뀌었다"

    # 판정 리포트 꼬리에 계측이 실린다 - 원장에 남아야 다음 기획안이 본다
    note = _gate_note(a, rich)
    assert "계측:" in note and "M²" in note and "AR" in note, note
    # 못 잰 지표는 적지 않는다(0 으로 채우지 않는다)
    assert "IC t" not in _gate_note(a, base), _gate_note(a, base)


def _check_chain_failure_releases_hypothesis():
    """**실패한 체인이 가설을 RUNNING 에 가두지 않는다** (2026-08-13 실측).

    퀀트 카드의 CLI 직접 호출 경로는 작업 큐가 없어서, 체인이 죽으면 실패가
    어디에도 안 남고 가설만 RUNNING 에 갇혔다 - e2379857 이 job 0건·실험 행
    0건·"사유 기록 없음" 으로 하루 3회 스톨 회수(회당 30분 낭비). 실패 즉시
    PROPOSED 복귀 + 예외 재전파(삼키면 호출부가 실패를 모른다)를 고정한다.
    """
    row = ("h-9", "모멘텀 가설", {"type": "momentum"},
           ["krx-basket-daily/v1"], "PROPOSED")
    cur = _FakeCursor(row, ["krx-basket-daily/v1"])

    def boom(h, hid):
        raise RuntimeError("체인 폭발")

    try:
        orchestrate("h-9", conn=_FakeConn(cur), market_conn=_FakeMarket(),
                    run_chain=boom)
        raise AssertionError("예외가 삼켜졌다 - 호출부가 실패를 알 수 없다")
    except RuntimeError as e:
        assert "체인 폭발" in str(e), e
    joined = " ".join(cur.update_sqls)
    assert "status='PROPOSED'" in joined, \
        f"실패 후 PROPOSED 복귀가 없다 - RUNNING 감금 재발: {cur.update_sqls}"


def _check_dataset_comes_from_hypothesis():
    """**데이터셋은 사상 결과에서 온다 - 상수가 아니다.** (2026-08-12)

    `resolve` 가 계산해 둔 값을 버리고 모듈 상수를 쓰고 있었다. 그래서 v3 를
    만들어도 실험은 영원히 v2 로 돌았다. `resolve` 는 같은 이름이면 최신 버전을
    고르므로, 사상 결과를 읽기만 하면 새 데이터셋이 저절로 쓰인다.
    """
    assert dataset_of({"required_data_products": ["krx-basket-daily/v3"]})         == ("krx-basket-daily", "v3")
    # 이름에 슬래시가 여럿이어도 마지막이 버전이다
    assert dataset_of({"required_data_products": ["a/b/v9"]}) == ("a/b", "v9")
    # 사상 결과가 없거나 모양이 이상하면 **조용히 다른 데이터로 돌지 않는다**
    assert dataset_of({}) == (DATASET_NAME, DATASET_VERSION)
    assert dataset_of({"required_data_products": ["market_bars"]})         == (DATASET_NAME, DATASET_VERSION)


def _check_gate_note_carries_the_distance():
    """**기각에도 거리를 적는다.** (2026-08-12 사고 원문)

    실험 75a6d09e(momentum) 는 초과 +157.51%p · IR 1.26 · DSR 0.976 을 내고
    기각됐는데 환류엔 `fragility_fragile` 한 줄만 남았다. 리서치는 그걸
    "엣지가 없다" 로 읽고 다른 엣지를 설계했다 - 실제로 막은 건 낙폭이었다.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from release_gate import evaluate as gate_evaluate
    from release_gate import metrics_from_rows

    rows = [
        ("excess_return_pct", 157.51, "TEST"),
        ("information_ratio", 1.2552, "TEST"),
        ("deflated_sharpe", 0.9762, "TEST"),
        ("bootstrap_ci_low", -0.0029, "TEST"),
        ("max_drawdown", -0.5052, "TEST"),
        ("turnover_total", 114.8667, "TEST"),
        ("max_drawdown", -0.0709, "WALK_FORWARD"),
    ]
    m = metrics_from_rows(rows)
    note = _gate_note(gate_evaluate(m, fragility="FRAGILE"), m)

    # ▶ **막은 조항이 이름과 거리로 나와야 한다.** "기각" 만으로는 다음 설계가
    #   무엇을 바꿔야 하는지 알 수 없다.
    assert "max_drawdown -50.52%" in note, note
    assert "15.52% 모자람" in note, note
    assert "fragility" in note, note
    # 통과한 조항 수가 보여야 "거의 다 됐다" 를 읽는다
    assert "관문 " in note and "/" in note, note
    # 미측정을 거리로 위장하지 않는다
    m2 = dict(m); m2.pop("deflated_sharpe")
    note2 = _gate_note(gate_evaluate(m2, fragility="ROBUST"), m2)
    assert "deflated_sharpe 미측정" in note2, note2

    # ▶ **못 잰 것이 교훈이 되면 없는 사실이 원장에 남는다.** momentum 은 PBO 를
    #   재지 않았는데 환류에 OVERFIT_PBO(과적합됐다)가 적혔다.
    from factory_bridge import lessons_from

    d = gate_evaluate(m, fragility="FRAGILE")
    assert "pbo" in d.unmeasured, d.unmeasured
    assert "pbo" in d.failed, "미측정이 차단을 못 하면 관문이 눈을 감는다"
    kept = [x for x in d.failed if x not in set(d.unmeasured)]
    assert "OVERFIT_PBO" not in lessons_from(failed_criteria=kept,
                                             fragility="FRAGILE"), "안 잰 것을 과적합으로 적었다"
    assert "BEAR_FRAGILE" in lessons_from(failed_criteria=kept,
                                          fragility="FRAGILE"), "실제로 넘은 낙폭이 교훈에서 빠졌다"


def _check_signal_ic_is_recorded():
    """**신호 IC 를 매 실험에 남긴다.** (2026-08-12 실측)

    격자가 `alive` 를 판정할 때 IC 를 우선한다 - 백테스트의 포트폴리오 구성과
    신호의 횡단면 예측력을 분리해야 하기 때문이다. 단 IC 는 집중도·데이터
    결함을 대신 잡는 지표가 아니므로 pnl 집중도 관문과 함께 본다. IC 를 안
    남기면 격자가 초과수익으로 떨어져 그 칸을 "살아있음" 으로 오판한다.
    """
    import ast

    src = Path(__file__).read_text(encoding="utf-8")
    chain = next(n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.FunctionDef) and n.name == "_default_chain")
    body = ast.get_source_segment(src, chain) or ""
    assert "signal_ic" in body and "_ic.summarize" in body, \
        "판정 사슬이 신호 IC 를 안 잰다"
    for name in ("signal_ic_t", "signal_ic_hit_rate", "signal_ic_breadth"):
        assert f'"{name}"' in body, f"{name} 을 안 남긴다 - 격자가 못 읽는다"
    # **자기 연결로 써야 한다** (2026-08-13 실측). 앞 구간이 닫은 conn 을
    # 재사용해 매 실험 InterfaceError 로 죽었고, IC 는 로그에만 남고 원장은
    # 0건이었다 - 측정되는데 저장이 죽으면 자격 검사가 영영 미측정이다.
    assert "_c2 = connect_writer" in body,           "IC 기록이 공유 conn 을 재사용한다 - 닫힌 연결이면 매번 죽는다"
    assert "_ic_horizon = signal_horizon(config)" in body, \
        "IC 가 사전등록 horizon 대신 형성창을 예측기간으로 쓴다"
    assert '"signal_ic_version": _ic.IC_VERSION' in body, \
        "IC 계산 버전이 원장에 없어 과거의 잘못된 값과 구분할 수 없다"
    assert signal_horizon({"horizon_days": 2, "lookback_days": 20}) == 2
    assert signal_horizon({"holding_horizon": 5, "lookback_days": 20}) == 5
    assert signal_horizon({"lookback_days": 20}) == 20
    # **격자가 읽는 이름과 같아야 한다** - 다르면 조용히 안 보인다(오늘 12번)
    import grid  # noqa: PLC0415
    assert "signal_ic_t" in grid._SQL, "격자가 읽는 지표 이름과 갈렸다"
    # IC 가 실패해도 실험은 종결돼야 한다 - 부수 검사가 본 검사를 죽이면 안 된다
    assert "IC 실패" in body and "except Exception" in body
    print("  신호 IC 적재             OK")


def _check_self_falsification_is_wired():
    """**가설이 스스로 건 반증을 실제로 돌린다.** (2026-08-12)

    계약은 "반증 없는 가설은 미완성" 이라며 강제하는데 아무도 돌리지 않았다 -
    기획안 10건에 49개가 저장만 돼 있었다. 만들어 놓고 안 부르는 것은
    오늘만 세 번째다(관문 336줄, 정의만 된 검사, 이것).
    """
    import ast

    src = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    chain = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and n.name == "_default_chain")
    body = ast.get_source_segment(src, chain) or ""
    assert "falsification" in body and "_fx.run(" in body, \
        "판정 사슬이 자기반증을 안 돌린다 - 계약이 강제한 것을 무시한다"
    assert "cost_stress" in body, "비용 스트레스를 안 건다 - 가장 많이 요청된 반증이다"
    # **반증 재실행이 실험으로 등록되면 안 된다** - 시도 수가 올라 DSR 이 깎인다
    idx = body.index("_fx.run(")
    seg = body[max(0, idx - 1400):idx]
    assert "register_and_run" not in seg, \
        "비용 스트레스가 실험으로 등록된다 - 검증할수록 DSR 이 깎인다"

    # 반증 요약이 환류 문장에 실려야 다음 기획안이 읽는다
    orch = ast.get_source_segment(src, next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
        and n.name == "orchestrate")) or ""
    assert "_fx.note(" in orch and "gate_note" in orch, \
        "자기반증 결과가 환류에 안 실린다"
    print("  자기반증이 사슬에 걸림   OK")


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
    # 정의만 해 두고 부르지 않던 검사 - 안 부르는 검사는 검사가 아니다
    _check_dataset_comes_from_hypothesis()
    print("  데이터셋 상수 아님       OK")
    _check_gate_note_carries_the_distance()
    print("  기각에도 관문 거리 적재   OK")
    _check_self_falsification_is_wired()
    _check_signal_ic_is_recorded()
    print("오케스트레이터 15개 영역 통과. 실행은 --run [--hypothesis <id>]")
