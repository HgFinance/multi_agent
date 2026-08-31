"""CEO 지시 분기의 골든 라우팅 회귀 기준.

이 파일은 분기 통합 리팩토링보다 **먼저** 고정된다. 목적은 두 가지다.

1. 지금 흩어져 있는 6개 분기 지점(`apps/api/ceo.py`의 순차 if 체인,
   `looks_like_user_order_request`, `looks_like_conditional_paper_rule`,
   `parse_compound_paper_order`/`parse_analysis_then_conditional_paper_order`,
   `build_ceo_task_plan`, `infer_workflow_mode`)이 **합쳐서** 내리는 레인 판정을
   질의 문자열 하나에서 재현 가능한 형태로 못박는다.
2. 지금 깨져 있는 판정을 `expectedFailure`로 **결함으로 명시**해, 통합 커밋이
   결함을 조용히 유지하거나 조용히 바꾸지 못하게 한다.

`_route()`는 프로덕션 코드를 고치지 않고 현재 체인을 그대로 조립한 참조 구현이다.
단일 진입점(`classify_ceo_request`)이 생기면 이 함수 본문만 그 호출로 바뀌고,
아래 케이스 표는 그대로여야 한다 - 구조 이관 커밋의 성공 기준이 그것이다.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from apps.api.conditional_rule_language import looks_like_conditional_paper_rule
from orchestration.ceo_query_routing import build_deterministic_bff_plan
from orchestration.ceo_workflow_scope import infer_workflow_mode
from orchestration.compound_paper_orders import (
    parse_analysis_then_conditional_paper_order,
    parse_compound_paper_order,
)
from orchestration.user_order_language import (
    is_clearly_non_executable_order_language,
    looks_like_user_order_request,
)

# 주문 레인 4종. 이 레인에 들어간 질의는 부서 fan-out을 만들지 않고
# Trading 해석 카드 한 장으로 간다 - 그래서 기대 부서는 빈 튜플이다.
ORDER_LANES = frozenset(
    {
        "immediate_order",
        "conditional_order",
        "compound_order",
        "analysis_then_order",
    }
)


@dataclass(frozen=True)
class GoldenRoute:
    """한 질의의 레인 판정과, 분석 레인일 때 선택된 부서."""

    lane: str
    departments: tuple[str, ...]
    workflow_mode: str
    routing_basis: str


def _route(query: str, previous_question_context: str | None = None) -> GoldenRoute:
    """현재 분기 체인을 그대로 조립한다 (프로덕션 코드 수정 없음).

    순서는 `apps/api/ceo.py`의 순차 if 체인과 동일하다. 순서가 곧 우선순위이므로
    임의로 바꾸면 같은 질의가 다른 레인으로 간다.
    """

    plan = build_deterministic_bff_plan(
        query, previous_question_context=previous_question_context
    )
    departments = tuple(
        str(department)
        for department in plan.get("requested_departments", ())
        if str(department) not in {"ceo", "qa"}
    )
    workflow_mode = infer_workflow_mode(query)
    basis = str(plan.get("routing_basis") or "")
    order_route_requested = looks_like_conditional_paper_rule(
        query
    ) or looks_like_user_order_request(query)

    def order(lane: str) -> GoldenRoute:
        return GoldenRoute(lane, (), workflow_mode, basis)

    if plan.get("mode") == "clarification_required" and not order_route_requested:
        return GoldenRoute("clarification", (), workflow_mode, basis)
    if parse_analysis_then_conditional_paper_order(query) is not None:
        return order("analysis_then_order")
    if parse_compound_paper_order(query) is not None:
        return order("compound_order")
    if looks_like_conditional_paper_rule(query):
        return order("conditional_order")
    if looks_like_user_order_request(query) and not (
        is_clearly_non_executable_order_language(query)
    ):
        return order("immediate_order")
    if workflow_mode == "analysis":
        return GoldenRoute("department_analysis", departments, workflow_mode, basis)
    # 결정론으로 확정하지 못한 binding 질의. root body에 플랜이 실리지 않고
    # `ceo-agent` 코멘트를 읽는 LLM 플래너 경로로 넘어간다.
    return GoldenRoute("llm_planner_required", departments, workflow_mode, basis)


@dataclass(frozen=True)
class GoldenCase:
    query: str
    lane: str
    departments: tuple[str, ...] = ()
    previous_question_context: str | None = None
    note: str = ""


# 현행유지 - 통합 리팩토링이 끝나도 그대로여야 하는 판정.
STABLE_CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        "오늘 전체 업무 현황을 요약해줘",
        "department_analysis",
        ("research", "risk"),
        note="'업무 브리핑' 전용 카테고리가 없어 기본값으로 간다",
    ),
    GoldenCase(
        "반도체 관련주 전망 분석해줘",
        "department_analysis",
        ("research", "risk"),
    ),
    GoldenCase("계좌 현황 보여줘", "department_analysis", ("accounting",)),
    GoldenCase("잔고 조회", "department_analysis", ("accounting",)),
    GoldenCase("삼성전자 2주 시장가 매수해", "immediate_order"),
    GoldenCase(
        "삼성전자가 5분내 60초선에 데드크로스하면 10주 매수해",
        "conditional_order",
    ),
    GoldenCase(
        "삼성전자 2주 시장가 매수하고 300000원 이상이면 2주 매도해",
        "compound_order",
    ),
    GoldenCase(
        "리서치 분석 후 삼성전자 300000원 넘으면 10주 매도해줘",
        "analysis_then_order",
    ),
    GoldenCase(
        "이평 깨지면 매도해",
        "conditional_order",
        note="부정이 없는 정상 조건주문. 결함 1 수정이 이 레인을 막으면 안 된다",
    ),
    GoldenCase(
        "삼성전자 분석해줘. 실제 주문은 하지 마",
        "department_analysis",
        ("research", "risk"),
        note="부정이 이미 정상 동작하는 기준선",
    ),
    GoldenCase(
        "주문은 하지 말고 분석만 해줘",
        "department_analysis",
        ("research", "risk"),
    ),
    GoldenCase(
        "세금 계산은 하지 말고 전략만 검토해",
        "department_analysis",
        ("research", "quant", "risk"),
    ),
    GoldenCase(
        "삼성전자 2주 시장가 매수하지 마",
        "llm_planner_required",
        ("research", "risk"),
        note="즉시 주문 레인에는 이미 부정 가드가 있다",
    ),
    GoldenCase(
        "이어서",
        "department_analysis",
        ("research", "risk"),
        previous_question_context="반도체 관련주 전망 분석해줘",
        note="이전 질의 플랜 승계",
    ),
    GoldenCase("안녕", "clarification"),
)


# 목적지가 아직 결정되지 않은 판정. 지금 값은 회귀 기준으로만 고정하고,
# 담당자 결정(키워드 사전 확장 범위, 순수 부정 문장 목적지, LLM 플래너 적용
# 범위)이 나오면 이 행부터 바뀐다.
UNDECIDED_CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        "반도체 관련주 전망 어때?",
        "clarification",
        note="'전망'·'어때'가 사전에 없어 되묻는다. 사전 확장 여부 미결정",
    ),
    GoldenCase(
        "삼성전자 투자해도 될까?",
        "clarification",
        note="'투자'·'될까' 미포함. 사전 확장 여부 미결정",
    ),
    GoldenCase(
        "리밸런싱은 하지 말고 비중만 알려줘",
        "llm_planner_required",
        ("research", "risk"),
        note="'리밸런싱'이 binding 어휘라 부정에도 binding으로 남는다",
    ),
)


class CeoRouteGoldenTest(unittest.TestCase):
    """레인 판정의 회귀 기준."""

    def _assert_case(self, case: GoldenCase) -> None:
        route = _route(case.query, case.previous_question_context)
        self.assertEqual(route.lane, case.lane, msg=case.query)
        if case.lane in ORDER_LANES:
            return
        self.assertEqual(route.departments, case.departments, msg=case.query)

    def test_stable_routes(self) -> None:
        for case in STABLE_CASES:
            with self.subTest(query=case.query):
                self._assert_case(case)

    def test_undecided_routes_are_pinned(self) -> None:
        for case in UNDECIDED_CASES:
            with self.subTest(query=case.query):
                self._assert_case(case)

    def test_previous_question_context_is_recorded(self) -> None:
        route = _route("이어서", "반도체 관련주 전망 분석해줘")
        self.assertEqual(route.routing_basis, "previous_question_context")


class CeoRouteKnownDefectTest(unittest.TestCase):
    """지금 깨져 있는 판정. 수정 커밋에서 `expectedFailure`를 뗀다."""

    def _assert_lane(
        self,
        query: str,
        lane: str,
        departments: tuple[str, ...] = (),
    ) -> None:
        route = _route(query)
        self.assertEqual(route.lane, lane, msg=query)
        if lane not in ORDER_LANES:
            self.assertEqual(route.departments, departments, msg=query)

    # 결함 1 - 주문 레인 4종에 부정 가드가 대칭으로 걸려 있지 않다.
    # 즉시 주문만 `is_clearly_non_executable_order_language`를 통과시키고,
    # 조건·복합·연계 레인은 부정 문장을 그대로 삼킨다.
    @unittest.expectedFailure
    def test_negated_conditional_order_does_not_enter_order_lane(self) -> None:
        self._assert_lane(
            "이평 깨지면 매도하지 마", "department_analysis", ("research", "risk")
        )

    @unittest.expectedFailure
    def test_negated_price_conditional_does_not_enter_order_lane(self) -> None:
        self._assert_lane(
            "삼성전자 300000원 이상이면 10주 매도하지 마",
            "department_analysis",
            ("research", "risk"),
        )

    @unittest.expectedFailure
    def test_negated_compound_order_does_not_enter_order_lane(self) -> None:
        self._assert_lane(
            "삼성전자 2주 시장가 매수하지 말고 300000원 이상이면 2주 매도하지 마",
            "department_analysis",
            ("research", "risk"),
        )

    @unittest.expectedFailure
    def test_negated_analysis_then_order_does_not_enter_order_lane(self) -> None:
        self._assert_lane(
            "리서치 분석 후 삼성전자 300000원 넘으면 10주 매도하지 마",
            "department_analysis",
            ("research", "risk"),
        )

    # 결함 1 - `explicit_non_execution` 어휘에 `매수`/`매도`가 빠져 있어
    # 순수 부정 문장이 여전히 binding으로 분류되고 LLM 플래너로 넘어간다.
    # 조건 트리거가 없는 문장도, 있는 문장도 같은 레인이어야 한다.
    @unittest.expectedFailure
    def test_bare_negated_sell_is_not_binding(self) -> None:
        self._assert_lane(
            "손실 나도 매도하지 마", "department_analysis", ("research", "risk")
        )

    @unittest.expectedFailure
    def test_negated_sell_with_timeframe_is_not_binding(self) -> None:
        self._assert_lane(
            "5분내 60초선 깨져도 매도하지 마",
            "department_analysis",
            ("research", "risk"),
        )

    @unittest.expectedFailure
    def test_bare_negated_buy_is_not_binding(self) -> None:
        self._assert_lane("매수하지 마", "department_analysis", ("research", "risk"))

    # 결함 3 - 부정으로 부서를 제거할 수 없다. `stages`가 기본값을 선적재한 뒤
    # `add`만 하므로 명시적으로 배제한 부서가 그대로 남는다.
    @unittest.expectedFailure
    def test_negated_department_is_excluded(self) -> None:
        self._assert_lane(
            "리스크 검토도 하지 말고 뉴스만 정리해줘",
            "department_analysis",
            ("research",),
        )

    # 결함 4 - 부정 어휘가 `하지 마` 계열 한 종류뿐이라
    # `건드리지 말고`가 부정으로 인식되지 않고 오히려 부서를 추가한다.
    @unittest.expectedFailure
    def test_alternative_negation_vocabulary_is_recognized(self) -> None:
        self._assert_lane(
            "회계쪽은 건드리지 말고 리서치만 해줘",
            "department_analysis",
            ("research", "risk"),
        )

    # 계좌 상태 질문인데 상태/보고 어구가 사전에 없어 Research/Risk로 fan-out 된다.
    @unittest.expectedFailure
    def test_holdings_and_pnl_request_is_account_status(self) -> None:
        self._assert_lane(
            "지금 보유 종목과 수익 보여줘", "department_analysis", ("accounting",)
        )

    # 연계 레인 문법이 종목명을 요구해서, 종목명 없는 같은 형태가
    # 조건주문 레인으로 떨어진다.
    @unittest.expectedFailure
    def test_analysis_then_order_without_instrument(self) -> None:
        self._assert_lane(
            "리서치 분석 후 300000원 넘으면 10주 매도해줘", "analysis_then_order"
        )

    # 리서치 선행이 필요한 문장인데 연계 레인 문법이 `리서치 분석 후` 접두만
    # 인식해서, 같은 의미의 다른 표현이 LLM 플래너로 넘어간다.
    @unittest.expectedFailure
    def test_research_first_then_conditional_order(self) -> None:
        self._assert_lane(
            "삼성전자 먼저 분석하고, 추세가 유효하면 대략 10주 매수해",
            "analysis_then_order",
        )


if __name__ == "__main__":
    unittest.main()
