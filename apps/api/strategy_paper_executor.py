"""Allowlisted PAPER strategy runtime for approved research Bundles.

This process is intentionally a small deterministic signal worker, not Hermes
and not a broker adapter. It reads one immutable SMA 5/20/60 Bundle, obtains
1-minute consolidated candles from market-api, aggregates complete 3-minute
bars, and records entry/take-profit signals. No broker credential, Docker
socket, or Trading service proof is present in this container.

The explicit ``signal_only`` state is important: a running container is not
reported as an automatic-order system until the separate Trading-owned
StrategySignal -> OrderIntent -> Risk -> OMS adapter has been connected and
verified.
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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


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
    if not isinstance(execution, dict) or execution.get("orders_enabled") is not False:
        raise RuntimeError("strategy bundle must be signal-only")
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


def _has_contiguous_bars(bars: list[dict[str, Any]], count: int = 60) -> bool:
    if len(bars) < count:
        return False
    stamps = [_timestamp(row.get("bucket_time")) for row in bars[-count:]]
    return all(
        left is not None
        and right is not None
        and (right - left).total_seconds() == 180
        for left, right in itertools.pairwise(stamps)
    )


def _sma(values: list[Decimal], length: int) -> Decimal:
    return sum(values[-length:], Decimal(0)) / Decimal(length)


class PaperSignalRuntime:
    def __init__(self, bundle: dict[str, Any], *, state_dir: Path, market_api: str) -> None:
        self.bundle = bundle
        self.state_dir = state_dir
        self.market_api = market_api
        self.deployment_id = str(bundle["deployment_id"])
        self.state_path = state_dir / f"{self.deployment_id}.json"
        self.signal_path = state_dir / f"{self.deployment_id}.signals.jsonl"
        self.signal_lock_path = state_dir / f"{self.deployment_id}.signals.lock"
        default_state: dict[str, Any] = {
            "schema": "strategy-paper-runtime-state.v1",
            "deployment_id": self.deployment_id,
            "status": "STARTING",
            "execution_status": "SIGNAL_ONLY",
            "orders_enabled": False,
            "symbols": list(bundle["symbols"]),
            "last_poll_at": None,
            "last_bar_by_symbol": {},
            "signals_generated": 0,
            "signals_by_symbol": {},
            "positions_simulated": {},
            "errors": [],
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
            "execution_status": "SIGNAL_ONLY",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        event["signal_key"] = self._signal_key(event)
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
            bars = _aggregate_3m(_fetch_1m(self.market_api, symbol))
            if not _has_contiguous_bars(bars, 60):
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
    runtime = PaperSignalRuntime(
        bundle,
        state_dir=Path(os.getenv("STRATEGY_PAPER_RUNTIME_STATE_DIR", "/var/lib/strategy-runtime")),
        market_api=os.getenv("MARKET_API_URL", "http://market-api:8036"),
    )
    runtime.run(once=args.once)


if __name__ == "__main__":
    main()
