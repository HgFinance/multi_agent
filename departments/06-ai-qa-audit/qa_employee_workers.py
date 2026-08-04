"""QA department employee LangGraph workers.

Hermes remains the QA department head.  Every active employee below is an
independent LangGraph worker using the local Ollama/Qwen model for bounded
non-binding context; deterministic QA engines remain the source of truth.
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
from uuid import UUID

from langgraph.graph import END, StateGraph



def _load_skill_package() -> None:
    package_name = "qa_worker_skill_runtime"
    if package_name in sys.modules:
        return
    package_dir = Path(__file__).with_name("skills")
    spec = importlib.util.spec_from_file_location(
        package_name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("QA skill package is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)


_load_skill_package()

from qa_worker_skill_runtime.contracts import QASkillContext, make_result
from qa_worker_skill_runtime.guards import build_context, scope_check
from qa_worker_skill_runtime.tools import invoke_tool
from qa_worker_skill_runtime.trace import SkillTrace
from qa_worker_skill_runtime.rag_router import choose_rag_route

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
    output_contract: str = "qa.worker-context.v1"
    max_attempts: int = 3
    skill_ids: tuple[str, ...] = ()


_QA_GUARDS = (
    "guard.input_normalize.v1",
    "guard.scope_check.v1",
    "guard.secret_redaction.v1",
)
_QA_TRACE = (
    "audit.trace_record.v1",
    "audit.replay_manifest.v1",
    "audit.cost_latency.v1",
)


WORKER_SPECS: tuple[WorkerSpec, ...] = (
    WorkerSpec(
        "evidence-qa-worker",
        "Evidence and citation QA analyst",
        ("qa.evidence.check",),
        "always",
        skill_ids=_QA_GUARDS
        + (
            "context.internal_api.v1",
            "context.repository_read.v1",
            "context.cache_read.v1",
            "guard.pit_filter.v1",
            "verify.schema.v1",
            "verify.citation.v1",
            "verify.provenance_chain.v1",
            "verify.numeric_temporal.v1",
            "advisory.grounded_summary.v1",
        )
        + _QA_TRACE
        + ("fallback.retry_budget.v1", "fallback.human_escalation.v1"),
    ),
    WorkerSpec(
        "hallucination-critic-worker",
        "Hallucination and contradiction critic",
        ("qa.evidence.rag",),
        "when_unsupported_claim_exists",
        skill_ids=_QA_GUARDS
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
            "rag.entity_link.v1",
            "rag.graph_context.v1",
            "rag.hyper_extract.v1",
            "rag.self_check.v1",
            "verify.schema.v1",
            "verify.citation.v1",
            "verify.provenance_chain.v1",
            "verify.contradiction.v1",
            "advisory.grounded_summary.v1",
        )
        + _QA_TRACE
        + ("fallback.retry_budget.v1", "fallback.human_escalation.v1"),
    ),
    WorkerSpec(
        "model-and-internal-audit-worker",
        "Model risk and internal audit analyst",
        ("qa.model_risk.evaluate", "qa.internal_audit.evaluate"),
        "when_audit_input_exists",
        skill_ids=_QA_GUARDS
        + (
            "context.internal_api.v1",
            "context.repository_read.v1",
            "verify.schema.v1",
            "verify.provenance_chain.v1",
            "calc.deterministic_gate.v1",
            "advisory.grounded_summary.v1",
        )
        + _QA_TRACE
        + ("fallback.human_escalation.v1",),
    ),
    WorkerSpec(
        "ops-and-permission-worker",
        "Agent operations and tool permission analyst",
        ("qa.ops.evaluate", "qa.tool_permission.check"),
        "when_ops_input_exists",
        skill_ids=_QA_GUARDS
        + (
            "context.internal_api.v1",
            "verify.schema.v1",
            "verify.provenance_chain.v1",
            "calc.deterministic_gate.v1",
            "advisory.grounded_summary.v1",
        )
        + _QA_TRACE
        + ("fallback.human_escalation.v1",),
    ),
    WorkerSpec(
        "incident-postmortem-worker",
        "Incident timeline and postmortem analyst",
        ("qa.incident.record",),
        "when_incident_exists",
        skill_ids=_QA_GUARDS
        + (
            "context.internal_api.v1",
            "context.repository_read.v1",
            "verify.schema.v1",
            "verify.provenance_chain.v1",
            "verify.numeric_temporal.v1",
            "advisory.grounded_summary.v1",
        )
        + _QA_TRACE
        + ("fallback.human_escalation.v1",),
    ),
)


def _model_name() -> str:
    return os.getenv("OLLAMA_CHAT_MODEL") or "qwen3:1.7b"


def _base_url() -> str:
    raw = (os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434/v1").rstrip("/")
    return raw if raw.endswith("/v1") else f"{raw}/v1"


def default_worker_llm(system: str, prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(
        base_url=_base_url(),
        api_key=os.getenv("OLLAMA_API_KEY", "ollama"),
        timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "8")),
    )
    response = client.chat.completions.create(
        model=_model_name(),
        temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


def _compact(value: Any, limit: int = 9000) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)[:limit]


def _parse_worker_output(raw: str, worker_id: str) -> tuple[dict[str, Any], bool]:
    text = (raw or "").strip()
    candidate = text.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {"worker_id": worker_id, "summary": text[:4000], "confidence": None,
                "evidence_refs": [], "escalate": True, "schema_valid": False}, False
    if not isinstance(parsed, dict) or not isinstance(parsed.get("summary"), str):
        return {"worker_id": worker_id, "summary": "", "schema_valid": False, "escalate": True}, False
    refs = parsed.get("evidence_refs", [])
    valid = isinstance(refs, list) and isinstance(parsed.get("escalate", False), bool)
    return {"worker_id": worker_id, "summary": parsed["summary"][:4000],
            "confidence": parsed.get("confidence"), "evidence_refs": refs,
            "escalate": parsed.get("escalate", True), "schema_valid": valid}, valid


def build_worker_graph(spec: WorkerSpec, tool: WorkerTool, llm: WorkerLLM | None = None):
    def read_tool(state: WorkerState) -> dict[str, Any]:
        return {"tool_output": tool(state.get("input", {}))}

    def call_llm(state: WorkerState) -> dict[str, Any]:
        worker_llm = llm or default_worker_llm
        system = (
            f"You are {spec.role}. You are a QA employee, not the Hermes supervisor. "
            "Use only supplied evidence. Never change a binding QA verdict, approve an order, "
            "write a ledger, or close a finding. Return JSON with summary, confidence, "
            "evidence_refs, escalate."
        )
        prompt = (f"Worker: {spec.worker_id}\nAllowed tools: {', '.join(spec.tools)}\n"
                  f"Output contract: {spec.output_contract}\nEvidence:\n{_compact(state.get('tool_output', {}))}")
        errors: list[str] = []
        for attempt in range(1, spec.max_attempts + 1):
            try:
                output, schema_valid = _parse_worker_output(worker_llm(system, prompt), spec.worker_id)
                if schema_valid:
                    return {"output": output, "status": "COMPLETED", "attempts": attempt, "error": None}
                errors.append("worker_output_schema_invalid")
            except Exception as exc:  # noqa: BLE001 - fail-closed worker boundary.
                errors.append(type(exc).__name__)
        return {"output": {"worker_id": spec.worker_id,
                            "summary": "직원 LLM 결과를 검증하지 못했습니다.",
                            "confidence": 0.0, "evidence_refs": [], "escalate": True,
                            "schema_valid": False},
                "status": "DEGRADED", "attempts": spec.max_attempts,
                "error": ";".join(errors[-3:])}

    def validate(state: WorkerState) -> dict[str, Any]:
        if state.get("status") == "COMPLETED" and state.get("output", {}).get("schema_valid"):
            return {}
        return {"status": "DEGRADED"}

    graph = StateGraph(WorkerState)
    graph.add_node("tool", read_tool)
    graph.add_node("worker_llm", call_llm)
    graph.add_node("validate", validate)
    graph.set_entry_point("tool")
    graph.add_edge("tool", "worker_llm")
    graph.add_edge("worker_llm", "validate")
    graph.add_edge("validate", END)
    return graph.compile()


def build_worker_graph(
    spec: WorkerSpec,
    tool: WorkerTool,
    llm: WorkerLLM | None = None,
    trace: SkillTrace | None = None,
):
    """Build a guarded QA Worker graph with explicit Skill evidence."""

    def _context(state: WorkerState) -> QASkillContext | None:
        raw = state.get("skill_context")
        if not raw:
            return None
        try:
            return QASkillContext.model_validate(raw)
        except Exception:  # noqa: BLE001 - invalid trace context is non-fatal.
            return None

    def _manifest(state: WorkerState) -> dict[str, Any]:
        context = _context(state)
        return trace.manifest(context) if trace is not None and context is not None else {}

    def _safe_output(error: str) -> dict[str, Any]:
        return {
            "worker_id": spec.worker_id,
            "summary": "AI-QA evidence boundary failed; the result is inconclusive.",
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
                tool_name=spec.tools[0] if spec.tools else "qa.tool.unknown",
            )
            skill_results.append(invocation.result.model_dump(mode="json"))
            if trace is not None:
                trace.record(context, invocation.result, latency_ms=invocation.latency_ms)
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
            f"You are the {spec.role}. You are an AI-QA employee, not Hermes supervisor. "
            "Use only supplied evidence. Never change a binding QA verdict, approve an order, "
            "write a ledger, or close a finding. Return JSON with summary, confidence, "
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
        if state.get("status") == "COMPLETED" and state.get("output", {}).get("schema_valid"):
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


def _evidence_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {"tool": "qa.evidence.check", "assessment": payload.get("assessment", {})}


def _hallucination_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {"tool": "qa.evidence.rag", "reviews": payload.get("hallucination_reviews", [])}


def _audit_tool(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "tools": ["qa.model_risk.evaluate", "qa.internal_audit.evaluate"],
        "model_risk": payload.get("model_risk"),
        "internal_audit": payload.get("internal_audit"),
    }
    model_risk_input = payload.get("model_risk_input")
    if model_risk_input is not None:
        try:
            from model_risk import ModelRiskEngine, ModelRiskInput

            assessment = ModelRiskEngine().evaluate(
                ModelRiskInput(
                    model_id=UUID(str(model_risk_input["model_id"])),
                    model_version=str(model_risk_input["model_version"]),
                    prompt_version=str(model_risk_input["prompt_version"]),
                    dataset_version=str(model_risk_input["dataset_version"]),
                    evaluation_count=int(model_risk_input["evaluation_count"]),
                    accuracy=float(model_risk_input["accuracy"]),
                    calibration_error=float(model_risk_input["calibration_error"]),
                    drift_score=float(model_risk_input["drift_score"]),
                    protected_failure_rate=float(model_risk_input["protected_failure_rate"]),
                )
            )
            result["model_risk"] = {
                "decision": assessment.decision.value,
                "reason_codes": list(assessment.reason_codes),
                "calculation_version": assessment.calculation_version,
                "input_hash": assessment.input_hash,
            }
        except Exception as exc:  # noqa: BLE001 - invalid audit input escalates
            result["model_risk"] = {
                "decision": "ESCALATE",
                "reason_codes": ["model_risk_input_invalid"],
                "calculation_version": "qa-model-risk-v1",
                "error": type(exc).__name__,
            }

    audit_events = payload.get("internal_audit_events")
    if audit_events is not None:
        try:
            from internal_audit import InternalAuditEngine

            assessment = InternalAuditEngine().evaluate(
                events=audit_events,
                expected_department=str(payload.get("internal_audit_department", "qa")),
            )
            result["internal_audit"] = {
                "decision": assessment.decision.value,
                "findings": list(assessment.findings),
                "calculation_version": assessment.calculation_version,
                "input_hash": assessment.input_hash,
            }
        except Exception as exc:  # noqa: BLE001 - invalid audit input escalates
            result["internal_audit"] = {
                "decision": "ESCALATE",
                "findings": ["internal_audit_input_invalid"],
                "calculation_version": "qa-internal-audit-v1",
                "error": type(exc).__name__,
            }
    return result


def _ops_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {"tools": ["qa.ops.evaluate", "qa.tool_permission.check"],
            "ops": payload.get("ops_assessment"), "permission": payload.get("permission_check")}


def _incident_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {"tool": "qa.incident.record", "incident": payload.get("incident"),
            "incident_events": payload.get("incident_events", [])}


def _should_run(spec: WorkerSpec, payload: dict[str, Any]) -> bool:
    if spec.trigger == "always":
        return True
    if spec.trigger == "when_unsupported_claim_exists":
        return any(c.get("result") in {"UNSUPPORTED", "CONTRADICTED"}
                   for c in payload.get("assessment", {}).get("claim_checks", []))
    if spec.trigger == "when_audit_input_exists":
        return bool(payload.get("model_risk") or payload.get("internal_audit"))
    if spec.trigger == "when_ops_input_exists":
        return bool(payload.get("ops_assessment") or payload.get("permission_check"))
    return bool(payload.get("incident"))


def run_employee_workers(
    payload: dict[str, Any],
    llm: WorkerLLM | None = None,
    trace_bridge: Any | None = None,
) -> dict[str, Any]:
    tools: dict[str, WorkerTool] = {
        "evidence-qa-worker": _evidence_tool,
        "hallucination-critic-worker": _hallucination_tool,
        "model-and-internal-audit-worker": _audit_tool,
        "ops-and-permission-worker": _ops_tool,
        "incident-postmortem-worker": _incident_tool,
    }
    reports: list[dict[str, Any]] = []
    trace_errors: list[str] = []
    not_executed: list[str] = []
    input_hash = str(
        payload.get("input_hash")
        or hashlib.sha256(_compact(payload).encode("utf-8")).hexdigest()
    )
    if trace_bridge is None and os.environ.get("QA_TRACE_PERSIST", "false").strip().lower() == "true":
        try:
            from audit.worker_trace_bridge import build_default_trace_bridge

            trace_bridge = build_default_trace_bridge()
        except Exception as exc:  # noqa: BLE001 - optional audit boundary
            trace_errors.append(type(exc).__name__)
    for spec in WORKER_SPECS:
        if not _should_run(spec, payload):
            not_executed.append(spec.worker_id)
            continue
        worker_trace = SkillTrace()
        state = build_worker_graph(
            spec, tools[spec.worker_id], llm, trace=worker_trace
        ).invoke(
            {"worker_id": spec.worker_id, "input": payload}
        )
        reports.append({"worker_id": spec.worker_id, "role": spec.role,
                        "tools": list(spec.tools), "status": state.get("status", "DEGRADED"),
                        "attempts": state.get("attempts", 0), "output": state.get("output", {}),
                        "error": state.get("error"), "output_contract": spec.output_contract,
 "input_hash": input_hash, "skills": list(spec.skill_ids), "skill_results": state.get("skill_results", []), "rag_plan": state.get("rag_plan", {}), "trace": state.get("trace_manifest", {})})
        if trace_bridge is not None:
            try:
                trace_bridge.record(
                    worker_id=spec.worker_id,
                    trace_id=str(payload.get("trace_id", "")),
                    input_hash=input_hash,
                    tools=spec.tools,
                    payload=payload,
                    output=state.get("output", {}),
                )
            except Exception as exc:  # noqa: BLE001 - audit must escalate
                trace_errors.append(f"{spec.worker_id}:{type(exc).__name__}")
    failed = [r["worker_id"] for r in reports if r["status"] != "COMPLETED"]
    if trace_errors:
        failed.extend(f"trace:{error}" for error in trace_errors)
    return {"runtime": {"executor": "LangGraph", "provider": "ollama", "model": _model_name(),
                         "max_retries": 2, "max_attempts": 3},
            "workers": reports,
            "executed": [r["worker_id"] for r in reports if r["status"] == "COMPLETED"],
            "failed": failed, "not_executed": not_executed, "degraded": bool(failed),
            "input_hash": input_hash}
