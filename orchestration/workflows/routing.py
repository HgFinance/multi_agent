"""Deterministic event-to-expert routing boundary."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import WorkflowContractError
from .manifest import load_workflow


@dataclass(frozen=True)
class RoutingDecision:
    event: str
    calls: tuple[str, ...]
    deterministic_check: bool
    action: str | None
    reason: str


def route_event(event: str) -> RoutingDecision:
    """Return an allow-listed routing decision.

    Unknown events are deliberately blocked.  The router never turns an
    event into an order or a Risk approval.
    """

    if not event or not isinstance(event, str):
        raise WorkflowContractError("event는 비어 있지 않은 문자열이어야 합니다")
    spec = load_workflow("event-routing")
    rules = spec.metadata.get("rules", {})
    if not isinstance(rules, dict):
        raise WorkflowContractError("event-routing.rules가 mapping이 아닙니다")
    raw = rules.get(event)
    if not isinstance(raw, dict):
        return RoutingDecision(
            event=event,
            calls=(),
            deterministic_check=True,
            action="ENTRY_BLOCKED",
            reason="unknown event is fail-closed",
        )
    calls = raw.get("call", ())
    if isinstance(calls, str) or not isinstance(calls, (list, tuple)):
        raise WorkflowContractError(f"{event}: call은 배열이어야 합니다")
    return RoutingDecision(
        event=event,
        calls=tuple(str(value) for value in calls),
        deterministic_check=bool(raw.get("deterministic_check", False)),
        action=str(raw["action"]) if raw.get("action") else None,
        reason="deterministic rule" if raw.get("deterministic_check") else "allow-listed expert pool",
    )

