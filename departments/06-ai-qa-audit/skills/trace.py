"""In-process Skill trace and replay manifest for AI-QA Workers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import QASkillContext, QASkillResult


@dataclass
class SkillTrace:
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        context: QASkillContext,
        result: QASkillResult,
        *,
        latency_ms: int = 0,
    ) -> None:
        self.events.append(
            {
                "trace_id": context.trace_id,
                "worker_id": context.worker_id,
                "skill_id": result.skill_id,
                "status": result.status,
                "input_hash": context.input_hash,
                "output_hash": result.output_hash,
                "profile_version": context.profile_version,
                "attempt": context.attempt,
                "latency_ms": latency_ms,
                "error_code": result.error_code,
                "escalate": result.escalate,
            }
        )

    def manifest(self, context: QASkillContext) -> dict[str, Any]:
        return {
            "trace_id": context.trace_id,
            "worker_id": context.worker_id,
            "input_hash": context.input_hash,
            "profile_version": context.profile_version,
            "as_of": context.as_of.isoformat(),
            "events": list(self.events),
        }
