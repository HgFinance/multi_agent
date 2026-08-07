"""Mandate-driven QA verification pipeline.

담당: Claude Code (동규 위임, 2026-08-07 스코프 확정 메시지 기준). Risk의
``risk_mandate_workers.py``와 같은 패턴 — 얇은 동기 오케스트레이터가 두 개의
독립 QA 직원 보고서를 만들고, 그 위에 non-binding fan-in만 얹는다. 이 모듈은
``EvidenceQaEngine``의 판정을 바꾸지 않고, Finding을 닫지 않고, 주문을 만들지
않는다.

이 파일이 다루는 QA 직원은 셋이다.

* ``qa-runner`` — ``EvidenceQaEngine.check_artifact()``를 그대로 감싼
  결정론적 직원. Claim 판정에 대해 authoritative하지만 Finding을 닫거나
  주문을 만드는 binding 권한은 없다.
* ``hallucination-critic-worker`` — qa-runner가 UNSUPPORTED/CONTRADICTED로
  판정한 Claim이 있을 때만 실행되는 advisory 직원. Pinecone Namespace는
  ``qa-hallucination-reference`` 하나로 고정이며 요청 payload로 바꿀 수 없다
  (ADR-0006). 근거가 없으면 조용히 통과시키지 않고 ESCALATE로 fail-closed한다.
* ``incident-postmortem-worker`` — ``request.incident``가 있을 때만 실행되는
  advisory 직원. FACT/INFERENCE 분류 자체는 하지 않는다(그 분류는
  ``qa_employee_workers.py``의 LangGraph LLM 직원 몫) — 여기서는 Entry 형식이
  올바른지만 결정론으로 점검한다.

Risk 쪽 Pinecone Namespace·Contract·RiskEngine은 이 모듈이 소유하지 않는다.
Risk Verdict를 바꾸거나, Risk Namespace를 조회하거나, RiskEngine을
재실행하거나, Risk Mandate를 바꾸거나, 주문을 승인/제출하지 않는다.

자체 점검: python departments/06-ai-qa-audit/qa_mandate_workers.py
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

import requests
from pydantic import BaseModel, ConfigDict, Field, model_validator

_QA_DIR = Path(__file__).resolve().parent
_EVIDENCE_DIR = _QA_DIR / "evidence"
if str(_EVIDENCE_DIR) not in sys.path:
    sys.path.insert(0, str(_EVIDENCE_DIR))

from evidence_qa_engine import (
    Artifact,
    ClaimCheckResult,
    EvidenceAccess,
    EvidenceChunk,
    EvidenceQaEngine,
    EvidenceStore,
    QaContext,
)

# ADR-0006: QA의 정적 참고자료 Namespace는 이 하나로 고정한다. 요청 payload나
# 환경변수로 바꿀 수 없다 — Risk의 compliance corpus와 섞이면 안 된다.
QA_NAMESPACE = "qa-hallucination-reference"

_INCIDENT_ENTRY_TYPES = {"FACT", "INFERENCE", "ACTION", "DECISION"}


class EvidenceAccessInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: UUID
    granted: bool
    reason: str = ""


class ReferenceEvidence(BaseModel):
    """hallucination-critic-worker에 직접 공급하는 참고자료 1건.

    Pinecone 응답 형태와 대칭이도록 유지한다 — Pinecone에서 온 것이든
    요청에 직접 실린 것이든 아래 코드가 같은 모양으로 다룬다."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=10000)
    source: str | None = Field(default=None, max_length=500)
    contradicts: bool = False


class QaVerificationRequest(BaseModel):
    """세 QA 직원에게 동일하게 전달되는 입력. Artifact/EvidenceChunk는
    evidence_qa_engine.py의 기존 Pydantic 계약을 그대로 재사용한다."""

    model_config = ConfigDict(extra="forbid")

    verification_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    event_id: str | None = Field(default=None, max_length=100)
    timestamp: str | None = None
    artifact: Artifact
    evidence_chunks: tuple[EvidenceChunk, ...] = ()
    evidence_access: tuple[EvidenceAccessInput, ...] = ()
    decision_time: datetime
    incident: dict[str, Any] | None = None
    hallucination_query_vector: list[float] | None = Field(default=None, min_length=1)
    hallucination_evidence: list[ReferenceEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_hallucination_query(self) -> QaVerificationRequest:
        if self.hallucination_query_vector is not None and not all(
            isinstance(item, (int, float)) for item in self.hallucination_query_vector
        ):
            raise ValueError("hallucination_query_vector must contain numbers")
        return self


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    return value


def _input_hash(request: QaVerificationRequest) -> str:
    canonical = json.dumps(_jsonable(request), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_qa_runner(request: QaVerificationRequest) -> dict[str, Any]:
    """EvidenceQaEngine.check_artifact()를 그대로 감싼 결정론적 직원."""

    store = EvidenceStore(
        chunks={chunk.evidence_id: chunk for chunk in request.evidence_chunks},
        access={
            item.evidence_id: EvidenceAccess(granted=item.granted, reason=item.reason)
            for item in request.evidence_access
        },
    )
    ctx = QaContext(evidence_store=store, decision_time=request.decision_time)
    assessment = EvidenceQaEngine().check_artifact(request.artifact, ctx)
    unsupported = [
        check.claim_index
        for check in assessment.claim_checks
        if check.result in (ClaimCheckResult.UNSUPPORTED, ClaimCheckResult.CONTRADICTED)
    ]

    return {
        "worker_id": "qa-runner",
        "department": "QA",
        "event_id": request.event_id,
        "timestamp": request.timestamp,
        "role": "Deterministic Evidence QA Gate runner",
        "authoritative": True,
        "binding": False,
        "status": "COMPLETED",
        "decision": assessment.decision.value,
        "reason_codes": [code.value for code in assessment.reason_codes],
        "claim_checks": [_jsonable(check) for check in assessment.claim_checks],
        "findings": [_jsonable(finding) for finding in assessment.findings],
        "unsupported_claim_indexes": unsupported,
        "action_required": bool(unsupported) or assessment.decision.value != "PASS",
        "qa_decision_id": str(assessment.qa_decision_id),
        "calculation_version": assessment.calculation_version,
        "evidence_input_hash": assessment.input_hash,
        "input_hash": _input_hash(request),
        "summary": "Deterministic Evidence QA Gate completed; no finding was closed and no verdict was overridden.",
    }


class PineconeEvidenceClient:
    """QA 전용 최소 Pinecone data-plane 클라이언트. 환경변수로만 인증한다."""

    def __init__(self, *, api_key: str | None = None, index_host: str | None = None, timeout_seconds: float = 5.0) -> None:
        self.api_key = api_key or os.getenv("PINECONE_API_KEY", "").strip()
        self.index_host = (index_host or os.getenv("PINECONE_INDEX_HOST", "").strip()).rstrip("/")
        self.timeout_seconds = timeout_seconds

    def query(self, vector: list[float], *, namespace: str, top_k: int = 8) -> list[dict[str, Any]]:
        if not self.api_key or not self.index_host:
            raise RuntimeError("PINECONE_NOT_CONFIGURED")
        response = requests.post(
            f"{self.index_host}/query",
            headers={"Api-Key": self.api_key, "Content-Type": "application/json"},
            json={"vector": vector, "namespace": namespace, "topK": top_k, "includeMetadata": True},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        matches = response.json().get("matches", [])
        return [item for item in matches if isinstance(item, dict)]


def _has_unsupported_claims(qa_report: Mapping[str, Any]) -> bool:
    return bool(qa_report.get("unsupported_claim_indexes"))


def run_hallucination_critic_worker(
    request: QaVerificationRequest,
    qa_report: Mapping[str, Any],
    *,
    pinecone: PineconeEvidenceClient | None = None,
) -> dict[str, Any]:
    """qa-runner가 UNSUPPORTED/CONTRADICTED를 낸 Claim에 대해서만 실행되는
    advisory 직원. 이 직원은 qa-runner의 binding 판정을 절대 뒤집지 않는다 —
    "CONFIRMED"든 "ESCALATE"든 항상 사람 검토로 보낸다."""

    base = {
        "worker_id": "hallucination-critic-worker",
        "department": "QA",
        "event_id": request.event_id,
        "timestamp": request.timestamp,
        "role": "Unsupported/contradicted claim corroboration critic",
        "authoritative": False,
        "binding": False,
        "namespace": QA_NAMESPACE,
        "input_hash": _input_hash(request),
    }

    if not _has_unsupported_claims(qa_report):
        return {
            **base,
            "status": "NOT_EXECUTED",
            "verdict": "NOT_REQUIRED",
            "action_required": False,
            "evidence_refs": [],
            "evidence_source": None,
            "error": None,
            "summary": "No unsupported/contradicted claim exists; the critic did not run.",
        }

    evidence = [item.model_dump(mode="json") for item in request.hallucination_evidence]
    source = "request"
    error: str | None = None
    if not evidence and request.hallucination_query_vector is not None:
        try:
            matches = (pinecone or PineconeEvidenceClient()).query(
                request.hallucination_query_vector, namespace=QA_NAMESPACE
            )
            source = "pinecone"
            evidence = [
                {
                    "evidence_id": str(match.get("id", "pinecone-match")),
                    "title": str((match.get("metadata") or {}).get("title", "Pinecone reference evidence")),
                    "text": str((match.get("metadata") or {}).get("text", "")),
                    "source": "pinecone",
                    "score": match.get("score"),
                    "contradicts": bool((match.get("metadata") or {}).get("contradicts", False)),
                }
                for match in matches
            ]
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            error = type(exc).__name__

    contradicting = [item for item in evidence if item.get("contradicts") is True]
    if contradicting:
        verdict, status = "CONFIRMED", "COMPLETED"
    elif evidence:
        verdict, status = "NO_CORROBORATING_CONTRADICTION", "COMPLETED"
    else:
        # 근거가 전혀 없으면 qa-runner의 FAIL/CONDITIONAL을 조용히 지지하지
        # 않는다 — fail-closed로 사람 검토를 요구한다.
        verdict, status = "ESCALATE", "DEGRADED"

    return {
        **base,
        "status": status,
        "verdict": verdict,
        # qa-runner가 이미 문제를 찾았으므로, 이 직원이 실행된 이상 사람
        # 검토는 항상 필요하다 — 이 직원 스스로 그 필요를 해제하지 않는다.
        "action_required": True,
        "evidence_refs": [item.get("evidence_id") for item in evidence],
        "evidence_source": source,
        "error": error,
        "summary": "Reference evidence was evaluated as advisory context; it never clears a QA finding.",
    }


def run_incident_postmortem_worker(request: QaVerificationRequest) -> dict[str, Any]:
    """request.incident가 있을 때만 실행되는 advisory 직원. FACT/INFERENCE
    분류 자체는 하지 않는다 — Entry 형식이 올바른지만 결정론으로 확인한다."""

    base = {
        "worker_id": "incident-postmortem-worker",
        "department": "QA",
        "event_id": request.event_id,
        "timestamp": request.timestamp,
        "role": "Incident timeline entry-shape checker",
        "authoritative": False,
        "binding": False,
        "input_hash": _input_hash(request),
    }

    if not request.incident:
        return {
            **base,
            "status": "NOT_EXECUTED",
            "verdict": "NOT_REQUIRED",
            "action_required": False,
            "entries_recorded": 0,
            "unclassified_entries": 0,
            "summary": "No incident was supplied; the postmortem worker did not run.",
        }

    entries = request.incident.get("entries") or []
    unclassified = [
        entry
        for entry in entries
        if not isinstance(entry, dict) or str(entry.get("entry_type", "")).upper() not in _INCIDENT_ENTRY_TYPES
    ]
    needs_review = bool(unclassified) or not entries
    return {
        **base,
        "status": "COMPLETED",
        "verdict": "ESCALATE" if needs_review else "RECORDED",
        "action_required": needs_review,
        "entries_recorded": len(entries) - len(unclassified),
        "unclassified_entries": len(unclassified),
        "summary": "Incident timeline entries were checked for FACT/INFERENCE shape; no finding was closed.",
    }


def synthesize_qa_head(
    qa_report: Mapping[str, Any],
    hallucination_report: Mapping[str, Any],
    incident_report: Mapping[str, Any],
    request: QaVerificationRequest,
) -> dict[str, Any]:
    """Hermes QA Head를 위한 non-binding fan-in. qa-runner의 decision을
    절대 뒤집지 않는다 — advisory 직원은 escalate만 추가할 수 있다."""

    reports = [dict(qa_report), dict(hallucination_report), dict(incident_report)]
    high_or_actionable = [report for report in reports if report.get("action_required")]
    if qa_report.get("decision") == "FAIL":
        decision = "FAIL"
    elif high_or_actionable:
        decision = "ESCALATE"
    else:
        decision = qa_report.get("decision", "PASS")

    return {
        "decision": decision,
        "binding": False,
        "manual_review_required": decision != "PASS",
        "safe_action": "HOLD_FINDING_OPEN" if decision != "PASS" else "NO_ACTION",
        "reports": reports,
        "hermes_context": {
            "verification_id": request.verification_id,
            "employee_reports": reports,
            "instruction": (
                "Summarize evidence for the QA supervisor and human reviewer. Never close a "
                "finding, change a QA verdict, alter a Risk verdict, or approve/submit an order."
            ),
        },
    }


def build_qa_head_dispatch(request: QaVerificationRequest) -> dict[str, Any]:
    """QA Head가 소유하는 불변 fan-out envelope."""

    verification = request.model_dump(mode="json")
    digest = _input_hash(request)
    return {
        "dispatcher": "qa-head",
        "verification_id": request.verification_id,
        "input_hash": digest,
        "worker_inputs": {
            "qa-runner": {"verification": verification, "input_hash": digest},
            "hallucination-critic-worker": {"verification": verification, "input_hash": digest},
            "incident-postmortem-worker": {"verification": verification, "input_hash": digest},
        },
        "mutation_allowed": False,
    }


def assess_qa_verification(request: QaVerificationRequest | Mapping[str, Any]) -> dict[str, Any]:
    """세 QA 직원을 실행하고 각자의 독립 보고서와 fan-in을 반환한다."""

    normalized = (
        request if isinstance(request, QaVerificationRequest) else QaVerificationRequest.model_validate(request)
    )
    dispatch = build_qa_head_dispatch(normalized)
    qa_report = run_qa_runner(normalized)
    hallucination_report = run_hallucination_critic_worker(normalized, qa_report)
    incident_report = run_incident_postmortem_worker(normalized)

    non_degraded_statuses = {"COMPLETED", "NOT_EXECUTED"}
    pipeline_status = (
        "COMPLETED"
        if qa_report["status"] == "COMPLETED"
        and hallucination_report["status"] in non_degraded_statuses
        and incident_report["status"] in non_degraded_statuses
        else "DEGRADED"
    )

    return {
        "verification_id": normalized.verification_id,
        "pipeline_status": pipeline_status,
        "dispatch": dispatch,
        "employees": {
            "qa-runner": qa_report,
            "hallucination-critic-worker": hallucination_report,
            "incident-postmortem-worker": incident_report,
        },
        "qa_head": synthesize_qa_head(qa_report, hallucination_report, incident_report, normalized),
    }


__all__ = [
    "QA_NAMESPACE",
    "EvidenceAccessInput",
    "PineconeEvidenceClient",
    "QaVerificationRequest",
    "ReferenceEvidence",
    "assess_qa_verification",
    "build_qa_head_dispatch",
    "run_hallucination_critic_worker",
    "run_incident_postmortem_worker",
    "run_qa_runner",
    "synthesize_qa_head",
]


if __name__ == "__main__":
    from datetime import timezone
    from uuid import uuid4

    now = datetime.now(timezone.utc)
    fund_id, trace_id, artifact_id = uuid4(), uuid4(), uuid4()

    def _artifact(**claim_kwargs: Any) -> Artifact:
        from evidence_qa_engine import Claim

        claim = Claim(claim_index=0, **claim_kwargs)
        return Artifact(
            artifact_version_id=artifact_id,
            artifact_type="research_packet",
            producer="research-supervisor",
            fund_id=fund_id,
            trace_id=trace_id,
            claims=(claim,),
        )

    from evidence_qa_engine import ClaimKind

    # 1. 근거 있는 Fact -> qa-runner PASS, 두 advisory 직원 모두 NOT_REQUIRED/NOT_EXECUTED
    ev1 = EvidenceChunk(
        evidence_id=uuid4(),
        source="research-api",
        published_at=now,
        observed_at=now,
        excerpt="근거",
        numeric_value=Decimal(100),
        unit="KRW",
    )
    artifact_supported = _artifact(
        text="AAPL 종가는 100원", kind=ClaimKind.FACT, subject="AAPL",
        numeric_value=Decimal(100), unit="KRW", evidence_ids=(ev1.evidence_id,),
    )
    req1 = QaVerificationRequest(
        verification_id="ver-1",
        artifact=artifact_supported,
        evidence_chunks=(ev1,),
        decision_time=now,
    )
    result1 = assess_qa_verification(req1)
    assert result1["pipeline_status"] == "COMPLETED", result1
    assert result1["employees"]["qa-runner"]["decision"] == "PASS"
    assert result1["employees"]["hallucination-critic-worker"]["status"] == "NOT_EXECUTED"
    assert result1["employees"]["incident-postmortem-worker"]["status"] == "NOT_EXECUTED"
    assert result1["qa_head"]["decision"] == "PASS"
    assert result1["qa_head"]["binding"] is False
    assert result1["dispatch"]["mutation_allowed"] is False

    # 2. 근거 없는 Fact -> qa-runner FAIL, 근거 없는 hallucination critic ESCALATE (fail-closed) -> DEGRADED
    artifact_unsupported = _artifact(text="AAPL은 반등한다", kind=ClaimKind.FACT, subject="AAPL")
    req2 = QaVerificationRequest(verification_id="ver-2", artifact=artifact_unsupported, decision_time=now)
    result2 = assess_qa_verification(req2)
    assert result2["employees"]["qa-runner"]["decision"] == "FAIL"
    hallu2 = result2["employees"]["hallucination-critic-worker"]
    assert hallu2["status"] == "DEGRADED" and hallu2["verdict"] == "ESCALATE"
    assert hallu2["namespace"] == QA_NAMESPACE
    assert result2["pipeline_status"] == "DEGRADED"
    assert result2["qa_head"]["decision"] == "FAIL"
    assert result2["qa_head"]["safe_action"] == "HOLD_FINDING_OPEN"

    # 3. 공급된 참고자료가 상충을 확인해주면 CONFIRMED (그래도 binding은 여전히 False)
    req3 = QaVerificationRequest(
        verification_id="ver-3",
        artifact=artifact_unsupported,
        decision_time=now,
        hallucination_evidence=[
            ReferenceEvidence(evidence_id="ref-1", title="Incident precedent", text="...", contradicts=True)
        ],
    )
    result3 = assess_qa_verification(req3)
    hallu3 = result3["employees"]["hallucination-critic-worker"]
    assert hallu3["verdict"] == "CONFIRMED" and hallu3["status"] == "COMPLETED"
    assert hallu3["authoritative"] is False and hallu3["binding"] is False
    assert result3["qa_head"]["decision"] == "FAIL", "critic은 여전히 qa-runner의 FAIL을 못 바꾼다"

    # 4. incident가 있으면 postmortem worker가 실행되고, entry 형식이 틀리면 ESCALATE
    req4 = QaVerificationRequest(
        verification_id="ver-4",
        artifact=artifact_supported,
        evidence_chunks=(ev1,),
        decision_time=now,
        incident={"incident_id": str(uuid4()), "entries": [{"entry_type": "UNKNOWN", "summary": "x", "occurred_at": now.isoformat()}]},
    )
    result4 = assess_qa_verification(req4)
    postmortem4 = result4["employees"]["incident-postmortem-worker"]
    assert postmortem4["status"] == "COMPLETED" and postmortem4["verdict"] == "ESCALATE"
    assert postmortem4["unclassified_entries"] == 1
    # pipeline_status는 실행 성공 여부다 - postmortem worker는 성공적으로 실행돼
    # 문제를 찾아냈다. 그 escalation은 qa_head.decision으로 올라간다.
    assert result4["pipeline_status"] == "COMPLETED"
    assert result4["qa_head"]["decision"] == "ESCALATE"

    # 5. 같은 입력을 두 번 평가하면 세 직원 모두 같은 input_hash를 공유한다 (재현성)
    result1b = assess_qa_verification(req1)
    for worker_id in ("qa-runner", "hallucination-critic-worker", "incident-postmortem-worker"):
        assert result1["employees"][worker_id]["input_hash"] == result1b["employees"][worker_id]["input_hash"]
    assert result1["dispatch"]["input_hash"] == result1["employees"]["qa-runner"]["input_hash"]

    print("ok - QA mandate worker 5개 시나리오 점검 통과")
