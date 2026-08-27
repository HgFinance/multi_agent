"""Small, transportable models for the autonomous research loop.

The models are deliberately independent of Postgres, Pydantic and the old
factory vocabulary.  A research session can therefore be resumed in a clean
workspace, inspected by a human, or handed to another execution backend
without importing the retired pipeline.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
from typing import Any, Mapping
import uuid


SCHEMA_VERSION = "autonomous-quant-research.v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, *values: object) -> str:
    payload = canonical_json(values).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:16]}"


def _text(value: object, field_name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


def _texts(values: object, field_name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple, set)):
        raise ValueError(f"{field_name} must be a sequence of strings")
    result = tuple(_text(value, field_name) for value in values)
    return tuple(dict.fromkeys(result))


@dataclass(frozen=True)
class Objective:
    goal: str
    universe: str = "unspecified"
    horizon: str = "unspecified"
    constraints: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)
    version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _text(self.goal, "goal")
        _text(self.universe, "universe")
        _text(self.horizon, "horizon")
        _texts(self.constraints, "constraints")


@dataclass(frozen=True)
class Hypothesis:
    hypothesis_id: str
    statement: str
    mechanism: str
    expected_behavior: str
    falsifiers: tuple[str, ...]
    dimensions: Mapping[str, str]
    parent_id: str | None = None
    role: str = "explore"
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("hypothesis_id", "statement", "mechanism", "expected_behavior"):
            _text(getattr(self, name), name)
        if not self.falsifiers:
            raise ValueError("falsifiers must contain at least one test")
        _texts(self.falsifiers, "falsifiers")
        if not self.dimensions:
            raise ValueError("dimensions must describe the research representation")


@dataclass(frozen=True)
class ExperimentPlan:
    plan_id: str
    hypothesis_id: str
    objective: str
    method: str
    data_requirements: tuple[str, ...]
    splits: tuple[str, ...]
    cost_model: str
    seed: int
    signature: Mapping[str, str]
    preregistration_hash: str
    status: str = "PLANNED"
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("plan_id", "hypothesis_id", "objective", "method", "cost_model", "preregistration_hash"):
            _text(getattr(self, name), name)
        if not self.data_requirements or not self.splits:
            raise ValueError("data_requirements and splits are required")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        if not self.signature:
            raise ValueError("signature must not be empty")


@dataclass(frozen=True)
class ExperimentResult:
    plan_id: str
    status: str
    cost_included: bool
    oos_evaluated: bool
    leakage_detected: bool
    robustness: Mapping[str, bool]
    metrics: Mapping[str, Any] = field(default_factory=dict)
    artifacts: tuple[str, ...] = ()
    failure_modes: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    preregistration_hash: str | None = None
    failure_reason: str | None = None
    observed_at: str = field(default_factory=utc_now)

    def validate(self) -> None:
        _text(self.plan_id, "plan_id")
        if self.status not in {"COMPLETED", "FAILED", "BLOCKED"}:
            raise ValueError(f"unsupported result status: {self.status!r}")
        if not isinstance(self.cost_included, bool):
            raise ValueError("cost_included must be boolean")
        if not isinstance(self.oos_evaluated, bool):
            raise ValueError("oos_evaluated must be boolean")
        if not isinstance(self.leakage_detected, bool):
            raise ValueError("leakage_detected must be boolean")
        if not isinstance(self.robustness, Mapping):
            raise ValueError("robustness must be a mapping")
        if not self.robustness or any(not isinstance(value, bool) for value in self.robustness.values()):
            raise ValueError("robustness must contain named boolean checks")
        if self.status != "COMPLETED" and not str(self.failure_reason or "").strip():
            raise ValueError("failed or blocked results require failure_reason")
        if self.preregistration_hash is not None:
            _text(self.preregistration_hash, "preregistration_hash")
        _validate_finite(self.metrics, "metrics")


@dataclass(frozen=True)
class ResearchEvent:
    event_type: str
    payload: Mapping[str, Any]
    event_id: str = field(default_factory=lambda: f"evt-{uuid.uuid4().hex[:16]}")
    created_at: str = field(default_factory=utc_now)
    schema: str = SCHEMA_VERSION


def _validate_finite(value: Any, path: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        if not isfinite(float(value)):
            raise ValueError(f"{path} contains a non-finite value")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_finite(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_finite(item, f"{path}[{index}]")
        return
    raise ValueError(f"{path} contains unsupported value {type(value).__name__}")


def to_dict(value: Any) -> dict[str, Any]:
    if not hasattr(value, "__dataclass_fields__"):
        raise TypeError(f"expected dataclass, got {type(value).__name__}")
    return asdict(value)


def from_result_dict(payload: Mapping[str, Any]) -> ExperimentResult:
    result = ExperimentResult(
        plan_id=_text(payload.get("plan_id"), "plan_id"),
        status=_text(payload.get("status"), "status").upper(),
        cost_included=payload.get("cost_included"),
        oos_evaluated=payload.get("oos_evaluated"),
        leakage_detected=payload.get("leakage_detected"),
        robustness=payload.get("robustness") or {},
        metrics=payload.get("metrics") or {},
        artifacts=_texts(payload.get("artifacts"), "artifacts"),
        failure_modes=_texts(payload.get("failure_modes"), "failure_modes"),
        limitations=_texts(payload.get("limitations"), "limitations"),
        preregistration_hash=(str(payload["preregistration_hash"]).strip() if payload.get("preregistration_hash") else None),
        failure_reason=(str(payload["failure_reason"]).strip() if payload.get("failure_reason") else None),
        observed_at=str(payload.get("observed_at") or utc_now()),
    )
    result.validate()
    return result
