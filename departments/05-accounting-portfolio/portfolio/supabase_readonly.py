"""Async, read-only Supabase/PostgreSQL adapter for portfolio recommendations.

The adapter reads only canonical tables from ``supabase/migrations``. It never
uses a service-role key, mutates database state, or invents a portfolio when a
strategy version does not contain the suitability metadata required by the
portfolio contract.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import socket
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

READ_ONLY_DRIVER_ENV = "SUPABASE_READONLY_DRIVER"
DEFAULT_DSN_ENVIRONMENTS = ("SUPABASE_DATABASE_URL", "DATABASE_URL")
DEFAULT_CONNECT_RETRIES = 2
DEFAULT_CONNECT_BACKOFF_SECONDS = 0.5

CONNECTION_PROBE_SQL = "SELECT current_setting('transaction_read_only') AS transaction_read_only"
SCHEMA_PROBE_SQL = """
SELECT
    to_regclass('strategy.versions')::text AS strategy_versions,
    to_regclass('research.documents')::text AS research_documents,
    to_regclass('execution.market_snapshots')::text AS market_snapshots,
    to_regclass('accounting.portfolio_snapshots')::text AS accounting_snapshots
"""

PORTFOLIO_DATA_DIAGNOSTIC_SQL = """
SELECT
    COUNT(*) AS versions_total,
    COUNT(*) FILTER (
        WHERE v.effective_from <= %s
          AND (v.effective_to IS NULL OR v.effective_to > %s)
    ) AS versions_as_of,
    COUNT(*) FILTER (
        WHERE v.deployment_state IN ('SHADOW', 'PAPER', 'LIVE_CANDIDATE', 'LIVE')
    ) AS versions_deployable,
    COUNT(*) FILTER (WHERE v.target_portfolio_schema IS NOT NULL) AS versions_with_schema,
    COUNT(*) FILTER (
        WHERE v.effective_from <= %s
          AND (v.effective_to IS NULL OR v.effective_to > %s)
          AND v.deployment_state IN ('SHADOW', 'PAPER', 'LIVE_CANDIDATE', 'LIVE')
          AND s.status IN ('SHADOW', 'PAPER', 'LIVE_CANDIDATE', 'LIVE')
          AND v.target_portfolio_schema IS NOT NULL
          AND v.target_portfolio_schema <> '{}'::jsonb
    ) AS candidate_rows_before_contract
FROM strategy.versions AS v
JOIN strategy.strategies AS s ON s.strategy_id = v.strategy_id
"""

MARKET_DATA_DIAGNOSTIC_SQL = """
SELECT
    COUNT(*) AS snapshots_total,
    COUNT(*) FILTER (WHERE as_of <= %s) AS snapshots_as_of,
    COUNT(*) FILTER (WHERE as_of <= %s AND quality_status IN ('PASS', 'WARN'))
        AS usable_snapshots_as_of
FROM execution.market_snapshots
"""

PORTFOLIO_CATALOG_SQL = """
SELECT
    s.strategy_id::text AS strategy_id,
    s.strategy_code,
    s.name,
    s.family,
    s.status AS strategy_status,
    v.strategy_version_id::text AS strategy_version_id,
    v.version,
    v.target_portfolio_schema,
    v.config,
    v.effective_from,
    v.effective_to,
    v.deployment_state
FROM strategy.versions AS v
JOIN strategy.strategies AS s ON s.strategy_id = v.strategy_id
WHERE v.effective_from IS NOT NULL
  AND v.effective_from <= %s
  AND (v.effective_to IS NULL OR v.effective_to > %s)
  AND v.deployment_state IN ('SHADOW', 'PAPER', 'LIVE_CANDIDATE', 'LIVE')
  AND s.status IN ('SHADOW', 'PAPER', 'LIVE_CANDIDATE', 'LIVE')
ORDER BY v.effective_from DESC, s.strategy_code, v.version DESC
LIMIT 200
"""

RESEARCH_DOCUMENTS_SQL = """
SELECT
    d.document_id::text AS document_id,
    d.document_type,
    d.title,
    d.canonical_url,
    d.published_at,
    d.observed_at,
    d.status,
    s.source_code
FROM research.documents AS d
JOIN reference.data_sources AS s ON s.source_id = d.source_id
WHERE d.observed_at <= %s
  AND (d.published_at IS NULL OR d.published_at <= %s)
  AND d.status IN ('ACTIVE', 'CORRECTED')
ORDER BY d.observed_at DESC
LIMIT 100
"""

MARKET_SNAPSHOTS_SQL = """
SELECT
    market_snapshot_id::text AS market_snapshot_id,
    instrument_id::text AS instrument_id,
    as_of,
    bid::text AS bid,
    ask::text AS ask,
    last_price::text AS last_price,
    mid::text AS mid,
    spread::text AS spread,
    currency,
    quality_status,
    source_ref,
    content_hash
FROM execution.market_snapshots
WHERE as_of <= %s
  AND quality_status IN ('PASS', 'WARN')
ORDER BY as_of DESC
LIMIT 200
"""

# The portfolio UI must not derive symbols from a static browser catalog when
# Supabase is the live source.  Resolve the canonical instrument identity,
# primary symbol, domestic asset class and the latest PIT market value in one
# read-only query.  The join is intentionally restricted to Korean equities;
# bonds, global assets and derivatives cannot enter the current product scope.
DOMESTIC_EQUITY_UNIVERSE_SQL = """
SELECT
    i.instrument_id::text AS instrument_id,
    s.symbol,
    s.market AS exchange,
    i.display_name AS name,
    i.instrument_type,
    i.asset_class,
    i.currency,
    i.status AS instrument_status,
    m.market_snapshot_id::text AS market_snapshot_id,
    m.as_of AS market_as_of,
    m.last_price::text AS last_price,
    m.mid::text AS mid,
    m.quality_status,
    m.source_ref
FROM reference.instruments AS i
JOIN reference.instrument_symbols AS s
  ON s.instrument_id = i.instrument_id
 AND s.is_primary = TRUE
 AND s.valid_from <= %s
 AND (s.valid_to IS NULL OR s.valid_to > %s)
JOIN LATERAL (
    SELECT
        ms.market_snapshot_id,
        ms.as_of,
        ms.last_price,
        ms.mid,
        ms.quality_status,
        ms.source_ref
    FROM execution.market_snapshots AS ms
    WHERE ms.instrument_id = i.instrument_id
      AND ms.as_of <= %s
      AND ms.quality_status IN ('PASS', 'WARN')
    ORDER BY ms.as_of DESC
    LIMIT 1
) AS m ON TRUE
WHERE i.instrument_type IN ('EQUITY', 'STOCK')
  AND i.market IN ('KRX', 'KOSPI', 'KOSDAQ')
  AND i.currency = 'KRW'
  AND i.status = 'ACTIVE'
ORDER BY s.symbol
LIMIT 500
"""

ACCOUNTING_SNAPSHOT_SQL = """
SELECT
    portfolio_snapshot_id::text AS portfolio_snapshot_id,
    fund_id::text AS fund_id,
    book_id::text AS book_id,
    as_of,
    cash,
    positions,
    gross_exposure::text AS gross_exposure,
    net_exposure::text AS net_exposure,
    nav::text AS nav,
    currency,
    quality_status,
    content_hash,
    schema_version
FROM accounting.portfolio_snapshots
WHERE fund_id = %s::uuid
  AND as_of <= %s
ORDER BY as_of DESC
LIMIT 1
"""


class SupabaseReadOnlyError(RuntimeError):
    """Raised when a read-only adapter cannot be configured or queried."""


@dataclass(frozen=True)
class SupabasePreflight:
    """Safe, credential-free connectivity and schema diagnostics."""

    status: str
    driver: str
    dsn_configured: bool
    dns_status: str
    connection_status: str
    schema_status: str
    schema_objects: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    read_only: bool = True
    external_writes: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "driver": self.driver,
            "dsn_configured": self.dsn_configured,
            "dns_status": self.dns_status,
            "connection_status": self.connection_status,
            "schema_status": self.schema_status,
            "schema_objects": list(self.schema_objects),
            "reasons": list(self.reasons),
            "read_only": self.read_only,
            "external_writes": self.external_writes,
        }


RowFetcher = Callable[[str, tuple[Any, ...]], Awaitable[Sequence[Mapping[str, Any]]]]


@dataclass(frozen=True)
class SupabaseReadSnapshot:
    """Canonical read model passed into the async recommendation graph."""

    as_of: datetime
    source: str
    quality_status: str
    candidates: tuple[dict[str, Any], ...]
    research_context: dict[str, Any]
    market_context: dict[str, Any]
    accounting_context: dict[str, Any]
    reasons: tuple[str, ...] = ()
    queries: tuple[str, ...] = ()
    read_only: bool = True
    external_writes: bool = False
    preflight: dict[str, Any] = field(default_factory=dict)
    data_diagnostics: dict[str, Any] = field(default_factory=dict)

    def as_pipeline_context(self) -> dict[str, Any]:
        """Return JSON-safe context with explicit provenance and safety flags."""

        return {
            "source": self.source,
            "as_of": self.as_of.isoformat(),
            "quality_status": self.quality_status,
            "reasons": list(self.reasons),
            "queries": list(self.queries),
            "read_only": self.read_only,
            "external_writes": self.external_writes,
            "preflight": dict(self.preflight),
            "data_diagnostics": dict(self.data_diagnostics),
            "candidates": [dict(candidate) for candidate in self.candidates],
            "research": self.research_context,
            "market": self.market_context,
            "accounting": self.accounting_context,
        }


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    return value.astimezone(timezone.utc)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _candidate_payloads(value: Any) -> list[Mapping[str, Any]]:
    value = _decode_json(value)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if not isinstance(value, Mapping):
        return []
    for key in ("candidates", "portfolios", "items"):
        nested = value.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, Mapping)]
    if "portfolio_id" in value or "risk_band" in value:
        return [value]
    return []


def _allocations(value: Any) -> dict[str, Any] | None:
    value = _decode_json(value)
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, list):
        return None
    result: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, Mapping):
            return None
        key = item.get("instrument_id") or item.get("symbol") or item.get("asset")
        weight = item.get("target_weight") or item.get("weight")
        if not key or weight is None:
            return None
        result[str(key)] = weight
    return result or None


def _load_portfolio_candidate_model() -> Any:
    path = Path(__file__).with_name("suitability.py")
    spec = importlib.util.spec_from_file_location("supabase_portfolio_suitability", path)
    if spec is None or spec.loader is None:
        raise SupabaseReadOnlyError("portfolio suitability contract unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.PortfolioCandidate


def _normalize_candidates(
    rows: Sequence[Mapping[str, Any]], *, as_of: datetime
) -> tuple[tuple[dict[str, Any], ...], list[str]]:
    model = _load_portfolio_candidate_model()
    candidates: list[dict[str, Any]] = []
    reasons: list[str] = []
    for row in rows:
        strategy_code = str(row.get("strategy_code") or "unknown-strategy")
        version_id = str(row.get("strategy_version_id") or "unknown-version")
        version_as_of = row.get("effective_from") or as_of
        schema_payloads = _candidate_payloads(row.get("target_portfolio_schema"))
        config = _decode_json(row.get("config"))
        if isinstance(config, Mapping):
            schema_payloads.extend(_candidate_payloads(config.get("portfolio_candidates")))
            schema_payloads.extend(_candidate_payloads(config.get("portfolio")))
        if not schema_payloads:
            reasons.append(f"CATALOG_METADATA_MISSING:{strategy_code}")
            continue
        for payload in schema_payloads:
            candidate = dict(_json_value(payload))
            candidate["portfolio_id"] = str(
                candidate.get("portfolio_id")
                or candidate.get("id")
                or f"{strategy_code}:v{row.get('version', 'unknown')}"
            )
            candidate["name"] = str(candidate.get("name") or row.get("name") or strategy_code)
            if "target_allocations" not in candidate:
                candidate["target_allocations"] = _allocations(candidate.get("target_portfolio"))
            asset_classes = set((candidate.get("target_allocations") or {}).keys())
            if asset_classes != {"KOREA_EQUITY"}:
                reasons.append(
                    f"NON_DOMESTIC_PORTFOLIO:{strategy_code}:{','.join(sorted(asset_classes))}"
                )
                continue
            candidate.setdefault("as_of", _json_value(version_as_of))
            candidate.setdefault(
                "evidence_refs", [f"supabase:strategy.versions:{version_id}"]
            )
            try:
                validated = model.model_validate(candidate)
            except Exception as exc:  # noqa: BLE001 - invalid DB rows fail closed.
                reasons.append(
                    f"CATALOG_ROW_INVALID:{strategy_code}:{type(exc).__name__}"
                )
                continue
            candidates.append(validated.model_dump(mode="json"))
    deduplicated = {
        candidate["portfolio_id"]: candidate for candidate in candidates
    }
    return tuple(deduplicated[key] for key in sorted(deduplicated)), reasons


def _documents_context(rows: Sequence[Mapping[str, Any]], as_of: datetime) -> dict[str, Any]:
    documents = [_json_value(dict(row)) for row in rows]
    return {
        "status": "LIVE" if documents else "EMPTY",
        "source": "supabase.research.documents",
        "as_of": as_of.isoformat(),
        "documents": documents,
        "evidence_refs": [
            f"supabase:research.documents:{row.get('document_id')}"
            for row in rows
            if row.get("document_id")
        ],
        "read_only": True,
    }


def _market_context(rows: Sequence[Mapping[str, Any]], as_of: datetime) -> dict[str, Any]:
    snapshots = [_json_value(dict(row)) for row in rows]
    return {
        "status": "LIVE" if snapshots else "EMPTY",
        "source": "supabase.execution.market_snapshots",
        "as_of": as_of.isoformat(),
        "snapshots": snapshots,
        "read_only": True,
    }


def _instrument_context(rows: Sequence[Mapping[str, Any]], as_of: datetime) -> dict[str, Any]:
    """Normalize the PIT domestic-equity universe for the BFF projection."""

    instruments: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        asset_class = str(row.get("asset_class") or "").upper()
        instrument_type = str(row.get("instrument_type") or "").upper()
        if not symbol or (
            asset_class not in {"KOREA_EQUITY", "EQUITY", "STOCK"}
            and instrument_type not in {"EQUITY", "STOCK"}
        ):
            continue
        instruments.append(
            {
                "instrument_id": str(row.get("instrument_id") or ""),
                "symbol": symbol,
                "exchange": str(row.get("exchange") or row.get("market") or "KRX"),
                "name": str(row.get("name") or symbol),
                "asset_class": "KOREA_EQUITY",
                "currency": str(row.get("currency") or "KRW"),
                "data_status": str(row.get("quality_status") or "LIVE"),
                "market_as_of": row.get("market_as_of") or row.get("as_of"),
                "last_price": row.get("last_price"),
                "mid": row.get("mid"),
                "evidence_refs": [
                    ref
                    for ref in (
                        f"supabase:reference.instruments:{row.get('instrument_id')}",
                        f"supabase:execution.market_snapshots:{row.get('market_snapshot_id')}",
                    )
                    if not ref.endswith(":None") and not ref.endswith(":")
                ],
            }
        )
    return {
        "status": "LIVE" if instruments else "EMPTY",
        "source": "supabase.reference.instruments",
        "as_of": as_of.isoformat(),
        "instruments": instruments,
        "read_only": True,
    }


def _accounting_context(
    rows: Sequence[Mapping[str, Any]], as_of: datetime, fund_id: str | None
) -> dict[str, Any]:
    return {
        "status": "LIVE" if rows else ("NOT_REQUESTED" if not fund_id else "EMPTY"),
        "source": "supabase.accounting.portfolio_snapshots",
        "as_of": as_of.isoformat(),
        "snapshot": _json_value(dict(rows[0])) if rows else None,
        "read_only": True,
    }


class _Psycopg2Fetcher:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    async def __call__(
        self, query: str, args: tuple[Any, ...]
    ) -> Sequence[Mapping[str, Any]]:
        return await asyncio.to_thread(self._fetch_sync, query, args)

    def _fetch_sync(self, query: str, args: tuple[Any, ...]) -> list[Mapping[str, Any]]:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
        except ModuleNotFoundError as exc:
            raise SupabaseReadOnlyError("psycopg2-binary is required for DB reads") from exc
        connection = psycopg2.connect(self.dsn, connect_timeout=8)
        try:
            connection.set_session(readonly=True, autocommit=False)
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(query, args)
                return [dict(row) for row in cursor.fetchall()]
        finally:
            connection.rollback()
            connection.close()


class _AsyncpgFetcher:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    async def __call__(
        self, query: str, args: tuple[Any, ...]
    ) -> Sequence[Mapping[str, Any]]:
        try:
            import asyncpg
        except ModuleNotFoundError as exc:
            raise SupabaseReadOnlyError("asyncpg is required for the async DB driver") from exc
        # statement_cache_size=0 - Supabase 풀러의 transaction mode(6543) 에서는
        # 커넥션이 트랜잭션마다 다른 서버 세션에 붙는다. asyncpg 는 기본으로
        # prepared statement 를 캐시하는데, 그 캐시는 앞선 세션에만 존재하므로
        # 두 번째 호출부터 `prepared statement "__asyncpg_stmt_x__" does not exist`
        # 로 죽는다. psycopg2 는 prepared statement 를 안 써서 영향이 없다.
        connection = await asyncpg.connect(self.dsn, timeout=8, statement_cache_size=0)
        try:
            async with connection.transaction(readonly=True):
                asyncpg_query = re.sub(
                    r"%s",
                    lambda match: f"${len(re.findall(r'%s', query[:match.start()])) + 1}",
                    query,
                )
                rows = await connection.fetch(asyncpg_query, *args)
                return [dict(row) for row in rows]
        finally:
            await connection.close()


class SupabaseReadOnlyAdapter:
    """Read canonical Supabase data without exposing any write operation."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        fetcher: RowFetcher | None = None,
        driver: str | None = None,
    ) -> None:
        self.dsn = dsn or next(
            (os.getenv(name) for name in DEFAULT_DSN_ENVIRONMENTS if os.getenv(name)),
            None,
        )
        self._fetcher = fetcher
        self.driver = (driver or os.getenv(READ_ONLY_DRIVER_ENV, "auto")).lower()
        try:
            retries = int(os.getenv("SUPABASE_CONNECT_RETRIES", str(DEFAULT_CONNECT_RETRIES)))
        except ValueError:
            retries = DEFAULT_CONNECT_RETRIES
        try:
            backoff = float(
                os.getenv(
                    "SUPABASE_CONNECT_BACKOFF_SECONDS",
                    str(DEFAULT_CONNECT_BACKOFF_SECONDS),
                )
            )
        except ValueError:
            backoff = DEFAULT_CONNECT_BACKOFF_SECONDS
        self.connect_retries = min(max(retries, 0), 3)
        self.connect_backoff_seconds = min(max(backoff, 0.0), 5.0)

    def _driver_name(self) -> str:
        if self._fetcher is not None:
            return "injected"
        if self.driver in {"psycopg2", "asyncpg"}:
            return self.driver
        try:
            import asyncpg  # noqa: F401
        except ModuleNotFoundError:
            return "psycopg2"
        return "asyncpg"

    def _dsn_endpoint(self) -> tuple[str, int]:
        if not self.dsn:
            raise SupabaseReadOnlyError("DSN_NOT_CONFIGURED")
        parsed = urlsplit(self.dsn)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise SupabaseReadOnlyError("INVALID_POSTGRES_DSN")
        try:
            port = parsed.port or 5432
        except ValueError as exc:
            raise SupabaseReadOnlyError("INVALID_POSTGRES_PORT") from exc
        return parsed.hostname, port

    async def diagnose_connection(self) -> SupabasePreflight:
        """Check DNS, read-only connectivity, and canonical table visibility.

        The result intentionally contains no host, user, password, or driver error
        text. It is safe to return in CLI output and operational logs.
        """
        if self._fetcher is not None:
            return SupabasePreflight(
                "BYPASSED",
                "injected",
                False,
                "BYPASSED",
                "BYPASSED",
                "BYPASSED",
                reasons=("INJECTED_FETCHER",),
            )
        if not self.dsn:
            return SupabasePreflight(
                "FAIL",
                self.driver,
                False,
                "NOT_CHECKED",
                "NOT_CHECKED",
                "NOT_CHECKED",
                reasons=("DSN_NOT_CONFIGURED",),
            )

        try:
            host, port = self._dsn_endpoint()
        except SupabaseReadOnlyError as exc:
            return SupabasePreflight(
                "FAIL",
                self.driver,
                True,
                "INVALID",
                "NOT_CHECKED",
                "NOT_CHECKED",
                reasons=(str(exc),),
            )

        try:
            await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        except Exception as exc:  # noqa: BLE001 - return a sanitized diagnostic code.
            return SupabasePreflight(
                "FAIL",
                self._driver_name(),
                True,
                "FAIL",
                "NOT_CHECKED",
                "NOT_CHECKED",
                reasons=(f"DNS_RESOLUTION_FAILED:{type(exc).__name__}",),
            )

        try:
            fetcher = self._get_fetcher()
            connection_rows = await self._fetch(fetcher, CONNECTION_PROBE_SQL, ())
            transaction_read_only = str(
                dict(connection_rows[0]).get("transaction_read_only", "")
                if connection_rows
                else ""
            ).lower()
            if transaction_read_only not in {"on", "true", "1"}:
                return SupabasePreflight(
                    "FAIL",
                    self._driver_name(),
                    True,
                    "PASS",
                    "FAIL",
                    "NOT_CHECKED",
                    reasons=("READ_ONLY_TRANSACTION_NOT_CONFIRMED",),
                )
        except Exception as exc:  # noqa: BLE001 - do not expose DSN-bearing text.
            return SupabasePreflight(
                "FAIL",
                self._driver_name(),
                True,
                "PASS",
                "FAIL",
                "NOT_CHECKED",
                reasons=(f"DATABASE_CONNECTION_FAILED:{type(exc).__name__}",),
            )

        try:
            rows = await self._fetch(fetcher, SCHEMA_PROBE_SQL, ())
            row = dict(rows[0]) if rows else {}
            expected = (
                "strategy_versions",
                "research_documents",
                "market_snapshots",
                "accounting_snapshots",
            )
            objects = tuple(name for name in expected if row.get(name))
            missing = tuple(name for name in expected if not row.get(name))
            if missing:
                return SupabasePreflight(
                    "FAIL",
                    self._driver_name(),
                    True,
                    "PASS",
                    "PASS",
                    "FAIL",
                    objects,
                    tuple(f"SCHEMA_OBJECT_MISSING:{name}" for name in missing),
                )
        except Exception as exc:  # noqa: BLE001 - do not expose DSN-bearing text.
            return SupabasePreflight(
                "FAIL",
                self._driver_name(),
                True,
                "PASS",
                "PASS",
                "FAIL",
                reasons=(f"SCHEMA_PROBE_FAILED:{type(exc).__name__}",),
            )

        return SupabasePreflight(
            "PASS",
            self._driver_name(),
            True,
            "PASS",
            "PASS",
            "PASS",
            objects,
        )

    def _get_fetcher(self) -> RowFetcher:
        if self._fetcher is not None:
            return self._fetcher
        if not self.dsn:
            raise SupabaseReadOnlyError(
                "SUPABASE_DATABASE_URL or DATABASE_URL is required for read-only access"
            )
        if self._driver_name() == "psycopg2":
            return _Psycopg2Fetcher(self.dsn)
        if self._driver_name() == "asyncpg":
            return _AsyncpgFetcher(self.dsn)
        raise SupabaseReadOnlyError("READ_ONLY_DRIVER_UNAVAILABLE")

    async def _fetch(
        self, fetcher: RowFetcher, query: str, args: tuple[Any, ...]
    ) -> Sequence[Mapping[str, Any]]:
        rows = await fetcher(query, args)
        return rows

    async def load_snapshot(
        self,
        *,
        as_of: datetime | str,
        fund_id: str | None = None,
    ) -> SupabaseReadSnapshot:
        """Load PIT data; any query failure produces a safe degraded snapshot."""

        cutoff = _as_utc(as_of)
        preflight = SupabasePreflight(
            "BYPASSED",
            "injected",
            False,
            "BYPASSED",
            "BYPASSED",
            "BYPASSED",
            reasons=("INJECTED_FETCHER",),
        )
        if self._fetcher is None:
            for attempt in range(self.connect_retries + 1):
                preflight = await self.diagnose_connection()
                if preflight.status == "PASS":
                    break
                retryable = any(
                    reason.startswith(("DNS_RESOLUTION_FAILED", "DATABASE_CONNECTION_FAILED"))
                    for reason in preflight.reasons
                )
                if not retryable or attempt >= self.connect_retries:
                    return SupabaseReadSnapshot(
                        cutoff,
                        "SUPABASE_UNAVAILABLE",
                        "UNAVAILABLE",
                        (),
                        {"status": "UNAVAILABLE", "read_only": True},
                        {"status": "UNAVAILABLE", "read_only": True},
                        {"status": "NOT_REQUESTED" if not fund_id else "UNAVAILABLE", "read_only": True},
                        preflight.reasons,
                        ("connection_preflight",),
                        preflight=preflight.as_dict(),
                    )
                await asyncio.sleep(self.connect_backoff_seconds * (attempt + 1))
        try:
            fetcher = self._get_fetcher()
        except SupabaseReadOnlyError as exc:
            return SupabaseReadSnapshot(
                cutoff,
                "SUPABASE_UNAVAILABLE",
                "UNAVAILABLE",
                (),
                {"status": "UNAVAILABLE", "read_only": True},
                {"status": "UNAVAILABLE", "read_only": True},
                {"status": "NOT_REQUESTED" if not fund_id else "UNAVAILABLE", "read_only": True},
                (type(exc).__name__,),
                ("connection_preflight",),
                preflight=preflight.as_dict(),
            )

        jobs: list[tuple[str, str, tuple[Any, ...]]] = [
            ("portfolio_catalog", PORTFOLIO_CATALOG_SQL, (cutoff, cutoff)),
            ("research_documents", RESEARCH_DOCUMENTS_SQL, (cutoff, cutoff)),
            ("market_snapshots", MARKET_SNAPSHOTS_SQL, (cutoff,)),
            (
                "domestic_equity_universe",
                DOMESTIC_EQUITY_UNIVERSE_SQL,
                (cutoff, cutoff, cutoff),
            ),
        ]
        if fund_id:
            jobs.append(("accounting_snapshot", ACCOUNTING_SNAPSHOT_SQL, (fund_id, cutoff)))

        results = await asyncio.gather(
            *(self._fetch(fetcher, query, args) for _, query, args in jobs),
            return_exceptions=True,
        )
        by_name: dict[str, Sequence[Mapping[str, Any]]] = {}
        reasons: list[str] = []
        failed_queries: set[str] = set()
        for (name, _, _), result in zip(jobs, results, strict=True):
            if isinstance(result, Exception):
                failed_queries.add(name)
                reasons.append(f"QUERY_FAILED:{name}:{type(result).__name__}")
            else:
                by_name[name] = result

        data_diagnostics: dict[str, Any] = {}
        diagnostic_query_names: list[str] = []
        if self._fetcher is None:
            diagnostic_jobs = (
                (
                    "portfolio_data_diagnostic",
                    PORTFOLIO_DATA_DIAGNOSTIC_SQL,
                    (cutoff, cutoff, cutoff, cutoff),
                ),
                (
                    "market_data_diagnostic",
                    MARKET_DATA_DIAGNOSTIC_SQL,
                    (cutoff, cutoff),
                ),
            )
            diagnostic_results = await asyncio.gather(
                *(self._fetch(fetcher, query, args) for _, query, args in diagnostic_jobs),
                return_exceptions=True,
            )
            for (name, _, _), result in zip(diagnostic_jobs, diagnostic_results, strict=True):
                diagnostic_query_names.append(name)
                if isinstance(result, Exception):
                    reasons.append(f"DATA_DIAGNOSTICS_FAILED:{name}:{type(result).__name__}")
                    continue
                data_diagnostics[name] = dict(result[0]) if result else {}

        candidates, candidate_reasons = _normalize_candidates(
            by_name.get("portfolio_catalog", ()), as_of=cutoff
        )
        reasons.extend(candidate_reasons)
        research_context = _documents_context(by_name.get("research_documents", ()), cutoff)
        market_context = _market_context(by_name.get("market_snapshots", ()), cutoff)
        market_context["instrument_universe"] = _instrument_context(
            by_name.get("domestic_equity_universe", ()), cutoff
        )
        accounting_context = _accounting_context(
            by_name.get("accounting_snapshot", ()), cutoff, fund_id
        )

        quality = "PASS"
        if "portfolio_catalog" in failed_queries:
            quality = "FAIL"
        elif not candidates:
            quality = "WARN"
            reasons.append("NO_VALID_PORTFOLIO_CANDIDATES")
        if "domestic_equity_universe" in failed_queries:
            quality = "WARN" if quality != "FAIL" else quality
            reasons.append("DOMESTIC_EQUITY_UNIVERSE_QUERY_FAILED")
        elif not market_context["instrument_universe"]["instruments"]:
            quality = "WARN" if quality != "FAIL" else quality
            reasons.append("NO_PIT_DOMESTIC_EQUITY_INSTRUMENTS")
        elif (
            failed_queries
            or research_context["status"] == "EMPTY"
            or market_context["status"] == "EMPTY"
        ):
            quality = "WARN"
        else:
            quality = "PASS"
        if research_context["status"] == "EMPTY":
            reasons.append("NO_PIT_RESEARCH_DOCUMENTS")
        if market_context["status"] == "EMPTY":
            reasons.append("NO_PIT_MARKET_SNAPSHOTS")
        pit_diagnostics = {
            "ready": quality == "PASS",
            "quality_status": quality,
            "reasons": list(dict.fromkeys(reasons)),
            "candidate_count": len(candidates),
            "research_document_count": len(research_context.get("documents", [])),
            "market_snapshot_count": len(market_context.get("snapshots", [])),
            "domestic_instrument_count": len(market_context.get("instrument_universe", {}).get("instruments", [])),
            "as_of": cutoff.isoformat(),
        }
        data_diagnostics["pit_readiness"] = pit_diagnostics
        return SupabaseReadSnapshot(
            cutoff,
            "SUPABASE",
            quality,
            candidates,
            research_context,
            market_context,
            accounting_context,
            tuple(reasons),
            tuple(name for name, _, _ in jobs) + tuple(diagnostic_query_names),
            preflight=preflight.as_dict(),
            data_diagnostics=data_diagnostics,
        )


__all__ = [
    "ACCOUNTING_SNAPSHOT_SQL",
    "DOMESTIC_EQUITY_UNIVERSE_SQL",
    "MARKET_DATA_DIAGNOSTIC_SQL",
    "MARKET_SNAPSHOTS_SQL",
    "PORTFOLIO_CATALOG_SQL",
    "PORTFOLIO_DATA_DIAGNOSTIC_SQL",
    "RESEARCH_DOCUMENTS_SQL",
    "SupabasePreflight",
    "SupabaseReadOnlyAdapter",
    "SupabaseReadOnlyError",
    "SupabaseReadSnapshot",
]
