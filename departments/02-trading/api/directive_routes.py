"""HTTP routes and runtime construction for USER-priority PAPER directives."""
from __future__ import annotations

import os
from uuid import UUID

from fastapi import APIRouter, Header

from broker.ls_paper_broker import LSPaperBroker, LSPaperBrokerError
from directives.contracts import UserDirectiveRequest
from directives.market_data import (
    FixtureMarketDataProvider,
    HttpMarketDataProvider,
    LsPaperFallbackMarketDataProvider,
)
from directives.repository import InMemoryDirectiveRepository, PostgresDirectiveRepository
from directives.service import DirectiveServiceError, UserDirectiveService, require_paper_execution_mode


router = APIRouter(prefix="/trading/v1/user-directives", tags=["user-paper-directives"])
_service: UserDirectiveService | None = None


def _production() -> bool:
    return os.environ.get("APP_ENV", "").strip().lower() in {"prod", "production"}


def _quote_fallback_enabled() -> bool:
    """Allow the read-only LS quote fallback without changing order routing.

    The TSDB quote projection only covers the bounded realtime subscription
    basket, so a conditional rule on any other symbol can never satisfy the
    freshness window at admission time.  Binding that fallback to
    TRADING_BROKER_ADAPTER used to be the only way to enable it, but that flag
    also moves order placement onto the LS PAPER broker - a far larger change
    than reading one quote.  Keep the two decisions separate.
    """

    return os.environ.get(
        "TRADING_MARKET_QUOTE_LS_FALLBACK", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def configure_directive_runtime() -> UserDirectiveService:
    """Build the fail-closed production runtime; no implicit memory fallback."""
    global _service
    require_paper_execution_mode()
    adapter = os.environ.get("TRADING_BROKER_ADAPTER", "paper").strip().lower()
    if adapter not in {"paper", "ls-paper"}:
        raise DirectiveServiceError(
            "TRADING_LIVE_ADAPTER_FORBIDDEN",
            "authenticated user directives require paper or ls-paper adapter",
            503,
        )
    external_broker = LSPaperBroker.from_env() if adapter == "ls-paper" else None
    repository_mode = os.environ.get("TRADING_DIRECTIVE_REPOSITORY", "postgres").strip().lower()
    auth_mode = os.environ.get("TRADING_AUTH_MODE", "service").strip().lower()
    if auth_mode == "fixture" and _production():
        raise DirectiveServiceError(
            "TRADING_FIXTURE_AUTH_FORBIDDEN",
            "fixture auth is forbidden in production",
            503,
        )
    if repository_mode == "memory":
        if auth_mode != "fixture" or _production():
            raise DirectiveServiceError(
                "TRADING_MEMORY_REPOSITORY_FORBIDDEN",
                "in-memory directives require explicit non-production fixture auth",
                503,
            )
        repository = InMemoryDirectiveRepository()
        market_data = FixtureMarketDataProvider()
    elif repository_mode == "postgres":
        dsn = os.environ.get("PAPER_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not dsn:
            raise DirectiveServiceError(
                "TRADING_DIRECTIVE_DB_UNAVAILABLE",
                "PAPER_DATABASE_URL or DATABASE_URL is required",
                503,
            )
        repository = PostgresDirectiveRepository(dsn)
        market_data = HttpMarketDataProvider.from_env()
    else:
        raise DirectiveServiceError(
            "TRADING_DIRECTIVE_REPOSITORY_INVALID",
            "TRADING_DIRECTIVE_REPOSITORY must be postgres or memory",
            503,
        )
    # Order routing stays on `external_broker`; the quote fallback is a
    # read-only t1101 read and is chosen independently.  Explicit configuration
    # that cannot be honored fails startup rather than silently degrading back
    # to the projection-only path.
    quote_broker = external_broker
    if quote_broker is None and _quote_fallback_enabled():
        try:
            quote_broker = LSPaperBroker.from_env()
        except LSPaperBrokerError as exc:
            raise DirectiveServiceError(
                "TRADING_MARKET_QUOTE_FALLBACK_UNAVAILABLE",
                f"LS PAPER quote fallback is enabled but unavailable: {exc}",
                503,
            ) from exc
    if quote_broker is not None:
        market_data = LsPaperFallbackMarketDataProvider(market_data, quote_broker)
    _service = UserDirectiveService(
        repository,
        market_data,
        external_broker=external_broker,
    )
    return _service


def set_directive_service_for_tests(service: UserDirectiveService | None) -> None:
    global _service
    if _production():
        raise RuntimeError("test directive service injection is forbidden in production")
    _service = service


def _required_service() -> UserDirectiveService:
    if _service is None:
        raise DirectiveServiceError(
            "TRADING_DIRECTIVE_RUNTIME_UNAVAILABLE",
            "directive runtime has not completed startup",
            503,
        )
    return _service


def required_directive_service() -> UserDirectiveService:
    """Return the configured service for other authenticated internal routes."""

    return _required_service()


@router.post("", status_code=202)
def submit_user_directive(
    body: UserDirectiveRequest,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    if idempotency_key is None:
        raise DirectiveServiceError(
            "TRADING_IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key header is required",
            400,
        )
    if idempotency_key != body.idempotency_key:
        raise DirectiveServiceError(
            "TRADING_IDEMPOTENCY_KEY_MISMATCH",
            "Idempotency-Key header must match the canonical request body",
            409,
        )
    return _required_service().submit(body, authorization).view()


@router.get("/{directive_id}")
def get_user_directive(
    directive_id: UUID,
    authorization: str | None = Header(default=None),
) -> dict:
    return _required_service().get_status(directive_id, authorization).view()


__all__ = [
    "configure_directive_runtime",
    "required_directive_service",
    "router",
    "set_directive_service_for_tests",
]
