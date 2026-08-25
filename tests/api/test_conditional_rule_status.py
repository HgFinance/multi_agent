from __future__ import annotations

import pytest

from apps.api.conditional_rule_status import (
    ConditionalStatusError,
    build_conditional_execution_status,
)


def _directive(**changes):
    value = {
        "directive_id": "55555555-5555-4555-8555-555555555555",
        "state": "COMPLETED",
        "mode": "PAPER",
        "error_code": "TRADING_FILL_ACCOUNTING_PENDING",
        "legs": [
            {
                "symbol": "005930",
                "side": "SELL",
                "order_type": "MARKET",
                "requested_quantity": "1",
                "filled_quantity": "1",
                "average_fill_price": "248250",
                "broker_order_id": "ls-paper:12695",
            }
        ],
    }
    value.update(changes)
    return value


def test_snapshot_reports_fill_but_not_unacknowledged_accounting() -> None:
    status = build_conditional_execution_status(
        rule_id="rule-1",
        directive=_directive(),
        expected_directive_id="55555555-5555-4555-8555-555555555555",
    )

    assert status.filled_quantity == "1"
    assert status.average_fill_price == "248250"
    assert status.workflow_state == "ACCOUNTING_PENDING"
    assert status.accounting_acknowledged is False
    assert "Ticker : 005930" in status.final_answer
    assert "Status : SELL" in status.final_answer
    assert "체결 수량 : 1주" in status.final_answer
    assert "평균 체결가 : 248,250원" in status.final_answer
    assert "브로커 주문 ID : ls-paper:12695" in status.final_answer
    assert "회계 반영 : 대기" in status.final_answer
    assert "1.0000000000주" not in status.final_answer
    assert "248250.000" not in status.final_answer


def test_snapshot_normalizes_database_numeric_scale() -> None:
    directive = _directive()
    directive["legs"][0].update(
        {
            "requested_quantity": "1.0000000000",
            "filled_quantity": "1.0000000000",
            "average_fill_price": "248250.00000000000000000000",
        }
    )

    status = build_conditional_execution_status(
        rule_id="rule-1",
        directive=directive,
        workflow_state="COMPLETED",
    )

    assert status.requested_quantity == "1"
    assert status.filled_quantity == "1"
    assert status.average_fill_price == "248250"
    assert "체결 수량 : 1주" in status.final_answer
    assert "평균 체결가 : 248,250원" in status.final_answer
    assert "회계 반영 : 완료" in status.final_answer


def test_snapshot_rejects_event_directive_mismatch() -> None:
    with pytest.raises(ConditionalStatusError, match="mismatch"):
        build_conditional_execution_status(
            rule_id="rule-1",
            directive=_directive(),
            expected_directive_id="another-directive",
        )
