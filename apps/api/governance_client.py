#!/usr/bin/env python3
"""Thin async client for the external governance-api service.

Extracted out of ``main.py`` so both the FastAPI app (browser-facing ``/ui``
proxy routes) and ``portfolio_runtime.py`` (server-side Mandate content
lookup for the CEO task planner) share one source of truth for
``GOVERNANCE_API_URL``/auth/timeout instead of two copies that can drift.

Browser는 Domain API를 직접 호출하지 않는다. Mandate 변경은 CEO Office가 소유하므로
이 BFF가 얇게 전달하고, 정책 검증·Risk/QA/사용자 승인·영속화는 governance-api가 한다.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import HTTPException

GOVERNANCE_API_URL = os.getenv("GOVERNANCE_API_URL", "").rstrip("/")
GOVERNANCE_API_AUTH_TOKEN = os.getenv("GOVERNANCE_API_AUTH_TOKEN", "").strip()
GOVERNANCE_API_TIMEOUT_SECONDS = float(os.getenv("GOVERNANCE_API_TIMEOUT_SECONDS", "8"))
_LOGGER = logging.getLogger(__name__)


class GovernanceProxyError(HTTPException):
    """Carries the upstream Governance API error body through untouched.

    A plain ``HTTPException(detail=payload)`` gets re-wrapped by FastAPI into
    ``{"detail": payload}``, nesting ``error_code``/``message`` one level too
    deep for the frontend (``governanceClient.ts``), which reads them at the
    top level. Every governance-api error handler already emits that
    ``{error_code, message, detail, trace_id}`` shape, so pass it straight
    through instead of collapsing it into FastAPI's single ``detail`` field.
    """

    def __init__(self, status_code: int, payload: object) -> None:
        super().__init__(status_code=status_code, detail=payload)
        self.payload = payload


class GovernanceTransportError(HTTPException):
    """Safe browser response for a BFF-to-Governance transport failure.

    The original httpx exception stays in the BFF log only.  Browser clients
    need an actionable, stable error code, but must not receive internal
    service addresses or low-level network diagnostics.
    """

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        message: str,
        reason: str,
        trace_id: str | None,
    ) -> None:
        payload = {
            "error_code": error_code,
            "message": message,
            "detail": {"reason": reason},
            "trace_id": trace_id,
        }
        super().__init__(status_code=status_code, detail=payload)
        self.payload = payload


def _request_trace_id(body: dict[str, object] | None) -> str | None:
    """Return an existing client trace id without inventing a second one."""

    if not body:
        return None
    trace_id = body.get("trace_id")
    return trace_id.strip() if isinstance(trace_id, str) and trace_id.strip() else None


def _transport_error(
    *,
    method: str,
    path: str,
    reason: str,
    exc: httpx.RequestError | None,
    trace_id: str | None,
) -> GovernanceTransportError:
    """Log the diagnostic detail and expose only a safe response payload."""

    if reason == "timeout":
        status_code = 504
        error_code = "GOVERNANCE_API_TIMEOUT"
        message = "거버넌스 서비스 응답이 지연되고 있습니다. 잠시 후 다시 시도해 주세요."
    elif reason == "connect_error":
        status_code = 503
        error_code = "GOVERNANCE_API_CONNECT_FAILED"
        message = "거버넌스 서비스에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."
    elif reason == "not_configured":
        status_code = 503
        error_code = "GOVERNANCE_API_NOT_CONFIGURED"
        message = "거버넌스 서비스 연결 설정이 준비되지 않았습니다. 관리자에게 문의해 주세요."
    else:
        status_code = 503
        error_code = "GOVERNANCE_API_TRANSPORT_ERROR"
        message = "거버넌스 서비스와 통신 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."

    _LOGGER.warning(
        "governance transport failure method=%s path=%s reason=%s trace_id=%s exception_type=%s",
        method,
        path,
        reason,
        trace_id,
        type(exc).__name__ if exc is not None else None,
        exc_info=exc is not None,
    )
    return GovernanceTransportError(
        status_code=status_code,
        error_code=error_code,
        message=message,
        reason=reason,
        trace_id=trace_id,
    )


async def governance_request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
) -> object:
    trace_id = _request_trace_id(body)
    if not GOVERNANCE_API_URL:
        raise _transport_error(
            method=method,
            path=path,
            reason="not_configured",
            exc=None,
            trace_id=trace_id,
        )
    headers = (
        {"X-Governance-Internal-Token": GOVERNANCE_API_AUTH_TOKEN}
        if GOVERNANCE_API_AUTH_TOKEN
        else None
    )
    try:
        async with httpx.AsyncClient(
            base_url=GOVERNANCE_API_URL,
            timeout=GOVERNANCE_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.request(
                method,
                path,
                params=params,
                json=body,
                headers=headers,
            )
    except httpx.TimeoutException as exc:
        raise _transport_error(
            method=method,
            path=path,
            reason="timeout",
            exc=exc,
            trace_id=trace_id,
        ) from exc
    except httpx.ConnectError as exc:
        raise _transport_error(
            method=method,
            path=path,
            reason="connect_error",
            exc=exc,
            trace_id=trace_id,
        ) from exc
    except httpx.RequestError as exc:
        raise _transport_error(
            method=method,
            path=path,
            reason="transport_error",
            exc=exc,
            trace_id=trace_id,
        ) from exc

    payload: object
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": f"governance_api_http_{response.status_code}"}
    if response.status_code >= 400:
        raise GovernanceProxyError(response.status_code, payload)
    return payload


def fetch_current_mandate_by_fund(fund_id: str) -> dict[str, Any] | None:
    """Fund 하나의 현재 Mandate를 **동기로** 읽는다. 실패는 `None`이다.

    동기인 이유: 호출부(`/ui/ceo/ask`)가 `hermes kanban create` 같은 블로킹
    subprocess를 쓰는 동기 라우트다. FastAPI는 동기 라우트를 threadpool에서
    돌리므로 그 안에서 동기 HTTP를 쓰는 편이 맞고, 라우트를 async로 바꾸면
    subprocess 호출이 이벤트 루프를 막는다.

    **예외를 올리지 않는다.** Mandate 스냅샷은 CEO 워크플로의 참고 맥락이고
    (`binding: false`), 이것 때문에 질의 접수 자체가 실패하면 사용자는 Mandate가
    없다는 이유로 아무 질문도 못 한다. 못 읽으면 스냅샷 없이 진행하고, 부서는
    "Mandate 블록이 없다"는 정확한 사실을 보게 된다 - 기본 한도를 지어내는 것보다
    안전하다(개발 원칙 9).

    409(한 Fund에 Mandate 2개 이상)도 `None`이다. 모호할 때 임의로 하나를 고르면
    사용자가 정하지 않은 한도가 판단 근거로 쓰인다.
    """

    if not GOVERNANCE_API_URL or not fund_id:
        return None
    headers = (
        {"X-Governance-Internal-Token": GOVERNANCE_API_AUTH_TOKEN}
        if GOVERNANCE_API_AUTH_TOKEN
        else None
    )
    try:
        with httpx.Client(
            base_url=GOVERNANCE_API_URL,
            timeout=GOVERNANCE_API_TIMEOUT_SECONDS,
        ) as client:
            response = client.get(
                f"/governance/v1/mandates/by-fund/{fund_id}/current",
                headers=headers,
            )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


async def fetch_mandate_policy_content(mandate_id: str) -> dict[str, Any] | None:
    """Best-effort server-side Mandate content lookup for the CEO planner.

    Mandate content is advisory context for the LLM task planner, not a
    binding gate -- a missing/unavailable governance-api, an unset
    ``mandate_id``, or any other failure returns ``None`` instead of raising,
    so the portfolio pipeline never blocks on this enrichment.
    """

    try:
        response = await governance_request(
            "GET", f"/governance/v1/mandates/{mandate_id}/current"
        )
    except Exception:  # noqa: BLE001 - best-effort enrichment, never blocks the run.
        return None
    if not isinstance(response, dict):
        return None
    policy = response.get("policy")
    return policy if isinstance(policy, dict) else None
