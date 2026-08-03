"""evidence_qa_engine.py의 __main__ 자체 점검을 pytest로 옮긴 것.

소유: 동규 (AI QA/감사본부). 시나리오 번호와 내용은 원본과 동일하게 유지한다.

실행: python -m pytest departments/06-ai-qa-audit/tests/test_evidence_qa_engine.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evidence"))

from evidence_qa_engine import (
    CHECKER_VERSION,
    Artifact,
    CheckFailureReason,
    Claim,
    ClaimCheckResult,
    ClaimKind,
    EvidenceAccess,
    EvidenceChunk,
    EvidenceQaEngine,
    EvidenceStore,
    FindingSeverity,
    QaAssessment,
    QaContext,
    QaDecisionValue,
    ToolResultRecord,
)

now = datetime.now(timezone.utc)
fund, trace = uuid4(), uuid4()
artifact_id = uuid4()
engine = EvidenceQaEngine()


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


def result_is(assessment: QaAssessment, idx: int, expected: ClaimCheckResult, why: str):
    actual = assessment.claim_checks[idx].result
    assert actual is expected, f"{why}: {actual} (기대: {expected})"


def decision_is(assessment: QaAssessment, expected: QaDecisionValue, why: str):
    assert assessment.decision is expected, f"{why}: {assessment.decision} (기대: {expected})"


def test_01_fact_with_matching_evidence_supported_pass():
    ev1 = evidence(numeric_value=Decimal(70000), unit="KRW")
    a1 = artifact_with(Claim(claim_index=0, text="AAPL 종가는 70000원", kind=ClaimKind.FACT,
                             subject="AAPL", numeric_value=Decimal(70000), unit="KRW",
                             evidence_ids=(ev1.evidence_id,)))
    r1 = engine.check_artifact(a1, ctx_with(store_with(ev1)))
    result_is(r1, 0, ClaimCheckResult.SUPPORTED, "정상 Fact")
    decision_is(r1, QaDecisionValue.PASS, "정상 Fact 단독")
    assert not r1.findings, "정상 Claim인데 Finding이 열림"


def test_02_fact_without_evidence_unsupported_fail_opens_finding():
    a2 = artifact_with(Claim(claim_index=0, text="AAPL은 반등한다", kind=ClaimKind.FACT, subject="AAPL"))
    r2 = engine.check_artifact(a2, ctx_with(EvidenceStore()))
    result_is(r2, 0, ClaimCheckResult.UNSUPPORTED, "근거 없는 Fact")
    decision_is(r2, QaDecisionValue.FAIL, "근거 없는 Fact")
    assert CheckFailureReason.FACT_WITHOUT_EVIDENCE in r2.reason_codes
    assert len(r2.findings) == 1 and r2.findings[0].severity is FindingSeverity.HIGH


def test_03_inference_without_evidence_not_applicable_pass():
    a3 = artifact_with(Claim(claim_index=0, text="다음 주 반등 가능성이 있다", kind=ClaimKind.INFERENCE))
    r3 = engine.check_artifact(a3, ctx_with(EvidenceStore()))
    result_is(r3, 0, ClaimCheckResult.NOT_APPLICABLE, "Inference는 근거 강제 안 함")
    decision_is(r3, QaDecisionValue.PASS, "Inference 단독")


def test_04_nonexistent_evidence_id_unsupported():
    a4 = artifact_with(Claim(claim_index=0, text="매출이 늘었다", kind=ClaimKind.FACT, subject="매출",
                             evidence_ids=(uuid4(),)))
    r4 = engine.check_artifact(a4, ctx_with(EvidenceStore()))
    result_is(r4, 0, ClaimCheckResult.UNSUPPORTED, "존재하지 않는 근거")
    assert CheckFailureReason.EVIDENCE_NOT_FOUND in r4.reason_codes


def test_05_access_denied_evidence_unsupported():
    ev5 = evidence()
    a5 = artifact_with(Claim(claim_index=0, text="내부 보고서에 따르면", kind=ClaimKind.FACT,
                             subject="내부", evidence_ids=(ev5.evidence_id,)))
    r5 = engine.check_artifact(a5, ctx_with(store_with(ev5, denied={ev5.evidence_id})))
    result_is(r5, 0, ClaimCheckResult.UNSUPPORTED, "접근 권한 없는 근거")
    assert CheckFailureReason.EVIDENCE_ACCESS_DENIED in r5.reason_codes


def test_06_evidence_published_after_decision_time_pit_violation_unsupported():
    ev6 = evidence(published_offset=timedelta(hours=2), observed_offset=timedelta(hours=2))
    a6 = artifact_with(Claim(claim_index=0, text="내일 발표 예정", kind=ClaimKind.FACT,
                             subject="발표", evidence_ids=(ev6.evidence_id,)))
    r6 = engine.check_artifact(a6, ctx_with(store_with(ev6)))
    result_is(r6, 0, ClaimCheckResult.UNSUPPORTED, "미래 근거(PIT 위반)")
    assert CheckFailureReason.EVIDENCE_NOT_YET_VALID in r6.reason_codes


def test_07_numeric_mismatch_unsupported():
    ev7 = evidence(numeric_value=Decimal(50000), unit="KRW")
    a7 = artifact_with(Claim(claim_index=0, text="AAPL 종가는 70000원", kind=ClaimKind.FACT,
                             subject="AAPL", numeric_value=Decimal(70000), unit="KRW",
                             evidence_ids=(ev7.evidence_id,)))
    r7 = engine.check_artifact(a7, ctx_with(store_with(ev7)))
    result_is(r7, 0, ClaimCheckResult.UNSUPPORTED, "숫자 불일치")
    assert CheckFailureReason.NUMERIC_CITATION_MISMATCH in r7.reason_codes


def test_08_contradicting_evidence_without_uncertainty_flag_contradicted_fail():
    ev8a = evidence(source="A", numeric_value=Decimal(70000), unit="KRW")
    ev8b = evidence(source="B", numeric_value=Decimal(50000), unit="KRW")
    a8 = artifact_with(Claim(claim_index=0, text="AAPL 종가는 약 6만원대", kind=ClaimKind.FACT,
                             subject="AAPL", evidence_ids=(ev8a.evidence_id, ev8b.evidence_id)))
    r8 = engine.check_artifact(a8, ctx_with(store_with(ev8a, ev8b)))
    result_is(r8, 0, ClaimCheckResult.CONTRADICTED, "상충 근거 미표시")
    assert CheckFailureReason.UNACKNOWLEDGED_CONTRADICTION in r8.reason_codes
    decision_is(r8, QaDecisionValue.FAIL, "상충 Claim 포함")


def test_09_same_contradiction_with_acknowledged_uncertainty_supported():
    ev8a = evidence(source="A", numeric_value=Decimal(70000), unit="KRW")
    ev8b = evidence(source="B", numeric_value=Decimal(50000), unit="KRW")
    a9 = artifact_with(Claim(claim_index=0, text="AAPL 종가는 출처마다 다르게 보고됨", kind=ClaimKind.FACT,
                             subject="AAPL", evidence_ids=(ev8a.evidence_id, ev8b.evidence_id),
                             acknowledges_uncertainty=True))
    r9 = engine.check_artifact(a9, ctx_with(store_with(ev8a, ev8b)))
    result_is(r9, 0, ClaimCheckResult.SUPPORTED, "상충이어도 불확실성 표시하면 통과")


def test_10_tool_result_deviates_from_summary_contradicted():
    ev10 = evidence(numeric_value=Decimal(100), unit="주")
    tool10 = ToolResultRecord(tool_name="portfolio-api", output_values={"AAPL": Decimal(60)})
    a10 = artifact_with(
        Claim(claim_index=0, text="AAPL 보유량은 100주", kind=ClaimKind.FACT, subject="AAPL",
              numeric_value=Decimal(100), unit="주", evidence_ids=(ev10.evidence_id,),
              tool_source="portfolio-api"),
        tool_results=(tool10,),
    )
    r10 = engine.check_artifact(a10, ctx_with(store_with(ev10)))
    result_is(r10, 0, ClaimCheckResult.CONTRADICTED, "Tool 결과와 요약 불일치")
    assert CheckFailureReason.TOOL_SUMMARY_DEVIATION in r10.reason_codes


def test_11_tool_result_matches_summary_supported():
    ev10 = evidence(numeric_value=Decimal(100), unit="주")
    tool11 = ToolResultRecord(tool_name="portfolio-api", output_values={"AAPL": Decimal(100)})
    a11 = artifact_with(
        Claim(claim_index=0, text="AAPL 보유량은 100주", kind=ClaimKind.FACT, subject="AAPL",
              numeric_value=Decimal(100), unit="주", evidence_ids=(ev10.evidence_id,),
              tool_source="portfolio-api"),
        tool_results=(tool11,),
    )
    r11 = engine.check_artifact(a11, ctx_with(store_with(ev10)))
    result_is(r11, 0, ClaimCheckResult.SUPPORTED, "Tool 결과와 요약 일치")


def test_12_one_invalid_one_valid_evidence_partial_warn():
    ev12 = evidence(numeric_value=Decimal(70000), unit="KRW")
    a12 = artifact_with(Claim(claim_index=0, text="AAPL 종가는 70000원", kind=ClaimKind.FACT,
                              subject="AAPL", numeric_value=Decimal(70000), unit="KRW",
                              evidence_ids=(uuid4(), ev12.evidence_id)))
    r12 = engine.check_artifact(a12, ctx_with(store_with(ev12)))
    result_is(r12, 0, ClaimCheckResult.PARTIAL, "일부 근거 무효, 나머지로 성립")
    assert CheckFailureReason.PARTIAL_EVIDENCE_SET in r12.reason_codes
    decision_is(r12, QaDecisionValue.WARN, "PARTIAL Claim 포함 - WARN이지 FAIL 아님")


def test_13_one_failing_claim_among_several_fails_whole_only_that_finding_opens():
    ev13 = evidence(numeric_value=Decimal(70000), unit="KRW")
    a13 = artifact_with(
        Claim(claim_index=0, text="AAPL 종가는 70000원", kind=ClaimKind.FACT, subject="AAPL",
              numeric_value=Decimal(70000), unit="KRW", evidence_ids=(ev13.evidence_id,)),
        Claim(claim_index=1, text="MSFT는 반등 여력이 있다", kind=ClaimKind.INFERENCE),
        Claim(claim_index=2, text="삼성전자 실적이 개선됐다", kind=ClaimKind.FACT, subject="삼성전자"),
    )
    r13 = engine.check_artifact(a13, ctx_with(store_with(ev13)))
    decision_is(r13, QaDecisionValue.FAIL, "3개 중 1개 실패")
    assert len(r13.claim_checks) == 3
    assert len(r13.findings) == 1, "실패한 Claim만 Finding이 열려야 함"


def test_14_blank_claim_text_rejected_at_construction():
    with pytest.raises(ValidationError):
        Claim(claim_index=0, text="   ", kind=ClaimKind.INFERENCE)


def test_15_fact_claim_without_subject_rejected_at_construction():
    with pytest.raises(ValidationError):
        Claim(claim_index=0, text="실적이 개선됐다", kind=ClaimKind.FACT)


def test_16_artifact_without_claims_rejected_at_construction():
    with pytest.raises(ValidationError):
        artifact_with()


def test_17_reproducibility_same_decision_and_input_hash():
    ev1 = evidence(numeric_value=Decimal(70000), unit="KRW")
    a1 = artifact_with(Claim(claim_index=0, text="AAPL 종가는 70000원", kind=ClaimKind.FACT,
                             subject="AAPL", numeric_value=Decimal(70000), unit="KRW",
                             evidence_ids=(ev1.evidence_id,)))
    r17a = engine.check_artifact(a1, ctx_with(store_with(ev1)))
    r17b = engine.check_artifact(a1, ctx_with(store_with(ev1)))
    assert r17a.decision is r17b.decision
    assert [c.result for c in r17a.claim_checks] == [c.result for c in r17b.claim_checks]
    assert r17a.input_hash == r17b.input_hash, "같은 Artifact·Context인데 input_hash가 다름"
    assert r17a.calculation_version == CHECKER_VERSION


def test_18_different_artifact_yields_different_input_hash():
    ev1 = evidence(numeric_value=Decimal(70000), unit="KRW")
    a1 = artifact_with(Claim(claim_index=0, text="AAPL 종가는 70000원", kind=ClaimKind.FACT,
                             subject="AAPL", numeric_value=Decimal(70000), unit="KRW",
                             evidence_ids=(ev1.evidence_id,)))
    r17a = engine.check_artifact(a1, ctx_with(store_with(ev1)))
    a2 = artifact_with(Claim(claim_index=0, text="AAPL은 반등한다", kind=ClaimKind.FACT, subject="AAPL"))
    r18 = engine.check_artifact(a2, ctx_with(EvidenceStore()))
    assert r18.input_hash != r17a.input_hash, "다른 Artifact인데 input_hash가 같음"
