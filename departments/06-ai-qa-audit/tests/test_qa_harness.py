from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for module_name in tuple(sys.modules):
    if module_name == "harness" or module_name.startswith("harness."):
        sys.modules.pop(module_name, None)

from harness import DepartmentHarness, HarnessDecision
from harness.journal import LogEventType
from harness.manifest import QA_SKILLS


def test_qa_harness_blocks_forbidden_tools_and_secrets() -> None:
    harness = DepartmentHarness(QA_SKILLS)
    forbidden = harness.execute(
        "qa.evidence.check",
        trace_id="trace-1",
        payload={"claim": "x"},
        tool_name="oms.submit",
        handler=lambda _: {"decision": "PASS"},
    )
    assert forbidden.decision is HarnessDecision.BLOCKED
    secret = harness.execute(
        "qa.evidence.check",
        trace_id="trace-1",
        payload={"token": "never-log"},
        handler=lambda _: {"decision": "PASS"},
    )
    assert secret.decision is HarnessDecision.BLOCKED


def test_qa_harness_escalates_ungrounded_rag_and_failures() -> None:
    harness = DepartmentHarness(QA_SKILLS)
    rag = harness.execute("qa.evidence.rag", trace_id="trace-2", payload={}, handler=lambda _: {"grounded": False})
    assert rag.decision is HarnessDecision.ESCALATE
    failed = harness.execute("qa.model_risk.evaluate", trace_id="trace-3", payload={}, handler=lambda _: 1)
    assert failed.fallback_used is True
    assert failed.output["decision"] == "ESCALATE"


def test_qa_harness_retries_twice_then_returns_success() -> None:
    harness = DepartmentHarness(QA_SKILLS)
    calls = 0

    def flaky_handler(_: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("transient")
        return {"decision": "PASS", "grounded": True}

    result = harness.execute(
        "qa.evidence.check",
        trace_id="trace-retry",
        payload={},
        handler=flaky_handler,
    )

    assert calls == 3
    assert result.decision is HarnessDecision.READY


def test_qa_harness_journal_replay_and_review() -> None:
    harness = DepartmentHarness(QA_SKILLS, hermes_profile="qa-department")
    output = {
        "decision": "PASS",
        "grounded": True,
        "rationale": "claim is supported",
        "evidence_refs": ["policy-1"],
    }

    result = harness.execute(
        "qa.evidence.check",
        trace_id="trace-qa-log",
        run_id="run-qa-log",
        employee_profile="evidence-qa-agent",
        as_of="2026-08-02T00:00:00Z",
        asset="SYMBOL_A",
        model_version="qa-engine-1",
        prompt_version="prompt-1",
        parameter_version="params-1",
        payload={"claim": "supported"},
        handler=lambda _: output,
    )

    assert result.decision is HarnessDecision.READY
    events = harness.journal.events_for_run("run-qa-log")
    assert [event.event_type for event in events] == [
        LogEventType.INPUT_SNAPSHOT,
        LogEventType.AGENT_OUTPUT,
        LogEventType.VALIDATION,
        LogEventType.DECISION,
    ]
    assert all(event.hermes_profile == "qa-department" for event in events)
    assert events[1].rationale == "claim is supported"
    assert events[1].evidence_refs == ("policy-1",)
    replay = harness.journal.replay("run-qa-log", lambda _: output)
    assert replay.output_match and replay.decision_match and not replay.diffs
    review = harness.journal.review("run-qa-log")
    assert review["event_count"] == 4
    assert review["replay_ready"] is True
    assert review["fallback_rate"] == 0.0
