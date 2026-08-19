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
from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import ValidationError

from orchestration.contracts.user_paper_order import (
    CandidateDecision,
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

_AGGREGATE_SCOPE_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?:전량|전부|모두|모든|전체|다)"
    r"(?![가-힣A-Za-z0-9])"
)
_SELL_ALL_RE = re.compile(
    rf"^\s*(?:(?:내|현재)\s*)?"
    rf"(?:(?:보유\s*)?계좌(?:에|의|에서)?\s*)?"
    rf"(?:(?:보유(?:한|\s*중인)?|있는)\s*)?"
    rf"(?:(?:종목|주식)\s*)?"
    rf"(?:전량|전부|모두|모든|전체|다)\s*{_SELL_VERB}"
    rf"(?:\s*(?:주세요|줘))?[.!]*\s*$"
)
_CANCEL_ALL_RE = re.compile(
    rf"^\s*(?:(?:내|현재)\s*)?"
    rf"(?:"
    rf"(?:(?:미체결|대기\s*중인|대기|열린)\s*)?(?:주문|오더)\s*"
    rf"(?:전량|전부|모두|모든|전체|다)"
    rf"|(?:전량|전부|모두|모든|전체|다)\s*"
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
    r"(?:[0-9A-Za-z]{6}|[가-힣A-Za-z][가-힣A-Za-z0-9&+._\- ]{0,79})"
)
_ALLOWED_RESIDUAL_RE = re.compile(
    r"^(?:(?:을|를|은|는|이|가|에|에서|로|으로|좀|만|내|현재|계좌|"
    r"보유|종목|주식|주문|주세요|줘)\s*)*$"
)
_LEADING_DISCORD_MENTION_RE = re.compile(r"^\s*<@!?\d{15,25}>\s*")


@dataclass(frozen=True)
class _PriceMatch:
    span: tuple[int, int]
    value: int


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
    if _READ_ONLY_RE.search(raw_text):
        return OrderReasonCode.READ_ONLY_REQUEST
    if _QUESTION_RE.search(raw_text):
        return OrderReasonCode.QUESTION_OR_ADVICE
    return None


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


def _expected_evidence(
    evidence: Mapping[EvidenceField, TextEvidence],
    *,
    field: EvidenceField,
    span: tuple[int, int],
    normalized: str,
) -> bool:
    item = evidence.get(field)
    return bool(
        item
        and (item.start, item.end) == span
        and item.normalized == normalized
    )


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


def _aggregate_match(
    raw_text: str,
) -> tuple[DirectiveAction, re.Match[str], re.Match[str]] | None:
    if _SELL_ALL_RE.fullmatch(raw_text):
        actions = list(_SELL_RE.finditer(raw_text))
        scopes = list(_AGGREGATE_SCOPE_RE.finditer(raw_text))
        if len(actions) == 1 and len(scopes) == 1:
            return DirectiveAction.SELL_ALL, actions[0], scopes[0]
    if _CANCEL_ALL_RE.fullmatch(raw_text):
        actions = list(_CANCEL_RE.finditer(raw_text))
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
    if not _expected_evidence(
        evidence,
        field=EvidenceField.ACTION,
        span=action_match.span(),
        normalized=action.value,
    ) or not _expected_evidence(
        evidence,
        field=EvidenceField.AGGREGATE_SCOPE,
        span=scope_match.span(),
        normalized="ALL",
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
    # Discord preserves the bot mention in the exact authenticated source
    # text. It is delivery metadata, not unsupported trading language. Only a
    # single leading snowflake mention is ignored; mentions elsewhere remain
    # visible to the strict residual check.
    leading_mention = _LEADING_DISCORD_MENTION_RE.match(raw_text)
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

    instrument = evidence.get(EvidenceField.INSTRUMENT)
    if instrument is None:
        return _clarify(digest, OrderReasonCode.EVIDENCE_MISSING)
    if (
        candidate.instrument_mention != instrument.text
        or instrument.normalized != candidate.instrument_mention
        or not _INSTRUMENT_RE.fullmatch(instrument.text)
    ):
        return _clarify(digest, OrderReasonCode.MISSING_OR_CONFLICTING_INSTRUMENT)

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
    ) or not _expected_evidence(
        evidence,
        field=EvidenceField.QUANTITY,
        span=quantity_match.span(),
        normalized=str(quantity),
    ) or (
        order_type_span is not None
        and not _expected_evidence(
            evidence,
            field=EvidenceField.ORDER_TYPE,
            span=order_type_span,
            normalized=order_type.value,
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

    consumed = [
        side_match.span(),
        quantity_match.span(),
        (instrument.start, instrument.end),
    ]
    if order_type_span is not None:
        consumed.append(order_type_span)
    if price_span is not None:
        consumed.append(price_span)
    if limit_markers:
        consumed.append(limit_markers[0].span())
    if not _residual_supported(raw_text, list(dict.fromkeys(consumed))):
        return _clarify(digest, OrderReasonCode.UNSUPPORTED_TEXT)

    return VerifiedPaperDirective(
        raw_text_sha256=digest,
        action=DirectiveAction.PLACE_ORDER,
        payload=CanonicalPlaceOrderPayload(
            instrument_mention=candidate.instrument_mention,
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

    unsafe = _unsafe_language(raw_text)
    if unsafe is not None:
        return _not_order(digest, unsafe)
    if _NOTIONAL_RE.search(raw_text):
        return _clarify(digest, OrderReasonCode.NOTIONAL_UNSUPPORTED)
    if _APPROXIMATE_RE.search(raw_text):
        return _clarify(digest, OrderReasonCode.APPROXIMATE_VALUE)
    if _COMPOUND_RE.search(raw_text) or re.search(r"(?<!\d)[.!]\s*\S", raw_text):
        return _clarify(digest, OrderReasonCode.MULTIPLE_COMMANDS)

    aggregate = _aggregate_match(raw_text)
    if structured.decision is not CandidateDecision.EXECUTE:
        if aggregate or _ORDER_CONTEXT_RE.search(raw_text):
            return _clarify(digest, OrderReasonCode.HERMES_DID_NOT_PROPOSE_EXECUTION)
        return _not_order(digest, OrderReasonCode.NO_ORDER_COMMAND)

    evidence, evidence_error = _validate_evidence(raw_text, structured)
    if evidence_error is not None or evidence is None:
        return _clarify(
            digest, evidence_error or OrderReasonCode.EVIDENCE_SPAN_INVALID
        )
    if aggregate is not None:
        return _verify_aggregate(raw_text, digest, structured, evidence, aggregate)
    return _verify_place_order(raw_text, digest, structured, evidence)


__all__ = [
    "MAX_PRICE",
    "MAX_QUANTITY",
    "MAX_TEXT_LENGTH",
    "is_clearly_non_executable_order_language",
    "looks_like_user_order_request",
    "parse_strict_positive_integer",
    "raw_text_sha256",
    "verify_order_candidate",
]
