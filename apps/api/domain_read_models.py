"""Read-only domain projections for the AI Office dashboard.

These endpoints expose the same operator event projection as ``/ui/snapshot``
without pretending that a registry entry is a live worker. A domain is marked
``DEGRADED`` until an ``agent.status.v1`` event or portfolio runtime heartbeat
has been observed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:  # script execution path
    from operations_read_model import build_operations_snapshot
except ModuleNotFoundError:  # pragma: no cover - package import path
    from apps.api.operations_read_model import build_operations_snapshot


DOMAIN_DEPARTMENTS: dict[str, tuple[str, ...]] = {
    "research": ("research-department",),
    "strategy": ("quant-backtest-department",),
    "risk": ("risk-management",),
    "qa": ("qa-department",),
    "risk-qa": ("risk-management", "qa-department"),
}


def build_domain_read_model(domain: str) -> dict[str, Any]:
    """Build a browser-safe read model for one dashboard domain."""

    department_codes = DOMAIN_DEPARTMENTS[domain]
    operations = build_operations_snapshot()
    departments = [
        row
        for row in operations.get("departments", [])
        if row.get("department_code") in department_codes
    ]
    statuses = [
        row
        for row in operations.get("agent_statuses", [])
        if row.get("department_code") in department_codes
    ]
    runtime = operations.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    runtime_departments = [
        row
        for row in runtime.get("departments", [])
        if isinstance(row, dict) and row.get("department_code") in department_codes
    ]
    observed = bool(statuses or runtime_departments)
    domain_status = "CONNECTED" if observed else "DEGRADED"
    return {
        "schema_version": "operator-domain.v1",
        "domain": domain,
        "mode": "DEMO",
        "status": domain_status,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source": "agent.status.v1 + operator-operations.v1",
        "event_bridge_connected": bool(operations.get("event_bridge_connected")),
        "sequence": int(operations.get("sequence", 0)),
        "departments": departments,
        "agents": statuses,
        "runtime": {
            "run_id": runtime.get("run_id"),
            "status": runtime.get("status", "OFFLINE"),
            "phase": runtime.get("phase"),
            "departments": runtime_departments,
        },
        "warnings": ([] if observed else ["실제 LangGraph status event가 아직 관찰되지 않았습니다."]),
    }


__all__ = ["DOMAIN_DEPARTMENTS", "build_domain_read_model"]
