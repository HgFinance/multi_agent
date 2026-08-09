"""Risk department employee Workers.

직원 1명. **그중 LLM 을 쓰는 것은 1명뿐이다**(2026-08-06).

  compliance-policy-worker   Point-in-time 정책 근거(Agentic RAG) — LLM
  risk-runner                시장·유동성·파생/counterparty 잡무 — **LLM 없음.**
                              결정론 모듈(RiskEngine) 출력을 그대로 옮긴다

기존 core-risk-worker / derivatives-counterparty-worker 를 `risk-runner` 하나로
흡수했다. 둘 다 **답이 하나로 정해지는 일**이었다 - market/liquidity 판정은
`pre_trade_check()` 의 `RiskEngine.check_order()` 가 이미 verdict 로 내리고,
counterparty 판정도 같은 RiskEngine 결과의 counterparty_health CheckOutcome 이
이미 낸다. 그 위에 `qwen3:1.7b` 를 얹어봐야 이미 나온 verdict 를 다시 서술하는
것뿐이고, 그 과정에서 Risk 수치가 LLM 문장을 거치는 경로만 생긴다
(CLAUDE.md 개발 원칙 4·9번, trading 의 desk-runner 와 같은 기준).

compliance-policy-worker 만 남긴 이유는 하나다. Policy 문서 원문에 대한
관련성·인용 판단은 결정론 모듈이 대신 만들 수 없다(Agentic RAG:
retrieve→grade→generate→hallucination_check) - `grounded: false` 면 escalate 한다.

The Hermes profile is the department-head boundary.  This module owns only the
employee layer: `compliance-policy-worker` is an independently compiled
LangGraph that reads an allow-listed deterministic tool result and asks the
local Ollama model for bounded, non-binding context; `risk-runner` calls no
model at all.  Neither can approve an order or change a gate.
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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

# orchestration.employee_dispatch는 이 파일을 spec_from_file_location으로 직접 로드한다 -
# departments/03-risk가 sys.path에 없는 채로 실행될 수 있어 tools.legal_wiki_tool을
# 절대 import하기 전에 직접 등록한다(같은 파일의 _load_skill_package()와 동일한 이유).
_DEPARTMENT_DIR = Path(__file__).resolve().parent
if str(_DEPARTMENT_DIR) not in sys.path:
    sys.path.insert(0, str(_DEPARTMENT_DIR))

from tools.legal_wiki_tool import (
    LegalWikiAnswerFn,
    LegalWikiQueryInput,
    query_legal_wiki,
)

from departments.employee_worker_runtime import run_coroutine_sync
from departments.risk_qa_worker_profiles import (
    RISK_WORKER_TECH,
    WorkerTechProfile,
    tech_profile_for,
)
from orchestration.llm_observability import record_llm_call


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
    query_mode: str | None
    routing_rationale: str | None
    routing_by_llm: bool
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
    profile_version: str = "risk-profile-v1"
    max_attempts: int = 3
    skill_ids: tuple[str, ...] = ()
    tech_profile: WorkerTechProfile | None = None
    # RISK_MANDATE_WORKER_FLOW.md §11 — 이 worker가 스스로 query_mode를 판단해야 하면 True.
    # 구조화된 query_mode가 이미 입력에 있으면(§4 구조화 우선) LLM 라우팅은 건너뛴다.
    route_query_mode: bool = False


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
        "compliance-policy-worker",
        "Point-in-time policy evidence analyst",
        ("risk.compliance.check",),
        "when_compliance_evidence_exists",
        tech_profile=tech_profile_for(RISK_WORKER_TECH, "compliance-policy-worker"),
        route_query_mode=True,
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
        if state.get("query_mode") is not None:
            payload = {**payload, "query_mode": state["query_mode"]}
        try:
            context = build_context(
                payload,
                worker_id=spec.worker_id,
            profile_version=spec.profile_version,
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
            f"Technology stack: {', '.join(tech_profile.get('stack', ()))}\n"
            f"Technology usage: {'; '.join(tech_profile.get('usage', ()))}\n"
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
                    # 라우팅 결과를 그대로 흘려보낸다 - 부서장은 이 worker가 왜 그
                    # query_mode를 골랐는지도 함께 받는다(RISK_MANDATE_WORKER_FLOW.md §11).
                    output["query_mode"] = state.get("query_mode")
                    output["routing_rationale"] = state.get("routing_rationale")
                    output["routing_by_llm"] = state.get("routing_by_llm", False)
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

    def route(state: WorkerState) -> dict[str, Any]:
        worker_llm = llm or default_worker_llm
        mode, rationale, routed_by_llm = _route_query_mode(
            spec, state.get("input", {}), worker_llm=worker_llm
        )
        return {
            "query_mode": mode,
            "routing_rationale": rationale,
            "routing_by_llm": routed_by_llm,
        }

    def validate(state: WorkerState) -> dict[str, Any]:
        if state.get("status") == "COMPLETED" and state.get("output", {}).get(
            "schema_valid"
        ):
            return {}
        return {"status": "DEGRADED", "trace_manifest": _manifest(state)}

    graph = StateGraph(WorkerState)
    graph.add_node("route", route)
    graph.add_node("tool", read_tool)
    graph.add_node("worker_llm", call_llm)
    graph.add_node("validate", validate)
    graph.set_entry_point("route")
    graph.add_edge("route", "tool")
    graph.add_edge("tool", "worker_llm")
    graph.add_edge("worker_llm", "validate")
    graph.add_edge("validate", END)
    return graph.compile()


def _core_risk_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "tools": ["risk.trading_state.read", "risk.p1.snapshot", "risk.case.check"],
        "trading_state": payload.get("trading_state"),
        "p1_snapshot": payload.get("p1_snapshot", payload.get("market_snapshot")),
        "context": payload.get("context", {}),
        "assessment": payload.get("assessment", {}),
    }


# ── compliance-policy-worker의 자체 라우팅 (RISK_MANDATE_WORKER_FLOW.md §11) ──────
# 부서장이 query_mode를 구조화된 필드로 이미 보냈으면 그걸 우선한다(§4). 없고 자연어
# 질문만 있으면 이 worker의 모델(Ollama, call_llm과 동일 인스턴스)이 직접 분류한다.
_QUERY_MODES = (
    "MANDATE_REVIEW",
    "RISK_POLICY_REVIEW",
    "LEGAL_QUERY",
    "MIXED_REVIEW",
    "NOT_APPLICABLE",
)
_LEGAL_QUERY_MODES = ("LEGAL_QUERY", "MIXED_REVIEW")

_ROUTE_SYSTEM = (
    "You are the compliance-policy-worker routing classifier. Classify the compliance "
    "question into exactly one query_mode; do not judge compliance itself. "
    "MANDATE_REVIEW: 사용자 목표/위험선호/자산정책의 자연어 모호성만 검토, 법률/정책 검색 안 함. "
    "RISK_POLICY_REVIEW: 내부 Risk 정책/Restricted List만 검토. "
    "LEGAL_QUERY: 법령/행정규칙/법령해석례/판례 검색이 필요. "
    "MIXED_REVIEW: 내부 정책과 법률 근거를 함께 검토해야 함. "
    "NOT_APPLICABLE: 정책/법률 검토와 무관. "
    'Return JSON only: {"query_mode": "<one of the above>", "routing_rationale": '
    '"<one short sentence>"}.'
)


def _route_query_mode(
    spec: WorkerSpec, payload: dict[str, Any], *, worker_llm: WorkerLLM
) -> tuple[str | None, str | None, bool]:
    """Return (query_mode, routing_rationale, decided_by_llm)."""

    if not spec.route_query_mode:
        return None, None, False
    explicit = payload.get("query_mode")
    if explicit in _QUERY_MODES:
        return explicit, "structured_input_priority", False
    compliance = payload.get("compliance") or {}
    question = compliance.get("query") or compliance.get("question", "")
    if not question:
        # 구조화된 query_mode도 자연어 질문도 없으면, 이미 공급된 evidence를 다루는
        # 기존 RISK_POLICY_REVIEW 계약으로 취급한다 - 무근거로 NOT_APPLICABLE 단정 안 함.
        mode = "RISK_POLICY_REVIEW" if compliance else "NOT_APPLICABLE"
        return mode, "no_question_supplied", False
    raw = worker_llm(_ROUTE_SYSTEM, f"Question: {question}")
    try:
        candidate = raw.strip()
        if "```" in candidate:
            candidate = candidate.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(candidate)
        mode = parsed.get("query_mode")
        if mode in _QUERY_MODES:
            return (
                mode,
                str(parsed.get("routing_rationale", ""))[:500],
                bool(getattr(worker_llm, "uses_model", True)),
            )
    except Exception:  # noqa: BLE001, S110 - routing boundary fails closed.
        pass
    # 분류 실패 시 검색 범위를 줄이지 않고 가장 넓게 본다(정책+법률 모두) - 못 찾음을
    # 근거 없음으로 단정하지 않는다(RISK_MANDATE_WORKER_FLOW.md §9).
    return "MIXED_REVIEW", "routing_parse_failed_defaulted_to_mixed_review", True


def _parse_as_of(raw: Any) -> date:
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def _compliance_tool(
    payload: dict[str, Any], *, legal_answer_fn: LegalWikiAnswerFn | None = None
) -> dict[str, Any]:
    mode = payload.get("query_mode")
    compliance = payload.get("compliance") or {}
    result: dict[str, Any] = {
        "tool": "risk.compliance.check",
        "query_mode": mode,
        "compliance": compliance,
    }
    if mode in _LEGAL_QUERY_MODES:
        question = compliance.get("query") or compliance.get("question", "")
        if question:
            legal = query_legal_wiki(
                LegalWikiQueryInput(query=question, as_of=_parse_as_of(payload.get("as_of"))),
                answer_fn=legal_answer_fn,
            )
            result["legal"] = legal.model_dump(mode="json")
    return result


def _counterparty_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": "risk.trading_state.record.read",
        "counterparty": payload.get("counterparty"),
        "derivatives": payload.get("derivatives", {}),
    }


# ── risk-runner: 결정론 잡무 (LLM 없음, 2026-08-06) ──────────────────────────
# core-risk-worker(RiskEngine.check_order 서술)와 derivatives-counterparty-worker
# (counterparty_health CheckOutcome 서술)를 합쳐 흡수했다. 둘 다 답이 하나로
# 정해지는 일이었다 - market/liquidity 판정은 pre_trade_check()의
# RiskEngine.check_order()가 이미 verdict로 내리고, counterparty 판정도 같은
# RiskEngine 결과의 counterparty_health CheckOutcome이 이미 낸다
# (scripts.py의 counterparty_check()가 이미 LLM 없이 동작하는 것과 같은 원칙,
# trading의 desk-runner와 같은 기준: 답이 하나로 정해지는 일에 LLM을 얹지 않는다).
RUNNER_ID = "risk-runner"
RUNNER_ROLE = "Risk desk runner — market/liquidity/counterparty gate 결과 조회(결정론, LLM 없음)"
RUNNER_TOOLS = (
    "risk.trading_state.read",
    "risk.p1.snapshot",
    "risk.case.check",
    "risk.trading_state.record.read",
)


def risk_runner(payload: dict[str, Any]) -> dict[str, Any]:
    """시장·유동성·파생/counterparty 잡무. **모델을 부르지 않는다.**

    RiskEngine.check_order()가 이미 만든 verdict/check_results를 그대로 옮긴다 -
    `summary` 필드가 없는 것이 이 직원의 요지다.
    """
    core = _core_risk_tool(payload)
    counterparty = _counterparty_tool(payload)
    assessment = core.get("assessment") or {}
    check_results = assessment.get("check_results", [])
    verdict = assessment.get("verdict")

    blockers: list[str] = []
    if verdict and str(verdict).upper() != "APPROVE":
        blockers.append(f"risk_verdict_{str(verdict).lower()}")
    blockers.extend(
        f"check_failed:{c.get('name')}"
        for c in check_results
        if not c.get("passed", True) and c.get("name")
    )

    return {
        "worker_id": RUNNER_ID,
        "role": RUNNER_ROLE,
        "tools": list(RUNNER_TOOLS),
        "status": "COMPLETED",
        "attempts": 1,
        "llm": False,  # 이 직원은 모델을 안 부른다. 계약으로 박는다
        "output": {
            "worker_id": RUNNER_ID,
            "facts": {"core_risk": core, "counterparty": counterparty},
            "blockers": blockers,
            "escalate": bool(blockers),
            "decided_by": "deterministic",
            "authoritative": False,  # 판정은 RiskEngine이 한다. 이건 옮긴 것일 뿐이다
        },
        "error": None,
        "output_contract": "risk.risk-runner.v1",
    }


def _should_run(spec: WorkerSpec, payload: dict[str, Any]) -> bool:
    if spec.trigger == "always":
        return True
    return bool(payload.get("compliance"))


def _run_employee_workers_sequential(
    payload: dict[str, Any],
    llm: WorkerLLM | None = None,
    legal_answer_fn: LegalWikiAnswerFn | None = None
) -> dict[str, Any]:
    tools: dict[str, WorkerTool] = {
        "compliance-policy-worker": lambda value: _compliance_tool(
            value, legal_answer_fn=legal_answer_fn
        ),
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
                "technology": spec.tech_profile.as_dict() if spec.tech_profile else {},
                "skill_results": state.get("skill_results", []),
                "rag_plan": state.get("rag_plan", {}),
                "trace": state.get("trace_manifest", {}),
            }
        )
    failed = [r["worker_id"] for r in reports if r["status"] != "COMPLETED"]
    # risk-runner는 레지스트리 밖이다 - LLM Worker의 failed/degraded 판정에 섞이지 않는다.
    reports.append(risk_runner(payload))
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
    legal_answer_fn: LegalWikiAnswerFn | None = None,
) -> dict[str, Any]:
    """Fan out guarded Risk Worker graphs and deterministically fan them in."""

    tools: dict[str, WorkerTool] = {
        "compliance-policy-worker": lambda value: _compliance_tool(
            value, legal_answer_fn=legal_answer_fn
        ),
    }
    input_hash = str(
        payload.get("input_hash")
        or hashlib.sha256(_compact(payload).encode("utf-8")).hexdigest()
    )
    not_executed = [
        spec.worker_id for spec in WORKER_SPECS if not _should_run(spec, payload)
    ]
    eligible = [spec for spec in WORKER_SPECS if _should_run(spec, payload)]

    async def run_one(spec: WorkerSpec) -> dict[str, Any]:
        worker_trace = SkillTrace()
        try:
            state = await build_worker_graph(
                spec,
                tools[spec.worker_id],
                llm,
                trace=worker_trace,
            ).ainvoke({"worker_id": spec.worker_id, "input": payload})
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
                "skills": list(spec.skill_ids),
                "technology": spec.tech_profile.as_dict() if spec.tech_profile else {},
                "skill_results": state.get("skill_results", []),
                "rag_plan": state.get("rag_plan", {}),
                "trace": state.get("trace_manifest", {}),
            }
        except Exception as exc:  # noqa: BLE001 - Worker boundary fails closed.
            return {
                "worker_id": spec.worker_id,
                "role": spec.role,
                "tools": list(spec.tools),
                "status": "DEGRADED",
                "attempts": 0,
                "output": {
                    "worker_id": spec.worker_id,
                    "summary": "Risk Worker graph failed; human review is required.",
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

    reports = list(await asyncio.gather(*(run_one(spec) for spec in eligible)))
    failed = [item["worker_id"] for item in reports if item["status"] != "COMPLETED"]
    # risk-runner는 레지스트리 밖이다 - LLM Worker의 failed/degraded 판정에 섞이지 않는다.
    reports.append(risk_runner(payload))
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
    legal_answer_fn: LegalWikiAnswerFn | None = None
) -> dict[str, Any]:
    """Synchronous compatibility boundary for the async Risk fan-in."""

    return run_coroutine_sync(
        run_employee_workers_async(
            payload,
            llm=llm,
            legal_answer_fn=legal_answer_fn,
        )
    )
