from __future__ import annotations

import pytest

from orchestration.contracts.user_paper_order import (
    CandidateDecision,
    DirectiveAction,
    EvidenceField,
    HermesOrderCandidate,
    NotOrder,
    OrderClarification,
    OrderReasonCode,
    OrderSide,
    OrderType,
    TextEvidence,
    VerifiedPaperDirective,
)
from orchestration.user_order_language import (
    MAX_PRICE,
    deterministic_order_candidate,
    is_clearly_non_executable_order_language,
    looks_like_user_order_request,
    parse_strict_positive_integer,
    raw_text_sha256,
    verify_order_candidate,
)


@pytest.mark.parametrize(
    ("raw", "instrument", "side", "quantity", "order_type", "limit_price"),
    [
        (
            "<@1536991290842030130> 삼성전자 3주 매수",
            "삼성전자",
            OrderSide.BUY,
            "3",
            OrderType.MARKET,
            None,
        ),
        (
            "SK하이닉스 보유수량 확인해서 시장가로 1주 매도",
            "SK하이닉스",
            OrderSide.SELL,
            "1",
            OrderType.MARKET,
            None,
        ),
        (
            "내 PAPER 계좌에서 보유 중인 삼성전자 2주 시장가 매도해줘",
            "삼성전자",
            OrderSide.SELL,
            "2",
            OrderType.MARKET,
            None,
        ),
        (
            "지금 삼성전자 한 주 시장가로 매수 주문 넣어주세요",
            "삼성전자",
            OrderSide.BUY,
            "1",
            OrderType.MARKET,
            None,
        ),
        (
            "삼성전자 3주 27만원 지정가로 매수",
            "삼성전자",
            OrderSide.BUY,
            "3",
            OrderType.LIMIT,
            "270000",
        ),
        (
            "124500 아이티센글로벌 시장가로 30주 매수해줘",
            "124500",
            OrderSide.BUY,
            "30",
            OrderType.MARKET,
            None,
        ),
    ],
)
def test_deterministic_candidate_builds_exact_verified_order_evidence(
    raw: str,
    instrument: str,
    side: OrderSide,
    quantity: str,
    order_type: OrderType,
    limit_price: str | None,
) -> None:
    candidate = deterministic_order_candidate(raw)
    assert candidate is not None
    verified = verify_order_candidate(raw, candidate)
    assert isinstance(verified, VerifiedPaperDirective)
    assert verified.payload is not None
    assert verified.payload.instrument_mention == instrument
    assert verified.payload.side is side
    assert verified.payload.quantity == quantity
    assert verified.payload.order_type is order_type
    assert verified.payload.limit_price == limit_price
    for evidence in candidate.evidence:
        assert raw[evidence.start : evidence.end] == evidence.text


@pytest.mark.parametrize(
    "raw",
    [
        "삼성전자 3주 매수해도 될까?",
        "삼성전자 3주 매수하지 마",
        "삼성전자가 오르면 3주 매수",
        "예시: 삼성전자 3주 매수",
        "삼성전자와 SK하이닉스 각각 1주 매수",
        "삼성전자 100만원어치 매수",
        "LIVE 계좌로 삼성전자 3주 매수",
        "삼성전자 3주 매수하고 1주 매도",
    ],
)
def test_deterministic_candidate_rejects_unsafe_or_ambiguous_language(raw: str) -> None:
    assert deterministic_order_candidate(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "삼성전자 10주 시장가 매수?",
        "삼성전자 매수하지 마",
        "만약 오르면 삼성전자 10주 시장가 매수",
        "삼성전자 매수 내역 알려줘",
        "예시: 삼성전자 10주 시장가 매수",
    ],
)
def test_clearly_non_executable_language_stays_in_advisory_chat(raw: str) -> None:
    assert is_clearly_non_executable_order_language(raw)


def test_actionable_and_live_language_still_reaches_strict_order_lane() -> None:
    assert not is_clearly_non_executable_order_language("삼성전자 매수 10주 시장가")
    assert not is_clearly_non_executable_order_language(
        "SK하이닉스 보유수량 확인해서 시장가로 1주 매도"
    )
    assert not is_clearly_non_executable_order_language(
        "실계좌로 삼성전자 매수 10주 시장가"
    )


def _evidence(
    raw: str,
    field: EvidenceField,
    text: str,
    normalized: str,
    *,
    occurrence: int = 0,
) -> TextEvidence:
    start = -1
    cursor = 0
    for _ in range(occurrence + 1):
        start = raw.index(text, cursor)
        cursor = start + len(text)
    return TextEvidence(
        field=field,
        start=start,
        end=start + len(text),
        text=text,
        normalized=normalized,
    )


def _place_candidate(
    raw: str,
    *,
    instrument: str,
    side_text: str,
    side: OrderSide,
    quantity_text: str,
    quantity: int,
    order_type_text: str | None,
    order_type: OrderType,
    limit_price_text: str | None = None,
    limit_price: int | None = None,
) -> HermesOrderCandidate:
    evidence = [
        _evidence(raw, EvidenceField.INSTRUMENT, instrument, instrument),
        _evidence(raw, EvidenceField.SIDE, side_text, side.value),
        _evidence(raw, EvidenceField.QUANTITY, quantity_text, str(quantity)),
    ]
    if order_type_text is not None:
        evidence.append(
            _evidence(
                raw,
                EvidenceField.ORDER_TYPE,
                order_type_text,
                order_type.value,
            )
        )
    if limit_price_text is not None:
        evidence.append(
            _evidence(
                raw,
                EvidenceField.LIMIT_PRICE,
                limit_price_text,
                str(limit_price),
            )
        )
    return HermesOrderCandidate(
        raw_text_sha256=raw_text_sha256(raw),
        decision=CandidateDecision.EXECUTE,
        action=DirectiveAction.PLACE_ORDER,
        instrument_mention=instrument,
        side=side,
        quantity=str(quantity),
        order_type=order_type,
        limit_price=str(limit_price) if limit_price is not None else None,
        evidence=tuple(evidence),
    )


def _aggregate_candidate(
    raw: str,
    *,
    action: DirectiveAction,
    action_text: str,
    scope_text: str,
) -> HermesOrderCandidate:
    return HermesOrderCandidate(
        raw_text_sha256=raw_text_sha256(raw),
        decision=CandidateDecision.EXECUTE,
        action=action,
        evidence=(
            _evidence(raw, EvidenceField.ACTION, action_text, action.value),
            _evidence(raw, EvidenceField.AGGREGATE_SCOPE, scope_text, "ALL"),
        ),
    )


def _non_order_candidate(raw: str) -> HermesOrderCandidate:
    return HermesOrderCandidate(
        raw_text_sha256=raw_text_sha256(raw),
        decision=CandidateDecision.NOT_ORDER,
        reason_codes=(OrderReasonCode.NO_ORDER_COMMAND,),
    )


def _sino(value: int) -> str:
    digits = " 일이삼사오육칠팔구"
    if value < 10:
        return digits[value]
    tens, ones = divmod(value, 10)
    return ("" if tens == 1 else digits[tens]) + "십" + (
        digits[ones] if ones else ""
    )


def _native(value: int) -> str:
    ones = {
        1: "한",
        2: "두",
        3: "세",
        4: "네",
        5: "다섯",
        6: "여섯",
        7: "일곱",
        8: "여덟",
        9: "아홉",
    }
    tens = {
        10: "열",
        20: "스물",
        30: "서른",
        40: "마흔",
        50: "쉰",
        60: "예순",
        70: "일흔",
        80: "여든",
        90: "아흔",
    }
    if value < 10:
        return ones[value]
    decade, unit = divmod(value, 10)
    return tens[decade * 10] + (ones[unit] if unit else "")


def _execute_place(raw: str, candidate: HermesOrderCandidate) -> VerifiedPaperDirective:
    result = verify_order_candidate(raw, candidate)
    assert isinstance(result, VerifiedPaperDirective), result
    assert result.action is DirectiveAction.PLACE_ORDER
    assert result.payload is not None
    return result


def test_place_order_accepts_redundant_exact_action_side_evidence() -> None:
    """Hermes may ground PLACE_ORDER and BUY in the same exact buy verb."""

    raw = "<@1536991290842030130> 삼성전자 3주 시장가 매수"
    candidate = _place_candidate(
        raw,
        instrument="삼성전자",
        side_text="매수",
        side=OrderSide.BUY,
        quantity_text="3주",
        quantity=3,
        order_type_text="시장가",
        order_type=OrderType.MARKET,
    )
    payload = candidate.model_dump(mode="json")
    payload["evidence"].append(
        _evidence(
            raw,
            EvidenceField.ACTION,
            "매수",
            DirectiveAction.PLACE_ORDER.value,
        ).model_dump(mode="json")
    )

    result = verify_order_candidate(raw, payload)

    assert isinstance(result, VerifiedPaperDirective)
    assert result.payload is not None
    assert result.payload.quantity == "3"


def test_place_order_accepts_exact_semantic_tokens_from_inflected_command() -> None:
    raw = "124500 아이티센글로벌 시장가로 30주 매수해줘"
    result = _execute_place(
        raw,
        _place_candidate(
            raw,
            instrument="아이티센글로벌",
            side_text="매수",
            side=OrderSide.BUY,
            quantity_text="30",
            quantity=30,
            order_type_text="시장가",
            order_type=OrderType.MARKET,
        ),
    )

    assert result.payload is not None
    assert result.payload.instrument_mention == "124500"
    assert result.payload.quantity == "30"


def test_exact_user_example_compiles_to_unresolved_paper_payload() -> None:
    raw = "삼성전자 매수 10주 시장가"
    result = _execute_place(
        raw,
        _place_candidate(
            raw,
            instrument="삼성전자",
            side_text="매수",
            side=OrderSide.BUY,
            quantity_text="10주",
            quantity=10,
            order_type_text="시장가",
            order_type=OrderType.MARKET,
        ),
    )

    assert result.mode == "PAPER"
    assert result.binding is False
    assert result.requires_authenticated_admission is True
    assert result.payload.instrument_mention == "삼성전자"
    assert result.payload.quantity == "10"
    assert result.canonical_payload() == {
        "instrument_mention": "삼성전자",
        "side": "BUY",
        "quantity": "10",
        "order_type": "MARKET",
        "time_in_force": "DAY",
        "limit_price": None,
    }


def test_holdings_preflight_followed_by_imperative_sell_executes() -> None:
    raw = "<@1536991290842030130> SK하이닉스 보유수량 확인해서 시장가로 1주 매도"
    result = _execute_place(
        raw,
        _place_candidate(
            raw,
            instrument="SK하이닉스",
            side_text="매도",
            side=OrderSide.SELL,
            quantity_text="1주",
            quantity=1,
            order_type_text="시장가로",
            order_type=OrderType.MARKET,
        ),
    )

    assert result.canonical_payload() == {
        "instrument_mention": "SK하이닉스",
        "side": "SELL",
        "quantity": "1",
        "order_type": "MARKET",
        "time_in_force": "DAY",
        "limit_price": None,
    }


@pytest.mark.parametrize(
    ("raw", "instrument", "side_text", "side", "quantity_text", "quantity", "order_type_text"),
    [
        (
            "내 PAPER 계좌에서 보유 중인 삼성전자 2주 시장가 매도해줘",
            "삼성전자",
            "매도해줘",
            OrderSide.SELL,
            "2주",
            2,
            "시장가",
        ),
        (
            "지금 삼성전자 한 주 시장가로 매수 주문 넣어주세요",
            "삼성전자",
            "매수",
            OrderSide.BUY,
            "한 주",
            1,
            "시장가로",
        ),
        (
            "모의투자 계좌에서 SK하이닉스 1주 팔아줘",
            "SK하이닉스",
            "팔아줘",
            OrderSide.SELL,
            "1주",
            1,
            None,
        ),
        (
            "현재 보유잔고 조회 후 SK하이닉스 1주 매도 요청",
            "SK하이닉스",
            "매도",
            OrderSide.SELL,
            "1주",
            1,
            None,
        ),
        (
            "바로 삼성전자 1주 시장가 매수 주문 실행해줘",
            "삼성전자",
            "매수",
            OrderSide.BUY,
            "1주",
            1,
            "시장가",
        ),
    ],
)
def test_safe_natural_order_adornments_preserve_exact_execution_fields(
    raw: str,
    instrument: str,
    side_text: str,
    side: OrderSide,
    quantity_text: str,
    quantity: int,
    order_type_text: str | None,
) -> None:
    result = _execute_place(
        raw,
        _place_candidate(
            raw,
            instrument=instrument,
            side_text=side_text,
            side=side,
            quantity_text=quantity_text,
            quantity=quantity,
            order_type_text=order_type_text,
            order_type=OrderType.MARKET,
        ),
    )

    assert result.payload is not None
    assert result.payload.instrument_mention == instrument
    assert result.payload.side is side
    assert result.payload.quantity == str(quantity)
    assert result.payload.order_type is OrderType.MARKET


@pytest.mark.parametrize(
    "raw",
    [
        "SK하이닉스 보유수량 확인해서 1주 매도해도 될까?",
        "PAPER 계좌에서 SK하이닉스 1주 매도하지 마",
        "SK하이닉스 현재가 확인해서 시장가 1주 매도",
        "SK하이닉스 보유수량 확인해줘 그리고 1주 매도",
    ],
)
def test_natural_order_language_never_weakens_speech_act_guards(raw: str) -> None:
    assert is_clearly_non_executable_order_language(raw)


def test_explicit_won_price_compiles_as_limit_and_preserves_code_mention() -> None:
    raw = "005930 5주 70,000원에 매수"
    result = _execute_place(
        raw,
        _place_candidate(
            raw,
            instrument="005930",
            side_text="매수",
            side=OrderSide.BUY,
            quantity_text="5주",
            quantity=5,
            order_type_text="70,000원",
            order_type=OrderType.LIMIT,
            limit_price_text="70,000원",
            limit_price=70_000,
        ),
    )
    assert result.payload is not None
    assert result.payload.instrument_mention == "005930"
    assert result.payload.order_type is OrderType.LIMIT
    assert result.payload.limit_price == "70000"


def test_alphanumeric_krx_code_is_valid_exact_instrument_evidence() -> None:
    raw = "00088k 5주 시장가 매수"
    result = _execute_place(
        raw,
        _place_candidate(
            raw,
            instrument="00088k",
            side_text="매수",
            side=OrderSide.BUY,
            quantity_text="5주",
            quantity=5,
            order_type_text="시장가",
            order_type=OrderType.MARKET,
        ),
    )
    assert result.payload is not None
    assert result.payload.instrument_mention == "00088k"


@pytest.mark.parametrize(
    ("price_text", "price"),
    [("지정가 70000", 70_000), ("지정가 7만원", 70_000), ("지정가 칠만원", 70_000)],
)
def test_explicit_limit_marker_supports_strict_price_forms(
    price_text: str, price: int
) -> None:
    raw = f"삼성전자 3주 {price_text} 매수"
    marker = "지정가"
    amount = price_text.removeprefix("지정가 ")
    result = _execute_place(
        raw,
        _place_candidate(
            raw,
            instrument="삼성전자",
            side_text="매수",
            side=OrderSide.BUY,
            quantity_text="3주",
            quantity=3,
            order_type_text=marker,
            order_type=OrderType.LIMIT,
            limit_price_text=amount,
            limit_price=price,
        ),
    )
    assert result.payload is not None
    assert result.payload.limit_price == "70000"


def test_sino_and_native_korean_numbers_cover_every_value_one_through_99() -> None:
    for value in range(1, 100):
        assert parse_strict_positive_integer(_sino(value)) == value
        assert parse_strict_positive_integer(_native(value)) == value
    assert parse_strict_positive_integer("스무") == 20


@pytest.mark.parametrize(
    ("quantity_text", "quantity"),
    [("한 주", 1), ("열한 주", 11), ("스물한 주", 21), ("구십구 주", 99), ("아흔아홉 주", 99)],
)
def test_korean_quantity_evidence_compiles(
    quantity_text: str, quantity: int
) -> None:
    raw = f"삼성전자 {quantity_text} 시장가 매수"
    result = _execute_place(
        raw,
        _place_candidate(
            raw,
            instrument="삼성전자",
            side_text="매수",
            side=OrderSide.BUY,
            quantity_text=quantity_text,
            quantity=quantity,
            order_type_text="시장가",
            order_type=OrderType.MARKET,
        ),
    )
    assert result.payload is not None
    assert result.payload.quantity == str(quantity)


@pytest.mark.parametrize(
    "token",
    ["0", "01", "+1", "-1", "1.5", "1,2", "7,0,000", "한두", "십여", "백"],
)
def test_noncanonical_and_out_of_scope_integer_forms_are_rejected(token: str) -> None:
    with pytest.raises(ValueError):
        parse_strict_positive_integer(token)
    with pytest.raises(ValueError):
        parse_strict_positive_integer(str(MAX_PRICE + 1))


@pytest.mark.parametrize(
    ("raw", "action", "action_text", "scope_text"),
    [
        ("보유종목 전량 매도해", DirectiveAction.SELL_ALL, "매도해", "전량"),
        ("내 계좌 주식 모두 팔아줘", DirectiveAction.SELL_ALL, "팔아줘", "모두"),
        ("미체결 주문 전부 취소해", DirectiveAction.CANCEL_ALL, "취소해", "전부"),
        ("모든 열린 주문 철회해줘", DirectiveAction.CANCEL_ALL, "철회해줘", "모든"),
    ],
)
def test_clear_aggregate_commands_compile(
    raw: str,
    action: DirectiveAction,
    action_text: str,
    scope_text: str,
) -> None:
    result = verify_order_candidate(
        raw,
        _aggregate_candidate(
            raw,
            action=action,
            action_text=action_text,
            scope_text=scope_text,
        ),
    )
    assert isinstance(result, VerifiedPaperDirective)
    assert result.action is action
    assert result.payload is None
    assert result.canonical_payload() == {}


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("삼성전자 10주 시장가 매수?", OrderReasonCode.QUESTION_OR_ADVICE),
        ("삼성전자 10주 시장가 매수해도 돼", OrderReasonCode.QUESTION_OR_ADVICE),
        ("삼성전자 10주 시장가 매수하지 마", OrderReasonCode.NEGATED_OR_PROHIBITED),
        ("보유종목 전량 매도하지 마", OrderReasonCode.NEGATED_OR_PROHIBITED),
        ("만약 오르면 삼성전자 10주 시장가 매수", OrderReasonCode.CONDITIONAL_OR_HYPOTHETICAL),
        ("삼성전자 10주 시장가 매수 내역 알려줘", OrderReasonCode.READ_ONLY_REQUEST),
        ("예시: 삼성전자 10주 시장가 매수", OrderReasonCode.EXAMPLE_OR_QUOTED_TEXT),
        ("'삼성전자 10주 시장가 매수'라고 입력하면", OrderReasonCode.EXAMPLE_OR_QUOTED_TEXT),
    ],
)
def test_non_imperative_language_never_executes(
    raw: str, reason: OrderReasonCode
) -> None:
    # A hostile Hermes can still propose EXECUTE.  The deterministic speech-act
    # guard runs before compilation and downgrades it to NOT_ORDER.
    candidate = (
        _aggregate_candidate(
            raw,
            action=DirectiveAction.SELL_ALL,
            action_text="매도",
            scope_text="전량",
        )
        if raw.startswith("보유종목")
        else _place_candidate(
            raw,
            instrument="삼성전자",
            side_text="매수",
            side=OrderSide.BUY,
            quantity_text="10주",
            quantity=10,
            order_type_text="시장가",
            order_type=OrderType.MARKET,
        )
    )
    result = verify_order_candidate(raw, candidate)
    assert isinstance(result, NotOrder)
    assert reason in result.reason_codes


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("삼성전자 10주 시장가 매수하고 현대차 2주 시장가 매도", OrderReasonCode.MULTIPLE_COMMANDS),
        ("삼성전자 약 10주 시장가 매수", OrderReasonCode.APPROXIMATE_VALUE),
        ("삼성전자 100만원어치 시장가 매수", OrderReasonCode.NOTIONAL_UNSUPPORTED),
    ],
)
def test_compound_approximate_and_notional_language_requires_clarification(
    raw: str, reason: OrderReasonCode
) -> None:
    instrument = "삼성전자"
    quantity_text = "10주" if "10주" in raw else "100만원"
    candidate = _place_candidate(
        raw,
        instrument=instrument,
        side_text="매수",
        side=OrderSide.BUY,
        quantity_text=quantity_text,
        quantity=10 if "10주" in raw else 100,
        order_type_text="시장가",
        order_type=OrderType.MARKET,
    )
    result = verify_order_candidate(raw, candidate)
    assert isinstance(result, OrderClarification)
    assert reason in result.reason_codes


@pytest.mark.parametrize("raw", ["삼성전자 0주 시장가 매수", "삼성전자 1,2주 시장가 매수", "삼성전자 1.5주 시장가 매수"])
def test_bad_quantity_cannot_compile(raw: str) -> None:
    token = raw.split()[1]
    candidate = _place_candidate(
        raw,
        instrument="삼성전자",
        side_text="매수",
        side=OrderSide.BUY,
        quantity_text=token,
        quantity=12,
        order_type_text="시장가",
        order_type=OrderType.MARKET,
    )
    result = verify_order_candidate(raw, candidate)
    assert isinstance(result, OrderClarification)
    assert OrderReasonCode.INVALID_NUMBER in result.reason_codes


def test_market_plus_price_is_fail_closed() -> None:
    raw = "삼성전자 10주 시장가 70,000원에 매수"
    candidate = _place_candidate(
        raw,
        instrument="삼성전자",
        side_text="매수",
        side=OrderSide.BUY,
        quantity_text="10주",
        quantity=10,
        order_type_text="시장가",
        order_type=OrderType.MARKET,
    )
    result = verify_order_candidate(raw, candidate)
    assert isinstance(result, OrderClarification)
    assert OrderReasonCode.CONFLICTING_MARKET_AND_PRICE in result.reason_codes


def test_missing_order_type_defaults_to_market_without_fabricated_evidence() -> None:
    raw = "삼성전자 2주 매수해"
    result = _execute_place(
        raw,
        _place_candidate(
            raw,
            instrument="삼성전자",
            side_text="매수해",
            side=OrderSide.BUY,
            quantity_text="2주",
            quantity=2,
            order_type_text=None,
            order_type=OrderType.MARKET,
        ),
    )
    assert result.canonical_payload() == {
        "instrument_mention": "삼성전자",
        "side": "BUY",
        "quantity": "2",
        "order_type": "MARKET",
        "time_in_force": "DAY",
        "limit_price": None,
    }
    assert {item.field for item in result.evidence} == {
        EvidenceField.INSTRUMENT,
        EvidenceField.SIDE,
        EvidenceField.QUANTITY,
    }


def test_explicit_market_still_requires_exact_order_type_evidence() -> None:
    raw = "삼성전자 2주 시장가 매수해"
    candidate = _place_candidate(
        raw,
        instrument="삼성전자",
        side_text="매수해",
        side=OrderSide.BUY,
        quantity_text="2주",
        quantity=2,
        order_type_text="시장가",
        order_type=OrderType.MARKET,
    )
    payload = candidate.model_dump(mode="json")
    payload["evidence"] = [
        item for item in payload["evidence"] if item["field"] != "ORDER_TYPE"
    ]
    result = verify_order_candidate(raw, payload)
    assert isinstance(result, OrderClarification)
    assert result.reason_codes == (OrderReasonCode.EVIDENCE_FIELD_MISMATCH,)


def test_limit_marker_without_price_still_requires_clarification() -> None:
    raw = "삼성전자 2주 지정가로 매수해"
    candidate = _place_candidate(
        raw,
        instrument="삼성전자",
        side_text="매수해",
        side=OrderSide.BUY,
        quantity_text="2주",
        quantity=2,
        order_type_text=None,
        order_type=OrderType.MARKET,
    )
    result = verify_order_candidate(raw, candidate)
    assert isinstance(result, OrderClarification)
    assert result.reason_codes == (OrderReasonCode.MISSING_LIMIT_PRICE,)


def test_candidate_value_must_match_deterministic_number() -> None:
    raw = "삼성전자 10주 시장가 매수"
    candidate = _place_candidate(
        raw,
        instrument="삼성전자",
        side_text="매수",
        side=OrderSide.BUY,
        quantity_text="10주",
        quantity=11,
        order_type_text="시장가",
        order_type=OrderType.MARKET,
    )
    result = verify_order_candidate(raw, candidate)
    assert isinstance(result, OrderClarification)
    assert OrderReasonCode.CANDIDATE_MISMATCH in result.reason_codes


def test_evidence_must_be_exact_original_substring_at_exact_span() -> None:
    raw = "삼성전자 10주 시장가 매수"
    candidate = _place_candidate(
        raw,
        instrument="삼성전자",
        side_text="매수",
        side=OrderSide.BUY,
        quantity_text="10주",
        quantity=10,
        order_type_text="시장가",
        order_type=OrderType.MARKET,
    )
    payload = candidate.model_dump(mode="json")
    payload["evidence"][0]["start"] += 1
    payload["evidence"][0]["end"] += 1
    result = verify_order_candidate(raw, payload)
    assert isinstance(result, OrderClarification)
    assert OrderReasonCode.EVIDENCE_TEXT_MISMATCH in result.reason_codes


def test_discord_mention_trailing_spaces_are_one_uniform_delivery_offset() -> None:
    raw = "<@1536991290842030130>  SK하이닉스 2주 시장가 매도"
    candidate = _place_candidate(
        raw,
        instrument="SK하이닉스",
        side_text="매도",
        side=OrderSide.SELL,
        quantity_text="2주",
        quantity=2,
        order_type_text="시장가",
        order_type=OrderType.MARKET,
    )
    payload = candidate.model_dump(mode="json")
    for evidence in payload["evidence"]:
        evidence["start"] -= 2
        evidence["end"] -= 2

    result = verify_order_candidate(raw, payload)

    assert isinstance(result, VerifiedPaperDirective)
    assert result.payload is not None
    assert result.payload.instrument_mention == "SK하이닉스"
    assert result.payload.quantity == "2"


def test_discord_offset_repair_rejects_non_uniform_evidence_drift() -> None:
    raw = "<@1536991290842030130>  SK하이닉스 2주 시장가 매도"
    candidate = _place_candidate(
        raw,
        instrument="SK하이닉스",
        side_text="매도",
        side=OrderSide.SELL,
        quantity_text="2주",
        quantity=2,
        order_type_text="시장가",
        order_type=OrderType.MARKET,
    )
    payload = candidate.model_dump(mode="json")
    for evidence in payload["evidence"]:
        evidence["start"] -= 2
        evidence["end"] -= 2
    payload["evidence"][0]["start"] -= 1
    payload["evidence"][0]["end"] -= 1

    result = verify_order_candidate(raw, payload)

    assert isinstance(result, OrderClarification)
    assert result.reason_codes == (OrderReasonCode.EVIDENCE_TEXT_MISMATCH,)


def test_partial_instrument_substring_leaves_unsupported_residual() -> None:
    raw = "삼성전자 10주 시장가 매수"
    candidate = _place_candidate(
        raw,
        instrument="삼성",
        side_text="매수",
        side=OrderSide.BUY,
        quantity_text="10주",
        quantity=10,
        order_type_text="시장가",
        order_type=OrderType.MARKET,
    )
    result = verify_order_candidate(raw, candidate)
    assert isinstance(result, OrderClarification)
    assert OrderReasonCode.UNSUPPORTED_TEXT in result.reason_codes


def test_raw_hash_binds_candidate_to_one_exact_message() -> None:
    raw = "삼성전자 10주 시장가 매수"
    candidate = _place_candidate(
        raw,
        instrument="삼성전자",
        side_text="매수",
        side=OrderSide.BUY,
        quantity_text="10주",
        quantity=10,
        order_type_text="시장가",
        order_type=OrderType.MARKET,
    )
    result = verify_order_candidate(raw + " ", candidate)
    assert isinstance(result, OrderClarification)
    assert OrderReasonCode.RAW_TEXT_HASH_MISMATCH in result.reason_codes


def test_candidate_cannot_select_live_or_claim_binding_authority() -> None:
    raw = "삼성전자 10주 시장가 매수"
    payload = _place_candidate(
        raw,
        instrument="삼성전자",
        side_text="매수",
        side=OrderSide.BUY,
        quantity_text="10주",
        quantity=10,
        order_type_text="시장가",
        order_type=OrderType.MARKET,
    ).model_dump(mode="json")
    payload["mode"] = "LIVE"
    payload["binding"] = True
    result = verify_order_candidate(raw, payload)
    assert isinstance(result, OrderClarification)
    assert OrderReasonCode.INVALID_CANDIDATE_SCHEMA in result.reason_codes


def test_plain_non_order_remains_not_order() -> None:
    raw = "오늘 포트폴리오 브리핑 부탁해"
    result = verify_order_candidate(raw, _non_order_candidate(raw))
    assert isinstance(result, NotOrder)
    assert result.reason_codes == (OrderReasonCode.NO_ORDER_COMMAND,)


@pytest.mark.parametrize(
    "raw",
    [
        "삼성전자 10주 매수해",
        "삼성전자 사도 돼?",
        "매수하지 마",
        "미체결 주문 취소 내역 알려줘",
        "보유 종목 전량 청산해",
        "BUY 10 shares of 005930",
        "cancel the open order",
        "liquidate all positions",
        "실계좌로 해줘",
        "실전투자로 전환해",
        "LIVE account please",
        "real-money order",
    ],
)
def test_routing_detector_has_high_recall_without_granting_authority(raw: str) -> None:
    assert looks_like_user_order_request(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        "삼성전자 재무제표 분석해줘",
        "오늘 포트폴리오 수익률 알려줘",
        "반도체 업황을 조사해줘",
        "안녕하세요",
        "",
    ],
)
def test_routing_detector_ignores_ordinary_advisory_text(raw: str) -> None:
    assert looks_like_user_order_request(raw) is False


@pytest.mark.parametrize(
    "prefix",
    ["LIVE로", "실계좌로", "실전투자로", "실거래로", "real account에서"],
)
def test_explicit_live_request_is_never_silently_converted_to_paper(prefix: str) -> None:
    raw = f"{prefix} 삼성전자 10주 시장가 매수"
    candidate = _place_candidate(
        raw,
        instrument="삼성전자",
        side_text="매수",
        side=OrderSide.BUY,
        quantity_text="10주",
        quantity=10,
        order_type_text="시장가",
        order_type=OrderType.MARKET,
    )
    result = verify_order_candidate(raw, candidate)
    assert isinstance(result, OrderClarification)
    assert result.reason_codes == (OrderReasonCode.LIVE_MODE_FORBIDDEN,)


def test_invalid_candidate_shape_is_fail_closed_not_an_exception() -> None:
    raw = "삼성전자 10주 시장가 매수"
    result = verify_order_candidate(
        raw,
        {
            "schema_version": "user-paper-order-interpretation.v1",
            "mode": "PAPER",
            "binding": False,
            "raw_text_sha256": raw_text_sha256(raw),
            "decision": "EXECUTE",
            "action": "PLACE_ORDER",
            # missing execution fields and evidence
        },
    )
    assert isinstance(result, OrderClarification)
    assert result.reason_codes == (OrderReasonCode.INVALID_CANDIDATE_SCHEMA,)
