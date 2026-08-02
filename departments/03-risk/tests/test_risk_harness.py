from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for module_name in tuple(sys.modules):
    if module_name == "harness" or module_name.startswith("harness."):
        sys.modules.pop(module_name, None)

from harness import DepartmentHarness, HarnessDecision
from harness.manifest import RISK_SKILLS
from harness.redis_check import check_redis_urls


def test_risk_harness_blocks_forbidden_tools_and_secrets() -> None:
    harness = DepartmentHarness(RISK_SKILLS)
    blocked = harness.execute(
        "risk.pre_trade.check",
        trace_id="trace-1",
        payload={"order": "x"},
        tool_name="oms.submit",
        handler=lambda _: {"verdict": "approve"},
    )
    assert blocked.decision is HarnessDecision.BLOCKED
    secret = harness.execute(
        "risk.pre_trade.check",
        trace_id="trace-1",
        payload={"api_key": "never-log"},
        handler=lambda _: {"verdict": "approve"},
    )
    assert secret.decision is HarnessDecision.BLOCKED


def test_risk_harness_fallback_and_grounding_are_fail_closed() -> None:
    harness = DepartmentHarness(RISK_SKILLS)
    failed = harness.execute("risk.pre_trade.check", trace_id="trace-2", payload={}, handler=lambda _: 1)
    assert failed.fallback_used is True
    assert failed.output["verdict"] == "reject"
    rag = harness.execute("risk.compliance.check", trace_id="trace-3", payload={}, handler=lambda _: {"grounded": False})
    assert rag.decision is HarnessDecision.ESCALATE


def test_redis_health_check_never_requires_or_exposes_a_secret() -> None:
    result = check_redis_urls({})
    assert result["ready"] is False
    assert result["secret_values_exposed"] is False
    assert result["checks"][0]["error_class"] == "REDIS_URL_MISSING"


def test_risk_harness_retries_twice_then_returns_success() -> None:
    harness = DepartmentHarness(RISK_SKILLS)
    calls = 0

    def flaky_handler(_: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("transient")
        return {"verdict": "reject", "trading_state": "HALTED"}

    result = harness.execute(
        "risk.pre_trade.check",
        trace_id="trace-retry",
        payload={},
        handler=flaky_handler,
    )

    assert calls == 3
    assert result.decision is HarnessDecision.READY
