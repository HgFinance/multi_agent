"""Focused tests for the canonical QA runtime boundary."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

QA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QA_DIR))

from qa_runtime import QARunner, build_qa_task_context
from runtime_contracts import ErrorCode, WorkerStatus, canonical_payload_hash


class _NoCallTools:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, task, payload):
        return [{"tool": "evidence", "scope": "qa"}]

    def allow(self, request, task):
        return True

    def execute(self, request, task):
        self.calls += 1
        return {"tool": request.tool, "payload": {"ok": True}}


class _Executor:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result if result is not None else {"status": "COMPLETED", "summary": "ok"}
        self.error = error

    def invoke(self, task, evidence_refs, input_payload):
        if self.error is not None:
            raise self.error
        return self.result


def _task(payload: dict) -> object:
    return build_qa_task_context(payload)


def test_canonical_hash_is_shared_by_context_and_replay() -> None:
    payload = {"assessment": {"decision": "PASS"}}
    outcome = QARunner(tools=None, executor=_Executor()).run(_task(payload), payload)

    expected = canonical_payload_hash(payload)
    assert outcome.passed
    assert outcome.payload_hash == expected
    assert outcome.replay_manifest is not None
    assert outcome.replay_manifest.input_hash == expected
    assert outcome.worker_context is not None
    assert outcome.worker_context.input_hash == expected
    assert outcome.worker_context.schema_version == "qa.worker-context.v1"
    assert outcome.worker_context.department == "qa-department"
    assert outcome.worker_context.producer_worker == "qa-runner"


def test_malformed_input_fails_before_tool_execution() -> None:
    tools = _NoCallTools()
    outcome = QARunner(tools=tools, executor=_Executor()).run(_task({"x": 1}), ["not-an-object"])

    assert outcome.error_code is ErrorCode.INVALID_INPUT
    assert outcome.status is not WorkerStatus.COMPLETED
    assert not outcome.passed
    assert tools.calls == 0


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TimeoutError("slow"), ErrorCode.TIMEOUT),
        (MemoryError("full"), ErrorCode.OOM),
        (RuntimeError("crashed"), ErrorCode.CRASHED),
    ],
)
def test_worker_error_codes_are_preserved(error: Exception, code: ErrorCode) -> None:
    payload = {"assessment": {"decision": "PASS"}}
    outcome = QARunner(tools=None, executor=_Executor(error=error)).run(_task(payload), payload)

    assert outcome.error_code is code
    assert not outcome.passed
    assert outcome.worker_context is not None
    assert outcome.worker_context.status is not WorkerStatus.COMPLETED
    assert code.value in outcome.worker_context.reason_codes


def test_schema_failure_and_tool_denial_are_not_projected_as_pass() -> None:
    payload = {"assessment": {"decision": "PASS"}}
    malformed = QARunner(tools=None, executor=_Executor(result={"unexpected": True})).run(
        _task(payload), payload
    )
    assert malformed.error_code is ErrorCode.SCHEMA_FAILURE
    assert not malformed.passed

    class _Denied(_NoCallTools):
        def allow(self, request, task):
            return False

    denied_tools = _Denied()
    denied = QARunner(tools=denied_tools, executor=_Executor()).run(
        _task({"tool_calls": [{"tool": "evidence", "scope": "qa"}]}),
        {"tool_calls": [{"tool": "evidence", "scope": "qa"}]},
    )
    assert denied.error_code is ErrorCode.TOOLCALL_DENIED
    assert not denied.passed
    assert denied_tools.calls == 0


def test_worker_context_mapping_mismatch_fails_closed() -> None:
    payload = {"assessment": {"decision": "PASS"}}
    mismatched = {
        "context_id": "ctx-1",
        "schema_version": "qa.worker-context.v1",
        "case_id": "other-case",
        "task_id": "other-task",
        "input_contract": "qa.department-input.v1",
        "department": "qa-department",
        "trace_id": "other-trace",
        "producer_worker": "qa-runner",
        "consumer_worker": "qa-runner",
        "status": "COMPLETED",
        "advisory": {"summary": "wrong"},
        "reason_codes": [],
        "input_refs": [],
        "output_refs": [],
        "profile_version": "p",
        "model_version": "m",
        "adapter_version": "a",
        "input_hash": canonical_payload_hash(payload),
        "attempt": 1,
        "timeout_ms": 1000,
        "created_at": "2026-08-09T00:00:00+00:00",
    }
    outcome = QARunner(tools=None, executor=_Executor(result=mismatched)).run(_task(payload), payload)
    assert outcome.error_code is ErrorCode.SCHEMA_FAILURE
    assert not outcome.passed