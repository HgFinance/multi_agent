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


def test_factory_init_remains_a_separate_one_shot_permission_boundary() -> None:
    factory_init = _services(ROOT / "docker-compose.yml")["factory-kanban-init"]

    assert factory_init["network_mode"] == "none"
    assert factory_init["user"] == "0:0"
    assert factory_init["restart"] == "no"
