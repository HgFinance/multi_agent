"""Platform/IAM provisioning.py 계약 테스트 - 전부 I/O 없음.

platform_iam/provisioning.py 는 순수 함수라 DB/Redis 없이 CI에서 항상 돈다.
자체 점검(__main__)이 이미 같은 시나리오를 검증하지만, pytest 로도 고정해
CI에서 회귀를 잡는다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "platform_iam"))
sys.path.insert(0, str(ROOT / "departments" / "07-agent-workforce" / "lifecycle"))

from access import AccessRequest, Environment, RequestStatus, ResourceKind  # noqa: E402
from provisioning import (  # noqa: E402
    PostgresGrantPlan,
    ProvisioningError,
    RedisNamespacePlan,
    ToolConfirmationPlan,
    plan_provisioning,
)

_T0 = datetime(2026, 8, 10, tzinfo=timezone.utc)
_AGENT_UUID = "11111111-2222-3333-4444-555555555555"


def _request(
    resource_kind: ResourceKind,
    *,
    resource_ref: str = "x",
    tool_id: str | None = None,
    agent_id: str = _AGENT_UUID,
    status: RequestStatus = RequestStatus.APPROVED,
) -> AccessRequest:
    return AccessRequest(
        request_id="r1", agent_id=agent_id, resource_kind=resource_kind,
        resource_ref=resource_ref, tool_id=tool_id, environment=Environment.SHADOW,
        justification="test", requested_by="hr-department",
        expires_at=_T0 + timedelta(days=1), requested_at=_T0, status=status,
    )


def test_non_approved_request_is_rejected() -> None:
    req = _request(ResourceKind.ENVIRONMENT, status=RequestStatus.REQUESTED)
    with pytest.raises(ProvisioningError):
        plan_provisioning(req)


def test_tool_without_permission_id_is_rejected() -> None:
    req = _request(ResourceKind.TOOL, tool_id="tool-1")
    with pytest.raises(ProvisioningError):
        plan_provisioning(req)


def test_tool_with_permission_id_returns_confirmation_only() -> None:
    req = _request(ResourceKind.TOOL, tool_id="tool-1")
    plan = plan_provisioning(req, tool_permission_id="perm-abc")
    assert isinstance(plan, ToolConfirmationPlan)
    assert plan.provisioning_ref == "tool-permission:perm-abc"


def test_data_without_grant_mapping_fails_closed() -> None:
    req = _request(ResourceKind.DATA, resource_ref="unmapped-resource")
    with pytest.raises(ProvisioningError):
        plan_provisioning(req, resource_ref_grants={})


def test_data_with_grant_mapping_returns_role_plan() -> None:
    req = _request(ResourceKind.DATA, resource_ref="market-api:read")
    plan = plan_provisioning(
        req, resource_ref_grants={"market-api:read": ("SELECT", "workspace.market_data")}
    )
    assert isinstance(plan, PostgresGrantPlan)
    assert plan.grant_verb == "SELECT"
    assert plan.grant_target == "workspace.market_data"
    assert plan.role_name == f"agent_{_AGENT_UUID.replace('-', '_')}_shadow"
    assert plan.provisioning_ref == f"postgres-role:{plan.role_name}"


def test_data_request_rejects_non_uuid_agent_id() -> None:
    req = _request(ResourceKind.DATA, resource_ref="market-api:read", agent_id="not-a-uuid")
    with pytest.raises(ProvisioningError):
        plan_provisioning(
            req, resource_ref_grants={"market-api:read": ("SELECT", "workspace.market_data")}
        )


def test_data_request_rejects_sql_injection_attempt_in_agent_id() -> None:
    req = _request(
        ResourceKind.DATA, resource_ref="market-api:read", agent_id="'; DROP ROLE x; --"
    )
    with pytest.raises(ProvisioningError):
        plan_provisioning(
            req, resource_ref_grants={"market-api:read": ("SELECT", "workspace.market_data")}
        )


def test_environment_request_returns_redis_namespace_plan() -> None:
    req = _request(ResourceKind.ENVIRONMENT)
    plan = plan_provisioning(req)
    assert isinstance(plan, RedisNamespacePlan)
    assert plan.namespace_prefix == f"memory:agent:{_AGENT_UUID}:*"
    assert plan.provisioning_ref == f"redis-namespace:agent:{_AGENT_UUID}"


def test_default_resource_ref_grants_table_starts_empty() -> None:
    """RESOURCE_REF_GRANTS 를 임의로 채우지 않는다는 설계 결정 자체를 고정한다.

    실제 매핑이 늘어나면 이 테스트는 지워도 된다 - 이 테스트가 막는 것은
    "누군가 검증 없이 매핑을 슬쩍 채워 넣는 것"이 아니라 "비어 있어야 한다는
    설계를 잊고 기본값을 바꾸는 것"이다.
    """

    import provisioning

    assert provisioning.RESOURCE_REF_GRANTS == {}
