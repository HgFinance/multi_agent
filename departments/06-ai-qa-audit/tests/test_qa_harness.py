from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for module_name in tuple(sys.modules):
    if module_name == "harness" or module_name.startswith("harness."):
        sys.modules.pop(module_name, None)

from harness import DepartmentHarness, HarnessDecision
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
