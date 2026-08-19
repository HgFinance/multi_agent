"""Write-capable Supabase connections with an enforceable runtime role.

``SET ROLE`` is session state.  It cannot be a security boundary through the
Supabase transaction pooler (port 6543), whose backend may change after every
commit.  Runtime-role connections therefore use an explicit session DSN, or
upgrade the standard Supavisor shared-pool endpoint to session mode (port
5432), before selecting the scoped role.
"""

from __future__ import annotations

import os
import re
from urllib.parse import SplitResult, urlsplit, urlunsplit


_ROLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ALLOWED_RUNTIME_ROLES = frozenset({
    "svc_dataset_builder",
    "svc_quant",
})


def _runtime_role(explicit_role: str | None = None) -> str:
    raw_role = (
        os.environ.get("DATABASE_RUNTIME_ROLE", "")
        if explicit_role is None
        else explicit_role
    )
    role = raw_role.strip()
    if role and _ROLE_PATTERN.fullmatch(role) is None:
        raise RuntimeError("DATABASE_RUNTIME_ROLE is not a safe SQL role name")
    if role and role not in _ALLOWED_RUNTIME_ROLES:
        allowed = ", ".join(sorted(_ALLOWED_RUNTIME_ROLES))
        raise RuntimeError(f"quant runtime role must be one of: {allowed}")
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


def runtime_session_dsn(dsn: str, *, role: str | None = None) -> str:
    """Return a session-stable DSN whenever ``SET ROLE`` is requested."""

    selected_role = _runtime_role(role)
    if not selected_role:
        return dsn
    override = os.environ.get("DATABASE_SESSION_URL", "").strip()
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


def connect(
    dsn: str,
    *,
    connect_timeout: int = 20,
    runtime_role: str | None = None,
):
    import psycopg2

    role = _runtime_role(runtime_role)
    selected_dsn = runtime_session_dsn(dsn, role=role)
    conn = psycopg2.connect(selected_dsn, connect_timeout=connect_timeout)
    try:
        conn.set_session(readonly=False)
        if role:
            # selected_dsn is session-stable here, so the reduction survives
            # every later commit on this client connection.
            with conn.cursor() as cursor:
                cursor.execute(f'SET ROLE "{role}"')
            conn.commit()
            # Prove that the role survived a transaction boundary.  This
            # fails closed if a transaction pool endpoint slipped through.
            with conn.cursor() as cursor:
                cursor.execute("select current_user")
                active_role = str(cursor.fetchone()[0])
            conn.commit()
            if active_role != role:
                raise RuntimeError(
                    f"database runtime role did not persist: {active_role}"
                )
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        raise
    return conn
