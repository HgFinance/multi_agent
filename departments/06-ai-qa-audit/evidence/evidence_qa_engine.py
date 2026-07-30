#!/usr/bin/env python3
"""Sprint K2: P0 Evidence QA Engine.

소유: 동규 (AI QA/감사본부)
근거: docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md 6.2(Evidence와 Eval), 7.1(Research/Trading
      Artifact QA 검사 순서 8단계), 12(Sprint K2: Evidence QA)
      docs/HEDGE_FUND_MASTER_PLAN.md 5.6(QA 독립성), 5.9(결정론적 서비스)
      supabase/migrations/20260729000500_audit_api_security.sql의 audit.claim_checks,
      audit.qa_decisions, audit.findings 컬럼·값과 이름을 맞췄다.

LLM은 관련성 판단과 서술 작성에만 쓴다 (CLAUDE.md). 팀 가이드 7절 "LLM-as-a-Judge는 보조
신호다. Schema, Exact Match, Citation Location, 수치 재계산, Policy Rule과 Tool Trace 검사를
먼저 수행한다"의 그 "먼저 수행하는" 부분이 이 파일이다 - 여기엔 LLM 호출이 전혀 없다.
LLM Judge는 이 파이프라인이 안정화된 뒤 얹을 백로그다(skills/agentic-rag가 Risk 쪽에서
이미 하는 것과 같은 원칙 - retrieve/grade/generate는 LLM, PIT 필터·인용 검증은 순수 Python).

qa-audit-supervisor는 이 엔진의 판정을 해석·Escalation만 하고 판정 자체를 바꾸지 않는다.
원 작성 본부(리서치·트레이딩 등)는 QA Finding을 수정·종료할 수 없다
(CLAUDE.md "절대 깨면 안 되는 권한 분리").

Claim/Artifact/EvidenceChunk/ToolResultRecord는 LLM이 생성한 Research Packet/Order Intent에서
그대로 파싱해 넣는 값이므로 Pydantic으로 검증한다(CLAUDE.md 개발 원칙 2번 "LLM 출력은 항상
Pydantic Schema로 검증한다", departments/02-trading/contracts/contracts.py와 같은 원칙).
Schema/필수 Field 위반(팀 가이드 7.1 #1)은 여기서 생성 시점에 막히고, 이 엔진까지 오지 않는다 -
트레이딩본부가 OrderIntent 검증을 그 계층에서 끝내는 것과 동일하다.

자체 점검: python departments/06-ai-qa-audit/evidence/evidence_qa_engine.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

CHECKER_VERSION = "qa-evidence-p0-v1"
GATE_NAME = "research_trading_artifact"

# 숫자 인용 검증 허용 오차. 사람이 반올림해 옮겨 적는 정도는 통과시키되 사실과 다른
# 숫자는 잡는다.
NUMERIC_TOLERANCE = Decimal("0.01")

# 두 근거가 이 비율 이상 벌어지면 "상충"으로 본다 (반올림 차이가 아니라 실제 불일치).
CONTRADICTION_TOLERANCE = Decimal("0.05")


class ClaimKind(StrEnum):
    """팀 가이드 7.1 #5 "Fact, Inference, Forecast와 Recommendation 구분"."""

    FACT = "fact"
    INFERENCE = "inference"
    FORECAST = "forecast"
    RECOMMENDATION = "recommendation"


class ClaimCheckResult(StrEnum):
    """audit.claim_checks.result와 값이 같아야 한다."""

    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class QaDecisionValue(StrEnum):
    """audit.qa_decisions.decision과 값이 같아야 한다."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    CONDITIONAL = "CONDITIONAL"


class FindingSeverity(StrEnum):
    """audit.findings.severity와 값이 같아야 한다."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class CheckFailureReason(StrEnum):
    """audit.qa_decisions.reason_codes에 그대로 들어갈 구조화된 코드.
    Schema/필수 Field 위반(팀 가이드 7.1 #1)은 이제 Claim/Artifact 생성 시점에 Pydantic
    ValidationError로 막히므로 여기엔 없다 - 이 엔진까지 온 Claim은 이미 그 검사를 통과했다."""

    EVIDENCE_NOT_FOUND = "evidence_not_found"
    EVIDENCE_ACCESS_DENIED = "evidence_access_denied"
    EVIDENCE_NOT_YET_VALID = "evidence_not_yet_valid"  # PIT 위반(미래 근거)
    NUMERIC_CITATION_MISMATCH = "numeric_citation_mismatch"
    FACT_WITHOUT_EVIDENCE = "fact_without_evidence"
    UNACKNOWLEDGED_CONTRADICTION = "unacknowledged_contradiction"
    TOOL_SUMMARY_DEVIATION = "tool_summary_deviation"
    PARTIAL_EVIDENCE_SET = "partial_evidence_set"


# ---------------------------------------------------------------------------
# 입력 (rag-librarian-evidence-curator / Model Gateway Trace의 스텁)
# ---------------------------------------------------------------------------


class QaBase(BaseModel):
    """LLM 출력(Research Packet/Order Intent에서 파싱한 Claim 등)의 신뢰 경계.
    트레이딩본부 contracts.py의 Base와 같은 원칙 - 여기서 전부 검증하고 검증을 줄이지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceChunk(QaBase):
    evidence_id: UUID
    source: str = Field(min_length=1)
    published_at: datetime
    observed_at: datetime
    excerpt: str = Field(min_length=1)
    numeric_value: Decimal | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def _numeric_needs_unit(self):
        if self.numeric_value is not None and self.unit is None:
            raise ValueError("근거에 수치가 있으면 단위(unit)도 있어야 합니다")
        return self


@dataclass(frozen=True)
class EvidenceAccess:
    """접근 판정 자체는 IAM/Entitlement 쪽 결과이지 LLM 출력이 아니므로 일반 값 객체로 둔다."""

    granted: bool
    reason: str = ""


@dataclass
class EvidenceStore:
    """rag-librarian-evidence-curator가 채우는 값의 스텁. QA는 근거를 직접 수집하지
    않는다(팀 가이드 3.2 "참조" 행) - 주어진 Evidence ID로 찾아볼 뿐이다."""

    chunks: dict[UUID, EvidenceChunk] = field(default_factory=dict)
    access: dict[UUID, EvidenceAccess] = field(default_factory=dict)

    def lookup(self, evidence_id: UUID) -> tuple[EvidenceChunk | None, EvidenceAccess]:
        chunk = self.chunks.get(evidence_id)
        access = self.access.get(evidence_id, EvidenceAccess(granted=chunk is not None))
        return chunk, access


class ToolResultRecord(QaBase):
    """팀 가이드 7.1 #7 "Tool 결과와 Agent 요약의 변형 여부" 검사용. audit.tool_calls의
    output_hash 대신 P0 프로토타입에서는 검증 가능한 숫자 필드를 직접 들고 있는다."""

    tool_name: str = Field(min_length=1)
    output_values: dict[str, Decimal] = Field(default_factory=dict)


class Claim(QaBase):
    claim_index: int = Field(ge=0)
    text: str = Field(min_length=1)
    kind: ClaimKind
    subject: str | None = None
    numeric_value: Decimal | None = None
    unit: str | None = None
    evidence_ids: tuple[UUID, ...] = ()
    acknowledges_uncertainty: bool = False  # 팀 가이드 7.1 #6
    tool_source: str | None = None  # ToolResultRecord.tool_name과 매칭

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, v: str) -> str:
        # min_length=1은 공백만 있는 문자열은 못 막는다 - "   "도 길이 3이라 통과해버림.
        if not v.strip():
            raise ValueError("Claim 텍스트가 공백만으로 이루어져 있습니다")
        return v

    @model_validator(mode="after")
    def _check(self):
        # 팀 가이드 7.1 #1 Schema/필수 Field: Fact 주장은 주체(subject) 없이 존재할 수 없다.
        if self.kind is ClaimKind.FACT and not self.subject:
            raise ValueError("Fact Claim에 주체(subject)가 없습니다")
        if self.numeric_value is not None and self.unit is None:
            raise ValueError("숫자 주장에는 단위(unit)가 있어야 합니다")
        return self


class Artifact(QaBase):
    artifact_version_id: UUID
    artifact_type: str = Field(min_length=1)  # "research_packet" | "order_intent" | ...
    producer: str = Field(min_length=1)
    fund_id: UUID
    trace_id: UUID
    claims: tuple[Claim, ...] = Field(min_length=1)
    tool_results: tuple[ToolResultRecord, ...] = ()


@dataclass
class QaContext:
    evidence_store: EvidenceStore
    decision_time: datetime  # PIT 기준 시각 - 이 Artifact가 실제로 쓰인 시점
    checker_version: str = CHECKER_VERSION


# ---------------------------------------------------------------------------
# 출력 (audit.claim_checks / audit.qa_decisions / audit.findings 컬럼과 대응)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimCheck:
    claim_check_id: UUID
    artifact_version_id: UUID
    claim_index: int
    claim: str
    evidence_chunk_ids: tuple[UUID, ...]
    result: ClaimCheckResult
    reason: str
    checker_version: str
    checked_at: datetime


@dataclass(frozen=True)
class FindingDraft:
    """audit.findings 1행 초안. QA만 실제로 Open한다 - 원 작성 본부는 만들 수 없다."""

    finding_id: UUID
    fund_id: UUID
    finding_type: str
    severity: FindingSeverity
    artifact_version_id: UUID
    description: str
    opened_by: str
    trace_id: UUID
    created_at: datetime


@dataclass(frozen=True)
class QaAssessment:
    """audit.qa_decisions 1행 + 딸린 claim_checks/findings."""

    qa_decision_id: UUID
    artifact_version_id: UUID
    gate: str
    decision: QaDecisionValue
    reason_codes: tuple[CheckFailureReason, ...]
    claim_checks: tuple[ClaimCheck, ...]
    findings: tuple[FindingDraft, ...]
    decided_by: str
    trace_id: UUID
    decided_at: datetime

    @property
    def blocked(self) -> bool:
        return self.decision is QaDecisionValue.FAIL


class EvidenceQaEngine:
    """결정론적 Evidence QA Gate. 여기 없는 판정 경로는 없다."""

    def __init__(self, service_identity: str = "svc_qa_evaluator") -> None:
        self.service_identity = service_identity

    def check_artifact(
        self,
        artifact: Artifact,
        ctx: QaContext,
        qa_decision_id: UUID | None = None,
    ) -> QaAssessment:
        # Claim이 비어있는 Artifact는 Pydantic이 생성 시점에 이미 막는다
        # (Artifact.claims: Field(min_length=1)) - 여기까지 오지 않는다.
        qa_decision_id = qa_decision_id or uuid4()
        claim_checks: list[ClaimCheck] = []
        reason_codes: list[CheckFailureReason] = []
        findings: list[FindingDraft] = []

        for claim in artifact.claims:
            result, reason, failure = self._check_claim(claim, artifact, ctx)
            claim_checks.append(ClaimCheck(
                claim_check_id=uuid4(),
                artifact_version_id=artifact.artifact_version_id,
                claim_index=claim.claim_index,
                claim=claim.text,
                evidence_chunk_ids=claim.evidence_ids,
                result=result,
                reason=reason,
                checker_version=ctx.checker_version,
                checked_at=ctx.decision_time,
            ))
            if failure is not None:
                reason_codes.append(failure)

            # 8. Material Unsupported Claim이면 Block - 여기서 Finding도 함께 연다.
            # 원 작성 본부가 이 Finding을 스스로 닫을 수 없다(팀 가이드 1절 "QA Finding을
            # 작성자가 직접 종료" 금지).
            if result in (ClaimCheckResult.UNSUPPORTED, ClaimCheckResult.CONTRADICTED):
                findings.append(FindingDraft(
                    finding_id=uuid4(),
                    fund_id=artifact.fund_id,
                    finding_type=(
                        "unsupported_claim" if result is ClaimCheckResult.UNSUPPORTED
                        else "contradicted_claim"
                    ),
                    severity=FindingSeverity.HIGH,
                    artifact_version_id=artifact.artifact_version_id,
                    description=f"Claim #{claim.claim_index} ({artifact.artifact_type}): {reason}",
                    opened_by=self.service_identity,
                    trace_id=artifact.trace_id,
                    created_at=ctx.decision_time,
                ))

        return QaAssessment(
            qa_decision_id=qa_decision_id,
            artifact_version_id=artifact.artifact_version_id,
            gate=GATE_NAME,
            decision=self._aggregate(claim_checks),
            reason_codes=tuple(reason_codes),
            claim_checks=tuple(claim_checks),
            findings=tuple(findings),
            decided_by=self.service_identity,
            trace_id=artifact.trace_id,
            decided_at=ctx.decision_time,
        )

    @staticmethod
    def _aggregate(claim_checks: list[ClaimCheck]) -> QaDecisionValue:
        results = [c.result for c in claim_checks]
        if any(r in (ClaimCheckResult.UNSUPPORTED, ClaimCheckResult.CONTRADICTED) for r in results):
            return QaDecisionValue.FAIL
        if any(r is ClaimCheckResult.PARTIAL for r in results):
            return QaDecisionValue.WARN
        return QaDecisionValue.PASS

    def _check_claim(
        self, claim: Claim, artifact: Artifact, ctx: QaContext,
    ) -> tuple[ClaimCheckResult, str, CheckFailureReason | None]:
        # 1. JSON Schema와 필수 Field - 빈 텍스트/주체 없는 Fact는 Claim 생성 시점에
        # 이미 Pydantic이 막았다(QaBase 트러스트 경계). 여기서부터는 통과한 Claim만 본다.

        # 5. Fact, Inference, Forecast와 Recommendation 구분 - Fact만 근거를 강제한다.
        if not claim.evidence_ids:
            if claim.kind is ClaimKind.FACT:
                return (
                    ClaimCheckResult.UNSUPPORTED, "사실 주장인데 인용 근거가 없습니다",
                    CheckFailureReason.FACT_WITHOUT_EVIDENCE,
                )
            return ClaimCheckResult.NOT_APPLICABLE, f"{claim.kind.value} - 근거 인용 대상 아님", None

        # 2. Evidence ID 존재와 접근 권한 + 3. published_at/observed_at <= decision_time.
        # 근거 일부가 무효해도 남은 근거로 주장이 성립하면 PARTIAL로 남기고, 전부
        # 무효하면 그때 UNSUPPORTED다 - 첫 문제에서 바로 끊지 않는다.
        valid_chunks: list[EvidenceChunk] = []
        problems: list[tuple[str, CheckFailureReason]] = []
        for evidence_id in claim.evidence_ids:
            chunk, access = ctx.evidence_store.lookup(evidence_id)
            if chunk is None:
                problems.append((f"근거 {evidence_id}를 찾을 수 없음", CheckFailureReason.EVIDENCE_NOT_FOUND))
                continue
            if not access.granted:
                problems.append((
                    f"근거 {evidence_id} 접근 권한 없음 ({access.reason})",
                    CheckFailureReason.EVIDENCE_ACCESS_DENIED,
                ))
                continue
            if chunk.published_at > ctx.decision_time or chunk.observed_at > ctx.decision_time:
                problems.append((
                    f"근거 {evidence_id}가 판단 시점({ctx.decision_time.isoformat()}) 이후 데이터",
                    CheckFailureReason.EVIDENCE_NOT_YET_VALID,
                ))
                continue
            valid_chunks.append(chunk)

        if not valid_chunks:
            first_detail, first_reason = problems[0]
            return ClaimCheckResult.UNSUPPORTED, f"인용 근거가 전부 무효합니다: {first_detail}", first_reason

        # 4. Claim의 숫자·단위·주체와 Citation 일치 (유효한 근거만 대상).
        if claim.numeric_value is not None:
            matched = any(
                chunk.numeric_value is not None
                and chunk.unit == claim.unit
                and abs(chunk.numeric_value - claim.numeric_value) <= abs(claim.numeric_value) * NUMERIC_TOLERANCE
                for chunk in valid_chunks
            )
            if not matched:
                return (
                    ClaimCheckResult.UNSUPPORTED,
                    f"주장한 수치 {claim.numeric_value}{claim.unit or ''}가 유효 근거와 일치하지 않습니다",
                    CheckFailureReason.NUMERIC_CITATION_MISMATCH,
                )

        # 6. 상충 Evidence와 불확실성 표시 (유효한 근거끼리만 비교).
        numeric_chunks = [c for c in valid_chunks if c.numeric_value is not None]
        if len(numeric_chunks) >= 2:
            values = [c.numeric_value for c in numeric_chunks]
            spread = max(values) - min(values)
            if spread > max(values) * CONTRADICTION_TOLERANCE and not claim.acknowledges_uncertainty:
                return (
                    ClaimCheckResult.CONTRADICTED,
                    "인용 근거끼리 값이 상충하는데 불확실성 표시가 없습니다",
                    CheckFailureReason.UNACKNOWLEDGED_CONTRADICTION,
                )

        # 7. Tool 결과와 Agent 요약의 변형 여부.
        if claim.tool_source is not None and claim.numeric_value is not None and claim.subject is not None:
            tool_record = next(
                (t for t in artifact.tool_results if t.tool_name == claim.tool_source), None,
            )
            if tool_record is not None and claim.subject in tool_record.output_values:
                actual = tool_record.output_values[claim.subject]
                if abs(actual - claim.numeric_value) > abs(actual) * NUMERIC_TOLERANCE:
                    return (
                        ClaimCheckResult.CONTRADICTED,
                        f"Tool 실제 결과({actual})와 Agent 요약({claim.numeric_value})이 다릅니다",
                        CheckFailureReason.TOOL_SUMMARY_DEVIATION,
                    )

        if problems:
            return (
                ClaimCheckResult.PARTIAL,
                f"근거 {len(problems)}건은 무효했지만 남은 {len(valid_chunks)}건으로 주장이 성립합니다",
                CheckFailureReason.PARTIAL_EVIDENCE_SET,
            )
        return ClaimCheckResult.SUPPORTED, "", None


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    fund, trace = uuid4(), uuid4()
    artifact_id = uuid4()

    def evidence(
        source="research-api", published_offset=timedelta(hours=-1), observed_offset=timedelta(hours=-1),
        numeric_value=None, unit=None, excerpt="근거 원문",
    ) -> EvidenceChunk:
        return EvidenceChunk(
            evidence_id=uuid4(), source=source,
            published_at=now + published_offset, observed_at=now + observed_offset,
            excerpt=excerpt, numeric_value=numeric_value, unit=unit,
        )

    def store_with(*chunks: EvidenceChunk, denied: set | None = None) -> EvidenceStore:
        denied = denied or set()
        access = {
            c.evidence_id: EvidenceAccess(granted=c.evidence_id not in denied, reason="권한 없음")
            for c in chunks
        }
        return EvidenceStore(chunks={c.evidence_id: c for c in chunks}, access=access)

    def ctx_with(store: EvidenceStore) -> QaContext:
        return QaContext(evidence_store=store, decision_time=now)

    def artifact_with(*claims: Claim, tool_results: tuple[ToolResultRecord, ...] = ()) -> Artifact:
        return Artifact(
            artifact_version_id=artifact_id, artifact_type="research_packet",
            producer="research-supervisor", fund_id=fund, trace_id=trace,
            claims=claims, tool_results=tool_results,
        )

    engine = EvidenceQaEngine()

    def result_is(assessment: QaAssessment, idx: int, expected: ClaimCheckResult, why: str):
        actual = assessment.claim_checks[idx].result
        assert actual is expected, f"{why}: {actual} (기대: {expected})"

    def decision_is(assessment: QaAssessment, expected: QaDecisionValue, why: str):
        assert assessment.decision is expected, f"{why}: {assessment.decision} (기대: {expected})"

    # 1. Fact + 일치하는 근거 1건 -> SUPPORTED, 전체 PASS
    ev1 = evidence(numeric_value=Decimal("70000"), unit="KRW")
    a1 = artifact_with(Claim(claim_index=0, text="AAPL 종가는 70000원", kind=ClaimKind.FACT,
                             subject="AAPL", numeric_value=Decimal("70000"), unit="KRW",
                             evidence_ids=(ev1.evidence_id,)))
    r1 = engine.check_artifact(a1, ctx_with(store_with(ev1)))
    result_is(r1, 0, ClaimCheckResult.SUPPORTED, "정상 Fact")
    decision_is(r1, QaDecisionValue.PASS, "정상 Fact 단독")
    assert not r1.findings, "정상 Claim인데 Finding이 열림"

    # 2. Fact + 근거 없음 -> UNSUPPORTED, FAIL, Finding 1건
    a2 = artifact_with(Claim(claim_index=0, text="AAPL은 반등한다", kind=ClaimKind.FACT, subject="AAPL"))
    r2 = engine.check_artifact(a2, ctx_with(EvidenceStore()))
    result_is(r2, 0, ClaimCheckResult.UNSUPPORTED, "근거 없는 Fact")
    decision_is(r2, QaDecisionValue.FAIL, "근거 없는 Fact")
    assert CheckFailureReason.FACT_WITHOUT_EVIDENCE in r2.reason_codes
    assert len(r2.findings) == 1 and r2.findings[0].severity is FindingSeverity.HIGH

    # 3. Inference + 근거 없음 -> NOT_APPLICABLE (근거 강제 안 함), PASS
    a3 = artifact_with(Claim(claim_index=0, text="다음 주 반등 가능성이 있다", kind=ClaimKind.INFERENCE))
    r3 = engine.check_artifact(a3, ctx_with(EvidenceStore()))
    result_is(r3, 0, ClaimCheckResult.NOT_APPLICABLE, "Inference는 근거 강제 안 함")
    decision_is(r3, QaDecisionValue.PASS, "Inference 단독")

    # 4. 존재하지 않는 Evidence ID -> UNSUPPORTED
    a4 = artifact_with(Claim(claim_index=0, text="매출이 늘었다", kind=ClaimKind.FACT, subject="매출",
                             evidence_ids=(uuid4(),)))
    r4 = engine.check_artifact(a4, ctx_with(EvidenceStore()))
    result_is(r4, 0, ClaimCheckResult.UNSUPPORTED, "존재하지 않는 근거")
    assert CheckFailureReason.EVIDENCE_NOT_FOUND in r4.reason_codes

    # 5. 접근 권한 없는 Evidence -> UNSUPPORTED
    ev5 = evidence()
    a5 = artifact_with(Claim(claim_index=0, text="내부 보고서에 따르면", kind=ClaimKind.FACT,
                             subject="내부", evidence_ids=(ev5.evidence_id,)))
    r5 = engine.check_artifact(a5, ctx_with(store_with(ev5, denied={ev5.evidence_id})))
    result_is(r5, 0, ClaimCheckResult.UNSUPPORTED, "접근 권한 없는 근거")
    assert CheckFailureReason.EVIDENCE_ACCESS_DENIED in r5.reason_codes

    # 6. 판단 시점 이후에 발행된 근거 (PIT 위반) -> UNSUPPORTED
    ev6 = evidence(published_offset=timedelta(hours=2), observed_offset=timedelta(hours=2))
    a6 = artifact_with(Claim(claim_index=0, text="내일 발표 예정", kind=ClaimKind.FACT,
                             subject="발표", evidence_ids=(ev6.evidence_id,)))
    r6 = engine.check_artifact(a6, ctx_with(store_with(ev6)))
    result_is(r6, 0, ClaimCheckResult.UNSUPPORTED, "미래 근거(PIT 위반)")
    assert CheckFailureReason.EVIDENCE_NOT_YET_VALID in r6.reason_codes

    # 7. 숫자 불일치 -> UNSUPPORTED
    ev7 = evidence(numeric_value=Decimal("50000"), unit="KRW")
    a7 = artifact_with(Claim(claim_index=0, text="AAPL 종가는 70000원", kind=ClaimKind.FACT,
                             subject="AAPL", numeric_value=Decimal("70000"), unit="KRW",
                             evidence_ids=(ev7.evidence_id,)))
    r7 = engine.check_artifact(a7, ctx_with(store_with(ev7)))
    result_is(r7, 0, ClaimCheckResult.UNSUPPORTED, "숫자 불일치")
    assert CheckFailureReason.NUMERIC_CITATION_MISMATCH in r7.reason_codes

    # 8. 상충하는 두 근거, 불확실성 표시 없음 -> CONTRADICTED, Finding
    ev8a = evidence(source="A", numeric_value=Decimal("70000"), unit="KRW")
    ev8b = evidence(source="B", numeric_value=Decimal("50000"), unit="KRW")
    a8 = artifact_with(Claim(claim_index=0, text="AAPL 종가는 약 6만원대", kind=ClaimKind.FACT,
                             subject="AAPL", evidence_ids=(ev8a.evidence_id, ev8b.evidence_id)))
    r8 = engine.check_artifact(a8, ctx_with(store_with(ev8a, ev8b)))
    result_is(r8, 0, ClaimCheckResult.CONTRADICTED, "상충 근거 미표시")
    assert CheckFailureReason.UNACKNOWLEDGED_CONTRADICTION in r8.reason_codes
    decision_is(r8, QaDecisionValue.FAIL, "상충 Claim 포함")

    # 9. 같은 상충 근거인데 acknowledges_uncertainty=True -> 통과 (SUPPORTED)
    a9 = artifact_with(Claim(claim_index=0, text="AAPL 종가는 출처마다 다르게 보고됨", kind=ClaimKind.FACT,
                             subject="AAPL", evidence_ids=(ev8a.evidence_id, ev8b.evidence_id),
                             acknowledges_uncertainty=True))
    r9 = engine.check_artifact(a9, ctx_with(store_with(ev8a, ev8b)))
    result_is(r9, 0, ClaimCheckResult.SUPPORTED, "상충이어도 불확실성 표시하면 통과")

    # 10. Tool 실제 결과와 Agent 요약이 다름 -> CONTRADICTED
    ev10 = evidence(numeric_value=Decimal("100"), unit="주")
    tool10 = ToolResultRecord(tool_name="portfolio-api", output_values={"AAPL": Decimal("60")})
    a10 = artifact_with(
        Claim(claim_index=0, text="AAPL 보유량은 100주", kind=ClaimKind.FACT, subject="AAPL",
              numeric_value=Decimal("100"), unit="주", evidence_ids=(ev10.evidence_id,),
              tool_source="portfolio-api"),
        tool_results=(tool10,),
    )
    r10 = engine.check_artifact(a10, ctx_with(store_with(ev10)))
    result_is(r10, 0, ClaimCheckResult.CONTRADICTED, "Tool 결과와 요약 불일치")
    assert CheckFailureReason.TOOL_SUMMARY_DEVIATION in r10.reason_codes

    # 11. Tool 실제 결과와 요약이 일치 -> SUPPORTED
    tool11 = ToolResultRecord(tool_name="portfolio-api", output_values={"AAPL": Decimal("100")})
    a11 = artifact_with(
        Claim(claim_index=0, text="AAPL 보유량은 100주", kind=ClaimKind.FACT, subject="AAPL",
              numeric_value=Decimal("100"), unit="주", evidence_ids=(ev10.evidence_id,),
              tool_source="portfolio-api"),
        tool_results=(tool11,),
    )
    r11 = engine.check_artifact(a11, ctx_with(store_with(ev10)))
    result_is(r11, 0, ClaimCheckResult.SUPPORTED, "Tool 결과와 요약 일치")

    # 12. 근거 2건 중 1건은 존재하지 않고 1건은 유효+일치 -> PARTIAL (완전 실패 아님)
    ev12 = evidence(numeric_value=Decimal("70000"), unit="KRW")
    a12 = artifact_with(Claim(claim_index=0, text="AAPL 종가는 70000원", kind=ClaimKind.FACT,
                              subject="AAPL", numeric_value=Decimal("70000"), unit="KRW",
                              evidence_ids=(uuid4(), ev12.evidence_id)))
    r12 = engine.check_artifact(a12, ctx_with(store_with(ev12)))
    result_is(r12, 0, ClaimCheckResult.PARTIAL, "일부 근거 무효, 나머지로 성립")
    assert CheckFailureReason.PARTIAL_EVIDENCE_SET in r12.reason_codes
    decision_is(r12, QaDecisionValue.WARN, "PARTIAL Claim 포함 - WARN이지 FAIL 아님")

    # 13. 여러 Claim 중 1개만 실패해도 전체 FAIL, Finding은 실패한 것만 생성
    ev13 = evidence(numeric_value=Decimal("70000"), unit="KRW")
    a13 = artifact_with(
        Claim(claim_index=0, text="AAPL 종가는 70000원", kind=ClaimKind.FACT, subject="AAPL",
              numeric_value=Decimal("70000"), unit="KRW", evidence_ids=(ev13.evidence_id,)),
        Claim(claim_index=1, text="MSFT는 반등 여력이 있다", kind=ClaimKind.INFERENCE),
        Claim(claim_index=2, text="삼성전자 실적이 개선됐다", kind=ClaimKind.FACT, subject="삼성전자"),  # 근거 없음
    )
    r13 = engine.check_artifact(a13, ctx_with(store_with(ev13)))
    decision_is(r13, QaDecisionValue.FAIL, "3개 중 1개 실패")
    assert len(r13.claim_checks) == 3
    assert len(r13.findings) == 1, "실패한 Claim만 Finding이 열려야 함"

    # 14. Claim 텍스트가 공백뿐 -> Pydantic이 생성 시점에 막는다 (엔진까지 오지 않음)
    try:
        Claim(claim_index=0, text="   ", kind=ClaimKind.INFERENCE)
        raise AssertionError("공백만 있는 Claim 텍스트가 생성됐다")
    except ValidationError:
        pass

    # 15. Fact Claim에 주체(subject)가 없음 -> Pydantic이 생성 시점에 막는다
    try:
        Claim(claim_index=0, text="실적이 개선됐다", kind=ClaimKind.FACT)
        raise AssertionError("주체 없는 Fact Claim이 생성됐다")
    except ValidationError:
        pass

    # 16. Claim이 하나도 없는 Artifact -> Pydantic이 생성 시점에 막는다 (조용히 통과 안 시킴)
    try:
        artifact_with()
        raise AssertionError("Claim 없는 Artifact가 생성됐다")
    except ValidationError:
        pass

    # 17. 재현성 - 같은 Artifact·Context를 두 번 평가하면 같은 판정
    r17a = engine.check_artifact(a1, ctx_with(store_with(ev1)))
    r17b = engine.check_artifact(a1, ctx_with(store_with(ev1)))
    assert r17a.decision is r17b.decision
    assert [c.result for c in r17a.claim_checks] == [c.result for c in r17b.claim_checks]

    print("ok - Evidence QA Engine 17개 시나리오 점검 통과")
