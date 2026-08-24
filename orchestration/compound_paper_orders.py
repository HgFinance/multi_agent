"""Small, deterministic grammar for one safe compound PAPER request.

This module only recognizes the explicitly supported shape:

    <instrument> <shares>주 시장가 매수 ... 그리고
    <price>원 초과/넘으면 <shares>주 매도 ...

It does not submit orders.  The existing authenticated PAPER order and
conditional-rule paths remain the authorities for admission and execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from orchestration.conditional_rules import (
    ActionSide,
    EvaluationClock,
    EvaluationPolicy,
    ExpressionNode,
    ExpressionType,
    RuleAction,
    SizingPolicy,
    SizingType,
)
from orchestration.conditional_rules.contracts import ValueUnit
from orchestration.contracts.user_paper_order import (
    CandidateDecision,
    HermesOrderCandidate,
)
from orchestration.user_order_language import (
    deterministic_order_candidate,
    verify_order_candidate,
)


_COMPOUND_SPLIT = re.compile(r"(?:\.|;)?\s*그리고\s*", re.IGNORECASE)
_DISCORD_PREFIX = re.compile(r"^(?:<@!?\d+>|@[^\s]+)\s*")
_TRIGGER = re.compile(
    r"^(?P<price>[1-9]\d{0,2}(?:,\d{3})*|[1-9]\d*)\s*원?\s*"
    r"(?P<operator>초과|넘으면|이상이면|이상(?:일\s*때)?)\s*"
    r"(?:즉시\s*)?(?P<quantity>[1-9]\d{0,2}(?:,\d{3})*|[1-9]\d*)\s*"
    r"(?:주|주식|개)\s*매도(?:해\s*(?:줘|주세요|줘요)|해줘|해주세요|해)?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CompoundPaperOrderPlan:
    """The two bounded instructions that existing authorities will execute."""

    immediate_instruction: str
    conditional_instruction: str
    instrument_mention: str
    immediate_quantity: int
    conditional_quantity: int
    trigger_price: Decimal
    trigger_operator: str
    immediate_candidate: HermesOrderCandidate


def _integer(value: str) -> int:
    return int(value.replace(",", ""))


def _clean_query(raw_text: str) -> str:
    value = " ".join(str(raw_text or "").strip().split())
    value = _DISCORD_PREFIX.sub("", value, count=1)
    return value.strip()


def parse_compound_paper_order(raw_text: str) -> CompoundPaperOrderPlan | None:
    """Return a plan only for one immediate BUY followed by one SELL trigger."""

    normalized = _clean_query(raw_text)
    parts = _COMPOUND_SPLIT.split(normalized, maxsplit=1)
    if len(parts) != 2:
        return None
    immediate, conditional = (part.strip(" .") for part in parts)
    if not immediate or not conditional:
        return None

    candidate = deterministic_order_candidate(immediate)
    if candidate is None or candidate.decision is not CandidateDecision.EXECUTE:
        return None
    verified = verify_order_candidate(immediate, candidate)
    if getattr(verified, "decision", None) is not CandidateDecision.EXECUTE:
        return None
    if candidate.side is None or candidate.side.value != "BUY":
        return None
    if candidate.quantity is None or candidate.instrument_mention is None:
        return None
    if candidate.order_type is None or candidate.order_type.value != "MARKET":
        return None

    trigger = _TRIGGER.fullmatch(conditional)
    if trigger is None:
        return None
    trigger_price = Decimal(trigger.group("price").replace(",", ""))
    conditional_quantity = _integer(trigger.group("quantity"))
    immediate_quantity = int(candidate.quantity)
    if conditional_quantity != immediate_quantity:
        return None

    operator = "GTE" if trigger.group("operator").startswith("이상") else "GT"
    return CompoundPaperOrderPlan(
        immediate_instruction=immediate,
        conditional_instruction=(
            f"{candidate.instrument_mention} {trigger_price}원 "
            f"{'이상' if operator == 'GTE' else '초과'} 시 "
            f"{conditional_quantity}주 시장가 매도"
        ),
        instrument_mention=candidate.instrument_mention,
        immediate_quantity=immediate_quantity,
        conditional_quantity=conditional_quantity,
        trigger_price=trigger_price,
        trigger_operator=operator,
        immediate_candidate=candidate,
    )


def build_compound_conditional_candidate(
    plan: CompoundPaperOrderPlan,
    *,
    expires_at=None,
):
    """Build the validated-shape candidate; resolution/authority remain upstream."""

    condition = ExpressionNode(
        type=ExpressionType.COMPARISON,
        operator=plan.trigger_operator,
        left=ExpressionNode(type=ExpressionType.MARKET, field="LAST_PRICE"),
        right=ExpressionNode(
            type=ExpressionType.LITERAL,
            value=plan.trigger_price,
            unit=ValueUnit.PRICE,
        ),
    )
    return {
        "symbol": plan.instrument_mention,
        "condition": condition,
        "action": RuleAction(
            side=ActionSide.SELL,
            sizing=SizingPolicy(
                type=SizingType.FIXED_SHARES,
                value=Decimal(plan.conditional_quantity),
            ),
            order_type="MARKET",
        ),
        "evaluation": EvaluationPolicy(clock=EvaluationClock.QUOTE),
        "expires_at": expires_at,
    }


__all__ = [
    "CompoundPaperOrderPlan",
    "build_compound_conditional_candidate",
    "parse_compound_paper_order",
]
