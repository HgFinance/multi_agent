"""Contracts for the low-risk container consolidation and readiness edges."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _services(path: Path) -> dict[str, dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("services", {})


def test_control_event_worker_reuses_both_existing_consumers() -> None:
    root = _services(ROOT / "docker-compose.yml")
    ceo = _services(ROOT / "departments/00-ceo-office/compose.yaml")
    workforce = _services(ROOT / "departments/07-agent-workforce/compose.yaml")
    worker = root["control-event-worker"]

    assert "notification-worker" not in ceo
    assert "improvement-worker" not in workforce
    assert worker["depends_on"]["redis"]["condition"] == "service_healthy"
    assert "orchestration.control_event_worker" in " ".join(
        worker["healthcheck"]["test"]
    )
    dockerfile = (ROOT / "Dockerfile.control-event-worker").read_text(encoding="utf-8")
    assert "governance_events" in dockerfile
    assert "workforce_events" in dockerfile


def test_discord_retention_is_owned_by_maintenance_scheduler() -> None:
    services = _services(ROOT / "docker-compose.yml")
    scheduler = services["maintenance-retention-scheduler"]

    assert "discord-retention-worker" not in services
    assert scheduler["environment"]["DISCORD_RETENTION_ENABLED"].endswith("true}")
    assert scheduler["environment"]["HERMES_PROFILE"] == "ceo-agent"


def test_market_and_accounting_startup_dependencies_are_deep_but_bounded() -> None:
    root = _services(ROOT / "docker-compose.yml")
    accounting = _services(ROOT / "departments/05-accounting-portfolio/compose.yaml")

    assert "/ready" in " ".join(root["market-api"]["healthcheck"]["test"])
    assert (
        root["conditional-rule-worker"]["depends_on"]["market-api"]["condition"]
        == "service_healthy"
    )
    assert (
        accounting["accounting-ls-paper-reconciler"]["depends_on"]["portfolio-bff"][
            "condition"
        ]
        == "service_healthy"
    )
    # Ledger posting remains independent. Market unavailability may defer NAV
    # valuation, but must never block immutable fill journals at startup.
    ledger_dependencies = accounting["accounting-ledger-consumer"].get("depends_on", {})
    assert "market-api" not in ledger_dependencies


def test_strategy_hermes_is_an_explicit_opt_in_boundary() -> None:
    autonomous = _services(ROOT / "docker-compose.yml")["strategy-hermes"]

    assert autonomous["profiles"] == ["strategy-hermes"]
    assert "autonomous_research_lab" in " ".join(autonomous["volumes"])
    assert not any("docker.sock" in volume for volume in autonomous["volumes"])


def test_priority_runtime_services_have_explicit_healthchecks() -> None:
    root = _services(ROOT / "docker-compose.yml")
    ceo = _services(ROOT / "departments/00-ceo-office/compose.yaml")
    trading = _services(ROOT / "departments/02-trading/compose.yaml")
    accounting = _services(ROOT / "departments/05-accounting-portfolio/compose.yaml")
    workforce = _services(ROOT / "departments/07-agent-workforce/compose.yaml")

    services = {
        **root,
        **ceo,
        **trading,
        **accounting,
        **workforce,
    }
    expected = {
        "strategy-hermes",
        "portfolio-worker",
        "research-api",
        "trading-directive-worker",
        "accounting-ledger-consumer",
        "accounting-ls-paper-reconciler",
        "governance-api",
        "workforce-api",
    }
    assert expected <= services.keys()
    assert all(services[name].get("healthcheck") for name in expected)
    assert services["strategy-hermes"]["healthcheck"]["test"] == [
        "CMD-SHELL",
        "test -d /var/lib/autonomous-research/intake && test -d /var/lib/autonomous-research/labs",
    ]
    assert "--healthcheck" in services["accounting-ledger-consumer"]["healthcheck"]["test"]
    assert "--healthcheck" in services["accounting-ls-paper-reconciler"]["healthcheck"]["test"]
    assert "--healthcheck" in services["trading-directive-worker"]["healthcheck"]["test"]
    assert "--healthcheck" in services["portfolio-worker"]["healthcheck"]["test"]


def test_research_mcp_has_loop_stall_restart_contract() -> None:
    services = _services(ROOT / "docker-compose.yml")
    for name in ("research-mcp", "research-liaison-mcp"):
        environment = services[name]["environment"]
        assert environment["RESEARCH_MCP_LOOP_STALL_SECONDS"] == (
            "${RESEARCH_MCP_LOOP_STALL_SECONDS:-90}"
        )
        assert services[name]["restart"] == "unless-stopped"

    source = (ROOT / "departments/01-research/api/mcp_server.py").read_text(
        encoding="utf-8"
    )
    assert "class _LoopStallWatchdog" in source
    assert "_with_loop_watchdog(" in source
