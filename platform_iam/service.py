#!/usr/bin/env python3
"""Platform/IAM 폴링 서비스 — HR API의 APPROVED 요청을 발견해 실제로 provisioning한다.

소유: 영주 (CEO/HR, Platform/IAM 담당자 미정 상태에서 최초 구현)
근거: docs/02-engineering/PLATFORM_IAM_SPEC.md 2.2·4.1

흐름:
  [1] GET {HR_API_URL}/workforce/v1/access-requests?status=APPROVED
  [2] resource_kind별로 provisioning.py의 순수 함수(plan_provisioning)에 계획을 맡김
  [3] 계획을 postgres_role_manager/redis_namespace_manager로 실행
  [4] POST {HR_API_URL}/workforce/v1/access-requests/{id}/provision 로 provisioning_ref 기록

이 파일이 판단하는 것은 없다 - "계획을 세운다"는 provisioning.py, "실행한다"는
두 manager가 하고, 여기는 그 사이를 연결만 한다. HR API에도, Postgres/Redis에도
직접 SQL/명령을 새로 만들지 않는다 - 전부 이미 만든 함수를 순서대로 부른다.

## TOOL 자원은 아직 처리하지 못한다 (알려진 공백, 숨기지 않는다)

workforce.agent_tool_permissions를 조회하는 API가 HR 쪽에도 이 저장소 어디에도
없다(2026-08-10 확인, grep 0건). TOOL 요청은 건너뛰고 명시적으로 로그를 남긴다 -
조용히 무시하지 않는다. HR이 그 조회 엔드포인트를 추가하면 이 파일의
_process_tool()만 채우면 된다.

## 실패 처리 원칙

Provisioning 어느 단계든 실패하면 그 요청은 **APPROVED 상태에 그대로 남는다**
(HR의 provision 콜백을 안 부르므로). 다음 폴링 주기에 다시 시도한다. 개발
원칙 9(실패는 확대가 아니라 차단)를 따른다 - 절반만 provisioning된 상태로
PROVISIONED를 찍지 않는다.

자체 점검: python platform_iam/service.py
  - HR_API_URL 없으면 import만 확인한다.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

try:
    from provisioning import (
        PostgresGrantPlan,
        ProvisioningError,
        RedisNamespacePlan,
        ToolConfirmationPlan,
        plan_provisioning,
    )
except ModuleNotFoundError:  # direct/standalone execution
    from platform_iam.provisioning import (
        PostgresGrantPlan,
        ProvisioningError,
        RedisNamespacePlan,
        ToolConfirmationPlan,
        plan_provisioning,
    )

HR_API_URL = os.getenv("HR_API_URL", "http://127.0.0.1:8044").rstrip("/")
HR_API_TIMEOUT_SECONDS = float(os.getenv("HR_API_TIMEOUT_SECONDS", "8"))


class ServiceError(RuntimeError):
    """HR API 왕복 자체가 실패함 (provisioning 로직과는 다른 층의 오류)."""


@dataclass(frozen=True)
class ProvisioningOutcome:
    request_id: str
    resource_kind: str
    status: str  # "PROVISIONED" | "SKIPPED" | "FAILED"
    detail: str
    provisioning_ref: str | None = None


def _request_dict_to_access_request(payload: dict[str, Any]) -> Any:
    """HR API 응답(JSON dict)을 access.AccessRequest로 되돌린다."""

    try:
        from access import AccessRequest, Environment, RequestStatus, ResourceKind
    except ModuleNotFoundError:  # direct/standalone execution
        import sys
        from pathlib import Path

        sys.path.insert(
            0, str(Path(__file__).resolve().parents[1] / "departments" / "07-agent-workforce" / "lifecycle")
        )
        from access import AccessRequest, Environment, RequestStatus, ResourceKind
    return AccessRequest(
        request_id=payload["request_id"],
        agent_id=payload["agent_id"],
        resource_kind=ResourceKind(payload["resource_kind"]),
        resource_ref=payload["resource_ref"],
        tool_id=payload.get("tool_id"),
        profile_version_id=payload.get("profile_version_id"),
        environment=Environment(payload["environment"]),
        justification=payload["justification"],
        requested_by=payload["requested_by"],
        expires_at=datetime.fromisoformat(payload["expires_at"]),
        requested_at=datetime.fromisoformat(payload["requested_at"]),
        scope=payload.get("scope") or {},
        approval_id=payload.get("approval_id"),
        approvals=payload.get("approvals") or [],
        status=RequestStatus(payload["status"]),
        trace_id=payload.get("trace_id") or "",
    )


class PlatformIamService:
    def __init__(
        self,
        *,
        hr_api_url: str = HR_API_URL,
        postgres_dsn: str | None = None,
        redis_url: str | None = None,
        role_password_provider: Callable[[], str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._hr_api_url = hr_api_url
        self._postgres_dsn = postgres_dsn or os.environ.get("DATABASE_URL")
        self._redis_url = redis_url or os.environ.get("REDIS_URL")
        self._role_password_provider = role_password_provider or (lambda: os.urandom(16).hex())
        self._client = client or httpx.Client(timeout=HR_API_TIMEOUT_SECONDS)

    def _fetch_approved_requests(self) -> list[dict[str, Any]]:
        try:
            resp = self._client.get(
                f"{self._hr_api_url}/workforce/v1/access-requests",
                params={"status": "APPROVED"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ServiceError(f"HR API 조회 실패: {exc}") from exc
        return resp.json().get("access_requests", [])

    def _confirm_provision(
        self, request_id: str, *, provisioning_ref: str, tool_permission_id: str | None = None
    ) -> None:
        body: dict[str, Any] = {
            "provisioning_ref": provisioning_ref,
            "provisioned_by": "platform-iam",
            "effective_from": datetime.now(timezone.utc).isoformat(),
        }
        if tool_permission_id:
            body["tool_permission_id"] = tool_permission_id
        try:
            resp = self._client.post(
                f"{self._hr_api_url}/workforce/v1/access-requests/{request_id}/provision",
                json=body,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ServiceError(f"HR API provision 콜백 실패 ({request_id}): {exc}") from exc

    def _process_one(self, payload: dict[str, Any]) -> ProvisioningOutcome:
        request_id = payload["request_id"]
        resource_kind = payload["resource_kind"]
        try:
            access_request = _request_dict_to_access_request(payload)
        except (KeyError, ValueError) as exc:
            return ProvisioningOutcome(request_id, resource_kind, "FAILED", f"요청 파싱 실패: {exc}")

        if access_request.resource_kind.value == "TOOL":
            # 알려진 공백 - 위 모듈 docstring 참고. 조용히 넘기지 않는다.
            return ProvisioningOutcome(
                request_id, resource_kind, "SKIPPED",
                "TOOL 자원 처리 보류 - workforce.agent_tool_permissions 조회 엔드포인트 없음",
            )

        try:
            plan = plan_provisioning(access_request)
        except ProvisioningError as exc:
            return ProvisioningOutcome(request_id, resource_kind, "FAILED", f"계획 수립 실패: {exc}")

        try:
            if isinstance(plan, PostgresGrantPlan):
                if not self._postgres_dsn:
                    return ProvisioningOutcome(
                        request_id, resource_kind, "FAILED", "DATABASE_URL 미설정 - DATA provisioning 불가"
                    )
                from postgres_role_manager import apply_grant_plan  # lazy: psycopg2 optional
                ref = apply_grant_plan(
                    plan, dsn=self._postgres_dsn, role_password=self._role_password_provider()
                )
            elif isinstance(plan, RedisNamespacePlan):
                if not self._redis_url:
                    return ProvisioningOutcome(
                        request_id, resource_kind, "FAILED", "REDIS_URL 미설정 - ENVIRONMENT provisioning 불가"
                    )
                from redis_namespace_manager import register_namespace  # lazy: redis optional
                ref = register_namespace(plan, redis_url=self._redis_url)
            else:  # pragma: no cover - TOOL은 위에서 이미 갈렸다
                return ProvisioningOutcome(request_id, resource_kind, "FAILED", "알 수 없는 계획 타입")
        except Exception as exc:  # noqa: BLE001 - 실행 실패는 전부 fail-closed로 접는다.
            return ProvisioningOutcome(request_id, resource_kind, "FAILED", f"실행 실패: {exc}")

        try:
            self._confirm_provision(request_id, provisioning_ref=ref)
        except ServiceError as exc:
            # 실제 Role/Namespace는 이미 만들어졌지만 HR에 알리지 못했다 - 다음
            # 폴링에서 재시도하면 apply_grant_plan/register_namespace는 멱등이라
            # 안전하게 다시 실행되고, 이번엔 콜백만 다시 시도된다.
            return ProvisioningOutcome(request_id, resource_kind, "FAILED", str(exc))

        return ProvisioningOutcome(request_id, resource_kind, "PROVISIONED", "완료", provisioning_ref=ref)

    def run_once(self) -> list[ProvisioningOutcome]:
        """한 번의 폴링 주기. 이번에 발견한 APPROVED 요청을 전부 처리한다."""

        approved = self._fetch_approved_requests()
        return [self._process_one(p) for p in approved]


# ---------------------------------------------------------------------------
# 자체 점검 (python platform_iam/service.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("ok - import 확인 (httpx, provisioning 연결)")

    hr_url = os.environ.get("HR_API_URL")
    if not hr_url:
        print("HR_API_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    service = PlatformIamService(hr_api_url=hr_url)
    outcomes = service.run_once()
    print(f"ok - run_once 호출 완료 ({len(outcomes)}건 처리)")
    for o in outcomes:
        print(f"  {o.request_id} [{o.resource_kind}] -> {o.status}: {o.detail}")
