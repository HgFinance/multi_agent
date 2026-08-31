"""Deterministic verifier for Trading Hermes' PAPER-order candidates.

The language model has no order capability.  It can only propose a strict
``HermesOrderCandidate``.  This module independently re-reads the original
text, rejects non-imperative or ambiguous speech, verifies every evidence span
against the exact original string, and emits an unresolved PAPER directive.

Instrument lookup deliberately does not live here.  ``instrument_mention`` is
the exact user substring and must be resolved by the authenticated caller
against the active KRX reference catalog before any OMS admission attempt.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from orchestration.contracts.user_paper_order import (
    CandidateDecision,
    CanonicalBasketOrderItem,
    CanonicalPlaceBasketPayload,
    CanonicalPlaceOrderPayload,
    DirectiveAction,
    EvidenceField,
    HermesOrderCandidate,
    NotOrder,
    OrderClarification,
    OrderLanguageResult,
    OrderReasonCode,
    OrderSide,
    OrderType,
    TextEvidence,
    VerifiedPaperDirective,
)

MAX_TEXT_LENGTH = 500
MAX_QUANTITY = 10**18 - 1
MAX_PRICE = 10**24 - 1

_ASCII_INTEGER = r"(?:[1-9]\d*)"
_GROUPED_INTEGER = r"(?:[1-9]\d{0,2}(?:,\d{3})+)"
_ARABIC_INTEGER = rf"(?:{_GROUPED_INTEGER}|{_ASCII_INTEGER})"

_SINO_DIGITS = {
    "일": 1,
    "이": 2,
    "삼": 3,
    "사": 4,
    "오": 5,
    "육": 6,
    "칠": 7,
    "팔": 8,
    "구": 9,
}
_NATIVE_ONES = {
    "한": 1,
    "두": 2,
    "세": 3,
    "네": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
}
_NATIVE_TENS = {
    "열": 10,
    "스물": 20,
    "서른": 30,
    "마흔": 40,
    "쉰": 50,
    "예순": 60,
    "일흔": 70,
    "여든": 80,
    "아흔": 90,
}


def _sino_text(value: int) -> str:
    reverse = {number: text for text, number in _SINO_DIGITS.items()}
    if value < 10:
        return reverse[value]
    tens, ones = divmod(value, 10)
    return ("" if tens == 1 else reverse[tens]) + "십" + (
        reverse[ones] if ones else ""
    )


def _native_texts(value: int) -> tuple[str, ...]:
    reverse_ones = {number: text for text, number in _NATIVE_ONES.items()}
    reverse_tens = {number: text for text, number in _NATIVE_TENS.items()}
    if value < 10:
        return (reverse_ones[value],)
    tens, ones = divmod(value, 10)
    base = reverse_tens[tens * 10]
    if not ones:
        return (base, "스무") if value == 20 else (base,)
    return (base + reverse_ones[ones],)


_KOREAN_INTEGER_TEXTS = tuple(
    sorted(
        {
            text
            for value in range(1, 100)
            for text in (_sino_text(value), *_native_texts(value))
        },
        key=lambda text: (-len(text), text),
    )
)


def _space_tolerant(text: str) -> str:
    return r"\s*".join(re.escape(character) for character in text)


_KOREAN_INTEGER = "(?:" + "|".join(
    _space_tolerant(text) for text in _KOREAN_INTEGER_TEXTS
) + ")"
_INTEGER_TOKEN = rf"(?:{_ARABIC_INTEGER}|{_KOREAN_INTEGER})"


def parse_strict_positive_integer(
    token: str,
    *,
    max_value: int = MAX_PRICE,
) -> int:
    """Parse an exact positive Arabic or Korean integer.

    Arabic comma grouping must be canonical.  Korean native and Sino-Korean
    counter forms are accepted from 1 through 99; approximate expressions such
    as ``한두`` and ``십여`` are never numbers.
    """

    if not isinstance(token, str):
        raise ValueError("integer token must be a string")
    value_text = token.strip()
    if not value_text:
        raise ValueError("integer token is empty")

    if re.fullmatch(_ASCII_INTEGER, value_text):
        value = int(value_text)
    elif re.fullmatch(_GROUPED_INTEGER, value_text):
        value = int(value_text.replace(",", ""))
    else:
        korean = re.sub(r"\s+", "", value_text)
        if korean in _NATIVE_ONES:
            value = _NATIVE_ONES[korean]
        elif korean == "스무":
            value = 20
        else:
            value = 0
            for tens_text, tens_value in _NATIVE_TENS.items():
                if korean == tens_text:
                    value = tens_value
                    break
                if korean.startswith(tens_text):
                    suffix = korean[len(tens_text) :]
                    if suffix in _NATIVE_ONES:
                        value = tens_value + _NATIVE_ONES[suffix]
                    break
            if not value:
                if korean in _SINO_DIGITS:
                    value = _SINO_DIGITS[korean]
                else:
                    match = re.fullmatch(
                        r"([일이삼사오육칠팔구]?)십([일이삼사오육칠팔구]?)",
                        korean,
                    )
                    if not match:
                        raise ValueError("integer token is not canonical")
                    tens = _SINO_DIGITS.get(match.group(1), 1)
                    ones = _SINO_DIGITS.get(match.group(2), 0)
                    value = tens * 10 + ones

    if value <= 0 or value > max_value:
        raise ValueError("integer token is outside the supported range")
    return value


_BUY_VERB = (
    r"(?:매수(?:해\s*줘|해줘|해주세요|하세요|해|하자|할게)?|"
    r"구매(?:해\s*줘|해줘|해주세요|하세요|해)?|"
    r"사\s*줘|사줘|사주세요|사라|사자|사)"
)
_SELL_VERB = (
    r"(?:매도(?:해\s*줘|해줘|해주세요|하세요|해|하자|할게)?|"
    r"팔아\s*줘|팔아줘|팔아|파세요|팔자)"
)
_CANCEL_VERB = (
    r"(?:취소(?:해\s*줘|해줘|해주세요|하세요|해|하자)?|"
    r"철회(?:해\s*줘|해줘|해주세요|하세요|해|하자)?)"
)
_WORD_LEFT = r"(?<![가-힣A-Za-z0-9])"
_WORD_RIGHT = r"(?![가-힣A-Za-z0-9])"

_BUY_RE = re.compile(_WORD_LEFT + _BUY_VERB + _WORD_RIGHT)
_SELL_RE = re.compile(_WORD_LEFT + _SELL_VERB + _WORD_RIGHT)
_CANCEL_RE = re.compile(_WORD_LEFT + _CANCEL_VERB + _WORD_RIGHT)

_QUANTITY_RE = re.compile(
    rf"(?<![가-힣A-Za-z0-9,.])(?P<token>{_INTEGER_TOKEN})\s*"
    r"(?:주식|주)(?![가-힣A-Za-z0-9])"
)
_MALFORMED_ARABIC_QUANTITY_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9,])[+-]?(?:\d[\d,.]*)(?:\s*)(?:주식|주)"
    r"(?![가-힣A-Za-z0-9])"
)

_MARKET_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9])시장가(?:로|에)?(?![가-힣A-Za-z0-9])"
)
_LIMIT_MARKER_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9])지정가(?:는|로|에)?(?![가-힣A-Za-z0-9])"
)
_WON_AMOUNT_RE = re.compile(
    rf"(?<![가-힣A-Za-z0-9,])(?P<token>{_INTEGER_TOKEN})\s*"
    r"(?P<man>만\s*)?원(?=(?:에)?(?:\s|$))"
)
_BARE_AMOUNT_RE = re.compile(
    rf"\s*(?P<token>{_INTEGER_TOKEN})\s*(?P<man>만)?"
)

# One vocabulary for "the whole position/order set", shared by the sentence
# gates below and by the span scanners in ``_aggregate_match``.  Keeping a
# single tuple prevents the sentence gate and the evidence scanner from
# drifting apart and silently accepting a sentence whose spans cannot be found.
_AGGREGATE_SCOPE_WORDS = ("전량", "전부", "모두", "모든", "전체", "일괄", "다")
_AGGREGATE_SCOPE_WORD = "(?:" + "|".join(_AGGREGATE_SCOPE_WORDS) + ")"
# Korean is routinely written without a space between the scope word and the
# verb ("전량매도", "일괄매도").  The plain word boundaries reject that, so the
# aggregate scanners accept a boundary that is either a real non-word char or
# the adjacent half of the same command.  Each lookbehind stays fixed-width.
_AFTER_AGGREGATE_SCOPE = "(?:" + "|".join(
    f"(?<={word})" for word in _AGGREGATE_SCOPE_WORDS
) + ")"
_BEFORE_AGGREGATE_VERB = rf"(?={_SELL_VERB}|{_CANCEL_VERB})"
# Korean stacks these words for emphasis ("전량 일괄매도", "전부 다 취소").  The
# stack is redundant, not a second command, so the whole contiguous run is one
# AGGREGATE_SCOPE span.  The repetition is bounded to keep matching linear.
_AGGREGATE_SCOPE_PHRASE = (
    _AGGREGATE_SCOPE_WORD
    + rf"(?:\s*{_WORD_LEFT}{_AGGREGATE_SCOPE_WORD}){{0,2}}"
)

_AGGREGATE_SCOPE_RE = re.compile(
    _WORD_LEFT + _AGGREGATE_SCOPE_PHRASE
    + rf"(?:{_WORD_RIGHT}|{_BEFORE_AGGREGATE_VERB})"
)
_AGGREGATE_SELL_RE = re.compile(
    rf"(?:{_WORD_LEFT}|{_AFTER_AGGREGATE_SCOPE}){_SELL_VERB}{_WORD_RIGHT}"
)
_AGGREGATE_CANCEL_RE = re.compile(
    rf"(?:{_WORD_LEFT}|{_AFTER_AGGREGATE_SCOPE}){_CANCEL_VERB}{_WORD_RIGHT}"
)
_SELL_ALL_RE = re.compile(
    rf"^\s*(?:(?:내|현재)\s*)?"
    rf"(?:(?:보유\s*)?계좌(?:에|의|에서)?\s*)?"
    rf"(?:(?:보유(?:한|\s*중인)?|있는)\s*)?"
    rf"(?:(?:종목|주식)\s*)?"
    rf"{_AGGREGATE_SCOPE_PHRASE}\s*{_SELL_VERB}"
    rf"(?:\s*(?:주세요|줘))?[.!]*\s*$"
)
_CANCEL_ALL_RE = re.compile(
    rf"^\s*(?:(?:내|현재)\s*)?"
    rf"(?:"
    rf"(?:(?:미체결|대기\s*중인|대기|열린)\s*)?(?:주문|오더)\s*"
    rf"{_AGGREGATE_SCOPE_PHRASE}"
    rf"|{_AGGREGATE_SCOPE_PHRASE}\s*"
    rf"(?:(?:미체결|대기\s*중인|대기|열린)\s*)?(?:주문|오더)"
    rf")\s*{_CANCEL_VERB}(?:\s*(?:주세요|줘))?[.!]*\s*$"
)

_QUESTION_RE = re.compile(
    r"\?|(?:할까|할까요|해도\s*돼|해도\s*될까|해\s*줄래|해줄래|"
    r"사도\s*돼|팔아도\s*돼|가능(?:해|할까|한가)|어때|될까)(?:요)?[.!]*\s*$"
)
_NEGATION_RE = re.compile(
    r"(?:하지\s*마|하지마|말아(?:\s*줘)?|말(?:아|자)|"
    r"안\s*(?:사|팔|매수|매도|취소)|(?:매수|매도|취소)\s*안|않(?:아|게|도록)?)"
)
_CONDITIONAL_RE = re.compile(
    r"(?:만약|가정(?:하면|해서)?|(?:오르|내리|떨어지|되|한다|간다|온다)면|"
    r"(?:일|인)\s*경우|조건(?:으로|부로)?|때(?:만|에))"
)
_READ_ONLY_RE = re.compile(
    r"(?:알려\s*줘|알려줘|보여\s*줘|보여줘|조회|내역|상태|추천|분석|"
    r"조사|설명|확인|취소율|주문했|체결됐|얼마(?:야|인지)?)"
)
# A holdings check immediately followed by an imperative sell is a safety
# preflight, not a read-only speech act. Keep the accepted grammar narrow so
# an arbitrary sentence containing "확인" cannot cross the execution boundary.
_HOLDINGS_PREFLIGHT_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?:(?:내|현재)\s*)?(?:보유\s*)?"
    r"(?:수량|잔고)(?:을|를)?\s*(?:확인|조회)"
    r"(?:하고|해서|한\s*(?:뒤|후)|\s*후)(?![가-힣A-Za-z0-9])"
)
_PAPER_ACCOUNT_SCOPE_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?:내\s*)?"
    r"(?:paper|페이퍼|모의\s*투자|모의)\s*계좌(?:에서|에|로)?"
    r"(?![가-힣A-Za-z0-9])",
    re.IGNORECASE,
)
_PAPER_MODE_SCOPE_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?:paper|페이퍼|모의\s*투자|모의)(?![가-힣A-Za-z0-9])",
    re.IGNORECASE,
)
_POSITION_SCOPE_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?:내\s*)?(?:계좌에\s*)?"
    r"(?:보유\s*(?:중인|하고\s*있는|한)|가지고\s*있는|갖고\s*있는)"
    r"(?![가-힣A-Za-z0-9])"
)
_IMMEDIATE_EXECUTION_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?:지금|바로|당장)(?![가-힣A-Za-z0-9])"
)
_ORDER_SUBMISSION_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?:주문(?:을)?\s*)?"
    r"(?:넣어\s*(?:줘|주세요)|내\s*(?:줘|주세요)|"
    r"실행(?:해\s*줘|해줘|해\s*주세요|해주세요)?|"
    r"처리(?:해\s*줘|해줘|해\s*주세요|해주세요)|요청)"
    r"(?![가-힣A-Za-z0-9])"
)
_DEICTIC_ORDER_REFERENCE_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9])이거(?![가-힣A-Za-z0-9])"
)
_PLACE_ORDER_ADORNMENT_PATTERNS = (
    _HOLDINGS_PREFLIGHT_RE,
    _PAPER_ACCOUNT_SCOPE_RE,
    _PAPER_MODE_SCOPE_RE,
    _POSITION_SCOPE_RE,
    _IMMEDIATE_EXECUTION_RE,
    _ORDER_SUBMISSION_RE,
    _DEICTIC_ORDER_REFERENCE_RE,
)
_EXAMPLE_RE = re.compile(
    r"(?:예시|예를\s*들|라고\s*(?:입력|말|쓰|하면)|문구|무슨\s*뜻|"
    r"프롬프트|테스트|따옴표|[\"'“”‘’])"
)
_COMPOUND_RE = re.compile(
    r"(?:그리고|그\s*다음|동시에|각각|;|/|\n|\r|"
    r"(?:매수하|매도하|사|팔)고\s+)"
)
_APPROXIMATE_RE = re.compile(
    r"(?:약|대략|대충|정도|쯤|한두|두세|십여|가능한\s*만큼|적당히|조금)"
)
_NOTIONAL_RE = re.compile(r"(?:원\s*어치|만원\s*어치|금액으로)")
_BASKET_NOTIONAL_RE = re.compile(
    rf"(?<![가-힣A-Za-z0-9,.])(?P<token>{_ARABIC_INTEGER})\s*만\s*원\s*씩"
    r"(?![가-힣A-Za-z0-9])"
)
_BASKET_QUANTITY_MEMBER_RE = re.compile(
    rf"(?P<instrument>(?:[0-9A-Za-z]{{6}}|[가-힣A-Za-z]"
    rf"[가-힣A-Za-z0-9&+._\- ]{{0,79}}?))\s+"
    rf"(?P<quantity>{_INTEGER_TOKEN})\s*(?:주식|주)"
)
_BASKET_MEMBER_NOTIONAL_RE = re.compile(
    rf"\s*(?P<instrument>(?:[0-9A-Za-z]{{6}}|[가-힣A-Za-z]"
    rf"[가-힣A-Za-z0-9&+._\- ]{{0,79}}?))\s+"
    rf"(?P<token>{_INTEGER_TOKEN})\s*만\s*원(?:\s*어치)?\s*"
)
_LIVE_MODE_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_])live(?![A-Za-z0-9_])|live\s*account|real[-\s]*money|real\s*account|"
    r"production\s*broker|라이브|실\s*계좌|실전\s*(?:투자|거래)?|"
    r"실\s*거래|실제\s*(?:계좌|주문|거래|집행)|"
    r"실제로\s*(?:주문|매수|매도|사|팔|거래|집행))",
    re.IGNORECASE,
)
_ROUTING_ORDER_RE = re.compile(
    r"(?:매수|매도|주문|오더|취소|철회|구매|청산|"
    r"사\s*줘|사줘|사주세요|사라|사자|살까|사도\s*돼|"
    r"팔아\s*줘|팔아줘|팔아|파세요|팔자|팔아도\s*돼|"
    r"포지션\s*(?:정리|청산|닫)|전량\s*(?:정리|매도)|"
    r"\bbuy\b|\bsell\b|\border\b|\bcancel\b|"
    r"\bliquidat(?:e|ion)\b|\bflatten\b|close\s+(?:the\s+)?position)",
    re.IGNORECASE,
)
_ORDER_CONTEXT_RE = re.compile(
    r"(?:주문|매수|매도|시장가|지정가|전량|전부|미체결|취소|\d\s*주|"
    + _KOREAN_INTEGER
    + r"\s*주)"
)

_INSTRUMENT_RE = re.compile(
    r"(?:"
    r"\d{6}(?:\s+[가-힣A-Za-z][가-힣A-Za-z0-9&+._\- ]{0,72})?"
    r"|[0-9A-Za-z]{6}"
    r"|[가-힣A-Za-z][가-힣A-Za-z0-9&+._\- ]{0,79}"
    r")"
)
_ALLOWED_RESIDUAL_RE = re.compile(
    r"^(?:(?:을|를|은|는|이|가|에|에서|로|으로|좀|만|내|현재|계좌|"
    r"보유|종목|주식|주문|주세요|줘)\s*)*$"
)
_LEADING_ORDER_ADDRESSEE_RE = re.compile(
    r"^(?:(?:\s*<@!?\d{15,25}>\s*)|"
    r"(?:\s*@홍진표[ \t]*대표(?![가-힣A-Za-z0-9])\s*))+"
)
_LEADING_DISCORD_MENTION_PARTS_RE = re.compile(
    r"^(?P<leading>\s*)<@!?\d{15,25}>(?P<trailing>\s*)"
)


@dataclass(frozen=True)
class _PriceMatch:
    span: tuple[int, int]
    value: int


@dataclass(frozen=True)
class _BasketMatch:
    instruments: tuple[str, ...]
    list_span: tuple[int, int]
    amount: int | None
    amount_match: re.Match[str] | None
    quantities: tuple[int, ...]
    notionals_krw: tuple[int, ...]
    side: OrderSide
    action_match: re.Match[str]
    market_match: re.Match[str] | None


@dataclass(frozen=True)
class DelayedPaperOrderPlan:
    """One strictly parsed relative-time PAPER order.

    ``payload`` is produced by the existing immediate-order grammar after the
    time phrase is blanked in place.  No second instrument/side/quantity
    grammar exists for delayed orders.
    """

    payload: CanonicalPlaceOrderPayload
    delay_seconds: int
    trigger_span: tuple[int, int]


# "3분 뒤" was the only delay wording the parser knew, so "3분 기다렸다가" fell
# through to the immediate lane where the unconsumed phrase corrupted the
# instrument span and surfaced as MISSING_OR_CONFLICTING_INSTRUMENT — a message
# about the wrong field entirely (2026-08-28).  Longer alternatives come first
# so the trailing boundary check cannot truncate one of them.
RELATIVE_DELAY_SUFFIX = (
    r"(?:뒤|후|있다가|기다렸다가|기다렸다|기다린\s*(?:뒤|후)"
    r"|지나서|지나면|지난\s*(?:뒤|후))(?:에)?"
)

_RELATIVE_DELAY_RE = re.compile(
    rf"(?<![가-힣A-Za-z0-9,.])(?P<token>{_INTEGER_TOKEN})\s*"
    rf"(?P<unit>초|분|시간)\s*{RELATIVE_DELAY_SUFFIX}(?![가-힣A-Za-z0-9])"
)
_RELATIVE_DELAY_UNIT_SECONDS = {"초": 1, "분": 60, "시간": 3600}
MAX_RELATIVE_DELAY_SECONDS = 24 * 60 * 60


# A duration the delay grammar does not cover still occupies the sentence, so
# the leftover text corrupts the instrument span and the user was told the
# *instrument* was missing or conflicting (2026-08-28, "3분 기다렸다가").  Name
# the real gap instead.  "3분봉" is excluded by the trailing boundary.
_UNPARSED_DURATION_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9,.])\d+\s*(?:초|분|시간)(?![가-힣A-Za-z0-9])"
)


def _instrument_or_delay_reason(raw_text: str) -> "OrderReasonCode":
    if _RELATIVE_DELAY_RE.search(raw_text) is None and _UNPARSED_DURATION_RE.search(
        raw_text
    ):
        return OrderReasonCode.UNSUPPORTED_DELAY_EXPRESSION
    return OrderReasonCode.MISSING_OR_CONFLICTING_INSTRUMENT


def raw_text_sha256(raw_text: str) -> str:
    """Hash the exact, unnormalized user text."""

    if not isinstance(raw_text, str):
        raise TypeError("raw_text must be a string")
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


def looks_like_user_order_request(raw_text: str) -> bool:
    """Return whether text should be routed to the strict order interpreter.

    This is intentionally a high-recall detector, not execution authorization.
    Questions, negations, and even forbidden LIVE/real-account requests return
    ``True`` so the deterministic verifier can produce an explicit safe result.
    Ordinary advisory text without an order or execution context returns
    ``False``.
    """

    if not isinstance(raw_text, str):
        raise TypeError("raw_text must be a string")
    text = raw_text.strip()
    if not text:
        return False
    return bool(_ROUTING_ORDER_RE.search(text) or _LIVE_MODE_RE.search(text))


def _clarify(digest: str, *reasons: OrderReasonCode) -> OrderClarification:
    return OrderClarification(raw_text_sha256=digest, reason_codes=tuple(dict.fromkeys(reasons)))


def _not_order(digest: str, *reasons: OrderReasonCode) -> NotOrder:
    return NotOrder(raw_text_sha256=digest, reason_codes=tuple(dict.fromkeys(reasons)))


def _unsafe_language(raw_text: str) -> OrderReasonCode | None:
    if any(ord(character) < 32 and character not in {"\t"} for character in raw_text):
        return OrderReasonCode.EXAMPLE_OR_QUOTED_TEXT
    if _EXAMPLE_RE.search(raw_text):
        return OrderReasonCode.EXAMPLE_OR_QUOTED_TEXT
    if _NEGATION_RE.search(raw_text):
        return OrderReasonCode.NEGATED_OR_PROHIBITED
    if _CONDITIONAL_RE.search(raw_text):
        return OrderReasonCode.CONDITIONAL_OR_HYPOTHETICAL
    # Remove only the explicit holdings-preflight clause before deciding
    # whether the whole utterance is read-only. Question/advice and negation
    # checks still run on the exact source text and remain fail-closed.
    executable_text = _HOLDINGS_PREFLIGHT_RE.sub(" ", raw_text)
    if _READ_ONLY_RE.search(executable_text):
        return OrderReasonCode.READ_ONLY_REQUEST
    if _QUESTION_RE.search(raw_text):
        return OrderReasonCode.QUESTION_OR_ADVICE
    return None


def _place_order_adornment_spans(raw_text: str) -> list[tuple[int, int]]:
    """Return narrow, non-authoritative language spans around one order.

    These phrases may describe PAPER scope, a holdings preflight, timing, or a
    polite submission wrapper. They never supply instrument, side, quantity,
    price, or order type; those fields still require exact evidence.
    """

    spans = {
        match.span()
        for pattern in _PLACE_ORDER_ADORNMENT_PATTERNS
        for match in pattern.finditer(raw_text)
    }
    return sorted(spans)


def is_clearly_non_executable_order_language(raw_text: str) -> bool:
    """Identify advice/read-only syntax that should remain in ordinary CEO chat.

    This is a routing optimization only. The verifier repeats the same check,
    so bypassing this helper cannot grant execution. Explicit LIVE language is
    intentionally not included and remains routed to the visible
    ``LIVE_MODE_FORBIDDEN`` rejection.
    """

    if not isinstance(raw_text, str):
        raise TypeError("raw_text must be a string")
    return _unsafe_language(raw_text) is not None


def _validate_evidence(
    raw_text: str, candidate: HermesOrderCandidate
) -> tuple[dict[EvidenceField, TextEvidence] | None, OrderReasonCode | None]:
    by_field: dict[EvidenceField, TextEvidence] = {}
    ordered: list[TextEvidence] = []
    for evidence in candidate.evidence:
        if evidence.field in by_field:
            return None, OrderReasonCode.EVIDENCE_FIELD_MISMATCH
        if evidence.end > len(raw_text):
            return None, OrderReasonCode.EVIDENCE_SPAN_INVALID
        if raw_text[evidence.start : evidence.end] != evidence.text:
            return None, OrderReasonCode.EVIDENCE_TEXT_MISMATCH
        by_field[evidence.field] = evidence
        ordered.append(evidence)

    ordered.sort(key=lambda item: (item.start, item.end, item.field.value))
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left.end <= right.start:
            continue
        # A price written with an explicit won unit (``70,000원에``) is itself
        # the deterministic evidence that the order is LIMIT.  In that one
        # case the same exact substring legitimately supports both fields.
        same_supported_span = (
            (left.start, left.end) == (right.start, right.end)
            and {left.field, right.field}
            in (
                {EvidenceField.ORDER_TYPE, EvidenceField.LIMIT_PRICE},
                {EvidenceField.ACTION, EvidenceField.SIDE},
            )
        )
        if not same_supported_span:
            return None, OrderReasonCode.EVIDENCE_SPAN_INVALID
    return by_field, None


def _align_discord_delivery_whitespace(
    raw_text: str, candidate: HermesOrderCandidate
) -> HermesOrderCandidate:
    """Repair one narrow Discord-only evidence coordinate presentation drift.

    Discord keeps the leading bot mention and following whitespace in the
    authenticated source. Hermes occasionally counts the mention but omits
    only that following whitespace in its offsets. Accept the uniform shift
    only when every evidence substring then exactly matches the source.
    """

    mention = _LEADING_DISCORD_MENTION_PARTS_RE.match(raw_text)
    if mention is None:
        return candidate
    shift = len(mention.group("trailing"))
    if shift <= 0 or not candidate.evidence:
        return candidate

    shifted: list[TextEvidence] = []
    for evidence in candidate.evidence:
        start = evidence.start + shift
        end = evidence.end + shift
        if end > len(raw_text) or raw_text[start:end] != evidence.text:
            return candidate
        shifted.append(evidence.model_copy(update={"start": start, "end": end}))
    return candidate.model_copy(update={"evidence": tuple(shifted)})


def _expected_evidence(
    evidence: Mapping[EvidenceField, TextEvidence],
    *,
    field: EvidenceField,
    span: tuple[int, int],
    normalized: str,
    alternative_spans: tuple[tuple[int, int], ...] = (),
) -> bool:
    item = evidence.get(field)
    return bool(
        item
        and (item.start, item.end) in (span, *alternative_spans)
        and item.normalized == normalized
    )


def _literal_subspan(match: re.Match[str], *tokens: str) -> tuple[int, int]:
    """Return the first deterministic semantic token inside a grammar match."""

    text = match.group(0)
    for token in tokens:
        offset = text.find(token)
        if offset >= 0:
            start = match.start() + offset
            return start, start + len(token)
    return match.span()


def _price_value(match: re.Match[str]) -> int:
    base = parse_strict_positive_integer(match.group("token"), max_value=MAX_PRICE)
    value = base * (10_000 if match.groupdict().get("man") else 1)
    if value > MAX_PRICE:
        raise ValueError("price exceeds supported range")
    return value


def _price_matches(raw_text: str, limit_markers: list[re.Match[str]]) -> list[_PriceMatch]:
    matches: dict[tuple[int, int], _PriceMatch] = {}
    for match in _WON_AMOUNT_RE.finditer(raw_text):
        matches[match.span()] = _PriceMatch(match.span(), _price_value(match))

    for marker in limit_markers:
        suffix = raw_text[marker.end() :]
        bare = _BARE_AMOUNT_RE.match(suffix)
        if not bare:
            continue
        start = marker.end() + bare.start("token")
        end = marker.end() + bare.end()
        # Keep only the numeric amount; whitespace between marker and value is
        # not evidence and therefore remains harmless residual whitespace.
        while end > start and raw_text[end - 1].isspace():
            end -= 1
        proxy = bare
        value = _price_value(proxy)
        if any(
            existing.span[0] == start
            and existing.span[1] >= end
            and existing.value == value
            for existing in matches.values()
        ):
            continue
        matches[(start, end)] = _PriceMatch((start, end), value)
    return sorted(matches.values(), key=lambda item: item.span)


def _basket_match(raw_text: str) -> _BasketMatch | None:
    """Return one strict same-notional BUY basket grammar, if present.

    The user must list two to twenty exact catalog mentions before one
    ``N만원씩`` allocation and one buy verb.  This parser neither expands a
    theme/list nor infers a symbol; catalog resolution remains downstream of
    the authenticated BFF.
    """

    amounts = list(_BASKET_NOTIONAL_RE.finditer(raw_text))
    buys = list(_BUY_RE.finditer(raw_text))
    sells = list(_SELL_RE.finditer(raw_text))
    market_markers = list(_MARKET_RE.finditer(raw_text))
    if (
        len(amounts) != 1
        or len(buys) != 1
        or sells
        or len(market_markers) > 1
        or _QUANTITY_RE.search(raw_text)
        or _LIMIT_MARKER_RE.search(raw_text)
        or _WON_AMOUNT_RE.search(raw_text)
    ):
        return None
    amount_match = amounts[0]
    action_match = buys[0]
    if action_match.start() < amount_match.end():
        return None
    try:
        amount = parse_strict_positive_integer(
            amount_match.group("token"), max_value=MAX_PRICE // 10_000
        ) * 10_000
    except ValueError:
        return None

    leading = _LEADING_ORDER_ADDRESSEE_RE.match(raw_text)
    list_start = leading.end() if leading is not None else 0
    while list_start < amount_match.start() and raw_text[list_start].isspace():
        list_start += 1
    noun_wrapper = re.match(r"(?:종목|주식)\s+", raw_text[list_start : amount_match.start()])
    if noun_wrapper is not None:
        list_start += noun_wrapper.end()
    list_end = amount_match.start()
    while list_end > list_start and raw_text[list_end - 1] in " \t,":
        list_end -= 1
    if list_end <= list_start:
        return None
    list_text = raw_text[list_start:list_end]
    if "," not in list_text:
        return None

    instruments: list[str] = []
    for index, part in enumerate(list_text.split(",")):
        mention = part.strip()
        if index == len(list_text.split(",")) - 1:
            mention = mention.rstrip("을를").strip()
        if (
            not mention
            or len(mention) > 80
            or _INSTRUMENT_RE.fullmatch(mention) is None
        ):
            return None
        # KRX codes are stronger source evidence than an adjacent display
        # name, just as in the single-order grammar.
        numeric_code_with_name = re.fullmatch(
            r"(?P<code>\d{6})\s+[가-힣A-Za-z][가-힣A-Za-z0-9&+._\- ]{0,72}",
            mention,
        )
        instruments.append(
            numeric_code_with_name.group("code")
            if numeric_code_with_name is not None
            else mention
        )
    if not 2 <= len(instruments) <= 20 or len(set(instruments)) != len(instruments):
        return None
    return _BasketMatch(
        instruments=tuple(instruments),
        list_span=(list_start, list_end),
        amount=amount,
        amount_match=amount_match,
        quantities=(),
        notionals_krw=(),
        side=OrderSide.BUY,
        action_match=action_match,
        market_match=market_markers[0] if market_markers else None,
    )


def _member_notional_basket_match(raw_text: str) -> _BasketMatch | None:
    """Return a strict per-member KRW notional BUY basket grammar."""

    if (
        _BASKET_NOTIONAL_RE.search(raw_text)
        or _QUANTITY_RE.search(raw_text)
        or _LIMIT_MARKER_RE.search(raw_text)
    ):
        return None
    buys = list(_BUY_RE.finditer(raw_text))
    sells = list(_SELL_RE.finditer(raw_text))
    market_markers = list(_MARKET_RE.finditer(raw_text))
    if len(buys) != 1 or sells or len(market_markers) > 1:
        return None
    action_match = buys[0]
    leading = _LEADING_ORDER_ADDRESSEE_RE.match(raw_text)
    list_start = leading.end() if leading is not None else 0
    while list_start < action_match.start() and raw_text[list_start].isspace():
        list_start += 1
    noun_wrapper = re.match(
        r"(?:종목|주식)\s+", raw_text[list_start : action_match.start()]
    )
    if noun_wrapper is not None:
        list_start += noun_wrapper.end()
    list_end = min(
        action_match.start(),
        market_markers[0].start() if market_markers else action_match.start(),
    )
    while list_end > list_start and raw_text[list_end - 1] in " \t,":
        list_end -= 1
    if list_end <= list_start:
        return None
    list_text = raw_text[list_start:list_end]
    if "," not in list_text:
        return None

    instruments: list[str] = []
    notionals_krw: list[int] = []
    cursor = 0
    while cursor < len(list_text):
        member = _BASKET_MEMBER_NOTIONAL_RE.match(list_text, cursor)
        if member is None:
            return None
        mention = member.group("instrument").strip()
        numeric_code_with_name = re.fullmatch(
            r"(?P<code>\d{6})\s+[가-힣A-Za-z][가-힣A-Za-z0-9&+._\- ]{0,72}",
            mention,
        )
        try:
            notional_krw = (
                parse_strict_positive_integer(
                    member.group("token"), max_value=MAX_PRICE // 10_000
                )
                * 10_000
            )
        except ValueError:
            return None
        instruments.append(
            numeric_code_with_name.group("code")
            if numeric_code_with_name is not None
            else mention
        )
        notionals_krw.append(notional_krw)
        cursor = member.end()
        if cursor == len(list_text):
            break
        separator = re.match(r",\s*", list_text[cursor:])
        if separator is None:
            return None
        cursor += separator.end()
        if cursor == len(list_text):
            return None
    if (
        not 2 <= len(instruments) <= 20
        or len(set(instruments)) != len(instruments)
    ):
        return None
    return _BasketMatch(
        instruments=tuple(instruments),
        list_span=(list_start, list_end),
        amount=None,
        amount_match=None,
        quantities=(),
        notionals_krw=tuple(notionals_krw),
        side=OrderSide.BUY,
        action_match=action_match,
        market_match=market_markers[0] if market_markers else None,
    )


def _quantity_basket_match(raw_text: str) -> _BasketMatch | None:
    """Return a strict same-direction, explicit-quantity basket grammar."""

    if _BASKET_NOTIONAL_RE.search(raw_text) or _WON_AMOUNT_RE.search(raw_text):
        return None
    buys = list(_BUY_RE.finditer(raw_text))
    sells = list(_SELL_RE.finditer(raw_text))
    market_markers = list(_MARKET_RE.finditer(raw_text))
    if (
        bool(buys) == bool(sells)
        or len(buys) + len(sells) != 1
        or len(market_markers) > 1
        or _LIMIT_MARKER_RE.search(raw_text)
    ):
        return None
    action_match = buys[0] if buys else sells[0]
    side = OrderSide.BUY if buys else OrderSide.SELL
    leading = _LEADING_ORDER_ADDRESSEE_RE.match(raw_text)
    list_start = leading.end() if leading is not None else 0
    while list_start < action_match.start() and raw_text[list_start].isspace():
        list_start += 1
    noun_wrapper = re.match(r"(?:종목|주식)\s+", raw_text[list_start : action_match.start()])
    if noun_wrapper is not None:
        list_start += noun_wrapper.end()
    list_end = min(
        action_match.start(),
        market_markers[0].start() if market_markers else action_match.start(),
    )
    while list_end > list_start and raw_text[list_end - 1] in " \t,":
        list_end -= 1
    if list_end <= list_start:
        return None
    list_text = raw_text[list_start:list_end]
    if "," not in list_text:
        return None

    instruments: list[str] = []
    quantities: list[int] = []
    for part in list_text.split(","):
        member = _BASKET_QUANTITY_MEMBER_RE.fullmatch(part.strip())
        if member is None:
            return None
        mention = member.group("instrument").strip()
        try:
            quantity = parse_strict_positive_integer(
                member.group("quantity"), max_value=MAX_QUANTITY
            )
        except ValueError:
            return None
        instruments.append(mention)
        quantities.append(quantity)
    if (
        not 2 <= len(instruments) <= 20
        or len(set(instruments)) != len(instruments)
    ):
        return None
    return _BasketMatch(
        instruments=tuple(instruments),
        list_span=(list_start, list_end),
        amount=None,
        amount_match=None,
        quantities=tuple(quantities),
        notionals_krw=(),
        side=side,
        action_match=action_match,
        market_match=market_markers[0] if market_markers else None,
    )


def _verify_basket(
    raw_text: str,
    digest: str,
    candidate: HermesOrderCandidate,
    evidence: Mapping[EvidenceField, TextEvidence],
    basket: _BasketMatch,
) -> OrderLanguageResult:
    if candidate.action is not DirectiveAction.PLACE_BASKET:
        return _clarify(digest, OrderReasonCode.CANDIDATE_MISMATCH)
    is_same_notional = basket.amount is not None
    is_member_notional = bool(basket.notionals_krw)
    expected_quantities = tuple(str(quantity) for quantity in basket.quantities)
    expected_notionals = tuple(str(notional) for notional in basket.notionals_krw)
    if (
        candidate.instrument_mention is not None
        or candidate.basket_instrument_mentions != basket.instruments
        or candidate.quantity is not None
        or candidate.order_type is not OrderType.MARKET
        or candidate.limit_price is not None
    ):
        return _clarify(digest, OrderReasonCode.CANDIDATE_MISMATCH)
    if is_same_notional:
        if (
            candidate.side is not OrderSide.BUY
            or candidate.notional_krw != str(basket.amount)
            or candidate.basket_quantities
            or candidate.basket_notionals_krw
        ):
            return _clarify(digest, OrderReasonCode.CANDIDATE_MISMATCH)
    elif is_member_notional:
        if (
            candidate.side is not OrderSide.BUY
            or candidate.notional_krw is not None
            or candidate.basket_quantities
            or candidate.basket_notionals_krw != expected_notionals
        ):
            return _clarify(digest, OrderReasonCode.CANDIDATE_MISMATCH)
    elif (
        candidate.side is not basket.side
        or candidate.notional_krw is not None
        or candidate.basket_quantities != expected_quantities
        or candidate.basket_notionals_krw
    ):
        return _clarify(digest, OrderReasonCode.CANDIDATE_MISMATCH)
    required_fields = {
        EvidenceField.BASKET_INSTRUMENTS,
        EvidenceField.SIDE,
    }
    if is_same_notional:
        required_fields.add(EvidenceField.NOTIONAL)
    if basket.market_match is not None:
        required_fields.add(EvidenceField.ORDER_TYPE)
    allowed_fields = required_fields | {EvidenceField.ACTION}
    if not required_fields.issubset(evidence) or not set(evidence).issubset(
        allowed_fields
    ):
        return _clarify(digest, OrderReasonCode.EVIDENCE_FIELD_MISMATCH)
    if not _expected_evidence(
        evidence,
        field=EvidenceField.BASKET_INSTRUMENTS,
        span=basket.list_span,
        normalized="LIST",
    ) or not _expected_evidence(
        evidence,
        field=EvidenceField.SIDE,
        span=basket.action_match.span(),
        normalized=basket.side.value,
        alternative_spans=(
            _literal_subspan(
                basket.action_match,
                *(("매수", "구매", "사") if basket.side is OrderSide.BUY else ("매도", "팔")),
            ),
        ),
    ):
        return _clarify(digest, OrderReasonCode.EVIDENCE_FIELD_MISMATCH)
    if is_same_notional and (
        basket.amount_match is None
        or not _expected_evidence(
            evidence,
            field=EvidenceField.NOTIONAL,
            span=basket.amount_match.span(),
            normalized=str(basket.amount),
        )
    ):
        return _clarify(digest, OrderReasonCode.EVIDENCE_FIELD_MISMATCH)
    if basket.market_match is not None and not _expected_evidence(
        evidence,
        field=EvidenceField.ORDER_TYPE,
        span=basket.market_match.span(),
        normalized=OrderType.MARKET.value,
        alternative_spans=(_literal_subspan(basket.market_match, "시장가"),),
    ):
        return _clarify(digest, OrderReasonCode.EVIDENCE_FIELD_MISMATCH)
    action_evidence = evidence.get(EvidenceField.ACTION)
    if action_evidence is not None and not _expected_evidence(
        evidence,
        field=EvidenceField.ACTION,
        span=basket.action_match.span(),
        normalized=DirectiveAction.PLACE_BASKET.value,
        alternative_spans=(
            _literal_subspan(
                basket.action_match,
                *(("매수", "구매", "사") if basket.side is OrderSide.BUY else ("매도", "팔")),
            ),
        ),
    ):
        return _clarify(digest, OrderReasonCode.EVIDENCE_FIELD_MISMATCH)
    consumed = [
        basket.list_span,
        basket.action_match.span(),
    ]
    if basket.amount_match is not None:
        consumed.append(basket.amount_match.span())
    if basket.market_match is not None:
        consumed.append(basket.market_match.span())
    if not _residual_supported(raw_text, list(dict.fromkeys(consumed))):
        return _clarify(digest, OrderReasonCode.UNSUPPORTED_TEXT)
    return VerifiedPaperDirective(
        raw_text_sha256=digest,
        action=DirectiveAction.PLACE_BASKET,
        payload=CanonicalPlaceBasketPayload(
            orders=tuple(
                CanonicalBasketOrderItem(
                    instrument_mention=mention,
                    notional_krw=(
                        str(basket.amount)
                        if is_same_notional
                        else (
                            str(basket.notionals_krw[index])
                            if is_member_notional
                            else None
                        )
                    ),
                    quantity=(
                        str(basket.quantities[index])
                        if not is_same_notional and not is_member_notional
                        else None
                    ),
                    side=basket.side,
                )
                for index, mention in enumerate(basket.instruments)
            )
        ),
        evidence=candidate.evidence,
    )


def _scope_word_spans(scope_match: re.Match[str]) -> tuple[tuple[int, int], ...]:
    """Every individual scope word inside a redundant stacked scope run."""

    base = scope_match.start()
    return tuple(
        (base + word.start(), base + word.end())
        for word in re.finditer(_AGGREGATE_SCOPE_WORD, scope_match.group(0))
    )


def _aggregate_match(
    raw_text: str,
) -> tuple[DirectiveAction, re.Match[str], re.Match[str]] | None:
    if _SELL_ALL_RE.fullmatch(raw_text):
        actions = list(_AGGREGATE_SELL_RE.finditer(raw_text))
        scopes = list(_AGGREGATE_SCOPE_RE.finditer(raw_text))
        if len(actions) == 1 and len(scopes) == 1:
            return DirectiveAction.SELL_ALL, actions[0], scopes[0]
    if _CANCEL_ALL_RE.fullmatch(raw_text):
        actions = list(_AGGREGATE_CANCEL_RE.finditer(raw_text))
        scopes = list(_AGGREGATE_SCOPE_RE.finditer(raw_text))
        if len(actions) == 1 and len(scopes) == 1:
            return DirectiveAction.CANCEL_ALL, actions[0], scopes[0]
    return None


def _verify_aggregate(
    raw_text: str,
    digest: str,
    candidate: HermesOrderCandidate,
    evidence: Mapping[EvidenceField, TextEvidence],
    aggregate: tuple[DirectiveAction, re.Match[str], re.Match[str]],
) -> OrderLanguageResult:
    action, action_match, scope_match = aggregate
    if candidate.action is not action:
        return _clarify(digest, OrderReasonCode.CANDIDATE_MISMATCH)
    if set(evidence) != {EvidenceField.ACTION, EvidenceField.AGGREGATE_SCOPE}:
        return _clarify(digest, OrderReasonCode.EVIDENCE_FIELD_MISMATCH)
    # The single-order path already accepts the bare verb literal inside a
    # polite form ("매도" within "매도해줘").  The aggregate path demanded the
    # whole grammar match instead, so the same sentence verified as a single
    # order but clarified as a sell-all (2026-08-31).  One convention.
    if not _expected_evidence(
        evidence,
        field=EvidenceField.ACTION,
        span=action_match.span(),
        normalized=action.value,
        alternative_spans=(
            _literal_subspan(
                action_match,
                *(
                    ("매도", "팔아", "팔", "파")
                    if action is DirectiveAction.SELL_ALL
                    else ("취소", "철회")
                ),
            ),
        ),
    ) or not _expected_evidence(
        evidence,
        field=EvidenceField.AGGREGATE_SCOPE,
        span=scope_match.span(),
        normalized="ALL",
        # A stacked run ("전량 일괄") is redundant emphasis: any one of its
        # words carries the whole scope, so either span is honest evidence.
        alternative_spans=_scope_word_spans(scope_match),
    ):
        return _clarify(digest, OrderReasonCode.EVIDENCE_FIELD_MISMATCH)
    return VerifiedPaperDirective(
        raw_text_sha256=digest,
        action=action,
        payload=None,
        evidence=candidate.evidence,
    )


def _residual_supported(raw_text: str, spans: list[tuple[int, int]]) -> bool:
    remaining = list(raw_text)
    for start, end in spans:
        remaining[start:end] = " " * (end - start)
    # Discord can preserve both the bot snowflake and its rendered CEO display
    # name when a user addresses the bot twice. They are delivery metadata,
    # not trading language. Only consecutive, exact leading addressees are
    # ignored; arbitrary names and mentions elsewhere remain fail-closed.
    leading_mention = _LEADING_ORDER_ADDRESSEE_RE.match(raw_text)
    if leading_mention:
        remaining[leading_mention.start() : leading_mention.end()] = " " * (
            leading_mention.end() - leading_mention.start()
        )
    residual = " ".join("".join(remaining).strip(" \t,.!").split())
    return "?" not in residual and bool(_ALLOWED_RESIDUAL_RE.fullmatch(residual))


def _verify_place_order(
    raw_text: str,
    digest: str,
    candidate: HermesOrderCandidate,
    evidence: Mapping[EvidenceField, TextEvidence],
) -> OrderLanguageResult:
    preflight_matches = list(_HOLDINGS_PREFLIGHT_RE.finditer(raw_text))
    if len(preflight_matches) > 1:
        return _clarify(digest, OrderReasonCode.UNSUPPORTED_TEXT)
    adornment_spans = _place_order_adornment_spans(raw_text)

    buy_matches = list(_BUY_RE.finditer(raw_text))
    sell_matches = list(_SELL_RE.finditer(raw_text))
    if bool(buy_matches) == bool(sell_matches):
        return _clarify(digest, OrderReasonCode.MISSING_OR_CONFLICTING_SIDE)
    side_matches = buy_matches if buy_matches else sell_matches
    if len(side_matches) != 1:
        return _clarify(digest, OrderReasonCode.MISSING_OR_CONFLICTING_SIDE)
    side = OrderSide.BUY if buy_matches else OrderSide.SELL
    side_match = side_matches[0]

    quantities = list(_QUANTITY_RE.finditer(raw_text))
    if len(quantities) != 1:
        malformed = list(_MALFORMED_ARABIC_QUANTITY_RE.finditer(raw_text))
        reason = (
            OrderReasonCode.INVALID_NUMBER
            if malformed
            else OrderReasonCode.MISSING_OR_CONFLICTING_QUANTITY
        )
        return _clarify(digest, reason)
    quantity_match = quantities[0]
    try:
        quantity = parse_strict_positive_integer(
            quantity_match.group("token"), max_value=MAX_QUANTITY
        )
    except ValueError:
        return _clarify(digest, OrderReasonCode.INVALID_NUMBER)

    market_markers = list(_MARKET_RE.finditer(raw_text))
    limit_markers = list(_LIMIT_MARKER_RE.finditer(raw_text))
    try:
        prices = _price_matches(raw_text, limit_markers)
    except ValueError:
        return _clarify(digest, OrderReasonCode.INVALID_NUMBER)

    if len(market_markers) > 1 or len(limit_markers) > 1 or len(prices) > 1:
        return _clarify(digest, OrderReasonCode.MISSING_OR_CONFLICTING_ORDER_TYPE)
    if market_markers and (limit_markers or prices):
        return _clarify(digest, OrderReasonCode.CONFLICTING_MARKET_AND_PRICE)
    if market_markers:
        order_type = OrderType.MARKET
        order_type_span = market_markers[0].span()
        limit_price: int | None = None
        price_span: tuple[int, int] | None = None
    elif limit_markers or prices:
        if not prices:
            return _clarify(digest, OrderReasonCode.MISSING_LIMIT_PRICE)
        order_type = OrderType.LIMIT
        order_type_span = (
            limit_markers[0].span() if limit_markers else prices[0].span
        )
        limit_price = prices[0].value
        price_span = prices[0].span
    else:
        # A complete PAPER place-order command with no price/type language has
        # one deterministic interpretation: MARKET.  This policy default is
        # not source evidence, so Hermes must not fabricate an ORDER_TYPE span.
        order_type = OrderType.MARKET
        order_type_span = None
        limit_price = None
        price_span = None

    consumed = [
        side_match.span(),
        quantity_match.span(),
        *adornment_spans,
    ]
    if order_type_span is not None:
        consumed.append(order_type_span)
    if price_span is not None:
        consumed.append(price_span)
    if limit_markers:
        consumed.append(limit_markers[0].span())
    authoritative_instrument_span = _deterministic_instrument_span(
        raw_text, list(dict.fromkeys(consumed))
    )
    if authoritative_instrument_span is None:
        return _clarify(digest, _instrument_or_delay_reason(raw_text))
    authoritative_instrument = raw_text[
        authoritative_instrument_span[0] : authoritative_instrument_span[1]
    ]

    instrument = evidence.get(EvidenceField.INSTRUMENT)
    if instrument is None:
        return _clarify(digest, OrderReasonCode.EVIDENCE_MISSING)
    allowed_instrument_evidence = {
        (authoritative_instrument_span, authoritative_instrument)
    }
    verified_instrument_mention = authoritative_instrument
    numeric_code_with_name = re.fullmatch(
        r"(?P<code>\d{6})\s+(?P<name>[가-힣A-Za-z][가-힣A-Za-z0-9&+._\- ]{0,72})",
        authoritative_instrument,
    )
    if numeric_code_with_name is not None:
        code = numeric_code_with_name.group("code")
        name = numeric_code_with_name.group("name")
        code_start = authoritative_instrument_span[0]
        name_start = code_start + numeric_code_with_name.start("name")
        allowed_instrument_evidence.update(
            {
                ((code_start, code_start + len(code)), code),
                ((name_start, name_start + len(name)), name),
            }
        )
        # A user-supplied six-digit code is authoritative. The adjacent name
        # remains useful grounding, but can never override the catalog lookup.
        verified_instrument_mention = code
    if (
        candidate.instrument_mention != instrument.text
        or instrument.normalized != candidate.instrument_mention
        or not _INSTRUMENT_RE.fullmatch(instrument.text)
    ):
        return _clarify(digest, _instrument_or_delay_reason(raw_text))
    if (
        ((instrument.start, instrument.end), instrument.text)
        not in allowed_instrument_evidence
    ):
        return _clarify(digest, OrderReasonCode.UNSUPPORTED_TEXT)

    required_fields = {
        EvidenceField.INSTRUMENT,
        EvidenceField.SIDE,
        EvidenceField.QUANTITY,
    }
    if order_type_span is not None:
        required_fields.add(EvidenceField.ORDER_TYPE)
    if order_type is OrderType.LIMIT:
        required_fields.add(EvidenceField.LIMIT_PRICE)
    allowed_fields = required_fields | {EvidenceField.ACTION}
    if not required_fields.issubset(evidence) or not set(evidence).issubset(
        allowed_fields
    ):
        return _clarify(digest, OrderReasonCode.EVIDENCE_FIELD_MISMATCH)

    if candidate.action is not DirectiveAction.PLACE_ORDER:
        return _clarify(digest, OrderReasonCode.CANDIDATE_MISMATCH)
    action_evidence = evidence.get(EvidenceField.ACTION)
    if action_evidence is not None and not _expected_evidence(
        evidence,
        field=EvidenceField.ACTION,
        span=side_match.span(),
        normalized=DirectiveAction.PLACE_ORDER.value,
        alternative_spans=(
            _literal_subspan(
                side_match,
                *(("매수", "구매", "사") if side is OrderSide.BUY else ("매도", "팔")),
            ),
        ),
    ):
        return _clarify(digest, OrderReasonCode.EVIDENCE_FIELD_MISMATCH)
    if candidate.side is not side or candidate.quantity != str(quantity):
        return _clarify(digest, OrderReasonCode.CANDIDATE_MISMATCH)
    if candidate.order_type is not order_type or candidate.limit_price != (
        str(limit_price) if limit_price is not None else None
    ):
        return _clarify(digest, OrderReasonCode.CANDIDATE_MISMATCH)

    if not _expected_evidence(
        evidence,
        field=EvidenceField.SIDE,
        span=side_match.span(),
        normalized=side.value,
        alternative_spans=(
            _literal_subspan(
                side_match,
                *(("매수", "구매", "사") if side is OrderSide.BUY else ("매도", "팔")),
            ),
        ),
    ) or not _expected_evidence(
        evidence,
        field=EvidenceField.QUANTITY,
        span=quantity_match.span(),
        normalized=str(quantity),
        alternative_spans=(quantity_match.span("token"),),
    ) or (
        order_type_span is not None
        and not _expected_evidence(
            evidence,
            field=EvidenceField.ORDER_TYPE,
            span=order_type_span,
            normalized=order_type.value,
            alternative_spans=(
                _literal_subspan(
                    market_markers[0]
                    if order_type is OrderType.MARKET
                    else limit_markers[0],
                    "시장가" if order_type is OrderType.MARKET else "지정가",
                ),
            )
            if market_markers or limit_markers
            else (),
        )
    ):
        return _clarify(digest, OrderReasonCode.EVIDENCE_FIELD_MISMATCH)
    if order_type is OrderType.LIMIT and (
        price_span is None
        or not _expected_evidence(
            evidence,
            field=EvidenceField.LIMIT_PRICE,
            span=price_span,
            normalized=str(limit_price),
        )
    ):
        return _clarify(digest, OrderReasonCode.EVIDENCE_FIELD_MISMATCH)

    consumed.append(authoritative_instrument_span)
    if not _residual_supported(raw_text, list(dict.fromkeys(consumed))):
        return _clarify(digest, OrderReasonCode.UNSUPPORTED_TEXT)

    return VerifiedPaperDirective(
        raw_text_sha256=digest,
        action=DirectiveAction.PLACE_ORDER,
        payload=CanonicalPlaceOrderPayload(
            instrument_mention=verified_instrument_mention,
            side=side,
            quantity=str(quantity),
            order_type=order_type,
            limit_price=str(limit_price) if limit_price is not None else None,
        ),
        evidence=candidate.evidence,
    )


def verify_order_candidate(
    raw_text: str,
    candidate: HermesOrderCandidate | Mapping[str, Any],
) -> OrderLanguageResult:
    """Verify one Hermes candidate against the exact original user text.

    This function never resolves a symbol and never calls an OMS.  Invalid
    candidates become ``CLARIFY`` results instead of exceptions, so an LLM or
    transport failure cannot accidentally widen execution authority.
    """

    if not isinstance(raw_text, str):
        raise TypeError("raw_text must be a string")
    digest = raw_text_sha256(raw_text)
    if not raw_text.strip() or len(raw_text) > MAX_TEXT_LENGTH:
        return _clarify(digest, OrderReasonCode.UNSUPPORTED_TEXT)
    # Never reinterpret an explicit real-account request as PAPER.  The only
    # supported mode is PAPER, so this must remain a visible terminal language
    # result even when Hermes itself emitted a malformed or LIVE candidate.
    if _LIVE_MODE_RE.search(raw_text):
        return _clarify(digest, OrderReasonCode.LIVE_MODE_FORBIDDEN)

    try:
        structured = (
            candidate
            if isinstance(candidate, HermesOrderCandidate)
            else HermesOrderCandidate.model_validate(candidate)
        )
    except (ValidationError, TypeError, ValueError):
        return _clarify(digest, OrderReasonCode.INVALID_CANDIDATE_SCHEMA)
    if structured.raw_text_sha256 != digest:
        return _clarify(digest, OrderReasonCode.RAW_TEXT_HASH_MISMATCH)
    structured = _align_discord_delivery_whitespace(raw_text, structured)

    unsafe = _unsafe_language(raw_text)
    if unsafe is not None:
        return _not_order(digest, unsafe)
    basket = (
        _basket_match(raw_text)
        or _member_notional_basket_match(raw_text)
        or _quantity_basket_match(raw_text)
    )
    if _NOTIONAL_RE.search(raw_text) and basket is None:
        return _clarify(digest, OrderReasonCode.NOTIONAL_UNSUPPORTED)
    if _APPROXIMATE_RE.search(raw_text):
        return _clarify(digest, OrderReasonCode.APPROXIMATE_VALUE)
    if (
        (_COMPOUND_RE.search(raw_text) or re.search(r"(?<!\d)[.!]\s*\S", raw_text))
        and basket is None
    ):
        return _clarify(digest, OrderReasonCode.MULTIPLE_COMMANDS)

    aggregate = _aggregate_match(raw_text)
    if structured.decision is not CandidateDecision.EXECUTE:
        if basket or aggregate or _ORDER_CONTEXT_RE.search(raw_text):
            return _clarify(digest, OrderReasonCode.HERMES_DID_NOT_PROPOSE_EXECUTION)
        return _not_order(digest, OrderReasonCode.NO_ORDER_COMMAND)

    evidence, evidence_error = _validate_evidence(raw_text, structured)
    if evidence_error is not None or evidence is None:
        return _clarify(
            digest, evidence_error or OrderReasonCode.EVIDENCE_SPAN_INVALID
        )
    if aggregate is not None:
        return _verify_aggregate(raw_text, digest, structured, evidence, aggregate)
    if basket is not None:
        return _verify_basket(raw_text, digest, structured, evidence, basket)
    return _verify_place_order(raw_text, digest, structured, evidence)


_DETERMINISTIC_RESIDUAL_WORDS = frozenset(
    {
        "을",
        "를",
        "은",
        "는",
        "이",
        "가",
        "에",
        "에서",
        "로",
        "으로",
        "좀",
        "만",
        "내",
        "현재",
        "계좌",
        "보유",
        "종목",
        "주식",
        "주문",
        "주세요",
        "줘",
    }
)
_DETERMINISTIC_INSTRUMENT_SUFFIXES = (
    "으로",
    "에서",
    "을",
    "를",
    "은",
    "는",
    "이",
    "가",
    "에",
    "로",
)


def _deterministic_instrument_span(
    raw_text: str,
    consumed: list[tuple[int, int]],
) -> tuple[int, int] | None:
    """Extract the one residual instrument phrase without normalizing offsets."""

    remaining = list(raw_text)
    for start, end in consumed:
        remaining[start:end] = " " * (end - start)
    mention = _LEADING_ORDER_ADDRESSEE_RE.match(raw_text)
    if mention:
        remaining[mention.start() : mention.end()] = " " * (
            mention.end() - mention.start()
        )

    tokens: list[tuple[int, int, str]] = []
    for match in re.finditer(r"\S+", "".join(remaining)):
        start, end = match.span()
        while start < end and raw_text[start] in "\t ,.!":
            start += 1
        while end > start and raw_text[end - 1] in "\t ,.!":
            end -= 1
        if start < end:
            tokens.append((start, end, raw_text[start:end]))
    while tokens and tokens[0][2] in _DETERMINISTIC_RESIDUAL_WORDS:
        tokens.pop(0)
    while tokens and tokens[-1][2] in _DETERMINISTIC_RESIDUAL_WORDS:
        tokens.pop()
    if not tokens:
        return None

    start, end = tokens[0][0], tokens[-1][1]
    candidate = raw_text[start:end]
    for suffix in _DETERMINISTIC_INSTRUMENT_SUFFIXES:
        if candidate.endswith(suffix) and len(candidate) > len(suffix):
            end -= len(suffix)
            candidate = raw_text[start:end]
            break
    if not candidate or not _INSTRUMENT_RE.fullmatch(candidate):
        return None
    if not _residual_supported(raw_text, [*consumed, (start, end)]):
        return None
    return start, end


def _deterministic_basket_candidate(raw_text: str) -> HermesOrderCandidate | None:
    """Build exact evidence for a strict same-direction PAPER basket grammar."""

    basket = (
        _basket_match(raw_text)
        or _member_notional_basket_match(raw_text)
        or _quantity_basket_match(raw_text)
    )
    if basket is None:
        return None
    evidence = [
        TextEvidence(
            field=EvidenceField.BASKET_INSTRUMENTS,
            start=basket.list_span[0],
            end=basket.list_span[1],
            text=raw_text[basket.list_span[0] : basket.list_span[1]],
            normalized="LIST",
        ),
        TextEvidence(
            field=EvidenceField.SIDE,
            start=basket.action_match.start(),
            end=basket.action_match.end(),
            text=basket.action_match.group(0),
            normalized=basket.side.value,
        ),
    ]
    if basket.amount_match is not None:
        evidence.append(
            TextEvidence(
                field=EvidenceField.NOTIONAL,
                start=basket.amount_match.start(),
                end=basket.amount_match.end(),
                text=basket.amount_match.group(0),
                normalized=str(basket.amount),
            )
        )
    if basket.market_match is not None:
        evidence.append(
            TextEvidence(
                field=EvidenceField.ORDER_TYPE,
                start=basket.market_match.start(),
                end=basket.market_match.end(),
                text=basket.market_match.group(0),
                normalized=OrderType.MARKET.value,
            )
        )
    candidate = HermesOrderCandidate(
        raw_text_sha256=raw_text_sha256(raw_text),
        decision=CandidateDecision.EXECUTE,
        action=DirectiveAction.PLACE_BASKET,
        basket_instrument_mentions=basket.instruments,
        basket_quantities=tuple(str(quantity) for quantity in basket.quantities),
        basket_notionals_krw=tuple(
            str(notional) for notional in basket.notionals_krw
        ),
        side=basket.side,
        notional_krw=str(basket.amount) if basket.amount is not None else None,
        order_type=OrderType.MARKET,
        evidence=tuple(evidence),
    )
    return (
        candidate
        if isinstance(verify_order_candidate(raw_text, candidate), VerifiedPaperDirective)
        else None
    )


def deterministic_order_candidate(raw_text: str) -> HermesOrderCandidate | None:
    """Build exact evidence for one unambiguous place order without an LLM.

    This helper grants no authority: callers must still pass its output through
    :func:`verify_order_candidate` and the authenticated PAPER admission gate.
    It intentionally returns ``None`` for aggregates, advice, unsafe language,
    malformed numbers, and any residual text it cannot account for exactly.
    """

    if (
        not isinstance(raw_text, str)
        or not raw_text.strip()
        or len(raw_text) > MAX_TEXT_LENGTH
        or _unsafe_language(raw_text) is not None
        or _LIVE_MODE_RE.search(raw_text)
        or _RELATIVE_DELAY_RE.search(raw_text)
        or _APPROXIMATE_RE.search(raw_text)
        or _COMPOUND_RE.search(raw_text)
        or re.search(r"(?<!\d)[.!]\s*\S", raw_text)
        or _aggregate_match(raw_text) is not None
    ):
        return None

    basket_candidate = _deterministic_basket_candidate(raw_text)
    if basket_candidate is not None:
        return basket_candidate
    if _NOTIONAL_RE.search(raw_text):
        return None

    buy_matches = list(_BUY_RE.finditer(raw_text))
    sell_matches = list(_SELL_RE.finditer(raw_text))
    if bool(buy_matches) == bool(sell_matches):
        return None
    side_matches = buy_matches if buy_matches else sell_matches
    quantities = list(_QUANTITY_RE.finditer(raw_text))
    market_markers = list(_MARKET_RE.finditer(raw_text))
    limit_markers = list(_LIMIT_MARKER_RE.finditer(raw_text))
    if (
        len(side_matches) != 1
        or len(quantities) != 1
        or len(market_markers) > 1
        or len(limit_markers) > 1
    ):
        return None
    try:
        quantity = parse_strict_positive_integer(
            quantities[0].group("token"), max_value=MAX_QUANTITY
        )
        prices = _price_matches(raw_text, limit_markers)
    except ValueError:
        return None
    if len(prices) > 1 or (market_markers and (limit_markers or prices)):
        return None

    if market_markers:
        order_type = OrderType.MARKET
        order_type_span: tuple[int, int] | None = market_markers[0].span()
        limit_price: int | None = None
        price_span: tuple[int, int] | None = None
    elif limit_markers or prices:
        if not prices:
            return None
        order_type = OrderType.LIMIT
        order_type_span = (
            limit_markers[0].span() if limit_markers else prices[0].span
        )
        limit_price = prices[0].value
        price_span = prices[0].span
    else:
        order_type = OrderType.MARKET
        order_type_span = None
        limit_price = None
        price_span = None

    side = OrderSide.BUY if buy_matches else OrderSide.SELL
    side_match = side_matches[0]
    quantity_match = quantities[0]
    adornments = _place_order_adornment_spans(raw_text)
    consumed = [side_match.span(), quantity_match.span(), *adornments]
    if order_type_span is not None:
        consumed.append(order_type_span)
    if price_span is not None:
        consumed.append(price_span)
    if limit_markers:
        consumed.append(limit_markers[0].span())
    instrument_span = _deterministic_instrument_span(
        raw_text, list(dict.fromkeys(consumed))
    )
    if instrument_span is None:
        return None
    instrument = raw_text[instrument_span[0] : instrument_span[1]]

    evidence = [
        TextEvidence(
            field=EvidenceField.INSTRUMENT,
            start=instrument_span[0],
            end=instrument_span[1],
            text=instrument,
            normalized=instrument,
        ),
        TextEvidence(
            field=EvidenceField.SIDE,
            start=side_match.start(),
            end=side_match.end(),
            text=side_match.group(0),
            normalized=side.value,
        ),
        TextEvidence(
            field=EvidenceField.QUANTITY,
            start=quantity_match.start(),
            end=quantity_match.end(),
            text=quantity_match.group(0),
            normalized=str(quantity),
        ),
    ]
    if order_type_span is not None:
        evidence.append(
            TextEvidence(
                field=EvidenceField.ORDER_TYPE,
                start=order_type_span[0],
                end=order_type_span[1],
                text=raw_text[order_type_span[0] : order_type_span[1]],
                normalized=order_type.value,
            )
        )
    if price_span is not None and limit_price is not None:
        evidence.append(
            TextEvidence(
                field=EvidenceField.LIMIT_PRICE,
                start=price_span[0],
                end=price_span[1],
                text=raw_text[price_span[0] : price_span[1]],
                normalized=str(limit_price),
            )
        )

    candidate = HermesOrderCandidate(
        raw_text_sha256=raw_text_sha256(raw_text),
        decision=CandidateDecision.EXECUTE,
        action=DirectiveAction.PLACE_ORDER,
        instrument_mention=instrument,
        side=side,
        quantity=str(quantity),
        order_type=order_type,
        limit_price=str(limit_price) if limit_price is not None else None,
        evidence=tuple(evidence),
    )
    return (
        candidate
        if isinstance(verify_order_candidate(raw_text, candidate), VerifiedPaperDirective)
        else None
    )


def deterministic_delayed_order_plan(raw_text: str) -> DelayedPaperOrderPlan | None:
    """Parse exactly one ``N초/분/시간 뒤`` order without LLM arithmetic.

    The delay itself is never treated as an immediate-order adornment.  This
    keeps :func:`deterministic_order_candidate` fail-closed so a scheduled
    request cannot accidentally submit now if routing regresses.
    """

    if not isinstance(raw_text, str) or len(raw_text) > MAX_TEXT_LENGTH:
        return None
    matches = list(_RELATIVE_DELAY_RE.finditer(raw_text))
    if len(matches) != 1:
        return None
    match = matches[0]
    try:
        amount = parse_strict_positive_integer(
            match.group("token"), max_value=MAX_RELATIVE_DELAY_SECONDS
        )
    except ValueError:
        return None
    delay_seconds = amount * _RELATIVE_DELAY_UNIT_SECONDS[match.group("unit")]
    if delay_seconds > MAX_RELATIVE_DELAY_SECONDS:
        return None

    sanitized = (
        raw_text[: match.start()]
        + (" " * (match.end() - match.start()))
        + raw_text[match.end() :]
    )
    candidate = deterministic_order_candidate(sanitized)
    if candidate is None:
        return None
    verified = verify_order_candidate(sanitized, candidate)
    if not isinstance(verified, VerifiedPaperDirective) or verified.payload is None:
        return None
    return DelayedPaperOrderPlan(
        payload=verified.payload,
        delay_seconds=delay_seconds,
        trigger_span=match.span(),
    )


__all__ = [
    "MAX_PRICE",
    "MAX_QUANTITY",
    "MAX_TEXT_LENGTH",
    "MAX_RELATIVE_DELAY_SECONDS",
    "DelayedPaperOrderPlan",
    "deterministic_delayed_order_plan",
    "deterministic_order_candidate",
    "is_clearly_non_executable_order_language",
    "looks_like_user_order_request",
    "parse_strict_positive_integer",
    "raw_text_sha256",
    "verify_order_candidate",
]
