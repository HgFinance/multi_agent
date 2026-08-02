"""Deterministic model-risk review for the QA department.

This is an evidence gate, not a model-serving agent.  It refuses to produce a
PASS without immutable model/prompt/dataset versions and quantitative test
results.  A future ``model-risk-agent`` may explain this result, but cannot
override it.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

CALCULATION_VERSION = "qa-model-risk-v1"


class ModelRiskDecision(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class ModelRiskInput:
    model_id: UUID
    model_version: str
    prompt_version: str
    dataset_version: str
    evaluation_count: int
    accuracy: float
    calibration_error: float
    drift_score: float
    protected_failure_rate: float


@dataclass(frozen=True)
class ModelRiskAssessment:
    decision: ModelRiskDecision
    reason_codes: tuple[str, ...]
    calculation_version: str
    input_hash: str


class ModelRiskEngine:
    """Apply fixed thresholds; no LLM or statistical fallback is involved."""

    def __init__(
        self,
        *,
        min_evaluations: int = 100,
        max_calibration_error: float = 0.10,
        max_drift_score: float = 0.20,
        max_protected_failure_rate: float = 0.05,
    ) -> None:
        self.min_evaluations = min_evaluations
        self.max_calibration_error = max_calibration_error
        self.max_drift_score = max_drift_score
        self.max_protected_failure_rate = max_protected_failure_rate

    def evaluate(self, evidence: ModelRiskInput) -> ModelRiskAssessment:
        payload = evidence.__dict__ | {"model_id": str(evidence.model_id)}
        input_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        reasons: list[str] = []
        if not evidence.model_version.strip() or not evidence.prompt_version.strip() or not evidence.dataset_version.strip():
            reasons.append("version_lineage_missing")
        values = (evidence.accuracy, evidence.calibration_error, evidence.drift_score, evidence.protected_failure_rate)
        if any(not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0 for value in values):
            reasons.append("metric_out_of_range")
        if evidence.evaluation_count < self.min_evaluations:
            reasons.append("insufficient_evaluation_count")
        if evidence.calibration_error > self.max_calibration_error:
            reasons.append("calibration_error_exceeded")
        if evidence.drift_score > self.max_drift_score:
            reasons.append("drift_exceeded")
        if evidence.protected_failure_rate > self.max_protected_failure_rate:
            reasons.append("protected_failure_rate_exceeded")
        if "metric_out_of_range" in reasons or "version_lineage_missing" in reasons:
            decision = ModelRiskDecision.ESCALATE
        elif any(reason.endswith("exceeded") for reason in reasons):
            decision = ModelRiskDecision.FAIL
        elif reasons:
            decision = ModelRiskDecision.WARN
        else:
            decision = ModelRiskDecision.PASS
        return ModelRiskAssessment(decision, tuple(reasons), CALCULATION_VERSION, input_hash)
