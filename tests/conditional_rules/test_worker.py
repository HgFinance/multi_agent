from __future__ import annotations

import io
import threading
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from apps.api.conditional_rule_worker import (
    ConditionalRuleWorker,
    HttpRuntimeClient,
    RuntimeDataError,
    RuntimeInputs,
)
from orchestration.conditional_rules.market_data import MarketPriceSnapshot
from orchestration.conditional_rules import (
    ActiveRule,
    ConditionalRuleSpec,
    EvaluationContext,
    EvaluationFrame,
    SubmitReadyExecution,
    TriggerClaim,
)


NOW = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)


def active_rule(*, threshold: str = "100", side: str = "BUY") -> ActiveRule:
    spec = ConditionalRuleSpec.model_validate(
        {
            "schema_version": "conditional-trade-rule.v1",
            "authority": {
                "user_id": "10000000-0000-0000-0000-000000000001",
                "fund_id": "20000000-0000-0000-0000-000000000001",
                "book_id": "30000000-0000-0000-0000-000000000001",
            },
            "instrument_id": "40000000-0000-0000-0000-000000000001",
            "symbol": "005930",
            "condition": {
                "type": "COMPARISON",
                "operator": "GT",
                "left": {"type": "MARKET", "field": "LAST_PRICE"},
                "right": {"type": "LITERAL", "value": threshold, "unit": "PRICE"},
            },
            "action": {
                "side": side,
                "sizing": {"type": "FIXED_SHARES", "value": "2"},
            },
            "evaluation": {"clock": "QUOTE", "max_data_age_seconds": 10},
            "execution_mode": "PAPER",
            "repeat_policy": "ONCE",
            "expires_at": (NOW + timedelta(days=30)).isoformat(),
            "raw_instruction_sha256": "0" * 64,
        }
    )
    return ActiveRule(
        rule_id=UUID("50000000-0000-0000-0000-000000000001"),
        rule_version=1,
        row_version=1,
        spec_sha256="a" * 64,
        spec=spec,
    )


def inputs(*, price: str, market_open: bool = True) -> RuntimeInputs:
    observed = datetime.now(timezone.utc)
    frame = EvaluationFrame(
        market={"LAST_PRICE": Decimal(price)},
        portfolio={},
        indicators={},
        observed_at=observed,
    )
    return RuntimeInputs(
        evaluation_context=EvaluationContext(current=frame),
        evaluation_key=f"QUOTE:{observed.isoformat()}",
        context_sha256="b" * 64,
        data_watermark=observed,
        membership_active=True,
        fund_active=True,
        book_active=True,
        market_session_available=True,
        market_open=market_open,
        data_complete=True,
        quote_fresh=True,
        current_price=Decimal(price),
        available_cash=Decimal("1000000"),
        position_quantity=Decimal("10"),
        sellable_quantity=Decimal("10"),
        lot_size=Decimal("1"),
    )


class FakeStore:
    def __init__(self, rule: ActiveRule) -> None:
        self.rule = rule
        self.active = [rule]
        self.claimed: list[tuple[ActiveRule, TriggerClaim]] = []
        self.pending: list[SubmitReadyExecution] = []
        self.false = 0
        self.claims = 0
        self.execution_decisions: list[tuple[bool, str, Decimal | None]] = []
        self.submitted: list[UUID] = []
        self.submitting: list[UUID] = []

    def expire_due(self) -> int:
        return 0

    def list_active(self, *, limit: int = 100, offset: int = 0):
        return self.active[offset : offset + limit]

    def list_claimed(self, *, limit: int = 100):
        return self.claimed[:limit]

    def list_submit_ready(self, *, limit: int = 100):
        return self.pending[:limit]

    def record_false(self, *args, **kwargs) -> bool:
        self.false += 1
        return True

    def record_error(self, *args, **kwargs) -> bool:
        raise AssertionError("unexpected evaluation error")

    def claim_true(self, *args, **kwargs):
        self.claims += 1
        return TriggerClaim("trg_test", "eval_test")

    def create_execution(
        self, rule, claim, *, allowed: bool, guard_code: str, quantity
    ):
        self.execution_decisions.append((allowed, guard_code, quantity))
        if not allowed:
            return None
        return SubmitReadyExecution(
            rule_execution_id=UUID("60000000-0000-0000-0000-000000000001"),
            trigger_id=claim.trigger_id,
            rule_id=rule.rule_id,
            rule_version=rule.rule_version,
            idempotency_key="rule:test:execution",
        )

    def mark_submitting(self, rule_execution_id: UUID) -> None:
        self.submitting.append(rule_execution_id)

    def mark_retryable_failure(self, *args, **kwargs) -> None:
        raise AssertionError("unexpected retryable failure")

    def mark_terminal_failure(self, *args, **kwargs) -> None:
        raise AssertionError("unexpected terminal failure")

    def mark_submitted(self, rule_execution_id: UUID, *, directive_id: UUID) -> None:
        self.submitted.append(directive_id)


class FakeClient:
    def __init__(self, runtime_inputs: RuntimeInputs) -> None:
        self.runtime_inputs = runtime_inputs
        self.submit_calls = 0

    def load_inputs(self, rule: ActiveRule) -> RuntimeInputs:
        return self.runtime_inputs

    def submit(self, execution: SubmitReadyExecution) -> UUID:
        self.submit_calls += 1
        return UUID("70000000-0000-0000-0000-000000000001")


class FakePriceResolver:
    def __init__(self, price: str = "299500") -> None:
        self.price = Decimal(price)
        self.calls: list[str] = []

    def snapshot(self, symbol: str) -> MarketPriceSnapshot:
        self.calls.append(symbol)
        return MarketPriceSnapshot(
            symbol=symbol,
            price=self.price,
            observed_at=datetime.now(timezone.utc),
            source="test-ls-t1102",
        )


def test_http_runtime_client_uses_ls_price_and_only_reuses_within_cycle() -> None:
    resolver = FakePriceResolver()
    client = HttpRuntimeClient(
        trading_api_url="http://trading.test",
        market_api_url="http://market.test",
        price_resolver=resolver,
    )

    first = client._snapshot("005930")
    second = client._snapshot("005930")
    assert first[0] == Decimal("299500")
    assert second == first
    assert resolver.calls == ["005930"]

    client.begin_cycle()
    client._snapshot("005930")
    assert resolver.calls == ["005930", "005930"]


class ContextRevokedClient(FakeClient):
    def __init__(self, runtime_inputs: RuntimeInputs) -> None:
        super().__init__(runtime_inputs)
        self.loads = 0

    def load_inputs(self, rule: ActiveRule) -> RuntimeInputs:
        self.loads += 1
        if self.loads == 1:
            return self.runtime_inputs
        raise RuntimeDataError(
            "TRADING_CONDITIONAL_RULE_NOT_ACTIVE",
            "rule authority changed after trigger claim",
            retryable=False,
            status_code=409,
        )


def test_false_condition_is_recorded_without_trigger() -> None:
    rule = active_rule(threshold="100")
    store = FakeStore(rule)
    client = FakeClient(inputs(price="90"))

    result = ConditionalRuleWorker(store, client).process_once()

    assert result["evaluated"] == 1
    assert store.false == 1
    assert store.claims == 0
    assert client.submit_calls == 0


def test_active_rules_are_evaluated_with_bounded_parallelism() -> None:
    rule = active_rule(threshold="100")
    store = FakeStore(rule)
    store.active = [rule, rule]

    class ParallelClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(inputs(price="90"))
            self.barrier = threading.Barrier(2)
            self.lock = threading.Lock()
            self.active_loads = 0
            self.max_active_loads = 0

        def load_inputs(self, rule: ActiveRule) -> RuntimeInputs:
            del rule
            with self.lock:
                self.active_loads += 1
                self.max_active_loads = max(
                    self.max_active_loads, self.active_loads
                )
            try:
                self.barrier.wait(timeout=2)
                return self.runtime_inputs
            finally:
                with self.lock:
                    self.active_loads -= 1

    client = ParallelClient()
    result = ConditionalRuleWorker(
        store,
        client,
        max_workers=2,
    ).process_once()

    assert result["evaluated"] == 2
    assert result["errors"] == 0
    assert store.false == 2
    assert client.max_active_loads == 2


def test_active_rule_pages_rotate_without_starvation() -> None:
    rule = active_rule(threshold="100")
    store = FakeStore(rule)
    store.active = [rule, rule]
    client = FakeClient(inputs(price="90"))
    worker = ConditionalRuleWorker(store, client, batch_size=1, max_workers=1)

    first = worker.process_once()
    second = worker.process_once()

    assert first["evaluated"] == 1
    assert second["evaluated"] == 1
    assert store.false == 2


def test_true_condition_rechecks_guard_and_submits_existing_paper_lane() -> None:
    rule = active_rule(threshold="100")
    store = FakeStore(rule)
    client = FakeClient(inputs(price="110"))

    result = ConditionalRuleWorker(store, client).process_once()

    assert result["triggered"] == 1
    assert result["submitted"] == 1
    assert store.execution_decisions == [(True, "READY_FOR_PAPER_DIRECTIVE", Decimal("2"))]
    assert client.submit_calls == 1
    assert store.submitted == [UUID("70000000-0000-0000-0000-000000000001")]


def test_market_closed_is_durable_guard_rejection_and_never_submits() -> None:
    rule = active_rule(threshold="100")
    store = FakeStore(rule)
    client = FakeClient(inputs(price="110", market_open=False))

    result = ConditionalRuleWorker(store, client).process_once()

    assert result["triggered"] == 1
    assert result["submitted"] == 0
    assert store.execution_decisions == [(False, "MARKET_CLOSED_NO_ORDER", None)]
    assert client.submit_calls == 0


def test_restart_recovers_claimed_trigger_without_re_evaluating() -> None:
    rule = active_rule(threshold="100")
    store = FakeStore(rule)
    store.active = []
    store.claimed = [(rule, TriggerClaim("trg_recovered", "eval_recovered"))]
    client = FakeClient(inputs(price="110"))

    result = ConditionalRuleWorker(store, client).process_once()

    assert result["claimed_recovered"] == 1
    assert result["submitted"] == 1
    assert store.claims == 0
    assert client.submit_calls == 1


def test_non_retryable_context_change_rejects_claim_without_submission() -> None:
    rule = active_rule(threshold="100")
    store = FakeStore(rule)
    client = ContextRevokedClient(inputs(price="110"))

    result = ConditionalRuleWorker(store, client).process_once()

    assert result["triggered"] == 1
    assert result["submitted"] == 0
    assert store.execution_decisions == [
        (False, "TRADING_CONDITIONAL_RULE_NOT_ACTIVE", None)
    ]
    assert client.submit_calls == 0


def test_trading_conflict_is_terminal_and_preserves_upstream_code(monkeypatch) -> None:
    response = io.BytesIO(
        b'{"error_code":"TRADING_MARKET_SESSION_CLOSED","message":"closed"}'
    )

    def rejected(*args, **kwargs):
        raise urllib.error.HTTPError(
            "http://trading.test/submit",
            409,
            "Conflict",
            {},
            response,
        )

    monkeypatch.setattr("urllib.request.urlopen", rejected)
    client = HttpRuntimeClient(
        trading_api_url="http://trading.test",
        market_api_url="http://market.test",
    )

    with pytest.raises(RuntimeDataError) as raised:
        client._json("http://trading.test/submit", method="POST")

    assert raised.value.code == "TRADING_MARKET_SESSION_CLOSED"
    assert raised.value.retryable is False
