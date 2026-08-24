import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestration.llm_observability import (
    _metric_metadata,
    langfuse_enabled,
    langfuse_worker_event_name,
    langsmith_project,
    close_root_trace,
    publish_metric,
    publish_langfuse_metric,
    publish_root_trace,
    redacted_trace,
    start_root_trace,
)


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


@pytest.fixture(autouse=True)
def _clear_langfuse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """매 테스트 기본값이 꺼짐이어야 한다 - 실행 셸에 남은 값의 영향을 받지 않는다."""

    for key in ("LANGFUSE_TRACING", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
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

    name = langfuse_worker_event_name(stage="research", worker_id="research-data-worker")
    assert name == "llm.performance.metric:research:research-data-worker"


def test_langfuse_disabled_by_default() -> None:
    assert langfuse_enabled() is False


@pytest.mark.parametrize(
    "env",
    [
        {},  # 전부 미설정
        {"LANGFUSE_TRACING": "true"},  # key 없음
        {"LANGFUSE_TRACING": "true", "LANGFUSE_PUBLIC_KEY": "pk"},  # secret 없음
        {"LANGFUSE_TRACING": "false", "LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"},  # 스위치 꺼짐
    ],
)
def test_langfuse_requires_tracing_and_both_keys(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert langfuse_enabled() is False


def test_langfuse_enabled_when_switch_and_both_keys_present(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_publish_langfuse_metric_never_raises_on_unreachable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """자격증명은 있지만 host 가 존재하지 않을 때도 예외가 새어 나가면 안 된다 -
    관측 실패가 실제 파이프라인(portfolio_recommendation.py)을 죽이면 안 되기 때문."""

    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-fake")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-fake")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:1")
    result = publish_langfuse_metric(
        {"worker_id": "w", "stage": "research", "status": "COMPLETED", "attempts": 1, "error_count": 0},
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


def test_langsmith_projects_keep_workflow_and_metrics_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_PROJECT", "First")
    monkeypatch.setenv("LANGSMITH_METRICS_PROJECT", "HgFinance-Metrics")
    monkeypatch.setenv("LANGSMITH_EVALS_PROJECT", "HgFinance-Evals")

    assert langsmith_project("workflow") == "First"
    assert langsmith_project("metrics") == "HgFinance-Metrics"
    assert langsmith_project("evals") == "HgFinance-Evals"


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

    assert publish_metric(
        {"worker_id": "qwen-risk-worker", "stage": "risk", "status": "COMPLETED"},
        trace_id="t_worker",
    ) is True

    assert client.create_run.call_count == 1
    kwargs = client.create_run.call_args.kwargs
    assert kwargs["project_name"] == "HgFinance-Metrics"
    assert kwargs["inputs"] == {}
    assert kwargs["outputs"] == {}


def test_publish_metric_defaults_to_metrics_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")

    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: client)

    assert publish_metric({"worker_id": "qwen-research-worker"}) is True
    assert client.create_run.call_args.kwargs["project_name"] == "HgFinance-Metrics"


def test_publish_root_trace_sends_empty_payload_with_correlation_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")
    monkeypatch.setenv("LANGSMITH_PROJECT", "First")

    import orchestration.llm_observability as observability

    client = Mock()
    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: client)

    assert publish_root_trace(
        request_id="discord:req-1",
        root_id="t_root",
        workflow_mode="analysis",
        source="discord",
    ) is True

    kwargs = client.create_run.call_args.kwargs
    assert kwargs["inputs"] == {}
    assert kwargs["outputs"] == {}
    metadata = kwargs["extra"]["metadata"]
    assert metadata["request_id"] == "discord:req-1"
    assert metadata["root_id"] == "t_root"
    assert metadata["workflow_mode"] == "analysis"
    assert metadata["source"] == "discord"
    assert "api_key" not in metadata
    assert "prompt" not in metadata


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

    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: Mock())
    handle = start_root_trace(
        request_id="discord:req-1",
        workflow_mode="analysis",
        source="discord",
    )

    assert handle is not None
    assert handle.context.startswith("trace-root.")
    assert FakeRunTree.posted == 1
    assert FakeRunTree.instance.kwargs["project_name"] == "First"
    assert not hasattr(handle, "prompt")


def test_close_root_trace_ends_and_patches_without_inputs_or_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-not-printed")
    monkeypatch.setenv("LANGSMITH_PROJECT", "First")

    class FakeRunTree:
        instance = None

        def __init__(self, **kwargs):
            self.end_kwargs = None
            self.patch_kwargs = None
            type(self).instance = self

        @classmethod
        def from_headers(cls, headers, **kwargs):
            assert headers == {"langsmith-trace": "trace-root"}
            return cls(**kwargs)

        def end(self, **kwargs):
            self.end_kwargs = kwargs

        def patch(self, **kwargs):
            self.patch_kwargs = kwargs

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(RunTree=FakeRunTree))
    import orchestration.llm_observability as observability

    monkeypatch.setattr(observability, "_safe_langsmith_client", lambda: Mock())
    assert close_root_trace(
        "trace-root",
        request_id="discord:req-1",
        root_id="t_root",
        workflow_mode="analysis",
        source="discord",
        status="completed",
    ) is True

    run = FakeRunTree.instance
    assert run.end_kwargs["metadata"]["root_id"] == "t_root"
    assert run.end_kwargs["metadata"]["raw_payloads_sent"] is False
    assert run.end_kwargs["error"] is None
    assert run.patch_kwargs == {"exclude_inputs": True}


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
