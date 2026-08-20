"""Fail-closed ambiguity checks for conditional-rule previews.

Hermes supplies a structured candidate; this module does not calculate market
values or decide triggers.  It only prevents a preview from silently assigning
meaning to known ambiguous Korean trading phrases.
"""

from __future__ import annotations

import re

from orchestration.conditional_rules import ConditionalRuleSpec, ExpressionType


_EXPLICIT_POSITION_PERCENT = re.compile(
    r"(?:보유\s*(?:수량|주식|분)|보유분)(?:의|에서)?\s*\d+(?:\.\d+)?\s*%"
)
_PERCENT_SELL = re.compile(r"(?:비중|보유|포지션)?.{0,12}\d+(?:\.\d+)?\s*%.{0,8}매도")
_RETURN_MOVE = re.compile(r"\d+(?:\.\d+)?\s*%.{0,8}(?:상승|하락|오르면|내리면)")
_ENTRY_BASELINE = re.compile(r"(?:평균\s*)?(?:매입가|매수가|평단)")
_TIMEFRAME = re.compile(r"(?:1|3|5|10|15|30|60)\s*분봉|(?:일봉|주봉|월봉)")


def _walk(node):
    yield node
    for child in (node.left, node.right, node.operand):
        if child is not None:
            yield from _walk(child)
    for child in node.children or ():
        yield from _walk(child)


def clarification_codes(raw_instruction: str, rule: ConditionalRuleSpec) -> tuple[str, ...]:
    normalized = " ".join(raw_instruction.strip().split())
    codes: list[str] = []
    nodes = tuple(_walk(rule.condition))

    if rule.action.sizing.type.value == "POSITION_PERCENT":
        if _PERCENT_SELL.search(normalized) and not _EXPLICIT_POSITION_PERCENT.search(normalized):
            codes.append("AMBIGUOUS_POSITION_PERCENT")

    uses_average_entry = any(
        node.type is ExpressionType.PORTFOLIO and node.field == "AVG_ENTRY_PRICE"
        for node in nodes
    )
    if uses_average_entry and _RETURN_MOVE.search(normalized) and not _ENTRY_BASELINE.search(normalized):
        codes.append("AMBIGUOUS_RETURN_BASELINE")

    indicator_timeframes = {
        node.timeframe.value
        for node in nodes
        if node.type is ExpressionType.INDICATOR and node.timeframe is not None
    }
    if indicator_timeframes and not _TIMEFRAME.search(normalized):
        if indicator_timeframes != {"1D"}:
            codes.append("TIMEFRAME_NOT_IN_INSTRUCTION")

    return tuple(dict.fromkeys(codes))


def preview_assumptions(raw_instruction: str, rule: ConditionalRuleSpec) -> tuple[str, ...]:
    normalized = " ".join(raw_instruction.strip().split())
    nodes = tuple(_walk(rule.condition))
    indicator_timeframes = {
        node.timeframe.value
        for node in nodes
        if node.type is ExpressionType.INDICATOR and node.timeframe is not None
    }
    assumptions = [
        "PAPER_ONLY",
        "ONE_SHOT",
        "MARKET_CLOSED_REJECTS_WITHOUT_ORDER",
    ]
    if indicator_timeframes == {"1D"} and not _TIMEFRAME.search(normalized):
        assumptions.append("DEFAULTED_TO_DAILY_COMPLETED_BAR")
    return tuple(assumptions)


__all__ = ["clarification_codes", "preview_assumptions"]
