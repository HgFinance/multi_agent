"""Small, read-only probes shared by long-running service entrypoints.

Health probes must not execute domain work or mutate a ledger. Keeping the
transport checks here gives worker entrypoints one implementation while their
Compose healthchecks remain explicit, discoverable commands.
"""

from __future__ import annotations

import os
import re
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path

_ROLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def probe_postgres(
    *,
    dsn_env: str = "DATABASE_URL",
    role: str | None = None,
    role_env: str | None = None,
    required_relation: str | None = None,
) -> None:
    """Require a reachable PostgreSQL login and optional schema relation.

    The probe starts a transaction only for SET LOCAL and SELECT and rolls it
    back on exit. It never runs a domain write. Role names are validated before
    being interpolated as identifiers.
    """

    dsn = str(os.environ.get(dsn_env) or "").strip()
    if not dsn:
        raise RuntimeError(f"{dsn_env} is not configured")
    selected_role = str(
        role or (os.environ.get(role_env or "") if role_env else "") or ""
    ).strip()
    if selected_role and not _ROLE.fullmatch(selected_role):
        raise RuntimeError(f"invalid PostgreSQL role for {dsn_env}")

    try:
        import psycopg2
        from psycopg2 import sql
    except ImportError as exc:  # pragma: no cover - image dependency contract
        raise RuntimeError("psycopg2 is required for the PostgreSQL health probe") from exc

    try:
        with psycopg2.connect(dsn, connect_timeout=3) as connection, connection.cursor() as cursor:
            if selected_role:
                cursor.execute(
                    sql.SQL("set local role {}").format(sql.Identifier(selected_role))
                )
            cursor.execute("select 1")
            if cursor.fetchone() != (1,):
                raise RuntimeError(f"{dsn_env} probe returned an invalid result")
            if required_relation:
                cursor.execute("select to_regclass(%s)", (required_relation,))
                if cursor.fetchone() != (required_relation,):
                    raise RuntimeError(
                        "required PostgreSQL relation is unavailable: "
                        f"{required_relation}"
                    )
    except psycopg2.Error as exc:
        raise RuntimeError(f"{dsn_env} PostgreSQL probe failed") from exc


def probe_http(url: str, *, timeout: float = 3.0) -> None:
    """Require a successful internal health/readiness endpoint."""

    target = str(url or "").strip()
    if not target:
        raise RuntimeError("health endpoint is not configured")
    request = urllib.request.Request(
        target,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "hgfinance-healthcheck/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"health endpoint returned HTTP {response.status}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("health endpoint probe failed") from exc


def probe_sqlite(
    path: str | os.PathLike[str],
    *,
    required_tables: Iterable[str] = (),
) -> None:
    """Open an existing SQLite runtime store read-only and run integrity checks."""

    database = Path(path)
    if not database.is_file():
        raise RuntimeError(f"SQLite runtime store is missing: {database}")
    uri = f"file:{database.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=3) as connection:
            if connection.execute("pragma quick_check").fetchone() != ("ok",):
                raise RuntimeError(f"SQLite runtime store failed quick_check: {database}")
            names = {
                str(row[0])
                for row in connection.execute(
                    "select name from sqlite_master where type = 'table'"
                )
            }
            missing = set(required_tables) - names
            if missing:
                raise RuntimeError(f"SQLite runtime tables are missing: {sorted(missing)}")
    except sqlite3.Error as exc:
        raise RuntimeError("SQLite runtime store probe failed") from exc
