from __future__ import annotations

import json

from orchestration.adapters.ceo_supervisor import (
    CeoSupervisorService,
    SupervisorAction,
    SupervisorDecision,
    SupervisorState,
)
from orchestration.ceo_workflow_scope import build_root_body
from orchestration.risk_advisory_context import fetch_risk_advisory_context


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Client:
    environment = {}

    def __init__(self) -> None:
        self.created = []

    def create_task(self, **kwargs):
        self.created.append(kwargs)
        return {"id": "t_risk-child"}


def test_root_body_carries_only_advisory_identifiers() -> None:
    body = build_root_body(
        "삼성전자 리스크",
        "request-1",
        advisory_fund_id="fund-1",
        advisory_book_id="00000000-0000-0000-0000-000000000001",
    )
    assert "advisory_fund_id=fund-1" in body
    assert "advisory_book_id=00000000-0000-0000-0000-000000000001" in body
    assert "nav=" not in body
    assert "credential" not in body.casefold()


def test_snapshot_is_compacted_to_read_only_risk_context(monkeypatch) -> None:
    monkeypatch.setenv("ACCOUNTING_API_URL", "http://accounting-api:8000")
    payload = {
        "authoritative": False,
        "source_of_record": "accounting.journals (Supabase)",
        "portfolio": {
            "as_of": "2026-08-23T09:00:00+00:00",
            "quality_status": "WARN",
            "nav": "1000000",
            "cash": "200000",
            "securities_value": "800000",
            "gross_exposure": "800000",
            "net_exposure": "800000",
            "positions": [
                {
                    "instrument_id": "i-1",
                    "symbol": "005930",
                    "display_name": "삼성전자",
                    "quantity": "10",
                    "market_value": "800000",
                    "weight": "0.8",
                    "raw_provider_output": "must not pass",
                }
            ],
        },
        "sector_exposure": {"status": "unavailable_reference_mapping"},
    }

    def fake_urlopen(request, timeout):
        assert request.full_url.endswith(
            "/accounting/v1/ledgers/00000000-0000-0000-0000-000000000001/advisory-snapshot"
        )
        assert timeout <= 2.0
        return _Response(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    context = fetch_risk_advisory_context(
        "advisory_book_id=00000000-0000-0000-0000-000000000001"
    )
    assert context is not None
    assert "raw_provider_output" not in context
    assert '"nav":"1000000"' in context
    assert '"authoritative":false' in context


def test_accounting_failure_is_fail_open(monkeypatch) -> None:
    monkeypatch.setenv("ACCOUNTING_API_URL", "http://accounting-api:8000")

    def fail_urlopen(*args, **kwargs):
        raise TimeoutError("accounting unavailable")

    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    assert fetch_risk_advisory_context(
        "advisory_book_id=00000000-0000-0000-0000-000000000001"
    ) is None


def test_risk_child_receives_optional_context_without_changing_role() -> None:
    client = _Client()
    service = CeoSupervisorService(client)
    state = SupervisorState(
        parent_task_id="t_root",
        children=(),
        risk_advisory_context='{"contract":"hgfinance.risk-advisory-portfolio.v1","nav":"1"}',
    )
    service._execute(
        SupervisorDecision(
            SupervisorAction.CREATE_TASK,
            "t_root",
            assignee="risk-management",
            title="Risk analysis",
            body="risk instruction",
        ),
        state,
    )
    created = client.created[0]
    assert "workflow_role=primary" in created["body"]
    assert "hgfinance.risk-advisory-portfolio.v1" in created["body"]
    assert created["assignee"] == "risk-management"
