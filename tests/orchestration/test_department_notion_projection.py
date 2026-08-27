import pytest

from orchestration.adapters.department_notion_projection import (
    DepartmentNotionProjection,
    DepartmentNotionProjectionError,
    _body_markdown,
    _humanize_risk_result,
    _NotionTransport,
    _task_title,
)
from orchestration.adapters.terminal_projection_utils import (
    qa_projection_checks,
    qa_projection_findings,
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
        ("PATCH", "blocks/old-1", {"in_trash": True}),
        ("PATCH", "blocks/old-2", {"in_trash": True}),
        ("PATCH", "blocks/page-1/children", {"children": children}),
    ]


def test_notion_transport_chunks_page_creation_children_at_api_limit():
    class RecordingTransport(_NotionTransport):
        def __init__(self):
            super().__init__("token")
            self.calls = []

        def _request(self, method, path, body=None):
            self.calls.append((method, path, body))
            if method == "POST":
                return {"id": "page-1"}
            return {"id": "page-1"}

    transport = RecordingTransport()
    children = [
        {"object": "block", "type": "paragraph", "paragraph": {"n": index}}
        for index in range(205)
    ]

    transport.create_page("db-1", {}, children)

    assert len(transport.calls) == 3
    assert len(transport.calls[0][2]["children"]) == 100
    assert [len(call[2]["children"]) for call in transport.calls[1:]] == [100, 5]


def test_notion_transport_append_only_sends_missing_tail_after_readback():
    first = {"object": "block", "type": "paragraph", "paragraph": {"n": 1}}
    second = {"object": "block", "type": "paragraph", "paragraph": {"n": 2}}
    third = {"object": "block", "type": "paragraph", "paragraph": {"n": 3}}

    class RecordingTransport(_NotionTransport):
        def __init__(self):
            super().__init__("token")
            self.calls = []

        def _request(self, method, path, body=None):
            self.calls.append((method, path, body))
            if method == "GET":
                return {"results": [first, second], "has_more": False}
            return {"id": "page-1"}

    transport = RecordingTransport()
    transport.append_blocks("page-1", [first, second, third])

    assert transport.calls == [
        ("GET", "blocks/page-1/children?page_size=100", None),
        ("PATCH", "blocks/page-1/children", {"children": [third]}),
    ]


def test_notion_transport_retry_after_ambiguous_append_does_not_duplicate():
    first = {"object": "block", "type": "paragraph", "paragraph": {"n": 1}}
    second = {"object": "block", "type": "paragraph", "paragraph": {"n": 2}}

    class RecordingTransport(_NotionTransport):
        def __init__(self):
            super().__init__("token")
            self.blocks = [first]
            self.append_attempts = 0

        def _request(self, method, path, body=None):
            if method == "GET":
                return {"results": list(self.blocks), "has_more": False}
            if path == "blocks/page-1/children":
                self.append_attempts += 1
                self.blocks.extend(body["children"])
                if self.append_attempts == 1:
                    raise DepartmentNotionProjectionError("response lost")
            return {"id": "page-1"}

    transport = RecordingTransport()
    with pytest.raises(DepartmentNotionProjectionError):
        transport.append_blocks("page-1", [second])

    transport.append_blocks("page-1", [second])

    assert transport.blocks == [first, second]
    assert transport.append_attempts == 1


def test_notion_transport_skips_archived_blocks_when_replacing_body():
    class RecordingTransport(_NotionTransport):
        def __init__(self):
            super().__init__("token")
            self.calls = []

        def _request(self, method, path, body=None):
            self.calls.append((method, path, body))
            if method == "GET":
                return {
                    "results": [
                        {"id": "archived-1", "archived": True},
                        {"id": "trash-1", "in_trash": True},
                        {"id": "active-1"},
                    ],
                    "has_more": False,
                }
            return {"id": "page-1"}

    transport = RecordingTransport()
    transport.replace_blocks("page-1", [{"object": "block"}])

    assert ("PATCH", "blocks/active-1", {"in_trash": True}) in transport.calls
    assert not any("archived-1" in str(call) for call in transport.calls)
    assert not any("trash-1" in str(call) for call in transport.calls)


def test_notion_title_lookup_never_uses_a_shared_human_title_as_a_contains_key():
    class RecordingTransport(_NotionTransport):
        def __init__(self):
            super().__init__("token")
            self.calls = []

        def _request(self, method, path, body=None):
            self.calls.append((method, path, body))
            return {"results": []}

    transport = RecordingTransport()

    assert transport.query_title("db", "제목", "사용자 PAPER 조건주문 · 2026-08-27") == ()
    assert len(transport.calls) == 1

    assert transport.query_title("db", "제목", "t_trade1 · 사용자 PAPER 조건주문") == []
    assert len(transport.calls) == 3
    assert transport.calls[-1][2]["filter"]["title"] == {"contains": "t_trade1"}


def test_quant_notion_projection_is_manager_facing_and_hides_runtime_fields():
    task = {
        "id": "t_quant1",
        "title": "quant-liaison primary",
        "assignee": "quant-liaison",
        "status": "done",
        "completed_at": 1787802033,
        "run_metadata": {
            "final_answer": "검증된 성과지표는 자료 부족으로 산출하지 않고 보류했습니다.",
            "symbol": "069500.KS",
            "as_of": "2026-08-27T03:20:00Z",
            "source": "research-liaison-mcp",
            "evidence_refs": ["ls-tr:example"],
        },
    }

    title = _task_title(task, "quant-backtest")
    body = _body_markdown(
        task=task,
        root_task_id="t_root1",
        department="quant-backtest",
        result_text=task["run_metadata"]["final_answer"],
    )

    assert title.startswith("퀀트·백테스트 검토 결과")
    assert "Task ID" not in body
    assert "Workflow Root Task ID" not in body
    assert "Terminal Metadata" not in body
    assert "research-liaison-mcp" in body
    assert "확인된 근거 좌표: 1건" in body


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

    title = props["제목"]["title"][0]["text"]["content"]
    assert title.startswith("AAPL execution review · ")
    assert "t_trade1" not in title
    assert props["trade_case_id"]["rich_text"][0]["text"]["content"] == "case-77"
    assert "Task ID" not in props
    assert "workflow_root_task_id" not in props
    assert children


def test_trading_projection_renders_conditional_rule_for_managers():
    transport = FakeTransport(
        {
            "제목": {"type": "title"},
            "결과 요약": {"type": "rich_text"},
            "상세 결과": {"type": "rich_text"},
            "종목": {"type": "rich_text"},
            "조건 규칙 상태": {"type": "rich_text"},
            "검토 결과": {
                "type": "select",
                "select": {"options": [{"name": "처리 확인"}]},
            },
        }
    )
    task = _trading_task()
    task.update(
        {
            "title": "사용자 PAPER 조건주문 검토",
            "run_metadata": {
                "final_answer": "PAPER 조건 규칙이 활성화되었습니다.",
                "tool_result": {
                    "mode": "PAPER",
                    "state": "ACTIVE",
                    "rule_active": True,
                    "summary": {
                        "symbol": "043200",
                        "side": "SELL",
                        "sizing_type": "ALL",
                        "order_type": "MARKET",
                        "repeat_policy": "ONCE",
                    },
                },
            },
        }
    )

    result = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x"}, transport=transport
    ).project(root_task_id="t_root1", task=task)

    assert result.status == "created"
    _, props, children = transport.created[0]
    assert props["종목"]["rich_text"][0]["text"]["content"] == "043200"
    assert "조건 규칙 상태" in props
    assert props["검토 결과"]["select"]["name"] == "처리 확인"
    rendered = str(children)
    assert "PAPER 조건 규칙의 활성화 기록" in rendered
    assert "주문 접수·체결·원장 반영 결과를 의미하지 않습니다." in rendered
    assert "Task ID" not in rendered
    assert "workflow_root_task_id" not in rendered


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
    # A new human-facing title also checks the former ID-prefixed title once,
    # so existing live cards are migrated in place instead of duplicated.
    assert transport.query_calls == 6
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
    assert transport.query_calls == 3
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
    assert transport.query_calls == 6
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
    title = transport.created[0][1]["제목"]["title"][0]["text"]["content"]
    assert title.startswith("Samsung risk review · ")
    assert "t_trade1" not in title
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
                "snapshot authoritative=false, quality_status=WARN, gross/net exposure, "
                "Accounting advisory snapshot, 투자지침 snapshot, Advisory Risk, "
                "Risk Engine, Trading 활성화, "
                "max_gross_exposure 150%, max_instrument_weight 15%, "
                "max_sector_weight 35%, max_concurrent_positions 8, "
                "unavailable_reference_mapping, REQUIRES_USER_REVIEW, "
                "PROVISIONAL_CRYPTO, as_of 2026-08-26.\n"
                "법률 판정: no_breach\n"
                "이번 법률 조회는 PAPER만으로는 no_breach으로 보았지만 추가 확인이 필요합니다.\n"
                "error: null\n"
                'block_reason: "현재 포지션 자료가 없습니다."\n'
                "[Terminal handoff]\n"
                "- mode: fast_advisory\n"
                "- execution: PROHIBITED / 미수행"
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
    risk_title = props["제목"]["title"][0]["text"]["content"]
    assert risk_title.startswith("삼성전자 포지션 리스크 검토 · ")
    assert "t_trade1" not in risk_title
    narrative = str(props["리스크 검토 요약"])
    assert "현재 유효한 투자지침 스냅샷을 확인할 수 없는 상태" in narrative
    assert "총액 기준 노출" in narrative
    assert "국내 주식" in narrative
    assert "투자지침이 없어 투자지침을 확인하지 못했고" in narrative
    assert "순자산 가치" in narrative
    assert "보통" in narrative
    assert "총액·순액 노출" in narrative
    assert "회계 조회 자료" in narrative
    assert "투자지침 조회 자료" in narrative
    assert "자문성 리스크" in narrative
    assert "결정론적 리스크 검증 시스템" in narrative
    assert "거래 활성화" in narrative
    assert "총노출 한도" in narrative
    assert "종목 비중 한도" in narrative
    assert "섹터 비중 한도" in narrative
    assert "동시 보유 종목 수 한도" in narrative
    assert "자료 품질 상태: 주의" in narrative
    assert "공식 확정 자료가 아님" in narrative
    assert "참조 분류 미확인" in narrative
    assert "사람 검토 필요" in narrative
    assert "가상자산" in narrative
    assert "기준 시각" in narrative
    assert "현재 입력만으로 위반을 확인하지 못함" in narrative
    assert "법률 위반 여부를 확정할 수 없으며" in narrative
    assert "판단 보류 사유" in narrative
    assert "snapshot_resolvable" not in narrative
    assert "authoritative=false" not in narrative
    assert "quality_status" not in narrative
    assert "max_gross_exposure" not in narrative
    assert "max_instrument_weight" not in narrative
    assert "max_sector_weight" not in narrative
    assert "max_concurrent_positions" not in narrative
    assert "unavailable_reference_mapping" not in narrative
    assert "REQUIRES_USER_REVIEW" not in narrative
    assert "PROVISIONAL_CRYPTO" not in narrative
    assert "as_of" not in narrative
    assert "Accounting advisory snapshot" not in narrative
    assert "투자지침 snapshot" not in narrative
    assert "Advisory Risk" not in narrative
    assert "Risk Engine" not in narrative
    assert "Trading 활성화" not in narrative
    assert "투자지침 조회 자료이 주문" not in narrative
    assert "결정론적 결정론적" not in narrative
    assert "block_reason" not in narrative
    assert "error: null" not in narrative
    assert "Terminal handoff" not in narrative
    assert "- mode: fast_advisory" not in narrative
    assert "짧은 전달용 요약" not in str(props)
    rendered = str(children)
    assert "리스크 부서 검토 결과" in rendered


def test_risk_projection_renders_structured_result_and_populates_columns():
    transport = FakeTransport(
        {
            "제목": {"type": "title"},
            "상세 검토 보고서": {"type": "rich_text"},
            "리스크 검토 요약": {"type": "rich_text"},
            "리스크 검사 결과": {"type": "rich_text"},
            "상위·법무 검토 필요": {"type": "checkbox"},
            "법률·컴플라이언스 판정": {
                "type": "select",
                "select": {"options": [{"name": "ambiguous"}]},
            },
            "작성 시각": {"type": "date"},
        }
    )
    task = _trading_task()
    task.update(
        {
            "id": "t_structured_risk",
            "assignee": "risk-management",
            "title": "Risk 검토: 골든크로스 백테스트",
            "result": "",
            "run_metadata": {
                "verdict": "DEFER",
                "legal_verdict": "ambiguous",
                "result": {
                    "recommendation": "REQUIRES_USER_REVIEW",
                    "execution_authority": "none",
                    "paper_only": True,
                    "live_order_approval": False,
                    "mandate_status": "not_provided",
                    "review_findings": ["거래비용 자료가 없습니다."],
                },
                "required_validation": ["원자료와 재현 trace를 확인합니다."],
                "block_reason": "사용자 Mandate가 없습니다.",
                "calculation": {
                    "cost_basis_krw": 8000000,
                    "market_value_krw": 7600000,
                    "unrealized_pnl_krw": -400000,
                    "loss_rate_percent": -5.0,
                    "rounding": "소수점 둘째 자리",
                },
            },
        }
    )

    result = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x", "NOTION_RISK_DB": "risk-db"},
        transport=transport,
    ).project(root_task_id="t_root1", task=task)

    assert result.status == "created"
    _, props, children = transport.created[0]
    rendered = str(children)
    assert "{'recommendation'" not in rendered
    assert "권고" in rendered
    assert "주요 위험 요인" in rendered
    assert "필수 검증 항목" in rendered
    assert "리스크 검사 결과" in props
    assert "계산 요약" in props["리스크 검사 결과"]["rich_text"][0]["text"]["content"]
    risk_summary = props["리스크 검토 요약"]["rich_text"][0]["text"]["content"]
    assert "###" not in risk_summary
    assert "**" not in risk_summary
    assert props["상위·법무 검토 필요"]["checkbox"] is True
    assert "Department Task Result" not in rendered
    assert "Original Instruction" not in rendered
    assert "worker_session_id" not in rendered
    assert "workflow_root_task_id" not in rendered


def test_risk_humanization_keeps_contextual_terms_grammatical():
    rendered = _humanize_risk_result(
        "Risk PAPER 조회입니다. 스냅샷 범위와 동결 스냅샷상 자료를 확인했습니다. "
        "Mandate snapshot이 주문을 승인하지 않으며 결정론적 Risk Engine 검증이 필요합니다. "
        "섹터 매핑은 5개 전부 미매핑"
    )

    assert "리스크 분석용 가상거래" in rendered
    assert "조회 자료 범위" in rendered
    assert "동결된 조회 자료 기준" in rendered
    assert "투자지침 조회 자료가 주문" in rendered
    assert "결정론적 리스크 검증 시스템이 필요" in rendered
    assert "섹터 분류는 5개 모두 확인되지 않음" in rendered
    assert "결정론적 결정론적" not in rendered


def test_risk_humanization_labels_quality_status_for_managers():
    rendered = _humanize_risk_result("포트폴리오 조회 자료는 비권위적(WARN)입니다.")

    assert rendered == "포트폴리오 조회 자료는 비권위적(자료 품질: 주의)입니다."


def test_risk_humanization_does_not_corrupt_unavailable_status():
    rendered = _humanize_risk_result(
        "UNAVAILABLE 관측과 NAV를 확인했습니다. Proposal-only Workforce Agent와 NO_SNAPSHOT Scorecard입니다."
    )

    assert "관측 시스템에서 확인 불가" in rendered
    assert "순자산 가치" in rendered
    assert "제안 전용 인력 운영 에이전트" in rendered
    assert "확인 자료 없음 성과표" in rendered
    assert "U순자산 가치AILABLE" not in rendered


def test_risk_humanization_translates_legal_metadata_names():
    rendered = _humanize_risk_result(
        "cited_documents가 비어 있습니다. legal_wiki_calls=1, "
        "legal_status=OK, legal_verdict=ambiguous, source_references를 확인했습니다."
    )

    assert "확인된 인용 문서가 비어 있습니다" in rendered
    assert "법률 Wiki 호출 횟수=1" in rendered
    assert "법률 조회 상태=OK" in rendered
    assert "법률 검토 결과=ambiguous" in rendered
    assert "공식 근거 좌표를 확인했습니다" in rendered
    assert "cited_documents" not in rendered
    assert "legal_wiki_calls" not in rendered


def test_risk_humanization_translates_veto_status_for_managers():
    rendered = _humanize_risk_result(
        "independent risk veto pending deterministic order-time 결정론적 리스크 검증 시스템 checks"
    )

    assert rendered == "독립 리스크 검토가 필요하며 주문 시점의 결정론적 리스크 검증 결과를 확인해야 합니다"
    assert "pending" not in rendered
    assert "checks" not in rendered

    already_humanized = _humanize_risk_result(
        "독립 리스크 검토 pending 주문 시점 결정론적 결정론적 리스크 검증 시스템 checks"
    )
    assert already_humanized == rendered


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


def test_hr_projection_uses_native_database_and_manager_language():
    transport = FakeTransport(
        {
            "후보 role_code": {"type": "title"},
            "CEO 승인": {"type": "checkbox"},
            "IAM 생성": {"type": "checkbox"},
            "QA 독립검증": {"type": "checkbox"},
            "서술": {"type": "rich_text"},
            "원본 리포트": {"type": "rich_text"},
            "생성 시각": {"type": "date"},
        }
    )
    task = _trading_task()
    task.update(
        {
            "id": "t_hr1",
            "assignee": "hr-department",
            "title": "HR: 리스크 분석 보조 Agent 채용 설계",
            "run_metadata": {
                "final_answer": (
                    "제안서를 작성했습니다. 처리량·비용은 NO_SNAPSHOT, "
                    "최근 관측은 UNAVAILABLE입니다. "
                    "/opt/data/shared-kanban/kanban/workspaces/t_hr1/agent.md"
                ),
                "recommendation": "proposal_only_pending_evidence",
                "improvement_candidates": 0,
                "risk_scorecard": {
                    "capacity": "NO_SNAPSHOT",
                    "cost": "NO_SNAPSHOT",
                    "quality_metrics": "—",
                },
                "observability_risk": "UNAVAILABLE",
                "worker_session_id": "must-not-be-projected",
            },
        }
    )

    result = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x", "NOTION_HR_DB": "hr-db"},
        transport=transport,
    ).project(root_task_id="t_root1", task=task)

    assert result.status == "created"
    database_id, props, children = transport.created[0]
    assert database_id == "hr-db"
    assert props["후보 role_code"]["title"][0]["text"]["content"].startswith(
        "t_hr1 · 리스크 분석"
    )
    assert props["CEO 승인"]["checkbox"] is False
    assert props["IAM 생성"]["checkbox"] is False
    assert props["QA 독립검증"]["checkbox"] is False
    rendered = str(children)
    assert "HR 부서 업무·성과 요약" in rendered
    assert "근거 보강 후 재검토하는 조건부 제안" in rendered
    assert "확인 자료 없음" in rendered
    assert "관측 시스템에서 확인 불가" in rendered
    assert "NO_SNAPSHOT" not in rendered
    assert "UNAVAILABLE" not in rendered
    assert "/opt/data/shared-kanban" not in rendered
    assert "worker_session_id" not in rendered
    assert "structured_summary" not in rendered


def test_hr_projection_reads_authoritative_api_reads_envelope():
    transport = FakeTransport(
        {
            "후보 role_code": {"type": "title"},
            "CEO 승인": {"type": "checkbox"},
            "IAM 생성": {"type": "checkbox"},
            "QA 독립검증": {"type": "checkbox"},
        }
    )
    task = _trading_task()
    task.update(
        {
            "id": "t_hr_api_reads",
            "assignee": "hr-department",
            "title": "HR: Workforce 근거 검토",
            "run_metadata": {
                "api_reads": {
                    "improvements": {"candidate_count": 0, "http_status": 200},
                    "observability": {
                        "idle_state_counts": {"UNAVAILABLE": 8},
                        "http_status": 200,
                    },
                    "scorecard_brief": {
                        "capacity": {
                            "research-department": "NO_SNAPSHOT",
                            "risk-management": "NO_SNAPSHOT",
                        },
                        "cost": {
                            "research-department": "NO_SNAPSHOT",
                            "risk-management": "NO_SNAPSHOT",
                        },
                        "quality": {
                            "research-department": {
                                "eval_run_refs": 0,
                                "eval_score": "—",
                            },
                            "risk-management": {
                                "eval_run_refs": 0,
                                "eval_score": "—",
                            },
                        },
                    },
                    "proposal_only": True,
                }
            },
        }
    )

    result = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x", "NOTION_HR_DB": "hr-db"},
        transport=transport,
    ).project(root_task_id="t_root1", task=task)

    assert result.status == "created"
    rendered = str(transport.created[0][2])
    assert "근거 보강 후 재검토하는 조건부 제안" in rendered
    assert "개선 후보: 0건" in rendered
    assert "처리량 자료: 확인 자료 없음" in rendered
    assert "비용 자료: 확인 자료 없음" in rendered
    assert "품질 지표: 확인 자료 없음" in rendered
    assert "관측 시스템에서 확인 불가" in rendered


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
            "claim_checks 판정": {
                "type": "multi_select",
                "multi_select": {
                    "options": [{"name": "UNSUPPORTED"}]
                },
            },
            "claim_narrative": {"type": "rich_text"},
            "원본 리포트": {"type": "rich_text"},
            "reason_codes": {
                "type": "multi_select",
                "multi_select": {
                    "options": [{"name": "numeric_citation_mismatch"}]
                },
            },
            "input_hash": {"type": "rich_text"},
            "calculation_version": {"type": "rich_text"},
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
                        "finding_id": "QA-F001",
                        "statement": "NAV bridge has an unexplained residual",
                        "owner": "Accounting Engine",
                        "block_condition": "공식 수치 확정 차단",
                        "status": "OPEN",
                    }
                ],
                "checks": [{"check": "nav_bridge", "result": "FAIL"}],
                "reason_codes": ["numeric_citation_mismatch"],
                "input_hash": "sha256:" + "a" * 64,
                "calculation_version": "qa-checker-v1",
                "escalate": True,
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
    assert "순자산 bridge has an unexplained residual" in rendered
    assert "QA-F001" not in rendered
    assert "workflow_root_task_id" not in rendered
    assert props["판정"]["select"]["name"] == "FAIL"
    assert props["findings severity"]["select"]["name"] == "HIGH"
    assert "QA-F001" not in props["findings"]["rich_text"][0]["text"]["content"]
    assert "순자산 대사: 실패" in props["claim_checks"]["rich_text"][0]["text"]["content"]
    assert props["claim_checks 판정"]["multi_select"][0]["name"] == "UNSUPPORTED"
    assert props["reason_codes"]["multi_select"][0]["name"] == "numeric_citation_mismatch"
    assert props["input_hash"]["rich_text"][0]["text"]["content"].startswith("sha256:")
    assert props["calculation_version"]["rich_text"][0]["text"]["content"] == "qa-checker-v1"
    assert props["claim_narrative"]["rich_text"]
    assert props["escalate"]["checkbox"] is True
    assert "trade_case_id" not in props
    assert "trace_id" not in props


def test_qa_projection_normalizes_flattened_audit_metadata():
    transport = FakeTransport(
        {
            "제목": {"type": "title"},
            "판정": {"type": "select", "select": {"options": [{"name": "FAIL"}]}},
            "findings severity": {
                "type": "select",
                "select": {"options": [{"name": "BLOCKER"}]},
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
            "body": "workflow_root_task_id=t_root1\nworkflow_role=qa",
            "run_metadata": {
                "overall": "FAIL",
                "decision_status": "BLOCKED_FOR_DECISION",
                "snapshot_value_consistency": "PASS",
                "nav_bridge_reconciliation_disclosure": "FAIL",
                "evidence_provenance": "FAIL",
                "finding": "NAV bridge gap remains unresolved.",
                "recommended_action": "Add source and as-of coordinates before finalization.",
                "nav_cash_securities_gap_krw": "2951361",
                "mapped_positions": 0,
                "positions_count": 5,
                "pnl_available": False,
            },
        }
    )

    result = DepartmentNotionProjection(
        env={"NOTION_TOKEN": "x", "NOTION_QA_DB": "qa-db"},
        transport=transport,
    ).project(root_task_id="t_root1", task=task)

    assert result.status == "created"
    rendered = str(transport.created[0][2])
    assert "스냅샷 수치 일치: 통과" in rendered
    assert "순자산 대사 공개: 실패" in rendered
    assert "순자산 bridge gap remains unresolved." in rendered
    assert "순자산 대사 차이 2,951,361원" in rendered
    assert "섹터 매핑 0/5건" in rendered
    assert "손익 미제공" in rendered
    assert "Add source and as-of coordinates" in rendered
    assert "구체적인 문제 설명이 없습니다" not in rendered
    props = transport.created[0][1]
    assert props["findings severity"]["select"]["name"] == "BLOCKER"
    assert props["findings"]["rich_text"]
    assert props["claim_checks"]["rich_text"]
    assert props["claim_narrative"]["rich_text"]
    assert props["escalate"]["checkbox"] is True
    assert "trade_case_id" not in props
    assert "trace_id" not in props


def test_qa_projection_normalizes_audit_finding_object_without_issue_text():
    metadata = {
        "checks": {"scope_paper_read_only": "PASS"},
        "finding": {
            "id": "F-1",
            "severity": "MEDIUM",
            "block_condition": "future_official_nav_reconciliation_order_or_risk_decision_use_requires_reproducible_source_coordinates",
        },
    }

    findings = qa_projection_findings({}, metadata)

    assert findings[0]["statement"] == "재현 가능한 출처 좌표가 없습니다."
    assert findings[0]["recommended_action"]
    assert qa_projection_checks({}, metadata) == {"scope_paper_read_only": "PASS"}
