import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestration.llm_observability import (
    _metric_metadata,
    close_root_trace,
    langfuse_enabled,
    langfuse_worker_event_name,
    langsmith_usage_limit_exhausted,
    _mark_langsmith_quota_pause,
    langsmith_project,
    publish_langfuse_metric,
    publish_metric,
    publish_root_trace,
    redacted_trace,
    start_root_trace,
    suppress_langsmith_automatic_tracing,
    trace_correlation_metadata,
    trace_should_publish,
    worker_graph_trace_config,
)


def test_monthly_unique_trace_limit_stops_observer_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hard monthly limit is not retried every cooldown window."""

    import orchestration.llm_observability as observability

    with observability._LANGSMITH_QUOTA_LOCK:
        observability._LANGSMITH_QUOTA_PAUSED_UNTIL = 0.0
        observability._LANGSMITH_USAGE_LIMITED = False

    try:
        _mark_langsmith_quota_pause(
            RuntimeError(
                "429 Too Many Requests: tenant exceeded usage limits: "
                "Monthly unique traces usage limit exceeded"
            )
        )
        assert langsmith_usage_limit_exhausted()
        assert not observability.langsmith_enabled()
    finally:
        with observability._LANGSMITH_QUOTA_LOCK:
            observability._LANGSMITH_QUOTA_PAUSED_UNTIL = 0.0
            observability._LANGSMITH_USAGE_LIMITED = False


@pytest.fixture(autouse=True)
def _emit_observability_events_in_unit_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep contract tests deterministic while production uses sampling."""

    monkeypatch.setenv("LANGSMITH_TRACE_SAMPLE_RATE", "1")
    monkeypatch.setenv("LANGSMITH_METRIC_SAMPLE_RATE", "1")


def test_langsmith_metric_allowlist_excludes_raw_payloads() -> None:
    safe = _metric_metadata(
        {
            "worker_id": "fundamental-valuation-worker",
            "model_name": "qwen3:1.7b",
            "latency_ms": 120,
            "prompt_tokens": 30,
            "completion_tokens": 12,
            "eval_score": 1.0,
            "prompt": "sensitive prompt text",
            "output": "sensitive completion text",
        },
        trace_id="trace-1",
    )

    assert safe == {
        "worker_id": "fundamental-valuation-worker",
        "model_name": "qwen3:1.7b",
        "latency_ms": 120,
        "prompt_tokens": 30,
        "completion_tokens": 12,
        "eval_score": 1.0,
        "trace_id": "trace-1",
    }


def test_langsmith_root_trace_allowlist_keeps_only_safe_correlation_metadata() -> None:
    safe = _metric_metadata(
        {
            "request_id": "discord:req-1",
            "root_id": "t_root",
            "task_id": "t_root",
            "department": "ceo-agent",
            "workflow_role": "root",
            "workflow_mode": "analysis",
            "provider": "openai-codex",
            "api_key": "must-not-appear",
            "prompt": "must-not-appear",
        }
    )

    assert safe == {
        "request_id": "discord:req-1",
        "root_id": "t_root",
        "task_id": "t_root",
        "department": "ceo-agent",
        "workflow_role": "root",
        "workflow_mode": "analysis",
        "provider": "openai-codex",
    }


def test_worker_trace_config_carries_request_root_task_correlation() -> None:
    correlation = trace_correlation_metadata(
        {
            "request_id": "req-1",
            "root_task_id": "root-1",
            "task_id": "task-1",
            "trace_id": "trace-1",
            "prompt": "must never be copied",
        },
        input_hash="sha256:ignored",
    )
    config = worker_graph_trace_config(
        stage="qa",
        worker_id="qa-worker",
        role="auditor",
        correlation=correlation,
        workflow_mode="analysis",
        analysis_mode="fast_advisory",
        configured_max_turns=8,
        actual_turns=5,
    )

    assert config["metadata"]["request_id"] == "req-1"
    assert config["metadata"]["root_id"] == "root-1"
    assert config["metadata"]["task_id"] == "task-1"
    assert config["metadata"]["trace_id"] == "trace-1"
    assert config["metadata"]["profile"] == "qa-department"
    assert config["metadata"]["department"] == "qa"
    assert config["metadata"]["workflow_mode"] == "analysis"
    assert config["metadata"]["analysis_mode"] == "fast_advisory"
    assert config["metadata"]["configured_max_turns"] == 8
    assert config["metadata"]["actual_turns"] == 5
    assert "prompt" not in config["metadata"]


def test_worker_trace_config_generates_complete_opaque_correlation() -> None:
    first = worker_graph_trace_config(stage="risk", worker_id="risk-worker")
    second = worker_graph_trace_config(stage="risk", worker_id="risk-worker")
    for key in ("request_id", "root_id", "task_id", "trace_id"):
        assert first["metadata"][key]
    assert first["metadata"]["trace_id"] != second["metadata"]["trace_id"]


def test_suppress_langsmith_automatic_tracing_uses_disabled_context(monkeypatch):
    events: list[object] = []

    class Context:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, *_args):
            events.append("exit")

    def fake_tracing_context(*, enabled):
        events.append(enabled)
        return Context()

    monkeypatch.setitem(
        sys.modules,
        "langsmith",
        SimpleNamespace(tracing_context=fake_tracing_context),
    )

    with suppress_langsmith_automatic_tracing():
        events.append("body")

    assert events == [False, "enter", "body", "exit"]


def test_trace_correlation_has_deterministic_legacy_fallbacks() -> None:
    correlation = trace_correlation_metadata({}, input_hash="sha256:abc123")

    assert correlation["request_id"] == "local:sha256:abc123"
    assert correlation["root_id"] == correlation["request_id"]
    assert correlation["task_id"].endswith("-task")


@pytest.fixture(autouse=True)
def _clear_langfuse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """매 테스트 기본값이 꺼짐이어야 한다 - 실행 셸에 남은 값의 영향을 받지 않는다."""

    for key in (
        "LANGFUSE_TRACING",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
    ):
        monkeypatch.delenv(key, raising=False)
    for key in (
        "LANGSMITH_TRACING",
        "LANGSMITH_ENDPOINT",
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
        "LANGSMITH_METRICS_PROJECT",
        "LANGSMITH_EVALS_PROJECT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_langfuse_worker_event_name_is_the_single_source_of_truth() -> None:
    """write 측(publish_langfuse_metric)과 read 측(HR observability.py)이 같은
    문자열을 조립해야 하므로, 포맷이 바뀌면 이 테스트가 먼저 깨져야 한다."""

    name = langfuse_worker_event_name(
        stage="research", worker_id="research-data-worker"
    )
    assert name == "llm.performance.metric:research:research-data-worker"


def test_langfuse_disabled_by_default() -> None:
    assert langfuse_enabled() is False


@pytest.mark.parametrize(
    "env",
    [
        {},  # 전부 미설정
        {"LANGFUSE_TRACING": "true"},  # key 없음
        {"LANGFUSE_TRACING": "true", "LANGFUSE_PUBLIC_KEY": "pk"},  # secret 없음
        {
            "LANGFUSE_TRACING": "false",
            "LANGFUSE_PUBLIC_KEY": "pk",
            "LANGFUSE_SECRET_KEY": "sk",
        },  # 스위치 꺼짐
    ],
)
def test_langfuse_requires_tracing_and_both_keys(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert langfuse_enabled() is False


def test_langfuse_enabled_when_switch_and_both_keys_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    assert langfuse_enabled() is True


def test_publish_langfuse_metric_is_false_and_silent_when_disabled() -> None:
    """기본(꺼짐) 상태에서는 예외 없이 False - 파이프라인을 막지 않는다."""

    result = publish_langfuse_metric(
        {"worker_id": "w", "stage": "research", "status": "COMPLETED", "attempts": 1},
        trace_id="t1",
    )
    assert result is False


def test_publish_langfuse_metric_never_raises_on_unreachable_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """자격증명은 있지만 host 가 존재하지 않을 때도 예외가 새어 나가면 안 된다 -
    관측 실패가 실제 파이프라인(portfolio_recommendation.py)을 죽이면 안 되기 때문."""

    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-fake")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-fake")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:1")
    result = publish_langfuse_metric(
        {
            "worker_id": "w",
            "stage": "research",
            "status": "COMPLETED",
            "attempts": 1,
            "error_count": 0,
        },
        trace_id="t1",
    )
    # create_event() 는 OTel 배치라 네트워크 실패와 무관하게 큐잉 성공 시 True 를
    # 돌려준다(llm_observability.py 의 publish_langfuse_metric docstring 참고) -
    # 여기서 검증하는 것은 "예외가 새지 않는다"이지 "전송이 확인됐다"가 아니다.
    assert result in (True, False)


def test_publish_root_trace_is_noop_without_tracing_or_key() -> None:
    assert publish_root_trace(request_id="discord:req-1", root_id="t_root") is False


def test_publish_root_trace_is_noop_when_sdk_client_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    import orchestration.llm_observability as observability

    monkeypatch.setattr(
        observability,
        "_safe_langsmith_client",
        lambda: (_ for _ in ()).throw(ImportError("langsmith unavailable")),
    )
    assert publish_root_trace(request_id="discord:req-1", root_id="t_root") is False


def test_publish_root_trace_swallows_client_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    import orchestration.llm_observability as observability

    client = Mock()
    client.create_run.side_effect = RuntimeError("network failure")
    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: client)

    assert publish_root_trace(request_id="discord:req-1", root_id="t_root") is False


def test_langsmith_projects_keep_workflow_and_metrics_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_PROJECT", "First")
    monkeypatch.setenv("LANGSMITH_METRICS_PROJECT", "HgFinance-Metrics")
    monkeypatch.setenv("LANGSMITH_EVALS_PROJECT", "HgFinance-Evals")

    assert langsmith_project("workflow") == "First"
    assert langsmith_project("metrics") == "HgFinance-Metrics"
    assert langsmith_project("evals") == "HgFinance-Evals"


def test_langsmith_workflow_project_never_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)

    assert langsmith_project("workflow") == "First"


def test_trace_sampling_keeps_failures_and_drops_ordinary_successes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACE_SAMPLE_RATE", "0")

    assert not trace_should_publish(identity="ordinary-success", status="completed")
    assert trace_should_publish(identity="failed-worker", status="failed")
    assert trace_should_publish(identity="slow-worker", status="completed", latency_ms=45_000)


def test_publish_metric_uses_metrics_project_without_creating_a_second_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")
    monkeypatch.setenv("LANGSMITH_PROJECT", "First")
    monkeypatch.setenv("LANGSMITH_METRICS_PROJECT", "HgFinance-Metrics")

    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: client)

    assert (
        publish_metric(
            {"worker_id": "qwen-risk-worker", "stage": "risk", "status": "COMPLETED"},
            trace_id="t_worker",
        )
        is True
    )

    assert client.create_run.call_count == 1
    kwargs = client.create_run.call_args.kwargs
    assert kwargs["project_name"] == "HgFinance-Metrics"
    assert kwargs["inputs"] == {}
    assert kwargs["outputs"] == {}
    assert kwargs["end_time"] is not None


def test_publish_metric_defaults_to_metrics_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: client)

    assert publish_metric({"worker_id": "qwen-research-worker"}) is True
    assert client.create_run.call_args.kwargs["project_name"] == "HgFinance-Metrics"


def test_publish_metric_aggregates_before_network_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")
    monkeypatch.setenv("LANGSMITH_METRIC_AGGREGATION_WINDOW_SECONDS", "300")
    # Aggregation must represent all local events even when one-run traces are
    # sampled out; sampling before reduction would bias the window statistics.
    monkeypatch.setenv("LANGSMITH_METRIC_SAMPLE_RATE", "0")

    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: client)
    observability._METRIC_AGGREGATES.clear()

    try:
        assert publish_metric(
            {
                "worker_id": "worker-a",
                "stage": "risk",
                "model_name": "qwen",
                "status": "COMPLETED",
                "latency_ms": 100,
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "max_tokens": 256,
            },
            trace_id="trace-a",
            aggregate=True,
        )
        assert publish_metric(
            {
                "worker_id": "worker-b",
                "stage": "risk",
                "model_name": "qwen",
                "status": "FAILED",
                "error_count": 1,
                "latency_ms": 200,
                "prompt_tokens": 30,
                "completion_tokens": 7,
                "max_tokens": 256,
            },
            trace_id="trace-b",
            aggregate=True,
        )
        client.create_run.assert_not_called()

        assert observability._flush_metric_aggregates(force=True) == 1
        kwargs = client.create_run.call_args.kwargs
        metadata = kwargs["extra"]["metadata"]
        assert metadata["metric_count"] == 2
        assert metadata["worker_count"] == 2
        assert metadata["error_count"] == 1
        assert metadata["failed_count"] == 1
        assert metadata["error_rate"] == 0.5
        assert metadata["prompt_tokens"] == 50
        assert metadata["completion_tokens"] == 12
        assert metadata["max_tokens"] == 256
        assert metadata["p95_latency_ms"] == 200
        assert kwargs["project_name"] == "HgFinance-Metrics"
    finally:
        observability._METRIC_AGGREGATES.clear()


def test_metric_aggregate_waits_through_langsmith_quota_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: client)
    observability._METRIC_AGGREGATES.clear()
    monkeypatch.setattr(observability, "langsmith_enabled", lambda: False)

    try:
        assert publish_metric(
            {
                "worker_id": "worker-quota-paused",
                "stage": "risk",
                "model_name": "qwen",
                "status": "COMPLETED",
                "latency_ms": 100,
            },
            aggregate=True,
        )
        assert observability._flush_metric_aggregates(force=True) == 0
        assert len(observability._METRIC_AGGREGATES) == 1
        monkeypatch.setattr(observability, "langsmith_enabled", lambda: True)
        assert observability._flush_metric_aggregates(force=True) == 1
        client.create_run.assert_called_once()
    finally:
        observability._METRIC_AGGREGATES.clear()


def test_publish_metric_can_confirm_quota_rejection_before_reporting_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: client)
    enabled = iter((True, False))
    monkeypatch.setattr(observability, "langsmith_enabled", lambda: next(enabled))

    assert (
        publish_metric(
            {"worker_id": "qa-department", "status": "COMPLETED"},
            confirm_delivery=True,
        )
        is False
    )
    client.flush.assert_called_once_with(timeout=3.0)


def test_publish_metric_preserves_explicit_name_and_terminal_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: client)
    started = datetime(2026, 8, 26, 8, 30, tzinfo=timezone.utc)
    ended = started + timedelta(seconds=121)

    assert publish_metric(
        {"worker_id": "qa-department", "status": "COMPLETED"},
        project_name="First",
        name="qa.hermes.terminal",
        start_time=started,
        end_time=ended,
    )

    kwargs = client.create_run.call_args.kwargs
    assert kwargs["name"] == "qa.hermes.terminal"
    assert kwargs["project_name"] == "First"
    assert kwargs["start_time"] == started
    assert kwargs["end_time"] == ended


def test_publish_root_trace_sends_empty_payload_with_correlation_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")
    monkeypatch.setenv("LANGSMITH_PROJECT", "First")

    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: client)

    assert (
        publish_root_trace(
            request_id="discord:req-1",
            root_id="t_root",
            workflow_mode="analysis",
            source="discord",
        )
        is True
    )

    kwargs = client.create_run.call_args.kwargs
    assert kwargs["inputs"] == {}
    assert kwargs["outputs"] == {}
    metadata = kwargs["extra"]["metadata"]
    assert metadata["request_id"] == "discord:req-1"
    assert metadata["root_id"] == "t_root"
    assert metadata["task_id"] == "t_root"
    assert metadata["trace_id"] == "discord:req-1"
    assert metadata["workflow_mode"] == "analysis"
    assert metadata["source"] == "discord"
    assert "api_key" not in metadata
    assert "prompt" not in metadata


def test_publish_root_trace_accepts_stable_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: client)

    assert publish_root_trace(
        request_id="root-retry",
        root_id="root-retry",
        run_id="00000000-0000-0000-0000-000000000001",
    )
    assert client.create_run.call_args.kwargs["id"] == (
        "00000000-0000-0000-0000-000000000001"
    )


def test_publish_completed_root_trace_includes_redacted_semantic_qa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")
    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: client)

    assert publish_root_trace(
        request_id="discord:req-1",
        root_id="t_root",
        status="completed",
        semantic_qa={
            "semantic_qa_version": "hgfinance.semantic-qa.v1",
            "semantic_qa_verdict": "PASS",
            "semantic_qa_score": 1.0,
            "raw_answer": "must not leave the boundary",
        },
    )
    metadata = client.create_run.call_args.kwargs["extra"]["metadata"]
    assert metadata["stage"] == "ceo-terminal"
    assert metadata["semantic_qa_verdict"] == "PASS"
    assert "raw_answer" not in metadata


def test_start_root_trace_posts_and_returns_only_dotted_order_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")
    monkeypatch.setenv("LANGSMITH_PROJECT", "First")

    class FakeRunTree:
        posted = 0
        instance = None

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            type(self).instance = self

        def post(self):
            type(self).posted += 1

        def to_headers(self):
            return {
                "langsmith-trace": "trace-root.00000000-0000-0000-0000-000000000001"
            }

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(RunTree=FakeRunTree))
    import orchestration.llm_observability as observability

    monkeypatch.setattr(observability, "_structured_langsmith_client", lambda: Mock())
    handle = start_root_trace(
        request_id="discord:req-1",
        workflow_mode="analysis",
        source="discord",
        query="삼성전자 분석; secret=do-not-send",
    )

    assert handle is not None
    assert handle.context.startswith("trace-root.")
    assert handle.run_id is None
    assert FakeRunTree.posted == 1
    assert FakeRunTree.instance.kwargs["project_name"] == "First"
    metadata = FakeRunTree.instance.kwargs["extra"]["metadata"]
    assert all(
        metadata[key] for key in ("request_id", "root_id", "task_id", "trace_id")
    )
    input_summary = FakeRunTree.instance.kwargs["inputs"]["summary"]
    assert input_summary["kind"] == "user_query"
    assert input_summary["present"] is True
    assert input_summary["raw_payloads_sent"] is False
    assert "secret" not in repr(FakeRunTree.instance.kwargs["inputs"])
    assert not hasattr(handle, "prompt")


def test_close_root_trace_updates_only_terminal_fields_without_renaming_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")
    monkeypatch.setenv("LANGSMITH_PROJECT", "First")

    class FakeRunTree:
        instance = None

        def __init__(self, **kwargs):
            self.id = "run-root-id"
            type(self).instance = self

        @classmethod
        def from_headers(cls, headers, **kwargs):
            assert headers == {"langsmith-trace": "trace-root"}
            return cls(**kwargs)

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(RunTree=FakeRunTree))
    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_structured_langsmith_client", lambda: client)
    assert (
        close_root_trace(
            "trace-root",
            request_id="discord:req-1",
            root_id="t_root",
            task_id="t_hr_primary",
            department="hr-department",
            workflow_mode="analysis",
            source="discord",
            status="completed",
            terminal_metadata={
                "http_status": 503,
                "error_code": "paper_order_hermes_runtime_unavailable",
                "terminal_reason": "HTTPException",
            },
        )
        is True
    )

    kwargs = client.update_run.call_args.kwargs
    assert kwargs["run_id"] == "run-root-id"
    assert kwargs["extra"]["metadata"]["root_id"] == "t_root"
    assert kwargs["extra"]["metadata"]["task_id"] == "t_hr_primary"
    assert kwargs["extra"]["metadata"]["department"] == "hr-department"
    assert kwargs["extra"]["metadata"]["raw_payloads_sent"] is False
    assert kwargs["extra"]["metadata"]["latency_scope"] == "end_to_end"
    assert kwargs["extra"]["metadata"]["http_status"] == 503
    assert (
        kwargs["extra"]["metadata"]["error_code"]
        == "paper_order_hermes_runtime_unavailable"
    )
    assert kwargs["extra"]["metadata"]["terminal_reason"] == "HTTPException"
    assert kwargs["error"] is None
    assert "name" not in kwargs
    assert "start_time" not in kwargs
    assert "inputs" not in kwargs
    assert kwargs["outputs"] == {
        "request_id": "discord:req-1",
        "root_id": "t_root",
        "task_id": "t_hr_primary",
        "department": "hr-department",
        "workflow_mode": "analysis",
        "source": "discord",
        "status": "completed",
        "terminal_status": "completed",
        "terminal_reason": "HTTPException",
        "error_code": "paper_order_hermes_runtime_unavailable",
        "http_status": 503,
    }


def test_close_root_trace_prefers_explicit_start_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    class FakeRunTree:
        @classmethod
        def from_headers(cls, *_args, **_kwargs):
            raise AssertionError("explicit run ID must bypass header reconstruction")

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(RunTree=FakeRunTree))
    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_structured_langsmith_client", lambda: client)

    assert close_root_trace(
        "trace-root",
        run_id="run-from-start",
        request_id="discord:req-1",
        status="completed",
    )
    assert client.update_run.call_args.kwargs["run_id"] == "run-from-start"
    client.flush.assert_not_called()


def test_close_root_trace_omits_mismatched_legacy_dotted_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    monkeypatch.setitem(
        sys.modules,
        "langsmith",
        SimpleNamespace(RunTree=SimpleNamespace),
    )
    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_structured_langsmith_client", lambda: client)

    assert close_root_trace(
        "20260827T090000000000Z11111111-1111-4111-8111-111111111111",
        run_id="22222222-2222-4222-8222-222222222222",
        request_id="request-1",
        status="completed",
    )

    kwargs = client.update_run.call_args.kwargs
    assert "trace_id" not in kwargs
    assert "dotted_order" not in kwargs


def test_close_root_trace_can_reconcile_without_persisted_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_structured_langsmith_client", lambda: client)

    assert close_root_trace(
        run_id="historical-pending-run",
        request_id="request-1",
        root_id="t_root",
        task_id="t_synthesis",
        status="completed",
    )
    assert client.update_run.call_args.kwargs["run_id"] == "historical-pending-run"
    assert client.update_run.call_args.kwargs["outputs"]["task_id"] == "t_synthesis"


def test_historical_close_updates_by_run_id_without_provider_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_structured_langsmith_client", lambda: client)

    assert close_root_trace(
        run_id="run-id",
        request_id="request-1",
        root_id="t_root",
        status="completed",
    )
    kwargs = client.update_run.call_args.kwargs
    assert kwargs["run_id"] == "run-id"


def test_duplicate_root_close_is_idempotent_without_provider_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    import orchestration.llm_observability as observability

    client = Mock()
    client.update_run.side_effect = RuntimeError(
        "Duplicate run update requests for the same run are not supported."
    )
    monkeypatch.setattr(observability, "_structured_langsmith_client", lambda: client)

    assert close_root_trace(
        "trace-root",
        run_id="run-from-start",
        request_id="discord:req-1",
        status="completed",
    )


def test_close_root_trace_recovers_legacy_root_id_from_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    class FakeRunTree:
        @classmethod
        def from_headers(cls, *_args, **_kwargs):
            raise AssertionError("legacy root context should resolve its UUID")

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(RunTree=FakeRunTree))
    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_structured_langsmith_client", lambda: client)

    assert close_root_trace(
        "20260826T052841584555Z01a03c8a-c170-72f1-ae24-b603a16f7dd6",
        request_id="discord:req-1",
        root_id="t_root",
        status="blocked",
        error_class="workflow_timeout_exceeded",
    )
    assert (
        client.update_run.call_args.kwargs["run_id"]
        == "01a03c8a-c170-72f1-ae24-b603a16f7dd6"
    )
    client.flush.assert_not_called()


def test_redacted_trace_is_noop_when_langsmith_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    import orchestration.llm_observability as observability

    monkeypatch.setattr(
        observability,
        "_safe_langsmith_client",
        lambda: (_ for _ in ()).throw(ImportError("langsmith unavailable")),
    )

    with redacted_trace(trace_id="t_root", model_name="model", stage="analysis"):
        assert True
