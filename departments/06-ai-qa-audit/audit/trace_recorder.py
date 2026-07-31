#!/usr/bin/env python3
"""Sprint K2: P0 Agent/Tool Trace 수집.

소유: 동규 (AI QA/감사본부)
근거: docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md 6.1(audit.agent_runs/tool_calls), 8.1(svc_audit_collector),
      12(Sprint K2 "Agent/Tool Trace 수집") - Evidence QA(Claim/PIT Rule Check)와 나란히 K2를
      이루는 두 번째 축이다.
      supabase/migrations/20260729000500_audit_api_security.sql의 audit.agent_runs,
      audit.tool_calls 컬럼·Enum 값과 이름을 맞췄다.

이 모듈은 "기록"만 한다. Tool Allowlist 위반 판정(Unauthorized Tool Call 탐지)은
tool-permission-security-reviewer(P1 tier)의 몫이라 여기서 승인·거부를 판단하지 않는다 -
호출자가 이미 내린 REQUESTED/ALLOWED/DENIED 상태를 그대로 기록할 뿐이다.

evidence_qa_engine.py의 7단계 "Tool 결과와 Agent 요약의 변형 여부" 검사는 지금 가벼운
ToolResultRecord 스텁으로 대신하고 있는데, 여기서 실제로 기록된 ToolCallRecord가 쌓이면
그 스텁을 대체할 진짜 입력이 된다 - as_tool_result()가 그 변환을 담당한다.

자체 점검: python departments/06-ai-qa-audit/audit/trace_recorder.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from repository import PostgresAuditRepository


class AgentRunStatus(StrEnum):
    """audit.agent_runs.status와 값이 같아야 한다."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


TERMINAL_RUN_STATES = frozenset(
    {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.TIMED_OUT, AgentRunStatus.CANCELLED}
)


class ToolCallStatus(StrEnum):
    """audit.tool_calls.status와 값이 같아야 한다."""

    REQUESTED = "REQUESTED"
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


TERMINAL_TOOL_CALL_STATES = frozenset(
    {ToolCallStatus.DENIED, ToolCallStatus.COMPLETED, ToolCallStatus.FAILED, ToolCallStatus.TIMED_OUT}
)


class TraceRecorderError(Exception):
    """Trace를 조용히 잃어버리지 않는다 - 앞뒤가 안 맞는 기록 시도는 예외로 올린다."""


@dataclass
class AgentRunRecord:
    """audit.agent_runs 1행."""

    agent_run_id: UUID
    trace_id: UUID
    agent_id: UUID
    profile_version_id: UUID
    input_hash: str
    started_at: datetime
    case_id: UUID | None = None
    fund_id: UUID | None = None
    model_id: UUID | None = None
    ended_at: datetime | None = None
    status: AgentRunStatus = AgentRunStatus.RUNNING
    error_code: str | None = None
    token_usage: dict = field(default_factory=dict)
    cost: dict = field(default_factory=dict)
    output_artifact_version_id: UUID | None = None
    trace_uri: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATES


@dataclass
class ToolCallRecord:
    """audit.tool_calls 1행."""

    tool_call_id: UUID
    agent_run_id: UUID
    trace_id: UUID
    tool_name: str
    scope: dict
    input_hash: str
    occurred_at: datetime
    status: ToolCallStatus = ToolCallStatus.REQUESTED
    output_hash: str | None = None
    policy_version: str | None = None
    latency_ms: int | None = None
    error_code: str | None = None
    completed_at: datetime | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TOOL_CALL_STATES


class TraceRecorder:
    """결정론적 Trace 기록소. svc_audit_collector가 이 모양대로 Supabase에 적재한다
    (지금은 인메모리 - DB 배선은 Sprint K0)."""

    def __init__(self, repository: "PostgresAuditRepository | None" = None) -> None:
        self.runs: dict[UUID, AgentRunRecord] = {}
        self.tool_calls: dict[UUID, ToolCallRecord] = {}
        self._by_profile_input: dict[tuple[UUID, str], UUID] = {}
        # repository가 있으면 audit.agent_runs/tool_calls에 write-through 한다(repository.py 참고).
        # 인메모리는 그대로 불변식 검사의 근거로 남는다 - RedisTradingStateStore와 같은 구도.
        self._repository = repository

    # -- Agent Run -------------------------------------------------------------

    def start_run(
        self,
        trace_id: UUID,
        agent_id: UUID,
        profile_version_id: UUID,
        input_hash: str,
        *,
        case_id: UUID | None = None,
        fund_id: UUID | None = None,
        model_id: UUID | None = None,
        started_at: datetime | None = None,
    ) -> AgentRunRecord:
        """같은 (profile_version_id, input_hash)로 아직 RUNNING인 Run이 있으면 그걸 그대로
        돌려준다 - 재시도로 같은 입력이 두 번 Run 잡히는 것을 막는다(OMS.create_order와
        같은 멱등성 원칙)."""
        key = (profile_version_id, input_hash)
        existing_id = self._by_profile_input.get(key)
        if existing_id is not None:
            existing = self.runs[existing_id]
            if existing.status is AgentRunStatus.RUNNING:
                return existing

        run = AgentRunRecord(
            agent_run_id=uuid4(), trace_id=trace_id, agent_id=agent_id,
            profile_version_id=profile_version_id, input_hash=input_hash,
            started_at=started_at or _now(), case_id=case_id, fund_id=fund_id, model_id=model_id,
        )
        self.runs[run.agent_run_id] = run
        self._by_profile_input[key] = run.agent_run_id
        if self._repository is not None:
            self._repository.insert_run(run)
        return run

    def complete_run(
        self,
        agent_run_id: UUID,
        *,
        output_artifact_version_id: UUID | None = None,
        token_usage: dict | None = None,
        cost: dict | None = None,
        trace_uri: str | None = None,
        ended_at: datetime | None = None,
    ) -> AgentRunRecord:
        run = self._require_run(agent_run_id)
        self._require_no_open_tool_calls(run, "완료")
        run.status = AgentRunStatus.COMPLETED
        run.ended_at = ended_at or _now()
        run.output_artifact_version_id = output_artifact_version_id
        run.token_usage = token_usage or {}
        run.cost = cost or {}
        run.trace_uri = trace_uri
        self._sync_run_terminal(run)
        return run

    def fail_run(self, agent_run_id: UUID, error_code: str, *, ended_at: datetime | None = None) -> AgentRunRecord:
        run = self._require_run(agent_run_id)
        run.status = AgentRunStatus.FAILED
        run.error_code = error_code
        run.ended_at = ended_at or _now()
        self._sync_run_terminal(run)
        return run

    def timeout_run(self, agent_run_id: UUID, *, ended_at: datetime | None = None) -> AgentRunRecord:
        # 응답이 없으면 UNKNOWN이지 FAILED로 추정하지 않는 OMS 원칙과 같다 - TIMED_OUT은
        # 별도 상태로 남긴다.
        run = self._require_run(agent_run_id)
        run.status = AgentRunStatus.TIMED_OUT
        run.ended_at = ended_at or _now()
        self._sync_run_terminal(run)
        return run

    def cancel_run(self, agent_run_id: UUID, *, ended_at: datetime | None = None) -> AgentRunRecord:
        run = self._require_run(agent_run_id)
        run.status = AgentRunStatus.CANCELLED
        run.ended_at = ended_at or _now()
        self._sync_run_terminal(run)
        return run

    # -- Tool Call ---------------------------------------------------------------

    def record_tool_call(
        self,
        agent_run_id: UUID,
        tool_name: str,
        scope: dict,
        input_hash: str,
        *,
        policy_version: str | None = None,
        occurred_at: datetime | None = None,
    ) -> ToolCallRecord:
        run = self._require_run(agent_run_id)
        if run.is_terminal:
            raise TraceRecorderError(
                f"종료된 Run({run.status.value})에는 Tool Call을 추가로 붙일 수 없습니다"
            )
        call = ToolCallRecord(
            tool_call_id=uuid4(), agent_run_id=agent_run_id, trace_id=run.trace_id,
            tool_name=tool_name, scope=scope, input_hash=input_hash,
            occurred_at=occurred_at or _now(), policy_version=policy_version,
        )
        self.tool_calls[call.tool_call_id] = call
        return call

    def allow_tool_call(self, tool_call_id: UUID) -> ToolCallRecord:
        return self._transition_tool_call(tool_call_id, ToolCallStatus.ALLOWED, {ToolCallStatus.REQUESTED})

    def deny_tool_call(self, tool_call_id: UUID, reason: str) -> ToolCallRecord:
        call = self._transition_tool_call(
            tool_call_id, ToolCallStatus.DENIED, {ToolCallStatus.REQUESTED, ToolCallStatus.ALLOWED},
        )
        call.error_code = reason
        call.completed_at = _now()
        self._sync_tool_call_terminal(call)
        return call

    def complete_tool_call(
        self, tool_call_id: UUID, output_hash: str, *, completed_at: datetime | None = None,
    ) -> ToolCallRecord:
        call = self._transition_tool_call(
            tool_call_id, ToolCallStatus.COMPLETED, {ToolCallStatus.REQUESTED, ToolCallStatus.ALLOWED},
        )
        call.output_hash = output_hash
        call.completed_at = completed_at or _now()
        call.latency_ms = _latency_ms(call.occurred_at, call.completed_at)
        self._sync_tool_call_terminal(call)
        return call

    def fail_tool_call(
        self, tool_call_id: UUID, error_code: str, *, completed_at: datetime | None = None,
    ) -> ToolCallRecord:
        call = self._transition_tool_call(
            tool_call_id, ToolCallStatus.FAILED, {ToolCallStatus.REQUESTED, ToolCallStatus.ALLOWED},
        )
        call.error_code = error_code
        call.completed_at = completed_at or _now()
        call.latency_ms = _latency_ms(call.occurred_at, call.completed_at)
        self._sync_tool_call_terminal(call)
        return call

    # -- 내부 ------------------------------------------------------------------

    def _sync_run_terminal(self, run: AgentRunRecord) -> None:
        if self._repository is not None:
            self._repository.update_run_terminal(run)

    def _sync_tool_call_terminal(self, call: ToolCallRecord) -> None:
        # audit.tool_calls는 append-only(DB 트리거) - REQUESTED/ALLOWED 중간 상태는 쓰지 않고
        # 종결 상태(DENIED/COMPLETED/FAILED)에 도달했을 때 최종 스냅샷 1행만 insert한다.
        if self._repository is not None:
            self._repository.insert_tool_call_terminal(call)

    def _require_run(self, agent_run_id: UUID) -> AgentRunRecord:
        run = self.runs.get(agent_run_id)
        if run is None:
            raise TraceRecorderError(f"존재하지 않는 Agent Run입니다: {agent_run_id}")
        return run

    def _require_no_open_tool_calls(self, run: AgentRunRecord, action: str) -> None:
        # Run을 종료하는데 REQUESTED/ALLOWED로 미해결 상태인 Tool Call이 남아 있으면
        # 안 된다 - 상태 불명을 성공으로 추정하지 않는다(팀 가이드 4.1 #9와 같은 fail-closed 원칙).
        open_calls = [
            c for c in self.tool_calls.values()
            if c.agent_run_id == run.agent_run_id and not c.is_terminal
        ]
        if open_calls:
            raise TraceRecorderError(
                f"Tool Call {len(open_calls)}건이 미해결 상태인데 Run을 {action} 처리할 수 없습니다"
            )

    def _transition_tool_call(
        self, tool_call_id: UUID, to_status: ToolCallStatus, allowed_from: set[ToolCallStatus],
    ) -> ToolCallRecord:
        call = self.tool_calls.get(tool_call_id)
        if call is None:
            raise TraceRecorderError(f"존재하지 않는 Tool Call입니다: {tool_call_id}")
        if call.status not in allowed_from:
            raise TraceRecorderError(
                f"Tool Call 상태 {call.status.value}에서 {to_status.value}로 갈 수 없습니다"
            )
        call.status = to_status
        return call


def as_tool_result_output_values(
    tool_calls: list[ToolCallRecord], numeric_outputs: dict[UUID, dict[str, Decimal]],
) -> dict[str, Decimal]:
    """완료된 Tool Call들을 evidence_qa_engine.ToolResultRecord.output_values 모양으로
    합친다. numeric_outputs는 output_hash 뒤에 실제로 숨어있는 구조화 값의 스텁이다
    (해시만으로는 재계산할 수 없어 P0 프로토타입에서는 호출자가 원본 숫자를 같이 넘긴다)."""
    merged: dict[str, Decimal] = {}
    for call in tool_calls:
        if call.status is not ToolCallStatus.COMPLETED:
            continue
        merged.update(numeric_outputs.get(call.tool_call_id, {}))
    return merged


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _latency_ms(occurred_at: datetime, completed_at: datetime) -> int:
    return max(0, int((completed_at - occurred_at).total_seconds() * 1000))


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    trace, agent, profile = uuid4(), uuid4(), uuid4()

    recorder = TraceRecorder()

    def raises(fn, why: str):
        try:
            fn()
        except TraceRecorderError:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. 정상 흐름: Run 시작 -> Tool Call 요청 -> 완료 -> Run 완료
    run = recorder.start_run(trace, agent, profile, "hash_1", started_at=now)
    assert run.status is AgentRunStatus.RUNNING
    call = recorder.record_tool_call(run.agent_run_id, "market-api", {"symbol": "AAPL"}, "call_hash_1",
                                     occurred_at=now)
    assert call.status is ToolCallStatus.REQUESTED
    recorder.allow_tool_call(call.tool_call_id)
    completed_call = recorder.complete_tool_call(
        call.tool_call_id, "out_hash_1", completed_at=now + timedelta(milliseconds=120),
    )
    assert completed_call.status is ToolCallStatus.COMPLETED
    assert completed_call.latency_ms == 120
    finished = recorder.complete_run(run.agent_run_id, ended_at=now + timedelta(seconds=1))
    assert finished.status is AgentRunStatus.COMPLETED

    # 2. Tool Call 거부(DENIED)도 기록되고, 그 상태로 Run은 정상 완료될 수 있다
    run2 = recorder.start_run(trace, agent, profile, "hash_2", started_at=now)
    call2 = recorder.record_tool_call(run2.agent_run_id, "broker-adapter", {}, "call_hash_2", occurred_at=now)
    denied = recorder.deny_tool_call(call2.tool_call_id, "out_of_allowlist")
    assert denied.status is ToolCallStatus.DENIED
    recorder.complete_run(run2.agent_run_id)  # DENIED는 종결 상태라 Run 완료를 막지 않는다

    # 3. 같은 (profile_version_id, input_hash)로 RUNNING 중에 다시 start_run -> 같은 Run 재사용 (멱등)
    run3a = recorder.start_run(trace, agent, profile, "hash_3", started_at=now)
    run3b = recorder.start_run(trace, agent, profile, "hash_3", started_at=now)
    assert run3a.agent_run_id == run3b.agent_run_id, "같은 입력인데 Run이 중복 생성됨"
    assert len([r for r in recorder.runs.values() if r.input_hash == "hash_3"]) == 1

    # 4. 종료된 Run에는 Tool Call을 못 붙인다
    raises(lambda: recorder.record_tool_call(run.agent_run_id, "x", {}, "h"), "완료된 Run에 Tool Call 추가")

    # 5. Tool Call이 미해결 상태(REQUESTED)로 남아 있으면 Run을 완료 처리할 수 없다
    run5 = recorder.start_run(trace, agent, profile, "hash_5", started_at=now)
    recorder.record_tool_call(run5.agent_run_id, "market-api", {}, "call_hash_5", occurred_at=now)
    raises(lambda: recorder.complete_run(run5.agent_run_id), "미해결 Tool Call이 있는데 Run 완료")

    # 6. 존재하지 않는 Run/Tool Call ID -> 에러
    raises(lambda: recorder.complete_run(uuid4()), "존재하지 않는 Run 완료 시도")
    raises(lambda: recorder.complete_tool_call(uuid4(), "h"), "존재하지 않는 Tool Call 완료 시도")

    # 7. 이미 종결된 Tool Call은 다시 전이할 수 없다 (DENIED -> COMPLETED 금지)
    raises(lambda: recorder.complete_tool_call(call2.tool_call_id, "h"), "DENIED에서 COMPLETED로 재전이")

    # 8. fail_run / timeout_run이 각각 올바른 상태와 error_code를 남긴다
    run8 = recorder.start_run(trace, agent, profile, "hash_8", started_at=now)
    failed = recorder.fail_run(run8.agent_run_id, "model_error")
    assert failed.status is AgentRunStatus.FAILED and failed.error_code == "model_error"

    run9 = recorder.start_run(trace, agent, profile, "hash_9", started_at=now)
    timed_out = recorder.timeout_run(run9.agent_run_id)
    assert timed_out.status is AgentRunStatus.TIMED_OUT, "응답 없음을 FAILED로 추정하면 안 됨"

    # 9. as_tool_result_output_values - 완료된 Tool Call만 합쳐지고 DENIED/미완료는 제외
    run10 = recorder.start_run(trace, agent, profile, "hash_10", started_at=now)
    call10a = recorder.record_tool_call(run10.agent_run_id, "portfolio-api", {}, "h10a", occurred_at=now)
    recorder.complete_tool_call(call10a.tool_call_id, "out10a")
    call10b = recorder.record_tool_call(run10.agent_run_id, "portfolio-api", {}, "h10b", occurred_at=now)
    recorder.deny_tool_call(call10b.tool_call_id, "denied")
    merged = as_tool_result_output_values(
        [call10a, call10b],
        {call10a.tool_call_id: {"AAPL": Decimal("100")}, call10b.tool_call_id: {"MSFT": Decimal("999")}},
    )
    assert merged == {"AAPL": Decimal("100")}, "DENIED Tool Call 값이 섞이면 안 됨"

    print("ok - Agent/Tool Trace Recorder 9개 시나리오 점검 통과")
