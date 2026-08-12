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
        request = ceo.CeoAsk(query="q", request_id="request-1")
        with patch.object(ceo.hermes_boundary, "create_kanban_task", return_value=None):
            with patch("apps.api.ceo_hermes_client.ask_ceo") as ask:
                with self.assertRaises(HTTPException) as raised:
                    ceo.ceo_query(request)

        self.assertEqual(raised.exception.status_code, 503)
        ask.assert_not_called()

    def test_root_task_is_enqueued_without_direct_ceo_call(self) -> None:
        request = ceo.CeoAsk(query="q", request_id="request-2")
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

    def test_mandate_snapshot_is_frozen_into_the_root_body(self) -> None:
        """`fund_id`가 오면 Mandate 한도가 root body에 박힌다.

        부서 Hermes 컨테이너에는 `DATABASE_URL`도 governance MCP도 없어서
        `mandate_version_id`만 넘기면 풀 수 없다 - 값을 함께 실어야 한다.
        """

        request = ceo.CeoAsk(query="q", request_id="request-3", fund_id="fund-1")
        mandate = {
            "mandate_id": "m-1",
            "current_version": 2,
            "content_hash": "sha256:abc",
            "policy": {"risk_bounds": {"max_drawdown_pct": "0.15", "currency": "KRW"}},
        }
        task = {"task_id": "t_root", "status": "ready"}
        with patch.object(ceo, "fetch_current_mandate_by_fund", return_value=mandate) as fetch:
            with patch.object(ceo.hermes_boundary, "create_kanban_task", return_value=task) as create:
                with patch.object(ceo.hermes_boundary, "comment_root_scope", return_value=True):
                    ceo.ceo_query(request)

        fetch.assert_called_once_with("fund-1")
        body = create.call_args.kwargs["body"]
        self.assertIn("hgfinance.mandate-snapshot.v1", body)
        self.assertIn("mandate_version=2", body)
        self.assertIn("risk.max_drawdown_pct=0.15", body)
        # 질의는 여전히 마지막 절에 있어야 한다 - 스냅샷이 질의에 섞이면
        # `extract_user_query`가 한도 문자열을 사용자 질문으로 읽는다.
        self.assertTrue(body.rstrip().endswith("q"))

    def test_no_fund_id_means_no_mandate_lookup_and_no_block(self) -> None:
        """`fund_id`가 없으면 조회 자체를 하지 않는다. 기본 한도를 지어내지 않는다."""

        request = ceo.CeoAsk(query="q", request_id="request-4")
        task = {"task_id": "t_root", "status": "ready"}
        with patch.object(ceo, "fetch_current_mandate_by_fund") as fetch:
            with patch.object(ceo.hermes_boundary, "create_kanban_task", return_value=task) as create:
                with patch.object(ceo.hermes_boundary, "comment_root_scope", return_value=True):
                    ceo.ceo_query(request)

        fetch.assert_not_called()
        self.assertNotIn("mandate-snapshot", create.call_args.kwargs["body"])

    def test_mandate_lookup_failure_does_not_block_the_query(self) -> None:
        """Mandate를 못 읽어도 질의는 접수된다.

        여기서 실패시키면 Mandate가 없는 사용자는 아무 질문도 못 한다. CEO 산출물은
        `binding: false`라 스냅샷 부재가 잘못된 주문으로 이어지지 않는다.
        """

        request = ceo.CeoAsk(query="q", request_id="request-5", fund_id="fund-1")
        task = {"task_id": "t_root", "status": "ready"}
        with patch.object(ceo, "fetch_current_mandate_by_fund", return_value=None):
            with patch.object(ceo.hermes_boundary, "create_kanban_task", return_value=task) as create:
                with patch.object(ceo.hermes_boundary, "comment_root_scope", return_value=True):
                    response = ceo.ceo_query(request)

        self.assertEqual(response["task_id"], "t_root")
        self.assertNotIn("mandate-snapshot", create.call_args.kwargs["body"])

    def test_route_returns_accepted_status(self) -> None:
        route = next(route for route in ceo.router.routes if route.path == "/ui/ceo/ask")
        self.assertEqual(route.status_code, 202)


if __name__ == "__main__":
    unittest.main()
