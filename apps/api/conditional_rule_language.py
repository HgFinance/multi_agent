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
_KRW_INTEGER = r"(?:[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*)"
# A bare ``100만원`` is only a notional when it is grammatically tied to the
# order action.  This keeps a trigger price such as ``7만원을 넘으면`` from
# being mistaken for the later order's sizing while accepting the natural
# ``100만원 시장가 매수`` form.
_EXPLICIT_KRW_NOTIONAL = re.compile(
    rf"(?P<amount>{_KRW_INTEGER})\s*(?P<man>만\s*)?원"
    r"(?:\s*(?:어치|만큼))?(?:을|를)?\s*"
    r"(?:시장가(?:로|에)?\s*)?(?:로\s*)?"
    r"(?:매수|매도|buy|sell)",
    re.IGNORECASE,
)
_RETURN_MOVE = re.compile(r"\d+(?:\.\d+)?\s*%.{0,8}(?:상승|하락|오르면|내리면)")
_ENTRY_BASELINE = re.compile(r"(?:평균\s*)?(?:매입가|매수가|평단)")
_TIMEFRAME = re.compile(
    r"(?:1|3|5|10|15|30|60)\s*분봉|1\s*시간봉|일봉"
)

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
    r"상승\s*시|하락\s*시|트레일링|추적\s*손절|고점\s*(?:대비|에서)|" + _SUPPORTED_INDICATOR_PATTERN + r")",
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


def _explicit_krw_notional_values(raw_instruction: str) -> tuple[int, ...]:
    """Extract integer KRW amounts explicitly bound to an order action.

    This is an ambiguity validator, not an order parser: it only verifies that
    a structured NOTIONAL_KRW candidate repeats an exact source amount.
    """

    values: list[int] = []
    for match in _EXPLICIT_KRW_NOTIONAL.finditer(raw_instruction):
        amount = int(match.group("amount").replace(",", ""))
        if match.group("man") is not None:
            amount *= 10_000
        values.append(amount)
    return tuple(values)


def clarification_codes(raw_instruction: str, rule: ConditionalRuleSpec) -> tuple[str, ...]:
    normalized = " ".join(raw_instruction.strip().split())
    codes: list[str] = []
    nodes = tuple(_walk(rule.condition))

    if rule.action.sizing.type.value == "POSITION_PERCENT":
        if _PERCENT_SELL.search(normalized) and not _EXPLICIT_POSITION_PERCENT.search(normalized):
            codes.append("AMBIGUOUS_POSITION_PERCENT")
    if rule.action.sizing.type.value == "NOTIONAL_KRW":
        expected_notional = int(rule.action.sizing.value or 0)
        source_notionals = _explicit_krw_notional_values(normalized)
        if not source_notionals:
            codes.append("NOTIONAL_AMOUNT_NOT_IN_INSTRUCTION")
        elif expected_notional not in source_notionals:
            codes.append("NOTIONAL_AMOUNT_MISMATCH")

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
    if any(
        node.type is ExpressionType.TIME
        and node.field == "OBSERVED_AT_EPOCH_SECONDS"
        for node in nodes
    ):
        assumptions.extend(
            (
                "SERVER_ADMISSION_TIME_ANCHORED",
                "FIVE_MINUTE_EXECUTION_WINDOW",
            )
        )
    if any(
        node.type is ExpressionType.TIME
        and node.field == "KST_SECONDS_SINCE_MIDNIGHT"
        for node in nodes
    ):
        assumptions.extend(("KST_TIME_WINDOW", "MARKET_SESSION_GUARD"))
    if any(node.type is ExpressionType.TRAILING_STOP for node in nodes):
        assumptions.extend(
            (
                "DURABLE_HIGH_WATERMARK",
                "TRAILING_STOP_SELL_ONLY",
                "FRESH_QUOTE_ONLY",
            )
        )
    if rule.action.sizing.type.value == "NOTIONAL_KRW":
        assumptions.extend(
            (
                "KRW_NOTIONAL_MAXIMUM",
                "FRESH_PRICE_AND_LOT_SIZE_AT_EXECUTION",
                "TRADING_QUOTE_CAP_RECHECK",
            )
        )
    if indicator_timeframes == {"1D"} and not _TIMEFRAME.search(normalized):
        assumptions.append("DEFAULTED_TO_DAILY_COMPLETED_BAR")
    return tuple(assumptions)


def condition_overview(rule: ConditionalRuleSpec) -> dict[str, object]:
    """Stable display metadata for a confirmed compound condition.

    The raw AST remains canonical, but UI/Discord should not have to reverse
    engineer it just to tell a user which frames and indicators are involved.
    This is descriptive only and is never fed back into validation or runtime.
    """

    nodes = tuple(_walk(rule.condition))
    order = {"1M": 1, "3M": 3, "5M": 5, "10M": 10, "15M": 15, "30M": 30, "1H": 60, "1D": 1440}
    timeframes = sorted(
        {
            node.timeframe.value
            for node in nodes
            if node.type is ExpressionType.INDICATOR and node.timeframe is not None
        },
        key=lambda value: order[value],
    )
    indicators: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for node in nodes:
        if node.type is not ExpressionType.INDICATOR or node.timeframe is None:
            continue
        parameters = tuple(
            sorted((str(key).upper(), str(value)) for key, value in (node.parameters or {}).items())
        )
        identity = (node.name, node.output or "VALUE", node.timeframe.value, parameters)
        if identity in seen:
            continue
        seen.add(identity)
        indicators.append(
            {
                "name": node.name,
                "output": node.output or "VALUE",
                "timeframe": node.timeframe.value,
                "parameters": dict(parameters),
            }
        )
    has_cross = any(node.type is ExpressionType.CROSS for node in nodes)
    trailing = next(
        (node for node in nodes if node.type is ExpressionType.TRAILING_STOP), None
    )
    time_window_kst: list[str] = []
    for node in nodes:
        if (
            node.type is not ExpressionType.COMPARISON
            or node.left is None
            or node.right is None
            or node.left.type is not ExpressionType.TIME
            or node.left.field != "KST_SECONDS_SINCE_MIDNIGHT"
            or node.right.type is not ExpressionType.LITERAL
            or isinstance(node.right.value, bool)
        ):
            continue
        try:
            seconds = int(str(node.right.value))
        except (TypeError, ValueError):  # semantic validation owns rejection.
            continue
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        time_window_kst.append(f"{node.operator} {hours:02d}:{minutes:02d}:{seconds:02d}")
    result: dict[str, object] = {
        "trigger_style": (
            "TRAILING" if trailing is not None else "EDGE" if has_cross else "LEVEL"
        ),
        "referenced_timeframes": timeframes,
        "indicators": indicators,
        "evaluation_boundary": (
            "LATEST_COMPLETED_BAR_AT_OR_BEFORE_PRIMARY_CLOSE"
            if rule.evaluation.clock.value == "BAR_CLOSE"
            else "FRESH_QUOTE"
        ),
        "time_window_kst": time_window_kst,
    }
    if trailing is not None:
        parameters = {
            str(key).upper(): value for key, value in (trailing.parameters or {}).items()
        }
        result["trailing_stop"] = {
            "drawdown_ratio": str(parameters["DRAWDOWN"]),
            "activation_return_ratio": (
                str(parameters["ACTIVATION_RETURN"])
                if parameters.get("ACTIVATION_RETURN") is not None
                else None
            ),
            "watermark": "HIGHEST_FRESH_QUOTE_SINCE_ACTIVE",
            "expected_position_quantity": (
                str(parameters["EXPECTED_POSITION_QUANTITY"])
                if parameters.get("EXPECTED_POSITION_QUANTITY") is not None
                else None
            ),
        }
    return result


__all__ = [
    "clarification_codes",
    "condition_overview",
    "looks_like_conditional_paper_rule",
    "preview_assumptions",
]
