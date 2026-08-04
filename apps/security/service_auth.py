"""Minimal HS256 Service Token verifier for internal domain APIs.

The issuer remains an infrastructure concern.  Domain APIs only trust a
short-lived, signed token whose subject, department, service and scopes match
the command being called.  Secrets are read from the process environment and
are never included in error details or logs.
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


class ServiceAuthError(ValueError):
    """Raised when a Service Token cannot authorize a request."""

    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ServiceIdentity:
    """Verified identity and immutable claims used by a domain command."""

    subject: str
    department: str
    service: str
    scopes: frozenset[str]
    claims: Mapping[str, Any]


def _decode_segment(value: str, *, code: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise ServiceAuthError(code, "Service Token 형식이 올바르지 않습니다", status_code=401) from exc


def _json_segment(value: str, *, code: str) -> dict[str, Any]:
    try:
        decoded = json.loads(_decode_segment(value, code=code))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceAuthError(code, "Service Token JSON이 올바르지 않습니다", status_code=401) from exc
    if not isinstance(decoded, dict):
        raise ServiceAuthError(code, "Service Token JSON 객체가 필요합니다", status_code=401)
    return decoded


def authenticate_service_token(
    authorization: str | None,
    *,
    required_scope: str,
    expected_department: str,
    expected_service: str | None = None,
    expected_subject: str | None = None,
    secret_env: str,
    issuer_env: str | None = None,
    audience_env: str | None = None,
    now: float | None = None,
) -> ServiceIdentity:
    """Verify a Bearer HS256 token and enforce its domain scope.

    Missing server configuration is a 503-worthy deployment error.  Invalid
    or insufficient credentials are 401/403-worthy request errors.  The
    verifier deliberately requires ``exp`` and a non-empty scope list.
    """

    secret = os.environ.get(secret_env, "")
    if len(secret) < 32:
        raise ServiceAuthError(
            "SERVICE_AUTH_NOT_CONFIGURED",
            f"{secret_env}가 설정되지 않았거나 충분히 길지 않습니다",
            status_code=503,
        )

    if not authorization:
        raise ServiceAuthError("SERVICE_AUTH_REQUIRED", "Bearer Service Token이 필요합니다", status_code=401)
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise ServiceAuthError("SERVICE_AUTH_INVALID", "Bearer Service Token 형식이 필요합니다", status_code=401)
    token = parts[1]
    segments = token.split(".")
    if len(segments) != 3:
        raise ServiceAuthError("SERVICE_AUTH_INVALID", "Service Token 형식이 올바르지 않습니다", status_code=401)

    header = _json_segment(segments[0], code="SERVICE_AUTH_INVALID")
    claims = _json_segment(segments[1], code="SERVICE_AUTH_INVALID")
    if header.get("alg") != "HS256":
        raise ServiceAuthError("SERVICE_AUTH_ALGORITHM_DENIED", "허용되지 않은 Service Token 알고리즘입니다", status_code=401)

    expected_signature = hmac.new(
        secret.encode("utf-8"),
        f"{segments[0]}.{segments[1]}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    supplied_signature = _decode_segment(segments[2], code="SERVICE_AUTH_INVALID")
    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise ServiceAuthError("SERVICE_AUTH_INVALID", "Service Token 서명이 유효하지 않습니다", status_code=401)

    subject = claims.get("sub")
    department = claims.get("department")
    service = claims.get("service")
    scopes = claims.get("scopes")
    if not all(isinstance(value, str) and value.strip() for value in (subject, department, service)):
        raise ServiceAuthError("SERVICE_AUTH_INVALID", "Service Token Identity claim이 없습니다", status_code=401)
    if not isinstance(scopes, list) or not scopes or not all(isinstance(scope, str) and scope.strip() for scope in scopes):
        raise ServiceAuthError("SERVICE_AUTH_INVALID", "Service Token scopes claim이 올바르지 않습니다", status_code=401)

    current_time = time.time() if now is None else now
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or current_time >= float(exp):
        raise ServiceAuthError("SERVICE_AUTH_EXPIRED", "Service Token이 만료됐습니다", status_code=401)
    nbf = claims.get("nbf")
    if nbf is not None and (
        isinstance(nbf, bool) or not isinstance(nbf, (int, float)) or current_time < float(nbf)
    ):
        raise ServiceAuthError("SERVICE_AUTH_NOT_YET_VALID", "Service Token이 아직 유효하지 않습니다", status_code=401)

    if issuer_env:
        expected_issuer = os.environ.get(issuer_env, "").strip()
        if expected_issuer and claims.get("iss") != expected_issuer:
            raise ServiceAuthError("SERVICE_AUTH_ISSUER_DENIED", "Service Token issuer가 일치하지 않습니다", status_code=403)
    token_audience = claims.get("aud")
    if audience_env:
        expected_audience = os.environ.get(audience_env, "").strip()
        if expected_audience:
            if isinstance(token_audience, str):
                audiences = {token_audience}
            elif isinstance(token_audience, list) and all(isinstance(value, str) for value in token_audience):
                audiences = set(token_audience)
            else:
                audiences = set()
            if expected_audience not in audiences:
                raise ServiceAuthError("SERVICE_AUTH_AUDIENCE_DENIED", "Service Token audience가 일치하지 않습니다", status_code=403)

    if department != expected_department:
        raise ServiceAuthError("SERVICE_SCOPE_DENIED", "요청 본부와 Service Token 본부가 다릅니다", status_code=403)
    if expected_service is not None and service != expected_service:
        raise ServiceAuthError("SERVICE_SERVICE_DENIED", "호출 Service와 Service Token이 다릅니다", status_code=403)
    if required_scope not in scopes:
        raise ServiceAuthError("SERVICE_SCOPE_DENIED", "필요한 Service Token scope가 없습니다", status_code=403)
    if expected_subject is not None and subject != expected_subject:
        raise ServiceAuthError("SERVICE_SUBJECT_DENIED", "요청 주체와 Service Token subject가 다릅니다", status_code=403)

    return ServiceIdentity(
        subject=subject,
        department=department,
        service=service,
        scopes=frozenset(scopes),
        claims=claims,
    )
