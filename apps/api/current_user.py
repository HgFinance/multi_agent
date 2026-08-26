#!/usr/bin/env python3
"""Portfolio BFF의 로컬 모의투자 사용자 경계를 담당한다.

이 저장소는 로그인·세션·외부 사용자 인증을 구현하지 않는다. 로컬 모의투자
실행에서는 하나의 고정 데모 ID를 사용한다. 브라우저와 동일 출처 프록시는
``X-User-Id``를 같은 값으로 보내고, ``/ui/me``의 직접 조회도 그 ID로
초기화된다. 이 헤더는 공개 서비스의 사용자 인증 수단이 아니다.

``PORTFOLIO_AUTH_MODE``에 다른 값이 들어오면 fixture-only 계약 위반으로 즉시
실패한다. 운영 배포와 외부 사용자 로그인 연동은 이 모의투자 범위에 포함하지 않는다.
"""
from __future__ import annotations

import json
import os
import re
from uuid import UUID

import psycopg2
from fastapi import Header, HTTPException, Request

_REQUEST_STATE_KEY = "portfolio_authenticated_user_id"
_MISSING = object()
FIXED_DEMO_USER_ID = "00000000-0000-4000-8000-00000000cec0"


class AuthConfigurationError(RuntimeError):
    """The deployment cannot establish a safe user-authentication boundary."""


def _http_error(status_code: int, detail: str) -> HTTPException:
    # Do not include a credential, URL, key id, or upstream exception in browser errors.
    return HTTPException(status_code=status_code, detail=detail)


def auth_required() -> bool:
    """The closed-network fixture never waits for a browser login."""

    return False


def auth_mode() -> str:
    """Resolve the only supported authentication mode.

    A non-fixture value is rejected loudly so a removed login implementation
    cannot return by configuration drift.
    """

    configured = os.getenv("PORTFOLIO_AUTH_MODE", "fixture").strip().casefold()
    if configured not in {"", "fixture"}:
        raise AuthConfigurationError("fixture_only_portfolio_identity")
    return "fixture"


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


# 일부 KRX 상장사는 공시 표시명(`reference.instruments.display_name`)이
# 영문이지만 실제 사용자는 한글 통용 표기로 부른다(예: 네이버 -> "NAVER",
# 035420). 그 표는 리서치·퀀트·리스크가 공유하는 canonical 값이라 여기서
# 고치지 않는다 - 이 조회 지점에만 좁은 별칭을 둔다. 별칭은 심볼 코드로만
# 치환되고 그 뒤로는 원래의 exact-match 안전 질의를 그대로 타므로, 모호한
# 매칭을 새로 만들지 않는다(2026-08-20: "네이버 1주 매수"가 clarification
# 요구로 거부됐던 사례).
_INSTRUMENT_NAME_ALIASES: dict[str, str] = {
    "네이버": "035420",  # NAVER Corporation. 공시 표시명은 "NAVER".
    "하이닉스": "000660",  # 사용자가 흔히 생략하는 canonical 상호 접두사 "SK".
}
_NUMERIC_CODE_WITH_DISPLAY_NAME_RE = re.compile(
    r"^(?P<code>\d{6})(?:\s+[가-힣A-Za-z][가-힣A-Za-z0-9&+._\- ]{0,72})?$"
)


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
    code_with_name = _NUMERIC_CODE_WITH_DISPLAY_NAME_RE.fullmatch(query)
    canonical_code = code_with_name.group("code") if code_with_name else query.upper()
    if re.fullmatch(r"[0-9A-Z]{6}", canonical_code) is None:
        canonical_code = _INSTRUMENT_NAME_ALIASES.get(query)
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
    x_user_id: str | None,
    required: bool | None = None,
) -> str | None:
    """Resolve the fixed local demo identity without a login dependency."""

    try:
        auth_mode()
    except AuthConfigurationError as exc:
        raise _http_error(503, "portfolio_authentication_unavailable") from exc

    # Keep accepting an explicit fixture subject for internal contract tests
    # and server-to-server calls. Browser traffic is pinned by the frontend
    # proxy. A missing header is anonymous transport, not a login challenge.
    owner_id = (x_user_id or "").strip()
    del required
    return owner_id or None


def _cached_request_user(request: Request) -> object:
    return getattr(request.state, _REQUEST_STATE_KEY, _MISSING)


def set_authenticated_request_user(request: Request, owner_id: str | None) -> None:
    """Cache the boundary result so route dependencies never verify twice."""

    setattr(request.state, _REQUEST_STATE_KEY, owner_id)


def current_user(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> str | None:
    """Read the optional fixture identity supplied by a trusted local caller."""

    if not isinstance(x_user_id, str):
        x_user_id = None
    return authenticate_request_headers(x_user_id=x_user_id)


def optional_current_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> str | None:
    """Read an optional demo identity while preserving the private Discord bridge."""

    if not isinstance(authorization, str):
        authorization = None
    if not isinstance(x_user_id, str):
        x_user_id = None

    try:
        from .discord_ingress_auth import bearer_is_authorized
    except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
        from discord_ingress_auth import bearer_is_authorized  # type: ignore[no-redef]

    if bearer_is_authorized(authorization):
        return None
    return authenticate_request_headers(x_user_id=x_user_id, required=False)


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
    "FIXED_DEMO_USER_ID",
    "current_user",
    "optional_current_user",
    "require_any_fund_membership",
    "require_fund_membership",
    "require_owner",
    "require_trading_book_access",
    "resolve_active_trading_instrument",
    "set_authenticated_request_user",
]
