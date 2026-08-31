"""CEO 질의의 부정 판정과 어휘 사전을 모아 두는 단일 모듈.

이 모듈은 `ceo_query_routing`과 같은 계약을 승계한다 - LangGraph·Hermes·
LangSmith·Worker를 import 하지 않는다. BFF 경계에서 그래프를 적재하지 않고
같은 판정을 재현할 수 있어야 하기 때문이다.

## 왜 모았나

부정("…하지 마")을 알아보는 코드가 세 곳에 따로 있었고, 셋의 어휘가 서로 달랐다.

| 위치 | 쓰임 | 어휘 |
|---|---|---|
| `user_order_language._NEGATION_RE` | 즉시 주문 레인 진입 차단 | `하지 마`·`말아`·`안 사/팔`·`않` |
| `ceo_query_routing._is_negated_suffix` | 부서 키워드 추가 억제 | `하지 마/말고/않`·`안 `·`못 `·`금지`·`불가`·`없` |
| `ceo_workflow_scope`의 `explicit_non_execution` | binding 분류 회피 | `하지 마/말라/마세요/말/않`·`금지` |

같은 문장이 어디를 지나느냐에 따라 부정으로 보이기도 하고 안 보이기도 했다.
`"이평 깨지면 매도하지 마"`가 조건주문 레인에 그대로 들어간 것이 그 결과다.

## 무엇을 합치고 무엇을 남겼나

`NEGATION_MARKER_PATTERN`이 세 어휘의 **합집합**이다. 어느 하나도 좁히지 않았다 -
좁히면 안전 감도가 약해진다. 다만 호출부마다 판정 **폭**은 원래대로 유지한다.
`hard`(즉시 인접)와 `loose`(짧은 창 안) 두 단계를 구분해 두는 이유는,
"부서를 추가하지 않는다"와 "부서를 제거한다"의 위험도가 다르기 때문이다
(개발원칙 9). 추가 억제는 느슨해도 안전한 쪽으로 틀리지만, 제거는 사용자가
명시적으로 배제한 경우에만 해야 한다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# ---------------------------------------------------------------------------
# 부정 표지
# ---------------------------------------------------------------------------

# `안`·`못`은 한 음절이라 다른 낱말 안에 그대로 들어간다. `"5거래일 동안 추적"`의
# `동안 `이 부정으로 잡혀 정상 트레일링 조건주문이 막힌 적이 있다(2026-08-31 실측).
# 앞이 한글이면 어절이 아니므로 표지로 보지 않는다.
_STANDALONE = r"(?<![가-힣])"

# `user_order_language._NEGATION_RE`가 쓰던 어휘. 주문 동사에 직접 붙는 형태다.
ORDER_NEGATION_PATTERN = (
    r"하지\s*마|하지마|말아(?:\s*줘)?|말(?:아|자)|"
    rf"{_STANDALONE}안\s*(?:사|팔|매수|매도|취소)|(?:매수|매도|취소)\s*안|"
    r"않(?:아|게|도록)?"
)

# `_is_negated_suffix`와 `explicit_non_execution`이 쓰던 어휘의 합집합.
_SHARED_NEGATION_PATTERN = (
    r"하지\s*(?:마|말라|마세요|말고|말|않)|"
    r"수행하지\s*(?:마|말라|마세요|말)|"
    rf"{_STANDALONE}안\s|{_STANDALONE}못\s|금지|불가|없"
)

# 위 세 곳 어디에도 없던 배제 표현. `"회계쪽은 건드리지 말고"`가 부정으로
# 인식되지 않아 오히려 회계 부서를 **추가**하고 있었다. `없이`는 `없`에
# 이미 포함된다.
EXCLUSION_NEGATION_PATTERN = (
    r"건드리지\s*(?:마|말고|말)|빼(?:고|줘|주세요)|제외(?:하고|해\s*줘|해줘)|"
    r"말고"
)

# 세 어휘의 합집합 + 배제 표현. 새 코드는 이것을 쓴다.
NEGATION_MARKER_PATTERN = "|".join(
    (ORDER_NEGATION_PATTERN, _SHARED_NEGATION_PATTERN, EXCLUSION_NEGATION_PATTERN)
)

_ORDER_NEGATION_RE = re.compile(rf"(?:{ORDER_NEGATION_PATTERN})")
_NEGATION_MARKER_RE = re.compile(rf"(?:{NEGATION_MARKER_PATTERN})")

# 어절이 부정 표지에 직접 붙은 형태. 조사 하나까지만 허용한다.
_HARD_SUFFIX_RE = re.compile(
    rf"^(?:은|는|이|가|을|를)?\s*(?:{_SHARED_NEGATION_PATTERN})"
)
_HARD_SUFFIX_EXTENDED_RE = re.compile(
    rf"^(?:은|는|이|가|을|를)?\s*(?:{NEGATION_MARKER_PATTERN})"
)

# 같은 절 안에서 부정 표지까지 닿는 짧은 창. 문장부호를 넘지 않는다.
NEGATION_SUFFIX_WINDOW = 24
_LOOSE_SUFFIX_RE = re.compile(
    rf"^[^.!?\n]{{0,{NEGATION_SUFFIX_WINDOW}}}(?:{_SHARED_NEGATION_PATTERN})"
)
_LOOSE_SUFFIX_EXTENDED_RE = re.compile(
    rf"^[^.!?\n]{{0,{NEGATION_SUFFIX_WINDOW}}}(?:{NEGATION_MARKER_PATTERN})"
)

# 부정 표지가 지배하는 절의 시작 경계. 문장부호와 나열 연결어미까지다.
_CLAUSE_BOUNDARY_RE = re.compile(r"[.!?\n]|(?:하고|하며|한\s*뒤|한\s*후|그리고)\s")
NEGATION_CLAUSE_WINDOW = 32


def order_negation_match(text: str) -> re.Match[str] | None:
    """주문 문장에 붙은 부정을 찾는다 (즉시 주문 레인의 기존 판정)."""

    return _ORDER_NEGATION_RE.search(str(text or ""))


def is_negated_suffix(suffix: str, *, extended: bool = False) -> bool:
    """어절 뒤에 부정이 오는지 본다.

    `extended=False`가 기존 판정 폭이다. `extended=True`는 배제 표현까지
    포함하며, 부서 키워드 추가를 억제할 때 쓴다.
    """

    text = str(suffix or "")
    if extended:
        return bool(
            _HARD_SUFFIX_EXTENDED_RE.match(text)
            or _LOOSE_SUFFIX_EXTENDED_RE.match(text)
        )
    return bool(_HARD_SUFFIX_RE.match(text) or _LOOSE_SUFFIX_RE.match(text))


def contains_negation(text: str) -> bool:
    """구간 안 어디든 부정 표지가 있는지 본다.

    절 전체를 이미 잘라 낸 뒤 "이 절이 금지인가"만 묻는 호출부용이다.
    """

    return _NEGATION_MARKER_RE.search(str(text or "")) is not None


def negated_spans(text: str) -> tuple[tuple[int, int], ...]:
    """부정 표지가 지배하는 구간을 앞에서부터 반환한다.

    구간의 끝은 표지의 끝이고, 시작은 같은 절 안에서 표지로부터
    `NEGATION_CLAUSE_WINDOW`자 이내다. 절 경계(문장부호·나열 연결어미)를
    넘지 않으므로 `"삼성전자 분석해줘. 실제 주문은 하지 마"`의 앞 문장은
    부정 구간에 들어가지 않는다.
    """

    source = str(text or "")
    spans: list[tuple[int, int]] = []
    for match in _NEGATION_MARKER_RE.finditer(source):
        window_start = max(0, match.start() - NEGATION_CLAUSE_WINDOW)
        prefix = source[window_start : match.start()]
        boundaries = [
            boundary.end() for boundary in _CLAUSE_BOUNDARY_RE.finditer(prefix)
        ]
        start = window_start + (boundaries[-1] if boundaries else 0)
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], match.end())
            continue
        spans.append((start, match.end()))
    return tuple(spans)


# 주문 레인이 소유하는 행위 어휘. 부정이 이 어휘를 지배하면 주문이 아니다.
ORDER_ACTION_PATTERN = (
    r"매수|매도|주문|매매|체결|청산|"
    r"사\s*(?:줘|주세요|라)|팔아(?:\s*줘|주세요)?|"
    r"\bbuy\b|\bsell\b|\border\b"
)
_ORDER_ACTION_RE = re.compile(rf"(?:{ORDER_ACTION_PATTERN})", re.IGNORECASE)


def is_negated_order_instruction(text: str) -> bool:
    """주문 행위가 부정에 지배당하는 문장인지 본다.

    `"이평 깨지면 매도하지 마"`는 조건주문 문법을 그대로 만족하지만 주문이
    아니다. 즉시 주문 레인에는 이런 가드가 있었고 조건·복합·연계 레인에는
    없었다 - 같은 부정이 어느 문법에 걸리느냐에 따라 주문 카드가 생겼다.

    문장 전체가 아니라 **부정이 지배하는 구간**만 본다. 그래서
    `"삼성전자 말고 SK하이닉스 300000원 넘으면 매도해"`처럼 부정이 종목에만
    걸린 정상 조건주문은 계속 통과한다.
    """

    source = str(text or "")
    spans = negated_spans(source)
    if not spans:
        return False
    return any(
        span_start <= match.start() and match.end() <= span_end
        for span_start, span_end in spans
        for match in _ORDER_ACTION_RE.finditer(source)
    )


def dominant_negated_keys(
    spans: Iterable[tuple[int, int]],
    occurrences: Iterable[tuple[int, int, str]],
) -> tuple[str, ...]:
    """각 부정 구간에서 **부정이 직접 지배하는** 어휘의 key만 돌려준다.

    구간 안의 모든 어휘를 부정으로 보면 안 된다. `"손실 나도 매도하지 마"`는
    한 절이지만 사용자가 배제한 것은 `매도`뿐이고 `손실`은 조건이다. 그래서
    구간마다 표지에 가장 가까운 어휘 하나(같은 위치에서 시작하는 것은 함께)만
    지배 대상으로 본다. 이 판정은 목록에서 부서를 **빼는** 데 쓰이므로
    좁은 쪽으로 틀리게 둔다 - 잘못 빼면 안전 부서가 사라진다(개발원칙 9).
    """

    ordered = sorted(occurrences)
    dominant: list[str] = []
    for span_start, span_end in spans:
        inside = [
            occurrence
            for occurrence in ordered
            if span_start <= occurrence[0] and occurrence[1] <= span_end
        ]
        if not inside:
            continue
        nearest = max(start for start, _, _ in inside)
        dominant.extend(
            key for start, _, key in inside if start == nearest and key not in dominant
        )
    return tuple(dominant)


# ---------------------------------------------------------------------------
# 비집행 선언 (binding 분류에서 빠지는 문장)
# ---------------------------------------------------------------------------

# `explicit_non_execution`이 보던 행위 어휘. `매수`·`매도`가 빠져 있어서
# `"매도하지 마"`가 binding으로 분류돼 결정론 플랜이 root body에 실리지 않고
# LLM 플래너 경로로 넘어갔다 - 사용자가 아무것도 하지 말라고 한 문장이
# 가장 덜 결정론적인 경로로 간 셈이다.
NON_EXECUTION_ACTION_PATTERN = (
    r"주문(?:\s*제출)?|매매|매수|매도|집행|실행|원장\s*변경|설정\s*변경|"
    r"외부\s*(?:쓰기|변경)"
)
NON_EXECUTION_WINDOW = 80
_NON_EXECUTION_RE = re.compile(
    rf"(?:{NON_EXECUTION_ACTION_PATTERN})"
    rf"[^.!?\n]{{0,{NON_EXECUTION_WINDOW}}}"
    rf"(?:하지\s*(?:마|말라|마세요|말|않)|"
    rf"수행하지\s*(?:마|말라|마세요|말)|금지)"
)

NON_BINDING_PHRASES: tuple[str, ...] = (
    "do not place",
    "don't place",
    "do not execute",
    "don't execute",
    "실제 주문이나 집행은 하지",
    "주문이나 집행은 하지",
    "주문하지 말",
    "집행하지 말",
    "실행하지 말",
)

BINDING_TERMS: tuple[str, ...] = (
    "place order",
    "send order",
    "execute order",
    "broker",
    "buy ",
    "sell ",
    "주문",
    "매수",
    "매도",
    "집행",
    "배분 변경",
    "리밸런싱",
    "rebalance",
    "change nav",
    "nav 변경",
    "ledger post",
    "원장 반영",
    "promote to production",
    "production promotion",
    "프로덕션 승격",
    "deploy strategy",
    "전략 배포",
    "실제 거래",
    "실행해",
)


def non_execution_match(text: str) -> re.Match[str] | None:
    """`"주문은 하지 마"`처럼 행위를 명시적으로 금지한 절을 찾는다."""

    return _NON_EXECUTION_RE.search(str(text or ""))


# ---------------------------------------------------------------------------
# 의도 어휘
# ---------------------------------------------------------------------------

# 이 중 하나라도 있으면 되묻지 않는다. add-only 사전이므로 단어를 빼는 것은
# 곧 부정 누락이다 - 확장만 하고, 축소는 별도 결정이 필요하다.
QUERY_INTENT_TERMS: tuple[str, ...] = (
    "분석",
    "검토",
    "조회",
    "확인",
    "알려",
    "보여",
    "브리핑",
    "요약",
    "추천",
    "비교",
    "평가",
    "설명",
    "전략",
    "리스크",
    "위험",
    "주문",
    "매수",
    "매도",
    "분류",
    "analy",
    "review",
    "status",
    "summary",
    "recommend",
    "compare",
    "explain",
)


__all__ = [
    "BINDING_TERMS",
    "EXCLUSION_NEGATION_PATTERN",
    "NEGATION_CLAUSE_WINDOW",
    "NEGATION_MARKER_PATTERN",
    "NEGATION_SUFFIX_WINDOW",
    "NON_BINDING_PHRASES",
    "NON_EXECUTION_ACTION_PATTERN",
    "NON_EXECUTION_WINDOW",
    "ORDER_ACTION_PATTERN",
    "ORDER_NEGATION_PATTERN",
    "QUERY_INTENT_TERMS",
    "contains_negation",
    "dominant_negated_keys",
    "is_negated_order_instruction",
    "is_negated_suffix",
    "negated_spans",
    "non_execution_match",
    "order_negation_match",
]
