"""Production-shaped regression tests for CEO workflow read projections."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from apps.api import ceo, ceo_kanban_read


ROOT = "t_root"
RESEARCH = "t_research"
RISK = "t_risk"
QA = "t_qa"
SYNTHESIS = "t_synthesis"


def _payload(
    task_id: str,
    *,
    assignee: str,
    status: str,
    role: str | None = None,
    parents: tuple[str, ...] = (),
    children: tuple[str, ...] = (),
    metadata: dict[str, object] | None = None,
    latest_summary: str = "",
) -> dict[str, object]:
    body = ""
    if task_id == ROOT:
        body = (
            "hgfinance.ceo-workflow-scope.v1\n"
            "workflow_root_task_id=t_root\n"
            "workflow_role=scope_and_planning\n"
            "workflow_mode=analysis"
        )
    elif role:
        body = (
            "hgfinance.ceo-workflow-scope.v1\n"
            f"workflow_root_task_id={ROOT}\n"
            f"workflow_role={role}"
        )
    result: dict[str, object] = {
        "id": task_id,
        "title": task_id,
        "body": body,
        "assignee": assignee,
        "status": status,
        "parents": list(parents),
        "children": list(children),
        "created_at": 1786590605,
        "latest_summary": latest_summary,
        "runs": [],
    }
    if metadata is not None:
        result["metadata"] = metadata
    return result


def _production_board(*, qa_status: str = "done", synthesis_status: str = "running") -> dict[str, dict[str, object]]:
    final_metadata = {
        "primary_tasks": [RESEARCH, RISK],
        "selected_departments": ["research-department", "risk-management"],
        "qa_required": True,
        "qa_task_id": QA,
        "synthesis_task_ids": [SYNTHESIS],
    }
    board = {
        ROOT: _payload(
            ROOT,
            assignee="ceo-agent",
            status="running",
            metadata={
                "selected_departments": [],
                "qa_required": False,
                "workflow_metadata": final_metadata,
            },
            latest_summary=(
                "두 primary 결과가 준비되면 CEO가 즉시 종합하고, "
                "독립 QA는 별도 비동기 evaluation 경로에서 실행한다."
            ),
        ),
        RESEARCH: _payload(
            RESEARCH,
            assignee="research-department",
            status="done",
            role="primary",
            latest_summary="research complete",
        ),
        RISK: _payload(
            RISK,
            assignee="risk-management",
            status="done",
            role="primary",
            latest_summary="risk complete",
        ),
        QA: _payload(
            QA,
            assignee="qa-department",
            status=qa_status,
            role="qa",
            parents=(RESEARCH, RISK),
            latest_summary="QA evaluation",
        ),
        SYNTHESIS: _payload(
            SYNTHESIS,
            assignee="ceo-agent",
            status=synthesis_status,
            role="synthesis",
            parents=(RESEARCH, RISK),
            latest_summary="CEO synthesis",
        ),
    }
    board[ROOT]["runs"] = [{"metadata": json.dumps(final_metadata)}]
    return board


class CeoWorkflowProjectionRegressionTest(unittest.TestCase):
    def test_supplied_board_snapshot_ignores_purged_stale_edge(self) -> None:
        board = _production_board()
        board[ROOT]["children"] = ["t_already_purged", RESEARCH]
        fetched: list[str] = []

        def fetch(task_id: str) -> dict[str, object]:
            fetched.append(task_id)
            return board[task_id]

        workflow = ceo_kanban_read.load_workflow(
            ROOT,
            fetch=fetch,
            listed_rows=list(board.values()),
        )

        self.assertIn(RESEARCH, {node.task_id for node in workflow.nodes})
        self.assertNotIn("t_already_purged", fetched)

    def test_marker_synthesis_recovers_unmarked_legacy_parent(self) -> None:
        board = _production_board()
        board[RESEARCH]["body"] = "legacy primary without workflow marker"
        board[SYNTHESIS]["parents"] = [RESEARCH]
        board[ROOT]["metadata"] = {}
        board[ROOT]["runs"] = []

        def fetch(task_id: str) -> dict[str, object]:
            return board[task_id]

        workflow = ceo_kanban_read.load_workflow(
            ROOT,
            fetch=fetch,
            listed_rows=list(board.values()),
        )

        self.assertIn(RESEARCH, {node.task_id for node in workflow.nodes})

    def test_parentless_primary_and_async_lanes_are_discovered_by_marker(self) -> None:
        board = _production_board(qa_status="running", synthesis_status="running")

        def fetch(task_id: str) -> dict[str, object]:
            return board[task_id]

        with patch.object(ceo_kanban_read, "list_tasks", return_value=list(board.values())):
            workflow = ceo_kanban_read.load_workflow(ROOT, fetch=fetch)

        self.assertEqual(workflow.selected_departments, ("research-department", "risk-management"))
        self.assertTrue(workflow.qa_required)
        self.assertEqual([node.task_id for node in workflow.primary_nodes], [RESEARCH, RISK])
        self.assertEqual([node.parents for node in workflow.primary_nodes], [(), ()])
        self.assertEqual([node.task_id for node in workflow.qa_nodes], [QA])
        self.assertEqual(workflow.synthesis_node.task_id, SYNTHESIS)
        self.assertNotIn((ROOT, RESEARCH), workflow.edges)
        self.assertNotIn((ROOT, RISK), workflow.edges)
        self.assertIn((RESEARCH, QA), workflow.edges)
        self.assertIn((RESEARCH, SYNTHESIS), workflow.edges)

    def test_status_graph_and_list_projections_share_membership_and_progress(self) -> None:
        board = _production_board(qa_status="done", synthesis_status="running")

        def fetch(task_id: str) -> dict[str, object]:
            return board[task_id]

        with patch.object(ceo_kanban_read, "list_tasks", return_value=list(board.values())):
            workflow = ceo_kanban_read.load_workflow(ROOT, fetch=fetch)

        status = ceo._status_payload(workflow)
        self.assertEqual(status["workflow"]["selected_departments"], ["research-department", "risk-management"])
        self.assertEqual(status["progress"]["primary_total"], 2)
        self.assertEqual(status["progress"]["primary_done"], 2)
        self.assertEqual(status["progress"]["qa"], "done")
        self.assertEqual(status["progress"]["synthesis"], "running")

        with (
            patch.object(ceo, "_load", return_value=workflow),
            patch.object(ceo, "list_ceo_roots", return_value=[board[ROOT]]),
        ):
            graph = ceo.ceo_task_graph(ROOT)
            task_list = ceo.ceo_task_list(limit=20, include_archived=False, owner_id=None)
        self.assertEqual({node.id for node in graph.nodes}, {ROOT, RESEARCH, RISK, QA, SYNTHESIS})
        self.assertEqual(task_list.items[0].selected_departments, ["research-department", "risk-management"])

    def test_completed_planning_metadata_overrides_ingress_placeholder_and_summary(self) -> None:
        board = _production_board()
        root = board[ROOT]

        with patch.object(ceo.hermes_boundary, "list_kanban_tasks", return_value=list(board.values())):
            projection = ceo._scoped_planning_projection(root, timeout=0.1)
        acknowledgement = ceo._planning_acknowledgement(projection)

        self.assertEqual(
            acknowledgement["planning"]["selected_departments"],
            ["research-department", "risk-management"],
        )
        self.assertTrue(acknowledgement["planning"]["qa_required"])
        self.assertIn("준비되는 대로", acknowledgement["planning"]["summary"])
        self.assertNotIn("QA", acknowledgement["planning"]["summary"])
        self.assertNotIn("독립 QA를 거친 뒤", acknowledgement["planning"]["summary"])
        self.assertEqual(
            acknowledgement["planning"]["steps"][-2:],
            ["CEO Synthesis", "QA (async evaluation)"],
        )

    def test_parentless_primary_resolves_to_marker_root(self) -> None:
        board = _production_board()

        with patch.object(ceo_kanban_read, "list_tasks", return_value=list(board.values())):
            root_id = ceo_kanban_read.resolve_root_id(RESEARCH, fetch=board.__getitem__)
        self.assertEqual(root_id, ROOT)


if __name__ == "__main__":
    unittest.main()
