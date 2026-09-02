"""Regression checks for the single production HTTP BFF contract."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Self

from apps.api.ceo_mirror import CanonicalIngress, LockedRedisMirrorStore

ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_STACK_COMPOSE_FILES = (
    ROOT / "docker-compose.yml",
    ROOT / "departments/00-ceo-office/compose.yaml",
    ROOT / "departments/02-trading/compose.yaml",
    ROOT / "departments/05-accounting-portfolio/compose.yaml",
    ROOT / "departments/07-agent-workforce/compose.yaml",
)
_COMPOSE_TEST_ENV = {
    # CI has no developer .env file. Keep local verification equally isolated
    # so a workstation secret cannot hide a missing contract-test fixture.
    "COMPOSE_DISABLE_ENV_FILE": "1",
    "DATABASE_URL": "postgresql://test:test@localhost/test",
    "HEDGEFUND_RUNTIME_DB_PASSWORD": "compose-control-contract-test",
    "HEDGEFUND_TSDB_PASSWORD": "compose-contract-test",
    "CEO_HERMES_API_KEY": "compose-contract-ceo-hermes-key-32-bytes",
    "CEO_DISCORD_INGRESS_API_KEY": "compose-discord-ingress-key-0123456789abcdef",
    "NAVER_CLIENT_ID": "compose-contract-test",
    "NAVER_CLIENT_SECRET": "compose-contract-test",
    "SUPABASE_URL": "https://compose-contract-test.invalid",
    "SUPABASE_SERVICE_ROLE_KEY": "compose-contract-test",
    "TRADING_SERVICE_AUTH_SECRET": "compose-contract-trading-proof-secret-32-bytes",
    "TRADING_INTERNAL_SERVICE_AUTH_SECRET": (
        "compose-contract-internal-trading-secret-32-bytes"
    ),
    "MCP_TRADING_ORDER_API_KEY": "compose-contract-paper-order-mcp-key-32-bytes",
    "MCP_RISK_API_KEY": "compose-contract-risk-legal-mcp-key-32-bytes",
    "STRATEGY_PAPER_ORDER_TOKEN": "compose-contract-strategy-paper-order-token-32b",
    "HEDGEFUND_ACCOUNTING_DB_PASSWORD": "compose-contract-accounting-db",
    "HEDGEFUND_ORDER_DB_PASSWORD": "compose-contract-order-db",
    "HEDGEFUND_TRADING_DB_PASSWORD": "compose-contract-trading-db",
    "HEDGEFUND_CONDITIONAL_ORCHESTRATOR_DB_PASSWORD": (
        "compose-contract-conditional-orchestrator-db"
    ),
    "HEDGEFUND_CONDITIONAL_WORKER_DB_PASSWORD": (
        "compose-contract-conditional-worker-db"
    ),
}

# 이 딕셔너리는 `docker-compose.yml`과 그 기본 include fragment가 `${VAR:?}`로
# **필수**라고 선언한 변수를 전부 담아야 한다. 하나라도 빠지면 렌더 자체가 실패해
# 계약 검사가 시작도 못 하고, 로컬에서는 개발자의 `.env`가 그 구멍을 메워 줘서
# CI에서만 터진다.
_REQUIRED_COMPOSE_VARIABLES = re.compile(r"\$\{([A-Z_]+):\?")


def _run_compose(*args: str) -> subprocess.CompletedProcess[str]:
    """Render Compose with either the v2 plugin or legacy standalone binary."""

    candidates: list[list[str]] = []
    if shutil.which("docker") is not None:
        candidates.append(["docker", "compose"])
    if shutil.which("docker-compose") is not None:
        candidates.append(["docker-compose"])
    if not candidates:
        raise unittest.SkipTest("Docker Compose is not installed")

    failures: list[str] = []
    for prefix in candidates:
        result = subprocess.run(
            [*prefix, *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={
                **os.environ,
                **_COMPOSE_TEST_ENV,
            },
            check=False,
        )
        if result.returncode == 0:
            return result
        failures.append(result.stderr or result.stdout)
    raise AssertionError("\n".join(failures))


class ComposeTestEnvironmentTest(unittest.TestCase):
    """필수 변수 목록이 갈리면 계약 검사가 CI에서만 깨진다.

    Docker를 부르지 않는다. 기본 stack의 root와 include fragment가
    `${VAR:?}`로 필수라고 선언한 변수와 이 파일이 렌더에 넘기는 변수를
    대조할 뿐이다. 이 검사가 없으면 compose에 필수 변수가 하나 추가될 때
    개발자 `.env`가 구멍을 메워 로컬은 통과하고 CI만 빨개진다.
    """

    def test_every_required_compose_variable_is_supplied(self) -> None:
        required: set[str] = set()
        for compose_path in _DEFAULT_STACK_COMPOSE_FILES:
            compose = compose_path.read_text(encoding="utf-8")
            required.update(_REQUIRED_COMPOSE_VARIABLES.findall(compose))
        missing = sorted(required - set(_COMPOSE_TEST_ENV))
        self.assertEqual(
            missing,
            [],
            msg=(
                "docker-compose.yml이 필수로 선언한 변수가 _COMPOSE_TEST_ENV에 "
                f"없습니다: {missing}"
            ),
        )


class _FakeRedisLock:
    def __init__(self, owner: _FakeRedis, name: str) -> None:
        self.owner = owner
        self.name = name

    def __enter__(self) -> Self:
        self.owner.lock_entries.append(self.name)
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lock_entries: list[str] = []

    def lock(self, name: str, **_kwargs: object) -> _FakeRedisLock:
        return _FakeRedisLock(self, name)

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, **_kwargs: object) -> bool:
        self.values[key] = value
        return True


class BffConsolidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        result = _run_compose("config", "--format", "json")
        cls.compose = json.loads(result.stdout)
        cls.services = cls.compose["services"]

    def test_default_stack_has_one_host_8001_owner(self) -> None:
        owners = []
        for name, service in self.services.items():
            for port in service.get("ports", []):
                published = str(port.get("published", ""))
                if published == "8001":
                    owners.append(name)
        self.assertEqual(owners, ["portfolio-bff"])

    def test_removed_legacy_ui_bff_is_not_defined(self) -> None:
        self.assertNotIn("ui-bff", self.services)

    def test_portfolio_runtime_volume_is_shared_by_bff_and_worker(self) -> None:
        for name in ("portfolio-bff", "portfolio-worker"):
            mounts = self.services[name].get("volumes", [])
            self.assertTrue(
                any(
                    mount.get("source") == "portfolio_runtime_data"
                    and mount.get("target") == "/var/lib/portfolio"
                    for mount in mounts
                    if isinstance(mount, dict)
                )
            )

        portfolio_bff_env = self.services["portfolio-bff"].get("environment", {})
        portfolio_worker_env = self.services["portfolio-worker"].get("environment", {})
        self.assertEqual(
            portfolio_bff_env["PORTFOLIO_RUNTIME_STORE_PATH"],
            "/var/lib/portfolio/runtime.sqlite3",
        )
        self.assertEqual(
            portfolio_worker_env["PORTFOLIO_RUNTIME_STORE_PATH"],
            "/var/lib/portfolio/runtime.sqlite3",
        )
        self.assertEqual(portfolio_bff_env["PORTFOLIO_RUNTIME_EMBEDDED_WORKER"], "false")
        self.assertEqual(portfolio_worker_env["PORTFOLIO_RUNTIME_EMBEDDED_WORKER"], "false")

    def test_portfolio_bff_owns_mirror_environment(self) -> None:
        environment = self.services["portfolio-bff"].get("environment", {})
        for name in (
            "REDIS_URL",
            "UI_MIRROR_REDIS_URL",
            "UI_MIRROR_STREAM",
            "UI_MIRROR_DEDUPE_TTL_SECONDS",
            "UI_MIRROR_DEDUPE_WAIT_SECONDS",
            "UI_MIRROR_SSE_SECONDS",
        ):
            self.assertIn(name, environment)

    def test_redis_request_claim_uses_distributed_claim_lock(self) -> None:
        fake_redis = _FakeRedis()
        store = object.__new__(LockedRedisMirrorStore)
        store.client = fake_redis
        store.stream = "hf:ui-ceo-mirror:v1"
        store.ttl_seconds = 60
        store.request_prefix = "hf:ui-ceo-mirror:request:"
        store.source_prefix = "hf:ui-ceo-mirror:source:"
        store.event_prefix = "hf:ui-ceo-mirror:event:"

        record, created = store.claim_request(
            CanonicalIngress(
                query="claim lock contract",
                request_id="request-redis-lock-1",
                source="web",
                source_message_id="web:redis-lock:1",
                actor_id="web-user",
            )
        )

        self.assertTrue(created)
        self.assertEqual(record.request.request_id, "request-redis-lock-1")
        self.assertEqual(fake_redis.lock_entries, ["hf:ui-ceo-mirror:claim-lock"])


if __name__ == "__main__":
    unittest.main()
