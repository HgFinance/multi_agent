"""Verification-to-QA employee contract tests."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

QA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QA_DIR))
EVIDENCE_DIR = QA_DIR / "evidence"
sys.path.insert(0, str(EVIDENCE_DIR))

from evidence_qa_engine import Claim, ClaimKind, EvidenceChunk
from qa_mandate_workers import (
    PineconeEvidenceClient,
    QaVerificationRequest,
    ReferenceEvidence,
    assess_qa_verification,
    run_hallucination_critic_worker,
    run_incident_postmortem_worker,
    run_qa_runner,
)

NOW = datetime.now(timezone.utc)
FUND_ID, TRACE_ID, ARTIFACT_ID = uuid4(), uuid4(), uuid4()


def _artifact(**claim_kwargs: object) -> dict[str, object]:
    return {
        "artifact_version_id": str(ARTIFACT_ID),
        "artifact_type": "research_packet",
        "producer": "research-supervisor",
        "fund_id": str(FUND_ID),
        "trace_id": str(TRACE_ID),
        "claims": [{"claim_index": 0, **claim_kwargs}],
    }


def _request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "verification_id": "VER-20260807-001",
        "artifact": _artifact(text="AAPL은 반등한다", kind="fact", subject="AAPL"),
        "decision_time": NOW.isoformat(),
    }
    payload.update(overrides)
    return payload


def test_verification_is_dispatched_to_three_independent_qa_employees() -> None:
    result = assess_qa_verification(_request())

    assert result["dispatch"]["dispatcher"] == "qa-head"
    assert result["dispatch"]["mutation_allowed"] is False
    assert set(result["employees"]) == {
        "qa-runner",
        "hallucination-critic-worker",
        "incident-postmortem-worker",
    }
    hashes = {report["input_hash"] for report in result["employees"].values()}
    assert len(hashes) == 1
    assert hashes.pop() == result["dispatch"]["input_hash"]
    runner_report = result["employees"]["qa-runner"]
    critic_report = result["employees"]["hallucination-critic-worker"]
    assert runner_report["authoritative"] is True
    assert runner_report["binding"] is False
    assert critic_report["authoritative"] is False
    assert result["qa_head"]["binding"] is False


def test_qa_runner_passes_a_fully_supported_claim_without_running_advisory_workers() -> None:
    evidence = EvidenceChunk(
        evidence_id=uuid4(),
        source="research-api",
        published_at=NOW,
        observed_at=NOW,
        excerpt="근거",
        numeric_value=Decimal(100),
        unit="KRW",
    )
    request = QaVerificationRequest.model_validate(
        _request(
            artifact=_artifact(
                text="AAPL 종가는 100원",
                kind="fact",
                subject="AAPL",
                numeric_value="100",
                unit="KRW",
                evidence_ids=[str(evidence.evidence_id)],
            ),
            evidence_chunks=[evidence.model_dump(mode="json")],
        )
    )
    report = run_qa_runner(request)

    assert report["decision"] == "PASS"
    assert report["unsupported_claim_indexes"] == []
    assert report["action_required"] is False


def test_hallucination_critic_escalates_when_no_evidence_is_available() -> None:
    request = QaVerificationRequest.model_validate(_request())
    qa_report = run_qa_runner(request)
    report = run_hallucination_critic_worker(
        request, qa_report, pinecone=PineconeEvidenceClient(api_key="", index_host="")
    )

    assert report["status"] == "DEGRADED"
    assert report["verdict"] == "ESCALATE"
    assert report["namespace"] == "qa-hallucination-reference"
    assert report["error"] is None  # no query vector means no network was attempted


def test_hallucination_critic_never_clears_a_finding_even_when_confirmed() -> None:
    request = QaVerificationRequest.model_validate(
        _request(
            hallucination_evidence=[
                ReferenceEvidence(
                    evidence_id="ref-1", title="Incident precedent", text="...", contradicts=True
                ).model_dump(mode="json")
            ]
        )
    )
    qa_report = run_qa_runner(request)
    report = run_hallucination_critic_worker(request, qa_report)

    assert report["verdict"] == "CONFIRMED"
    assert report["authoritative"] is False
    assert report["binding"] is False
    assert report["action_required"] is True


def test_incident_postmortem_worker_flags_unclassified_entries_without_closing_anything() -> None:
    request = QaVerificationRequest.model_validate(
        _request(
            incident={
                "incident_id": str(uuid4()),
                "entries": [{"entry_type": "UNKNOWN", "summary": "x", "occurred_at": NOW.isoformat()}],
            }
        )
    )
    report = run_incident_postmortem_worker(request)

    assert report["status"] == "COMPLETED"
    assert report["verdict"] == "ESCALATE"
    assert report["unclassified_entries"] == 1
    assert report["binding"] is False


def test_claim_import_type_is_reused_not_redefined() -> None:
    # Ponytail check: qa_mandate_workers must reuse evidence_qa_engine's Claim/ClaimKind,
    # not redefine its own copy.
    claim = Claim(claim_index=0, text="x", kind=ClaimKind.FACT, subject="AAPL")
    assert claim.kind is ClaimKind.FACT
