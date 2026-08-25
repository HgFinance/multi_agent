from __future__ import annotations

import json

from departments.qwen_hybrid_runtime import prepare_request
from departments.worker_model_gateway import ModelBinding, worker_llm

HYBRID = {
    "version": "awq-hybrid-upgrade-v1",
    "status": "enabled",
    "numeric_adapter_model": "hgfinance-awq-arithmetic-2epoch",
    "unit_scale_normalization": True,
    "glossary_enabled": True,
    "glossary_path": "benchmarks/quantization/knowledge/bok800_2026/glossary_rag_v1.json",
}


def _binding() -> ModelBinding:
    return ModelBinding(
        provider="vllm-openai",
        base_url="http://vllm:8000/v1",
        model="qwen2.5-14b-instruct-awq",
        base_model="qwen2.5-14b-instruct-awq",
        adapter_id=None,
        adapter_version="none",
        api_key="vllm",
        timeout_seconds=120,
        hybrid_config=HYBRID,
    )


def test_numeric_requests_select_upgrade_adapter_and_unit_contract():
    prepared = prepare_request(
        system="You are a finance worker.",
        prompt="매출 1억원이 15% 감소하면 얼마인지 계산하세요.",
        base_model="qwen2.5-14b-instruct-awq",
        config=HYBRID,
    )
    assert prepared.model == "hgfinance-awq-arithmetic-2epoch"
    assert prepared.unit_scale_applied is True
    assert "0.015%" in prepared.system
    assert prepared.route == "numeric_unit_scale"


def test_non_numeric_requests_keep_qwen_base_model():
    prepared = prepare_request(
        system="You are a policy worker.",
        prompt="제공된 정책 근거의 불확실성을 요약하세요.",
        base_model="qwen2.5-14b-instruct-awq",
        config=HYBRID,
    )
    assert prepared.model == "qwen2.5-14b-instruct-awq"
    assert prepared.unit_scale_applied is False


def test_guided_json_is_repaired_once_without_answer_fallback(monkeypatch):
    calls: list[dict] = []
    responses = iter(
        [
            {"choices": [{"message": {"content": '{"value":"wrong"}'}}]},
            {"choices": [{"message": {"content": '{"value":0.15}'}}]},
        ]
    )

    class Response:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.value).encode()

    def fake_urlopen(request, timeout):
        calls.append(json.loads(request.data))
        return Response(next(responses))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    schema = {
        "type": "object",
        "properties": {"value": {"type": "number", "minimum": 0, "maximum": 1}},
        "required": ["value"],
        "additionalProperties": False,
    }
    result = worker_llm(_binding())(
        "Return the requested result.",
        "15%를 분수로 계산하세요.",
        json_schema=schema,
    )
    assert json.loads(result) == {"value": 0.15}
    assert len(calls) == 2
    assert calls[0]["model"] == "hgfinance-awq-arithmetic-2epoch"
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert "failed the application contract" in calls[1]["messages"][1]["content"]
