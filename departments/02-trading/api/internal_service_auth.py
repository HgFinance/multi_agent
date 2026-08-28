"""Fail-closed service authentication for legacy Trading/OMS mutations.

The authenticated USER_DIRECTIVE plane has its own request-bound proof format
in ``directives.auth``.  This module intentionally protects only the older
service-to-service OMS endpoints.  A token for one plane is not accepted by
the other plane, and an intent proposer never receives risk, submit, broker,
or cancel authority merely because it can reach the Trading API.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


INTENT_WRITE_SCOPE = "trading.intent.write"
RISK_DECISION_WRITE_SCOPE = "trading.risk_decision.write"
ORDER_SUBMIT_SCOPE = "trading.order.submit"
BROKER_EVENT_WRITE_SCOPE = "trading.broker_event.write"
ORDER_CANCEL_SCOPE = "trading.order.cancel"
CONDITIONAL_RULE_EXECUTE_SCOPE = "trading.conditional_rule.execute"
STRATEGY_PAPER_EXECUTE_SCOPE = "trading.strategy-paper.execute"

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


class InternalServiceAuthError(ValueError):
    """A deployment or caller failed the internal service-auth contract."""

    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class InternalServiceIdentity:
    subject: str
    department: str
    service: str
    scopes: frozenset[str]
    token_id: str
    claims: Mapping[str, Any]


@dataclass(frozen=True)
class MutationAuthPolicy:
    required_scopes: frozenset[str]
    department: str
    services: frozenset[str]


# The token issuer is responsible for assigning these identities.  The
# Trading API gets only the verifier secret; Hermes containers get neither the
# verifier secret nor privileged service identities.
INTENT_WRITER_POLICY = MutationAuthPolicy(
    frozenset({INTENT_WRITE_SCOPE}),
    "trading-department",
    frozenset({"trading-hermes", "trading-oms"}),
)
RISK_DECISION_POLICY = MutationAuthPolicy(
    frozenset({RISK_DECISION_WRITE_SCOPE}),
    "risk-management",
    frozenset({"risk-api"}),
)
ORDER_SUBMIT_POLICY = MutationAuthPolicy(
    frozenset({ORDER_SUBMIT_SCOPE}),
    "trading-department",
    frozenset({"trading-oms"}),
)
BROKER_EVENT_POLICY = MutationAuthPolicy(
    frozenset({BROKER_EVENT_WRITE_SCOPE}),
    "trading-department",
    frozenset({"paper-broker-adapter", "trading-reconciler"}),
)
ORDER_CANCEL_POLICY = MutationAuthPolicy(
    frozenset({ORDER_CANCEL_SCOPE}),
    "trading-department",
    frozenset({"trading-oms"}),
)
CASE_PAPER_ORDER_POLICY = MutationAuthPolicy(
    frozenset({ORDER_SUBMIT_SCOPE, BROKER_EVENT_WRITE_SCOPE}),
    "trading-department",
    frozenset({"trading-oms"}),
)
CONDITIONAL_RULE_EXECUTOR_POLICY = MutationAuthPolicy(
    frozenset({CONDITIONAL_RULE_EXECUTE_SCOPE}),
    "trading-department",
    frozenset({"conditional-rule-worker"}),
)
STRATEGY_PAPER_EXECUTE_POLICY = MutationAuthPolicy(
    frozenset({STRATEGY_PAPER_EXECUTE_SCOPE}),
    "trading-department",
    frozenset({"strategy-runtime-control"}),
)


def _fail(code: str, message: str, status_code: int) -> InternalServiceAuthError:
    return InternalServiceAuthError(code, message, status_code=status_code)


def _decode_segment(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise _fail("TRADING_INTERNAL_AUTH_INVALID", "invalid service token encoding", 401) from exc


def _json_segment(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(_decode_segment(value))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("TRADING_INTERNAL_AUTH_INVALID", "invalid service token JSON", 401) from exc
    if not isinstance(decoded, dict):
        raise _fail("TRADING_INTERNAL_AUTH_INVALID", "service token JSON object required", 401)
    return decoded


def required_internal_auth_config() -> tuple[str, str, str]:
    """Return verified production-grade verifier settings or fail closed."""

    secret = os.environ.get("TRADING_INTERNAL_SERVICE_AUTH_SECRET", "")
    issuer = os.environ.get("TRADING_INTERNAL_SERVICE_AUTH_ISSUER", "").strip()
    audience = os.environ.get("TRADING_INTERNAL_SERVICE_AUTH_AUDIENCE", "").strip()
    normalized_secret = secret.strip().casefold()
    placeholder = any(marker in normalized_secret for marker in _PLACEHOLDER_SECRET_MARKERS)
    user_directive_secret = os.environ.get("TRADING_SERVICE_AUTH_SECRET", "")
    shared_with_user_plane = bool(user_directive_secret) and hmac.compare_digest(
        secret,
        user_directive_secret,
    )
    if len(secret) < 32 or placeholder or shared_with_user_plane or not issuer or not audience:
        raise _fail(
            "TRADING_INTERNAL_AUTH_NOT_CONFIGURED",
            "internal Trading service authentication is not configured",
            503,
        )
    return secret, issuer, audience


def _required_timestamp(claims: Mapping[str, Any], name: str) -> float:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail("TRADING_INTERNAL_AUTH_INVALID", f"numeric {name} claim required", 401)
    return float(value)


def authenticate_internal_service(
    authorization: str | None,
    *,
    policy: MutationAuthPolicy,
    now: float | None = None,
) -> InternalServiceIdentity:
    """Verify a short-lived HS256 token and its least-privilege role policy."""

    secret, expected_issuer, expected_audience = required_internal_auth_config()
    parts = (authorization or "").split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise _fail("TRADING_INTERNAL_AUTH_REQUIRED", "Bearer service token required", 401)
    segments = parts[1].split(".")
    if len(segments) != 3:
        raise _fail("TRADING_INTERNAL_AUTH_INVALID", "invalid service token format", 401)

    header = _json_segment(segments[0])
    claims = _json_segment(segments[1])
    if header.get("alg") != "HS256" or header.get("typ") not in (None, "JWT"):
        raise _fail("TRADING_INTERNAL_AUTH_ALGORITHM_DENIED", "HS256 JWT required", 401)
    expected_signature = hmac.new(
        secret.encode("utf-8"),
        f"{segments[0]}.{segments[1]}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected_signature, _decode_segment(segments[2])):
        raise _fail("TRADING_INTERNAL_AUTH_INVALID", "invalid service token signature", 401)

    if claims.get("iss") != expected_issuer:
        raise _fail("TRADING_INTERNAL_AUTH_ISSUER_DENIED", "service token issuer mismatch", 403)
    raw_audience = claims.get("aud")
    if isinstance(raw_audience, str):
        audiences = {raw_audience}
    elif isinstance(raw_audience, list) and all(isinstance(item, str) for item in raw_audience):
        audiences = set(raw_audience)
    else:
        audiences = set()
    if expected_audience not in audiences:
        raise _fail("TRADING_INTERNAL_AUTH_AUDIENCE_DENIED", "service token audience mismatch", 403)

    current = time.time() if now is None else float(now)
    try:
        skew = max(float(os.environ.get("TRADING_INTERNAL_AUTH_CLOCK_SKEW_SECONDS", "5")), 0.0)
        max_ttl = max(float(os.environ.get("TRADING_INTERNAL_AUTH_MAX_TTL_SECONDS", "300")), 1.0)
    except ValueError as exc:
        raise _fail(
            "TRADING_INTERNAL_AUTH_NOT_CONFIGURED",
            "invalid service token timing policy",
            503,
        ) from exc
    issued_at = _required_timestamp(claims, "iat")
    not_before = _required_timestamp(claims, "nbf")
    expires_at = _required_timestamp(claims, "exp")
    if issued_at > current + skew or not_before > current + skew:
        raise _fail("TRADING_INTERNAL_AUTH_NOT_YET_VALID", "service token is not yet valid", 401)
    if expires_at <= current - skew:
        raise _fail("TRADING_INTERNAL_AUTH_EXPIRED", "service token expired", 401)
    if not_before < issued_at - skew or expires_at <= issued_at:
        raise _fail("TRADING_INTERNAL_AUTH_INVALID", "invalid service token validity window", 401)
    if expires_at - issued_at > max_ttl:
        raise _fail("TRADING_INTERNAL_AUTH_TTL_DENIED", "service token TTL exceeds policy", 401)

    subject = claims.get("sub")
    department = claims.get("department")
    service = claims.get("service")
    token_id = claims.get("jti")
    identity_values = (subject, department, service, token_id)
    if not all(isinstance(value, str) and value.strip() for value in identity_values):
        raise _fail("TRADING_INTERNAL_AUTH_INVALID", "service identity claims are incomplete", 401)
    raw_scopes = claims.get("scopes")
    if not isinstance(raw_scopes, list) or not all(
        isinstance(scope, str) and scope.strip() for scope in raw_scopes
    ):
        raise _fail("TRADING_INTERNAL_AUTH_INVALID", "non-empty scopes list required", 401)
    scopes = frozenset(raw_scopes)
    if department != policy.department or service not in policy.services:
        raise _fail("TRADING_INTERNAL_AUTH_IDENTITY_DENIED", "service identity is not allowed", 403)
    if not policy.required_scopes.issubset(scopes):
        raise _fail("TRADING_INTERNAL_AUTH_SCOPE_DENIED", "service token scope denied", 403)

    return InternalServiceIdentity(
        subject=subject.strip(),
        department=department.strip(),
        service=service.strip(),
        scopes=scopes,
        token_id=token_id.strip(),
        claims=claims,
    )


def scopes_for(policy: MutationAuthPolicy) -> tuple[str, ...]:
    """Stable introspection helper used by contract tests and documentation."""

    return tuple(sorted(policy.required_scopes))


__all__ = [
    "BROKER_EVENT_POLICY",
    "BROKER_EVENT_WRITE_SCOPE",
    "CASE_PAPER_ORDER_POLICY",
    "CONDITIONAL_RULE_EXECUTOR_POLICY",
    "CONDITIONAL_RULE_EXECUTE_SCOPE",
    "INTENT_WRITER_POLICY",
    "INTENT_WRITE_SCOPE",
    "InternalServiceAuthError",
    "InternalServiceIdentity",
    "MutationAuthPolicy",
    "ORDER_CANCEL_POLICY",
    "ORDER_CANCEL_SCOPE",
    "ORDER_SUBMIT_POLICY",
    "ORDER_SUBMIT_SCOPE",
    "RISK_DECISION_POLICY",
    "STRATEGY_PAPER_EXECUTE_POLICY",
    "STRATEGY_PAPER_EXECUTE_SCOPE",
    "RISK_DECISION_WRITE_SCOPE",
    "authenticate_internal_service",
    "required_internal_auth_config",
    "scopes_for",
]
