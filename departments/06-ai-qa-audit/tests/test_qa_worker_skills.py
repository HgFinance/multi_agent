"""AI-QA Skill boundary tests; no Ollama, database, or network required."""

from __future__ import annotations

import sys
from pathlib import Path

QA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QA_DIR))

import qa_employee_workers  # noqa: E402,F401 - loads the isolated skill package.
from qa_worker_skill_runtime.guards import build_context, pit_check, scope_check  # noqa: E402
from qa_worker_skill_runtime.rag_router import choose_rag_route  # noqa: E402
from qa_worker_skill_runtime.tools import invoke_tool  # noqa: E402
from qa_worker_skill_runtime.trace import SkillTrace  # noqa: E402


def test_scope_denial_is_escalation():
    context = build_context(
        {"allowed_scopes": ["qa.evidence.check"]}, worker_id="evidence-qa-worker"
    )
    result = scope_check(context, "ledger.write")
    assert result.status == "ESCALATE"
    assert result.error_code == "SCOPE_DENIED"


def test_pit_check_rejects_future_evidence_and_missing_time():
    context = build_context(
        {"as_of": "2026-08-04T00:00:00Z"}, worker_id="evidence-qa-worker"
    )
    assert pit_check(context, None).error_code == "MISSING_OBSERVED_AT"
    assert (
        pit_check(context, "2026-08-04T00:00:01Z").error_code
        == "FUTURE_EVIDENCE"
    )


def test_tool_exception_is_inconclusive_and_escalates():
    context = build_context({}, worker_id="evidence-qa-worker")
    invocation = invoke_tool(
        lambda _payload: (_ for _ in ()).throw(RuntimeError("down")),
        {},
        context,
        tool_name="qa.evidence.check",
    )
    assert invocation.result.status == "ESCALATE"
    assert invocation.result.escalate is True


def test_graph_route_and_trace_are_explicit():
    plan = choose_rag_route(
        {"claim": "unsupported citation contradicts entity relationship"},
        worker_id="hallucination-critic-worker",
    )
    assert plan.route == "GRAPH"
    context = build_context({}, worker_id="hallucination-critic-worker")
    trace = SkillTrace()
    result = scope_check(context, "qa.evidence.rag")
    trace.record(context, result)
    assert trace.manifest(context)["events"][0]["status"] == "COMPLETED"
