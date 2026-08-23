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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID, uuid4

from langgraph.graph import END, StateGraph

from departments.employee_worker_runtime import WorkerSpec, run_coroutine_sync
from departments.risk_qa_worker_profiles import (
    QA_WORKER_TECH,
    WorkerTechProfile,
    tech_profile_for,
)
from orchestration.llm_observability import (
    begin_worker_metric,
    end_worker_metric,
    publish_worker_activity,
    publish_worker_opportunity,
    record_llm_call,
    redacted_current_worker_generation,
    redacted_langfuse_worker_span,
)

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


# ▶ WorkerSpec 은 공용 런타임 것을 쓴다 (2026-08-12). risk 와 같은 이유 -
#   부서마다 다시 정의하면 오케스트레이터가 부서별 특례를 들고 있어야 하고,
#   그 특례를 안 고치면 새 워커가 조용히 빠진다.
#   ⚠ output_contract 는 spec 마다 명시한다(공용 기본값은 "worker-context.v1").


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
        output_contract="qa.worker-context.v1",
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
        output_contract="qa.worker-context.v1",
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
        with redacted_current_worker_generation() as generation:
            response = client.chat.completions.create(
                model=_model_name(),
                temperature=0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            generation.set_usage(getattr(response, "usage", None))
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
        # 필드 타입을 명시한다 - risk 와 같은 이유(2026-08-12 실측).
        system = (
            f"You are the {spec.role}. You are an AI-QA employee, not Hermes supervisor. "
            "Use only supplied evidence. Never change a binding QA verdict, approve an order, "
            "write a ledger, or close a finding.\n"
            "Return ONLY a JSON object with exactly these fields:\n"
            '  "summary": string — one paragraph, no markdown\n'
            '  "confidence": number between 0 and 1 (e.g. 0.75). '
            "NOT a word like \"high\"/\"medium\"/\"low\", NOT a percentage like 90.\n"
            '  "evidence_refs": array of strings. Use [] if you had none.\n'
            '  "escalate": boolean true or false — NOT an object, NOT a string.\n'
            "If evidence_refs is empty you MUST set escalate to true. "
            "Do not wrap the JSON in code fences."
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
    payload = _normalize_qa_payload(payload)
    result: dict[str, Any] = {
        "tools": ["qa.model_risk.evaluate", "qa.internal_audit.evaluate"],
        "model_risk": None,
        "internal_audit": payload.get("internal_audit"),
    }
    supplied_model_risk = payload.get("model_risk")
    if supplied_model_risk is not None:
        result["model_risk"] = {
            "decision": "ESCALATE",
            "reason_codes": ["model_risk_provenance_untrusted"],
            "calculation_version": "qa-model-risk-v1",
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
    if audit_events is None:
        pass
    elif not audit_events:
        result["internal_audit"] = {
            "decision": "ESCALATE",
            "findings": ["internal_audit_input_missing"],
            "calculation_version": "qa-internal-audit-v1",
        }
    else:
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
    payload = _normalize_qa_payload(payload)
    return {
        "tools": ["qa.ops.evaluate", "qa.tool_permission.check"],
        "ops": payload.get("ops_assessment"),
        "permission": payload.get("permission"),
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
def worker_tools() -> dict[str, WorkerTool]:
    """이 부서 Worker 의 도구 표. **부서가 소유한다.**

    risk 와 같은 이유로 오케스트레이터에서 여기로 되돌렸다 - 하드코딩된 표를
    안 고치면 새 워커가 `tool_not_registered` 로 조용히 DEGRADED 된다.
    evidence-qa/model-audit/ops-permission 은 qa-runner 로 흡수돼
    WORKER_SPECS 에 없으므로 이 표에도 없다.
    """
    return {
        "hallucination-critic-worker": _hallucination_tool,
        "incident-postmortem-worker": _incident_tool,
    }


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
def _normalize_qa_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Map legacy governed-input aliases onto the canonical runner names."""
    normalized = dict(payload)
    if normalized.get("permission") is None and "permission_check" in normalized:
        normalized["permission"] = normalized.get("permission_check")
    return normalized


def _qa_runner_failure(
    code: str,
    *,
    detail: str | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code}
    if detail:
        error["detail"] = detail
    if fields:
        error["fields"] = fields
    return {
        "worker_id": RUNNER_ID,
        "role": RUNNER_ROLE,
        "tools": list(RUNNER_TOOLS),
        "status": "ESCALATED",
        "attempts": 1,
        "llm": False,
        "output": {
            "worker_id": RUNNER_ID,
            "facts": {},
            "blockers": [code],
            "escalate": True,
            "decided_by": "canonical-runtime",
            "authoritative": False,
        },
        "error": error,
        "output_contract": "qa.qa-runner.v1",
    }


def _governed_input_errors(
    governed: Mapping[str, Any],
) -> list[str]:
    # ▶ **판정 필드는 생산자마다 이름이 다르다** (2026-08-11 실측)
    #   같은 governed input 인데 ModelRiskEngine 은 `decision` 을, InternalAuditEngine
    #   과 check_tool_permission 은 `status` 를 낸다. 여기서 `decision`/`result` 만
    #   요구하면 **엔진이 정상 판정을 냈는데도 malformed 로 막힌다** - 실제로
    #   디스패처 경로의 qa-runner 가 이 이유로 늘 ESCALATED 였다
    #   (malformed: ['internal_audit.decision', 'permission.result']).
    #
    #   값이 **없는 것**과 이름이 **다른 것**은 다르다. 전자를 막는 것이 fail-closed 고
    #   후자를 막는 것은 오작동이다 - 판정이 있는데 못 읽어 에스컬레이션하면 사람이
    #   볼 필요 없는 것을 보게 되고, 그러면 진짜 에스컬레이션이 묻힌다.
    #   그래서 **하나라도 있으면 통과**로 바꾼다. 전부 없으면 여전히 막힌다.
    required_fields = {
        "model_risk": ("decision", "status"),
        "internal_audit": ("decision", "status"),
        "ops_assessment": ("status", "decision"),
        "permission": ("result", "status"),
    }
    errors: list[str] = []
    for name, value in governed.items():
        if value is None:
            continue
        if not isinstance(value, Mapping) or not value:
            errors.append(name)
            continue
        if name == "assessment":
            if value.get("decision") in (None, ""):
                claim_checks = value.get("claim_checks")
                if not isinstance(claim_checks, (list, tuple)) or not claim_checks:
                    errors.append("assessment.decision")
        else:
            fields = required_fields[name]
            if all(value.get(f) in (None, "") for f in fields):
                # 어느 이름으로도 판정이 없다 - 이건 진짜 결측이다.
                errors.append(f"{name}.{fields[0]}")
        if name == "assessment" and "claim_checks" in value:
            claim_checks = value.get("claim_checks")
            valid_results = {
                "SUPPORTED",
                "PARTIAL",
                "UNSUPPORTED",
                "CONTRADICTED",
                "NOT_APPLICABLE",
            }
            if (
                isinstance(claim_checks, (str, bytes, bytearray))
                or not isinstance(claim_checks, (list, tuple))
                or any(
                    not isinstance(claim, Mapping)
                    or claim.get("result") not in valid_results
                    for claim in claim_checks
                )
            ):
                errors.append("assessment.claim_checks")
    return errors


def _qa_runner_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """evidence·model risk·internal audit·ops·permission 잡무. **모델을 부르지 않는다.**

    EvidenceQaEngine/ModelRiskEngine/InternalAuditEngine/OpsHealthMonitor/
    check_tool_permission이 이미 만든 판정을 그대로 옮긴다 - `summary` 필드가
    없는 것이 이 직원의 요지다.
    """
    payload = _normalize_qa_payload(payload)
    evidence = _evidence_tool(payload)
    audit = _audit_tool(payload)
    ops = _ops_tool(payload)

    governed = {
        "assessment": evidence.get("assessment") if "assessment" in payload else None,
        "model_risk": (
            audit.get("model_risk")
            if "model_risk" in payload or "model_risk_input" in payload
            else None
        ),
        "internal_audit": (
            audit.get("internal_audit")
            if "internal_audit" in payload or "internal_audit_events" in payload
            else None
        ),
        "ops_assessment": ops.get("ops") if "ops_assessment" in payload else None,
        "permission": (
            ops.get("permission")
            if "permission" in payload or "permission_check" in payload
            else None
        ),
    }
    malformed = _governed_input_errors(governed)
    if malformed:
        return _qa_runner_failure("SCHEMA_FAILURE", fields=malformed)

    if not any(isinstance(value, Mapping) and value for value in governed.values()):
        return _qa_runner_failure("MISSING_INPUT")
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

    decision = "FAIL" if blockers else "PASS"
    return {
        "worker_id": RUNNER_ID,
        "role": RUNNER_ROLE,
        "tools": list(RUNNER_TOOLS),
        "status": "DEGRADED" if blockers else "COMPLETED",
        "decision": decision,
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


def qa_runner(payload: dict[str, Any]) -> dict[str, Any]:
    """Compatibility projection backed exclusively by the canonical QARunner."""

    from qa_runtime import QARunner, ToolRegistry, build_qa_task_context, canonical_payload_hash

    if not isinstance(payload, Mapping):
        return _qa_runner_failure("INVALID_INPUT")

    try:
        normalized = _normalize_qa_payload(payload)
        projected: dict[str, Any] = _qa_runner_failure("MISSING_INPUT")
        digest = canonical_payload_hash(normalized)
        case_id, task_id, trace_id = _qa_dispatch_ids(normalized, digest)
        task_kwargs: dict[str, Any] = {
            "worker": RUNNER_ID,
            "case_id": case_id,
            "task_id": task_id,
            "trace_id": trace_id,
        }
        refs = _qa_input_refs(normalized)
        if refs is not None:
            task_kwargs["input_refs"] = refs
        task = build_qa_task_context(normalized, **task_kwargs)

        class _Executor:
            def invoke(
                self,
                _task: Any,
                _evidence_refs: Any,
                input_payload: dict[str, Any],
            ) -> dict[str, Any]:
                nonlocal projected
                projected = _qa_runner_projection(dict(input_payload))
                error_code = (projected.get("error") or {}).get("code")
                return {
                    "status": projected.get("decision", projected["status"]),
                    "decision": projected.get("decision"),
                    "summary": "Canonical deterministic QA runner projection",
                    "reason_codes": [error_code] if error_code else (),
                    "error_code": error_code,
                }

        outcome = QARunner(
            tools=ToolRegistry(),
            executor=_Executor(),
            profile_version="qa-employee-runner-v1",
            model_version="deterministic:qa-employee-runner-v1",
            adapter_version="qa-employee-adapter-v1",
        ).run(task, normalized)
    except Exception as exc:  # noqa: BLE001 - malformed input must fail closed.
        return _qa_runner_failure("SCHEMA_FAILURE", detail=type(exc).__name__)

    report = dict(projected)
    report["status"] = outcome.status.value
    report["input_hash"] = outcome.payload_hash
    report["worker_context"] = (
        outcome.worker_context.model_dump(mode="json", exclude_none=True)
        if outcome.worker_context is not None
        else None
    )
    report["replay_manifest"] = (
        outcome.replay_manifest.model_dump(mode="json")
        if outcome.replay_manifest is not None
        else None
    )
    if outcome.error_code is not None:
        report["error"] = {"code": outcome.error_code.value, "detail": outcome.error_detail}
        report["output"]["escalate"] = True
    return report


def _should_run(spec: WorkerSpec, payload: dict[str, Any]) -> bool:
    if spec.trigger == "always":
        return True
    if spec.trigger == "when_unsupported_claim_exists":
        assessment = payload.get("assessment")
        if not isinstance(assessment, Mapping):
            return False
        claim_checks = assessment.get("claim_checks", ())
        if isinstance(claim_checks, (str, bytes, bytearray)) or not isinstance(
            claim_checks, (list, tuple)
        ):
            return False
        return any(
            isinstance(claim, Mapping)
            and claim.get("result") in {"UNSUPPORTED", "CONTRADICTED"}
            for claim in claim_checks
        )
    if spec.trigger == "when_incident_exists":
        return bool(payload.get("incident"))
    raise ValueError(f"unsupported QA worker trigger: {spec.trigger!r}")
def _normalize_worker_report_status(report: dict[str, Any]) -> dict[str, Any]:
    """Transport escalation is never reported as a successful completion."""
    output = report.get("output")
    if (
        str(report.get("status", "")).upper() == "COMPLETED"
        and isinstance(output, Mapping)
        and output.get("escalate") is True
    ):
        report["status"] = "DEGRADED"
        report.setdefault("error", "WORKER_ESCALATED")
    return report

def _qa_dispatch_ids(payload: Mapping[str, Any], input_hash: str) -> tuple[str, str, str]:
    """Return stable fan-out identity, preferring an upstream Artifact trace."""
    if not isinstance(payload, Mapping):
        payload = {}
    case_id = str(
        payload.get("case_id")
        or payload.get("verification_id")
        or payload.get("mandate_id")
        or f"local:{input_hash[7:23]}"
    )
    task_id = str(payload.get("task_id") or f"{case_id}-task")
    artifact = payload.get("artifact")
    artifact_trace = (
        artifact.get("trace_id")
        if isinstance(artifact, Mapping)
        else getattr(artifact, "trace_id", None)
    )
    trace_id = str(
        artifact_trace
        or payload.get("trace_id")
        or payload.get("event_id")
        or f"qa-trace-{input_hash[7:23]}"
    )
    return case_id, task_id, trace_id


def _qa_input_refs(payload: Mapping[str, Any]) -> Any:
    """Use supplied refs, or derive one ref from an upstream Artifact."""
    if "input_refs" in payload:
        return payload.get("input_refs")
    artifact = payload.get("artifact")
    if artifact is None:
        return None
    artifact_id = (
        artifact.get("artifact_version_id")
        if isinstance(artifact, Mapping)
        else getattr(artifact, "artifact_version_id", None)
    )
    if artifact_id is None:
        return None
    artifact_type = (
        artifact.get("artifact_type", "qa-artifact")
        if isinstance(artifact, Mapping)
        else getattr(artifact, "artifact_type", "qa-artifact")
    )
    from runtime_contracts import sha256_hash

    return [{
        "type": f"qa-{str(artifact_type)}"[:64],
        "id": str(artifact_id),
        "content_hash": sha256_hash(artifact),
    }]


def _adapt_employee_report(
    report: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    input_hash: str,
    case_id: str,
    task_id: str,
    trace_id: str,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Adapt one live employee report to strict qa.worker-context.v1."""
    from qa_runtime import build_qa_task_context
    from runtime_contracts import WorkerContext, sha256_hash, to_worker_context

    normalized = dict(report) if isinstance(report, Mapping) else {}
    context_worker = str(normalized.get("worker_id") or worker_id or "qa-worker")
    try:
        supplied_hash = normalized.get("input_hash")
        if supplied_hash is not None and str(supplied_hash) != input_hash:
            raise ValueError("worker input_hash does not match dispatch payload")
        task_kwargs: dict[str, Any] = {
            "worker": context_worker,
            "case_id": case_id,
            "task_id": task_id,
            "trace_id": trace_id,
        }
        refs = _qa_input_refs(payload)
        if refs is not None:
            task_kwargs["input_refs"] = refs
        task = build_qa_task_context(payload, **task_kwargs)
        supplied_context = normalized.get("worker_context")
        if supplied_context is not None:
            existing_context = WorkerContext.model_validate(supplied_context)
            if (
                existing_context.schema_version != "qa.worker-context.v1"
                or existing_context.case_id != task.case_id
                or existing_context.task_id != task.task_id
                or existing_context.trace_id != task.trace_id
                or existing_context.consumer_worker != task.worker
                or existing_context.input_refs != task.input_refs
                or existing_context.input_hash != input_hash
            ):
                raise ValueError("worker context identity does not map to the dispatch task")
        output = normalized.get("output")
        output_mapping = output if isinstance(output, Mapping) else {}
        status = str(normalized.get("status", "DEGRADED"))
        decision = normalized.get("decision") or normalized.get("verdict")
        if (
            status.upper() == "COMPLETED"
            and context_worker != RUNNER_ID
            and decision is None
            and not output_mapping
            and not normalized.get("reason_codes")
        ):
            raise ValueError("completed worker report has no validated output")
        error = normalized.get("error")
        if status.upper() == "COMPLETED" and (
            error
            or output_mapping.get("escalate") is True
            or str(decision).upper() in {"FAIL", "ESCALATE"}
        ):
            status = "DEGRADED"
        error_code = (
            error.get("code")
            if isinstance(error, Mapping)
            else "SCHEMA_FAILURE"
            if error
            else None
        )
        reason_codes = normalized.get("reason_codes") or ()
        if isinstance(reason_codes, (str, bytes, bytearray)):
            reason_codes = (str(reason_codes),)
        summary = output_mapping.get("summary") or normalized.get("summary") or "QA worker completed"
        context = to_worker_context(
            task,
            producer_worker=(
                context_worker
                if context_worker == RUNNER_ID
                else f"{context_worker}-runner"
            ),
            profile_version="qa-worker-profile-v1",
            model_version=(
                f"deterministic:{context_worker}-v1"
                if context_worker == RUNNER_ID
                else _model_name()
            ),
            adapter_version="qa-employee-report-adapter-v1",
            status=status,
            advisory={
                "summary": str(summary),
                **(
                    {"suggested_verdict": str(decision)}
                    if decision is not None and str(decision)
                    else {}
                ),
            },
            decision=str(decision) if decision is not None else None,
            reason_codes=reason_codes,
            error_code=error_code,
            input_hash=input_hash,
            output_hash=sha256_hash(output_mapping),
            attempt=max(1, min(3, int(normalized.get("attempts") or 1))),
            timeout_ms=8_000,
        )
    except Exception as exc:  # noqa: BLE001 - malformed reports fail closed.
        normalized["status"] = "DEGRADED"
        normalized["error"] = {
            "code": "SCHEMA_FAILURE",
            "detail": f"worker_context:{type(exc).__name__}",
        }
        safe_output = (
            dict(normalized["output"])
            if isinstance(normalized.get("output"), Mapping)
            else {}
        )
        safe_output["escalate"] = True
        normalized["output"] = safe_output
        fallback_payload = dict(payload)
        fallback_payload.pop("input_refs", None)
        try:
            task = build_qa_task_context(
                fallback_payload,
                worker=context_worker,
                case_id=case_id,
                task_id=task_id,
                trace_id=trace_id,
            )
            context = to_worker_context(
                task,
                producer_worker=(
                    context_worker
                    if context_worker == RUNNER_ID
                    else f"{context_worker}-runner"
                ),
                profile_version="qa-worker-profile-v1",
                model_version=(
                    f"deterministic:{context_worker}-v1"
                    if context_worker == RUNNER_ID
                    else _model_name()
                ),
                adapter_version="qa-employee-report-adapter-v1",
                status="DEGRADED",
                advisory={
                    "summary": "Malformed QA worker report; human review is required.",
                    "suggested_verdict": "ESCALATE",
                },
                reason_codes=("SCHEMA_FAILURE",),
                error_code="SCHEMA_FAILURE",
                input_hash=input_hash,
                output_hash=sha256_hash(safe_output),
                attempt=1,
                timeout_ms=8_000,
            )
        except Exception:
            normalized["worker_context"] = None
            normalized["context_error"] = "SCHEMA_FAILURE"
            return normalized
    normalized["worker_context"] = WorkerContext.model_validate(
        context.model_dump(mode="json")
    ).model_dump(mode="json", exclude_none=True)
    return normalized
def _trigger_failure_result(
    payload: Any,
    input_hash: str,
    *,
    topology: str,
    detail: str,
) -> dict[str, Any]:
    report = _qa_runner_failure("SCHEMA_FAILURE", detail=detail)
    safe_payload = payload if isinstance(payload, Mapping) else {}
    case_id, task_id, trace_id = _qa_dispatch_ids(safe_payload, input_hash)
    report = _adapt_employee_report(
        report,
        safe_payload,
        input_hash=input_hash,
        case_id=case_id,
        task_id=task_id,
        trace_id=trace_id,
        worker_id=RUNNER_ID,
    )
    worker_ids = [spec.worker_id for spec in WORKER_SPECS]
    return {
        "runtime": {
            "executor": "LangGraph",
            "topology": topology,
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
        "workers": [report],
        "executed": [RUNNER_ID],
        "failed": [RUNNER_ID],
        "not_executed": worker_ids,
        "degraded": True,
        "input_hash": input_hash,
    }


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
    from qa_runtime import canonical_payload_hash

    payload = _normalize_qa_payload(payload) if isinstance(payload, Mapping) else payload
    input_hash = canonical_payload_hash(payload)
    case_id, task_id, trace_id = _qa_dispatch_ids(payload, input_hash)
    try:
        for spec in WORKER_SPECS:
            _should_run(spec, payload)
    except Exception as exc:  # noqa: BLE001 - malformed triggers fail closed.
        return _trigger_failure_result(
            payload,
            input_hash,
            topology="sequential",
            detail=type(exc).__name__,
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
        report = _normalize_worker_report_status(
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
        reports.append(
            _adapt_employee_report(
                report,
                payload,
                input_hash=input_hash,
                case_id=case_id,
                task_id=task_id,
                trace_id=trace_id,
            )
        )
        if trace_bridge is not None:
            try:
                trace_bridge.record(
                    worker_id=spec.worker_id,
                    trace_id=trace_id,
                    input_hash=input_hash,
                    tools=spec.tools,
                    payload=payload,
                    output=state.get("output", {}),
                )
            except Exception as exc:  # noqa: BLE001 - audit must escalate
                trace_errors.append(f"{spec.worker_id}:{type(exc).__name__}")
    reports.append(
        _adapt_employee_report(
            qa_runner(payload),
            payload,
            input_hash=input_hash,
            case_id=case_id,
            task_id=task_id,
            trace_id=trace_id,
            worker_id=RUNNER_ID,
        )
    )
    failed = [r["worker_id"] for r in reports if r["status"] != "COMPLETED"]
    if trace_errors:
        failed.extend(f"trace:{error}" for error in trace_errors)
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
        "executed": [r["worker_id"] for r in reports],
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
    from qa_runtime import canonical_payload_hash

    payload = _normalize_qa_payload(payload) if isinstance(payload, Mapping) else payload
    input_hash = canonical_payload_hash(payload)
    case_id, task_id, trace_id = _qa_dispatch_ids(payload, input_hash)

    if (
        trace_bridge is None
        and os.environ.get("QA_TRACE_PERSIST", "false").strip().lower() == "true"
    ):
        try:
            from audit.worker_trace_bridge import build_default_trace_bridge

            trace_bridge = build_default_trace_bridge()
        except Exception as exc:  # noqa: BLE001 - trace failure escalates safely.
            trace_errors.append(type(exc).__name__)

    try:
        not_executed = [
            spec.worker_id for spec in WORKER_SPECS if not _should_run(spec, payload)
        ]
        eligible = [spec for spec in WORKER_SPECS if _should_run(spec, payload)]
    except Exception as exc:  # noqa: BLE001 - malformed triggers fail closed.
        return _trigger_failure_result(
            payload,
            input_hash,
            topology="async_fan_out_fan_in_independent_graphs",
            detail=type(exc).__name__,
        )

    if incident_timeline is None and any(
        spec.worker_id == "incident-postmortem-worker" for spec in eligible
    ):
        try:
            from audit.incident_timeline import build_default_incident_timeline

            incident_timeline = build_default_incident_timeline()
        except Exception as exc:  # noqa: BLE001 - persistence boundary escalates safely.
            trace_errors.append(f"incident_timeline:{type(exc).__name__}")

    async def _execute_one(spec: WorkerSpec) -> dict[str, Any]:
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
        report = _normalize_worker_report_status(report)
        report = _adapt_employee_report(
            report,
            payload,
            input_hash=input_hash,
            case_id=case_id,
            task_id=task_id,
            trace_id=trace_id,
        )

        if trace_bridge is not None:
            try:
                await asyncio.to_thread(
                    trace_bridge.record,
                    worker_id=spec.worker_id,
                    trace_id=trace_id,
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

    async def run_one(spec: WorkerSpec) -> dict[str, Any]:
        """실행 결과에 손대지 않고 HR 유휴 관측 이벤트만 덧붙인다(2026-08-20).

        이 부서는 공용 run_worker_registry 가 아니라 자체 실행기를 쓰므로 계측도
        여기서 따로 한다 - 2026-08-10 배선이 orchestration/workflows/
        portfolio_recommendation.py 한 곳에만 있어서, 그 파이프라인 밖에서 돈
        Worker 가 HR 리포트에 IDLE 로 뜨고 있었다.

        publish 는 로컬 큐잉이라 네트워크를 기다리지 않고(실측 0.117ms), 실패해도
        예외를 올리지 않는다 - 계측이 Worker 판정을 바꾸지 못한다.
        """

        started = time.perf_counter()
        # 토큰·호출수는 record_llm_call() 이 이미 잰다 - begin_worker_metric() 이
        # 열려 있어야 그 값이 쌓인다(2026-08-20, 공용 런타임과 같은 계약).
        metric_token = begin_worker_metric(
            worker_id=spec.worker_id, role=spec.role,
            stage="qa", model_name=_model_name(),
        )
        with redacted_langfuse_worker_span(
            worker_id=spec.worker_id,
            role=spec.role,
            stage="qa",
            trace_id=str(trace_id or ""),
        ):
            report = await _execute_one(spec)
        status = str(report.get("status", "DEGRADED"))
        attempts = int(report.get("attempts", 0) or 0)
        measured = end_worker_metric(
            metric_token, status=status, attempts=attempts, eval_score=None,
        )
        publish_worker_activity(
            stage="qa",
            worker_id=spec.worker_id,
            role=spec.role,
            status=status,
            attempts=attempts,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_count=0 if status == "COMPLETED" else 1,
            trace_id=str(trace_id or ""),
            measured=measured,
        )
        return report

    # 미발화도 관측 사실이다 - 점유율의 분모(2026-08-20, 공용 런타임과 같은 계약).
    for spec in WORKER_SPECS:
        if spec.worker_id in not_executed:
            publish_worker_opportunity(
                stage="qa",
                worker_id=spec.worker_id,
                role=spec.role,
                trace_id=str(trace_id or ""),
            )

    reports = list(await asyncio.gather(*(run_one(spec) for spec in eligible)))
    try:
        runner_report = qa_runner(payload)
    except Exception as exc:  # noqa: BLE001 - fan-in fails closed.
        runner_report = _qa_runner_failure("SCHEMA_FAILURE", detail=type(exc).__name__)
    reports.append(
        _adapt_employee_report(
            _normalize_worker_report_status(runner_report),
            payload,
            input_hash=input_hash,
            case_id=case_id,
            task_id=task_id,
            trace_id=trace_id,
            worker_id=RUNNER_ID,
        )
    )
    failed = [item["worker_id"] for item in reports if item["status"] != "COMPLETED"]
    failed.extend(f"trace:{error}" for error in trace_errors)
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
        "executed": [item["worker_id"] for item in reports],
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
