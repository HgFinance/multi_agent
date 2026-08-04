"""TEST/PRODUCTION profile acceptance for Risk and AI-QA workers."""

from __future__ import annotations

from datetime import timedelta

import pytest

from departments.risk_qa_testkit import (
    ResearchPacket,
    make_test_packet,
    run_risk_qa_pipeline,
)


def test_test_profile_runs_full_risk_and_qa_skeleton():
    result = run_risk_qa_pipeline("test")

    assert result["mode"] == "test"
    assert result["production_enabled"] is False
    assert result["pipeline_status"] == "COMPLETED"
    assert result["safe_action"] == "NO_ACTION"
    assert [stage["stage"] for stage in result["stages"]] == [
        "research_packet_fixture",
        "risk_deterministic_gate_skeleton",
        "risk_worker_graphs",
        "qa_deterministic_gate_skeleton",
        "qa_worker_graphs",
        "risk_qa_test_gate",
    ]
    assert result["risk_gate"]["binding"] is False
    assert result["qa_gate"]["binding"] is False
    assert result["qa_gate"]["decision"] == "WARN"
    assert len(result["risk"]["workers"]) == 4
    assert len(result["qa"]["workers"]) == 5
    assert result["risk"]["not_executed"] == []
    assert result["qa"]["not_executed"] == []
    assert all(
        worker["input_hash"] == result["packet"]["input_hash"]
        for worker in result["risk"]["workers"] + result["qa"]["workers"]
    )
    assert all(worker["trace"]["events"] for worker in result["risk"]["workers"])
    assert all(worker["trace"]["events"] for worker in result["qa"]["workers"])


def test_research_packet_handoff_fields_are_preserved_and_pit_bound():
    packet = make_test_packet()
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
    with pytest.raises(ValueError, match="future claim"):
        ResearchPacket(
            **{
                **packet.model_dump(),
                "as_known_at": packet.as_known_at - timedelta(seconds=1),
            }
        )


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
