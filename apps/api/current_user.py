#!/usr/bin/env python3
"""Portfolio BFF의 사용자 인증과 소유권 판정을 담당하는 단일 경계.

운영 모드에서는 hosted Supabase Auth가 발급한 access token의 서명, issuer,
audience, 만료와 ``authenticated`` role을 검증하고 JWT ``sub``만 사용자 ID로
사용한다. ``X-User-Id``는 서명된 신원 자료가 아니므로 운영 신원을 만들 수 없다.

기존 deterministic fixture는 ``APP_ENV=local|test``와
``PORTFOLIO_AUTH_MODE=fixture``를 *둘 다* 명시한 경우에만 사용할 수 있다. 따라서
환경 변수를 빼먹은 배포는 fixture로 조용히 후퇴하지 않고 Supabase JWT 모드에서
fail closed 한다.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt
import psycopg2
from fastapi import Header, HTTPException, Request
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWTError

_AUTH_REQUIRED_DEFAULT = "true"
_DEFAULT_AUTH_MODE = "supabase_jwt"
_FIXTURE_ENVIRONMENTS = frozenset({"local", "test"})
_SUPPORTED_JWT_ALGORITHMS = ("RS256", "ES256", "EdDSA")
_REQUEST_STATE_KEY = "portfolio_authenticated_user_id"
_MISSING = object()
_EXTERNAL_USER_DISPLAY_NAME = "Authenticated Supabase user"


class AuthConfigurationError(RuntimeError):
    """The deployment cannot establish a safe user-authentication boundary."""


def _http_error(status_code: int, detail: str) -> HTTPException:
    # Do not include a JWT, URL, key id, or upstream exception in browser errors.
    return HTTPException(status_code=status_code, detail=detail)


def auth_required() -> bool:
    """Return whether fixture mode also requires an identified user."""

    return os.getenv("PORTFOLIO_AUTH_REQUIRED", _AUTH_REQUIRED_DEFAULT).casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def auth_mode() -> str:
    """Resolve the authentication mode without an insecure implicit fallback."""

    mode = (os.getenv("PORTFOLIO_AUTH_MODE", _DEFAULT_AUTH_MODE).strip().casefold())
    if mode not in {_DEFAULT_AUTH_MODE, "fixture"}:
        raise AuthConfigurationError("unsupported portfolio authentication mode")

    runtime_environment = os.getenv("APP_ENV", "production").strip().casefold()
    if mode == "fixture" and runtime_environment not in _FIXTURE_ENVIRONMENTS:
        raise AuthConfigurationError("fixture authentication is not allowed here")
    return mode


def _trusted_https_url(value: str, *, setting: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    runtime_environment = os.getenv("APP_ENV", "production").strip().casefold()
    allowed_schemes = {"https"}
    if runtime_environment in _FIXTURE_ENVIRONMENTS:
        allowed_schemes.add("http")
    if (
        not normalized
        or parsed.scheme.casefold() not in allowed_schemes
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AuthConfigurationError(f"invalid {setting}")
    return normalized


def _supabase_auth_settings() -> tuple[str, str, str]:
    configured_issuer = os.getenv("SUPABASE_AUTH_ISSUER", "").strip()
    if configured_issuer:
        issuer = _trusted_https_url(configured_issuer, setting="Supabase issuer")
    else:
        supabase_url = _trusted_https_url(
            os.getenv("SUPABASE_URL", ""), setting="Supabase URL"
        )
        issuer = f"{supabase_url}/auth/v1"

    configured_jwks_url = os.getenv("SUPABASE_AUTH_JWKS_URL", "").strip()
    jwks_url = _trusted_https_url(
        configured_jwks_url or f"{issuer}/.well-known/jwks.json",
        setting="Supabase JWKS URL",
    )
    audience = os.getenv("SUPABASE_AUTH_AUDIENCE", "authenticated").strip()
    if not audience or any(character.isspace() for character in audience):
        raise AuthConfigurationError("invalid Supabase audience")
    return issuer, audience, jwks_url


def _supabase_publishable_key() -> str:
    """Return only a browser-safe API key; reject service-role/secret material."""

    value = (
        os.getenv("SUPABASE_PUBLISHABLE_KEY", "").strip()
        or os.getenv("SUPABASE_ANON_KEY", "").strip()
    )
    if not value or value.startswith("sb_secret_"):
        raise AuthConfigurationError("Supabase publishable key is unavailable")
    if value.startswith("sb_publishable_"):
        return value

    # Legacy anon keys are themselves long-lived JWTs. Inspecting this API key
    # is safe for classification only; it is never accepted as a user token.
    try:
        key_claims = jwt.decode(
            value,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
            },
            algorithms=["HS256"],
        )
    except PyJWTError as exc:
        raise AuthConfigurationError("invalid Supabase publishable key") from exc
    if key_claims.get("role") != "anon":
        raise AuthConfigurationError("privileged Supabase key is forbidden")
    return value


@lru_cache(maxsize=4)
def _jwks_client(jwks_url: str) -> PyJWKClient:
    # Supabase publishes current asymmetric keys here. A bounded in-memory cache
    # avoids putting Auth in the request hot path while still permitting rotation.
    return PyJWKClient(
        jwks_url,
        cache_keys=True,
        max_cached_keys=16,
        cache_jwk_set=True,
        lifespan=300,
        timeout=5,
    )


def _canonical_user_uuid(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _http_error(401, "portfolio_access_token_invalid") from exc


def _validate_authenticated_claims(claims: dict[str, object]) -> str:
    if claims.get("role") != "authenticated":
        raise _http_error(401, "portfolio_access_token_invalid")
    return _canonical_user_uuid(claims.get("sub"))


def _fetch_supabase_user_id(*, user_url: str, api_key: str, token: str) -> str:
    """Ask Supabase Auth to verify a legacy/shared-secret access token."""

    try:
        response = httpx.get(
            user_url,
            headers={"apikey": api_key, "Authorization": f"Bearer {token}"},
            follow_redirects=False,
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        raise _http_error(503, "portfolio_authentication_unavailable") from exc
    if response.status_code == 429 or response.status_code >= 500:
        raise _http_error(503, "portfolio_authentication_unavailable")
    if response.status_code != 200:
        raise _http_error(401, "portfolio_access_token_invalid")
    try:
        payload = response.json()
    except ValueError as exc:
        raise _http_error(503, "portfolio_authentication_unavailable") from exc
    if not isinstance(payload, dict):
        raise _http_error(503, "portfolio_authentication_unavailable")
    return _canonical_user_uuid(payload.get("id"))


def _verify_legacy_supabase_access_token(
    token: str, *, issuer: str, audience: str
) -> str:
    """Verify HS256 with Auth `/user`; never load the Supabase JWT secret."""

    api_key = _supabase_publishable_key()
    try:
        # The Auth server establishes authenticity. We still validate the token
        # contract locally, then bind its subject to the server-returned user.
        claims = jwt.decode(
            token,
            options={
                "require": ["aud", "exp", "iat", "iss", "role", "sub"],
                "verify_signature": False,
                "verify_aud": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_nbf": True,
            },
            algorithms=["HS256"],
            audience=audience,
            issuer=issuer,
        )
    except PyJWTError as exc:
        raise _http_error(401, "portfolio_access_token_invalid") from exc
    subject = _validate_authenticated_claims(claims)
    verified_user_id = _fetch_supabase_user_id(
        user_url=f"{issuer}/user", api_key=api_key, token=token
    )
    if verified_user_id != subject:
        raise _http_error(401, "portfolio_access_token_invalid")
    return subject


def verify_supabase_access_token(token: str) -> str:
    """Verify one Supabase access token and return its canonical user UUID."""

    try:
        issuer, audience, jwks_url = _supabase_auth_settings()
        unverified_header = jwt.get_unverified_header(token)
        algorithm = unverified_header.get("alg")
        if algorithm == "HS256":
            return _verify_legacy_supabase_access_token(
                token, issuer=issuer, audience=audience
            )
        if algorithm not in _SUPPORTED_JWT_ALGORITHMS:
            raise _http_error(401, "portfolio_access_token_invalid")
        signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key=signing_key.key,
            algorithms=list(_SUPPORTED_JWT_ALGORITHMS),
            audience=audience,
            issuer=issuer,
            options={
                "require": ["aud", "exp", "iat", "iss", "role", "sub"],
                "verify_signature": True,
                "verify_aud": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_nbf": True,
            },
        )
    except AuthConfigurationError as exc:
        raise _http_error(503, "portfolio_authentication_unavailable") from exc
    except PyJWKClientConnectionError as exc:
        raise _http_error(503, "portfolio_authentication_unavailable") from exc
    except PyJWTError as exc:
        raise _http_error(401, "portfolio_access_token_invalid") from exc

    return _validate_authenticated_claims(claims)


def _bearer_token(authorization: str | None) -> str | None:
    raw = (authorization or "").strip()
    if not raw:
        return None
    scheme, separator, credentials = raw.partition(" ")
    token = credentials.strip()
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not token
        or any(character.isspace() for character in token)
    ):
        raise _http_error(401, "portfolio_access_token_invalid")
    return token


def _control_database_url() -> str:
    """Return the private operational DB used for authorization projections."""

    # ``CONTROL_DATABASE_URL`` is the deployment-facing name.  Compose maps it
    # to the application's long-standing ``DATABASE_URL`` contract, while
    # direct process launches can provide the explicit name themselves.
    value = (
        os.getenv("CONTROL_DATABASE_URL", "").strip()
        or os.getenv("DATABASE_URL", "").strip()
    )
    if not value:
        raise _http_error(503, "portfolio_identity_projection_unavailable")
    return value


def _project_verified_subject(owner_id: str) -> None:
    """Idempotently project a verified Auth subject into the control DB.

    Hosted Supabase Auth remains the identity source of truth.  The control DB
    stores only the verified UUID, a non-identifying operational label and the
    last observation time so domain foreign keys have a local parent row.
    Existing status/display preferences are deliberately never overwritten.
    """

    try:
        with psycopg2.connect(
            _control_database_url(), connect_timeout=5
        ) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                insert into governance.user_profiles
                  (user_id, display_name, timezone, status,
                   identity_provider, auth_subject_observed_at)
                values (%s, %s, 'Asia/Seoul', 'ACTIVE', 'supabase', now())
                on conflict (user_id) do update
                  set auth_subject_observed_at = excluded.auth_subject_observed_at
                returning status
                """,
                (owner_id, _EXTERNAL_USER_DISPLAY_NAME),
            )
            row = cursor.fetchone()
            if row is None or str(row[0]).upper() != "ACTIVE":
                # Raising inside the connection context rolls back even the
                # observation-time update.  SUSPENDED/CLOSED is never revived.
                raise _http_error(403, "portfolio_user_inactive")
    except HTTPException:
        raise
    except (psycopg2.Error, TypeError, ValueError) as exc:
        # Schema drift, connectivity and malformed DSNs are authorization
        # failures, not a reason to continue without the local FK parent.
        raise _http_error(503, "portfolio_identity_projection_unavailable") from exc


def authorized_fund_memberships(owner_id: str) -> list[dict[str, object]]:
    """Return only currently effective fund grants for one verified subject."""

    try:
        with psycopg2.connect(
            _control_database_url(), connect_timeout=5
        ) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select fm.fund_id::text, fm.role, fm.status,
                       fm.effective_from, fm.effective_to
                  from governance.user_profiles up
                  join governance.fund_memberships fm
                    on fm.user_id = up.user_id
                 where up.user_id = %s
                   and up.status = 'ACTIVE'
                   and fm.status = 'ACTIVE'
                   and fm.effective_from <= now()
                   and (fm.effective_to is null or fm.effective_to > now())
                 order by fm.fund_id, fm.role
                """,
                (owner_id,),
            )
            rows = cursor.fetchall()
    except (psycopg2.Error, TypeError, ValueError) as exc:
        raise _http_error(503, "portfolio_authorization_unavailable") from exc

    return [
        {
            "fund_id": str(row[0]),
            "role": str(row[1]),
            "status": str(row[2]),
            "effective_from": row[3].isoformat()
            if hasattr(row[3], "isoformat")
            else str(row[3]),
            "effective_to": (
                row[4].isoformat()
                if row[4] is not None and hasattr(row[4], "isoformat")
                else (str(row[4]) if row[4] is not None else None)
            ),
        }
        for row in rows
    ]


def active_user_profile(owner_id: str) -> dict[str, str]:
    """Load an optional control-DB display profile for ``/ui/me``.

    A missing projection is not a reason to deny the frontend-selected user.
    It is rendered with the selected identifier until the normal profile data
    is available.
    """

    try:
        with psycopg2.connect(
            _control_database_url(), connect_timeout=5
        ) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select display_name, status
                  from governance.user_profiles
                 where user_id = %s
                """,
                (owner_id,),
            )
            row = cursor.fetchone()
    except (psycopg2.Error, TypeError, ValueError) as exc:
        raise _http_error(503, "portfolio_authorization_unavailable") from exc
    if row is None:
        return {"display_name": owner_id, "status": "ACTIVE"}
    return {"display_name": str(row[0]), "status": "ACTIVE"}


def require_fund_membership(owner_id: str | None, fund_id: str | None) -> None:
    """Require an effective local grant for fund-scoped production requests."""

    try:
        mode = auth_mode()
    except AuthConfigurationError as exc:
        raise _http_error(503, "portfolio_authentication_unavailable") from exc
    if mode == "fixture":
        return
    if owner_id is None:
        raise _http_error(401, "portfolio_authentication_required")
    try:
        canonical_owner_id = str(UUID(str(owner_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _http_error(401, "portfolio_access_token_invalid") from exc
    if not fund_id:
        raise _http_error(422, "portfolio_fund_id_required")
    try:
        canonical_fund_id = str(UUID(str(fund_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _http_error(422, "portfolio_fund_id_invalid") from exc

    memberships = authorized_fund_memberships(canonical_owner_id)
    if not any(row["fund_id"] == canonical_fund_id for row in memberships):
        raise _http_error(403, "portfolio_fund_forbidden")


def require_any_fund_membership(owner_id: str | None) -> list[dict[str, object]]:
    """Require at least one effective grant before global operator projections."""

    try:
        mode = auth_mode()
    except AuthConfigurationError as exc:
        raise _http_error(503, "portfolio_authentication_unavailable") from exc
    if mode == "fixture":
        return []
    if owner_id is None:
        raise _http_error(401, "portfolio_authentication_required")
    memberships = authorized_fund_memberships(owner_id)
    if not memberships:
        raise _http_error(403, "portfolio_fund_membership_required")
    return memberships


_TRADING_BOOK_ROLES = frozenset({"OWNER", "CIO", "TRADER"})


def _fixture_trading_books(owner_id: str) -> list[dict[str, str]]:
    """Load an explicit test-only book seed; no implicit fixture book exists."""

    raw = os.getenv("PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON", "").strip()
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _http_error(503, "portfolio_fixture_trading_books_invalid") from exc
    if not isinstance(rows, list):
        raise _http_error(503, "portfolio_fixture_trading_books_invalid")

    result: list[dict[str, str]] = []
    try:
        for row in rows:
            if not isinstance(row, dict) or str(row.get("user_id")) != owner_id:
                continue
            if str(row.get("role", "")).upper() not in _TRADING_BOOK_ROLES:
                continue
            if str(row.get("fund_status", "")).upper() != "ACTIVE":
                continue
            if str(row.get("book_status", "")).upper() != "ACTIVE":
                continue
            name = " ".join(str(row["name"]).strip().split())
            if not name:
                raise ValueError("empty book name")
            result.append(
                {
                    "fund_id": str(UUID(str(row["fund_id"]))),
                    "book_id": str(UUID(str(row["book_id"]))),
                    "name": name,
                }
            )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise _http_error(503, "portfolio_fixture_trading_books_invalid") from exc
    return sorted(result, key=lambda item: (item["fund_id"], item["name"], item["book_id"]))


def authorized_trading_books(owner_id: str) -> list[dict[str, str]]:
    """Return active books for which the subject has trading authority."""

    try:
        canonical_owner_id = str(UUID(str(owner_id)))
        mode = auth_mode()
    except (TypeError, ValueError, AttributeError) as exc:
        raise _http_error(401, "portfolio_access_token_invalid") from exc
    except AuthConfigurationError as exc:
        raise _http_error(503, "portfolio_authentication_unavailable") from exc
    if mode == "fixture":
        return _fixture_trading_books(canonical_owner_id)

    try:
        with psycopg2.connect(
            _control_database_url(), connect_timeout=5
        ) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select distinct f.fund_id::text, b.book_id::text, b.name
                  from governance.user_profiles up
                  join governance.fund_memberships fm
                    on fm.user_id = up.user_id
                  join accounting.funds f
                    on f.fund_id = fm.fund_id
                  join accounting.books b
                    on b.fund_id = f.fund_id
                 where up.user_id = %s
                   and up.status = 'ACTIVE'
                   and fm.status = 'ACTIVE'
                   and fm.role = any(%s)
                   and fm.effective_from <= now()
                   and (fm.effective_to is null or fm.effective_to > now())
                   and f.status = 'ACTIVE'
                   and b.status = 'ACTIVE'
                 order by f.fund_id::text, b.name, b.book_id::text
                """,
                (canonical_owner_id, sorted(_TRADING_BOOK_ROLES)),
            )
            rows = cursor.fetchall()
    except (psycopg2.Error, TypeError, ValueError) as exc:
        raise _http_error(503, "portfolio_authorization_unavailable") from exc
    return [
        {"fund_id": str(row[0]), "book_id": str(row[1]), "name": str(row[2])}
        for row in rows
    ]


def require_trading_book_access(
    owner_id: str | None,
    fund_id: str | None,
    book_id: str | None,
) -> dict[str, str]:
    """Authorize a user-directed order against one ACTIVE fund and book."""

    try:
        mode = auth_mode()
    except AuthConfigurationError as exc:
        raise _http_error(503, "portfolio_authentication_unavailable") from exc
    if owner_id is None:
        raise _http_error(401, "portfolio_authentication_required")
    try:
        canonical_owner_id = str(UUID(str(owner_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _http_error(401, "portfolio_access_token_invalid") from exc
    try:
        canonical_fund_id = str(UUID(str(fund_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _http_error(422, "portfolio_fund_id_invalid") from exc
    try:
        canonical_book_id = str(UUID(str(book_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _http_error(422, "portfolio_book_id_invalid") from exc

    if mode == "fixture":
        allowed = _fixture_trading_books(canonical_owner_id)
        if not any(
            row["fund_id"] == canonical_fund_id
            and row["book_id"] == canonical_book_id
            for row in allowed
        ):
            raise _http_error(403, "portfolio_trading_book_forbidden")
        return {
            "user_id": canonical_owner_id,
            "fund_id": canonical_fund_id,
            "book_id": canonical_book_id,
            "role": "FIXTURE",
        }

    try:
        with psycopg2.connect(
            _control_database_url(), connect_timeout=5
        ) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select fm.role
                  from governance.user_profiles up
                  join governance.fund_memberships fm
                    on fm.user_id = up.user_id
                  join accounting.funds f
                    on f.fund_id = fm.fund_id
                  join accounting.books b
                    on b.fund_id = f.fund_id
                 where up.user_id = %s
                   and up.status = 'ACTIVE'
                   and fm.fund_id = %s::uuid
                   and b.book_id = %s::uuid
                   and f.status = 'ACTIVE'
                   and b.status = 'ACTIVE'
                   and fm.status = 'ACTIVE'
                   and fm.role = any(%s)
                   and fm.effective_from <= now()
                   and (fm.effective_to is null or fm.effective_to > now())
                 order by case fm.role
                            when 'OWNER' then 1
                            when 'CIO' then 2
                            when 'TRADER' then 3
                            else 4
                          end
                 limit 1
                """,
                (
                    canonical_owner_id,
                    canonical_fund_id,
                    canonical_book_id,
                    sorted(_TRADING_BOOK_ROLES),
                ),
            )
            row = cursor.fetchone()
    except (psycopg2.Error, TypeError, ValueError) as exc:
        raise _http_error(503, "portfolio_authorization_unavailable") from exc

    if row is None:
        raise _http_error(403, "portfolio_trading_book_forbidden")
    return {
        "user_id": canonical_owner_id,
        "fund_id": canonical_fund_id,
        "book_id": canonical_book_id,
        "role": str(row[0]),
    }


def resolve_active_trading_instrument(
    identifier: str,
    instrument_id: str | None = None,
) -> dict[str, str]:
    """Resolve an exact six-character code or exact display name to one KRX stock.

    Names are conveniences at the BFF edge, not trading identifiers.  The
    result is accepted only when the active reference catalog yields exactly
    one current KRX symbol; Trading independently resolves it again.
    """

    query = " ".join(str(identifier).strip().split())
    if not query:
        raise _http_error(422, "paper_order_instrument_clarification_required")
    canonical_code = query.upper()
    if re.fullmatch(r"[0-9A-Z]{6}", canonical_code) is None:
        canonical_code = None
    canonical_instrument_id: str | None = None
    if instrument_id is not None:
        try:
            canonical_instrument_id = str(UUID(str(instrument_id)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise _http_error(422, "paper_order_instrument_clarification_required") from exc

    try:
        with psycopg2.connect(
            _control_database_url(), connect_timeout=5
        ) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select distinct i.instrument_id::text, sy.symbol
                  from reference.instruments i
                  join reference.instrument_symbols sy
                    on sy.instrument_id = i.instrument_id
                 where i.status = 'ACTIVE'
                   and i.market = 'KRX'
                   and upper(i.asset_class) = 'EQUITY'
                   and upper(i.instrument_type) = 'STOCK'
                   and sy.valid_from <= now()
                   and (sy.valid_to is null or sy.valid_to > now())
                   and sy.symbol ~ '^[0-9A-Z]{6}$'
                   and (
                         sy.symbol = %s
                         or (
                           %s::text is null
                           and regexp_replace(lower(i.display_name), '\\s+', '', 'g')
                               = regexp_replace(lower(%s), '\\s+', '', 'g')
                         )
                       )
                   and (%s::uuid is null or i.instrument_id = %s::uuid)
                 order by sy.symbol, i.instrument_id::text
                """,
                (
                    canonical_code,
                    canonical_code,
                    query,
                    canonical_instrument_id,
                    canonical_instrument_id,
                ),
            )
            rows = cursor.fetchall()
    except (psycopg2.Error, TypeError, ValueError) as exc:
        raise _http_error(503, "portfolio_authorization_unavailable") from exc
    if len(rows) != 1:
        raise _http_error(422, "paper_order_instrument_clarification_required")
    return {"instrument_id": str(rows[0][0]), "symbol": str(rows[0][1])}


def authenticate_request_headers(
    *,
    authorization: str | None,
    x_user_id: str | None,
    required: bool | None = None,
) -> str | None:
    """Authenticate request headers according to the explicit deployment mode."""

    try:
        mode = auth_mode()
    except AuthConfigurationError as exc:
        raise _http_error(503, "portfolio_authentication_unavailable") from exc

    if mode == _DEFAULT_AUTH_MODE:
        token = _bearer_token(authorization)
        if token is None:
            raise _http_error(401, "portfolio_authentication_required")
        owner_id = verify_supabase_access_token(token)
        claimed_header_id = (x_user_id or "").strip()
        if claimed_header_id and claimed_header_id != owner_id:
            raise _http_error(403, "portfolio_identity_header_mismatch")
        _project_verified_subject(owner_id)
        return owner_id

    # Fixture identity exists solely for explicit local/test deterministic runs.
    owner_id = (x_user_id or "").strip()
    effective_required = auth_required() if required is None else required
    if not owner_id and effective_required:
        raise _http_error(401, "portfolio_authentication_required")
    return owner_id or None


def _cached_request_user(request: Request) -> object:
    return getattr(request.state, _REQUEST_STATE_KEY, _MISSING)


def set_authenticated_request_user(request: Request, owner_id: str | None) -> None:
    """Cache the boundary result so route dependencies never verify twice."""

    setattr(request.state, _REQUEST_STATE_KEY, owner_id)


def current_user(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> str | None:
    """FastAPI dependency returning the user id selected by the frontend."""

    owner_id = (x_user_id or "").strip()
    if not owner_id and auth_required():
        raise _http_error(401, "portfolio_authentication_required")
    return owner_id or None


def optional_current_user(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> str | None:
    """Return the frontend-selected user id when the route needs one."""

    owner_id = (x_user_id or "").strip()
    return owner_id or None


def require_owner(
    owner_id: str | None,
    expected_user_id: str | None = None,
    *,
    required: bool | None = None,
) -> None:
    """Require an authenticated subject and enforce resource ownership."""

    required = auth_required() if required is None else required
    if required and not owner_id:
        raise _http_error(401, "portfolio_authentication_required")
    if owner_id and expected_user_id and owner_id != expected_user_id:
        raise _http_error(403, "portfolio_recommendation_forbidden")


__all__ = [
    "active_user_profile",
    "authorized_fund_memberships",
    "authorized_trading_books",
    "auth_mode",
    "auth_required",
    "authenticate_request_headers",
    "current_user",
    "optional_current_user",
    "require_any_fund_membership",
    "require_fund_membership",
    "require_owner",
    "require_trading_book_access",
    "resolve_active_trading_instrument",
    "set_authenticated_request_user",
    "verify_supabase_access_token",
]
