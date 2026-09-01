"""Deployment contract for the governed Evolution Skills worker."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_model_overlay_runs_14b_proposal_worker_with_read_only_canonical_skills() -> (
    None
):
    overlay = yaml.safe_load(
        (ROOT / "docker-compose.model.yml").read_text(encoding="utf-8")
    )
    service = overlay["services"]["skill-evolution-worker"]
    command = [str(value) for value in service["command"]]

    assert service["depends_on"]["vllm"]["condition"] == "service_healthy"
    assert (
        service["environment"]["WORKER_MODEL_NAME"]
        == "${WORKER_MODEL_NAME:-qwen2.5-14b-instruct-awq}"
    )
    assert command.count("--department") == 8
    for department in (
        "00-ceo-office",
        "01-research",
        "02-trading",
        "03-risk",
        "04-quant-backtest",
        "05-accounting-portfolio",
        "06-ai-qa-audit",
        "07-agent-workforce",
    ):
        assert department in command
    assert "http://vllm:8000/v1" in command
    assert "/var/lib/portfolio/langsmith-feedback.sqlite3" in command
    assert "./skills:/opt/shared-skills:ro" in service["volumes"]
    assert any(
        "evolution-skills" in volume and not volume.endswith(":ro")
        for volume in service["volumes"]
    )

    control = overlay["services"]["skill-evolution-control-worker"]
    control_command = [str(value) for value in control["command"]]
    assert "control-daemon" in control_command
    assert "--repository-root" in control_command
    assert "./skills:/workspace/skills:rw" in control["volumes"]
    assert "./skills:/opt/shared-skills:ro" not in control["volumes"]


def test_feedback_producer_and_worker_share_persistent_occurrence_path() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    worker = compose["services"]["portfolio-worker"]

    assert worker["environment"]["EVOLUTION_SKILLS_HOME"] == "/var/lib/evolution-skills"
    assert any(
        volume.endswith(":/var/lib/evolution-skills") for volume in worker["volumes"]
    )
    audit = compose["services"]["audit-api"]
    qa_hermes = compose["services"]["qa-hermes"]
    assert audit["environment"]["EVOLUTION_SKILLS_HOME"] == "/var/lib/evolution-skills"
    assert any(
        volume.endswith(":/var/lib/evolution-skills") for volume in audit["volumes"]
    )
    assert any(
        volume.endswith(":/var/lib/evolution-skills:ro")
        for volume in qa_hermes["volumes"]
    )


def test_ceo_active_evolution_skill_has_one_canonical_runtime_delivery_path() -> None:
    compose = yaml.safe_load(
        (ROOT / "departments/00-ceo-office/compose.yaml").read_text(encoding="utf-8")
    )
    volumes = compose["services"]["ceo-hermes"]["volumes"]
    target = (
        "../../skills/evolved/ceo-canonical-evidence-react-enforced:"
        "/opt/shared-skills/evolved/ceo-canonical-evidence-react-enforced:ro"
    )

    assert volumes.count(target) == 1


def test_operations_image_contains_generator_entrypoint_and_model_gateway() -> None:
    dockerfile = (ROOT / "Dockerfile.operations-runtime").read_text(encoding="utf-8")
    assert "COPY scripts/evolution_skills.py" in dockerfile
    assert "COPY departments/worker_model_gateway.py" in dockerfile
    for source in (
        "orchestration/qa_feedback_benchmarks.py",
        "orchestration/qa_skill_evolution_bridge.py",
        "orchestration/llm_observability.py",
        "orchestration/semantic_qa.py",
        "orchestration/answer_contract.py",
        "departments/qwen_hybrid_runtime.py",
        "benchmarks/quantization/knowledge/bok800_2026/glossary_rag_v1.json",
    ):
        assert f"COPY {source}" in dockerfile
    assert "COPY departments/01-research/config/worker_model_registry.json" in dockerfile
    assert "PyYAML==6.0.2" in dockerfile
    assert '"jsonschema>=4.10,<5"' in dockerfile
    assert "COPY departments/01-research/factory" not in dockerfile
    entrypoint = (ROOT / "scripts/evolution_skills.py").read_text(encoding="utf-8")
    assert '"skill-evolution-proposal-worker", env=env' in entrypoint
    registry = yaml.safe_load(
        (ROOT / "departments/01-research/config/worker_model_registry.json").read_text(
            encoding="utf-8"
        )
    )
    assert registry["hybrid_runtime"]["version"] == "awq-hybrid-upgrade-v1"
    assert registry["hybrid_runtime"]["status"] == "enabled"


def test_legacy_skill_forge_entrypoint_is_not_reintroduced() -> None:
    assert not (ROOT / "departments/01-research/agents/skill_forge.py").exists()
    research_pipeline = (ROOT / "departments/01-research/scripts.py").read_text(
        encoding="utf-8"
    )
    assert "skill_forge" not in research_pipeline
