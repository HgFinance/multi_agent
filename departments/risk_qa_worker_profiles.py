"""Risk·QA Worker 기술 스택과 성과 지표의 실행 계약.

이 모듈은 문서용 설명이 아니라 Worker Registry가 실제로 반환하는
관측 메타데이터의 단일 원천이다. 기술 스택은 권한을 확장하지 않으며,
모든 Worker는 read-only·non-binding context만 만든다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Final


@dataclass(frozen=True)
class WorkerTechProfile:
    """Worker가 사용할 기술과 그 사용 목적을 명시한다."""

    stack: tuple[str, ...]
    usage: tuple[str, ...]
    inputs: tuple[str, ...]
    metrics: tuple[str, ...]
    write_capability: str = "NONE"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


RISK_WORKER_TECH: Final[Mapping[str, WorkerTechProfile]] = {
    "compliance-policy-worker": WorkerTechProfile(
        stack=(
            "LangGraph guarded Worker graph",
            "Agentic RAG retrieve·grade·generate·hallucination_check",
            "PIT·ACL·citation·provenance validators",
            "Supabase pgvector/BM25 adapter when configured",
            "Worker Model Gateway (production vLLM Qwen2.5-14B-AWQ; dev Ollama fallback)",
        ),
        usage=(
            "정책 문서의 observed/published 시점을 as_of 이전으로 제한한다",
            "문서 ID·버전·인용을 검증한 뒤에만 요약한다",
            "근거 부족·문서 부재·충돌은 ambiguous/ESCALATE로 남긴다",
            "PIKE/LightRAG는 실제 recall·latency 기준 충족 전까지 후보로 둔다",
        ),
        inputs=(
            "risk.compliance.check",
            "research.documents",
            "document_versions",
            "evidence_chunks",
            "risk.policies",
        ),
        metrics=(
            "grounded_rate",
            "citation_coverage_rate",
            "pit_rejection_rate",
            "escalation_rate",
            "rag_latency_ms",
        ),
    ),
}


QA_WORKER_TECH: Final[Mapping[str, WorkerTechProfile]] = {
    "hallucination-critic-worker": WorkerTechProfile(
        stack=(
            "LangGraph conditional Worker graph",
            "Agentic RAG hybrid retrieval/rerank route",
            "deterministic contradiction·unsupported claim checks",
            "PIT·prompt-injection·provenance guards",
            "Ollama critique node",
        ),
        usage=(
            "UNSUPPORTED/CONTRADICTED claim이 있을 때만 실행한다",
            "원 에이전트가 실제로 조회한 evidence와 주장을 비교한다",
            "새 외부 API를 임의 호출하지 않고 기존 evidence를 우선한다",
            "불일치가 해소되지 않으면 QA를 PASS로 올리지 않고 ESCALATE한다",
        ),
        inputs=(
            "qa.evidence.rag",
            "assessment.claim_checks",
            "hallucination_reviews",
            "source_evidence",
        ),
        metrics=(
            "unsupported_claim_detection_rate",
            "contradiction_detection_rate",
            "false_clear_rate",
            "critique_latency_ms",
        ),
    ),
    "incident-postmortem-worker": WorkerTechProfile(
        stack=(
            "LangGraph conditional Worker graph",
            "IncidentTimeline FACT/INFERENCE model",
            "RunJournal replay/review",
            "append-only trace projection",
            "Pydantic incident contract",
            "Ollama factual narrative node",
        ),
        usage=(
            "incident event 순서를 FACT와 INFERENCE로 분리한다",
            "trace/input/output hash와 시간순서를 보존한다",
            "원인·영향·재발방지책을 증거가 있는 범위에서만 작성한다",
            "금융·규제 영향은 CEO/Risk/Compliance로 즉시 전달한다",
        ),
        inputs=(
            "qa.incident.record",
            "incident",
            "incident_events",
            "trace_manifest",
        ),
        metrics=(
            "timeline_completeness_rate",
            "fact_inference_separation_rate",
            "replay_completeness_rate",
            "incident_escalation_latency_ms",
        ),
    ),
}


def tech_profile_for(
    registry: Mapping[str, WorkerTechProfile], worker_id: str
) -> WorkerTechProfile:
    """Return a required profile and fail fast on registry drift."""

    try:
        return registry[worker_id]
    except KeyError as exc:
        raise KeyError(f"worker_tech_profile_missing:{worker_id}") from exc
