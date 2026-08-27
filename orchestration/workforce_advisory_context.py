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
# ▶ 네 목록 모두 `reason` 을 싣는다 (2026-08-27). 이 필드가 없던 동안 HR Agent 는
#   전원 UNAVAILABLE 인 관측을 받고도 "관측 실패 사유가 이번 핸드오프에 제공되지
#   않았습니다"라고만 적을 수 있었다 - 실제 원인은 Langfuse limit 상수가 서버 상한을
#   넘겨 매 질의가 HTTP 400 이던 것이었고, 그 400 본문은 여기까지 오지 못했다.
#   투영이 사유를 빼면 "모른다"가 "왜 모르는지도 모른다"가 된다.
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
        "reason",
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
        "reason",
    ),
    "idle_agents": (
        "department",
        "worker_id",
        "trigger",
        "status",
        "last_seen_at",
        "idle_hours",
        "reason",
    ),
    "trigger_rates": (
        "department",
        "worker_id",
        "trigger",
        "status",
        "execution_count",
        "opportunity_count",
        "fire_rate",
        "reason",
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


def _get_json(base_url: str, path: str) -> tuple[dict[str, Any] | None, str | None]:
    """(payload, 실패 사유) 를 돌려준다 - 실패를 조용히 None 으로 접지 않는다.

    ▶ 2026-08-27. 이전에는 모든 예외를 `return None` 으로 삼켰고, 그 결과 HR
      과제에는 `observability_available: false` 한 줄만 남았다. workforce-api 가
      안 떠 있는 것(ConnectionRefused)과 503(WORKFORCE_API_URL 미설정)과 타임아웃은
      조치가 전부 다른데 Agent 에게는 셋 다 똑같아 보였다 - "근거가 부족합니다"로
      끝나는 답변의 절반이 여기서 나왔다.

      HTTP 상태 코드는 우리가 부른 우리 서비스의 응답이지 Trace 원문이 아니다 -
      redaction 규약과 무관하다.
    """

    request = urllib.request.Request(
        f"{base_url}{path}", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=_timeout_seconds()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, f"workforce_api_http_{exc.code}"
    except urllib.error.URLError as exc:
        return None, f"workforce_api_unreachable:{type(exc.reason).__name__}"
    except (json.JSONDecodeError, ValueError):
        return None, "workforce_api_response_not_json"
    except (OSError, TypeError) as exc:
        return None, f"workforce_api_request_failed:{type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, "workforce_api_response_not_an_object"
    return payload, None


def _compact_rows(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = payload.get(key)
    if not isinstance(rows, list):
        return []
    fields = _OBSERVABILITY_FIELDS[key]
    return [
        {
            field: row[field]
            for field in fields
            # 관측된 행의 reason 은 항상 None 이다 - 그 None 을 32행 * 4목록에
            # 실으면 잡음만 늘고 정작 사유가 있는 행이 묻힌다. 없는 키는 뺀다.
            if field in row and not (field == "reason" and row[field] is None)
        }
        for row in rows[:32]
        if isinstance(row, dict)
    ]


def _compact_context(
    observability: dict[str, Any] | None,
    improvements: dict[str, Any] | None,
    *,
    observability_error: str | None = None,
    improvements_error: str | None = None,
) -> str | None:
    if observability is None and improvements is None:
        # 둘 다 못 받았어도 **왜** 못 받았는지는 남긴다 - 여기서 None 을 돌려주면
        # 과제에 Workforce 블록 자체가 안 실리고, Agent 는 조회를 시도했다는
        # 사실조차 모른 채 "근거 없음"으로 결론짓는다.
        if not (observability_error or improvements_error):
            return None
        return json.dumps(
            {
                "contract": "hgfinance.workforce-advisory.v1",
                "source_of_record": "workforce-api",
                "raw_trace_payloads_included": False,
                "observability_available": False,
                "improvements_available": False,
                "observability_error": observability_error,
                "improvements_error": improvements_error,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    result: dict[str, Any] = {
        "contract": "hgfinance.workforce-advisory.v1",
        "source_of_record": "workforce-api",
        "raw_trace_payloads_included": False,
        "observability_available": observability is not None,
        "improvements_available": improvements is not None,
    }
    if observability_error:
        result["observability_error"] = observability_error
    if improvements_error:
        result["improvements_error"] = improvements_error
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
    observability, observability_error = _get_json(
        base_url,
        "/workforce/v1/departments/observability"
        "?lookback_hours=24&idle_threshold_hours=4",
    )
    improvements, improvements_error = _get_json(base_url, "/workforce/v1/improvements")
    return _compact_context(
        observability,
        improvements,
        observability_error=observability_error,
        improvements_error=improvements_error,
    )


__all__ = ["fetch_workforce_advisory_context"]
