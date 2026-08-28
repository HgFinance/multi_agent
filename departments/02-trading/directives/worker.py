"""Autonomous reconciler for durable authenticated-user PAPER directives."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone

from broker.ls_paper_broker import LSPaperBroker, LSPaperBrokerError

from orchestration.service_health import probe_http, probe_postgres

from .market_data import HttpMarketDataProvider, with_quote_fallback
from .repository import PostgresDirectiveRepository
from .service import (
    DirectiveServiceError,
    UserDirectiveService,
    require_paper_execution_mode,
)


def _settings() -> tuple[float, int]:
    try:
        poll = float(os.environ.get("TRADING_DIRECTIVE_WORKER_POLL_SECONDS", "1.0"))
        batch = int(os.environ.get("TRADING_DIRECTIVE_WORKER_BATCH_SIZE", "100"))
    except ValueError as exc:
        raise DirectiveServiceError(
            "TRADING_DIRECTIVE_WORKER_CONFIG_INVALID",
            "worker poll/batch settings are invalid",
            503,
        ) from exc
    if not 0.1 <= poll <= 60 or not 1 <= batch <= 1000:
        raise DirectiveServiceError(
            "TRADING_DIRECTIVE_WORKER_CONFIG_INVALID",
            "worker poll/batch settings are outside bounds",
            503,
        )
    return poll, batch


def build_service() -> UserDirectiveService:
    require_paper_execution_mode()
    adapter = os.environ.get("TRADING_BROKER_ADAPTER", "").strip().lower()
    if adapter not in {"paper", "ls-paper"}:
        raise DirectiveServiceError(
            "TRADING_LIVE_ADAPTER_FORBIDDEN",
            "directive worker only supports paper or ls-paper adapter",
            503,
        )
    if os.environ.get("TRADING_DIRECTIVE_REPOSITORY", "").strip().lower() != "postgres":
        raise DirectiveServiceError(
            "TRADING_DIRECTIVE_REPOSITORY_INVALID",
            "directive worker requires the durable postgres repository",
            503,
        )
    dsn = os.environ.get("PAPER_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise DirectiveServiceError(
            "TRADING_DIRECTIVE_DB_UNAVAILABLE", "DATABASE_URL is required", 503
        )
    external_broker = LSPaperBroker.from_env() if adapter == "ls-paper" else None
    # The admission API wraps its provider with the read-only LS quote
    # fallback; this worker used to construct the projection-only provider
    # bare, so a triggered conditional rule failed with
    # TRADING_MARKET_QUOTE_STALE while a fresh t1101 quote was available
    # (2026-08-28, 001210).  Both paths now share one decision.
    return UserDirectiveService(
        PostgresDirectiveRepository(dsn),
        with_quote_fallback(
            HttpMarketDataProvider.from_env(),
            external_broker=external_broker,
            broker_factory=_quote_broker_factory,
        ),
        external_broker=external_broker,
    )


def _quote_broker_factory() -> LSPaperBroker:
    try:
        return LSPaperBroker.from_env()
    except LSPaperBrokerError as exc:
        raise DirectiveServiceError(
            "TRADING_MARKET_QUOTE_FALLBACK_UNAVAILABLE",
            f"LS PAPER quote fallback is enabled but unavailable: {exc}",
            503,
        ) from exc


def run_once(
    service: UserDirectiveService,
    *,
    batch: int,
    now: datetime | None = None,
) -> dict[str, object]:
    """Reconcile one durable batch, including post-accounting finalization.

    ``now`` is injectable so crash/retry and acknowledgement-boundary tests can
    exercise the exact same worker entry point without depending on wall time.
    """

    current_time = now or datetime.now(timezone.utc)
    records, errors = service.reconcile_active(
        now=current_time, limit=batch
    )
    return {
        "reconciled": len(records),
        "errors": errors,
        "at": current_time.isoformat(),
    }


def healthcheck() -> None:
    """Probe the directive database and market read surface without reconciling."""

    _settings()
    require_paper_execution_mode()
    if os.environ.get("TRADING_DIRECTIVE_REPOSITORY", "").strip().lower() != "postgres":
        raise DirectiveServiceError(
            "TRADING_DIRECTIVE_REPOSITORY_INVALID",
            "directive worker requires the durable postgres repository",
            503,
        )
    dsn_env = "PAPER_DATABASE_URL" if os.environ.get("PAPER_DATABASE_URL") else "DATABASE_URL"
    probe_postgres(dsn_env=dsn_env, role_env="TRADING_DATABASE_ROLE")
    provider = HttpMarketDataProvider.from_env()
    probe_http(provider.base_url.rstrip("/") + "/health")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        healthcheck()
        return 0
    poll, batch = _settings()
    service = build_service()
    while True:
        result = run_once(service, batch=batch)
        if result["reconciled"] or result["errors"]:
            print(json.dumps(result, sort_keys=True), flush=True)
        if args.once:
            return 1 if result["errors"] else 0
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
