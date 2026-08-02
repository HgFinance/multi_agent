"""Fail-closed execution boundary for QA and Audit skills."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

SECRET_FIELDS = frozenset({"api_key", "apikey", "authorization", "password", "secret", "token", "private_key"})


class HarnessDecision(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class SkillSpec:
    name: str
    kind: str
    allowed_tools: frozenset[str] = frozenset()
    forbidden_tools: frozenset[str] = frozenset()
    requires_grounded: bool = False
    # 초기 실행 1회 뒤 재시도는 최대 2회다(총 3회). 마지막 실패는 QA
    # ESCALATE로 종료하여 수동 검토로 넘긴다.
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not self.name.strip() or self.max_attempts < 1:
            raise ValueError("invalid SkillSpec")


@dataclass(frozen=True)
class SkillResult:
    decision: HarnessDecision
    output: Mapping[str, Any]
    input_hash: str
    trace_id: str
    reason: str = ""
    fallback_used: bool = False


class DepartmentHarness:
    def __init__(self, skills: Sequence[SkillSpec]) -> None:
        self._skills = {skill.name: skill for skill in skills}
        if len(self._skills) != len(skills):
            raise ValueError("duplicate skill name")

    @property
    def skills(self) -> Mapping[str, SkillSpec]:
        return self._skills

    def preflight(
        self,
        skill_name: str,
        *,
        trace_id: str,
        payload: Mapping[str, Any],
        tool_name: str | None = None,
    ) -> SkillResult | None:
        if skill_name not in self._skills:
            return self._blocked(trace_id, "skill_not_registered")
        if not trace_id.strip():
            return self._blocked(trace_id, "trace_id_missing")
        if _contains_secret_field(payload):
            return self._blocked(trace_id, "secret_field_in_skill_payload")
        skill = self._skills[skill_name]
        if tool_name is not None and (tool_name in skill.forbidden_tools or tool_name not in skill.allowed_tools):
            return self._blocked(trace_id, "tool_not_allowed")
        return SkillResult(HarnessDecision.READY, {}, _hash_payload(payload), trace_id)

    def execute(
        self,
        skill_name: str,
        *,
        trace_id: str,
        payload: Mapping[str, Any],
        handler: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        tool_name: str | None = None,
    ) -> SkillResult:
        preflight = self.preflight(skill_name, trace_id=trace_id, payload=payload, tool_name=tool_name)
        if preflight is None or preflight.decision is not HarnessDecision.READY:
            return preflight or self._blocked(trace_id, "skill_not_registered")
        skill = self._skills[skill_name]
        for attempt in range(skill.max_attempts):
            try:
                output = handler(payload)
                if not isinstance(output, Mapping):
                    raise TypeError("skill output must be an object")
                if skill.requires_grounded and output.get("grounded") is not True:
                    return SkillResult(HarnessDecision.ESCALATE, dict(output), preflight.input_hash, trace_id, "grounded_result_required")
                return SkillResult(HarnessDecision.READY, dict(output), preflight.input_hash, trace_id)
            except Exception as exc:  # noqa: BLE001 - skill boundary must fail closed for any handler error
                if attempt + 1 < skill.max_attempts:
                    continue
                return SkillResult(
                    HarnessDecision.ESCALATE,
                    {"decision": "ESCALATE", "reason_codes": ["harness_fallback"], "findings": ["manual_review_required"]},
                    preflight.input_hash,
                    trace_id,
                    f"{type(exc).__name__}: {skill_name}",
                    True,
                )
        raise AssertionError("unreachable")

    @staticmethod
    def _blocked(trace_id: str, reason: str) -> SkillResult:
        return SkillResult(
            HarnessDecision.BLOCKED,
            {"decision": "ESCALATE", "reason_codes": [reason], "findings": ["manual_review_required"]},
            "",
            trace_id,
            reason,
            True,
        )


def _contains_secret_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(str(key).lower().replace("-", "_") in SECRET_FIELDS or _contains_secret_field(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_field(item) for item in value)
    return False


def _hash_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
