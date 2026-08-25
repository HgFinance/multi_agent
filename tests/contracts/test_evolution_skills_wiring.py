"""Deployment contract for the governed Evolution Skills worker."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_model_overlay_runs_14b_proposal_worker_with_read_only_canonical_skills() -> None:
    overlay = yaml.safe_load((ROOT / "docker-compose.model.yml").read_text(encoding="utf-8"))
    service = overlay["services"]["skill-evolution-worker"]
    command = [str(value) for value in service["command"]]

    assert service["depends_on"]["vllm"]["condition"] == "service_healthy"
    assert service["environment"]["WORKER_MODEL_NAME"] == "${WORKER_MODEL_NAME:-qwen2.5-14b-instruct-awq}"
    assert command.count("--department") == 2
    assert "01-research" in command and "04-quant-backtest" in command
    assert "http://vllm:8000/v1" in command
    assert "./skills:/opt/shared-skills:ro" in service["volumes"]
    assert any("evolution-skills" in volume and not volume.endswith(":ro") for volume in service["volumes"])


def test_feedback_producer_and_worker_share_persistent_occurrence_path() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    worker = compose["services"]["portfolio-worker"]

    assert worker["environment"]["EVOLUTION_SKILLS_HOME"] == "/var/lib/evolution-skills"
    assert any(
        volume.endswith(":/var/lib/evolution-skills")
        for volume in worker["volumes"]
    )


def test_factory_image_contains_generator_entrypoint_and_model_gateway() -> None:
    dockerfile = (ROOT / "Dockerfile.factory").read_text(encoding="utf-8")
    assert "COPY scripts/evolution_skills.py" in dockerfile
    assert "COPY departments/worker_model_gateway.py" in dockerfile
    assert "PyYAML==6.0.2" in dockerfile
