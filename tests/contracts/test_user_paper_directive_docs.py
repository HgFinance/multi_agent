"""Documentation contract for the explicit-user PAPER authority lane.

The repository historically used the unqualified sentence "all orders pass
Risk".  ADR-0007 narrows that invariant to Agent/automated orders while
keeping authenticated user PAPER directives deterministic and fail-closed.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADR = ROOT / "docs/02-engineering/adr/0007-authenticated-user-paper-directive-authority.md"
CURRENT = ROOT / "docs/CURRENT_PROJECT_ARCHITECTURE.md"
UNIFIED = ROOT / "docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md"
TRADING = ROOT / "departments/02-trading/README.md"
MASTER = ROOT / "docs/HEDGE_FUND_MASTER_PLAN.md"
ROUTES = ROOT / "docs/02-engineering/contracts/route-registry.v1.json"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_high_authority_docs_split_agent_and_user_order_authority() -> None:
    adr = _read(ADR)
    agents = _read(ROOT / "AGENTS.md")
    claude = _read(ROOT / "CLAUDE.md")
    master = _read(MASTER)

    assert "상태: Accepted" in adr
    assert "AUTOMATED_STRATEGY" in adr
    assert "USER_DIRECTIVE" in adr
    assert "USER_DIRECTIVE_HIGHEST" in adr
    assert all(term in adr for term in ("Risk", "alpha", "rebalancer"))
    assert "모든 주문은 결정론적 Risk Engine" not in agents
    assert "모든 주문은 결정론적 Risk Engine" not in claude
    assert "Agent·alpha·자동 전략 주문 후보" in master
    assert "ADR-0007" in master


def test_hermes_is_transport_and_never_owns_user_order_authority() -> None:
    corpus = "\n".join(_read(path) for path in (ADR, CURRENT, UNIFIED, TRADING))

    assert "결정론 parser" in corpus
    assert "Hermes는 사용자의 authority를 소유하지 않는다" in corpus
    assert "/trading/agent/order" in corpus
    assert "Agent submit" in corpus


def test_batch_directives_preserve_partial_and_unknown_outcomes() -> None:
    corpus = "\n".join(_read(path) for path in (ADR, CURRENT, UNIFIED, TRADING))
    expected_states = {
        "RECEIVED",
        "RUNNING",
        "IN_PROGRESS",
        "PARTIAL",
        "COMPLETED",
        "FAILED",
        "UNKNOWN",
    }

    assert expected_states <= {state for state in expected_states if state in corpus}
    assert "PARTIAL_FAILURE" not in corpus
    assert "reduce_only" in corpus
    assert "positive accounting position" in corpus
    assert "open SELL reservation" in corpus
    assert "한 자식이라도 실패" in corpus
    assert "ACKNOWLEDGED" in corpus
    assert "ACK만 있는" in corpus


def test_paper_account_is_durable_and_ls_live_is_market_read_only() -> None:
    corpus = "\n".join(_read(path) for path in (ADR, CURRENT, UNIFIED, TRADING))

    assert "local durable PaperBroker" in corpus
    assert "directive/leg/reservation ledger" in corpus
    assert "LS LIVE" in corpus
    assert "read-only" in corpus
    assert "LIVE order route" in corpus


def test_route_registry_exposes_only_the_documented_user_paper_bff_surface() -> None:
    registry = json.loads(ROUTES.read_text(encoding="utf-8"))
    operations = {
        (item["method"], item["path"])
        for item in registry["actual_routes"]["operator-bff"]["operations"]
    }
    expected = {
        ("POST", "/ui/paper-orders"),
        ("POST", "/ui/paper-orders/sell-all"),
        ("POST", "/ui/paper-orders/cancel-all"),
        ("GET", "/ui/paper-orders/{directive_id}"),
        ("GET", "/ui/paper-orders/{directive_id}/status"),
        ("POST", "/trading/agent/order"),
    }

    assert expected <= operations
