"""Operator BFF proxy for the QA verification assessment API."""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

try:  # Reuse the projector imported by ``apps.api.main`` in the local process.
    from current_user import current_user, require_any_fund_membership
    from langsmith_traces import qa_trace_timeseries
except ImportError:  # pragma: no cover - package import path
    from .current_user import current_user, require_any_fund_membership
    from .langsmith_traces import qa_trace_timeseries

router = APIRouter(tags=["qa-mandate"])
QA_API_URL = os.getenv("QA_API_URL", "").strip().rstrip("/")
QA_API_AUTH_TOKEN = os.getenv("QA_API_AUTH_TOKEN", "").strip()
QA_API_TIMEOUT_SECONDS = float(os.getenv("QA_API_TIMEOUT_SECONDS", "8"))
def _require_observability_actor(owner_id: str | None) -> str:
    """Require an authenticated operator with an effective fund grant."""

    if not owner_id:
        raise HTTPException(status_code=401, detail="portfolio_authentication_required")
    # Production requires a real local authorization projection. Explicit
    # fixture mode remains available for deterministic local/test runs.
    require_any_fund_membership(owner_id)
    return owner_id


async def _qa_request(method: str, path: str, *, body: dict[str, Any]) -> Any:
    if not QA_API_URL:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "qa_api_unavailable",
                "message": "QA_API_URL is not configured",
            },
        )
    headers = {}
    if QA_API_AUTH_TOKEN:
        # Legacy routes still receive the existing internal header.  The
        # observability feedback routes additionally use the service-auth
        # bearer contract enforced by audit-api.
        headers["X-Qa-Internal-Token"] = QA_API_AUTH_TOKEN
        headers["Authorization"] = f"Bearer {QA_API_AUTH_TOKEN}"
    try:
        async with httpx.AsyncClient(base_url=QA_API_URL, timeout=QA_API_TIMEOUT_SECONDS) as client:
            response = await client.request(method, path, json=body, headers=headers)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="qa_api_unavailable") from exc

    try:
        payload: Any = response.json()
    except ValueError:
        payload = {"detail": f"qa_api_http_{response.status_code}"}
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=payload)
    return payload


@router.post("/ui/qa/verifications/{verification_id}/assess")
async def assess_qa_verification(verification_id: str, body: dict[str, Any]) -> Any:
    """Send the immutable verification to the QA Head through the BFF boundary."""

    if body.get("verification_id") != verification_id:
        raise HTTPException(status_code=409, detail="verification_id_mismatch")
    return await _qa_request(
        "POST",
        f"/qa/v1/verifications/{verification_id}/assess",
        body=body,
    )


@router.get("/ui/qa/observability/langsmith")
async def qa_langsmith_traces(
    days: int = 7,
    owner_id: str | None = Depends(current_user),
) -> Any:
    """QA 부서 카드에 표시할 LangSmith trace 집계 - read-only, 자격증명 미노출.

    `LANGSMITH_API_KEY`는 이 프로세스(BFF) 밖으로 나가지 않는다 - 브라우저는
    이 집계 결과만 받는다.
    """

    _require_observability_actor(owner_id)
    return await qa_trace_timeseries(days=days)


__all__ = [
    "QA_API_URL",
    "assess_qa_verification",
    "qa_langsmith_traces",
    "router",
]
