"""MAS boundary contracts and deterministic conflict/replay checks."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from orchestration.contracts.mas import (
    ACTIONS,
    AnalysisOutput,
    DepartmentHandoff,
    Direction,
    EvidenceRef,
    Horizon,
    PredictionOutput,
    ProbabilityDistribution,
    Signal,
    build_replay_metadata,
    make_pipeline_event,
    resolve_signal_conflict,
    stable_hash,
    validate_worker_context,
)


AS_OF = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)


def _evidence(ref: str = "price:005930:2026-08-05") -> EvidenceRef:
    return EvidenceRef(source="price", ref=ref, as_of=AS_OF)


def test_prediction_requires_all_horizons_and_probability_sum() -> None:
    distribution = ProbabilityDistribution(up=0.45, down=0.20, side=0.35)
    prediction = PredictionOutput(
        run_id="run-1",
        as_of=AS_OF,
        horizons={horizon: distribution for horizon in Horizon},
    )
    assert set(prediction.horizons) == set(Horizon)

    with pytest.raises(ValidationError):
        ProbabilityDistribution(up=0.8, down=0.2, side=0.2)


def test_analysis_requires_evidence_and_decision_action_is_bounded() -> None:
    output = AnalysisOutput(
        run_id="run-1",
        as_of=AS_OF,
        asset_code="005930",
        signals=[
            Signal(
                signal_type="technical",
                direction=Direction.UP,
                confidence=0.7,
                evidence=[_evidence()],
            )
        ],
    )
    assert output.signals[0].evidence[0].ref.startswith("price:")

    with pytest.raises(ValidationError):
        Signal(signal_type="news", direction=Direction.UP, confidence=0.8, evidence=[])

    assert ACTIONS == {
        "close",
        "reduce_40",
        "reduce_20",
        "hold",
        "increase_20",
        "increase_40",
        "increase_upper_limit",
    }


def test_cross_department_handoff_is_head_to_head_and_non_binding() -> None:
    handoff = DepartmentHandoff(
        run_id="run-1",
        trace_id="trace-1",
        from_department="research",
        to_department="risk",
        from_role="research:head",
        to_role="risk:head",
        input_contract="mas.department-context.v1",
        output_contract="mas.department-context.v1",
        input_hash=stable_hash({"query": "국내 주식 포트폴리오"}),
        purpose="risk review",
        as_of=AS_OF,
    )
    assert handoff.binding is False

    with pytest.raises(ValidationError):
        DepartmentHandoff(
            **{**handoff.model_dump(), "from_role": "research:worker"}
        )


def test_conflict_resolution_uses_stop_rule_when_consensus_is_weak() -> None:
    resolution = resolve_signal_conflict(
        [
            Signal(
                signal_type="technical",
                direction=Direction.UP,
                confidence=0.5,
                evidence=[_evidence("price:1")],
            ),
            Signal(
                signal_type="news",
                direction=Direction.DOWN,
                confidence=0.6,
                evidence=[_evidence("news:1")],
            ),
        ]
    )
    assert resolution.stop_rule_triggered is True
    assert resolution.final_direction is Direction.SIDE
    assert resolution.stop_rule == "HOLD_ON_WEAK_CONSENSUS"


def test_worker_context_without_evidence_must_escalate() -> None:
    valid = validate_worker_context(
        {
            "worker_id": "worker-1",
            "summary": "근거 부족으로 검토 필요",
            "confidence": 0.0,
            "evidence_refs": [],
            "escalate": True,
            "schema_valid": True,
        }
    )
    assert valid.escalate is True

    with pytest.raises(ValidationError):
        validate_worker_context(
            {
                "worker_id": "worker-1",
                "summary": "완료",
                "confidence": 0.8,
                "evidence_refs": [],
                "escalate": False,
                "schema_valid": True,
            }
        )


def test_pipeline_event_and_replay_metadata_are_credential_free() -> None:
    event = make_pipeline_event(
        event_id="run-1:00001",
        run_id="run-1",
        event={
            "kind": "worker_completed",
            "stage": "research",
            "worker_id": "research-data-worker",
            "status": "COMPLETED",
            "input_hash": stable_hash({"query": "국내 주식"}),
            "summary": "근거 수집 완료",
        },
        occurred_at=AS_OF,
    )
    assert event.schema_id == "mas.pipeline-event.v1"
    metadata = build_replay_metadata(
        {"query": "국내 주식"},
        [{"symbol": "005930"}],
        {"pipeline_status": "COMPLETED", "data_context": {"source": "TEST"}},
    )
    assert metadata.replayable is True
    assert "credentials" in metadata.excludes
