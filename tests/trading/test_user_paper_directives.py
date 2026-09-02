from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest


TRADING_ROOT = Path(__file__).resolve().parents[2] / "departments" / "02-trading"
sys.path.insert(0, str(TRADING_ROOT))

from directives.auth import (  # noqa: E402
    EMPTY_PAYLOAD_SHA256,
    DirectiveAuthError,
    _required_config,
    decode_directive_proof,
)
from directives.contracts import (  # noqa: E402
    DirectiveAction,
    DirectiveLegState,
    DirectiveState,
    UserDirectiveRequest,
)
from directives.market_data import (
    FixtureMarketDataProvider,
    LsPaperFallbackMarketDataProvider,
    MarketDataError,
    TrustedQuote,
)  # noqa: E402
from directives.repository import (  # noqa: E402
    BARRIER_STALE_AFTER,
    DirectiveLeg,
    DirectiveRepositoryError,
    InMemoryDirectiveRepository,
    InstrumentRef,
    PostgresDirectiveRepository,
    _MemoryState,
    _load_driver,
)
from broker.ls_paper_broker import (  # noqa: E402
    KST,
    LSPaperBrokerError,
    LSPaperHolding,
    LSPaperOrderAck,
    LSPaperOrderStatus,
)
import directives.service as directive_service_module  # noqa: E402
from directives.service import DirectiveServiceError, UserDirectiveService  # noqa: E402
from directives.worker import (  # noqa: E402
    _should_log,
    _sleep_seconds,
    run_once as run_directive_worker_once,
)
from api.directive_routes import (  # noqa: E402
    set_directive_service_for_tests,
    submit_user_directive,
)


NOW = datetime(2026, 8, 18, 2, 0, tzinfo=timezone.utc)
SECRET = "unit-test-trading-proof-secret-at-least-32-bytes"
ISSUER = "portfolio-bff"
AUDIENCE = "trading-api"


def test_postgres_driver_adapts_durable_uuid_values() -> None:
    _load_driver()

    from psycopg2.extensions import adapt

    assert adapt(uuid4()).getquoted().startswith(b"'")


def _b64(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _token(claims: dict) -> str:
    header = _b64({"alg": "HS256", "typ": "JWT"})
    payload = _b64(claims)
    signature = hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"Bearer {header}.{payload}.{encoded}"


def _execute_token(request: UserDirectiveRequest, subject: UUID, *, jti: str | None = None, now=NOW) -> str:
    return _token(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": str(subject),
            "fund_id": str(request.fund_id),
            "book_id": str(request.book_id),
            "action": request.action.value,
            "instruction_ref": request.instruction_ref,
            "idempotency_key": request.idempotency_key,
            "payload_sha256": request.payload_sha256(),
            "jti": jti or f"jti-{uuid4()}",
            "iat": now.timestamp(),
            "nbf": now.timestamp(),
            "exp": (now + timedelta(seconds=60)).timestamp(),
            "scope": "trading.user-directive.execute",
        }
    )


def _read_token(record, subject: UUID, *, now=NOW) -> str:
    directive_id = record.directive_id
    return _token(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": str(subject),
            "fund_id": str(record.fund_id),
            "book_id": str(record.book_id),
            "action": "GET_STATUS",
            "instruction_ref": str(directive_id),
            "idempotency_key": f"status:{directive_id}",
            "payload_sha256": EMPTY_PAYLOAD_SHA256,
            "jti": f"read-{uuid4()}",
            "iat": now.timestamp(),
            "nbf": now.timestamp(),
            "exp": (now + timedelta(seconds=60)).timestamp(),
            "scope": "trading.user-directive.read",
        }
    )


def test_stale_tsdb_quote_falls_back_to_fresh_ls_paper_rest_quote():
    instrument = InstrumentRef(uuid4(), "005930", Decimal(1), None, "KRW")

    class StaleProvider:
        def quote(self, *_args, **_kwargs):
            raise MarketDataError(
                "TRADING_MARKET_QUOTE_STALE", "projection is stale", 409
            )

    class Broker:
        def get_quote(self, symbol):
            return {
                "symbol": symbol,
                "observed_at": NOW,
                "bid": Decimal("256000"),
                "ask": Decimal("256500"),
                "bid_size": Decimal("100"),
                "ask_size": Decimal("120"),
            }

    quote = LsPaperFallbackMarketDataProvider(
        StaleProvider(), Broker()
    ).quote(instrument, now=NOW)

    assert quote.symbol == "005930"
    assert quote.bid == Decimal("256000")
    assert quote.ask == Decimal("256500")
    assert quote.source == "ls-paper-rest:t1101"


def test_slow_ls_paper_rest_fallback_is_not_rejected_as_stale():
    """A quote observed after `now` must not read as a future quote.

    t1101 is throttled to about one call per second, so the fallback read
    routinely lands seconds after the caller stamped `now`.  Validating against
    the original stamp made the age negative and tripped the future-skew guard,
    which is how two live conditional rules were rejected on 2026-08-27 while
    holding the freshest quote this deployment can obtain.
    """

    instrument = InstrumentRef(uuid4(), "049080", Decimal(1), None, "KRW")
    fetch_seconds = 3.0

    class UnavailableProvider:
        def quote(self, *_args, **_kwargs):
            raise MarketDataError(
                "TRADING_MARKET_QUOTE_UNAVAILABLE", "no projection row", 503
            )

    class SlowBroker:
        def get_quote(self, symbol):
            return {
                "symbol": symbol,
                # Observed only after the throttled round trip completes.
                "observed_at": NOW + timedelta(seconds=fetch_seconds),
                "bid": Decimal("8820"),
                "ask": Decimal("8830"),
                "bid_size": Decimal("1706"),
                "ask_size": Decimal("209"),
            }

    provider_clock = iter((0.0, fetch_seconds))
    provider = LsPaperFallbackMarketDataProvider(
        UnavailableProvider(), SlowBroker(), monotonic=lambda: next(provider_clock)
    )
    quote = provider.quote(instrument, now=NOW, max_age_seconds=30.0)

    assert quote.source == "ls-paper-rest:t1101"
    assert quote.bid == Decimal("8820")


def test_trusted_conditional_validator_receives_the_admission_quote() -> None:
    """Policy-specific sizing validation runs inside the final quote boundary."""

    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("1000000"))
    request = h.request(DirectiveAction.PLACE_ORDER, key="conditional-quote-validator")
    proof = decode_directive_proof(
        _execute_token(request, h.user), now=NOW.timestamp()
    )
    seen: list[tuple[InstrumentRef, TrustedQuote]] = []

    record = h.service.submit_trusted_rule(
        request,
        proof,
        now=NOW,
        conditional_quantity_validator=lambda instrument, quote: seen.append(
            (instrument, quote)
        ),
    )

    assert record.legs[0].state is DirectiveLegState.FILLED
    assert seen == [(h.instrument, h.market.quotes["005930"])]


class Harness:
    def __init__(self) -> None:
        self.user = uuid4()
        self.fund = uuid4()
        self.book = uuid4()
        self.instrument = InstrumentRef(uuid4(), "005930", Decimal(1), None, "KRW")
        self.repository = InMemoryDirectiveRepository()
        self.repository.grant(self.user, self.fund, self.book)
        self.repository.add_instrument(self.instrument)
        self.repository.set_market_session(NOW - timedelta(hours=1), NOW + timedelta(hours=4))
        self.market = FixtureMarketDataProvider()
        self.market.set_quote(
            TrustedQuote(
                str(self.instrument.instrument_id),
                self.instrument.symbol,
                NOW,
                Decimal("69900"),
                Decimal("70000"),
                Decimal("1000"),
                Decimal("1000"),
                "fixture",
            )
        )
        self.service = UserDirectiveService(self.repository, self.market)

    def request(
        self,
        action: DirectiveAction,
        *,
        key: str | None = None,
        payload: dict | None = None,
    ) -> UserDirectiveRequest:
        if payload is None and action is DirectiveAction.PLACE_ORDER:
            payload = {
                "instrument_id": None,
                "symbol": "005930",
                "side": "BUY",
                "quantity": "1",
                "order_type": "MARKET",
                "limit_price": None,
                "time_in_force": "DAY",
            }
        return UserDirectiveRequest(
            fund_id=self.fund,
            book_id=self.book,
            action=action,
            instruction_ref=f"instruction-{uuid4()}",
            idempotency_key=key or f"idem-{uuid4()}",
            payload=payload or {},
        )


def _basket_payload(*instruments: InstrumentRef, notional_krw: str = "1000000") -> dict:
    return {
        "orders": [
            {
                "instrument_id": str(instrument.instrument_id),
                "symbol": instrument.symbol,
                "notional_krw": notional_krw,
                "side": "BUY",
                "order_type": "MARKET",
                "time_in_force": "DAY",
            }
            for instrument in instruments
        ]
    }


def _member_notional_basket_payload(
    *members: tuple[InstrumentRef, str],
) -> dict:
    return {
        "orders": [
            {
                "instrument_id": str(instrument.instrument_id),
                "symbol": instrument.symbol,
                "notional_krw": notional_krw,
                "quantity": None,
                "side": "BUY",
                "order_type": "MARKET",
                "time_in_force": "DAY",
            }
            for instrument, notional_krw in members
        ]
    }


def _quantity_basket_payload(
    *members: tuple[InstrumentRef, str], side: str = "BUY"
) -> dict:
    return {
        "orders": [
            {
                "instrument_id": str(instrument.instrument_id),
                "symbol": instrument.symbol,
                "notional_krw": None,
                "quantity": quantity,
                "side": side,
                "order_type": "MARKET",
                "time_in_force": "DAY",
            }
            for instrument, quantity in members
        ]
    }


@pytest.fixture(autouse=True)
def _runtime_env(monkeypatch):
    monkeypatch.setenv("TRADING_EXECUTION_MODE", "PAPER")
    monkeypatch.setenv("TRADING_SERVICE_AUTH_SECRET", SECRET)
    monkeypatch.setenv("TRADING_SERVICE_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("TRADING_SERVICE_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("TRADING_SERVICE_AUTH_CLOCK_SKEW_SECONDS", "0")
    monkeypatch.setenv("TRADING_MARKET_QUOTE_MAX_AGE_SECONDS", "10")


@pytest.mark.parametrize(
    "placeholder",
    [
        "CHANGE_ME_CHANGE_ME_CHANGE_ME_CHANGE_ME",
        "replace-with-a-real-secret-before-deploying",
        "example-secret-that-is-long-enough-to-pass",
    ],
)
def test_placeholder_service_secrets_fail_closed(monkeypatch, placeholder):
    monkeypatch.setenv("TRADING_SERVICE_AUTH_SECRET", placeholder)

    with pytest.raises(DirectiveAuthError) as denied:
        _required_config()

    assert denied.value.code == "TRADING_PROOF_AUTH_NOT_CONFIGURED"
    assert denied.value.status_code == 503


def test_execute_binding_one_time_jti_idempotency_and_user_risk_bypass():
    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))
    request = h.request(DirectiveAction.PLACE_ORDER, key="idem-place-0001")
    proof = _execute_token(request, h.user, jti="execute-jti-0001")

    record = h.service.submit(request, proof, now=NOW)
    assert record.state is DirectiveState.IN_PROGRESS
    assert record.error_code == "TRADING_FILL_ACCOUNTING_PENDING"
    assert record.legs[0].state is DirectiveLegState.FILLED
    assert record.legs[0].filled_quantity == Decimal("1")
    assert record.legs[0].expires_at == NOW + timedelta(hours=4)
    assert "risk_decision_id" not in record.view()

    with pytest.raises(DirectiveServiceError, match="already consumed") as replay:
        h.service.submit(request, proof, now=NOW)
    assert replay.value.code == "TRADING_PROOF_REPLAY"

    same = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    assert same.directive_id == record.directive_id
    assert same.state is DirectiveState.IN_PROGRESS
    assert len(same.legs) == 1

    changed = request.model_copy(
        update={"payload": {**request.payload, "quantity": "2"}}
    )
    with pytest.raises(DirectiveServiceError) as conflict:
        h.service.submit(changed, _execute_token(changed, h.user), now=NOW)
    assert conflict.value.code == "TRADING_IDEMPOTENCY_CONFLICT"

    # A worker retry before accounting acknowledgement must be harmless and
    # must not manufacture a second Fill from the already-consumed quote.
    pending = run_directive_worker_once(h.service, batch=100, now=NOW)
    assert pending == {
        "reconciled": 1,
        "deferred": [],
        "errors": [],
        "at": NOW.isoformat(),
    }
    assert record.state is DirectiveState.IN_PROGRESS
    assert len(h.repository.state.direct_fills) == 1

    # Model a consumer crash/retry at the idempotent acknowledgement boundary.
    h.repository.acknowledge_direct_fills(record.legs[0].leg_id)
    h.repository.acknowledge_direct_fills(record.legs[0].leg_id)
    finalized = run_directive_worker_once(h.service, batch=100, now=NOW)
    assert finalized["reconciled"] == 1
    assert finalized["errors"] == []
    assert record.state is DirectiveState.COMPLETED
    assert record.error_code is None
    assert len(h.repository.state.direct_fills) == 1
    assert run_directive_worker_once(h.service, batch=100, now=NOW)["reconciled"] == 0


def test_basket_preflight_advances_injected_quote_clock(monkeypatch):
    h = Harness()
    second = InstrumentRef(uuid4(), "000660", Decimal(1), None, "KRW")
    third = InstrumentRef(uuid4(), "051910", Decimal(1), None, "KRW")
    for instrument in (second, third):
        h.repository.add_instrument(instrument)

    class SequentialMarket:
        def __init__(self):
            self.quote_times: list[datetime] = []
            self.asks = {
                "005930": Decimal("70000"),
                "000660": Decimal("25000"),
                "051910": Decimal("300000"),
            }

        def quote(self, instrument, *, now, max_age_seconds=None):
            index = len(self.quote_times)
            self.quote_times.append(now)
            observed_at = NOW + timedelta(seconds=3 * index)
            if now < observed_at - timedelta(seconds=2):
                raise MarketDataError(
                    "TRADING_MARKET_QUOTE_STALE",
                    "sequential quote clock did not advance",
                    409,
                )
            ask = self.asks[instrument.symbol]
            return TrustedQuote(
                str(instrument.instrument_id),
                instrument.symbol,
                observed_at,
                ask - Decimal("100"),
                ask,
                Decimal("1000"),
                Decimal("1000"),
                "sequential-fixture",
            )

    market = SequentialMarket()
    clock = iter((0.0, 0.0, 3.0, 6.0))
    monkeypatch.setattr(
        directive_service_module.time, "monotonic", lambda: next(clock)
    )
    h.service = UserDirectiveService(h.repository, market)
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("5000000"))
    request = h.request(
        DirectiveAction.PLACE_BASKET,
        key="idem-basket-sequential-quote-clock-0001",
        payload=_basket_payload(h.instrument, second, third),
    )

    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert len(market.quote_times) == 3
    assert market.quote_times[0] == NOW
    assert market.quote_times[1] == NOW + timedelta(seconds=3)
    assert market.quote_times[2] == NOW + timedelta(seconds=6)
    assert len(record.legs) == 3


def test_basket_quote_clock_includes_book_guard_wait(monkeypatch):
    """A fresh fallback quote must survive time spent waiting on the book.

    ``submit`` stamps the admission time before the durable book guard.  The
    prior basket clock started only after that guard, so a two-second lock wait
    plus a throttled t1101 read made the just-received quote appear too far in
    the future and surfaced as TRADING_MARKET_QUOTE_STALE.
    """

    h = Harness()
    clock = {"seconds": 0.0}
    monkeypatch.setattr(
        directive_service_module.time,
        "monotonic",
        lambda: clock["seconds"],
    )
    original_book_guard = h.repository.book_guard

    @contextmanager
    def delayed_book_guard(fund_id, book_id):
        with original_book_guard(fund_id, book_id):
            clock["seconds"] = 3.0
            yield

    monkeypatch.setattr(h.repository, "book_guard", delayed_book_guard)

    class FreshAfterWaitMarket:
        def __init__(self):
            self.quote_times: list[datetime] = []

        def quote(self, instrument, *, now, max_age_seconds=None):
            self.quote_times.append(now)
            observed_at = NOW + timedelta(seconds=3)
            if now < observed_at - timedelta(seconds=2):
                raise MarketDataError(
                    "TRADING_MARKET_QUOTE_STALE",
                    "fresh quote was compared with the pre-lock clock",
                    409,
                )
            return TrustedQuote(
                str(instrument.instrument_id),
                instrument.symbol,
                observed_at,
                Decimal("69900"),
                Decimal("70000"),
                Decimal("1000"),
                Decimal("1000"),
                "fresh-after-book-wait",
            )

    market = FreshAfterWaitMarket()
    h.service = UserDirectiveService(h.repository, market)
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("1000000"))
    request = h.request(
        DirectiveAction.PLACE_BASKET,
        key="idem-basket-book-wait-clock-0001",
        payload=_quantity_basket_payload((h.instrument, "1"), side="BUY"),
    )

    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert market.quote_times == [NOW + timedelta(seconds=3)]
    assert len(record.legs) == 1


def test_same_notional_paper_basket_preflights_all_members_and_tracks_all_legs():
    h = Harness()
    second = InstrumentRef(uuid4(), "000660", Decimal(1), None, "KRW")
    h.repository.add_instrument(second)
    h.market.set_quote(
        TrustedQuote(
            str(second.instrument_id),
            second.symbol,
            NOW,
            Decimal("24900"),
            Decimal("25000"),
            Decimal("1000"),
            Decimal("1000"),
            "fixture",
        )
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("3000000"))
    request = h.request(
        DirectiveAction.PLACE_BASKET,
        key="idem-basket-preflight-0001",
        payload=_basket_payload(h.instrument, second),
    )

    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert record.action is DirectiveAction.PLACE_BASKET
    assert record.state is DirectiveState.IN_PROGRESS
    assert record.error_code == "TRADING_FILL_ACCOUNTING_PENDING"
    assert [(leg.symbol, leg.requested_quantity) for leg in record.legs] == [
        ("005930", Decimal("14")),
        ("000660", Decimal("40")),
    ]
    assert all(leg.state is DirectiveLegState.FILLED for leg in record.legs)
    assert len(h.repository.state.direct_fills) == 2


def test_exact_krx_market_cap_query_completes_ten_isolated_paper_buy_legs_e2e():
    """Exact reported query reaches the existing in-memory PAPER fill boundary."""

    from orchestration.contracts.user_paper_order import VerifiedPaperDirective
    from orchestration.dynamic_universe_orders import (
        expand_to_basket_instruction,
        parse_dynamic_universe_order,
    )
    from orchestration.user_order_language import (
        deterministic_order_candidate,
        verify_order_candidate,
    )

    raw_query = (
        "현재 KRX 시가총액 상위 10개 종목을 각각 최대 300만원씩 "
        "PAPER 시장가로 매수해줘."
    )
    top_ten = (
        ("005930", "삼성전자", Decimal("70000")),
        ("000660", "SK하이닉스", Decimal("100000")),
        ("005935", "삼성전자우", Decimal("150000")),
        ("402340", "SK스퀘어", Decimal("200000")),
        ("009150", "삼성전기", Decimal("250000")),
        ("207940", "삼성바이오로직스", Decimal("300000")),
        ("373220", "LG에너지솔루션", Decimal("500000")),
        ("012450", "한화에어로스페이스", Decimal("750000")),
        ("105560", "KB금융", Decimal("1000000")),
        ("329180", "HD현대중공업", Decimal("1500000")),
    )

    plan = parse_dynamic_universe_order(raw_query)
    assert plan is not None
    assert plan.market_scope == "KRX"
    expanded = expand_to_basket_instruction(
        plan,
        [{"symbol": symbol, "name": name} for symbol, name, _ in top_ten],
    )
    assert expanded is not None
    candidate = deterministic_order_candidate(expanded)
    assert candidate is not None
    verified = verify_order_candidate(expanded, candidate)
    assert isinstance(verified, VerifiedPaperDirective)
    assert len(verified.payload.orders) == 10

    h = Harness()
    catalog = {h.instrument.symbol: h.instrument}
    asks = {symbol: ask for symbol, _, ask in top_ten}
    for symbol, _name, ask in top_ten[1:]:
        instrument = InstrumentRef(uuid4(), symbol, Decimal(1), None, "KRW")
        catalog[symbol] = instrument
        h.repository.add_instrument(instrument)
        h.market.set_quote(
            TrustedQuote(
                str(instrument.instrument_id),
                symbol,
                NOW,
                ask - Decimal("100"),
                ask,
                Decimal("1000"),
                Decimal("1000"),
                "isolated-e2e-fixture",
            )
        )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("35000000"))
    payload = {
        "orders": [
            {
                "instrument_id": str(catalog[item.instrument_mention].instrument_id),
                "symbol": item.instrument_mention,
                "notional_krw": item.notional_krw,
                "quantity": None,
                "side": item.side.value,
                "order_type": item.order_type.value,
                "time_in_force": item.time_in_force,
            }
            for item in verified.payload.orders
        ]
    }
    request = UserDirectiveRequest(
        fund_id=h.fund,
        book_id=h.book,
        action=DirectiveAction.PLACE_BASKET,
        instruction_ref="exact-krx-market-cap-e2e",
        idempotency_key="exact-krx-market-cap-e2e-0001",
        payload=payload,
    )

    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert record.action is DirectiveAction.PLACE_BASKET
    assert record.state is DirectiveState.IN_PROGRESS
    assert len(record.legs) == 10
    assert all(leg.state is DirectiveLegState.FILLED for leg in record.legs)
    assert all(
        leg.requested_quantity * asks[str(leg.symbol)] <= Decimal("3000000")
        for leg in record.legs
    )
    assert len(h.repository.state.direct_fills) == 10

    for leg in record.legs:
        h.repository.acknowledge_direct_fills(leg.leg_id)
    outcome = run_directive_worker_once(h.service, batch=100, now=NOW)

    assert outcome["errors"] == []
    assert record.state is DirectiveState.COMPLETED
    assert len(record.legs) == 10


def test_member_notionals_preserve_each_buy_ceiling_before_basket_submission():
    h = Harness()
    second = InstrumentRef(uuid4(), "000660", Decimal(1), None, "KRW")
    h.repository.add_instrument(second)
    h.market.set_quote(
        TrustedQuote(
            str(second.instrument_id),
            second.symbol,
            NOW,
            Decimal("24900"),
            Decimal("25000"),
            Decimal("1000"),
            Decimal("1000"),
            "fixture",
        )
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("2000000"))
    request = h.request(
        DirectiveAction.PLACE_BASKET,
        key="idem-basket-member-notional-0001",
        payload=_member_notional_basket_payload(
            (h.instrument, "1000000"),
            (second, "500000"),
        ),
    )

    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert [(leg.symbol, leg.requested_quantity) for leg in record.legs] == [
        ("005930", Decimal("14")),
        ("000660", Decimal("20")),
    ]
    assert all(leg.state is DirectiveLegState.FILLED for leg in record.legs)


def test_explicit_quantity_sell_basket_preflights_all_positions_and_tracks_legs():
    h = Harness()
    second = InstrumentRef(uuid4(), "000660", Decimal(1), None, "KRW")
    h.repository.add_instrument(second)
    h.market.set_quote(
        TrustedQuote(
            str(second.instrument_id),
            second.symbol,
            NOW,
            Decimal("24900"),
            Decimal("25000"),
            Decimal("1000"),
            Decimal("1000"),
            "fixture",
        )
    )
    h.repository.set_position(h.fund, h.book, h.instrument.instrument_id, Decimal("5"))
    h.repository.set_position(h.fund, h.book, second.instrument_id, Decimal("4"))
    request = h.request(
        DirectiveAction.PLACE_BASKET,
        key="idem-basket-sell-quantity-0001",
        payload=_quantity_basket_payload(
            (h.instrument, "3"), (second, "2"), side="SELL"
        ),
    )

    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert record.action is DirectiveAction.PLACE_BASKET
    assert [(leg.symbol, leg.side, leg.requested_quantity, leg.reduce_only) for leg in record.legs] == [
        ("005930", "SELL", Decimal("3"), True),
        ("000660", "SELL", Decimal("2"), True),
    ]
    assert all(leg.state is DirectiveLegState.FILLED for leg in record.legs)


def test_quantity_sell_basket_does_not_create_a_first_leg_when_any_member_is_unsellable():
    h = Harness()
    second = InstrumentRef(uuid4(), "000660", Decimal(1), None, "KRW")
    h.repository.add_instrument(second)
    h.market.set_quote(
        TrustedQuote(
            str(second.instrument_id),
            second.symbol,
            NOW,
            Decimal("24900"),
            Decimal("25000"),
            Decimal("1000"),
            Decimal("1000"),
            "fixture",
        )
    )
    h.repository.set_position(h.fund, h.book, h.instrument.instrument_id, Decimal("5"))
    h.repository.set_position(h.fund, h.book, second.instrument_id, Decimal("1"))
    request = h.request(
        DirectiveAction.PLACE_BASKET,
        key="idem-basket-sell-unsellable-0001",
        payload=_quantity_basket_payload(
            (h.instrument, "3"), (second, "2"), side="SELL"
        ),
    )

    with pytest.raises(DirectiveServiceError) as denied:
        h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert denied.value.code == "TRADING_INSUFFICIENT_SELLABLE_POSITION"
    directive_id = h.repository.state.idempotency[
        (h.user, h.fund, h.book, request.idempotency_key)
    ]
    record = h.repository.get(directive_id)
    assert record is not None
    assert record.legs == []


def test_basket_rejects_an_unaffordable_member_before_creating_any_leg():
    h = Harness()
    second = InstrumentRef(uuid4(), "000660", Decimal(1), None, "KRW")
    h.repository.add_instrument(second)
    h.market.set_quote(
        TrustedQuote(
            str(second.instrument_id),
            second.symbol,
            NOW,
            Decimal("1990000"),
            Decimal("2000000"),
            Decimal("1000"),
            Decimal("1000"),
            "fixture",
        )
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("5000000"))
    request = h.request(
        DirectiveAction.PLACE_BASKET,
        key="idem-basket-small-0001",
        payload=_basket_payload(h.instrument, second),
    )

    with pytest.raises(DirectiveServiceError) as denied:
        h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert denied.value.code == "TRADING_BASKET_NOTIONAL_TOO_SMALL"
    directive_id = h.repository.state.idempotency[
        (h.user, h.fund, h.book, request.idempotency_key)
    ]
    record = h.repository.get(directive_id)
    assert record is not None
    assert record.state is DirectiveState.FAILED
    assert record.legs == []


def test_external_basket_stops_before_unattempted_member_after_terminal_result():
    class Broker:
        def __init__(self) -> None:
            self.placements: list[str] = []

        def place_order(self, *, symbol, **_kwargs):
            self.placements.append(symbol)
            if symbol == "000660":
                raise LSPaperBrokerError("LS_PAPER_REJECTED", "broker rejected")
            return LSPaperOrderAck(symbol + "-order", "111951000", symbol)

        def order_status(self, broker_order_id, *, order_date=None):
            symbol = broker_order_id.removesuffix("-order")
            return LSPaperOrderStatus(
                broker_order_id=broker_order_id,
                state="FILLED",
                requested_quantity=Decimal("14"),
                filled_quantity=Decimal("14"),
                fill_price=Decimal("70000"),
                order_date=NOW.date(),
            )

    h = Harness()
    second = InstrumentRef(uuid4(), "000660", Decimal(1), None, "KRW")
    third = InstrumentRef(uuid4(), "051910", Decimal(1), None, "KRW")
    for instrument, ask in ((second, Decimal("25000")), (third, Decimal("300000"))):
        h.repository.add_instrument(instrument)
        h.market.set_quote(
            TrustedQuote(
                str(instrument.instrument_id),
                instrument.symbol,
                NOW,
                ask - Decimal("100"),
                ask,
                Decimal("1000"),
                Decimal("1000"),
                "fixture",
            )
        )
    broker = Broker()
    h.service = UserDirectiveService(
        h.repository, h.market, external_broker=broker  # type: ignore[arg-type]
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("5000000"))
    request = h.request(
        DirectiveAction.PLACE_BASKET,
        key="idem-basket-stop-0001",
        payload=_basket_payload(h.instrument, second, third),
    )

    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert broker.placements == ["005930", "000660"]
    assert [leg.symbol for leg in record.legs] == ["005930", "000660"]
    assert record.state is DirectiveState.PARTIAL
    assert record.error_code == "TRADING_BASKET_SUBMISSION_STOPPED"


def test_ls_paper_sell_all_sizes_each_child_from_current_broker_holdings() -> None:
    class Broker:
        def __init__(self) -> None:
            self.holdings_reads = 0
            self.placements: list[tuple[str, str, Decimal]] = []
            self.quantities: dict[str, Decimal] = {}

        def get_holdings(self):
            self.holdings_reads += 1
            return (
                LSPaperHolding("006400", "삼성SDI", Decimal("10"), Decimal("10")),
                LSPaperHolding("005930", "삼성전자", Decimal("30"), Decimal("29")),
                LSPaperHolding("000660", "SK하이닉스", Decimal("6"), Decimal("6")),
            )

        def place_order(self, **kwargs):
            symbol = kwargs["symbol"]
            quantity = Decimal(kwargs["quantity"])
            self.placements.append((symbol, kwargs["side"], quantity))
            self.quantities[symbol] = quantity
            return LSPaperOrderAck(f"order-{symbol}", "090000", symbol)

        def order_status(self, broker_order_id, *, order_date=None):
            symbol = broker_order_id.removeprefix("order-")
            return LSPaperOrderStatus(
                broker_order_id=broker_order_id,
                state="ACKNOWLEDGED",
                requested_quantity=self.quantities[symbol],
                filled_quantity=Decimal("0"),
                fill_price=None,
                order_date=NOW.date(),
            )

    h = Harness()
    second = InstrumentRef(uuid4(), "000660", Decimal(1), None, "KRW")
    third = InstrumentRef(uuid4(), "006400", Decimal(1), None, "KRW")
    h.repository.add_instrument(second)
    h.repository.add_instrument(third)
    # Deliberately make the local projection stale and incomplete.
    h.repository.set_position(h.fund, h.book, h.instrument.instrument_id, Decimal("1"))
    broker = Broker()
    h.service = UserDirectiveService(
        h.repository, h.market, external_broker=broker  # type: ignore[arg-type]
    )

    request = h.request(DirectiveAction.SELL_ALL, key="idem-sell-all-broker-snapshot")
    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert broker.holdings_reads == 1
    assert broker.placements == [
        ("000660", "SELL", Decimal("6")),
        ("005930", "SELL", Decimal("29")),
        ("006400", "SELL", Decimal("10")),
    ]
    assert [leg.symbol for leg in record.legs] == ["000660", "005930", "006400"]
    assert all(leg.reduce_only for leg in record.legs)
    assert record.state is DirectiveState.IN_PROGRESS


def test_ls_paper_adapter_uses_broker_fill_and_never_resubmits() -> None:
    class Broker:
        def __init__(self) -> None:
            self.placements = 0

        def place_order(self, **_kwargs):
            self.placements += 1
            return LSPaperOrderAck("6439", "111951000", "005930")

        def order_status(self, broker_order_id, *, order_date=None):
            assert broker_order_id == "6439"
            return LSPaperOrderStatus(
                broker_order_id="6439",
                state="FILLED",
                requested_quantity=Decimal(1),
                filled_quantity=Decimal(1),
                fill_price=Decimal(70000),
                order_date=NOW.date(),
            )

    h = Harness()
    broker = Broker()
    h.service = UserDirectiveService(
        h.repository,
        h.market,
        external_broker=broker,  # type: ignore[arg-type]
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))
    request = h.request(DirectiveAction.PLACE_ORDER, key="idem-ls-paper-0001")

    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert broker.placements == 1
    assert record.legs[0].broker_order_id == "ls-paper:6439"
    assert record.legs[0].state is DirectiveLegState.FILLED
    fill = next(iter(h.repository.state.direct_fills.values()))
    assert fill.source == "ls-paper:CSPAQ13700"

    same = h.service.submit(
        request,
        _execute_token(request, h.user),
        now=NOW,
    )
    assert same.directive_id == record.directive_id
    assert broker.placements == 1
    assert len(h.repository.state.direct_fills) == 1


def test_ls_paper_read_only_status_recovers_unknown_acknowledged_leg_to_fill() -> None:
    class Broker:
        state = "ACKNOWLEDGED"

        def __init__(self) -> None:
            self.placements = 0

        def place_order(self, **_kwargs):
            self.placements += 1
            return LSPaperOrderAck("7935", "090000", "005930")

        def order_status(self, broker_order_id, *, order_date=None):
            assert broker_order_id == "7935"
            filled = Decimal(1) if self.state == "FILLED" else Decimal(0)
            return LSPaperOrderStatus(
                broker_order_id="7935",
                state=self.state,
                requested_quantity=Decimal(1),
                filled_quantity=filled,
                fill_price=Decimal(70000) if filled else None,
                order_date=NOW.date(),
            )

    h = Harness()
    broker = Broker()
    h.service = UserDirectiveService(
        h.repository,
        h.market,
        external_broker=broker,  # type: ignore[arg-type]
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))
    request = h.request(DirectiveAction.PLACE_ORDER, key="idem-ls-paper-recover-unknown")
    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    leg = h.repository.mark_broker_leg_unknown(
        record,
        record.legs[0],
        error_code="LS_PAPER_CANCEL_AMBIGUOUS",
        error_message="cancel outcome unknown",
    )
    assert leg.state is DirectiveLegState.UNKNOWN

    broker.state = "FILLED"
    recovered = h.service.get_status(
        record.directive_id,
        _read_token(record, h.user),
        now=NOW,
    )

    assert broker.placements == 1
    assert recovered.legs[0].state is DirectiveLegState.FILLED
    assert recovered.legs[0].filled_quantity == Decimal(1)
    assert recovered.legs[0].error_code is None
    assert len(h.repository.state.direct_fills) == 1


def test_ls_paper_ambiguous_submission_stays_unknown_without_retry() -> None:
    class Broker:
        def __init__(self) -> None:
            self.placements = 0

        def place_order(self, **_kwargs):
            self.placements += 1
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_AMBIGUOUS",
                "transport failed",
                ambiguous=True,
            )

        def order_status(self, _broker_order_id, *, order_date=None):
            raise AssertionError("an order without broker id cannot be queried")

    h = Harness()
    broker = Broker()
    h.service = UserDirectiveService(
        h.repository,
        h.market,
        external_broker=broker,  # type: ignore[arg-type]
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))
    request = h.request(DirectiveAction.PLACE_ORDER, key="idem-ls-paper-unknown")

    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    assert record.state is DirectiveState.UNKNOWN
    assert record.legs[0].state is DirectiveLegState.UNKNOWN
    assert broker.placements == 1
    assert h.repository.active_directives(limit=100) == []

    run_directive_worker_once(h.service, batch=100, now=NOW)
    assert broker.placements == 1
    assert len(h.repository.state.direct_fills) == 0


def test_ls_paper_unexpected_placement_error_returns_durable_unknown() -> None:
    class Broker:
        def __init__(self) -> None:
            self.placements = 0

        def place_order(self, **_kwargs):
            self.placements += 1
            raise RuntimeError("unexpected adapter failure")

        def order_status(self, _broker_order_id, *, order_date=None):
            raise AssertionError("an order without broker id cannot be queried")

    h = Harness()
    broker = Broker()
    h.service = UserDirectiveService(
        h.repository,
        h.market,
        external_broker=broker,  # type: ignore[arg-type]
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))
    request = h.request(
        DirectiveAction.PLACE_ORDER,
        key="idem-ls-paper-unexpected-placement",
    )

    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert broker.placements == 1
    assert record.state is DirectiveState.UNKNOWN
    assert record.legs[0].state is DirectiveLegState.UNKNOWN
    assert record.legs[0].error_code == "LS_PAPER_ORDER_AMBIGUOUS_INTERNAL"
    assert h.repository.active_directives(limit=100) == []


def test_ls_paper_closing_auction_fill_is_reconciled_before_local_expiry() -> None:
    class Broker:
        filled = False

        def place_order(self, **_kwargs):
            return LSPaperOrderAck("17566", "152759000", "005930")

        def order_status(self, _broker_order_id, *, order_date=None):
            return LSPaperOrderStatus(
                broker_order_id="17566",
                state="FILLED" if self.filled else "ACKNOWLEDGED",
                requested_quantity=Decimal(1),
                filled_quantity=Decimal(1 if self.filled else 0),
                fill_price=Decimal("271000") if self.filled else None,
                order_date=NOW.date(),
                last_execution_at=(NOW + timedelta(hours=5)) if self.filled else None,
            )

    h = Harness()
    broker = Broker()
    h.service = UserDirectiveService(
        h.repository,
        h.market,
        external_broker=broker,  # type: ignore[arg-type]
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("500000"))
    h.market.set_quote(
        TrustedQuote(
            str(h.instrument.instrument_id),
            "005930",
            NOW,
            Decimal("270500"),
            Decimal("271000"),
            Decimal("1000"),
            Decimal("1000"),
            "fixture",
        )
    )
    request = h.request(
        DirectiveAction.PLACE_ORDER,
        key="idem-ls-paper-closing-auction",
    )
    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    broker.filled = True

    reconciled, errors = h.service.reconcile_active(now=NOW + timedelta(hours=5))

    assert errors == []
    observed = next(row for row in reconciled if row.directive_id == record.directive_id)
    assert observed.legs[0].state is DirectiveLegState.FILLED
    assert observed.legs[0].filled_quantity == Decimal(1)
    assert observed.legs[0].average_fill_price == Decimal("271000")
    assert observed.error_code == "TRADING_FILL_ACCOUNTING_PENDING"


def test_ls_paper_unfilled_day_order_expires_after_broker_grace() -> None:
    class Broker:
        def place_order(self, **_kwargs):
            return LSPaperOrderAck("17567", "152759000", "005930")

        def order_status(self, _broker_order_id, *, order_date=None):
            return LSPaperOrderStatus(
                broker_order_id="17567",
                state="ACKNOWLEDGED",
                requested_quantity=Decimal(1),
                filled_quantity=Decimal(0),
                fill_price=None,
                order_date=NOW.date(),
            )

    h = Harness()
    h.service = UserDirectiveService(
        h.repository,
        h.market,
        external_broker=Broker(),  # type: ignore[arg-type]
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("500000"))
    request = h.request(
        DirectiveAction.PLACE_ORDER,
        key="idem-ls-paper-close-grace-expiry",
    )
    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    expires_at = record.legs[0].expires_at
    assert expires_at is not None

    h.service.reconcile_active(now=expires_at + timedelta(seconds=121))

    assert record.state is DirectiveState.FAILED
    assert record.legs[0].state is DirectiveLegState.EXPIRED
    assert record.legs[0].error_code == "TRADING_PAPER_ORDER_EXPIRED"


@pytest.mark.parametrize("historical_result", ["query_rejected", "missing"])
def test_ls_paper_historical_day_order_expires_when_status_is_unavailable(
    historical_result: str,
) -> None:
    class Broker:
        historical = False

        def place_order(self, **_kwargs):
            return LSPaperOrderAck("17568", "152759000", "005930")

        def order_status(self, _broker_order_id, *, order_date=None):
            if self.historical:
                if historical_result == "query_rejected":
                    raise LSPaperBrokerError(
                        "LS_PAPER_QUERY_REJECTED", "historical query rejected"
                    )
                return None
            return LSPaperOrderStatus(
                broker_order_id="17568",
                state="ACKNOWLEDGED",
                requested_quantity=Decimal(1),
                filled_quantity=Decimal(0),
                fill_price=None,
                order_date=NOW.date(),
            )

    h = Harness()
    broker = Broker()
    h.service = UserDirectiveService(
        h.repository,
        h.market,
        external_broker=broker,  # type: ignore[arg-type]
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("500000"))
    request = h.request(
        DirectiveAction.PLACE_ORDER,
        key=f"idem-ls-paper-historical-{historical_result}",
    )
    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    expires_at = record.legs[0].expires_at
    assert expires_at is not None
    broker.historical = True

    h.service.reconcile_active(now=expires_at + timedelta(seconds=121))

    assert record.state is DirectiveState.FAILED
    assert record.legs[0].state is DirectiveLegState.EXPIRED
    assert record.legs[0].error_code == "TRADING_PAPER_ORDER_EXPIRED"
    assert h.repository.state.barriers == {}


def test_ls_paper_transient_status_error_does_not_expire_acknowledged_leg() -> None:
    class Broker:
        transient_error = False

        def place_order(self, **_kwargs):
            return LSPaperOrderAck("17569", "152759000", "005930")

        def order_status(self, _broker_order_id, *, order_date=None):
            if self.transient_error:
                raise LSPaperBrokerError("LS_PAPER_HTTP_ERROR", "temporary outage")
            return LSPaperOrderStatus(
                broker_order_id="17569",
                state="ACKNOWLEDGED",
                requested_quantity=Decimal(1),
                filled_quantity=Decimal(0),
                fill_price=None,
                order_date=NOW.date(),
            )

    h = Harness()
    broker = Broker()
    h.service = UserDirectiveService(
        h.repository,
        h.market,
        external_broker=broker,  # type: ignore[arg-type]
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("500000"))
    request = h.request(
        DirectiveAction.PLACE_ORDER, key="idem-ls-paper-transient-query"
    )
    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    expires_at = record.legs[0].expires_at
    assert expires_at is not None
    broker.transient_error = True

    h.service.reconcile_active(now=expires_at + timedelta(seconds=121))

    assert record.state is DirectiveState.IN_PROGRESS
    assert record.legs[0].state is DirectiveLegState.ACKNOWLEDGED


def test_ls_paper_sell_all_releases_barrier_after_historical_day_expiry() -> None:
    class Broker:
        historical = False
        holdings_empty = False
        placements = 0

        def get_holdings(self):
            if self.holdings_empty:
                return ()
            return (LSPaperHolding("005930", "삼성전자", Decimal(1), Decimal(1)),)

        def place_order(self, **_kwargs):
            self.placements += 1
            return LSPaperOrderAck("17570", "152759000", "005930")

        def order_status(self, _broker_order_id, *, order_date=None):
            if self.historical:
                raise LSPaperBrokerError(
                    "LS_PAPER_QUERY_REJECTED", "historical query rejected"
                )
            return LSPaperOrderStatus(
                broker_order_id="17570",
                state="ACKNOWLEDGED",
                requested_quantity=Decimal(1),
                filled_quantity=Decimal(0),
                fill_price=None,
                order_date=NOW.date(),
            )

    h = Harness()
    broker = Broker()
    h.service = UserDirectiveService(
        h.repository,
        h.market,
        external_broker=broker,  # type: ignore[arg-type]
    )
    request = h.request(
        DirectiveAction.SELL_ALL, key="idem-ls-sell-all-historical-expiry"
    )
    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    expires_at = record.legs[0].expires_at
    assert expires_at is not None
    assert h.repository.state.barriers
    broker.historical = True
    broker.holdings_empty = True

    h.service.reconcile_active(now=expires_at + timedelta(seconds=121))

    assert record.state is DirectiveState.COMPLETED
    assert record.legs[0].state is DirectiveLegState.EXPIRED
    assert broker.placements == 1
    assert h.repository.state.barriers == {}


def test_ls_paper_cancel_all_calls_broker_before_terminal_projection() -> None:
    class Broker:
        def __init__(self) -> None:
            self.placements = 0
            self.cancellations = 0

        def place_order(self, **_kwargs):
            self.placements += 1
            return LSPaperOrderAck("6439", "111951000", "005930")

        def order_status(self, broker_order_id, *, order_date=None):
            assert broker_order_id == "6439"
            return LSPaperOrderStatus(
                broker_order_id="6439",
                state="ACKNOWLEDGED",
                requested_quantity=Decimal(1),
                filled_quantity=Decimal(0),
                fill_price=None,
                order_date=NOW.date(),
            )

        def cancel_order(self, **kwargs):
            assert kwargs == {
                "broker_order_id": "6439",
                "symbol": "005930",
                "quantity": Decimal(1),
            }
            self.cancellations += 1
            return LSPaperOrderAck("6440", "112001000", "005930")

    h = Harness()
    broker = Broker()
    h.service = UserDirectiveService(
        h.repository,
        h.market,
        external_broker=broker,  # type: ignore[arg-type]
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))
    place = h.request(
        DirectiveAction.PLACE_ORDER,
        key="idem-ls-paper-cancel-target",
        payload={
            "instrument_id": str(h.instrument.instrument_id),
            "symbol": "005930",
            "side": "BUY",
            "quantity": "1",
            "order_type": "LIMIT",
            "limit_price": "69000",
            "time_in_force": "DAY",
        },
    )
    placed = h.service.submit(place, _execute_token(place, h.user), now=NOW)
    assert placed.legs[0].state is DirectiveLegState.ACKNOWLEDGED

    cancel = h.request(DirectiveAction.CANCEL_ALL, key="idem-ls-paper-cancel-all")
    cancelled = h.service.submit(cancel, _execute_token(cancel, h.user), now=NOW)

    assert broker.placements == 1
    assert broker.cancellations == 1
    assert cancelled.state is DirectiveState.COMPLETED
    assert cancelled.legs[0].state is DirectiveLegState.CANCELLED
    assert cancelled.legs[0].broker_order_id == "ls-paper-cancel:6440"
    assert placed.legs[0].state is DirectiveLegState.CANCELLED
    assert placed.state is DirectiveState.FAILED


def test_ls_paper_ambiguous_cancel_is_never_reissued() -> None:
    class Broker:
        def __init__(self) -> None:
            self.cancellations = 0

        def place_order(self, **_kwargs):
            return LSPaperOrderAck("6439", "111951000", "005930")

        def order_status(self, _broker_order_id, *, order_date=None):
            return LSPaperOrderStatus(
                broker_order_id="6439",
                state="ACKNOWLEDGED",
                requested_quantity=Decimal(1),
                filled_quantity=Decimal(0),
                fill_price=None,
                order_date=NOW.date(),
            )

        def cancel_order(self, **_kwargs):
            self.cancellations += 1
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_AMBIGUOUS",
                "transport failed",
                ambiguous=True,
            )

    h = Harness()
    broker = Broker()
    h.service = UserDirectiveService(
        h.repository,
        h.market,
        external_broker=broker,  # type: ignore[arg-type]
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))
    place = h.request(
        DirectiveAction.PLACE_ORDER,
        key="idem-ls-paper-ambiguous-cancel-target",
        payload={
            "instrument_id": str(h.instrument.instrument_id),
            "symbol": "005930",
            "side": "BUY",
            "quantity": "1",
            "order_type": "LIMIT",
            "limit_price": "69000",
            "time_in_force": "DAY",
        },
    )
    h.service.submit(place, _execute_token(place, h.user), now=NOW)
    cancel = h.request(
        DirectiveAction.CANCEL_ALL,
        key="idem-ls-paper-ambiguous-cancel-all",
    )
    waiting = h.service.submit(cancel, _execute_token(cancel, h.user), now=NOW)

    assert waiting.state is DirectiveState.UNKNOWN
    assert waiting.legs[0].state is DirectiveLegState.UNKNOWN
    assert broker.cancellations == 1

    run_directive_worker_once(h.service, batch=100, now=NOW)
    run_directive_worker_once(h.service, batch=100, now=NOW)
    assert broker.cancellations == 1


def test_worker_recovers_returned_filled_internal_error_without_duplicate_fill():
    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))
    request = h.request(DirectiveAction.PLACE_ORDER, key="idem-filled-recovery")
    original_reconcile = h.repository.reconcile_cancel_legs

    def fail_after_fill(record):
        if any(leg.state is DirectiveLegState.FILLED for leg in record.legs):
            raise RuntimeError("simulated post-fill SQL failure")
        return original_reconcile(record)

    h.repository.reconcile_cancel_legs = fail_after_fill  # type: ignore[method-assign]

    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert record.state is DirectiveState.PARTIAL
    assert record.error_code == "TRADING_DIRECTIVE_INTERNAL_ERROR"
    assert record.legs[0].state is DirectiveLegState.FILLED
    assert len(h.repository.state.direct_fills) == 1

    h.repository.acknowledge_direct_fills(record.legs[0].leg_id)
    h.repository.reconcile_cancel_legs = original_reconcile  # type: ignore[method-assign]

    recovered = run_directive_worker_once(h.service, batch=100, now=NOW)

    assert recovered["reconciled"] == 1
    assert recovered["errors"] == []
    assert record.state is DirectiveState.COMPLETED
    assert record.error_code is None
    assert len(h.repository.state.direct_fills) == 1


def test_status_read_proof_binds_subject_fund_book_and_directive():
    h = Harness()
    request = h.request(DirectiveAction.SELL_ALL)
    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    assert record.state is DirectiveState.COMPLETED
    assert h.service.get_status(record.directive_id, _read_token(record, h.user), now=NOW).directive_id == record.directive_id

    with pytest.raises(DirectiveServiceError) as denied:
        h.service.get_status(record.directive_id, _read_token(record, uuid4()), now=NOW)
    assert denied.value.code == "TRADING_PROOF_BINDING_DENIED"


def test_market_buy_requires_fresh_quote_cash_and_reserves_cost_buffer():
    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("70699"))
    request = h.request(DirectiveAction.PLACE_ORDER)
    with pytest.raises(DirectiveServiceError) as denied:
        h.service.submit(request, _execute_token(request, h.user), now=NOW)
    assert denied.value.code == "TRADING_INSUFFICIENT_CASH"

    h2 = Harness()
    h2.repository.set_cash(h2.fund, h2.book, "KRW", Decimal("100000"))
    h2.market.quotes.clear()
    request2 = h2.request(DirectiveAction.PLACE_ORDER)
    waiting = h2.service.submit(
        request2, _execute_token(request2, h2.user), now=NOW
    )
    assert waiting.state is DirectiveState.RECEIVED
    assert waiting.error_code == "TRADING_MARKET_QUOTE_PENDING"
    assert waiting.legs == []
    assert h2.repository.available_cash(h2.fund, h2.book, "KRW") == Decimal("100000")


def test_transient_basket_quote_failure_waits_then_reuses_same_directive() -> None:
    """No-leg quote delay is queued and resumed without a second placement."""

    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))

    class RecoveringMarket:
        available = False
        reads = 0

        def quote(self, instrument, *, now, max_age_seconds=None):
            self.reads += 1
            if not self.available:
                raise MarketDataError(
                    "TRADING_MARKET_QUOTE_UNAVAILABLE",
                    "projection and broker read are still pending",
                    503,
                )
            return h.market.quote(
                instrument, now=now, max_age_seconds=max_age_seconds
            )

    market = RecoveringMarket()
    h.service = UserDirectiveService(h.repository, market)
    request = h.request(
        DirectiveAction.PLACE_BASKET,
        key="idem-basket-quote-wait-0001",
        payload=_quantity_basket_payload((h.instrument, "1"), side="BUY"),
    )

    waiting = h.service.submit(
        request, _execute_token(request, h.user), now=NOW
    )

    assert waiting.state is DirectiveState.RECEIVED
    assert waiting.error_code == "TRADING_MARKET_QUOTE_PENDING"
    assert waiting.legs == []
    assert directive_service_module.is_market_quote_deferred(waiting)

    first = run_directive_worker_once(h.service, batch=100, now=NOW)
    assert first["errors"] == []
    assert first["deferred"] == [
        f"{waiting.directive_id}:TRADING_MARKET_QUOTE_PENDING"
    ]
    assert waiting.legs == []

    market.available = True
    resumed = run_directive_worker_once(h.service, batch=100, now=NOW)

    assert resumed["errors"] == []
    assert resumed["deferred"] == []
    assert waiting.state is DirectiveState.IN_PROGRESS
    assert waiting.error_code == "TRADING_FILL_ACCOUNTING_PENDING"
    assert len(waiting.legs) == 1
    assert len(h.repository.state.direct_fills) == 1


def test_structurally_invalid_quote_still_fails_instead_of_waiting() -> None:
    """Malformed data is permanent input failure, not a retryable delay."""

    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))

    class InvalidMarket:
        def quote(self, *_args, **_kwargs):
            raise MarketDataError(
                "TRADING_MARKET_QUOTE_INVALID", "best_bid is malformed", 503
            )

    h.service = UserDirectiveService(h.repository, InvalidMarket())
    request = h.request(
        DirectiveAction.PLACE_BASKET,
        key="idem-basket-invalid-quote-0001",
        payload=_quantity_basket_payload((h.instrument, "1"), side="BUY"),
    )

    with pytest.raises(DirectiveServiceError) as denied:
        h.service.submit(request, _execute_token(request, h.user), now=NOW)

    assert denied.value.code == "TRADING_MARKET_QUOTE_INVALID"
    record = h.repository.get(denied.value.directive_id)
    assert record is not None
    assert record.state is DirectiveState.FAILED
    assert record.legs == []


def test_cancel_all_releases_direct_reservation_and_is_actual_for_local_leg():
    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))
    place = h.request(
        DirectiveAction.PLACE_ORDER,
        payload={
            "instrument_id": str(h.instrument.instrument_id),
            "symbol": "005930",
            "side": "BUY",
            "quantity": "1",
            "order_type": "LIMIT",
            "limit_price": "69000",
            "time_in_force": "DAY",
        },
    )
    placed = h.service.submit(place, _execute_token(place, h.user), now=NOW)
    assert h.repository.available_cash(h.fund, h.book, "KRW") < Decimal("100000")

    cancel = h.request(DirectiveAction.CANCEL_ALL)
    cancelled = h.service.submit(cancel, _execute_token(cancel, h.user), now=NOW)
    assert cancelled.state is DirectiveState.COMPLETED
    assert placed.legs[0].state is DirectiveLegState.CANCELLED
    assert placed.state is DirectiveState.FAILED
    assert h.repository.available_cash(h.fund, h.book, "KRW") == Decimal("100000")
    assert (h.fund, h.book) not in h.repository.state.barriers


def test_place_resumes_after_preemption_confirmation_and_ignores_cancel_legs():
    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))
    lower = DirectiveLeg(
        uuid4(), uuid4(), 0, h.instrument.instrument_id, "005930", "BUY", "MARKET",
        Decimal("1"), None, state=DirectiveLegState.UNKNOWN,
    )
    h.repository.state.lower_orders.append(lower)
    request = h.request(
        DirectiveAction.PLACE_ORDER,
        payload={
            "instrument_id": str(h.instrument.instrument_id),
            "symbol": "005930",
            "side": "BUY",
            "quantity": "1",
            "order_type": "LIMIT",
            "limit_price": "69000",
            "time_in_force": "DAY",
        },
    )

    waiting = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    assert waiting.state is DirectiveState.UNKNOWN
    assert [leg.side for leg in waiting.legs] == [None]

    # Simulate the PAPER adapter's later cancel confirmation.  Status must
    # resume the canonical payload and must not aggregate this cancellation
    # audit leg as the requested PLACE order.
    lower.state = DirectiveLegState.CANCELLED
    waiting.legs[0].state = DirectiveLegState.CANCELLED
    resumed = h.service.get_status(
        waiting.directive_id, _read_token(waiting, h.user), now=NOW
    )
    order_legs = [leg for leg in resumed.legs if leg.side is not None]
    assert resumed.state is DirectiveState.IN_PROGRESS
    assert len(order_legs) == 1
    assert order_legs[0].state is DirectiveLegState.ACKNOWLEDGED


def test_sell_all_ack_is_in_progress_until_position_and_open_sell_are_zero():
    h = Harness()
    h.repository.set_position(h.fund, h.book, h.instrument.instrument_id, Decimal("10"))
    request = h.request(DirectiveAction.SELL_ALL)
    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    assert record.state is DirectiveState.IN_PROGRESS
    leg = next(item for item in record.legs if item.side == "SELL")
    assert leg.reduce_only and leg.requested_quantity == Decimal("10")
    assert h.repository.state.positions[(h.fund, h.book, h.instrument.instrument_id)] == Decimal("10")

    # Position projection alone is insufficient while the open sell
    # reservation remains.
    h.repository.set_position(h.fund, h.book, h.instrument.instrument_id, Decimal("0"))
    status = h.service.get_status(record.directive_id, _read_token(record, h.user), now=NOW)
    assert status.state is DirectiveState.IN_PROGRESS

    reservation = h.repository.state.reservations[leg.leg_id]
    h.repository.state.reservations[leg.leg_id] = (*reservation[:-1], False)
    leg.state = DirectiveLegState.FILLED
    leg.filled_quantity = leg.requested_quantity
    done = h.service.get_status(record.directive_id, _read_token(record, h.user), now=NOW)
    assert done.state is DirectiveState.COMPLETED
    assert (h.fund, h.book) not in h.repository.state.barriers


def test_quote_event_is_idempotent_partial_fills_require_new_evidence_and_accounting_ack():
    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("500000"))
    h.market.set_quote(
        TrustedQuote(
            str(h.instrument.instrument_id), "005930", NOW,
            Decimal("69900"), Decimal("70000"), Decimal("1"), Decimal("1"),
            "fixture",
        )
    )
    request = h.request(
        DirectiveAction.PLACE_ORDER,
        payload={
            "instrument_id": str(h.instrument.instrument_id),
            "symbol": "005930",
            "side": "BUY",
            "quantity": "3",
            "order_type": "MARKET",
            "limit_price": None,
            "time_in_force": "DAY",
        },
    )
    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    leg = record.legs[0]
    assert record.state is DirectiveState.IN_PROGRESS
    assert leg.state is DirectiveLegState.PARTIALLY_FILLED
    assert leg.filled_quantity == Decimal("1")
    assert len(h.repository.state.direct_fills) == 1

    # The identical quote event cannot be consumed twice.
    same = h.service.get_status(record.directive_id, _read_token(record, h.user), now=NOW)
    assert same.legs[0].filled_quantity == Decimal("1")
    assert len(h.repository.state.direct_fills) == 1

    later = NOW + timedelta(seconds=1)
    h.market.set_quote(
        TrustedQuote(
            str(h.instrument.instrument_id), "005930", later,
            Decimal("69900"), Decimal("70000"), Decimal("40"), Decimal("40"),
            "fixture",
        )
    )
    filled = h.service.get_status(
        record.directive_id, _read_token(record, h.user, now=later), now=later
    )
    assert filled.state is DirectiveState.IN_PROGRESS
    assert filled.error_code == "TRADING_FILL_ACCOUNTING_PENDING"
    assert filled.legs[0].filled_quantity == Decimal("3")
    # Fill evidence alone does not release the cash reservation.
    assert h.repository.available_cash(h.fund, h.book, "KRW") < Decimal("500000")
    h.repository.acknowledge_direct_fills(leg.leg_id)
    assert h.repository.state.positions[(h.fund, h.book, h.instrument.instrument_id)] == Decimal("3")
    assert not h.repository.state.reservations[leg.leg_id][-1]
    done = h.service.get_status(
        record.directive_id, _read_token(record, h.user, now=later), now=later
    )
    assert done.state is DirectiveState.COMPLETED


@pytest.mark.parametrize("pending", [True, False])
def test_postgres_accounting_pending_query_matches_repository_contract(pending):
    class Cursor:
        statement = ""
        params = ()

        def execute(self, statement, params):
            self.statement = statement
            self.params = params

        def fetchone(self):
            return (pending,)

    cursor = Cursor()

    @contextmanager
    def fake_cursor():
        yield cursor

    repository = PostgresDirectiveRepository("postgresql://unused")
    repository._cursor = fake_cursor  # type: ignore[method-assign]
    directive_id = uuid4()

    assert repository.has_unaccounted_fills(directive_id) is pending
    assert cursor.params == (directive_id,)
    normalized_sql = " ".join(cursor.statement.split())
    assert "fill.accounting_acknowledged_at is null" in normalized_sql
    assert "execution.outbox_consumed" in normalized_sql
    assert "consumed.consumer='accounting-ledger'" in normalized_sql
    assert "trading-user-directive-fill-v1" in normalized_sql


def test_postgres_available_cash_uses_the_canonical_cash_account_code():
    """Production chart-of-accounts stores cash as 1000, never ``CASH``."""

    class Cursor:
        statements: list[str]

        def __init__(self):
            self.statements = []

        def execute(self, statement, params):
            self.statements.append(" ".join(statement.split()))

        def fetchone(self):
            if len(self.statements) == 1:
                return (Decimal("125000"),)
            return (Decimal("25000"),)

    cursor = Cursor()

    @contextmanager
    def fake_cursor():
        yield cursor

    repository = PostgresDirectiveRepository("postgresql://unused")
    repository._cursor = fake_cursor  # type: ignore[method-assign]

    available = repository.available_cash(uuid4(), uuid4(), "KRW")

    assert available == Decimal("100000")
    assert "la.account_code='1000'" in cursor.statements[0]
    assert "la.account_code='CASH'" not in cursor.statements[0]


def test_postgres_directive_sql_respects_immutable_proofs_and_locks_only_orders():
    source = (TRADING_ROOT / "directives" / "repository.py").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(source.split()).casefold()
    postgres_source = normalized.split("class postgresdirectiverepository:", 1)[1]
    release_barrier = postgres_source.split("def release_barrier", 1)[1].split(
        "def resolve_instrument", 1
    )[0]
    cancel_open_orders = postgres_source.split("def cancel_open_orders", 1)[1]

    assert "on conflict (proof_jti) do nothing returning directive_id" in normalized
    assert "where proof_jti=%s for update" not in normalized
    assert "limit 1 for update" in release_barrier
    assert "for update of o" not in release_barrier
    assert "left join lateral" in cancel_open_orders
    assert "for update of o" in cancel_open_orders
    assert postgres_source.count("symbol ~ '^[0-9a-z]{6}$'") >= 3
    assert "symbol ~ '^[0-9]{6}$'" not in postgres_source


def test_postgres_cancel_reconciliation_does_not_mix_distinct_with_row_locking():
    source = inspect.getsource(
        PostgresDirectiveRepository.reconcile_cancel_legs
    ).lower()

    assert "select distinct" not in source
    assert "where exists (" in source
    assert "leg.linked_order_id=target.order_id" in source
    assert "for update of target" in source


def test_trading_contract_strip_uppers_exact_alphanumeric_krx_symbol() -> None:
    h = Harness()
    request = h.request(
        DirectiveAction.PLACE_ORDER,
        payload={
            "instrument_id": None,
            "symbol": " 00088k ",
            "side": "BUY",
            "quantity": "1",
            "order_type": "MARKET",
            "limit_price": None,
            "time_in_force": "DAY",
        },
    )

    assert request.place_order().symbol == "00088K"
    assert request.canonical_payload()["symbol"] == "00088K"

    for invalid in ("00088-", "00088KK", "Samsung Electronics"):
        with pytest.raises(ValueError):
            h.request(
                DirectiveAction.PLACE_ORDER,
                payload={**request.payload, "symbol": invalid},
            )


def test_postgres_resolution_filters_exact_alphanumeric_krx_symbol() -> None:
    instrument_id = uuid4()

    class Cursor:
        statement = ""
        params = ()

        def execute(self, statement, params):
            self.statement = " ".join(statement.split())
            self.params = params

        def fetchall(self):
            return [(instrument_id, "00088K", Decimal(1), None, "KRW")]

    cursor = Cursor()

    @contextmanager
    def fake_cursor():
        yield cursor

    repository = PostgresDirectiveRepository("postgresql://unused")
    repository._cursor = fake_cursor  # type: ignore[method-assign]

    resolved = repository.resolve_instrument(uuid4(), uuid4(), None, "00088K")

    assert resolved.symbol == "00088K"
    assert "sy.symbol ~ '^[0-9A-Z]{6}$'" in cursor.statement
    assert cursor.params == ("00088K", None, None)


def test_partial_direct_cancel_is_partial_and_retains_unaccounted_fill_reservation():
    h = Harness()
    h.repository.set_position(h.fund, h.book, h.instrument.instrument_id, Decimal("3"))
    h.market.set_quote(
        TrustedQuote(
            str(h.instrument.instrument_id), "005930", NOW,
            Decimal("69900"), Decimal("70000"), Decimal("1"), Decimal("1"),
            "fixture",
        )
    )
    place = h.request(
        DirectiveAction.PLACE_ORDER,
        payload={
            "instrument_id": str(h.instrument.instrument_id),
            "symbol": "005930",
            "side": "SELL",
            "quantity": "3",
            "order_type": "MARKET",
            "limit_price": None,
            "time_in_force": "DAY",
        },
    )
    placed = h.service.submit(place, _execute_token(place, h.user), now=NOW)
    assert placed.legs[0].filled_quantity == Decimal("1")

    cancel = h.request(DirectiveAction.CANCEL_ALL)
    result = h.service.submit(cancel, _execute_token(cancel, h.user), now=NOW)
    child = next(item for item in result.legs if item.side is None)
    assert result.state is DirectiveState.PARTIAL
    assert child.error_code == "TRADING_CANCEL_LOST_RACE"
    assert child.target_filled_quantity == Decimal("1")
    assert placed.state is DirectiveState.PARTIAL
    # Only the already-filled but not-yet-journaled share remains reserved.
    assert h.repository.open_sell_quantity(h.fund, h.book) == Decimal("1")
    h.repository.acknowledge_direct_fills(placed.legs[0].leg_id)
    assert h.repository.state.positions[(h.fund, h.book, h.instrument.instrument_id)] == Decimal("2")
    assert h.repository.open_sell_quantity(h.fund, h.book) == Decimal("0")


def test_sell_all_waits_for_unaccounted_inbound_buy_then_sizes_projected_position():
    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("500000"))
    buy = h.request(
        DirectiveAction.PLACE_ORDER,
        payload={
            "instrument_id": str(h.instrument.instrument_id),
            "symbol": "005930",
            "side": "BUY",
            "quantity": "2",
            "order_type": "MARKET",
            "limit_price": None,
            "time_in_force": "DAY",
        },
    )
    bought = h.service.submit(buy, _execute_token(buy, h.user), now=NOW)
    assert bought.state is DirectiveState.IN_PROGRESS
    assert bought.error_code == "TRADING_FILL_ACCOUNTING_PENDING"
    assert h.repository.state.positions.get(
        (h.fund, h.book, h.instrument.instrument_id), Decimal(0)
    ) == 0

    sell_all_request = h.request(DirectiveAction.SELL_ALL)
    waiting = h.service.submit(
        sell_all_request, _execute_token(sell_all_request, h.user), now=NOW
    )
    assert waiting.state is DirectiveState.IN_PROGRESS
    assert waiting.error_code == "TRADING_INBOUND_FILL_ACCOUNTING_PENDING"
    assert not any(leg.side == "SELL" for leg in waiting.legs)

    h.repository.acknowledge_direct_fills(bought.legs[0].leg_id)
    bought_done = h.service.get_status(
        bought.directive_id, _read_token(bought, h.user), now=NOW
    )
    assert bought_done.state is DirectiveState.COMPLETED
    resumed = h.service.get_status(
        waiting.directive_id, _read_token(waiting, h.user), now=NOW
    )
    sell_leg = next(leg for leg in resumed.legs if leg.side == "SELL")
    assert sell_leg.requested_quantity == Decimal("2")
    assert sell_leg.state is DirectiveLegState.FILLED
    assert resumed.state is DirectiveState.IN_PROGRESS
    h.repository.acknowledge_direct_fills(sell_leg.leg_id)
    done = h.service.get_status(
        waiting.directive_id, _read_token(waiting, h.user), now=NOW
    )
    assert done.state is DirectiveState.COMPLETED


def test_autonomous_reconciler_fills_without_status_poll_and_expires_day_orders():
    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("500000"))
    limit = h.request(
        DirectiveAction.PLACE_ORDER,
        payload={
            "instrument_id": str(h.instrument.instrument_id),
            "symbol": "005930",
            "side": "BUY",
            "quantity": "1",
            "order_type": "LIMIT",
            "limit_price": "69900",
            "time_in_force": "DAY",
        },
    )
    open_order = h.service.submit(limit, _execute_token(limit, h.user), now=NOW)
    assert open_order.legs[0].state is DirectiveLegState.ACKNOWLEDGED
    later = NOW + timedelta(seconds=1)
    h.market.set_quote(
        TrustedQuote(
            str(h.instrument.instrument_id), "005930", later,
            Decimal("69800"), Decimal("69900"), Decimal("1000"), Decimal("1000"),
            "fixture",
        )
    )
    reconciled, errors = h.service.reconcile_active(now=later)
    assert errors == []
    accounting_pending = next(
        row for row in reconciled if row.directive_id == open_order.directive_id
    )
    assert accounting_pending.state is DirectiveState.IN_PROGRESS
    assert accounting_pending.error_code == "TRADING_FILL_ACCOUNTING_PENDING"
    h.repository.acknowledge_direct_fills(open_order.legs[0].leg_id)
    worker_result = run_directive_worker_once(h.service, batch=100, now=later)
    assert worker_result["errors"] == []
    assert open_order.state is DirectiveState.COMPLETED

    h2 = Harness()
    h2.repository.set_cash(h2.fund, h2.book, "KRW", Decimal("500000"))
    expiring = h2.request(
        DirectiveAction.PLACE_ORDER,
        payload={
            "instrument_id": str(h2.instrument.instrument_id),
            "symbol": "005930",
            "side": "BUY",
            "quantity": "1",
            "order_type": "LIMIT",
            "limit_price": "69000",
            "time_in_force": "DAY",
        },
    )
    expired = h2.service.submit(expiring, _execute_token(expiring, h2.user), now=NOW)
    h2.service.reconcile_active(now=NOW + timedelta(hours=5))
    assert expired.state is DirectiveState.FAILED
    assert expired.legs[0].state is DirectiveLegState.EXPIRED
    assert h2.repository.available_cash(h2.fund, h2.book, "KRW") == Decimal("500000")


def test_day_expiry_releases_reservation_partial_fill_is_terminal_partial_unknown_is_not_expired():
    h = Harness()
    h.repository.set_position(h.fund, h.book, h.instrument.instrument_id, Decimal("3"))
    payload = {
        "instrument_id": str(h.instrument.instrument_id),
        "symbol": "005930",
        "side": "SELL",
        "quantity": "3",
        "order_type": "LIMIT",
        "limit_price": "70000",
        "time_in_force": "DAY",
    }
    request = h.request(DirectiveAction.PLACE_ORDER, payload=payload)
    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    record.legs[0].state = DirectiveLegState.PARTIALLY_FILLED
    record.legs[0].filled_quantity = Decimal("1")
    later = NOW + timedelta(hours=5)
    status = h.service.get_status(record.directive_id, _read_token(record, h.user, now=later), now=later)
    assert status.state is DirectiveState.PARTIAL
    assert record.legs[0].state is DirectiveLegState.EXPIRED
    assert h.repository.open_sell_quantity(h.fund, h.book) == 0
    assert (h.fund, h.book) not in h.repository.state.barriers

    unknown = DirectiveLeg(
        uuid4(), uuid4(), 0, h.instrument.instrument_id, "005930", "SELL", "MARKET",
        Decimal("1"), None, state=DirectiveLegState.UNKNOWN, expires_at=NOW,
    )
    ghost = h.request(DirectiveAction.CANCEL_ALL)
    accepted, _ = h.repository.accept(ghost, _proof_object(ghost, h.user, "ghost-jti"))
    unknown.directive_id = accepted.directive_id
    accepted.legs.append(unknown)
    h.repository.expire_open_legs(accepted, now=later)
    assert unknown.state is DirectiveLegState.UNKNOWN


def _proof_object(request: UserDirectiveRequest, subject: UUID, jti: str):
    from directives.auth import decode_directive_proof

    return decode_directive_proof(_execute_token(request, subject, jti=jti), now=NOW.timestamp())


def test_lower_priority_directive_waits_behind_higher_priority_barrier() -> None:
    h = Harness()
    second = InstrumentRef(uuid4(), "000660", Decimal(1), None, "KRW")
    h.repository.add_instrument(second)
    h.market.set_quote(
        TrustedQuote(
            str(second.instrument_id),
            second.symbol,
            NOW,
            Decimal("100000"),
            Decimal("100100"),
            Decimal("1000"),
            Decimal("1000"),
            "fixture",
        )
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("1000000"))

    higher_request = h.request(DirectiveAction.SELL_ALL, key="idem-priority-owner")
    higher, _ = h.repository.accept(
        higher_request, _proof_object(higher_request, h.user, "priority-owner-jti")
    )
    h.repository.set_state(higher.directive_id, DirectiveState.IN_PROGRESS)
    h.repository.activate_barrier(higher, reduce_only=True)

    queued_request = h.request(
        DirectiveAction.PLACE_BASKET,
        key="idem-priority-queued",
        payload=_quantity_basket_payload(
            (h.instrument, "1"), (second, "1"), side="BUY"
        ),
    )
    queued = h.service.submit(
        queued_request,
        _execute_token(queued_request, h.user, jti="priority-queued-jti"),
        now=NOW,
    )

    assert queued.state is DirectiveState.RECEIVED
    assert queued.error_code == "TRADING_HIGHER_PRIORITY_ACTIVE"
    assert queued.legs == []
    assert h.repository.state.barriers[(h.fund, h.book)][0] == higher.directive_id

    h.repository.set_state(higher.directive_id, DirectiveState.FAILED)
    h.repository.release_barrier(higher)
    reconciled, errors = h.service.reconcile_active(now=NOW)

    assert errors == []
    assert any(item.directive_id == queued.directive_id for item in reconciled)
    executed = h.repository.get(queued.directive_id)
    assert executed is not None
    assert executed.state is DirectiveState.IN_PROGRESS
    assert {leg.symbol for leg in executed.legs} == {"000660", "005930"}
    assert all(leg.state is DirectiveLegState.FILLED for leg in executed.legs)


def test_terminal_barrier_is_discarded_before_new_directive() -> None:
    h = Harness()
    stale_request = h.request(DirectiveAction.SELL_ALL, key="idem-stale-barrier")
    stale, _ = h.repository.accept(
        stale_request, _proof_object(stale_request, h.user, "stale-barrier-jti")
    )
    h.repository.set_state(stale.directive_id, DirectiveState.FAILED)
    h.repository.activate_barrier(stale, reduce_only=True)

    current_request = h.request(DirectiveAction.SELL_ALL, key="idem-current-barrier")
    current, _ = h.repository.accept(
        current_request, _proof_object(current_request, h.user, "current-barrier-jti")
    )
    h.repository.activate_barrier(current, reduce_only=True)

    assert h.repository.state.barriers[(h.fund, h.book)][0] == current.directive_id


def test_barrier_release_reelects_other_active_directive_and_restart_hydrates():
    h = Harness()
    first = h.request(DirectiveAction.PLACE_ORDER, key="idem-barrier-a")
    second = h.request(DirectiveAction.PLACE_ORDER, key="idem-barrier-b")
    a, _ = h.repository.accept(first, _proof_object(first, h.user, "barrier-jti-a"))
    b, _ = h.repository.accept(second, _proof_object(second, h.user, "barrier-jti-b"))
    h.repository.set_state(a.directive_id, DirectiveState.IN_PROGRESS)
    h.repository.set_state(b.directive_id, DirectiveState.IN_PROGRESS)
    h.repository.activate_barrier(a, reduce_only=False)
    h.repository.activate_barrier(b, reduce_only=False)
    h.repository.set_state(b.directive_id, DirectiveState.FAILED)
    h.repository.release_barrier(b)
    assert h.repository.state.barriers[(h.fund, h.book)][0] == a.directive_id

    restarted = InMemoryDirectiveRepository(h.repository.state)
    assert restarted.get(a.directive_id).directive_id == a.directive_id


def test_non_paper_mode_fails_before_runtime_construction(monkeypatch):
    monkeypatch.setenv("TRADING_EXECUTION_MODE", "LIVE")
    h = Harness.__new__(Harness)
    with pytest.raises(DirectiveServiceError) as denied:
        UserDirectiveService(InMemoryDirectiveRepository(), FixtureMarketDataProvider())
    assert denied.value.code == "TRADING_PAPER_MODE_REQUIRED"


def test_trading_post_requires_matching_idempotency_header():
    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))
    request = h.request(DirectiveAction.PLACE_ORDER, key="idem-route-0001")
    proof = _execute_token(request, h.user)
    set_directive_service_for_tests(h.service)
    try:
        with pytest.raises(DirectiveServiceError) as missing:
            submit_user_directive(request, proof, None)
        assert missing.value.code == "TRADING_IDEMPOTENCY_KEY_REQUIRED"
        with pytest.raises(DirectiveServiceError) as mismatch:
            submit_user_directive(request, proof, "idem-route-other")
        assert mismatch.value.code == "TRADING_IDEMPOTENCY_KEY_MISMATCH"
    finally:
        set_directive_service_for_tests(None)


def test_trading_contract_rejects_noncanonical_idempotency_key():
    h = Harness()
    with pytest.raises(ValueError):
        h.request(DirectiveAction.PLACE_ORDER, key="bad key with spaces")


def test_worker_queue_prioritizes_emergency_directive_over_older_place():
    h = Harness()
    older = h.request(DirectiveAction.PLACE_ORDER, key="idem-worker-old")
    emergency = h.request(DirectiveAction.SELL_ALL, key="idem-worker-emergency")
    old_record, _ = h.repository.accept(
        older, _proof_object(older, h.user, "worker-old-jti")
    )
    emergency_record, _ = h.repository.accept(
        emergency, _proof_object(emergency, h.user, "worker-emergency-jti")
    )
    queued = h.repository.active_directives(limit=1)
    assert queued[0].directive_id == emergency_record.directive_id
    assert queued[0].priority > old_record.priority


def test_worker_backs_off_only_for_closed_session_errors():
    assert _sleep_seconds(
        {"errors": ["directive:TRADING_MARKET_SESSION_UNAVAILABLE"]},
        1.0,
    ) == 5.0
    assert _sleep_seconds(
        {"errors": ["directive:TRADING_MARKET_SESSION_CLOSED"]},
        10.0,
    ) == 10.0
    assert _sleep_seconds(
        {"errors": [], "deferred": ["directive:TRADING_MARKET_SESSION_CLOSED"]},
        1.0,
    ) == 5.0
    assert _sleep_seconds(
        {
            "errors": [
                "directive:TRADING_MARKET_SESSION_UNAVAILABLE",
                "directive:OTHER",
            ]
        },
        1.0,
    ) == 1.0


def test_worker_defers_closed_session_without_repeated_state_writes():
    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("100000"))
    request = h.request(DirectiveAction.PLACE_ORDER, key="idem-worker-session-wait")
    record, _ = h.repository.accept(
        request,
        _proof_object(request, h.user, "worker-session-wait-jti"),
    )
    h.repository.set_market_session(
        NOW - timedelta(hours=2), NOW - timedelta(hours=1)
    )

    first = run_directive_worker_once(h.service, batch=100, now=NOW)
    waiting = h.repository.get(record.directive_id)
    assert waiting is not None
    assert first["errors"] == []
    assert first["deferred"] == [
        f"{record.directive_id}:TRADING_MARKET_SESSION_CLOSED"
    ]
    assert waiting.state is DirectiveState.RECEIVED
    assert waiting.legs == []
    first_updated_at = waiting.updated_at

    second = run_directive_worker_once(
        h.service, batch=100, now=NOW + timedelta(seconds=1)
    )
    still_waiting = h.repository.get(record.directive_id)
    assert still_waiting is not None
    assert second["errors"] == []
    assert second["deferred"] == first["deferred"]
    assert still_waiting.updated_at == first_updated_at

    h.repository.set_market_session(
        NOW - timedelta(hours=1), NOW + timedelta(hours=4)
    )
    resumed = run_directive_worker_once(
        h.service, batch=100, now=NOW + timedelta(seconds=1)
    )
    executed = h.repository.get(record.directive_id)
    assert resumed["errors"] == []
    assert resumed["deferred"] == []
    assert executed is not None
    assert executed.state is DirectiveState.IN_PROGRESS
    assert len(executed.legs) == 1


def test_worker_log_throttle_keeps_changes_immediate_and_dedupes_steady_state():
    result = {
        "reconciled": 2,
        "deferred": ["directive:TRADING_MARKET_SESSION_CLOSED"],
        "errors": [],
        "at": NOW.isoformat(),
    }
    first, signature = _should_log(
        result,
        previous_signature=None,
        previous_at=0.0,
        now=100.0,
    )
    assert first is True
    assert signature is not None

    repeated, repeated_signature = _should_log(
        {**result, "at": (NOW + timedelta(seconds=5)).isoformat()},
        previous_signature=signature,
        previous_at=100.0,
        now=105.0,
    )
    assert repeated is False
    assert repeated_signature == signature

    periodic, _ = _should_log(
        result,
        previous_signature=signature,
        previous_at=100.0,
        now=160.0,
    )
    assert periodic is True
    changed, _ = _should_log(
        {**result, "errors": ["directive:OTHER"]},
        previous_signature=signature,
        previous_at=159.0,
        now=160.0,
    )
    assert changed is True


def test_worker_retry_touch_rotates_same_priority_rows_without_state_rewrite():
    h = Harness()
    first_request = h.request(DirectiveAction.PLACE_ORDER, key="idem-worker-first")
    second_request = h.request(DirectiveAction.PLACE_ORDER, key="idem-worker-second")
    first, _ = h.repository.accept(
        first_request, _proof_object(first_request, h.user, "worker-first-jti")
    )
    second, _ = h.repository.accept(
        second_request, _proof_object(second_request, h.user, "worker-second-jti")
    )
    first.updated_at = NOW
    second.updated_at = NOW + timedelta(seconds=1)

    assert h.repository.active_directives(limit=1)[0].directive_id == first.directive_id
    assert h.repository.touch_active(first.directive_id)
    assert first.state is DirectiveState.RECEIVED
    assert h.repository.active_directives(limit=1)[0].directive_id == second.directive_id


def _sell_position_payload(symbol: str = "005930") -> dict:
    return {
        "instrument_id": None,
        "symbol": symbol,
        "side": "SELL",
        "order_type": "MARKET",
        "time_in_force": "DAY",
        "reduce_only": True,
    }


def test_expired_sell_position_releases_the_book_barrier() -> None:
    """The stall behind repeated TRADING_HIGHER_PRIORITY_ACTIVE (2026-09-01).

    SELL_POSITION was the one expiring action with no branch in
    _reconcile_expired_scope, so its legs expired while the row stayed
    IN_PROGRESS - an active state - and kept the priority-2000 barrier. Every
    later user order queued behind a directive that could never finish.
    """

    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("1000000"))
    request = h.request(
        DirectiveAction.SELL_POSITION,
        key="idem-sell-position-expiry",
        payload=_sell_position_payload(),
    )
    record, _ = h.repository.accept(
        request, _proof_object(request, h.user, "sell-position-expiry-jti")
    )
    h.repository.set_state(record.directive_id, DirectiveState.IN_PROGRESS)
    h.repository.activate_barrier(record, reduce_only=True)
    h.repository.create_acknowledged_leg(
        record,
        h.instrument,
        side="SELL",
        order_type="MARKET",
        quantity=Decimal("1"),
        limit_price=None,
        reserve_cash=None,
        reduce_only=True,
        expires_at=NOW + timedelta(hours=1),
    )
    assert h.repository.state.barriers[(h.fund, h.book)][0] == record.directive_id

    h.service.reconcile_active(now=NOW + timedelta(hours=2))

    settled = h.repository.get(record.directive_id)
    assert settled.legs[0].state is DirectiveLegState.EXPIRED
    assert settled.state is DirectiveState.FAILED
    assert settled.error_code == "TRADING_PAPER_ORDER_EXPIRED"
    assert (h.fund, h.book) not in h.repository.state.barriers


def test_partially_filled_sell_position_expiry_is_partial_not_failed() -> None:
    h = Harness()
    request = h.request(
        DirectiveAction.SELL_POSITION,
        key="idem-sell-position-partial",
        payload=_sell_position_payload(),
    )
    record, _ = h.repository.accept(
        request, _proof_object(request, h.user, "sell-position-partial-jti")
    )
    h.repository.set_state(record.directive_id, DirectiveState.IN_PROGRESS)
    h.repository.activate_barrier(record, reduce_only=True)
    leg = h.repository.create_acknowledged_leg(
        record,
        h.instrument,
        side="SELL",
        order_type="MARKET",
        quantity=Decimal("2"),
        limit_price=None,
        reserve_cash=None,
        reduce_only=True,
        expires_at=NOW + timedelta(hours=1),
    )
    leg.state = DirectiveLegState.PARTIALLY_FILLED
    leg.filled_quantity = Decimal("1")

    h.service.reconcile_active(now=NOW + timedelta(hours=2))

    settled = h.repository.get(record.directive_id)
    assert settled.state is DirectiveState.PARTIAL
    assert (h.fund, h.book) not in h.repository.state.barriers


def test_barrier_is_reclaimed_from_an_owner_that_holds_nothing_open() -> None:
    """A worker that dies between activate_barrier and its first leg.

    IN_PROGRESS and UNKNOWN both count as active, so the terminal-state check
    alone never freed the book and the whole scope stayed locked.
    """

    h = Harness()
    owner_request = h.request(DirectiveAction.SELL_ALL, key="idem-dead-owner")
    owner, _ = h.repository.accept(
        owner_request, _proof_object(owner_request, h.user, "dead-owner-jti")
    )
    h.repository.set_state(owner.directive_id, DirectiveState.IN_PROGRESS)
    h.repository.activate_barrier(owner, reduce_only=True)
    owner.updated_at = datetime.now(timezone.utc) - BARRIER_STALE_AFTER - timedelta(seconds=1)

    later_request = h.request(DirectiveAction.PLACE_ORDER, key="idem-after-dead-owner")
    later, _ = h.repository.accept(
        later_request, _proof_object(later_request, h.user, "after-dead-owner-jti")
    )
    h.repository.activate_barrier(later, reduce_only=False)

    assert h.repository.state.barriers[(h.fund, h.book)][0] == later.directive_id


def test_a_live_owner_keeps_the_barrier_however_long_it_waits() -> None:
    """Open legs, not the clock, are what make holding the book legitimate.

    A resting broker order can outlive any deadline; reclaiming from it would
    reintroduce exactly the buy-during-liquidation race the barrier prevents.
    """

    h = Harness()
    owner_request = h.request(DirectiveAction.SELL_ALL, key="idem-live-owner")
    owner, _ = h.repository.accept(
        owner_request, _proof_object(owner_request, h.user, "live-owner-jti")
    )
    h.repository.set_state(owner.directive_id, DirectiveState.IN_PROGRESS)
    h.repository.activate_barrier(owner, reduce_only=True)
    h.repository.create_acknowledged_leg(
        owner,
        h.instrument,
        side="SELL",
        order_type="LIMIT",
        quantity=Decimal("1"),
        limit_price=Decimal("70000"),
        reserve_cash=None,
        reduce_only=True,
        expires_at=NOW + timedelta(days=1),
    )
    owner.updated_at = datetime.now(timezone.utc) - BARRIER_STALE_AFTER - timedelta(hours=9)

    later_request = h.request(DirectiveAction.PLACE_ORDER, key="idem-blocked-by-live")
    later, _ = h.repository.accept(
        later_request, _proof_object(later_request, h.user, "blocked-by-live-jti")
    )
    with pytest.raises(DirectiveRepositoryError) as excinfo:
        h.repository.activate_barrier(later, reduce_only=False)
    assert excinfo.value.code == "TRADING_HIGHER_PRIORITY_ACTIVE"
    assert h.repository.state.barriers[(h.fund, h.book)][0] == owner.directive_id


def test_ls_paper_status_query_targets_the_leg_order_day_not_today() -> None:
    """CSPAQ13700 is per-day; asking about today loses yesterday's order.

    Regression for 2026-09-01: a SELL_ALL placed 17 minutes before the close
    was reconciled the next morning against *that* morning's order book, so
    LS returned no row, the leg stayed ACKNOWLEDGED, its directive stayed
    IN_PROGRESS and its priority-2000 barrier held the book for 19.8 hours.
    """

    class Broker:
        def __init__(self) -> None:
            self.asked_dates: list[object] = []
            self.rolled = False

        def place_order(self, **_kwargs):
            return LSPaperOrderAck("42139", "152759000", "005930")

        def order_status(self, broker_order_id, *, order_date=None):
            self.asked_dates.append(order_date)
            # Only the placement day's book knows this order. Querying any
            # other day returns nothing at all - which the caller must not
            # read as "still working".
            if order_date != NOW.astimezone(KST).date():
                return None
            return LSPaperOrderStatus(
                broker_order_id=broker_order_id,
                state="CANCELLED" if self.rolled else "ACKNOWLEDGED",
                requested_quantity=Decimal(1),
                filled_quantity=Decimal(0),
                fill_price=None,
                order_date=order_date,
            )

    h = Harness()
    broker = Broker()
    h.service = UserDirectiveService(
        h.repository, h.market, external_broker=broker  # type: ignore[arg-type]
    )
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("500000"))
    request = h.request(DirectiveAction.PLACE_ORDER, key="idem-ls-prior-session")
    record = h.service.submit(request, _execute_token(request, h.user), now=NOW)
    assert record.legs[0].state is DirectiveLegState.ACKNOWLEDGED

    # Next session: the KRX calendar has moved on, the leg has not, and the
    # exchange has since cancelled the unfilled DAY order.
    broker.rolled = True
    next_day = NOW + timedelta(days=1)
    h.repository.set_market_session(next_day - timedelta(hours=1), next_day + timedelta(hours=4))
    h.service.reconcile_active(now=next_day)

    assert broker.asked_dates, "the leg must be reconciled against the broker"
    assert broker.asked_dates[-1] == NOW.astimezone(KST).date()
    assert broker.asked_dates[-1] != next_day.astimezone(KST).date()
    assert record.legs[0].state is DirectiveLegState.CANCELLED


def test_leg_order_date_prefers_day_expiry_and_falls_back_to_creation() -> None:
    """The DAY expiry is the canonical session close of the placement day."""

    h = Harness()
    request = h.request(DirectiveAction.PLACE_ORDER, key="idem-order-date-helper")
    record, _ = h.repository.accept(
        request, _proof_object(request, h.user, "order-date-helper-jti")
    )
    close = datetime(2026, 8, 31, 6, 30, tzinfo=timezone.utc)  # 15:30 KST
    leg = DirectiveLeg(
        leg_id=uuid4(),
        directive_id=record.directive_id,
        leg_index=0,
        instrument_id=h.instrument.instrument_id,
        symbol="005930",
        side="BUY",
        order_type="MARKET",
        requested_quantity=Decimal(1),
        limit_price=None,
        expires_at=close,
    )
    assert directive_service_module.leg_order_date(record, leg) == close.astimezone(KST).date()

    legacy = DirectiveLeg(
        leg_id=uuid4(),
        directive_id=record.directive_id,
        leg_index=1,
        instrument_id=h.instrument.instrument_id,
        symbol="005930",
        side="BUY",
        order_type="MARKET",
        requested_quantity=Decimal(1),
        limit_price=None,
        expires_at=None,
    )
    assert directive_service_module.leg_order_date(record, legacy) == (
        record.created_at.astimezone(KST).date()
    )


def test_submit_outside_the_session_parks_received_without_holding_the_barrier() -> None:
    """A closed session is an admission wait at accept, exactly as in the worker.

    Regression for 2026-09-01: only ``reconcile_active`` applied this policy,
    so an order placed outside the session was terminally FAILED on
    submission instead of waiting for the next open.  ``_sell_all`` also takes
    the book barrier before resolving the session close, so parking must
    release it or the whole book stalls until the next open.
    """

    h = Harness()
    h.repository.set_cash(h.fund, h.book, "KRW", Decimal("500000"))
    # A holding is required, or SELL_ALL is an honest no-op that completes
    # before it ever resolves the session close.
    h.repository.set_position(
        h.fund, h.book, h.instrument.instrument_id, Decimal(10),
        average_cost=Decimal("60000"),
    )
    closed = NOW + timedelta(hours=6)  # past the fixture session close
    request = h.request(DirectiveAction.SELL_ALL, key="idem-session-closed-accept")

    record = h.service.submit(
        request, _execute_token(request, h.user, now=closed), now=closed
    )

    assert record.state is DirectiveState.RECEIVED
    assert record.error_code == "TRADING_MARKET_SESSION_CLOSED"
    assert not record.legs
    assert directive_service_module.is_market_session_deferred(record)
    # The barrier must not survive the park, or every later order queues.
    assert h.repository.state.barriers == {}
