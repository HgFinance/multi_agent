"""Fail-closed Risk/QA production readiness checks.

The preflight only probes configured dependencies and never submits orders,
posts ledger entries, changes a Risk state, or enables a production flag.
Secrets are represented by boolean presence only.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
DATABASE_ENV_NAMES: tuple[str, ...] = ("RISK_QA_DATABASE_URL", "DATABASE_URL")

REQUIRED_TABLES: tuple[tuple[str, str], ...] = (
    ("reference", "data_sources"),
    ("accounting", "funds"),
    ("accounting", "portfolio_snapshots"),
    ("execution", "market_snapshots"),
    ("governance", "mandates"),
    ("governance", "mandate_versions"),
    ("risk", "policies"),
    ("risk", "limits"),
    ("risk", "trading_states"),
    ("risk", "risk_decisions"),
    ("workforce", "agent_profiles"),
    ("workforce", "agent_profile_versions"),
    ("audit", "agent_runs"),
    ("audit", "tool_calls"),
    ("audit", "domain_events"),
    ("audit", "qa_decisions"),
)

RLS_TABLES: tuple[tuple[str, str], ...] = (
    ("accounting", "funds"),
    ("accounting", "portfolio_snapshots"),
    ("governance", "mandates"),
    ("risk", "policies"),
    ("risk", "risk_requests"),
    ("risk", "snapshots"),
    ("audit", "agent_runs"),
    ("audit", "tool_calls"),
)

REQUIRED_FLAGS: tuple[str, ...] = (
    "RISK_QA_RUNTIME",
    "RISK_QA_PRODUCTION_ENABLED",
    "QA_CHECK_CONTRACT_APPROVED",
    "QA_TRACE_PERSIST",
    "QA_INCIDENT_PERSIST",
    "RISK_REQUIRE_P1_ANALYTICS",
    "RISK_CONTEXT_SOURCE",
    "RISK_BROKER_ADAPTER",
)

SERVICE_AUTH_ENV_NAMES: tuple[str, ...] = (
    "RISK_SERVICE_AUTH_SECRET",
    "RISK_SERVICE_AUTH_ISSUER",
    "RISK_SERVICE_AUTH_AUDIENCE",
    "QA_SERVICE_AUTH_SECRET",
    "QA_SERVICE_AUTH_ISSUER",
    "QA_SERVICE_AUTH_AUDIENCE",
)


def _present(environ: Mapping[str, str], name: str) -> bool:
    return bool(environ.get(name, "").strip())


def _database_dsn(environ: Mapping[str, str]) -> tuple[str, str]:
    """Return the canonical app DSN without exposing its value in reports."""

    for name in DATABASE_ENV_NAMES:
        value = environ.get(name, "").strip()
        if value:
            return name, value
    return "", ""


def _check(
    name: str, status: str, *, reason: str | None = None, **details: Any
) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "status": status}
    if reason:
        result["reason"] = reason
    result.update(details)
    return result


def _check_configuration(environ: Mapping[str, str]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    database_env, _ = _database_dsn(environ)
    checks.append(
        _check(
            "DATABASE_URL",
            "PASS" if database_env else "FAIL",
            reason=None if database_env else "DATABASE_URL_MISSING",
            configured=bool(database_env),
            configured_env=database_env or None,
        )
    )
    required_values = SERVICE_AUTH_ENV_NAMES
    for name in required_values:
        checks.append(
            _check(
                name,
                "PASS" if _present(environ, name) else "FAIL",
                reason=None if _present(environ, name) else f"{name}_MISSING",
                configured=_present(environ, name),
            )
        )
    for name in ("RISK_SERVICE_AUTH_SECRET", "QA_SERVICE_AUTH_SECRET"):
        secret_length = len(environ.get(name, "").strip())
        checks.append(
            _check(
                f"{name}_FORMAT",
                "PASS" if secret_length >= 32 else "FAIL",
                reason=None if secret_length >= 32 else f"{name}_TOO_SHORT",
                configured=bool(secret_length),
            )
        )

    redis_configured = _present(environ, "REDIS_URL")
    checks.append(
        _check(
            "REDIS_URL",
            "PASS" if redis_configured else "FAIL",
            reason=None if redis_configured else "REDIS_URL_REQUIRED_FOR_TRADING_STATE",
            configured=redis_configured,
        )
    )
    event_redis_name = (
        "RISK_QA_EVENT_REDIS_URL"
        if _present(environ, "RISK_QA_EVENT_REDIS_URL")
        else "REDIS_URL"
    )
    event_redis_configured = _present(environ, event_redis_name)
    checks.append(
        _check(
            "RISK_QA_EVENT_REDIS_URL",
            "PASS" if event_redis_configured else "FAIL",
            reason=None
            if event_redis_configured
            else "RISK_QA_EVENT_REDIS_URL_REQUIRED",
            configured=event_redis_configured,
            configured_env=event_redis_name if event_redis_configured else None,
        )
    )
    packet_url_configured = _present(environ, "RISK_QA_RESEARCH_PACKET_URL")
    checks.append(
        _check(
            "RISK_QA_RESEARCH_PACKET_URL",
            "PASS" if packet_url_configured else "FAIL",
            reason=None
            if packet_url_configured
            else "RISK_QA_RESEARCH_PACKET_URL_REQUIRED",
            configured=packet_url_configured,
        )
    )

    expected = {
        "RISK_QA_RUNTIME": "production",
        "RISK_QA_PRODUCTION_ENABLED": "true",
        "QA_CHECK_CONTRACT_APPROVED": "true",
        "QA_TRACE_PERSIST": "true",
        "QA_INCIDENT_PERSIST": "true",
        "RISK_REQUIRE_P1_ANALYTICS": "true",
        "RISK_CONTEXT_SOURCE": "database",
    }
    for name in REQUIRED_FLAGS:
        actual = environ.get(name, "").strip().lower()
        expected_value = expected.get(name)
        if expected_value is None:
            checks.append(
                _check(
                    name,
                    "PASS" if _present(environ, name) else "FAIL",
                    reason=None if _present(environ, name) else f"{name}_MISSING",
                    configured=_present(environ, name),
                )
            )
            continue
        checks.append(
            _check(
                name,
                "PASS" if actual == expected_value else "FAIL",
                reason=None
                if actual == expected_value
                else f"EXPECTED_{expected_value.upper()}",
                configured=bool(actual),
            )
        )
    return checks


def _load_psycopg2() -> Any:
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover - depends on deployment image
        raise RuntimeError(
            "psycopg2-binary is required for production preflight"
        ) from exc
    return psycopg2


def _check_database(environ: Mapping[str, str], *, as_of: str) -> dict[str, Any]:
    database_env, dsn = _database_dsn(environ)
    if not dsn:
        return _check("postgres", "FAIL", reason="DATABASE_URL_MISSING")
    conn = None
    try:
        psycopg2 = _load_psycopg2()
        conn = psycopg2.connect(dsn, connect_timeout=6)
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT current_setting('transaction_read_only')")
            read_only = str(cur.fetchone()[0]).lower() in {"on", "true", "1"}
            if not read_only:
                return _check(
                    "postgres", "FAIL", reason="READ_ONLY_TRANSACTION_NOT_CONFIRMED"
                )

            missing: list[str] = []
            for schema, table in REQUIRED_TABLES:
                cur.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",))
                if cur.fetchone()[0] is None:
                    missing.append(f"{schema}.{table}")

            rls: dict[str, dict[str, Any]] = {}
            rls_failures: list[str] = []
            for schema, table in RLS_TABLES:
                cur.execute(
                    """
                    SELECT c.relrowsecurity, c.relforcerowsecurity,
                           (SELECT count(*) FROM pg_policies p
                            WHERE p.schemaname = %s AND p.tablename = %s)
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = %s AND c.relname = %s
                    """,
                    (schema, table, schema, table),
                )
                row = cur.fetchone()
                key = f"{schema}.{table}"
                if row is None:
                    rls_failures.append(key)
                    continue
                rls[key] = {
                    "enabled": bool(row[0]),
                    "forced": bool(row[1]),
                    "policy_count": int(row[2]),
                }
                if not row[0] or int(row[2]) == 0:
                    rls_failures.append(key)

            cur.execute(
                """
                SELECT
                    (SELECT count(*) FROM accounting.funds WHERE status = 'ACTIVE'),
                    (SELECT count(*) FROM risk.policies
                     WHERE status = 'ACTIVE' AND effective_from <= %s
                       AND (effective_to IS NULL OR effective_to > %s)),
                    (SELECT count(*) FROM governance.mandates m
                     JOIN governance.mandate_versions mv ON mv.mandate_id = m.mandate_id
                     WHERE m.status = 'ACTIVE' AND mv.version = m.current_version
                       AND mv.effective_from <= %s
                       AND (mv.effective_to IS NULL OR mv.effective_to > %s)),
                    (SELECT count(*) FROM strategy.versions v
                     JOIN strategy.strategies s ON s.strategy_id = v.strategy_id
                     WHERE v.effective_from <= %s
                       AND (v.effective_to IS NULL OR v.effective_to > %s)
                       AND v.deployment_state IN ('SHADOW', 'PAPER', 'LIVE_CANDIDATE', 'LIVE')
                       AND s.status IN ('SHADOW', 'PAPER', 'LIVE_CANDIDATE', 'LIVE')
                       AND v.target_portfolio_schema IS NOT NULL),
                    (SELECT count(*) FROM execution.market_snapshots
                     WHERE as_of <= %s AND quality_status IN ('PASS', 'WARN')),
                    (SELECT count(*) FROM workforce.agent_profiles ap
                     JOIN workforce.agent_profile_versions apv ON apv.agent_id = ap.agent_id
                     WHERE ap.employment_status = 'ACTIVE'
                       AND apv.status IN ('APPROVED', 'ACTIVE'))
                """,
                (as_of, as_of, as_of, as_of, as_of, as_of, as_of),
            )
            counts = cur.fetchone()
        conn.rollback()
        data_counts = {
            "active_funds": int(counts[0]),
            "active_policies_as_of": int(counts[1]),
            "active_mandates_as_of": int(counts[2]),
            "deployable_portfolio_versions_as_of": int(counts[3]),
            "usable_market_snapshots_as_of": int(counts[4]),
            "active_worker_profiles": int(counts[5]),
        }
        if missing:
            return _check(
                "postgres",
                "FAIL",
                reason="SCHEMA_OBJECTS_MISSING",
                read_only=read_only,
                missing_objects=missing,
                rls=rls,
            )
        if rls_failures:
            return _check(
                "postgres",
                "FAIL",
                reason="RLS_NOT_READY",
                read_only=read_only,
                rls_failures=rls_failures,
                rls=rls,
            )
        data_requirements = {
            "active_funds": 1,
            "active_policies_as_of": 1,
            "active_mandates_as_of": 1,
            "deployable_portfolio_versions_as_of": 1,
            "usable_market_snapshots_as_of": 1,
            "active_worker_profiles": 5,
        }
        data_failures = [
            name
            for name, minimum in data_requirements.items()
            if data_counts[name] < minimum
        ]
        if data_failures:
            return _check(
                "postgres",
                "FAIL",
                reason="PIT_DATA_NOT_READY",
                read_only=read_only,
                data_failures=data_failures,
                data_counts=data_counts,
                rls=rls,
            )
        return _check(
            "postgres",
            "PASS",
            read_only=read_only,
            rls=rls,
            data_counts=data_counts,
        )
    except Exception as exc:  # noqa: BLE001 - preflight returns sanitized class only
        if conn is not None:
            conn.rollback()
        return _check(
            "postgres",
            "FAIL",
            reason=f"DATABASE_PROBE_FAILED:{type(exc).__name__}",
            configured_env=database_env or None,
        )
    finally:
        if conn is not None:
            conn.close()


def _check_redis(environ: Mapping[str, str]) -> dict[str, Any]:
    redis_url = environ.get("REDIS_URL", "").strip()
    if not redis_url:
        return _check("redis", "FAIL", reason="REDIS_URL_MISSING")
    try:
        import redis

        client = redis.Redis.from_url(
            redis_url, socket_connect_timeout=5, socket_timeout=5
        )
        client.ping()
        client.close()
        return _check("redis", "PASS", probe="PING_ONLY", external_writes=False)
    except Exception as exc:  # noqa: BLE001 - preflight returns sanitized class only
        return _check(
            "redis", "FAIL", reason=f"REDIS_PROBE_FAILED:{type(exc).__name__}"
        )


def _check_ollama(environ: Mapping[str, str]) -> dict[str, Any]:
    """Verify that Ollama exposes the configured worker model (read-only)."""

    base_url = environ.get("OLLAMA_BASE_URL", "").strip().rstrip("/")
    model = environ.get("OLLAMA_CHAT_MODEL", "").strip()
    if not base_url and not model:
        return _check("ollama", "SKIPPED", reason="OLLAMA_NOT_CONFIGURED")
    if not base_url or not model:
        return _check(
            "ollama",
            "FAIL",
            reason="OLLAMA_CONFIGURATION_INCOMPLETE",
            configured=bool(base_url or model),
        )

    native_base_url = base_url.removesuffix("/v1")
    request = Request(
        f"{native_base_url}/api/tags",
        headers={"Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=4) as response:
            payload = json.load(response)
        models = payload.get("models", []) if isinstance(payload, dict) else []
        names = {
            str(item.get("name"))
            for item in models
            if isinstance(item, dict) and item.get("name")
        }
        if model not in names:
            return _check(
                "ollama",
                "FAIL",
                reason="OLLAMA_MODEL_MISSING",
                model=model,
                available_model_count=len(names),
            )
        return _check("ollama", "PASS", model=model, probe="GET /api/tags")
    except (OSError, URLError, ValueError) as exc:
        return _check(
            "ollama",
            "FAIL",
            reason=f"OLLAMA_PROBE_FAILED:{type(exc).__name__}",
            model=model,
        )


def run_preflight(
    environ: Mapping[str, str] | None = None,
    *,
    as_of: str,
) -> dict[str, Any]:
    """Run a sanitized, fail-closed readiness report without enabling Production."""

    env = os.environ if environ is None else environ
    checks = _check_configuration(env)
    checks.append(_check_database(env, as_of=as_of))
    checks.append(_check_redis(env))
    checks.append(_check_ollama(env))
    failed = [item["name"] for item in checks if item.get("status") != "PASS"]
    return {
        "status": "PASS" if not failed else "BLOCKED",
        "as_of": as_of,
        "checks": checks,
        "failed_checks": failed,
        "production_enabled": False,
        "external_writes": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Risk/QA Production readiness preflight"
    )
    parser.add_argument("--as-of", required=True, help="PIT cutoff in ISO-8601 format")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ModuleNotFoundError:
        pass
    args = _parser().parse_args(argv)
    report = run_preflight(as_of=args.as_of)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
