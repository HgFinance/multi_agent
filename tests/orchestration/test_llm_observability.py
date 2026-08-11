import os

import pytest

from orchestration.llm_observability import (
    _metric_metadata,
    langfuse_enabled,
    langfuse_worker_event_name,
    publish_langfuse_metric,
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


@pytest.fixture(autouse=True)
def _clear_langfuse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """매 테스트 기본값이 꺼짐이어야 한다 - 실행 셸에 남은 값의 영향을 받지 않는다."""

    for key in ("LANGFUSE_TRACING", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
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
