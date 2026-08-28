"""Allowlisted PAPER strategy runtime for approved research Bundles.

This process is intentionally a small deterministic signal worker, not Hermes
and not a broker adapter. It reads one immutable SMA 5/20/60 Bundle, tail-polls
raw trade and quote rows from read-only TimescaleDB, aggregates finalized
3-minute bars, and records entry/take-profit signals. When the Bundle enables
PAPER execution, it sends the signal through the private runtime-control
order boundary; the child still receives no broker credential or Docker
socket. The Trading API owns the PAPER account, quote/session, cash/position,
idempotency, and LS PAPER adapter checks.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import itertools
import json
import math
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, HTTPError, urlopen


def _poll_seconds() -> float:
    try:
        value = float(os.getenv("STRATEGY_PAPER_POLL_SECONDS", "15"))
    except ValueError:
        value = 15.0
    if not math.isfinite(value):
        value = 15.0
    return max(5.0, min(value, 300.0))


POLL_SECONDS = _poll_seconds()
_STOP = False


def _stop(_signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _json_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def _read_bundle(path: Path, expected_hash: str) -> dict[str, Any]:
    if _json_hash(path) != expected_hash:
        raise RuntimeError("strategy bundle hash mismatch")
    bundle = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(bundle, dict):
        raise TypeError("strategy bundle must be an object")
    if bundle.get("schema") != "autonomous-strategy-paper-bundle.v1":
        raise RuntimeError("unsupported strategy bundle schema")
    if bundle.get("mode") != "PAPER":
        raise RuntimeError("strategy runtime is PAPER only")
    execution = bundle.get("execution")
    if not isinstance(execution, dict):
        raise RuntimeError("strategy bundle execution metadata is required")
    orders_enabled = execution.get("orders_enabled")
    signal_only = execution.get("signal_only")
    if not isinstance(orders_enabled, bool) or signal_only is not (not orders_enabled):
        raise RuntimeError("strategy bundle execution mode is inconsistent")
    if orders_enabled:
        quantity = _decimal(execution.get("order_quantity"))
        if quantity is None or quantity != quantity.to_integral_value() or quantity <= 0:
            raise RuntimeError("strategy bundle order_quantity must be a positive integer")
        if not isinstance(execution.get("trading_route"), str) or not execution["trading_route"].strip():
            raise RuntimeError("strategy bundle PAPER trading route is required")
    strategy = bundle.get("strategy")
    if not isinstance(strategy, dict) or strategy.get("kind") != "SMA_ALIGNMENT":
        raise RuntimeError("unsupported strategy bundle kind")
    if (strategy.get("timeframe"), strategy.get("fast"), strategy.get("mid"), strategy.get("slow")) != (
        "3M", 5, 20, 60
    ):
        raise RuntimeError("unsupported SMA alignment parameters")
    symbols = bundle.get("symbols")
    if not isinstance(symbols, list) or not symbols or any(
        not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isdigit()
        for symbol in symbols
    ):
        raise RuntimeError("strategy bundle symbols are invalid")
    return bundle


class PaperOrderGateway:
    """Submit one idempotent signal to the private runtime-control boundary."""

    def __init__(self, *, control_url: str, token: str, timeout_seconds: float = 5.0) -> None:
        if not control_url.strip() or not token.strip():
            raise RuntimeError("PAPER order gateway is not configured")
        self.control_url = control_url.rstrip("/")
        self.token = token.strip()
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 15.0))

    def submit(
        self,
        *,
        deployment_id: str,
        symbol: str,
        side: str,
        quantity: Decimal,
        signal_key: str,
    ) -> dict[str, Any]:
        url = f"{self.control_url}/deployments/{quote(deployment_id, safe='')}/orders"
        payload = {
            "deployment_id": deployment_id,
            "symbol": symbol,
            "side": side,
            "quantity": str(quantity),
            "signal_key": signal_key,
        }
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "strategy-paper-runtime/v2",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"PAPER order gateway HTTP {response.status}")
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"PAPER order gateway HTTP {exc.code}") from exc
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(f"PAPER order gateway unavailable: {type(exc).__name__}") from exc
        if not isinstance(body, dict) or not isinstance(body.get("directive"), dict):
            raise RuntimeError("PAPER order gateway returned an invalid directive")
        return body


def _fetch_1m(market_api: str, symbol: str, limit: int = 220) -> list[dict[str, Any]]:
    url = f"{market_api.rstrip('/')}/bars/{quote(symbol, safe='')}?interval=1M&source=consolidated&limit={limit}"
    request = Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "strategy-paper-runtime/v1"})
    with urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"market-api HTTP {response.status}")
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, list):
        raise TypeError("market-api bars response is not a list")
    return [row for row in body if isinstance(row, dict)]


def _fetch_instrument_id(market_api: str, symbol: str) -> str:
    """Resolve the canonical UUID once; live market rows use instrument_id."""

    url = f"{market_api.rstrip('/')}/instrument/{quote(symbol, safe='')}"
    request = Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "strategy-paper-runtime/v1"},
    )
    with urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"market-api instrument HTTP {response.status}")
        body = json.loads(response.read().decode("utf-8"))
    instrument_id = body.get("instrument_id") if isinstance(body, dict) else None
    if not isinstance(instrument_id, str) or len(instrument_id) != 36:
        raise RuntimeError("market-api returned an invalid instrument_id")
    return instrument_id


def _aggregate_3m(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate consecutive complete 1M rows using UTC epoch buckets."""

    ordered = []
    for row in rows:
        stamp = _timestamp(row.get("bucket_time"))
        op = _decimal(row.get("open"))
        high = _decimal(row.get("high"))
        low = _decimal(row.get("low"))
        close = _decimal(row.get("close"))
        if stamp is None or None in (op, high, low, close):
            continue
        ordered.append((stamp, op, high, low, close, bool(row.get("is_final", True))))
    ordered.sort(key=lambda item: item[0])
    buckets: dict[int, list[tuple[Any, ...]]] = {}
    for row in ordered:
        key = int(row[0].timestamp()) // 180
        buckets.setdefault(key, []).append(row)
    result: list[dict[str, Any]] = []
    for key, bucket in sorted(buckets.items()):
        minutes = {int(row[0].timestamp()) // 60 for row in bucket}
        if (
            len(bucket) != 3
            or len(minutes) != 3
            or not all(row[5] for row in bucket)
        ):
            continue
        bucket.sort(key=lambda item: item[0])
        if any(
            right[0].timestamp() - left[0].timestamp() != 60
            for left, right in itertools.pairwise(bucket)
        ):
            continue
        result.append(
            {
                "bucket_time": datetime.fromtimestamp(key * 180, timezone.utc).isoformat(),
                "open": str(bucket[0][1]),
                "high": str(max(row[2] for row in bucket)),
                "low": str(min(row[3] for row in bucket)),
                "close": str(bucket[-1][4]),
            }
        )
    return result


def _aggregate_tick_rows_3m(
    rows: list[dict[str, Any]], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Build finalized 3-minute OHLCV bars directly from raw trade rows.

    Unlike 1-minute chart rows, a valid tick stream does not need a trade in
    every minute.  A bucket with at least one valid trade is a real market
    observation; empty buckets stay absent and are never forward-filled.
    """

    boundary = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    buckets: dict[int, list[tuple[datetime, Decimal, Decimal]]] = {}
    for row in rows:
        stamp = _timestamp(row.get("event_time"))
        price = _decimal(row.get("price"))
        if stamp is None or price is None:
            continue
        key = int(stamp.timestamp()) // 180
        bucket_end = datetime.fromtimestamp((key + 1) * 180, timezone.utc)
        if bucket_end > boundary:
            continue
        try:
            quantity = Decimal(str(row.get("quantity", "0")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not quantity.is_finite() or quantity < 0:
            continue
        buckets.setdefault(key, []).append((stamp, price, quantity))

    result: list[dict[str, Any]] = []
    for key, bucket in sorted(buckets.items()):
        bucket.sort(key=lambda item: item[0])
        prices = [item[1] for item in bucket]
        result.append(
            {
                "bucket_time": datetime.fromtimestamp(key * 180, timezone.utc).isoformat(),
                "open": str(prices[0]),
                "high": str(max(prices)),
                "low": str(min(prices)),
                "close": str(prices[-1]),
                "volume": str(sum((item[2] for item in bucket), Decimal(0))),
                "trade_count": len(bucket),
            }
        )
    return result


def _has_contiguous_bars(bars: list[dict[str, Any]], count: int = 60) -> bool:
    """Retained strict helper for callers that need wall-clock continuity."""
    if len(bars) < count:
        return False
    stamps = [_timestamp(row.get("bucket_time")) for row in bars[-count:]]
    return all(
        left is not None
        and right is not None
        and (right - left).total_seconds() == 180
        for left, right in itertools.pairwise(stamps)
    )


def _has_sufficient_bars(bars: list[dict[str, Any]], count: int = 60) -> bool:
    """Return whether enough valid completed bars exist for the SMA window.

    A market-data provider may omit no-trade minutes and a Korean session is
    separated by an overnight boundary.  Requiring every observed 3-minute
    bar to be exactly 180 seconds after the previous one would therefore
    reject otherwise valid session data.  `_aggregate_3m` already enforces
    that each individual bar contains three distinct, consecutive finalized
    1-minute rows; the SMA window only needs `count` such observations.
    """
    if len(bars) < count:
        return False
    return all(_timestamp(row.get("bucket_time")) is not None for row in bars[-count:])


class TimescaleMarketFeed:
    """Read-only tail feed for one PAPER strategy's raw ticks and quotes.

    The feed keeps a small in-memory watermark and re-reads a three-minute
    overlap on every poll so late rows are not lost.  Raw rows are persisted
    by ``ls-realtime``; this process only reads them and derives finalized
    3-minute observations locally.
    """

    def __init__(
        self,
        dsn: str,
        *,
        market_api: str,
        # One full trading day plus the current session gives the 60-bar SMA
        # warm-up room even when the raw feed has no overnight trades.
        lookback_hours: int = 24,
        statement_timeout_ms: int = 3_000,
        batch_limit: int = 200_000,
    ) -> None:
        if not dsn.strip():
            raise RuntimeError("STRATEGY_PAPER_TIMESCALE_DATABASE_URL is required")
        import psycopg2

        self._psycopg2 = psycopg2
        self._dsn = dsn
        self._market_api = market_api
        self._lookback = timedelta(hours=max(1, min(int(lookback_hours), 24)))
        self._statement_timeout_ms = max(500, min(int(statement_timeout_ms), 10_000))
        self._batch_limit = max(1_000, min(int(batch_limit), 500_000))
        self._connection = None
        self._instrument_ids: dict[str, str] = {}
        self._watermarks: dict[str, datetime] = {}
        self._ticks: dict[str, dict[str, dict[str, Any]]] = {}

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    def _conn(self):
        if self._connection is None or self._connection.closed:
            self._connection = self._psycopg2.connect(self._dsn, connect_timeout=3)
        return self._connection

    def _instrument_id(self, symbol: str) -> str:
        instrument_id = self._instrument_ids.get(symbol)
        if instrument_id is None:
            instrument_id = _fetch_instrument_id(self._market_api, symbol)
            self._instrument_ids[symbol] = instrument_id
        return instrument_id

    def read(self, symbol: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        instrument_id = self._instrument_id(symbol)
        now = datetime.now(timezone.utc)
        watermark = self._watermarks.get(symbol)
        since = (watermark - timedelta(seconds=180)) if watermark else now - self._lookback
        connection = self._conn()
        try:
            with connection.cursor() as cursor:
                cursor.execute("set transaction read only")
                cursor.execute(
                    "select set_config('statement_timeout', %s, true)",
                    (f"{self._statement_timeout_ms}ms",),
                )
                cursor.execute(
                    """
                    select event_time, price, quantity, observed_at, source_event_id
                      from market.market_ticks
                     where instrument_id = %s
                       and event_time >= %s
                     order by event_time desc, source_event_id desc nulls last
                     limit %s
                    """,
                    (instrument_id, since, self._batch_limit),
                )
                # Bound the query at the live edge. An ascending LIMIT would
                # return only the oldest portion of a busy 24-hour window and
                # silently hide the newest bars once the batch is full.
                tick_rows = list(reversed(cursor.fetchall()))
                columns = [item[0] for item in cursor.description]
                cursor.execute(
                    """
                    select event_time, observed_at, best_bid, best_ask,
                           mid_price, spread
                      from market.market_quotes
                     where instrument_id = %s
                     order by event_time desc, received_at desc
                     limit 1
                    """,
                    (instrument_id,),
                )
                quote_row = cursor.fetchone()
                quote_columns = [item[0] for item in cursor.description]
            connection.rollback()
        except Exception as exc:
            try:
                connection.rollback()
            except Exception:  # noqa: BLE001 - connection may be broken
                pass
            self.close()
            raise RuntimeError(f"Timescale market read failed: {type(exc).__name__}") from exc

        rows = [dict(zip(columns, row)) for row in tick_rows]
        tick_store = self._ticks.setdefault(symbol, {})
        for row in rows:
            event_id = str(row.get("source_event_id") or "")
            if event_id:
                tick_store[event_id] = row
            stamp = _timestamp(row.get("event_time"))
            if stamp is not None and stamp > self._watermarks.get(symbol, since):
                self._watermarks[symbol] = stamp
        cutoff = now - self._lookback
        self._ticks[symbol] = {
            event_id: row
            for event_id, row in tick_store.items()
            if (_timestamp(row.get("event_time")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
        }
        bars = _aggregate_tick_rows_3m(list(self._ticks[symbol].values()), now=now)
        quote = dict(zip(quote_columns, quote_row)) if quote_row else None
        return bars, quote


def _sma(values: list[Decimal], length: int) -> Decimal:
    return sum(values[-length:], Decimal(0)) / Decimal(length)


class PaperSignalRuntime:
    def __init__(
        self,
        bundle: dict[str, Any],
        *,
        state_dir: Path,
        market_api: str,
        market_feed: TimescaleMarketFeed | None = None,
        order_gateway: PaperOrderGateway | None = None,
    ) -> None:
        self.bundle = bundle
        self.state_dir = state_dir
        self.market_api = market_api
        self.market_feed = market_feed
        execution = bundle.get("execution") if isinstance(bundle.get("execution"), dict) else {}
        self.orders_enabled = bool(execution.get("orders_enabled"))
        self.order_gateway = order_gateway
        if self.orders_enabled and self.order_gateway is None:
            raise RuntimeError("PAPER order gateway is required when orders_enabled=true")
        self.deployment_id = str(bundle["deployment_id"])
        self.state_path = state_dir / f"{self.deployment_id}.json"
        self.signal_path = state_dir / f"{self.deployment_id}.signals.jsonl"
        self.signal_lock_path = state_dir / f"{self.deployment_id}.signals.lock"
        default_state: dict[str, Any] = {
            "schema": "strategy-paper-runtime-state.v1",
            "deployment_id": self.deployment_id,
            "status": "STARTING",
            "execution_status": "PAPER_ORDERING" if self.orders_enabled else "SIGNAL_ONLY",
            "orders_enabled": self.orders_enabled,
            "symbols": list(bundle["symbols"]),
            "last_poll_at": None,
            "last_bar_by_symbol": {},
            "signals_generated": 0,
            "signals_by_symbol": {},
            "positions_simulated": {},
            "errors": [],
            "data_source": (
                "TIMESCALE_RAW_TICKS_QUOTES"
                if market_feed is not None
                else "MARKET_API_1M_COMPATIBILITY"
            ),
            "last_quote_by_symbol": {},
        }
        self.state = default_state
        if self.state_path.exists():
            try:
                existing = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = None
            if (
                isinstance(existing, dict)
                and existing.get("schema") == default_state["schema"]
                and existing.get("deployment_id") == self.deployment_id
            ):
                self.state = existing
        prior_orders_enabled = self.state.get("orders_enabled")
        if self.orders_enabled and prior_orders_enabled is False:
            # A legacy SIGNAL_ONLY run never owned a broker position. Do not
            # carry its simulated position/watermark into the first real
            # PAPER-order run; the immutable signal files remain as audit
            # evidence and are ignored by _restore_signal_state below.
            self.state["last_bar_by_symbol"] = {}
            self.state["positions_simulated"] = {}
        self.state["data_source"] = (
            "TIMESCALE_RAW_TICKS_QUOTES"
            if market_feed is not None
            else str(self.state.get("data_source") or "MARKET_API_1M_COMPATIBILITY")
        )
        self.state["orders_enabled"] = self.orders_enabled
        self.state["execution_status"] = (
            "PAPER_ORDERING" if self.orders_enabled else "SIGNAL_ONLY"
        )
        self.state.setdefault("last_quote_by_symbol", {})
        self._restore_signal_state()
        _write_json(self.state_path, self.state)

    @staticmethod
    def _signal_key(event: dict[str, Any]) -> str:
        return "|".join(
            str(event.get(field) or "")
            for field in ("deployment_id", "symbol", "action", "bar_time", "reason")
        )

    def _restore_signal_state(self) -> None:
        if not self.signal_path.exists():
            return
        positions: dict[str, dict[str, str]] = {}
        count = 0
        per_symbol: dict[str, int] = {}
        last_signal: dict[str, Any] | None = None
        try:
            lines = self.signal_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (
                not isinstance(event, dict)
                or event.get("schema") != "strategy-paper-signal.v1"
                or event.get("deployment_id") != self.deployment_id
            ):
                continue
            if self.orders_enabled:
                order = event.get("order")
                if not isinstance(order, dict) or not order.get("directive_id"):
                    continue
            symbol = str(event.get("symbol") or "")
            action = event.get("action")
            price = _decimal(event.get("price"))
            bar_time = str(event.get("bar_time") or "")
            if not symbol or action not in {"BUY", "SELL"} or price is None or not bar_time:
                continue
            count += 1
            per_symbol[symbol] = per_symbol.get(symbol, 0) + 1
            if action == "BUY":
                positions[symbol] = {"entry_price": str(price), "entry_bar": bar_time}
            else:
                positions.pop(symbol, None)
            last_signal = event
        self.state["signals_generated"] = count
        self.state["signals_by_symbol"] = per_symbol
        self.state["positions_simulated"] = positions
        if last_signal is not None:
            self.state["last_signal"] = last_signal

    def _save(self) -> None:
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(self.state_path, self.state)

    def _error(self, message: str) -> None:
        errors = self.state.setdefault("errors", [])
        if isinstance(errors, list):
            errors.append(message)
            del errors[:-10]
        self.state["status"] = "WAITING_FOR_MARKET_DATA"
        self._save()

    def _signal(
        self,
        symbol: str,
        action: str,
        bar: dict[str, Any],
        *,
        price: Decimal,
        reason: str,
    ) -> bool:
        event = {
            "schema": "strategy-paper-signal.v1",
            "deployment_id": self.deployment_id,
            "symbol": symbol,
            "action": action,
            "price": str(price),
            "bar_time": bar["bucket_time"],
            "reason": reason,
            "execution_status": "PAPER_ORDERING" if self.orders_enabled else "SIGNAL_ONLY",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        event["signal_key"] = self._signal_key(event)
        # The gateway is idempotent on this deterministic signal key. Submit
        # before writing the local event so a process crash cannot leave an
        # apparent signal whose order was never admitted. A retry after a
        # successful broker crossing resolves to the same durable directive.
        if self.orders_enabled:
            assert self.order_gateway is not None
            order = self.order_gateway.submit(
                deployment_id=self.deployment_id,
                symbol=symbol,
                side=action,
                quantity=Decimal(str(self.bundle["execution"]["order_quantity"])),
                signal_key=event["signal_key"],
            )
            directive = order.get("directive")
            event["order"] = {
                "execution_status": order.get("execution_status"),
                "directive_id": directive.get("directive_id") if isinstance(directive, dict) else None,
                "state": directive.get("state") if isinstance(directive, dict) else None,
                "legs": directive.get("legs") if isinstance(directive, dict) else [],
            }
        self.signal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.signal_lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                lock_handle.seek(0)
                existing_lines = (
                    self.signal_path.read_text(encoding="utf-8").splitlines()
                    if self.signal_path.exists()
                    else ()
                )
                for line in existing_lines:
                    try:
                        existing = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(existing, dict) and (
                        existing.get("signal_key") == event["signal_key"]
                        or self._signal_key(existing) == event["signal_key"]
                    ):
                        return False
                with self.signal_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        self.state["signals_generated"] = int(self.state.get("signals_generated") or 0) + 1
        per_symbol = self.state.setdefault("signals_by_symbol", {})
        if isinstance(per_symbol, dict):
            per_symbol[symbol] = int(per_symbol.get(symbol) or 0) + 1
        self.state["last_signal"] = event
        return True

    def poll_once(self) -> None:
        self.state["last_poll_at"] = datetime.now(timezone.utc).isoformat()
        any_data = False
        for symbol in self.bundle["symbols"]:
            if self.market_feed is not None:
                bars, quote = self.market_feed.read(symbol)
                if quote is not None:
                    quote_state = {
                        key: (
                            value.isoformat()
                            if isinstance(value, datetime)
                            else str(value) if isinstance(value, Decimal) else value
                        )
                        for key, value in quote.items()
                    }
                    quote_by_symbol = self.state.setdefault("last_quote_by_symbol", {})
                    if isinstance(quote_by_symbol, dict):
                        quote_by_symbol[symbol] = quote_state
            else:
                bars = _aggregate_3m(_fetch_1m(self.market_api, symbol))
            if not _has_sufficient_bars(bars, 60):
                continue
            any_data = True
            bar = bars[-1]
            if self.state.setdefault("last_bar_by_symbol", {}).get(symbol) == bar["bucket_time"]:
                continue
            closes = [_decimal(item["close"]) for item in bars]
            values = [value for value in closes if value is not None]
            if len(values) < 60:
                continue
            close = values[-1]
            fast, mid, slow = _sma(values, 5), _sma(values, 20), _sma(values, 60)
            positions = self.state.setdefault("positions_simulated", {})
            current = positions.get(symbol) if isinstance(positions, dict) else None
            if current is None and close > fast > mid > slow:
                created = self._signal(
                    symbol,
                    "BUY",
                    bar,
                    price=close,
                    reason="CLOSE_GT_SMA5_GT_SMA20_GT_SMA60",
                )
                if created and isinstance(positions, dict):
                    positions[symbol] = {"entry_price": str(close), "entry_bar": bar["bucket_time"]}
            elif isinstance(current, dict):
                entry = _decimal(current.get("entry_price"))
                target = entry * (Decimal(1) + Decimal("0.02")) if entry is not None else None
                if target is not None and close >= target:
                    created = self._signal(
                        symbol,
                        "SELL",
                        bar,
                        price=close,
                        reason="TAKE_PROFIT_2PCT",
                    )
                    if created and isinstance(positions, dict):
                        positions.pop(symbol, None)
            if isinstance(self.state.get("last_bar_by_symbol"), dict):
                self.state["last_bar_by_symbol"][symbol] = bar["bucket_time"]
        self.state["status"] = "RUNNING" if any_data else "WAITING_FOR_MARKET_DATA"
        self._save()

    def run(self, *, once: bool = False) -> None:
        while not _STOP:
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - runtime remains observable and retries
                self._error(f"{type(exc).__name__}: {exc}")
            if once:
                return
            time.sleep(POLL_SECONDS)
        self.state["status"] = "STOPPED"
        self._save()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--expected-hash", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    bundle = _read_bundle(args.bundle, args.expected_hash)
    market_api = os.getenv("MARKET_API_URL", "http://market-api:8036")
    dsn = os.getenv("STRATEGY_PAPER_TIMESCALE_DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("STRATEGY_PAPER_TIMESCALE_DATABASE_URL is required")
    feed = TimescaleMarketFeed(
        dsn,
        market_api=market_api,
        lookback_hours=int(os.getenv("STRATEGY_PAPER_TICK_LOOKBACK_HOURS", "24")),
        statement_timeout_ms=int(
            os.getenv("STRATEGY_PAPER_MARKET_STATEMENT_TIMEOUT_MS", "3000")
        ),
    )
    execution = bundle.get("execution") if isinstance(bundle.get("execution"), dict) else {}
    order_gateway = None
    if execution.get("orders_enabled") is True:
        order_gateway = PaperOrderGateway(
            control_url=os.getenv(
                "STRATEGY_PAPER_ORDER_CONTROL_URL",
                "http://strategy-runtime-control:8000",
            ),
            token=os.getenv("STRATEGY_PAPER_DEPLOYMENT_ORDER_TOKEN", ""),
            timeout_seconds=float(os.getenv("STRATEGY_PAPER_ORDER_TIMEOUT_SECONDS", "5")),
        )
    try:
        runtime = PaperSignalRuntime(
            bundle,
            state_dir=Path(os.getenv("STRATEGY_PAPER_RUNTIME_STATE_DIR", "/var/lib/strategy-runtime")),
            market_api=market_api,
            market_feed=feed,
            order_gateway=order_gateway,
        )
        runtime.run(once=args.once)
    finally:
        feed.close()


if __name__ == "__main__":
    main()
