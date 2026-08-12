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

import os
from typing import Any

import httpx
from fastapi import HTTPException

GOVERNANCE_API_URL = os.getenv("GOVERNANCE_API_URL", "").rstrip("/")
GOVERNANCE_API_AUTH_TOKEN = os.getenv("GOVERNANCE_API_AUTH_TOKEN", "").strip()
GOVERNANCE_API_TIMEOUT_SECONDS = float(os.getenv("GOVERNANCE_API_TIMEOUT_SECONDS", "8"))


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


async def governance_request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
) -> object:
    if not GOVERNANCE_API_URL:
        raise HTTPException(status_code=503, detail="governance_api_unavailable")
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
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="governance_api_unavailable") from exc

    payload: object
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": f"governance_api_http_{response.status_code}"}
    if response.status_code >= 400:
        raise GovernanceProxyError(response.status_code, payload)
    return payload


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
