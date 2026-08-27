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
    assert service["environment"]["LS_DATA_ACCESS_MODE"] == "readonly"
    assert service["environment"]["LS_ALLOWED_TR_CODES"].split(",") == [
        "t1665", "t8410", "t8411", "t8412", "t8451", "t8452", "t8453",
        "t1441", "t1444", "t1452", "t1463", "t1466", "t1481", "t1482",
        "t1489", "t1492",
    ]
    assert service["environment"]["STRATEGY_MARKET_DATA_PARENT"] == "/tmp/strategy-market-data"
    assert service["environment"]["AUTONOMOUS_RESEARCH_MAX_CONCURRENCY"] == "${AUTONOMOUS_RESEARCH_MAX_CONCURRENCY:-2}"
    assert "/app/repo/quant-data" in service["tmpfs"]
    assert "/tmp/strategy-market-data" in service["tmpfs"]
    assert not any("quant-data" in volume for volume in service["volumes"])
    assert not any("docker.sock" in volume for volume in service["volumes"])
    assert "research-mcp" not in service.get("depends_on", {})
    assert not any("research-hermes" in str(value) or "quant-hermes" in str(value) for value in service.values())
    assert not any("factory" in str(value).lower() for value in service.values())
    assert not any("order" in str(value).lower() or "broker" in str(value).lower() for value in service.values())
    entrypoint = (ROOT / "deploy/strategy-hermes/entrypoint.sh").read_text(encoding="utf-8")
    assert 'chmod 0777 "$lab_root/intake"' in entrypoint
    supervisor = (ROOT / "departments/01-research/autonomous/strategy_hermes_supervisor.py").read_text(encoding="utf-8")
    assert '"--retry-blocked"' in supervisor
    assert "ThreadPoolExecutor" in supervisor
    assert "_configured_max_concurrency" in supervisor


def test_strategy_hermes_owns_the_autonomous_namespace_not_research_hq() -> None:
    ownership = (ROOT / "departments/01-research/autonomous/OWNERSHIP.md").read_text(encoding="utf-8")
    package = (ROOT / "departments/01-research/autonomous/__init__.py").read_text(encoding="utf-8")
    research_soul = (ROOT / "departments/01-research/hermes/SOUL.md").read_text(encoding="utf-8")
    research_config = (ROOT / "departments/01-research/hermes/config.yaml").read_text(encoding="utf-8")
    strategy_config = (ROOT / "departments/01-research/strategy-hermes/config.yaml").read_text(encoding="utf-8")

    assert "논리적 소유자와 실제 연구 실행자는 `Strategy Hermes`" in ownership
    assert "RUNTIME_OWNER = \"strategy-hermes\"" in package
    assert "Strategy generation, strategy-code authoring, backtesting" in research_soul
    assert "head_persona: research-methodology-head" in research_config
    assert "run: python3 departments/01-research/autonomous/runner.py" not in research_config
    assert "collaborate: python3 departments/01-research/autonomous/runner.py" not in research_config
    assert "head_persona: strategy-hermes" in strategy_config


def test_research_hq_profile_does_not_expose_strategy_runner_usage() -> None:
    soul = (ROOT / "departments/01-research/hermes/SOUL.md").read_text(encoding="utf-8")
    assert "Do not invoke `autonomous/runner.py` from this profile." in soul
    assert "independent Strategy Hermes intake" in soul


def test_strategy_skill_owner_is_not_research_hq() -> None:
    from orchestration.skill_contract import skill_owners, validate_skill_for_profile

    assert skill_owners("autonomous-quant-research") == frozenset({"strategy-hermes"})
    assert validate_skill_for_profile(
        "autonomous-quant-research", "strategy-hermes", root=ROOT / "skills"
    ) == "autonomous-quant-research"


def test_portfolio_bff_shares_only_the_autonomous_research_intake_volume() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    bff = compose["services"]["portfolio-bff"]
    assert "autonomous_research_lab:/var/lib/autonomous-research" in bff["volumes"]
    assert bff["environment"]["AUTONOMOUS_RESEARCH_LAB_ROOT"] == "/var/lib/autonomous-research"


def test_strategy_discord_notifier_is_read_only_and_opt_in() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["strategy-research-discord-notifier"]

    assert service["profiles"] == ["strategy-hermes"]
    assert service["image"] == "hedgefund-strategy-discord-notifier:latest"
    assert "strategy_research_discord_notifier" in " ".join(service["command"])
    assert "autonomous_research_lab:/var/lib/autonomous-research:ro" in service["volumes"]
    assert "strategy_discord_notifier_state:/var/lib/strategy-discord-notifier" in service["volumes"]
    assert service["environment"]["STRATEGY_DISCORD_REPORT_ENABLED"].startswith("${STRATEGY_DISCORD_REPORT_ENABLED:-")
    assert "--healthcheck" in service["healthcheck"]["test"]
    assert not any("docker.sock" in volume for volume in service["volumes"])


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


def test_retired_experiment_factory_surface_is_not_reintroduced() -> None:
    """The old skill and runner stay absent from live registration surfaces."""

    assert not (ROOT / "skills/experiment-factory").exists()
    assert not (ROOT / "Dockerfile.factory").exists()
    assert not (ROOT / "deploy/local/docker-compose.factory.yml").exists()
    assert not (ROOT / "departments/01-research/factory/factory_shepherd.py").exists()

    contract = (ROOT / "orchestration/skill_contract.py").read_text(encoding="utf-8")
    sync_script = (ROOT / "scripts/sync_hermes_profiles.sh").read_text(encoding="utf-8")
    registry = (ROOT / "skills/evolution-registry.json").read_text(encoding="utf-8")
    assert "experiment-factory" not in contract
    assert "experiment-factory" not in sync_script
    assert "experiment-factory" not in registry
