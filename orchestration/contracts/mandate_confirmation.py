"""Deterministic boundary between an LLM proposal and mandate activation."""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MandateConfirmationGate(BaseModel):
    """Audit record for proposal -> Risk/QA -> user confirmation -> activate."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1)
    policy_payload: dict[str, Any]
    policy_schema_valid: bool
    risk_approved: bool
    qa_approved: bool
    user_confirmed: bool
    user_confirmation_id: str | None = None
    user_confirmed_at: datetime | None = None
    action: Literal["ACTIVATE", "HOLD"] = "HOLD"
    reason: str = ""

    @model_validator(mode="after")
    def evaluate(self) -> MandateConfirmationGate:
        if not self.policy_schema_valid:
            self.action = "HOLD"
            self.reason = "POLICY_SCHEMA_INVALID"
        elif not self.risk_approved:
            self.action = "HOLD"
            self.reason = "RISK_APPROVAL_REQUIRED"
        elif not self.qa_approved:
            self.action = "HOLD"
            self.reason = "QA_APPROVAL_REQUIRED"
        elif not self.user_confirmed or not self.user_confirmation_id:
            self.action = "HOLD"
            self.reason = "USER_CONFIRMATION_REQUIRED"
        else:
            self.action = "ACTIVATE"
            self.reason = "ALL_CONFIRMATION_GATES_PASSED"
        return self


def evaluate_mandate_confirmation(
    *,
    proposal_id: str,
    policy_payload: dict[str, Any],
    risk_approved: bool,
    qa_approved: bool,
    user_confirmed: bool,
    user_confirmation_id: str | None,
    user_confirmed_at: datetime | None,
) -> MandateConfirmationGate:
    """Validate the handoff without interpreting LLM text or changing state."""

    policy_schema_valid = False
    try:
        # Keep this import lazy to avoid a CEO-policy/workflow import cycle.
        try:
            from mandate.policy import MandatePolicy  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            from policy import MandatePolicy  # type: ignore[import-not-found]

        try:
            MandatePolicy.model_validate(policy_payload)
        except Exception:
            direct_policy = sys.modules.get("policy")
            if direct_policy is None:
                raise
            direct_policy.MandatePolicy.model_validate(policy_payload)
        policy_schema_valid = True
    except Exception:  # noqa: BLE001 - malformed policy becomes HOLD.
        policy_schema_valid = False

    return MandateConfirmationGate(
        proposal_id=proposal_id,
        policy_payload=policy_payload,
        policy_schema_valid=policy_schema_valid,
        risk_approved=risk_approved,
        qa_approved=qa_approved,
        user_confirmed=user_confirmed,
        user_confirmation_id=user_confirmation_id,
        user_confirmed_at=user_confirmed_at,
    )


__all__ = ["MandateConfirmationGate", "evaluate_mandate_confirmation"]
