"""Risk department employee LangGraph workers.

The Hermes profile is the department-head boundary.  This module owns only the
employee layer: each worker is an independently compiled LangGraph that reads
an allow-listed deterministic tool result and asks the local Ollama model for
bounded, non-binding context.  It cannot approve an order or change a gate.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

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


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: str
    role: str
    tools: tuple[str, ...]
    trigger: str
    output_contract: str = "risk.worker-context.v1"
    max_attempts: int = 3


WORKER_SPECS: tuple[WorkerSpec, ...] = (
    WorkerSpec(
        "market-liquidity-worker",
        "Market and liquidity risk analyst",
        ("risk.trading_state.read", "risk.p1.snapshot"),
        "always",
    ),
    WorkerSpec(
        "pre-trade-risk-worker",
        "Pre-trade deterministic gate analyst",
        ("risk.case.check",),
        "always",
    ),
    WorkerSpec(
        "compliance-policy-worker",
        "Point-in-time policy evidence analyst",
        ("risk.compliance.check",),
        "when_compliance_evidence_exists",
    ),
    WorkerSpec(
        "derivatives-counterparty-worker",
        "Derivatives and counterparty exposure analyst",
        ("risk.trading_state.record.read",),
        "when_counterparty_or_derivatives_signal_exists",
    ),
)


def _model_name() -> str:
    return os.getenv("OLLAMA_CHAT_MODEL", "qwen3:1.7b")


def _base_url() -> str:
    raw = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
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
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
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
        return {"worker_id": worker_id, "summary": "", "schema_valid": False, "escalate": True}, False
    evidence_refs = parsed.get("evidence_refs", [])
    valid = isinstance(evidence_refs, list) and isinstance(parsed.get("escalate", False), bool)
    output = {
        "worker_id": worker_id,
        "summary": parsed["summary"][:4000],
        "confidence": parsed.get("confidence"),
        "evidence_refs": evidence_refs,
        "escalate": parsed.get("escalate", True),
        "schema_valid": valid,
    }
    return output, valid


def build_worker_graph(spec: WorkerSpec, tool: WorkerTool, llm: WorkerLLM | None = None):
    """Build one independent employee graph: tool -> Qwen -> schema check."""

    def read_tool(state: WorkerState) -> dict[str, Any]:
        return {"tool_output": tool(state.get("input", {}))}

    def call_llm(state: WorkerState) -> dict[str, Any]:
        worker_llm = llm or default_worker_llm
        system = (
            f"You are {spec.role}. You are a Risk employee, not the Hermes supervisor. "
            "Use only supplied tool evidence. Never approve, resize, reject, submit an order, "
            "change a limit, or write a ledger. Return JSON with summary, confidence, "
            "evidence_refs, escalate."
        )
        prompt = (
            f"Worker: {spec.worker_id}\nAllowed tools: {', '.join(spec.tools)}\n"
            f"Output contract: {spec.output_contract}\nEvidence:\n{_compact(state.get('tool_output', {}))}"
        )
        errors: list[str] = []
        for attempt in range(1, spec.max_attempts + 1):
            try:
                output, schema_valid = _parse_worker_output(worker_llm(system, prompt), spec.worker_id)
                if schema_valid:
                    return {"output": output, "status": "COMPLETED", "attempts": attempt, "error": None}
                errors.append("worker_output_schema_invalid")
            except Exception as exc:  # noqa: BLE001 - worker boundary is fail-closed.
                errors.append(type(exc).__name__)
        return {
            "output": {
                "worker_id": spec.worker_id,
                "summary": "직원 LLM 결과를 검증하지 못했습니다.",
                "confidence": 0.0,
                "evidence_refs": [],
                "escalate": True,
                "schema_valid": False,
            },
            "status": "DEGRADED",
            "attempts": spec.max_attempts,
            "error": ";".join(errors[-3:]),
        }

    def validate(state: WorkerState) -> dict[str, Any]:
        output = state.get("output", {})
        if state.get("status") == "COMPLETED" and output.get("schema_valid"):
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


def _market_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {"tool": "risk.trading_state.read", "trading_state": payload.get("trading_state"),
            "context": payload.get("context", {})}


def _pre_trade_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {"tool": "risk.case.check", "assessment": payload.get("assessment", {})}


def _compliance_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {"tool": "risk.compliance.check", "compliance": payload.get("compliance", {})}


def _counterparty_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {"tool": "risk.trading_state.record.read", "counterparty": payload.get("counterparty"),
            "derivatives": payload.get("derivatives", {})}


def _should_run(spec: WorkerSpec, payload: dict[str, Any]) -> bool:
    if spec.trigger == "always":
        return True
    if spec.trigger == "when_compliance_evidence_exists":
        return bool(payload.get("compliance"))
    return bool(payload.get("counterparty") or payload.get("derivatives"))


def run_employee_workers(payload: dict[str, Any], llm: WorkerLLM | None = None) -> dict[str, Any]:
    tools: dict[str, WorkerTool] = {
        "market-liquidity-worker": _market_tool,
        "pre-trade-risk-worker": _pre_trade_tool,
        "compliance-policy-worker": _compliance_tool,
        "derivatives-counterparty-worker": _counterparty_tool,
    }
    reports: list[dict[str, Any]] = []
    not_executed: list[str] = []
    input_hash = hashlib.sha256(_compact(payload).encode("utf-8")).hexdigest()
    for spec in WORKER_SPECS:
        if not _should_run(spec, payload):
            not_executed.append(spec.worker_id)
            continue
        state = build_worker_graph(spec, tools[spec.worker_id], llm).invoke(
            {"worker_id": spec.worker_id, "input": payload}
        )
        reports.append({
            "worker_id": spec.worker_id,
            "role": spec.role,
            "tools": list(spec.tools),
            "status": state.get("status", "DEGRADED"),
            "attempts": state.get("attempts", 0),
            "output": state.get("output", {}),
            "error": state.get("error"),
            "output_contract": spec.output_contract,
            "input_hash": input_hash,
        })
    failed = [r["worker_id"] for r in reports if r["status"] != "COMPLETED"]
    return {
        "runtime": {"executor": "LangGraph", "provider": "ollama", "model": _model_name(),
                    "max_retries": 2, "max_attempts": 3},
        "workers": reports,
        "executed": [r["worker_id"] for r in reports if r["status"] == "COMPLETED"],
        "failed": failed,
        "not_executed": not_executed,
        "degraded": bool(failed),
        "input_hash": input_hash,
    }
