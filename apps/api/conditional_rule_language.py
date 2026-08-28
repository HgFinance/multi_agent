"""Fail-closed ambiguity checks for conditional-rule previews.

Hermes supplies a structured candidate; this module does not calculate market
values or decide triggers.  It only prevents a preview from silently assigning
meaning to known ambiguous Korean trading phrases.
"""

from __future__ import annotations

import re

from orchestration.conditional_rules import ConditionalRuleSpec, ExpressionType, list_supported_indicators
from orchestration.user_order_language import RELATIVE_DELAY_SUFFIX


_EXPLICIT_POSITION_PERCENT = re.compile(
    r"(?:보유\s*(?:수량|주식|분)|보유분)(?:의|에서)?\s*\d+(?:\.\d+)?\s*%"
)
_PERCENT_SELL = re.compile(r"(?:비중|보유|포지션)?.{0,12}\d+(?:\.\d+)?\s*%.{0,8}매도")
_RETURN_MOVE = re.compile(r"\d+(?:\.\d+)?\s*%.{0,8}(?:상승|하락|오르면|내리면)")
_ENTRY_BASELINE = re.compile(r"(?:평균\s*)?(?:매입가|매수가|평단)")
_TIMEFRAME = re.compile(r"(?:1|3|5|10|15|30|60)\s*분봉|(?:일봉|주봉|월봉)")

_SUPPORTED_INDICATOR_TERMS = sorted(
    {
        str(term)
        for item in list_supported_indicators()
        for term in (item["name"], *item["aliases"])
        if term
    },
    key=len,
    reverse=True,
)
_SUPPORTED_INDICATOR_PATTERN = "|".join(re.escape(term) for term in _SUPPORTED_INDICATOR_TERMS)

_ORDER_ACTION = re.compile(r"(?:매수|매도|buy|sell)", re.IGNORECASE)
_CONDITIONAL_TRIGGER = re.compile(
    r"(?:조건\s*주문|이면|라면|할\s*때|경우|이상|이하|초과|미만|"
    r"높으면|낮으면|오르면|내리면|넘으면|떨어지면|도달하면|닿으면|터치하면|"
    r"(?:상향\s*)?돌파|(?:하향\s*)?이탈|골든\s*크로스|데드\s*크로스|"
    r"이동\s*평균|이평|(?:\d+\s*)?일선|볼린저|거래량|평단|매입가|"
    r"상승\s*시|하락\s*시|" + _SUPPORTED_INDICATOR_PATTERN + r")",
    re.IGNORECASE,
)
_RELATIVE_TIME_TRIGGER = re.compile(
    r"(?<![\w,])(?:[1-9]\d*|[일이삼사오육칠팔구십한두세네열스물서른마흔쉰예순일흔여든아흔\s]+)"
    rf"\s*(?:초|분|시간)\s*{RELATIVE_DELAY_SUFFIX}(?!\w)"
)
# A wall-clock instant is as much a trigger as a price or an indicator, but
# only the relative form ("4분 뒤") used to be recognized.  "15:15 되면 매수"
# therefore fell through to the immediate-order lane, which refuses it for
# lacking the conditional-rule marker, and the user saw a flat rejection with
# no rule created (2026-08-27).  ``시간`` is excluded so the relative grammar
# above keeps owning "3시간 뒤".
_ABSOLUTE_TIME_TRIGGER = re.compile(
    r"(?<![\d:])(?:오전|오후|아침|저녁|낮)?\s*"
    r"(?:"
    r"(?:[01]?\d|2[0-3])\s*:\s*[0-5]\d(?![\d:])"
    r"|(?:[01]?\d|2[0-3])\s*시(?!\s*간)(?:\s*[0-5]?\d\s*분)?"
    r")"
)
_NON_BINDING_CONDITIONAL = re.compile(
    r"(?:\?|해도\s*될|해야\s*할|할까|어때|알려\s*줘|설명|추천|"
    r"예시|가정|백테스트|분석\s*해)",
    re.IGNORECASE,
)


def looks_like_conditional_paper_rule(raw_instruction: str) -> bool:
    """Recognize an explicit conditional buy/sell command, fail closed.

    This classifier only selects the isolated Trading-Hermes interpretation
    lane.  It never builds an AST or activates a rule; the MCP schema and the
    deterministic validators remain authoritative.  Advice/questions stay on
    the non-binding CEO path.
    """

    normalized = " ".join(str(raw_instruction or "").strip().split())
    return bool(
        normalized
        and _ORDER_ACTION.search(normalized)
        and (
            _CONDITIONAL_TRIGGER.search(normalized)
            or _RELATIVE_TIME_TRIGGER.search(normalized)
            or _ABSOLUTE_TIME_TRIGGER.search(normalized)
        )
        and not _NON_BINDING_CONDITIONAL.search(normalized)
    )


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
    if any(node.type is ExpressionType.TIME for node in nodes):
        assumptions.extend(
            (
                "SERVER_ADMISSION_TIME_ANCHORED",
                "FIVE_MINUTE_EXECUTION_WINDOW",
            )
        )
    if indicator_timeframes == {"1D"} and not _TIMEFRAME.search(normalized):
        assumptions.append("DEFAULTED_TO_DAILY_COMPLETED_BAR")
    return tuple(assumptions)


__all__ = [
    "clarification_codes",
    "looks_like_conditional_paper_rule",
    "preview_assumptions",
]
