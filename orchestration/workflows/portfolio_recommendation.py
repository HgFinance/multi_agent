"""Async TEST/Supabase-read-only pipeline for user suitability and all investment departments.

The graph connects Research -> Trading -> Risk -> QA -> Accounting -> CEO.
Each department stage uses LangGraph ``Send`` fan-out to independent Worker
graphs and a reducer-backed fan-in node. No order, ledger, credential, or
No order, ledger, credential, or production side effect is permitted.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import operator
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
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

ROOT = Path(__file__).resolve().parents[2]
PIPELINE_VERSION = "portfolio-recommendation-full-async-v1"

DEPARTMENTS: tuple[str, ...] = (
    "research",
    "trading",
    "risk",
    "qa",
    "accounting",
    "ceo",
)

_MODULE_PATHS = {
    "research": ROOT / "departments/01-research/employee_workers.py",
    "trading": ROOT / "departments/02-trading/employee_workers.py",
    "risk": ROOT / "departments/03-risk/risk_employee_workers.py",
    "qa": ROOT / "departments/06-ai-qa-audit/qa_employee_workers.py",
    "accounting": ROOT / "departments/05-accounting-portfolio/employee_workers.py",
    "ceo": ROOT / "departments/00-ceo-office/employee_workers.py",
}


def _load_module(stage: str) -> Any:
    name = f"portfolio_full_pipeline_{stage}_workers"
    if name in sys.modules:
        return sys.modules[name]
    path = _MODULE_PATHS[stage]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"worker module unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
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
    return {
        "market-liquidity-worker": module._market_tool,
        "pre-trade-risk-worker": module._pre_trade_tool,
        "compliance-policy-worker": module._compliance_tool,
        "derivatives-counterparty-worker": module._counterparty_tool,
    }


def _qa_tools(module: Any) -> Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    return {
        "evidence-qa-worker": module._evidence_tool,
        "hallucination-critic-worker": module._hallucination_tool,
        "model-and-internal-audit-worker": module._audit_tool,
        "ops-and-permission-worker": module._ops_tool,
        "incident-postmortem-worker": module._incident_tool,
    }


def _specs(stage: str) -> tuple[WorkerSpec, ...]:
    return tuple(_load_module(stage).WORKER_SPECS)


def _selected_specs(stage: str, payload: Mapping[str, Any]) -> tuple[WorkerSpec, ...]:
    module = _load_module(stage)
    selector = getattr(module, "_should_run", None)
    if selector is None:
        return tuple(spec for spec in module.WORKER_SPECS if should_run(spec, payload))
    return tuple(spec for spec in module.WORKER_SPECS if selector(spec, dict(payload)))


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
        "portfolio_suitability": state.get("suitability_context", {}),
        "portfolio_candidates": state.get("portfolio_candidates", []),
        "data_source": data_context.get("source", "TEST"),
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
        "compliance": {"grounded": True, "evidence_refs": ["research:portfolio-catalog:v1"]},
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
    return context


def _worker_tools(stage: str, module: Any) -> Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    if stage == "risk":
        return _risk_tools(module)
    if stage == "qa":
        return _qa_tools(module)
    return tools_for_specs(tuple(module.WORKER_SPECS))


async def _invoke_worker(stage: str, spec: WorkerSpec, payload: Mapping[str, Any]) -> dict[str, Any]:
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
    worker_llm = (
        _deterministic_worker_llm
        if str(payload.get("source", "TEST")).upper() == "TEST"
        else default_worker_llm
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
        return {
            "stage": stage,
            "worker_id": spec.worker_id,
            "role": spec.role,
            "status": state.get("status", "DEGRADED"),
            "attempts": state.get("attempts", 0),
            "output": state.get("output", {}),
            "error": state.get("error"),
            "output_contract": spec.output_contract,
            "input_hash": payload.get("input_hash"),
            "binding": False,
        }
    except Exception as exc:  # noqa: BLE001 - cross-department boundary fails closed.
        return {
            "stage": stage,
            "worker_id": spec.worker_id,
            "role": spec.role,
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


def build_portfolio_recommendation_graph() -> Any:
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

    def risk_precheck(state: PortfolioPipelineState) -> dict[str, Any]:
        matched = state.get("suitability", {}).get("status") == "MATCHED"
        data_context = state.get("data_context", {})
        data_quality = data_context.get("quality_status", "TEST")
        live_data_blocked = (
            data_context.get("source") not in {None, "TEST"}
            and data_quality != "PASS"
        )
        approved = matched and not live_data_blocked
        return {
            "risk_gate": {
                "status": "COMPLETED",
                "verdict": "approve" if approved else "reject",
                "safe_action": "NO_ACTION" if approved else "HOLD",
                "reason": (
                    "SUITABILITY_MATCHED"
                    if approved
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
        decision = (
            "PASS"
            if evidence_ok and not unsupported_claim_fixture and live_data_quality == "PASS"
            else "WARN"
        )
        return {
            "qa_gate": {
                "status": "COMPLETED",
                "decision": decision,
                "reason": (
                    "EVIDENCE_PRESENT_BUT_TEST_UNSUPPORTED_CLAIM"
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
            }
            for spec in specs
        ]


    def route_stage(stage: str) -> Callable[[PortfolioPipelineState], list[Send]]:
        def route(state: PortfolioPipelineState) -> list[Send]:
            payload = _stage_payload(state, stage)
            if not _live_worker_inputs_ready(state):
                return [
                    Send(
                        f"{stage}_fan_in",
                        {
                            "worker_reports": _safe_skip_reports(
                                stage,
                                _specs(stage),
                                payload.get("input_hash"),
                            ),
                        },
                    )
                ]
            selected = _selected_specs(stage, payload)
            if not selected:
                return [
                    Send(
                        f"{stage}_fan_in",
                        {
                            "worker_reports": [
                                {
                                    "stage": stage,
                                    "worker_id": f"{stage}-no-worker",
                                    "status": "DEGRADED",
                                    "error": "NO_ELIGIBLE_WORKER",
                                    "binding": False,
                                }
                            ]
                        },
                    )
                ]
            return [
                Send(
                    f"{stage}_worker",
                    {
                        "stage": stage,
                        "worker_id": spec.worker_id,
                        "worker_input": payload,
                    },
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
            report = await _invoke_worker(stage, spec, payload)
            return {"worker_reports": [report]}

        return run

    def fan_in_node(stage: str) -> Callable[[PortfolioPipelineState], dict[str, Any]]:
        def fan_in(state: PortfolioPipelineState) -> dict[str, Any]:
            reports = [item for item in state.get("worker_reports", []) if item.get("stage") == stage]
            failed = [item["worker_id"] for item in reports if item.get("status") != "COMPLETED"]
            result: dict[str, Any] = {
                "department_reports": {
                    stage: {
                        "status": "DEGRADED" if failed else "COMPLETED",
                        "worker_ids": [item["worker_id"] for item in reports],
                        "executed": len(reports),
                        "failed": failed,
                        "binding": False,
                        "fan_out": True,
                        "fan_in": True,
                    }
                }
            }
            if any(item.get("status") == "SKIPPED_SAFE" for item in reports):
                result["worker_reports"] = reports
            return result

        return fan_in

    def finalize(state: PortfolioPipelineState) -> dict[str, Any]:
        reports = state.get("department_reports", {})
        degraded = [
            stage for stage, report in reports.items() if report.get("status") != "COMPLETED"
        ]
        matched = state.get("suitability", {}).get("status") == "MATCHED"
        data_context = state.get("data_context", {})
        live_data_blocked = (
            data_context.get("source") not in {None, "TEST"}
            and data_context.get("quality_status") != "PASS"
        )
        pipeline_status = "DEGRADED" if degraded or live_data_blocked else "COMPLETED"
        safe_action = "NO_ACTION" if matched and not degraded and not live_data_blocked else "HOLD"
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
    graph.add_node("trading_fanout", lambda state: {})
    graph.add_node("risk_fanout", lambda state: {})
    graph.add_node("qa_fanout", lambda state: {})
    graph.add_node("accounting_fanout", lambda state: {})
    graph.add_node("ceo_fanout", lambda state: {})

    graph.add_edge("validate_profile", "research_fanout")
    graph.add_conditional_edges("research_fanout", route_stage("research"))
    graph.add_edge("research_worker", "research_fan_in")
    graph.add_edge("research_fan_in", "trading_fanout")
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

    app = build_portfolio_recommendation_graph()
    state = await app.ainvoke(_initial_state(profile, candidates, data_context))
    return dict(state.get("result", state))


__all__ = [
    "PIPELINE_VERSION",
    "PortfolioPipelineState",
    "build_portfolio_recommendation_graph",
    "run_portfolio_recommendation_pipeline_async",
]
