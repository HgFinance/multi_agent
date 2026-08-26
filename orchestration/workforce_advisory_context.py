"""Best-effort, read-only Workforce context for HR advisory tasks.

The Workforce API already owns department capacity, LLM usage, idle status,
trigger rates, and improvement-candidate state.  This adapter only transports
a bounded projection of those existing records into the assigned HR Kanban
task.  It does not calculate a second scorecard or grant HR any new authority.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from orchestration.ceo_workflow_scope import selected_primary_profiles_from_body


_HR_PROFILE = "hr-department"
_OBSERVABILITY_FIELDS: dict[str, tuple[str, ...]] = {
    "capacity": (
        "department",
        "status",
        "arrivals",
        "duration_p95_ms",
        "retry_rate",
        "error_rate",
        "utilization",
        "queue_p95_ms",
    ),
    "llm_usage": (
        "department",
        "status",
        "arrivals",
        "llm_calls",
        "prompt_tokens",
        "completion_tokens",
        "avg_attempts",
        "status_counts",
    ),
    "idle_agents": (
        "department",
        "worker_id",
        "trigger",
        "status",
        "last_seen_at",
        "idle_hours",
    ),
    "trigger_rates": (
        "department",
        "worker_id",
        "trigger",
        "status",
        "execution_count",
        "opportunity_count",
        "fire_rate",
    ),
}
_IMPROVEMENT_FIELDS = (
    "candidate_id",
    "target_type",
    "target_ref",
    "target_current_version",
    "expected_effect",
    "risk_class",
    "status",
)


def _timeout_seconds() -> float:
    try:
        return max(
            0.1,
            min(
                10.0,
                float(os.getenv("WORKFORCE_ADVISORY_TIMEOUT_SECONDS", "5")),
            ),
        )
    except ValueError:
        return 5.0


def _get_json(base_url: str, path: str) -> dict[str, Any] | None:
    request = urllib.request.Request(
        f"{base_url}{path}", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=_timeout_seconds()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return None
    return payload if isinstance(payload, dict) else None


def _compact_rows(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = payload.get(key)
    if not isinstance(rows, list):
        return []
    fields = _OBSERVABILITY_FIELDS[key]
    return [
        {field: row[field] for field in fields if field in row}
        for row in rows[:32]
        if isinstance(row, dict)
    ]


def _compact_context(
    observability: dict[str, Any] | None,
    improvements: dict[str, Any] | None,
) -> str | None:
    if observability is None and improvements is None:
        return None

    result: dict[str, Any] = {
        "contract": "hgfinance.workforce-advisory.v1",
        "source_of_record": "workforce-api",
        "raw_trace_payloads_included": False,
        "observability_available": observability is not None,
        "improvements_available": improvements is not None,
    }
    if observability is not None:
        result["observed_at"] = observability.get("observed_at")
        for key in _OBSERVABILITY_FIELDS:
            result[key] = _compact_rows(observability, key)
    if improvements is not None:
        candidates = improvements.get("candidates")
        if not isinstance(candidates, list):
            candidates = []
        result["improvement_candidate_count"] = len(candidates)
        result["improvement_candidates"] = [
            {
                field: candidate[field]
                for field in _IMPROVEMENT_FIELDS
                if field in candidate
            }
            for candidate in candidates[:20]
            if isinstance(candidate, dict)
        ]
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def fetch_workforce_advisory_context(root_body: str) -> str | None:
    """Fetch existing Workforce records once, only for an HR primary."""

    if _HR_PROFILE not in selected_primary_profiles_from_body(root_body):
        return None
    base_url = os.getenv("WORKFORCE_API_URL", "http://workforce-api:8000").strip().rstrip("/")
    if not base_url:
        return None
    observability = _get_json(
        base_url,
        "/workforce/v1/departments/observability"
        "?lookback_hours=24&idle_lookback_hours=24&idle_threshold_hours=4",
    )
    improvements = _get_json(base_url, "/workforce/v1/improvements")
    return _compact_context(observability, improvements)


__all__ = ["fetch_workforce_advisory_context"]
