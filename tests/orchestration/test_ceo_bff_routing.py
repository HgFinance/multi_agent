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
