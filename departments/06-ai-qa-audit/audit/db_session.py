"""Explicit writer mode and session-stable runtime role selection."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from urllib.parse import SplitResult, urlsplit, urlunsplit

_ROLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def runtime_role(environ: Mapping[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    role = str(source.get("DATABASE_RUNTIME_ROLE", "") or "").strip()
    if role and _ROLE_PATTERN.fullmatch(role) is None:
        raise RuntimeError("DATABASE_RUNTIME_ROLE is not a safe SQL role name")
    if role and role not in {
            "svc_audit_api", "svc_qa_worker", "svc_qa_reproducer"}:
        raise RuntimeError("QA runtime role is not allowlisted")
    return role


def _replace_port(parts: SplitResult, port: int) -> str:
    credentials, separator, host_port = parts.netloc.rpartition("@")
    prefix = f"{credentials}{separator}" if separator else ""
    if host_port.startswith("["):
        host, closing, _old_port = host_port.partition("]")
        replacement = f"{host}{closing}:{port}"
    else:
        host = host_port.rsplit(":", 1)[0]
        replacement = f"{host}:{port}"
    return urlunsplit(parts._replace(netloc=f"{prefix}{replacement}"))


def runtime_session_dsn(
    dsn: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Choose a direct/session endpoint before applying session ``SET ROLE``."""

    source = os.environ if environ is None else environ
    if not runtime_role(source):
        return dsn
    override = str(source.get("DATABASE_SESSION_URL", "") or "").strip()
    candidate = override or dsn
    parts = urlsplit(candidate)
    try:
        port = parts.port
    except ValueError as exc:
        raise RuntimeError("DATABASE_SESSION_URL has an invalid port") from exc
    host = (parts.hostname or "").lower()
    if port == 6543 and host.endswith(".pooler.supabase.com") and not override:
        return _replace_port(parts, 5432)
    if port == 6543 or re.search(r"(?:^|\s)port\s*=\s*6543(?:\s|$)", candidate):
        raise RuntimeError(
            "DATABASE_RUNTIME_ROLE requires a session/direct DATABASE_SESSION_URL; "
            "transaction-pool port 6543 cannot preserve SET ROLE"
        )
    return candidate


def configure_writer_connection(
    connection,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Prepare one pooled connection before any canonical QA write.

    The caller must establish this connection with :func:`runtime_session_dsn`.
    ``SET ROLE`` is then committed once so every later transaction on the
    session-stable connection runs as the scoped role.  An unset role preserves
    local-development behavior while still selecting READ WRITE.
    """

    source = os.environ if environ is None else environ
    role = runtime_role(source)
    set_session = getattr(connection, "set_session", None)
    if set_session is not None:
        set_session(readonly=False)
    if role:
        with connection.cursor() as cursor:
            cursor.execute(f'SET ROLE "{role}"')
        connection.commit()
        # Verify across a commit boundary so a 6543 transaction-pool
        # connection cannot silently resume with the broker login.
        with connection.cursor() as cursor:
            cursor.execute("select current_user")
            active_role = str(cursor.fetchone()[0])
        connection.commit()
        if active_role != role:
            raise RuntimeError(
                f"database runtime role did not persist: {active_role}"
            )
    return role or None
