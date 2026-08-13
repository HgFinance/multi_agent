"""CEO synthesis/QA terminal projection contracts."""

from __future__ import annotations

import unittest
from typing import Any

from orchestration.adapters.ceo_notion_projection import CeoNotionProjection
from orchestration.adapters.ceo_supervisor import CeoSupervisorService
from orchestration.adapters.qa_audit_projection import QaAuditProjection
from orchestration.ceo_workflow_scope import build_root_body

ROOT = "t_root"
RESEARCH = "t_research"
RISK = "t_risk"
QA = "t_qa"
SYNTHESIS = "t_synthesis"


def _task(
    task_id: str,
    role: str,
    *,
    action: str | None = None,
    assignee: str = "research-department",
    status: str = "done",
    metadata: dict[str, Any] | None = None,
    summary: str = "summary",
) -> dict[str, Any]:
    marker = [
        "hgfinance.ceo-workflow-scope.v1",
        f"workflow_root_task_id={ROOT}",
        f"workflow_role={role}",
    ]
    if action:
        marker.append(f"hgfinance.ceo-supervisor.v1 action={action}")
    return {
        "id": task_id,
        "assignee": assignee,
        "status": status,
        "body": "\n".join(marker),
        "latest_summary": summary,
        "metadata": metadata or {},
        "created_at": 1786590605,
        "completed_at": 1786590816 if status == "done" else None,
    }


class FakeNotionTransport:
    def __init__(self) -> None:
        self.pages: list[dict[str, Any]] = []
        self.fail = False

    def query_projection(self, database_id: str, projection_key: str) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("notion unavailable")
        return [page for page in self.pages if page["projection_key"] == projection_key]

    def create_page(self, database_id: str, properties: dict[str, Any], children: list[dict[str, Any]]) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("notion unavailable")
        key = properties["projection_key"]["rich_text"][0]["text"]["content"]
        self.pages.append({"id": f"page-{len(self.pages) + 1}", "projection_key": key, "properties": properties, "children": children})
        return self.pages[-1]


class FakeAuditRepository:
    def __init__(self) -> None:
        self.records: dict[str, Any] = {}
        self.fail = False

    def persist_kanban_qa(self, record: Any) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("audit unavailable")
        if record.eval_run_id in self.records:
            return {"duplicate": True, "eval_run_id": record.eval_run_id}
        self.records[record.eval_run_id] = record
        return {"duplicate": False, "eval_run_id": record.eval_run_id}


class FakeSupervisorProjection:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def project(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(str(kwargs["task"]["id"]))
        return {"status": "observed"}


class FakeSupervisorClient:
    def __init__(self, root: dict[str, Any], tasks: list[dict[str, Any]]) -> None:
        self.root = root
        self.tasks = tasks
        self.comments: list[dict[str, str]] = []

    def workflow(self, task_id: str) -> tuple[str, tuple[dict[str, Any], ...]]:
        return ROOT, tuple(self.tasks)

    def show(self, task_id: str) -> dict[str, Any]:
        return self.root

    def comment_task(self, task_id: str, text: str) -> None:
        self.comments.append({"task_id": task_id, "body": text})

    def create_task(self, **kwargs: Any) -> dict[str, str]:
        return {"id": "created-task"}


class TerminalProjectionWiringTests(unittest.TestCase):
    def test_supervisor_observes_synthesis_once_without_changing_decision(self) -> None:
        root = {"id": ROOT, "body": build_root_body("q", "req-1"), "status": "done"}
        primary = _task(RESEARCH, "primary", assignee="research-department")
        synthesis = _task(SYNTHESIS, "synthesis", action="SYNTHESIZE", assignee="ceo-agent")
        client = FakeSupervisorClient(root, [primary, synthesis])
        projection = FakeSupervisorProjection()
        service = CeoSupervisorService(client, synthesis_projection=projection)

        first = service.handle_terminal_event({"event_id": "s1", "task_id": SYNTHESIS, "kind": "completed"})
        second = service.handle_terminal_event({"event_id": "s1", "task_id": SYNTHESIS, "kind": "completed"})

        self.assertIsNotNone(first)
        self.assertEqual(first.action.value, "RUN_QA")
        self.assertIsNone(second)
        self.assertEqual(projection.calls, [SYNTHESIS])


class TerminalProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.primary = [
            _task(RESEARCH, "primary", assignee="research-department", summary="research"),
            _task(RISK, "primary", assignee="risk-management", summary="risk"),
        ]
        self.qa = _task(
            QA,
            "qa",
            action="RUN_QA",
            assignee="qa-department",
            metadata={
                "verdict": "CONDITIONAL PASS",
                "highest_severity": "MEDIUM",
                "findings": [{"finding_id": "f1", "severity": "MEDIUM"}],
                "checks": [{"check": "citation", "result": "PASS"}],
                "sources_http": ["https://example.test/source"],
                "artifacts": ["artifact-1"],
                "tests_run": ["arithmetic", "units"],
                "worker_session_id": "qa-session-1",
                "evaluated_primary_task_ids": [RESEARCH, RISK],
            },
        )
        self.synthesis = _task(
            SYNTHESIS,
            "synthesis",
            action="SYNTHESIZE",
            assignee="ceo-agent",
            metadata={
                "original_query": "Compare the selected companies",
                "selected_departments": ["research-department", "risk-management"],
                "workflow_mode": "analysis",
                "final_answer": "Final advisory answer",
            },
        )
        self.workflow = self.primary + [self.qa, self.synthesis]
        self.root = {
            "id": ROOT,
            "body": build_root_body("Compare the selected companies", "req-1"),
            "status": "done",
        }
        self.workflow.insert(0, self.root)

    def test_synthesis_done_is_idempotent_and_excludes_hidden_reasoning(self) -> None:
        transport = FakeNotionTransport()
        env = {"NOTION_TOKEN": "token", "NOTION_CEO_DB": "ceo-db"}
        first = CeoNotionProjection(env=env, transport=transport).project(
            root_task_id=ROOT, task=self.synthesis, workflow_tasks=self.workflow
        )
        second = CeoNotionProjection(env=env, transport=transport).project(
            root_task_id=ROOT, task=self.synthesis, workflow_tasks=self.workflow
        )
        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(transport.pages), 1)
        payload = transport.pages[0]
        self.assertNotIn("reasoning", str(payload).casefold())
        self.assertNotIn("chain_of_thought", str(payload).casefold())
        original_query = payload["properties"]["original_query"]["rich_text"][0]["text"]["content"]
        self.assertEqual(original_query, "Compare the selected companies")

    def test_notion_failure_is_non_binding(self) -> None:
        transport = FakeNotionTransport()
        transport.fail = True
        result = CeoNotionProjection(
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "ceo-db"},
            transport=transport,
        ).project(root_task_id=ROOT, task=self.synthesis, workflow_tasks=self.workflow)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["retryable"])
        self.assertEqual(self.synthesis["status"], "done")

    def test_primary_and_qa_done_do_not_create_notion_page(self) -> None:
        transport = FakeNotionTransport()
        projection = CeoNotionProjection(
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "ceo-db"},
            transport=transport,
        )
        self.assertEqual(projection.project(root_task_id=ROOT, task=self.primary[0], workflow_tasks=self.workflow)["status"], "skipped")
        self.assertEqual(projection.project(root_task_id=ROOT, task=self.qa, workflow_tasks=self.workflow)["status"], "skipped")
        self.assertEqual(transport.pages, [])

    def test_qa_persists_lossless_verdict_and_primary_ids_once(self) -> None:
        repository = FakeAuditRepository()
        projection = QaAuditProjection(repository=repository)
        first = projection.project(root_task_id=ROOT, task=self.qa, workflow_tasks=self.workflow)
        second = QaAuditProjection(repository=repository).project(root_task_id=ROOT, task=self.qa, workflow_tasks=self.workflow)
        self.assertEqual(first["status"], "persisted")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(repository.records), 1)
        record = next(iter(repository.records.values()))
        self.assertEqual(record.original_verdict, "CONDITIONAL PASS")
        self.assertEqual(record.canonical_decision, "WARN")
        self.assertEqual(record.evaluated_primary_task_ids, (RESEARCH, RISK))
        self.assertEqual(record.findings[0]["finding_id"], "f1")

    def test_qa_persistence_failure_is_not_pass(self) -> None:
        repository = FakeAuditRepository()
        repository.fail = True
        result = QaAuditProjection(repository=repository).project(
            root_task_id=ROOT, task=self.qa, workflow_tasks=self.workflow
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["original_verdict"], "CONDITIONAL PASS")
        self.assertNotEqual(result["canonical_decision"], "PASS")

    def test_non_qa_terminal_task_does_not_persist_audit(self) -> None:
        repository = FakeAuditRepository()
        result = QaAuditProjection(repository=repository).project(
            root_task_id=ROOT, task=self.synthesis, workflow_tasks=self.workflow
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(repository.records, {})

    def test_qa_projection_failure_does_not_block_fast_synthesis(self) -> None:
        class FailingQaProjection:
            def project(self, **kwargs: Any) -> None:
                raise RuntimeError("audit persistence unavailable")

        client = FakeSupervisorClient(
            self.root,
            [self.primary[0], self.primary[1], self.qa],
        )
        service = CeoSupervisorService(client, qa_projection=FailingQaProjection())

        decision = service.handle_terminal_event(
            {"event_id": "qa-persistence-failure", "task_id": QA, "kind": "completed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action.value, "SYNTHESIZE")


if __name__ == "__main__":
    unittest.main()
