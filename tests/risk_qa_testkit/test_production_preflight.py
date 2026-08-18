from __future__ import annotations

import json

from departments.risk_qa_testkit.production_preflight import (
    _check_configuration,
    _check_ollama,
    _database_dsn,
    run_preflight,
)


def test_canonical_database_dsn_prefers_supabase_specific_environment() -> None:
    selected, dsn = _database_dsn(
        {
            "DATABASE_URL": "postgresql://legacy.invalid/app",
            "RISK_QA_DATABASE_URL": "postgresql://canonical.invalid/app",
        }
    )

    assert selected == "RISK_QA_DATABASE_URL"
    assert dsn == "postgresql://canonical.invalid/app"


def test_ollama_preflight_is_skipped_when_not_configured() -> None:
    result = _check_ollama({})
    assert result["status"] == "SKIPPED"
    assert result["reason"] == "OLLAMA_NOT_CONFIGURED"


def test_ollama_preflight_requires_endpoint_and_model() -> None:
    result = _check_ollama({"OLLAMA_BASE_URL": "http://ollama:11434/v1"})
    assert result["status"] == "FAIL"
    assert result["reason"] == "OLLAMA_CONFIGURATION_INCOMPLETE"


def test_production_configuration_requires_event_bus_and_packet_contract() -> None:
    checks = {item["name"]: item for item in _check_configuration({})}

    assert checks["RISK_QA_EVENT_REDIS_URL"]["status"] == "FAIL"
    assert (
        checks["RISK_QA_EVENT_REDIS_URL"]["reason"]
        == "RISK_QA_EVENT_REDIS_URL_REQUIRED"
    )
    assert checks["RISK_QA_RESEARCH_PACKET_URL"]["status"] == "FAIL"
    assert (
        checks["RISK_QA_RESEARCH_PACKET_URL"]["reason"]
        == "RISK_QA_RESEARCH_PACKET_URL_REQUIRED"
    )


def test_production_preflight_is_fail_closed_and_does_not_echo_secrets() -> None:
    secret = "not-a-real-secret-for-test-only"
    environment = {
        "DATABASE_URL": f"postgresql://user:{secret}@db.invalid:5432/app",
        "REDIS_URL": "redis://redis.invalid:6379/0",
        "RISK_QA_RUNTIME": "production",
        "RISK_QA_PRODUCTION_ENABLED": "true",
        "QA_CHECK_CONTRACT_APPROVED": "true",
        "QA_TRACE_PERSIST": "true",
        "QA_INCIDENT_PERSIST": "true",
        "QA_INGEST_MODE": "disabled",
        "QA_ENABLE_LEGACY_EVIDENCE_INGESTION": "false",
        "RISK_REQUIRE_P1_ANALYTICS": "true",
        "RISK_CONTEXT_SOURCE": "database",
        "RISK_BROKER_ADAPTER": "paper",
        "RISK_SERVICE_AUTH_SECRET": "x" * 32,
        "RISK_SERVICE_AUTH_ISSUER": "test-issuer",
        "RISK_SERVICE_AUTH_AUDIENCE": "test-audience",
        "QA_SERVICE_AUTH_SECRET": "y" * 32,
        "QA_SERVICE_AUTH_ISSUER": "test-issuer",
        "QA_SERVICE_AUTH_AUDIENCE": "test-audience",
    }

    report = run_preflight(environment, as_of="2026-08-04T00:00:00+00:00")
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["status"] == "BLOCKED"
    assert report["production_enabled"] is False
    assert report["external_writes"] is False
    assert secret not in serialized
    assert next(item for item in report["checks"] if item["name"] == "postgres")[
        "reason"
    ].startswith("DATABASE_PROBE_FAILED:")
    assert (
        next(item for item in report["checks"] if item["name"] == "redis")["status"]
        == "FAIL"
    )


def test_production_configuration_blocks_legacy_evidence_writes() -> None:
    checks = {
        item["name"]: item
        for item in _check_configuration(
            {
                "QA_INGEST_MODE": "legacy-manual",
                "QA_ENABLE_LEGACY_EVIDENCE_INGESTION": "true",
            }
        )
    }

    assert checks["QA_INGEST_MODE"]["status"] == "FAIL"
    assert checks["QA_INGEST_MODE"]["reason"] == "EXPECTED_DISABLED"
    assert checks["QA_ENABLE_LEGACY_EVIDENCE_INGESTION"]["status"] == "FAIL"
    assert (
        checks["QA_ENABLE_LEGACY_EVIDENCE_INGESTION"]["reason"]
        == "EXPECTED_FALSE"
    )
    assert "QA_POLICY_SOURCE_ID" not in checks
    assert "OPENAI_API_KEY" not in checks
    assert "qa_policy_corpus" not in checks
