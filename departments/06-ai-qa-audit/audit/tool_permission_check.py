#!/usr/bin/env python3
"""P1 경계 위 P0 조각: Tool Allowlist 위반 탐지 (결정론적, LLM 없음).

소유: 동규 (AI QA/감사본부)
근거: docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md 11.4("Unauthorized Tool Call 0"),
      14(DoD "Agent와 Tool의 권한 위반을 Trace에서 탐지한다")
      tool-permission-security-reviewer(P1) 페르소나의 판정 로직 - 페르소나 자체(권한 요청
      해석, Escalation 서술)는 P1이지만, "Tool이 Allowlist에 있는가"라는 판정 자체는 집합
      소속 여부 확인일 뿐이라 LLM이 필요 없다. Risk approve/reject와 같은 원칙 - Pre-trade
      Hot Path든 Tool 권한 검사든, 판정은 결정론적 코드가 하고 LLM은 결과를 설명만 한다.

workforce.agent_profiles/Job Profile이 아직 없어서(인사팀 영역, Seed 0건) 실제 Allowlist는
AgentToolPolicy로 스텁 처리한다 - risk_engine.py의 RiskContext, evidence_qa_engine.py의
EvidenceStore와 같은 패턴이다.

자체 점검: python departments/06-ai-qa-audit/audit/tool_permission_check.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trace_recorder import ToolCallRecord, ToolCallStatus, TraceRecorder


class ToolPermissionResult(StrEnum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"


@dataclass(frozen=True)
class AgentToolPolicy:
    """workforce.agent_profiles/Job Profile의 Tool Allowlist 스텁."""

    agent_id: UUID
    profile_version_id: UUID
    allowed_tools: frozenset[str]


@dataclass(frozen=True)
class ToolPermissionCheck:
    result: ToolPermissionResult
    reason: str


def check_tool_permission(
    policy: AgentToolPolicy, tool_name: str
) -> ToolPermissionCheck:
    """Allowlist 소속 여부만 본다 - 애매하면 판단하는 게 아니라 집합에 없으면 그냥 거부다."""
    if tool_name in policy.allowed_tools:
        return ToolPermissionCheck(ToolPermissionResult.ALLOWED, "")
    return ToolPermissionCheck(
        ToolPermissionResult.DENIED,
        f"'{tool_name}'은 Profile {policy.profile_version_id}의 Tool Allowlist에 없습니다",
    )


def record_and_check_tool_call(
    recorder: TraceRecorder,
    agent_run_id: UUID,
    policy: AgentToolPolicy,
    tool_name: str,
    scope: dict,
    input_hash: str,
) -> ToolCallRecord:
    """Trace에 Tool Call을 기록하면서 동시에 권한을 판정한다 - DoD "Trace에서 권한 위반을
    탐지한다"가 이 함수다. Allowlist에 없으면 REQUESTED에서 곧바로 DENIED로 끝난다(그 뒤에
    실제 Tool을 호출하지 않는다)."""
    call = recorder.record_tool_call(agent_run_id, tool_name, scope, input_hash)
    check = check_tool_permission(policy, tool_name)
    if check.result is ToolPermissionResult.DENIED:
        return recorder.deny_tool_call(call.tool_call_id, check.reason)
    return recorder.allow_tool_call(call.tool_call_id)


def count_unauthorized_calls(tool_calls: list[ToolCallRecord]) -> int:
    """팀 가이드 11.4 핵심 Metric "Unauthorized Tool Call 0"에 대응하는 집계."""
    return sum(1 for c in tool_calls if c.status is ToolCallStatus.DENIED)


if __name__ == "__main__":
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    trace, agent, profile = uuid4(), uuid4(), uuid4()
    recorder = TraceRecorder()
    run = recorder.start_run(trace, agent, profile, "hash_perm_1", started_at=now)

    policy = AgentToolPolicy(
        agent_id=agent,
        profile_version_id=profile,
        allowed_tools=frozenset({"market-api", "portfolio-api"}),
    )

    # 1. Allowlist에 있는 Tool -> ALLOWED
    check_ok = check_tool_permission(policy, "market-api")
    assert check_ok.result is ToolPermissionResult.ALLOWED
    assert check_ok.reason == ""

    # 2. Allowlist에 없는 Tool -> DENIED, 이유가 남는다
    check_bad = check_tool_permission(policy, "broker-adapter-submit")
    assert check_bad.result is ToolPermissionResult.DENIED
    assert "broker-adapter-submit" in check_bad.reason

    # 3. 통합 - 허용된 Tool은 기록되면서 ALLOWED 상태가 된다
    allowed_call = record_and_check_tool_call(
        recorder,
        run.agent_run_id,
        policy,
        "market-api",
        {"symbol": "AAPL"},
        "call_h1",
    )
    assert allowed_call.status is ToolCallStatus.ALLOWED
    assert not allowed_call.is_terminal, (
        "ALLOWED는 아직 완료 전이라 종결 상태가 아니어야 함"
    )

    # 4. 통합 - 금지된 Tool은 기록되면서 곧바로 DENIED(종결)로 끝난다
    denied_call = record_and_check_tool_call(
        recorder,
        run.agent_run_id,
        policy,
        "broker-adapter-submit",
        {},
        "call_h2",
    )
    assert denied_call.status is ToolCallStatus.DENIED
    assert denied_call.is_terminal, "DENIED는 종결 상태여야 함"
    assert denied_call.error_code is not None and "Allowlist" in denied_call.error_code

    # 5. Unauthorized Tool Call 집계 - DENIED만 센다
    recorder.complete_tool_call(allowed_call.tool_call_id, "out_h1")
    assert count_unauthorized_calls([allowed_call, denied_call]) == 1

    # 6. run 종료 시점엔 열린 Tool Call이 없어야 한다 (ALLOWED->COMPLETED 처리 후)
    recorder.complete_run(run.agent_run_id)

    print("ok - Tool Allowlist 위반 탐지 6개 시나리오 점검 통과")
