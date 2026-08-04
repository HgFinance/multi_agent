"""Acceptance checks for the AI Office operator projection."""

from __future__ import annotations

import os
import unittest


# The BFF must remain testable in DEMO mode without attempting a configured
# remote database.  This does not expose or print the caller's real DSN.
os.environ["DATABASE_URL"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from apps.api.main import app, _repo  # noqa: E402


class OperatorBffOperationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _repo.cache_clear()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def test_snapshot_projects_department_and_event_contracts(self) -> None:
        response = self.client.get("/ui/snapshot")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        operations = body["operations"]
        self.assertEqual(operations["schema_version"], "operator-operations.v1")
        self.assertEqual(len(operations["departments"]), 8)
        self.assertEqual(operations["status"], "DEGRADED")
        self.assertFalse(operations["runtime_connected"])
        self.assertFalse(operations["event_bridge_connected"])
        self.assertEqual(operations["message_count"], 0)
        self.assertIn(
            "risk.decision.v1",
            {event["event_type"] for event in operations["communications"]},
        )

    def test_existing_read_model_remains_the_single_financial_source(self) -> None:
        body = self.client.get("/ui/snapshot").json()
        self.assertEqual(body["mode"], "DEMO")
        self.assertEqual(body["trading"]["orders"][0]["state"], "FILLED")
        self.assertEqual(body["sources"]["portfolio"], "scripted-loop")

    def test_integration_projection_exposes_readiness_without_secrets(self) -> None:
        response = self.client.get("/ui/integrations")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"notion", "discord", "instagram", "gmail", "finance"})
        self.assertFalse(body["notion"]["configured"])
        self.assertNotIn("TOKEN", body["notion"].get("value", ""))
        self.assertNotIn("WEBHOOK", body["discord"].get("value", ""))


if __name__ == "__main__":
    unittest.main()
