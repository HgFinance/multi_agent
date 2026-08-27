"""Deterministic CEO query routing shared by BFF and portfolio workflows.

This module intentionally has no LangGraph, worker, or Hermes imports.  The
same bounded routing decision can therefore run at the BFF boundary without
loading the full portfolio graph or invoking a second LLM planner.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from orchestration.canonical_profiles import canonical_profile_for_department

# The response plane. QA is an asynchronous post-response consumer and is not
# an analysis primary in this list.
DEPARTMENTS: tuple[str, ...] = (
    "research",
    "quant",
    "trading",
    "risk",
    "accounting",
    "ceo",
)

PORTFOLIO_WORKFLOW = "portfolio-recommendation"
STRATEGY_WORKFLOW = "strategy-research"

CATEGORY_WORKFLOWS: dict[str, str] = {
    "PORTFOLIO_RECOMMENDATION": PORTFOLIO_WORKFLOW,
    "MARKET_RESEARCH": PORTFOLIO_WORKFLOW,
    "RISK_REVIEW": PORTFOLIO_WORKFLOW,
    "TAX_LIQUIDITY": PORTFOLIO_WORKFLOW,
    "REBALANCING_PROPOSAL": PORTFOLIO_WORKFLOW,
    "STRATEGY_PROPOSAL": STRATEGY_WORKFLOW,
}

# Category defaults are conservative.  Free-form keyword matches may add a
# response-plane department, never remove one.  QA keywords remain audit
# intent only because QA is scheduled after the CEO response.
CATEGORY_DEPARTMENTS: dict[str, tuple[str, ...]] = {
    "PORTFOLIO_RECOMMENDATION": ("research", "quant", "risk", "ceo"),
    "MARKET_RESEARCH": ("research", "ceo"),
    "RISK_REVIEW": ("research", "risk", "ceo"),
    "TAX_LIQUIDITY": ("research", "risk", "accounting", "ceo"),
    "REBALANCING_PROPOSAL": (
        "research",
        "quant",
        "trading",
        "risk",
        "accounting",
        "ceo",
    ),
    "STRATEGY_PROPOSAL": ("research", "quant", "ceo"),
}

_QUERY_STAGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "trading": ("주문", "매수", "매도", "체결", "리밸런싱", "거래"),
    "accounting": ("세금", "수수료", "원장", "nav", "현금", "현금흐름", "대사", "회계"),
    "research": ("종목", "주식", "etf", "뉴스", "시장", "수익", "유니버스", "업종", "국내", "글로벌"),
    "risk": ("위험", "리스크", "손실", "변동", "헤지", "레버리지", "공매도", "보수적"),
    "qa": ("검증", "근거", "신뢰", "감사", "오류", "검토", "출처"),
    "quant": ("전략", "백테스트", "가설", "과적합", "레짐", "데이터셋", "피처"),
}


def build_ceo_task_plan(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Build the canonical bounded route without an LLM or external I/O."""

    query = " ".join(str(profile.get("query", "")).split())
    category = str(profile.get("category", "")).strip().upper()
    category_departments = CATEGORY_DEPARTMENTS.get(category)
    category_recognized = category in CATEGORY_WORKFLOWS
    workflow = CATEGORY_WORKFLOWS.get(category, PORTFOLIO_WORKFLOW)

    if not query:
        requested_departments = list(category_departments or DEPARTMENTS)
        return {
            "mode": "category_default" if category_departments else "portfolio_default",
            "category": category or "PORTFOLIO_RECOMMENDATION",
            "workflow": workflow,
            "category_recognized": category_recognized,
            "original_query": "",
            "rewritten_query": "카테고리와 사용자 프로필에 맞는 비구속적 포트폴리오 후보를 검토한다.",
            "requested_departments": requested_departments,
            "routing_basis": "category_default" if category_departments else "structured_suitability_default",
            "matched_terms": {},
        }

    normalized = query.lower()
    stages: set[str] = set(category_departments or ("research", "risk", "ceo"))
    matched_terms: dict[str, list[str]] = {}
    for stage, terms in _QUERY_STAGE_KEYWORDS.items():
        hits = [term for term in terms if term in normalized]
        if hits:
            stages.add(stage)
            matched_terms[stage] = hits

    ordered = [stage for stage in DEPARTMENTS if stage in stages]
    return {
        "mode": "free_query",
        "category": category or "PORTFOLIO_RECOMMENDATION",
        "workflow": workflow,
        "category_recognized": category_recognized,
        "original_query": query,
        "rewritten_query": (
            f"{query} 사용자 요청을 적합성·근거·리스크 관점에서 검토하고, "
            "주문이나 장부 변경 없이 결과를 설명한다."
        ),
        "requested_departments": ordered,
        "matched_terms": matched_terms,
        "routing_basis": "bounded_query_intent_router",
    }


_DEPARTMENT_INSTRUCTIONS: dict[str, str] = {
    "research": (
        "Inspect current authoritative market/news/company evidence relevant to the "
        "user request. Return dated evidence, source references, uncertainties, and "
        "a concise non-binding research conclusion. Do not place or recommend an order."
    ),
    "quant": (
        "Assess the requested strategy, signal, or quantitative claim using the "
        "available reproducible data and methodology. State assumptions, limitations, "
        "and whether more backtest evidence is needed; do not promote a strategy."
    ),
    "trading": (
        "Review the trading or rebalancing implications as a read-only advisory. "
        "Do not submit, stage, or authorize an order; report only deterministic checks "
        "and unresolved inputs."
    ),
    "risk": (
        "Assess portfolio, market, mandate, and concentration risks relevant to the "
        "request. Cite the evidence used, identify missing facts, and make no approval "
        "or execution decision."
    ),
    "accounting": (
        "Review the accounting, liquidity, fee, NAV, or portfolio-state implications "
        "relevant to the request using read-only evidence. Do not mutate a ledger or "
        "confirm NAV."
    ),
}


def build_deterministic_bff_plan(query: str) -> dict[str, Any]:
    """Return a complete machine-readable plan for the BFF fast path.

    The route selection is the canonical ``build_ceo_task_plan`` result.  This
    helper only converts logical department names into the canonical Hermes
    profiles and one-line briefs required by the existing supervisor
    materializer; it does not add another routing policy.
    """

    plan = build_ceo_task_plan({"query": query})
    selected_departments = [
        str(department)
        for department in plan.get("requested_departments", [])
        if str(department) not in {"ceo", "qa"}
    ]
    selected_profiles: list[str] = []
    instructions: dict[str, str] = {}
    for department in selected_departments:
        profile = canonical_profile_for_department(department)
        if profile in selected_profiles:
            continue
        selected_profiles.append(profile)
        instructions[profile] = _DEPARTMENT_INSTRUCTIONS[department]

    configured_mode = os.getenv("CEO_BFF_ANALYSIS_MODE", "standard_analysis").strip().lower()
    analysis_mode = (
        configured_mode
        if configured_mode in {"fast_advisory", "standard_analysis", "full_experiment"}
        else "standard_analysis"
    )
    return {
        **plan,
        "selected_primary_profiles": tuple(selected_profiles),
        "delegation_instructions": instructions,
        "analysis_mode": analysis_mode,
        "producer": "portfolio-bff-deterministic",
    }


__all__ = [
    "CATEGORY_DEPARTMENTS",
    "CATEGORY_WORKFLOWS",
    "DEPARTMENTS",
    "PORTFOLIO_WORKFLOW",
    "STRATEGY_WORKFLOW",
    "build_ceo_task_plan",
    "build_deterministic_bff_plan",
]
