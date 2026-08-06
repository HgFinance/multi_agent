"""QA department employee Workers.

직원 2명. **그중 LLM 을 쓰는 것은 2명뿐이다**(2026-08-06).

  hallucination-critic-worker   Unsupported/contradicted claim 검증 — LLM
  incident-postmortem-worker    FACT/INFERENCE 분리 postmortem — LLM
  qa-runner                     evidence·model risk·internal audit·ops·permission
                                 잡무 — **LLM 없음.** 결정론 엔진 출력을 그대로 옮긴다

기존 evidence-qa-worker / model-and-internal-audit-worker / ops-and-permission-worker
를 `qa-runner` 하나로 흡수했다. 셋 다 **답이 하나로 정해지는 일**이었다 -
`EvidenceQaEngine.check_artifact()`, `ModelRiskEngine.evaluate()`,
`InternalAuditEngine.evaluate()`, `OpsHealthMonitor.evaluate()`,
`check_tool_permission()` 이 이미 PASS/WARN/FAIL/ESCALATE 나 ALLOWED/DENIED를
결정론으로 낸다(`tool_permission_check.py` 자체 docstring: "판정은 결정론적
코드가 하고 LLM은 결과를 설명만 한다"). 그 위에 `qwen3:1.7b` 를 얹어봐야 이미
나온 판정을 다시 서술하는 것뿐이다(CLAUDE.md 개발 원칙 4·9번, risk의
risk-runner·trading의 desk-runner와 같은 기준).

hallucination-critic-worker와 incident-postmortem-worker만 남긴 이유는 이 둘은
결정론 엔진이 대신할 수 없는 일을 한다. Contradiction 탐지는
`verify.contradiction.v1`을 뒷받침하는 엔진이 없어 실제 semantic 비교가 필요하고,
FACT/INFERENCE 분리는 `IncidentTimeline`이 보관·정렬만 할 뿐 분류하지 않으므로
worker 자신이 분류한다.

The Hermes profile is the department-head boundary.  This module owns only the
employee layer: `hallucination-critic-worker`/`incident-postmortem-worker` are
independently compiled LangGraph workers that read an allow-listed
deterministic tool result and ask the local Ollama model for bounded,
non-binding context; `qa-runner` calls no model at all.  Neither can change a
binding QA verdict, approve an order, write a ledger, or close a finding.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID, uuid4

from langgraph.graph import END, StateGraph

from departments.employee_worker_runtime import run_coroutine_sync
from departments.risk_qa_worker_profiles import (
    QA_WORKER_TECH,
    WorkerTechProfile,
    tech_profile_for,
)
from orchestration.llm_observability import record_llm_call

# This module is loaded directly from its file path by the shared dispatcher.
# The QA ``audit`` directory is intentionally a namespace package (no
# ``__init__.py``), so its department directory must be importable explicitly
# before the incident timeline persistence boundary is resolved.
_BASE = Path(__file__).resolve().parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))


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
from qa_worker_skill_runtime.rag_router import choose_rag_route
from qa_worker_skill_runtime.tools import invoke_tool
from qa_worker_skill_runtime.trace import SkillTrace

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
    tech_profile: WorkerTechProfile | None = None


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
        "hallucination-critic-worker",
        "Hallucination and contradiction critic",
        ("qa.evidence.rag",),
        "when_unsupported_claim_exists",
        tech_profile=tech_profile_for(QA_WORKER_TECH, "hallucination-critic-worker"),
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
        "incident-postmortem-worker",
        "Incident timeline and postmortem analyst",
        ("qa.incident.record",),
        "when_incident_exists",
        tech_profile=tech_profile_for(QA_WORKER_TECH, "incident-postmortem-worker"),
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
    started = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=_model_name(),
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        record_llm_call(
            usage=getattr(response, "usage", None),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return response.choices[0].message.content or ""
    except Exception:
        record_llm_call(latency_ms=int((time.perf_counter() - started) * 1000), error=True)
        raise


def _compact(value: Any, limit: int = 9000) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)[:limit]


_INCIDENT_ENTRY_TYPES = {"FACT", "INFERENCE", "ACTION", "DECISION"}


def _validate_incident_entries(raw: Any) -> tuple[list[dict[str, Any]], bool]:
    """FACT/INFERENCE 분류는 incident-postmortem-worker 자신의 몫이다(IncidentTimeline은
    보관·정렬만 한다). entries가 없으면 유효(빈 목록) - 형식이 틀리면만 무효로 재시도시킨다."""
    if raw is None:
        return [], True
    if not isinstance(raw, list):
        return [], False
    cleaned: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            return [], False
        entry_type = str(item.get("entry_type", "")).upper()
        summary = item.get("summary")
        occurred_at = item.get("occurred_at")
        if entry_type not in _INCIDENT_ENTRY_TYPES or not isinstance(summary, str):
            return [], False
        try:
            datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return [], False
        cleaned.append(
            {"entry_type": entry_type, "summary": summary[:2000], "occurred_at": occurred_at}
        )
    return cleaned, True


def _parse_worker_output(
    raw: str, worker_id: str, *, require_entries: bool = False
) -> tuple[dict[str, Any], bool]:
    text = (raw or "").strip()
    candidate = text.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
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
    refs = parsed.get("evidence_refs", [])
    valid = isinstance(refs, list) and isinstance(parsed.get("escalate", False), bool)
    entries: list[dict[str, Any]] = []
    if require_entries:
        entries, entries_valid = _validate_incident_entries(parsed.get("entries"))
        valid = valid and entries_valid
    result = {
        "worker_id": worker_id,
        "summary": parsed["summary"][:4000],
        "confidence": parsed.get("confidence"),
        "evidence_refs": refs,
        "escalate": parsed.get("escalate", True),
        "schema_valid": valid,
    }
    if require_entries:
        result["entries"] = entries
    return result, valid


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
        return (
            trace.manifest(context) if trace is not None and context is not None else {}
        )

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
        tech_profile = spec.tech_profile.as_dict() if spec.tech_profile else {}
        require_entries = spec.worker_id == "incident-postmortem-worker"
        system = (
            f"You are the {spec.role}. You are an AI-QA employee, not Hermes supervisor. "
            "Use only supplied evidence. Never change a binding QA verdict, approve an order, "
            "write a ledger, or close a finding. Return JSON with summary, confidence, "
            "evidence_refs, and escalate."
        )
        if require_entries:
            system += (
                ' Additionally return "entries": a list of {entry_type, summary, occurred_at} '
                "objects reconstructing the incident timeline. entry_type must be FACT (directly "
                "observed, cite evidence) or INFERENCE (your interpretation) - never blend the "
                "two in one entry. occurred_at is ISO-8601. Omit entries you cannot support."
            )
        prompt = (
            f"Worker: {spec.worker_id}\n"
            f"Allowed tools: {', '.join(spec.tools)}\n"
            f"Required skills: {', '.join(spec.skill_ids)}\n"
            f"Technology stack: {', '.join(tech_profile.get('stack', ()))}\n"
            f"Technology usage: {'; '.join(tech_profile.get('usage', ()))}\n"
            f"Output contract: {spec.output_contract}\n"
            f"Evidence:\n{_compact(state.get('tool_output', {}))}"
        )
        errors: list[str] = []
        for attempt in range(1, spec.max_attempts + 1):
            try:
                output, schema_valid = _parse_worker_output(
                    worker_llm(system, prompt),
                    spec.worker_id,
                    require_entries=require_entries,
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


def _evidence_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {"tool": "qa.evidence.check", "assessment": payload.get("assessment", {})}


def _hallucination_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": "qa.evidence.rag",
        "reviews": payload.get("hallucination_reviews", []),
    }


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
                    protected_failure_rate=float(
                        model_risk_input["protected_failure_rate"]
                    ),
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
    return {
        "tools": ["qa.ops.evaluate", "qa.tool_permission.check"],
        "ops": payload.get("ops_assessment"),
        "permission": payload.get("permission_check"),
    }


def _incident_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": "qa.incident.record",
        "incident": payload.get("incident"),
        "incident_events": payload.get("incident_events", []),
    }


def _persist_incident_entries(
    timeline: Any, payload: dict[str, Any], output: dict[str, Any]
) -> list[str]:
    """incident-postmortem-worker가 분류한 FACT/INFERENCE entries를 audit.incident_events로
    옮긴다. IncidentTimeline은 QA_INCIDENT_PERSIST 여부에 따라 인메모리거나 Postgres다
    (build_default_incident_timeline). 저장 실패는 삼키지 않고 에러 목록으로 돌려준다 -
    호출자가 trace_errors/degraded에 반영한다."""
    entries = output.get("entries") or []
    incident_id = (payload.get("incident") or {}).get("incident_id")
    if not entries or not incident_id or timeline is None:
        return []
    from audit.incident_timeline import IncidentEntryType

    errors: list[str] = []
    for entry in entries:
        try:
            timeline.add_event(
                incident_id,
                "incident-postmortem-worker",
                IncidentEntryType(entry["entry_type"]),
                entry["summary"],
                datetime.fromisoformat(str(entry["occurred_at"]).replace("Z", "+00:00")),
                "svc_qa_incident_worker",
            )
        except Exception as exc:  # noqa: BLE001 - persistence failure must escalate.
            errors.append(f"incident_event:{type(exc).__name__}")
    return errors


# ── qa-runner: 결정론 잡무 (LLM 없음, 2026-08-06) ────────────────────────────
# evidence-qa-worker(EvidenceQaEngine.check_artifact 서술), model-and-internal-audit-worker
# (ModelRiskEngine/InternalAuditEngine 서술), ops-and-permission-worker
# (OpsHealthMonitor/check_tool_permission 서술)를 합쳐 흡수했다. 셋 다 답이 하나로
# 정해지는 일이었다 - PASS/WARN/FAIL/ESCALATE·ALLOWED/DENIED를 결정론 엔진이 이미
# 낸다(tool_permission_check.py 자체 docstring: "판정은 결정론적 코드가 하고
# LLM은 결과를 설명만 한다"). risk의 risk-runner·trading의 desk-runner와 같은 기준.
RUNNER_ID = "qa-runner"
RUNNER_ROLE = (
    "QA desk runner — evidence/model risk/internal audit/ops/permission 결과 조회"
    "(결정론, LLM 없음)"
)
RUNNER_TOOLS = (
    "qa.evidence.check",
    "qa.model_risk.evaluate",
    "qa.internal_audit.evaluate",
    "qa.ops.evaluate",
    "qa.tool_permission.check",
)


def qa_runner(payload: dict[str, Any]) -> dict[str, Any]:
    """evidence·model risk·internal audit·ops·permission 잡무. **모델을 부르지 않는다.**

    EvidenceQaEngine/ModelRiskEngine/InternalAuditEngine/OpsHealthMonitor/
    check_tool_permission이 이미 만든 판정을 그대로 옮긴다 - `summary` 필드가
    없는 것이 이 직원의 요지다.
    """
    evidence = _evidence_tool(payload)
    audit = _audit_tool(payload)
    ops = _ops_tool(payload)

    blockers: list[str] = []
    evidence_decision = (evidence.get("assessment") or {}).get("decision")
    if evidence_decision and str(evidence_decision).upper() != "PASS":
        blockers.append(f"evidence_{str(evidence_decision).lower()}")

    model_risk_decision = (audit.get("model_risk") or {}).get("decision")
    if model_risk_decision and str(model_risk_decision).upper() != "PASS":
        blockers.append(f"model_risk_{str(model_risk_decision).lower()}")

    internal_audit_decision = (audit.get("internal_audit") or {}).get("decision")
    if internal_audit_decision and str(internal_audit_decision).upper() != "PASS":
        blockers.append(f"internal_audit_{str(internal_audit_decision).lower()}")

    ops_status = (ops.get("ops") or {}).get("status")
    if ops_status and str(ops_status).lower() != "healthy":
        blockers.append(f"ops_{str(ops_status).lower()}")

    permission_result = (ops.get("permission") or {}).get("result")
    if permission_result and str(permission_result).upper() != "ALLOWED":
        blockers.append(f"permission_{str(permission_result).lower()}")

    return {
        "worker_id": RUNNER_ID,
        "role": RUNNER_ROLE,
        "tools": list(RUNNER_TOOLS),
        "status": "COMPLETED",
        "attempts": 1,
        "llm": False,  # 이 직원은 모델을 안 부른다. 계약으로 박는다
        "output": {
            "worker_id": RUNNER_ID,
            "facts": {"evidence": evidence, "audit": audit, "ops": ops},
            "blockers": blockers,
            "escalate": bool(blockers),
            "decided_by": "deterministic",
            "authoritative": False,  # 판정은 각 결정론 엔진이 한다. 이건 옮긴 것일 뿐이다
        },
        "error": None,
        "output_contract": "qa.qa-runner.v1",
    }


def _should_run(spec: WorkerSpec, payload: dict[str, Any]) -> bool:
    if spec.trigger == "always":
        return True
    if spec.trigger == "when_unsupported_claim_exists":
        return any(
            c.get("result") in {"UNSUPPORTED", "CONTRADICTED"}
            for c in payload.get("assessment", {}).get("claim_checks", [])
        )
    return bool(payload.get("incident"))


def _run_employee_workers_sequential(
    payload: dict[str, Any],
    llm: WorkerLLM | None = None,
    trace_bridge: Any | None = None,
) -> dict[str, Any]:
    tools: dict[str, WorkerTool] = {
        "hallucination-critic-worker": _hallucination_tool,
        "incident-postmortem-worker": _incident_tool,
    }
    reports: list[dict[str, Any]] = []
    trace_errors: list[str] = []
    not_executed: list[str] = []
    input_hash = str(
        payload.get("input_hash")
        or hashlib.sha256(_compact(payload).encode("utf-8")).hexdigest()
    )
    if (
        trace_bridge is None
        and os.environ.get("QA_TRACE_PERSIST", "false").strip().lower() == "true"
    ):
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
                "technology": spec.tech_profile.as_dict() if spec.tech_profile else {},
                "skill_results": state.get("skill_results", []),
                "rag_plan": state.get("rag_plan", {}),
                "trace": state.get("trace_manifest", {}),
            }
        )
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
    # qa-runner는 레지스트리 밖이다 - LLM Worker의 failed/degraded 판정에 섞이지 않는다.
    reports.append(qa_runner(payload))
    return {
        "runtime": {
            "executor": "LangGraph",
            "provider": "ollama",
            "model": _model_name(),
            "max_retries": 2,
            "max_attempts": 3,
            "technology_profiles": {
                spec.worker_id: spec.tech_profile.as_dict()
                for spec in WORKER_SPECS
                if spec.tech_profile is not None
            },
        },
        "workers": reports,
        "executed": [r["worker_id"] for r in reports if r["status"] == "COMPLETED"],
        "failed": failed,
        "not_executed": not_executed,
        "degraded": bool(failed),
        "input_hash": input_hash,
    }


async def run_employee_workers_async(
    payload: dict[str, Any],
    llm: WorkerLLM | None = None,
    trace_bridge: Any | None = None,
    incident_timeline: Any | None = None,
) -> dict[str, Any]:
    """Fan out guarded QA Worker graphs and deterministically fan them in."""

    tools: dict[str, WorkerTool] = {
        "hallucination-critic-worker": _hallucination_tool,
        "incident-postmortem-worker": _incident_tool,
    }
    reports: list[dict[str, Any]] = []
    trace_errors: list[str] = []
    input_hash = str(
        payload.get("input_hash")
        or hashlib.sha256(_compact(payload).encode("utf-8")).hexdigest()
    )

    if (
        trace_bridge is None
        and os.environ.get("QA_TRACE_PERSIST", "false").strip().lower() == "true"
    ):
        try:
            from audit.worker_trace_bridge import build_default_trace_bridge

            trace_bridge = build_default_trace_bridge()
        except Exception as exc:  # noqa: BLE001 - trace failure escalates safely.
            trace_errors.append(type(exc).__name__)

    not_executed = [
        spec.worker_id for spec in WORKER_SPECS if not _should_run(spec, payload)
    ]
    eligible = [spec for spec in WORKER_SPECS if _should_run(spec, payload)]

    if incident_timeline is None and any(
        spec.worker_id == "incident-postmortem-worker" for spec in eligible
    ):
        try:
            from audit.incident_timeline import build_default_incident_timeline

            incident_timeline = build_default_incident_timeline()
        except Exception as exc:  # noqa: BLE001 - persistence boundary escalates safely.
            trace_errors.append(f"incident_timeline:{type(exc).__name__}")

    async def run_one(spec: WorkerSpec) -> dict[str, Any]:
        worker_trace = SkillTrace()
        try:
            state = await build_worker_graph(
                spec,
                tools[spec.worker_id],
                llm,
                trace=worker_trace,
            ).ainvoke({"worker_id": spec.worker_id, "input": payload})
            report = {
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
                "technology": spec.tech_profile.as_dict() if spec.tech_profile else {},
                "skill_results": state.get("skill_results", []),
                "rag_plan": state.get("rag_plan", {}),
                "trace": state.get("trace_manifest", {}),
            }
        except Exception as exc:  # noqa: BLE001 - Worker boundary fails closed.
            report = {
                "worker_id": spec.worker_id,
                "role": spec.role,
                "tools": list(spec.tools),
                "status": "DEGRADED",
                "attempts": 0,
                "output": {
                    "worker_id": spec.worker_id,
                    "summary": "QA Worker graph failed; human review is required.",
                    "confidence": 0.0,
                    "evidence_refs": [],
                    "escalate": True,
                    "schema_valid": False,
                },
                "error": type(exc).__name__,
                "output_contract": spec.output_contract,
                "input_hash": input_hash,
                "skills": list(spec.skill_ids),
                "technology": spec.tech_profile.as_dict() if spec.tech_profile else {},
                "skill_results": [],
                "rag_plan": {},
                "trace": {},
            }

        if trace_bridge is not None:
            try:
                await asyncio.to_thread(
                    trace_bridge.record,
                    worker_id=spec.worker_id,
                    trace_id=str(payload.get("trace_id") or uuid4()),
                    input_hash=input_hash,
                    tools=spec.tools,
                    payload=payload,
                    output=report.get("output", {}),
                )
            except Exception as exc:  # noqa: BLE001 - audit must escalate.
                trace_errors.append(f"{spec.worker_id}:{type(exc).__name__}")
                report["trace_error"] = type(exc).__name__

        if spec.worker_id == "incident-postmortem-worker" and report["status"] == "COMPLETED":
            persist_errors = await asyncio.to_thread(
                _persist_incident_entries, incident_timeline, payload, report.get("output", {})
            )
            if persist_errors:
                trace_errors.extend(persist_errors)
                report["incident_persist_errors"] = persist_errors
        return report

    reports = list(await asyncio.gather(*(run_one(spec) for spec in eligible)))
    failed = [item["worker_id"] for item in reports if item["status"] != "COMPLETED"]
    failed.extend(f"trace:{error}" for error in trace_errors)
    # qa-runner는 레지스트리 밖이다 - LLM Worker의 failed/degraded 판정에 섞이지 않는다.
    reports.append(qa_runner(payload))
    return {
        "runtime": {
            "executor": "LangGraph",
            "topology": "async_fan_out_fan_in_independent_graphs",
            "provider": "ollama",
            "model": _model_name(),
            "max_retries": 2,
            "max_attempts": 3,
            "technology_profiles": {
                spec.worker_id: spec.tech_profile.as_dict()
                for spec in WORKER_SPECS
                if spec.tech_profile is not None
            },
        },
        "workers": reports,
        "executed": [
            item["worker_id"] for item in reports if item["status"] == "COMPLETED"
        ],
        "failed": failed,
        "not_executed": not_executed,
        "degraded": bool(failed),
        "input_hash": input_hash,
    }


def run_employee_workers(
    payload: dict[str, Any],
    llm: WorkerLLM | None = None,
    trace_bridge: Any | None = None,
    incident_timeline: Any | None = None,
) -> dict[str, Any]:
    """Synchronous compatibility boundary for the async QA fan-in."""

    return run_coroutine_sync(
        run_employee_workers_async(
            payload,
            llm=llm,
            trace_bridge=trace_bridge,
            incident_timeline=incident_timeline,
        )
    )
