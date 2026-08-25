"""Trading-owned conversion of an ACTIVE PAPER Risk Plan into rule candidates.

Risk supplies immutable numeric levels.  This adapter never recalculates them
and never submits an order; the existing conditional-rule admission, Risk
Engine recheck, reservation, and idempotency layers remain mandatory.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from orchestration.conditional_rules import EvaluationPolicy, ExpressionNode, RuleAction


class RiskPlanRuleCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    leg: str
    client_request_id: str
    raw_instruction: str
    candidate: dict[str, Any]


class RiskPlanTradingBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "trading.risk-plan-rule-bundle.v1"
    risk_plan_id: str
    source_input_hash: str
    execution_mode: str = "PAPER"
    candidates: tuple[RiskPlanRuleCandidate, ...]


def _key(plan_id: str, input_hash: str, leg: str) -> str:
    digest = hashlib.sha256(f"{plan_id}:{input_hash}:{leg}".encode()).hexdigest()
    return f"risk-plan-{leg.lower()}-{digest[:48]}"


def _condition(operator: str, price: Decimal) -> dict[str, Any]:
    return {
        "type": "COMPARISON",
        "operator": operator,
        "left": {"type": "MARKET", "field": "LAST_PRICE"},
        "right": {"type": "LITERAL", "value": str(price), "unit": "PRICE"},
    }


def build_paper_rule_bundle(
    plan: Mapping[str, Any], *, symbol: str
) -> RiskPlanTradingBundle:
    if plan.get("schema_version") != "risk.position-risk-plan.v1":
        raise ValueError("unsupported position risk plan contract")
    if plan.get("execution_mode") != "PAPER":
        raise ValueError("position risk plan conversion is PAPER-only")
    if plan.get("state") != "ACTIVE":
        raise ValueError("only an ACTIVE position risk plan can be converted")
    if plan.get("action") != "PROPOSE":
        raise ValueError("only a proposed numeric control plan can create rules")

    required = (
        "risk_plan_id",
        "instrument_id",
        "input_hash",
        "stop_price",
        "take_profit_price",
        "quantity_cap",
        "current_quantity",
        "expires_at",
    )
    missing = [name for name in required if plan.get(name) in (None, "")]
    if missing:
        raise ValueError(f"risk plan missing fields: {missing}")
    plan_id = str(plan["risk_plan_id"])
    input_hash = str(plan["input_hash"])
    canonical_symbol = symbol.strip().upper()
    if len(canonical_symbol) != 6 or not canonical_symbol.isalnum():
        raise ValueError("symbol must be a six-character market symbol")
    quantity_cap = Decimal(str(plan["quantity_cap"])).quantize(
        Decimal("1"), rounding=ROUND_DOWN
    )
    current_quantity = Decimal(str(plan["current_quantity"])).quantize(
        Decimal("1"), rounding=ROUND_DOWN
    )
    quantity = min(quantity_cap, current_quantity)
    if quantity <= 0:
        raise ValueError("an ACTIVE exit plan requires a positive protected position")
    take_quantity = max(
        Decimal(1),
        (quantity * Decimal("0.50")).quantize(Decimal("1"), rounding=ROUND_DOWN),
    )
    evaluation = {
        "clock": "QUOTE",
        "market_closed_policy": "REJECT_TRIGGER",
        "max_data_age_seconds": 30,
    }

    def leg(name: str, operator: str, price: Decimal, shares: Decimal) -> RiskPlanRuleCandidate:
        return RiskPlanRuleCandidate(
            leg=name,
            client_request_id=_key(plan_id, input_hash, name),
            raw_instruction=(
                f"Risk Plan {plan_id} {name}: {canonical_symbol} LAST_PRICE {operator} {price}; "
                f"PAPER SELL {shares} shares"
            ),
            candidate={
                "symbol": canonical_symbol,
                "condition": _condition(operator, price),
                "action": {
                    "side": "SELL",
                    "sizing": {"type": "FIXED_SHARES", "value": str(shares)},
                    "order_type": "MARKET",
                    "time_in_force": "DAY",
                },
                "evaluation": evaluation,
                "expires_at": str(plan["expires_at"]),
            },
        )

    candidates = (
        leg("STOP", "LTE", Decimal(str(plan["stop_price"])), quantity),
        leg(
            "TAKE_PROFIT",
            "GTE",
            Decimal(str(plan["take_profit_price"])),
            take_quantity,
        ),
    )
    for item in candidates:
        ExpressionNode.model_validate(item.candidate["condition"])
        RuleAction.model_validate(item.candidate["action"])
        EvaluationPolicy.model_validate(item.candidate["evaluation"])

    return RiskPlanTradingBundle(
        risk_plan_id=plan_id,
        source_input_hash=input_hash,
        candidates=candidates,
    )


__all__ = ["RiskPlanTradingBundle", "build_paper_rule_bundle"]
