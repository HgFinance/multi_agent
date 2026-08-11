"""CEO BFF -> Hermes API and durable root-task boundary contracts."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from apps.api import ceo
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


class CeoRootTaskBoundaryTest(unittest.TestCase):
    def test_root_task_failure_does_not_call_ceo(self) -> None:
        request = ceo.hermes_boundary.AgentAsk(query="q", request_id="request-1")
        with patch.object(ceo.hermes_boundary, "create_kanban_task", return_value=None):
            with patch.object(ceo, "ask_ceo") as ask:
                with self.assertRaises(HTTPException) as raised:
                    ceo.ceo_query(request)

        self.assertEqual(raised.exception.status_code, 503)
        ask.assert_not_called()

    def test_root_task_is_created_before_ceo_call(self) -> None:
        request = ceo.hermes_boundary.AgentAsk(query="q", request_id="request-2")
        task = {"task_id": "t_root", "status": "ready"}
        result = {"answer": "ok", "session_id": "session-2"}
        with patch.object(ceo.hermes_boundary, "create_kanban_task", return_value=task) as create:
            with patch.object(ceo, "ask_ceo", return_value=result) as ask:
                response = ceo.ceo_query(request)

        create.assert_called_once()
        ask.assert_called_once()
        self.assertIn("t_root", ask.call_args.kwargs["query"])
        self.assertEqual(response["task"], task)
        self.assertEqual(response["answer"], "ok")


if __name__ == "__main__":
    unittest.main()
