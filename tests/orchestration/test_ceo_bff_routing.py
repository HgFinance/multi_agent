from __future__ import annotations

from orchestration.ceo_query_routing import build_deterministic_bff_plan
from orchestration.ceo_workflow_scope import (
    build_root_body,
    selected_primary_profiles_from_body,
)


def test_bff_route_is_serialized_for_the_existing_supervisor_materializer() -> None:
    plan = build_deterministic_bff_plan("삼성전자 시장 위험을 분석해줘")

    body = build_root_body(
        "삼성전자 시장 위험을 분석해줘",
        "request-bff-route",
        producer=plan["producer"],
        selected_primary_profiles=plan["selected_primary_profiles"],
        delegation_instructions=plan["delegation_instructions"],
        analysis_mode=plan["analysis_mode"],
        routing_basis=plan["routing_basis"],
        routing_category=plan["category"],
    )

    assert selected_primary_profiles_from_body(body) == (
        "research-department",
        "risk-management",
    )
    assert "producer=portfolio-bff-deterministic" in body.splitlines()
    assert "analysis_mode=standard_analysis" in body.splitlines()
    assert "delegation_instruction.research-department=" in body
    assert "delegation_instruction.risk-management=" in body
    assert "delegation_instruction.qa-department=" not in body


def test_hr_e2e_query_routes_only_to_workforce_profile() -> None:
    plan = build_deterministic_bff_plan(
        "HR E2E 검증: PAPER/read-only로 Workforce API GET 3건만 실행하고 "
        "주문·원장 변경은 금지"
    )

    assert plan["routing_basis"] == "explicit_hr_e2e_scope"
    assert plan["category"] == "HR_E2E_READONLY"
    assert plan["selected_primary_profiles"] == ("hr-department",)
    assert tuple(plan["requested_departments"]) == ("hr",)


def test_explicit_research_scope_ignores_prohibited_order_wording() -> None:
    plan = build_deterministic_bff_plan(
        "리서치 부서에서 삼성전자 최근 사업 방향을 공식 자료와 뉴스로 검토해줘. "
        "투자 추천과 주문은 하지 마."
    )

    assert plan["routing_basis"] == "explicit_research_scope"
    assert plan["requested_departments"] == ["research", "ceo"]
    assert plan["selected_primary_profiles"] == ("research-department",)
    assert plan["analysis_mode"] == "fast_advisory"
    assert "trading" not in plan["matched_terms"]


def test_mixed_language_research_department_scope_does_not_fan_out() -> None:
    plan = build_deterministic_bff_plan(
        "Research 부서만 사용해 삼성전자 실적의 긍정·반대 근거를 검증해줘. "
        "실제 주문은 하지 마."
    )

    assert plan["routing_basis"] == "explicit_research_scope"
    assert plan["selected_primary_profiles"] == ("research-department",)
    assert plan["analysis_mode"] == "fast_advisory"


def test_explicit_quant_scope_does_not_fan_out_to_research_or_risk() -> None:
    plan = build_deterministic_bff_plan(
        "Quant 부서에서 069500.KS 데이터셋의 시계열 범위와 원본 좌표를 "
        "읽기 전용으로 확인해줘. 근거가 부족한 성과지표는 HOLD로 표시해줘."
    )

    assert plan["routing_basis"] == "explicit_quant_scope"
    assert plan["requested_departments"] == ["quant", "ceo"]
    assert plan["selected_primary_profiles"] == ("quant-backtest-department",)


def test_quant_scope_treats_joined_order_prohibition_as_safety_text() -> None:
    plan = build_deterministic_bff_plan(
        "Quant 부서에서 069500.KS 데이터셋의 시계열 범위와 원본 좌표를 "
        "읽기 전용으로 확인해 주세요. 주문·승격은 하지 마세요."
    )

    assert plan["routing_basis"] == "explicit_quant_scope"
    assert plan["selected_primary_profiles"] == ("quant-backtest-department",)


def test_quant_scope_allows_market_data_terms_without_research_fanout() -> None:
    plan = build_deterministic_bff_plan(
        "Quant 부서가 069500.KS(KODEX 200 ETF)의 데이터 품질과 시계열 범위를 "
        "읽기 전용으로 검토하고, 근거가 없으면 성과지표를 HOLD로 표시해줘."
    )

    assert plan["routing_basis"] == "explicit_quant_scope"
    assert plan["requested_departments"] == ["quant", "ceo"]
    assert plan["selected_primary_profiles"] == ("quant-backtest-department",)


def test_quant_scope_keeps_performance_terms_on_the_quant_pipeline() -> None:
    plan = build_deterministic_bff_plan(
        "Quant 부서에서 069500.KS 최근 20거래일 수익률·변동성·샤프·MDD를 "
        "검증하고 데이터가 없으면 HOLD해줘."
    )

    assert plan["routing_basis"] == "explicit_quant_scope"
    assert plan["requested_departments"] == ["quant", "ceo"]
    assert plan["selected_primary_profiles"] == ("quant-backtest-department",)


def test_quant_scope_keeps_data_quality_terms_on_the_quant_pipeline() -> None:
    plan = build_deterministic_bff_plan(
        "Quant 부서에서 069500.KS OHLCV 결측·중복·거래일 정합성을 점검해줘."
    )

    assert plan["routing_basis"] == "explicit_quant_scope"
    assert plan["selected_primary_profiles"] == ("quant-backtest-department",)


def test_bff_route_can_be_scoped_to_one_authorized_department() -> None:
    plan = build_deterministic_bff_plan(
        "원장과 현금 대사 상태를 PAPER 읽기 전용으로 검토해줘",
        selected_departments=("accounting",),
    )

    assert plan["requested_departments"] == ["accounting"]
    assert plan["selected_primary_profiles"] == ("accounting-portfolio-department",)
    assert set(plan["delegation_instructions"]) == {"accounting-portfolio-department"}


def test_account_status_briefing_routes_only_to_accounting() -> None:
    plan = build_deterministic_bff_plan("오늘 장끝났으니까 계좌현황 브리핑해줘")

    assert plan["category"] == "ACCOUNT_STATUS"
    assert plan["routing_basis"] == "accounting_account_status_intent"
    assert plan["requested_departments"] == ["accounting", "ceo"]
    assert plan["selected_primary_profiles"] == ("accounting-portfolio-department",)
    assert set(plan["delegation_instructions"]) == {"accounting-portfolio-department"}


def test_trading_pnl_routes_only_to_accounting() -> None:
    plan = build_deterministic_bff_plan("오늘 매매손익 분석해줘")

    assert plan["category"] == "ACCOUNT_STATUS"
    assert plan["routing_basis"] == "accounting_account_status_intent"
    assert plan["requested_departments"] == ["accounting", "ceo"]
    assert plan["selected_primary_profiles"] == ("accounting-portfolio-department",)
    assert plan["matched_terms"]["accounting"] == ["매매손익"]


def test_explicit_accounting_e2e_scope_ignores_prohibited_trade_wording() -> None:
    plan = build_deterministic_bff_plan(
        "CEO 회계 부서 E2E 검증: PAPER 환경에서 회계 Hermes가 계좌 현황을 "
        "읽기 전용으로 확인하고 실거래·원장 변경·순자산 가치 확정은 하지 않는다."
    )

    assert plan["category"] == "ACCOUNT_STATUS"
    assert plan["routing_basis"] == "accounting_account_status_intent"
    assert plan["selected_primary_profiles"] == ("accounting-portfolio-department",)
    assert "trading" not in plan["matched_terms"]


def test_account_status_with_explicit_risk_request_keeps_both_specialists() -> None:
    plan = build_deterministic_bff_plan("계좌현황과 집중위험을 함께 브리핑해줘")

    assert plan["requested_departments"] == ["risk", "accounting", "ceo"]
    assert plan["selected_primary_profiles"] == (
        "risk-management",
        "accounting-portfolio-department",
    )
    assert plan["routing_basis"] == "bounded_query_intent_router"


def test_naver_is_not_misclassified_as_nav_accounting_signal() -> None:
    plan = build_deterministic_bff_plan("NAVER 실적을 분석해줘")

    assert "accounting" not in plan["matched_terms"]
    assert plan["selected_primary_profiles"] == (
        "research-department",
        "risk-management",
    )


def test_filler_query_requires_clarification_without_department_fanout() -> None:
    plan = build_deterministic_bff_plan("오우")

    assert plan["mode"] == "clarification_required"
    assert plan["routing_basis"] == "insufficient_query_intent"
    assert plan["selected_primary_profiles"] == ()
    assert plan["requested_departments"] == []
