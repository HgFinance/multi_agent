"""Small, deterministic grammars for safe compound PAPER requests.

This module only recognizes the explicitly supported shape:

    <instrument> <shares>주 시장가 매수 ... 그리고
    <price>원 초과/넘으면 <shares>주 매도 ...

or an entry-relative take-profit/stop-loss pair:

    <shares>주 시장가 매수하고 매수가 대비 3% 상승하면 매도하고
    2% 하락하면 매도

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


# "그리고" was the only recognized seam, so the very common
# "…매수하고 …매도해줘" shape fell through to the single-order lane, which
# flags it MULTIPLE_COMMANDS and refuses (2026-08-27).  The verb stays with
# the first leg, so only the connective is consumed.
_COMPOUND_SPLIT = re.compile(
    r"(?:\.|;)?\s*그리고\s*"
    r"|(?<=매수)\s*(?:하고|한\s*뒤(?:에)?|한\s*후(?:에)?|해\s*놓고)\s*"
    r"|(?<=매수)\s*(?:하고|한\s*뒤(?:에)?|한\s*후(?:에)?)\s*(?:그\s*)?(?:다음|후)\s*",
    re.IGNORECASE,
)
_DISCORD_PREFIX = re.compile(r"^(?:<@!?\d+>|@[^\s]+)\s*")
_TRIGGER = re.compile(
    r"^(?P<price>[1-9]\d{0,2}(?:,\d{3})*|[1-9]\d*)\s*원?\s*"
    r"(?P<operator>초과|넘으면|이상이면|이상(?:일\s*때)?)\s*"
    r"(?:즉시\s*)?(?P<quantity>[1-9]\d{0,2}(?:,\d{3})*|[1-9]\d*)\s*"
    r"(?:주|주식|개)\s*매도(?:해\s*(?:줘|주세요|줘요)|해줘|해주세요|해)?\s*$",
    re.IGNORECASE,
)
# "매수가 대비 1% 상승하면 매도" / "매수가 대비 2% 하락하면 매도" - the trigger
# is a percentage away from the entry price rather than an absolute number, so
# no literal price exists to compare against.  Both directions are the same
# shape: take-profit compares upward, stop-loss downward.  The quantity may be
# omitted; the leg then sells exactly what the first leg just bought.
_ENTRY_RELATIVE_TRIGGER = re.compile(
    r"^(?:매수가|매입가|평단가?|매수\s*단가)\s*(?:대비|보다|에서)?\s*"
    r"(?P<percent>\d{1,2}(?:\.\d{1,2})?)\s*%\s*(?:이상\s*)?"
    r"(?:(?P<up>상승|오르|올라가|올라|올랐|익절)"
    r"|(?P<down>하락|떨어지|내리|내려가|빠지|손절))(?:하)?\s*"
    r"(?:면|시|할\s*때|했을\s*때|하면)\s*"
    r"(?:즉시\s*)?"
    r"(?:(?P<quantity>[1-9]\d{0,2}(?:,\d{3})*|[1-9]\d*)\s*(?:주|주식|개)\s*)?"
    r"(?:시장가로?\s*)?매도(?:해\s*(?:줘|주세요|줘요)|해줘|해주세요|해)?\s*$",
    re.IGNORECASE,
)
# A two-sided entry exit is deliberately parsed separately from the single
# trigger above.  The entry price is unambiguous because this grammar is only
# reached after one immediate BUY.  The first leg must still name that baseline;
# the second may omit it only because it shares the first leg's entry price.
_ENTRY_RELATIVE_EXIT_FRAGMENT = re.compile(
    r"(?:(?P<baseline>(?:매수가|매입가|평단가?|매수\s*단가)\s*"
    r"(?:대비|보다|에서)?)\s*)?"
    r"(?P<percent>\d{1,2}(?:\.\d{1,2})?)\s*%\s*(?:이상\s*)?"
    r"(?:(?P<up>상승|오르|올라가|올라|올랐|익절)"
    r"|(?P<down>하락|떨어지|내리|내려가|빠지|손절))(?:하)?\s*"
    r"(?:면|시|할\s*때|했을\s*때|하면)\s*"
    r"(?:즉시\s*)?"
    r"(?:(?P<quantity>[1-9]\d{0,2}(?:,\d{3})*|[1-9]\d*)\s*"
    r"(?:주|주식|개)\s*)?"
    r"(?:시장가로?\s*)?매도(?:해\s*(?:줘|주세요|줘요)|해줘|해주세요|해)?",
    re.IGNORECASE,
)
_ENTRY_BRACKET_FILLER = re.compile(
    r"^(?:[\s,.;]*(?:(?:그리고|하고|또는|및|or|oco(?:로)?|"
    r"한\s*쪽\s*실행\s*시\s*나머지\s*취소)|(?:해\s*(?:줘|주세요|줘요)|해줘|해주세요)))*[\s,.;]*$",
    re.IGNORECASE,
)
# A delayed trailing exit is only meaningful after the first PAPER buy fills.
# It deliberately names both the entry-return arming level and the high-water
# drawdown, avoiding an implicit trailing distance or a guessed cost basis.
_ENTRY_TRAILING_TRIGGER = re.compile(
    r"^(?:매수가|매입가|평단가?|매수\s*단가)\s*(?:대비|보다|에서)?\s*"
    r"(?P<activation>\d{1,2}(?:\.\d{1,2})?)\s*%\s*"
    r"(?:수익(?:이\s*)?(?:난|나면)?|상승(?:한|하면)|오른|올라간)\s*"
    r"(?:뒤|이후|후|부터)?\s*(?:그\s*)?고점\s*(?:대비|에서)?\s*"
    r"(?P<drawdown>\d{1,2}(?:\.\d{1,2})?)\s*%\s*"
    r"(?:하락|떨어지|내리|내려가|빠지)(?:하)?\s*"
    r"(?:면|시|할\s*때|했을\s*때|하면)\s*(?:즉시\s*)?"
    r"(?:(?P<quantity>[1-9]\d{0,2}(?:,\d{3})*|[1-9]\d*)\s*"
    r"(?:주|주식|개)\s*)?"
    r"(?:시장가로?\s*)?매도(?:해\s*(?:줘|주세요|줘요)|해줘|해주세요|해)?\s*$",
    re.IGNORECASE,
)
# A compound exit is normally DAY-only.  This narrow suffix gives a user an
# explicit, reviewable way to keep that *exit* alive across sessions without
# turning every omitted expiry into an unattended multi-day instruction.
# ``다음 거래일까지`` means the current (or next) eligible session plus one
# subsequent session, hence two KRX regular-session closes from activation.
_ENTRY_LIFETIME_SUFFIX = re.compile(
    r"(?:[\s,.;]*(?:(?:최대|최장)\s*)?"
    r"(?P<days>[1-9]|1\d|20)\s*거래일\s*(?:동안\s*)?"
    r"(?:추적(?:해)?|유지(?:해)?|보유)?\s*|[\s,.;]*다음\s*거래일\s*까지\s*)$",
    re.IGNORECASE,
)
_ANALYSIS_THEN_CONDITIONAL = re.compile(
    r"^(?:research|리서치)\s*분석\s*후\s*(?P<conditional>.+?)\s*$",
    re.IGNORECASE,
)
_PRICE_TRIGGER = re.compile(
    r"(?P<instrument>.+?)\s+"
    r"(?P<price>[1-9]\d{0,2}(?:,\d{3})*|[1-9]\d*)\s*원?\s*"
    r"(?:초과|넘으면|이상이면|이상(?:일\s*때)?)",
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
    trigger_price: Decimal | None
    trigger_operator: str
    immediate_candidate: HermesOrderCandidate
    # Set instead of `trigger_price` when the trigger is a percentage above the
    # entry price.  Exactly one of the two is populated.
    trigger_entry_percent: Decimal | None = None
    # A two-sided entry exit has exactly one positive take-profit and one
    # negative stop-loss percentage.  It compiles into one LOGICAL OR SELL
    # rule, not two independent sell directives.
    entry_exit_percents: tuple[Decimal, ...] = ()
    trailing_drawdown_percent: Decimal | None = None
    # The requested maximum life of the protective exit begins only after the
    # immediate BUY is fully filled.  ``None`` preserves the DAY-order default.
    exit_lifetime_trading_days: int | None = None

    @property
    def is_entry_exit_bracket(self) -> bool:
        return len(self.entry_exit_percents) == 2

    @property
    def is_entry_trailing_stop(self) -> bool:
        return self.trailing_drawdown_percent is not None


@dataclass(frozen=True)
class AnalysisThenConditionalPaperOrderPlan:
    """A research prerequisite followed by the existing Trading rule lane.

    This is intentionally a routing plan only.  It does not create a rule or
    an order.  The supervisor creates the existing Trading interpretation card
    only after the requested Research primary reaches a usable terminal state.
    """

    analysis_instruction: str
    conditional_instruction: str


def _integer(value: str) -> int:
    return int(value.replace(",", ""))


def _clean_query(raw_text: str) -> str:
    value = " ".join(str(raw_text or "").strip().split())
    value = _DISCORD_PREFIX.sub("", value, count=1)
    return value.strip()


def _entry_exit_quantity(value: str | None, *, immediate_quantity: int) -> int:
    return immediate_quantity if value is None else _integer(value)


def _strip_entry_lifetime(conditional: str) -> tuple[str, int | None]:
    """Remove the one explicit entry-exit lifetime suffix, if present."""

    match = _ENTRY_LIFETIME_SUFFIX.search(conditional)
    if match is None:
        return conditional, None
    # The alternate Korean phrase is deliberately exact; it is not a guess at
    # a calendar date and always has the same two-session meaning.
    days = 2 if "다음" in match.group(0) else int(match.group("days"))
    return conditional[: match.start()].strip(" ,.;"), days


def _parse_entry_exit_bracket(
    conditional: str,
    *,
    immediate_instruction: str,
    immediate_quantity: int,
    instrument_mention: str,
    immediate_candidate: HermesOrderCandidate,
    exit_lifetime_trading_days: int | None,
) -> CompoundPaperOrderPlan | None:
    """Parse one take-profit plus one stop-loss into a single exit rule.

    The residual-text check is important: two familiar words elsewhere in a
    sentence must not be silently elevated into an autonomous exit bracket.
    """

    matches = tuple(_ENTRY_RELATIVE_EXIT_FRAGMENT.finditer(conditional))
    if len(matches) != 2:
        return None
    if matches[0].group("baseline") is None:
        return None
    residual = _ENTRY_RELATIVE_EXIT_FRAGMENT.sub("", conditional)
    if _ENTRY_BRACKET_FILLER.fullmatch(residual) is None:
        return None

    signed: list[Decimal] = []
    for match in matches:
        percent = Decimal(match.group("percent"))
        if not (Decimal("0") < percent <= Decimal("50")):
            return None
        quantity = _entry_exit_quantity(
            match.group("quantity"), immediate_quantity=immediate_quantity
        )
        if quantity != immediate_quantity:
            return None
        signed.append(-percent if match.group("down") is not None else percent)

    # This phase supports precisely one take-profit and one stop-loss.  Two
    # take-profits, two stops, or duplicate thresholds need a staged position
    # state machine and must remain outside this compact grammar.
    if len({value > 0 for value in signed}) != 2 or len(set(signed)) != 2:
        return None
    take_profit, stop_loss = sorted(signed, reverse=True)
    return CompoundPaperOrderPlan(
        immediate_instruction=immediate_instruction,
        conditional_instruction=(
            f"{instrument_mention} 매수가 대비 {take_profit}% 이상 상승 시 "
            f"{immediate_quantity}주 시장가 매도 또는 매수가 대비 "
            f"{abs(stop_loss)}% 이상 하락 시 {immediate_quantity}주 시장가 매도"
        ),
        instrument_mention=instrument_mention,
        immediate_quantity=immediate_quantity,
        conditional_quantity=immediate_quantity,
        trigger_price=None,
        trigger_operator="GTE",
        immediate_candidate=immediate_candidate,
        trigger_entry_percent=take_profit,
        entry_exit_percents=(take_profit, stop_loss),
        exit_lifetime_trading_days=exit_lifetime_trading_days,
    )


def _parse_entry_trailing_stop(
    conditional: str,
    *,
    immediate_instruction: str,
    immediate_quantity: int,
    instrument_mention: str,
    immediate_candidate: HermesOrderCandidate,
    exit_lifetime_trading_days: int | None,
) -> CompoundPaperOrderPlan | None:
    """Parse one fill-gated entry-relative trailing SELL exit."""

    match = _ENTRY_TRAILING_TRIGGER.fullmatch(conditional)
    if match is None:
        return None
    activation = Decimal(match.group("activation"))
    drawdown = Decimal(match.group("drawdown"))
    if not (
        Decimal("0") <= activation <= Decimal("50")
        and Decimal("0") < drawdown <= Decimal("50")
    ):
        return None
    quantity = _entry_exit_quantity(
        match.group("quantity"), immediate_quantity=immediate_quantity
    )
    if quantity != immediate_quantity:
        return None
    return CompoundPaperOrderPlan(
        immediate_instruction=immediate_instruction,
        conditional_instruction=(
            f"{instrument_mention} 매수가 대비 {activation}% 수익 이후 "
            f"고점 대비 {drawdown}% 하락 시 {immediate_quantity}주 시장가 매도"
        ),
        instrument_mention=instrument_mention,
        immediate_quantity=immediate_quantity,
        conditional_quantity=immediate_quantity,
        trigger_price=None,
        trigger_operator="TRAILING_STOP",
        immediate_candidate=immediate_candidate,
        trigger_entry_percent=activation,
        trailing_drawdown_percent=drawdown,
        exit_lifetime_trading_days=exit_lifetime_trading_days,
    )


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

    immediate_quantity = int(candidate.quantity)

    conditional, exit_lifetime_trading_days = _strip_entry_lifetime(conditional)
    if not conditional:
        return None

    bracket = _parse_entry_exit_bracket(
        conditional,
        immediate_instruction=immediate,
        immediate_quantity=immediate_quantity,
        instrument_mention=candidate.instrument_mention,
        immediate_candidate=candidate,
        exit_lifetime_trading_days=exit_lifetime_trading_days,
    )
    if bracket is not None:
        return bracket

    trailing = _parse_entry_trailing_stop(
        conditional,
        immediate_instruction=immediate,
        immediate_quantity=immediate_quantity,
        instrument_mention=candidate.instrument_mention,
        immediate_candidate=candidate,
        exit_lifetime_trading_days=exit_lifetime_trading_days,
    )
    if trailing is not None:
        return trailing

    trigger = _TRIGGER.fullmatch(conditional)
    if trigger is not None:
        conditional_quantity = _integer(trigger.group("quantity"))
        if conditional_quantity != immediate_quantity:
            return None
        trigger_price = Decimal(trigger.group("price").replace(",", ""))
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
            exit_lifetime_trading_days=exit_lifetime_trading_days,
        )

    entry = _ENTRY_RELATIVE_TRIGGER.fullmatch(conditional)
    if entry is None:
        return None
    percent = Decimal(entry.group("percent"))
    if not (Decimal("0") < percent <= Decimal("50")):
        return None
    raw_quantity = entry.group("quantity")
    # Omitting the quantity means "sell what the first leg just bought"; any
    # explicit number still has to match, so the pair can never go net short.
    conditional_quantity = (
        immediate_quantity if raw_quantity is None else _integer(raw_quantity)
    )
    if conditional_quantity != immediate_quantity:
        return None
    falling = entry.group("down") is not None
    return CompoundPaperOrderPlan(
        immediate_instruction=immediate,
        # "매수가" must survive into the generated instruction: the preview
        # flags AMBIGUOUS_RETURN_BASELINE when an entry-price rule cannot point
        # at the baseline word in its own text.
        conditional_instruction=(
            f"{candidate.instrument_mention} 매수가 대비 {percent}% 이상 "
            f"{'하락' if falling else '상승'} 시 "
            f"{conditional_quantity}주 시장가 매도"
        ),
        instrument_mention=candidate.instrument_mention,
        immediate_quantity=immediate_quantity,
        conditional_quantity=conditional_quantity,
        trigger_price=None,
        trigger_operator="LTE" if falling else "GTE",
        immediate_candidate=candidate,
        trigger_entry_percent=-percent if falling else percent,
        exit_lifetime_trading_days=exit_lifetime_trading_days,
    )


def parse_analysis_then_conditional_paper_order(
    raw_text: str,
) -> AnalysisThenConditionalPaperOrderPlan | None:
    """Recognize ``Research analysis first, then conditional PAPER order``.

    The analysis clause is kept separate from the conditional instruction so
    the CEO planner cannot accidentally route the whole request to the direct
    Trading fast lane.  Recognition is deliberately narrow and fail-closed;
    unsupported language remains on the ordinary conditional path.
    """

    normalized = _clean_query(raw_text)
    match = _ANALYSIS_THEN_CONDITIONAL.fullmatch(normalized)
    if match is None:
        return None
    conditional = match.group("conditional").strip(" .")
    trigger = _PRICE_TRIGGER.search(conditional)
    if trigger is None or "매도" not in conditional:
        return None
    instrument = trigger.group("instrument").strip(" ,")
    if not instrument:
        return None
    return AnalysisThenConditionalPaperOrderPlan(
        analysis_instruction=(
            f"{instrument}에 대한 Research 관점의 투자 분석을 수행하고 "
            "분석 근거와 불확실성을 정리해 주세요."
        ),
        conditional_instruction=conditional,
    )


def build_compound_conditional_candidate(
    plan: CompoundPaperOrderPlan,
    *,
    expires_at=None,
):
    """Build the validated-shape candidate; resolution/authority remain upstream."""

    if plan.is_entry_trailing_stop:
        condition = ExpressionNode(
            type=ExpressionType.TRAILING_STOP,
            parameters={
                "DRAWDOWN": plan.trailing_drawdown_percent / Decimal(100),
                "ACTIVATION_RETURN": (plan.trigger_entry_percent or Decimal(0))
                / Decimal(100),
                "EXPECTED_POSITION_QUANTITY": Decimal(plan.immediate_quantity),
            },
        )
    elif plan.entry_exit_percents:
        conditions = []
        for percent in plan.entry_exit_percents:
            threshold = ExpressionNode(
                type=ExpressionType.ARITHMETIC,
                operator="MUL",
                left=ExpressionNode(
                    type=ExpressionType.PORTFOLIO, field="AVG_ENTRY_PRICE"
                ),
                right=ExpressionNode(
                    type=ExpressionType.LITERAL,
                    value=Decimal(1) + percent / Decimal(100),
                    unit=ValueUnit.NUMBER,
                ),
            )
            conditions.append(
                ExpressionNode(
                    type=ExpressionType.COMPARISON,
                    operator="GTE" if percent > 0 else "LTE",
                    left=ExpressionNode(type=ExpressionType.MARKET, field="LAST_PRICE"),
                    right=threshold,
                )
            )
        exit_condition = ExpressionNode(
            type=ExpressionType.LOGICAL,
            operator="OR",
            children=tuple(conditions),
        )
        # AVG_ENTRY_PRICE is the canonical account cost basis.  Restrict this
        # entry-originated bracket to a position consisting of exactly the
        # just-filled entry quantity; otherwise an older holding in the same
        # symbol would silently change the user's "매수가 대비" baseline.
        condition = ExpressionNode(
            type=ExpressionType.LOGICAL,
            operator="AND",
            children=(
                ExpressionNode(
                    type=ExpressionType.COMPARISON,
                    operator="EQ",
                    left=ExpressionNode(
                        type=ExpressionType.PORTFOLIO,
                        field="POSITION_QUANTITY",
                    ),
                    right=ExpressionNode(
                        type=ExpressionType.LITERAL,
                        value=Decimal(plan.immediate_quantity),
                        unit=ValueUnit.SHARES,
                    ),
                ),
                exit_condition,
            ),
        )
    elif plan.trigger_entry_percent is not None:
        # LAST_PRICE >= AVG_ENTRY_PRICE * (1 + pct/100).  The entry price is
        # read from the book at evaluation time, so the rule stays correct even
        # if the first leg fills at a different price than quoted.
        threshold = ExpressionNode(
            type=ExpressionType.ARITHMETIC,
            operator="MUL",
            left=ExpressionNode(type=ExpressionType.PORTFOLIO, field="AVG_ENTRY_PRICE"),
            right=ExpressionNode(
                type=ExpressionType.LITERAL,
                value=Decimal(1) + plan.trigger_entry_percent / Decimal(100),
                unit=ValueUnit.NUMBER,
            ),
        )
    else:
        threshold = ExpressionNode(
            type=ExpressionType.LITERAL,
            value=plan.trigger_price,
            unit=ValueUnit.PRICE,
        )
    if not plan.entry_exit_percents and not plan.is_entry_trailing_stop:
        condition = ExpressionNode(
            type=ExpressionType.COMPARISON,
            operator=plan.trigger_operator,
            left=ExpressionNode(type=ExpressionType.MARKET, field="LAST_PRICE"),
            right=threshold,
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
        "activation_lifetime_trading_days": plan.exit_lifetime_trading_days,
    }


__all__ = [
    "AnalysisThenConditionalPaperOrderPlan",
    "CompoundPaperOrderPlan",
    "build_compound_conditional_candidate",
    "parse_analysis_then_conditional_paper_order",
    "parse_compound_paper_order",
]
