from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "departments" / "02-trading" / "api"
DEPT_DIR = ROOT / "departments" / "02-trading"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))
if str(DEPT_DIR) not in sys.path:
    sys.path.insert(0, str(DEPT_DIR))

import directive_routes  # noqa: E402
import strategy_paper_routes as routes  # noqa: E402


def test_strategy_paper_route_builds_bound_market_directive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = UUID("00000000-0000-4000-8000-00000000cec0")
    fund_id = UUID("3838f7d6-0c7c-4e54-85f3-316a451e7eeb")
    book_id = UUID("07d913de-9a5b-4cf5-b893-31a625445761")
    monkeypatch.setenv("STRATEGY_PAPER_USER_ID", str(user_id))
    monkeypatch.setenv("STRATEGY_PAPER_FUND_ID", str(fund_id))
    monkeypatch.setenv("STRATEGY_PAPER_BOOK_ID", str(book_id))
    monkeypatch.setattr(
        routes,
        "authenticate_internal_service",
        lambda *_args, **_kwargs: SimpleNamespace(service="strategy-runtime-control"),
    )
    seen: list[tuple[object, object]] = []

    class Service:
        def submit_trusted_rule(self, request, proof, *, now):
            seen.append((request, proof))
            return SimpleNamespace(
                view=lambda: {
                    "directive_id": "directive-1",
                    "state": "IN_PROGRESS",
                    "legs": [{"state": "ACKNOWLEDGED"}],
                }
            )

    monkeypatch.setattr(directive_routes, "required_directive_service", lambda: Service())
    result = routes.submit_strategy_paper_order(
        routes.StrategyPaperOrderRequest(
            deployment_id="deployment-0123456789abcdef01234567",
            symbol="000660",
            side="BUY",
            quantity="1",
            signal_key="deployment-0123456789abcdef01234567|000660|BUY|bar",
        ),
        authorization="Bearer test",
    )

    assert result["execution_status"] == "PAPER_ORDER_SUBMITTED"
    assert result["directive"]["directive_id"] == "directive-1"
    request, proof = seen[0]
    assert request.fund_id == fund_id
    assert request.book_id == book_id
    assert request.payload["symbol"] == "000660"
    assert request.payload["side"] == "BUY"
    assert proof.subject == user_id
