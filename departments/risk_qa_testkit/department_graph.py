"""TEST용 Risk/QA Department Graph.

부서장(Hermes)과 직원(LangGraph Worker)을 한 번의 실행에서 검증한다.
실제 Hermes 프로세스나 Production credential을 호출하지 않으며, head_llm과
worker_llm은 호출자가 주입한다. 따라서 TEST에서는 결정론적 stub을 사용하고,
PRODUCTION adapter가 승인되기 전까지 이 그래프는 binding decision을 만들지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph


WorkerLLM = Callable[[str, str], str]
HeadLLM = Callable[[str, str], str]
WorkerTool = Callable[[dict[str, Any]], dict[str, Any]]


class DepartmentState(TypedDict, total=False):
    input: dict[str, Any]
    head_context: dict[str, Any]
    delegation_plan: list[dict[str, Any]]
    selected_worker_ids: list[str]
    worker_reports: list[dict[str, Any]]
    handoffs: list[dict[str, Any]]
    not_executed: list[str]
    head_output: dict[str, Any]
    status: str
    error: str | None


@dataclass(frozen=True)
class DepartmentGraphSpec:
    department: str
    profile: str
    head_id: str
    output_contract: str
    worker_module: Any
    worker_tools: dict[str, WorkerTool]


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Peer/head handoff에 필요한 최소 정보만 전달한다."""

    output = report.get("output") or {}
    return {
        "worker_id": report.get("worker_id"),
        "status": report.get("status", "DEGRADED"),
        "attempts": report.get("attempts", 0),
        "summary": output.get("summary", ""),
        "confidence": output.get("confidence"),
        "evidence_refs": list(output.get("evidence_refs", [])),
        "escalate": bool(output.get("escalate", True)),
        "input_hash": report.get("input_hash"),
        "trace": report.get("trace", {}),
    }


def _decode_head_output(raw: str, spec: DepartmentGraphSpec) -> dict[str, Any]:
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = candidate.replace("```json", "", 1).replace("```", "").strip()
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("department head output must be an object")
    summary = parsed.get("summary")
    recommendation = parsed.get("recommendation")
    escalate = parsed.get("escalate")
    if not isinstance(summary, str) or not summary:
        raise ValueError("department head summary is required")
    if not isinstance(recommendation, str) or not recommendation:
        raise ValueError("department head recommendation is required")
    if not isinstance(escalate, bool):
        raise ValueError("department head escalate must be boolean")
    return {
        "head_id": spec.head_id,
        "department": spec.department,
        "runtime": {"executor": "Hermes", "binding": False},
        "output_contract": spec.output_contract,
        "summary": summary[:4000],
        "recommendation": recommendation[:80],
        "safe_action": parsed.get("safe_action", "ESCALATE" if escalate else "NO_ACTION"),
        "escalate": escalate,
        "binding": False,
        "schema_valid": True,
    }


def _head_failure(spec: DepartmentGraphSpec, input_hash: str, error: str) -> dict[str, Any]:
    return {
        "head_id": spec.head_id,
        "department": spec.department,
        "runtime": {"executor": "Hermes", "binding": False},
        "output_contract": spec.output_contract,
        "summary": "Department head runtime failed; human review is required.",
        "recommendation": "ESCALATE",
        "safe_action": "HOLD" if spec.department == "risk-management" else "ESCALATE",
        "escalate": True,
        "binding": False,
        "schema_valid": False,
        "error": error,
        "input_hash": input_hash,
    }


def build_department_graph(
    spec: DepartmentGraphSpec,
    *,
    worker_llm: WorkerLLM,
    head_llm: HeadLLM,
) -> Any:
    """Build one parent graph containing head nodes and nested Worker Graphs.

    The registry order is intentional: each subsequent employee receives only a
    compact summary from the previous executed employee. It never receives a
    peer's unrestricted input, secrets, or binding decision authority.
    """

    worker_specs = tuple(spec.worker_module.WORKER_SPECS)

    def head_intake(state: DepartmentState) -> DepartmentState:
        payload = dict(state.get("input", {}))
        input_hash = str(payload.get("input_hash") or _hash(payload))
        selected = [
            worker.worker_id
            for worker in worker_specs
            if spec.worker_module._should_run(worker, payload)
        ]
        head_context = {
            "head_id": spec.head_id,
            "department": spec.department,
            "profile": spec.profile,
            "runtime": "Hermes",
            "binding": False,
            "input_hash": input_hash,
        }
        delegation_plan = [
            {
                "from": spec.head_id,
                "to": worker.worker_id,
                "type": "HEAD_DELEGATION",
                "tools": list(worker.tools),
                "trigger": worker.trigger,
                "input_hash": input_hash,
            }
            for worker in worker_specs
            if worker.worker_id in selected
        ]
        return {
            "head_context": head_context,
            "delegation_plan": delegation_plan,
            "selected_worker_ids": selected,
            "worker_reports": [],
            "handoffs": [],
            "not_executed": [],
            "status": "RUNNING",
            "error": None,
        }

    def make_worker_node(worker_spec: Any) -> Callable[[DepartmentState], DepartmentState]:
        def execute_worker(state: DepartmentState) -> DepartmentState:
            selected = set(state.get("selected_worker_ids", []))
            reports = list(state.get("worker_reports", []))
            handoffs = list(state.get("handoffs", []))
            not_executed = list(state.get("not_executed", []))
            if worker_spec.worker_id not in selected:
                not_executed.append(worker_spec.worker_id)
                return {"not_executed": not_executed}

            payload = dict(state.get("input", {}))
            compact_peers = [_compact_report(report) for report in reports]
            payload["department_head_context"] = state.get("head_context", {})
            payload["peer_context"] = compact_peers
            input_hash = str(payload.get("input_hash") or _hash(payload))
            previous = reports[-1]["worker_id"] if reports else state["head_context"]["head_id"]
            handoffs.append(
                {
                    "from": previous,
                    "to": worker_spec.worker_id,
                    "type": "PEER_CONTEXT" if reports else "HEAD_DELEGATION",
                    "input_hash": input_hash,
                    "allowed_fields": [
                        "worker_id",
                        "status",
                        "summary",
                        "confidence",
                        "evidence_refs",
                        "escalate",
                        "input_hash",
                        "trace",
                    ],
                }
            )
            try:
                worker_trace = spec.worker_module.SkillTrace()
                worker_graph = spec.worker_module.build_worker_graph(
                    worker_spec,
                    spec.worker_tools[worker_spec.worker_id],
                    worker_llm,
                    trace=worker_trace,
                )
                result = worker_graph.invoke({
                    "worker_id": worker_spec.worker_id,
                    "input": payload,
                })
                report = {
                    "worker_id": worker_spec.worker_id,
                    "role": worker_spec.role,
                    "tools": list(worker_spec.tools),
                    "status": result.get("status", "DEGRADED"),
                    "attempts": result.get("attempts", 0),
                    "output": result.get("output", {}),
                    "error": result.get("error"),
                    "output_contract": worker_spec.output_contract,
                    "input_hash": input_hash,
                    "skills": list(worker_spec.skill_ids),
                    "skill_results": result.get("skill_results", []),
                    "rag_plan": result.get("rag_plan", {}),
                    "trace": result.get("trace_manifest", {}),
                    "received_from": previous,
                }
            except Exception as exc:  # noqa: BLE001 - parent graph is fail-closed.
                report = {
                    "worker_id": worker_spec.worker_id,
                    "role": worker_spec.role,
                    "tools": list(worker_spec.tools),
                    "status": "DEGRADED",
                    "attempts": 0,
                    "output": {
                        "worker_id": worker_spec.worker_id,
                        "summary": "Nested Worker Graph failed; human review is required.",
                        "confidence": 0.0,
                        "evidence_refs": [],
                        "escalate": True,
                        "schema_valid": False,
                    },
                    "error": type(exc).__name__,
                    "output_contract": worker_spec.output_contract,
                    "input_hash": input_hash,
                    "skills": list(worker_spec.skill_ids),
                    "skill_results": [],
                    "rag_plan": {},
                    "trace": {},
                    "received_from": previous,
                }
            reports.append(report)
            return {"worker_reports": reports, "handoffs": handoffs}

        execute_worker.__name__ = f"employee_{worker_spec.worker_id.replace('-', '_')}"
        return execute_worker

    def head_synthesis(state: DepartmentState) -> DepartmentState:
        payload = state.get("input", {})
        input_hash = str(payload.get("input_hash") or _hash(payload))
        compact_workers = [_compact_report(report) for report in state.get("worker_reports", [])]
        system = (
            f"You are the {spec.head_id}, the Hermes department head for {spec.department}. "
            "Synthesize employee context only. You do not make a binding Risk or QA decision, "
            "submit an order, write a ledger, close an audit finding, or change permissions. "
            "Return JSON with summary, recommendation, safe_action, escalate."
        )
        prompt = json.dumps(
            {
                "head_context": state.get("head_context", {}),
                "input_hash": input_hash,
                "deterministic_gate": payload.get("deterministic_gate", {}),
                "worker_reports": compact_workers,
                "handoffs": state.get("handoffs", []),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        try:
            head_output = _decode_head_output(head_llm(system, prompt), spec)
        except Exception as exc:  # noqa: BLE001 - head failure cannot pass.
            head_output = _head_failure(spec, input_hash, type(exc).__name__)
        head_output.update(
            {
                "input_hash": input_hash,
                "worker_ids": [report["worker_id"] for report in state.get("worker_reports", [])],
                "not_executed": list(state.get("not_executed", [])),
                "handoff_count": len(state.get("handoffs", [])),
            }
        )
        head_output["output_hash"] = _hash(head_output)
        failed_workers = [
            report["worker_id"]
            for report in state.get("worker_reports", [])
            if report.get("status") != "COMPLETED"
        ]
        advisory_workers = [
            report["worker_id"]
            for report in state.get("worker_reports", [])
            if bool((report.get("output") or {}).get("escalate", False))
        ]
        if advisory_workers:
            head_output["escalate"] = True
            head_output["recommendation"] = "ESCALATE"
            head_output["safe_action"] = "HOLD" if spec.department == "risk-management" else "ESCALATE"
            head_output["worker_advisories"] = advisory_workers
        if failed_workers:
            head_output["worker_failures"] = failed_workers
        return {
            "head_output": head_output,
            "status": "COMPLETED" if head_output.get("schema_valid") and not failed_workers else "DEGRADED",
            "error": head_output.get("error"),
        }

    graph = StateGraph(DepartmentState)
    graph.add_node("head_intake", head_intake)
    for worker_spec in worker_specs:
        graph.add_node(
            f"employee_{worker_spec.worker_id.replace('-', '_')}",
            make_worker_node(worker_spec),
        )
    graph.add_node("head_synthesis", head_synthesis)
    graph.set_entry_point("head_intake")
    previous = "head_intake"
    for worker_spec in worker_specs:
        current = f"employee_{worker_spec.worker_id.replace('-', '_')}"
        graph.add_edge(previous, current)
        previous = current
    graph.add_edge(previous, "head_synthesis")
    graph.add_edge("head_synthesis", END)
    return graph.compile()


def run_department_graph(
    spec: DepartmentGraphSpec,
    payload: dict[str, Any],
    *,
    worker_llm: WorkerLLM | None,
    head_llm: HeadLLM,
    worker_runtime: str = "ollama",
) -> dict[str, Any]:
    """Run the parent graph and return a JSON-safe department report."""

    graph = build_department_graph(spec, worker_llm=worker_llm, head_llm=head_llm)
    state = graph.invoke({"input": dict(payload)})
    reports = list(state.get("worker_reports", []))
    failed = [report["worker_id"] for report in reports if report.get("status") != "COMPLETED"]
    input_hash = str(payload.get("input_hash") or _hash(payload))
    return {
        "department": spec.department,
        "head": state.get("head_output", _head_failure(spec, input_hash, "HEAD_OUTPUT_MISSING")),
        "workers": reports,
        "executed": [report["worker_id"] for report in reports],
        "failed": failed,
        "not_executed": list(state.get("not_executed", [])),
        "handoffs": list(state.get("handoffs", [])),
        "status": state.get("status", "DEGRADED"),
        "degraded": bool(failed) or state.get("status") != "COMPLETED",
        "input_hash": input_hash,
        "runtime": {
            "executor": "LangGraph",
            "head_executor": "Hermes",
            "worker_provider": worker_runtime,
            "worker_model": "qwen3:1.7b",
            "binding": False,
        },
    }
