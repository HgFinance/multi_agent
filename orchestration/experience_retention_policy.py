"""Small, shared policy primitives for MemoHarness D5 retention.

The policy is deliberately scoped to the D5 Experience Bank relation.  It
does not apply to Kanban, accounting, audit, or other control-plane tables.
"""

from __future__ import annotations

from typing import Any, Iterable


BYTES_PER_MB = 1024 * 1024
D5_WARNING_RELATION_BYTES = 30 * BYTES_PER_MB
D5_CLEANUP_RELATION_BYTES = 35 * BYTES_PER_MB
D5_EMERGENCY_RELATION_BYTES = 45 * BYTES_PER_MB
D5_WRITE_STOP_RELATION_BYTES = 48 * BYTES_PER_MB
D5_HARD_LIMIT_RELATION_BYTES = 50 * BYTES_PER_MB

D5_OPERATIONAL_RETENTION_DAYS = 14
D5_FAILURE_RETENTION_DAYS = 30
D5_SUCCESS_RETENTION_DAYS = 90
D5_PRESERVE_RECENT_DAYS = 14
D5_PRESERVE_LATEST_PER_GROUP = 3

OPERATIONAL_FAILURE_CODES = frozenset(
    {
        "PROVIDER_AUTH",
        "PROVIDER_QUOTA",
        "PROVIDER_UNAVAILABLE",
        "PROVIDER_TIMEOUT",
        "NETWORK_TIMEOUT",
        "RATE_LIMITED",
    }
)


def _normalized_codes(values: Iterable[Any] | None) -> set[str]:
    if values is None or isinstance(values, (str, bytes)):
        return set()
    return {str(value or "").strip().upper() for value in values if str(value or "").strip()}


def retention_bucket(success: bool, failure_codes: Iterable[Any] | None) -> str:
    """Return the retention class without inspecting free-form payloads."""

    if _normalized_codes(failure_codes) & OPERATIONAL_FAILURE_CODES:
        return "operational_failure"
    return "success" if success else "orchestration_failure"


def retention_days(success: bool, failure_codes: Iterable[Any] | None) -> int:
    return {
        "operational_failure": D5_OPERATIONAL_RETENTION_DAYS,
        "orchestration_failure": D5_FAILURE_RETENTION_DAYS,
        "success": D5_SUCCESS_RETENTION_DAYS,
    }[retention_bucket(success, failure_codes)]


def capacity_band(relation_size_bytes: int) -> str:
    size = max(0, int(relation_size_bytes))
    if size >= D5_HARD_LIMIT_RELATION_BYTES:
        return "hard_limit"
    if size >= D5_WRITE_STOP_RELATION_BYTES:
        return "write_stop"
    if size >= D5_EMERGENCY_RELATION_BYTES:
        return "emergency"
    if size >= D5_CLEANUP_RELATION_BYTES:
        return "cleanup"
    if size >= D5_WARNING_RELATION_BYTES:
        return "warning"
    return "normal"


__all__ = [
    "BYTES_PER_MB",
    "D5_CLEANUP_RELATION_BYTES",
    "D5_EMERGENCY_RELATION_BYTES",
    "D5_FAILURE_RETENTION_DAYS",
    "D5_HARD_LIMIT_RELATION_BYTES",
    "D5_OPERATIONAL_RETENTION_DAYS",
    "D5_PRESERVE_LATEST_PER_GROUP",
    "D5_PRESERVE_RECENT_DAYS",
    "D5_SUCCESS_RETENTION_DAYS",
    "D5_WARNING_RELATION_BYTES",
    "D5_WRITE_STOP_RELATION_BYTES",
    "OPERATIONAL_FAILURE_CODES",
    "capacity_band",
    "retention_bucket",
    "retention_days",
]
