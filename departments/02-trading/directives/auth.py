"""Strict BFF proof verification for authenticated PAPER directives.

The BFF signs short-lived, request-bound HS256 proofs.  Trading verifies the
signature and every authority-bearing claim again; the browser token itself is
never accepted by this service.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .contracts import DirectiveAction, UserDirectiveRequest


EXECUTE_SCOPE = "trading.user-directive.execute"
READ_SCOPE = "trading.user-directive.read"
READ_ACTION = "GET_STATUS"
EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"{}").hexdigest()

# Long placeholder values are just as unsafe as short secrets.  Keep this
# intentionally narrow to deployment-template vocabulary so high-entropy
# production secrets are not rejected because of an incidental English word.
_PLACEHOLDER_SECRET_MARKERS = (
    "change_me",
    "changeme",
    "replace-with",
    "replace_with",
    "example",
    "placeholder",
    "your-secret",
    "your_secret",
)


class DirectiveAuthError(ValueError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class DirectiveProof:
    subject: UUID
    fund_id: UUID
    book_id: UUID
    action: DirectiveAction
    instruction_ref: str
    idempotency_key: str
    payload_sha256: str
    jti: str
    issued_at: float
    not_before: float
    expires_at: float
    scope: frozenset[str]


@dataclass(frozen=True)
class DirectiveReadProof:
    subject: UUID
    fund_id: UUID
    book_id: UUID
    directive_id: UUID
    jti: str
    issued_at: float
    not_before: float
    expires_at: float
    scope: frozenset[str]


def _fail(code: str, message: str, status: int) -> DirectiveAuthError:
    return DirectiveAuthError(code, message, status)


def _decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise _fail("TRADING_PROOF_INVALID", "invalid service proof encoding", 401) from exc


def _json(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(_decode(value))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("TRADING_PROOF_INVALID", "invalid service proof JSON", 401) from exc
    if not isinstance(decoded, dict):
        raise _fail("TRADING_PROOF_INVALID", "service proof JSON object required", 401)
    return decoded


def _required_config() -> tuple[str, str, str]:
    secret = os.environ.get("TRADING_SERVICE_AUTH_SECRET", "")
    issuer = os.environ.get("TRADING_SERVICE_AUTH_ISSUER", "").strip()
    audience = os.environ.get("TRADING_SERVICE_AUTH_AUDIENCE", "").strip()
    normalized_secret = secret.strip().casefold()
    is_placeholder = any(
        marker in normalized_secret for marker in _PLACEHOLDER_SECRET_MARKERS
    )
    if len(secret) < 32 or is_placeholder or not issuer or not audience:
        raise _fail(
            "TRADING_PROOF_AUTH_NOT_CONFIGURED",
            "trading service proof verifier is not configured",
            503,
        )
    return secret, issuer, audience


def _verified_claims(
    authorization: str | None,
    *,
    now: float | None,
    required_scope: str,
) -> tuple[dict[str, Any], dict[str, float], frozenset[str]]:
    """Verify a bounded service JWT and return otherwise-untrusted claims."""
    secret, issuer, audience = _required_config()
    parts = (authorization or "").split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise _fail("TRADING_PROOF_REQUIRED", "Bearer service proof required", 401)
    segments = parts[1].split(".")
    if len(segments) != 3:
        raise _fail("TRADING_PROOF_INVALID", "invalid service proof format", 401)
    header, claims = _json(segments[0]), _json(segments[1])
    if header.get("alg") != "HS256" or header.get("typ") not in (None, "JWT"):
        raise _fail("TRADING_PROOF_ALGORITHM_DENIED", "HS256 JWT required", 401)
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{segments[0]}.{segments[1]}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, _decode(segments[2])):
        raise _fail("TRADING_PROOF_INVALID", "invalid service proof signature", 401)

    if claims.get("iss") != issuer:
        raise _fail("TRADING_PROOF_ISSUER_DENIED", "service proof issuer mismatch", 403)
    token_aud = claims.get("aud")
    if isinstance(token_aud, str):
        audiences = {token_aud}
    elif isinstance(token_aud, list) and all(isinstance(item, str) for item in token_aud):
        audiences = set(token_aud)
    else:
        raise _fail("TRADING_PROOF_AUDIENCE_DENIED", "service proof audience is invalid", 403)
    if audience not in audiences:
        raise _fail("TRADING_PROOF_AUDIENCE_DENIED", "service proof audience mismatch", 403)

    current = time.time() if now is None else float(now)
    try:
        skew = max(float(os.environ.get("TRADING_SERVICE_AUTH_CLOCK_SKEW_SECONDS", "5")), 0.0)
        max_ttl = max(float(os.environ.get("TRADING_SERVICE_AUTH_MAX_TTL_SECONDS", "300")), 1.0)
    except ValueError as exc:
        raise _fail("TRADING_PROOF_AUTH_NOT_CONFIGURED", "invalid proof timing policy", 503) from exc
    timestamps: dict[str, float] = {}
    for name in ("iat", "nbf", "exp"):
        value = claims.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _fail("TRADING_PROOF_INVALID", f"numeric {name} claim required", 401)
        timestamps[name] = float(value)
    if timestamps["iat"] > current + skew or timestamps["nbf"] > current + skew:
        raise _fail("TRADING_PROOF_NOT_YET_VALID", "service proof is not yet valid", 401)
    if timestamps["exp"] <= current - skew:
        raise _fail("TRADING_PROOF_EXPIRED", "service proof expired", 401)
    if timestamps["nbf"] < timestamps["iat"] - skew:
        raise _fail("TRADING_PROOF_INVALID", "nbf precedes iat", 401)
    if timestamps["exp"] <= timestamps["iat"] or timestamps["exp"] - timestamps["iat"] > max_ttl:
        raise _fail("TRADING_PROOF_TTL_DENIED", "service proof TTL exceeds policy", 401)

    raw_scope = claims.get("scope")
    if not isinstance(raw_scope, str):
        raise _fail("TRADING_PROOF_SCOPE_DENIED", "service proof scope required", 403)
    scopes = frozenset(item for item in raw_scope.split() if item)
    if required_scope not in scopes:
        raise _fail("TRADING_PROOF_SCOPE_DENIED", "service proof scope denied", 403)
    return claims, timestamps, scopes


def _uuid_claim(claims: dict[str, Any], name: str) -> UUID:
    try:
        return UUID(str(claims[name]))
    except (KeyError, TypeError, ValueError) as exc:
        raise _fail("TRADING_PROOF_INVALID", f"valid UUID {name} claim required", 401) from exc


def _string_claim(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise _fail("TRADING_PROOF_INVALID", f"non-empty {name} claim required", 401)
    return value.strip()


def _digest_claim(claims: dict[str, Any]) -> str:
    digest = _string_claim(claims, "payload_sha256").lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise _fail("TRADING_PROOF_INVALID", "payload_sha256 must be lowercase SHA-256 hex", 401)
    return digest


def decode_directive_proof(
    authorization: str | None,
    *,
    now: float | None = None,
    required_scope: str = EXECUTE_SCOPE,
) -> DirectiveProof:
    """Verify an execute proof and parse the immutable directive bindings."""
    claims, timestamps, scopes = _verified_claims(
        authorization,
        now=now,
        required_scope=required_scope,
    )
    try:
        action = DirectiveAction(_string_claim(claims, "action"))
    except ValueError as exc:
        raise _fail("TRADING_PROOF_INVALID", "invalid directive action claim", 401) from exc
    return DirectiveProof(
        subject=_uuid_claim(claims, "sub"),
        fund_id=_uuid_claim(claims, "fund_id"),
        book_id=_uuid_claim(claims, "book_id"),
        action=action,
        instruction_ref=_string_claim(claims, "instruction_ref"),
        idempotency_key=_string_claim(claims, "idempotency_key"),
        payload_sha256=_digest_claim(claims),
        jti=_string_claim(claims, "jti"),
        issued_at=timestamps["iat"],
        not_before=timestamps["nbf"],
        expires_at=timestamps["exp"],
        scope=scopes,
    )


def decode_directive_read_proof(
    authorization: str | None,
    directive_id: UUID,
    *,
    now: float | None = None,
) -> DirectiveReadProof:
    """Verify a status-read proof bound to exactly one directive identifier."""
    claims, timestamps, scopes = _verified_claims(
        authorization,
        now=now,
        required_scope=READ_SCOPE,
    )
    expected = {
        "action": (claims.get("action"), READ_ACTION),
        "instruction_ref": (claims.get("instruction_ref"), str(directive_id)),
        "idempotency_key": (claims.get("idempotency_key"), f"status:{directive_id}"),
        "payload_sha256": (claims.get("payload_sha256"), EMPTY_PAYLOAD_SHA256),
    }
    mismatch = next((name for name, values in expected.items() if values[0] != values[1]), None)
    if mismatch:
        raise _fail("TRADING_PROOF_BINDING_DENIED", f"status proof {mismatch} mismatch", 403)
    _string_claim(claims, "jti")
    return DirectiveReadProof(
        subject=_uuid_claim(claims, "sub"),
        fund_id=_uuid_claim(claims, "fund_id"),
        book_id=_uuid_claim(claims, "book_id"),
        directive_id=directive_id,
        jti=_string_claim(claims, "jti"),
        issued_at=timestamps["iat"],
        not_before=timestamps["nbf"],
        expires_at=timestamps["exp"],
        scope=scopes,
    )


def bind_proof(proof: DirectiveProof, request: UserDirectiveRequest) -> None:
    """Bind every authority-bearing execute claim to the canonical request."""
    expected = {
        "fund_id": (proof.fund_id, request.fund_id),
        "book_id": (proof.book_id, request.book_id),
        "action": (proof.action, request.action),
        "instruction_ref": (proof.instruction_ref, request.instruction_ref),
        "idempotency_key": (proof.idempotency_key, request.idempotency_key),
        "payload_sha256": (proof.payload_sha256, request.payload_sha256()),
    }
    mismatch = next((name for name, values in expected.items() if values[0] != values[1]), None)
    if mismatch:
        raise _fail("TRADING_PROOF_BINDING_DENIED", f"service proof {mismatch} mismatch", 403)


def bind_read_proof(proof: DirectiveReadProof, record: Any) -> None:
    """Bind a verified read proof to the durable directive owner and scope."""
    expected = {
        "sub": (proof.subject, record.user_id),
        "fund_id": (proof.fund_id, record.fund_id),
        "book_id": (proof.book_id, record.book_id),
        "directive_id": (proof.directive_id, record.directive_id),
    }
    mismatch = next((name for name, values in expected.items() if values[0] != values[1]), None)
    if mismatch:
        raise _fail("TRADING_PROOF_BINDING_DENIED", f"status proof {mismatch} mismatch", 403)


# Backwards-compatible name retained for imports written before read/execute
# scopes were separated.
REQUIRED_SCOPE = EXECUTE_SCOPE


__all__ = [
    "DirectiveAuthError",
    "DirectiveProof",
    "DirectiveReadProof",
    "EMPTY_PAYLOAD_SHA256",
    "EXECUTE_SCOPE",
    "READ_ACTION",
    "READ_SCOPE",
    "REQUIRED_SCOPE",
    "bind_proof",
    "bind_read_proof",
    "decode_directive_proof",
    "decode_directive_read_proof",
]
