"""CEO synthesis/QA terminal projection contracts."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from orchestration.adapters.ceo_notion_projection import (
    CeoNotionProjection,
    NotionProjectionError,
)
from orchestration.adapters.ceo_supervisor import (
    CeoSupervisorService,
    ChildTaskState,
    _augment_risk_legal_answer,
    _handoff_provenance,
)
from orchestration.adapters.qa_audit_projection import QaAuditProjection
from orchestration.adapters.terminal_projection_utils import strip_internal_handoff
from orchestration.ceo_workflow_scope import build_root_body

ROOT = "t_root"
RESEARCH = "t_research"
RISK = "t_risk"
QA = "t_qa"
SYNTHESIS = "t_synthesis"


def test_strip_internal_handoff_keeps_only_user_ready_answer() -> None:
    value = "결과를 확인했습니다.\n\n[Terminal handoff]\n- mode: fast_advisory"

    assert strip_internal_handoff(value) == "결과를 확인했습니다."


def test_risk_legal_answer_keeps_only_verified_coordinates() -> None:
    payload = _task(
        RISK,
        "primary",
        assignee="risk-management",
        metadata={
            "legal_routing_verification": {
                "source_references": [
                    {
                        "clause": "제172조",
                        "title": "내부자의 단기매매차익 반환",
                        "official_url": "https://www.law.go.kr/DRF/lawService.do",
                    }
                ]
            }
        },
    )

    answer = _augment_risk_legal_answer(
        "법률 검토 참고 대상으로 제172조 페이지가 제시되었습니다.",
        [payload],
    )

    assert "https://www.law.go.kr/DRF/lawService.do" in answer
    assert "확인된 인용 문서" not in answer


def test_risk_legal_answer_removes_unverified_statute_claim() -> None:
    payload = _task(
        RISK,
        "primary",
        assignee="risk-management",
        metadata={"legal_routing_verification": {"source_references": []}},
    )

    answer = _augment_risk_legal_answer(
        "자본시장법 제172조 페이지가 제시되었습니다.",
        [payload],
    )

    assert "자본시장법 제172조" not in answer
    assert "공식 법률 근거 좌표를 확인하지 못했으므로" in answer


def test_risk_legal_answer_accepts_flat_run_metadata() -> None:
    payload = _task(
        RISK,
        "primary",
        assignee="risk-management",
        metadata={
            "legal_wiki_calls": 1,
            "legal_status": "OK_but_ambiguous_escalate",
            "legal_verdict": "ambiguous",
            "legal_pages_visited": ["https://www.law.go.kr/법령/자본시장법"],
            "legal_source_references": [
                {
                    "clause_id": "제172조",
                    "title": "내부자의 단기매매차익 반환",
                    "authority": "금융위원회",
                    "effective_from": "2026-02-03",
                    "origin_url": "https://www.law.go.kr/DRF/lawService.do",
                }
            ],
        },
    )

    answer = _augment_risk_legal_answer(
        "법률 검토 참고 대상으로 제172조 페이지가 제시되었습니다.",
        [payload],
    )

    assert "https://www.law.go.kr/DRF/lawService.do" in answer
    provenance = _handoff_provenance(ChildTaskState.from_hermes(payload))
    legal_evidence = provenance["legal_evidence"]
    assert legal_evidence["status"] == "OK_but_ambiguous_escalate"
    assert legal_evidence["invocation_count"] == 1
    assert legal_evidence["source_references"][0]["clause"] == "제172조"


def test_research_handoff_preserves_source_coordinates_and_limitations() -> None:
    payload = _task(
        RESEARCH,
        "primary",
        metadata={
            "sources": [
                {
                    "title": "공식 실적 발표",
                    "url": "https://example.com/official",
                    "published": "2026-08-27",
                    "accessed": "2026-08-27 18:30 KST",
                    "citation": "abc12345",
                }
            ],
            "limitations": ["기사 전문은 확인하지 못함"],
        },
    )

    provenance = _handoff_provenance(ChildTaskState.from_hermes(payload))

    assert provenance["source_references"][0]["url"] == "https://example.com/official"
    assert provenance["source_references"][0]["citation"] == "abc12345"
    assert provenance["limitations"] == ["기사 전문은 확인하지 못함"]


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


def _background_task(task_id: str = "t_background") -> dict[str, Any]:
    return {
        "id": task_id,
        "assignee": "research-department",
        "status": "done",
        "body": (
            "hgfinance.continuous-research.v1\n"
            "workflow_plane=continuous_research\n"
            "workflow_role=background_research"
        ),
        "latest_summary": "background intelligence",
    }


def _foreign_primary_task(task_id: str = "t_foreign") -> dict[str, Any]:
    return {
        "id": task_id,
        "assignee": "research-department",
        "status": "done",
        "body": "workflow_root_task_id=t_other\nworkflow_role=primary",
        "latest_summary": "foreign request",
    }


class FakeNotionTransport:
    def __init__(self) -> None:
        self.pages: list[dict[str, Any]] = []
        self.fail = False
        self.schema_calls = 0
        self.query_calls = 0
        self.create_calls = 0

    def database_schema(self, _database_id: str) -> dict[str, Any]:
        self.schema_calls += 1
        return {
            "properties": {
                "제목": {"type": "title", "title": {}},
                "projection_key": {"type": "rich_text", "rich_text": {}},
            }
        }

    def query_projection(
        self, database_id: str, projection_key: str
    ) -> list[dict[str, Any]]:
        self.query_calls += 1
        if self.fail:
            raise NotionProjectionError("notion unavailable", status=503)
        return [page for page in self.pages if page["projection_key"] == projection_key]

    def create_page(
        self,
        database_id: str,
        properties: dict[str, Any],
        children: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.create_calls += 1
        if self.fail:
            raise NotionProjectionError("notion unavailable", status=503)
        key = properties["projection_key"]["rich_text"][0]["text"]["content"]
        self.pages.append(
            {
                "id": f"page-{len(self.pages) + 1}",
                "projection_key": key,
                "properties": properties,
                "children": children,
            }
        )
        return self.pages[-1]

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        page = next(page for page in self.pages if page["id"] == page_id)
        page["properties"] = properties
        return page

    def append_blocks(
        self, page_id: str, children: list[dict[str, Any]]
    ) -> dict[str, Any]:
        page = next(page for page in self.pages if page["id"] == page_id)
        page["children"].extend(children)
        return page


class ProductionReportNotionTransport:
    """실제 CEO report DB의 13개 property schema fixture."""

    def __init__(self) -> None:
        self.pages: list[dict[str, Any]] = []

    def database_schema(self, _database_id: str) -> dict[str, Any]:
        names = (
            "브리핑명",
            "기준일",
            "상태",
            "구분",
            "전체 업무",
            "완료",
            "진행 중",
            "승인 대기",
            "차단·오류",
            "대표 결정사항",
            "핵심 성과",
            "문제·위험",
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

    def create_page(
        self,
        _database_id: str,
        properties: dict[str, Any],
        children: list[dict[str, Any]],
    ) -> dict[str, Any]:
        page = {
            "id": f"page-{len(self.pages) + 1}",
            "properties": properties,
            "children": children,
        }
        self.pages.append(page)
        return page

    def query_title(
        self, _database_id: str, _property_name: str, title: str
    ) -> list[dict[str, Any]]:
        return [
            page
            for page in self.pages
            if page["properties"]["브리핑명"]["title"][0]["text"]["content"] == title
        ]

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        page = next(page for page in self.pages if page["id"] == page_id)
        page["properties"] = properties
        return page

    def append_blocks(
        self, page_id: str, children: list[dict[str, Any]]
    ) -> dict[str, Any]:
        page = next(page for page in self.pages if page["id"] == page_id)
        page["children"].extend(children)
        return page


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
        synthesis = _task(
            SYNTHESIS, "synthesis", action="SYNTHESIZE", assignee="ceo-agent"
        )
        client = FakeSupervisorClient(root, [primary, synthesis])
        projection = FakeSupervisorProjection()
        service = CeoSupervisorService(client, synthesis_projection=projection)

        first = service.handle_terminal_event(
            {"event_id": "s1", "task_id": SYNTHESIS, "kind": "completed"}
        )
        second = service.handle_terminal_event(
            {"event_id": "s1", "task_id": SYNTHESIS, "kind": "completed"}
        )

        # QA is now an idempotent post-response observer; a completed
        # synthesis must not return a second supervisor RUN_QA decision that
        # could create a duplicate audit task.
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(projection.calls, [SYNTHESIS])

    def test_completed_qa_refreshes_the_existing_ceo_report(self) -> None:
        root = {"id": ROOT, "body": build_root_body("q", "req-1"), "status": "done"}
        primary = _task(RESEARCH, "primary", assignee="research-department")
        qa = _task(QA, "qa", action="RUN_QA", assignee="qa-department")
        synthesis = _task(
            SYNTHESIS, "synthesis", action="SYNTHESIZE", assignee="ceo-agent"
        )
        client = FakeSupervisorClient(root, [primary, qa, synthesis])
        qa_projection = FakeSupervisorProjection()
        ceo_projection = FakeSupervisorProjection()
        service = CeoSupervisorService(
            client,
            qa_projection=qa_projection,
            synthesis_projection=ceo_projection,
        )

        service.handle_terminal_event(
            {"event_id": "qa-refresh", "task_id": QA, "kind": "completed"}
        )

        self.assertEqual(qa_projection.calls, [QA])
        self.assertEqual(ceo_projection.calls, [SYNTHESIS])


class TerminalProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.primary = [
            _task(
                RESEARCH, "primary", assignee="research-department", summary="research"
            ),
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
        original_query = payload["properties"]["original_query"]["rich_text"][0][
            "text"
        ]["content"]
        self.assertEqual(original_query, "Compare the selected companies")

    def test_synthesis_correction_updates_existing_page(self) -> None:
        transport = FakeNotionTransport()
        projection = CeoNotionProjection(
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "ceo-db"},
            transport=transport,
        )
        projection.project(
            root_task_id=ROOT,
            task=self.synthesis,
            workflow_tasks=self.workflow,
        )

        corrected = projection.project(
            root_task_id=ROOT,
            task=self.synthesis,
            workflow_tasks=self.workflow,
            event={
                "force_upsert": True,
                "correction": "권위 DB 확인: 248250원 체결, 회계 반영 대기",
            },
        )

        self.assertEqual(corrected["status"], "updated")
        self.assertEqual(len(transport.pages), 1)
        self.assertIn("248250", str(transport.pages[0]["children"]))

    def test_single_delegated_primary_projects_started_card(self) -> None:
        root = dict(self.root)
        root["body"] += "\nselected_primary_profiles=research-department\n"
        child = dict(self.primary[0])
        child["status"] = "running"

        class Delivery:
            def __init__(self) -> None:
                self.cards: list[dict[str, Any]] = []

            def upsert_thread_card(self, **kwargs: Any) -> str:
                self.cards.append(kwargs)
                return "created"

        delivery = Delivery()
        service = CeoSupervisorService(
            FakeSupervisorClient(root, [child]),
            discord_delivery=delivery,
        )

        status = service._deliver_department_progress(
            root_task_id=ROOT,
            root_payload=root,
            task_payload=child,
            event={"task_id": RESEARCH, "kind": "started"},
        )

        self.assertEqual(status, "created")
        self.assertEqual(len(delivery.cards), 1)
        self.assertIn("⏳ 분석 중입니다...", delivery.cards[0]["content"])

    def test_ceo_schema_is_reused_but_projection_query_remains_idempotent(self) -> None:
        transport = FakeNotionTransport()
        projection = CeoNotionProjection(
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "ceo-db"},
            transport=transport,
        )

        first = projection.project(
            root_task_id=ROOT,
            task=self.synthesis,
            workflow_tasks=self.workflow,
        )
        second = projection.project(
            root_task_id=ROOT,
            task=self.synthesis,
            workflow_tasks=self.workflow,
        )

        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(transport.schema_calls, 1)
        self.assertEqual(transport.query_calls, 2)
        self.assertEqual(transport.create_calls, 1)

    def test_notion_failure_is_non_binding(self) -> None:
        transport = FakeNotionTransport()
        transport.fail = True
        projection = CeoNotionProjection(
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "ceo-db"},
            transport=transport,
        )
        result = projection.project(
            root_task_id=ROOT,
            task=self.synthesis,
            workflow_tasks=self.workflow,
        )
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["retryable"])
        self.assertEqual(self.synthesis["status"], "done")

        transport.fail = False
        retry = projection.project(
            root_task_id=ROOT,
            task=self.synthesis,
            workflow_tasks=self.workflow,
        )
        self.assertEqual(retry["status"], "created")
        self.assertEqual(transport.schema_calls, 1)
        self.assertEqual(transport.query_calls, 2)
        self.assertEqual(transport.create_calls, 1)

    def test_production_ceo_report_schema_maps_properties_and_marker(self) -> None:
        transport = ProductionReportNotionTransport()
        client = type(
            "Kanban",
            (),
            {
                "comments": [],
                "comment_task": lambda self, task_id, text: self.comments.append(
                    (task_id, text)
                ),
            },
        )()
        projection = CeoNotionProjection(
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "ceo-db"},
            transport=transport,
            kanban_client=client,
        )
        result = projection.project(
            root_task_id=ROOT, task=self.synthesis, workflow_tasks=self.workflow
        )
        self.assertEqual(result["status"], "created")
        properties = transport.pages[0]["properties"]
        self.assertEqual(
            set(properties),
            {
                "브리핑명",
                "기준일",
                "상태",
                "구분",
                "전체 업무",
                "완료",
                "진행 중",
                "승인 대기",
                "차단·오류",
                "대표 결정사항",
                "핵심 성과",
                "문제·위험",
                "다음 우선순위",
            },
        )
        self.assertEqual(properties["상태"]["select"]["name"], "완료")
        self.assertEqual(properties["구분"]["select"]["name"], "CEO")
        self.assertEqual(properties["전체 업무"]["number"], len(self.workflow))
        title = properties["브리핑명"]["title"][0]["text"]["content"]
        self.assertTrue(title.startswith("CEO 종합 보고 · "))
        report = str(transport.pages[0]["children"])
        self.assertIn("리서치 부서", report)
        self.assertIn("리스크 부서", report)
        self.assertNotIn("Root task", report)
        self.assertNotIn("Selected departments", report)
        self.assertNotIn("Workflow mode", report)
        self.assertIn(
            "projection_key=ceo-synthesis:t_root:t_synthesis", client.comments[0][1]
        )

    def test_production_ceo_correction_recreates_missing_page(self) -> None:
        transport = ProductionReportNotionTransport()
        client = type(
            "Kanban",
            (),
            {
                "comment_task": lambda *_args: None,
                "show": lambda _self, _task_id: self.synthesis,
            },
        )()
        projection = CeoNotionProjection(
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "ceo-db"},
            transport=transport,
            kanban_client=client,
        )

        result = projection.project(
            root_task_id=ROOT,
            task=self.synthesis,
            workflow_tasks=self.workflow,
            event={"force_upsert": True, "correction": "248250원 체결 정정"},
        )

        self.assertEqual(result["status"], "created")
        self.assertEqual(len(transport.pages), 1)
        self.assertIn("248250", str(transport.pages[0]))

    def test_production_ceo_replay_queries_title_when_kanban_marker_is_missing(self) -> None:
        transport = ProductionReportNotionTransport()
        client = type(
            "Kanban",
            (),
            {
                "comments": [],
                "comment_task": lambda self, task_id, text: self.comments.append(
                    (task_id, text)
                ),
            },
        )()
        projection = CeoNotionProjection(
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "ceo-db"},
            transport=transport,
            kanban_client=client,
        )

        first = projection.project(
            root_task_id=ROOT, task=self.synthesis, workflow_tasks=self.workflow
        )
        client.comments.clear()
        replay = projection.project(
            root_task_id=ROOT, task=self.synthesis, workflow_tasks=self.workflow
        )

        self.assertEqual(first["status"], "created")
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(len(transport.pages), 1)

    def test_primary_and_qa_done_do_not_create_notion_page(self) -> None:
        transport = FakeNotionTransport()
        projection = CeoNotionProjection(
            env={"NOTION_TOKEN": "token", "NOTION_CEO_DB": "ceo-db"},
            transport=transport,
        )
        self.assertEqual(
            projection.project(
                root_task_id=ROOT, task=self.primary[0], workflow_tasks=self.workflow
            )["status"],
            "skipped",
        )
        self.assertEqual(
            projection.project(
                root_task_id=ROOT, task=self.qa, workflow_tasks=self.workflow
            )["status"],
            "skipped",
        )
        self.assertEqual(transport.pages, [])

    def test_qa_persists_lossless_verdict_and_primary_ids_once(self) -> None:
        repository = FakeAuditRepository()
        projection = QaAuditProjection(repository=repository)
        first = projection.project(
            root_task_id=ROOT, task=self.qa, workflow_tasks=self.workflow
        )
        second = QaAuditProjection(repository=repository).project(
            root_task_id=ROOT, task=self.qa, workflow_tasks=self.workflow
        )
        self.assertEqual(first["status"], "persisted")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(repository.records), 1)
        record = next(iter(repository.records.values()))
        self.assertEqual(record.original_verdict, "CONDITIONAL PASS")
        self.assertEqual(record.canonical_decision, "WARN")
        self.assertEqual(record.evaluated_primary_task_ids, (RESEARCH, RISK))
        self.assertEqual(record.findings[0]["finding_id"], "f1")
        self.assertEqual(first["checks"], [{"check": "citation", "result": "PASS"}])

    def test_qa_terminal_publishes_correlated_langsmith_metadata(self) -> None:
        repository = FakeAuditRepository()
        client = FakeSupervisorClient(self.root, self.workflow[1:])
        with patch(
            "orchestration.llm_observability.langsmith_enabled", return_value=True
        ), patch(
            "orchestration.llm_observability.publish_metric", return_value=True
        ) as publish:
            result = QaAuditProjection(
                repository=repository,
                kanban_client=client,
                env={"HERMES_HOME": "/nonexistent"},
            ).project(
                root_task_id=ROOT, task=self.qa, workflow_tasks=self.workflow
            )

        self.assertEqual(result["langsmith_status"], "published")
        metric = publish.call_args.args[0]
        self.assertEqual(metric["root_id"], ROOT)
        self.assertEqual(metric["task_id"], QA)
        self.assertEqual(metric["workflow_role"], "qa")
        self.assertEqual(metric["output_verdict"], "CONDITIONAL PASS")
        self.assertEqual(metric["telemetry_completeness"], "terminal-handoff")
        self.assertNotIn("findings", metric)
        self.assertFalse(publish.call_args.kwargs["confirm_delivery"])

    def test_qa_terminal_uses_log_observed_tool_errors(self) -> None:
        repository = FakeAuditRepository()
        client = FakeSupervisorClient(self.root, self.workflow[1:])
        with patch(
            "orchestration.llm_observability.langsmith_enabled", return_value=True
        ), patch(
            "orchestration.llm_observability.publish_metric", return_value=True
        ) as publish, patch(
            "scripts.hermes_worker_observability.worker_log_metrics",
            return_value={
                "llm_calls": 4,
                "tool_calls": 3,
                "tool_error_count": 2,
                "tool_duration_total_ms": 150,
                "tool_latency_available": True,
                "tool_timing_source": "hermes-log-duration",
            },
        ):
            QaAuditProjection(
                repository=repository,
                kanban_client=client,
                env={"HERMES_HOME": "/nonexistent"},
            ).project(root_task_id=ROOT, task=self.qa, workflow_tasks=self.workflow)

        metric = publish.call_args.args[0]
        self.assertEqual(metric["tool_error_count"], 2)
        self.assertEqual(metric["tool_calls"], 3)
        self.assertTrue(metric["tool_latency_available"])
        self.assertEqual(metric["tool_timing_source"], "hermes-log-duration")
        self.assertEqual(metric["telemetry_completeness"], "runtime-and-terminal")

    def test_qa_accepts_worker_overall_as_the_verdict(self) -> None:
        repository = FakeAuditRepository()
        qa = dict(self.qa)
        qa["metadata"] = {"overall": "FAIL", "decision": "DEFER"}

        result = QaAuditProjection(repository=repository).project(
            root_task_id=ROOT, task=qa, workflow_tasks=[*self.primary, qa, self.root]
        )

        self.assertEqual(result["original_verdict"], "FAIL")
        self.assertEqual(result["canonical_decision"], "FAIL")

    def test_qa_accepts_profile_specific_terminal_verdict_field(self) -> None:
        repository = FakeAuditRepository()
        qa = dict(self.qa)
        qa["metadata"] = {"overall_decision": "PASS", "overall_status": "COMPLETED"}
        qa["body"] += json.dumps(
            {
                "root_task_id": ROOT,
                "langsmith_evidence": {
                    "status": "READY",
                    "trace_count": 2,
                    "traces": [
                        {"task_id": RESEARCH, "raw_payloads_sent": False},
                        {"task_id": RISK, "raw_payloads_sent": False},
                    ],
                },
            }
        )

        result = QaAuditProjection(repository=repository).project(
            root_task_id=ROOT, task=qa, workflow_tasks=[*self.primary, qa, self.root]
        )

        self.assertEqual(result["original_verdict"], "PASS")
        self.assertEqual(result["canonical_decision"], "PASS")

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

    def test_qa_projection_excludes_background_and_foreign_primary_tasks(self) -> None:
        background = _background_task()
        foreign = _foreign_primary_task()
        repository = FakeAuditRepository()
        workflow = [
            self.root,
            *self.primary,
            background,
            foreign,
            *[_background_task(f"t_background_{i}") for i in range(100)],
            self.qa,
        ]

        result = QaAuditProjection(repository=repository).project(
            root_task_id=ROOT,
            task=self.qa,
            workflow_tasks=workflow,
        )

        self.assertEqual(result["status"], "persisted")
        record = next(iter(repository.records.values()))
        self.assertEqual(record.evaluated_primary_task_ids, (RESEARCH, RISK))
        self.assertNotIn(background["id"], record.evaluated_primary_task_ids)
        self.assertNotIn(foreign["id"], record.evaluated_primary_task_ids)

    def test_qa_projection_uses_root_selected_primary_profiles(self) -> None:
        selected_root = dict(self.root)
        selected_root["body"] = (
            f"{self.root['body']}\n"
            "selected_primary_profiles=research-department,risk-management"
        )
        accounting = _task(
            "t_accounting",
            "primary",
            assignee="accounting-portfolio-department",
        )
        repository = FakeAuditRepository()
        result = QaAuditProjection(repository=repository).project(
            root_task_id=ROOT,
            task=self.qa,
            workflow_tasks=[selected_root, *self.primary, accounting, self.qa],
        )
        self.assertEqual(result["status"], "persisted")
        record = next(iter(repository.records.values()))
        self.assertEqual(record.evaluated_primary_task_ids, (RESEARCH, RISK))
        self.assertNotIn("t_accounting", record.evaluated_primary_task_ids)

    def test_qa_projection_excludes_selected_primary_until_terminal(self) -> None:
        running = _task(
            "t_running",
            "primary",
            assignee="research-department",
            status="running",
        )
        selected_root = dict(self.root)
        selected_root["body"] = (
            f"{self.root['body']}\n"
            "selected_primary_profiles=research-department,risk-management"
        )
        repository = FakeAuditRepository()
        result = QaAuditProjection(repository=repository).project(
            root_task_id=ROOT,
            task=self.qa,
            workflow_tasks=[selected_root, running, self.primary[1], self.qa],
        )
        self.assertEqual(result["status"], "persisted")
        record = next(iter(repository.records.values()))
        self.assertEqual(record.evaluated_primary_task_ids, (RISK,))

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

    def test_terminal_reconciliation_does_not_repeat_successful_current_card(
        self,
    ) -> None:
        class DeliverySpy:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def upsert_thread_card(self, **kwargs: Any) -> str:
                self.calls.append(str(kwargs["source_task"]["id"]))
                return "created"

        root = dict(self.root)
        root["body"] += (
            "\nselected_primary_profiles=research-department,risk-management"
        )
        client = FakeSupervisorClient(root, [*self.primary])
        delivery = DeliverySpy()
        service = CeoSupervisorService(client, discord_delivery=delivery)

        current = self.primary[0]
        service._deliver_department_progress(
            root_task_id=ROOT,
            root_payload=root,
            task_payload=current,
            event={
                "event_id": "terminal-current",
                "task_id": RESEARCH,
                "kind": "completed",
            },
        )
        service._reconcile_department_terminal_progress(
            root_task_id=ROOT,
            root_payload=root,
            task_payloads=(current, self.primary[1]),
            payloads_are_authoritative=True,
            skip_task_ids=(RESEARCH,),
        )

        self.assertEqual(delivery.calls, [RESEARCH, RISK])

    def test_risk_discord_card_strips_internal_terminal_handoff(self) -> None:
        class DeliverySpy:
            def __init__(self) -> None:
                self.contents: list[str] = []

            def upsert_thread_card(self, **kwargs: Any) -> str:
                self.contents.append(str(kwargs["content"]))
                return "created"

        root = dict(self.root)
        root["body"] += "\nselected_primary_profiles=risk-management"
        risk = dict(self.primary[1])
        risk["result"] = (
            "### 결론\nPAPER 손실은 -400,000원입니다.\n"
            "[Terminal handoff]\n- mode: fast_advisory\n- execution: PROHIBITED"
        )
        client = FakeSupervisorClient(root, [risk])
        delivery = DeliverySpy()
        service = CeoSupervisorService(client, discord_delivery=delivery)

        service._deliver_department_progress(
            root_task_id=ROOT,
            root_payload=root,
            task_payload=risk,
            event={"event_id": "risk-terminal", "task_id": RISK, "kind": "completed"},
        )

        assert delivery.contents
        assert "PAPER 손실은 -400,000원입니다." in delivery.contents[0]
        assert "Terminal handoff" not in delivery.contents[0]
        assert "- mode: fast_advisory" not in delivery.contents[0]

    def test_handler_keeps_failed_current_card_retryable(self) -> None:
        class Client(FakeSupervisorClient):
            def authoritative_workflow_snapshot(self, _root_id: str, _task_id: str):
                return ROOT, tuple(self.tasks), self.root

        class DeliverySpy:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def upsert_thread_card(self, **kwargs: Any) -> str:
                self.calls.append(str(kwargs["source_task"]["id"]))
                return "failed" if len(self.calls) == 1 else "created"

        root = dict(self.root)
        root["body"] += (
            "\nselected_primary_profiles=research-department,risk-management"
        )
        client = Client(root, [*self.primary])
        delivery = DeliverySpy()
        service = CeoSupervisorService(
            client,
            discord_delivery=delivery,
            qa_required=False,
        )

        service.handle_terminal_event(
            {
                "event_id": "terminal-retryable-card",
                "task_id": RESEARCH,
                "kind": "completed",
            }
        )

        self.assertEqual(delivery.calls[:2], [RESEARCH, RESEARCH])


if __name__ == "__main__":
    unittest.main()
