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
    "market-liquidity-worker": WorkerTechProfile(
        stack=(
            "LangGraph StateGraph",
            "Pydantic RiskSkillContext/RiskSkillResult",
            "Risk TradingState·P1 snapshot read adapter",
            "Redis hot-state read model",
            "Ollama OpenAI-compatible advisory node",
        ),
        usage=(
            "PIT·freshness guard를 먼저 통과시킨다",
            "TradingState·시장 스냅샷·노출을 결정론적으로 요약한다",
            "신규 진입 차단·HALTED 상태를 우선 판단한다",
            "LLM은 수치와 근거를 설명하는 non-binding 문장만 생성한다",
        ),
        inputs=(
            "risk.trading_state.read",
            "risk.p1.snapshot",
            "market_snapshot",
            "portfolio_state",
        ),
        metrics=(
            "snapshot_freshness_rate",
            "liquidity_gate_latency_ms",
            "missing_input_rate",
            "degraded_rate",
        ),
    ),
    "pre-trade-risk-worker": WorkerTechProfile(
        stack=(
            "LangGraph StateGraph",
            "Pydantic contract validation",
            "deterministic RiskEngine.check_order",
            "idempotency·fail-closed gate",
            "Ollama advisory explanation node",
        ),
        usage=(
            "OrderIntent·RiskContext·Policy version을 스키마 검증한다",
            "RiskEngine의 한도·현금·집중도·TradingState 결과만 사용한다",
            "RAG·외부 HTTP·재시도형 LLM은 hot path에서 금지한다",
            "실패는 RESIZE·REJECT·HOLD로만 전파한다",
        ),
        inputs=(
            "risk.case.check",
            "order_intent",
            "risk_context",
            "policy_version",
        ),
        metrics=(
            "risk_gate_latency_ms",
            "schema_reject_rate",
            "fail_closed_rate",
            "decision_replay_match_rate",
        ),
    ),
    "compliance-policy-worker": WorkerTechProfile(
        stack=(
            "LangGraph guarded Worker graph",
            "Agentic RAG retrieve·grade·generate·hallucination_check",
            "PIT·ACL·citation·provenance validators",
            "Supabase pgvector/BM25 adapter when configured",
            "Ollama grounded advisory node",
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
    "derivatives-counterparty-worker": WorkerTechProfile(
        stack=(
            "LangGraph StateGraph",
            "Pydantic exposure context",
            "TradingState record·counterparty read adapter",
            "deterministic exposure and state-uncertainty checks",
            "Ollama advisory explanation node",
        ),
        usage=(
            "파생상품·상대방·미결제 상태가 명시된 경우에만 활성화한다",
            "확정되지 않은 Broker/FCM 상태를 Filled·Cancelled로 추정하지 않는다",
            "상대방 상태 불명·정산 불일치는 Reconciliation/ESCALATE로 보낸다",
            "GraphRAG는 관계 데이터가 실제 적재된 뒤 별도 benchmark로 도입한다",
        ),
        inputs=(
            "risk.trading_state.record.read",
            "counterparty_state",
            "derivatives_snapshot",
            "settlement_status",
        ),
        metrics=(
            "counterparty_state_coverage",
            "unconfirmed_state_rate",
            "exposure_reconciliation_rate",
            "escalation_rate",
        ),
    ),
}


QA_WORKER_TECH: Final[Mapping[str, WorkerTechProfile]] = {
    "evidence-qa-worker": WorkerTechProfile(
        stack=(
            "LangGraph guarded Worker graph",
            "deterministic EvidenceQAEngine",
            "Pydantic claim/citation contract",
            "PIT·provenance·numeric temporal validators",
            "Ollama grounded explanation node",
        ),
        usage=(
            "모든 핵심 claim을 source/evidence와 1:1로 매핑한다",
            "PASS/WARN/FAIL은 EvidenceQAEngine이 결정하고 LLM은 설명만 한다",
            "미검증 인용·미래 데이터·수치 불일치는 WARN/ESCALATE로 남긴다",
            "QA는 주문·Risk 승인·Finding 종결을 수행하지 않는다",
        ),
        inputs=(
            "qa.evidence.check",
            "assessment.claim_checks",
            "research_packet",
            "evidence_refs",
        ),
        metrics=(
            "citation_coverage_rate",
            "claim_verification_rate",
            "unsupported_claim_rate",
            "qa_gate_latency_ms",
        ),
    ),
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
    "model-and-internal-audit-worker": WorkerTechProfile(
        stack=(
            "LangGraph conditional Worker graph",
            "ModelRiskEngine",
            "InternalAuditEngine",
            "Pydantic model/audit contract",
            "RunJournal replay metadata",
            "Ollama advisory summary node",
        ),
        usage=(
            "model·prompt·dataset version과 재현성 지표를 확인한다",
            "InternalAuditEngine의 SoD·권한·감사 결과를 독립적으로 해석한다",
            "PASS/WARN/FAIL/ESCALATE 결정은 결정론적 엔진을 따른다",
            "재현 실패·보호 실패율·드리프트는 CEO/Risk로 에스컬레이션한다",
        ),
        inputs=(
            "qa.model_risk.evaluate",
            "qa.internal_audit.evaluate",
            "model_risk_input",
            "internal_audit_events",
        ),
        metrics=(
            "replay_match_rate",
            "model_drift_rate",
            "audit_finding_coverage",
            "escalation_rate",
        ),
    ),
    "ops-and-permission-worker": WorkerTechProfile(
        stack=(
            "LangGraph conditional Worker graph",
            "OpsHealthMonitor",
            "ToolPermissionCheck",
            "Pydantic permission/audit contract",
            "trace and cost-latency projection",
            "Ollama operational summary node",
        ),
        usage=(
            "agent latency·error·cost와 queue/model health를 결정론적으로 집계한다",
            "allowlist·scope·department·SoD 위반을 탐지한다",
            "권한을 스스로 확장하거나 Secret 값을 출력하지 않는다",
            "위반은 즉시 격리·ESCALATE하고 정상으로 완화하지 않는다",
        ),
        inputs=(
            "qa.ops.evaluate",
            "qa.tool_permission.check",
            "ops_assessment",
            "permission_check",
        ),
        metrics=(
            "permission_violation_rate",
            "agent_error_rate",
            "p95_latency_ms",
            "trace_completeness_rate",
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

