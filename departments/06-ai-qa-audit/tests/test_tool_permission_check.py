"""tool_permission_check.py의 __main__ 자체 점검을 pytest로 옮긴 것.

소유: 동규 (AI QA/감사본부). repository 없는 기본 TraceRecorder()를 쓰므로 DB 의존이
없다 - 원본과 동일하게 6개 시나리오를 검증한다.

실행: python -m pytest departments/06-ai-qa-audit/tests/test_tool_permission_check.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "audit"))

from tool_permission_check import (  # noqa: E402
    AgentToolPolicy,
    ToolPermissionResult,
    check_tool_permission,
    count_unauthorized_calls,
    record_and_check_tool_call,
)
from trace_recorder import ToolCallStatus, TraceRecorder  # noqa: E402

now = datetime.now(timezone.utc)
trace, agent, profile = uuid4(), uuid4(), uuid4()
policy = AgentToolPolicy(
    agent_id=agent, profile_version_id=profile,
    allowed_tools=frozenset({"market-api", "portfolio-api"}),
)


def test_01_allowlisted_tool_allowed():
    check_ok = check_tool_permission(policy, "market-api")
    assert check_ok.result is ToolPermissionResult.ALLOWED
    assert check_ok.reason == ""


def test_02_non_allowlisted_tool_denied_with_reason():
    check_bad = check_tool_permission(policy, "broker-adapter-submit")
    assert check_bad.result is ToolPermissionResult.DENIED
    assert "broker-adapter-submit" in check_bad.reason


def test_03_integration_allowed_tool_recorded_as_allowed():
    recorder = TraceRecorder()
    run = recorder.start_run(trace, agent, profile, "hash_perm_1", started_at=now)
    allowed_call = record_and_check_tool_call(
        recorder, run.agent_run_id, policy, "market-api", {"symbol": "AAPL"}, "call_h1",
    )
    assert allowed_call.status is ToolCallStatus.ALLOWED
    assert not allowed_call.is_terminal, "ALLOWED는 아직 완료 전이라 종결 상태가 아니어야 함"


def test_04_integration_denied_tool_recorded_as_terminal_denied():
    recorder = TraceRecorder()
    run = recorder.start_run(trace, agent, profile, "hash_perm_1", started_at=now)
    denied_call = record_and_check_tool_call(
        recorder, run.agent_run_id, policy, "broker-adapter-submit", {}, "call_h2",
    )
    assert denied_call.status is ToolCallStatus.DENIED
    assert denied_call.is_terminal, "DENIED는 종결 상태여야 함"
    assert denied_call.error_code is not None and "Allowlist" in denied_call.error_code


def test_05_unauthorized_call_count_only_counts_denied():
    recorder = TraceRecorder()
    run = recorder.start_run(trace, agent, profile, "hash_perm_1", started_at=now)
    allowed_call = record_and_check_tool_call(
        recorder, run.agent_run_id, policy, "market-api", {"symbol": "AAPL"}, "call_h1",
    )
    denied_call = record_and_check_tool_call(
        recorder, run.agent_run_id, policy, "broker-adapter-submit", {}, "call_h2",
    )
    recorder.complete_tool_call(allowed_call.tool_call_id, "out_h1")
    assert count_unauthorized_calls([allowed_call, denied_call]) == 1


def test_06_run_completes_after_open_tool_calls_are_resolved():
    recorder = TraceRecorder()
    run = recorder.start_run(trace, agent, profile, "hash_perm_1", started_at=now)
    allowed_call = record_and_check_tool_call(
        recorder, run.agent_run_id, policy, "market-api", {"symbol": "AAPL"}, "call_h1",
    )
    record_and_check_tool_call(
        recorder, run.agent_run_id, policy, "broker-adapter-submit", {}, "call_h2",
    )
    recorder.complete_tool_call(allowed_call.tool_call_id, "out_h1")
    recorder.complete_run(run.agent_run_id)
