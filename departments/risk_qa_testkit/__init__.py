"""Synthetic TEST-mode pipeline for the Risk and AI-QA boundary."""

from .pipeline import (
    PipelineMode,
    ResearchPacket,
    ResearchPacketV2,
    RiskQaPacket,
    WorkerRuntime,
    run_risk_qa_pipeline,
    make_test_packet,
)
from .department_graph import DepartmentGraphSpec, run_department_graph
from .portfolio_pipeline import run_portfolio_recommendation_pipeline
from .replay import ReplayValidationError, build_test_replay_bundle, validate_replay_bundle

__all__ = [
    "PipelineMode",
    "ResearchPacket",
    "ResearchPacketV2",
    "RiskQaPacket",
    "WorkerRuntime",
 "run_risk_qa_pipeline",
 "make_test_packet",
 "DepartmentGraphSpec",
    "run_department_graph",
    "run_portfolio_recommendation_pipeline",
    "ReplayValidationError",
    "build_test_replay_bundle",
    "validate_replay_bundle",
]
