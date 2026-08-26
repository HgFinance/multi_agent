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


def test_root_body_prefers_exact_book_accounting_snapshot(monkeypatch) -> None:
    exact = '{"contract":"hgfinance.risk-advisory-portfolio.v1","book_id":"book-1"}'

    monkeypatch.setattr(
        "orchestration.risk_advisory_context.fetch_risk_advisory_context",
        lambda root_body: exact if "advisory_book_id=book-1" in root_body else None,
    )

    def reject_global_fallback(_fund_id):
        raise AssertionError("global demo snapshot must not replace an exact Book")

    monkeypatch.setattr(
        "orchestration.accounting_advisory_context.fetch_accounting_advisory_context",
        reject_global_fallback,
    )

    body = build_root_body(
        "SK하이닉스 포지션 리스크",
        "request-exact-book",
        advisory_fund_id="fund-1",
        advisory_book_id="book-1",
    )

    assert "hgfinance.accounting-snapshot.v1" in body
    assert exact in body


def test_root_body_does_not_fall_back_to_another_book(monkeypatch) -> None:
    monkeypatch.setattr(
        "orchestration.risk_advisory_context.fetch_risk_advisory_context",
        lambda _root_body: None,
    )

    fallback_called = False

    def wrong_global_snapshot(_fund_id):
        nonlocal fallback_called
        fallback_called = True
        return '{"book_id":"wrong-demo-book"}'

    monkeypatch.setattr(
        "orchestration.accounting_advisory_context.fetch_accounting_advisory_context",
        wrong_global_snapshot,
    )

    body = build_root_body(
        "삼성전자 포지션 리스크",
        "request-missing-exact-book",
        advisory_fund_id="fund-1",
        advisory_book_id="book-1",
    )

    assert fallback_called is False
    assert "wrong-demo-book" not in body
    assert "hgfinance.accounting-snapshot.v1" not in body


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


def test_market_snapshot_uses_user_request_not_embedded_position_order(monkeypatch) -> None:
    monkeypatch.setenv("ACCOUNTING_API_URL", "http://accounting-api:8000")
    monkeypatch.setenv("MARKET_API_URL", "http://market-api:8036")
    accounting = {
        "portfolio": {
            "positions": [
                {"instrument_id": "i-naver", "symbol": "035420", "display_name": "NAVER"},
                {"instrument_id": "i-hynix", "symbol": "000660", "display_name": "SK하이닉스"},
            ]
        }
    }
    market = {
        "symbol": "000660",
        "last_trade": {"price": "1724000", "event_time": "2026-08-26T03:50:00Z"},
        "last_quote": {"best_bid": "1723000", "best_ask": "1724000"},
    }
    requested_urls: list[str] = []

    def fake_urlopen(request, timeout):
        requested_urls.append(request.full_url)
        if "advisory-snapshot" in request.full_url:
            return _Response(accounting)
        assert request.full_url.endswith("/snapshot/000660")
        return _Response(market)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    context = fetch_risk_advisory_context(
        "advisory_book_id=00000000-0000-0000-0000-000000000001\n"
        "## Accounting Engine snapshot\n"
        '{"positions":[{"symbol":"035420","display_name":"NAVER"}]}\n'
        "## User request\nSK하이닉스 PAPER 포지션 리스크 계획을 세워줘"
    )

    assert context is not None
    decoded = json.loads(context)
    assert decoded["market_snapshot"]["symbol"] == "000660"
    assert not any(url.endswith("/snapshot/035420") for url in requested_urls)


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
