"""Async TEST/Supabase-read-only pipeline for user suitability and all investment departments.

The graph connects Research -> Trading -> Risk -> QA -> Accounting -> CEO.
Each department stage uses LangGraph ``Send`` fan-out to independent Worker
graphs and a reducer-backed fan-in node. No order, ledger, credential, or
No order, ledger, credential, or production side effect is permitted.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import operator
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from departments.employee_worker_runtime import (
    WorkerSpec,
    build_independent_worker_graph,
    default_worker_llm,
    should_run,
    tools_for_specs,
)
from orchestration.adapters.ceo_task_planner import build_task_plan
from orchestration.contracts.mas import (
    DepartmentHandoff,
    build_replay_metadata,
    make_pipeline_event,
    stable_hash,
    validate_worker_context,
)
from orchestration.llm_observability import (
    begin_worker_metric,
    end_worker_metric,
    publish_metric,
    redacted_trace,
)

ROOT = Path(__file__).resolve().parents[2]
PIPELINE_VERSION = "portfolio-recommendation-full-async-v1"
RuntimeEventCallback = Callable[[Mapping[str, Any]], None]

# 2026-08-10 팀 합의: 포트폴리오 구성 요청은 요청 시점에 research -> quant 를 부른다.
# 이전 설계는 "백테스트는 strategy_research_cycle 에서 미리 끝내고 추천은 그 결과를
# 쓴다"였는데, 검증된 전략과 추천을 잇는 계보가 실제로 없었다(USER_INPUT_SCOPE_ANALYSIS
# §J "포트폴리오 후보 카탈로그를 누가 만드는가"). 팀은 카탈로그를 만드는 대신 요청
# 시점 백테스트로 가기로 했다.
#
# MAS_PIPELINE_CONTRACTS 의 "Quant 를 포트폴리오 추천 그래프에 **암묵적으로** 끼워
# 넣지 않는다"와 충돌하지 않는다 - 금지된 것은 암묵적 삽입이고, 여기서는 선언된
# 단계로 명시한다. strategy_research_cycle(전략 승격용 quant -> qa -> ceo)은 그대로
# 별도 흐름으로 남는다. 한 부서가 두 흐름에 나오는 것은 research·risk 도 마찬가지다.
DEPARTMENTS: tuple[str, ...] = (
    "research",
    "quant",
    "trading",
    "risk",
    "qa",
    "accounting",
    "ceo",
)

_QUERY_WORKER_TERMS: dict[str, tuple[str, ...]] = {
    "research-data-worker": ("종목", "주식", "시장", "가격", "시세", "유니버스", "국내", "글로벌"),
    "microstructure-worker": ("유동성", "거래량", "호가", "체결", "스프레드"),
    "technical-signal-worker": ("차트", "기술적", "추세", "모멘텀", "기술 분석"),
    "fundamental-valuation-worker": ("재무", "실적", "밸류", "가치", "저평가", "고평가"),
    "news-macro-worker": ("뉴스", "금리", "환율", "거시", "경제", "정책"),
    "evidence-rag-worker": ("근거", "출처", "자료", "검증", "인용"),
    "order-constraint-worker": ("주문", "한도", "제약", "컴플라이언스"),
    "execution-planning-worker": ("체결", "실행", "집행"),
    "venue-cost-worker": ("수수료", "슬리피지", "거래비용"),
    "derivatives-structure-worker": ("파생", "레버리지", "공매도", "옵션", "선물"),
    "compliance-policy-worker": ("규정", "정책", "법", "컴플라이언스", "감사"),
    # 회계 직원 8명이 2026-08-07에 exception-investigation-worker 하나로 합쳐졌다.
    # 강등된 7명이 쓰던 용어도 여기로 모은다 - 사용자는 "손익"이나 "기준가"라고
    # 물을 뿐 어느 직원이 남았는지 모른다. 용어를 지우면 그 질의가 회계본부로
    # 라우팅되지 않고 조용히 빠진다.
    "exception-investigation-worker": ("원장", "대사", "거래내역", "nav", "기준가", "마감",
                                       "pnl", "손익", "성과", "차이", "불일치", "예외"),
    "hallucination-critic-worker": ("근거", "출처", "검증", "인용", "환각", "모순", "신뢰"),
    "incident-postmortem-worker": ("사고", "장애", "재발", "인시던트"),
    "strategy-hypothesis-worker": ("전략", "가설", "아이디어", "전략 추천"),
    "dataset-feature-worker": ("데이터셋", "피처", "지표 구성"),
    "backtest-optimization-worker": ("백테스트", "과거 성과", "수익률 검증", "최적화"),
    "regime-robustness-worker": ("국면", "레짐", "스트레스", "견고성"),
}

# core-risk-worker/derivatives-counterparty-worker(risk)와 evidence-qa-worker/
# model-and-internal-audit-worker/ops-and-permission-worker(qa)는 2026-08-06에
# risk-runner/qa-runner(결정론, LLM 없음)로 흡수됐다 - WORKER_SPECS에 더 없으니
# 이 free-text query 라우팅 대상에도 없다.
# 2026-08-07: 회계 8명도 같은 이유로 back-office-runner에 흡수됐다. 기본 직원이던
# portfolio-control-worker/investor-reporting-worker가 둘 다 강등돼서 남은 조사관
# 하나로 바꿨다 - 없는 id를 남겨두면 fallback이 조용히 빈 목록이 된다.
_QUERY_WORKER_FALLBACKS: dict[str, tuple[str, ...]] = {
    "research": ("research-data-worker", "evidence-rag-worker"),
    # quant 7명 중 trigger 가 always 인 둘. 나머지 5명은 backtest_request 등
    # 전용 신호가 있을 때만 켜지므로 자유 질의 fallback 에 넣지 않는다.
    "quant": ("strategy-hypothesis-worker", "dataset-feature-worker"),
    "trading": (),
    "risk": ("compliance-policy-worker",),
    "qa": ("hallucination-critic-worker", "incident-postmortem-worker"),
    "accounting": ("exception-investigation-worker",),
    "ceo": ("executive-briefing-worker",),
}

# 카테고리는 두 가지를 따로 결정한다 - (1) 어느 Workflow 소속인가, (2) 그 Workflow
# 안에서 어느 부서를 부르는가. 이 둘을 분리해 두는 이유는 CLAUDE.md "5개 흐름 - 서로
# 분리, 섞지 않는다"와 MAS_PIPELINE_CONTRACTS.md "Quant와 HR은 포트폴리오 추천 그래프에
# 암묵적으로 끼워 넣지 않는다"를 코드에서도 지키기 위해서다.
#
# 특히 전략 연구 요청("이런 전략 어때?")은 quant-backtest 를 아래 DEPARTMENTS 에
# 추가해서 푸는 문제가 **아니다.** 그건 strategy-research 체인(quant → qa → ceo)이
# 소유하며, 이 그래프에 quant 를 끼워 넣으면 위 두 문서를 동시에 위반한다.
PORTFOLIO_WORKFLOW = "portfolio-recommendation"
STRATEGY_WORKFLOW = "strategy-research"

CATEGORY_WORKFLOWS: dict[str, str] = {
    "PORTFOLIO_RECOMMENDATION": PORTFOLIO_WORKFLOW,
    "MARKET_RESEARCH": PORTFOLIO_WORKFLOW,
    "RISK_REVIEW": PORTFOLIO_WORKFLOW,
    "TAX_LIQUIDITY": PORTFOLIO_WORKFLOW,
    "REBALANCING_PROPOSAL": PORTFOLIO_WORKFLOW,
    # 이 그래프가 처리할 수 없다 - task_plan.workflow 로 호출부에 알린다.
    "STRATEGY_PROPOSAL": STRATEGY_WORKFLOW,
}

# Category is the first routing hint. The free-form query may expand this
# bounded set when it clearly needs another domain.
# quant 는 **포트폴리오·전략을 구성하는 카테고리에만** 넣는다. 팀 합의는 "요청마다
# research -> quant"였지만 그 대상은 구성 요청이다 - "삼전 지금 사도 돼?" 같은 단순
# 종목 질문(MARKET_RESEARCH)까지 백테스트를 돌리면 3단계가 4단계 + 실제 연산으로
# 늘어나 대화 응답성이 무너진다. RISK_REVIEW·TAX_LIQUIDITY 도 기존 보유분에 대한
# 질문이라 새 후보를 만들지 않으므로 제외한다.
CATEGORY_DEPARTMENTS: dict[str, tuple[str, ...]] = {
    "PORTFOLIO_RECOMMENDATION": ("research", "quant", "risk", "qa", "ceo"),
    "MARKET_RESEARCH": ("research", "qa", "ceo"),
    "RISK_REVIEW": ("research", "risk", "qa", "ceo"),
    "TAX_LIQUIDITY": ("research", "risk", "accounting", "qa", "ceo"),
    "REBALANCING_PROPOSAL": (
        "research",
        "quant",
        "trading",
        "risk",
        "accounting",
        "qa",
        "ceo",
    ),
    # 전략 제안. 정식 승격 흐름은 여전히 strategy-research(quant -> qa -> ceo)가
    # 소유하지만(task_plan.workflow 참고), 대화에서 들어온 전략 질문에 답하려면
    # 이 그래프에서도 백테스트 근거가 필요하다. 주문·원장 부서는 넣지 않는다.
    "STRATEGY_PROPOSAL": ("research", "quant", "qa", "ceo"),
}


def _configured_worker_runtime() -> str:
    """Resolve the portfolio Worker runtime without hiding explicit config."""

    configured = os.getenv("PORTFOLIO_WORKER_RUNTIME", "").strip().lower()
    if configured:
        return configured
    if os.getenv("OLLAMA_BASE_URL") and os.getenv("OLLAMA_CHAT_MODEL"):
        return "ollama"
    return "deterministic_test"


def build_ceo_task_plan(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Create a bounded department plan from a free-form user request.

    The request remains available to the workers. This deterministic first pass
    is a safety guard: it limits which department may be called, while the CEO
    worker can explain and refine the plan. It never creates orders or changes
    financial state.
    """
    query = " ".join(str(profile.get("query", "")).split())
    category = str(profile.get("category", "")).strip().upper()
    category_departments = CATEGORY_DEPARTMENTS.get(category)
    # 알 수 없는 카테고리를 거절하지 않는다 - 표에 없으면 더 넓은 집합으로 떨어져
    # 자문 결과가 비는 대신 부서를 더 부른다(개발 원칙 9: 실패는 확대가 아니라
    # 차단 방향이나, 여기서 "확대"는 주문이 아니라 읽기 부서 호출이라 안전하다).
    # 다만 조용히 넘기지 않고 category_recognized 로 호출부·감사에 드러낸다.
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
    stages: set[str] = set(category_departments or ("research", "risk", "qa", "ceo"))
    keywords: dict[str, tuple[str, ...]] = {
        "trading": ("주문", "매수", "매도", "체결", "리밸런싱", "거래"),
        "accounting": ("세금", "수수료", "원장", "nav", "현금", "현금흐름", "대사", "회계"),
        "research": ("종목", "주식", "etf", "뉴스", "시장", "수익", "유니버스", "업종", "국내", "글로벌"),
        "risk": ("위험", "리스크", "손실", "변동", "헤지", "레버리지", "공매도", "보수적"),
        "qa": ("검증", "근거", "신뢰", "감사", "오류", "검토", "출처"),
    }
    matched_terms: dict[str, list[str]] = {}
    for stage, terms in keywords.items():
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

_MODULE_PATHS = {
    "research": ROOT / "departments/01-research/employee_workers.py",
    "quant": ROOT / "departments/04-quant-backtest/employee_workers.py",
    "trading": ROOT / "departments/02-trading/employee_workers.py",
    "risk": ROOT / "departments/03-risk/risk_employee_workers.py",
    "qa": ROOT / "departments/06-ai-qa-audit/qa_employee_workers.py",
    "accounting": ROOT / "departments/05-accounting-portfolio/employee_workers.py",
    "ceo": ROOT / "departments/00-ceo-office/employee_workers.py",
}

_MODULE_LOAD_LOCK = RLock()


def _load_module(stage: str) -> Any:
    name = f"portfolio_full_pipeline_{stage}_workers"
    with _MODULE_LOAD_LOCK:
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "WORKER_SPECS"):
            return module
        path = _MODULE_PATHS[stage]
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"worker module unavailable: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
        return module


def _deterministic_worker_llm(system: str, prompt: str) -> str:
    """TEST-only Worker response; production never uses this adapter."""

    worker_id = "worker"
    for line in prompt.splitlines():
        if line.startswith("Worker id:"):
            worker_id = line.split(":", 1)[1].strip()
            break
    department = "qa" if "AI-QA" in system or "QA" in system else "investment"
    return json.dumps(
        {
            "summary": f"TEST async {department} context for {worker_id}",
            "confidence": 0.75,
            "evidence_refs": ["research:portfolio-catalog:v1"],
            "escalate": False,
        }
    )


def _risk_tools(module: Any) -> Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    # core-risk-worker/derivatives-counterparty-worker는 risk-runner로 흡수됐고
    # module.WORKER_SPECS에 더 없다 - 이 dict는 남은 LLM Worker만 다룬다.
    return {
        "compliance-policy-worker": module._compliance_tool,
    }


def _qa_tools(module: Any) -> Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    # evidence-qa-worker/model-and-internal-audit-worker/ops-and-permission-worker는
    # qa-runner로 흡수됐고 module.WORKER_SPECS에 더 없다.
    return {
        "hallucination-critic-worker": module._hallucination_tool,
        "incident-postmortem-worker": module._incident_tool,
    }


def _specs(stage: str) -> tuple[WorkerSpec, ...]:
    return tuple(_load_module(stage).WORKER_SPECS)


def _query_selected_specs(
    stage: str,
    specs: Sequence[WorkerSpec],
    payload: Mapping[str, Any],
) -> tuple[WorkerSpec, ...]:
    task_plan = payload.get("task_plan")
    query = str(payload.get("user_query", "")).lower().strip()
    if not query or not isinstance(task_plan, Mapping) or task_plan.get("mode") != "free_query":
        return tuple(specs)

    matched = tuple(
        spec
        for spec in specs
        if any(term in query for term in _QUERY_WORKER_TERMS.get(spec.worker_id, ()))
    )
    if matched:
        return matched
    fallback_ids = set(_QUERY_WORKER_FALLBACKS.get(stage, ()))
    return tuple(spec for spec in specs if spec.worker_id in fallback_ids)


def _selected_specs(stage: str, payload: Mapping[str, Any]) -> tuple[WorkerSpec, ...]:
    task_plan = payload.get("task_plan")
    if isinstance(task_plan, Mapping):
        requested_departments = task_plan.get("requested_departments")
        if isinstance(requested_departments, Sequence) and stage not in requested_departments:
            return ()
    module = _load_module(stage)
    selector = getattr(module, "_should_run", None)
    if selector is None:
        selected = tuple(spec for spec in module.WORKER_SPECS if should_run(spec, payload))
    else:
        selected = tuple(spec for spec in module.WORKER_SPECS if selector(spec, dict(payload)))
    return _query_selected_specs(stage, selected, payload)


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _merge_dicts(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(left or {})
    merged.update(dict(right or {}))
    return merged


class PortfolioPipelineState(TypedDict, total=False):
    trace_id: str
    case_id: str
    as_of: str
    user_profile: dict[str, Any]
    task_plan: dict[str, Any]
    portfolio_candidates: list[dict[str, Any]]
    data_context: dict[str, Any]
    suitability: dict[str, Any]
    suitability_context: dict[str, Any]
    risk_gate: dict[str, Any]
    qa_gate: dict[str, Any]
    worker_reports: Annotated[list[dict[str, Any]], operator.add]
    department_reports: Annotated[dict[str, Any], _merge_dicts]
    pipeline_status: str
    safe_action: str
    manual_review_required: bool
    pipeline_events: Annotated[list[dict[str, Any]], operator.add]
    result: dict[str, Any]


def _load_suitability() -> Any:
    name = "portfolio_suitability_full_pipeline"
    if name in sys.modules:
        return sys.modules[name]
    path = ROOT / "departments/05-accounting-portfolio/portfolio/suitability.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"portfolio suitability contract unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_supabase_adapter() -> Any:
    name = "portfolio_supabase_readonly"
    if name in sys.modules:
        return sys.modules[name]
    path = ROOT / "departments/05-accounting-portfolio/portfolio/supabase_readonly.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Supabase read-only adapter unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _profile_context(profile: Mapping[str, Any]) -> dict[str, Any]:
    # User identity is not forwarded to employee Workers.
    return {key: value for key, value in profile.items() if key != "user_id"}


def _stage_payload(state: PortfolioPipelineState, stage: str) -> dict[str, Any]:
    suitability = state.get("suitability", {})
    data_context = state.get("data_context", {})
    research_context = data_context.get("research", {})
    market_context = data_context.get("market", {})
    accounting_context = data_context.get("accounting", {})
    live_source = data_context.get("source") == "SUPABASE"
    default_status = "LIVE" if live_source else "TEST"
    optional_inputs: dict[str, Any] = {
        "compliance": data_context.get("compliance", state.get("compliance", {})),
        "trading_state": data_context.get("trading_state", state.get("trading_state", {})),
        "counterparty": data_context.get("counterparty", state.get("counterparty", {})),
        "derivatives": data_context.get("derivatives", state.get("derivatives", {})),
        "assessment": state.get("assessment", state.get("risk_gate", {})),
        "hallucination_reviews": state.get("hallucination_reviews", []),
        "model_risk": state.get("model_risk", {}),
        "internal_audit": state.get("internal_audit", {}),
        "ops_assessment": state.get("ops_assessment", {}),
        "permission_check": state.get("permission_check", {}),
        "incident": state.get("incident", {}),
        "incident_events": state.get("incident_events", []),
    }
    if not live_source:
        optional_inputs.update(
            {
                "compliance": {
                    "grounded": True,
                    "evidence_refs": ["research:portfolio-catalog:v1"],
                },
                "trading_state": "ENABLED",
                "counterparty": {"status": "HEALTHY"},
                "derivatives": {"status": "NONE"},
                "assessment": {
                    "verdict": "approve",
                    "claim_checks": [
                        {"result": "SUPPORTED"},
                        {"result": "UNSUPPORTED", "claim_id": "portfolio-test-claim"},
                    ],
                },
                "hallucination_reviews": [
                    {"claim_id": "portfolio-test-claim", "result": "UNSUPPORTED"}
                ],
                "model_risk": {"status": "TESTING"},
                "internal_audit": {"status": "TESTING"},
                "ops_assessment": {"status": "HEALTHY", "breaches": []},
                "permission_check": {"result": "ALLOWED"},
                "incident": {"incident_id": "incident-portfolio-test", "severity": "SEV3"},
                "incident_events": [],
            }
        )
    context: dict[str, Any] = {
        "trace_id": state.get("trace_id", ""),
        "case_id": state.get("case_id", ""),
        "as_of": state.get("as_of", ""),
        "input_hash": _hash(
            {
                "trace_id": state.get("trace_id"),
                "suitability": suitability,
                "stage": stage,
            }
        ),
        "user_profile": _profile_context(state.get("user_profile", {})),
        "user_query": state.get("user_profile", {}).get("query", ""),
        "task_plan": state.get("task_plan", {}),
        "portfolio_suitability": state.get("suitability_context", {}),
        "portfolio_candidates": state.get("portfolio_candidates", []),
        "data_source": data_context.get("source", "TEST"),
        "worker_runtime": _configured_worker_runtime(),
        "data_quality": data_context.get("quality_status", "TEST"),
        "read_only": True,
        "external_writes": False,
        # Worker tools receive read models only. They never receive credentials
        # or a write-capable database handle.
        "universe": {"status": default_status},
        "market_snapshot": {
            **market_context,
            "status": market_context.get("status", default_status),
            "as_of": state.get("as_of", ""),
        },
        "market_features": {"status": default_status},
        "price_history": {"status": default_status},
        "fundamentals": {"status": default_status},
        "filings": {"status": default_status},
        "news": {"status": default_status},
        "macro": {"status": default_status},
        "geopolitical": {"status": default_status},
        "order_book": {"status": default_status},
        "evidence": {
            "status": research_context.get("status", default_status),
            "refs": research_context.get(
                "evidence_refs", ["research:portfolio-catalog:v1"]
            ),
            "documents": research_context.get("documents", []),
        },
        "evidence_request": {"query": "portfolio suitability evidence"},
        "documents": {
            "status": research_context.get("status", default_status),
            "items": research_context.get("documents", []),
        },
        "research_packet": {
            "status": "COMPLETED",
            "input_hash": state.get("suitability_context", {}).get("input_hash"),
        },
        "portfolio_snapshot": {
            **accounting_context,
            "status": accounting_context.get("status", "TEST"),
        },
        "strategy_bundle": {"status": "ADVISORY_ONLY", "production_promotion": False},
        "risk_decision": state.get("risk_gate", {"status": "PENDING", "binding": False}),
        "approved_risk": {"status": "ADVISORY_ONLY", "binding": False},
        "execution_request": {"status": "ADVISORY_ONLY", "binding": False},
        "derivatives_signal": {"status": "NONE"},
        "order_constraints": {"status": "NOT_APPLICABLE"},
        "order_intent": {"status": "ADVISORY_ONLY", "binding": False},
        "venue_costs": {"status": "TEST"},
        **optional_inputs,
        "nav_close": {"status": "TEST"},
        "open_breaks": [],
        "approval_state": "ADVISORY_ONLY",
        "treasury_signal": {"status": "NONE"},
        "cash": "0",
        "margin": "0",
        "collateral": "0",
        "pnl_request": {"status": "TEST"},
        "pnl_snapshot": {"status": "TEST"},
        "fills": [],
        "costs": {},
        "investor_report": {"status": "ADVISORY_ONLY"},
        "reporting_snapshot": {"status": "TEST"},
        "risk_snapshot": {"status": "TEST"},
        "corporate_action": {"status": "NONE"},
        "valuation": {"status": "TEST"},
        "fee_accrual": {"status": "TEST"},
        "tax": {"status": "TEST"},
        "department_reports": state.get("department_reports", {}),
        "accounting_snapshot": {"status": "TEST", "official": False},
        "qa_assessment": state.get("qa_gate", {"decision": "PENDING", "binding": False}),
    }
    context["stage"] = stage
    plan = state.get("task_plan", {})
    requested = plan.get("requested_departments", DEPARTMENTS) if isinstance(plan, Mapping) else DEPARTMENTS
    requested_stages = [item for item in DEPARTMENTS if item in requested]
    current_index = requested_stages.index(stage) if stage in requested_stages else 0
    previous_stage = requested_stages[current_index - 1] if current_index > 0 else "ceo"
    handoff = DepartmentHandoff(
        run_id=str(state.get("trace_id", "unknown-run")),
        trace_id=str(state.get("trace_id", "unknown-trace")),
        from_department=previous_stage,
        to_department=stage,
        from_role=f"{previous_stage}:head",
        to_role=f"{stage}:head",
        input_contract="mas.department-context.v1",
        output_contract="mas.department-context.v1",
        input_hash=stable_hash(context),
        purpose="비구속적 사용자 요청 분석 컨텍스트의 부서장 간 handoff",
        as_of=datetime.fromisoformat(str(state.get("as_of")))
        if state.get("as_of")
        else datetime.now(timezone.utc),
    )
    context["handoff"] = handoff.model_dump(mode="json")
    return context


def _worker_tools(stage: str, module: Any) -> Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    if stage == "risk":
        return _risk_tools(module)
    if stage == "qa":
        return _qa_tools(module)
    return tools_for_specs(tuple(module.WORKER_SPECS))


def _validate_worker_report(report: dict[str, Any]) -> dict[str, Any]:
    """Close the Worker boundary before its output reaches fan-in."""

    # Incident entries are QA-local Timeline persistence metadata. They are
    # intentionally not part of the shared worker-context.v1 contract, whose
    # additionalProperties guard must remain strict for every department.
    if (
        report.get("stage") == "qa"
        and report.get("worker_id") == "incident-postmortem-worker"
    ):
        output = dict(report.get("output", {}))
        # The read-only portfolio pipeline does not own IncidentTimeline
        # persistence. Keep the QA-only entries out of the public worker
        # report so strict BFF response schemas remain unchanged.
        output.pop("entries", None)
        report["output"] = output

    validation_output = dict(report.get("output", {}))
    # Risk compliance keeps route metadata next to its advisory answer for
    # Risk Head/Office traceability. Those fields are intentionally outside
    # the shared MAS worker-context.v1 contract and must not invalidate the
    # cross-department fan-in contract.
    if report.get("stage") == "risk" and report.get("worker_id") == "compliance-policy-worker":
        for field in ("query_mode", "routing_rationale", "routing_by_llm"):
            validation_output.pop(field, None)
    try:
        validate_worker_context(validation_output)
    except Exception as exc:  # noqa: BLE001 - contract boundary must fail closed.
        report["status"] = "DEGRADED"
        report["error"] = f"worker_context_contract_invalid:{type(exc).__name__}"
        report["contract_validation"] = {
            "status": "FAIL",
            "safe_action": "HOLD",
            "reason": str(exc)[:240],
        }
        output = dict(report.get("output", {}))
        output.update({"schema_valid": False, "escalate": True, "confidence": 0.0})
        report["output"] = output
    else:
        report["contract_validation"] = {"status": "PASS", "safe_action": "NO_ACTION"}
    return report


def _technology_profile(spec: WorkerSpec) -> dict[str, Any]:
    profile = getattr(spec, "tech_profile", None)
    return profile.as_dict() if profile is not None else {}


async def _invoke_worker(
    stage: str,
    spec: WorkerSpec,
    payload: Mapping[str, Any],
    *,
    event_callback: RuntimeEventCallback | None = None,
) -> dict[str, Any]:
    module = _load_module(stage)
    tools = _worker_tools(stage, module)
    tool = tools.get(spec.worker_id)
    if tool is None:
        return {
            "stage": stage,
            "worker_id": spec.worker_id,
            "status": "DEGRADED",
            "error": "tool_not_registered",
            "binding": False,
        }
    if event_callback:
        event_callback(
            {
                "kind": "worker_started",
                "stage": stage,
                "worker_id": spec.worker_id,
                "role": spec.role,
                "technology": _technology_profile(spec),
                "input_hash": payload.get("input_hash"),
                "output_contract": spec.output_contract,
            }
        )
    configured_runtime = _configured_worker_runtime()
    use_ollama = configured_runtime in {"ollama", "live"}
    worker_llm = default_worker_llm
    if str(payload.get("source", "TEST")).upper() == "TEST":
        # Let the operator projection observe a real running Worker graph in
        # local TEST mode; this does not create work or alter any result.
        await asyncio.sleep(float(os.getenv("PORTFOLIO_WORKER_UI_YIELD_SECONDS", "0.15")))
        worker_llm = (
            default_worker_llm
            if use_ollama
            else _deterministic_worker_llm
        )
    metric_token = begin_worker_metric(
        worker_id=spec.worker_id,
        role=spec.role,
        stage=stage,
        model_name=(os.getenv("OLLAMA_CHAT_MODEL") or "qwen3:1.7b")
        if worker_llm is default_worker_llm
        else "deterministic-test",
    )
    try:
        if stage in {"risk", "qa"}:
            trace = module.SkillTrace()
            app = module.build_worker_graph(
                spec,
                tool,
                worker_llm,
                trace=trace,
            )
        else:
            app = build_independent_worker_graph(spec, tool, worker_llm)
        # Required async LangGraph boundary: never replace this with invoke().
        state = await app.ainvoke({"worker_id": spec.worker_id, "input": dict(payload)})
        report = {
            "stage": stage,
            "worker_id": spec.worker_id,
            "role": spec.role,
            "technology": _technology_profile(spec),
            "status": state.get("status", "DEGRADED"),
            "attempts": state.get("attempts", 0),
            "output": state.get("output", {}),
            "error": state.get("error"),
            "output_contract": spec.output_contract,
            "input_hash": payload.get("input_hash"),
            "binding": False,
        }
        report = _validate_worker_report(report)
        performance = end_worker_metric(
            metric_token,
            status=report["status"],
            attempts=int(report.get("attempts", 0)),
            eval_score=1.0
            if report.get("contract_validation", {}).get("status") == "PASS"
            else 0.0,
        )
        report["performance"] = performance
        publish_metric(
            performance,
            trace_id=str(payload.get("trace_id") or payload.get("case_id") or ""),
        )
        if event_callback:
            event_callback(
                {
                    "kind": "worker_completed",
                    "stage": stage,
                    "worker_id": spec.worker_id,
                    "role": spec.role,
                    "status": report["status"],
                    "summary": report.get("output", {}).get("summary"),
                    "input_hash": report.get("input_hash"),
                    "output_contract": report.get("output_contract"),
                    "attempts": report.get("attempts", 0),
                    "performance": performance,
                }
            )
        return report
    except Exception as exc:  # noqa: BLE001 - cross-department boundary fails closed.
        report = {
            "stage": stage,
            "worker_id": spec.worker_id,
            "role": spec.role,
            "technology": _technology_profile(spec),
            "status": "DEGRADED",
            "attempts": 0,
            "output": {
                "summary": "Worker graph failed; downstream review is required.",
                "confidence": 0.0,
                "evidence_refs": [],
                "escalate": True,
                "schema_valid": False,
            },
            "error": type(exc).__name__,
            "output_contract": spec.output_contract,
            "input_hash": payload.get("input_hash"),
            "binding": False,
        }
        performance = end_worker_metric(metric_token, status="DEGRADED", attempts=0, eval_score=0.0)
        report["performance"] = performance
        publish_metric(
            performance,
            trace_id=str(payload.get("trace_id") or payload.get("case_id") or ""),
        )
        if event_callback:
            event_callback(
                {
                    "kind": "worker_completed",
                    "stage": stage,
                    "worker_id": spec.worker_id,
                    "role": spec.role,
                    "status": report["status"],
                    "summary": report.get("output", {}).get("summary"),
                    "input_hash": report.get("input_hash"),
                    "output_contract": report.get("output_contract"),
                    "attempts": report.get("attempts", 0),
                    "performance": performance,
                }
            )
        return report


def _initial_state(
    profile: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    data_context: Mapping[str, Any] | None = None,
) -> PortfolioPipelineState:
    now = str(profile.get("as_of", ""))
    seed_hash = _hash({"profile": profile, "candidates": candidates})
    trace_id = f"portfolio-test-{seed_hash[:24]}"
    return {
        "trace_id": trace_id,
        "case_id": f"case-{trace_id}",
        "as_of": now,
        "user_profile": dict(profile),
        "task_plan": build_task_plan(
            profile,
            deterministic_fallback=build_ceo_task_plan,
            valid_departments=DEPARTMENTS,
        ),
        "portfolio_candidates": [dict(item) for item in candidates],
        "data_context": dict(
            data_context
            or {
                "source": "TEST",
                "quality_status": "TEST",
                "read_only": True,
                "external_writes": False,
                "research": {"status": "TEST"},
                "market": {"status": "TEST"},
                "accounting": {"status": "TEST"},
            }
        ),
        "worker_reports": [],
        "department_reports": {},
        "manual_review_required": True,
    }


def build_portfolio_recommendation_graph(
    event_callback: RuntimeEventCallback | None = None,
) -> Any:
    """Compile the complete async LangGraph fan-out/fan-in pipeline."""

    graph = StateGraph(PortfolioPipelineState)

    def validate_profile(state: PortfolioPipelineState) -> dict[str, Any]:
        suitability = _load_suitability()
        if state["portfolio_candidates"]:
            result = suitability.recommend_portfolios(
                state["user_profile"],
                state["portfolio_candidates"],
            )
        else:
            normalized_profile = suitability.InvestorProfile.model_validate(
                state["user_profile"]
            )
            effective_risk_band = {
                1: suitability.PortfolioRiskBand.LOW,
                2: suitability.PortfolioRiskBand.MEDIUM,
                3: suitability.PortfolioRiskBand.HIGH,
            }[normalized_profile.effective_risk_score]
            result = suitability.SuitabilityResult(
                status=suitability.SuitabilityStatus.NO_MATCH,
                calculation_version=suitability.CALCULATION_VERSION,
                input_hash=_hash(
                    {"profile": state["user_profile"], "candidates": []}
                ),
                profile_user_id=normalized_profile.user_id,
                effective_risk_band=effective_risk_band,
                investment_amount=normalized_profile.investment_amount,
                currency=normalized_profile.currency,
                recommendations=[],
                exclusions=[],
                manual_review_required=True,
            )
        context = {
            "status": result.status.value,
            "calculation_version": result.calculation_version,
            "input_hash": result.input_hash,
            "effective_risk_band": result.effective_risk_band.value,
            "recommendation_ids": [item.portfolio_id for item in result.recommendations],
            "exclusion_ids": [item.portfolio_id for item in result.exclusions],
            "binding": False,
        }
        return {
            "suitability": result.model_dump(mode="json"),
            "suitability_context": context,
        }

    def upstream_contracts_safe(state: PortfolioPipelineState, stages: Sequence[str]) -> bool:
        """Require completed upstream fan-in before a gate can pass."""

        reports = state.get("department_reports", {})
        for stage in stages:
            department = reports.get(stage, {})
            if department.get("status") not in {"COMPLETED", "NOT_REQUESTED"}:
                return False
            if any(
                item.get("contract_validation", {}).get("status") == "FAIL"
                for item in state.get("worker_reports", [])
                if item.get("stage") == stage
            ):
                return False
        return True

    def risk_precheck(state: PortfolioPipelineState) -> dict[str, Any]:
        matched = state.get("suitability", {}).get("status") == "MATCHED"
        data_context = state.get("data_context", {})
        data_quality = data_context.get("quality_status", "TEST")
        live_data_blocked = (
            data_context.get("source") not in {None, "TEST"}
            and data_quality != "PASS"
        )
        upstream_safe = upstream_contracts_safe(state, ("research",))
        approved = matched and not live_data_blocked and upstream_safe
        return {
            "risk_gate": {
                "status": "COMPLETED",
                "verdict": "approve" if approved else "reject",
                "safe_action": "NO_ACTION" if approved else "HOLD",
                "reason": (
                "SUITABILITY_MATCHED"
                if approved
                else "UPSTREAM_WORKER_CONTRACT_FAILED"
                if not upstream_safe
                else "LIVE_DATA_QUALITY_BLOCKED"
                if matched and live_data_blocked
                else "NO_SUITABLE_PORTFOLIO"
                ),
                "data_quality": data_quality,
                "binding": False,
            }
        }

    def qa_precheck(state: PortfolioPipelineState) -> dict[str, Any]:
        recommendations = state.get("suitability", {}).get("recommendations", [])
        evidence_ok = bool(recommendations) and all(
            item.get("evidence_refs") for item in recommendations
        )
        # The TEST fixture deliberately carries one unsupported claim so the
        # hallucination worker is exercised without turning WARN into PASS.
        unsupported_claim_fixture = state.get("data_context", {}).get("source") != "SUPABASE"
        live_data_quality = state.get("data_context", {}).get("quality_status", "TEST")
        upstream_safe = upstream_contracts_safe(state, ("research", "risk"))
        decision = (
            "PASS"
            if evidence_ok
            and not unsupported_claim_fixture
            and live_data_quality == "PASS"
            and upstream_safe
            else "WARN"
        )
        return {
            "qa_gate": {
                "status": "COMPLETED",
                "decision": decision,
                "reason": (
                "UPSTREAM_WORKER_CONTRACT_FAILED"
                if not upstream_safe
                else "EVIDENCE_PRESENT_BUT_TEST_UNSUPPORTED_CLAIM"
                    if unsupported_claim_fixture and evidence_ok
                    else "LIVE_DATA_QUALITY_WARNING"
                    if live_data_quality != "PASS"
                    else "NO_MATCH_OR_EVIDENCE_GAP"
                ),
                "binding": False,
            }
        }

    def _live_worker_inputs_ready(state: PortfolioPipelineState) -> bool:
        """Allow live workers only when the canonical PIT packet is usable."""
        data_context = state.get("data_context", {})
        if data_context.get("source") == "TEST":
            return True
        market = data_context.get("market", {})
        return (
            data_context.get("source") == "SUPABASE"
            and data_context.get("quality_status") == "PASS"
            and bool(state.get("portfolio_candidates"))
            and bool(market.get("snapshots"))
        )

    def _live_input_block_message(state: PortfolioPipelineState) -> str:
        data_context = state.get("data_context", {})
        diagnostics = data_context.get("data_diagnostics", {}).get("pit_readiness", {})
        reasons = diagnostics.get("reasons") or data_context.get("reasons") or ["PIT_DATA_NOT_READY"]
        counts = (
            f"후보 {diagnostics.get('candidate_count', len(state.get('portfolio_candidates', [])))}개 · "
            f"연구 문서 {diagnostics.get('research_document_count', 0)}개 · "
            f"시장 스냅샷 {diagnostics.get('market_snapshot_count', 0)}개 · "
            f"국내 종목 {diagnostics.get('domestic_instrument_count', 0)}개"
        )
        return f"PIT 입력이 준비되지 않아 안전하게 실행을 차단했습니다. {counts} · 사유: {', '.join(map(str, reasons[:3]))}"


    def _safe_skip_reports(
        stage: str,
        specs: Sequence[WorkerSpec],
        input_hash: str | None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "stage": stage,
                "worker_id": spec.worker_id,
                "role": spec.role,
                "status": "SKIPPED_SAFE",
                "skip_reason": "LIVE_DATA_NOT_READY",
                "execution_reason": "LIVE_DATA_NOT_READY",
                "attempts": 0,
                "output": {
                    "worker_id": spec.worker_id,
                    "summary": "Live PIT inputs are not ready; worker execution was blocked.",
                    "confidence": 0.0,
                    "evidence_refs": [],
                    "escalate": True,
                    "schema_valid": False,
                },
                "error": "LIVE_DATA_NOT_READY",
                "output_contract": spec.output_contract,
                "input_hash": input_hash,
                "binding": False,
                "technology": _technology_profile(spec),
            }
            for spec in specs
        ]


    def _plan_skip_reports(
        stage: str,
        specs: Sequence[WorkerSpec],
        input_hash: str | None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "stage": stage,
                "worker_id": spec.worker_id,
                "role": spec.role,
                "status": "SKIPPED_SAFE",
                "skip_reason": "NOT_REQUESTED",
                "execution_reason": "NOT_REQUESTED",
                "attempts": 0,
                "output": {
                    "worker_id": spec.worker_id,
                    "summary": "CEO task plan did not select this department for the user request.",
                    "confidence": 1.0,
                    "evidence_refs": [],
                    "escalate": False,
                    "schema_valid": True,
                },
                "error": None,
                "output_contract": spec.output_contract,
                "input_hash": input_hash,
                "binding": False,
                "technology": _technology_profile(spec),
            }
            for spec in specs
        ]


    def route_stage(stage: str) -> Callable[[PortfolioPipelineState], list[Send]]:
        def route(state: PortfolioPipelineState) -> list[Send]:
            payload = _stage_payload(state, stage)
            task_plan = payload.get("task_plan", {})
            requested_departments = task_plan.get("requested_departments", DEPARTMENTS) if isinstance(task_plan, Mapping) else DEPARTMENTS
            if stage not in requested_departments:
                if event_callback:
                    event_callback({
                        "kind": "department_skipped",
                        "stage": stage,
                        "message": "CEO task plan에 따라 이 부서는 이번 요청에서 호출하지 않습니다.",
                    })
                return [
                    Send(
                        f"{stage}_fan_in",
                        {"worker_reports": _plan_skip_reports(stage, _specs(stage), payload.get("input_hash"))},
                    )
                ]
            if not _live_worker_inputs_ready(state):
                if event_callback:
                    event_callback({"kind": "department_blocked", "stage": stage, "message": _live_input_block_message(state)})
                return [
                    Send(
                        f"{stage}_fan_in",
                        {"worker_reports": _safe_skip_reports(stage, _specs(stage), payload.get("input_hash"))},
                    )
                ]
            selected = _selected_specs(stage, payload)
            if not selected:
                # 조건부 Worker가 전부 트리거되지 않은 것은 실패가 아니다 (예: risk-runner/
                # qa-runner 흡수 이후 risk/qa 는 조건부 LLM Worker만 남았고, 근거가 없으면
                # LLM 서술 없이 결정론 판정만으로 끝나는 게 정상이다). NOT_REQUESTED(CEO
                # task plan 제외)와는 다른 사유이므로 skip_reason을 구분해 DEGRADED로
                # 떨어뜨리지 않고 SKIPPED_SAFE로 안전하게 취급한다.
                if event_callback:
                    event_callback({"kind": "department_blocked", "stage": stage, "message": "조건에 맞는 Worker가 없어 안전하게 보류했습니다."})
                no_trigger_reports = [
                    {
                        "stage": stage,
                        "worker_id": spec.worker_id,
                        "role": spec.role,
                        "status": "SKIPPED_SAFE",
                        "skip_reason": "NO_TRIGGER_SIGNAL",
                        "execution_reason": "NO_TRIGGER_SIGNAL",
                        "attempts": 0,
                        "output": {
                            "worker_id": spec.worker_id,
                            "summary": "No conditional trigger signal was present; worker execution was not needed.",
                            "confidence": 1.0,
                            "evidence_refs": [],
                            "escalate": False,
                            "schema_valid": True,
                        },
                        "error": None,
                        "output_contract": spec.output_contract,
                        "input_hash": payload.get("input_hash"),
                        "binding": False,
                        "technology": _technology_profile(spec),
                    }
                    for spec in _specs(stage)
                ]
                if not no_trigger_reports:
                    no_trigger_reports = [{
                        "stage": stage,
                        "worker_id": "dynamic-alpha-strategy-worker",
                        "role": "Request-scoped Quant Alpha Strategy evaluator",
                        "status": "SKIPPED_SAFE",
                        "skip_reason": "NO_VALID_STRATEGY_BUNDLE",
                        "execution_reason": "NO_VALID_STRATEGY_BUNDLE",
                        "attempts": 0,
                        "output": {
                            "worker_id": "dynamic-alpha-strategy-worker",
                            "summary": "No validated Quant Alpha Strategy Bundle was supplied.",
                            "confidence": 1.0,
                            "evidence_refs": [],
                            "escalate": False,
                            "schema_valid": True,
                        },
                        "error": None,
                        "output_contract": "trading.temporary-strategy-worker.v1",
                        "input_hash": payload.get("input_hash"),
                        "binding": False,
                        "technology": {
                            "executor": "deterministic_strategy_worker",
                            "topology": "dynamic_parallel_fan_out_fan_in",
                            "provider": "none",
                            "model": "none",
                        },
                    }]
                return [
                    Send(
                        f"{stage}_fan_in",
                        {"worker_reports": no_trigger_reports},
                    )
                ]
            if event_callback:
                # Publish the validated head-to-head handoff together with the stage
                # start event.  The worker receives the same object in ``worker_input``;
                # exposing it here makes the runtime projection and replay auditable.
                event_callback(
                    {
                        "kind": "department_started",
                        "stage": stage,
                        "worker_ids": [spec.worker_id for spec in selected],
                        "handoff": payload.get("handoff"),
                    }
                )
            return [
                Send(
                    f"{stage}_worker",
                    {"stage": stage, "worker_id": spec.worker_id, "worker_input": payload},
                )
                for spec in selected
            ]

        return route

    def worker_node(_stage: str) -> Callable[[dict[str, Any]], Any]:
        async def run(state: dict[str, Any]) -> dict[str, Any]:
            stage = str(state["stage"])
            worker_id = str(state["worker_id"])
            payload = dict(state.get("worker_input", {}))
            spec = next(spec for spec in _specs(stage) if spec.worker_id == worker_id)
            report = await _invoke_worker(stage, spec, payload, event_callback=event_callback)
            return {"worker_reports": [report]}

        return run

    def fan_in_node(stage: str) -> Callable[[PortfolioPipelineState], dict[str, Any]]:
        def fan_in(state: PortfolioPipelineState) -> dict[str, Any]:
            reports = [item for item in state.get("worker_reports", []) if item.get("stage") == stage]
            failed = [
                item["worker_id"]
                for item in reports
                if item.get("status") not in {"COMPLETED", "SKIPPED_SAFE"}
            ]
            completed = [item for item in reports if item.get("status") == "COMPLETED"]
            skipped_safe = [item for item in reports if item.get("status") == "SKIPPED_SAFE"]
            not_requested = [
                item for item in reports if item.get("skip_reason") == "NOT_REQUESTED"
            ]
            skip_reasons: dict[str, int] = {}
            for item in reports:
                reason = item.get("skip_reason")
                if reason:
                    skip_reasons[str(reason)] = skip_reasons.get(str(reason), 0) + 1
            all_not_requested = bool(reports) and len(not_requested) == len(reports)
            all_live_blocked = bool(reports) and all(
                item.get("skip_reason") == "LIVE_DATA_NOT_READY" for item in reports
            )
            all_no_valid_strategy = bool(reports) and all(
                item.get("skip_reason") == "NO_VALID_STRATEGY_BUNDLE" for item in reports
            )
            if all_not_requested:
                department_status = "NOT_REQUESTED"
            elif all_live_blocked:
                department_status = "BLOCKED"
            elif all_no_valid_strategy:
                department_status = "NOT_APPLICABLE"
            elif failed:
                department_status = "DEGRADED"
            else:
                department_status = "COMPLETED"
            result: dict[str, Any] = {
                "department_reports": {
                    stage: {
                        "status": department_status,
                        "legacy_status": "SKIPPED" if department_status in {"NOT_REQUESTED", "BLOCKED", "NOT_APPLICABLE"} else department_status,
                        "worker_ids": [item["worker_id"] for item in reports],
                        "executed": len(completed),
                        "completed": len(completed),
                        "skipped_safe": len(skipped_safe),
                        "not_requested": len(not_requested),
                        "failed_count": len(failed),
                        "skip_reasons": skip_reasons,
                        "skip_reason": next(iter(skip_reasons), None),
                        "failed": failed,
                        "binding": False,
                        "fan_out": True,
                        "fan_in": True,
                    }
                }
            }
            if skipped_safe or not_requested:
                result["worker_reports"] = reports
            if event_callback:
                event_callback(
                    {
                        "kind": "department_completed",
                        "stage": stage,
                        "status": result["department_reports"][stage]["status"],
                            "message": (
                                "CEO task plan에 따라 이번 요청에서 호출하지 않았습니다."
                    if result["department_reports"][stage]["status"] in {"NOT_REQUESTED", "BLOCKED"}
                                else f"{stage} 부서가 {result['department_reports'][stage]['executed']}개 Worker 결과를 취합했습니다."
                            ),
                    }
                )
            return result

        return fan_in

    def finalize(state: PortfolioPipelineState) -> dict[str, Any]:
        reports = state.get("department_reports", {})
        degraded = [
            stage
            for stage, report in reports.items()
        if report.get("status") not in {"COMPLETED", "NOT_REQUESTED", "NOT_APPLICABLE"}
        ]
        matched = state.get("suitability", {}).get("status") == "MATCHED"
        data_context = state.get("data_context", {})
        live_data_blocked = (
            data_context.get("source") not in {None, "TEST"}
            and data_context.get("quality_status") != "PASS"
        )
        risk_gate = state.get("risk_gate", {})
        qa_gate = state.get("qa_gate", {})
        # A completed precheck is not necessarily an approval.  Keep the
        # final action fail-closed when an upstream contract or risk gate
        # rejects the case; otherwise a rejected gate could still surface as
        # ``NO_ACTION`` merely because its department node completed.
        risk_safe = risk_gate.get("safe_action") == "NO_ACTION"
        qa_safe = qa_gate.get("decision") not in {"FAIL", "REJECT", "ESCALATE"}
        gate_degraded = any(
            gate.get("reason") == "UPSTREAM_WORKER_CONTRACT_FAILED"
            for gate in (risk_gate, qa_gate)
        )
        pipeline_status = (
            "DEGRADED"
            if degraded or live_data_blocked or gate_degraded
            else "COMPLETED"
        )
        safe_action = (
            "NO_ACTION"
            if matched
            and not degraded
            and not live_data_blocked
            and risk_safe
            and qa_safe
            else "HOLD"
        )
        result = {
            "workflow": "portfolio-recommendation-full",
            "pipeline_version": PIPELINE_VERSION,
            "pipeline_status": pipeline_status,
            "safe_action": safe_action,
            "production_enabled": False,
            "external_writes": False,
            "binding": False,
            "trace_id": state.get("trace_id"),
            "case_id": state.get("case_id"),
            "user_query": state.get("user_profile", {}).get("query", ""),
            "task_plan": state.get("task_plan", {}),
            "suitability": state.get("suitability", {}),
            "data_context": data_context,
            "risk_gate": state.get("risk_gate", {}),
            "qa_gate": state.get("qa_gate", {}),
            "department_reports": reports,
            "degraded_departments": degraded,
            "worker_reports": state.get("worker_reports", []),
            "manual_review_required": True,
        }
        return {
            "pipeline_status": pipeline_status,
            "safe_action": safe_action,
            "result": result,
        }

    graph.add_node("validate_profile", validate_profile)
    graph.add_node("risk_precheck", risk_precheck)
    graph.add_node("qa_precheck", qa_precheck)
    graph.add_node("finalize", finalize)

    for stage in DEPARTMENTS:
        graph.add_node(f"{stage}_worker", worker_node(stage))
        graph.add_node(f"{stage}_fan_in", fan_in_node(stage))

    graph.add_edge(START, "validate_profile")

    # Explicit fan-out/fan-in barriers preserve the department sequence.
    graph.add_node("research_fanout", lambda state: {})
    graph.add_node("quant_fanout", lambda state: {})
    graph.add_node("trading_fanout", lambda state: {})
    graph.add_node("risk_fanout", lambda state: {})
    graph.add_node("qa_fanout", lambda state: {})
    graph.add_node("accounting_fanout", lambda state: {})
    graph.add_node("ceo_fanout", lambda state: {})

    graph.add_edge("validate_profile", "research_fanout")
    graph.add_conditional_edges("research_fanout", route_stage("research"))
    graph.add_edge("research_worker", "research_fan_in")
    # quant 는 research 바로 다음이다 - 백테스트 입력(가격·유니버스·피처)이 리서치
    # 산출물이고, 그 결과가 trading/risk/qa 판단의 근거가 되어야 하기 때문이다.
    graph.add_edge("research_fan_in", "quant_fanout")
    graph.add_conditional_edges("quant_fanout", route_stage("quant"))
    graph.add_edge("quant_worker", "quant_fan_in")
    graph.add_edge("quant_fan_in", "trading_fanout")
    graph.add_conditional_edges("trading_fanout", route_stage("trading"))
    graph.add_edge("trading_worker", "trading_fan_in")
    graph.add_edge("trading_fan_in", "risk_precheck")
    graph.add_edge("risk_precheck", "risk_fanout")
    graph.add_conditional_edges("risk_fanout", route_stage("risk"))
    graph.add_edge("risk_worker", "risk_fan_in")
    graph.add_edge("risk_fan_in", "qa_precheck")
    graph.add_edge("qa_precheck", "qa_fanout")
    graph.add_conditional_edges("qa_fanout", route_stage("qa"))
    graph.add_edge("qa_worker", "qa_fan_in")
    graph.add_edge("qa_fan_in", "accounting_fanout")
    graph.add_conditional_edges("accounting_fanout", route_stage("accounting"))
    graph.add_edge("accounting_worker", "accounting_fan_in")
    graph.add_edge("accounting_fan_in", "ceo_fanout")
    graph.add_conditional_edges("ceo_fanout", route_stage("ceo"))
    graph.add_edge("ceo_worker", "ceo_fan_in")
    graph.add_edge("ceo_fan_in", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


async def run_portfolio_recommendation_pipeline_async(
    profile: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]] | None = None,
    *,
    data_adapter: Any | None = None,
    event_callback: RuntimeEventCallback | None = None,
) -> dict[str, Any]:
    """Run complete async graph with TEST or Supabase read-only inputs.

    Passing candidates explicitly keeps deterministic TEST callers stable. If
    omitted, the adapter loads canonical PIT data from Supabase and an adapter
    failure remains a degraded, HOLD result.
    """

    data_context: Mapping[str, Any] | None = None
    if candidates is None:
        adapter_module = _load_supabase_adapter()
        adapter = data_adapter or adapter_module.SupabaseReadOnlyAdapter()
        snapshot = await adapter.load_snapshot(
            as_of=profile.get("as_of", ""),
            fund_id=profile.get("fund_id"),
        )
        candidates = snapshot.candidates
        data_context = snapshot.as_pipeline_context()

    pipeline_events: list[dict[str, Any]] = []
    event_run_id = stable_hash({"profile": profile, "candidates": candidates})[:24]

    def emit(event: Mapping[str, Any]) -> None:
        normalized = make_pipeline_event(
            event_id=f"{event_run_id}:{len(pipeline_events) + 1:05d}",
            run_id=event_run_id,
            event=event,
        )
        pipeline_events.append(normalized.model_dump(mode="json"))
        if event_callback:
            forwarded = dict(event)
            forwarded["pipeline_event"] = normalized.model_dump(mode="json")
            event_callback(forwarded)

    emit(
        {
            "kind": "pipeline_started",
            "stage": "portfolio-recommendation",
            "department": "orchestration",
            "status": "RUNNING",
            "summary": "Portfolio recommendation pipeline started.",
        }
    )
    app = build_portfolio_recommendation_graph(event_callback=emit)
    with redacted_trace(
        trace_id=event_run_id,
        model_name=os.getenv("OLLAMA_CHAT_MODEL") or "qwen3:1.7b",
        stage="portfolio-recommendation",
    ):
        state = await app.ainvoke(
            _initial_state(profile, candidates, data_context),
            config={
                "run_name": "portfolio-recommendation-full",
                "tags": [
                    "hgfinance",
                    "portfolio-recommendation",
                    "redacted",
                    f"trace_id:{event_run_id}",
                ],
                "metadata": {
                    "observability_schema": "llm.performance.v1",
                    "raw_payloads_sent": False,
                },
            },
        )
    result = dict(state.get("result", state))
    emit(
        {
            "kind": "pipeline_completed",
            "stage": "portfolio-recommendation",
            "department": "orchestration",
            "status": str(result.get("pipeline_status", "DEGRADED")),
            "safe_action": str(result.get("safe_action", "HOLD")),
            "summary": "Portfolio recommendation pipeline completed.",
        }
    )
    result["pipeline_events"] = pipeline_events
    result["pipeline_event_count"] = len(pipeline_events)
    result["replay"] = build_replay_metadata(profile, candidates, result).model_dump(mode="json")
    return result


__all__ = [
    "PIPELINE_VERSION",
    "PortfolioPipelineState",
    "build_portfolio_recommendation_graph",
    "run_portfolio_recommendation_pipeline_async",
]
