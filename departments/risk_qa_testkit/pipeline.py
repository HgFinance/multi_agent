"""TEST/PRODUCTION boundary and full synthetic Risk -> QA pipeline.

TEST mode executes every Risk and AI-QA Worker against a deterministic
ResearchPacket fixture. It never reads production credentials, live APIs,
Redis, Supabase, TimescaleDB, or the placeholder corpus.

PRODUCTION mode is intentionally OFF until real-data adapters, credentials,
and their runtime acceptance gates are approved.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from departments.risk_qa_testkit.department_graph import (
    DepartmentGraphSpec,
    run_department_graph,
)
from departments.risk_qa_testkit.research_packet import (
    ResearchPacketV2,
    RiskQaPacket,
    make_canonical_test_packet,
    packet_from_api_payload,
)
from departments.risk_qa_testkit.replay import (
    ReplayValidationError,
    build_test_replay_bundle,
    validate_replay_bundle,
)


ROOT = Path(__file__).resolve().parents[2]


class PipelineMode(str, Enum):
    TEST = "test"
    PRODUCTION = "production"


class WorkerRuntime(str, Enum):
    DETERMINISTIC = "deterministic"
    OLLAMA = "ollama"


class ProductionDisabled(RuntimeError):
    """Raised only by callers that explicitly require a live production run."""


def _hash_payload(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _LegacyResearchPacket(BaseModel):
    """Minimal cross-team handoff contract from Research to Risk and QA."""

    model_config = ConfigDict(extra="forbid")

    packet_id: str
    artifact_id: str
    case_id: str
    trace_id: str
    as_known_at: datetime
    input_hash: str = Field(min_length=16)
    source_refs: list[str] = Field(min_length=1)
    claims: list[dict[str, Any]] = Field(min_length=1)
    risk_input: dict[str, Any]
    qa_input: dict[str, Any]

    @field_validator("as_known_at")
    @classmethod
    def as_known_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("as_known_at must include timezone")
        return value

    @model_validator(mode="after")
    def claims_must_be_point_in_time(self) -> "ResearchPacket":
        for claim in self.claims:
            observed_at = claim.get("observed_at")
            if observed_at is None:
                raise ValueError("every claim requires observed_at")
            try:
                observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("claim observed_at must be ISO datetime") from exc
            if observed.tzinfo is None:
                raise ValueError("claim observed_at must include timezone")
            if observed > self.as_known_at:
                raise ValueError("future claim is not allowed by ResearchPacket PIT")
        return self


# Compatibility name for callers; the authoritative payload is now V2.
ResearchPacket = RiskQaPacket


def _load_worker_module(path: Path, module_name: str) -> Any:
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"worker module unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _worker_modules() -> tuple[Any, Any]:
    risk = _load_worker_module(
        ROOT / "departments/03-risk/risk_employee_workers.py",
        "risk_employee_workers_testkit",
    )
    qa = _load_worker_module(
        ROOT / "departments/06-ai-qa-audit/qa_employee_workers.py",
        "qa_employee_workers_testkit",
    )
    return risk, qa


def _skeleton_llm(system: str, prompt: str) -> str:
    """Deterministic Qwen-shaped response used only in TEST mode."""

    match = re.search(r"Worker:\s*([^\n]+)", prompt)
    worker_id = match.group(1).strip() if match else "test-worker"
    department = "qa" if "AI-QA" in system or "QA" in system else "risk"
    return json.dumps(
        {
            "summary": f"TEST skeleton {department} evidence summary for {worker_id}",
            "confidence": 0.75,
            "evidence_refs": ["research:evidence:test-citation-001"],
            "escalate": False,
        }
    )


def _test_department_head_llm(system: str, prompt: str) -> str:
    """Hermes-shaped deterministic Head adapter used only by TEST E2E."""

    context = json.loads(prompt)
    is_risk = "risk-supervisor" in system
    gate = context.get("deterministic_gate", {})
    workers = context.get("worker_reports", [])
    worker_failed = any(
        report.get("status") != "COMPLETED" or report.get("escalate")
        for report in workers
    )
    if is_risk:
        escalate = worker_failed or gate.get("status") != "COMPLETED"
        return json.dumps(
            {
                "summary": "TEST Hermes head synthesized Risk employee context.",
                "recommendation": "REVIEW_RISK_PROPOSAL",
                "safe_action": "HOLD" if escalate else "NO_ACTION",
                "escalate": escalate,
            }
        )
    escalate = worker_failed or gate.get("decision") != "PASS"
    return json.dumps(
        {
            "summary": "TEST Hermes head synthesized independent QA employee context.",
            "recommendation": "ESCALATE_QA_REVIEW" if escalate else "PROPOSE_QA_PASS",
            "safe_action": "ESCALATE" if escalate else "NO_ACTION",
            "escalate": escalate,
        }
    )


def _legacy_make_test_packet(*, as_known_at: datetime | None = None) -> ResearchPacket:
    """Build a complete handoff fixture and activate every conditional Worker."""

    known_at = as_known_at or datetime(2026, 8, 4, tzinfo=timezone.utc)
    if known_at.tzinfo is None:
        raise ValueError("test packet as_known_at must include timezone")
    packet_id = "research-packet-test-001"
    artifact_id = "artifact-test-001"
    case_id = "case-test-001"
    trace_id = "trace-test-risk-qa-001"
    source_refs = [
        "research:evidence:test-citation-001",
        "research:market_snapshot:test-snapshot-001",
    ]
    scopes = [
        "risk.trading_state.read",
        "risk.p1.snapshot",
        "risk.case.check",
        "risk.compliance.check",
        "risk.trading_state.record.read",
        "qa.evidence.check",
        "qa.evidence.rag",
        "qa.model_risk.evaluate",
        "qa.internal_audit.evaluate",
        "qa.ops.evaluate",
        "qa.tool_permission.check",
        "qa.incident.record",
    ]
    claims = [
        {
            "claim_id": "claim-test-supported",
            "text": "TEST fixture market evidence is available",
            "result": "SUPPORTED",
            "evidence_refs": [source_refs[0]],
            "observed_at": known_at.isoformat(),
        },
        {
            "claim_id": "claim-test-unsupported",
            "text": "TEST fixture intentionally exercises unsupported-claim routing",
            "result": "UNSUPPORTED",
            "evidence_refs": [],
            "observed_at": known_at.isoformat(),
        },
    ]
    risk_input = {
        "trace_id": trace_id,
        "case_id": case_id,
        "as_of": known_at.isoformat(),
        "allowed_scopes": scopes,
        "query": "test compliance policy PIT citation and counterparty evidence",
        "trading_state": "ENABLED",
        "assessment": {"verdict": "approve", "source": "test-risk-engine"},
        "compliance": {
            "grounded": True,
            "observed_at": known_at.isoformat(),
            "evidence_refs": [source_refs[0]],
        },
        "counterparty": {"status": "HEALTHY", "observed_at": known_at.isoformat()},
        "derivatives": {"greeks": {"delta": "0.10"}, "observed_at": known_at.isoformat()},
    }
    qa_input = {
        "trace_id": trace_id,
        "case_id": case_id,
        "decision_time": known_at.isoformat(),
        "allowed_scopes": scopes,
        "query": "test citation contradiction and entity relationship review",
        "artifact": {
            "artifact_id": artifact_id,
            "packet_id": packet_id,
            "source_refs": source_refs,
        },
        "evidence_store": {"source_refs": source_refs, "as_known_at": known_at.isoformat()},
        "assessment": {"claim_checks": claims},
        "hallucination_reviews": claims,
        "model_risk": {"status": "TESTING", "model_id": "qwen3:1.7b"},
        "internal_audit": {"status": "TESTING", "events": []},
        "ops_assessment": {"status": "HEALTHY", "breaches": []},
        "permission_check": {"result": "ALLOWED", "reason": "test allowlist"},
        "incident": {
            "incident_id": "incident-test-001",
            "severity": "SEV3",
            "observed_at": known_at.isoformat(),
        },
        "incident_events": [],
    }
    core = {
        "packet_id": packet_id,
        "artifact_id": artifact_id,
        "case_id": case_id,
        "trace_id": trace_id,
        "as_known_at": known_at.isoformat(),
        "source_refs": source_refs,
        "claims": claims,
    }
    packet_hash = _hash_payload(core)
    risk_input["input_hash"] = packet_hash
    qa_input["input_hash"] = packet_hash
    return ResearchPacket(
        **core,
        input_hash=packet_hash,
        risk_input=risk_input,
        qa_input=qa_input,
    )


def make_test_packet(*, as_known_at: datetime | None = None) -> RiskQaPacket:
    """Return the canonical ResearchPacketV2-backed Risk/QA fixture."""

    return make_canonical_test_packet(as_known_at=as_known_at)


def _worker_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "DEGRADED" if report.get("degraded") else "COMPLETED",
        "executed": report.get("executed", []),
        "failed": report.get("failed", []),
        "not_executed": report.get("not_executed", []),
        "degraded": report.get("degraded"),
        "worker_count": len(report.get("workers", [])),
        "skill_result_count": sum(
            len(worker.get("skill_results", []))
            for worker in report.get("workers", [])
        ),
    }


def _risk_deterministic_gate_skeleton(packet: ResearchPacket) -> dict[str, Any]:
    """Represent the production RiskEngine owner without binding execution."""

    trading_state = packet.risk_input.get("trading_state")
    assessment = packet.risk_input.get("assessment") or {}
    if trading_state != "ENABLED" or assessment.get("verdict") not in {
        "approve",
        "resize",
        "reject",
    }:
        return {
            "status": "DEGRADED",
            "binding": False,
            "safe_action": "HOLD",
            "reason": "INVALID_TEST_RISK_GATE_INPUT",
        }
    return {
        "status": "COMPLETED",
        "binding": False,
        "verdict": assessment["verdict"],
        "calculation_version": "test-risk-engine-skeleton-v1",
        "input_hash": packet.input_hash,
    }


def _qa_deterministic_gate_skeleton(packet: ResearchPacket) -> dict[str, Any]:
    """Represent EvidenceQaEngine ownership without declaring an operational PASS."""

    claims = packet.qa_input.get("assessment", {}).get("claim_checks", [])
    unsupported = sum(
        claim.get("result") in {"UNSUPPORTED", "CONTRADICTED"}
        for claim in claims
    )
    return {
        "status": "COMPLETED",
        "binding": False,
        "decision": "WARN" if unsupported else "PASS",
        "unsupported_claim_count": unsupported,
        "calculation_version": "test-evidence-qa-engine-skeleton-v1",
        "input_hash": packet.input_hash,
    }


def run_risk_qa_pipeline(
    mode: PipelineMode | str = PipelineMode.TEST,
    *,
    packet: ResearchPacket | None = None,
    worker_llm: Any | None = None,
    head_llm: Any | None = None,
    worker_runtime: WorkerRuntime | str = WorkerRuntime.DETERMINISTIC,
) -> dict[str, Any]:
    """Run TEST end-to-end or return an explicit production OFF result."""

    selected = PipelineMode(mode)
    if selected is PipelineMode.PRODUCTION:
        return {
            "mode": selected.value,
            "production_enabled": False,
            "pipeline_status": "OFF",
            "safe_action": "HOLD",
            "reason": "PRODUCTION_DISABLED_UNTIL_REAL_ADAPTER_ACCEPTANCE",
            "stages": [],
        }

    packet = packet or make_test_packet()
    runtime = WorkerRuntime(worker_runtime)
    risk_gate = _risk_deterministic_gate_skeleton(packet)
    qa_gate = _qa_deterministic_gate_skeleton(packet)
    risk_module, qa_module = _worker_modules()
    risk_payload = {
        **packet.risk_input,
        "packet_id": packet.packet_id,
        "artifact_id": packet.artifact_id,
        "deterministic_gate": risk_gate,
    }
    risk_spec = DepartmentGraphSpec(
        department="risk-management",
        profile="risk-management",
        head_id="risk-supervisor",
        output_contract="risk.department-head-context.v1",
        worker_module=risk_module,
        worker_tools={
            "market-liquidity-worker": risk_module._market_tool,
            "pre-trade-risk-worker": risk_module._pre_trade_tool,
            "compliance-policy-worker": risk_module._compliance_tool,
            "derivatives-counterparty-worker": risk_module._counterparty_tool,
        },
    )
    if worker_llm is None and runtime is WorkerRuntime.DETERMINISTIC:
        worker_llm = _skeleton_llm
    head_llm = head_llm or _test_department_head_llm
    risk_report = run_department_graph(
        risk_spec,
        risk_payload,
        worker_llm=worker_llm,
        head_llm=head_llm,
        worker_runtime=runtime.value,
    )

    department_handoff = {
        "from": risk_report["head"]["head_id"],
        "to": "qa-audit-supervisor",
        "type": "DEPARTMENT_HANDOFF",
        "purpose": "independent QA review of Risk employee context",
        "trace_id": packet.trace_id,
        "input_hash": packet.input_hash,
        "artifact_id": packet.artifact_id,
        "binding": False,
    }
    qa_payload = {
        **packet.qa_input,
        "packet_id": packet.packet_id,
        "artifact_id": packet.artifact_id,
        "deterministic_gate": qa_gate,
        "risk_department_report": {
            "head": risk_report["head"],
            "workers": [
                {
                    "worker_id": worker["worker_id"],
                    "status": worker["status"],
                    "output": worker.get("output", {}),
                }
                for worker in risk_report["workers"]
            ],
        },
    }
    qa_spec = DepartmentGraphSpec(
        department="qa-department",
        profile="qa-department",
        head_id="qa-audit-supervisor",
        output_contract="qa.department-head-context.v1",
        worker_module=qa_module,
        worker_tools={
            "evidence-qa-worker": qa_module._evidence_tool,
            "hallucination-critic-worker": qa_module._hallucination_tool,
            "model-and-internal-audit-worker": qa_module._audit_tool,
            "ops-and-permission-worker": qa_module._ops_tool,
            "incident-postmortem-worker": qa_module._incident_tool,
        },
    )
    qa_report = run_department_graph(
        qa_spec,
        qa_payload,
        worker_llm=worker_llm,
        head_llm=head_llm,
        worker_runtime=runtime.value,
    )
    qa_report["received_department_handoff"] = department_handoff
    risk_ok = (
        risk_report.get("degraded") is False
        and risk_report.get("head", {}).get("binding") is False
        and len(risk_report.get("workers", [])) == 4
        and not risk_report.get("not_executed")
        and len(risk_report.get("handoffs", [])) == 4
    )
    qa_ok = (
        qa_report.get("degraded") is False
        and qa_report.get("head", {}).get("binding") is False
        and len(qa_report.get("workers", [])) == 5
        and not qa_report.get("not_executed")
        and len(qa_report.get("handoffs", [])) == 5
        and qa_report.get("received_department_handoff", {}).get("from") == "risk-supervisor"
    )
    # RiskQaPacket validates this against the canonical ResearchPacketV2 hash.
    packet_ok = isinstance(packet.research_packet, ResearchPacketV2)
    completed = packet_ok and risk_ok and qa_ok
    skeleton_ok = (
        risk_gate["status"] == "COMPLETED"
        and qa_gate["status"] == "COMPLETED"
    )
    completed = completed and skeleton_ok
    try:
        replay_bundle = build_test_replay_bundle(
            packet,
            risk_verdict=str(risk_gate.get("verdict", "HOLD")),
            qa_decision=str(qa_gate.get("decision", "WARN")),
        )
        replay_contract = validate_replay_bundle(replay_bundle)
    except ReplayValidationError as exc:
        replay_contract = {
            "status": "DEGRADED",
            "replayable": False,
            "error_code": "RISK_QA_REPLAY_CONTRACT_INVALID",
            "error": str(exc),
        }
    completed = completed and replay_contract.get("replayable") is True
    manual_review_required = bool(
        qa_gate.get("decision") != "PASS"
        or qa_report.get("head", {}).get("escalate")
    )
    return {
        "mode": selected.value,
        "production_enabled": False,
        "worker_runtime": runtime.value,
        "pipeline_status": "COMPLETED" if completed else "DEGRADED",
        "safe_action": "NO_ACTION" if completed else "HOLD",
        "manual_review_required": manual_review_required,
        "packet": {
            "packet_id": packet.packet_id,
            "artifact_id": packet.artifact_id,
            "case_id": packet.case_id,
            "trace_id": packet.trace_id,
            "as_known_at": packet.as_known_at.isoformat(),
            "input_hash": packet.input_hash,
            "source_refs": packet.source_refs,
            "claim_count": len(packet.claims),
        },
        "risk_gate": risk_gate,
        "qa_gate": qa_gate,
        "replay_contract": replay_contract,
        "stages": [
            {"stage": "research_packet_fixture", "status": "COMPLETED" if packet_ok else "DEGRADED"},
            {"stage": "risk_deterministic_gate_skeleton", "status": risk_gate["status"], "binding": False},
        {"stage": "risk_department_head_employee_graph", "status": "COMPLETED" if risk_ok else "DEGRADED", "summary": _worker_summary(risk_report), "head_id": "risk-supervisor", "handoff_count": len(risk_report.get("handoffs", []))},
        {"stage": "risk_to_qa_department_handoff", "status": "COMPLETED", "binding": False},
        {"stage": "qa_deterministic_gate_skeleton", "status": qa_gate["status"], "binding": False},
        {"stage": "qa_department_head_employee_graph", "status": "COMPLETED" if qa_ok else "DEGRADED", "summary": _worker_summary(qa_report), "head_id": "qa-audit-supervisor", "handoff_count": len(qa_report.get("handoffs", []))},
            {"stage": "risk_qa_test_gate", "status": "COMPLETED" if completed else "DEGRADED"},
            {"stage": "cross_domain_replay_contract", "status": replay_contract["status"]},
        ],
        "risk": risk_report,
        "qa": qa_report,
    }
