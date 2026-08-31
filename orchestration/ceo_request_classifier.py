"""CEO 지시가 어느 레인으로 가는지 정하는 **단일 지시점**.

## 왜 하나로 모았나

같은 판정이 여섯 곳에 흩어져 있었다. `apps/api/ceo.py`의 순차 if 체인,
`looks_like_user_order_request`, `looks_like_conditional_paper_rule`,
`parse_compound_paper_order`/`parse_analysis_then_conditional_paper_order`,
`build_ceo_task_plan`, `infer_workflow_mode`. 각자는 맞았지만, **어떤 순서로
무엇을 검사하는지가** 한 파일에도 한 함수에도 적혀 있지 않았다. 그래서
`"이평 깨지면 매도하지 마"`가 조건주문 레인으로 들어가는 것을 아무도 못 봤다 -
즉시 주문 레인에만 부정 가드가 있었고, 그 사실이 두 파일 건너에 있었다.

## 무엇을 합치지 **않았나**

판정 방식을 하나로 통일한 것이 아니다. 주문 판정은 결정론으로 남고
(CLAUDE.md 개발원칙 4, ADR-0007), 라우터 결과는 `verify_primary_route`가
질의만으로 재현·검증할 수 있어야 하며, LLM 플래너는 그 둘 중 어느 것도
대체하지 않는다. 합친 것은 **순서**다.

## 계약

- 이 모듈은 LangGraph·Hermes·LangSmith·Worker를 import 하지 않는다.
  `ceo_query_routing`의 계약을 그대로 승계한다.
- LLM 플래너를 **호출하지 않는다.** 결정론으로 못 정한 경우
  `lane="llm_planner_required"`를 반환만 하고, 실제 호출은 상위(supervisor)가 한다.
- fail-closed다. 안 맞으면 부서 fan-out이 아니라 되묻기로 떨어진다.
- QA는 응답 후 비동기 감사다. 어떤 단계에서도 QA를 primary로 고르지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

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

try:  # pragma: no cover - 패키지 경로
    from apps.api.conditional_rule_language import looks_like_conditional_paper_rule
except ImportError:  # pragma: no cover - ``python apps/api/main.py`` 직접 실행 경로
    from conditional_rule_language import (  # type: ignore[no-redef]
        looks_like_conditional_paper_rule,
    )


CeoLane = Literal[
    "clarification",  # 입력 불명확 - 부서 선택 안 함
    "immediate_order",  # 즉시 PAPER 주문
    "conditional_order",  # 조건/예약 PAPER 주문
    "compound_order",  # 즉시 + 조건 매도
    "analysis_then_order",  # 리서치 선행 후 조건주문
    "department_analysis",  # 부서 fan-out 분석
    "llm_planner_required",  # 결정론으로 못 정함 - LLM 플래너의 몫
]

# 주문 문법이 소유하는 레인. 부서 fan-out을 만들지 않는다.
ORDER_LANES: frozenset[str] = frozenset(
    {
        "immediate_order",
        "conditional_order",
        "compound_order",
        "analysis_then_order",
    }
)


@dataclass(frozen=True)
class CeoRouteDecision:
    """한 질의의 레인 판정과 그 근거.

    `routing_basis`·`category`·`matched_terms`·`analysis_mode`는 기존
    결정론 플랜의 값을 그대로 옮긴다. `experience_bank`,
    `d5_improvement_pipeline`, `ceo_kanban_read`가 이 문자열들을 읽으므로
    값의 의미를 바꾸지 않는다.
    """

    lane: CeoLane
    workflow_mode: Literal["analysis", "binding"]
    selected_primary_profiles: tuple[str, ...]
    delegation_instructions: Mapping[str, str]
    analysis_mode: str
    category: str
    routing_basis: str
    matched_terms: Mapping[str, list[str]]
    # 부정으로 명시 배제된 부서. 결정론 라우터가 채운다.
    excluded_departments: tuple[str, ...] = ()
    # 왜 이 레인인지. 관측용이며 권한을 만들지 않는다.
    reason_codes: tuple[str, ...] = ()
    # 주문 레인일 때 이미 파싱된 결과. 호출부가 재파싱하지 않아도 된다.
    order_plan: object | None = None
    # 주문 **문법**이 감지됐는지. 부정 여부와 무관하다. 되묻기 게이트를
    # 건너뛸지와 D5 조회를 할지에만 쓰인다 - 집행 권한이 아니다.
    order_grammar_detected: bool = False
    # 기존 결정론 플랜 원본. root body 직렬화가 이 딕셔너리를 그대로 쓴다.
    routing_plan: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_order_lane(self) -> bool:
        return self.lane in ORDER_LANES


def classify_ceo_request(
    query: str,
    *,
    previous_question_context: str | None = None,
    selected_departments: Sequence[str] | None = None,
    routing_plan: Mapping[str, Any] | None = None,
    read_only_hr_e2e: bool = False,
    read_only_risk_e2e: bool = False,
) -> CeoRouteDecision:
    """질의 하나를 정확히 한 레인으로 보낸다.

    `routing_plan`은 BFF가 이미 만든 결정론 플랜이다. 주면 다시 만들지 않는다 -
    부서 별칭 경로(회계 alias 등)가 자기 스코프로 만든 플랜을 그대로 쓴다.

    `read_only_*` 플래그는 CEO가 소유한 읽기 전용 E2E 스코프다. 이 요청들은
    합법적으로 `매도`·`6개월 이내` 같은 어휘를 담고 있어서, 주문 문법이
    먼저 집어가지 않게 상위에서 판정해 넘긴다.
    """

    text = str(query or "")

    # ── Stage 0~1, 4~6 ──────────────────────────────────────────────────
    # 정규화, 명시적 HR 스코프, 단일부서 스코프, 카테고리 기본값 + 키워드
    # 사전, 되묻기 게이트는 `build_deterministic_bff_plan`이 이미 한 곳에서
    # 수행한다. 여기서 다시 구현하면 `verify_primary_route`가 검증하는
    # 정책이 둘로 갈린다.
    plan = dict(
        routing_plan
        if routing_plan is not None
        else build_deterministic_bff_plan(
            text,
            previous_question_context=previous_question_context,
            selected_departments=selected_departments,
        )
    )

    # ── Stage 3 ─────────────────────────────────────────────────────────
    # binding 여부. 주문 권한이 아니라 워크플로 등급이다.
    workflow_mode = infer_workflow_mode(text)

    def decide(
        lane: CeoLane,
        *reason_codes: str,
        order_plan: object | None = None,
        order_grammar_detected: bool = False,
    ) -> CeoRouteDecision:
        return CeoRouteDecision(
            lane=lane,
            workflow_mode=workflow_mode,  # type: ignore[arg-type]
            selected_primary_profiles=tuple(
                str(profile)
                for profile in plan.get("selected_primary_profiles", ())
                if str(profile).strip()
            ),
            delegation_instructions=dict(plan.get("delegation_instructions") or {}),
            analysis_mode=str(plan.get("analysis_mode") or "standard_analysis"),
            category=str(plan.get("category") or "PORTFOLIO_RECOMMENDATION"),
            routing_basis=str(plan.get("routing_basis") or ""),
            matched_terms=dict(plan.get("matched_terms") or {}),
            excluded_departments=tuple(plan.get("excluded_departments") or ()),
            reason_codes=reason_codes,
            order_plan=order_plan,
            order_grammar_detected=order_grammar_detected,
            routing_plan=plan,
        )

    # 주문 **문법** 감지. 부정·질문·금지 문장도 True다 - 결정론 검증기가
    # 눈에 보이는 안전한 결과를 내도록 일부러 고감도로 잡는다.
    order_grammar = bool(
        looks_like_conditional_paper_rule(text) or looks_like_user_order_request(text)
    )

    # ── Stage 6 ─────────────────────────────────────────────────────────
    # 되묻기가 주문 레인보다 먼저다. 단, 주문 문법이 잡힌 문장은 엄격 PAPER
    # 레인의 결정론 검증기가 명시적 사유를 내도록 넘긴다.
    if plan.get("mode") == "clarification_required" and not order_grammar:
        return decide("clarification", "insufficient_query_intent")

    # ── Stage 1 ─────────────────────────────────────────────────────────
    # 읽기 전용 E2E 스코프는 주문 문법 검사를 건너뛴다.
    read_only_scope = bool(read_only_hr_e2e or read_only_risk_e2e)

    # ── Stage 2 ─────────────────────────────────────────────────────────
    # 주문 문법 4종. 좁은 것부터 넓은 것 순서이며, 순서가 곧 우선순위다.
    if not read_only_scope:
        analysis_then = parse_analysis_then_conditional_paper_order(text)
        if analysis_then is not None:
            return decide(
                "analysis_then_order",
                "order_grammar.analysis_then_conditional",
                order_plan=analysis_then,
                order_grammar_detected=order_grammar,
            )
        compound = parse_compound_paper_order(text)
        if compound is not None:
            return decide(
                "compound_order",
                "order_grammar.compound",
                order_plan=compound,
                order_grammar_detected=order_grammar,
            )
        if looks_like_conditional_paper_rule(text):
            return decide(
                "conditional_order",
                "order_grammar.conditional_rule",
                order_grammar_detected=order_grammar,
            )
        if looks_like_user_order_request(text) and not (
            is_clearly_non_executable_order_language(text)
        ):
            return decide(
                "immediate_order",
                "order_grammar.immediate",
                order_grammar_detected=order_grammar,
            )

    # ── Stage 5 결과 확정 ───────────────────────────────────────────────
    # 결정론 플랜이 그대로 응답 평면이 되는 경우. Risk E2E는 CEO가 소유한
    # 처리를 유지해야 하므로 자유 문장에서 온 research/risk 기본값을
    # 물려받지 않는다.
    if workflow_mode == "analysis" and not read_only_risk_e2e:
        return decide(
            "department_analysis",
            "deterministic_department_route",
            order_grammar_detected=order_grammar,
        )

    # ── Stage 7 ─────────────────────────────────────────────────────────
    # 결정론으로 확정하지 못했다. 여기서 LLM을 부르지 않는다 - 호출은 상위 몫이다.
    return decide(
        "llm_planner_required",
        "read_only_risk_e2e_scope" if read_only_risk_e2e else "binding_not_resolved",
        order_grammar_detected=order_grammar,
    )


__all__ = [
    "ORDER_LANES",
    "CeoLane",
    "CeoRouteDecision",
    "classify_ceo_request",
]
