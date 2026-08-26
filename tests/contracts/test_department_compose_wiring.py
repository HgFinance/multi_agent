"""Department Compose가 durable 저장소 모드를 실제 컨테이너에 주입하는지 검증한다."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _service_block(compose_text: str, service_name: str) -> str:
    """Return one service block from the small department Compose fragments."""
    lines = compose_text.splitlines()
    start = lines.index(f"  {service_name}:") + 1
    block = []
    for line in lines[start:]:
        if line.startswith("  ") and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


class DepartmentComposeWiringTests(unittest.TestCase):
    def test_redis_stream_transport_is_aof_persistent(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        service = _service_block(compose, "redis")

        self.assertIn('"--appendonly", "yes"', service)
        self.assertIn('"--appendfsync", "everysec"', service)
        self.assertIn("- redis_data:/data", service)
        self.assertIn("\n  redis_data:\n", compose)

    def test_research_liaison_packages_stock_evidence_contract(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        services = (
            _service_block(compose, "research-mcp"),
            _service_block(compose, "research-liaison-mcp"),
        )
        dockerfile = (
            ROOT / "departments/01-research/Dockerfile.mcp"
        ).read_text(encoding="utf-8")

        for service in services:
            self.assertIn("context: .", service)
            self.assertIn(
                "dockerfile: departments/01-research/Dockerfile.mcp", service
            )
        self.assertIn(
            "departments/04-quant-backtest/pipeline/stock_universe.py",
            dockerfile,
        )
        self.assertIn(
            "/app/departments/04-quant-backtest/pipeline/stock_universe.py",
            dockerfile,
        )
        self.assertIn("mcp==1.26.0", dockerfile)
        self.assertNotRegex(dockerfile, r"(?m)^\s+mcp\s*\\$")

    def test_quant_api_owns_database_access_and_supports_scoped_role(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        service = _service_block(compose, "quant-api")

        self.assertIn(
            "dockerfile: departments/04-quant-backtest/Dockerfile", service
        )
        self.assertIn(
            "DATABASE_URL: ${QUANT_DATABASE_URL:-${DATABASE_URL:", service
        )
        self.assertNotIn("\n      QUANT_DATABASE_URL:", service)
        self.assertIn(
            "MCP_RESEARCH_API_KEY: ${MCP_RESEARCH_API_KEY:-}", service
        )
        self.assertIn('"127.0.0.1:8037:8037"', service)
        self.assertIn(
            "QUANT_DATABASE_URL=",
            (ROOT / ".env.example").read_text(encoding="utf-8"),
        )

        migration = (
            ROOT
            / "supabase/migrations/20260817000100_quant_api_seed_read.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("grant usage on schema research to svc_quant", migration)
        self.assertIn(
            "grant select on table research.packet_claims to svc_quant", migration
        )
        self.assertNotIn("insert", migration.lower())
        self.assertNotIn("update", migration.lower())

    def test_quant_hermes_gets_mcp_auth_but_no_database_secret(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        service = _service_block(compose, "quant-hermes")

        self.assertIn(
            "MCP_RESEARCH_API_KEY: ${MCP_RESEARCH_API_KEY:-}", service
        )
        self.assertNotIn("DATABASE_URL:", service)
        self.assertNotIn("QUANT_DATABASE_URL:", service)

    def test_trading_api_receives_paper_db_and_shared_database_url(self) -> None:
        compose = (ROOT / "departments/02-trading/compose.yaml").read_text(
            encoding="utf-8"
        )
        service = _service_block(compose, "trading-api")

        self.assertIn("PAPER_DB: ${PAPER_DB:-true}", service)
        self.assertIn("DATABASE_URL: *trading-database-url", service)
        self.assertIn("x-trading-database-url: &trading-database-url", compose)
        self.assertIn("role%3Dsvc_trading_api", compose)

    def test_trading_hermes_uses_standard_profile_path(self) -> None:
        compose = (ROOT / "departments/02-trading/compose.yaml").read_text(
            encoding="utf-8"
        )
        # 호스트 접두사(${USERPROFILE} / /home/ubuntu)는 배포 환경마다 다르므로
        # 고정하지 않는다. 고정하는 것은 **레이아웃**이다 - profiles/<부서> 아래에
        # 있고, 구형 ~/.hermes-<부서> 로 돌아가지 않는다.
        self.assertIn("/.hermes/profiles/trading-department:/opt/data", compose)
        self.assertNotIn(".hermes-trading-department", compose)

        sync_script = (ROOT / "scripts/sync_hermes_profiles.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('DEST_ROOT="${HERMES_HOME:-$HOME/.hermes}/profiles"', sync_script)
        self.assertNotIn("HERMES_HOME_PREFIX", sync_script)

    def test_accounting_api_defaults_to_durable_paper_mode(self) -> None:
        compose = (ROOT / "departments/05-accounting-portfolio/compose.yaml").read_text(
            encoding="utf-8"
        )
        service = _service_block(compose, "accounting-api")

        self.assertIn("ACCOUNTING_MODE: ${ACCOUNTING_MODE:-PAPER_DB}", service)
        self.assertIn("DATABASE_URL: ${DATABASE_URL:-}", service)

    def test_accounting_hermes_uses_standard_profile_path(self) -> None:
        compose = (ROOT / "departments/05-accounting-portfolio/compose.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "/.hermes/profiles/accounting-portfolio-department:/opt/data", compose
        )
        self.assertNotIn(".hermes-accounting-portfolio-department", compose)

    def test_kanban_dispatcher_uses_standalone_escape_hatch_without_s6_reconcile(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        service = _service_block(compose, "kanban-dispatcher")

        self.assertIn("init: true", service)
        self.assertIn('command:\n      ["/bin/sh", "-c",', service)
        self.assertIn(
            "deploy/hermes-dispatch-guard/check_guard.py && exec hermes "
            "kanban daemon --force",
            service,
        )
        self.assertIn(
            '--interval \\"$${KANBAN_DISPATCH_INTERVAL:-1}\\"', service
        )
        self.assertNotIn('command: ["gateway", "run"]', service)
        self.assertIn(
            "PYTHONPATH: /app/repo/deploy/hermes-dispatch-guard", service
        )
        self.assertIn('HGFINANCE_DISPATCH_GUARD: "1"', service)
        self.assertIn("HERMES_HOME: /opt/data", service)
        self.assertIn("HERMES_KANBAN_HOME: /opt/data/shared-kanban", service)
        self.assertIn('HERMES_KANBAN_DISPATCH_IN_GATEWAY: "false"', service)
        self.assertIn(
            "KANBAN_DISPATCH_INTERVAL: ${KANBAN_DISPATCH_INTERVAL:-1}",
            service,
        )
        self.assertIn(
            "HGFINANCE_FAST_ADVISORY_MAX_TURNS: "
            "${HGFINANCE_FAST_ADVISORY_MAX_TURNS:-12}",
            service,
        )
        self.assertIn(
            "HGFINANCE_USER_RESPONSE_MAX_TURNS: "
            "${HGFINANCE_USER_RESPONSE_MAX_TURNS:-12}",
            service,
        )
        self.assertIn(
            "HGFINANCE_USER_RESPONSE_REASONING: "
            "${HGFINANCE_USER_RESPONSE_REASONING:-medium}",
            service,
        )
        self.assertIn(
            "MCP_RESEARCH_API_KEY: ${MCP_RESEARCH_API_KEY:-}", service
        )
        self.assertIn("/home/ubuntu/.hermes:/opt/data", service)

        for compose_path in (
            ROOT / "docker-compose.yml",
            ROOT / "departments/00-ceo-office/compose.yaml",
            ROOT / "departments/02-trading/compose.yaml",
            ROOT / "departments/05-accounting-portfolio/compose.yaml",
            ROOT / "departments/07-agent-workforce/compose.yaml",
        ):
            self.assertIn(
                'HERMES_KANBAN_DISPATCH_IN_GATEWAY: "false"',
                compose_path.read_text(encoding="utf-8"),
                msg=f"embedded dispatcher must remain disabled in {compose_path}",
            )
