from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("root_stress_test", ROOT / "scripts/stress_test.py")
assert SPEC is not None and SPEC.loader is not None
stress_test = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stress_test
SPEC.loader.exec_module(stress_test)


def test_percentile_is_bounded_and_deterministic() -> None:
    assert stress_test.percentile([], 0.95) is None
    assert stress_test.percentile([30.0, 10.0, 20.0], 0.50) == 20.0
    assert stress_test.percentile([30.0, 10.0, 20.0], 0.99) == 30.0


def test_e2e_runner_fetches_the_user_facing_result(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_request(
        url: str,
        *,
        method: str = "GET",
        body=None,
        headers=None,
        timeout: float,
    ):
        del body, headers, timeout
        calls.append((method, url))
        if method == "POST":
            return 202, 2.0, {"task_id": "task-1"}, None
        if url.endswith("/result"):
            return 200, 3.0, {"status": "completed", "result": {"summary": "ok"}}, None
        return (
            200,
            1.0,
            {
                "status": "completed",
                "created_at": "2026-08-30T00:00:00+00:00",
                "completed_at": "2026-08-30T00:00:01+00:00",
            },
            None,
        )

    monkeypatch.setattr(stress_test, "_request", fake_request)
    result = stress_test._run_e2e(
        stress_test.SCENARIOS[0],
        base_url="http://127.0.0.1:8001",
        query="상태를 요약해줘",
        headers={},
        timeout=1.0,
        poll_interval=0.05,
        workflow_timeout=1.0,
        allow_workflow=True,
    )

    assert result["status"] == "PASS"
    assert result["final_result_status"] == 200
    assert result["final_result_ms"] == 3.0
    assert calls[-1] == ("GET", "http://127.0.0.1:8001/ui/ceo/tasks/task-1/result")


def test_read_only_targets_are_configurable_without_changing_scenario_paths() -> None:
    scenario = stress_test.SCENARIO_BY_NAME["risk_observability"]
    assert stress_test._scenario_url(
        scenario,
        base_url="http://bff.example.test",
        service_urls={"risk_observability": "https://risk.example.test/"},
    ) == "https://risk.example.test/risk/v1/observability/runtime"


def test_required_service_targets_fail_closed_for_ci() -> None:
    scenarios = stress_test._load_scenarios("read_only")
    try:
        stress_test._require_service_urls(scenarios, {})
    except ValueError as exc:
        assert "research_health" in str(exc)
    else:
        raise AssertionError("missing CI service targets must fail closed")


def test_service_url_json_rejects_e2e_override() -> None:
    try:
        stress_test._service_urls(
            '{"ceo_readonly_e2e":"https://ceo.example.test"}',
            [],
        )
    except ValueError as exc:
        assert "read-only scenario" in str(exc)
    else:
        raise AssertionError("E2E target must not be accepted as a read-only override")
