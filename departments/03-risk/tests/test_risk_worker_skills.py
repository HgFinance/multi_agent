"""Risk Skill boundary tests; no Ollama, database, or network required."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

RISK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RISK_DIR))

import risk_employee_workers  # noqa: E402,F401 - loads the isolated skill package.
from risk_worker_skill_runtime.contracts import hash_payload  # noqa: E402
from risk_worker_skill_runtime.guards import (  # noqa: E402
    build_context,
    freshness_check,
    scope_check,
)
from risk_worker_skill_runtime.rag_router import choose_rag_route  # noqa: E402
from risk_worker_skill_runtime.tools import invoke_tool  # noqa: E402
from risk_worker_skill_runtime.trace import SkillTrace  # noqa: E402


def test_context_hash_and_scope_are_deterministic():
    payload = {
        "case_id": "case-1",
        "as_of": "2026-08-04T00:00:00Z",
        "allowed_scopes": ["risk.case.check"],
    }
    context = build_context(payload, worker_id="pre-trade-risk-worker")
    assert context.input_hash == hash_payload(payload)
    assert scope_check(context, "risk.case.check").status == "COMPLETED"
    denied = scope_check(context, "ledger.write")
    assert denied.status == "ESCALATE"
    assert denied.error_code == "SCOPE_DENIED"


def test_missing_scope_is_denied_fail_closed():
    context = build_context({}, worker_id="pre-trade-risk-worker")

    denied = scope_check(context, "risk.case.check")

    assert denied.status == "ESCALATE"
    assert denied.error_code == "SCOPE_DENIED"


def test_pit_freshness_rejects_future_and_stale_data():
    context = build_context(
        {"as_of": "2026-08-04T00:00:00Z"}, worker_id="market-liquidity-worker"
    )
    future = freshness_check(
        context, "2026-08-04T00:00:01Z", max_age_seconds=3600
    )
    stale = freshness_check(
        context, "2026-08-03T00:00:00Z", max_age_seconds=3600
    )
    assert future.error_code == "FUTURE_INPUT"
    assert stale.error_code == "STALE_INPUT"


def test_tool_exception_escalates_without_llm_fallback():
    context = build_context({}, worker_id="pre-trade-risk-worker")
    invocation = invoke_tool(
        lambda _payload: (_ for _ in ()).throw(RuntimeError("down")),
        {},
        context,
        tool_name="risk.case.check",
    )
    assert invocation.result.status == "ESCALATE"
    assert invocation.result.escalate is True
    assert invocation.result.tool_calls == ["risk.case.check"]


def test_rag_route_and_trace_manifest_are_bounded():
    plan = choose_rag_route(
        {"query": "check compliance policy citation and PIT evidence"},
        worker_id="compliance-policy-worker",
    )
    assert plan.route == "HYBRID"
    assert plan.max_chunks <= 20
    context = build_context(
        {"as_of": datetime.now(timezone.utc)}, worker_id="compliance-policy-worker"
    )
    trace = SkillTrace()
    result = scope_check(context, "risk.compliance.check")
    trace.record(context, result, latency_ms=1)
    manifest = trace.manifest(context)
    assert manifest["input_hash"] == context.input_hash
    assert manifest["events"][0]["skill_id"] == "guard.scope_check.v1"
