from __future__ import annotations

import sys
from types import ModuleType

from orchestration import risk_observability


def test_risk_span_uses_sdk_compatible_tags_and_closes_successfully(monkeypatch):
    recorded: dict[str, object] = {}

    class FakeRun:
        def __init__(self, metadata):
            self.metadata = dict(metadata)
            self.outputs = {}

    class FakeContext:
        def __init__(self, run):
            self.run = run

        def __enter__(self):
            recorded["entered"] = True
            return self.run

        def __exit__(self, exc_type, exc, traceback):
            recorded["exit"] = (exc_type, exc, traceback)

    def fake_trace(name, **kwargs):
        recorded["name"] = name
        recorded["tags"] = kwargs["tags"]
        return FakeContext(FakeRun(kwargs["metadata"]))

    fake_langsmith = ModuleType("langsmith")
    fake_langsmith.trace = fake_trace
    monkeypatch.setitem(sys.modules, "langsmith", fake_langsmith)
    monkeypatch.setattr(risk_observability, "_client", lambda: object())
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")

    with risk_observability.risk_span(
        "risk.advisory",
        {"task_id": "task-1", "status": "running"},
        inputs={"task_id": "task-1", "input_chars": 12, "unsafe": "hidden"},
    ) as run:
        assert run is not None
        risk_observability.set_risk_span_outputs(
            run, {"status": "OK", "page_count": 1, "unsafe": "hidden"}
        )

    assert recorded["name"] == "risk.advisory"
    assert recorded["tags"] == ["hgfinance", "risk", "redacted"]
    assert run.outputs == {"status": "OK", "page_count": 1}
    assert run.metadata["status"] == "success"
    assert isinstance(run.metadata["duration_ms"], int)
    assert recorded["exit"] == (None, None, None)


def test_risk_span_preserves_business_status(monkeypatch):
    class FakeRun:
        def __init__(self, metadata):
            self.metadata = dict(metadata)

    class FakeContext:
        def __init__(self, run):
            self.run = run

        def __enter__(self):
            return self.run

        def __exit__(self, exc_type, exc, traceback):
            return None

    fake_langsmith = ModuleType("langsmith")
    fake_langsmith.trace = lambda _name, **kwargs: FakeContext(
        FakeRun(kwargs["metadata"])
    )
    monkeypatch.setitem(sys.modules, "langsmith", fake_langsmith)
    monkeypatch.setattr(risk_observability, "_client", lambda: object())
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")

    with risk_observability.risk_span(
        "risk.advisory", {"task_id": "task-1", "status": "running"}
    ) as run:
        run.metadata["status"] = "DEFER"

    assert run.metadata["status"] == "DEFER"
