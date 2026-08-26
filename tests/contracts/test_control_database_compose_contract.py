"""Deployment contract for the canonical operational/control PostgreSQL."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

ROOT_COMPOSE_FILES = (
    ROOT / "docker-compose.yml",
    ROOT / "departments/00-ceo-office/compose.yaml",
    ROOT / "departments/02-trading/compose.yaml",
    ROOT / "departments/05-accounting-portfolio/compose.yaml",
    ROOT / "departments/07-agent-workforce/compose.yaml",
)

EXPECTED_DEFAULT_CONTROL_DB_CONSUMERS = {
    "accounting-api",
    "accounting-close-scheduler",
    "accounting-ledger-consumer",
    "accounting-ls-paper-reconciler",
    "audit-api",
    "batch-collectors",
    "ceo-kanban-supervisor",
    "control-event-worker",
    "factory-autopilot",
    "factory-experiment-worker",
    "governance-api",
    "ls-realtime",
    "market-api",
    "paper-order-orchestrator-mcp",
    "portfolio-bff",
    "portfolio-worker",
    "qa-reproduction-worker",
    "qa-worker",
    "quant-api",
    "research-api",
    "research-liaison-mcp",
    "research-mcp",
    "risk-api",
    "trading-api",
    "trading-directive-worker",
    "trading-outbox-relay",
    "workforce-api",
}

EXPECTED_EB_CONTROL_DB_CONSUMERS = {
    "accounting-api",
    "accounting-ledger-consumer",
    "portfolio-bff",
    "portfolio-worker",
    "trading-api",
    "trading-directive-worker",
    "trading-outbox-relay",
}


def _services(*paths: Path) -> dict[str, dict]:
    services: dict[str, dict] = {}
    for path in paths:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        additions = document.get("services") or {}
        overlap = services.keys() & additions.keys()
        assert not overlap, f"duplicate service definitions: {sorted(overlap)}"
        services.update(additions)
    return services


def _default_database_consumers(services: dict[str, dict]) -> set[str]:
    return {
        name
        for name, service in services.items()
        if not service.get("profiles")
        and "DATABASE_URL" in (service.get("environment") or {})
    }


def test_default_stack_has_the_exact_control_database_consumers() -> None:
    services = _services(*ROOT_COMPOSE_FILES)

    assert _default_database_consumers(services) == EXPECTED_DEFAULT_CONTROL_DB_CONSUMERS
    for name in ("portfolio-bff", "portfolio-worker"):
        assert "DATABASE_URL" in services[name]["environment"]
    # The root stack serves the closed-network UI fixture, while the worker
    # must always read the governed production catalog. AWS overrides the BFF
    # to production explicitly after its private control DB is wired.
    assert services["portfolio-bff"]["environment"]["PORTFOLIO_DATA_MODE"] == "test"
    assert services["portfolio-worker"]["environment"]["PORTFOLIO_DATA_MODE"] == (
        "production"
    )


def test_eb_stack_has_the_exact_private_control_database_consumers() -> None:
    template = (ROOT / "deploy/eb/docker-compose.yml").read_text(encoding="utf-8")
    services = _services(ROOT / "deploy/eb/docker-compose.yml")

    assert "${CONTROL_DATABASE_URL:?" in template
    assert "${DATABASE_URL:?AWS private" not in template
    assert _default_database_consumers(services) == EXPECTED_EB_CONTROL_DB_CONSUMERS
    for name in EXPECTED_EB_CONTROL_DB_CONSUMERS:
        assert "private operational PostgreSQL" in services[name]["environment"][
            "DATABASE_URL"
        ]
    for name in ("portfolio-bff", "portfolio-worker"):
        assert services[name]["environment"]["PORTFOLIO_DATA_MODE"] == "production"
    assert services["accounting-ledger-consumer"]["environment"][
        "ACCOUNTING_DATABASE_ROLE"
    ].endswith("svc_accounting_ledger}")


def test_local_override_keeps_the_user_order_pipeline_on_one_control_database() -> None:
    # The Windows override is intentionally gitignored and therefore absent in
    # CI clones. When present, its local cutover must cover the canonical pair
    # and must not reintroduce the removed legacy BFF.
    path = ROOT / "docker-compose.override.yml"
    if not path.exists():
        return

    services = _services(path)
    for name in (
        "portfolio-bff",
        "portfolio-worker",
        "paper-order-orchestrator-mcp",
        "trading-api",
        "trading-directive-worker",
    ):
        environment = services[name]["environment"]
        assert "LOCAL_CONTROL_DATABASE_URL" in environment["DATABASE_URL"]
    for name in ("portfolio-bff", "portfolio-worker"):
        environment = services[name]["environment"]
        assert environment["PORTFOLIO_DATA_MODE"] == "production"
    assert "USERPROFILE" in services["paper-order-orchestrator-mcp"]["volumes"][0]
    assert services["paper-order-orchestrator-mcp"]["volumes"][0].endswith(
        ":/opt/kanban"
    )
    assert "ui-bff" not in services
