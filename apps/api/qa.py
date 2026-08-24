"""Operator BFF proxy for the QA verification assessment API."""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

try:  # Reuse the projector imported by ``apps.api.main`` in the local process.
    from current_user import current_user
    from langsmith_traces import qa_trace_timeseries
    from orchestration.langsmith_feedback import canonical_department
except ImportError:  # pragma: no cover - package import path
    from .current_user import current_user
    from .langsmith_traces import qa_trace_timeseries
    from orchestration.langsmith_feedback import canonical_department

router = APIRouter(tags=["qa-mandate"])
QA_API_URL = os.getenv("QA_API_URL", "").strip().rstrip("/")
QA_API_AUTH_TOKEN = os.getenv("QA_API_AUTH_TOKEN", "").strip()
QA_API_TIMEOUT_SECONDS = float(os.getenv("QA_API_TIMEOUT_SECONDS", "8"))
_FEEDBACK_DEPARTMENTS = frozenset({
    "research",
    "trading",
    "risk",
    "qa",
    "quant",
    "accounting-portfolio",
    "ceo",
    "hr",
})


def _feedback_department(value: str) -> str:
    normalized = canonical_department(value)
    if normalized not in _FEEDBACK_DEPARTMENTS:
        raise HTTPException(status_code=422, detail="invalid_feedback_department")
    return normalized


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

    del owner_id  # 인증 게이트만 거치면 되고 요청자별로 값이 갈리지 않는다.
    return await qa_trace_timeseries(days=days)


@router.get("/ui/qa/observability/feedback/pending")
async def qa_observability_feedback_pending(
    limit: int = 50,
    owner_id: str | None = Depends(current_user),
) -> Any:
    """Return redacted LangSmith findings awaiting QA review."""

    del owner_id
    return await _qa_request(
        "GET",
        f"/qa/v1/observability/feedback/pending?limit={max(1, min(int(limit), 100))}",
        body={},
    )


@router.get("/ui/departments/{department}/observability/feedback")
async def department_observability_feedback(
    department: str,
    limit: int = 50,
    owner_id: str | None = Depends(current_user),
) -> Any:
    """Return only the selected department's redacted LangSmith findings."""

    if not owner_id:
        raise HTTPException(status_code=401, detail="portfolio_authentication_required")
    department_key = _feedback_department(department)
    return await _qa_request(
        "GET",
        f"/qa/v1/observability/feedback/department/{department_key}?limit={max(1, min(int(limit), 100))}",
        body={},
    )


@router.post("/ui/departments/{department}/observability/feedback/{artifact_id}")
async def add_department_observability_feedback(
    department: str,
    artifact_id: str,
    body: dict[str, Any],
    owner_id: str | None = Depends(current_user),
) -> Any:
    """Append a comment scoped to the selected department's own artifact."""

    if not owner_id:
        raise HTTPException(status_code=401, detail="portfolio_authentication_required")
    department_key = _feedback_department(department)
    comment = str(body.get("comment") or "").strip()
    if not comment:
        raise HTTPException(status_code=422, detail="invalid_department_feedback_comment")
    return await _qa_request(
        "POST",
        f"/qa/v1/observability/feedback/{artifact_id}/department-review",
        body={
            "reviewer_user_id": owner_id,
            "reviewer_department": department_key,
            "comment": comment[:1_200],
        },
    )


@router.post("/ui/qa/observability/feedback/{artifact_id}/decision")
async def decide_qa_observability_feedback(
    artifact_id: str,
    body: dict[str, Any],
    owner_id: str | None = Depends(current_user),
) -> Any:
    """Submit one server-attributed QA approval/rejection decision."""

    decision = str(body.get("decision") or "").upper()
    reason = str(body.get("reason") or "").strip()
    if decision not in {"APPROVED", "REJECTED"} or not reason:
        raise HTTPException(status_code=422, detail="invalid_feedback_decision")
    return await _qa_request(
        "POST",
        f"/qa/v1/observability/feedback/{artifact_id}/decision",
        body={
            "decision": decision,
            "approved_by": owner_id or "qa-user",
            "reason": reason[:240],
        },
    )


__all__ = [
    "QA_API_URL",
    "assess_qa_verification",
    "qa_langsmith_traces",
    "qa_observability_feedback_pending",
    "department_observability_feedback",
    "add_department_observability_feedback",
    "decide_qa_observability_feedback",
    "router",
]
