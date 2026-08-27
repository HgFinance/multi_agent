#!/usr/bin/env python3
"""Lease and independently reproduce accepted intraday forward evidence.

The transport QA worker only accepts the immutable event and creates durable
work.  This separate process receives a database-joined, lease-fenced input
bundle through SECURITY DEFINER functions, reads the market store in a
read-only transaction, and commits either a scientific PASS/FAIL result or an
infrastructure retry.  It never promotes a strategy.
"""

from __future__ import annotations

import hmac
import json
import os
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_QA_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[3]
_QUANT_PIPELINE = (
    _REPO_ROOT / "departments" / "04-quant-backtest" / "pipeline")
_RESEARCH_CONTRACTS = (
    _REPO_ROOT / "departments" / "01-research" / "contracts")
for _path in (_QA_DIR, _QUANT_PIPELINE, _RESEARCH_CONTRACTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from audit.db_session import (  # noqa: E402
    configure_writer_connection,
    runtime_session_dsn,
)
from intraday_experiment_runner import (  # noqa: E402
    QA_REPRODUCTION_VERSION,
    preflight_qa_reproduction_runtime,
    reproduce_forward_confirmation,
)
from intraday_trial_ledger import stable_fingerprint  # noqa: E402

WORKER_VERSION = "qa-forward-reproduction-worker-v1"
DEFAULT_LEASE_SECONDS = 7_200
DEFAULT_HEARTBEAT_SECONDS = 60
DEFAULT_POLL_SECONDS = 15
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_METADATA_STATEMENT_TIMEOUT_MS = 30_000
DEFAULT_METADATA_LOCK_TIMEOUT_MS = 5_000
DEFAULT_MARKET_STATEMENT_TIMEOUT_MS = 300_000
DEFAULT_MARKET_LOCK_TIMEOUT_MS = 5_000
MIN_LEASE_SECONDS = 30
MAX_LEASE_SECONDS = 7_200

_CLAIM_SQL = """
    select audit.claim_intraday_forward_reproduction_work(%s, %s)
"""
_HEARTBEAT_SQL = """
    select audit.heartbeat_intraday_forward_reproduction_work(
      %s::uuid, %s::uuid, %s, %s)
"""
_COMPLETE_SQL = """
    select audit.complete_intraday_forward_reproduction_work(
      %s::uuid, %s::uuid, %s, %s, %s::jsonb, %s)
"""
_FAIL_SQL = """
    select audit.fail_intraday_forward_reproduction_work(
      %s::uuid, %s::uuid, %s, %s)
"""


class ReproductionLeaseLost(RuntimeError):
    """The database fence no longer belongs to this process."""


@dataclass(frozen=True)
class RuntimeSettings:
    metadata_dsn: str
    market_dsn: str
    lease_seconds: int
    heartbeat_seconds: int
    poll_seconds: int
    connect_timeout_seconds: int
    metadata_statement_timeout_ms: int
    metadata_lock_timeout_ms: int
    market_statement_timeout_ms: int
    market_lock_timeout_ms: int


def worker_name() -> str:
    explicit = str(os.environ.get("QA_REPRODUCTION_WORKER_ID", "") or "").strip()
    return explicit or f"{WORKER_VERSION}/{socket.gethostname()}/{os.getpid()}"


def _positive_int(name: str, default: int, *, minimum: int,
                  maximum: int) -> int:
    raw = str(os.environ.get(name, default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(
            f"{name} must be between {minimum} and {maximum}")
    return value


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError(f"{field} must be a JSON object")
    return value


def _runtime_settings() -> RuntimeSettings:
    metadata_dsn = str(os.environ.get("DATABASE_URL", "") or "").strip()
    market_dsn = str(os.environ.get(
        "QA_REPRODUCTION_TIMESCALE_DATABASE_URL", "") or "").strip()
    if not metadata_dsn:
        raise RuntimeError("DATABASE_URL is required")
    if not market_dsn:
        raise RuntimeError(
            "QA_REPRODUCTION_TIMESCALE_DATABASE_URL is required")

    lease_seconds = _positive_int(
        "QA_REPRODUCTION_LEASE_SECONDS", DEFAULT_LEASE_SECONDS,
        minimum=MIN_LEASE_SECONDS, maximum=MAX_LEASE_SECONDS)
    heartbeat_seconds = _positive_int(
        "QA_REPRODUCTION_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS,
        minimum=10, maximum=max(10, lease_seconds // 3))
    poll_seconds = _positive_int(
        "QA_REPRODUCTION_POLL_SECONDS", DEFAULT_POLL_SECONDS,
        minimum=1, maximum=300)
    connect_timeout_seconds = _positive_int(
        "QA_REPRODUCTION_CONNECT_TIMEOUT_SECONDS",
        DEFAULT_CONNECT_TIMEOUT_SECONDS, minimum=1, maximum=30)

    # A blocking market query must be cancelled while enough lease remains to
    # record the infrastructure retry.  This is intentionally an effective
    # cap: lowering the lease can never leave an old, unsafe query timeout.
    query_budget_ms = max(
        1_000, (lease_seconds - heartbeat_seconds) * 1_000)
    metadata_statement_timeout_ms = min(
        _positive_int(
            "QA_REPRODUCTION_METADATA_STATEMENT_TIMEOUT_MS",
            DEFAULT_METADATA_STATEMENT_TIMEOUT_MS,
            minimum=1_000, maximum=MAX_LEASE_SECONDS * 1_000),
        query_budget_ms,
    )
    metadata_lock_timeout_ms = min(
        _positive_int(
            "QA_REPRODUCTION_METADATA_LOCK_TIMEOUT_MS",
            DEFAULT_METADATA_LOCK_TIMEOUT_MS,
            minimum=100, maximum=MAX_LEASE_SECONDS * 1_000),
        metadata_statement_timeout_ms,
    )
    market_statement_timeout_ms = min(
        _positive_int(
            "QA_REPRODUCTION_MARKET_STATEMENT_TIMEOUT_MS",
            DEFAULT_MARKET_STATEMENT_TIMEOUT_MS,
            minimum=1_000, maximum=MAX_LEASE_SECONDS * 1_000),
        query_budget_ms,
    )
    market_lock_timeout_ms = min(
        _positive_int(
            "QA_REPRODUCTION_MARKET_LOCK_TIMEOUT_MS",
            DEFAULT_MARKET_LOCK_TIMEOUT_MS,
            minimum=100, maximum=MAX_LEASE_SECONDS * 1_000),
        market_statement_timeout_ms,
    )
    return RuntimeSettings(
        metadata_dsn=metadata_dsn,
        market_dsn=market_dsn,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        poll_seconds=poll_seconds,
        connect_timeout_seconds=connect_timeout_seconds,
        metadata_statement_timeout_ms=metadata_statement_timeout_ms,
        metadata_lock_timeout_ms=metadata_lock_timeout_ms,
        market_statement_timeout_ms=market_statement_timeout_ms,
        market_lock_timeout_ms=market_lock_timeout_ms,
    )


def _postgres_options(*, statement_timeout_ms: int,
                      lock_timeout_ms: int) -> str:
    statement_timeout_ms = int(statement_timeout_ms)
    lock_timeout_ms = int(lock_timeout_ms)
    if statement_timeout_ms < 1 or lock_timeout_ms < 1:
        raise RuntimeError("database timeouts must be positive")
    if lock_timeout_ms > statement_timeout_ms:
        raise RuntimeError("lock_timeout must not exceed statement_timeout")
    return (
        f"-c statement_timeout={statement_timeout_ms} "
        f"-c lock_timeout={lock_timeout_ms}"
    )


def _database_error_message(error: BaseException) -> str:
    diag = getattr(error, "diag", None)
    primary = str(getattr(diag, "message_primary", "") or "").strip()
    return primary or str(error)


def _raise_if_lease_fence_error(error: BaseException, *, operation: str
                                ) -> None:
    message = _database_error_message(error).lower()
    lease_fence_phrases = (
        "lease is stale",
        "lease was lost",
        "lost its database lease fence",
        "stale or already completed",
        "stale or owned by another worker",
    )
    if any(phrase in message for phrase in lease_fence_phrases):
        raise ReproductionLeaseLost(
            f"QA reproduction {operation} lost its database lease fence"
        ) from error


def connect_metadata_database(
        dsn: str, *,
        connect_timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        statement_timeout_ms: int = DEFAULT_METADATA_STATEMENT_TIMEOUT_MS,
        lock_timeout_ms: int = DEFAULT_METADATA_LOCK_TIMEOUT_MS):
    """Open a session-stable, scoped write connection for fenced DB APIs."""

    import psycopg2

    selected = runtime_session_dsn(dsn)
    connection = psycopg2.connect(
        selected,
        connect_timeout=int(connect_timeout_seconds),
        options=_postgres_options(
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms),
    )
    try:
        configure_writer_connection(connection)
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        connection.close()
        raise
    return connection


def connect_market_database(
        dsn: str, *,
        connect_timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        statement_timeout_ms: int = DEFAULT_MARKET_STATEMENT_TIMEOUT_MS,
        lock_timeout_ms: int = DEFAULT_MARKET_LOCK_TIMEOUT_MS):
    """Open and prove one transaction-local read-only raw-market snapshot.

    The market DSN is separately configured, but it may still be a Supavisor
    transaction-pool endpoint.  Never use a session/default read-only GUC here:
    it could be inherited by an unrelated writer after this transaction ends.
    The proved transaction remains open for the complete reproduction and is
    discarded when ``process_once`` closes the connection.
    """

    import psycopg2

    connection = psycopg2.connect(
        dsn,
        connect_timeout=int(connect_timeout_seconds),
        options=_postgres_options(
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms),
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("set transaction read only")
            cursor.execute("show transaction_read_only")
            mode = str(cursor.fetchone()[0]).lower()
        if mode != "on":
            raise RuntimeError(
                "QA reproduction market connection is not read-only")
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        connection.close()
        raise
    return connection


def _claimed_bundle(row: Any) -> dict[str, Any] | None:
    if row is None or row[0] is None:
        return None
    bundle = _json_object(row[0], field="reproduction input")
    work = bundle.get("work_item") or {}
    if (not work.get("work_item_id") or not work.get("lease_token")
            or not work.get("reproduction_request_id")):
        raise RuntimeError("claimed reproduction input lacks its lease fence")
    return bundle


def claim_next(connection, *, worker: str,
               lease_seconds: int) -> dict[str, Any] | None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(_CLAIM_SQL, (worker, int(lease_seconds)))
            row = cursor.fetchone()
        bundle = _claimed_bundle(row)
        connection.commit()
        return bundle
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        raise


def heartbeat(connection, *, bundle: dict[str, Any], worker: str,
              lease_seconds: int) -> None:
    work = bundle["work_item"]
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                _HEARTBEAT_SQL,
                (work["work_item_id"], work["lease_token"], worker,
                 int(lease_seconds)),
            )
            row = cursor.fetchone()
        connection.commit()
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        _raise_if_lease_fence_error(exc, operation="heartbeat")
        raise
    if row is None or row[0] is not True:
        raise ReproductionLeaseLost(
            "QA reproduction heartbeat lost its database lease fence")


def complete(connection, *, bundle: dict[str, Any], worker: str,
             result: dict[str, Any]) -> str:
    work = bundle["work_item"]
    verdict = str(result.get("verdict") or "")
    if verdict not in {"PASS", "FAIL", "INCONCLUSIVE"}:
        raise RuntimeError("QA reproduction returned an unknown verdict")
    if (result.get("promotion_authority") is not False
            or result.get("version") != QA_REPRODUCTION_VERSION):
        raise RuntimeError("QA reproduction result violates its audit boundary")
    evidence = dict(result)
    supplied_fingerprint = str(
        evidence.pop("result_fingerprint", "") or "")
    expected_fingerprint = stable_fingerprint(evidence)
    if (len(supplied_fingerprint) != 64
            or any(character not in "0123456789abcdef"
                   for character in supplied_fingerprint)
            or not hmac.compare_digest(
                supplied_fingerprint, expected_fingerprint)):
        raise RuntimeError(
            "QA reproduction result fingerprint does not match its evidence")
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                _COMPLETE_SQL,
                (work["work_item_id"], work["lease_token"], worker, verdict,
                 json.dumps(result, sort_keys=True, separators=(",", ":"),
                            default=str),
                 WORKER_VERSION),
            )
            row = cursor.fetchone()
        connection.commit()
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        _raise_if_lease_fence_error(exc, operation="completion")
        raise
    if row is None or row[0] is None:
        raise ReproductionLeaseLost(
            "QA reproduction completion lost its database lease fence")
    return str(row[0])


def fail(connection, *, bundle: dict[str, Any], worker: str,
         error: BaseException) -> str | None:
    work = bundle["work_item"]
    message = f"{type(error).__name__}: {str(error)[:1_500]}"
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                _FAIL_SQL,
                (work["work_item_id"], work["lease_token"], worker, message),
            )
            row = cursor.fetchone()
        connection.commit()
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        _raise_if_lease_fence_error(exc, operation="failure recording")
        raise
    if row is None or row[0] is None:
        raise ReproductionLeaseLost(
            "QA reproduction failure recording lost its database lease fence")
    return str(row[0])


def probe_readiness(settings: RuntimeSettings) -> None:
    """Exercise both DB boundaries without consuming a queue item.

    The claim function executes in a transaction that is always rolled back,
    proving function resolution, role grants, joins, and the lease mutation.
    The raw store probe executes under the same enforced read-only session and
    bounded statement/lock timeouts used by reproduction work.
    """

    metadata = None
    market = None
    try:
        metadata = connect_metadata_database(
            settings.metadata_dsn,
            connect_timeout_seconds=settings.connect_timeout_seconds,
            statement_timeout_ms=settings.metadata_statement_timeout_ms,
            lock_timeout_ms=settings.metadata_lock_timeout_ms,
        )
        try:
            with metadata.cursor() as cursor:
                cursor.execute(
                    _CLAIM_SQL,
                    (f"{WORKER_VERSION}/healthcheck/{socket.gethostname()}",
                     int(settings.lease_seconds)),
                )
                row = cursor.fetchone()
            _claimed_bundle(row)
        finally:
            metadata.rollback()

        market = connect_market_database(
            settings.market_dsn,
            connect_timeout_seconds=settings.connect_timeout_seconds,
            statement_timeout_ms=settings.market_statement_timeout_ms,
            lock_timeout_ms=settings.market_lock_timeout_ms,
        )
        with market.cursor() as cursor:
            cursor.execute("select 1")
            row = cursor.fetchone()
        if row is None or row[0] != 1:
            raise RuntimeError("QA reproduction market readiness probe failed")
        market.rollback()
    finally:
        if market is not None:
            try:
                market.close()
            except Exception:
                pass
        if metadata is not None:
            try:
                metadata.close()
            except Exception:
                pass


def process_once(
        metadata_connection, *, market_connect: Callable[[], Any],
        reproduce: Callable[..., dict] = reproduce_forward_confirmation,
        runtime_preflight: Callable[[dict], dict] | None = None,
        worker: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
        monotonic_fn: Callable[[], float] = time.monotonic) -> dict | None:
    bundle = claim_next(
        metadata_connection, worker=worker, lease_seconds=lease_seconds)
    if bundle is None:
        return None

    market_connection = None
    last_heartbeat = float(monotonic_fn())

    def lease_guard(force: bool = False) -> None:
        nonlocal last_heartbeat
        now = float(monotonic_fn())
        if force or now - last_heartbeat >= heartbeat_seconds:
            heartbeat(
                metadata_connection, bundle=bundle, worker=worker,
                lease_seconds=lease_seconds)
            last_heartbeat = now

    try:
        lease_guard(True)
        preflight = runtime_preflight
        if preflight is None and reproduce is reproduce_forward_confirmation:
            preflight = preflight_qa_reproduction_runtime
        runtime_artifact = preflight(bundle) if preflight is not None else None
        if (runtime_artifact is None
                or runtime_artifact.get("reproduction_route_available") is True):
            market_connection = market_connect()
        result = reproduce(
            market_connection, bundle, lease_guard=lease_guard)
        lease_guard(True)
        result_id = complete(
            metadata_connection, bundle=bundle, worker=worker, result=result)
        return {
            "status": "COMPLETED",
            "verdict": result["verdict"],
            "result_id": result_id,
            "work_item_id": bundle["work_item"]["work_item_id"],
        }
    except ReproductionLeaseLost:
        try:
            metadata_connection.rollback()
        except Exception:
            pass
        raise
    except Exception as exc:
        try:
            metadata_connection.rollback()
        except Exception:
            pass
        try:
            status = fail(
                metadata_connection, bundle=bundle, worker=worker, error=exc)
        except ReproductionLeaseLost:
            try:
                metadata_connection.rollback()
            except Exception:
                pass
            raise
        except Exception as fail_exc:
            try:
                metadata_connection.rollback()
            except Exception:
                pass
            return {
                "status": "ERROR",
                "work_item_id": bundle["work_item"]["work_item_id"],
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "failure_record_error":
                    f"{type(fail_exc).__name__}: {str(fail_exc)[:300]}",
            }
        return {
            "status": status or "LEASE_LOST",
            "work_item_id": bundle["work_item"]["work_item_id"],
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    finally:
        if market_connection is not None:
            try:
                market_connection.close()
            except Exception:
                pass


def serve(settings: RuntimeSettings | None = None) -> None:
    settings = settings or _runtime_settings()
    identity = worker_name()
    metadata = connect_metadata_database(
        settings.metadata_dsn,
        connect_timeout_seconds=settings.connect_timeout_seconds,
        statement_timeout_ms=settings.metadata_statement_timeout_ms,
        lock_timeout_ms=settings.metadata_lock_timeout_ms,
    )
    print(f"{WORKER_VERSION} started as {identity}", flush=True)
    try:
        while True:
            try:
                result = process_once(
                    metadata,
                    market_connect=lambda: connect_market_database(
                        settings.market_dsn,
                        connect_timeout_seconds=
                            settings.connect_timeout_seconds,
                        statement_timeout_ms=
                            settings.market_statement_timeout_ms,
                        lock_timeout_ms=settings.market_lock_timeout_ms),
                    worker=identity, lease_seconds=settings.lease_seconds,
                    heartbeat_seconds=settings.heartbeat_seconds)
                if result is not None:
                    print(json.dumps(result, sort_keys=True), flush=True)
            except ReproductionLeaseLost as exc:
                print(f"lease lost: {exc}", file=sys.stderr, flush=True)
            except Exception as exc:
                print(
                    f"worker error: {type(exc).__name__}: {str(exc)[:500]}",
                    file=sys.stderr, flush=True)
                try:
                    metadata.close()
                except Exception:
                    pass
                time.sleep(min(30, settings.poll_seconds))
                metadata = connect_metadata_database(
                    settings.metadata_dsn,
                    connect_timeout_seconds=settings.connect_timeout_seconds,
                    statement_timeout_ms=
                        settings.metadata_statement_timeout_ms,
                    lock_timeout_ms=settings.metadata_lock_timeout_ms)
            time.sleep(settings.poll_seconds)
    finally:
        try:
            metadata.close()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    settings = _runtime_settings()
    if arguments == ["--healthcheck"]:
        probe_readiness(settings)
        print("qa reproduction dependencies ready", flush=True)
        return
    if arguments:
        raise RuntimeError(
            f"unknown QA reproduction worker arguments: {arguments}")
    serve(settings)


if __name__ == "__main__":
    main()
