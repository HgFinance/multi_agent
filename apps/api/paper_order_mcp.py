"""Authenticated MCP boundary for Trading Hermes PAPER-order interpretations.

This module intentionally exposes one tool and owns no order business logic.
The tool delegates lazily to :mod:`apps.api.user_order_orchestrator` so merely
importing or inspecting the MCP surface cannot initialize database, Kanban, or
Trading API clients.
"""

from __future__ import annotations

import hmac
import inspect
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any

from orchestration.contracts.user_paper_order import HermesOrderCandidate


MCP_PORT = 8046
MCP_PATH = "/mcp"
MIN_API_KEY_BYTES = 32
_PLACEHOLDER_MARKERS = (
    "${",
    "change_me",
    "changeme",
    "example",
    "placeholder",
    "replace_me",
    "secret_here",
    "your_api_key",
)


def validate_api_key(value: str | None) -> str:
    """Return a usable boundary key or fail closed.

    The key is required in every runtime.  In particular, production can never
    turn an empty environment variable into an unauthenticated MCP surface.
    """

    raw = str(value or "")
    token = raw.strip()
    lowered = token.casefold()
    if not token:
        raise RuntimeError("MCP_TRADING_ORDER_API_KEY is required")
    if token != raw:
        raise RuntimeError("MCP_TRADING_ORDER_API_KEY must not contain whitespace")
    if len(token.encode("utf-8")) < MIN_API_KEY_BYTES:
        raise RuntimeError(
            "MCP_TRADING_ORDER_API_KEY must contain at least 32 bytes"
        )
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        raise RuntimeError("MCP_TRADING_ORDER_API_KEY must not be a placeholder")
    if len(set(token)) == 1:
        raise RuntimeError("MCP_TRADING_ORDER_API_KEY must be a generated secret")
    if any(ord(character) < 33 or ord(character) > 126 for character in token):
        raise RuntimeError(
            "MCP_TRADING_ORDER_API_KEY must contain printable ASCII only"
        )
    return token


def is_authorized(header: str | None, expected_token: str) -> bool:
    """Validate one exact Bearer credential in constant time."""

    if not header:
        return False
    scheme, separator, supplied = header.partition(" ")
    if separator != " " or scheme.casefold() != "bearer" or not supplied:
        return False
    if supplied != supplied.strip() or " " in supplied or "\t" in supplied:
        return False
    return hmac.compare_digest(supplied.encode(), expected_token.encode())


class BearerAuthMiddleware:
    """Small ASGI middleware that protects the complete HTTP surface."""

    def __init__(self, app: Any, *, api_key: str) -> None:
        self.app = app
        self.api_key = validate_api_key(api_key)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            authorization_headers = [
                raw_value.decode("latin-1")
                for raw_name, raw_value in scope.get("headers", ())
                if raw_name.lower() == b"authorization"
            ]
            header = (
                authorization_headers[0]
                if len(authorization_headers) == 1
                else None
            )
            if not is_authorized(header, self.api_key):
                body = b'{"error":"unauthorized","detail":"Bearer credential required"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                            (b"www-authenticate", b"Bearer"),
                            (b"cache-control", b"no-store"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


async def _delegate_to_orchestrator(
    *,
    root_task_id: str,
    trading_task_id: str,
    interpretation: HermesOrderCandidate,
) -> dict[str, Any]:
    # Import at invocation time.  Hermes receives no database or Trading proof
    # secret; only this trusted server process imports the authority boundary.
    from apps.api.user_order_orchestrator import (  # noqa: PLC0415
        process_user_paper_order as orchestrate,
    )

    result = orchestrate(
        root_task_id=root_task_id,
        trading_task_id=trading_task_id,
        interpretation=interpretation.model_dump(mode="json"),
    )
    if inspect.isawaitable(result):
        result = await result
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        result = model_dump(mode="json")
    if not isinstance(result, dict):
        raise RuntimeError("paper-order orchestrator returned a non-object result")
    return result


def build_server(*, host: str = "0.0.0.0", port: int = MCP_PORT):
    """Build the single-tool FastMCP server supported by the pinned image."""

    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        name="hgfinance-user-paper-order",
        instructions=(
            "Trading Hermes interpretation boundary. PAPER only. This server "
            "exposes exactly one order workflow tool."
        ),
        host=host,
        port=port,
        streamable_http_path=MCP_PATH,
    )

    @server.tool(
        name="process_user_paper_order",
        description=(
            "Submit exactly one strict, non-binding Trading Hermes interpretation "
            "for the marked user PAPER-order workflow. Authority fields are "
            "derived by the trusted orchestrator; interpretation.mode is fixed "
            "to PAPER, and callers must never add user, fund, book, mode-override, "
            "token, or service-proof arguments."
        ),
        structured_output=True,
    )
    async def process_user_paper_order(
        root_task_id: str,
        trading_task_id: str,
        interpretation: HermesOrderCandidate,
    ) -> dict[str, Any]:
        return await _delegate_to_orchestrator(
            root_task_id=root_task_id,
            trading_task_id=trading_task_id,
            interpretation=interpretation,
        )

    return server


def build_app(server: Any, *, api_key: str) -> BearerAuthMiddleware:
    """Wrap the Streamable HTTP ASGI app in mandatory Bearer authentication."""

    return BearerAuthMiddleware(server.streamable_http_app(), api_key=api_key)


def check_readiness() -> None:
    """Fail unless every dependency needed for a safe first submission exists.

    A listening TCP socket alone is insufficient: a cold-start race against
    Trading or a missing migration would turn the first user order into an
    intentionally non-retriable UNKNOWN outcome.  This probe emits no secret
    or connection string; success is represented only by exit status zero.
    """

    validate_api_key(os.environ.get("MCP_TRADING_ORDER_API_KEY"))
    dsn = (
        os.environ.get("CONTROL_DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )
    if not dsn:
        raise RuntimeError("operational database URL is required")

    import psycopg2
    from psycopg2 import sql

    role = os.environ.get(
        "ORDER_ORCHESTRATOR_DATABASE_ROLE", "svc_order_orchestrator"
    ).strip()
    if not role:
        raise RuntimeError("order orchestrator database role is required")
    with psycopg2.connect(dsn, connect_timeout=3) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
            cursor.execute(
                "select to_regclass('execution.user_order_requests'), "
                "to_regclass('execution.user_order_interpretations')"
            )
            tables = cursor.fetchone()
            if not tables or any(table is None for table in tables):
                raise RuntimeError("PAPER order workflow migration is not ready")

    kanban_db = Path(
        os.environ.get("HERMES_KANBAN_DB", "/opt/kanban/kanban.db")
    )
    if not kanban_db.is_file():
        raise RuntimeError("shared Kanban database is not ready")
    kanban = sqlite3.connect(
        f"file:{kanban_db.as_posix()}?mode=rw", uri=True, timeout=2
    )
    try:
        kanban.execute("select 1").fetchone()
    finally:
        kanban.close()

    trading_url = os.environ.get(
        "TRADING_API_URL", "http://trading-api:8000"
    ).rstrip("/")
    if not trading_url:
        raise RuntimeError("TRADING_API_URL is required")
    with urllib.request.urlopen(f"{trading_url}/health/ready", timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("Trading PAPER API is not ready")


def main() -> None:
    """Run the internal-only Streamable HTTP MCP server on port 8046."""

    if sys.argv[1:] == ["--healthcheck"]:
        check_readiness()
        return
    if sys.argv[1:]:
        raise SystemExit("usage: python -m apps.api.paper_order_mcp [--healthcheck]")

    import uvicorn

    api_key = validate_api_key(os.environ.get("MCP_TRADING_ORDER_API_KEY"))
    server = build_server()
    uvicorn.run(
        build_app(server, api_key=api_key),
        host="0.0.0.0",
        port=MCP_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()


__all__ = [
    "BearerAuthMiddleware",
    "MCP_PATH",
    "MCP_PORT",
    "build_app",
    "build_server",
    "check_readiness",
    "is_authorized",
    "validate_api_key",
]
