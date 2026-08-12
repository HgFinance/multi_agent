"""CEO Kanban Read Model과 `/ui/ceo/tasks/*` 응답 계약.

픽스처는 Hermes Agent v0.19.0 `kanban show --json` 실측 형태를 그대로 쓴다
(`{"task": {...}, "latest_summary": ..., "parents": [...], "children": [...],
"runs": [...]}`). CLI를 부르지 않으므로 Hermes 설치 없이 돌아간다.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api import ceo, ceo_kanban_read
from apps.api.ceo_kanban_read import (
    QA_BLOCKED_VERDICT,
    KanbanTaskNotFound,
    KanbanUnavailable,
    load_workflow,
)
from orchestration.adapters.ceo_supervisor import SUPERVISOR_MARKER
from orchestration.ceo_workflow_scope import build_root_body

ROOT_ID = "t_root0001"
RESEARCH_ID = "t_res00001"
RISK_ID = "t_risk0001"
QA_ID = "t_qa000001"
SYNTHESIS_ID = "t_syn00001"

_CREATED_AT = 1786497945  # 2026-08-12T01:25:45Z


def _task(
    task_id: str,
    *,
    assignee: str,
    status: str,
    body: str = "body",
    title: str = "title",
    parents: tuple[str, ...] = (),
    children: tuple[str, ...] = (),
    latest_summary: str | None = None,
    runs: tuple[dict[str, Any], ...] = (),
    completed_at: int | None = None,
) -> dict[str, Any]:
    """Hermes `kanban show --json` 응답 한 건."""

    return {
        "id": task_id,
        "title": title,
        "body": body,
        "assignee": assignee,
        "status": status,
        "created_at": _CREATED_AT,
        "started_at": None,
        "completed_at": completed_at,
        "result": latest_summary,
        "session_id": None,
        "latest_summary": latest_summary,
        "parents": list(parents),
        "children": list(children),
        "comments": [],
        "events": [],
        "runs": list(runs),
    }


def _board(
    *,
    qa_status: str = "todo",
    qa_summary: str | None = None,
    synthesis_status: str = "todo",
    synthesis_summary: str | None = None,
    include_qa: bool = True,
    include_synthesis: bool = True,
    risk_status: str = "running",
) -> dict[str, dict[str, Any]]:
    """Root -> (research, risk) -> QA -> Synthesis 그래프."""

    children = [RESEARCH_ID, RISK_ID]
    if include_qa:
        children.append(QA_ID)
    if include_synthesis:
        children.append(SYNTHESIS_ID)

    board: dict[str, dict[str, Any]] = {
        ROOT_ID: _task(
            ROOT_ID,
            assignee="ceo-agent",
            status="running",
            body=build_root_body("엔비디아 최신 사업 리스크만 분석해줘.", "req-1"),
            title="사용자 질의: 엔비디아 최신 사업 리스크만 분석해줘.",
            children=tuple(children),
        ),
        RESEARCH_ID: _task(
            RESEARCH_ID,
            assignee="research-department",
            status="done",
            parents=(ROOT_ID,),
            latest_summary="리서치 요약",
            completed_at=_CREATED_AT + 60,
        ),
        RISK_ID: _task(
            RISK_ID,
            assignee="risk-management",
            status=risk_status,
            parents=(ROOT_ID,),
            latest_summary="리스크 요약" if risk_status == "done" else None,
        ),
    }
    if include_qa:
        board[QA_ID] = _task(
            QA_ID,
            assignee="qa-department",
            status=qa_status,
            body=f"{SUPERVISOR_MARKER} action=RUN_QA",
            parents=(RESEARCH_ID, RISK_ID),
            latest_summary=qa_summary,
        )
    if include_synthesis:
        board[SYNTHESIS_ID] = _task(
            SYNTHESIS_ID,
            assignee="ceo-agent",
            status=synthesis_status,
            body=f"{SUPERVISOR_MARKER} action=SYNTHESIZE",
            parents=(RESEARCH_ID, RISK_ID, QA_ID) if include_qa else (RESEARCH_ID, RISK_ID),
            latest_summary=synthesis_summary,
        )
    return board


def _fetch_from(board: dict[str, dict[str, Any]]):
    def fetch(task_id: str) -> dict[str, Any]:
        try:
            return board[task_id]
        except KeyError as exc:
            raise KanbanTaskNotFound(f"no such task: {task_id}") from exc

    return fetch


class WorkflowReadModelTest(unittest.TestCase):
    def test_graph_is_loaded_from_root_and_classified(self) -> None:
        workflow = load_workflow(ROOT_ID, fetch=_fetch_from(_board()))

        self.assertEqual(workflow.root_task_id, ROOT_ID)
        self.assertEqual(len(workflow.nodes), 5)
        # QA와 Synthesis는 분석 부서로 세지 않는다. Supervisor와 같은 분류다.
        self.assertEqual(
            [node.task_id for node in workflow.primary_nodes],
            [RESEARCH_ID, RISK_ID],
        )
        self.assertEqual([node.task_id for node in workflow.qa_nodes], [QA_ID])
        self.assertIsNotNone(workflow.synthesis_node)
        self.assertEqual(workflow.synthesis_node.task_id, SYNTHESIS_ID)
        self.assertEqual(
            workflow.selected_departments,
            ("research-department", "risk-management"),
        )

    def test_child_id_resolves_to_the_same_workflow(self) -> None:
        workflow = load_workflow(QA_ID, fetch=_fetch_from(_board()))

        self.assertEqual(workflow.root_task_id, ROOT_ID)

    def test_edges_only_keep_parents_inside_the_graph(self) -> None:
        board = _board()
        board[RISK_ID]["parents"] = [ROOT_ID, "t_other999"]

        workflow = load_workflow(ROOT_ID, fetch=_fetch_from(board))

        self.assertNotIn(("t_other999", RISK_ID), workflow.edges)
        self.assertIn((ROOT_ID, RISK_ID), workflow.edges)
        self.assertIn((QA_ID, SYNTHESIS_ID), workflow.edges)

    def test_user_query_is_separated_from_the_scope_header(self) -> None:
        workflow = load_workflow(ROOT_ID, fetch=_fetch_from(_board()))

        self.assertEqual(workflow.query, "엔비디아 최신 사업 리스크만 분석해줘.")
        self.assertNotIn("workflow_scope", workflow.query or "")

    def test_status_is_running_while_a_child_is_open(self) -> None:
        workflow = load_workflow(ROOT_ID, fetch=_fetch_from(_board()))

        self.assertEqual(workflow.status, "running")
        self.assertEqual(workflow.qa_stage, "todo")
        self.assertEqual(workflow.synthesis_stage, "todo")

    def test_status_is_completed_once_synthesis_is_done(self) -> None:
        workflow = load_workflow(
            ROOT_ID,
            fetch=_fetch_from(
                _board(
                    risk_status="done",
                    qa_status="done",
                    qa_summary="verdict: PASS",
                    synthesis_status="done",
                    synthesis_summary="종합 결과. decision: HOLD",
                )
            ),
        )

        self.assertEqual(workflow.status, "completed")
        self.assertEqual(workflow.qa_verdict, "PASS")
        self.assertEqual(workflow.decision, "HOLD")
        self.assertIsNone(workflow.block_reason)

    def test_blocked_qa_produces_the_blocked_decision_verdict(self) -> None:
        board = _board(risk_status="done", qa_status="blocked")
        board[QA_ID]["block_reason"] = "근거 인용이 없습니다"

        workflow = load_workflow(ROOT_ID, fetch=_fetch_from(board))

        self.assertEqual(workflow.status, "blocked")
        self.assertEqual(workflow.qa_verdict, QA_BLOCKED_VERDICT)
        self.assertEqual(workflow.block_reason, "근거 인용이 없습니다")

    def test_failed_run_outcome_is_read_from_runs(self) -> None:
        board = _board(risk_status="done", include_qa=False, include_synthesis=False)
        board[RISK_ID]["status"] = "ready"
        board[RISK_ID]["runs"] = [{"outcome": "crashed", "error": "worker crashed"}]

        workflow = load_workflow(ROOT_ID, fetch=_fetch_from(board))

        self.assertEqual(workflow.status, "failed")

    def test_decision_is_null_when_synthesis_only_narrates(self) -> None:
        """라벨 없는 서술에서 결론을 추측하지 않는다(개발 원칙 2)."""

        workflow = load_workflow(
            ROOT_ID,
            fetch=_fetch_from(
                _board(
                    risk_status="done",
                    qa_status="done",
                    synthesis_status="done",
                    synthesis_summary="엔비디아는 매수를 보류하는 것이 좋겠습니다.",
                )
            ),
        )

        self.assertIsNone(workflow.decision)
        # QA가 라벨 없이 완료되면 완료 사실 자체가 PASS다.
        self.assertEqual(workflow.qa_verdict, "PASS")

    def test_qa_required_is_false_only_when_synthesis_ran_without_qa(self) -> None:
        with_qa = load_workflow(ROOT_ID, fetch=_fetch_from(_board()))
        self.assertTrue(with_qa.qa_required)

        before_qa = load_workflow(
            ROOT_ID,
            fetch=_fetch_from(_board(include_qa=False, include_synthesis=False)),
        )
        self.assertTrue(before_qa.qa_required)

        skipped = load_workflow(
            ROOT_ID,
            fetch=_fetch_from(_board(include_qa=False, synthesis_status="done")),
        )
        self.assertFalse(skipped.qa_required)

    def test_non_canonical_assignee_does_not_break_the_read_model(self) -> None:
        """정책 계층과 달리 화면용 Read Model은 Fail open이어야 한다."""

        board = _board(include_qa=False, include_synthesis=False)
        board[RISK_ID]["assignee"] = "risk-department"  # 폐기된 별칭

        workflow = load_workflow(ROOT_ID, fetch=_fetch_from(board))

        self.assertIn("risk-department", workflow.selected_departments)
        self.assertEqual(workflow.by_id[RISK_ID].department, "risk-department")

    def test_epoch_is_converted_to_iso_utc(self) -> None:
        workflow = load_workflow(ROOT_ID, fetch=_fetch_from(_board()))

        self.assertEqual(workflow.root.created_at, "2026-08-12T01:25:45Z")


class CeoRootFilterTest(unittest.TestCase):
    def test_only_user_originated_roots_are_listed(self) -> None:
        rows = [
            {"id": ROOT_ID, "body": build_root_body("질의", "req-1")},
            {"id": SYNTHESIS_ID, "body": f"{SUPERVISOR_MARKER} action=SYNTHESIZE"},
            {"id": "t_freeform", "body": "그냥 만든 Task"},
        ]
        with patch.object(ceo_kanban_read, "list_tasks", return_value=rows):
            roots = ceo_kanban_read.list_ceo_roots(limit=20)

        self.assertEqual([row["id"] for row in roots], [ROOT_ID])

    def test_limit_is_applied(self) -> None:
        rows = [
            {"id": f"t_root{index:04d}", "body": build_root_body("질의", f"req-{index}")}
            for index in range(5)
        ]
        with patch.object(ceo_kanban_read, "list_tasks", return_value=rows):
            roots = ceo_kanban_read.list_ceo_roots(limit=2)

        self.assertEqual(len(roots), 2)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(ceo.router)
    return TestClient(app)


class CeoTaskApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client()

    def test_status_endpoint_reports_progress(self) -> None:
        with patch.object(ceo, "load_workflow", return_value=load_workflow(ROOT_ID, fetch=_fetch_from(_board()))):
            response = self.client.get(f"/ui/ceo/tasks/{ROOT_ID}")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["schema_version"], "ceo.task-status.v1")
        self.assertEqual(body["status"], "running")
        self.assertEqual(body["root_task_id"], ROOT_ID)
        self.assertEqual(
            body["workflow"],
            {
                "selected_departments": ["research-department", "risk-management"],
                "qa_required": True,
            },
        )
        self.assertEqual(
            body["progress"],
            {"primary_total": 2, "primary_done": 1, "qa": "todo", "synthesis": "todo"},
        )

    def test_graph_endpoint_returns_nodes_and_edges(self) -> None:
        with patch.object(ceo, "load_workflow", return_value=load_workflow(ROOT_ID, fetch=_fetch_from(_board()))):
            response = self.client.get(f"/ui/ceo/tasks/{ROOT_ID}/graph")

        body = response.json()
        self.assertEqual(body["root"], ROOT_ID)
        roles = {node["id"]: node["role"] for node in body["nodes"]}
        self.assertEqual(roles[ROOT_ID], "root")
        self.assertEqual(roles[RESEARCH_ID], "primary")
        self.assertEqual(roles[QA_ID], "qa")
        self.assertEqual(roles[SYNTHESIS_ID], "synthesis")
        self.assertIn([ROOT_ID, RESEARCH_ID], body["edges"])
        self.assertIn([QA_ID, SYNTHESIS_ID], body["edges"])

    def test_result_is_null_while_processing(self) -> None:
        with patch.object(ceo, "load_workflow", return_value=load_workflow(ROOT_ID, fetch=_fetch_from(_board()))):
            response = self.client.get(f"/ui/ceo/tasks/{ROOT_ID}/result")

        body = response.json()
        self.assertEqual(body["status"], "processing")
        self.assertIsNone(body["result"])
        self.assertIs(body["binding"], False)

    def test_result_is_normalized_when_completed(self) -> None:
        workflow = load_workflow(
            ROOT_ID,
            fetch=_fetch_from(
                _board(
                    risk_status="done",
                    qa_status="done",
                    qa_summary="verdict: PASS",
                    synthesis_status="done",
                    synthesis_summary="엔비디아의 최신 사업 리스크를 종합하면 ... decision: DEFER",
                )
            ),
        )
        with patch.object(ceo, "load_workflow", return_value=workflow):
            response = self.client.get(f"/ui/ceo/tasks/{ROOT_ID}/result")

        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["result"]["decision"], "DEFER")
        self.assertEqual(body["result"]["qa_verdict"], "PASS")
        self.assertEqual(body["qa_verdict"], "PASS")
        self.assertEqual(
            set(body["departments"]), {"research", "risk", "qa"}
        )

    def test_result_reports_qa_block_reason(self) -> None:
        board = _board(risk_status="done", qa_status="blocked")
        board[QA_ID]["block_reason"] = "인용 근거 부족"
        workflow = load_workflow(ROOT_ID, fetch=_fetch_from(board))

        with patch.object(ceo, "load_workflow", return_value=workflow):
            response = self.client.get(f"/ui/ceo/tasks/{ROOT_ID}/result")

        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["qa_verdict"], QA_BLOCKED_VERDICT)
        self.assertEqual(body["block_reason"], "인용 근거 부족")
        self.assertIsNone(body["result"])

    def test_archive_covers_the_whole_graph_children_first(self) -> None:
        workflow = load_workflow(ROOT_ID, fetch=_fetch_from(_board()))
        with patch.object(ceo, "load_workflow", return_value=workflow):
            with patch.object(ceo, "archive_tasks") as archive:
                response = self.client.post(f"/ui/ceo/tasks/{ROOT_ID}/archive")

        self.assertEqual(response.status_code, 200)
        archived = archive.call_args.args[0]
        self.assertEqual(archived[-1], ROOT_ID, "Root는 마지막이어야 한다")
        self.assertEqual(set(archived), {ROOT_ID, RESEARCH_ID, RISK_ID, QA_ID, SYNTHESIS_ID})
        self.assertEqual(response.json()["status"], "archived")

    def test_unknown_task_is_404(self) -> None:
        with patch.object(ceo, "load_workflow", side_effect=KanbanTaskNotFound("no such task")):
            response = self.client.get("/ui/ceo/tasks/t_missing1")

        self.assertEqual(response.status_code, 404)

    def test_kanban_outage_is_503_not_500(self) -> None:
        with patch.object(ceo, "load_workflow", side_effect=KanbanUnavailable("CLI 없음")):
            response = self.client.get(f"/ui/ceo/tasks/{ROOT_ID}")

        self.assertEqual(response.status_code, 503)

    def test_malformed_task_id_is_rejected_before_the_cli(self) -> None:
        with patch.object(ceo, "load_workflow") as load:
            response = self.client.get("/ui/ceo/tasks/not-a-task-id")

        self.assertEqual(response.status_code, 422)
        load.assert_not_called()

    def test_delete_route_does_not_exist(self) -> None:
        """감사 추적은 지우지 않는다. 정리는 Archive로만 한다."""

        self.assertEqual(self.client.delete(f"/ui/ceo/tasks/{ROOT_ID}").status_code, 405)


class CeoTaskListApiTest(unittest.TestCase):
    def test_list_returns_query_and_departments(self) -> None:
        client = _client()
        rows = [{"id": ROOT_ID, "body": build_root_body("엔비디아 최신 사업 리스크", "req-1")}]
        workflow = load_workflow(ROOT_ID, fetch=_fetch_from(_board()))

        with patch.object(ceo, "list_ceo_roots", return_value=rows):
            with patch.object(ceo, "load_workflow", return_value=workflow):
                response = client.get("/ui/ceo/tasks", params={"limit": 20})

        body = response.json()
        self.assertEqual(body["schema_version"], "ceo.task-list.v1")
        self.assertEqual(len(body["items"]), 1)
        item = body["items"][0]
        self.assertEqual(item["task_id"], ROOT_ID)
        self.assertEqual(item["query"], "엔비디아 최신 사업 리스크")
        self.assertEqual(item["status"], "running")
        self.assertEqual(
            item["selected_departments"], ["research-department", "risk-management"]
        )

    def test_limit_is_bounded(self) -> None:
        client = _client()
        self.assertEqual(client.get("/ui/ceo/tasks", params={"limit": 0}).status_code, 422)
        self.assertEqual(client.get("/ui/ceo/tasks", params={"limit": 101}).status_code, 422)


class KanbanCliErrorMappingTest(unittest.TestCase):
    def test_missing_task_message_becomes_not_found(self) -> None:
        completed = type("P", (), {"returncode": 1, "stdout": "no such task: t_x", "stderr": ""})()
        with patch.object(ceo_kanban_read.subprocess, "run", return_value=completed):
            with self.assertRaises(KanbanTaskNotFound):
                ceo_kanban_read.run_kanban(("show", "t_x", "--json"))

    def test_other_failures_become_unavailable(self) -> None:
        completed = type("P", (), {"returncode": 2, "stdout": "", "stderr": "database is locked"})()
        with patch.object(ceo_kanban_read.subprocess, "run", return_value=completed):
            with self.assertRaises(KanbanUnavailable):
                ceo_kanban_read.run_kanban(("list", "--json"))

    def test_missing_cli_becomes_unavailable(self) -> None:
        with patch.object(ceo_kanban_read.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(KanbanUnavailable):
                ceo_kanban_read.run_kanban(("list", "--json"))


if __name__ == "__main__":
    unittest.main()
