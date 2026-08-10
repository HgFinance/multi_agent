"""Platform/IAM service.py <-> HR API 종단 계약 테스트.

실제 Postgres/Redis 없이, HR FastAPI 앱을 httpx.ASGITransport 로 직접 연결해
"APPROVED 요청 -> Platform/IAM 발견 -> provisioning -> PROVISIONED" 전체
왕복을 검증한다. postgres_role_manager/redis_namespace_manager 는
monkeypatch 로 대체한다 - 이 테스트가 검증하는 것은 "SQL/Redis 명령이 맞다"가
아니라 "service.py가 HR API 계약을 올바르게 오간다"이다(그 부분은
postgres_role_manager.py/redis_namespace_manager.py 자체의 __main__
자체 점검이 실 DB/Redis로 따로 검증한다).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "platform_iam"))

_HR_API_DIR = ROOT / "departments" / "07-agent-workforce" / "api"


def _load_hr_app():
    import importlib.util

    spec = importlib.util.spec_from_file_location("hr_app_platform_iam_test", _HR_API_DIR / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hr_app_platform_iam_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def hr_app():
    os.environ["DATABASE_URL"] = ""  # InMemoryAccessRepository 강제 - 실 DB 미접촉
    module = _load_hr_app()
    assert type(module._access_repo).__name__ == "InMemoryAccessRepository"
    return module


@pytest.fixture()
def service_against_hr(hr_app):
    from service import PlatformIamService

    # starlette.testclient.TestClient는 httpx.Client의 서브클래스라(anyio portal로
    # ASGI 앱에 동기 브리지) service.py의 httpx.Client 타입 자리에 그대로 넣을 수
    # 있다 - httpx.ASGITransport는 AsyncClient 전용이라 여기서는 못 쓴다.
    from fastapi.testclient import TestClient

    client = TestClient(hr_app.app, base_url="http://hr-testserver")
    return PlatformIamService(
        hr_api_url="http://hr-testserver",
        postgres_dsn="postgresql://fake-dsn-not-used",
        redis_url="redis://fake-url-not-used",
        client=client,
    )


def _approve_via_hr(
    hr_app, *, resource_kind: str, resource_ref: str, environment: str = "SHADOW",
    tool_id: str | None = None,
) -> str:
    from fastapi.testclient import TestClient

    client = TestClient(hr_app.app)
    body = {
        "agent_id": "11111111-2222-3333-4444-555555555555",
        "resource_kind": resource_kind,
        "resource_ref": resource_ref,
        "environment": environment,
        "justification": "platform-iam e2e test",
        "requested_by": "hr-department",
        "expires_at": "2026-09-10T00:00:00Z",
        "requested_at": "2026-08-10T00:00:00Z",
    }
    if tool_id is not None:
        body["tool_id"] = tool_id
    created = client.post("/workforce/v1/access-requests", json=body)
    assert created.status_code == 200, created.text
    request_id = created.json()["request_id"]
    approved = client.post(
        f"/workforce/v1/access-requests/{request_id}/approve",
        json={"approver": "ceo-agent", "approval_id": "appr-1", "at": "2026-08-10T00:00:00Z"},
    )
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED", approved.text
    return request_id


def test_data_request_end_to_end_reaches_provisioned(hr_app, service_against_hr, monkeypatch: pytest.MonkeyPatch) -> None:
    request_id = _approve_via_hr(hr_app, resource_kind="DATA", resource_ref="market-api:read")

    import provisioning
    monkeypatch.setitem(provisioning.RESOURCE_REF_GRANTS, "market-api:read", ("SELECT", "workspace.market_data"))

    import postgres_role_manager
    monkeypatch.setattr(
        postgres_role_manager, "apply_grant_plan", lambda plan, **_: plan.provisioning_ref
    )

    outcomes = service_against_hr.run_once()
    assert len(outcomes) == 1
    assert outcomes[0].status == "PROVISIONED", outcomes[0].detail
    assert outcomes[0].provisioning_ref == outcomes[0].provisioning_ref  # sanity

    from fastapi.testclient import TestClient

    client = TestClient(hr_app.app)
    final = client.get("/workforce/v1/access-requests", params={"status": "PROVISIONED"})
    ids = [r["request_id"] for r in final.json()["access_requests"]]
    assert request_id in ids


def test_environment_request_end_to_end_reaches_provisioned(hr_app, service_against_hr, monkeypatch: pytest.MonkeyPatch) -> None:
    request_id = _approve_via_hr(hr_app, resource_kind="ENVIRONMENT", resource_ref="memory-namespace")

    import redis_namespace_manager
    monkeypatch.setattr(
        redis_namespace_manager, "register_namespace", lambda plan, **_: plan.provisioning_ref
    )

    outcomes = service_against_hr.run_once()
    assert len(outcomes) == 1
    assert outcomes[0].status == "PROVISIONED", outcomes[0].detail

    from fastapi.testclient import TestClient

    client = TestClient(hr_app.app)
    final = client.get("/workforce/v1/access-requests", params={"status": "PROVISIONED"})
    ids = [r["request_id"] for r in final.json()["access_requests"]]
    assert request_id in ids


def test_tool_request_is_explicitly_skipped_not_silently_dropped(hr_app, service_against_hr) -> None:
    # access.py의 __post_init__이 TOOL 요구사항(tool_id 필수)을 강제하므로 넣는다.
    _approve_via_hr(hr_app, resource_kind="TOOL", resource_ref="some-tool", tool_id="tool-1")

    outcomes = service_against_hr.run_once()
    assert len(outcomes) == 1
    assert outcomes[0].status == "SKIPPED"
    assert "agent_tool_permissions" in outcomes[0].detail


def test_unmapped_data_resource_stays_approved_not_provisioned(hr_app, service_against_hr) -> None:
    """매핑 없는 resource_ref는 실패로 접히고, 요청은 APPROVED에 남아야 한다."""

    request_id = _approve_via_hr(hr_app, resource_kind="DATA", resource_ref="truly-unmapped-resource")

    outcomes = service_against_hr.run_once()
    assert outcomes[0].status == "FAILED"

    from fastapi.testclient import TestClient

    client = TestClient(hr_app.app)
    still_approved = client.get("/workforce/v1/access-requests", params={"status": "APPROVED"})
    ids = [r["request_id"] for r in still_approved.json()["access_requests"]]
    assert request_id in ids, "실패한 요청이 APPROVED에서 사라지면 안 된다 - 재시도 가능해야 한다"
