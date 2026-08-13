"""Regression checks for the single production HTTP BFF contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Self

from apps.api.ceo_mirror import CanonicalIngress, LockedRedisMirrorStore

ROOT = Path(__file__).resolve().parents[2]


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
        if shutil.which("docker") is None:
            raise unittest.SkipTest("docker is not installed")
        result = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "DATABASE_URL": "postgresql://test:test@localhost/test",
                "HEDGEFUND_TSDB_PASSWORD": "compose-contract-test",
            },
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
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

    def test_legacy_ui_bff_is_not_in_default_stack(self) -> None:
        self.assertNotIn("ui-bff", self.services)

    def test_legacy_ui_bff_has_no_host_port_when_explicitly_loaded(self) -> None:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--profile",
                "legacy-ui-bff",
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "DATABASE_URL": "postgresql://test:test@localhost/test",
                "HEDGEFUND_TSDB_PASSWORD": "compose-contract-test",
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        service = json.loads(result.stdout)["services"]["ui-bff"]
        self.assertEqual(service.get("profiles"), ["legacy-ui-bff"])
        self.assertNotIn("ports", service)

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
