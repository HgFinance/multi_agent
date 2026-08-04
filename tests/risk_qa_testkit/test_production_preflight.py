from __future__ import annotations

import json
from pathlib import Path

from departments.risk_qa_testkit.production_preflight import _database_dsn, run_preflight


def test_canonical_database_dsn_prefers_supabase_specific_environment() -> None:
    selected, dsn = _database_dsn(
        {
            "DATABASE_URL": "postgresql://legacy.invalid/app",
            "RISK_QA_DATABASE_URL": "postgresql://canonical.invalid/app",
        }
    )

    assert selected == "RISK_QA_DATABASE_URL"
    assert dsn == "postgresql://canonical.invalid/app"


def test_production_preflight_is_fail_closed_and_does_not_echo_secrets(tmp_path: Path) -> None:
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "policy.md").write_text("# Real policy\nActual policy text.\n", encoding="utf-8")
    secret = "not-a-real-secret-for-test-only"
    environment = {
        "DATABASE_URL": f"postgresql://user:{secret}@db.invalid:5432/app",
        "QA_POLICY_SOURCE_ID": "00000000-0000-4000-8000-000000000001",
        "OPENAI_API_KEY": secret,
        "REDIS_URL": "redis://redis.invalid:6379/0",
        "QA_POLICY_CORPUS_DIR": str(policy_dir),
        "RISK_QA_RUNTIME": "production",
        "RISK_QA_PRODUCTION_ENABLED": "true",
        "QA_CHECK_CONTRACT_APPROVED": "true",
        "QA_TRACE_PERSIST": "true",
        "QA_INGEST_MODE": "production",
        "RISK_REQUIRE_P1_ANALYTICS": "true",
        "RISK_CONTEXT_SOURCE": "database",
        "RISK_BROKER_ADAPTER": "paper",
    }

    report = run_preflight(environment, as_of="2026-08-04T00:00:00+00:00")
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["status"] == "BLOCKED"
    assert report["production_enabled"] is False
    assert report["external_writes"] is False
    assert secret not in serialized
    assert next(item for item in report["checks"] if item["name"] == "qa_policy_corpus")["status"] == "PASS"
    assert next(item for item in report["checks"] if item["name"] == "postgres")["reason"].startswith("DATABASE_PROBE_FAILED:")
    assert next(item for item in report["checks"] if item["name"] == "redis")["status"] == "FAIL"


def test_placeholder_policy_corpus_blocks_production(tmp_path: Path) -> None:
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "placeholder.md").write_text("# Policy\nSAMPLE_PLACEHOLDER\n", encoding="utf-8")

    report = run_preflight(
        {"QA_POLICY_CORPUS_DIR": str(policy_dir)},
        as_of="2026-08-04T00:00:00+00:00",
    )

    policy_check = next(item for item in report["checks"] if item["name"] == "qa_policy_corpus")
    assert report["status"] == "BLOCKED"
    assert policy_check["reason"] == "PLACEHOLDER_POLICY_DOCUMENTS"
