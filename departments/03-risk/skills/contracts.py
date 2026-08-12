"""Typed contracts for Risk Worker skills and tool boundaries."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def hash_payload(value: Any) -> str:
    """Return a stable hash without persisting raw financial input."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


class RiskSkillContext(BaseModel):
    """Immutable-ish execution context passed to every Risk Skill."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    case_id: str | None = None
    worker_id: str
    # Worker Profile version; the wire contract is carried separately in schema_version.
    profile_version: str = "risk-profile-v1"
    as_of: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    input_hash: str
    allowed_scopes: tuple[str, ...] = ()
    timeout_ms: int = Field(default=8_000, ge=1, le=120_000)
    attempt: int = Field(default=1, ge=1, le=3)

    @field_validator("as_of", mode="before")
    @classmethod
    def normalize_as_of(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        raise ValueError("as_of must be an ISO datetime")


class RiskSkillResult(BaseModel):
    """Structured result; a non-completed status is never a pass."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    status: Literal["COMPLETED", "DEGRADED", "ESCALATE"]
    output: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    output_hash: str
    error_code: str | None = None
    escalate: bool = False


def make_result(
    skill_id: str,
    status: Literal["COMPLETED", "DEGRADED", "ESCALATE"],
    output: Any = None,
    *,
    evidence_refs: list[str] | None = None,
    tool_calls: list[str] | None = None,
    error_code: str | None = None,
    escalate: bool | None = None,
) -> RiskSkillResult:
    """Build a result with a deterministic output hash."""

    normalized = output if isinstance(output, dict) else {"value": output}
    return RiskSkillResult(
        skill_id=skill_id,
        status=status,
        output=normalized,
        evidence_refs=evidence_refs or [],
        tool_calls=tool_calls or [],
        output_hash=hash_payload(normalized),
        error_code=error_code,
        escalate=(status == "ESCALATE") if escalate is None else escalate,
    )
