"""Operator BFF proxy for the Risk mandate assessment API."""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

try:  # Reuse the projector imported by ``apps.api.main`` in the local process.
    from agent_status import publish_agent_status
except ImportError:  # pragma: no cover - package import path
    from .agent_status import publish_agent_status

router = APIRouter(tags=["risk-mandate"])
# The local compose contract publishes risk-api on 8041.  Keep the environment
# override for deployed BFFs, but make the documented local two-process setup
# work without a second, undocumented variable.
RISK_API_URL = os.getenv("RISK_API_URL", "http://127.0.0.1:8041").strip().rstrip("/")
RISK_API_AUTH_TOKEN = os.getenv("RISK_API_AUTH_TOKEN", "").strip()
RISK_API_TIMEOUT_SECONDS = float(os.getenv("RISK_API_TIMEOUT_SECONDS", "8"))


async def _risk_request(
    method: str, path: str, *, body: dict[str, Any] | None = None
) -> Any:
    if not RISK_API_URL:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "risk_api_unavailable",
                "message": "RISK_API_URL is not configured",
            },
        )
    headers = (
        {"X-Risk-Internal-Token": RISK_API_AUTH_TOKEN}
        if RISK_API_AUTH_TOKEN
        else None
    )
    try:
        async with httpx.AsyncClient(
            base_url=RISK_API_URL,
            timeout=RISK_API_TIMEOUT_SECONDS,
        ) as client:
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


def _publish_assessment_statuses(mandate_id: str, payload: dict[str, Any]) -> None:
    """Project Risk worker lifecycle into the operator read model.

    The domain API remains the source of the assessment result.  This small
    adapter only publishes sanitized worker status; it never publishes the
    mandate, policy text, credentials, or a binding order decision.
    """

    trace_id = str(payload.get("trace_id") or mandate_id)
    head_state = payload.get("risk_head_state")
    head_state = head_state if isinstance(head_state, dict) else {}
    routing = head_state.get("routing")
    routing = routing if isinstance(routing, dict) else {}
    compliance_route = routing.get("compliance-policy-worker")
    if not isinstance(compliance_route, dict):
        compliance_route = routing
    runtime = payload.get("employee_runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    runtime_workers = runtime.get("workers")
    runtime_workers = runtime_workers if isinstance(runtime_workers, dict) else {}
    compliance_runtime = runtime_workers.get("compliance-policy-worker")
    compliance_runtime = (
        compliance_runtime if isinstance(compliance_runtime, dict) else {}
    )
    employees = payload.get("employees")
    if not isinstance(employees, dict):
        return
    compliance_report = employees.get("compliance-policy-worker")
    compliance_report = (
        compliance_report if isinstance(compliance_report, dict) else {}
    )
    route_metadata = {
        "run_id": head_state.get("run_id"),
        "query_mode": compliance_route.get("query_mode"),
        "routing_rationale": compliance_route.get("routing_rationale"),
        "routing_by_llm": compliance_route.get("routing_by_llm"),
        "generation_status": compliance_report.get(
            "generation_status", compliance_runtime.get("status")
        ),
    }

    for worker_id, report in employees.items():
        if not isinstance(report, dict):
            continue
        if worker_id == "risk-runner":
            decision = str(
                payload.get("decision") or report.get("verdict") or "HOLD"
            ).upper()
            status = (
                "BLOCKED"
                if decision in {"REJECT", "HALTED"}
                else "WAITING_APPROVAL"
            )
            role = "Deterministic portfolio limit exposure runner"
        elif worker_id == "compliance-policy-worker":
            status = (
                "DEGRADED"
                if (
                    str(report.get("status", "")).upper() == "DEGRADED"
                    or str(report.get("generation_status", "")).upper()
                    == "DEGRADED"
                )
                else "IDLE"
            )
            role = "Point-in-time policy evidence analyst"
        else:
            continue
        publish_agent_status(
            department_code="risk-management",
            agent_id=worker_id,
            worker_id=worker_id,
            status=status,
            role=role,
            reason=(
                f"Risk assessment {mandate_id}: "
                f"{report.get('verdict', report.get('status', 'COMPLETED'))}"
            ),
            trace_id=trace_id,
            case_id=mandate_id,
            metadata=(
                route_metadata
                if worker_id == "compliance-policy-worker"
                else {"run_id": head_state.get("run_id")}
            ),
            producer="risk-mandate-bff",
        )


def _publish_assessment_failure(mandate_id: str, error: str) -> None:
    """Close an in-flight projection safely when the domain call fails."""

    for worker_id, role in (
        ("risk-runner", "Deterministic portfolio limit exposure runner"),
        ("compliance-policy-worker", "Point-in-time policy evidence analyst"),
    ):
        publish_agent_status(
            department_code="risk-management",
            agent_id=worker_id,
            worker_id=worker_id,
            status="DEGRADED",
            role=role,
            reason=f"Risk assessment {mandate_id} unavailable: {error}",
            case_id=mandate_id,
            producer="risk-mandate-bff",
        )


@router.post("/ui/risk/mandates/{mandate_id}/assess")
async def assess_risk_mandate(mandate_id: str, body: dict[str, Any]) -> Any:
    """Send the immutable mandate to the Risk Head through the BFF boundary."""

    if body.get("mandate_id") != mandate_id:
        raise HTTPException(status_code=409, detail="mandate_id_mismatch")
    # Make the in-flight worker visible before the domain call.  The final
    # projection below is deliberately non-binding and is safe to replay.
    for worker_id, role in (
        ("risk-runner", "Deterministic portfolio limit exposure runner"),
        ("compliance-policy-worker", "Point-in-time policy evidence analyst"),
    ):
        publish_agent_status(
            department_code="risk-management",
            agent_id=worker_id,
            worker_id=worker_id,
            status="QUEUED",
            role=role,
            reason=f"Risk assessment {mandate_id} queued",
            case_id=mandate_id,
            producer="risk-mandate-bff",
        )

    try:
        payload = await _risk_request(
            "POST",
            f"/risk/v1/mandates/{mandate_id}/assess",
            body=body,
        )
    except HTTPException:
        _publish_assessment_failure(mandate_id, "risk-api request failed")
        raise
    if isinstance(payload, dict):
        _publish_assessment_statuses(mandate_id, payload)
    return payload


@router.get("/ui/risk/mandate-presets")
async def get_risk_mandate_presets() -> Any:
    """Project the Risk-owned versioned preset matrix to the operator UI."""

    return await _risk_request("GET", "/risk/v1/mandate-presets")


@router.post("/ui/risk/position-risk-plans/calculate")
async def calculate_position_risk_plan(body: dict[str, Any]) -> Any:
    """Proxy deterministic PAPER planning; this endpoint never submits an order."""

    return await _risk_request(
        "POST", "/risk/v1/position-risk-plans/calculate", body=body
    )


@router.post("/ui/risk/position-risk-plans/transitions")
async def transition_position_risk_plan(body: dict[str, Any]):
    return await _risk_request(
        "POST", "/risk/v1/position-risk-plans/transitions", body=body
    )


@router.post("/ui/risk/position-risk-plans/projections")
async def record_position_risk_plan_projection(body: dict[str, Any]):
    """Proxy non-authoritative Discord/Notion delivery evidence."""

    return await _risk_request(
        "POST", "/risk/v1/position-risk-plans/projections", body=body
    )


async def activate_mandate_limits(body: dict[str, Any]) -> Any:
    """Internal BFF orchestration helper used after Governance activation."""

    return await _risk_request(
        "POST", "/risk/v1/mandate-limits/activate-from-mandate", body=body
    )


async def validate_proposed_mandate_limits(body: dict[str, Any]) -> Any:
    return await _risk_request(
        "POST", "/risk/v1/mandate-limits/validate-proposed", body=body
    )


__all__ = [
    "RISK_API_URL",
    "activate_mandate_limits",
    "assess_risk_mandate",
    "calculate_position_risk_plan",
    "get_risk_mandate_presets",
    "router",
    "validate_proposed_mandate_limits",
]
