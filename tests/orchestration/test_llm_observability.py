from orchestration.llm_observability import _metric_metadata


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
