from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from apps.api import current_user as auth


def test_auth_mode_is_fixture_only_by_default() -> None:
    with patch.dict(os.environ, {"PORTFOLIO_AUTH_MODE": ""}, clear=False):
        assert auth.auth_mode() == "fixture"


@pytest.mark.parametrize("removed_mode", ("external", "supabase", "login", "oauth"))
def test_auth_mode_rejects_removed_external_mode(removed_mode: str) -> None:
    with patch.dict(os.environ, {"PORTFOLIO_AUTH_MODE": removed_mode}, clear=False):
        with pytest.raises(auth.AuthConfigurationError, match="fixture_only"):
            auth.auth_mode()


def test_fixture_identity_accepts_explicit_header_and_can_be_optional() -> None:
    subject = str(uuid4())
    with patch.dict(
        os.environ,
        {"PORTFOLIO_AUTH_MODE": "fixture", "PORTFOLIO_AUTH_REQUIRED": "false"},
        clear=False,
    ):
        assert auth.authenticate_request_headers(x_user_id=subject) == subject
        assert auth.authenticate_request_headers(x_user_id=None) is None


def test_required_fixture_identity_rejects_missing_header() -> None:
    with patch.dict(
        os.environ,
        {"PORTFOLIO_AUTH_MODE": "fixture", "PORTFOLIO_AUTH_REQUIRED": "true"},
        clear=False,
    ):
        with pytest.raises(HTTPException) as error:
            auth.authenticate_request_headers(x_user_id=None)
    assert error.value.status_code == 401
    assert error.value.detail == "portfolio_authentication_required"


def test_current_user_reads_only_the_fixed_identity_header() -> None:
    subject = str(uuid4())
    with patch.dict(
        os.environ,
        {"PORTFOLIO_AUTH_MODE": "fixture", "PORTFOLIO_AUTH_REQUIRED": "true"},
        clear=False,
    ):
        assert auth.current_user(x_user_id=subject) == subject


def test_fixture_scope_never_requires_database_membership() -> None:
    with (
        patch.dict(
            os.environ,
            {"PORTFOLIO_AUTH_MODE": "fixture", "PORTFOLIO_AUTH_REQUIRED": "false"},
            clear=False,
        ),
        patch.object(auth, "authorized_fund_memberships") as memberships,
    ):
        auth.require_fund_membership("fixture-user", "fixture-fund")
        assert auth.require_any_fund_membership("fixture-user") == []

    memberships.assert_not_called()


def test_active_user_profile_falls_back_to_selected_subject_when_unprovisioned() -> None:
    subject = str(uuid4())
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value = cursor_context
    with (
        patch.dict(
            os.environ,
            {"CONTROL_DATABASE_URL": "postgresql://control-db/portfolio"},
            clear=False,
        ),
        patch.object(auth.psycopg2, "connect", return_value=connection),
    ):
        profile = auth.active_user_profile(subject)

    assert profile == {"display_name": subject, "status": "ACTIVE"}


def test_authorized_funds_include_only_rows_returned_by_active_grant_query() -> None:
    subject = str(uuid4())
    fund_id = str(uuid4())
    now = datetime.now(timezone.utc)
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (fund_id, "OWNER", "ACTIVE", now, None),
        (fund_id, "TRADER", "ACTIVE", now, now + timedelta(days=1)),
    ]
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value = cursor_context
    with (
        patch.dict(
            os.environ,
            {"CONTROL_DATABASE_URL": "postgresql://control-db/portfolio"},
            clear=False,
        ),
        patch.object(auth.psycopg2, "connect", return_value=connection),
    ):
        memberships = auth.authorized_fund_memberships(subject)

    assert [row["role"] for row in memberships] == ["OWNER", "TRADER"]
    sql = " ".join(cursor.execute.call_args.args[0].split()).casefold()
    assert "up.status = 'active'" in sql
    assert "fm.status = 'active'" in sql
    assert "fm.effective_from <= now()" in sql
    assert "fm.effective_to is null or fm.effective_to > now()" in sql


def test_require_owner_enforces_only_the_local_demo_contract() -> None:
    owner = str(uuid4())
    with patch.dict(os.environ, {"PORTFOLIO_AUTH_REQUIRED": "true"}, clear=False):
        auth.require_owner(owner, owner)
        with pytest.raises(HTTPException) as error:
            auth.require_owner(owner, str(uuid4()))
    assert error.value.status_code == 403
    assert error.value.detail == "portfolio_recommendation_forbidden"
