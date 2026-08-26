from __future__ import annotations

import json
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


def test_profiles_one_risk_hermes_session_without_payloads(tmp_path):
    session = "20260826_073418_027d09"
    (tmp_path / "agent.log").write_text(
        "\n".join(
            (
                (
                    f"2026-08-26 07:34:38,361 INFO [{session}] "
                    "agent.conversation_loop: API call #1: model=gpt-5.6-luna "
                    "provider=openai-codex in=25201 out=35 total=25236 latency=8.3s"
                ),
                (
                    f"2026-08-26 07:35:09,271 INFO [{session}] "
                    "agent.tool_executor: tool "
                    "mcp__risk_legal__query_risk_legal_wiki "
                    "completed (9.03s, 1582 chars)"
                ),
                (
                    "2026-08-26 07:35:15,849 INFO agent.tool_executor: "
                    "tool web_search completed (1.12s, 5964 chars)"
                ),
                (
                    f"2026-08-26 07:35:32,213 INFO [{session}] "
                    "agent.conversation_loop: API call #2: model=gpt-5.6-luna "
                    "provider=openai-codex in=61535 out=244 total=61779 latency=38.9s"
                ),
                (
                    f"2026-08-26 07:35:32,230 WARNING [{session}] "
                    "agent.tool_executor: Tool execute_code returned error "
                    "(0.00s): hidden"
                ),
                (
                    f"2026-08-26 07:35:42,123 INFO [{session}] "
                    "agent.conversation_loop: Turn ended: reason=text_response"
                ),
            )
        ),
        encoding="utf-8",
    )

    profile = risk_observability.profile_risk_hermes_session(tmp_path, session)

    assert profile["llm_call_count"] == 2
    assert profile["llm_latency_ms_total"] == 47_200
    assert profile["llm_latency_ms_max"] == 38_900
    assert profile["llm_context_growth_tokens"] == 36_334
    assert profile["legal_wiki_call_count"] == 1
    assert profile["web_tool_call_count"] == 1
    assert profile["tool_error_count"] == 1
    assert profile["code_tool_block_count"] == 1
    assert "hidden" not in json.dumps(profile)


def test_publishes_idempotent_redacted_risk_worker_profile(monkeypatch, tmp_path):
    session = "20260826_081649_fa4d32"
    (tmp_path / "agent.log").write_text(
        f"2026-08-26 08:17:01,741 INFO [{session}] "
        "agent.conversation_loop: API call #1: model=gpt-5.6-luna "
        "provider=openai-codex in=25030 out=44 total=25074 latency=3.6s\n"
        f"2026-08-26 08:17:28,774 INFO [{session}] "
        "agent.tool_executor: tool mcp__risk_legal__query_risk_legal_wiki "
        "completed (9.93s, 1700 chars)\n"
        f"2026-08-26 08:18:00,178 INFO [{session}] "
        "agent.conversation_loop: Turn ended: reason=text_response\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(risk_observability.urllib_request, "urlopen", fake_urlopen)
    environment = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "secret-not-sent",
        "LANGSMITH_ENDPOINT": "https://example.invalid",
        "LANGSMITH_PROJECT": "First",
    }

    assert risk_observability.publish_risk_hermes_profile(
        task_id="t_risk",
        root_id="t_root",
        session_id=session,
        log_dir=tmp_path,
        started_ms=1_787_732_201_000,
        ended_ms=1_787_732_269_000,
        status="done",
        environment=environment,
    )

    payload = captured["payload"]["post"][0]
    assert payload["name"] == "risk.hermes-worker-profile"
    assert payload["outputs"]["llm_call_count"] == 1
    assert payload["outputs"]["legal_wiki_call_count"] == 1
    assert payload["extra"]["metadata"]["raw_payloads_sent"] is False
    assert "secret-not-sent" not in json.dumps(payload)
    assert captured["timeout"] == 3.0
