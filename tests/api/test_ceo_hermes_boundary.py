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

    def test_invalid_qa_primary_is_rejected_before_bff_cli(self) -> None:
        with patch.object(hermes_boundary.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                hermes_boundary.create_kanban_task(
                    assignee="qa-department",
                    title="QA primary",
                    body="workflow_root_task_id=root\nworkflow_role=primary",
                    idempotency_key="root:primary:qa-department",
                )

        run.assert_not_called()


class CeoRootTaskBoundaryTest(unittest.TestCase):
    def test_root_task_failure_does_not_call_ceo(self) -> None:
        request = ceo.CeoAsk(query="q", request_id="request-1")
        with patch.object(ceo.hermes_boundary, "create_kanban_task", return_value=None):
            with patch("apps.api.ceo_hermes_client.ask_ceo") as ask:
                with self.assertRaises(HTTPException) as raised:
                    ceo.ceo_query(request)

        self.assertEqual(raised.exception.status_code, 503)
        ask.assert_not_called()

    @patch.dict("os.environ", {"CEO_PLANNING_WAIT_SECONDS": "0"}, clear=False)
    def test_root_task_is_enqueued_without_direct_ceo_call(self) -> None:
        request = ceo.CeoAsk(query="q", request_id="request-2")
        task = {"task_id": "t_root", "status": "ready"}
        with patch.object(ceo.hermes_boundary, "create_kanban_task", return_value=task) as create:
            with patch.object(ceo.hermes_boundary, "comment_root_scope", return_value=True) as comment, patch.object(
                ceo.hermes_boundary, "show_kanban_task", return_value=None
            ):
                with patch("apps.api.ceo_hermes_client.ask_ceo") as ask:
                    response = ceo.ceo_query(request)

        create.assert_called_once()
        comment.assert_called_once_with(task_id="t_root", request_id="request-2")
        ask.assert_not_called()
        self.assertEqual(response["task"], task)
        self.assertEqual(response["task_id"], "t_root")
        self.assertEqual(response["schema_version"], "ceo.query-accepted.v2")
        self.assertEqual(response["status"], "accepted")
        self.assertIsNone(response["session_id"])

    @patch.dict("os.environ", {"CEO_PLANNING_WAIT_SECONDS": "0"}, clear=False)
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
                with patch.object(
                    ceo.hermes_boundary, "comment_root_scope", return_value=True
                ), patch.object(ceo.hermes_boundary, "show_kanban_task", return_value=None):
                    ceo.ceo_query(request)

        fetch.assert_called_once_with("fund-1")
        body = create.call_args.kwargs["body"]
        self.assertIn("hgfinance.mandate-snapshot.v1", body)
        self.assertIn("mandate_version=2", body)
        self.assertIn("risk.max_drawdown_pct=0.15", body)
        # 질의는 여전히 마지막 절에 있어야 한다 - 스냅샷이 질의에 섞이면
        # `extract_user_query`가 한도 문자열을 사용자 질문으로 읽는다.
        self.assertTrue(body.rstrip().endswith("q"))

    @patch.dict("os.environ", {"CEO_PLANNING_WAIT_SECONDS": "0"}, clear=False)
    def test_no_fund_id_means_no_mandate_lookup_and_no_block(self) -> None:
        """`fund_id`가 없으면 조회 자체를 하지 않는다. 기본 한도를 지어내지 않는다."""

        request = ceo.CeoAsk(query="q", request_id="request-4")
        task = {"task_id": "t_root", "status": "ready"}
        with patch.object(ceo, "fetch_current_mandate_by_fund") as fetch:
            with patch.object(ceo.hermes_boundary, "create_kanban_task", return_value=task) as create:
                with patch.object(
                    ceo.hermes_boundary, "comment_root_scope", return_value=True
                ), patch.object(ceo.hermes_boundary, "show_kanban_task", return_value=None):
                    ceo.ceo_query(request)

        fetch.assert_not_called()
        self.assertNotIn("mandate-snapshot", create.call_args.kwargs["body"])

    @patch.dict("os.environ", {"CEO_PLANNING_WAIT_SECONDS": "0"}, clear=False)
    def test_mandate_lookup_failure_does_not_block_the_query(self) -> None:
        """Mandate를 못 읽어도 질의는 접수된다.

        여기서 실패시키면 Mandate가 없는 사용자는 아무 질문도 못 한다. CEO 산출물은
        `binding: false`라 스냅샷 부재가 잘못된 주문으로 이어지지 않는다.
        """

        request = ceo.CeoAsk(query="q", request_id="request-5", fund_id="fund-1")
        task = {"task_id": "t_root", "status": "ready"}
        with patch.object(ceo, "fetch_current_mandate_by_fund", return_value=None):
            with patch.object(ceo.hermes_boundary, "create_kanban_task", return_value=task) as create:
                with patch.object(
                    ceo.hermes_boundary, "comment_root_scope", return_value=True
                ), patch.object(ceo.hermes_boundary, "show_kanban_task", return_value=None):
                    response = ceo.ceo_query(request)

        self.assertEqual(response["task_id"], "t_root")
        self.assertNotIn("mandate-snapshot", create.call_args.kwargs["body"])

    def test_planned_response_uses_only_current_root_departments(self) -> None:
        request = ceo.CeoAsk(query="q", request_id="request-3")
        root = {
            "id": "t_root",
            "status": "running",
            "children": [
                {
                    "id": "t_research",
                    "assignee": "research-department",
                    "body": "workflow_role=primary",
                },
                {
                    "id": "t_risk",
                    "assignee": "risk-management",
                    "body": "workflow_role=primary",
                },
                {
                    "id": "t_qa",
                    "assignee": "qa-department",
                    "body": "workflow_role=qa",
                },
            ],
            "latest_summary": "Research와 Risk를 분석한 뒤 QA 검증을 진행합니다.",
        }
        with patch.object(
            ceo.hermes_boundary,
            "create_kanban_task",
            return_value={"task_id": "t_root", "status": "ready"},
        ), patch.object(ceo.hermes_boundary, "comment_root_scope", return_value=True), patch.object(
            ceo.hermes_boundary, "show_kanban_task", return_value=root
        ):
            response = ceo.ceo_query(request)

        self.assertEqual(response["status"], "planned")
        self.assertEqual(
            response["planning"]["selected_departments"],
            ["research-department", "risk-management"],
        )
        self.assertTrue(response["planning"]["qa_required"])
        self.assertIn("Research", response["answer"])
        self.assertIn("Risk", response["answer"])
        self.assertNotIn("QA", response["answer"])
        self.assertNotIn("Quant", response["answer"])

    def test_planning_without_qa_does_not_claim_qa(self) -> None:
        root = {
            "id": "t_root",
            "children": [
                {"assignee": "research-department", "body": "workflow_role=primary"},
                {"assignee": "risk-management", "body": "workflow_role=primary"},
            ],
        }
        acknowledgement = ceo._planning_acknowledgement(root)
        self.assertFalse(acknowledgement["planning"]["qa_required"])
        self.assertNotIn("QA", acknowledgement["answer"])

    def test_planned_metadata_without_materialized_child_does_not_claim_delegation(self) -> None:
        # Regression for t_71e0df48: the CEO run metadata selected QA, but no
        # QA child was created.  A planning summary must not become a
        # user-facing delegation claim.
        root = {
            "id": "t_71e0df48",
            "children": [],
            "latest_summary": "QA fast_advisory 점검으로 위임했습니다.",
            "runs": [
                {
                    # Hermes task_runs expose metadata as JSON text in the
                    # production show projection.
                    "metadata": json.dumps(
                        {
                            "selected_primary_profiles": "qa-department",
                            "analysis_mode": "fast_advisory",
                        }
                    )
                }
            ],
        }

        acknowledgement = ceo._planning_acknowledgement(root)

        self.assertEqual(acknowledgement["status"], "accepted")
        self.assertEqual(acknowledgement["planning"]["planned_departments"], ["qa-department"])
        self.assertEqual(acknowledgement["planning"]["materialized_departments"], [])
        self.assertNotIn("QA에서", acknowledgement["answer"])
        self.assertIn("CEO가 직접 확인 중입니다", acknowledgement["answer"])

    def test_display_uses_materialized_primary_children_only(self) -> None:
        root = {
            "id": "t_root",
            "children": [
                {
                    "id": "t_research",
                    "assignee": "research-department",
                    "body": "workflow_role=primary",
                },
            ],
            "runs": [
                {
                    "metadata": {
                        "selected_primary_profiles": (
                            "research-department,qa-department"
                        ),
                        "qa_required": True,
                    }
                }
            ],
        }

        acknowledgement = ceo._planning_acknowledgement(root)

        self.assertEqual(
            acknowledgement["planning"]["selected_departments"],
            ["research-department"],
        )
        self.assertEqual(
            acknowledgement["planning"]["planned_departments"],
            ["research-department", "qa-department"],
        )
        self.assertFalse(acknowledgement["planning"]["qa_required"])
        self.assertNotIn("QA", acknowledgement["answer"])

    def test_planning_projection_uses_current_root_scope_only(self) -> None:
        root = {"id": "t_root", "children": []}
        rows = (
            {
                "id": "t_old",
                "assignee": "quant-backtest-department",
                "body": "workflow_root_task_id=t_old_root\nworkflow_role=primary",
            },
            {
                "id": "t_research",
                "assignee": "research-department",
                "body": "workflow_root_task_id=t_root\nworkflow_role=primary",
            },
            {
                "id": "t_risk",
                "assignee": "risk-management",
                "body": "workflow_root_task_id=t_root\nworkflow_role=primary",
            },
            {
                "id": "t_qa",
                "assignee": "qa-department",
                "body": "workflow_root_task_id=t_root\nworkflow_role=qa",
            },
        )
        with patch.object(ceo.hermes_boundary, "list_kanban_tasks", return_value=rows):
            projection = ceo._scoped_planning_projection(root, timeout=0.1)
        acknowledgement = ceo._planning_acknowledgement(projection)
        self.assertEqual(
            acknowledgement["planning"]["selected_departments"],
            ["research-department", "risk-management"],
        )
        self.assertTrue(acknowledgement["planning"]["qa_required"])
        self.assertNotIn("Quant", acknowledgement["answer"])

    def test_task_status_route_reads_planning_projection(self) -> None:
        root = {
            "id": "t_root",
            "status": "running",
            "children": [
                {"assignee": "quant-backtest-department", "body": "workflow_role=primary"}
            ],
        }
        workflow = MagicMock()
        workflow.root_task_id = "t_root"
        workflow.status = "running"
        workflow.root.profile = "ceo-agent"
        workflow.root.created_at = None
        workflow.root.completed_at = None
        workflow.query = None
        workflow.selected_departments = ("quant-backtest-department",)
        workflow.qa_required = True
        workflow.primary_nodes = (MagicMock(done=False),)
        workflow.qa_stage = "todo"
        workflow.synthesis_stage = "todo"
        with (
            patch.object(ceo, "_load", return_value=workflow),
            patch.object(ceo.hermes_boundary, "show_kanban_task", return_value=root),
        ):
            response = ceo.ceo_task_status("t_root")
        self.assertEqual(response["task_id"], "t_root")
        self.assertEqual(
            response["planning"]["selected_departments"],
            ["quant-backtest-department"],
        )

    @patch.dict(
        "os.environ",
        {"CEO_PLANNING_WAIT_SECONDS": "0.01", "CEO_PLANNING_READ_TIMEOUT_SECONDS": "0.1"},
        clear=False,
    )
    def test_planning_timeout_returns_accepted_fallback(self) -> None:
        with patch.object(
            ceo.hermes_boundary,
            "show_kanban_task",
            return_value={"id": "t_root", "children": []},
        ):
            response = ceo._wait_for_planning("t_root")
        self.assertEqual(response["status"], "accepted")
        self.assertEqual(response["planning"]["selected_departments"], [])

    def test_route_returns_accepted_status(self) -> None:
        """`POST /ui/ceo/ask`는 이제 `ceo.router`가 아니라 `ceo_mirror_api.router`가
        유일하게 등록한다 - `ceo.ceo_query`는 순수 함수라 자체 route가 없다
        (`tests/api/test_main_routes.py`가 이 단일 소유 상태를 앱 전체 기준으로 고정).
        """
        from apps.api import ceo_mirror_api

        route = next(
            route
            for route in ceo_mirror_api.router.routes
            if route.path == "/ui/ceo/ask"
        )
        self.assertEqual(route.status_code, 202)
        self.assertFalse(
            any(route.path == "/ui/ceo/ask" for route in ceo.router.routes),
            "ceo.router가 /ask를 다시 등록하면 mirror와 경로가 또 겹친다",
        )


if __name__ == "__main__":
    unittest.main()
