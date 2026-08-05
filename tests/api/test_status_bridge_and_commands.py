"""Integration tests for operator status projection and safe command intake."""

from __future__ import annotations

import os
import unittest

os.environ["DATABASE_URL"] = ""
os.environ["ENABLE_AGENT_ASK"] = "0"

from fastapi.testclient import TestClient

from apps.api.main import app, _repo
from agent_status import AGENT_STATUS_PROJECTOR, publish_agent_status
from apps.api.kanban_status_bridge import KanbanStatusBridge


class StatusBridgeAndCommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _repo.cache_clear()
        AGENT_STATUS_PROJECTOR.reset()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        AGENT_STATUS_PROJECTOR.reset()
        cls.client.close()

    def setUp(self) -> None:
        AGENT_STATUS_PROJECTOR.reset()

    def test_department_agent_routes_are_explicit_and_disabled_by_default(self) -> None:
        for path in ("research", "risk", "quant", "qa"):
            response = self.client.post(f"/{path}/agent/ask", json={"query": "status"})
            self.assertEqual(response.status_code, 503, path)

        self.assertEqual(
            self.client.post("/agent/ask", json={"department": "risk-management", "query": "status"}).status_code,
            404,
        )

    def test_status_event_projects_to_snapshot_and_websocket(self) -> None:
        publish_agent_status(
            department_code="trading-department",
            agent_id="pre-trade-risk-worker",
            worker_id="pre-trade-risk-worker",
            status="RUNNING",
            role="Pre-trade Risk",
            reason="test event",
        )
        snapshot = self.client.get("/ui/snapshot").json()
        operations = snapshot["operations"]
        self.assertEqual(operations["sequence"], 1)
        self.assertTrue(operations["event_bridge_connected"])
        self.assertEqual(operations["agent_statuses"][0]["status"], "RUNNING")
        trading = next(row for row in operations["departments"] if row["department_code"] == "trading-department")
        self.assertEqual(trading["status"], "RUNNING")
        self.assertEqual(trading["active_worker_count"], 1)

        with self.client.websocket_connect("/ws/operations") as websocket:
            envelope = websocket.receive_json()
        self.assertEqual(envelope["event_type"], "operations.snapshot_required.v1")
        self.assertEqual(envelope["sequence"], 1)

    def test_kanban_bridge_normalizes_and_deduplicates_task_events(self) -> None:
        bridge = KanbanStatusBridge(AGENT_STATUS_PROJECTOR)
        first = bridge.publish_task_event(
            {
                "event_id": "kanban-test-event-001",
                "department_code": "trading-department",
                "profile_id": "trader-worker",
                "status": "in_progress",
                "title": "paper risk review",
            }
        )
        replay = bridge.publish_task_event(
            {
                "event_id": "kanban-test-event-001",
                "department_code": "trading-department",
                "profile_id": "trader-worker",
                "status": "in_progress",
                "title": "changed title is ignored on replay",
            }
        )
        self.assertEqual(first.sequence, 1)
        self.assertEqual(replay.sequence, first.sequence)
        self.assertEqual(first.status, "RUNNING")

    def test_domain_read_models_are_degraded_until_runtime_event(self) -> None:
        for path, domain in (
            ("/ui/research", "research"),
            ("/ui/strategy", "strategy"),
            ("/ui/risk", "risk"),
            ("/ui/qa", "qa"),
            ("/ui/risk-qa", "risk-qa"),
        ):
            body = self.client.get(path).json()
            self.assertEqual(body["schema_version"], "operator-domain.v1")
            self.assertEqual(body["domain"], domain)
            self.assertEqual(body["mode"], "DEMO")
            self.assertIn(body["status"], {"CONNECTED", "DEGRADED"})

    def test_command_is_idempotent_audited_and_non_binding(self) -> None:
        payload = {
            "command": "SET_TRADING_STATE",
            "target": {"fund_id": "test-command-fund"},
            "requested_state": "ENTRY_BLOCKED",
            "reason": "manual review required",
            "idempotency_key": "status-command-test-001",
            "expected_version": 0,
        }
        response = self.client.post("/ui/commands/trading-state", json=payload)
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "PENDING_APPROVAL")
        self.assertEqual(body["execution_status"], "NOT_EXECUTED")
        self.assertFalse(body["binding_executed"])

        replay = self.client.post("/ui/commands/trading-state", json=payload)
        self.assertEqual(replay.status_code, 202)
        self.assertTrue(replay.json()["replayed"])

        stale = {**payload, "idempotency_key": "status-command-test-002", "expected_version": 1}
        self.assertEqual(self.client.post("/ui/commands/trading-state", json=stale).status_code, 409)
        audit = self.client.get("/ui/commands/audit").json()
        self.assertTrue(any(item["command_id"] == body["command_id"] for item in audit["events"]))


if __name__ == "__main__":
    unittest.main()
