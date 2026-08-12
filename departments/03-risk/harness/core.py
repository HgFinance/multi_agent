"""Fail-closed execution boundary for Risk skills.

Hermes/LangGraph may select a skill, but this harness owns the last preflight
before a tool or model call.  It never logs secrets and it never turns a
failed call into an approval.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .journal import RunJournal

SECRET_FIELDS = frozenset(
    {"api_key", "apikey", "authorization", "password", "secret", "token", "private_key"}
)


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
    # 초기 실행 1회 뒤 재시도는 최대 2회다(총 3회). 무한 재시도나 승인
    # 방향의 자동 fallback은 허용하지 않으며, 마지막 실패는 Risk reject/HALT로 끝난다.
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
    retry_count: int = 0


class DepartmentHarness:
    def __init__(
        self,
        skills: Sequence[SkillSpec],
        *,
        journal: RunJournal | None = None,
        hermes_profile: str = "risk-management",
    ) -> None:
        self._skills = {skill.name: skill for skill in skills}
        if len(self._skills) != len(skills):
            raise ValueError("duplicate skill name")
        self.journal = journal or RunJournal(hermes_profile=hermes_profile)

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
        input_hash = ""
        if skill_name not in self._skills:
            return self._blocked(trace_id, "skill_not_registered")
        if not trace_id.strip():
            return self._blocked(trace_id, "trace_id_missing")
        if _contains_secret_field(payload):
            return self._blocked(trace_id, "secret_field_in_skill_payload")
        skill = self._skills[skill_name]
        if tool_name is not None and (
            tool_name in skill.forbidden_tools or tool_name not in skill.allowed_tools
        ):
            return self._blocked(trace_id, "tool_not_allowed")
        input_hash = _hash_payload(payload)
        return SkillResult(HarnessDecision.READY, {}, input_hash, trace_id)

    def execute(
        self,
        skill_name: str,
        *,
        trace_id: str,
        payload: Mapping[str, Any],
        handler: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        tool_name: str | None = None,
        run_id: str | None = None,
        employee_profile: str = "unknown",
        as_of: str | None = None,
        asset: str | None = None,
        model_version: str | None = None,
        prompt_version: str | None = None,
        parameter_version: str | None = None,
    ) -> SkillResult:
        preflight = self.preflight(
            skill_name, trace_id=trace_id, payload=payload, tool_name=tool_name
        )
        if preflight is None:
            return self._blocked(trace_id, "skill_not_registered")
        if preflight.decision is not HarnessDecision.READY:
            self.journal.validation(
                run_id=run_id or trace_id,
                trace_id=trace_id,
                employee_profile=employee_profile,
                inputs_hash=preflight.input_hash,
                schema_id=skill_name,
                schema_valid=False,
                domain_valid=False,
                failed_rule=preflight.reason,
                fallback_reason=preflight.reason,
                as_of=as_of,
                asset=asset,
                model_version=model_version,
                prompt_version=prompt_version,
                parameter_version=parameter_version,
            )
            self.journal.decision(
                run_id=run_id or trace_id,
                trace_id=trace_id,
                employee_profile=employee_profile,
                inputs_hash=preflight.input_hash,
                output=preflight.output,
                schema_id=skill_name,
                schema_valid=False,
                domain_valid=False,
                failed_rule=preflight.reason,
                fallback_reason=preflight.reason,
                as_of=as_of,
                asset=asset,
            )
            return preflight
        skill = self._skills[skill_name]
        execution_run_id = run_id or trace_id
        self.journal.input_snapshot(
            run_id=execution_run_id,
            trace_id=trace_id,
            employee_profile=employee_profile,
            payload=payload,
            schema_id=skill_name,
            as_of=as_of,
            asset=asset,
            model_version=model_version,
            prompt_version=prompt_version,
            parameter_version=parameter_version,
        )
        for attempt in range(skill.max_attempts):
            try:
                output = handler(payload)
                if not isinstance(output, Mapping):
                    raise TypeError("skill output must be an object")
                if _contains_secret_field(output):
                    raise ValueError("secret_field_in_skill_output")
                output_dict = dict(output)
                self.journal.agent_output(
                    run_id=execution_run_id,
                    trace_id=trace_id,
                    employee_profile=employee_profile,
                    output=output_dict,
                    inputs_hash=preflight.input_hash,
                    schema_id=skill_name,
                    schema_valid=True,
                    retry_count=attempt,
                    as_of=as_of,
                    asset=asset,
                    model_version=model_version,
                    prompt_version=prompt_version,
                    parameter_version=parameter_version,
                )
                if skill.requires_grounded and output.get("grounded") is not True:
                    result = SkillResult(
                        HarnessDecision.ESCALATE,
                        output_dict,
                        preflight.input_hash,
                        trace_id,
                        "grounded_result_required",
                        True,
                        attempt,
                    )
                    self.journal.validation(
                        run_id=execution_run_id,
                        trace_id=trace_id,
                        employee_profile=employee_profile,
                        inputs_hash=preflight.input_hash,
                        schema_id=skill_name,
                        schema_valid=True,
                        domain_valid=False,
                        failed_rule="grounded_result_required",
                        retry_count=attempt,
                    )
                    self.journal.decision(
                        run_id=execution_run_id,
                        trace_id=trace_id,
                        employee_profile=employee_profile,
                        inputs_hash=preflight.input_hash,
                        output=output_dict,
                        schema_id=skill_name,
                        schema_valid=True,
                        domain_valid=False,
                        failed_rule="grounded_result_required",
                        fallback_reason="grounded_result_required",
                        retry_count=attempt,
                    )
                    return result
                self.journal.validation(
                    run_id=execution_run_id,
                    trace_id=trace_id,
                    employee_profile=employee_profile,
                    inputs_hash=preflight.input_hash,
                    schema_id=skill_name,
                    schema_valid=True,
                    domain_valid=True,
                    retry_count=attempt,
                )
                result = SkillResult(
                    HarnessDecision.READY,
                    output_dict,
                    preflight.input_hash,
                    trace_id,
                    retry_count=attempt,
                )
                self.journal.decision(
                    run_id=execution_run_id,
                    trace_id=trace_id,
                    employee_profile=employee_profile,
                    inputs_hash=preflight.input_hash,
                    output=output_dict,
                    schema_id=skill_name,
                    schema_valid=True,
                    domain_valid=True,
                    retry_count=attempt,
                )
                return result
            except Exception as exc:  # noqa: BLE001 - skill boundary must fail closed for any handler error
                self.journal.validation(
                    run_id=execution_run_id,
                    trace_id=trace_id,
                    employee_profile=employee_profile,
                    inputs_hash=preflight.input_hash,
                    schema_id=skill_name,
                    schema_valid=False,
                    domain_valid=False,
                    failed_rule=type(exc).__name__,
                    retry_count=attempt,
                )
                if attempt + 1 < skill.max_attempts:
                    continue
                result = SkillResult(
                    HarnessDecision.ESCALATE,
                    {
                        "verdict": "reject",
                        "trading_state": "HALTED",
                        "reason_codes": ["harness_fallback"],
                    },
                    preflight.input_hash,
                    trace_id,
                    f"{type(exc).__name__}: {skill_name}",
                    True,
                    attempt,
                )
                self.journal.decision(
                    run_id=execution_run_id,
                    trace_id=trace_id,
                    employee_profile=employee_profile,
                    inputs_hash=preflight.input_hash,
                    output=result.output,
                    schema_id=skill_name,
                    schema_valid=False,
                    domain_valid=False,
                    failed_rule=type(exc).__name__,
                    fallback_reason="harness_fallback",
                    retry_count=attempt,
                )
                return result
        raise AssertionError("unreachable")

    @staticmethod
    def _blocked(trace_id: str, reason: str) -> SkillResult:
        return SkillResult(
            HarnessDecision.BLOCKED,
            {"verdict": "reject", "trading_state": "HALTED", "reason_codes": [reason]},
            "",
            trace_id,
            reason,
            True,
        )


def _contains_secret_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower().replace("-", "_") in SECRET_FIELDS
            or _contains_secret_field(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_field(item) for item in value)
    return False


def _hash_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
