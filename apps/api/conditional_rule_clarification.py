"""Decide whether a rejected conditional rule is a question for the user.

A rejection is a question only when the *user's sentence* carried more than one
reading.  When the sentence was complete and the system simply could not
express it, asking the user to rephrase pushes a system defect onto them and
hides the gap: ``bollingerband(종가,2,0,20)`` was rejected on 2026-08-28 for an
OFFSET parameter the registry had never declared, and a re-question loop would
have had the user retype the instruction until the offset disappeared, leaving
the registry hole in place and the rule quietly wrong.

So the class of the code decides, never the fact of the rejection:

``USER_AMBIGUITY``
    The instruction genuinely reads two ways.  Ask, with an open question that
    names the missing fact and proposes no value of its own.
``CAPABILITY_GAP``
    The instruction was clear and the platform cannot express it.  State the
    limit and, where one exists, the supported alternative.  Never invite the
    same sentence back.
``INTERPRETER_DEFECT``
    The platform supports the request but the interpreter emitted a malformed
    AST.  Say so plainly and record it; it is not the user's sentence to fix.

A mixed set never asks: the strongest reason not to ask wins, because a
capability gap blocks the rule whatever the user answers.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class ClarificationClass(StrEnum):
    USER_AMBIGUITY = "USER_AMBIGUITY"
    CAPABILITY_GAP = "CAPABILITY_GAP"
    INTERPRETER_DEFECT = "INTERPRETER_DEFECT"


class ClarificationSource(StrEnum):
    """Where an unclassified code came from, which sets its safe default."""

    # Hermes fills a free-form reason only after deciding the sentence was
    # ambiguous, so an unrecognised value there is a question, not a defect.
    HERMES_REASON = "HERMES_REASON"
    # The deterministic detector emits ambiguity codes exclusively.
    AMBIGUITY_DETECTOR = "AMBIGUITY_DETECTOR"
    # A validator code we have not classified is a system-side omission.
    SEMANTIC_REJECTION = "SEMANTIC_REJECTION"


_SOURCE_DEFAULT: dict[ClarificationSource, ClarificationClass] = {
    ClarificationSource.HERMES_REASON: ClarificationClass.USER_AMBIGUITY,
    ClarificationSource.AMBIGUITY_DETECTOR: ClarificationClass.USER_AMBIGUITY,
    ClarificationSource.SEMANTIC_REJECTION: ClarificationClass.INTERPRETER_DEFECT,
}


_ASK = ClarificationClass.USER_AMBIGUITY
_GAP = ClarificationClass.CAPABILITY_GAP
_DEFECT = ClarificationClass.INTERPRETER_DEFECT


# code -> (class, user-facing Korean text).  An ambiguity entry asks for the
# missing fact without naming a candidate value; proposing one would let the
# agent choose the threshold and collect a rubber stamp for it.
CLARIFICATION_CODES: dict[str, tuple[ClarificationClass, str]] = {
    # -- the instruction reads two ways -----------------------------------
    "AMBIGUOUS_POSITION_PERCENT": (
        _ASK,
        "매도 비중의 기준(보유수량 기준)을 명시해 주세요",
    ),
    "NOTIONAL_AMOUNT_NOT_IN_INSTRUCTION": (
        _ASK,
        "최대 주문금액을 '100만원 매수'처럼 금액과 주문 동사를 함께 명시해 주세요",
    ),
    "NOTIONAL_AMOUNT_MISMATCH": (
        _DEFECT,
        "원문에 적힌 최대 주문금액과 조건주문 해석값이 달라 시스템 결함으로 기록했습니다",
    ),
    "AMBIGUOUS_RETURN_BASELINE": (
        _ASK,
        "상승·하락률의 기준(평균 매입가 등)을 명시해 주세요",
    ),
    "TIMEFRAME_NOT_IN_INSTRUCTION": (_ASK, "지표의 봉 주기를 명시해 주세요"),
    "TIMEFRAME_REQUIRED_FOR_CROSS": (
        _ASK,
        "돌파·이탈 조건의 봉 주기를 명시해 주세요",
    ),
    "TIME_WINDOW_AM_PM_REQUIRED": (
        _ASK,
        "시간대의 오전·오후 또는 24시간 표기(예: 14:00)를 명시해 주세요",
    ),
    "QUANTITY_REQUIRED": (_ASK, "매수 수량을 명시해 주세요(예: 1주)"),
    "CONDITIONAL_RULE_AST_REQUIRED": (
        _ASK,
        "조건을 한 가지 의미로 확정할 수 없습니다. 종목·조건·수량을 한 문장으로 다시 말씀해 주세요",
    ),
    "OCO_EXIT_BRACKET_REQUIRES_EXACTLY_TWO_LEGS": (
        _ASK,
        "OCO 청산은 같은 보유분에 대한 익절 조건과 손절 조건 두 가지를 모두 명시해 주세요",
    ),
    "OCO_EXIT_BRACKET_SYMBOL_MISMATCH": (
        _ASK,
        "OCO의 익절·손절 조건은 같은 종목이어야 합니다. 대상 종목을 하나로 명시해 주세요",
    ),
    "OCO_EXIT_BRACKET_SIZING_MISMATCH": (
        _ASK,
        "OCO의 익절·손절 조건은 같은 매도 수량 또는 비율이어야 합니다. 청산 수량을 하나로 명시해 주세요",
    ),
    "OCO_EXIT_BRACKET_EXPIRY_MISMATCH": (
        _ASK,
        "OCO의 익절·손절 조건은 같은 추적 만료 시각이어야 합니다. 만료 조건을 하나로 명시해 주세요",
    ),
    "MISSING_TRAILING_STOP_PARAMETER": (
        _DEFECT,
        "트레일링 손절의 고점 대비 하락률이 조건식에 들어가지 않았습니다",
    ),
    "TRAILING_STOP_NODE_REQUIRED": (
        _DEFECT,
        "트레일링 손절 노드가 올바르게 구성되지 않았습니다",
    ),
    "INVALID_TRAILING_STOP_PARAMETER": (
        _DEFECT,
        "트레일링 손절 비율이 올바른 범위로 해석되지 않았습니다",
    ),
    # -- the platform cannot express it -----------------------------------
    "UNSUPPORTED_INDICATOR": (_GAP, "요청하신 지표를 지원하지 않습니다"),
    "UNSUPPORTED_INDICATOR_PARAMETER": (
        _GAP,
        "지표에 지원하지 않는 설정값이 있습니다",
    ),
    "UNSUPPORTED_INDICATOR_TIMEFRAME": (
        _GAP,
        "해당 지표가 요청하신 봉 주기를 지원하지 않습니다",
    ),
    "UNSUPPORTED_INDICATOR_OUTPUT": (
        _GAP,
        "해당 지표에 요청하신 선이 없습니다",
    ),
    "UNSUPPORTED_INDICATOR_PRICE_SOURCE": (
        _GAP,
        "지표 계산은 종가 기준만 지원합니다",
    ),
    "UNSUPPORTED_MARKET_FIELD": (_GAP, "요청하신 시세 항목을 지원하지 않습니다"),
    "UNSUPPORTED_PORTFOLIO_FIELD": (
        _GAP,
        "요청하신 계좌·보유 항목을 지원하지 않습니다",
    ),
    "UNSUPPORTED_TIME_FIELD": (_GAP, "요청하신 시간 항목을 지원하지 않습니다"),
    "TIME_WINDOW_OPERATOR_UNSUPPORTED": (
        _GAP,
        "시간창은 이전·이후·사이 범위 비교로만 지정할 수 있습니다",
    ),
    "TIME_WINDOW_LITERAL_INVALID": (
        _DEFECT,
        "시간창 기준값이 올바른 KST 시각으로 해석되지 않았습니다",
    ),
    "TIME_WINDOW_SHAPE_INVALID": (
        _DEFECT,
        "시간창 조건이 허용된 직접 비교 형태로 해석되지 않았습니다",
    ),
    "UNSUPPORTED_EXPRESSION": (_GAP, "조건식에 지원하지 않는 형태가 있습니다"),
    "UNSUPPORTED_ARITHMETIC_OPERATOR": (
        _GAP,
        "조건식에 지원하지 않는 연산자가 있습니다",
    ),
    "UNSUPPORTED_BOOLEAN_OPERATOR": (
        _GAP,
        "조건식에 지원하지 않는 비교 연산자가 있습니다",
    ),
    "UNSUPPORTED_LOGICAL_OPERATOR": (
        _GAP,
        "조건식에 지원하지 않는 논리 연산자가 있습니다",
    ),
    "UNSUPPORTED_UNIT_ARITHMETIC": (
        _GAP,
        "서로 다른 단위끼리는 계산할 수 없습니다",
    ),
    "BOOLEAN_COMPARISON_UNSUPPORTED": (
        _GAP,
        "참/거짓 값은 크기 비교를 할 수 없습니다",
    ),
    "INDICATOR_REQUIRES_BAR_CLOSE": (
        _GAP,
        "해당 지표는 완성된 봉에서만 계산할 수 있어 실시간 호가 기준으로 걸 수 없습니다",
    ),
    "INDICATOR_REQUIRES_REALTIME": (
        _GAP,
        "해당 지표는 실시간 값만 제공되어 완성봉 기준으로 걸 수 없습니다",
    ),
    "QUOTE_FIELD_UNAVAILABLE": (
        _GAP,
        "시가·고가·저가·종가는 완성된 봉에서만 확인할 수 있고, 실시간 기준으로는 현재가만 쓸 수 있습니다",
    ),
    "CROSS_REQUIRES_BAR_CLOSE": (
        _GAP,
        "돌파·이탈 조건은 완성된 봉 두 개가 필요해 실시간 호가 기준으로 걸 수 없습니다",
    ),
    "CROSS_PORTFOLIO_UNSUPPORTED": (
        _GAP,
        "보유·계좌 값은 직전 봉 기록이 없어 돌파·이탈 조건에 쓸 수 없습니다",
    ),
    "EXPRESSION_TOO_COMPLEX": (_GAP, "조건식이 현재 지원 범위보다 복잡합니다"),
    "INDICATOR_PARAMETER_TOO_LARGE": (
        _GAP,
        "지표 기간이 지원 한도(500봉)를 넘습니다",
    ),
    "INDICATOR_HISTORY_UNAVAILABLE": (
        _GAP,
        "요청하신 지표 기간은 해당 봉 주기에서 PAPER 데이터 조회 한도를 넘습니다",
    ),
    "CROSS_TIMEFRAME_MISMATCH": (
        _GAP,
        "돌파·이탈의 양쪽 값은 같은 봉 주기여야 합니다. 다른 주기는 AND 조건으로 함께 확인해 주세요",
    ),
    "PRIMARY_TIMEFRAME_TOO_SLOW": (
        _DEFECT,
        "기준 봉 주기가 조건에 포함된 더 빠른 봉보다 느리게 해석되었습니다",
    ),
    "OCO_EXIT_BRACKET_SELL_ONLY": (
        _GAP,
        "현재 OCO는 이미 보유한 동일 종목을 청산하는 매도 익절·손절 브래킷만 지원합니다",
    ),
    "TRAILING_STOP_REQUIRES_QUOTE": (
        _GAP,
        "트레일링 손절은 신선한 현재가 기준에서만 지원합니다",
    ),
    "TRAILING_STOP_SELL_ONLY": (
        _GAP,
        "트레일링 손절은 이미 보유한 종목을 청산하는 매도 조건만 지원합니다",
    ),
    "TRAILING_STOP_COMPOSITION_UNSUPPORTED": (
        _GAP,
        "트레일링 손절은 현재 다른 AND·OR 조건이나 시간창과 결합할 수 없습니다",
    ),
    "TRAILING_STOP_PARAMETER_UNSUPPORTED": (
        _GAP,
        "트레일링 손절에는 고점 대비 하락률과 선택적 활성 수익률만 지정할 수 있습니다",
    ),
    # -- the interpreter built a malformed AST ----------------------------
    "INVALID_INDICATOR_PARAMETER": (_DEFECT, "지표 설정값이 올바르지 않습니다"),
    "MISSING_INDICATOR_PARAMETER": (_DEFECT, "지표 설정값이 빠졌습니다"),
    "LITERAL_TYPE_MISMATCH": (_DEFECT, "조건식의 값 형식이 맞지 않습니다"),
    "UNIT_MISMATCH": (_DEFECT, "조건식 양쪽의 단위가 맞지 않습니다"),
    "DIVISION_BY_ZERO": (_DEFECT, "조건식에 0으로 나누는 계산이 있습니다"),
    "NON_FINITE_LITERAL": (_DEFECT, "조건식에 유효하지 않은 숫자가 있습니다"),
    "CONDITION_NOT_BOOLEAN": (_DEFECT, "조건식이 참/거짓으로 판정되지 않습니다"),
    "LOGICAL_REQUIRES_BOOL": (
        _DEFECT,
        "AND·OR 조건의 항목이 참/거짓이 아닙니다",
    ),
    "INDICATOR_SOURCE_MISMATCH": (
        _DEFECT,
        "지표의 데이터 출처 지정이 맞지 않습니다",
    ),
    "INDICATOR_PROVIDER_MISMATCH": (
        _DEFECT,
        "지표의 제공자 지정이 맞지 않습니다",
    ),
    "OCO_GROUP_ID_SERVER_MANAGED": (
        _DEFECT,
        "OCO 그룹 식별자는 시스템이 요청 단위로 생성해야 합니다",
    ),
}


def normalize_code(code: str) -> str:
    """Return the bare code from a possibly annotated rejection string.

    The orchestrator keeps the offending field beside the code
    ("UNSUPPORTED_PORTFOLIO_FIELD: unsupported portfolio field 'AVG_BUY_PRICE'")
    so Hermes can correct its own AST.  Classification uses only the head.
    """

    return str(code or "").split(":", 1)[0].strip().upper()


def extract_code(detail: Any) -> str:
    """Read the bare code out of an HTTPException detail.

    ``_validate_semantics`` raises ``detail={"code":..., "message":...}``.
    Stringifying that mapping is what leaked a raw Python dict to the user as
    "확인 필요: {'code': 'UNSUPPORTED_INDICATOR_PARAMETER', ...}" (2026-08-28).
    """

    if isinstance(detail, Mapping):
        return normalize_code(str(detail.get("code") or ""))
    return normalize_code(str(detail or ""))


def classify_code(
    code: str,
    *,
    source: ClarificationSource = ClarificationSource.SEMANTIC_REJECTION,
) -> ClarificationClass:
    entry = CLARIFICATION_CODES.get(normalize_code(code))
    if entry is not None:
        return entry[0]
    return _SOURCE_DEFAULT[source]


def should_ask(
    codes: tuple[str, ...],
    *,
    source: ClarificationSource = ClarificationSource.SEMANTIC_REJECTION,
) -> bool:
    """Ask only when every reason is the user's sentence being ambiguous."""

    if not codes:
        return False
    return all(
        classify_code(code, source=source) is ClarificationClass.USER_AMBIGUITY
        for code in codes
    )


def _label(code: str, *, source: ClarificationSource) -> str:
    entry = CLARIFICATION_CODES.get(normalize_code(code))
    if entry is not None:
        return entry[1]
    if source is ClarificationSource.SEMANTIC_REJECTION:
        return "조건 해석 결과가 검증을 통과하지 못했습니다"
    return str(code).strip() or "조건을 한 가지 의미로 확정할 수 없습니다"


def clarification_message(
    codes: tuple[str, ...],
    *,
    source: ClarificationSource = ClarificationSource.SEMANTIC_REJECTION,
    raw_instruction: str | None = None,
) -> str:
    """Render one honest message whose shape follows the strongest class."""

    classes = {classify_code(code, source=source) for code in codes}
    details = "; ".join(
        dict.fromkeys(_label(code, source=source) for code in codes)
    )
    if raw_instruction and "3분봉" in " ".join(raw_instruction.split()):
        details = "3분봉 데이터는 지원하지 않아 5분봉으로 수행합니다" + (
            "; " + details if details else ""
        )

    head = "조건주문을 활성화하지 않았습니다. "
    tail = " 주문·체결·원장 반영은 없습니다."
    if ClarificationClass.CAPABILITY_GAP in classes:
        body = (
            (details or "요청하신 조건을 현재 지원하지 않습니다")
            + ". 같은 표현으로 다시 요청하셔도 동일하게 거부됩니다"
        )
    elif ClarificationClass.INTERPRETER_DEFECT in classes:
        body = (
            (details or "조건 해석에 실패했습니다")
            + ". 문장 문제가 아니라 해석 오류이며 시스템 결함으로 기록했습니다"
        )
    else:
        body = details or "조건을 한 가지 의미로 확정할 수 없습니다"
    return head + body + "." + tail


__all__ = [
    "CLARIFICATION_CODES",
    "ClarificationClass",
    "ClarificationSource",
    "clarification_message",
    "classify_code",
    "extract_code",
    "normalize_code",
    "should_ask",
]
