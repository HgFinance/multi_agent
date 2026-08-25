import pytest

from orchestration.adapters.department_notion_projection import (
    DepartmentNotionProjection,
    DepartmentNotionProjectionError,
)


class FakeTransport:
    def __init__(self, schema, existing=()):
        self.schema = schema
        self.existing = list(existing)
        self.created = []
        self.schema_calls = 0
        self.query_calls = 0
        self.create_calls = 0
        self.updated = []
        self.appended = []

    def database_schema(self, database_id):
        self.schema_calls += 1
        return {"properties": self.schema}

    def query_title(self, database_id, title_property, title):
        self.query_calls += 1
        return self.existing

    def create_page(self, database_id, properties, children):
        self.create_calls += 1
        self.created.append((database_id, properties, children))
        return {"id": "page-1"}

    def update_page(self, page_id, properties):
        self.updated.append((page_id, properties))
        return {"id": page_id}

    def append_blocks(self, page_id, children):
        self.appended.append((page_id, children))
        return {"id": page_id}


def _trading_task():
    return {
        "id": "t_trade1",
        "title": "AAPL execution review",
        "assignee": "trading-department",
        "status": "done",
        "body": (
            "workflow_root_task_id=t_root1\n"
            "workflow_role=primary\n"
            "사용자 요청을 검토하라."
        ),
        "run_metadata": {
            "final_answer": "주문은 실행하지 않고 리스크만 검토했습니다.",
            "trade_case_id": "case-77",
        },
        "completed_at": 1787180000,
    }


def test_trading_projection_uses_existing_schema_without_task_id_abuse():
    transport = FakeTransport(
        {
            "제목": {"type": "title"},
            "서술": {"type": "rich_text"},
            "원본 리포트": {"type": "rich_text"},
            "생성 시각": {"type": "date"},
            "trade_case_id": {"type": "rich_text"},
        }
    )
    projection = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x"},
        transport=transport,
    )

    result = projection.project(
        root_task_id="t_root1",
        task=_trading_task(),
    )

    assert result.status == "created"
    assert len(transport.created) == 1

    _, props, children = transport.created[0]

    assert props["제목"]["title"][0]["text"]["content"].startswith("t_trade1 · ")
    assert props["trade_case_id"]["rich_text"][0]["text"]["content"] == "case-77"
    assert "Task ID" not in props
    assert "workflow_root_task_id" not in props
    assert children


def test_duplicate_title_is_idempotent():
    transport = FakeTransport(
        {
            "제목": {"type": "title"},
            "서술": {"type": "rich_text"},
        },
        existing=({"id": "existing-page"},),
    )
    projection = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x"},
        transport=transport,
    )

    result = projection.project(
        root_task_id="t_root1",
        task=_trading_task(),
    )

    assert result.duplicate is True
    assert not transport.created


def test_correction_upserts_existing_department_page():
    transport = FakeTransport(
        {
            "제목": {"type": "title"},
            "서술": {"type": "rich_text"},
            "원본 리포트": {"type": "rich_text"},
        },
        existing=({"id": "existing-page"},),
    )
    projection = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x"}, transport=transport
    )

    result = projection.project(
        root_task_id="t_root1",
        task=_trading_task(),
        event={
            "force_upsert": True,
            "correction": "권위 DB 확인: 삼성전자 1주 248250원 체결",
        },
    )

    assert result.status == "updated"
    assert result.page_id == "existing-page"
    assert "248250" in str(transport.updated[0][1])
    assert not transport.appended
    assert not transport.created


def test_correction_recreates_missing_department_page() -> None:
    transport = FakeTransport(
        {
            "제목": {"type": "title"},
            "서술": {"type": "rich_text"},
        }
    )
    projection = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x"}, transport=transport
    )

    result = projection.project(
        root_task_id="t_root1",
        task=_trading_task(),
        event={"force_upsert": True, "correction": "248250원 체결 정정"},
    )

    assert result.status == "created"
    assert result.page_id == "page-1"
    assert "248250" in str(transport.created[0])


def test_schema_is_reused_by_one_projection_owner():
    transport = FakeTransport({"제목": {"type": "title"}})
    projection = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x"},
        transport=transport,
    )

    first = _trading_task()
    second = _trading_task()
    second["id"] = "t_trade2"
    second["title"] = "another review"

    assert projection.project(root_task_id="t_root1", task=first).status == "created"
    assert projection.project(root_task_id="t_root1", task=second).status == "created"
    assert transport.schema_calls == 1
    assert transport.query_calls == 2
    assert transport.create_calls == 2


def test_cached_schema_mismatch_is_refetched_before_failing_closed():
    transport = FakeTransport({"제목": {"type": "rich_text"}})
    projection = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x"},
        transport=transport,
    )
    first = projection.project(root_task_id="t_root1", task=_trading_task())
    assert first.status == "failed"

    transport.schema = {"제목": {"type": "title"}}
    second = _trading_task()
    second["id"] = "t_trade2"
    result = projection.project(root_task_id="t_root1", task=second)

    assert result.status == "created"
    assert transport.schema_calls == 2
    assert transport.query_calls == 1
    assert transport.create_calls == 1


def test_create_schema_error_invalidates_cache_for_next_retry():
    class Create400Transport(FakeTransport):
        def __init__(self):
            super().__init__({"제목": {"type": "title"}})
            self.fail_once = True

        def create_page(self, database_id, properties, children):
            self.create_calls += 1
            if self.fail_once:
                self.fail_once = False
                raise DepartmentNotionProjectionError("schema changed", status=400)
            self.created.append((database_id, properties, children))
            return {"id": "page-1"}

    transport = Create400Transport()
    projection = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x"},
        transport=transport,
    )

    with pytest.raises(DepartmentNotionProjectionError):
        projection.project(root_task_id="t_root1", task=_trading_task())

    retry = _trading_task()
    retry["id"] = "t_trade2"
    result = projection.project(root_task_id="t_root1", task=retry)

    assert result.status == "created"
    assert transport.schema_calls == 2
    assert transport.query_calls == 2
    assert transport.create_calls == 2


def test_unconfigured_research_projection_is_skipped_without_guessing_db():
    task = _trading_task()
    task["assignee"] = "research-department"

    projection = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x"},
        transport=FakeTransport({}),
    )

    result = projection.project(
        root_task_id="t_root1",
        task=task,
    )

    assert result.status == "skipped"
    assert result.error == "NOTION_RESEARCH_DB missing"


def test_research_projection_uses_explicit_research_database():
    transport = FakeTransport(
        {
            "종목": {"type": "title"},
            "서술": {"type": "rich_text"},
            "생성 시각": {"type": "date"},
        }
    )
    projection = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x", "NOTION_RESEARCH_DB": "research-db"},
        transport=transport,
    )

    task = _trading_task()
    task["assignee"] = "research-department"
    task["title"] = "Samsung evidence review"

    result = projection.project(root_task_id="t_root1", task=task)

    assert result.status == "created"
    database_id, props, _ = transport.created[0]
    assert database_id == "research-db"
    assert props["종목"]["title"][0]["text"]["content"].startswith("t_trade1 · ")


def test_risk_projection_uses_explicit_risk_database():
    transport = FakeTransport(
        {
            "제목": {"type": "title"},
            "서술": {"type": "rich_text"},
        }
    )
    records = []
    projection = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x", "NOTION_RISK_DB": "risk-db"},
        transport=transport,
        projection_recorder=records.append,
    )

    task = _trading_task()
    task["assignee"] = "risk-management"
    task["title"] = "Samsung risk review"
    task["run_metadata"]["position_risk_plan"] = {
        "risk_plan_id": "1dc772a0-1775-4f3b-9434-a6d24897c349",
        "trace_id": "risk-trace-1",
        "state": "VALIDATED",
        "action": "DEFER",
    }

    result = projection.project(root_task_id="t_root1", task=task)

    assert result.status == "created"
    assert transport.created[0][0] == "risk-db"
    assert result.evidence_status == "RECORDED"
    assert records[0]["target"] == "NOTION"
    assert records[0]["delivery_status"] == "DELIVERED"
    assert records[0]["readback_status"] == "NOT_CHECKED"
