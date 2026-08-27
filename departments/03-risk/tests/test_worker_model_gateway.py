"""Risk employee model-plane wiring tests (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

RISK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RISK_DIR))

import risk_employee_workers

from departments import worker_model_gateway


class _Binding:
    provider = "vllm-openai"
    model = "Qwen2.5-14B-Instruct-AWQ"


def _worker_json(_system: str, _prompt: str) -> str:
    return (
        '{"summary":"legal evidence reviewed","confidence":0.8,'
        '"evidence_refs":["law:1"],"escalate":false}'
    )


def test_risk_default_worker_uses_gateway_when_model_plane_is_configured(monkeypatch):
    monkeypatch.setenv("WORKER_MODEL_BASE_URL", "http://vllm:8000/v1")
    calls: list[str] = []

    def fake_llm_for_worker(worker_id: str):
        calls.append(worker_id)
        return _worker_json, _Binding()

    monkeypatch.setattr(worker_model_gateway, "llm_for_worker", fake_llm_for_worker)

    assert risk_employee_workers.default_worker_llm("system", "prompt") == (
        '{"summary":"legal evidence reviewed","confidence":0.8,'
        '"evidence_refs":["law:1"],"escalate":false}'
    )
    assert calls == ["compliance-policy-worker"]


def test_risk_report_exposes_qwen_gateway_runtime(monkeypatch):
    monkeypatch.setenv("WORKER_MODEL_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.setenv("WORKER_MODEL_NAME", "Qwen2.5-14B-Instruct-AWQ")

    def fake_resolve(worker_id: str | None = None):
        assert worker_id == "compliance-policy-worker"
        return _Binding()

    monkeypatch.setattr(worker_model_gateway, "resolve", fake_resolve)
    monkeypatch.setattr(
        worker_model_gateway,
        "llm_for_worker",
        lambda worker_id: (_worker_json, _Binding()),
    )

    report = risk_employee_workers.run_employee_workers(
        {
            "case_id": "risk-gateway-test",
            "query_mode": "LEGAL_QUERY",
            "compliance": {"query": "법률 근거를 확인해줘", "grounded": True},
            "assessment": {"verdict": "approve"},
            "trading_state": "ENABLED",
        },
        legal_answer_fn=lambda *_args: {
            "answer": {
                "verdict": "clear",
                "cited_documents": ["law:1"],
                "rationale": "검증된 근거",
                "confidence": 0.8,
                "escalate": False,
            },
            "pages_visited": ["law:1"],
            "context_chars": 100,
        },
    )

    assert report["runtime"]["provider"] == "vllm-openai"
    assert report["runtime"]["model"] == "Qwen2.5-14B-Instruct-AWQ"
    assert report["failed"] == []
    assert report["executed"] == ["compliance-policy-worker", "risk-runner"]
