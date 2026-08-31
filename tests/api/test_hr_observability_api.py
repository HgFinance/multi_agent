"""HTTP boundary tests for the HR unified Langfuse observability endpoint.

2026-08-26 통합. 이 파일은 test_hr_idle_agents_api.py / test_hr_capacity_api.py /
test_hr_llm_usage_api.py / test_hr_trigger_rates_api.py 넷을 대체한다 - 엔드포인트가
`GET /workforce/v1/departments/observability` 하나로 합쳐졌기 때문이다. 합친 근거는
app.py 의 해당 핸들러 머리말 참고(넷이 같은 Langfuse 이벤트를 각자 읽고 있었다).

여기서 지키는 계약은 통합 전과 같다:
  - Langfuse 자격증명이 없어도 200이고, 항목은 UNAVAILABLE 이다("모른다"를 "쉰다"로
    바꾸지 않는다).
  - Worker registry 부재만 503 이다(빈 목록=유휴 없음 으로 위장하지 않는다).
  - 창 파라미터가 0 이하면 422 다.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("dotenv")

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "departments/07-agent-workforce/api"))
sys.path.insert(0, str(ROOT / "departments/07-agent-workforce/scorecard"))
sys.path.insert(0, str(ROOT / "departments/07-agent-workforce"))

from workforce_api_loader import load_workforce_api

workforce_api = load_workforce_api()
from observability import WorkerRegistryUnavailable

PATH = "/workforce/v1/departments/observability"


def _without_langfuse(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    response = TestClient(workforce_api.app).get(PATH)
    assert response.status_code == 200
    return response.json()


def test_observability_returns_all_four_reports_in_one_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """네 관측이 한 응답에 다 있어야 한다 - 하나라도 빠지면 그 표가 조용히 빈다."""

    body = _without_langfuse(monkeypatch)
    for key in ("idle_agents", "capacity", "llm_usage", "trigger_rates", "kanban_latency"):
        assert body[key], f"{key} 가 비어 있다 - 통합 응답이 한 축을 잃었다"
    assert body["window_start"] < body["window_end"]


def test_observability_folds_missing_langfuse_into_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """자격증명 부재는 501/503 이 아니라 항목별 UNAVAILABLE 이다(통합 전과 동일)."""

    body = _without_langfuse(monkeypatch)

    assert all(worker["status"] == "UNAVAILABLE" for worker in body["idle_agents"])

    assert all(report["status"] == "UNAVAILABLE" for report in body["capacity"])
    assert all(report["arrivals"] is None for report in body["capacity"])
    assert all(report["queue_p95_ms"] is None for report in body["capacity"])

    assert all(report["status"] == "UNAVAILABLE" for report in body["llm_usage"])
    assert all(report["arrivals"] is None for report in body["llm_usage"])
    assert all(report["llm_calls"] is None for report in body["llm_usage"])

    assert all(report["status"] == "UNAVAILABLE" for report in body["trigger_rates"])
    assert all(report["execution_count"] is None for report in body["trigger_rates"])
    assert all(report["opportunity_count"] is None for report in body["trigger_rates"])
    assert all(report["fire_rate"] is None for report in body["trigger_rates"])


def test_observability_reports_zero_langfuse_queries_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reader 를 못 만들면 왕복이 0이어야 한다 - 실패 경로가 조용히 조회를 내면 안 된다."""

    body = _without_langfuse(monkeypatch)
    assert body["langfuse_queries"] == 0


def test_observability_keeps_missing_kanban_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_KANBAN_DB", "/not-mounted/kanban.db")
    body = _without_langfuse(monkeypatch)
    assert all(report["status"] == "UNAVAILABLE" for report in body["kanban_latency"])
    assert all(report["reason"] == "kanban_db_unavailable" for report in body["kanban_latency"])


def test_observability_keeps_registry_failure_as_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(**_kwargs: object) -> object:
        raise WorkerRegistryUnavailable("worker_registry_invalid:test")

    monkeypatch.setattr(workforce_api, "collect_workforce_observability", unavailable)
    response = TestClient(workforce_api.app).get(PATH)
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "worker_registry_unavailable"


def test_observability_rejects_non_positive_lookback_hours() -> None:
    response = TestClient(workforce_api.app).get(PATH, params={"lookback_hours": 0})
    assert response.status_code == 422


def test_observability_rejects_non_positive_idle_threshold_hours() -> None:
    response = TestClient(workforce_api.app).get(PATH, params={"idle_threshold_hours": 0})
    assert response.status_code == 422
