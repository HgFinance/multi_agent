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


def test_ambiguous_sentence_is_asked_with_an_open_question() -> None:
    codes = ("QUANTITY_REQUIRED", "TIMEFRAME_NOT_IN_INSTRUCTION")
    assert should_ask(codes, source=ClarificationSource.HERMES_REASON) is True
    message = clarification_message(
        codes, source=ClarificationSource.HERMES_REASON
    )
    assert "매수 수량을 명시해 주세요" in message
    assert "지표의 봉 주기를 명시해 주세요" in message
    assert "거부됩니다" not in message


def test_ambiguity_labels_never_propose_a_value_of_their_own() -> None:
    """A closed question with a candidate value is the agent choosing it."""

    for code, (kind, label) in CLARIFICATION_CODES.items():
        if kind is not ClarificationClass.USER_AMBIGUITY:
            continue
        assert "할까요" not in label, code
        assert "하시겠" not in label, code


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
