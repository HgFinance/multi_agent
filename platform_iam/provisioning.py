#!/usr/bin/env python3
"""Platform/IAM 결정론 핵심 — AccessRequest 하나를 어떻게 provisioning할지 계획만 세운다.

소유: 영주 (CEO/HR, Platform/IAM 담당자 미정 상태에서 최초 구현)
근거: docs/02-engineering/PLATFORM_IAM_SPEC.md 2·3절,
      departments/07-agent-workforce/lifecycle/access.py (provisioning_ref 계약의 원본)

여기에 LLM이 없다. "이 AccessRequest에 어떤 Postgres Role/Redis Namespace가
필요한가"는 규칙표 조회이지 판단이 아니다 — CLAUDE.md 개발 원칙 2와 이 세션
전체가 확인한 "결정론 함수가 정답을 만들 수 있으면 LLM을 쓰지 않는다" 원칙 그대로다.

이 모듈은 **I/O를 하지 않는다.** CREATE ROLE도, Redis 연결도, DB 조회도 없다.
"무엇을 해야 하는가"(계획)와 "그것을 실제로 하는 것"(실행)을 분리한다 -
postgres_role_manager.py/redis_namespace_manager.py가 실행을 맡는다. 이렇게
나누면 이 파일의 로직 전체를 DB·Redis 없이 테스트할 수 있다.

## resource_ref -> GRANT 매핑표가 비어 있는 이유

RESOURCE_REF_GRANTS 를 임의로 채우지 않는다. 실제 DATA 요청이 나올 때마다
도현님(회계·인프라 담당)과 합의해서 채운다 - tool_gateway.py의 ENDPOINT_SCOPES가
커진 방식과 같다. 매핑에 없는 resource_ref는 "권한 없음으로 처리"가 아니라
ProvisioningError로 즉시 거부한다(fail-closed) - 모르는 자원에 조용히 최소
권한을 주는 것도, 조용히 통과시키는 것도 둘 다 위험하다.

불변식:
  1. APPROVED 상태가 아닌 AccessRequest는 계획을 세우지 않는다.
  2. TOOL 자원은 새 인프라를 만들지 않는다 - 실제 강제는 tool_gateway.py가
     config.yaml로 이미 하고 있다. 여기서는 agent_tool_permissions에 그 권한이
     ACTIVE로 선언돼 있다는 사실만 provisioning_ref로 증명한다.
  3. DATA 자원의 role_name은 agent_id가 유효한 UUID 형식일 때만 만든다 -
     SQL identifier(role 이름)는 psycopg2 placeholder로 바인딩할 수 없어
     직접 문자열을 조합해야 하므로, UUID 형식 검증이 곧 injection 방지다.

자체 점검: python platform_iam/provisioning.py
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

try:
    from access import AccessRequest, RequestStatus, ResourceKind
except ModuleNotFoundError:  # direct/standalone execution
    import sys
    from pathlib import Path

    sys.path.insert(
        0, str(Path(__file__).resolve().parents[1] / "departments" / "07-agent-workforce" / "lifecycle")
    )
    from access import AccessRequest, RequestStatus, ResourceKind


class ProvisioningError(RuntimeError):
    """계획을 세울 수 없다 - 매핑 없음, 상태 위반, 잘못된 agent_id 등. fail-closed."""


# resource_ref -> (PostgreSQL GRANT verb, 대상 schema.table 또는 schema).
# 의도적으로 비어 있다 - 위 모듈 docstring 참고.
RESOURCE_REF_GRANTS: dict[str, tuple[str, str]] = {}

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


@dataclass(frozen=True)
class ToolConfirmationPlan:
    """TOOL 자원 - 새 인프라 없음, agent_tool_permissions 실재만 증명."""

    permission_id: str
    provisioning_ref: str


@dataclass(frozen=True)
class PostgresGrantPlan:
    """DATA 자원 - CREATE ROLE + GRANT 실행 계획."""

    role_name: str
    grant_verb: str
    grant_target: str
    provisioning_ref: str


@dataclass(frozen=True)
class RedisNamespacePlan:
    """ENVIRONMENT 자원 - Memory Namespace 등록 계획."""

    namespace_prefix: str
    provisioning_ref: str


ProvisioningPlan = ToolConfirmationPlan | PostgresGrantPlan | RedisNamespacePlan


def _role_name(agent_id: str, environment: str) -> str:
    if not _UUID_RE.match(agent_id):
        # agent_id가 workforce.agent_profiles(agent_id) FK라 UUID여야 한다.
        # Role 이름은 SQL identifier라 바인딩 파라미터로 못 넣는다 - 형식을
        # 먼저 검증해야 그 다음 문자열 조합이 injection에서 안전해진다.
        raise ProvisioningError(f"agent_id가 UUID 형식이 아니다: {agent_id!r}")
    # UUID의 하이픈은 PostgreSQL identifier에서 그대로 못 쓰므로 밑줄로 바꾼다.
    safe_agent = agent_id.replace("-", "_")
    return f"agent_{safe_agent}_{environment.lower()}"


def plan_provisioning(
    request: AccessRequest,
    *,
    tool_permission_id: str | None = None,
    resource_ref_grants: Mapping[str, tuple[str, str]] | None = None,
) -> ProvisioningPlan:
    """AccessRequest 하나에 대한 provisioning 계획을 세운다. I/O 없음.

    tool_permission_id: TOOL 자원일 때, 호출부가 이미 workforce.agent_tool_permissions
      에서 조회해 온 ACTIVE permission_id. 이 함수는 그 조회를 직접 하지 않는다
      (I/O 없음 불변식) - 없으면 계획을 세우지 못하고 거부한다.
    resource_ref_grants: 테스트에서 자기만의 매핑을 주입할 때 씀. 운영에서는
      생략하면 모듈 상수 RESOURCE_REF_GRANTS를 쓴다.
    """

    if request.status is not RequestStatus.APPROVED:
        raise ProvisioningError(
            f"APPROVED 상태가 아닌 요청은 provisioning 계획을 세우지 않는다: "
            f"{request.status.value} (request_id={request.request_id})"
        )

    if request.resource_kind is ResourceKind.TOOL:
        if not tool_permission_id:
            raise ProvisioningError(
                f"TOOL 자원인데 유효한 tool_permission_id가 없다 - "
                f"workforce.agent_tool_permissions 확인 필요 (request_id={request.request_id})"
            )
        return ToolConfirmationPlan(
            permission_id=tool_permission_id,
            provisioning_ref=f"tool-permission:{tool_permission_id}",
        )

    if request.resource_kind is ResourceKind.DATA:
        grants = resource_ref_grants if resource_ref_grants is not None else RESOURCE_REF_GRANTS
        grant = grants.get(request.resource_ref)
        if grant is None:
            raise ProvisioningError(
                f"resource_ref '{request.resource_ref}'에 대한 GRANT 매핑이 없다 - "
                "RESOURCE_REF_GRANTS에 추가 필요 (설정 오류, fail-closed)"
            )
        verb, target = grant
        role_name = _role_name(request.agent_id, request.environment.value)
        return PostgresGrantPlan(
            role_name=role_name,
            grant_verb=verb,
            grant_target=target,
            provisioning_ref=f"postgres-role:{role_name}",
        )

    if request.resource_kind is ResourceKind.ENVIRONMENT:
        return RedisNamespacePlan(
            namespace_prefix=f"memory:agent:{request.agent_id}:*",
            provisioning_ref=f"redis-namespace:agent:{request.agent_id}",
        )

    raise ProvisioningError(f"알 수 없는 resource_kind: {request.resource_kind}")


# ---------------------------------------------------------------------------
# 자체 점검 (python platform_iam/provisioning.py) - I/O 없음, 매번 실행 가능
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import datetime, timedelta, timezone

    from access import Environment

    t0 = datetime(2026, 8, 10, tzinfo=timezone.utc)
    agent_uuid = "11111111-2222-3333-4444-555555555555"

    def _req(resource_kind: ResourceKind, resource_ref: str = "x", tool_id: str | None = None) -> AccessRequest:
        return AccessRequest(
            request_id="r1", agent_id=agent_uuid, resource_kind=resource_kind,
            resource_ref=resource_ref, tool_id=tool_id, environment=Environment.SHADOW,
            justification="selfcheck", requested_by="hr-department",
            expires_at=t0 + timedelta(days=1), requested_at=t0,
            status=RequestStatus.APPROVED,
        )

    # 1) APPROVED 아니면 거부.
    not_approved = AccessRequest(
        request_id="r0", agent_id=agent_uuid, resource_kind=ResourceKind.ENVIRONMENT,
        resource_ref="x", environment=Environment.SHADOW, justification="j",
        requested_by="hr-department", expires_at=t0 + timedelta(days=1), requested_at=t0,
    )
    try:
        plan_provisioning(not_approved)
        raise AssertionError("REQUESTED 상태에서 계획이 세워졌다 - 안 된다")
    except ProvisioningError:
        pass
    print("  APPROVED 아니면 거부           OK")

    # 2) TOOL - permission_id 없으면 거부, 있으면 확인 계획.
    tool_req = _req(ResourceKind.TOOL, tool_id="tool-1")
    try:
        plan_provisioning(tool_req)
        raise AssertionError("tool_permission_id 없이 TOOL 계획이 세워졌다")
    except ProvisioningError:
        pass
    plan = plan_provisioning(tool_req, tool_permission_id="perm-abc")
    assert isinstance(plan, ToolConfirmationPlan)
    assert plan.provisioning_ref == "tool-permission:perm-abc"
    print("  TOOL 확인 계획                OK")

    # 3) DATA - 매핑 없으면 거부(fail-closed), 있으면 Role 계획.
    data_req = _req(ResourceKind.DATA, resource_ref="market-api:read")
    try:
        plan_provisioning(data_req)
        raise AssertionError("매핑 없는 resource_ref가 통과했다 - fail-closed 위반")
    except ProvisioningError:
        pass
    plan2 = plan_provisioning(
        data_req, resource_ref_grants={"market-api:read": ("SELECT", "workspace.market_data")}
    )
    assert isinstance(plan2, PostgresGrantPlan)
    assert plan2.role_name == "agent_11111111_2222_3333_4444_555555555555_shadow"
    assert plan2.grant_verb == "SELECT" and plan2.grant_target == "workspace.market_data"
    assert plan2.provisioning_ref == f"postgres-role:{plan2.role_name}"
    print("  DATA fail-closed + Role 계획   OK")

    # 3b) agent_id가 UUID가 아니면 거부 (injection 방지).
    bad_req = _req(ResourceKind.DATA, resource_ref="market-api:read")
    bad_req = AccessRequest(**{**bad_req.__dict__, "agent_id": "'; DROP ROLE x; --"})
    try:
        plan_provisioning(bad_req, resource_ref_grants={"market-api:read": ("SELECT", "t")})
        raise AssertionError("UUID 아닌 agent_id가 Role 이름 조합까지 통과했다")
    except ProvisioningError:
        pass
    print("  잘못된 agent_id 거부           OK")

    # 4) ENVIRONMENT - Redis Namespace 계획.
    env_req = _req(ResourceKind.ENVIRONMENT)
    plan3 = plan_provisioning(env_req)
    assert isinstance(plan3, RedisNamespacePlan)
    assert plan3.namespace_prefix == f"memory:agent:{agent_uuid}:*"
    assert plan3.provisioning_ref == f"redis-namespace:agent:{agent_uuid}"
    print("  ENVIRONMENT Namespace 계획     OK")

    print("Platform/IAM provisioning 5개 영역 통과 (I/O 없음)")
