"""Static and network-free contract tests for the Qwen AWQ v1 model plane."""

from __future__ import annotations

import json
from pathlib import Path

from departments.worker_model_gateway import DEFAULT_VLLM_MODEL, resolve


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "departments/01-research/config/worker_model_registry.json"
MODEL_COMPOSE = ROOT / "docker-compose.model.yml"
RUNTIME_SCRIPT = ROOT / "scripts/model_plane/vllm_runtime.sh"

EXPECTED_LLM_WORKERS = {
    "executive-briefing-worker",
    "competing-explanation-worker",
    "holdings-analyst-worker",
    "compliance-policy-worker",
    "result-interpretation-worker",
    "strategy-author-worker",
    "exception-investigation-worker",
    "hallucination-critic-worker",
    "incident-postmortem-worker",
    "profile-architecture-worker",
}


def test_registry_covers_every_llm_worker_and_keeps_adapter_explicit() -> None:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    assert registry["runtime_profile"] == "qwen-awq-v1"
    assert registry["base_model"]["served_model_name"] == DEFAULT_VLLM_MODEL
    assert set(registry["workers"]) == EXPECTED_LLM_WORKERS
    assert all(item["status"] == "base_model" for item in registry["workers"].values())
    adapter = registry["adapters"]["hgfinance-awq-arithmetic-2epoch"]
    assert adapter["status"] == "available_for_explicit_route"
    assert adapter["quality_gate"] == "FinanceBench_HOLD"


def test_gateway_resolves_qwen_v1_for_every_worker_without_network() -> None:
    env = {
        "WORKER_MODEL_BASE_URL": "http://vllm:8000/v1",
        "WORKER_MODEL_NAME": DEFAULT_VLLM_MODEL,
        "WORKER_MODEL_REGISTRY_PATH": str(REGISTRY),
    }
    for worker_id in EXPECTED_LLM_WORKERS:
        binding = resolve(worker_id, env=env)
        assert binding.provider == "vllm-openai"
        assert binding.model == DEFAULT_VLLM_MODEL
        assert binding.adapter_id is None


def test_runtime_entrypoint_is_pinned_and_fail_closed() -> None:
    compose = MODEL_COMPOSE.read_text(encoding="utf-8")
    script = RUNTIME_SCRIPT.read_text(encoding="utf-8")
    digest = "sha256:0a51ea5b4ae2dc5d81890e5173f54203d2a3ae0cfffe51b8fd2afd4391bfd967"
    assert "vllm/vllm-openai:latest" not in compose
    assert digest in compose
    assert "com.hgfinance.runtime.owner: compose" in compose
    assert "com.hgfinance.runtime.launcher: scripts/model_plane/vllm_runtime.sh" in compose
    assert "refusing to stop or remove it" in script
    assert "docker run" not in script
    assert "check_duplicate_model_containers" in script
