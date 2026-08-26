"""Deterministic contracts for cross-department handoffs.

This module deliberately contains no LLM or department business logic.  A
workflow step can only consume the handoff emitted by the previous step.  A
failed step stops the chain and exposes the declared safe action instead of
silently passing an incomplete result forward.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

SAFE_FAILURE_ACTIONS = frozenset(
    {
        "HOLD",
        "REJECT",
        "ESCALATE",
        "BREAK",
        "NO_SUBMIT",
        "REJECT_PROMOTION",
        "NO_REQUISITION",
        "REJECT_CANDIDATE",
        "DENY_PERMISSION",
        "NO_APPROVAL",
        "NO_CHANGE",
        "REJECT_REVISION",
        "ROLLBACK",
        "ENTRY_BLOCKED",
    }
)


class WorkflowContractError(ValueError):
    """Raised when a workflow would cross an invalid boundary."""


@dataclass(frozen=True)
class StepSpec:
    """One ordered boundary between two department-owned components."""

    id: str
    sequence: int
    department: str
    task: str
    input_contract: str
    output_contract: str
    timeout_seconds: int
    max_attempts: int
    failure_action: str
    owner: str
    forbidden_actions: tuple[str, ...] = ()
    async_post_response: bool = False


@dataclass(frozen=True)
class WorkflowSpec:
    """Validated workflow metadata and its ordered handoff steps."""

    name: str
    version: str
    kind: str
    description: str
    steps: tuple[StepSpec, ...]
    boundary_rules: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.name or not self.version or not self.kind:
            raise WorkflowContractError("name, version, kind는 필수입니다")
        if not self.steps:
            raise WorkflowContractError(f"{self.name}: steps가 비어 있습니다")

        expected_sequences = list(range(1, len(self.steps) + 1))
        actual_sequences = [step.sequence for step in self.steps]
        if actual_sequences != expected_sequences:
            raise WorkflowContractError(
                f"{self.name}: step sequence가 연속적이지 않습니다: {actual_sequences}"
            )

        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise WorkflowContractError(f"{self.name}: 중복 step id가 있습니다")

        for step in self.steps:
            if not step.department or not step.owner:
                raise WorkflowContractError(f"{self.name}/{step.id}: owner/department 누락")
            if step.timeout_seconds <= 0 or step.max_attempts <= 0:
                raise WorkflowContractError(
                    f"{self.name}/{step.id}: timeout과 max_attempts는 양수여야 합니다"
                )
            if not step.input_contract or not step.output_contract:
                raise WorkflowContractError(
                    f"{self.name}/{step.id}: input/output contract 누락"
                )
            if step.failure_action not in SAFE_FAILURE_ACTIONS:
                raise WorkflowContractError(
                    f"{self.name}/{step.id}: 안전하지 않은 failure action {step.failure_action!r}"
                )

        async_steps = [step for step in self.steps if step.async_post_response]
        if async_steps:
            if async_steps != [self.steps[-1]]:
                raise WorkflowContractError(
                    f"{self.name}: post-response async step은 마지막이어야 합니다"
                )
            if async_steps[0].department != "qa-department":
                raise WorkflowContractError(
                    f"{self.name}: post-response async step은 QA가 소유해야 합니다"
                )

        for previous, current in zip(
            self.steps, self.steps[1:], strict=False
        ):
            if current.input_contract != previous.output_contract:
                raise WorkflowContractError(
                    f"{self.name}: {previous.id} -> {current.id} handoff 불일치 "
                    f"({previous.output_contract!r} != {current.input_contract!r})"
                )


@dataclass(frozen=True)
class StepRun:
    """Auditable result of one orchestration boundary."""

    step_id: str
    sequence: int
    status: str
    input_contract: str
    output_contract: str
    failure_action: str
    attempts: int
    detail: str = ""


@dataclass(frozen=True)
class WorkflowRun:
    """A run record containing only orchestration metadata, not domain state."""

    run_id: str
    workflow: str
    mode: str
    status: str
    safe_action: str | None
    steps: tuple[StepRun, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
