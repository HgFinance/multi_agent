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
        # qa_summary에 "verdict: PASS" 라벨이 명시되면 그대로 PASS
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
        # QA가 라벨 없이 완료되어도 정규식이 못 잡으면 None(판정 불명확).
        # "완료했으니까 자동으로 PASS"는 위험(개발원칙9 - 진입차단).
        self.assertIsNone(workflow.qa_verdict)

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


class LegacyQaAliasReadModelTest(unittest.TestCase):
    """2026-08-12 AWS 실측(t_b2f8506d): CEO-ask 파이프라인 밖의 과거 데이터.

    root assignee가 quant-backtest-department(ceo-agent 아님)이고 body에
    `## User request`가 없다 - `/ui/ceo/ask`로 만들어진 워크플로가 아니라
    다른 파이프라인(삼성전자 E2E 등)이 공유 Kanban 판에 남긴 Task다. 자식은
    `ai-qa-audit-department` - SOUL.md가 명시적으로 금지하는 폐기 별칭이라
    canonical_profiles.py가 신규 생성 시 거부하는 이름이지만, 이미 판에 있는
    과거 데이터는 Read Model이 여전히 정확히 분류해야 한다.
    """

    ROOT = "t_b2f8506d"
    LEGACY_QA = "t_4ebee73f"

    def _board(self) -> dict[str, dict[str, Any]]:
        return {
            self.ROOT: _task(
                self.ROOT,
                assignee="quant-backtest-department",
                status="blocked",
                title="삼성전자 퀀트·밸류에이션 분석",
                body="삼성전자 퀀트·밸류에이션 분석",  # CEO workflow scope 마커 없음
                children=(self.LEGACY_QA,),
            ),
            self.LEGACY_QA: _task(
                self.LEGACY_QA,
                assignee="ai-qa-audit-department",
                status="todo",
                title="삼성전자 투자판단 독립 QA·감사",
                body="삼성전자 투자판단 독립 QA·감사",
                parents=(self.ROOT,),
            ),
        }

    def test_legacy_qa_alias_is_not_counted_as_a_selected_department(self) -> None:
        workflow = load_workflow(self.ROOT, fetch=_fetch_from(self._board()))

        self.assertEqual(workflow.selected_departments, ())
        self.assertNotIn("ai-qa-audit-department", workflow.selected_departments)

    def test_legacy_qa_alias_is_classified_as_qa_role(self) -> None:
        workflow = load_workflow(self.ROOT, fetch=_fetch_from(self._board()))

        self.assertEqual(
            workflow.by_id[self.LEGACY_QA].role(root_task_id=workflow.root_task_id),
            "qa",
        )
        self.assertEqual([node.task_id for node in workflow.qa_nodes], [self.LEGACY_QA])

    def test_non_ceo_ask_root_still_reports_null_query(self) -> None:
        """`/ui/ceo/ask`가 안 만든 Task임을 프론트가 알 수 있는 유일한 신호."""

        workflow = load_workflow(self.ROOT, fetch=_fetch_from(self._board()))

        self.assertIsNone(workflow.query)


class CeoSupervisorUserInputReadModelTest(unittest.TestCase):
    """2026-08-12 AWS 실측(t_b5126836): CEO Planner가 부서를 못 골랐을 때.

    `orchestration/adapters/ceo_supervisor.py`의 `decide_supervisor`가
    `no_analysis_children`으로 만드는 REQUEST_USER_INPUT Task. 이 Task는
    root 하나에만 매달린(parents == {root}) ceo-agent Task라는 점에서
    Synthesis(부모가 root 밖에도 있음)와 위상이 다르다.
    """

    ROOT = "t_b5126836"
    USER_INPUT = "t_87d56aad"

    def _board(self) -> dict[str, dict[str, Any]]:
        return {
            self.ROOT: _task(
                self.ROOT,
                assignee="ceo-agent",
                status="done",
                title="사용자 질의: test",
                body=build_root_body("test", "req-2"),
                children=(self.USER_INPUT,),
            ),
            self.USER_INPUT: _task(
                self.USER_INPUT,
                assignee="ceo-agent",
                status="done",
                title="CEO planner produced no executable child task",
                body=f"{SUPERVISOR_MARKER} action=REQUEST_USER_INPUT no_analysis_children",
                parents=(self.ROOT,),
            ),
        }

    def test_role_is_user_input_not_primary(self) -> None:
        workflow = load_workflow(self.ROOT, fetch=_fetch_from(self._board()))

        self.assertEqual(
            workflow.by_id[self.USER_INPUT].role(root_task_id=workflow.root_task_id),
            "user_input",
        )
        self.assertEqual(workflow.primary_nodes, ())
        self.assertEqual(workflow.selected_departments, ())

    def test_result_has_no_departments_and_no_synthesis(self) -> None:
        workflow = load_workflow(self.ROOT, fetch=_fetch_from(self._board()))

        self.assertIsNone(workflow.synthesis_node)
        self.assertEqual(workflow.department_summaries, {})


class CeoAdHocDecompositionReadModelTest(unittest.TestCase):
    """2026-08-12 AWS 실측 워크플로(t_19d55864)를 그대로 픽스처화한 회귀 테스트.

    CEO 자신의 LLM 턴이 부서 선택과 동시에 QA·Synthesis Task까지 한 번에
    만들어둔 실제 사례. 이 Task들 body에는 `orchestration/adapters/
    ceo_supervisor.py` 데몬이 붙이는 `SUPERVISOR_MARKER`가 없다 - 데몬은 CEO
    턴이 부서를 못 고르거나 재시도가 필요할 때만 개입하기 때문이다. 마커
    문자열이 아니라 그래프 구조(parents)로 역할을 판정해야 하는 근거가 이
    실측 데이터다.
    """

    ROOT = "t_19d55864"
    RESEARCH = "t_dcda69e2"
    RISK = "t_c6041d55"
    QA = "t_2a126814"
    SYNTHESIS = "t_5c286252"

    def _board(self) -> dict[str, dict[str, Any]]:
        return {
            self.ROOT: _task(
                self.ROOT,
                assignee="ceo-agent",
                status="done",
                title="사용자 질의: 엔비디아 최신 사업 리스크만 분석해줘.",
                body=build_root_body("엔비디아 최신 사업 리스크만 분석해줘.", "req-1"),
                children=(self.RESEARCH, self.RISK),
                completed_at=_CREATED_AT + 786,
            ),
            self.RESEARCH: _task(
                self.RESEARCH,
                assignee="research-department",
                status="done",
                title="NVIDIA 최신 사업 리스크 조사",
                body="NVIDIA 최신 사업 리스크 조사 task...",
                parents=(self.ROOT,),
                children=(self.QA,),
                latest_summary="research 요약",
            ),
            self.RISK: _task(
                self.RISK,
                assignee="risk-management",
                status="done",
                title="NVIDIA 리스크 관점 검토",
                body="NVIDIA 리스크 관점 검토 task...",
                parents=(self.ROOT,),
                children=(self.QA,),
                latest_summary="risk 요약",
            ),
            self.QA: _task(
                self.QA,
                assignee="qa-department",
                status="done",
                title="NVIDIA 리스크 분석 독립 QA",
                # 실측 그대로: SUPERVISOR_MARKER 없이 CEO가 직접 지시문을 작성.
                body="독립 QA task. 부모 Research task와 Risk task가 terminal 상태가 "
                "된 뒤 두 산출물을 검토하라. verdict는 PASS/CONDITIONAL/FAIL 중 하나로.",
                parents=(self.RISK, self.RESEARCH),
                children=(self.SYNTHESIS,),
                latest_summary="verdict: PASS",
            ),
            self.SYNTHESIS: _task(
                self.SYNTHESIS,
                assignee="ceo-agent",
                status="done",
                title="CEO NVIDIA 최신 사업 리스크 최종 합성",
                # 실측 그대로: SUPERVISOR_MARKER 없음.
                body="CEO follow-up/synthesis task for user request "
                "'엔비디아 최신 사업 리스크만 분석해줘.' ... decision: DEFER",
                parents=(self.QA, self.RISK, self.RESEARCH),
                latest_summary="엔비디아 최신 사업 리스크 종합 결과. decision: DEFER",
            ),
        }

    def test_synthesis_is_found_without_supervisor_marker(self) -> None:
        workflow = load_workflow(self.ROOT, fetch=_fetch_from(self._board()))

        self.assertIsNotNone(workflow.synthesis_node)
        self.assertEqual(workflow.synthesis_node.task_id, self.SYNTHESIS)
        self.assertEqual(
            workflow.synthesis_node.role(root_task_id=workflow.root_task_id),
            "synthesis",
        )

    def test_qa_is_found_without_supervisor_marker(self) -> None:
        workflow = load_workflow(self.ROOT, fetch=_fetch_from(self._board()))

        self.assertEqual([node.task_id for node in workflow.qa_nodes], [self.QA])

    def test_ceo_synthesis_task_is_excluded_from_selected_departments(self) -> None:
        workflow = load_workflow(self.ROOT, fetch=_fetch_from(self._board()))

        self.assertEqual(
            workflow.selected_departments,
            ("research-department", "risk-management"),
        )
        self.assertNotIn("ceo-agent", workflow.selected_departments)

    def test_progress_counts_only_real_departments(self) -> None:
        workflow = load_workflow(self.ROOT, fetch=_fetch_from(self._board()))

        self.assertEqual(len(workflow.primary_nodes), 2)

    def test_result_resolves_decision_and_summary(self) -> None:
        workflow = load_workflow(self.ROOT, fetch=_fetch_from(self._board()))

        self.assertEqual(workflow.status, "completed")
        self.assertEqual(workflow.decision, "DEFER")
        self.assertIn("종합 결과", workflow.synthesis_node.summary)

    def test_graph_role_matches_topology_not_body_text(self) -> None:
        workflow = load_workflow(self.ROOT, fetch=_fetch_from(self._board()))
        roles = {
            node.task_id: node.role(root_task_id=workflow.root_task_id)
            for node in workflow.nodes
        }
        self.assertEqual(roles[self.ROOT], "root")
        self.assertEqual(roles[self.RESEARCH], "primary")
        self.assertEqual(roles[self.RISK], "primary")
        self.assertEqual(roles[self.QA], "qa")
        self.assertEqual(roles[self.SYNTHESIS], "synthesis")


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

    def test_qa_verdict_requires_explicit_label_not_completed_status(self) -> None:
        """QA가 완료되었어도 'verdict: VALUE' 라벨 형식 없으면 None.

        2026-08-12 AWS 실측: QA 자유 서술에 "verdict는 CONDITIONAL입니다"처럼
        한국어 조사가 붙으면 정규식('verdict[:=]')이 못 잡는다. "완료했으니
        PASS"로 낙관하면 QA의 조건부/불명확 판정을 깨끗한 통과로 둔갑시킨다.
        """

        board = _board(risk_status="done", qa_status="done")
        board[QA_ID]["latest_summary"] = (
            "NVIDIA 독립 QA를 완료했습니다. ... verdict는 CONDITIONAL입니다."
        )
        workflow = load_workflow(ROOT_ID, fetch=_fetch_from(board))

        self.assertIsNone(workflow.qa_verdict)

    def test_qa_verdict_accepts_explicit_label_only(self) -> None:
        """'verdict: VALUE' 또는 'qa_verdict: VALUE' 명시 형식만 인정."""

        board = _board(risk_status="done", qa_status="done")
        board[QA_ID]["latest_summary"] = "... verdict: PASS"
        workflow = load_workflow(ROOT_ID, fetch=_fetch_from(board))

        self.assertEqual(workflow.qa_verdict, "PASS")

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
