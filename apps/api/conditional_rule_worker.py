"""Independent deterministic evaluator for authenticated conditional PAPER rules.

Hermes is deliberately absent from this hot path.  The worker reads only
already-confirmed ASTs, obtains market/portfolio facts through internal read
APIs, claims a trigger exactly once in PostgreSQL, and asks Trading to admit a
server-derived directive through its existing PAPER boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4

import jwt

from orchestration.conditional_rules import (
    ActiveRule,
    Candle,
    EvaluationClock,
    EvaluationContext,
    EvaluationError,
    ExecutionGuardInput,
    ExpressionNode,
    ExpressionType,
    GuardDecision,
    IndicatorEngine,
    IndicatorValue,
    PostgresRuleWorkerStore,
    RuleState,
    RuleWorkerStoreError,
    SubmitReadyExecution,
    Timeframe,
    TriggerClaim,
    evaluate_condition,
    guard_rule_execution,
)
from orchestration.conditional_rules.bar_data import (
    BarResolver,
    BarResolverError,
    LSChartBarResolver,
)
from orchestration.conditional_rules.indicators import DEFAULT_REGISTRY
from orchestration.conditional_rules.market_data import (
    LSPaperMarketPriceResolver,
    LSTimescaleMarketPriceResolver,
    MarketPriceResolver,
    MarketPriceResolverError,
)
from orchestration.conditional_rules.semantic import normalized_indicator_parameters

LOG = logging.getLogger("conditional-rule-worker")
INTERNAL_SCOPE = "trading.conditional_rule.execute"


class RuntimeDataError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class RuntimeInputs:
    evaluation_context: EvaluationContext
    evaluation_key: str
    context_sha256: str
    data_watermark: datetime
    membership_active: bool
    fund_active: bool
    book_active: bool
    market_session_available: bool
    market_open: bool
    data_complete: bool
    quote_fresh: bool
    current_price: Decimal
    available_cash: Decimal
    position_quantity: Decimal
    sellable_quantity: Decimal
    lot_size: Decimal

    def guard(self, rule: ActiveRule) -> ExecutionGuardInput:
        return ExecutionGuardInput(
            now=datetime.now(timezone.utc),
            rule_state=RuleState.TRIGGERED,
            evaluated_rule_version=rule.rule_version,
            active_rule_version=rule.rule_version,
            membership_active=self.membership_active,
            fund_active=self.fund_active,
            book_active=self.book_active,
            market_session_available=self.market_session_available,
            market_open=self.market_open,
            data_complete=self.data_complete,
            quote_fresh=self.quote_fresh,
            current_price=self.current_price,
            available_cash=self.available_cash,
            position_quantity=self.position_quantity,
            sellable_quantity=self.sellable_quantity,
            lot_size=self.lot_size,
            trigger_already_claimed=False,
        )


class WorkerStore(Protocol):
    def expire_due(self) -> int: ...
    def activate_ready_bundles(self, *, limit: int = 100) -> int: ...
    def list_active(self, *, limit: int = 100, offset: int = 0) -> list[ActiveRule]: ...
    def list_claimed(
        self, *, limit: int = 100
    ) -> list[tuple[ActiveRule, TriggerClaim]]: ...
    def record_false(
        self,
        rule: ActiveRule,
        *,
        evaluation_key: str,
        context_sha256: str,
        data_watermark: datetime,
    ) -> bool: ...
    def record_error(
        self,
        rule: ActiveRule,
        *,
        evaluation_key: str,
        context_sha256: str,
        data_watermark: datetime,
        error_code: str,
        error_message: str,
    ) -> bool: ...
    def claim_true(
        self,
        rule: ActiveRule,
        *,
        evaluation_key: str,
        context_sha256: str,
        data_watermark: datetime,
    ) -> TriggerClaim | None: ...
    def create_execution(
        self,
        rule: ActiveRule,
        claim: TriggerClaim,
        *,
        allowed: bool,
        guard_code: str,
        quantity: Decimal | None,
    ) -> SubmitReadyExecution | None: ...
    def list_submit_ready(self, *, limit: int = 100) -> list[SubmitReadyExecution]: ...
    def mark_submitting(self, rule_execution_id: UUID) -> None: ...
    def mark_retryable_failure(
        self, rule_execution_id: UUID, *, code: str, message: str
    ) -> None: ...
    def mark_terminal_failure(
        self, rule_execution_id: UUID, *, code: str, message: str
    ) -> None: ...
    def mark_submitted(self, rule_execution_id: UUID, *, directive_id: UUID) -> None: ...


class RuntimeClient(Protocol):
    def load_inputs(self, rule: ActiveRule) -> RuntimeInputs: ...
    def submit(self, execution: SubmitReadyExecution) -> UUID: ...


def _children(node: ExpressionNode) -> tuple[ExpressionNode, ...]:
    return tuple(
        child
        for child in (node.left, node.right, node.operand, *(node.children or ()))
        if child is not None
    )


def _walk(node: ExpressionNode) -> tuple[ExpressionNode, ...]:
    values = [node]
    for child in _children(node):
        values.extend(_walk(child))
    return tuple(values)


def _required_history(rule: ActiveRule) -> dict[Timeframe, int]:
    requires_cross = any(
        node.type is ExpressionType.CROSS for node in _walk(rule.spec.condition)
    )
    result: dict[Timeframe, int] = {}
    primary = rule.spec.evaluation.primary_timeframe
    needs_bars = any(
        (
            node.type is ExpressionType.MARKET
            and node.field != "LAST_PRICE"
        )
        or (
            node.type is ExpressionType.INDICATOR
            and (DEFAULT_REGISTRY.get(node.name) is not None)
            and DEFAULT_REGISTRY.get(node.name).source == "LOCAL"
        )
        for node in _walk(rule.spec.condition)
    )
    if primary is not None and needs_bars:
        result[primary] = 2 if requires_cross else 1
    for node in _walk(rule.spec.condition):
        if node.type is not ExpressionType.INDICATOR or node.timeframe is None:
            continue
        definition = DEFAULT_REGISTRY.get(node.name)
        if definition is None:
            raise RuntimeDataError("UNSUPPORTED_INDICATOR", f"unsupported indicator {node.name}", retryable=False)
        if definition.source != "LOCAL":
            continue
        params = normalized_indicator_parameters(node)
        required = definition.required_history(params)
        if requires_cross:
            required += 1
        result[node.timeframe] = max(result.get(node.timeframe, 0), required)
    return result


def _portfolio_fields(node: ExpressionNode) -> frozenset[str]:
    return frozenset(
        item.field or ""
        for item in _walk(node)
        if item.type is ExpressionType.PORTFOLIO
    )


def _parse_time(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeDataError("MARKET_TIME_MISSING", f"{field} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeDataError("MARKET_TIME_INVALID", f"{field} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeDataError("MARKET_TIME_INVALID", f"{field} timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _decimal(value: Any, *, field: str, minimum: Decimal | None = None) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise RuntimeDataError("MARKET_VALUE_INVALID", f"{field} is not numeric") from exc
    if not parsed.is_finite() or (minimum is not None and parsed < minimum):
        raise RuntimeDataError("MARKET_VALUE_INVALID", f"{field} is outside bounds")
    return parsed


def _normalized_indicator_values(
    raw: Any, *, field: str
) -> dict[str, IndicatorValue]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RuntimeDataError(
            "INDICATOR_VALUES_INVALID",
            f"{field} must be an object of normalized IndicatorValue envelopes",
            retryable=False,
        )
    result: dict[str, IndicatorValue] = {}
    for key, envelope in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise RuntimeDataError(
                "INDICATOR_VALUES_INVALID",
                f"{field} contains an invalid key",
                retryable=False,
            )
        if not isinstance(envelope, dict):
            raise RuntimeDataError(
                "INDICATOR_VALUES_INVALID",
                f"{field}.{key} must be a normalized IndicatorValue envelope",
                retryable=False,
            )
        raw_value = envelope.get("value")
        if raw_value is None:
            raise RuntimeDataError(
                "INDICATOR_PROVIDER_PARTIAL_DATA",
                f"{field}.{key} has no value",
                retryable=False,
            )
        scalar = raw_value if isinstance(raw_value, bool) else _decimal(
            raw_value, field=f"{field}.{key}.value"
        )
        indicator = envelope.get("indicator")
        source = envelope.get("source")
        provider = envelope.get("provider")
        observed_at = envelope.get("observed_at")
        if not all(isinstance(item, str) and item.strip() for item in (indicator, source, provider)):
            raise RuntimeDataError(
                "INDICATOR_VALUES_INVALID",
                f"{field}.{key} is missing indicator/source/provider provenance",
                retryable=False,
            )
        if observed_at is None:
            raise RuntimeDataError(
                "INDICATOR_PROVIDER_PARTIAL_DATA",
                f"{field}.{key} is missing observed_at",
                retryable=False,
            )
        data_timestamp = envelope.get("data_timestamp")
        try:
            parsed_observed_at = _parse_time(observed_at, field=f"{field}.{key}.observed_at")
            parsed_data_timestamp = (
                _parse_time(data_timestamp, field=f"{field}.{key}.data_timestamp")
                if data_timestamp is not None
                else None
            )
        except RuntimeDataError as exc:
            raise RuntimeDataError(
                "INDICATOR_VALUES_INVALID",
                str(exc),
                retryable=False,
            ) from exc
        parameters = envelope.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise RuntimeDataError(
                "INDICATOR_VALUES_INVALID",
                f"{field}.{key}.parameters must be an object",
                retryable=False,
            )
        result[key] = IndicatorValue(
            value=scalar,
            indicator=indicator.strip().upper(),
            source=source.strip().upper(),
            provider=provider.strip().upper(),
            observed_at=parsed_observed_at,
            data_timestamp=parsed_data_timestamp,
            calculation_version=envelope.get("calculation_version"),
            output=str(envelope.get("output") or "VALUE").strip().upper(),
            timeframe=(
                str(envelope["timeframe"]).strip().upper()
                if envelope.get("timeframe") is not None
                else None
            ),
            parameters=parameters,
            market_data_source_id=(
                str(envelope["market_data_source_id"])
                if envelope.get("market_data_source_id") is not None
                else None
            ),
        )
    return result

def _canonical_hash(value: Mapping[str, Any]) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class HttpRuntimeClient:
    def __init__(
        self,
        *,
        trading_api_url: str,
        market_api_url: str,
        timeout_seconds: float = 8.0,
        price_resolver: MarketPriceResolver | None = None,
        bar_resolver: BarResolver | None = None,
    ) -> None:
        if not trading_api_url.strip() or not market_api_url.strip():
            raise RuntimeDataError(
                "CONDITIONAL_RULE_API_URL_REQUIRED",
                "Trading and Market API URLs are required",
                retryable=False,
            )
        self.trading_api_url = trading_api_url.rstrip("/")
        self.market_api_url = market_api_url.rstrip("/")
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 30.0))
        self._price_resolver = price_resolver
        self._bar_resolver = bar_resolver
        self._cycle_prices: dict[str, tuple[Decimal, datetime, dict[str, Any]]] = {}
        self._cycle_prices_lock = threading.Lock()
        self._price_resolver_lock = threading.Lock()
        self._bar_resolver_lock = threading.Lock()

    def begin_cycle(self) -> None:
        """Drop the bounded per-cycle quote snapshot before polling again."""

        with self._cycle_prices_lock:
            self._cycle_prices.clear()

    @staticmethod
    def _service_token() -> str:
        secret = os.getenv("TRADING_INTERNAL_SERVICE_AUTH_SECRET", "")
        issuer = os.getenv(
            "TRADING_INTERNAL_SERVICE_AUTH_ISSUER", "hedgefund-service-issuer"
        ).strip()
        audience = os.getenv(
            "TRADING_INTERNAL_SERVICE_AUTH_AUDIENCE", "trading-api"
        ).strip()
        normalized = secret.strip().casefold()
        if (
            len(secret) < 32
            or not issuer
            or not audience
            or any(marker in normalized for marker in ("change_me", "placeholder", "example"))
        ):
            raise RuntimeDataError(
                "CONDITIONAL_RULE_INTERNAL_AUTH_INVALID",
                "conditional rule worker internal authentication is not configured",
                retryable=False,
            )
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": audience,
            "sub": "conditional-rule-worker",
            "department": "trading-department",
            "service": "conditional-rule-worker",
            "scopes": [INTERNAL_SCOPE],
            "jti": str(uuid4()),
            "iat": now,
            "nbf": now - 1,
            "exp": now + 30,
        }
        return jwt.encode(claims, secret, algorithm="HS256")

    def _json(
        self,
        url: str,
        *,
        method: str = "GET",
        internal_auth: bool = False,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if internal_auth:
            headers["Authorization"] = f"Bearer {self._service_token()}"
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            error_code = "CONDITIONAL_RULE_UPSTREAM_REJECTED"
            error_message = f"upstream HTTP {exc.code}"
            try:
                payload = json.loads(detail)
                if isinstance(payload, dict):
                    candidate = payload.get("error_code")
                    message = payload.get("message")
                    if isinstance(candidate, str) and candidate.strip():
                        error_code = candidate.strip()[:128]
                    if isinstance(message, str) and message.strip():
                        error_message = message.strip()[:1000]
            except json.JSONDecodeError:
                if detail.strip():
                    error_message = f"{error_message}: {detail.strip()[:800]}"
            raise RuntimeDataError(
                error_code,
                error_message,
                # A 409 from Trading is a deterministic admission rejection
                # (closed market, insufficient funds, changed authority, ...).
                # Retrying it later could submit a stale trigger.
                retryable=exc.code >= 500 or exc.code in {408, 425, 429},
                status_code=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeDataError(
                "CONDITIONAL_RULE_UPSTREAM_UNAVAILABLE",
                "conditional rule upstream is unavailable",
                retryable=True,
            ) from exc

    def _context(self, rule_id: UUID) -> dict[str, Any]:
        value = self._json(
            f"{self.trading_api_url}/trading/v1/conditional-rule-executions/"
            f"rules/{rule_id}/context",
            internal_auth=True,
        )
        if not isinstance(value, dict):
            raise RuntimeDataError("TRADING_CONTEXT_INVALID", "Trading context is invalid")
        return value

    def _snapshot(
        self, symbol: str, instrument_id: UUID | None = None
    ) -> tuple[Decimal, datetime, dict[str, Any]]:
        normalized = symbol.strip().upper()
        cache_key = f"{normalized}:{instrument_id or ''}"
        with self._cycle_prices_lock:
            cached = self._cycle_prices.get(cache_key)
        if cached is not None:
            return cached
        resolver = self._price_resolver
        if resolver is None:
            with self._price_resolver_lock:
                resolver = self._price_resolver
                if resolver is None:
                    try:
                        # The collection layer owns the single LS WebSocket.
                        # When its market DB is wired, consume that shared tick
                        # stream instead of opening one REST request per rule.
                        if os.getenv(
                            "CONDITIONAL_RULE_MARKET_DATABASE_URL", ""
                        ).strip():
                            resolver = LSTimescaleMarketPriceResolver.from_env()
                        else:
                            # Compatibility for local deployments that have
                            # not yet wired the market DB overlay.
                            resolver = LSPaperMarketPriceResolver.from_env()
                    except MarketPriceResolverError as exc:
                        raise RuntimeDataError(exc.code, str(exc), retryable=exc.retryable) from exc
                    self._price_resolver = resolver
        try:
            shared_snapshot = getattr(resolver, "snapshot_for_instrument", None)
            if instrument_id is not None and callable(shared_snapshot):
                snapshot = shared_snapshot(normalized, instrument_id)
            else:
                snapshot = resolver.snapshot(normalized)
        except MarketPriceResolverError as exc:
            raise RuntimeDataError(exc.code, str(exc), retryable=exc.retryable) from exc
        value = {
            "last_trade": {
                "price": str(snapshot.price),
                "event_time": snapshot.observed_at.isoformat(),
            },
            "source": snapshot.source,
            "observed_at": snapshot.observed_at.isoformat(),
        }
        result = (snapshot.price, snapshot.observed_at, value)
        with self._cycle_prices_lock:
            cached = self._cycle_prices.get(cache_key)
            if cached is not None:
                return cached
            self._cycle_prices[cache_key] = result
            return result

    def _bars(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        """확정봉을 LS 통합차트에서 직접 받는다 - market_bars 는 읽지 않는다.

        예전에는 market-api 의 ``/bars?interval=...`` 을 호출했고, 그 뒤에는
        야간 백필이 채우는 ``market.market_bars`` 가 있었다. 그 잡이 배포
        이미지의 스케줄러에 등록돼 있지 않아 1M 이 영구 0행이었고, 응답이
        HTTP 200 + ``[]`` 라 실패가 정상 응답으로 위장됐다(2026-08-26 실측).
        야간 백필은 원리상 당일 장중 규칙을 만족시킬 수 없으므로, 실시간
        평가는 조달처를 브로커 차트로 옮긴다. market_bars 는 백테스트·PIT
        재현용 아카이브로 그대로 둔다(개발원칙 5).
        """

        resolver = self._bar_resolver
        if resolver is None:
            with self._bar_resolver_lock:
                resolver = self._bar_resolver
                if resolver is None:
                    try:
                        resolver = LSChartBarResolver.from_env()
                    except BarResolverError as exc:
                        raise RuntimeDataError(
                            exc.code, str(exc), retryable=exc.retryable
                        ) from exc
                    self._bar_resolver = resolver
        try:
            candles = resolver.bars(symbol, timeframe, limit)
        except BarResolverError as exc:
            raise RuntimeDataError(exc.code, str(exc), retryable=exc.retryable) from exc
        return sorted(candles, key=lambda item: item.bucket_time)

    def load_inputs(self, rule: ActiveRule) -> RuntimeInputs:
        context = self._context(rule.rule_id)
        if int(context.get("rule_version", 0)) != rule.rule_version:
            raise RuntimeDataError(
                "RULE_VERSION_CHANGED",
                "rule version changed while runtime context was loaded",
                retryable=False,
            )
        if str(context.get("spec_sha256")) != rule.spec_sha256:
            raise RuntimeDataError(
                "RULE_FINGERPRINT_CHANGED",
                "rule fingerprint changed while runtime context was loaded",
                retryable=False,
            )
        portfolio_raw = context.get("portfolio")
        instrument = context.get("instrument")
        if not isinstance(portfolio_raw, dict) or not isinstance(instrument, dict):
            raise RuntimeDataError("TRADING_CONTEXT_INVALID", "Trading portfolio context is invalid")

        current_price, quote_at, _ = self._snapshot(
            rule.spec.symbol, rule.spec.instrument_id
        )
        history = _required_history(rule)
        bars = {
            timeframe: self._bars(
                rule.spec.symbol,
                timeframe,
                min(required + 2, 2000),
            )
            for timeframe, required in history.items()
        }
        for timeframe, required in history.items():
            final_count = sum(1 for candle in bars[timeframe] if candle.is_final)
            if final_count < required:
                raise EvaluationError(
                    "INSUFFICIENT_HISTORY",
                    f"{timeframe.value} requires {required} final bars; got {final_count}",
                )

        position_quantity = _decimal(
            portfolio_raw.get("position_quantity", "0"),
            field="position_quantity",
            minimum=Decimal("0"),
        )
        sellable_quantity = _decimal(
            portfolio_raw.get("sellable_quantity", "0"),
            field="sellable_quantity",
            minimum=Decimal("0"),
        )
        average_cost = _decimal(
            portfolio_raw.get("average_cost", "0"),
            field="average_cost",
            minimum=Decimal("0"),
        )
        available_cash = _decimal(
            portfolio_raw.get("available_cash", "0"),
            field="available_cash",
            minimum=Decimal("0"),
        )
        lot_size = _decimal(
            instrument.get("lot_size", "1"),
            field="lot_size",
            minimum=Decimal("0.0000000001"),
        )

        requested_fields = _portfolio_fields(rule.spec.condition)
        market_value = position_quantity * current_price
        values: dict[str, Decimal] = {
            "POSITION_QUANTITY": position_quantity,
            "SELLABLE_QUANTITY": sellable_quantity,
            "AVG_ENTRY_PRICE": average_cost,
            "MARKET_VALUE": market_value,
            "AVAILABLE_CASH": available_cash,
            "UNREALIZED_PNL": (current_price - average_cost) * position_quantity,
        }
        if "PNL_PERCENT" in requested_fields:
            if position_quantity <= 0 or average_cost <= 0:
                raise RuntimeDataError(
                    "POSITION_COST_BASIS_UNAVAILABLE",
                    "PNL_PERCENT requires a positive current position and average cost",
                    retryable=False,
                )
            values["PNL_PERCENT"] = current_price / average_cost - Decimal("1")

        if requested_fields & {"PORTFOLIO_NAV", "POSITION_WEIGHT"}:
            holdings = portfolio_raw.get("holdings")
            if not isinstance(holdings, list):
                raise RuntimeDataError("TRADING_CONTEXT_INVALID", "holdings are missing")
            nav = available_cash
            for holding in holdings:
                if not isinstance(holding, dict):
                    raise RuntimeDataError("TRADING_CONTEXT_INVALID", "holding is invalid")
                quantity = _decimal(
                    holding.get("quantity", "0"),
                    field="holding.quantity",
                    minimum=Decimal("0"),
                )
                symbol = str(holding.get("symbol", ""))
                holding_instrument_id = holding.get("instrument_id")
                if symbol == rule.spec.symbol:
                    price = current_price
                else:
                    # The shared tick feed is keyed by the canonical
                    # instrument identity, not by a potentially ambiguous
                    # display symbol.  Trading's holdings read model already
                    # carries this field; the REST compatibility resolver can
                    # still use the symbol when running without the feed.
                    parsed_instrument_id = None
                    if holding_instrument_id is not None:
                        try:
                            parsed_instrument_id = UUID(str(holding_instrument_id))
                        except (ValueError, AttributeError) as exc:
                            raise RuntimeDataError(
                                "TRADING_CONTEXT_INVALID",
                                "holding.instrument_id is invalid",
                                retryable=False,
                            ) from exc
                    price = self._snapshot(symbol, parsed_instrument_id)[0]
                nav += quantity * price
            if nav <= 0:
                raise RuntimeDataError(
                    "PORTFOLIO_NAV_UNAVAILABLE",
                    "portfolio NAV must be positive",
                    retryable=False,
                )
            values["PORTFOLIO_NAV"] = nav
            values["POSITION_WEIGHT"] = market_value / nav

        evaluation_context = IndicatorEngine().build_context(
            rule.spec,
            bars=bars,
            portfolio={key: value for key, value in values.items() if key in requested_fields},
            current_market={"LAST_PRICE": current_price},
            external_indicators=_normalized_indicator_values(context.get("indicator_values"), field="indicator_values"),
            previous_external_indicators=_normalized_indicator_values(context.get("previous_indicator_values"), field="previous_indicator_values"),
            market_data_source_id=str(context["market_data_source_id"]) if context.get("market_data_source_id") is not None else None,
            calculation_profile=str(context.get("calculation_profile", "DEFAULT")),
            current_observed_at=(
                quote_at
                if rule.spec.evaluation.clock is EvaluationClock.QUOTE
                else None
            ),
        )
        watermark = (
            evaluation_context.current.observed_at
            if rule.spec.evaluation.clock is EvaluationClock.BAR_CLOSE
            else quote_at
        )
        key = (
            f"BAR_CLOSE:{rule.spec.evaluation.primary_timeframe.value}:{watermark.isoformat()}"
            if rule.spec.evaluation.clock is EvaluationClock.BAR_CLOSE
            else f"QUOTE:{quote_at.isoformat()}"
        )
        hash_payload = {
            "rule_id": str(rule.rule_id),
            "rule_version": rule.rule_version,
            "evaluation_key": key,
            "market_data_source_id": evaluation_context.market_data_source_id,
            "calculation_profile": evaluation_context.calculation_profile,
            "market": {
                key: str(value)
                for key, value in sorted(evaluation_context.current.market.items())
            },
            "portfolio": {
                key: str(value)
                for key, value in sorted(evaluation_context.current.portfolio.items())
            },
            "indicators": {
                key: str(value)
                for key, value in sorted(evaluation_context.current.indicators.items())
            },
            "previous": (
                {
                    "observed_at": evaluation_context.previous.observed_at.isoformat(),
                    "market": {
                        key: str(value)
                        for key, value in sorted(
                            evaluation_context.previous.market.items()
                        )
                    },
                    "indicators": {
                        key: str(value)
                        for key, value in sorted(
                            evaluation_context.previous.indicators.items()
                        )
                    },
                }
                if evaluation_context.previous is not None
                else None
            ),
        }
        age = (datetime.now(timezone.utc) - quote_at).total_seconds()
        return RuntimeInputs(
            evaluation_context=evaluation_context,
            evaluation_key=key,
            context_sha256=_canonical_hash(hash_payload),
            data_watermark=watermark,
            membership_active=context.get("membership_active") is True,
            fund_active=context.get("fund_active") is True,
            book_active=context.get("book_active") is True,
            market_session_available=context.get("market_session_available") is True,
            market_open=context.get("market_open") is True,
            data_complete=all(
                any(candle.is_final for candle in series) for series in bars.values()
            ) if bars else True,
            quote_fresh=-5 <= age <= rule.spec.evaluation.max_data_age_seconds,
            current_price=current_price,
            available_cash=available_cash,
            position_quantity=position_quantity,
            sellable_quantity=sellable_quantity,
            lot_size=lot_size,
        )

    def submit(self, execution: SubmitReadyExecution) -> UUID:
        value = self._json(
            f"{self.trading_api_url}/trading/v1/conditional-rule-executions/"
            f"{execution.rule_execution_id}/submit",
            method="POST",
            internal_auth=True,
        )
        if not isinstance(value, dict):
            raise RuntimeDataError("TRADING_SUBMISSION_INVALID", "Trading response is invalid")
        try:
            return UUID(str(value["directive_id"]))
        except (KeyError, ValueError) as exc:
            raise RuntimeDataError(
                "TRADING_SUBMISSION_INVALID",
                "Trading response has no directive identity",
                retryable=True,
            ) from exc


class ConditionalRuleWorker:
    def __init__(
        self,
        store: WorkerStore,
        client: RuntimeClient,
        *,
        batch_size: int = 100,
        max_workers: int = 8,
        history_backoff_seconds: float = 300.0,
    ) -> None:
        self.store = store
        self.client = client
        self.batch_size = max(1, min(int(batch_size), 1000))
        self.max_workers = max(1, min(int(max_workers), 32, self.batch_size))
        self.history_backoff_seconds = max(
            30.0, min(float(history_backoff_seconds), 3600.0)
        )
        self._history_backoff_until: dict[tuple[UUID, int], float] = {}
        self._active_offset = 0

    @staticmethod
    def _merge_counts(total: dict[str, int], delta: Mapping[str, int]) -> None:
        for key, value in delta.items():
            total[key] = total.get(key, 0) + int(value)

    def _parallel_map(self, items: list[Any], function: Any) -> list[dict[str, int]]:
        if not items:
            return []
        if self.max_workers == 1 or len(items) == 1:
            return [function(item) for item in items]
        results: list[dict[str, int]] = []
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(items)),
            thread_name_prefix="conditional-rule",
        ) as executor:
            futures = {executor.submit(function, item): item for item in items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    results.append(future.result())
                except Exception:  # noqa: BLE001 - isolate one rule from the batch.
                    rule_id = getattr(item, "rule_id", "unknown")
                    LOG.exception(
                        "conditional rule parallel evaluation failed",
                        extra={"rule_id": str(rule_id)},
                    )
                    results.append({"errors": 1})
        return results

    def _submit(self, execution: SubmitReadyExecution) -> bool:
        self.store.mark_submitting(execution.rule_execution_id)
        try:
            directive_id = self.client.submit(execution)
        except RuntimeDataError as exc:
            if exc.retryable:
                self.store.mark_retryable_failure(
                    execution.rule_execution_id, code=exc.code, message=str(exc)
                )
            else:
                self.store.mark_terminal_failure(
                    execution.rule_execution_id, code=exc.code, message=str(exc)
                )
            LOG.warning(
                "conditional execution submission failed",
                extra={
                    "rule_execution_id": str(execution.rule_execution_id),
                    "code": exc.code,
                    "retryable": exc.retryable,
                },
            )
            return False
        self.store.mark_submitted(execution.rule_execution_id, directive_id=directive_id)
        return True

    def _guard_claim(self, rule: ActiveRule, claim: TriggerClaim) -> bool:
        try:
            inputs = self.client.load_inputs(rule)
        except RuntimeDataError as exc:
            LOG.warning(
                "claimed conditional trigger is waiting for runtime context",
                extra={
                    "rule_id": str(rule.rule_id),
                    "trigger_id": claim.trigger_id,
                    "code": getattr(exc, "code", "RUNTIME_CONTEXT_FAILED"),
                },
            )
            if not exc.retryable:
                self.store.create_execution(
                    rule,
                    claim,
                    allowed=False,
                    guard_code=exc.code,
                    quantity=None,
                )
            return False
        except EvaluationError as exc:
            LOG.warning(
                "claimed conditional trigger is waiting for complete market data",
                extra={
                    "rule_id": str(rule.rule_id),
                    "trigger_id": claim.trigger_id,
                    "code": exc.code,
                },
            )
            return False
        decision: GuardDecision = guard_rule_execution(rule.spec, inputs.guard(rule))
        execution = self.store.create_execution(
            rule,
            claim,
            allowed=decision.allowed,
            guard_code=decision.code,
            quantity=decision.quantity,
        )
        return execution is not None and self._submit(execution)

    def _recover_claimed(self, item: tuple[ActiveRule, TriggerClaim]) -> dict[str, int]:
        rule, claim = item
        return {
            "claimed_recovered": 1,
            "submitted": int(self._guard_claim(rule, claim)),
        }

    def _evaluate_active(self, rule: ActiveRule) -> dict[str, int]:
        counts = {"evaluated": 0, "triggered": 0, "submitted": 0, "errors": 0}
        backoff_key = (rule.rule_id, rule.rule_version)
        now = time.monotonic()
        backoff_until = self._history_backoff_until.get(backoff_key)
        if backoff_until is not None:
            if now < backoff_until:
                return {"deferred": 1}
            self._history_backoff_until.pop(backoff_key, None)
        try:
            inputs = self.client.load_inputs(rule)
        except (RuntimeDataError, EvaluationError) as exc:
            counts["errors"] += 1
            if isinstance(exc, EvaluationError) and exc.code == "INSUFFICIENT_HISTORY":
                self._history_backoff_until[backoff_key] = (
                    time.monotonic() + self.history_backoff_seconds
                )
                LOG.warning(
                    "conditional rule waiting for market history",
                    extra={
                        "rule_id": str(rule.rule_id),
                        "code": exc.code,
                        "retry_after_seconds": int(self.history_backoff_seconds),
                    },
                )
                return counts
            LOG.warning(
                "conditional rule runtime data unavailable",
                extra={
                    "rule_id": str(rule.rule_id),
                    "code": getattr(exc, "code", "RUNTIME_DATA_INVALID"),
                },
            )
            return counts
        self._history_backoff_until.pop(backoff_key, None)
        try:
            result = evaluate_condition(rule.spec, inputs.evaluation_context)
        except EvaluationError as exc:
            counts["errors"] += 1
            self.store.record_error(
                rule,
                evaluation_key=inputs.evaluation_key,
                context_sha256=inputs.context_sha256,
                data_watermark=inputs.data_watermark,
                error_code=exc.code,
                error_message=str(exc),
            )
            return counts
        counts["evaluated"] += 1
        if not result:
            self.store.record_false(
                rule,
                evaluation_key=inputs.evaluation_key,
                context_sha256=inputs.context_sha256,
                data_watermark=inputs.data_watermark,
            )
            return counts
        claim = self.store.claim_true(
            rule,
            evaluation_key=inputs.evaluation_key,
            context_sha256=inputs.context_sha256,
            data_watermark=inputs.data_watermark,
        )
        if claim is None:
            return counts
        counts["triggered"] += 1
        counts["submitted"] += int(self._guard_claim(rule, claim))
        return counts

    def _next_active_batch(self) -> list[ActiveRule]:
        """Rotate bounded pages so rules after the first page are not starved."""

        batch = self.store.list_active(
            limit=self.batch_size,
            offset=self._active_offset,
        )
        if not batch and self._active_offset:
            self._active_offset = 0
            batch = self.store.list_active(limit=self.batch_size, offset=0)
        if len(batch) < self.batch_size:
            self._active_offset = 0
        else:
            self._active_offset += len(batch)
        return batch

    def process_once(self) -> dict[str, int]:
        begin_cycle = getattr(self.client, "begin_cycle", None)
        if callable(begin_cycle):
            begin_cycle()
        counts = {
            "expired": self.store.expire_due(),
            "retried": 0,
            "claimed_recovered": 0,
            "evaluated": 0,
            "triggered": 0,
            "submitted": 0,
            "errors": 0,
            "deferred": 0,
        }
        # This is a durable state transition, not a second order path.  The
        # existing conditional-rule worker activates a pending rule only after
        # the linked immediate PAPER request is COMPLETED.
        activate_ready_bundles = getattr(self.store, "activate_ready_bundles", None)
        if callable(activate_ready_bundles):
            try:
                activate_ready_bundles(limit=self.batch_size)
            except RuleWorkerStoreError as exc:
                counts["errors"] += 1
                LOG.warning(
                    "deferred compound PAPER activation unavailable",
                    extra={"code": exc.code, "retryable": exc.retryable},
                )
        for execution in self.store.list_submit_ready(limit=self.batch_size):
            counts["retried"] += 1
            counts["submitted"] += int(self._submit(execution))
        for delta in self._parallel_map(
            self.store.list_claimed(limit=self.batch_size),
            self._recover_claimed,
        ):
            self._merge_counts(counts, delta)
        for delta in self._parallel_map(
            self._next_active_batch(),
            self._evaluate_active,
        ):
            self._merge_counts(counts, delta)
        return counts


def _settings() -> tuple[PostgresRuleWorkerStore, HttpRuntimeClient, float, int, int]:
    dsn = os.getenv("CONDITIONAL_RULE_DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeDataError(
            "CONDITIONAL_RULE_DATABASE_REQUIRED",
            "CONDITIONAL_RULE_DATABASE_URL is required",
            retryable=False,
        )
    store = PostgresRuleWorkerStore(
        dsn,
        role=os.getenv(
            "CONDITIONAL_RULE_WORKER_DATABASE_ROLE", "svc_conditional_rule_worker"
        ).strip(),
    )
    client = HttpRuntimeClient(
        trading_api_url=os.getenv("TRADING_API_URL", "http://trading-api:8000"),
        market_api_url=os.getenv("MARKET_API_URL", "http://market-api:8036"),
        timeout_seconds=float(os.getenv("CONDITIONAL_RULE_HTTP_TIMEOUT_SECONDS", "8")),
    )
    poll = max(float(os.getenv("CONDITIONAL_RULE_WORKER_POLL_SECONDS", "30")), 0.1)
    batch = max(int(os.getenv("CONDITIONAL_RULE_WORKER_BATCH_SIZE", "100")), 1)
    workers = max(
        1,
        min(int(os.getenv("CONDITIONAL_RULE_WORKER_MAX_WORKERS", "8")), 32),
    )
    return store, client, poll, batch, workers


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store, client, poll, batch, workers = _settings()
    worker = ConditionalRuleWorker(
        store,
        client,
        batch_size=batch,
        max_workers=workers,
        history_backoff_seconds=float(
            os.getenv("CONDITIONAL_RULE_HISTORY_BACKOFF_SECONDS", "300")
        ),
    )
    if args.healthcheck:
        store.healthcheck()
        HttpRuntimeClient._service_token()
        # In the deployed PAPER stack the market DB is the shared LS realtime
        # feed boundary.  Prove that optional wiring is reachable during the
        # healthcheck; otherwise the process can be healthy while silently
        # falling back to per-rule REST polling.
        if os.getenv("CONDITIONAL_RULE_MARKET_DATABASE_URL", "").strip():
            resolver = LSTimescaleMarketPriceResolver.from_env()
            resolver.close()
        print("conditional-rule-worker ready")
        return 0
    if args.once:
        print(json.dumps(worker.process_once(), sort_keys=True))
        return 0
    while True:
        try:
            result = worker.process_once()
            LOG.info("conditional rule cycle", extra=result)
        except Exception:
            LOG.exception("conditional rule cycle failed closed")
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ConditionalRuleWorker",
    "HttpRuntimeClient",
    "RuntimeDataError",
    "RuntimeInputs",
    "main",
]
