"""Native Langfuse spans/generations must stay redacted and non-binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

import orchestration.llm_observability as observability


class _FakeObservation:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def update(self, **kwargs: Any) -> None:
        self.row.setdefault("updates", []).append(kwargs)


class _FakeScope:
    def __init__(self, client: "_FakeLangfuseClient", kwargs: dict[str, Any]) -> None:
        self.client = client
        self.row = dict(kwargs)
        self.row["parent"] = client.active[-1] if client.active else None
        self.observation = _FakeObservation(self.row)

    def __enter__(self) -> _FakeObservation:
        self.client.rows.append(self.row)
        self.client.active.append(self.row["name"])
        return self.observation

    def __exit__(self, *_: Any) -> None:
        self.client.active.pop()
        self.row["ended"] = True


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.active: list[str] = []

    def start_as_current_observation(self, **kwargs: Any) -> _FakeScope:
        return _FakeScope(self, kwargs)


@dataclass
class _Usage:
    prompt_tokens: int
    completion_tokens: int


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeLangfuseClient:
    captured = _FakeLangfuseClient()
    monkeypatch.setattr(observability, "langfuse_enabled", lambda: True)
    monkeypatch.setattr(observability, "_safe_langfuse_client", lambda: captured)
    return captured


def test_native_worker_span_and_generation_are_nested_and_redacted(
    client: _FakeLangfuseClient,
) -> None:
    token = observability.begin_worker_metric(
        worker_id="demo-worker", role="Demo", stage="research", model_name="qwen3:1.7b",
    )
    try:
        with observability.redacted_langfuse_worker_span(
            worker_id="demo-worker", role="Demo", stage="research", trace_id="case-1",
        ):
            with observability.redacted_current_worker_generation() as generation:
                generation.set_usage(_Usage(prompt_tokens=31, completion_tokens=17))
    finally:
        observability.end_worker_metric(token, status="COMPLETED", attempts=1, eval_score=None)

    span, generation = client.rows
    assert span["as_type"] == "span"
    assert span["name"] == "worker.run"
    assert generation["as_type"] == "generation"
    assert generation["name"] == "ollama.chat.completions"
    assert generation["model"] == "qwen3:1.7b"
    assert generation["parent"] == "worker.run"
    assert span["metadata"]["application_trace_id"] == "case-1"
    assert "latency_ms" not in generation["metadata"]
    assert "prompt_tokens" not in generation["metadata"]
    assert generation["updates"] == [{"usage_details": {"input": 31, "output": 17, "total": 48}}]
    assert span["ended"] and generation["ended"]


def test_native_observation_start_failure_does_not_change_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "langfuse_enabled", lambda: True)
    monkeypatch.setattr(
        observability, "_safe_langfuse_client", lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    with observability.redacted_langfuse_worker_span(
        worker_id="demo-worker", role="Demo", stage="research",
    ):
        assert 2 + 2 == 4
