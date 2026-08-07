"""Operator BFF proxy for the QA verification assessment API."""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["qa-mandate"])
QA_API_URL = os.getenv("QA_API_URL", "").strip().rstrip("/")
QA_API_AUTH_TOKEN = os.getenv("QA_API_AUTH_TOKEN", "").strip()
QA_API_TIMEOUT_SECONDS = float(os.getenv("QA_API_TIMEOUT_SECONDS", "8"))


async def _qa_request(method: str, path: str, *, body: dict[str, Any]) -> Any:
    if not QA_API_URL:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "qa_api_unavailable",
                "message": "QA_API_URL is not configured",
            },
        )
    headers = {"X-Qa-Internal-Token": QA_API_AUTH_TOKEN} if QA_API_AUTH_TOKEN else None
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


__all__ = ["QA_API_URL", "assess_qa_verification", "router"]
