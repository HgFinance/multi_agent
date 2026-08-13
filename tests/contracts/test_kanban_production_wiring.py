"""Production shared-Kanban environment and worker access contracts."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _service_block(source: str, service_name: str) -> str:
    lines = source.splitlines()
    start = lines.index(f"  {service_name}:")
    block: list[str] = []
    for line in lines[start:]:
        if line != lines[start] and line.startswith("  ") and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


def test_production_services_pin_the_shared_kanban_database() -> None:
    root_compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for service in ("portfolio-bff", "research-hermes", "quant-hermes", "risk-hermes", "qa-hermes"):
        assert "HERMES_KANBAN_DB: /opt/kanban/kanban.db" in _service_block(
            root_compose, service
        )
    for service in ("kanban-dispatcher", "ceo-kanban-supervisor"):
        assert "HERMES_KANBAN_DB: /opt/data/shared-kanban/kanban.db" in _service_block(
            root_compose, service
        )

    for path, service in (
        (ROOT / "departments/00-ceo-office/compose.yaml", "ceo-hermes"),
        (ROOT / "departments/02-trading/compose.yaml", "trading-hermes"),
        (ROOT / "departments/05-accounting-portfolio/compose.yaml", "accounting-hermes"),
        (ROOT / "departments/07-agent-workforce/compose.yaml", "workforce-hermes"),
    ):
        assert "HERMES_KANBAN_DB: /opt/kanban/kanban.db" in _service_block(
            path.read_text(encoding="utf-8"), service
        )


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
