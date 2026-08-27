"""Autonomous, evidence-driven strategy research runtime.

This package intentionally has no dependency on the retired strategy-factory
contracts.  It owns the research loop and emits portable JSON artifacts that a
separate execution adapter may consume.
"""

from .models import (
    ExperimentPlan,
    ExperimentResult,
    Hypothesis,
    Objective,
    ResearchEvent,
)

__all__ = [
    "ExperimentPlan",
    "ExperimentResult",
    "Hypothesis",
    "Objective",
    "ResearchEvent",
]
