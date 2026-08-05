"""Synthetic TEST-mode pipeline for the Risk and AI-QA boundary."""

from .department_graph import DepartmentGraphSpec, run_department_graph
from .pipeline import (
    PipelineMode,
    ResearchPacket,
    ResearchPacketV2,
    RiskQaPacket,
    WorkerRuntime,
    make_test_packet,
    run_risk_qa_pipeline,
)
from .portfolio_pipeline import run_portfolio_recommendation_pipeline
from .replay import (
    ReplayValidationError,
    build_test_replay_bundle,
    validate_replay_bundle,
)

__all__ = [
    "DepartmentGraphSpec",
    "PipelineMode",
    "ReplayValidationError",
    "ResearchPacket",
    "ResearchPacketV2",
    "RiskQaPacket",
    "WorkerRuntime",
    "build_test_replay_bundle",
    "make_test_packet",
    "run_department_graph",
    "run_portfolio_recommendation_pipeline",
    "run_risk_qa_pipeline",
    "validate_replay_bundle",
]
