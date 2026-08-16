#!/usr/bin/env python3
"""전략 공장 핸드오프 계약 - MethodologyLeadV1 / ExperimentProposalV1 / ExperimentOutcomeV1.

담당: 재일 (리서치본부 + 퀀트·백테스트본부)
근거: docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md 6.3·6.4·7.6.2절
      docs/02-engineering/RESEARCH_OUTPUT_ADVANCEMENT_STRATEGY.md 7.1절
      CLAUDE.md 개발원칙 2 "LLM 출력은 항상 Pydantic Schema 로 검증한다"

▶ 이 계약이 막는 것
  리서치가 "이런 방법이 있더라"를 자유 문장으로 넘기면 퀀트가 그것을 다시 해석해야
  하고, 해석하는 순간 **가설을 낸 부서와 검증하는 부서가 같아진다.** 그래서 넘기는
  것은 서술이 아니라 **사전 등록 가능한 실험**이어야 한다.

  세 계약이 각각 한 가지를 막는다.
    MethodologyLeadV1     - 출처 없이 기억으로 지어낸 리드
    ExperimentProposalV1  - 반대편도 경쟁 설명도 없는 "과거에 잘 됐다" 가설
    ExperimentOutcomeV1   - 기계가 대조할 수 없는 자유 서술 교훈

▶ 여기서 강제하는 불변식
  1. 리드의 refs 는 비면 안 된다. **출처 없는 리드는 리드가 아니다** - 스카우트가
     기억으로 재구성하는 것이 이 파이프라인의 첫 번째 오염원이다.
  2. lead_id 는 refs 에서 결정론적으로 계산한다. 같은 소스를 다시 주워도 같은 ID 라
     중복이 접힌다(24시간 상주에서 이게 없으면 같은 논문을 매일 새 리드로 만든다).
  3. 기획안은 counterparty(반대편 주체)가 비면 거부한다. "누가 잃어주는가"에 답하지
     못하는 것은 경제적 근거가 아니라 관찰이다.
  4. 경쟁 설명 코드 >=1 과 회의론자 서명이 없으면 발행 불가. **작성자와 다른 사람이**
     반증을 시도했다는 기록이 없으면 그 반증은 자기 검열이다.
  5. 기각 이력(past_outcomes)이 있는데 대응(lessons_addressed)이 비면 거부.
     같은 실험을 두 번 사는 것을 계약 수준에서 막는다.
  6. Outcome 의 미측정 지표는 **None 이지 0 이 아니다.** 0 은 "측정했더니 0" 이고
     None 은 "안 쟀다"다 - 섞이면 관문이 미측정을 통과로 읽는다(fail-closed).
  7. 교훈은 통제 어휘만. 자유 서술은 notes 한 필드에 격리하고 Gate 0 대조에 쓰지 않는다.

▶ 여기서 강제하지 않는 것
  - **의미 품질**("그럴듯한 근거인가")은 판정하지 않는다. 그건 실험이 판정한다.
    계약은 "답해야 할 질문에 답을 적었는가"만 본다 - 그래서 결정론이다.
  - **통제 어휘 사상**(edge_type/universe_key 가 실행면에 있는가)은 퀀트의 Gate 0 이
    한다. 실행면 어휘는 퀀트가 소유하므로, 여기서 또 검사하면 두 곳이 언젠가 갈라진다.

실행: python contracts/factory_contracts.py     # 자체 점검 (네트워크·DB 없음)
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

try:  # package import from factory workers
    from .alpha_semantics import lane_of as semantic_lane_of
    from .alpha_semantics import validate as validate_semantic_plan
except ImportError:  # direct-file self-check
    from alpha_semantics import lane_of as semantic_lane_of
    from alpha_semantics import validate as validate_semantic_plan

CONTRACT_VERSION = "research-quant-factory-v1"

MAX_EXCERPT_CHARS = 500
Probability = Annotated[float, Field(ge=0.0, le=1.0)]


class SourceType(str, Enum):
    PAPER = "PAPER"
    BLOG = "BLOG"
    VIDEO = "VIDEO"
    COMMUNITY = "COMMUNITY"
    INVESTOR_LETTER = "INVESTOR_LETTER"
    INTERNAL_FAILURE = "INTERNAL_FAILURE"   # 우리 기각 이력에서 다시 꺼낸 것


class ScoutLens(str, Enum):
    ACADEMIC = "ACADEMIC"
    PRACTITIONER = "PRACTITIONER"
    COMMUNITY = "COMMUNITY"
    CROSS_DOMAIN = "CROSS_DOMAIN"


class ResearchLane(str, Enum):
    DAILY_CROSS_SECTIONAL = "DAILY_CROSS_SECTIONAL"
    INTRADAY_EVENT = "INTRADAY_EVENT"


class Testability(str, Enum):
    """규칙으로 서술할 수 있는가. **UNUSABLE 은 실패가 아니라 정상 산출이다** -
    억지로 다듬어 넘기면 그 비용은 실험 예산에서 나간다."""

    RULE_EXPRESSIBLE = "RULE_EXPRESSIBLE"
    VAGUE = "VAGUE"
    UNUSABLE = "UNUSABLE"


class LeadStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNUSABLE = "UNUSABLE"
    BLOCKED = "BLOCKED"


class CompetingExplanation(str, Enum):
    """알파가 아닌 설명. 최소 하나는 반드시 지목해야 한다."""

    BETA_EXPOSURE = "BETA_EXPOSURE"
    LIQUIDITY_PREMIUM = "LIQUIDITY_PREMIUM"
    DATA_MINING = "DATA_MINING"
    COST_UNACCOUNTED = "COST_UNACCOUNTED"


class OutcomeDecision(str, Enum):
    # 실험 단계
    REJECT = "REJECT"
    REVISE = "REVISE"
    SUBMIT_TO_QA = "SUBMIT_TO_QA"
    GATE_HOLD = "GATE_HOLD"
    BLOCKED = "BLOCKED"
    # 승격·운용 단계 - **성공도 환류한다**(무엇이 통했는지도 배워야 한다)
    PROMOTED = "PROMOTED"
    KILLED = "KILLED"
    DEMOTED = "DEMOTED"
    ARCHIVED = "ARCHIVED"


class LessonCode(str, Enum):
    """통제 어휘. 자유 서술 교훈은 다음 기획안과 **기계 대조가 안 되고**,
    대조가 안 되는 교훈은 Gate 0 에서 아무것도 막지 못한다."""

    LEAKAGE_SUSPECT = "LEAKAGE_SUSPECT"
    COST_SENSITIVE = "COST_SENSITIVE"
    BEAR_FRAGILE = "BEAR_FRAGILE"
    SINGLE_REGIME_ONLY = "SINGLE_REGIME_ONLY"
    OVERFIT_PBO = "OVERFIT_PBO"
    OVERFIT_DSR = "OVERFIT_DSR"
    UNDERPOWERED_DATA = "UNDERPOWERED_DATA"
    CAPACITY_DOUBT = "CAPACITY_DOUBT"
    BASELINE_NOT_BEATEN = "BASELINE_NOT_BEATEN"
    LIVE_SLIPPAGE_BLOWN = "LIVE_SLIPPAGE_BLOWN"
    LIVE_MDD_EXCEEDED = "LIVE_MDD_EXCEEDED"


# 종결 판정인데 사유가 없으면 환류가 성립하지 않는 decision 들
_NEEDS_REASON = frozenset({
    OutcomeDecision.REJECT, OutcomeDecision.GATE_HOLD,
    OutcomeDecision.KILLED, OutcomeDecision.DEMOTED,
})


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware(v: datetime, field: str) -> datetime:
    if v.tzinfo is None:
        raise ValueError(f"{field} 는 timezone 이 있어야 한다 - 시점이 모호한 PIT 는 PIT 가 아니다")
    return v.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# MethodologyLeadV1
# ---------------------------------------------------------------------------

class SourceRef(_Base):
    """되짚을 수 있는 출처 하나. **발췌는 요약이 아니라 인용이다.**"""

    url: str
    title: str
    accessed_at: datetime
    excerpt: str                      # 원문 발췌
    author: str | None = None
    published_at: str | None = None   # 소스가 밝힌 발행일 (형식이 제각각이라 문자열)

    @field_validator("url", "title", "excerpt")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("url·title·excerpt 는 비면 안 된다")
        return v.strip()

    @field_validator("url")
    @classmethod
    def _looks_like_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"되짚을 수 있는 URL 이어야 한다: {v!r}")
        return v

    @field_validator("excerpt")
    @classmethod
    def _excerpt_bounded(cls, v: str) -> str:
        if len(v) > MAX_EXCERPT_CHARS:
            raise ValueError(
                f"발췌는 {MAX_EXCERPT_CHARS}자 이하다 - 본문을 통째로 나르지 않는다")
        return v

    @field_validator("accessed_at")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware(v, "accessed_at")


def lead_id_for(refs: list[SourceRef] | list[dict]) -> str:
    """출처에서 결정론적으로 계산하는 리드 ID.

    같은 소스를 다시 주워도 같은 ID 다 - 24시간 상주에서 이게 없으면 같은 논문을
    매일 새 리드로 만든다. url+title 만 쓴다(발췌·접근시각은 매번 달라지므로).
    """
    keys = []
    for r in refs:
        url = r.url if isinstance(r, SourceRef) else str(r.get("url", ""))
        title = r.title if isinstance(r, SourceRef) else str(r.get("title", ""))
        keys.append(f"{url.strip().lower()}|{title.strip().lower()}")
    blob = json.dumps(sorted(keys), ensure_ascii=False, separators=(",", ":"))
    return "lead_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class MethodologyLeadV1(_Base):
    """스카우트가 가져온 방법론 하나."""

    lead_id: str
    case_id: str
    scout_lens: ScoutLens
    source_type: SourceType
    as_known_at: datetime
    refs: tuple[SourceRef, ...]
    ast_contract: dict = Field(default_factory=dict)
    claimed_edge: str                 # 소스가 주장하는 엣지 (스카우트의 해석이 아니다)
    stated_mechanism: str = ""        # 왜 지속되는가에 대한 소스의 설명
    inferred: bool = False            # 메커니즘이 인용이 아니라 추론이면 True
    market_context: str = ""          # 소스가 실제로 다룬 시장·기간
    stated_failure_mode: str = ""     # 저자가 밝힌 무너지는 조건
    independent_mentions: int = 1
    testability: Testability = Testability.RULE_EXPRESSIBLE
    status: LeadStatus = LeadStatus.COMPLETE
    model_version: str = ""
    prompt_version: str = ""

    @field_validator("as_known_at")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware(v, "as_known_at")

    @field_validator("claimed_edge")
    @classmethod
    def _edge_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("claimed_edge 가 비었다 - 무엇을 주장하는지 없는 리드는 리드가 아니다")
        return v.strip()

    @field_validator("independent_mentions")
    @classmethod
    def _mentions(cls, v: int) -> int:
        if v < 1:
            raise ValueError("independent_mentions 는 1 이상이다(자기 자신이 1건)")
        return v

    @model_validator(mode="after")
    def _invariants(self) -> MethodologyLeadV1:
        # ① 출처 없는 리드는 리드가 아니다
        if not self.refs:
            raise ValueError(
                "refs 가 비었다 - 출처 없는 리드는 리드다. 기억으로 재구성하지 않는다")
        # ② ID 는 출처에서 계산된 값이어야 한다(중복 접기가 성립하려면)
        expected = lead_id_for(list(self.refs))
        if self.lead_id != expected:
            raise ValueError(
                f"lead_id 가 출처와 맞지 않는다: {self.lead_id!r} != {expected!r} - "
                f"임의 ID 를 쓰면 같은 소스가 매번 새 리드가 된다")
        # ③ 추론을 인용처럼 쓰지 않는다
        if self.stated_mechanism and self.inferred is False:
            pass  # 인용이라고 선언한 것 - 검증은 사람/QA 몫이다
        return self


# ---------------------------------------------------------------------------
# ExperimentProposalV1
# ---------------------------------------------------------------------------

class PriorCheck(_Base):
    """같은 시도 계열의 기각 이력 대조 결과. **Gate 0 이 이것을 다시 검증한다.**"""

    trial_family_id: str = ""
    trials_used: int = 0
    past_outcomes: tuple[str, ...] = ()
    lessons_addressed: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _addressed(self) -> PriorCheck:
        if self.past_outcomes and not self.lessons_addressed:
            raise ValueError(
                "기각 이력이 있는데 대응(lessons_addressed)이 비었다 - "
                "같은 실험을 두 번 사는 것을 막는 자리다")
        for code, how in self.lessons_addressed.items():
            if code not in LessonCode.__members__:
                raise ValueError(f"교훈 코드가 통제 어휘 밖이다: {code!r}")
            if not str(how).strip():
                raise ValueError(f"{code} 에 대한 대응이 비었다 - 빈 대응은 대응이 아니다")
        return self


class DataRequirement(_Base):
    tables: tuple[str, ...]
    min_history_days: int

    @model_validator(mode="after")
    def _sane(self) -> DataRequirement:
        if not self.tables:
            raise ValueError("어떤 데이터가 필요한지 비었다 - 퀀트가 NEEDS_DATA 를 판정할 수 없다")
        if self.min_history_days < 1:
            raise ValueError("min_history_days 는 1 이상이다")
        return self


class ExperimentProposalV1(_Base):
    """리서치본부의 정본 산출물. **퀀트가 질문 없이 사전 등록할 수 있어야 한다.**"""

    proposal_id: str
    case_id: str
    as_known_at: datetime
    lead_ids: tuple[str, ...]

    economic_rationale: str
    counterparty: str                              # 반대편 주체 - 비면 발행 불가
    competing_explanation: str
    competing_explanation_codes: tuple[CompetingExplanation, ...]
    skeptic_sign: str                              # 독립 회의론자 서명(worker run id)

    edge_type: str                                 # 어휘 사상은 퀀트 Gate 0 소관
    universe_key: str
    label: str = "forward_return"
    baseline: str = "equal_weight_buy_and_hold"
    falsification_tests: tuple[str, ...] = ()
    data_requirements: DataRequirement | None = None
    suggested_params: dict = Field(default_factory=dict)
    research_lane: ResearchLane = ResearchLane.DAILY_CROSS_SECTIONAL
    semantic_plan: dict = Field(default_factory=dict)
    trial_budget: int = 5
    prior_check: PriorCheck = Field(default_factory=PriorCheck)

    # 소스가 보고한 값. **우리 실험 결과와 같은 자리에 두지 않는다** - 남의 시장·
    # 남의 기간 숫자가 우리 백테스트 결과처럼 읽히는 순간 미검증 값이 근거로 승격된다.
    source_reported_effect: dict = Field(default_factory=dict)

    research_packet_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()

    @field_validator("as_known_at")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware(v, "as_known_at")

    @field_validator("economic_rationale", "counterparty", "competing_explanation",
                     "skeptic_sign", "edge_type", "universe_key")
    @classmethod
    def _required_text(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(
                f"{info.field_name} 가 비었다 - 발행 게이트가 요구하는 필수 항목이다")
        return v.strip()

    @field_validator("trial_budget")
    @classmethod
    def _budget(cls, v: int) -> int:
        if v < 1:
            raise ValueError("trial_budget 은 1 이상이다")
        return v

    @model_validator(mode="after")
    def _invariants(self) -> ExperimentProposalV1:
        if not self.lead_ids:
            raise ValueError(
                "lead_ids 가 비었다 - 근거가 된 방법론 리드 없이 기획안을 만들지 않는다")
        if not self.competing_explanation_codes:
            raise ValueError(
                "경쟁 설명 코드가 없다 - 베타·유동성·데이터마이닝·비용 중 최소 하나는 "
                "지목해야 한다(알파가 아닐 가능성을 적지 않은 기획안은 발행하지 않는다)")
        if not self.falsification_tests:
            raise ValueError(
                "반증 검사가 없다 - '이게 나오면 죽는다'가 없는 가설은 반증 가능하지 않다")
        if self.data_requirements is None:
            raise ValueError("data_requirements 가 없다 - 퀀트가 실행 가능성을 판정할 수 없다")
        if self.semantic_plan:
            parsed = validate_semantic_plan(self.semantic_plan)
            if semantic_lane_of(parsed) != self.research_lane.value:
                raise ValueError(
                    "semantic_plan clock and research_lane disagree - the formula "
                    "would be routed to the wrong evaluator")
        elif self.research_lane == ResearchLane.INTRADAY_EVENT:
            raise ValueError("INTRADAY_EVENT requires a typed semantic_plan")
        if self.research_lane == ResearchLane.INTRADAY_EVENT:
            if not self.suggested_params.get("intraday_signal_expr"):
                raise ValueError("INTRADAY_EVENT requires intraday_signal_expr")
            required = set(self.data_requirements.tables)
            if not {"market_quotes", "market_ticks"} <= required:
                raise ValueError(
                    "INTRADAY_EVENT requires raw market_quotes and market_ticks; "
                    "daily aggregates cannot prove execution alpha")
        return self


# ---------------------------------------------------------------------------
# ExperimentOutcomeV1
# ---------------------------------------------------------------------------

class OosSummary(_Base):
    """표본 외 요약. **미측정은 None 이다** - 0 은 "재 봤더니 0" 이라 뜻이 다르다."""

    excess_return_pct: float | None = None
    information_ratio: float | None = None
    max_drawdown_pct: float | None = None
    deflated_sharpe: float | None = None
    pbo: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    mean_net_bps_per_opportunity: float | None = None
    fill_rate: float | None = None
    sessions: int | None = None
    instruments: int | None = None
    positive_fold_ratio: float | None = None
    mean_capacity_shares_l1: float | None = None
    p10_capacity_shares_l1: float | None = None
    max_concurrent_opportunities: int | None = None


class ExperimentOutcomeV1(_Base):
    """퀀트 -> 리서치 환류. **적재가 실험 종결의 전제 조건이다.**"""

    outcome_id: str
    experiment_id: str
    hypothesis_id: str
    trial_family_id: str
    trial_number: int
    decision: OutcomeDecision
    decided_at: datetime
    proposal_id: str = ""              # 자체 생성 가설이면 빈 값(전환기 한정)
    failed_criteria: tuple[str, ...] = ()
    oos_summary: OosSummary = Field(default_factory=OosSummary)
    regime_concerns: tuple[str, ...] = ()
    lesson_codes: tuple[LessonCode, ...] = ()
    notes: str = ""                    # 자유 서술은 여기 하나에만. Gate 0 대조에 안 쓴다

    @field_validator("decided_at")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware(v, "decided_at")

    @field_validator("trial_number")
    @classmethod
    def _trial(cls, v: int) -> int:
        if v < 1:
            raise ValueError("trial_number 는 1부터다 - 0/음수는 '안 셌다'와 구분이 안 된다")
        return v

    @model_validator(mode="after")
    def _invariants(self) -> ExperimentOutcomeV1:
        # 부정 종결인데 사유가 없으면 환류가 성립하지 않는다
        if self.decision in _NEEDS_REASON and not (self.failed_criteria or self.lesson_codes):
            raise ValueError(
                f"{self.decision.value} 인데 failed_criteria 도 lesson_codes 도 비었다 - "
                f"사유 없는 기각은 다음 기획안이 대조할 것이 없다")
        return self


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

def _ref(url="https://arxiv.org/abs/1234.5678", title="Momentum crash hedging"):
    return SourceRef(url=url, title=title,
                     accessed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                     excerpt="We document that momentum strategies crash...")


def _lead(**kw):
    refs = kw.pop("refs", (_ref(),))
    base = dict(lead_id=lead_id_for(list(refs)), case_id="rc_1",
                scout_lens=ScoutLens.ACADEMIC, source_type=SourceType.PAPER,
                as_known_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                refs=refs, claimed_edge="모멘텀 붕괴는 변동성으로 예측된다")
    base.update(kw)
    return MethodologyLeadV1(**base)


def _proposal(**kw):
    base = dict(
        proposal_id="prop_1", case_id="rc_1",
        as_known_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        lead_ids=("lead_abc",),
        economic_rationale="강제 청산자가 반대편에 있다",
        counterparty="레버리지 청산 물량",
        competing_explanation="단순 베타 노출일 수 있다",
        competing_explanation_codes=(CompetingExplanation.BETA_EXPOSURE,),
        skeptic_sign="worker_run_42",
        edge_type="liquidity_shock_reversal", universe_key="krx_all",
        falsification_tests=("하락장 초과수익이 0 미만이면 기각",),
        data_requirements=DataRequirement(tables=("market_bars",), min_history_days=750),
    )
    base.update(kw)
    return ExperimentProposalV1(**base)


def _outcome(**kw):
    base = dict(outcome_id="out_1", experiment_id="exp_1", hypothesis_id="hyp_1",
                trial_family_id="fam_1", trial_number=3,
                decision=OutcomeDecision.REJECT,
                decided_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                failed_criteria=("pbo",),
                lesson_codes=(LessonCode.OVERFIT_PBO,))
    base.update(kw)
    return ExperimentOutcomeV1(**base)


def _rejects(fn, needle: str, why: str):
    try:
        fn()
    except (ValueError, TypeError) as exc:
        assert needle in str(exc), f"{why}: 메시지가 다르다 - {exc}"
        return
    raise AssertionError(f"{why}: 통과했다")


def _check_lead_requires_source():
    """**출처 없는 리드는 리드가 아니다** - 기억으로 재구성하는 것이 첫 오염원이다."""
    _rejects(lambda: _lead(refs=()), "refs 가 비었다", "출처 없는 리드")
    _rejects(lambda: SourceRef(url="arxiv.org/abs/1", title="t",
                               accessed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                               excerpt="e"),
             "되짚을 수 있는 URL", "스킴 없는 URL")


def _check_lead_id_folds_duplicates():
    """같은 소스를 다시 주워도 같은 ID - 상주 운영에서 중복이 접힌다."""
    a, b = _lead(), _lead()
    assert a.lead_id == b.lead_id
    # 접근 시각·발췌가 달라도 같은 ID(그것들은 매번 다르다)
    other = _ref()
    c = _lead(refs=(SourceRef(url=other.url, title=other.title,
                              accessed_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
                              excerpt="different excerpt"),))
    assert c.lead_id == a.lead_id
    # 다른 소스는 다른 ID
    d = _lead(refs=(_ref(url="https://arxiv.org/abs/9999.1", title="Other"),))
    assert d.lead_id != a.lead_id


def _check_lead_id_cannot_be_arbitrary():
    """임의 ID 를 쓰면 중복 접기가 무너진다."""
    _rejects(lambda: _lead(lead_id="lead_madeup"), "출처와 맞지 않는다", "임의 lead_id")


def _check_excerpt_is_quote_not_dump():
    _rejects(lambda: _ref(title="t") and SourceRef(
        url="https://x.com/a", title="t",
        accessed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        excerpt="x" * (MAX_EXCERPT_CHARS + 1)), "발췌는", "본문 통째 나르기")


def _check_unusable_lead_is_valid():
    """**UNUSABLE 은 실패가 아니라 정상 산출이다** - 억지로 다듬어 넘기면 실험 예산이 샌다."""
    lead = _lead(testability=Testability.UNUSABLE, status=LeadStatus.UNUSABLE)
    assert lead.testability is Testability.UNUSABLE


def _check_proposal_requires_counterparty():
    """"누가 반대편에서 잃어주는가"에 답 못 하면 경제적 근거가 아니다."""
    _rejects(lambda: _proposal(counterparty="  "), "counterparty", "반대편 없는 기획안")


def _check_proposal_requires_competing_explanation_and_sign():
    _rejects(lambda: _proposal(competing_explanation_codes=()),
             "경쟁 설명 코드가 없다", "경쟁 설명 없는 기획안")
    _rejects(lambda: _proposal(skeptic_sign=""), "skeptic_sign", "회의론자 서명 없는 기획안")


def _check_proposal_requires_falsification_and_data():
    _rejects(lambda: _proposal(falsification_tests=()), "반증 검사가 없다", "반증 없는 가설")
    _rejects(lambda: _proposal(data_requirements=None), "data_requirements", "데이터 요구 없음")


def _check_proposal_requires_lead():
    _rejects(lambda: _proposal(lead_ids=()), "lead_ids 가 비었다", "근거 리드 없는 기획안")


def _check_prior_check_demands_response():
    """**같은 실험을 두 번 사는 것**을 계약이 막는다."""
    _rejects(lambda: _proposal(prior_check=PriorCheck(
        trial_family_id="fam_1", trials_used=2, past_outcomes=("out_1",))),
        "대응(lessons_addressed)이 비었다", "기각 이력 무대응 재도전")
    _rejects(lambda: PriorCheck(past_outcomes=("out_1",),
                                lessons_addressed={"NOT_A_CODE": "대응"}),
             "통제 어휘 밖", "어휘 밖 교훈 코드")
    _rejects(lambda: PriorCheck(past_outcomes=("out_1",),
                                lessons_addressed={"OVERFIT_PBO": "  "}),
             "대응이 비었다", "빈 대응")
    # 정상: 대응이 있으면 통과
    ok = _proposal(prior_check=PriorCheck(
        trial_family_id="fam_1", trials_used=2, past_outcomes=("out_1",),
        lessons_addressed={"BEAR_FRAGILE": "하락장 표본을 2창에서 5창으로 늘린다"}))
    assert ok.prior_check.trials_used == 2


def _check_outcome_unmeasured_is_none_not_zero():
    """**미측정은 None 이다** - 0 으로 채우면 관문이 미측정을 통과로 읽는다."""
    o = _outcome()
    assert o.oos_summary.pbo is None and o.oos_summary.deflated_sharpe is None
    # 0 을 명시하면 그건 "재 봤더니 0"
    o2 = _outcome(oos_summary=OosSummary(pbo=0.0))
    assert o2.oos_summary.pbo == 0.0 and o2.oos_summary.pbo is not None


def _check_outcome_rejection_needs_reason():
    """사유 없는 기각은 다음 기획안이 대조할 것이 없다."""
    for d in (OutcomeDecision.REJECT, OutcomeDecision.KILLED,
              OutcomeDecision.GATE_HOLD, OutcomeDecision.DEMOTED):
        _rejects(lambda d=d: _outcome(decision=d, failed_criteria=(), lesson_codes=()),
                 "사유 없는 기각", f"{d.value} 무사유")
    # 성공 종결은 사유가 없어도 된다 - 그래도 환류는 한다(무엇이 통했는지도 배운다)
    ok = _outcome(decision=OutcomeDecision.PROMOTED, failed_criteria=(), lesson_codes=())
    assert ok.decision is OutcomeDecision.PROMOTED


def _check_outcome_trial_number_starts_at_one():
    _rejects(lambda: _outcome(trial_number=0), "1부터다", "trial_number 0")


def _check_tz_aware_required():
    _rejects(lambda: _outcome(decided_at=datetime(2026, 8, 10)),
             "timezone", "naive 시각")
    _rejects(lambda: _lead(as_known_at=datetime(2026, 8, 10)), "timezone", "naive as_known_at")


def _check_extra_fields_rejected():
    """계약에 없는 필드를 조용히 받으면 스키마가 계약이 아니다."""
    _rejects(lambda: _proposal(surprise="value"), "", "정의 안 된 필드")


def _check_vocabulary_check_is_not_here():
    """**어휘 사상은 퀀트 Gate 0 이 한다** - 두 곳에서 검사하면 언젠가 갈라진다.
    여기서는 비어 있지만 않으면 통과시킨다."""
    p = _proposal(edge_type="volatility_risk_premium", universe_key="something_new")
    assert p.edge_type == "volatility_risk_premium"


def _check_source_effect_is_separate_field():
    """남의 시장·남의 기간 숫자를 우리 결과와 같은 자리에 두지 않는다."""
    p = _proposal(source_reported_effect={"market": "US equities",
                                          "period": "1993-2018",
                                          "reported_value": 0.04})
    assert "reported_value" in p.source_reported_effect
    assert not hasattr(p, "oos_summary")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{CONTRACT_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_lead_requires_source();          print("  리드: 출처 필수          OK")
    _check_lead_id_folds_duplicates();      print("  리드: 중복 접기          OK")
    _check_lead_id_cannot_be_arbitrary();   print("  리드: 임의 ID 거부       OK")
    _check_excerpt_is_quote_not_dump();     print("  리드: 발췌 상한          OK")
    _check_unusable_lead_is_valid();        print("  리드: UNUSABLE 은 정상   OK")
    _check_proposal_requires_counterparty(); print("  기획: 반대편 주체 필수   OK")
    _check_proposal_requires_competing_explanation_and_sign()
    print("  기획: 경쟁설명·서명     OK")
    _check_proposal_requires_falsification_and_data()
    print("  기획: 반증·데이터 요구  OK")
    _check_proposal_requires_lead();        print("  기획: 근거 리드 필수     OK")
    _check_prior_check_demands_response();  print("  기획: 기각이력 대응 강제 OK")
    _check_outcome_unmeasured_is_none_not_zero()
    print("  환류: 미측정 != 0        OK")
    _check_outcome_rejection_needs_reason(); print("  환류: 사유 없는 기각 거부 OK")
    _check_outcome_trial_number_starts_at_one(); print("  환류: 시도번호 1부터   OK")
    _check_tz_aware_required();             print("  전역: tz-aware 강제      OK")
    _check_extra_fields_rejected();         print("  전역: 미정의 필드 거부   OK")
    _check_vocabulary_check_is_not_here();  print("  경계: 어휘검사는 퀀트    OK")
    _check_source_effect_is_separate_field(); print("  경계: 소스 수치 분리    OK")
    print("전략 공장 계약 17개 영역 통과.")
