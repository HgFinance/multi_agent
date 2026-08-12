"""trace_recorder.py의 __main__ 자체 점검을 pytest로 옮긴 것.

소유: 동규 (AI QA/감사본부). repository 인자 없이 TraceRecorder()를 쓰므로 DB 의존이
없다 - 원본과 동일하게 9개 시나리오를 검증한다.

실행: python -m pytest departments/06-ai-qa-audit/tests/test_trace_recorder.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "audit"))

from trace_recorder import (
    AgentRunStatus,
    ToolCallStatus,
    TraceRecorder,
    TraceRecorderError,
    as_tool_result_output_values,
)

now = datetime.now(timezone.utc)
trace, agent, profile = uuid4(), uuid4(), uuid4()


def raises(fn, why: str):
    with pytest.raises(TraceRecorderError):
        fn()


def test_01_normal_flow_run_start_tool_call_complete_run_complete():
    recorder = TraceRecorder()
    run = recorder.start_run(trace, agent, profile, "hash_1", started_at=now)
    assert run.status is AgentRunStatus.RUNNING
    call = recorder.record_tool_call(
        run.agent_run_id,
        "market-api",
        {"symbol": "AAPL"},
        "call_hash_1",
        occurred_at=now,
    )
    assert call.status is ToolCallStatus.REQUESTED
    recorder.allow_tool_call(call.tool_call_id)
    completed_call = recorder.complete_tool_call(
        call.tool_call_id,
        "out_hash_1",
        completed_at=now + timedelta(milliseconds=120),
    )
    assert completed_call.status is ToolCallStatus.COMPLETED
    assert completed_call.latency_ms == 120
    finished = recorder.complete_run(
        run.agent_run_id, ended_at=now + timedelta(seconds=1)
    )
    assert finished.status is AgentRunStatus.COMPLETED


def test_02_denied_tool_call_recorded_run_still_completes():
    recorder = TraceRecorder()
    run2 = recorder.start_run(trace, agent, profile, "hash_2", started_at=now)
    call2 = recorder.record_tool_call(
        run2.agent_run_id, "broker-adapter", {}, "call_hash_2", occurred_at=now
    )
    denied = recorder.deny_tool_call(call2.tool_call_id, "out_of_allowlist")
    assert denied.status is ToolCallStatus.DENIED
    recorder.complete_run(run2.agent_run_id)


def test_03_same_profile_input_hash_while_running_reuses_run():
    recorder = TraceRecorder()
    run3a = recorder.start_run(trace, agent, profile, "hash_3", started_at=now)
    run3b = recorder.start_run(trace, agent, profile, "hash_3", started_at=now)
    assert run3a.agent_run_id == run3b.agent_run_id, "같은 입력인데 Run이 중복 생성됨"
    assert len([r for r in recorder.runs.values() if r.input_hash == "hash_3"]) == 1


def test_04_cannot_attach_tool_call_to_terminal_run():
    recorder = TraceRecorder()
    run = recorder.start_run(trace, agent, profile, "hash_1", started_at=now)
    recorder.complete_run(run.agent_run_id)
    raises(
        lambda: recorder.record_tool_call(run.agent_run_id, "x", {}, "h"),
        "완료된 Run에 Tool Call 추가",
    )


def test_05_open_tool_call_blocks_run_completion():
    recorder = TraceRecorder()
    run5 = recorder.start_run(trace, agent, profile, "hash_5", started_at=now)
    recorder.record_tool_call(
        run5.agent_run_id, "market-api", {}, "call_hash_5", occurred_at=now
    )
    raises(
        lambda: recorder.complete_run(run5.agent_run_id),
        "미해결 Tool Call이 있는데 Run 완료",
    )


def test_06_nonexistent_run_or_tool_call_id_errors():
    recorder = TraceRecorder()
    raises(lambda: recorder.complete_run(uuid4()), "존재하지 않는 Run 완료 시도")
    raises(
        lambda: recorder.complete_tool_call(uuid4(), "h"),
        "존재하지 않는 Tool Call 완료 시도",
    )


def test_07_terminal_tool_call_cannot_transition_again():
    recorder = TraceRecorder()
    run2 = recorder.start_run(trace, agent, profile, "hash_2", started_at=now)
    call2 = recorder.record_tool_call(
        run2.agent_run_id, "broker-adapter", {}, "call_hash_2", occurred_at=now
    )
    recorder.deny_tool_call(call2.tool_call_id, "out_of_allowlist")
    raises(
        lambda: recorder.complete_tool_call(call2.tool_call_id, "h"),
        "DENIED에서 COMPLETED로 재전이",
    )


def test_08_fail_run_and_timeout_run_set_correct_status():
    recorder = TraceRecorder()
    run8 = recorder.start_run(trace, agent, profile, "hash_8", started_at=now)
    failed = recorder.fail_run(run8.agent_run_id, "model_error")
    assert failed.status is AgentRunStatus.FAILED and failed.error_code == "model_error"

    run9 = recorder.start_run(trace, agent, profile, "hash_9", started_at=now)
    timed_out = recorder.timeout_run(run9.agent_run_id)
    assert timed_out.status is AgentRunStatus.TIMED_OUT, (
        "응답 없음을 FAILED로 추정하면 안 됨"
    )


def test_09_as_tool_result_output_values_excludes_denied_and_incomplete():
    recorder = TraceRecorder()
    run10 = recorder.start_run(trace, agent, profile, "hash_10", started_at=now)
    call10a = recorder.record_tool_call(
        run10.agent_run_id, "portfolio-api", {}, "h10a", occurred_at=now
    )
    recorder.complete_tool_call(call10a.tool_call_id, "out10a")
    call10b = recorder.record_tool_call(
        run10.agent_run_id, "portfolio-api", {}, "h10b", occurred_at=now
    )
    recorder.deny_tool_call(call10b.tool_call_id, "denied")
    merged = as_tool_result_output_values(
        [call10a, call10b],
        {
            call10a.tool_call_id: {"AAPL": Decimal(100)},
            call10b.tool_call_id: {"MSFT": Decimal(999)},
        },
    )
    assert merged == {"AAPL": Decimal(100)}, "DENIED Tool Call 값이 섞이면 안 됨"
