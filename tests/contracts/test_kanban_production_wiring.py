"""Production shared-Kanban environment and worker access contracts."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _service_block(source: str, service_name: str) -> str:
    lines = source.splitlines()
    start = lines.index(f"  {service_name}:")
    block: list[str] = []
    for line in lines[start:]:
        if (
            line != lines[start]
            and line.startswith("  ")
            and not line.startswith("    ")
        ):
            break
        block.append(line)
    return "\n".join(block)


def test_production_services_pin_the_shared_kanban_database() -> None:
    root_compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for service in (
        "portfolio-bff",
        "research-hermes",
        "quant-hermes",
        "risk-hermes",
        "qa-hermes",
    ):
        assert "HERMES_KANBAN_DB: /opt/kanban/kanban.db" in _service_block(
            root_compose, service
        )
    for service in (
        "kanban-dispatcher",
        "ceo-kanban-supervisor",
        "maintenance-retention-scheduler",
    ):
        assert "HERMES_KANBAN_DB: /opt/data/shared-kanban/kanban.db" in _service_block(
            root_compose, service
        )


def test_dispatcher_routes_workers_through_qa_terminal_boundary() -> None:
    source = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dispatcher = _service_block(source, "kanban-dispatcher")

    assert "HERMES_BIN: /app/repo/scripts/qa_hermes_worker.py" in dispatcher
    assert "- .:/app/repo:ro" in dispatcher
    assert "mem_limit: 3g" in dispatcher
    assert "mem_reservation: 512m" in dispatcher
    assert "hostname: hedgefund-kanban-dispatcher" in dispatcher
    assert (
        "HERMES_KANBAN_CLAIM_TTL_SECONDS: "
        "${HERMES_KANBAN_CLAIM_TTL_SECONDS:-180}" in dispatcher
    )


def test_supervisor_qa_projection_uses_audit_runtime_role() -> None:
    root = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    supervisor = _service_block(root, "ceo-kanban-supervisor")
    assert "role%3Dsvc_audit_api" in supervisor

    overlay = (ROOT / "deploy/aws/docker-compose.paper-order.yml").read_text(
        encoding="utf-8"
    )
    overlay_supervisor = _service_block(overlay, "ceo-kanban-supervisor")
    assert "RISK_QA_DATABASE_URL: *audit-api-database-url" in overlay_supervisor


def test_retention_scheduler_uses_existing_workers_shared_lock_and_audit_lane() -> None:
    source = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    worker = _service_block(source, "maintenance-retention-scheduler")
    assert "orchestration.maintenance_retention" in worker
    assert (
        "HERMES_KANBAN_RETENTION_LOCK: /opt/data/shared-kanban/retention.lock" in worker
    )
    assert (
        "HERMES_KANBAN_RETENTION_AUDIT_DB: /opt/data/shared-kanban/retention-audit.db"
        in worker
    )
    assert "MEMOHARNESS_D5_RETENTION_INTERVAL_SECONDS" in worker
    assert "NOTION_RETENTION_INTERVAL_SECONDS" in worker
    assert "DISCORD_RETENTION_INTERVAL_SECONDS" in worker
    assert "orchestration.discord_retention" not in worker
    assert "ceo-kanban-supervisor" not in worker

    for path, service in (
        (ROOT / "departments/00-ceo-office/compose.yaml", "ceo-hermes"),
        (ROOT / "departments/02-trading/compose.yaml", "trading-hermes"),
        (
            ROOT / "departments/05-accounting-portfolio/compose.yaml",
            "accounting-hermes",
        ),
        (ROOT / "departments/07-agent-workforce/compose.yaml", "workforce-hermes"),
    ):
        assert "HERMES_KANBAN_DB: /opt/kanban/kanban.db" in _service_block(
            path.read_text(encoding="utf-8"), service
        )


def test_legacy_factory_compose_surface_is_removed() -> None:
    root = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    aws = (ROOT / "deploy/aws/docker-compose.paper-order.yml").read_text(encoding="utf-8")
    assert not any(name in root for name in (
        "factory-kanban-init", "factory-kanban-dispatcher",
        "factory-autopilot", "factory-experiment-worker",
    ))
    assert not any(name in aws for name in (
        "factory-kanban-dispatcher", "factory-autopilot", "factory-experiment-worker",
    ))
    assert not (ROOT / "deploy/local/docker-compose.factory.yml").exists()


def test_supervisor_inherits_pinned_environment_for_hermes_client() -> None:
    runner = (ROOT / "scripts/run_ceo_supervisor.py").read_text(encoding="utf-8")
    assert "environment = dict(os.environ)" in runner
    assert "HermesKanbanClient(environment=environment)" in runner


def test_ceo_worker_uses_supported_kanban_tools_only() -> None:
    soul = (ROOT / "departments/00-ceo-office/hermes/SOUL.md").read_text(
        encoding="utf-8"
    )
    assert "kanban_show" in soul
    assert "kanban_list" in soul
    assert "Do not inspect the Kanban SQLite database" in soul
    assert "python3 -c" not in soul
    assert "cp /opt/data/shared-kanban/kanban.db" not in soul
