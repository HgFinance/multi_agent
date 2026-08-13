"""Shared independent LangGraph runtime for department employee Workers.

Department heads are Hermes profiles.  This module owns only the employee
boundary: one compiled LangGraph per Worker, allow-listed context tools, a
local Ollama LLM call, bounded retries, and a non-binding ``worker-context``
output.  It never owns orders, Risk/QA verdicts, ledger postings, or HR
permission grants.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from orchestration.llm_observability import record_llm_call

WorkerLLM = Callable[[str, str], str]
# worker_id -> WorkerLLM. 부서별 LoRA adapter 처럼 **워커마다 모델 좌표가
# 다를 수 있는** 주입 경로다(2026-08-13). 단일 llm 인자는 워커 정체를 모른 채
# 공유되므로 Worker Model Gateway 의 worker_id→adapter 해석이 전달될 수 없었다
# - registry 에 adapter 를 켜도 조용한 no-op 이 되는 결함이 리뷰에서 잡혔다.
WorkerLLMFactory = Callable[[str], WorkerLLM]
WorkerTool = Callable[[Mapping[str, Any]], Mapping[str, Any]]


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
    """Runtime contract for one employee LLM and its allowed context tools.

    ▶ 앞의 네 자리(worker_id, role, tools, trigger)는 **위치인자 순서를 바꾸지 않는다.**
      부서들이 그 넷만 위치로 주고 나머지는 키워드로 준다.

    ▶ 아래 네 필드는 Risk·QA 가 각자 정의하던 것을 여기로 올린 것이다(2026-08-12).
      두 부서는 공용 런타임에서 `run_coroutine_sync` 만 가져다 쓰고 WorkerSpec 은
      자기 파일에 다시 정의하고 있었다 - 필드가 갈리자 오케스트레이터가
      `if stage == "risk"` 식 특례를 들고 있어야 했고, 부서가 워커를 추가하면
      그 특례를 손으로 고치기 전까지 새 워커가 조용히 빠졌다. 계약을 하나로 만든다.
    """

    worker_id: str
    role: str
    tools: tuple[str, ...]
    trigger: str = "always"
    input_fields: tuple[str, ...] = ()
    output_contract: str = "worker-context.v1"
    max_attempts: int = 3
    prompt_instructions: str = ""
    # 부서 Profile 버전. Risk 가 worker-context 에 실어 보낸다.
    profile_version: str = ""
    # 이 Worker 가 통과해야 하는 Skill 계약 ID 목록(guard/rag/verify/audit 계열).
    skill_ids: tuple[str, ...] = ()
    # 부서별 기술 프로필. 타입을 Any 로 둔다 - 정의가
    # departments/risk_qa_worker_profiles.py 에 있어 여기서 import 하면
    # 공용 런타임이 특정 부서에 의존하게 된다.
    tech_profile: Any = None
    # 이 Worker 가 스스로 query_mode 를 판단해야 하는가(Risk 전용, §11).
    route_query_mode: bool = False


def model_name() -> str:
    """Return the temporary low-memory Worker model; Hermes Head is separate."""
    return os.getenv("OLLAMA_CHAT_MODEL") or "qwen3:1.7b"


def _ollama_base_url() -> str:
    raw = (os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434/v1").rstrip("/")
    return raw if raw.endswith("/v1") else f"{raw}/v1"


def default_worker_llm(system: str, prompt: str) -> str:
    """Call the configured local Ollama OpenAI-compatible endpoint."""

    from openai import OpenAI

    client = OpenAI(
        api_key=os.getenv("OLLAMA_API_KEY", "ollama"),
        base_url=_ollama_base_url(),
        timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "8")),
    )
    started = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=model_name(),
            temperature=0,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        )
        record_llm_call(
            usage=getattr(response, "usage", None),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return str(response.choices[0].message.content or "")
    except Exception:
        record_llm_call(latency_ms=int((time.perf_counter() - started) * 1000), error=True)
        raise


def _compact(value: Any, limit: int = 8000) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return raw[:limit]


def run_coroutine_sync(coroutine: Any) -> Any:
    """Bridge an async LangGraph boundary for legacy synchronous callers.

    Department adapters are still exposed as synchronous functions because the
    Paper pipeline and a few CLI entrypoints use that contract.  The actual
    Worker execution remains asynchronous.  When a caller already owns an
    event loop, run the coroutine in a short-lived helper thread instead of
    falling back to the old sequential implementation or raising
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: list[Any] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:  # noqa: BLE001 - preserve boundary error.
            error.append(exc)

    thread = threading.Thread(target=run, name="worker-async-bridge", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


def context_tool(tool_name: str, input_fields: tuple[str, ...] = ()) -> WorkerTool:
    """Create a read-only tool adapter that exposes only selected input fields."""

    def read_context(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if input_fields:
            evidence = {field: payload.get(field) for field in input_fields if field in payload}
        else:
            evidence = dict(payload)
        return {"tool": tool_name, "evidence": evidence}

    return read_context


def tools_for_specs(specs: tuple[WorkerSpec, ...]) -> dict[str, WorkerTool]:
    """Build one read-only adapter per Worker from its allow-listed contract."""

    def make_tool(spec: WorkerSpec) -> WorkerTool:
        def read_context(payload: Mapping[str, Any]) -> Mapping[str, Any]:
            evidence = {field: payload.get(field) for field in spec.input_fields if field in payload}
            if not evidence:
                evidence = {"context_present": bool(payload)}
            return {"worker_id": spec.worker_id, "tools": list(spec.tools), "evidence": evidence}

        return read_context

    return {spec.worker_id: make_tool(spec) for spec in specs}


def _parse_output(raw: str, worker_id: str) -> tuple[dict[str, Any], bool]:
    candidate = raw.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return (
            {
                "worker_id": worker_id,
                "summary": candidate[:4000] or "empty_worker_output",
                "confidence": 0.0,
                "evidence_refs": [],
                "escalate": True,
                "schema_valid": False,
            },
            False,
        )
    if not isinstance(parsed, dict) or not isinstance(parsed.get("summary"), str):
        return ({"worker_id": worker_id, "summary": "invalid_worker_schema", "confidence": 0.0, "evidence_refs": [], "escalate": True, "schema_valid": False}, False)
    refs = parsed.get("evidence_refs", [])
    confidence = parsed.get("confidence")
    escalate = parsed.get("escalate", False)
    # ▶ 여기 규칙은 orchestration/contracts/mas.py 의 WorkerContextOutput 과
    #   **똑같아야 한다** (2026-08-12).
    #
    #   전에는 이 검사가 느슨해서(숫자면 통과, None 통과, 근거 없이 escalate=False 통과)
    #   워커 그래프는 COMPLETED 를 내고 **부서 경계의 Pydantic 이 나중에 거부**했다.
    #   결과가 둘이었다:
    #     1. 재시도(max_attempts)가 아예 안 걸린다 - 여기서 valid 였으니 다시 물어볼
    #        기회를 못 쓰고 그대로 확정한다.
    #     2. 실패가 `worker_context_contract_invalid:ValidationError` 로만 보여
    #        **어느 필드가 왜 틀렸는지 알 수 없다.**
    #   실측(qwen3:1.7b)에서 confidence=90 이 여기를 통과하고 경계에서 죽었다.
    valid = (
        isinstance(refs, list)
        and all(isinstance(item, str) for item in refs)
        # confidence 는 0~1 실수다. None 도 허용하지 않는다(계약이 float 필수).
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and 0.0 <= float(confidence) <= 1.0
        and isinstance(escalate, bool)
        # 근거가 없으면 반드시 escalate 다(WorkerContextOutput.require_evidence_or_escalation).
        and (bool(refs) or escalate)
    )
    output = {
        "worker_id": worker_id,
        "summary": parsed["summary"][:4000],
        "confidence": confidence,
        "evidence_refs": refs,
        "escalate": parsed.get("escalate", True),
        "schema_valid": valid,
    }
    if "proposal" in parsed:
        output["proposal"] = parsed["proposal"]
    return output, valid


def build_independent_worker_graph(spec: WorkerSpec, tool: WorkerTool, llm: WorkerLLM | None = None):
    """Compile one independent ``tool → worker LLM → validate`` graph."""

    def read_tool(state: WorkerState) -> dict[str, Any]:
        return {"tool_output": dict(tool(state.get("input", {})))}

    def call_llm(state: WorkerState) -> dict[str, Any]:
        worker_llm = llm or default_worker_llm
        # ▶ 필드의 **타입과 범위를 프롬프트에 명시한다** (2026-08-12).
        #   전에는 "Return JSON with summary, confidence, evidence_refs, and escalate"
        #   로만 적었다. 그러면 모델이 타입을 추측하고, 실측에서 **네 모델이 전부**
        #   틀렸다: qwen3:1.7b/gemma3:12b/deepseek-r1:14b 는 confidence 를
        #   "high"·"medium" 같은 낱말로, exaone3.5:7.8b 는 escalate 를 dict 로 냈다.
        #   계약(WorkerContextOutput)은 confidence float 0~1, escalate bool 을
        #   요구하므로 전부 DEGRADED 가 됐고, 원인이 모델 크기처럼 보였다.
        #   크기 문제가 아니라 **지시 누락**이었다.
        system = (
            f"You are the {spec.role}. You are one employee Worker in a department. "
            "Use only the supplied tool evidence. Produce advisory context only. "
            "Never place orders, approve risk, change limits, post ledger entries, "
            "change QA findings, grant permissions, or claim missing evidence.\n"
            "Return ONLY a JSON object with exactly these fields:\n"
            '  "summary": string — one paragraph, no markdown\n'
            '  "confidence": number between 0 and 1 (e.g. 0.75). '
            "NOT a word like \"high\"/\"medium\"/\"low\", NOT a percentage like 90.\n"
            '  "evidence_refs": array of strings identifying what you used '
            '(e.g. ["mandate:max_symbol_weight"]). Use [] if you had none.\n'
            '  "escalate": boolean true or false — NOT an object, NOT a string.\n'
            "If evidence_refs is empty you MUST set escalate to true. "
            "Do not wrap the JSON in code fences and do not add any other field."
        )
        if spec.prompt_instructions:
            system = f"{system}\nAdditional Worker contract: {spec.prompt_instructions}"
        prompt = (
            f"Worker id: {spec.worker_id}\n"
            f"Allowed tools: {', '.join(spec.tools)}\n"
            f"Output contract: {spec.output_contract}\n"
            f"Evidence:\n{_compact(state.get('tool_output', {}))}"
        )
        errors: list[str] = []
        for attempt in range(1, spec.max_attempts + 1):
            try:
                output, valid = _parse_output(worker_llm(system, prompt), spec.worker_id)
                if valid:
                    return {"output": output, "status": "COMPLETED", "attempts": attempt, "error": None}
                errors.append("worker_output_schema_invalid")
            except Exception as exc:  # noqa: BLE001 - employee boundary is fail-closed.
                errors.append(type(exc).__name__)
        return {
            "output": {"worker_id": spec.worker_id, "summary": "worker_llm_unavailable_or_invalid", "confidence": 0.0, "evidence_refs": [], "escalate": True, "schema_valid": False},
            "status": "DEGRADED",
            "attempts": spec.max_attempts,
            "error": ";".join(errors[-3:]),
        }

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


def should_run(spec: WorkerSpec, payload: Mapping[str, Any]) -> bool:
    if spec.trigger == "always":
        return True
    value = payload.get(spec.trigger)
    if isinstance(value, bool):
        return value
    return value not in (None, "", [], {})


def _resolve_worker_llm(spec: WorkerSpec, llm: WorkerLLM | None,
                        llm_factory: WorkerLLMFactory | None) -> WorkerLLM | None:
    """워커 하나가 쓸 LLM 을 확정한다. factory 가 있으면 factory 가 이긴다.

    factory 실패는 여기서 삼키지 않는다 - 호출부의 fail-closed 경로
    (DEGRADED + error)로 그대로 올라가야 한다.
    """
    if llm_factory is not None:
        return llm_factory(spec.worker_id)
    return llm


def _run_worker_registry_sequential(
    specs: tuple[WorkerSpec, ...],
    payload: Mapping[str, Any],
    *,
    tools: Mapping[str, WorkerTool],
    llm: WorkerLLM | None = None,
    llm_factory: WorkerLLMFactory | None = None,
) -> dict[str, Any]:
    """Run eligible independent employee graphs and return non-binding context."""

    input_hash = hashlib.sha256(_compact(payload).encode("utf-8")).hexdigest()
    reports: list[dict[str, Any]] = []
    not_executed: list[str] = []
    for spec in specs:
        if not should_run(spec, payload):
            not_executed.append(spec.worker_id)
            continue
        tool = tools.get(spec.worker_id)
        if tool is None:
            reports.append({"worker_id": spec.worker_id, "status": "DEGRADED", "error": "tool_not_registered", "tools": list(spec.tools), "output_contract": spec.output_contract, "input_hash": input_hash})
            continue
        try:
            worker_llm = _resolve_worker_llm(spec, llm, llm_factory)
        except Exception as exc:  # noqa: BLE001 - 모델 좌표 해석 실패는 fail-closed
            reports.append({"worker_id": spec.worker_id, "role": spec.role, "tools": list(spec.tools), "status": "DEGRADED", "attempts": 0, "output": {}, "error": f"llm_factory:{type(exc).__name__}", "output_contract": spec.output_contract, "input_hash": input_hash})
            continue
        state = build_independent_worker_graph(spec, tool, worker_llm).invoke({"worker_id": spec.worker_id, "input": dict(payload)})
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
    failed = [item["worker_id"] for item in reports if item["status"] != "COMPLETED"]
    return {
        "runtime": {"executor": "LangGraph", "topology": "async_fan_out_fan_in_independent_graphs", "provider": "ollama", "model": model_name(), "max_retries": 2, "max_attempts": 3},
        "workers": reports,
        "executed": [item["worker_id"] for item in reports if item["status"] == "COMPLETED"],
        "failed": failed,
        "not_executed": not_executed,
        "degraded": bool(failed),
        "input_hash": input_hash,
        "binding": False,
    }


def run_worker_registry(
    specs: tuple[WorkerSpec, ...],
    payload: Mapping[str, Any],
    *,
    tools: Mapping[str, WorkerTool],
    llm: WorkerLLM | None = None,
    llm_factory: WorkerLLMFactory | None = None,
) -> dict[str, Any]:
    """Synchronous compatibility boundary for async fan-out/fan-in execution."""

    return run_coroutine_sync(
        run_worker_registry_async(specs, payload, tools=tools, llm=llm,
                                  llm_factory=llm_factory)
    )


async def run_worker_registry_async(
    specs: tuple[WorkerSpec, ...],
    payload: Mapping[str, Any],
    *,
    tools: Mapping[str, WorkerTool],
    llm: WorkerLLM | None = None,
    llm_factory: WorkerLLMFactory | None = None,
) -> dict[str, Any]:
    """Run Worker graphs with LangGraph async fan-out/fan-in.

    The synchronous registry remains for legacy self-checks. New
    cross-department orchestration uses this boundary so every compiled Worker
    graph is invoked with ``await app.ainvoke(...)``.
    """

    input_hash = hashlib.sha256(_compact(payload).encode("utf-8")).hexdigest()
    not_executed = [spec.worker_id for spec in specs if not should_run(spec, payload)]
    eligible = [spec for spec in specs if should_run(spec, payload)]

    async def run_one(spec: WorkerSpec) -> dict[str, Any]:
        tool = tools.get(spec.worker_id)
        if tool is None:
            return {
                "worker_id": spec.worker_id,
                "status": "DEGRADED",
                "error": "tool_not_registered",
                "tools": list(spec.tools),
                "output_contract": spec.output_contract,
                "input_hash": input_hash,
            }
        try:
            app = build_independent_worker_graph(
                spec, tool, _resolve_worker_llm(spec, llm, llm_factory))
            state = await app.ainvoke({"worker_id": spec.worker_id, "input": dict(payload)})
            return {
                "worker_id": spec.worker_id,
                "role": spec.role,
                "tools": list(spec.tools),
                "status": state.get("status", "DEGRADED"),
                "attempts": state.get("attempts", 0),
                "output": state.get("output", {}),
                "error": state.get("error"),
                "output_contract": spec.output_contract,
                "input_hash": input_hash,
            }
        except Exception as exc:  # noqa: BLE001 - worker boundary fails closed.
            return {
                "worker_id": spec.worker_id,
                "role": spec.role,
                "tools": list(spec.tools),
                "status": "DEGRADED",
                "attempts": 0,
                "output": {
                    "worker_id": spec.worker_id,
                    "summary": "Worker graph failed; human review is required.",
                    "confidence": 0.0,
                    "evidence_refs": [],
                    "escalate": True,
                    "schema_valid": False,
                },
                "error": type(exc).__name__,
                "output_contract": spec.output_contract,
                "input_hash": input_hash,
            }

    # asyncio.gather preserves input order, making fan-in reproducible.
    reports = list(await asyncio.gather(*(run_one(spec) for spec in eligible)))
    failed = [item["worker_id"] for item in reports if item["status"] != "COMPLETED"]
    return {
        "runtime": {
            "executor": "LangGraph",
            "topology": "async_fan_out_fan_in_independent_graphs",
            "provider": "ollama",
            "model": model_name(),
            "max_retries": 2,
            "max_attempts": 3,
        },
        "workers": reports,
        "executed": [item["worker_id"] for item in reports if item["status"] == "COMPLETED"],
        "failed": failed,
        "not_executed": not_executed,
        "degraded": bool(failed),
        "input_hash": input_hash,
        "binding": False,
    }
