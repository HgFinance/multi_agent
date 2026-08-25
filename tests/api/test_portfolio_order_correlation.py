from datetime import datetime, timezone
from types import SimpleNamespace

from apps.api import ls_account_stream
from apps.api.user_order_workflow import (
    BrokerOrderCorrelation,
    InMemoryUserOrderRequestRepository,
)


def _audit_payload(directive_id: str, broker_order_no: str) -> dict:
    return {
        "schema_version": "paper-order-correlation.v1",
        "mode": "PAPER",
        "request_source": "DISCORD",
        "directive_id": directive_id,
        "directive_state": "COMPLETED",
        "legs": [
            {
                "symbol": "000660",
                "side": "BUY",
                "state": "FILLED",
                "requested_quantity": "2.0000000000",
                "filled_quantity": "2.0000000000",
                "average_fill_price": "1609000.0000000000",
                "broker_order_id": f"ls-paper:{broker_order_no}",
                "broker_order_no": broker_order_no,
            }
        ],
    }


def _record(repository: InMemoryUserOrderRequestRepository, suffix: str):
    return repository.admit(
        user_id=f"user-{suffix}",
        fund_id=f"fund-{suffix}",
        book_id=f"book-{suffix}",
        client_request_id=f"discord:message-{suffix}",
        raw_instruction="하이닉스 2주 매수",
    )


def test_existing_execution_snapshot_correlates_raw_ls_order_number() -> None:
    repository = InMemoryUserOrderRequestRepository()
    record = _record(repository, "one")
    repository.mark_outcome(
        record.order_request_id,
        state="COMPLETED",
        directive_id="11111111-1111-4111-8111-111111111111",
        event_type="BROKER_EXECUTION_SNAPSHOT",
        event_payload=_audit_payload(
            "11111111-1111-4111-8111-111111111111", "22988"
        ),
    )

    result = repository.broker_correlations(
        {"22988"}, recorded_after=datetime.now(timezone.utc)
    )

    assert result["22988"].broker_order_id == "ls-paper:22988"
    assert result["22988"].request_source == "DISCORD"
    assert result["22988"].symbol == "000660"


def test_duplicate_raw_broker_number_with_distinct_lineage_stays_unattributed() -> None:
    repository = InMemoryUserOrderRequestRepository()
    for suffix, directive_id in (
        ("one", "11111111-1111-4111-8111-111111111111"),
        ("two", "22222222-2222-4222-8222-222222222222"),
    ):
        record = _record(repository, suffix)
        repository.mark_outcome(
            record.order_request_id,
            state="COMPLETED",
            directive_id=directive_id,
            event_type="BROKER_EXECUTION_SNAPSHOT",
            event_payload=_audit_payload(directive_id, "22988"),
        )

    assert repository.broker_correlations(
        {"22988"}, recorded_after=datetime.now(timezone.utc)
    ) == {}


def test_portfolio_projection_labels_exact_conditional_order(monkeypatch) -> None:
    correlation = BrokerOrderCorrelation(
        broker_order_no="22988",
        broker_order_id="ls-paper:22988",
        directive_id="11111111-1111-4111-8111-111111111111",
        order_request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        user_id="user-one",
        fund_id="fund-one",
        book_id="book-one",
        client_request_id="discord:message-one",
        request_source="DISCORD",
        directive_state="COMPLETED",
        leg_state="FILLED",
        symbol="000660",
        side="BUY",
        requested_quantity="2.0000000000",
        filled_quantity="2.0000000000",
        average_fill_price="1609000.0000000000",
        recorded_at=datetime.now(timezone.utc),
    )
    order_repository = SimpleNamespace(
        broker_correlations=lambda *_args, **_kwargs: {"22988": correlation}
    )
    conditional_repository = SimpleNamespace(
        find_by_directive_ids=lambda _directive_ids: {
            correlation.directive_id: SimpleNamespace(
                directive_id=correlation.directive_id,
                rule_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                state=SimpleNamespace(value="COMPLETED"),
            )
        }
    )
    monkeypatch.setattr(ls_account_stream, "user_order_repository", lambda: order_repository)
    monkeypatch.setattr(
        ls_account_stream, "conditional_rule_repository", lambda: conditional_repository
    )
    today = datetime.now(ls_account_stream.KST).replace(tzinfo=None).isoformat()
    event = {
        "source": "LS_ORDER_HISTORY",
        "received_at": today,
        "kind": "FILLED",
        "broker_order_id": "22988",
        "order_no": "22988",
        "symbol": "000660",
        "side": "매수",
        "quantity": "2",
        "price": "1609000",
        "average_fill_price": "1609000",
    }

    projected, summary = ls_account_stream._project_internal_order_correlations(
        [event]
    )

    assert summary == {
        "status": "READY",
        "source": "execution.user_order_request_events",
        "attributed": 1,
        "unattributed": 0,
        "error": None,
    }
    assert projected[0]["origin"] == "INTERNAL_CONDITIONAL_ORDER"
    assert projected[0]["correlation_status"] == "ATTRIBUTED"
    assert projected[0]["conditional_rule_id"] == (
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    )
