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
    assert run.metadata["raw_payloads_sent"] is False
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


def test_risk_trace_sampling_keeps_diagnostic_work_and_samples_normal_work():
    environment = {
        "LANGSMITH_RISK_TRACE_SAMPLE_RATE": "0",
        "LANGSMITH_RISK_TRACE_SLOW_MS": "45000",
    }

    assert not risk_observability.risk_trace_should_publish(
        task_id="t_normal",
        status="completed",
        latency_ms=1_000,
        environment=environment,
    )
    assert not risk_observability.risk_trace_should_publish(
        task_id="t_normal",
        status="completed",
        latency_ms=1_000,
        environment=environment,
    )
    assert risk_observability.risk_trace_should_publish(
        task_id="t_error",
        status="completed",
        tool_error_count=1,
        latency_ms=1_000,
        environment=environment,
    )
    assert risk_observability.risk_trace_should_publish(
        task_id="t_legal",
        status="completed",
        legal_wiki_call_count=1,
        latency_ms=1_000,
        environment=environment,
    )
    assert risk_observability.risk_trace_should_publish(
        task_id="t_slow",
        status="completed",
        latency_ms=45_000,
        environment=environment,
    )
    assert risk_observability.risk_trace_should_publish(
        task_id="t_blocked",
        status="blocked",
        latency_ms=1_000,
        environment=environment,
    )


def test_risk_hermes_terminal_receipt_is_redacted_and_fail_open(monkeypatch):
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
    assert risk_observability.record_risk_hermes_terminal_activity(
        event_id="risk-terminal-1",
        task_id="t-risk-1",
        root_id="t-root-1",
        request_id="request-1",
        status="completed",
        started_ms=1_000,
        ended_ms=2_500,
        discord_status="sent",
        discord_channel_id="channel-1",
        discord_thread_id="thread-1",
        discord_message_id="message-1",
        environment={"RISK_API_URL": "http://risk-api:8000"},
    )

    payload = captured["payload"]
    assert payload["duration_ms"] == 1_500
    assert "result" not in payload
    assert "secret" not in json.dumps(payload)
    assert captured["timeout"] == 0.5


def test_risk_span_is_fail_open_when_langsmith_is_unavailable(monkeypatch):
    class UnavailableLangSmith:
        @staticmethod
        def trace(*_args, **_kwargs):
            raise RuntimeError("429 usage limit exceeded")

    monkeypatch.setitem(sys.modules, "langsmith", UnavailableLangSmith)
    monkeypatch.setattr(risk_observability, "_client", lambda: object())
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")

    with risk_observability.risk_span(
        "risk.advisory", {"task_id": "task-429", "status": "running"}
    ) as run:
        assert run is None
        business_result = {"status": "DEFER"}

    assert business_result == {"status": "DEFER"}


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
    client = type("FakeClient", (), {})()
    client.create_run = lambda **kwargs: setattr(client, "payload", kwargs)
    monkeypatch.setattr(risk_observability, "_client", lambda: client)
    environment = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "secret-not-sent",
        "LANGSMITH_ENDPOINT": "https://example.invalid",
        "LANGSMITH_PROJECT": "First",
    }

    assert risk_observability.publish_risk_hermes_profile(
        task_id="t_risk",
        task_body="workflow_root_task_id=t_root\nrequest_id=request-41",
        run_id="41",
        root_id="t_root",
        session_id=session,
        log_dir=tmp_path,
        started_ms=1_787_732_201_000,
        ended_ms=1_787_732_269_000,
        status="done",
        environment=environment,
    )

    payload = client.payload
    from scripts.hermes_worker_observability import department_worker_trace_identity

    identity = department_worker_trace_identity(
        task_id="t_risk",
        task_body="workflow_root_task_id=t_root",
        profile="risk-management",
        run_id="41",
        started_ms=1_787_732_201_000,
    )
    assert payload["name"] == "risk.hermes-worker-profile"
    assert payload["outputs"]["llm_call_count"] == 1
    assert payload["outputs"]["legal_wiki_call_count"] == 1
    assert payload["extra"]["metadata"]["raw_payloads_sent"] is False
    assert payload["extra"]["metadata"]["department"] == "risk"
    assert payload["extra"]["metadata"]["request_id"] == "request-41"
    assert payload["inputs"]["request_id"] == "request-41"
    assert payload["extra"]["metadata"]["trace_id"] == payload["trace_id"]
    assert payload["parent_run_id"] == identity["worker_run_id"]
    assert payload["trace_id"] == identity["trace_id"]
    assert payload["extra"]["metadata"]["parent_run_id"] == payload["parent_run_id"]
    assert payload["extra"]["metadata"]["latency_scope"] == "worker_execution"
    assert payload["extra"]["metadata"]["tool_latency_available"] is True
    assert "secret-not-sent" not in json.dumps(payload, default=str)
