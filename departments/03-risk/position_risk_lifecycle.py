"""State and authority rules for PAPER position risk plans."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal

from position_risk_planner import PositionRiskPlan
from pydantic import BaseModel, ConfigDict, Field


class RiskPlanState(StrEnum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    USER_APPROVED = "USER_APPROVED"
    AUTO_POLICY_APPROVED = "AUTO_POLICY_APPROVED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    TRIGGERED = "TRIGGERED"


_TRANSITIONS = {
    RiskPlanState.PROPOSED: {RiskPlanState.VALIDATED},
    RiskPlanState.VALIDATED: {
        RiskPlanState.USER_APPROVED,
        RiskPlanState.AUTO_POLICY_APPROVED,
        RiskPlanState.EXPIRED,
    },
    RiskPlanState.USER_APPROVED: {RiskPlanState.ACTIVE, RiskPlanState.EXPIRED},
    RiskPlanState.AUTO_POLICY_APPROVED: {RiskPlanState.ACTIVE, RiskPlanState.EXPIRED},
    RiskPlanState.ACTIVE: {
        RiskPlanState.SUPERSEDED,
        RiskPlanState.EXPIRED,
        RiskPlanState.TRIGGERED,
    },
    RiskPlanState.SUPERSEDED: set(),
    RiskPlanState.EXPIRED: set(),
    RiskPlanState.TRIGGERED: set(),
}


class RiskPlanTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_plan_id: str
    from_state: RiskPlanState
    to_state: RiskPlanState
    occurred_at: datetime
    actor_type: Literal["RISK", "USER", "AUTO_POLICY", "TRADING", "SYSTEM"]
    actor_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    trace_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)


def validate_transition(transition: RiskPlanTransition) -> None:
    if transition.to_state not in _TRANSITIONS[transition.from_state]:
        raise ValueError(
            f"invalid risk plan transition: {transition.from_state}->{transition.to_state}"
        )
    authority = {
        RiskPlanState.VALIDATED: {"RISK"},
        RiskPlanState.USER_APPROVED: {"USER"},
        RiskPlanState.AUTO_POLICY_APPROVED: {"AUTO_POLICY"},
        RiskPlanState.ACTIVE: {"TRADING"},
        RiskPlanState.TRIGGERED: {"TRADING"},
        RiskPlanState.SUPERSEDED: {"RISK", "SYSTEM"},
        RiskPlanState.EXPIRED: {"SYSTEM", "RISK"},
    }
    if transition.actor_type not in authority.get(transition.to_state, set()):
        raise ValueError(
            f"actor {transition.actor_type} cannot enter {transition.to_state}"
        )


def validate_superseding_plan(
    current: PositionRiskPlan,
    proposed: PositionRiskPlan,
    *,
    user_approved_relaxation: bool = False,
) -> None:
    """Forbid increasing loss budget or loosening a long stop without approval."""

    if current.instrument_id != proposed.instrument_id or current.fund_id != proposed.fund_id:
        raise ValueError("superseding plan scope mismatch")
    if current.entry_reference is None or proposed.entry_reference is None:
        raise ValueError("both plans require numeric prices")
    if current.stop_price is None or proposed.stop_price is None:
        raise ValueError("both plans require a stop price")
    if current.position_risk_amount is None or proposed.position_risk_amount is None:
        raise ValueError("both plans require a loss budget")
    relaxed_stop = proposed.stop_price < current.stop_price
    expanded_budget = proposed.position_risk_amount > current.position_risk_amount
    if (relaxed_stop or expanded_budget) and not user_approved_relaxation:
        raise ValueError("risk relaxation requires explicit user approval")


def conditional_rule_idempotency_key(plan: PositionRiskPlan) -> str:
    """Stable Trading admission key; replaying one plan cannot duplicate rules."""

    material = (
        f"{plan.risk_plan_id}:{plan.input_hash}:{plan.calculation_version}:"
        f"{plan.stop_price}:{plan.take_profit_price}:{plan.quantity_cap}"
    )
    return "risk-plan:" + hashlib.sha256(material.encode()).hexdigest()


__all__ = [
    "RiskPlanState",
    "RiskPlanTransition",
    "conditional_rule_idempotency_key",
    "validate_superseding_plan",
    "validate_transition",
]
