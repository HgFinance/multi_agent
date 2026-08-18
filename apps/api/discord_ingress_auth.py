"""Private authentication boundary for the CEO Discord-to-BFF bridge.

The Discord gateway is not a browser and therefore cannot present a Supabase
user JWT.  It receives one single-purpose service credential which is accepted
only on the canonical ``POST /ui/ceo/ingress`` path.  The Discord author is
resolved to a PAPER user/fund inside the BFF after this transport boundary.
"""

from __future__ import annotations

import hmac
import os
from typing import Any


DISCORD_INGRESS_SECRET_ENV = "CEO_DISCORD_INGRESS_API_KEY"
DISCORD_INGRESS_PATH = "/ui/ceo/ingress"
REQUEST_STATE_ATTRIBUTE = "hgfinance_internal_discord_ingress"
_PLACEHOLDER_MARKERS = (
    "change_me",
    "changeme",
    "example",
    "placeholder",
    "replace_me",
    "secret_here",
    "your_api_key",
)


def configured_secret() -> str | None:
    """Return a usable private bridge key without ever logging its value."""

    value = os.getenv(DISCORD_INGRESS_SECRET_ENV, "").strip()
    lowered = value.casefold()
    if (
        len(value.encode("utf-8")) < 32
        or len(set(value)) <= 1
        or any(marker in lowered for marker in _PLACEHOLDER_MARKERS)
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        return None
    return value


def bearer_is_authorized(authorization: str | None) -> bool:
    """Compare one exact Bearer credential in constant time."""

    secret = configured_secret()
    if secret is None or not isinstance(authorization, str):
        return False
    scheme, separator, credential = authorization.partition(" ")
    return bool(
        separator
        and scheme.casefold() == "bearer"
        and credential
        and hmac.compare_digest(credential, secret)
    )


def mark_request(request: Any) -> None:
    setattr(request.state, REQUEST_STATE_ATTRIBUTE, True)


def request_is_authorized(request: Any) -> bool:
    return getattr(request.state, REQUEST_STATE_ATTRIBUTE, False) is True


__all__ = [
    "DISCORD_INGRESS_PATH",
    "DISCORD_INGRESS_SECRET_ENV",
    "bearer_is_authorized",
    "configured_secret",
    "mark_request",
    "request_is_authorized",
]
