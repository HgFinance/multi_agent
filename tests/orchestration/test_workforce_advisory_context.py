from __future__ import annotations

import json
from unittest.mock import patch

from orchestration.workforce_advisory_context import (
    fetch_workforce_advisory_context,
)


def _root_body(profile: str) -> str:
    return (
        "hgfinance.ceo-workflow-scope.v1\n"
        "workflow_mode=analysis\n"
        f"selected_primary_profiles={profile}\n"
    )


def test_non_hr_primary_does_not_fetch_workforce_api() -> None:
    with patch(
        "orchestration.workforce_advisory_context.urllib.request.urlopen"
    ) as urlopen:
        assert fetch_workforce_advisory_context(
            _root_body("research-department")
        ) is None
    urlopen.assert_not_called()


def test_hr_context_reuses_existing_observability_and_improvement_endpoints(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKFORCE_API_URL", "http://workforce-api:8000")
    responses = {
        "/workforce/v1/departments/observability": {
            "observed_at": "2026-08-25T08:38:03+00:00",
            "capacity": [
                {
                    "department": "research",
                    "status": "MEASURED",
                    "arrivals": 97,
                    "duration_p95_ms": 10471.0,
                    "retry_rate": 0.268,
                    "error_rate": 0.134,
                    "secret": "must-not-pass",
                }
            ],
            "llm_usage": [],
            "idle_agents": [],
            "trigger_rates": [],
        },
        "/workforce/v1/improvements": {"candidates": []},
    }

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        assert timeout == 5.0
        path = request.full_url.split("?", 1)[0].removeprefix(
            "http://workforce-api:8000"
        )
        return Response(responses[path])

    with patch(
        "orchestration.workforce_advisory_context.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ) as urlopen:
        context = fetch_workforce_advisory_context(_root_body("hr-department"))

    assert context is not None
    payload = json.loads(context)
    assert urlopen.call_count == 2
    assert payload["contract"] == "hgfinance.workforce-advisory.v1"
    assert payload["capacity"][0]["department"] == "research"
    assert payload["capacity"][0]["error_rate"] == 0.134
    assert "secret" not in payload["capacity"][0]
    assert payload["improvement_candidate_count"] == 0
    assert payload["raw_trace_payloads_included"] is False


def test_hr_context_preserves_partial_availability() -> None:
    with patch(
        "orchestration.workforce_advisory_context._get_json",
        side_effect=[None, {"candidates": []}],
    ):
        context = fetch_workforce_advisory_context(_root_body("hr-department"))

    assert context is not None
    payload = json.loads(context)
    assert payload["observability_available"] is False
    assert payload["improvements_available"] is True
