"""Regression pin: portfolio_recommendation Workers resolve their LLM through
Worker Model Gateway, not the Ollama-hardcoded default_worker_llm.

근거: 2026-08-24 evidence-wiring-audit - WorkerPerformancePanel의 입·출력 토큰이
영구 "미측정"이었던 원인은 라이브 오피스 Worker 실행이 실제 서빙 중인 모델(vLLM
Qwen2.5-14B-AWQ)이 아니라 employee_worker_runtime.default_worker_llm에 하드코딩된
Ollama 엔드포인트를 계속 불렀기 때문이다(worker_model_gateway.py는 만들어져 있었지만
research-mcp 하나에만 배선돼 있었다). 이 테스트는 그 배선이 실제 실행 경로에서 쓰이는지
고정한다 - gateway 함수 자체 계약은 worker_model_gateway.py 자체 점검이 이미 검증하므로,
여기서는 portfolio_recommendation.py가 그 함수를 실제로 호출하고 그 결과(model_name)가
성과 이벤트에 실리는지만 본다.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from orchestration.workflows import portfolio_recommendation as pr

AS_OF = datetime(2026, 8, 4, tzinfo=timezone.utc).isoformat()


def _profile(**overrides: Any) -> dict[str, Any]:
    value = {
        "user_id": "user-test-gateway-wiring",
        "mindset": "RISK_SEEKING",
        "experience": "INTERMEDIATE",
        "investment_horizon_years": 5,
        "max_drawdown_pct": "0.25",
        "liquidity_need": "MEDIUM",
        "as_of": AS_OF,
    }
    value.update(overrides)
    return value


def _candidate(portfolio_id: str, **overrides: Any) -> dict[str, Any]:
    value = {
        "portfolio_id": portfolio_id,
        "name": f"Portfolio {portfolio_id}",
        "risk_band": "MEDIUM",
        "minimum_experience": "BEGINNER",
        "minimum_horizon_years": 3,
        "max_drawdown_pct": "0.15",
        "max_exit_days": 14,
        "target_allocations": {"GLOBAL_EQUITY": "0.6", "SHORT_TERM_BOND": "0.4"},
        "evidence_refs": ["research:portfolio-catalog:v1"],
        "as_of": AS_OF,
    }
    value.update(overrides)
    return value


class _FakeBinding:
    """resolve()가 돌려주는 ModelBinding의 최소 대역 - model만 있으면 된다."""

    def __init__(self, model: str) -> None:
        self.model = model


def _fake_worker_response(worker_id: str) -> str:
    # _deterministic_worker_llm과 같은 계약 형태(summary/confidence/evidence_refs/escalate).
    return json.dumps(
        {
            "summary": f"gateway-routed context for {worker_id}",
            "confidence": 0.75,
            "evidence_refs": ["research:portfolio-catalog:v1"],
            "escalate": False,
        }
    )


def test_worker_execution_resolves_llm_through_gateway_not_hardcoded_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gateway를 patch하면 그 반환값이 실제 Worker 실행에 쓰여야 한다.

    이전 코드는 default_worker_llm(하드코딩 Ollama)을 무조건 썼다 - 여기서는
    llm_for_worker를 감시용 대역으로 바꿔치기해서, _run_worker가 그 대역을 실제로
    호출하고 대역이 돌려준 model 이름이 worker_completed 이벤트의
    performance.model_name에 그대로 실리는지 확인한다.
    """

    monkeypatch.setenv("PORTFOLIO_WORKER_RUNTIME", "ollama")  # use_ollama=True 강제
    sentinel_model = "gateway-test-marker-model"
    calls: list[str] = []

    def fake_llm_for_worker(worker_id: str | None = None, **_kwargs: Any):
        def call(system: str, prompt: str, *, json_schema: Any = None) -> str:
            calls.append(worker_id or "")
            return _fake_worker_response(worker_id or "")

        call._json_schema_capable = True  # type: ignore[attr-defined]
        return call, _FakeBinding(sentinel_model)

    monkeypatch.setattr(pr, "llm_for_worker", fake_llm_for_worker)

    events: list[dict[str, Any]] = []

    async def _run() -> dict[str, Any]:
        return await pr.run_portfolio_recommendation_pipeline_async(
            _profile(),
            [_candidate("balanced")],
            event_callback=events.append,
        )

    result = asyncio.run(_run())

    assert result["pipeline_status"] == "COMPLETED", result
    assert calls, "fake gateway LLM이 한 번도 안 불렸다 - 여전히 기존 Ollama 하드코딩 경로를 쓰고 있다"

    performances = [
        event["performance"]
        for event in events
        if event.get("kind") == "worker_completed" and isinstance(event.get("performance"), dict)
    ]
    assert performances, "worker_completed 이벤트에 performance가 안 실렸다"
    assert all(p.get("model_name") == sentinel_model for p in performances), performances
