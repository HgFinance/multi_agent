"""TEST/PRODUCTION profile acceptance for Risk and AI-QA workers."""

from __future__ import annotations

import pytest

from departments.risk_qa_testkit import (
    ResearchPacketV2,
    RiskQaPacket,
    make_test_packet,
    run_risk_qa_pipeline,
)


def test_test_profile_runs_full_risk_and_qa_skeleton():
    result = run_risk_qa_pipeline("test")

    assert result["mode"] == "test"
    assert result["production_enabled"] is False
    assert result["worker_runtime"] == "deterministic"
    assert result["pipeline_status"] == "COMPLETED"
    assert result["safe_action"] == "NO_ACTION"
    assert result["manual_review_required"] is True  # intentional QA WARN fixture
    assert [stage["stage"] for stage in result["stages"]] == [
        "research_packet_fixture",
        "risk_deterministic_gate_skeleton",
        "risk_department_head_employee_graph",
        "risk_to_qa_department_handoff",
        "qa_deterministic_gate_skeleton",
        "qa_department_head_employee_graph",
        "risk_qa_test_gate",
        "cross_domain_replay_contract",
    ]
    assert result["risk_gate"]["binding"] is False
    assert result["qa_gate"]["binding"] is False
    assert result["qa_gate"]["decision"] == "WARN"
    assert result["replay_contract"]["status"] == "READY"
    assert result["replay_contract"]["replayable"] is True
    assert len(result["risk"]["workers"]) == 3
    assert len(result["qa"]["workers"]) == 5
    assert result["risk"]["not_executed"] == []
    assert result["qa"]["not_executed"] == []
    assert result["risk"]["head"]["head_id"] == "risk-supervisor"
    assert result["risk"]["head"]["binding"] is False
    assert result["qa"]["head"]["head_id"] == "qa-audit-supervisor"
    assert result["qa"]["head"]["binding"] is False
    assert result["qa"]["head"]["escalate"] is True  # intentional fixture WARN
    assert [handoff["type"] for handoff in result["risk"]["handoffs"]] == [
        "HEAD_DELEGATION",
        "PEER_CONTEXT",
        "PEER_CONTEXT",
    ]
    assert [handoff["type"] for handoff in result["qa"]["handoffs"]] == [
        "HEAD_DELEGATION",
        "PEER_CONTEXT",
        "PEER_CONTEXT",
        "PEER_CONTEXT",
        "PEER_CONTEXT",
    ]
    assert result["qa"]["received_department_handoff"]["from"] == "risk-supervisor"
    assert result["qa"]["received_department_handoff"]["to"] == "qa-audit-supervisor"
    assert all(
        worker["input_hash"] == result["packet"]["input_hash"]
        for worker in result["risk"]["workers"] + result["qa"]["workers"]
    )
    assert all(worker["trace"]["events"] for worker in result["risk"]["workers"])
    assert all(worker["trace"]["events"] for worker in result["qa"]["workers"])


def test_research_packet_handoff_fields_are_preserved_and_pit_bound():
    packet = make_test_packet()
    assert isinstance(packet.research_packet, ResearchPacketV2)
    assert packet.research_packet.status == "PARTIAL"
    assert packet.packet_id
    assert packet.artifact_id
    assert packet.case_id
    assert packet.trace_id
    assert packet.as_known_at.tzinfo is not None
    assert packet.input_hash
    assert packet.source_refs
    assert all(
        claim["observed_at"] <= packet.as_known_at.isoformat()
        for claim in packet.claims
    )
    with pytest.raises(ValueError):
        RiskQaPacket(**{**packet.model_dump(), "input_hash": "0" * 64})

    invalid = packet.research_packet.model_dump(mode="python")
    invalid["as_known_at"] = packet.as_known_at.replace(tzinfo=None)
    with pytest.raises(ValueError):
        ResearchPacketV2.model_validate(invalid)


def test_production_profile_is_off_and_cannot_look_like_success():
    result = run_risk_qa_pipeline("production")

    assert result == {
        "mode": "production",
        "production_enabled": False,
        "pipeline_status": "OFF",
        "safe_action": "HOLD",
        "reason": "PRODUCTION_DISABLED_UNTIL_REAL_ADAPTER_ACCEPTANCE",
        "stages": [],
    }


def test_department_head_failure_is_degraded_and_fail_closed():
    def broken_head(system: str, prompt: str) -> str:
        raise RuntimeError("head runtime unavailable")

    result = run_risk_qa_pipeline("test", head_llm=broken_head)

    assert result["pipeline_status"] == "DEGRADED"
    assert result["safe_action"] == "HOLD"
    assert result["risk"]["head"]["schema_valid"] is False
    assert result["risk"]["head"]["binding"] is False
    assert result["qa"]["head"]["schema_valid"] is False
    assert result["qa"]["head"]["binding"] is False


def test_worker_runtime_ollama_mode_uses_worker_injection_boundary():
    calls: list[str] = []

    def worker_smoke(system: str, prompt: str) -> str:
        calls.append(prompt.split("\n", 1)[0])
        return '{"summary":"ollama boundary smoke","confidence":0.5,"evidence_refs":[],"escalate":false}'

    result = run_risk_qa_pipeline(
        "test",
        worker_runtime="ollama",
        worker_llm=worker_smoke,
    )

    assert result["worker_runtime"] == "ollama"
    assert len(calls) == 8
    assert result["risk"]["runtime"]["worker_provider"] == "ollama"
    assert result["qa"]["runtime"]["worker_provider"] == "ollama"


def test_worker_rag_policy_is_fail_closed_per_role():
    result = run_risk_qa_pipeline("test")
    routes = {
        worker["worker_id"]: worker["rag_plan"]["route"]
        for domain in ("risk", "qa")
        for worker in result[domain]["workers"]
    }

    assert routes["core-risk-worker"] == "NO_RAG"
    assert routes["derivatives-counterparty-worker"] == "NO_RAG"
    assert routes["ops-and-permission-worker"] == "NO_RAG"
    assert routes["compliance-policy-worker"] in {"HYBRID", "GRAPH"}
    assert routes["incident-postmortem-worker"] in {"HYBRID", "GRAPH", "HYPERGRAPH"}
