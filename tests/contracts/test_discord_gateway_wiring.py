from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).parents[2]
GATEWAY_SERVICES = {
    "ceo-hermes": ROOT / "departments/00-ceo-office/compose.yaml",
    "research-hermes": ROOT / "docker-compose.yml",
    "quant-hermes": ROOT / "docker-compose.yml",
    "trading-hermes": ROOT / "departments/02-trading/compose.yaml",
    "accounting-hermes": ROOT / "departments/05-accounting-portfolio/compose.yaml",
    "risk-hermes": ROOT / "docker-compose.yml",
    "qa-hermes": ROOT / "docker-compose.yml",
    "workforce-hermes": ROOT / "departments/07-agent-workforce/compose.yaml",
}

EXPECTED_GATEWAY_PROFILES = {
    "ceo-hermes": "ceo-agent",
    "workforce-hermes": "hr-department",
    "research-hermes": "research-department",
    "quant-hermes": "quant-backtest-department",
    "accounting-hermes": "accounting-portfolio-department",
    "trading-hermes": "trading-department",
    "risk-hermes": "risk-management",
    "qa-hermes": "qa-department",
}


def _service_block(source: str, service_name: str) -> str:
    """Extract one top-level Compose service without requiring a YAML merge."""

    lines = source.splitlines()
    start = lines.index(f"  {service_name}:")
    block: list[str] = []
    for line in lines[start:]:
        if block and line.startswith("  ") and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


class DiscordGatewayWiringTests(unittest.TestCase):
    def test_all_eight_gateway_definitions_use_upstream_image_by_default(self) -> None:
        profiles: set[str] = set()
        for service, path in GATEWAY_SERVICES.items():
            source = path.read_text(encoding="utf-8")
            profile = EXPECTED_GATEWAY_PROFILES[service]
            block = _service_block(source, service)
            self.assertIn("image: nousresearch/hermes-agent:latest", block, service)
            self.assertNotIn("HERMES_GATEWAY_IMAGE", block, service)
            self.assertNotIn("Dockerfile.hermes-discord", block, service)
            self.assertIn(f"HERMES_PROFILE: {profile}", block, service)
            profiles.add(profile)
        self.assertEqual(profiles, set(EXPECTED_GATEWAY_PROFILES.values()))

    def test_idempotency_override_is_explicit_and_covers_all_gateways(self) -> None:
        override = (ROOT / "docker-compose.discord-idempotency.yml").read_text(
            encoding="utf-8"
        )
        for service in EXPECTED_GATEWAY_PROFILES:
            block = _service_block(override, service)
            self.assertIn("Dockerfile.hermes-discord", block, service)
            self.assertIn("HERMES_GATEWAY_IMAGE", block, service)

    def test_each_gateway_has_one_docker_owned_profile_runtime(self) -> None:
        sources = {
            path: path.read_text(encoding="utf-8")
            for path in set(GATEWAY_SERVICES.values())
        }
        for service, path in GATEWAY_SERVICES.items():
            profile = EXPECTED_GATEWAY_PROFILES[service]
            block = _service_block(sources[path], service)
            self.assertIn('command: ["gateway", "run"]', block, service)
            self.assertIn("restart: unless-stopped", block, service)
            self.assertIn("HERMES_HOME: /opt/data", block, service)
            self.assertIn(f"/.hermes/profiles/{profile}:/opt/data", block, service)
            self.assertIn("/.hermes/shared-kanban:/opt/kanban", block, service)
            self.assertNotRegex(block, r"\bsystemctl\b|\bsystemd\b", service)

    def test_compose_definitions_do_not_start_host_gateways(self) -> None:
        compose_paths = set(GATEWAY_SERVICES.values()) | {
            ROOT / "docker-compose.yml",
            ROOT / "departments/00-ceo-office/compose.yaml",
            ROOT / "departments/02-trading/compose.yaml",
            ROOT / "departments/05-accounting-portfolio/compose.yaml",
            ROOT / "departments/07-agent-workforce/compose.yaml",
        }
        for path in compose_paths:
            source = path.read_text(encoding="utf-8")
            self.assertNotRegex(source, r"\bsystemctl\b|\bsystemd\b", str(path))

    def test_docker_only_migration_runbook_covers_canary_and_rollback(self) -> None:
        runbook = (
            ROOT / "docs/02-engineering/HERMES_DISCORD_DOCKER_ONLY_MIGRATION.md"
        ).read_text(encoding="utf-8")
        for section in (
            "## A. PRECHECK",
            "## B. BACKUP",
            "## C. PULL / IMAGE WIRING",
            "## D. CEO Host Gateway stop",
            "## E. Docker CEO recreate 및 identity 확인",
            "## F. CEO canary + Kanban 검증",
            "## G. 나머지 7개 Gateway 전환",
            "## H. Host systemd 8개 disable",
            "## I. Single-owner 검증",
            "## J. EC2 reboot",
            "## K. Reboot acceptance",
            "## L. Rollback",
        ):
            self.assertIn(section, runbook)
        self.assertIn("systemctl --user stop hermes-gateway-ceo-agent", runbook)
        self.assertIn("systemctl --user disable", runbook)
        self.assertIn("systemctl --user enable --now", runbook)
        self.assertIn("docker compose stop ceo-hermes", runbook)

    def test_dispatcher_and_supervisor_keep_official_runtime_image(self) -> None:
        source = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        dispatcher_block = source[source.index("kanban-dispatcher:") : source.index("ceo-kanban-supervisor:")]
        supervisor_block = source[source.index("ceo-kanban-supervisor:") : source.index("paper-search-mcp:")]
        self.assertIn("image: nousresearch/hermes-agent:latest", dispatcher_block)
        self.assertIn("image: nousresearch/hermes-agent:latest", supervisor_block)
        self.assertNotIn("HERMES_GATEWAY_IMAGE", dispatcher_block)
        self.assertNotIn("HERMES_GATEWAY_IMAGE", supervisor_block)
        self.assertNotIn('command: ["gateway", "run"]', dispatcher_block)
        self.assertNotIn('command: ["gateway", "run"]', supervisor_block)

    def test_image_build_contains_fail_closed_patch_hook(self) -> None:
        dockerfile = (ROOT / "Dockerfile.hermes-discord").read_text(encoding="utf-8")
        hook = (ROOT / "deploy/hermes-discord/install_patch.py").read_text(encoding="utf-8")
        self.assertIn("gateway_patch.py", dockerfile)
        self.assertIn("refusing an unpatched gateway image", hook)


if __name__ == "__main__":
    unittest.main()
