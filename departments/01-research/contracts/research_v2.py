#!/usr/bin/env python3
"""Research V2 계약 - ResearchCaseV2 / AnalystFindingV1 / ResearchPacketV2.

담당: 재일 (리서치본부)
근거: docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md
        6.1절(단계별 Workflow, ResearchCaseV2), 6.3절(AnalystFindingV1),
        6.4절(ResearchPacketV2), Phase RQF-0
      docs/02-engineering/TECH_STACK_DECISIONS.md - 계약은 Pydantic v2 + JSON Schema
      CLAUDE.md 개발원칙 2 "LLM 출력은 항상 Pydantic Schema 로 검증한다"

▶ 이 계약이 막는 것
  지금 분석가 6인은 각자 다른 모양의 dict 를 돌려주고, 총괄이 그것을 자유 문장으로
  합친다. 그래서 (1) 어느 주장이 어느 근거에 기대는지 코드가 알 수 없고,
  (2) 총괄이 스키마를 이탈해도 늦게 발견되며(실측 19회 중 10회 실패),
  (3) 분석가가 침묵하면 그 분석가의 **코드 계산 결과까지 리포트에서 사라진다.**

  계약은 그 셋을 각각 막는다 - Claim 마다 evidence_ids 를 강제하고, 스키마 위반을
  즉시 예외로 만들고, 부분 실패를 PARTIAL 로 **표시하되 남긴다.**

▶ 여기서 강제하는 불변식 (계약이 곧 검사다)
  1. as_known_at 은 tz-aware 다. naive 는 거부한다 - 시점이 모호한 PIT 는 PIT 가 아니다.
  2. claim_type='fact' 는 evidence_ids 가 비면 안 된다. 근거 없는 사실 주장은 사실이 아니다.
     (framework 6.1 10단계 "미검증 Packet 발행 금지", RQF-1 완료기준 "모든 Fact Claim 이
      존재하는 Evidence ID 를 가진다")
  3. status=COMPLETE 인데 claims 가 비면 거부. 빈 완료는 완료가 아니다(fail-closed).
  4. confidence 는 0~1. 표본이 없으면 uncalibrated=True 를 달아야 하고, 그때는
     이 숫자를 정밀한 확률처럼 쓰지 않는다(framework 6.4 마지막 문단).
  5. 필수 관점(required_perspectives)과 선택 관점은 겹칠 수 없다 - 겹치면 '필수인데
     없어도 되는' 관점이 생긴다.
  6. Packet 의 macro/micro Outlook 이 참조하는 claim_id 는 실재해야 한다.
     **끊어진 참조를 허용하면 Claim Graph 가 그래프가 아니게 된다.**

▶ 여기서 강제하지 않는 것
  Cutoff Lock(framework 6.1 2단계)은 evidence/pit_manifest.py 가 한다. 계약은 어떤
  Tool 을 썼는지 tool_versions 로 **기록만** 하고, 부를 수 있는지는 Manifest 가 판정한다.
  두 곳에서 같은 판정을 하면 언젠가 갈라진다.

실행: python contracts/research_v2.py     # 자체 점검 (네트워크·DB 없음)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "research-contracts-v2"

# 분석가 6인. framework 6.1 5단계 Specialist Fan-out 과 같은 목록이다.
# hermes/config.yaml 의 페르소나 키와 대응한다(microstructure-analyst 등).
Perspective = Literal[
    "fundamental",
    "technical",
    "microstructure",
    "news_sentiment",
    "macro_regime",
    "geopolitical",
]

PERSPECTIVES: tuple[str, ...] = (
    "fundamental", "technical", "microstructure",
    "news_sentiment", "macro_regime", "geopolitical",
)

Horizon = Literal["1d", "5d", "20d", "60d"]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class CaseStatus(str, Enum):
    RECEIVED = "RECEIVED"
    RUNNING = "RUNNING"
    PUBLISHED = "PUBLISHED"
    BLOCKED = "BLOCKED"          # 필수 Mandate 누락 - 진행 불가
    INSUFFICIENT = "INSUFFICIENT"  # 치명적 반증 (framework 6.1 9단계)


class FindingStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"            # 일부만 됨. **결과를 버리지 않는다**
    INCONCLUSIVE = "INCONCLUSIVE"  # 근거 부족
    BLOCKED = "BLOCKED"            # 도구·권한 문제로 시작도 못 함


class ClaimType(str, Enum):
    FACT = "fact"          # 근거 문서에 그대로 있는 것
    INFERENCE = "inference"  # 사실에서 끌어낸 것
    FORECAST = "forecast"  # 아직 일어나지 않은 것


class Direction(str, Enum):
    SUPPORTIVE = "supportive"
    OPPOSING = "opposing"
    NEUTRAL = "neutral"


class _Base(BaseModel):
    # 계약에 없는 키가 조용히 들어오는 것을 막는다. 오타 난 필드가 무시되면
    # '넣었는데 왜 안 보이지' 를 며칠 뒤에 발견한다.
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_aware(v: datetime, field: str) -> datetime:
    if v.tzinfo is None:
        raise ValueError(
            f"{field} 는 timezone 이 있어야 한다 - 시점이 모호한 PIT 는 PIT 가 아니다")
    return v.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# ResearchCaseV2 - 본부장이 직원에게 내리는 업무지시이자 실행 경계
# ---------------------------------------------------------------------------

class Budgets(_Base):
    wall_clock_seconds: int = Field(gt=0, le=3600)
    max_llm_calls: int = Field(gt=0, le=200)
    max_retrieval_rounds: int = Field(ge=0, le=5)


class Trigger(_Base):
    type: Literal["disclosure", "news", "price", "regime", "scheduled", "manual"]
    source_event_ids: tuple[str, ...] = ()


class ResearchCaseV2(_Base):
    case_id: str = Field(min_length=1)
    instrument_ids: tuple[str, ...] = Field(min_length=1)
    trigger: Trigger
    mandate_version: str = Field(min_length=1)
    as_known_at: datetime
    horizons: tuple[Horizon, ...] = Field(min_length=1)
    required_perspectives: tuple[Perspective, ...] = Field(min_length=1)
    optional_perspectives: tuple[Perspective, ...] = ()
    budgets: Budgets
    priority: int = Field(ge=0, le=100)
    status: CaseStatus = CaseStatus.RECEIVED

    @field_validator("as_known_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "as_known_at")

    @model_validator(mode="after")
    def _perspectives_disjoint(self):
        dup = set(self.required_perspectives) & set(self.optional_perspectives)
        if dup:
            raise ValueError(
                f"필수이면서 선택인 관점이 있다: {sorted(dup)} - "
                f"필수는 없으면 Case 가 막히고 선택은 없어도 되는데, 둘 다일 수는 없다")
        if len(set(self.horizons)) != len(self.horizons):
            raise ValueError("horizons 에 중복이 있다")
        return self


# ---------------------------------------------------------------------------
# AnalystFindingV1 - 직원 한 명의 산출물
# ---------------------------------------------------------------------------

class Claim(_Base):
    claim_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    claim_type: ClaimType
    evidence_ids: tuple[str, ...] = ()
    direction: Direction = Direction.NEUTRAL
    confidence: Confidence
    numeric_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _fact_needs_evidence(self):
        if self.claim_type is ClaimType.FACT and not self.evidence_ids:
            raise ValueError(
                f"claim {self.claim_id}: fact 인데 evidence_ids 가 비었다 - "
                f"근거 없는 사실 주장은 사실이 아니다. 추론이면 claim_type='inference'")
        return self


class AnalystFindingV1(_Base):
    finding_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    perspective: Perspective
    as_known_at: datetime
    horizon: Horizon
    claims: tuple[Claim, ...] = ()
    contradictions: tuple[str, ...] = ()
    unanswered_questions: tuple[str, ...] = ()
    status: FindingStatus
    model_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    tool_versions: tuple[str, ...] = ()

    @field_validator("as_known_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "as_known_at")

    @model_validator(mode="after")
    def _status_matches_content(self):
        if self.status is FindingStatus.COMPLETE and not self.claims:
            raise ValueError(
                f"finding {self.finding_id}: COMPLETE 인데 claims 가 비었다 - "
                f"빈 완료는 완료가 아니다. 근거가 없으면 INCONCLUSIVE 다")
        if self.status is FindingStatus.BLOCKED and self.claims:
            raise ValueError(
                f"finding {self.finding_id}: BLOCKED 인데 claims 가 있다 - "
                f"시작도 못 했다면서 결과가 있을 수 없다. 일부 됐으면 PARTIAL 이다")
        ids = [c.claim_id for c in self.claims]
        if len(set(ids)) != len(ids):
            raise ValueError(f"finding {self.finding_id}: claim_id 중복")
        # 모순으로 지목한 claim 이 자기 주장이면 자기모순 표기다 - 허용하지 않는다
        overlap = set(self.contradictions) & set(ids)
        if overlap:
            raise ValueError(
                f"finding {self.finding_id}: 자기 claim 을 모순으로 지목했다 {sorted(overlap)}")
        return self

    def fact_claims(self) -> tuple[Claim, ...]:
        return tuple(c for c in self.claims if c.claim_type is ClaimType.FACT)


# ---------------------------------------------------------------------------
# ResearchPacketV2 - 부서의 공식 산출물
# ---------------------------------------------------------------------------

class Outlook(_Base):
    direction: Literal["positive", "neutral", "negative"]
    confidence: Confidence
    claim_ids: tuple[str, ...] = ()


class Calibration(_Base):
    cohort: str = Field(min_length=1)
    historical_brier: float | None = Field(default=None, ge=0.0, le=1.0)
    n_observations: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _brier_needs_sample(self):
        if self.historical_brier is not None and self.n_observations <= 0:
            raise ValueError(
                "historical_brier 가 있는데 n_observations 가 0 이다 - "
                "표본 없는 통계는 통계가 아니다")
        return self


class Lineage(_Base):
    graph_version: str = Field(min_length=1)
    model_versions: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)


class ResearchPacketV2(_Base):
    packet_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    as_known_at: datetime
    horizons: tuple[Horizon, ...] = Field(min_length=1)
    evidence_manifest_id: str | None = None
    claim_graph_id: str | None = None
    macro_outlook: Outlook
    micro_outlook: Outlook
    thesis: str = Field(min_length=1)
    catalysts: tuple[str, ...] = ()
    invalidation: tuple[str, ...] = ()
    dissent: tuple[str, ...] = ()
    evidence_gaps: tuple[str, ...] = ()
    calibration: Calibration
    # 표본이 없으면 confidence 를 정밀한 확률처럼 쓰지 않는다 (framework 6.4)
    uncalibrated: bool = True
    status: Literal["DRAFT", "PUBLISHED", "PARTIAL", "INSUFFICIENT"]
    lineage: Lineage
    # 이 Packet 을 만든 Finding 들. 참조 무결성 검사의 근거가 된다.
    findings: tuple[AnalystFindingV1, ...] = ()

    @field_validator("as_known_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "as_known_at")

    @model_validator(mode="after")
    def _graph_is_connected(self):
        known = {c.claim_id for f in self.findings for c in f.claims}
        for name, ol in (("macro_outlook", self.macro_outlook),
                         ("micro_outlook", self.micro_outlook)):
            missing = [cid for cid in ol.claim_ids if cid not in known]
            if missing:
                raise ValueError(
                    f"{name} 이 없는 claim 을 참조한다: {missing} - "
                    f"끊어진 참조를 허용하면 Claim Graph 가 그래프가 아니게 된다")
        if self.calibration.historical_brier is None and not self.uncalibrated:
            raise ValueError(
                "historical_brier 가 없는데 uncalibrated=False 다 - "
                "보정된 적 없는 confidence 를 보정됐다고 표시하지 않는다")
        if self.status == "PUBLISHED":
            # framework 6.1 10단계 "미검증 Packet 발행 금지"
            bad = [c.claim_id for f in self.findings for c in f.fact_claims()
                   if not c.evidence_ids]
            if bad:
                raise ValueError(f"PUBLISHED 인데 근거 없는 fact claim 이 있다: {bad}")
            if not self.findings:
                raise ValueError("PUBLISHED 인데 findings 가 비었다 - 빈 발행은 발행이 아니다")
        return self

    def perspectives_present(self) -> tuple[str, ...]:
        return tuple(sorted({f.perspective for f in self.findings}))

    def missing_required(self, case: ResearchCaseV2) -> tuple[str, ...]:
        """Case 가 요구한 관점 중 결과가 **쓸 수 있게** 오지 않은 것.

        BLOCKED 는 물론이고 INCONCLUSIVE 도 빠진 것으로 센다 - '돌긴 돌았다' 를
        '결과가 있다' 로 세면 필수 관점 누락이 숨는다.
        """
        usable = {f.perspective for f in self.findings
                  if f.status in (FindingStatus.COMPLETE, FindingStatus.PARTIAL)}
        return tuple(p for p in case.required_perspectives if p not in usable)


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

_T = datetime(2026, 8, 3, 1, 30, tzinfo=timezone.utc)


def _case(**kw) -> ResearchCaseV2:
    base = dict(
        case_id="research_case_1", instrument_ids=("inst_005930",),
        trigger=Trigger(type="disclosure"), mandate_version="mandate_1",
        as_known_at=_T, horizons=("1d", "20d"),
        required_perspectives=("fundamental", "technical"),
        budgets=Budgets(wall_clock_seconds=300, max_llm_calls=10, max_retrieval_rounds=2),
        priority=80)
    base.update(kw)
    return ResearchCaseV2(**base)


def _finding(**kw) -> AnalystFindingV1:
    base = dict(
        finding_id="finding_1", case_id="research_case_1", perspective="fundamental",
        as_known_at=_T, horizon="20d",
        claims=(Claim(claim_id="claim_1", statement="영업이익률이 개선됐다",
                      claim_type=ClaimType.FACT, evidence_ids=("dart_1",),
                      confidence=0.72),),
        status=FindingStatus.COMPLETE,
        model_version="agent-research@1", prompt_version="res-fundamental@1")
    base.update(kw)
    return AnalystFindingV1(**base)


def _packet(**kw) -> ResearchPacketV2:
    base = dict(
        packet_id="rp_1", case_id="research_case_1", instrument_id="inst_005930",
        trigger="disclosure", as_known_at=_T, horizons=("20d",),
        macro_outlook=Outlook(direction="neutral", confidence=0.58),
        micro_outlook=Outlook(direction="positive", confidence=0.66,
                              claim_ids=("claim_1",)),
        thesis="단기 촉매는 있으나 중기 환경은 중립이다",
        calibration=Calibration(cohort="disclosure_20d"),
        status="PUBLISHED", lineage=Lineage(graph_version="research-rqf-v1"),
        findings=(_finding(),))
    base.update(kw)
    return ResearchPacketV2(**base)


def _rejects(fn, needle: str, label: str):
    try:
        fn()
    except Exception as e:  # pydantic ValidationError 포함
        assert needle in str(e), f"{label}: 다른 이유로 거부됐다\n{e}"
        return
    raise AssertionError(f"{label}: 통과하면 안 되는데 통과했다")


def _check_case():
    c = _case()
    assert c.as_known_at.tzinfo is not None
    _rejects(lambda: _case(as_known_at=datetime(2026, 8, 3, 1, 30)),
             "timezone", "naive as_known_at")
    _rejects(lambda: _case(required_perspectives=("fundamental",),
                           optional_perspectives=("fundamental",)),
             "필수이면서 선택", "관점 중복")
    _rejects(lambda: _case(horizons=("1d", "1d")), "중복", "horizon 중복")
    _rejects(lambda: _case(priority=101), "less than or equal", "priority 범위")
    _rejects(lambda: _case(instrument_ids=()), "at least 1", "빈 종목")
    # 계약에 없는 키는 조용히 무시되지 않는다
    _rejects(lambda: _case(오타필드=1), "Extra inputs", "미지의 필드")
    print("  ResearchCaseV2           OK")


def _check_finding():
    f = _finding()
    assert len(f.fact_claims()) == 1
    _rejects(lambda: _finding(claims=(Claim(claim_id="c", statement="s",
                                            claim_type=ClaimType.FACT,
                                            confidence=0.5),)),
             "근거 없는 사실 주장", "근거 없는 fact")
    # 추론이면 근거 없이도 통과한다 - fact 만 막는다
    AnalystFindingV1(**{**_finding().model_dump(),
                        "claims": (Claim(claim_id="c", statement="s",
                                         claim_type=ClaimType.INFERENCE,
                                         confidence=0.5),)})
    _rejects(lambda: _finding(claims=(), status=FindingStatus.COMPLETE),
             "빈 완료는 완료가 아니다", "빈 COMPLETE")
    _rejects(lambda: _finding(status=FindingStatus.BLOCKED),
             "시작도 못 했다면서", "BLOCKED 인데 결과 있음")
    _rejects(lambda: _finding(contradictions=("claim_1",)),
             "자기 claim", "자기모순 표기")
    _rejects(lambda: _finding(claims=(
        Claim(claim_id="x", statement="a", claim_type=ClaimType.INFERENCE, confidence=0.1),
        Claim(claim_id="x", statement="b", claim_type=ClaimType.INFERENCE, confidence=0.2))),
        "claim_id 중복", "claim_id 중복")
    # 부분 실패를 버리지 않는다 - PARTIAL 은 결과를 갖고 살아남는다
    p = _finding(status=FindingStatus.PARTIAL)
    assert p.claims, "PARTIAL 이 결과를 잃었다"
    print("  AnalystFindingV1         OK")


def _check_packet():
    p = _packet()
    assert p.perspectives_present() == ("fundamental",)
    assert p.uncalibrated is True, "기본이 uncalibrated 여야 한다"
    _rejects(lambda: _packet(micro_outlook=Outlook(direction="positive", confidence=0.6,
                                                   claim_ids=("없는claim",))),
             "끊어진 참조", "없는 claim 참조")
    _rejects(lambda: _packet(uncalibrated=False),
             "보정된 적 없는", "미보정인데 보정됐다고 표시")
    # claim 참조까지 비워 '빈 발행' 규칙만 격리한다 - 안 그러면 끊어진 참조
    # 규칙이 먼저 걸려 이 검사가 무엇을 확인하는지 알 수 없게 된다
    _rejects(lambda: _packet(findings=(), status="PUBLISHED",
                             micro_outlook=Outlook(direction="positive", confidence=0.6)),
             "빈 발행은 발행이 아니다", "빈 PUBLISHED")
    # 보정값이 있으면 표본 수가 있어야 한다
    _rejects(lambda: Calibration(cohort="c", historical_brier=0.2),
             "표본 없는 통계", "표본 없는 Brier")
    ok = _packet(calibration=Calibration(cohort="c", historical_brier=0.2,
                                         n_observations=40),
                 uncalibrated=False)
    assert ok.calibration.n_observations == 40
    print("  ResearchPacketV2         OK")


def _check_missing_required_counts_inconclusive():
    """'돌긴 돌았다' 를 '결과가 있다' 로 세지 않는가 - 필수 관점 누락 판정."""
    case = _case(required_perspectives=("fundamental", "technical"))
    # technical 이 INCONCLUSIVE 로 왔다
    tech = _finding(finding_id="f2", perspective="technical",
                    claims=(), status=FindingStatus.INCONCLUSIVE)
    p = _packet(status="PARTIAL", findings=(_finding(), tech))
    assert p.missing_required(case) == ("technical",), p.missing_required(case)
    # PARTIAL 로 왔다면 쓸 수 있는 것으로 센다
    tech_partial = _finding(finding_id="f3", perspective="technical",
                            status=FindingStatus.PARTIAL)
    p2 = _packet(status="PARTIAL", findings=(_finding(), tech_partial))
    assert p2.missing_required(case) == ()
    print("  필수 관점 누락 판정      OK")


def _check_json_roundtrip():
    """계약이 JSON Schema 로 나가고 다시 들어오는가 (Event·API 계약 강제용)."""
    p = _packet()
    again = ResearchPacketV2.model_validate_json(p.model_dump_json())
    assert again == p, "왕복에서 값이 변했다"
    schema = ResearchPacketV2.model_json_schema()
    assert "properties" in schema and "as_known_at" in schema["properties"]
    # LLM 이 흔히 내는 실수 - 문자열 시각도 받아야 한다
    raw = p.model_dump_json()
    assert ResearchPacketV2.model_validate_json(raw).as_known_at == _T
    print("  JSON 왕복·Schema         OK")


def _check_perspectives_match_agents():
    """계약의 관점 목록이 실제 분석가 모듈과 어긋나지 않는가.

    선언만 늘고 구현이 없으면 '있는 줄 알았던 관점' 이 조용히 비어 있게 된다.
    """
    from pathlib import Path

    agents = Path(__file__).resolve().parent.parent / "agents"
    expected = {
        "fundamental": "fundamental_analyst.py",
        "technical": "technical_analyst.py",
        "microstructure": "microstructure_analyst.py",
        "news_sentiment": "news_sentiment_analyst.py",
        "macro_regime": "sector_regime_analyst.py",
        "geopolitical": "geopolitical_analyst.py",
    }
    assert set(expected) == set(PERSPECTIVES), "관점 목록과 매핑이 어긋난다"
    missing = [f"{k} -> {v}" for k, v in expected.items() if not (agents / v).exists()]
    assert not missing, f"관점은 선언됐는데 구현 모듈이 없다: {missing}"
    print("  관점 <-> 분석가 모듈     OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{CONTRACT_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_case()
    _check_finding()
    _check_packet()
    _check_missing_required_counts_inconclusive()
    _check_json_roundtrip()
    _check_perspectives_match_agents()
    print("Research V2 계약 6개 영역 통과.")
