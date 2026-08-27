from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_strategy_hermes_service_is_opt_in_and_has_no_department_or_order_surface() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["strategy-hermes"]

    assert service["profiles"] == ["strategy-hermes"]
    assert service["image"] == "hedgefund-strategy-hermes:latest"
    assert service["build"]["dockerfile"] == "Dockerfile.agent-runtime"
    assert service["entrypoint"] == ["/usr/local/bin/strategy-hermes-entrypoint"]
    assert "--loop" in service["command"]
    assert "strategy_hermes_supervisor.py" in " ".join(service["command"])
    assert any("autonomous_research_lab" in volume for volume in service["volumes"])
    assert any("strategy_hermes_home" in volume for volume in service["volumes"])
    assert service["environment"]["HERMES_PROFILE"] == "strategy-hermes"
    assert service["environment"]["AUTONOMOUS_RESEARCH_HERMES_PROVIDER"] == "openai-codex"
    assert service["environment"]["AUTONOMOUS_RESEARCH_HERMES_MODEL"] == "gpt-5.6-luna"
    assert not any("docker.sock" in volume for volume in service["volumes"])
    assert "research-mcp" not in service.get("depends_on", {})
    assert not any("research-hermes" in str(value) or "quant-hermes" in str(value) for value in service.values())
    assert not any("factory" in str(value).lower() for value in service.values())
    assert not any("order" in str(value).lower() or "broker" in str(value).lower() for value in service.values())


def test_portfolio_bff_shares_only_the_autonomous_research_intake_volume() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    bff = compose["services"]["portfolio-bff"]
    assert "autonomous_research_lab:/var/lib/autonomous-research" in bff["volumes"]
    assert bff["environment"]["AUTONOMOUS_RESEARCH_LAB_ROOT"] == "/var/lib/autonomous-research"


def test_retained_operational_services_do_not_reference_factory_image() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    for name in ("card-watchdog", "strategy-runtime-control"):
        service = services[name]
        assert service["image"] == "hedgefund-operations-runtime:latest"
        assert service["build"]["dockerfile"] == "Dockerfile.operations-runtime"
        assert "factory-autopilot" not in service.get("depends_on", {})


def test_model_overlay_evolution_workers_use_non_factory_runtime() -> None:
    overlay = yaml.safe_load((ROOT / "docker-compose.model.yml").read_text(encoding="utf-8"))
    for name in ("skill-evolution-worker", "skill-evolution-control-worker"):
        service = overlay["services"][name]
        assert service["image"] == "hedgefund-operations-runtime:latest"
        assert service["build"]["dockerfile"] == "Dockerfile.operations-runtime"
        assert "Dockerfile.factory" not in str(service)


def test_research_images_do_not_ship_retired_factory_sources() -> None:
    for name in ("departments/01-research/Dockerfile", "departments/01-research/Dockerfile.mcp"):
        dockerfile = (ROOT / name).read_text(encoding="utf-8")
        assert "COPY departments/01-research/factory" not in dockerfile
        assert "COPY factory ./factory" not in dockerfile
