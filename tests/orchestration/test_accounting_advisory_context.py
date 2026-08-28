from __future__ import annotations

import json
from unittest.mock import patch

from orchestration.accounting_advisory_context import fetch_accounting_advisory_context


def test_accounting_context_reads_the_canonical_fixed_book(monkeypatch) -> None:
    book_id = "07d913de-9a5b-4cf5-b893-31a625445761"
    monkeypatch.setenv("ACCOUNTING_ADVISORY_BOOK_ID", book_id)
    monkeypatch.setenv("ACCOUNTING_API_URL", "http://accounting-api:8000")
    payload = {
        "source_of_record": "accounting.journals (Supabase)",
        "authoritative": False,
        "portfolio": {
            "as_of": "2026-08-26T06:19:08+00:00",
            "nav": "505532048",
            "cash": "478730004",
            "securities_value": "23822750",
            "realized_pnl": "96660.73",
            "unrealized_pnl": "757978.27",
            "fees": "167706",
            "taxes": "5105",
            "quality_status": "WARN",
            "positions": [{"symbol": "000660", "quantity": "6"}],
        },
    }
    broker_payload = {
        "schema_version": "accounting.broker-evidence.v1",
        "as_of": "2026-08-26T06:20:00+00:00",
        "environment": "PAPER",
        "source": "LS OPEN API /stock/accno",
        "account": {"masked": "****1234"},
        "period": {"start": "2026-08-01", "end": "2026-08-26", "previous_date": "2026-08-25"},
        "coverage": {"t0424": {"name": "주식잔고2", "status": "OK", "pages": 1}},
        "account_summary": {"cash": {"deposit": "1000"}},
        "positions": [{"symbol": "000660", "quantity": "6", "unit_cost_bep": "201000"}],
        "activity": {},
        "performance": {},
        "exceptions": [],
        "evidence_refs": ["ls-tr:t0424"],
        "authoritative": False,
    }

    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return json.dumps(self.body).encode()

    def fake_urlopen(request, timeout):
        if request.full_url.startswith("http://accounting-api:8000/"):
            assert request.full_url == (
                "http://accounting-api:8000/accounting/v1/ledgers/"
                f"{book_id}/advisory-snapshot"
            )
            assert "/ui/snapshot" not in request.full_url
            return Response(payload)
        assert request.full_url == (
            "http://portfolio-bff:8000/internal/accounting/broker-evidence"
        )
        return Response(broker_payload)

    with patch(
        "orchestration.accounting_advisory_context.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        context = fetch_accounting_advisory_context(
            "3838f7d6-0c7c-4e54-85f3-316a451e7eeb"
        )

    assert context is not None
    compact = json.loads(context)
    assert compact["nav"] == "505532048"
    assert compact["positions"][0]["symbol"] == "000660"
    assert compact["source_of_record"] == "accounting.journals (Supabase)"
    assert compact["broker_evidence"]["schema_version"] == "accounting.broker-evidence.v1"
    assert compact["broker_evidence"]["positions"][0]["unit_cost_bep"] == "201000"
    assert compact["broker_evidence"]["authoritative"] is False
    assert compact["broker_evidence"]["position_reconciliation_scope"] == (
        "broker_internal_only"
    )
    assert "직접 비교해 새 대사 차이를 만들지 말고" in compact["broker_evidence"][
        "position_reconciliation_note"
    ]


def test_accounting_context_keeps_engine_snapshot_when_broker_is_unavailable(monkeypatch) -> None:
    book_id = "07d913de-9a5b-4cf5-b893-31a625445761"
    monkeypatch.setenv("ACCOUNTING_ADVISORY_BOOK_ID", book_id)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "source_of_record": "accounting.journals",
                    "authoritative": True,
                    "portfolio": {"nav": "10", "positions": []},
                }
            ).encode()

    def fake_urlopen(request, timeout):
        if "/advisory-snapshot" in request.full_url:
            return Response()
        raise OSError("portfolio BFF unavailable")

    with patch(
        "orchestration.accounting_advisory_context.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        compact = json.loads(fetch_accounting_advisory_context() or "{}")

    assert compact["nav"] == "10"
    assert "broker_evidence" not in compact


def test_accounting_context_refuses_missing_or_invalid_book(monkeypatch) -> None:
    for value in ("", "not-a-uuid"):
        monkeypatch.setenv("ACCOUNTING_ADVISORY_BOOK_ID", value)
        with patch(
            "orchestration.accounting_advisory_context.urllib.request.urlopen"
        ) as urlopen:
            assert fetch_accounting_advisory_context() is None
        urlopen.assert_not_called()
