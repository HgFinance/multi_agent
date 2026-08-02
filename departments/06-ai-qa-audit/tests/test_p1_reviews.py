from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from internal_audit import InternalAuditDecision, InternalAuditEngine
from model_risk import ModelRiskDecision, ModelRiskEngine, ModelRiskInput


def test_model_risk_requires_lineage_and_metrics() -> None:
    model = ModelRiskInput(uuid4(), "model-v1", "prompt-v1", "dataset-v1", 500, 0.9, 0.02, 0.04, 0.01)
    result = ModelRiskEngine().evaluate(model)
    assert result.decision is ModelRiskDecision.PASS
    bad = ModelRiskInput(uuid4(), "", "prompt-v1", "dataset-v1", 500, 0.9, 0.02, 0.04, 0.01)
    assert ModelRiskEngine().evaluate(bad).decision is ModelRiskDecision.ESCALATE


def test_internal_audit_blocks_unproven_or_cross_domain_trace() -> None:
    result = InternalAuditEngine().evaluate(
        events=[
            {"action": "qa.evidence.check", "department": "qa", "trace_id": str(uuid4()), "profile_status": "ACTIVE", "authorized": True},
            {"action": "oms.submit", "department": "qa", "trace_id": str(uuid4()), "profile_status": "ACTIVE", "authorized": True},
        ]
    )
    assert result.decision is InternalAuditDecision.ESCALATE
    assert "event_1:forbidden_cross_domain_action" in result.findings
