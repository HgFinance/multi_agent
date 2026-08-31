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
    _align_completed_bars,
)
from orchestration.conditional_rules.market_data import (
    MarketPriceResolverError,
    MarketPriceSnapshot,
)
from orchestration.conditional_rules import (
    ActiveRule,
    Candle,
    ConditionalRuleSpec,
    EvaluationContext,
    EvaluationFrame,
    EvaluationError,
    SubmitReadyExecution,
    Timeframe,
    TrailingStopState,
    TriggerClaim,
    advance_trailing_stop,
)
from orchestration.conditional_rules.semantic import trailing_stop_parameters


def _candle(at: datetime, close: str = "100") -> Candle:
    value = Decimal(close)
    return Candle(
        bucket_time=at,
        open=value,
        high=value + Decimal("1"),
        low=value - Decimal("1"),
        close=value,
        volume=Decimal("10"),
    )


def test_multi_timeframe_alignment_excludes_bars_newer_than_primary_close() -> None:
    kst = timezone(timedelta(hours=9))
    at = lambda hour, minute: datetime(2026, 8, 20, hour, minute, tzinfo=kst)
    aligned, watermark = _align_completed_bars(
        {
            Timeframe.M3: [_candle(at(9, 0))],
            Timeframe.M1: [_candle(at(9, minute)) for minute in range(4)],
            Timeframe.M15: [_candle(at(8, 45)), _candle(at(9, 0))],
        },
        primary=Timeframe.M3,
    )

    assert watermark == at(9, 3).astimezone(timezone.utc)
    assert [item.bucket_time for item in aligned[Timeframe.M1]] == [
        at(9, 0),
        at(9, 1),
        at(9, 2),
    ]
    assert [item.bucket_time for item in aligned[Timeframe.M15]] == [at(8, 45)]


NOW = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)


def active_rule(
    *,
    threshold: str = "100",
    side: str = "BUY",
    trailing_drawdown: str | None = None,
    trailing_activation_return: str | None = None,
    trailing_expected_position_quantity: str | None = None,
) -> ActiveRule:
    condition: dict[str, object] = {
        "type": "COMPARISON",
        "operator": "GT",
        "left": {"type": "MARKET", "field": "LAST_PRICE"},
        "right": {"type": "LITERAL", "value": threshold, "unit": "PRICE"},
    }
    if trailing_drawdown is not None:
        parameters: dict[str, str] = {"DRAWDOWN": trailing_drawdown}
        if trailing_activation_return is not None:
            parameters["ACTIVATION_RETURN"] = trailing_activation_return
        if trailing_expected_position_quantity is not None:
            parameters["EXPECTED_POSITION_QUANTITY"] = trailing_expected_position_quantity
        condition = {"type": "TRAILING_STOP", "parameters": parameters}
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
            "condition": condition,
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


def inputs(
    *,
    price: str,
    market_open: bool = True,
    average_entry_price: str = "100",
    position_quantity: str = "10",
    sellable_quantity: str = "10",
    observed: datetime | None = None,
) -> RuntimeInputs:
    observed = observed or datetime.now(timezone.utc)
    frame = EvaluationFrame(
        market={"LAST_PRICE": Decimal(price)},
        portfolio={"AVG_ENTRY_PRICE": Decimal(average_entry_price)},
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
        position_quantity=Decimal(position_quantity),
        sellable_quantity=Decimal(sellable_quantity),
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
        self.submission_acquired = True
        self.trailing_state: TrailingStopState | None = None
        self.entry_trailing_cancellations: list[tuple[Decimal, Decimal]] = []

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

    def observe_trailing_stop(
        self,
        rule: ActiveRule,
        *,
        last_price: Decimal,
        average_entry_price: Decimal,
        observed_at: datetime,
    ):
        observation = advance_trailing_stop(
            self.trailing_state,
            parameters=trailing_stop_parameters(rule.spec.condition),
            last_price=last_price,
            average_entry_price=average_entry_price,
            observed_at=observed_at,
        )
        if not observation.ignored_stale_quote:
            self.trailing_state = observation.state
        return observation

    def cancel_entry_trailing_on_position_mismatch(
        self,
        rule: ActiveRule,
        *,
        expected_position_quantity: Decimal,
        actual_position_quantity: Decimal,
        **_kwargs,
    ) -> bool:
        if self.trailing_state is None:
            return False
        self.entry_trailing_cancellations.append(
            (expected_position_quantity, actual_position_quantity)
        )
        self.active = []
        return True

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

    def mark_submitting(self, rule_execution_id: UUID) -> bool:
        self.submitting.append(rule_execution_id)
        return self.submission_acquired

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


def test_http_runtime_client_passes_canonical_instrument_to_shared_tick_resolver() -> None:
    instrument_id = UUID("40000000-0000-0000-0000-000000000001")

    class InstrumentAwareResolver:
        def __init__(self) -> None:
            self.calls = []

        def snapshot_for_instrument(self, symbol, received_instrument_id):
            self.calls.append((symbol, received_instrument_id))
            return MarketPriceSnapshot(
                symbol=symbol,
                price=Decimal("258000"),
                observed_at=datetime.now(timezone.utc),
                source="test-shared-ls-realtime",
            )

        def snapshot(self, symbol):
            raise AssertionError("shared resolver must receive the instrument id")

    resolver = InstrumentAwareResolver()
    client = HttpRuntimeClient(
        trading_api_url="http://trading.test",
        market_api_url="http://market.test",
        price_resolver=resolver,
    )

    price, _observed_at, _context = client._snapshot("005930", instrument_id)

    assert price == Decimal("258000")
    assert resolver.calls == [("005930", instrument_id)]


def test_shared_tick_gap_uses_cached_existing_paper_quote_adapter() -> None:
    instrument_id = UUID("40000000-0000-0000-0000-000000000002")

    class EmptySharedResolver:
        def snapshot_for_instrument(self, symbol, received_instrument_id):
            raise MarketPriceResolverError(
                "MARKET_PRICE_SHARED_DATA_GAP",
                "no LS realtime tick is available for the instrument",
            )

    fallback = FakePriceResolver(price="70100")
    client = HttpRuntimeClient(
        trading_api_url="http://trading.test",
        market_api_url="http://market.test",
        price_resolver=EmptySharedResolver(),
        fallback_price_resolver=fallback,
    )

    first = client._snapshot("487400", instrument_id)
    client.begin_cycle()
    second = client._snapshot("487400", instrument_id)

    assert first[0] == Decimal("70100")
    assert second == first
    assert fallback.calls == ["487400"]
    assert first[2]["source"] == "test-ls-t1102"


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


def test_single_worker_isolates_one_unexpected_rule_failure_from_later_rules() -> None:
    """A constrained worker must make progress after one malformed runtime call."""

    rule = active_rule(threshold="100")
    store = FakeStore(rule)
    store.active = [rule, rule]

    class OneBadThenHealthyClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(inputs(price="90"))
            self.loads = 0

        def load_inputs(self, received_rule: ActiveRule) -> RuntimeInputs:
            del received_rule
            self.loads += 1
            if self.loads == 1:
                raise RuntimeError("synthetic malformed provider response")
            return self.runtime_inputs

    client = OneBadThenHealthyClient()
    result = ConditionalRuleWorker(
        store,
        client,
        max_workers=1,
    ).process_once()

    assert result["errors"] == 1
    assert result["evaluated"] == 1
    assert store.false == 1
    assert client.loads == 2


def test_insufficient_history_is_backed_off_without_submitting() -> None:
    rule = active_rule()
    store = FakeStore(rule)

    class HistoryGapClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(inputs(price="90"))
            self.loads = 0

        def load_inputs(self, rule: ActiveRule) -> RuntimeInputs:
            del rule
            self.loads += 1
            raise EvaluationError("INSUFFICIENT_HISTORY", "history is not ready")

    client = HistoryGapClient()
    worker = ConditionalRuleWorker(store, client, history_backoff_seconds=300)

    first = worker.process_once()
    second = worker.process_once()

    assert first["errors"] == 1
    assert second["errors"] == 0
    assert second["deferred"] == 1
    assert client.loads == 1
    assert client.submit_calls == 0


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


def test_trailing_stop_arms_after_profit_and_exits_from_durable_high_watermark() -> None:
    rule = active_rule(
        side="SELL",
        trailing_drawdown="0.01",
        trailing_activation_return="0.02",
    )
    store = FakeStore(rule)
    start = datetime.now(timezone.utc)
    client = FakeClient(inputs(price="100", observed=start))
    worker = ConditionalRuleWorker(store, client)

    first = worker.process_once()
    client.runtime_inputs = inputs(price="102", observed=start + timedelta(seconds=1))
    armed = worker.process_once()
    client.runtime_inputs = inputs(price="100.98", observed=start + timedelta(seconds=2))
    exit_result = worker.process_once()

    assert first["triggered"] == 0
    assert armed["triggered"] == 0
    assert exit_result["triggered"] == 1
    assert exit_result["submitted"] == 1
    assert store.trailing_state is not None
    assert store.trailing_state.high_price == Decimal("102")
    assert store.trailing_state.armed_at == start + timedelta(seconds=1)
    assert client.submit_calls == 1


def test_trailing_stop_ignores_late_quote_after_a_newer_high_watermark() -> None:
    rule = active_rule(
        side="SELL",
        trailing_drawdown="0.01",
        trailing_activation_return="0.02",
    )
    start = datetime.now(timezone.utc)
    parameters = trailing_stop_parameters(rule.spec.condition)
    high = advance_trailing_stop(
        None,
        parameters=parameters,
        last_price=Decimal("103"),
        average_entry_price=Decimal("100"),
        observed_at=start,
    )
    newer = advance_trailing_stop(
        high.state,
        parameters=parameters,
        last_price=Decimal("105"),
        average_entry_price=Decimal("100"),
        observed_at=start + timedelta(seconds=1),
    )
    late = advance_trailing_stop(
        newer.state,
        parameters=parameters,
        last_price=Decimal("90"),
        average_entry_price=Decimal("100"),
        observed_at=start,
    )

    assert late.ignored_stale_quote is True
    assert late.condition_result is False
    assert late.state.high_price == Decimal("105")


def test_trailing_stop_does_not_track_before_a_sellable_position_exists() -> None:
    rule = active_rule(side="SELL", trailing_drawdown="0.01")
    store = FakeStore(rule)
    client = FakeClient(
        inputs(price="105", position_quantity="0", sellable_quantity="0")
    )

    result = ConditionalRuleWorker(store, client).process_once()

    assert result["triggered"] == 0
    assert store.false == 1
    assert store.trailing_state is None


def test_trailing_stop_entry_quantity_guard_blocks_mixed_existing_position() -> None:
    rule = active_rule(
        side="SELL",
        trailing_drawdown="0.01",
        trailing_expected_position_quantity="2",
    )
    store = FakeStore(rule)
    client = FakeClient(
        inputs(price="105", position_quantity="4", sellable_quantity="4")
    )

    result = ConditionalRuleWorker(store, client).process_once()

    assert result["triggered"] == 0
    assert store.false == 1
    assert store.trailing_state is None


def test_entry_trailing_stop_cancels_after_started_position_quantity_drift() -> None:
    rule = active_rule(
        side="SELL",
        trailing_drawdown="0.01",
        trailing_activation_return="0.02",
        trailing_expected_position_quantity="2",
    )
    store = FakeStore(rule)
    start = datetime.now(timezone.utc)
    client = FakeClient(inputs(price="103", position_quantity="2", sellable_quantity="2", observed=start))
    worker = ConditionalRuleWorker(store, client)

    started = worker.process_once()
    client.runtime_inputs = inputs(
        price="104",
        position_quantity="3",
        sellable_quantity="3",
        observed=start + timedelta(seconds=1),
    )
    drifted = worker.process_once()
    client.runtime_inputs = inputs(
        price="100",
        position_quantity="2",
        sellable_quantity="2",
        observed=start + timedelta(seconds=2),
    )
    later_match = worker.process_once()

    assert started["cancelled"] == 0
    assert drifted["cancelled"] == 1
    assert store.entry_trailing_cancellations == [(Decimal("2"), Decimal("3"))]
    assert later_match["evaluated"] == 0
    assert client.submit_calls == 0


def test_oco_submission_slot_loser_never_calls_external_trading_api() -> None:
    rule = active_rule(threshold="100")
    store = FakeStore(rule)
    store.submission_acquired = False
    client = FakeClient(inputs(price="110"))
    execution = SubmitReadyExecution(
        rule_execution_id=UUID("60000000-0000-0000-0000-000000000001"),
        trigger_id="trg_test",
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        idempotency_key="rule:test:execution",
    )

    assert not ConditionalRuleWorker(store, client)._submit(execution)
    assert client.submit_calls == 0
    assert store.submitting == [execution.rule_execution_id]


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
