from __future__ import annotations

import ast
import pathlib

import pytest

from apps.api.conditional_rule_clarification import (
    CLARIFICATION_CODES,
    ClarificationClass,
    ClarificationSource,
    clarification_message,
    classify_code,
    extract_code,
    should_ask,
    split_codes,
)


def _semantic_error_codes() -> set[str]:
    """Every code ``semantic.py`` can raise, read from the source itself."""

    source = pathlib.Path(
        "orchestration/conditional_rules/semantic.py"
    ).read_text()
    codes: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "_error":
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            value = node.args[0].value
            if isinstance(value, str):
                codes.add(value)
    return codes


def test_every_validator_code_is_classified() -> None:
    """An unclassified code is how a system limit becomes a user question."""

    unclassified = _semantic_error_codes() - set(CLARIFICATION_CODES)
    assert unclassified == set(), unclassified


def test_capability_gap_states_the_limit_and_never_asks() -> None:
    """The 2026-08-28 rejection of "bollingerband(종가,2,0,20)".

    The sentence was complete; the registry had no OFFSET.  Asking the user to
    rephrase would have buried the registry hole, so this class states the
    limit and says a retype changes nothing.
    """

    code = "UNSUPPORTED_INDICATOR_PARAMETER"
    assert classify_code(code) is ClarificationClass.CAPABILITY_GAP
    assert should_ask((code,)) is False
    message = clarification_message((code,))
    assert "지원하지 않는 설정값" in message
    assert "다시 요청하셔도 동일하게 거부됩니다" in message
    assert "명시해 주세요" not in message


@pytest.mark.parametrize(
    "code",
    (
        "INTRABAR_TIMEFRAME_UNSUPPORTED",
        "INTRABAR_TIMEFRAME_MISMATCH",
        "INTRABAR_FIELD_UNSUPPORTED",
        "INTRABAR_INDICATOR_UNSUPPORTED",
    ),
)
def test_intrabar_runtime_limits_are_capability_gaps(code: str) -> None:
    assert classify_code(code) is ClarificationClass.CAPABILITY_GAP
    assert should_ask((code,)) is False


def test_ambiguous_sentence_is_asked_with_an_open_question() -> None:
    codes = ("QUANTITY_REQUIRED", "TIMEFRAME_NOT_IN_INSTRUCTION")
    assert should_ask(codes, source=ClarificationSource.HERMES_REASON) is True
    message = clarification_message(
        codes, source=ClarificationSource.HERMES_REASON
    )
    assert "매수 수량을 명시해 주세요" in message
    assert "지표의 봉 주기를 명시해 주세요" in message
    assert "거부됩니다" not in message


def test_missing_conditional_threshold_has_a_specific_open_question() -> None:
    code = "CONDITION_THRESHOLD_REQUIRED"

    assert should_ask((code,)) is True
    assert "상승·하락 조건 값" in clarification_message((code,))


def test_unresolved_conditional_instrument_is_an_open_question_not_a_defect() -> None:
    code = "paper_order_instrument_clarification_required"

    assert should_ask((code,)) is True
    assert "6자리 코드" in clarification_message((code,))


def test_misspelled_condition_expression_requests_one_correction() -> None:
    code = "CONDITION_EXPRESSION_CLARIFICATION_REQUIRED"

    assert should_ask((code,)) is True
    assert "지표 철자" in clarification_message((code,))


def test_ambiguity_labels_never_propose_a_value_of_their_own() -> None:
    """A closed question with a candidate value is the agent choosing it."""

    for code, (kind, label) in CLARIFICATION_CODES.items():
        if kind is not ClarificationClass.USER_AMBIGUITY:
            continue
        assert "할까요" not in label, code
        assert "하시겠" not in label, code


def test_notional_text_accepts_the_direct_amount_plus_order_verb_form() -> None:
    assert classify_code("NOTIONAL_AMOUNT_NOT_IN_INSTRUCTION") is ClarificationClass.USER_AMBIGUITY
    assert "100만원 매수" in clarification_message(
        ("NOTIONAL_AMOUNT_NOT_IN_INSTRUCTION",)
    )
    assert classify_code("NOTIONAL_AMOUNT_MISMATCH") is ClarificationClass.INTERPRETER_DEFECT


def test_interpreter_defect_does_not_blame_the_sentence() -> None:
    assert classify_code("UNIT_MISMATCH") is ClarificationClass.INTERPRETER_DEFECT
    assert should_ask(("UNIT_MISMATCH",)) is False
    message = clarification_message(("UNIT_MISMATCH",))
    assert "문장 문제가 아니라" in message
    assert "시스템 결함으로 기록" in message


def test_a_mixed_set_never_asks() -> None:
    codes = ("QUANTITY_REQUIRED", "UNSUPPORTED_INDICATOR")
    assert should_ask(codes, source=ClarificationSource.HERMES_REASON) is False
    assert "다시 요청하셔도 동일하게 거부됩니다" in clarification_message(codes)


def test_http_detail_mapping_never_reaches_the_user_as_a_dict() -> None:
    """What the user actually saw on 2026-08-28 was ``str(detail)``."""

    detail = {
        "code": "UNSUPPORTED_INDICATOR_PARAMETER",
        "message": "BOLLINGER has unsupported parameters ['OFFSET']",
    }
    assert extract_code(detail) == "UNSUPPORTED_INDICATOR_PARAMETER"
    message = clarification_message((extract_code(detail),))
    assert "{" not in message and "'code'" not in message


def test_annotated_code_keeps_its_classification() -> None:
    annotated = "UNSUPPORTED_PORTFOLIO_FIELD: unsupported portfolio field 'AVG_BUY_PRICE'"
    assert classify_code(annotated) is ClarificationClass.CAPABILITY_GAP


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        (ClarificationSource.HERMES_REASON, ClarificationClass.USER_AMBIGUITY),
        (ClarificationSource.AMBIGUITY_DETECTOR, ClarificationClass.USER_AMBIGUITY),
        (
            ClarificationSource.SEMANTIC_REJECTION,
            ClarificationClass.INTERPRETER_DEFECT,
        ),
    ),
)
def test_unknown_code_defaults_to_the_safe_class_for_its_source(
    source: ClarificationSource, expected: ClarificationClass
) -> None:
    assert classify_code("SOMETHING_NOBODY_CLASSIFIED", source=source) is expected


def test_joined_hermes_reason_is_split_into_the_codes_it_carries() -> None:
    """What the user actually saw on 2026-09-01.

    Hermes answered with four reasons in one string.  Classifying the join as
    a single unknown code made it a USER_AMBIGUITY question and printed the
    raw enum blob, so two registered codes silently lost their Korean labels
    and a capability gap was served back as "rephrase it".
    """

    reason = (
        "UNSUPPORTED_MULTI_STAGE_POSITION_MANAGEMENT; AMBIGUOUS_RETURN_BASELINE; "
        "QUANTITY_REQUIRED; UNSUPPORTED_COMBINED_TRAILING_OR_MOVING_AVERAGE_EXIT"
    )
    codes = split_codes(reason)
    assert len(codes) == 4
    assert should_ask(codes, source=ClarificationSource.HERMES_REASON) is False

    message = clarification_message(
        codes, source=ClarificationSource.HERMES_REASON
    )
    assert "UNSUPPORTED" not in message and "_" not in message
    assert "다시 요청하셔도 동일하게 거부됩니다" in message


def test_split_keeps_an_annotated_detail_line_whole() -> None:
    """"CODE: offending detail" may contain a semicolon of its own."""

    annotated = "UNSUPPORTED_PORTFOLIO_FIELD: unsupported field 'X'; see registry"
    assert split_codes(annotated) == (annotated,)
    assert classify_code(annotated) is ClarificationClass.CAPABILITY_GAP


def test_unregistered_unsupported_name_states_the_limit_without_leaking() -> None:
    code = "UNSUPPORTED_SOMETHING_NOBODY_REGISTERED"
    assert (
        classify_code(code, source=ClarificationSource.HERMES_REASON)
        is ClarificationClass.CAPABILITY_GAP
    )
    message = clarification_message(
        (code,), source=ClarificationSource.HERMES_REASON
    )
    assert code not in message
    assert "요청하신 조건을 현재 지원하지 않습니다" in message


def test_hermes_prose_reason_still_reaches_the_user() -> None:
    """Only enum tokens are withheld; a written reason is the point of it."""

    reason = "종목을 하나로 확정하지 못했습니다"
    assert reason in clarification_message(
        (reason,), source=ClarificationSource.HERMES_REASON
    )


def test_capability_gap_answer_drops_the_ambiguity_prompts() -> None:
    """Asking for a quantity beside "a retype changes nothing" is the loop."""

    codes = ("QUANTITY_REQUIRED", "UNSUPPORTED_MULTI_STAGE_POSITION_MANAGEMENT")
    message = clarification_message(
        codes, source=ClarificationSource.HERMES_REASON
    )
    assert "매수 수량을 명시해 주세요" not in message
    assert "독립 규칙으로 나눠 주세요" in message
