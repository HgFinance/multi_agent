"""Short-lived, payload-bound proof for BFF to trading-api directives."""
from __future__ import annotations

import hashlib
import json
import os
import time
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping
from uuid import UUID, uuid4

import jwt


class TradingProofConfigurationError(RuntimeError):
    """The internal trading service identity is missing or unsafe."""


TRADING_DIRECTIVE_EXECUTE_SCOPE = "trading.user-directive.execute"
TRADING_DIRECTIVE_READ_SCOPE = "trading.user-directive.read"
DEFAULT_TRADING_PROOF_ISSUER = "portfolio-bff"
DEFAULT_TRADING_PROOF_AUDIENCE = "trading-api"
TRADING_PROOF_TTL_SECONDS = 20
_UNSAFE_SECRET_MARKERS = (
    "change_me",
    "changeme",
    "replace-with",
    "replace_me",
    "placeholder",
    "example-secret",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return _json_value(value.value)
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="python"))
    return value


def canonical_json(value: Any) -> str:
    """Serialize the directive payload identically on both internal services."""

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _required_setting(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise TradingProofConfigurationError(f"{name} is required")
    return value


def trading_proof_settings() -> tuple[bytes, str, str]:
    secret_text = _required_setting("TRADING_SERVICE_AUTH_SECRET")
    secret = secret_text.encode("utf-8")
    normalized_secret = secret_text.casefold()
    if len(secret) < 32 or any(
        marker in normalized_secret for marker in _UNSAFE_SECRET_MARKERS
    ):
        raise TradingProofConfigurationError(
            "TRADING_SERVICE_AUTH_SECRET must be a non-placeholder secret "
            "of at least 32 bytes"
        )
    issuer = _required_setting(
        "TRADING_SERVICE_AUTH_ISSUER", DEFAULT_TRADING_PROOF_ISSUER
    )
    audience = _required_setting(
        "TRADING_SERVICE_AUTH_AUDIENCE", DEFAULT_TRADING_PROOF_AUDIENCE
    )
    if any(character.isspace() for character in issuer + audience):
        raise TradingProofConfigurationError("trading service identity is invalid")
    return secret, issuer, audience


def issue_trading_directive_proof(
    *,
    subject: str,
    fund_id: str,
    book_id: str,
    action: str,
    instruction_ref: str,
    idempotency_key: str,
    payload_hash: str,
    scope: str = TRADING_DIRECTIVE_EXECUTE_SCOPE,
    now: int | None = None,
) -> str:
    """Issue a one-use-shaped proof; no Supabase bearer token is forwarded."""

    secret, issuer, audience = trading_proof_settings()
    issued_at = int(time.time()) if now is None else int(now)
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "fund_id": fund_id,
        "book_id": book_id,
        "action": action,
        "instruction_ref": instruction_ref,
        "idempotency_key": idempotency_key,
        "payload_sha256": payload_hash,
        "jti": str(uuid4()),
        "iat": issued_at,
        "nbf": issued_at - 1,
        "exp": issued_at + TRADING_PROOF_TTL_SECONDS,
        "scope": scope,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


__all__ = [
    "DEFAULT_TRADING_PROOF_AUDIENCE",
    "DEFAULT_TRADING_PROOF_ISSUER",
    "TRADING_DIRECTIVE_EXECUTE_SCOPE",
    "TRADING_DIRECTIVE_READ_SCOPE",
    "TRADING_PROOF_TTL_SECONDS",
    "TradingProofConfigurationError",
    "canonical_json",
    "issue_trading_directive_proof",
    "payload_sha256",
    "trading_proof_settings",
]
