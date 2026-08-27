"""Deterministic CEO query routing shared by BFF and portfolio workflows.

This module intentionally has no LangGraph, worker, or Hermes imports.  The
same bounded routing decision can therefore run at the BFF boundary without
loading the full portfolio graph or invoking a second LLM planner.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
ACCOUNT_STATUS_CATEGORY = "ACCOUNT_STATUS"
HR_E2E_CATEGORY = "HR_E2E_READONLY"
HR_E2E_WORKFLOW = "hr-workforce-e2e"
DISCORD_CEO_CASE_TYPE_PREFIX = "discord_ceo"

CATEGORY_WORKFLOWS: dict[str, str] = {
    "PORTFOLIO_RECOMMENDATION": PORTFOLIO_WORKFLOW,
    "MARKET_RESEARCH": PORTFOLIO_WORKFLOW,
    "RISK_REVIEW": PORTFOLIO_WORKFLOW,
    "TAX_LIQUIDITY": PORTFOLIO_WORKFLOW,
    "REBALANCING_PROPOSAL": PORTFOLIO_WORKFLOW,
    "STRATEGY_PROPOSAL": STRATEGY_WORKFLOW,
    ACCOUNT_STATUS_CATEGORY: PORTFOLIO_WORKFLOW,
    HR_E2E_CATEGORY: HR_E2E_WORKFLOW,
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
    # A read-only account/holdings status request is an Accounting-owned
    # report. Research and Risk may be added only when the user explicitly
    # asks for market or risk analysis in the same request.
    ACCOUNT_STATUS_CATEGORY: ("accounting", "ceo"),
    HR_E2E_CATEGORY: ("hr",),
}

_QUERY_STAGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "trading": ("주문", "매수", "매도", "체결", "리밸런싱", "거래"),
    "accounting": ("세금", "수수료", "원장", "nav", "현금", "현금흐름", "대사", "회계"),
    "research": (
        "종목",
        "주식",
        "etf",
        "뉴스",
        "시장",
        "수익",
        "유니버스",
        "업종",
        "국내",
        "글로벌",
    ),
    "risk": ("위험", "리스크", "손실", "변동", "헤지", "레버리지", "공매도", "보수적"),
    "qa": ("검증", "근거", "신뢰", "감사", "오류", "검토", "출처"),
    "quant": ("전략", "백테스트", "가설", "과적합", "레짐", "데이터셋", "피처"),
}

# These are deliberately status/report phrases, not bare words such as
# "종목" or "보유". A bare holding/company question can still require
# Research; an account-status briefing should not fan out to Research/Risk.
_ACCOUNT_STATUS_QUERY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?:오늘|금일|당일)?\s*(?:매매|거래)\s*손익(?:금액)?", "매매손익"),
    (r"계좌\s*(?:현황|상태|브리핑|요약)", "계좌현황"),
    (r"잔고\s*(?:현황|상태|브리핑|요약|조회|확인)", "잔고현황"),
    (r"잔고(?:를|을)?\s*(?:알려|보여|확인|조회)", "잔고조회"),
    (r"보유\s*(?:현황|내역|잔고)", "보유현황"),
    (r"(?:포트폴리오|자산|순자산|현금)\s*(?:현황|상태|브리핑|요약)", "자산현황"),
    (r"(?:순자산|현금\s*잔고|평가손익|실현손익|미실현손익)", "계좌지표"),
    (r"(?:장\s*끝|장\s*마감|장\s*종료).{0,24}(?:계좌|잔고|현황|브리핑)", "장마감계좌"),
    (
        r"\b(?:account|portfolio)\s+(?:status|summary|briefing|balance)\b",
        "account_status",
    ),
    (r"\b(?:cash|account)\s+balance\b", "cash_balance"),
    (r"\bholdings?\s+(?:status|summary|overview)\b", "holdings_status"),
    (r"\bnav\b", "nav"),
)

_QUERY_INTENT_TERMS = (
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

# Safety instructions such as "주문은 하지 마" describe a prohibition, not a
# Trading request.  The router must not fan out to an execution-adjacent
# department merely because a user explicitly forbade that action.
_NEGATED_TERM_SUFFIX_RE = re.compile(
    r"^(?:은|는|이|가|을|를)?\s*(?:하지\s*(?:마|말고|않)|안\s|못\s|금지|불가|없)"
)
_RESEARCH_SCOPE_RE = re.compile(
    r"(?:리서치|연구|research)\s*(?:부서|팀|본부|department)",
    re.IGNORECASE,
)
_QUANT_SCOPE_RE = re.compile(
    r"(?:퀀트|quant)\s*(?:부서|팀|본부|department)",
    re.IGNORECASE,
)


def is_read_only_hr_e2e_query(query: str) -> bool:
    """Recognize the explicit HR Workforce verification lane once.

    HR E2E prompts mention prohibited financial mutations as safety assertions.
    They must be routed to HR before the generic high-recall portfolio terms
    see words such as ``투자`` or ``원장``.
    """

    text = " ".join(str(query or "").split()).casefold()
    has_hr_scope = bool(
        re.search(r"(?:hr-department|\bhr\s*(?:부서|팀|본부)\b|hr\s+e2e)", text)
    )
    has_read_only_scope = any(
        marker in text
        for marker in (
            "e2e",
            "읽기 전용",
            "read-only",
            "workforce api",
            "연결 확인",
            "통합 검증",
        )
    )
    return has_hr_scope and has_read_only_scope


def _explicit_accounting_e2e_scope(normalized_query: str) -> bool:
    """Keep an explicit accounting PAPER/E2E audit out of Trading fan-out."""

    has_accounting = "accounting" in normalized_query or "회계" in normalized_query
    has_e2e = "e2e" in normalized_query or "검증" in normalized_query
    has_read_only = "paper" in normalized_query or "읽기 전용" in normalized_query
    return has_accounting and has_e2e and has_read_only


def _account_status_terms(normalized_query: str) -> list[str]:
    """Return deterministic account-status signals in query order."""

    matched: list[str] = []
    for pattern, label in _ACCOUNT_STATUS_QUERY_PATTERNS:
        if (
            re.search(pattern, normalized_query, flags=re.IGNORECASE)
            and label not in matched
        ):
            matched.append(label)
    return matched


def _query_term_matches(normalized_query: str, term: str) -> bool:
    """Match Korean phrases by containment and English abbreviations by word."""

    normalized_term = term.casefold()
    if normalized_term.isascii() and any(char.isalnum() for char in normalized_term):
        matches = re.finditer(
            rf"(?<![a-z0-9_]){re.escape(normalized_term)}(?![a-z0-9_])",
            normalized_query,
        )
    else:
        matches = re.finditer(re.escape(normalized_term), normalized_query)
    return any(
        not _is_negated_suffix(normalized_query[match.end() :]) for match in matches
    )


def _is_negated_suffix(suffix: str) -> bool:
    """Recognize short safety prohibitions, including joined Korean clauses."""

    if _NEGATED_TERM_SUFFIX_RE.match(suffix):
        return True
    return bool(
        re.match(
            r"^[^.!?\n]{0,24}(?:하지\s*(?:마|말고|않)|안\s|못\s|금지|불가|없)",
            suffix,
        )
    )


def _explicit_research_scope(normalized_query: str) -> bool:
    """Recognize a Research-only request without adding a second planner."""

    if not _RESEARCH_SCOPE_RE.search(normalized_query):
        return False
    return not any(
        _query_term_matches(normalized_query, term)
        for stage in ("trading", "risk", "quant", "accounting")
        for term in _QUERY_STAGE_KEYWORDS[stage]
    )


def _explicit_quant_scope(normalized_query: str) -> bool:
    """Recognize a Quant-only request without a second planner or fan-out.

    Quant vocabulary overlaps with the generic Research/Risk/Trading keyword
    table: returns, volatility, trading-day windows, fills, and market data
    quality are all valid Quant questions.  Once the user names Quant and does
    not name another department, that explicit scope is authoritative; the
    Quant read-only boundary remains responsible for rejecting any unsafe
    action wording.
    """

    if not _QUANT_SCOPE_RE.search(normalized_query):
        return False
    explicit_other_scope = _RESEARCH_SCOPE_RE.search(normalized_query) or re.search(
        r"(?:risk|리스크|위험|trading|트레이딩|거래|accounting|회계)"
        r"\s*(?:부서|팀|본부|department)",
        normalized_query,
        flags=re.IGNORECASE,
    )
    return not explicit_other_scope


def query_requires_clarification(query: str, category: str = "") -> bool:
    """Reject filler before any LLM planning or department fan-out."""

    normalized_query = " ".join(str(query or "").split()).casefold()
    normalized_category = str(category or "").strip().upper()
    if not normalized_query or normalized_category in CATEGORY_WORKFLOWS:
        return False
    if _account_status_terms(normalized_query):
        return False
    if any(
        _query_term_matches(normalized_query, term)
        for terms in _QUERY_STAGE_KEYWORDS.values()
        for term in terms
    ):
        return False
    if any(term in normalized_query for term in _QUERY_INTENT_TERMS):
        return False
    # Explicit tickers/identifiers are meaningful only when the query also
    # carries an action term; a bare short token such as conversational filler
    # must not trigger a research/risk fan-out.
    has_explicit_identifier = bool(
        re.search(
            r"(?:\$[A-Z][A-Z0-9._-]{1,14}\b|\b[A-Z]{2,6}\b|\b\d{6}\b)", str(query or "")
        )
    )
    return not has_explicit_identifier


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
            "routing_basis": "category_default"
            if category_departments
            else "structured_suitability_default",
            "matched_terms": {},
        }

    normalized = query.casefold()
    if is_read_only_hr_e2e_query(query):
        return {
            "mode": "hr_e2e_readonly",
            "category": HR_E2E_CATEGORY,
            "workflow": HR_E2E_WORKFLOW,
            "category_recognized": True,
            "original_query": query,
            "rewritten_query": (
                f"{query} Workforce 관측 API를 PAPER/read-only로 검증하고 "
                "주문·투자·원장·권한 변경 없이 결과를 보고한다."
            ),
            "requested_departments": ["hr"],
            "routing_basis": "explicit_hr_e2e_scope",
            "matched_terms": {"hr": ["hr_e2e", "workforce_api", "read_only"]},
        }
    if query_requires_clarification(query, category):
        return {
            "mode": "clarification_required",
            "category": category or "PORTFOLIO_RECOMMENDATION",
            "workflow": workflow,
            "category_recognized": category_recognized,
            "original_query": query,
            "rewritten_query": (
                "요청 대상과 원하는 작업(예: 종목 분석, 계좌 현황 조회, 위험 검토)을 "
                "구체적으로 다시 확인한다."
            ),
            "requested_departments": [],
            "routing_basis": "insufficient_query_intent",
            "matched_terms": {},
        }
    account_status_terms = _account_status_terms(normalized)
    explicit_accounting_e2e = not category and _explicit_accounting_e2e_scope(
        normalized
    )
    inferred_account_status = not category and (
        bool(account_status_terms) or explicit_accounting_e2e
    )
    if inferred_account_status:
        category = ACCOUNT_STATUS_CATEGORY
        category_departments = CATEGORY_DEPARTMENTS[ACCOUNT_STATUS_CATEGORY]
        category_recognized = True
        workflow = CATEGORY_WORKFLOWS[ACCOUNT_STATUS_CATEGORY]

    research_only = (
        not category
        and _explicit_research_scope(normalized)
        and not account_status_terms
    )
    quant_only = not category and _explicit_quant_scope(normalized)
    stages: set[str] = set(
        ("quant", "ceo")
        if quant_only
        else ("research", "ceo")
        if research_only
        else category_departments or ("research", "risk", "ceo")
    )
    matched_terms: dict[str, list[str]] = {}
    if account_status_terms:
        matched_terms["accounting"] = account_status_terms
    elif explicit_accounting_e2e:
        matched_terms["accounting"] = ["명시적 회계 PAPER E2E 검증"]
    for stage, terms in _QUERY_STAGE_KEYWORDS.items():
        if (explicit_accounting_e2e and stage != "accounting") or quant_only:
            continue
        hits = [term for term in terms if _query_term_matches(normalized, term)]
        if hits:
            stages.add(stage)
            matched_terms.setdefault(stage, []).extend(
                term for term in hits if term not in matched_terms.get(stage, [])
            )

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
        "routing_basis": (
            "accounting_account_status_intent"
            if inferred_account_status
            and not any(
                stage in matched_terms
                for stage in ("research", "quant", "trading", "risk")
            )
            else "explicit_research_scope"
            if research_only
            else "explicit_quant_scope"
            if quant_only
            else "bounded_query_intent_router"
        ),
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
    "hr": (
        "Perform the exact HR Workforce PAPER/read-only verification. Run the "
        "approved helper once and inspect only the three allowed Workforce GETs; "
        "do not place orders, invest, mutate a ledger, or change permissions."
    ),
}


def build_deterministic_bff_plan(
    query: str,
    *,
    selected_departments: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return a complete machine-readable plan for the BFF fast path.

    The route selection is the canonical ``build_ceo_task_plan`` result.  This
    helper only converts logical department names into the canonical Hermes
    profiles and one-line briefs required by the existing supervisor
    materializer; it does not add another routing policy.
    """

    plan = build_ceo_task_plan({"query": query})
    if selected_departments is None:
        selected = [
            str(department)
            for department in plan.get("requested_departments", [])
            if str(department) not in {"ceo", "qa"}
        ]
    else:
        selected = []
        for department in selected_departments:
            normalized = str(department).strip().lower()
            if normalized not in DEPARTMENTS or normalized in {"ceo", "qa"}:
                raise ValueError(f"unsupported selected department: {department}")
            if normalized not in selected:
                selected.append(normalized)
        if not selected:
            raise ValueError("selected_departments must contain a response department")
        # A department-specific alias is already authorized at its boundary;
        # preserve the shared keyword/category analysis as metadata, but scope
        # materialization to that department so CEO does not answer on its own.
        plan = {
            **plan,
            "requested_departments": selected,
            "routing_basis": "explicit_department_scope",
        }
    selected_profiles: list[str] = []
    instructions: dict[str, str] = {}
    for department in selected:
        profile = canonical_profile_for_department(department)
        if profile in selected_profiles:
            continue
        selected_profiles.append(profile)
        instructions[profile] = _DEPARTMENT_INSTRUCTIONS[department]

    configured_mode = (
        os.getenv("CEO_BFF_ANALYSIS_MODE", "standard_analysis").strip().lower()
    )
    analysis_mode = (
        configured_mode
        if configured_mode in {"fast_advisory", "standard_analysis", "full_experiment"}
        else "standard_analysis"
    )
    if (
        plan.get("routing_basis") == "explicit_research_scope"
        and selected == ["research"]
        and analysis_mode in {"fast_advisory", "standard_analysis"}
    ):
        # Research SOUL already defines the bounded fast-advisory contract.
        # Select it only for an explicitly scoped Research request so broad
        # portfolio/strategy workflows keep their existing operator setting.
        analysis_mode = "fast_advisory"
    return {
        **plan,
        "selected_primary_profiles": tuple(selected_profiles),
        "delegation_instructions": instructions,
        "analysis_mode": analysis_mode,
        "producer": "portfolio-bff-deterministic",
    }


@dataclass(frozen=True)
class RouteVerification:
    """Compare one materialized primary set with the canonical route."""

    valid: bool
    expected_category: str
    expected_primary_profiles: tuple[str, ...]
    actual_primary_profiles: tuple[str, ...]
    reason: str


def verify_primary_route(
    query: str,
    actual_primary_profiles: Sequence[str],
) -> RouteVerification:
    """Verify a materialized route without introducing another routing policy."""

    expected_plan = build_deterministic_bff_plan(query)
    expected = tuple(
        str(profile).strip()
        for profile in expected_plan.get("selected_primary_profiles", ())
        if str(profile).strip()
    )
    actual = tuple(
        dict.fromkeys(
            str(profile).strip()
            for profile in actual_primary_profiles
            if str(profile).strip()
        )
    )
    valid = set(expected) == set(actual)
    return RouteVerification(
        valid=valid,
        expected_category=str(
            expected_plan.get("category") or "PORTFOLIO_RECOMMENDATION"
        ),
        expected_primary_profiles=expected,
        actual_primary_profiles=actual,
        reason="match" if valid else "primary_profile_set_mismatch",
    )


__all__ = [
    "ACCOUNT_STATUS_CATEGORY",
    "CATEGORY_DEPARTMENTS",
    "CATEGORY_WORKFLOWS",
    "DEPARTMENTS",
    "DISCORD_CEO_CASE_TYPE_PREFIX",
    "HR_E2E_CATEGORY",
    "HR_E2E_WORKFLOW",
    "PORTFOLIO_WORKFLOW",
    "STRATEGY_WORKFLOW",
    "RouteVerification",
    "build_ceo_task_plan",
    "build_deterministic_bff_plan",
    "is_read_only_hr_e2e_query",
    "query_requires_clarification",
    "verify_primary_route",
]
