"""Offline deterministic single-vs-multi orchestration smoke replay.

Run with::

    python -m benchmarks.multi_agent_smoke_replay

This is a test-only replay, not a quality, cost, provider-latency, or broker
benchmark.  It uses fixed fake evidence/tool responses and the existing
conditional PAPER admission owner for the conditional-rule scenario.  No
network, model, observability, broker, Redis, PostgreSQL, or result-file
boundary is reachable from this module.
"""

from __future__ import annotations

import statistics
import time
from contextlib import ExitStack
from dataclasses import dataclass
from enum import Enum
from typing import Any
from unittest.mock import patch

from apps.api import conditional_rule_orchestrator as conditional_orchestrator
from apps.api import conditional_rules as conditional_rules_module
from apps.api import user_order_orchestrator
from apps.api.conditional_rules import ConditionalRuleCandidate
from apps.api.conditional_rule_workflow import InMemoryConditionalRuleRepository
from apps.api.user_order_workflow import InMemoryUserOrderRequestRepository
from orchestration.ceo_workflow_scope import (
    UserPaperOrderScope,
    build_scoped_task_body,
    build_user_paper_order_scope,
)
REPETITIONS = 5
RAW_CONDITIONAL = "삼성전자 5분봉 RSI(14)가 30 이하면 2주 시장가 매수"
SCENARIOS = ("evidence", "conditional_paper", "isolated_failure")
MODES = ("single_mode", "multi_mode")


class Status(str, Enum):
    SUCCESS = "success"
    FAILED_CLOSED = "failed_closed"


@dataclass(frozen=True)
class RunMetric:
    mode: str
    scenario: str
    wall_clock_ms: float
    status: Status
    isolated_failure: bool
    fake_tool_calls: int
    handoffs: int
    forbidden_side_effect_count: int
    output_contract_valid: bool
    temporary_artifact_cleanup: bool


class FakeReplay:
    """Counts only local fake boundaries; it has no external adapter."""

    def __init__(self, *, failure_role: str | None = None) -> None:
        self.failure_role = failure_role
        self.fake_tool_calls = 0
        self.handoffs = 0
        self.forbidden_side_effect_count = 0

    def call_role(self, role: str, fixture: dict[str, Any]) -> dict[str, Any]:
        self.fake_tool_calls += 1
        # The same fake latency is applied to every mode and role.
        time.sleep(0.0001)
        if role == self.failure_role:
            raise RuntimeError(f"deterministic fake failure: {role}")
        return dict(fixture)

    def handoff(self, _from: str, _to: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.handoffs += 1
        return dict(payload)


def _contract(result: dict[str, Any]) -> bool:
    return (
        isinstance(result, dict)
        and result.get("schema_version") == "offline-smoke-result.v1"
        and result.get("status") in {Status.SUCCESS.value, Status.FAILED_CLOSED.value}
        and isinstance(result.get("evidence_refs"), list)
        and isinstance(result.get("reason"), str)
    )


def _fixed_evidence() -> dict[str, Any]:
    return {"evidence_refs": ["fixture:evidence-001"], "summary": "fixed evidence"}


def _run_evidence(mode: str, replay: FakeReplay) -> dict[str, Any]:
    fixture = _fixed_evidence()
    try:
        if mode == "single_mode":
            result = replay.call_role("ceo", fixture)
        else:
            result = replay.handoff("ceo", "research", replay.call_role("research", fixture))
            result = replay.handoff("research", "risk", replay.call_role("risk", result))
            result = replay.handoff("risk", "qa", replay.call_role("qa", result))
        return {"schema_version": "offline-smoke-result.v1", "status": "success", **result, "reason": "evidence synthesized"}
    except RuntimeError as exc:
        return {"schema_version": "offline-smoke-result.v1", "status": "failed_closed", "evidence_refs": [], "reason": str(exc)}


def _candidate() -> ConditionalRuleCandidate:
    return ConditionalRuleCandidate.model_validate(
        {
            "symbol": "삼성전자",
            "condition": {
                "type": "COMPARISON", "operator": "LTE",
                "left": {"type": "INDICATOR", "name": "RSI", "timeframe": "5M", "parameters": {"period": 14}},
                "right": {"type": "LITERAL", "value": "30", "unit": "NUMBER"},
            },
            "action": {"side": "BUY", "sizing": {"type": "FIXED_SHARES", "value": "2"}, "order_type": "MARKET"},
            "evaluation": {"clock": "BAR_CLOSE", "primary_timeframe": "5M"},
        }
    )


def _paper_replay(replay: FakeReplay) -> dict[str, Any]:
    """Call the existing admission/AST owner against only in-memory fakes."""

    user_id = "11111111-1111-4111-8111-111111111111"
    fund_id = "22222222-2222-4222-8222-222222222222"
    book_id = "33333333-3333-4333-8333-333333333333"
    root_id, trading_id = "t_smokeRoot", "t_smokeTrade"
    orders = InMemoryUserOrderRequestRepository()
    rules = InMemoryConditionalRuleRepository()
    admission = orders.admit(
        user_id=user_id, fund_id=fund_id, book_id=book_id,
        client_request_id="offline-smoke-conditional", raw_instruction=RAW_CONDITIONAL,
    )
    orders.bind_root(admission.order_request_id, root_id)
    orders.bind_trading_task(admission.order_request_id, trading_id)
    scope = UserPaperOrderScope(
        order_request_id=admission.order_request_id,
        raw_instruction_sha256=admission.raw_instruction_sha256,
        fund_id=fund_id, book_id=book_id,
    )
    scope_text = build_user_paper_order_scope(scope)
    root_body = build_scoped_task_body(
        f"{scope_text}\nrequested_by={user_id}\n\n## User request\n{RAW_CONDITIONAL}", root_id,
        role="synthesis", workflow_mode="binding",
    )
    trading_body = build_scoped_task_body(
        scope_text, root_id, role="primary", workflow_mode="binding",
    )
    tasks = {
        root_id: {"id": root_id, "status": "running", "assignee": "ceo-agent", "body": root_body},
        trading_id: {"id": trading_id, "status": "running", "assignee": "trading-department", "body": trading_body},
    }
    with ExitStack() as stack:
        stack.enter_context(patch.object(conditional_orchestrator, "user_order_repository", lambda: orders))
        stack.enter_context(patch.object(conditional_orchestrator, "conditional_rule_repository", lambda: rules))
        stack.enter_context(patch.object(user_order_orchestrator.hermes_boundary, "show_kanban_task", lambda task_id, **_: tasks.get(task_id)))
        stack.enter_context(patch.object(conditional_rules_module, "require_trading_book_access", lambda *_: {"user_id": user_id, "fund_id": fund_id, "book_id": book_id}))
        stack.enter_context(patch.object(conditional_rules_module, "resolve_active_trading_instrument", lambda *_: {"instrument_id": "44444444-4444-4444-8444-444444444444", "symbol": "005930"}))
        result = conditional_orchestrator.process_user_conditional_paper_rule(
            root_task_id=root_id, trading_task_id=trading_id, candidate=_candidate(),
            interpretation_source="DETERMINISTIC",
        )
    # This scenario stops at the existing PAPER admission boundary; no submit is called.
    replay.fake_tool_calls += 1
    return {
        "schema_version": "offline-smoke-result.v1",
        "status": "success" if result.get("rule_active") else "failed_closed",
        "evidence_refs": ["fixture:conditional-ast"],
        "reason": (
            "existing conditional PAPER admission and AST validation"
            if result.get("rule_active")
            else ",".join(str(code) for code in result.get("reason_codes", []))
        ),
    }


def _run_conditional(mode: str, replay: FakeReplay) -> dict[str, Any]:
    if mode == "multi_mode":
        for role in ("research", "risk", "qa"):
            replay.call_role(role, {"evidence_refs": ["fixture:conditional-ast"]})
            replay.handoffs += 1
    return _paper_replay(replay)


def _run_failure(mode: str, replay: FakeReplay) -> dict[str, Any]:
    try:
        if mode == "single_mode":
            replay.call_role("ceo", _fixed_evidence())
        else:
            result = replay.call_role("research", _fixed_evidence())
            replay.handoffs += 1
            result = replay.call_role("risk", result)
            replay.handoffs += 1
            replay.call_role("qa", result)
        raise AssertionError("failure fixture was not consumed")
    except RuntimeError as exc:
        return {"schema_version": "offline-smoke-result.v1", "status": "failed_closed", "evidence_refs": [], "reason": str(exc)}


def run_once(mode: str, scenario: str) -> RunMetric:
    replay = FakeReplay(failure_role="research" if scenario == "isolated_failure" and mode == "multi_mode" else "ceo" if scenario == "isolated_failure" else None)
    started = time.perf_counter()
    with __import__("tempfile").TemporaryDirectory() as _temporary_directory:
        if scenario == "evidence":
            result = _run_evidence(mode, replay)
        elif scenario == "conditional_paper":
            result = _run_conditional(mode, replay)
        else:
            result = _run_failure(mode, replay)
    cleanup = not __import__("os").path.exists(_temporary_directory)
    status = Status(result["status"])
    return RunMetric(mode, scenario, (time.perf_counter() - started) * 1000, status, scenario == "isolated_failure" and mode == "multi_mode", replay.fake_tool_calls, replay.handoffs, replay.forbidden_side_effect_count, _contract(result), cleanup)


def run_benchmark() -> list[RunMetric]:
    return [run_once(mode, scenario) for scenario in SCENARIOS for mode in MODES for _ in range(REPETITIONS)]


def format_report(metrics: list[RunMetric]) -> str:
    lines = [
        "offline deterministic smoke benchmark, n=5; p95 미산출",
        "고정 fixture 기반 orchestration 비교이며 실제 provider latency·LLM 품질·비용의 비교가 아니다.",
        "실토큰·실비용: NOT_MEASURED: offline fake replay",
        "",
        "mode | scenario | status(success/failed_closed) | wall mean/p50/min/max ms | calls | handoffs | isolated",
    ]
    for scenario in SCENARIOS:
        for mode in MODES:
            rows = [item for item in metrics if item.scenario == scenario and item.mode == mode]
            values = [item.wall_clock_ms for item in rows]
            statuses = "/".join(str(sum(item.status == status for item in rows)) for status in (Status.SUCCESS, Status.FAILED_CLOSED))
            lines.append(f"{mode} | {scenario} | {statuses} | {statistics.mean(values):.3f}/{statistics.median(values):.3f}/{min(values):.3f}/{max(values):.3f} | {rows[0].fake_tool_calls} | {rows[0].handoffs} | {rows[0].isolated_failure}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_report(run_benchmark()))
