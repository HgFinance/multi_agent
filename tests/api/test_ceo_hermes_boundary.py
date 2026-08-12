"""CEO BFF -> Hermes API and durable root-task boundary contracts."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from apps.api import ceo, hermes_boundary
from apps.api.ceo_hermes_client import ask_ceo


class CeoHermesApiClientTest(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {
            "ENABLE_AGENT_ASK": "true",
            "HERMES_CEO_API_URL": "http://ceo-hermes:8642/v1",
            "HERMES_CEO_API_KEY": "x" * 32,
        },
        clear=False,
    )
    @patch("apps.api.ceo_hermes_client.httpx.Client")
    def test_uses_existing_authenticated_ceo_api(self, client_type: MagicMock) -> None:
        response = MagicMock()
        response.status_code = 200
        response.headers = {"X-Hermes-Session-Id": "session-1"}
        response.json.return_value = {
            "choices": [{"message": {"content": "CEO answer"}}]
        }
        client = client_type.return_value.__enter__.return_value
        client.post.return_value = response

        result = ask_ceo(query="analyze Samsung Electronics", timeout=10)

        client.post.assert_called_once_with(
            "http://ceo-hermes:8642/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {'x' * 32}",
                "Content-Type": "application/json",
            },
            json={
                "model": "hermes-agent",
                "messages": [
                    {"role": "user", "content": "analyze Samsung Electronics"}
                ],
                "stream": False,
            },
        )
        self.assertEqual(result["answer"], "CEO answer")
        self.assertEqual(result["session_id"], "session-1")


class CreateKanbanTaskCliContractTest(unittest.TestCase):
    """`hermes kanban create` only accepts `--initial-status {blocked,running}`.

    A root task has no parent, so leaving the flag off is what actually
    produces `status: ready` (verified against the real Hermes CLI). Passing
    `--initial-status ready` is a usage error the CLI rejects outright, which
    silently became a 503 here because ``create_kanban_task`` swallows every
    subprocess failure into ``None``.
    """

    def test_create_command_never_passes_an_invalid_initial_status(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout=json.dumps({"id": "t_root1", "status": "ready"}),
            stderr="",
        )
        with patch.object(hermes_boundary.subprocess, "run", return_value=completed) as run:
            task = hermes_boundary.create_kanban_task(
                assignee="ceo-agent",
                title="title",
                body="body",
                idempotency_key="idem-1",
            )

        self.assertEqual(task, {"task_id": "t_root1", "status": "ready", "source": "hermes-kanban"})
        command = run.call_args.args[0]
        self.assertNotIn("--initial-status", command)
        self.assertNotIn("ready", command)


class CeoRootTaskBoundaryTest(unittest.TestCase):
    def test_root_task_failure_does_not_call_ceo(self) -> None:
        request = ceo.hermes_boundary.AgentAsk(query="q", request_id="request-1")
        with patch.object(ceo.hermes_boundary, "create_kanban_task", return_value=None):
            with patch("apps.api.ceo_hermes_client.ask_ceo") as ask:
                with self.assertRaises(HTTPException) as raised:
                    ceo.ceo_query(request)

        self.assertEqual(raised.exception.status_code, 503)
        ask.assert_not_called()

    def test_root_task_is_enqueued_without_direct_ceo_call(self) -> None:
        request = ceo.hermes_boundary.AgentAsk(query="q", request_id="request-2")
        task = {"task_id": "t_root", "status": "ready"}
        with patch.object(ceo.hermes_boundary, "create_kanban_task", return_value=task) as create:
            with patch.object(ceo.hermes_boundary, "comment_root_scope", return_value=True) as comment:
                with patch("apps.api.ceo_hermes_client.ask_ceo") as ask:
                    response = ceo.ceo_query(request)

        create.assert_called_once()
        comment.assert_called_once_with(task_id="t_root", request_id="request-2")
        ask.assert_not_called()
        self.assertEqual(response["task"], task)
        self.assertEqual(response["task_id"], "t_root")
        self.assertEqual(response["schema_version"], "ceo.query-accepted.v1")
        self.assertIsNone(response["session_id"])

    def test_route_returns_accepted_status(self) -> None:
        route = next(route for route in ceo.router.routes if route.path == "/ui/ceo/ask")
        self.assertEqual(route.status_code, 202)


if __name__ == "__main__":
    unittest.main()
