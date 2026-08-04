"""Canonical ResearchPacketV2 adapter for the Risk/QA test boundary.

The Risk and QA workers need domain-specific read models, but the packet crossing
the Research -> Risk/QA boundary must remain the canonical ResearchPacketV2.
This module keeps those concerns separate: the envelope carries only trace and
derived read-only inputs while ``research_packet`` is the authoritative contract.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


ROOT = Path(__file__).resolve().parents[2]


def _load_research_contracts() -> Any:
    module_name = "research_contracts_v2_testkit"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = ROOT / "departments/01-research/contracts/research_v2.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"ResearchPacketV2 contract unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_contracts = _load_research_contracts()
ResearchPacketV2 = _contracts.ResearchPacketV2
AnalystFindingV1 = _contracts.AnalystFindingV1
Claim = _contracts.Claim
Outlook = _contracts.Outlook
Calibration = _contracts.Calibration
Lineage = _contracts.Lineage


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("as_known_at must include timezone")
    return value.astimezone(timezone.utc)


def _canonical_hash(packet: Any, artifact_id: str, trace_id: str) -> str:
    return _hash(
        {
            "research_packet": packet.model_dump(mode="json"),
            "artifact_id": artifact_id,
            "trace_id": trace_id,
        }
    )


class RiskQaPacket(BaseModel):
    """Risk/QA envelope whose authoritative payload is ``ResearchPacketV2``."""

    model_config = ConfigDict(extra="forbid")

    research_packet: ResearchPacketV2
    artifact_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    input_hash: str = Field(min_length=16)
    risk_input: dict[str, Any]
    qa_input: dict[str, Any]

    @model_validator(mode="after")
    def _hash_matches_canonical_packet(self) -> "RiskQaPacket":
        expected = _canonical_hash(
            self.research_packet,
            self.artifact_id,
            self.trace_id,
        )
        if self.input_hash != expected:
            raise ValueError("RiskQaPacket input_hash does not match ResearchPacketV2")
        if self.research_packet.as_known_at.tzinfo is None:
            raise ValueError("ResearchPacketV2 as_known_at must include timezone")
        return self

    @property
    def packet_id(self) -> str:
        return self.research_packet.packet_id

    @property
    def case_id(self) -> str:
        return self.research_packet.case_id

    @property
    def as_known_at(self) -> datetime:
        return self.research_packet.as_known_at

    @property
    def source_refs(self) -> list[str]:
        refs: list[str] = []
        for finding in self.research_packet.findings:
            for claim in finding.claims:
                refs.extend(str(ref) for ref in claim.evidence_ids)
        return list(dict.fromkeys(refs))

    @property
    def claims(self) -> list[dict[str, Any]]:
        """Return the QA claim-check view, never the canonical packet itself."""
        return list(self.qa_input.get("assessment", {}).get("claim_checks", []))


def build_domain_inputs(
    packet: Any,
    *,
    artifact_id: str,
    trace_id: str,
    claim_checks: list[dict[str, Any]] | None = None,
) -> RiskQaPacket:
    """Build deterministic Risk/QA read models from a canonical packet."""

    known_at = _aware(packet.as_known_at)
    source_refs = []
    for finding in packet.findings:
        for claim in finding.claims:
            source_refs.extend(str(ref) for ref in claim.evidence_ids)
    source_refs = list(dict.fromkeys(source_refs))
    if not source_refs:
        source_refs = [f"research:packet:{packet.packet_id}"]

    checks = claim_checks or []
    packet_hash = _canonical_hash(packet, artifact_id, trace_id)
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
    risk_input = {
        "trace_id": trace_id,
        "case_id": packet.case_id,
        "as_of": known_at.isoformat(),
        "allowed_scopes": scopes,
        "query": packet.thesis,
        "trading_state": "ENABLED",
        "assessment": {"verdict": "approve", "source": "test-risk-engine"},
        "compliance": {
            "grounded": True,
            "observed_at": known_at.isoformat(),
            "evidence_refs": source_refs[:1],
        },
        "counterparty": {"status": "HEALTHY", "observed_at": known_at.isoformat()},
        "derivatives": {
            "greeks": {"delta": "0.10"},
            "observed_at": known_at.isoformat(),
        },
    }
    qa_input = {
        "trace_id": trace_id,
        "case_id": packet.case_id,
        "as_of": known_at.isoformat(),
        "allowed_scopes": scopes,
        "artifact": {
            "artifact_id": artifact_id,
            "packet_id": packet.packet_id,
            "source_refs": source_refs,
        },
        "evidence_store": {
            "source_refs": source_refs,
            "as_known_at": known_at.isoformat(),
        },
        "assessment": {"claim_checks": checks},
        "hallucination_reviews": checks,
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
    risk_input["input_hash"] = packet_hash
    qa_input["input_hash"] = packet_hash
    return RiskQaPacket(
        research_packet=packet,
        artifact_id=artifact_id,
        trace_id=trace_id,
        input_hash=packet_hash,
        risk_input=risk_input,
        qa_input=qa_input,
    )


def make_canonical_test_packet(*, as_known_at: datetime | None = None) -> RiskQaPacket:
    """Create a valid ResearchPacketV2 fixture with an intentional QA warning."""

    known_at = _aware(as_known_at or datetime(2026, 8, 4, tzinfo=timezone.utc))
    packet_id = "research-packet-v2-test-001"
    case_id = "case-test-001"
    evidence_id = "research:evidence:test-citation-001"
    claims = (
        Claim(
            claim_id="claim-test-supported",
            statement="TEST fixture market evidence is available",
            claim_type="fact",
            evidence_ids=(evidence_id,),
            direction="supportive",
            confidence=0.75,
        ),
        Claim(
            claim_id="claim-test-unsupported",
            statement="TEST fixture intentionally exercises unsupported-claim routing",
            claim_type="inference",
            evidence_ids=(),
            direction="neutral",
            confidence=0.50,
        ),
    )
    finding = AnalystFindingV1(
        finding_id="finding-test-001",
        case_id=case_id,
        perspective="fundamental",
        as_known_at=known_at,
        horizon="20d",
        claims=claims,
        contradictions=(),
        unanswered_questions=("Validate unsupported inference in QA.",),
        status="COMPLETE",
        model_version="test-research-model-v1",
        prompt_version="test-research-prompt-v1",
        tool_versions=("research-fixture-v1",),
    )
    packet = ResearchPacketV2(
        packet_id=packet_id,
        case_id=case_id,
        instrument_id="TEST.SYMBOL",
        trigger="manual",
        as_known_at=known_at,
        horizons=("1d", "20d"),
        evidence_manifest_id="evidence-manifest-test-001",
        claim_graph_id="claim-graph-test-001",
        macro_outlook=Outlook(
            direction="neutral",
            confidence=0.50,
            claim_ids=("claim-test-supported",),
        ),
        micro_outlook=Outlook(
            direction="neutral",
            confidence=0.50,
            claim_ids=("claim-test-supported",),
        ),
        thesis="TEST compliance policy PIT citation and counterparty evidence",
        catalysts=("TEST evidence available",),
        invalidation=("unsupported claim remains unresolved",),
        dissent=("QA must review unsupported inference",),
        evidence_gaps=("claim-test-unsupported",),
        calibration=Calibration(cohort="test-cohort-v1"),
        uncalibrated=True,
        status="PARTIAL",
        lineage=Lineage(
            graph_version="research-test-graph-v1",
            model_versions={"fundamental": "test-research-model-v1"},
            prompt_versions={"fundamental": "test-research-prompt-v1"},
        ),
        findings=(finding,),
    )
    claim_checks = [
        {
            "claim_id": "claim-test-supported",
            "text": "TEST fixture market evidence is available",
            "result": "SUPPORTED",
            "evidence_refs": [evidence_id],
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
    return build_domain_inputs(
        packet,
        artifact_id="artifact-test-001",
        trace_id="trace-test-risk-qa-001",
        claim_checks=claim_checks,
    )


def packet_from_api_payload(payload: Mapping[str, Any]) -> RiskQaPacket:
    """Validate an API response containing a canonical packet before handoff."""

    raw = payload.get("research_packet") or payload.get("packet") or payload
    packet = ResearchPacketV2.model_validate(raw)
    artifact_id = str(payload.get("artifact_id") or f"artifact:{packet.packet_id}")
    trace_id = str(payload.get("trace_id") or f"trace:{packet.packet_id}")
    claim_checks = list(payload.get("claim_checks") or [])
    return build_domain_inputs(
        packet,
        artifact_id=artifact_id,
        trace_id=trace_id,
        claim_checks=claim_checks,
    )
