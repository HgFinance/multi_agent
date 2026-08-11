"""Deployment contract for the portfolio BFF and CEO Hermes boundary."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _service_block(text: str, service: str) -> str:
    lines = text.splitlines()
    start = lines.index(f"  {service}:")
    block: list[str] = []
    for line in lines[start:]:
        if block and line.startswith("  ") and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


def test_portfolio_bff_uses_cli_for_kanban_and_remote_ceo_api() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    bff = _service_block(compose, "portfolio-bff")

    assert 'ENABLE_AGENT_ASK: "true"' in bff
    assert 'ENABLE_KANBAN_TASK_TRACKING: "1"' in bff
    assert "HERMES_BIN: /usr/local/bin/hermes" in bff
    assert "HERMES_HOME: /opt/hermes-cli" in bff
    assert "HERMES_KANBAN_HOME: /opt/kanban" in bff
    assert "HERMES_CEO_API_URL: http://ceo-hermes:8642/v1" in bff
    assert "/home/ubuntu/.hermes/shared-kanban:/opt/kanban" in bff
    assert "/home/ubuntu/.hermes:/opt/data" not in bff
    assert '"${PORTFOLIO_BFF_PORT:-8001}:8000"' in bff


def test_ceo_gateway_exposes_only_authenticated_internal_api() -> None:
    compose = (ROOT / "departments/00-ceo-office/compose.yaml").read_text(
        encoding="utf-8"
    )
    ceo = _service_block(compose, "ceo-hermes")

    assert 'API_SERVER_ENABLED: "true"' in ceo
    assert "API_SERVER_HOST: 0.0.0.0" in ceo
    assert 'API_SERVER_PORT: "8642"' in ceo
    assert "API_SERVER_KEY: ${CEO_HERMES_API_KEY:-}" in ceo
    assert "ports:" not in ceo


def test_bff_dockerfile_pins_official_cli_without_gateway_command() -> None:
    dockerfile = (ROOT / "apps/api/Dockerfile").read_text(encoding="utf-8")

    assert "github.com/NousResearch/hermes-agent.git" in dockerfile
    assert "ARG HERMES_AGENT_REF=" in dockerfile
    assert "pip install --no-cache-dir /tmp/hermes-agent" in dockerfile
    assert 'CMD ["uvicorn"' in dockerfile
    assert 'command: ["gateway", "run"]' not in dockerfile
