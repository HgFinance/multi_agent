from __future__ import annotations

import json

from departments.qwen_hybrid_runtime import (
    prepare_request,
    validate_financial_semantics,
    validate_prompt_financial_alignment,
)
from departments.worker_model_gateway import (
    HybridStructuredOutputError,
    ModelBinding,
    worker_llm,
)

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
    assert calls[0]["max_tokens"] == 256
    assert calls[0]["stop"] == ["<|im_end|>", "<|endoftext|>"]
    assert calls[1]["max_tokens"] == 192
    assert calls[1]["model"] == "qwen2.5-14b-instruct-awq"
    assert "failed the application contract" in calls[1]["messages"][1]["content"]


def test_length_termination_has_one_bounded_retry(monkeypatch):
    calls: list[dict] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": '{"value":'},
                        }
                    ]
                }
            ).encode()

    def fake_urlopen(request, timeout):
        calls.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    schema = {"type": "object", "properties": {"value": {"type": "number"}}}
    try:
        worker_llm(_binding())("system", "15% 계산", json_schema=schema)
    except HybridStructuredOutputError as exc:
        assert "after bounded length retry" in str(exc)
        assert exc.retryable is False
    else:
        raise AssertionError("length 종료는 실패 폐쇄되어야 한다")
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == 256
    assert calls[1]["max_tokens"] == 384


def test_financial_semantic_mismatch_is_repaired_once(monkeypatch):
    calls: list[dict] = []
    responses = iter(
        [
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"price":100,"quantity":2,"notional":300}'
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"price":100,"quantity":2,"notional":200}'
                        },
                    }
                ]
            },
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
        "properties": {
            "price": {"type": "number"},
            "quantity": {"type": "number"},
            "notional": {"type": "number"},
        },
        "required": ["price", "quantity", "notional"],
    }
    result = worker_llm(_binding())(
        "Return JSON.", "100원 자산 2주의 notional을 계산하세요.", json_schema=schema
    )
    assert json.loads(result)["notional"] == 200
    assert len(calls) == 2
    assert calls[1]["model"] == "qwen2.5-14b-instruct-awq"
    assert "financial semantic mismatch" in calls[1]["messages"][1]["content"]


def test_financial_semantic_validator_never_supplies_an_answer():
    assert validate_financial_semantics(
        '{"entry_price":100,"stop_price":90,"quantity":3,'
        '"position_risk_amount":30}'
    ) is None
    error = validate_financial_semantics(
        '{"entry_price":100,"stop_price":90,"quantity":3,'
        '"position_risk_amount":300}'
    )
    assert error is not None
    assert "position_risk_amount" in error


def test_prompt_alignment_rejects_only_unambiguous_source_drift():
    prompt = "1603000원 주식을 2주 매수할 때 JSON으로 답하세요."
    assert validate_prompt_financial_alignment(
        prompt, '{"price":1603000,"quantity":2,"notional":3206000}'
    ) is None
    assert validate_prompt_financial_alignment(
        prompt, '{"price":801500,"quantity":2,"notional":1603000}'
    ) == "financial source alignment mismatch: price"

    # Buy/sell legs expose multiple prices, so a generic gateway must not guess
    # which one a caller's ``price`` field represents.
    ambiguous = "Bought 2 shares at KRW 48000 and sold 2 shares at KRW 51500."
    assert validate_prompt_financial_alignment(
        ambiguous, '{"price":51500,"quantity":2}'
    ) is None
