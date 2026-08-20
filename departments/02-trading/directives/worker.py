"""Autonomous reconciler for durable authenticated-user PAPER directives."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone

from broker.ls_paper_broker import LSPaperBroker
from .market_data import HttpMarketDataProvider
from .repository import PostgresDirectiveRepository
from .service import DirectiveServiceError, UserDirectiveService, require_paper_execution_mode


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
    return UserDirectiveService(
        PostgresDirectiveRepository(dsn),
        HttpMarketDataProvider.from_env(),
        external_broker=LSPaperBroker.from_env() if adapter == "ls-paper" else None,
    )


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
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
