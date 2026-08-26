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
        side_effect=[
            (None, "workforce_api_unreachable:ConnectionRefusedError"),
            ({"candidates": []}, None),
        ],
    ):
        context = fetch_workforce_advisory_context(_root_body("hr-department"))

    assert context is not None
    payload = json.loads(context)
    assert payload["observability_available"] is False
    assert payload["improvements_available"] is True
    # 2026-08-27: "못 받았다"만으로는 조치를 못 정한다 - 왜 못 받았는지가 같이 와야
    # Agent 가 "근거 부족"과 "관측 배관 장애"를 구분해 티켓을 낼 수 있다.
    assert payload["observability_error"] == "workforce_api_unreachable:ConnectionRefusedError"
    assert "improvements_error" not in payload


def test_total_failure_still_reports_why_instead_of_dropping_the_block() -> None:
    """둘 다 못 받아도 사유는 남긴다 (2026-08-27).

    이전에는 None 을 돌려줘 과제에 Workforce 블록 자체가 안 실렸다 - Agent 는
    조회를 시도했다는 사실조차 모른 채 "근거 없음"으로 결론지었다.
    """

    with patch(
        "orchestration.workforce_advisory_context._get_json",
        side_effect=[(None, "workforce_api_http_503"), (None, "workforce_api_http_503")],
    ):
        context = fetch_workforce_advisory_context(_root_body("hr-department"))

    assert context is not None
    payload = json.loads(context)
    assert payload["observability_available"] is False
    assert payload["improvements_available"] is False
    assert payload["observability_error"] == "workforce_api_http_503"


def test_unavailable_rows_keep_their_reason_and_observed_rows_stay_clean() -> None:
    """UNAVAILABLE 행의 사유는 투영에서 살아남고, 관측된 행은 None 으로 채우지 않는다."""

    observability = {
        "observed_at": "2026-08-27T00:00:00+00:00",
        "idle_agents": [
            {
                "department": "risk", "worker_id": "compliance-policy-worker",
                "trigger": "when_compliance_evidence_exists", "status": "UNAVAILABLE",
                "last_seen_at": None, "idle_hours": None,
                "reason": "langfuse_trace_list_failed:Error:http_400:Too big: expected number to be <=100",
            },
            {
                "department": "qa", "worker_id": "hallucination-critic-worker",
                "trigger": "when_unsupported_claim_exists", "status": "ACTIVE",
                "last_seen_at": "2026-08-27T00:00:00+00:00", "idle_hours": 0.5,
                "reason": None,
            },
        ],
    }
    with patch(
        "orchestration.workforce_advisory_context._get_json",
        side_effect=[(observability, None), ({"candidates": []}, None)],
    ):
        context = fetch_workforce_advisory_context(_root_body("hr-department"))

    payload = json.loads(context)
    unavailable, active = payload["idle_agents"]
    assert "http_400" in unavailable["reason"]
    assert "reason" not in active, "관측된 행에 None 사유가 실렸다"
