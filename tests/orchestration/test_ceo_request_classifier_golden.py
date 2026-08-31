"""CEO 지시 분기의 골든 라우팅 회귀 기준.

이 파일은 분기 통합 리팩토링보다 **먼저** 고정된다. 목적은 두 가지다.

1. 지금 흩어져 있는 6개 분기 지점(`apps/api/ceo.py`의 순차 if 체인,
   `looks_like_user_order_request`, `looks_like_conditional_paper_rule`,
   `parse_compound_paper_order`/`parse_analysis_then_conditional_paper_order`,
   `build_ceo_task_plan`, `infer_workflow_mode`)이 **합쳐서** 내리는 레인 판정을
   질의 문자열 하나에서 재현 가능한 형태로 못박는다.
2. 깨져 있던 판정을 `expectedFailure`로 **결함으로 명시**해, 통합 커밋이
   결함을 조용히 유지하거나 조용히 바꾸지 못하게 한다. 고쳐진 결함은
   표시를 떼고 정식 회귀 테스트로 승격한다.

`_route()`는 처음에 프로덕션 코드를 건드리지 않고 현재 체인을 손으로 조립한
참조 구현이었다. 단일 진입점(`classify_ceo_request`)으로 바꾼 뒤에도 아래
케이스 표가 한 줄도 바뀌지 않는 것이 구조 이관의 성공 기준이었다.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from orchestration.ceo_query_routing import verify_primary_route
from orchestration.query_lexicon import negated_spans
from orchestration.ceo_request_classifier import ORDER_LANES, classify_ceo_request


@dataclass(frozen=True)
class GoldenRoute:
    """한 질의의 레인 판정과, 분석 레인일 때 선택된 부서."""

    lane: str
    departments: tuple[str, ...]
    workflow_mode: str
    routing_basis: str


def _route(query: str, previous_question_context: str | None = None) -> GoldenRoute:
    """단일 지시점을 그대로 호출한다.

    이 함수는 처음에 `apps/api/ceo.py`의 순차 if 체인을 손으로 조립한 참조
    구현이었다. `classify_ceo_request()`로 바꾼 뒤에도 아래 케이스 표가
    한 줄도 바뀌지 않는 것이 구조 이관의 성공 기준이었다.
    """

    decision = classify_ceo_request(
        query, previous_question_context=previous_question_context
    )
    departments = tuple(
        str(department)
        for department in decision.routing_plan.get("requested_departments", ())
        if str(department) not in {"ceo", "qa"}
    )
    return GoldenRoute(
        decision.lane,
        () if decision.is_order_lane else departments,
        decision.workflow_mode,
        decision.routing_basis,
    )


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
        "department_analysis",
        ("research", "risk"),
        note="주문 문법은 잡히지만 부정이 지배하므로 주문 레인에 들어가지 않는다",
    ),
    GoldenCase(
        "이어서",
        "department_analysis",
        ("research", "risk"),
        previous_question_context="반도체 관련주 전망 분석해줘",
        note="이전 질의 플랜 승계",
    ),
    GoldenCase("안녕", "clarification"),
    # 아래 네 건은 main이 나중에 추가한 문법이다. 부정 가드가 이 레인들을
    # 막지 않는지 고정한다.
    GoldenCase(
        "삼성전자, SK하이닉스 100만원씩 매수",
        "immediate_order",
        note="바스켓 동일금액 매수",
    ),
    GoldenCase(
        "삼성전자 10주, SK하이닉스 5주 시장가 매수",
        "immediate_order",
        note="바스켓 수량 매수",
    ),
    GoldenCase(
        "평균 매입가 대비 2% 수익이 난 뒤 고점 대비 1% 하락하면 전량 매도",
        "conditional_order",
        note="트레일링 청산",
    ),
    GoldenCase(
        "시스템 상태 알려줘",
        "operational_status",
        note="런타임 조회 - 부서 primary 0개",
    ),
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


class CeoRouteVerificationTest(unittest.TestCase):
    """라우터 결과는 질의만으로 재현·검증할 수 있어야 한다.

    `verify_primary_route`는 이미 만들어진 카드 집합이 정본 경로와 같은지
    본다. 통합 리팩토링이 이 검증 대상을 좁히면 LLM이 끼어든 라우팅을
    가려낼 수 없게 된다.
    """

    def test_department_analysis_routes_stay_verifiable(self) -> None:
        for case in STABLE_CASES:
            if case.lane != "department_analysis":
                continue
            with self.subTest(query=case.query):
                decision = classify_ceo_request(
                    case.query,
                    previous_question_context=case.previous_question_context,
                )
                verification = verify_primary_route(
                    case.query, decision.selected_primary_profiles
                )
                # 이전 질의 승계는 후속 문장만으로는 재현되지 않는다.
                if case.previous_question_context is not None:
                    continue
                self.assertTrue(verification.valid, msg=case.query)


class ConditionalOrderSafetyStressTest(unittest.TestCase):
    """I08 and negation variants must never enter an executable order lane."""

    NON_EXECUTABLE_QUERIES = (
        "삼성전자가 7만 원 아래로 내려가면 주문하지 말고 나한테만 알려줘",
        "7만 원 안 넘으면 사지 마",
        "7만 원을 넘지 않을 때 매수하지 마",
        "삼성전자 5분봉 RSI가 30 아래여도 매수하지 마",
    )

    def test_negation_and_alert_only_language_never_selects_an_order_lane(self) -> None:
        for query in self.NON_EXECUTABLE_QUERIES:
            with self.subTest(query=query):
                decision = classify_ceo_request(query)
                self.assertNotIn(decision.lane, ORDER_LANES)
                self.assertTrue(
                    {"negated_order_instruction", "insufficient_query_intent"}
                    & set(decision.reason_codes)
                )


class CeoOperationalStatusLaneTest(unittest.TestCase):
    """운영 상태 조회는 부서 fan-out 없이 결정론 경로로 끝난다.

    `apps/api/ceo.py`가 이 레인에서만 root를 blocked로 만들고 결정론적으로
    완료시킨다. 레인 이름이 사라지면 그 경로가 조용히 꺼진다.
    """

    QUERIES = (
        "시스템 상태 알려줘",
        "런타임 헬스 점검해줘",
        "워크플로 지연 현황 요약해줘",
    )

    def test_operational_status_is_its_own_lane(self) -> None:
        for query in self.QUERIES:
            with self.subTest(query=query):
                decision = classify_ceo_request(query)
                self.assertEqual(decision.lane, "operational_status")
                self.assertEqual(decision.selected_primary_profiles, ())

    def test_plan_markers_survive_for_the_deterministic_completion(self) -> None:
        decision = classify_ceo_request("시스템 상태 알려줘")
        self.assertEqual(decision.routing_plan.get("mode"), "operational_status")
        self.assertEqual(decision.category, "SYSTEM_STATUS")
        self.assertEqual(decision.routing_basis, "operational_status_intent")

    def test_execution_wording_leaves_the_lane(self) -> None:
        """binding 어휘가 섞이면 운영 조회로 처리하지 않는다."""

        self.assertNotEqual(
            classify_ceo_request("주문 워크플로 상태 점검하고 집행해").lane,
            "operational_status",
        )


class CeoRouteNegationGuardTest(unittest.TestCase):
    """부정이 붙은 주문 문장은 네 레인 어디로도 들어가지 않는다.

    예전에는 즉시 주문 레인에만 가드가 있었다. 조건·복합·연계 레인은
    같은 부정 문장을 그대로 주문 카드로 만들었다.
    """

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

    # 조건·복합·연계 레인이 부정 문장을 그대로 삼키던 자리.
    def test_negated_conditional_order_does_not_enter_order_lane(self) -> None:
        self._assert_lane(
            "이평 깨지면 매도하지 마", "department_analysis", ("research", "risk")
        )

    def test_negated_price_conditional_does_not_enter_order_lane(self) -> None:
        self._assert_lane(
            "삼성전자 300000원 이상이면 10주 매도하지 마",
            "department_analysis",
            ("research", "risk"),
        )

    def test_negated_compound_order_does_not_enter_order_lane(self) -> None:
        self._assert_lane(
            "삼성전자 2주 시장가 매수하지 말고 300000원 이상이면 2주 매도하지 마",
            "department_analysis",
            ("research", "risk"),
        )

    def test_negated_analysis_then_order_does_not_enter_order_lane(self) -> None:
        self._assert_lane(
            "리서치 분석 후 삼성전자 300000원 넘으면 10주 매도하지 마",
            "department_analysis",
            ("research", "risk"),
        )

    # `explicit_non_execution` 어휘에 `매수`/`매도`가 없어서 순수 부정 문장이
    # binding으로 남고 LLM 플래너로 넘어가던 자리. 조건 트리거가 있든 없든
    # 같은 레인이어야 한다.
    def test_bare_negated_sell_is_not_binding(self) -> None:
        self._assert_lane(
            "손실 나도 매도하지 마", "department_analysis", ("research", "risk")
        )

    def test_negated_sell_with_timeframe_is_not_binding(self) -> None:
        self._assert_lane(
            "5분내 60초선 깨져도 매도하지 마",
            "department_analysis",
            ("research", "risk"),
        )

    def test_bare_negated_buy_is_not_binding(self) -> None:
        self._assert_lane("매수하지 마", "department_analysis", ("research", "risk"))

    # `하지 마` 계열만 부정으로 알아보던 자리. `건드리지 말고`가 인식되지 않아
    # 사용자가 배제한 부서가 오히려 fan-out에 **추가**됐다.
    def test_alternative_negation_vocabulary_is_recognized(self) -> None:
        self._assert_lane(
            "회계쪽은 건드리지 말고 리서치만 해줘",
            "department_analysis",
            ("research", "risk"),
        )

    def test_excluded_scope_still_counts_as_a_specific_request(self) -> None:
        """배제 어휘를 알아본 뒤에도 되묻기로 떨어지면 안 된다.

        되묻기 게이트는 "알아볼 만한 대상이 있는가"를 묻는다. 금지도
        대상을 지목한 것이므로, 넓은 부정 어휘는 부서 선택에만 적용한다.
        """

        self.assertNotEqual(_route("회계쪽은 건드리지 말고 리서치만 해줘").lane, "clarification")

    def test_single_syllable_negation_needs_a_word_boundary(self) -> None:
        """`동안`의 `안`은 부정이 아니다.

        한 음절 표지(`안`·`못`)를 낱말 안에서도 잡으면 정상 조건주문이
        막힌다 - `"최대 5거래일 동안 추적"`이 그렇게 걸렸다.
        """

        self._assert_lane(
            "고점 대비 1% 하락하면 매도해줘, 최대 5거래일 동안 추적",
            "conditional_order",
        )
        self._assert_lane("삼성전자 2주 시장가 매수를 잘못 했어", "immediate_order")

    def test_word_boundary_fix_keeps_real_negation(self) -> None:
        """어절 경계를 지켜도 진짜 부정은 그대로 잡는다.

        이 문장의 최종 레인은 `llm_planner_required`다. `주문 안 하고`는
        `infer_workflow_mode`의 비집행 선언 어휘(`하지 마`·`금지`)에 없어서
        여전히 binding으로 남기 때문이며, 이는 main과 같은 기존 동작이다.
        여기서 고정하는 것은 **주문 레인에 들어가지 않는다**는 것뿐이다.
        """

        decision = classify_ceo_request("주문 안 하고 분석만 해줘")
        self.assertFalse(decision.is_order_lane)
        self.assertIn("trading", decision.excluded_departments)

    # 사용자는 `안`·`못`을 뒤 용언에 붙여 쓴다. 공백을 요구하면 그 부정을
    # 통째로 놓치고, 같은 문장이 띄어쓰기 하나 차이로 주문 카드가 된다.
    SPACING_VARIANTS = (
        "이평 깨지면 매수 안 하고 지켜봐",
        "이평 깨지면 매수 안하고 지켜봐",
        "이평 깨지면 매수안하고 지켜봐",
        "이평 깨지면 주문 안하고 지켜봐",
        "이평 깨지면 매도 안할래",
        "이평 깨지면 매도 안함",
        "이평 깨지면 매도 못하게 해줘",
        "이평 깨지면 체결 안되게 해줘",
        "이평 깨지면 매도 않고 지켜봐",
        "이평 깨지면 매도하지말고 지켜봐",
    )

    def test_spacing_variants_are_all_negations(self) -> None:
        for query in self.SPACING_VARIANTS:
            with self.subTest(query=query):
                self.assertFalse(_route(query).lane in ORDER_LANES, msg=query)

    # 부정이 아닌데 `안`·`못`으로 시작하거나 끝나는 낱말들. 가드가 넓어질 때
    # 가장 먼저 깨지는 쪽이다.
    NON_NEGATIONS = (
        "장 안에서 매도해",
        "안전하게 매도해",
        "안정적으로 매수해",
        "불안하지만 매수해",
        "안내받고 매수해",
        "안건 정리하고 매수해",
        "삼성전자 2주 시장가 매수를 잘못 했어",
        "못을 박듯 매수해",
    )

    def test_lookalike_words_are_not_negations(self) -> None:
        for query in self.NON_NEGATIONS:
            with self.subTest(query=query):
                self.assertEqual(negated_spans(query), (), msg=query)

    def test_negation_on_the_instrument_still_allows_a_conditional_order(self) -> None:
        """가드가 레인 자체를 막아서는 안 된다. 부정이 종목에만 걸린 경우다."""

        self._assert_lane(
            "삼성전자 말고 SK하이닉스 300000원 이상이면 10주 매도해",
            "conditional_order",
        )


class CeoRouteExclusionTest(unittest.TestCase):
    """명시적으로 배제한 부서는 목록에서 빠진다.

    예전에는 기본값을 선적재한 뒤 `add`만 했기 때문에, 키워드 억제는
    추가만 막고 이미 들어와 있는 부서를 빼지 못했다.
    """

    def test_negated_department_is_excluded(self) -> None:
        route = _route("리스크 검토도 하지 말고 뉴스만 정리해줘")
        self.assertEqual(route.lane, "department_analysis")
        self.assertEqual(route.departments, ("research",))

    def test_exclusion_is_recorded(self) -> None:
        decision = classify_ceo_request("리스크 검토도 하지 말고 뉴스만 정리해줘")
        self.assertEqual(decision.excluded_departments, ("risk",))

    def test_condition_words_do_not_exclude_their_department(self) -> None:
        """부정이 지배하는 것은 행위지 조건이 아니다.

        `"손실 나도 매도하지 마"`에서 사용자가 뺀 것은 매도이고, `손실`은
        조건이다. Risk를 빼면 안전 부서가 사라진다.
        """

        decision = classify_ceo_request("손실 나도 매도하지 마")
        self.assertEqual(decision.excluded_departments, ("trading",))
        self.assertIn("risk", decision.routing_plan["requested_departments"])

    def test_excluding_every_department_keeps_the_default(self) -> None:
        """응답 부서가 하나도 남지 않는 배제는 받아들이지 않는다."""

        decision = classify_ceo_request("뉴스도 보지 말고 리스크도 보지 마")
        self.assertTrue(
            [
                department
                for department in decision.routing_plan["requested_departments"]
                if department != "ceo"
            ]
        )


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
