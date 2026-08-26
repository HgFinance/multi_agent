import pytest

from orchestration.adapters.department_notion_projection import (
    DepartmentNotionProjection,
    DepartmentNotionProjectionError,
    _NotionTransport,
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
        self.replaced = []

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

    def replace_blocks(self, page_id, children):
        self.replaced.append((page_id, children))


def test_notion_transport_replaces_existing_body_without_recreating_page():
    class RecordingTransport(_NotionTransport):
        def __init__(self):
            super().__init__("token")
            self.calls = []

        def _request(self, method, path, body=None):
            self.calls.append((method, path, body))
            if method == "GET":
                return {
                    "results": [{"id": "old-1"}, {"id": "old-2"}],
                    "has_more": False,
                }
            return {"id": "page-1"}

    transport = RecordingTransport()
    children = [{"object": "block", "type": "paragraph", "paragraph": {}}]

    transport.replace_blocks("page-1", children)

    assert transport.calls == [
        ("GET", "blocks/page-1/children?page_size=100", None),
        ("PATCH", "blocks/page-1/children", {"children": children}),
        ("PATCH", "blocks/old-1", {"archived": True}),
        ("PATCH", "blocks/old-2", {"archived": True}),
    ]


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
    assert len(transport.replaced) == 1
    assert "248250" in str(transport.replaced[0][1])
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


def test_risk_projection_prefers_complete_result_and_human_labels():
    transport = FakeTransport(
        {
            "제목": {"type": "title"},
            "리스크 검토 요약": {"type": "rich_text"},
            "상세 검토 보고서": {"type": "rich_text"},
            "작성 시각": {"type": "date"},
        }
    )
    projection = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x", "NOTION_RISK_DB": "risk-db"},
        transport=transport,
    )
    task = _trading_task()
    task.update(
        {
            "assignee": "risk-management",
            "title": "삼성전자 포지션 리스크 검토",
            "result": (
                "### 종합 위험도\nMODERATE\n\n"
                "`unversioned·snapshot_resolvable=false` Mandate가 없어 "
                "Mandate를 확인하지 못했고 gross 노출과 KOREA_EQUITY, NAV를 확인했습니다.\n"
                "법률 판정: no_breach\n"
                "이번 법률 조회는 PAPER만으로는 no_breach으로 보았지만 추가 확인이 필요합니다.\n"
                "error: null\n"
                'block_reason: "현재 포지션 자료가 없습니다."'
            ),
            "run_metadata": {
                "summary": "짧은 전달용 요약",
                "analysis_mode": "fast_advisory",
                "rating": "MODERATE",
                "portfolio_authoritative": False,
                "order_authorized": False,
                "worker_session_id": "must-not-be-projected",
            },
        }
    )

    result = projection.project(root_task_id="t_root1", task=task)

    assert result.status == "created"
    _, props, children = transport.created[0]
    assert props["제목"]["title"][0]["text"]["content"].startswith(
        "t_trade1 · 삼성전자"
    )
    narrative = str(props["리스크 검토 요약"])
    assert "현재 유효한 투자지침 스냅샷을 확인할 수 없는 상태" in narrative
    assert "총액 기준 노출" in narrative
    assert "국내 주식" in narrative
    assert "투자지침이 없어 투자지침을 확인하지 못했고" in narrative
    assert "순자산 가치" in narrative
    assert "보통" in narrative
    assert "현재 입력만으로 위반을 확인하지 못함" in narrative
    assert "법률 위반 여부를 확정할 수 없으며" in narrative
    assert "판단 보류 사유" in narrative
    assert "snapshot_resolvable" not in narrative
    assert "block_reason" not in narrative
    assert "error: null" not in narrative
    assert "짧은 전달용 요약" not in str(props)
    rendered = str(children)
    assert "리스크 부서 검토 결과" in rendered
    assert "분석 방식" in rendered
    assert "리스크 원본 시스템과 승인된 검증 절차" in rendered
    assert "포트폴리오 권위 데이터" in rendered
    assert "Department Task Result" not in rendered
    assert "Original Instruction" not in rendered
    assert "worker_session_id" not in rendered
    assert "workflow_root_task_id" not in rendered


def test_accounting_projection_uses_accounting_database_and_manager_labels():
    transport = FakeTransport(
        {
            "제목": {"type": "title"},
            "서술": {"type": "rich_text"},
        }
    )
    projection = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x", "NOTION_ACCOUNTING_DB": "accounting-db"},
        transport=transport,
    )
    task = _trading_task()
    task.update(
        {
            "assignee": "accounting-portfolio-department",
            "title": "PAPER 계정 NAV 및 대사 상태 검토",
            "result": "기준 시각: 2026-08-26T07:54:25Z\n공식 NAV 확정 전 Preliminary입니다.",
            "run_metadata": {
                "structured_summary": {
                    "nav": "999997007",
                    "cash": "980047007",
                    "open_breaks": "확인 자료 없음",
                    "paper_boundary": "주문과 원장 변경 없음",
                }
            },
        }
    )

    result = projection.project(root_task_id="t_root1", task=task)

    assert result.status == "created"
    assert transport.created[0][0] == "accounting-db"
    rendered = str(transport.created[0][2])
    assert "회계·포트폴리오 검토 결과" in rendered
    assert "주요 수치와 확인 사항" in rendered
    assert "Terminal Metadata" not in rendered
    assert "workflow_root_task_id" not in rendered


def test_accounting_projection_humanizes_runtime_field_names():
    transport = FakeTransport({"제목": {"type": "title"}})
    projection = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x", "NOTION_ACCOUNTING_DB": "accounting-db"},
        transport=transport,
    )
    task = _trading_task()
    task.update(
        {
            "assignee": "accounting-portfolio-department",
            "run_metadata": {},
            "result": (
                "as_of=2026-08-26; source_of_record=accounting.journals; "
                "quality_status=WARN; instrument_id=abc; snapshot weight=1%"
            ),
        }
    )

    result = projection.project(root_task_id="t_root1", task=task)

    assert result.status == "created"
    rendered = str(transport.created[0][2])
    assert "source_of_record" not in rendered
    assert "quality_status" not in rendered
    assert "instrument_id" not in rendered
    assert "기준 시각" in rendered
    assert "자료 기준" in rendered
    assert "자료 품질 상태" in rendered


def test_qa_projection_is_korean_and_uses_explicit_qa_database():
    transport = FakeTransport(
        {
            "제목": {"type": "title"},
            "판정": {"type": "select", "select": {"options": [{"name": "FAIL"}]}},
            "findings severity": {
                "type": "select",
                "select": {"options": [{"name": "HIGH"}]},
            },
            "findings": {"type": "rich_text"},
            "claim_checks": {"type": "rich_text"},
            "claim_narrative": {"type": "rich_text"},
            "원본 리포트": {"type": "rich_text"},
            "escalate": {"type": "checkbox"},
            "생성 시각": {"type": "date"},
        }
    )
    task = _trading_task()
    task.update(
        {
            "assignee": "qa-department",
            "body": (
                "workflow_root_task_id=t_root1\n"
                "workflow_role=qa\n"
                "action=RUN_QA"
            ),
            "run_metadata": {
                "overall": "FAIL",
                "numerical_posture": "DEFER",
                "highest_severity": "HIGH",
                "findings": [
                    {
                        "severity": "HIGH",
                        "summary": "NAV bridge is unexplained",
                        "owner": "Accounting Engine",
                        "block_condition": "공식 수치 확정 차단",
                    }
                ],
                "checks": [{"check": "nav_bridge", "result": "FAIL"}],
            },
        }
    )

    result = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x", "NOTION_QA_DB": "qa-db"},
        transport=transport,
    ).project(root_task_id="t_root1", task=task)

    assert result.status == "created"
    database_id, props, children = transport.created[0]
    assert database_id == "qa-db"
    rendered = str(children)
    assert "QA 감사 결과" in rendered
    assert "순자산 대사" in rendered
    assert "workflow_root_task_id" not in rendered
    assert props["판정"]["select"]["name"] == "FAIL"
    assert props["escalate"]["checkbox"] is True
