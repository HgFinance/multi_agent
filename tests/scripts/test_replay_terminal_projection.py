"""Projection-only terminal replay CLI contract tests."""

from __future__ import annotations

import copy
import unittest
from collections.abc import Mapping, Sequence
from typing import Any

from orchestration.adapters.ceo_notion_projection import NotionProjectionError
from scripts.replay_terminal_projection import (
    ReplayValidationError,
    replay_terminal_projection,
)

ROOT = "t_root"
QA = "t_qa"
SYNTHESIS = "t_synthesis"
PRIMARY = "t_primary"


def _body(role: str, action: str) -> str:
    return "\n".join(
        (
            "hgfinance.ceo-workflow-scope.v1",
            f"workflow_root_task_id={ROOT}",
            f"workflow_role={role}",
            f"hgfinance.ceo-supervisor.v1 action={action}",
        )
    )


class FakeKanban:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {
            ROOT: {
                "id": ROOT,
                "status": "done",
                "body": "## User request\nCompare companies",
            },
            PRIMARY: {
                "id": PRIMARY,
                "status": "done",
                "assignee": "research-department",
                "body": _body("primary", "DELEGATE"),
            },
            QA: {
                "id": QA,
                "status": "done",
                "assignee": "qa-department",
                "body": _body("qa", "RUN_QA"),
                "task_run": {
                    "id": "run-qa-1",
                    "metadata": {
                        "verdict": "CONDITIONAL PASS",
                        "highest_severity": "MEDIUM",
                        "findings": [{"finding_id": "F-QA-001"}],
                        "evaluated_primary_task_ids": [PRIMARY],
                        "worker_session_id": "qa-session-1",
                    },
                },
            },
            SYNTHESIS: {
                "id": SYNTHESIS,
                "status": "done",
                "assignee": "ceo-agent",
                "body": _body("synthesis", "SYNTHESIZE"),
                "task_run": {
                    "id": "run-synthesis-1",
                    "metadata": {
                        "final_answer": "Final answer",
                        "selected_departments": ["research-department"],
                        "workflow_mode": "analysis",
                    },
                },
            },
        }
        self.read_calls = 0
        self.write_calls: list[str] = []

    def show(self, task_id: str) -> dict[str, Any]:
        self.read_calls += 1
        return copy.deepcopy(self.tasks[task_id])

    def workflow(self, task_id: str) -> tuple[str, tuple[dict[str, Any], ...]]:
        self.read_calls += 1
        return ROOT, tuple(
            copy.deepcopy(task) for key, task in self.tasks.items() if key != ROOT
        )

    def comment_task(self, task_id: str, text: str) -> None:
        self.write_calls.append(f"comment:{task_id}:{text}")
        self.tasks[task_id].setdefault("comments", []).append({"body": text})

    def create_task(self, *_args: Any, **_kwargs: Any) -> None:
        self.write_calls.append("create_task")
        raise AssertionError("replay must not create a Kanban task")

    def work(self, *_args: Any, **_kwargs: Any) -> None:
        self.write_calls.append("work")
        raise AssertionError("replay must not execute a Kanban task")


class FakeAuditRepository:
    def __init__(self) -> None:
        self.records: dict[str, Any] = {}

    def persist_kanban_qa(self, record: Any) -> dict[str, Any]:
        if record.eval_run_id in self.records:
            return {"duplicate": True, "eval_run_id": record.eval_run_id}
        self.records[record.eval_run_id] = record
        return {"duplicate": False, "eval_run_id": record.eval_run_id}


class FakeNotionTransport:
    def __init__(self) -> None:
        self.pages: list[dict[str, Any]] = []

    def database_schema(self, _database_id: str) -> dict[str, Any]:
        return {
            "properties": {
                "제목": {"type": "title", "title": {}},
                "projection_key": {"type": "rich_text", "rich_text": {}},
            }
        }

    def query_projection(self, _database_id: str, projection_key: str) -> Sequence[Mapping[str, Any]]:
        return [page for page in self.pages if page["projection_key"] == projection_key]

    def create_page(
        self,
        _database_id: str,
        properties: Mapping[str, Any],
        _children: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        key = properties["projection_key"]["rich_text"][0]["text"]["content"]
        page = {"id": f"page-{len(self.pages) + 1}", "projection_key": key}
        self.pages.append(page)
        return page


class LegacyNotionTransport(FakeNotionTransport):
    """Simulate the production DB without a projection_key property."""

    def database_schema(self, _database_id: str) -> dict[str, Any]:
        names = (
            "브리핑명", "기준일", "상태", "구분", "전체 업무", "완료", "진행 중",
            "승인 대기", "차단·오류", "대표 결정사항", "핵심 성과", "문제·위험",
            "다음 우선순위",
        )
        properties: dict[str, Any] = {
            "브리핑명": {"type": "title", "title": {}},
            "기준일": {"type": "date", "date": {}},
            "상태": {"type": "select", "select": {"options": [{"name": "완료"}]}},
            "구분": {"type": "select", "select": {"options": [{"name": "CEO"}]}},
        }
        for name in names[4:9]:
            properties[name] = {"type": "number", "number": {}}
        for name in names[9:]:
            properties[name] = {"type": "rich_text", "rich_text": {}}
        return {"properties": properties}

    def query_projection(
        self, _database_id: str, _projection_key: str
    ) -> Sequence[Mapping[str, Any]]:
        raise NotionProjectionError("unknown property", status=400)

    def create_page(
        self,
        _database_id: str,
        properties: Mapping[str, Any],
        _children: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        title = properties.get("브리핑명") or properties.get("제목") or properties.get("title")
        page = {"id": f"page-{len(self.pages) + 1}", "title": title, "properties": properties}
        self.pages.append(page)
        return page


class FailingNotionTransport(FakeNotionTransport):
    def create_page(
        self,
        _database_id: str,
        _properties: Mapping[str, Any],
        _children: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        raise NotionProjectionError("validation error", status=400)


class ReplayTerminalProjectionTests(unittest.TestCase):
    def test_qa_replay_uses_existing_metadata_and_is_idempotent(self) -> None:
        client = FakeKanban()
        repository = FakeAuditRepository()

        first = replay_terminal_projection(
            projection_type="qa",
            root_task_id=ROOT,
            task_id_value=QA,
            client=client,
            qa_repository=repository,
            env={},
        )
        second = replay_terminal_projection(
            projection_type="qa",
            root_task_id=ROOT,
            task_id_value=QA,
            client=client,
            qa_repository=repository,
            env={},
        )

        self.assertEqual(first["detected_verdict"], "CONDITIONAL PASS")
        self.assertEqual(first["eval_run_id"], second["eval_run_id"])
        self.assertEqual(first["projection"]["canonical_decision"], "WARN")
        self.assertEqual(second["projection"]["status"], "duplicate")
        self.assertEqual(len(repository.records), 1)
        self.assertEqual(len(client.write_calls), 1)

    def test_notion_replay_is_idempotent_via_projection_key(self) -> None:
        client = FakeKanban()
        transport = FakeNotionTransport()

        first = replay_terminal_projection(
            projection_type="notion",
            root_task_id=ROOT,
            task_id_value=SYNTHESIS,
            client=client,
            notion_transport=transport,
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "db"},
        )
        second = replay_terminal_projection(
            projection_type="notion",
            root_task_id=ROOT,
            task_id_value=SYNTHESIS,
            client=client,
            notion_transport=transport,
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "db"},
        )

        self.assertEqual(first["projection"]["status"], "created")
        self.assertEqual(second["projection"]["status"], "duplicate")
        self.assertEqual(first["projection_key"], "ceo-synthesis:t_root:t_synthesis")
        self.assertEqual(len(transport.pages), 1)
        self.assertEqual(len(client.write_calls), 1)

    def test_dry_run_has_no_repository_notion_or_kanban_write(self) -> None:
        client = FakeKanban()
        result = replay_terminal_projection(
            projection_type="qa",
            root_task_id=ROOT,
            task_id_value=QA,
            client=client,
            dry_run=True,
            env={},
        )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["projection"]["status"], "persisted")
        self.assertEqual(client.write_calls, [])

    def test_notion_replay_uses_kanban_marker_when_property_is_missing(self) -> None:
        client = FakeKanban()
        transport = LegacyNotionTransport()

        first = replay_terminal_projection(
            projection_type="notion",
            root_task_id=ROOT,
            task_id_value=SYNTHESIS,
            client=client,
            notion_transport=transport,
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "db"},
        )
        second = replay_terminal_projection(
            projection_type="notion",
            root_task_id=ROOT,
            task_id_value=SYNTHESIS,
            client=client,
            notion_transport=transport,
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "db"},
        )

        self.assertEqual(first["projection"]["status"], "created")
        self.assertEqual(second["projection"]["status"], "duplicate")
        self.assertEqual(len(transport.pages), 1)
        self.assertEqual(len(client.write_calls), 1)

    def test_notion_validation_failure_is_structured_and_not_retried(self) -> None:
        client = FakeKanban()
        transport = FailingNotionTransport()
        result = replay_terminal_projection(
            projection_type="notion",
            root_task_id=ROOT,
            task_id_value=SYNTHESIS,
            client=client,
            notion_transport=transport,
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "db"},
        )
        self.assertEqual(result["projection"]["status"], "failed")
        self.assertFalse(result["projection"]["retryable"])
        self.assertEqual(len(transport.pages), 0)
        self.assertEqual(client.write_calls, [])

    def test_rejects_non_terminal_wrong_root_and_wrong_type(self) -> None:
        client = FakeKanban()
        client.tasks[QA]["status"] = "running"
        with self.assertRaises(ReplayValidationError):
            replay_terminal_projection(
                projection_type="qa",
                root_task_id=ROOT,
                task_id_value=QA,
                client=client,
                qa_repository=FakeAuditRepository(),
                env={},
            )

        client.tasks[QA]["status"] = "done"
        with self.assertRaises(ReplayValidationError):
            replay_terminal_projection(
                projection_type="qa",
                root_task_id="other-root",
                task_id_value=QA,
                client=client,
                qa_repository=FakeAuditRepository(),
                env={},
            )
        with self.assertRaises(ReplayValidationError):
            replay_terminal_projection(
                projection_type="notion",
                root_task_id=ROOT,
                task_id_value=QA,
                client=client,
                notion_transport=FakeNotionTransport(),
                env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "db"},
            )


    def test_production_action_marker_is_exact_and_not_substring_matching(self) -> None:
        from orchestration.adapters.terminal_projection_utils import action

        self.assertEqual(
            action({"body": "hgfinance.ceo-supervisor.v1 action=RUN_QA"}),
            "RUN_QA",
        )
        for body in (
            "please do action=RUN_QA someday",
            "RUN_QA",
            "foo hgfinance.ceo-supervisor.v1 action=RUN_QA garbage",
            "hgfinance.ceo-supervisor.v1 maybe action=RUN_QA",
        ):
            self.assertIsNone(action({"body": body}))

    def test_rejects_role_action_and_root_mismatches(self) -> None:
        from scripts.replay_terminal_projection import _validate_projection_type

        with self.assertRaises(ReplayValidationError):
            _validate_projection_type(
                "qa",
                {"body": _body("primary", "RUN_QA"), "assignee": "qa-department"},
            )
        with self.assertRaises(ReplayValidationError):
            _validate_projection_type(
                "qa",
                {
                    "body": "\n".join(
                        (
                            f"workflow_root_task_id={ROOT}",
                            "workflow_role=qa",
                        )
                    ),
                    "assignee": "qa-department",
                },
            )

    def test_kanban_db_option_is_forwarded_to_hermes_environment(self) -> None:
        from scripts.replay_terminal_projection import _effective_environment

        environment = _effective_environment(
            {"HERMES_KANBAN_DB": "/wrong/default.db"},
            "/opt/data/shared-kanban/kanban.db",
        )
        self.assertEqual(
            environment["HERMES_KANBAN_DB"],
            "/opt/data/shared-kanban/kanban.db",
        )


if __name__ == "__main__":
    unittest.main()
