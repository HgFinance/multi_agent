#!/usr/bin/env python3
"""Quant V2 계약 - HypothesisSpecV2 / ExperimentCardV1 / CalibrationGuidelineV1.

담당: 재일 (퀀트·백테스트본부)
근거: docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md
        7.2절(가설 사전 등록), 7.3절(검증 표준), 7.5절(ExperimentCardV1),
        7.6절(Quant 상태 머신), 8.2절(CalibrationGuidelineV1), Phase RQF-0
      docs/HEDGE_FUND_MASTER_PLAN.md 5.11절
      CLAUDE.md 개발원칙 5 "미래 데이터가 Backtest 와 과거 Replay 에 들어가지 않게 한다"

▶ 이 계약이 막는 단 하나의 문제: **결과를 보고 가설을 고치는 것**
  Backtest 를 돌려보고 Feature 를 바꾸고 다시 돌리면, 그 성과는 실력이 아니라
  선택의 산물이다. 그런데 지금은 그것을 막는 코드가 없다 - 가설 파일을 고쳐
  다시 돌리면 아무도 모른다.

  계약이 세 겹으로 막는다.
    1) PREREGISTERED 이후 실질 필드를 바꾸면 **같은 ID 를 수정하지 못하고**
       version 을 올려야 한다(7.2절). 무엇이 '실질' 인지 코드가 정의한다.
    2) trial_family_id 로 비슷한 실험 횟수를 누적해 **몇 번 만에 나온 결과인지**
       Card 에 남긴다. 12번째 시도에서 나온 Sharpe 1.5 는 1번째와 다르다.
    3) ExperimentCard 는 자기가 어느 hypothesis_version 을 돌렸는지 들고 있어야
       하며, 사전 등록되지 않은 가설로는 만들 수 없다.

▶ 여기서 강제하지 않는 것
  실제 통계 검정(DSR·PBO·CPCV)은 계약이 하지 않는다. 계약은 **그 결과를 담을
  자리와 '안 돌렸다'를 구분할 방법**만 준다 - null 과 NOT_RUN 을 같게 취급하면
  "검증했는데 값이 없다" 와 "검증을 안 했다" 가 섞인다.

  Production 승격도 하지 않는다. 퀀트는 SUBMIT_TO_QA 까지만 하고 승격은
  CEO·Risk·QA 승인 경로다(CLAUDE.md 권한 분리).

실행: python contracts/quant_v2.py     # 자체 점검 (네트워크·DB 없음)
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "quant-contracts-v2"

Probability = Annotated[float, Field(ge=0.0, le=1.0)]


class HypothesisStatus(str, Enum):
    """7.6절 상태 머신. 순서를 건너뛰지 않는다."""

    INTAKE = "INTAKE"
    PREREGISTERED = "PREREGISTERED"
    DATASET_CERTIFIED = "DATASET_CERTIFIED"
    RUNNING = "RUNNING"
    ROBUSTNESS_REVIEW = "ROBUSTNESS_REVIEW"
    SUPPORTED = "SUPPORTED"
    REJECTED = "REJECTED"
    NEEDS_DATA = "NEEDS_DATA"


# 상태 전이 허용표. 표에 없는 전이는 거부한다 - 조용히 건너뛰면
# '검증 안 하고 SUPPORTED' 가 가능해진다.
_ALLOWED: dict[HypothesisStatus, frozenset[HypothesisStatus]] = {
    HypothesisStatus.INTAKE: frozenset({HypothesisStatus.PREREGISTERED,
                                        HypothesisStatus.REJECTED}),
    HypothesisStatus.PREREGISTERED: frozenset({HypothesisStatus.DATASET_CERTIFIED,
                                               HypothesisStatus.NEEDS_DATA,
                                               HypothesisStatus.REJECTED}),
    HypothesisStatus.DATASET_CERTIFIED: frozenset({HypothesisStatus.RUNNING,
                                                   HypothesisStatus.REJECTED}),
    HypothesisStatus.RUNNING: frozenset({HypothesisStatus.ROBUSTNESS_REVIEW,
                                         HypothesisStatus.NEEDS_DATA,
                                         HypothesisStatus.REJECTED}),
    HypothesisStatus.ROBUSTNESS_REVIEW: frozenset({HypothesisStatus.SUPPORTED,
                                                   HypothesisStatus.REJECTED,
                                                   HypothesisStatus.NEEDS_DATA}),
    HypothesisStatus.NEEDS_DATA: frozenset({HypothesisStatus.PREREGISTERED,
                                            HypothesisStatus.REJECTED}),
    HypothesisStatus.SUPPORTED: frozenset(),   # 이후는 QA·Risk 경로 (우리 권한 밖)
    HypothesisStatus.REJECTED: frozenset(),
}


class ValidationResult(str, Enum):
    """PASS/FAIL 과 **NOT_RUN 을 반드시 구분한다.**

    안 돌린 것을 통과로 세는 순간 검증표 전체가 장식이 된다(fail-closed).
    """

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


class Decision(str, Enum):
    REJECT = "REJECT"
    REVISE = "REVISE"
    SUBMIT_TO_QA = "SUBMIT_TO_QA"   # 승격이 아니다. 제출이다.


class GuidelineStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    SHADOW = "SHADOW"       # QA 승인 전에는 Shadow Profile 에서만 (8.2절)
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TransitionError(ValueError):
    """상태 머신을 건너뛰었다."""


class PreregistrationError(ValueError):
    """사전 등록된 가설을 사후에 고치려 했다."""


def _aware(v: datetime, field: str) -> datetime:
    if v.tzinfo is None:
        raise ValueError(f"{field} 는 timezone 이 있어야 한다")
    return v.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# HypothesisSpecV2 - Backtest 결과를 보기 전에 불변으로 저장한다
# ---------------------------------------------------------------------------

class HypothesisOrigin(_Base):
    """이 가설이 어디서 왔는가. 리서치 Claim 까지 Lineage 를 잇는다(RQF-2)."""

    research_packet_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()


class Split(_Base):
    """사전 등록된 분할. 결과를 보고 고치면 안 되는 대표적인 것."""

    name: str = Field(min_length=1)
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    embargo_days: int = Field(ge=0, le=60)

    @field_validator("train_start", "train_end", "test_start", "test_end")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware(v, "split 시각")

    @model_validator(mode="after")
    def _ordered_and_purged(self):
        if not (self.train_start < self.train_end):
            raise ValueError(f"{self.name}: train_start < train_end 여야 한다")
        if not (self.test_start < self.test_end):
            raise ValueError(f"{self.name}: test_start < test_end 여야 한다")
        # ▶ 이것이 개발원칙 5 다. test 가 train 보다 앞서면 미래로 학습한 것이다.
        if self.test_start < self.train_end:
            raise ValueError(
                f"{self.name}: test_start 가 train_end 보다 앞선다 - "
                f"미래 데이터로 학습한 것이고, 그 성과는 실력이 아니다")
        gap_days = (self.test_start - self.train_end).total_seconds() / 86400.0
        if gap_days < self.embargo_days:
            raise ValueError(
                f"{self.name}: embargo {self.embargo_days}일을 선언했는데 실제 간격은 "
                f"{gap_days:.1f}일이다 - Label 기간이 겹쳐 누수가 남는다")
        return self


class HypothesisSpecV2(_Base):
    hypothesis_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    origin: HypothesisOrigin
    strategy_family: str = Field(min_length=1)
    economic_rationale: str = Field(min_length=10)
    # ▶ 경쟁 설명을 **필수**로 둔다. "왜 이 신호가 아니라 단순 시장 반등이
    #   아닌가" 를 미리 적지 않으면, 나중에 어떤 결과가 나와도 설명이 붙는다.
    competing_explanation: str = Field(min_length=10)
    universe_version: str = Field(min_length=1)
    decision_frequency: str = Field(min_length=1)
    holding_horizon: str = Field(min_length=1)
    features: tuple[str, ...] = Field(min_length=1)
    label: dict[str, Any] = Field(min_length=1)
    baseline: str = Field(min_length=1)
    entry_exit_rules: dict[str, Any] = Field(default_factory=dict)
    cost_model_version: str = Field(min_length=1)
    trial_family_id: str = Field(min_length=1)
    trial_budget: int = Field(ge=1, le=200)
    preregistered_splits: tuple[Split, ...] = Field(min_length=1)
    falsification_tests: tuple[str, ...] = Field(min_length=1)
    status: HypothesisStatus = HypothesisStatus.INTAKE
    preregistered_at: datetime | None = None

    @field_validator("preregistered_at")
    @classmethod
    def _tz(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _aware(v, "preregistered_at")

    @model_validator(mode="after")
    def _preregistration_is_real(self):
        past = (HypothesisStatus.PREREGISTERED, HypothesisStatus.DATASET_CERTIFIED,
                HypothesisStatus.RUNNING, HypothesisStatus.ROBUSTNESS_REVIEW,
                HypothesisStatus.SUPPORTED)
        if self.status in past and self.preregistered_at is None:
            raise ValueError(
                f"status={self.status.value} 인데 preregistered_at 이 없다 - "
                f"언제 등록했는지 모르면 사전 등록이 아니다")
        names = [s.name for s in self.preregistered_splits]
        if len(set(names)) != len(names):
            raise ValueError("preregistered_splits 이름 중복")
        return self

    # ▶ 무엇을 바꾸면 새 Version 이어야 하는가 (7.2절)
    #   "Feature, Label, Split, 비용 또는 폐기 기준" + 유니버스·지평·baseline.
    #   status·version·preregistered_at 은 실질이 아니다.
    MATERIAL_FIELDS: ClassVar[tuple[str, ...]] = (
        "features", "label", "preregistered_splits", "cost_model_version",
        "falsification_tests", "universe_version", "decision_frequency",
        "holding_horizon", "baseline", "entry_exit_rules", "strategy_family",
    )

    def material_fingerprint(self) -> str:
        """실질 내용의 해시. 같은 내용이면 같은 값이다(결정론).

        모델 전체를 한 번 직렬화한 뒤 실질 필드만 골라낸다. 필드마다
        모델 직렬화기를 부르면 값 타입이 모델과 달라 경고가 나고, 무엇보다
        **직렬화 실패가 조용히 default=str 로 떨어져** 서로 다른 가설이 같은
        지문을 가질 수 있다 - 그러면 사전 등록 검사 자체가 무력해진다.
        """
        dumped = self.model_dump(mode="json")
        missing = [f for f in self.MATERIAL_FIELDS if f not in dumped]
        if missing:
            # 필드 이름이 바뀌었는데 목록을 안 고친 경우. 조용히 빼면 그 필드는
            # 마음대로 바꿔도 새 Version 이 안 된다.
            raise ValueError(f"MATERIAL_FIELDS 에 없는 필드가 있다: {missing}")
        payload = {f: dumped[f] for f in self.MATERIAL_FIELDS}
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def revise(self, **changes) -> HypothesisSpecV2:
        """실질 변경이면 **version 을 올린 새 객체**를 낸다. 원본은 그대로다.

        사전 등록 이후 같은 ID 를 고치는 경로를 코드에 두지 않는다 - 두면 쓰인다.
        """
        candidate = self.model_copy(update=changes)
        if candidate.material_fingerprint() == self.material_fingerprint():
            # 실질이 안 변했으면 version 을 올리지 않는다 - 무의미한 버전 인플레
            return candidate
        if self.status is HypothesisStatus.INTAKE:
            return candidate       # 등록 전에는 자유롭게 고친다
        return candidate.model_copy(update={
            "version": self.version + 1,
            "status": HypothesisStatus.INTAKE,
            "preregistered_at": None,
        })

    def transition(self, to: HypothesisStatus, *, now: datetime | None = None
                   ) -> HypothesisSpecV2:
        if to not in _ALLOWED[self.status]:
            raise TransitionError(
                f"{self.status.value} -> {to.value} 는 허용되지 않는다 - "
                f"가능: {sorted(s.value for s in _ALLOWED[self.status]) or '없음(종착)'}")
        upd: dict[str, Any] = {"status": to}
        if to is HypothesisStatus.PREREGISTERED and self.preregistered_at is None:
            upd["preregistered_at"] = _aware(now or datetime.now(timezone.utc),
                                             "preregistered_at")
        return self.model_copy(update=upd)


# ---------------------------------------------------------------------------
# ExperimentCardV1 - 한 번의 실험이 남기는 것
# ---------------------------------------------------------------------------

class Validation(_Base):
    purged_walk_forward: ValidationResult = ValidationResult.NOT_RUN
    cpcv: ValidationResult = ValidationResult.NOT_RUN
    deflated_sharpe: float | None = None
    probability_of_backtest_overfitting: Probability | None = None
    bootstrap_ci_low: float | None = None
    bootstrap_ci_high: float | None = None

    @model_validator(mode="after")
    def _ci_ordered(self):
        lo, hi = self.bootstrap_ci_low, self.bootstrap_ci_high
        if (lo is None) != (hi is None):
            raise ValueError("bootstrap CI 는 양쪽을 함께 적는다 - 한쪽만 있으면 구간이 아니다")
        if lo is not None and lo > hi:
            raise ValueError(f"bootstrap CI 가 뒤집혔다: [{lo}, {hi}]")
        return self


class ExperimentCardV1(_Base):
    experiment_id: str = Field(min_length=1)
    hypothesis_id: str = Field(min_length=1)
    # ▶ 어느 **버전** 을 돌렸는지 없으면 사전 등록이 무의미하다.
    #   7.5절 원형에는 없지만 없으면 계약이 성립하지 않아 추가했다.
    hypothesis_version: int = Field(ge=1)
    hypothesis_fingerprint: str = Field(min_length=8)
    dataset_manifest_id: str = Field(min_length=1)
    dataset_hash: str = Field(min_length=8)
    code_hash: str = Field(min_length=8)
    dependency_lock_hash: str = Field(min_length=8)
    seed: int
    trial_family_id: str = Field(min_length=1)
    trial_number: int = Field(ge=1)
    cost_model_version: str = Field(min_length=1)
    validation: Validation
    oos_metrics: dict[str, float] = Field(default_factory=dict)
    regime_breakdown: dict[str, dict[str, float]] = Field(default_factory=dict)
    capacity: dict[str, float] = Field(default_factory=dict)
    failures: tuple[str, ...] = ()
    decision: Decision
    lineage: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _decision_is_earned(self):
        v = self.validation
        if self.decision is Decision.SUBMIT_TO_QA:
            # P0 검증(7.3절)을 안 돌리고 제출할 수 없다
            if v.purged_walk_forward is not ValidationResult.PASS:
                raise ValueError(
                    f"SUBMIT_TO_QA 인데 purged_walk_forward={v.purged_walk_forward.value} - "
                    f"NOT_RUN 을 통과로 세지 않는다(P0 검증)")
            if not self.oos_metrics:
                raise ValueError("SUBMIT_TO_QA 인데 oos_metrics 가 비었다")
            if not self.regime_breakdown:
                raise ValueError(
                    "SUBMIT_TO_QA 인데 regime_breakdown 이 비었다 - "
                    "특정 구간 의존성을 확인하지 않고 제출하지 않는다(7.3절 P0)")
            if v.bootstrap_ci_low is None:
                raise ValueError(
                    "SUBMIT_TO_QA 인데 bootstrap CI 가 없다 - "
                    "표본 불확실성을 표현하지 않은 성과는 성과가 아니다(7.3절 P0)")
            if self.failures:
                raise ValueError(
                    f"SUBMIT_TO_QA 인데 failures 가 남아 있다: {list(self.failures)}")
        return self

    def matches(self, spec: HypothesisSpecV2) -> bool:
        """이 Card 가 그 가설의 **그 버전** 을 돌린 것인가."""
        return (self.hypothesis_id == spec.hypothesis_id
                and self.hypothesis_version == spec.version
                and self.hypothesis_fingerprint == spec.material_fingerprint())


def card_for(spec: HypothesisSpecV2, **kw) -> ExperimentCardV1:
    """사전 등록된 가설로만 Card 를 만든다. 지문을 코드가 채운다.

    호출자가 지문을 손으로 넣게 두지 않는다 - 손으로 넣으면 어긋난 값이 들어간다.
    """
    if spec.status is HypothesisStatus.INTAKE:
        raise PreregistrationError(
            f"{spec.hypothesis_id}: INTAKE 상태에서 실험 Card 를 만들 수 없다 - "
            f"결과를 보기 전에 등록해야 사전 등록이다")
    return ExperimentCardV1(
        hypothesis_id=spec.hypothesis_id,
        hypothesis_version=spec.version,
        hypothesis_fingerprint=spec.material_fingerprint(),
        trial_family_id=spec.trial_family_id,
        cost_model_version=spec.cost_model_version,
        **kw,
    )


def trial_pressure(spec: HypothesisSpecV2, cards: tuple[ExperimentCardV1, ...]) -> dict:
    """같은 Family 에서 몇 번째 시도인가 - 다중 실험 편향의 크기.

    12번째 시도에서 나온 Sharpe 1.5 는 1번째와 다르다. 그 사실을 숨기지 않는다.
    """
    same = [c for c in cards if c.trial_family_id == spec.trial_family_id]
    used = len(same)
    return {
        "trial_family_id": spec.trial_family_id,
        "trials_used": used,
        "trial_budget": spec.trial_budget,
        "over_budget": used > spec.trial_budget,
        "submitted": sum(1 for c in same if c.decision is Decision.SUBMIT_TO_QA),
    }


# ---------------------------------------------------------------------------
# CalibrationGuidelineV1 - 검증된 짧은 운영 교훈 (8.2절)
# ---------------------------------------------------------------------------

class CalibrationGuidelineV1(_Base):
    candidate_id: str = Field(min_length=1)
    scope: str = Field(min_length=1)            # 예: research.news_sentiment.5d
    failure_pattern: str = Field(min_length=5)
    supporting_case_ids: tuple[str, ...] = ()
    minimum_cases: int = Field(default=30, ge=1)
    proposed_change: str = Field(min_length=5)
    baseline_eval_id: str | None = None
    challenger_eval_id: str | None = None
    heldout_delta: dict[str, float] = Field(default_factory=dict)
    expires_at: datetime
    status: GuidelineStatus = GuidelineStatus.CANDIDATE
    # QA 승인은 문장이 아니라 참조로 남긴다 (마스터플랜 590-596: 자기 승인 금지)
    qa_approval_id: str | None = None

    @field_validator("expires_at")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware(v, "expires_at")

    @model_validator(mode="after")
    def _promotion_is_earned(self):
        if self.status is GuidelineStatus.ACTIVE:
            if len(self.supporting_case_ids) < self.minimum_cases:
                raise ValueError(
                    f"{self.candidate_id}: ACTIVE 인데 근거 사례가 "
                    f"{len(self.supporting_case_ids)}건 < 최소 {self.minimum_cases}건")
            if not (self.baseline_eval_id and self.challenger_eval_id):
                raise ValueError(
                    f"{self.candidate_id}: ACTIVE 인데 baseline/challenger Eval 이 없다 - "
                    f"비교하지 않은 개선은 개선이 아니다")
            if not self.heldout_delta:
                raise ValueError(
                    f"{self.candidate_id}: ACTIVE 인데 heldout_delta 가 비었다 - "
                    f"숨겨진 검증 구간 결과 없이 승격하지 않는다")
            if not self.qa_approval_id:
                raise ValueError(
                    f"{self.candidate_id}: ACTIVE 인데 QA 승인 참조가 없다 - "
                    f"자기 산출물의 Production 승인을 단독 수행하지 않는다")
        return self

    def is_effective(self, as_of: datetime) -> bool:
        """이 시점에 적용되는가. 만료를 조용히 넘기지 않는다."""
        return self.status is GuidelineStatus.ACTIVE and _aware(as_of, "as_of") < self.expires_at


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _dt(y, m, d) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


def _split(**kw) -> Split:
    base = dict(name="fold1", train_start=_dt(2025, 1, 1), train_end=_dt(2025, 6, 1),
                test_start=_dt(2025, 6, 11), test_end=_dt(2025, 9, 1), embargo_days=5)
    base.update(kw)
    return Split(**base)


def _spec(**kw) -> HypothesisSpecV2:
    base = dict(
        hypothesis_id="hyp_1", version=1, origin=HypothesisOrigin(),
        strategy_family="event_driven",
        economic_rationale="공시 충격 이후 유동성 회복 속도가 후속 수익률과 관련된다",
        competing_explanation="단순 시장 반등 또는 업종 공통 충격일 수 있다",
        universe_version="krx-liquid-v1", decision_frequency="5m",
        holding_horizon="5d", features=("amihud_5d",), label={"kind": "fwd_ret_5d"},
        baseline="sector_neutral_momentum", cost_model_version="krx-cost-v1",
        trial_family_id="liquidity_recovery_v1", trial_budget=12,
        preregistered_splits=(_split(),),
        falsification_tests=("무작위 라벨에서 유의하면 폐기",))
    base.update(kw)
    return HypothesisSpecV2(**base)


def _card(spec: HypothesisSpecV2, **kw) -> ExperimentCardV1:
    base = dict(experiment_id="exp_1", dataset_manifest_id="ds_1",
                dataset_hash="sha256:aa" * 8, code_hash="sha256:bb" * 8,
                dependency_lock_hash="sha256:cc" * 8, seed=42, trial_number=1,
                validation=Validation(), decision=Decision.REJECT)
    base.update(kw)
    return card_for(spec, **base)


def _rejects(fn, needle, label):
    try:
        fn()
    except Exception as e:
        assert needle in str(e), f"{label}: 다른 이유로 거부\n{e}"
        return
    raise AssertionError(f"{label}: 통과하면 안 되는데 통과했다")


def _check_split_leakage():
    _split()
    _rejects(lambda: _split(test_start=_dt(2025, 5, 1)),
             "미래 데이터로 학습", "test 가 train 보다 앞섬")
    _rejects(lambda: _split(embargo_days=30),
             "embargo", "선언한 embargo 보다 간격이 짧음")
    _rejects(lambda: _split(train_end=_dt(2024, 1, 1)),
             "train_start < train_end", "train 순서 뒤집힘")
    print("  Split 누수 차단          OK")


def _check_preregistration_immutable():
    s = _spec().transition(HypothesisStatus.PREREGISTERED, now=_dt(2026, 8, 3))
    assert s.preregistered_at is not None
    fp = s.material_fingerprint()

    # 실질 변경 -> 새 version, 등록 해제
    revised = s.revise(features=("amihud_5d", "roll_spread"))
    assert revised.version == 2, revised.version
    assert revised.status is HypothesisStatus.INTAKE
    assert revised.preregistered_at is None
    assert revised.material_fingerprint() != fp
    # 원본은 그대로다 (frozen)
    assert s.version == 1 and s.material_fingerprint() == fp

    # 비실질 변경(설명 문구)은 version 을 올리지 않는다
    same = s.revise(economic_rationale="같은 논리를 다르게 적었다 - 실질은 그대로")
    assert same.version == 1, "비실질 변경이 버전을 올렸다"
    assert same.material_fingerprint() == fp

    # 등록 전에는 자유롭게 고친다
    draft = _spec()
    assert draft.revise(features=("x",)).version == 1
    print("  사전 등록 불변           OK")


def _check_state_machine():
    s = _spec()
    _rejects(lambda: s.transition(HypothesisStatus.SUPPORTED),
             "허용되지 않는다", "INTAKE -> SUPPORTED 건너뛰기")
    _rejects(lambda: s.transition(HypothesisStatus.RUNNING),
             "허용되지 않는다", "검증 없이 RUNNING")
    path = (HypothesisStatus.PREREGISTERED, HypothesisStatus.DATASET_CERTIFIED,
            HypothesisStatus.RUNNING, HypothesisStatus.ROBUSTNESS_REVIEW,
            HypothesisStatus.SUPPORTED)
    cur = s
    for st in path:
        cur = cur.transition(st, now=_dt(2026, 8, 3))
    assert cur.status is HypothesisStatus.SUPPORTED
    _rejects(lambda: cur.transition(HypothesisStatus.RUNNING),
             "종착", "SUPPORTED 이후 되돌리기")
    # preregistered_at 없는 상태로 PREREGISTERED 를 직접 만들 수 없다
    _rejects(lambda: _spec(status=HypothesisStatus.RUNNING),
             "사전 등록이 아니다", "등록 시각 없는 진행 상태")
    print("  상태 머신                OK")


def _check_card_binds_version():
    s = _spec()
    _rejects(lambda: _card(s), "결과를 보기 전에 등록", "INTAKE 에서 Card 생성")
    s1 = s.transition(HypothesisStatus.PREREGISTERED, now=_dt(2026, 8, 3))
    c = _card(s1)
    assert c.matches(s1)
    # 가설을 고치면 그 Card 는 더 이상 맞지 않는다
    s2 = s1.revise(label={"kind": "fwd_ret_20d"})
    assert not c.matches(s2), "고친 가설에 옛 Card 가 그대로 붙는다"
    print("  Card <-> 가설 버전 결속  OK")


def _check_submit_is_earned():
    s = _spec().transition(HypothesisStatus.PREREGISTERED, now=_dt(2026, 8, 3))
    full = dict(oos_metrics={"sharpe": 1.2}, regime_breakdown={"risk_on": {"sharpe": 1.4}},
                validation=Validation(purged_walk_forward=ValidationResult.PASS,
                                      bootstrap_ci_low=0.2, bootstrap_ci_high=1.9))
    _card(s, decision=Decision.SUBMIT_TO_QA, **full)      # 통과

    _rejects(lambda: _card(s, decision=Decision.SUBMIT_TO_QA,
                           **{**full, "validation": Validation(
                               bootstrap_ci_low=0.2, bootstrap_ci_high=1.9)}),
             "NOT_RUN 을 통과로 세지 않는다", "검증 안 하고 제출")
    _rejects(lambda: _card(s, decision=Decision.SUBMIT_TO_QA,
                           **{**full, "regime_breakdown": {}}),
             "regime_breakdown", "구간 의존성 확인 없이 제출")
    _rejects(lambda: _card(s, decision=Decision.SUBMIT_TO_QA,
                           **{**full, "validation": Validation(
                               purged_walk_forward=ValidationResult.PASS)}),
             "표본 불확실성", "CI 없이 제출")
    _rejects(lambda: _card(s, decision=Decision.SUBMIT_TO_QA,
                           **{**full, "failures": ("누수 의심",)}),
             "failures 가 남아", "실패 남긴 채 제출")
    # CI 한쪽만
    _rejects(lambda: Validation(bootstrap_ci_low=0.1),
             "양쪽을 함께", "CI 한쪽만")
    print("  제출은 획득하는 것       OK")


def _check_trial_pressure():
    s = _spec(trial_budget=3).transition(HypothesisStatus.PREREGISTERED,
                                         now=_dt(2026, 8, 3))
    cards = tuple(_card(s, experiment_id=f"exp_{i}", trial_number=i)
                  for i in range(1, 6))
    p = trial_pressure(s, cards)
    assert p["trials_used"] == 5 and p["over_budget"] is True, p
    other = _spec(trial_family_id="다른가족").transition(
        HypothesisStatus.PREREGISTERED, now=_dt(2026, 8, 3))
    assert trial_pressure(other, cards)["trials_used"] == 0
    print("  Trial 압력 계측          OK")


def _check_guideline_gate():
    base = dict(candidate_id="cal_1", scope="research.news_sentiment.5d",
                failure_pattern="단일 출처 인수설을 확정 사실로 취급",
                proposed_change="독립 출처 2개 미만이면 fact 가 아니라 inference",
                expires_at=_dt(2026, 11, 1))
    g = CalibrationGuidelineV1(**base)
    assert g.status is GuidelineStatus.CANDIDATE
    assert not g.is_effective(_dt(2026, 8, 3)), "CANDIDATE 가 적용되고 있다"

    ok = dict(base, status=GuidelineStatus.ACTIVE,
              supporting_case_ids=tuple(f"c{i}" for i in range(30)),
              baseline_eval_id="eval_b", challenger_eval_id="eval_c",
              heldout_delta={"brier": -0.03}, qa_approval_id="appr_1")
    a = CalibrationGuidelineV1(**ok)
    assert a.is_effective(_dt(2026, 8, 3))
    assert not a.is_effective(_dt(2026, 12, 1)), "만료를 조용히 넘겼다"

    for drop, needle in (("supporting_case_ids", "최소"),
                         ("baseline_eval_id", "비교하지 않은 개선"),
                         ("heldout_delta", "숨겨진 검증 구간"),
                         ("qa_approval_id", "자기 산출물")):
        bad = dict(ok)
        bad[drop] = () if drop == "supporting_case_ids" else (
            {} if drop == "heldout_delta" else None)
        _rejects(lambda b=bad: CalibrationGuidelineV1(**b), needle, f"승격 게이트 {drop}")
    print("  Guideline 승격 게이트    OK")


def _check_json_roundtrip():
    s = _spec().transition(HypothesisStatus.PREREGISTERED, now=_dt(2026, 8, 3))
    assert HypothesisSpecV2.model_validate_json(s.model_dump_json()) == s
    c = _card(s)
    assert ExperimentCardV1.model_validate_json(c.model_dump_json()) == c
    # 지문이 결정론인가 - 같은 내용이면 같은 값
    assert _spec().material_fingerprint() == _spec().material_fingerprint()
    # 실질 필드가 하나라도 다르면 지문이 달라야 한다. 하나씩 흔들어 확인한다 -
    # 직렬화가 조용히 뭉개지면 서로 다른 가설이 같은 지문을 갖고, 그러면
    # 사전 등록 검사 자체가 무력해진다.
    base = _spec()
    variants = {
        "features": ("amihud_5d", "roll_spread"),
        "label": {"kind": "fwd_ret_20d"},
        "cost_model_version": "krx-cost-v2",
        "universe_version": "krx-liquid-v2",
        "decision_frequency": "1d",
        "holding_horizon": "20d",
        "baseline": "equal_weight",
        "entry_exit_rules": {"stop": -0.05},
        "strategy_family": "cross_sectional",
        "falsification_tests": ("다른 반증 조건",),
        "preregistered_splits": (_split(name="fold2"),),
    }
    assert set(variants) == set(HypothesisSpecV2.MATERIAL_FIELDS), (
        "실질 필드 목록과 흔들기 목록이 어긋난다 - 새 필드를 추가하고 검사를 안 넣었다")
    for f, v in variants.items():
        other = base.model_copy(update={f: v})
        assert other.material_fingerprint() != base.material_fingerprint(), (
            f"{f} 를 바꿨는데 지문이 같다 - 이 필드는 마음대로 바꿔도 새 Version 이 안 된다")
    # 비실질 필드는 지문을 바꾸지 않는다
    for f, v in (("economic_rationale", "다르게 적은 같은 논리"),
                 ("competing_explanation", "다르게 적은 같은 반론"),
                 ("trial_budget", 99), ("version", 7)):
        assert base.model_copy(update={f: v}).material_fingerprint() == \
            base.material_fingerprint(), f"{f} 가 지문을 바꿨다"
    print("  JSON 왕복·지문 결정론    OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{CONTRACT_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_split_leakage()
    _check_preregistration_immutable()
    _check_state_machine()
    _check_card_binds_version()
    _check_submit_is_earned()
    _check_trial_pressure()
    _check_guideline_gate()
    _check_json_roundtrip()
    print("Quant V2 계약 8개 영역 통과.")
