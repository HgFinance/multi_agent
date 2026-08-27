"""Strategy Hermes-owned, evidence-driven strategy research runtime.

The physical package path is retained under ``01-research`` for repository and
rollback compatibility; its logical owner is Strategy Hermes, not Research HQ.
It intentionally has no dependency on the retired strategy-factory contracts.
It owns the research loop and emits portable JSON artifacts that a separate
validation or execution adapter may consume.
"""

RUNTIME_OWNER = "strategy-hermes"
BOUNDARY_CONTRACT = "strategy-hermes-owned-infrastructure.v1"

from .models import (
    ExperimentPlan,
    ExperimentResult,
    Hypothesis,
    Objective,
    ResearchEvent,
)

__all__ = [
    "BOUNDARY_CONTRACT",
    "ExperimentPlan",
    "ExperimentResult",
    "Hypothesis",
    "Objective",
    "ResearchEvent",
    "RUNTIME_OWNER",
]
