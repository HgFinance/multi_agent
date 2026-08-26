"""Shared static Bearer authentication for internal MCP HTTP surfaces."""

from __future__ import annotations

import hmac
from typing import Any

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


def validate_api_key(
    value: str | None,
    *,
    credential_name: str = "MCP API key",
) -> str:
    """Return a usable MCP key without ever including its value in errors."""

    raw = str(value or "")
    token = raw.strip()
    lowered = token.casefold()
    if not token:
        raise RuntimeError(f"{credential_name} is required")
    if token != raw:
        raise RuntimeError(f"{credential_name} must not contain whitespace")
    if len(token.encode("utf-8")) < MIN_API_KEY_BYTES:
        raise RuntimeError(f"{credential_name} must contain at least 32 bytes")
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        raise RuntimeError(f"{credential_name} must not be a placeholder")
    if len(set(token)) == 1:
        raise RuntimeError(f"{credential_name} must be a generated secret")
    if any(ord(character) < 33 or ord(character) > 126 for character in token):
        raise RuntimeError(f"{credential_name} must contain printable ASCII only")
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
    """Protect an ASGI MCP surface with one mandatory static Bearer key."""

    def __init__(
        self,
        app: Any,
        *,
        api_key: str,
        credential_name: str = "MCP API key",
    ) -> None:
        self.app = app
        self.api_key = validate_api_key(
            api_key,
            credential_name=credential_name,
        )

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


__all__ = [
    "BearerAuthMiddleware",
    "MIN_API_KEY_BYTES",
    "is_authorized",
    "validate_api_key",
]
