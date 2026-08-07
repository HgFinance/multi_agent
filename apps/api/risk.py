"""Operator BFF proxy for the Risk mandate assessment API."""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["risk-mandate"])
RISK_API_URL = os.getenv("RISK_API_URL", "").strip().rstrip("/")
RISK_API_AUTH_TOKEN = os.getenv("RISK_API_AUTH_TOKEN", "").strip()
RISK_API_TIMEOUT_SECONDS = float(os.getenv("RISK_API_TIMEOUT_SECONDS", "8"))


async def _risk_request(method: str, path: str, *, body: dict[str, Any]) -> Any:
    if not RISK_API_URL:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "risk_api_unavailable",
                "message": "RISK_API_URL is not configured",
            },
        )
    headers = {"X-Risk-Internal-Token": RISK_API_AUTH_TOKEN} if RISK_API_AUTH_TOKEN else None
    try:
        async with httpx.AsyncClient(base_url=RISK_API_URL, timeout=RISK_API_TIMEOUT_SECONDS) as client:
            response = await client.request(method, path, json=body, headers=headers)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="risk_api_unavailable") from exc

    try:
        payload: Any = response.json()
    except ValueError:
        payload = {"detail": f"risk_api_http_{response.status_code}"}
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=payload)
    return payload


@router.post("/ui/risk/mandates/{mandate_id}/assess")
async def assess_risk_mandate(mandate_id: str, body: dict[str, Any]) -> Any:
    """Send the immutable mandate to the Risk Head through the BFF boundary."""

    if body.get("mandate_id") != mandate_id:
        raise HTTPException(status_code=409, detail="mandate_id_mismatch")
    return await _risk_request(
        "POST",
        f"/risk/v1/mandates/{mandate_id}/assess",
        body=body,
    )


__all__ = ["RISK_API_URL", "assess_risk_mandate", "router"]
