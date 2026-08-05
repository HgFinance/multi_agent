"""Risk department employee LangGraph workers.

The Hermes profile is the department-head boundary.  This module owns only the
employee layer: each worker is an independently compiled LangGraph that reads
an allow-listed deterministic tool result and asks the local Ollama model for
bounded, non-binding context.  It cannot approve an order or change a gate.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph


def _load_skill_package() -> None:
    package_name = "risk_worker_skill_runtime"
    if package_name in sys.modules:
        return
    package_dir = Path(__file__).with_name("skills")
    spec = importlib.util.spec_from_file_location(
        package_name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("Risk skill package is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)


_load_skill_package()

from risk_worker_skill_runtime.contracts import RiskSkillContext, make_result
from risk_worker_skill_runtime.guards import build_context, scope_check
from risk_worker_skill_runtime.rag_router import choose_rag_route
from risk_worker_skill_runtime.tools import invoke_tool
from risk_worker_skill_runtime.trace import SkillTrace

WorkerLLM = Callable[[str, str], str]
WorkerTool = Callable[[dict[str, Any]], dict[str, Any]]


class WorkerState(TypedDict, total=False):
    worker_id: str
    input: dict[str, Any]
    tool_output: dict[str, Any]
    output: dict[str, Any]
    status: str
    attempts: int
    error: str | None
    skill_context: dict[str, Any]
    skill_results: list[dict[str, Any]]
    rag_plan: dict[str, Any]
    trace_manifest: dict[str, Any]


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: str
    role: str
    tools: tuple[str, ...]
    trigger: str
    output_contract: str = "risk.worker-context.v1"
    max_attempts: int = 3
    skill_ids: tuple[str, ...] = ()


_RISK_GUARDS = (
    "guard.input_normalize.v1",
    "guard.scope_check.v1",
    "guard.secret_redaction.v1",
)
_RISK_TRACE = (
    "audit.trace_record.v1",
    "audit.replay_manifest.v1",
    "audit.cost_latency.v1",
)


WORKER_SPECS: tuple[WorkerSpec, ...] = (
    WorkerSpec(
        "market-liquidity-worker",
        "Market and liquidity risk analyst",
        ("risk.trading_state.read", "risk.p1.snapshot"),
        "always",
        skill_ids=_RISK_GUARDS
        + (
            "context.internal_api.v1",
            "context.repository_read.v1",
            "context.cache_read.v1",
            "guard.pit_filter.v1",
            "calc.deterministic_gate.v1",
            "verify.schema.v1",
            "advisory.grounded_summary.v1",
        )
        + _RISK_TRACE
        + ("fallback.human_escalation.v1",),
    ),
    WorkerSpec(
        "pre-trade-risk-worker",
        "Pre-trade deterministic gate analyst",
        ("risk.case.check",),
        "always",
        skill_ids=_RISK_GUARDS
        + (
            "context.internal_api.v1",
            "calc.deterministic_gate.v1",
            "verify.schema.v1",
            "advisory.grounded_summary.v1",
        )
        + _RISK_TRACE
        + ("fallback.retry_budget.v1", "fallback.human_escalation.v1"),
    ),
    WorkerSpec(
        "compliance-policy-worker",
        "Point-in-time policy evidence analyst",
        ("risk.compliance.check",),
        "when_compliance_evidence_exists",
        skill_ids=_RISK_GUARDS
        + (
            "guard.pit_filter.v1",
            "guard.prompt_injection_scan.v1",
            "context.repository_read.v1",
            "context.cache_read.v1",
            "rag.route.v1",
            "rag.hybrid_retrieve.v1",
            "rag.rerank.v1",
            "rag.decompose.v1",
            "rag.context_stitch.v1",
            "rag.self_check.v1",
            "verify.schema.v1",
            "verify.citation.v1",
            "verify.provenance_chain.v1",
            "verify.numeric_temporal.v1",
            "advisory.grounded_summary.v1",
        )
        + _RISK_TRACE
        + ("fallback.retry_budget.v1", "fallback.human_escalation.v1"),
    ),
    WorkerSpec(
        "derivatives-counterparty-worker",
        "Derivatives and counterparty exposure analyst",
        ("risk.trading_state.record.read",),
        "when_counterparty_or_derivatives_signal_exists",
        skill_ids=_RISK_GUARDS
        + (
            "context.internal_api.v1",
            "context.repository_read.v1",
            "context.cache_read.v1",
            "guard.pit_filter.v1",
            "calc.deterministic_gate.v1",
            "verify.schema.v1",
            "verify.provenance_chain.v1",
            "advisory.grounded_summary.v1",
        )
        + _RISK_TRACE
        + ("fallback.human_escalation.v1",),
    ),
)


def _model_name() -> str:
    return os.getenv("OLLAMA_CHAT_MODEL") or "qwen3:1.7b"


def _base_url() -> str:
    raw = (os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434/v1").rstrip("/")
    return raw if raw.endswith("/v1") else f"{raw}/v1"


def default_worker_llm(system: str, prompt: str) -> str:
    """Call the local Ollama OpenAI-compatible endpoint lazily."""

    from openai import OpenAI

    client = OpenAI(
        base_url=_base_url(),
        api_key=os.getenv("OLLAMA_API_KEY", "ollama"),
        timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "8")),
    )
    response = client.chat.completions.create(
        model=_model_name(),
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content or ""


def _compact(value: Any, limit: int = 9000) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return encoded[:limit]


def _parse_worker_output(raw: str, worker_id: str) -> tuple[dict[str, Any], bool]:
    text = (raw or "").strip()
    candidate = text
    if "```" in candidate:
        candidate = candidate.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        # A prose response is retained as non-binding context, but is marked
        # degraded so a schema failure is never mistaken for a clean run.
        return {
            "worker_id": worker_id,
            "summary": text[:4000],
            "confidence": None,
            "evidence_refs": [],
            "escalate": True,
            "schema_valid": False,
        }, False
    if not isinstance(parsed, dict) or not isinstance(parsed.get("summary"), str):
        return {
            "worker_id": worker_id,
            "summary": "",
            "schema_valid": False,
            "escalate": True,
        }, False
    evidence_refs = parsed.get("evidence_refs", [])
    valid = isinstance(evidence_refs, list) and isinstance(
        parsed.get("escalate", False), bool
    )
    output = {
        "worker_id": worker_id,
        "summary": parsed["summary"][:4000],
        "confidence": parsed.get("confidence"),
        "evidence_refs": evidence_refs,
        "escalate": parsed.get("escalate", True),
        "schema_valid": valid,
    }
    return output, valid


def build_worker_graph(
    spec: WorkerSpec,
    tool: WorkerTool,
    llm: WorkerLLM | None = None,
    trace: SkillTrace | None = None,
):
    """Build a guarded Risk Worker graph with explicit Skill evidence."""

    def _context(state: WorkerState) -> RiskSkillContext | None:
        raw = state.get("skill_context")
        if not raw:
            return None
        try:
            return RiskSkillContext.model_validate(raw)
        except Exception:  # noqa: BLE001 - invalid trace context is non-fatal.
            return None

    def _manifest(state: WorkerState) -> dict[str, Any]:
        context = _context(state)
        return (
            trace.manifest(context) if trace is not None and context is not None else {}
        )

    def _safe_output(error: str) -> dict[str, Any]:
        return {
            "worker_id": spec.worker_id,
            "summary": "Risk Worker evidence boundary failed; human review is required.",
            "confidence": 0.0,
            "evidence_refs": [],
            "escalate": True,
            "schema_valid": False,
            "error": error,
        }

    def read_tool(state: WorkerState) -> dict[str, Any]:
        payload = state.get("input", {})
        try:
            context = build_context(
                payload,
                worker_id=spec.worker_id,
                profile_version=spec.output_contract,
                allowed_scopes=spec.tools,
            )
            skill_results: list[dict[str, Any]] = []
            rag_plan = choose_rag_route(payload, worker_id=spec.worker_id).as_dict()
            rag_result = make_result("rag.route.v1", "COMPLETED", rag_plan)
            skill_results.append(rag_result.model_dump(mode="json"))
            if trace is not None:
                trace.record(context, rag_result)
            for requested_tool in spec.tools:
                checked = scope_check(context, requested_tool)
                skill_results.append(checked.model_dump(mode="json"))
                if trace is not None:
                    trace.record(context, checked)
                if checked.status != "COMPLETED":
                    fallback = make_result(
                        "fallback.human_escalation.v1",
                        "ESCALATE",
                        {"reason": checked.error_code or "SCOPE_DENIED"},
                        error_code=checked.error_code or "SCOPE_DENIED",
                        escalate=True,
                    )
                    skill_results.append(fallback.model_dump(mode="json"))
                    if trace is not None:
                        trace.record(context, fallback)
                    return {
                        "skill_context": context.model_dump(mode="json"),
                        "skill_results": skill_results,
                        "rag_plan": rag_plan,
                        "tool_output": {},
                        "status": "DEGRADED",
                        "attempts": 0,
                        "error": checked.error_code or "SCOPE_DENIED",
                    }

            invocation = invoke_tool(
                tool,
                payload,
                context,
                tool_name=spec.tools,
            )
            skill_results.append(invocation.result.model_dump(mode="json"))
            if trace is not None:
                trace.record(
                    context, invocation.result, latency_ms=invocation.latency_ms
                )
            if invocation.result.status != "COMPLETED":
                fallback = make_result(
                    "fallback.human_escalation.v1",
                    "ESCALATE",
                    {"reason": invocation.result.error_code or "TOOL_FAILED"},
                    error_code=invocation.result.error_code or "TOOL_FAILED",
                    escalate=True,
                )
                skill_results.append(fallback.model_dump(mode="json"))
                if trace is not None:
                    trace.record(context, fallback)
                return {
                    "skill_context": context.model_dump(mode="json"),
                    "skill_results": skill_results,
                    "rag_plan": rag_plan,
                    "tool_output": {},
                    "status": "DEGRADED",
                    "attempts": 0,
                    "error": invocation.result.error_code or "TOOL_FAILED",
                }
            return {
                "skill_context": context.model_dump(mode="json"),
                "skill_results": skill_results,
                "rag_plan": rag_plan,
                "tool_output": invocation.result.output,
                "status": "COMPLETED",
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - worker boundary fails closed.
            return {
                "tool_output": {},
                "status": "DEGRADED",
                "attempts": 0,
                "error": type(exc).__name__,
            }

    def call_llm(state: WorkerState) -> dict[str, Any]:
        if state.get("status") != "COMPLETED":
            return {
                "output": _safe_output(state.get("error", "SKILL_BOUNDARY_FAILED")),
                "status": "DEGRADED",
                "attempts": 0,
                "error": state.get("error", "SKILL_BOUNDARY_FAILED"),
                "trace_manifest": _manifest(state),
            }
        worker_llm = llm or default_worker_llm
        system = (
            f"You are the {spec.role}. You are a Risk employee, not Hermes supervisor. "
            "Use only supplied tool evidence. Never approve, resize, reject, submit an order, "
            "change a limit, or write a ledger. Return JSON with summary, confidence, "
            "evidence_refs, and escalate."
        )
        prompt = (
            f"Worker: {spec.worker_id}\n"
            f"Allowed tools: {', '.join(spec.tools)}\n"
            f"Required skills: {', '.join(spec.skill_ids)}\n"
            f"Output contract: {spec.output_contract}\n"
            f"Evidence:\n{_compact(state.get('tool_output', {}))}"
        )
        errors: list[str] = []
        for attempt in range(1, spec.max_attempts + 1):
            try:
                output, schema_valid = _parse_worker_output(
                    worker_llm(system, prompt), spec.worker_id
                )
                if schema_valid:
                    context = _context(state)
                    skill_results = list(state.get("skill_results", []))
                    advisory = make_result(
                        "advisory.grounded_summary.v1",
                        "COMPLETED",
                        output,
                        evidence_refs=output.get("evidence_refs", []),
                    )
                    skill_results.append(advisory.model_dump(mode="json"))
                    if trace is not None and context is not None:
                        trace.record(context, advisory)
                    return {
                        "output": output,
                        "status": "COMPLETED",
                        "attempts": attempt,
                        "error": None,
                        "skill_results": skill_results,
                        "trace_manifest": _manifest(state),
                    }
                errors.append("worker_output_schema_invalid")
            except Exception as exc:  # noqa: BLE001 - worker boundary fail-closed.
                errors.append(type(exc).__name__)
        context = _context(state)
        skill_results = list(state.get("skill_results", []))
        retry_result = make_result(
            "fallback.retry_budget.v1",
            "DEGRADED",
            {"attempts": spec.max_attempts, "errors": errors[-3:]},
            error_code="RETRY_BUDGET_EXHAUSTED",
        )
        escalation = make_result(
            "fallback.human_escalation.v1",
            "ESCALATE",
            {"reason": "RETRY_BUDGET_EXHAUSTED"},
            error_code="RETRY_BUDGET_EXHAUSTED",
            escalate=True,
        )
        skill_results.extend(
            [retry_result.model_dump(mode="json"), escalation.model_dump(mode="json")]
        )
        if trace is not None and context is not None:
            trace.record(context, retry_result)
            trace.record(context, escalation)
        return {
            "output": _safe_output(";".join(errors[-3:])),
            "status": "DEGRADED",
            "attempts": spec.max_attempts,
            "error": ";".join(errors[-3:]),
            "skill_results": skill_results,
            "trace_manifest": _manifest(state),
        }

    def validate(state: WorkerState) -> dict[str, Any]:
        if state.get("status") == "COMPLETED" and state.get("output", {}).get(
            "schema_valid"
        ):
            return {}
        return {"status": "DEGRADED", "trace_manifest": _manifest(state)}

    graph = StateGraph(WorkerState)
    graph.add_node("tool", read_tool)
    graph.add_node("worker_llm", call_llm)
    graph.add_node("validate", validate)
    graph.set_entry_point("tool")
    graph.add_edge("tool", "worker_llm")
    graph.add_edge("worker_llm", "validate")
    graph.add_edge("validate", END)
    return graph.compile()


def _market_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "tools": ["risk.trading_state.read", "risk.p1.snapshot"],
        "trading_state": payload.get("trading_state"),
        "p1_snapshot": payload.get("p1_snapshot", payload.get("market_snapshot")),
        "context": payload.get("context", {}),
    }


def _pre_trade_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {"tool": "risk.case.check", "assessment": payload.get("assessment", {})}


def _compliance_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": "risk.compliance.check",
        "compliance": payload.get("compliance", {}),
    }


def _counterparty_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": "risk.trading_state.record.read",
        "counterparty": payload.get("counterparty"),
        "derivatives": payload.get("derivatives", {}),
    }


def _should_run(spec: WorkerSpec, payload: dict[str, Any]) -> bool:
    if spec.trigger == "always":
        return True
    if spec.trigger == "when_compliance_evidence_exists":
        return bool(payload.get("compliance"))
    return bool(payload.get("counterparty") or payload.get("derivatives"))


def run_employee_workers(
    payload: dict[str, Any], llm: WorkerLLM | None = None
) -> dict[str, Any]:
    tools: dict[str, WorkerTool] = {
        "market-liquidity-worker": _market_tool,
        "pre-trade-risk-worker": _pre_trade_tool,
        "compliance-policy-worker": _compliance_tool,
        "derivatives-counterparty-worker": _counterparty_tool,
    }
    reports: list[dict[str, Any]] = []
    not_executed: list[str] = []
    input_hash = str(
        payload.get("input_hash")
        or hashlib.sha256(_compact(payload).encode("utf-8")).hexdigest()
    )
    for spec in WORKER_SPECS:
        if not _should_run(spec, payload):
            not_executed.append(spec.worker_id)
            continue
        worker_trace = SkillTrace()
        state = build_worker_graph(
            spec, tools[spec.worker_id], llm, trace=worker_trace
        ).invoke({"worker_id": spec.worker_id, "input": payload})
        reports.append(
            {
                "worker_id": spec.worker_id,
                "role": spec.role,
                "tools": list(spec.tools),
                "status": state.get("status", "DEGRADED"),
                "attempts": state.get("attempts", 0),
                "output": state.get("output", {}),
                "error": state.get("error"),
                "output_contract": spec.output_contract,
                "input_hash": input_hash,
                "skills": list(spec.skill_ids),
                "skill_results": state.get("skill_results", []),
                "rag_plan": state.get("rag_plan", {}),
                "trace": state.get("trace_manifest", {}),
            }
        )
    failed = [r["worker_id"] for r in reports if r["status"] != "COMPLETED"]
    return {
        "runtime": {
            "executor": "LangGraph",
            "provider": "ollama",
            "model": _model_name(),
            "max_retries": 2,
            "max_attempts": 3,
        },
        "workers": reports,
        "executed": [r["worker_id"] for r in reports if r["status"] == "COMPLETED"],
        "failed": failed,
        "not_executed": not_executed,
        "degraded": bool(failed),
        "input_hash": input_hash,
    }
