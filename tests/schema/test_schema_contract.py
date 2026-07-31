from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SUPABASE_MIGRATIONS = ROOT / "supabase" / "migrations"
TIMESCALE_MIGRATIONS = ROOT / "timescaledb" / "migrations"


def read_sql_files(directory: Path) -> list[tuple[Path, str]]:
    return [(path, path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.sql"))]


def created_tables(sql: str) -> set[tuple[str, str]]:
    return set(
        re.findall(
            r"(?im)^create\s+table\s+([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)",
            sql,
        )
    )


class SupabaseSchemaContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.files = read_sql_files(SUPABASE_MIGRATIONS)
        cls.sql = "\n".join(content for _, content in cls.files)
        cls.tables = created_tables(cls.sql)

    def test_migration_sequence_is_complete(self) -> None:
        self.assertEqual(
            [path.name for path, _ in self.files],
            [
                "20260729000100_foundation_reference.sql",
                "20260729000200_governance_workforce.sql",
                "20260729000300_research_quant_strategy.sql",
                "20260729000400_execution_risk_accounting.sql",
                "20260729000500_audit_api_security.sql",
                "20260730000600_workforce_improvement_candidates.sql",
                "20260731000700_workforce_access_lifecycle.sql",
            ],
        )
        for path, sql in self.files:
            with self.subTest(path=path.name):
                self.assertRegex(sql.lstrip().lower(), r"^begin;")
                self.assertRegex(sql.rstrip().lower(), r"commit;$")

    def test_domain_schemas_and_table_counts(self) -> None:
        expected_counts = {
            "accounting": 18,
            "audit": 19,
            "execution": 12,
            "governance": 20,
            "quant": 12,
            "reference": 9,
            "research": 14,
            "risk": 16,
            "strategy": 9,
            "workforce": 20,
        }
        actual_counts = {
            schema: sum(1 for table_schema, _ in self.tables if table_schema == schema)
            for schema in expected_counts
        }
        self.assertEqual(actual_counts, expected_counts)
        self.assertNotIn("public", {schema for schema, _ in self.tables})

    def test_critical_end_to_end_entities_exist(self) -> None:
        required = {
            ("reference", "instruments"),
            ("research", "documents"),
            ("research", "document_versions"),
            ("research", "evidence_chunks"),
            ("governance", "cases"),
            ("governance", "case_events"),
            ("strategy", "versions"),
            ("strategy", "signals"),
            ("execution", "intent_groups"),
            ("execution", "order_intents"),
            ("risk", "risk_requests"),
            ("risk", "risk_decisions"),
            ("execution", "orders"),
            ("execution", "fills"),
            ("accounting", "journals"),
            ("accounting", "journal_lines"),
            ("accounting", "positions"),
            ("accounting", "nav_runs"),
            ("audit", "traces"),
            ("audit", "agent_runs"),
            ("workforce", "agent_profile_versions"),
        }
        self.assertTrue(required.issubset(self.tables), required - self.tables)

    def test_market_raw_data_is_not_stored_in_supabase(self) -> None:
        forbidden = {
            ("public", "market_ticks"),
            ("research", "market_ticks"),
            ("quant", "market_ticks"),
            ("public", "market_quotes"),
            ("research", "market_quotes"),
            ("quant", "market_quotes"),
        }
        self.assertTrue(self.tables.isdisjoint(forbidden))
        self.assertNotRegex(self.sql.lower(), r"references\s+market\.")

    def test_database_enforces_critical_controls(self) -> None:
        controls = [
            "validate_order_state_transition",
            "protect_posted_journal_lines",
            "protect_posted_journal",
            "validate_journal_posting",
            "reject_append_only_change",
            "case_events_append_only",
            "order_events_append_only",
            "fills_append_only",
            "tool_calls_append_only",
        ]
        for control in controls:
            with self.subTest(control=control):
                self.assertIn(control, self.sql)

        self.assertRegex(
            self.sql,
            r"(?is)filled_quantity\s+numeric.*?check\s*\(filled_quantity\s*<=\s*requested_quantity\)",
        )
        self.assertIn("base_debit numeric", self.sql)
        self.assertIn("base_credit numeric", self.sql)
        self.assertIn("imbalance <> 0", self.sql)

    def test_security_boundary_and_api_surface_exist(self) -> None:
        self.assertIn("enable row level security", self.sql.lower())
        self.assertIn("revoke all on all tables in schema", self.sql.lower())
        self.assertIn("grant usage on schema api to authenticated", self.sql.lower())

        required_views = {
            "investment_cases",
            "open_orders",
            "positions",
            "risk_status",
            "strategy_registry",
            "agent_registry",
        }
        actual_views = set(
            re.findall(
                r"(?im)^create\s+or\s+replace\s+view\s+api\.([a-z_][a-z0-9_]*)",
                self.sql,
            )
        )
        self.assertEqual(actual_views, required_views)
        self.assertIn("api.match_evidence_chunks", self.sql)
        self.assertIn("api.get_case_timeline", self.sql)

    def test_point_in_time_and_versioning_contracts_exist(self) -> None:
        for token in (
            "published_at",
            "observed_at",
            "content_hash",
            "schema_version",
            "strategy_version_id",
            "trace_id",
            "idempotency_key",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.sql)


class TimescaleSchemaContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.files = read_sql_files(TIMESCALE_MIGRATIONS)
        cls.sql = "\n".join(content for _, content in cls.files)
        cls.tables = created_tables(cls.sql)

    def test_market_data_plane_entities_exist(self) -> None:
        required = {
            ("market", "market_ticks"),
            ("market", "market_quotes"),
            ("market", "market_bars"),
            ("market", "microstructure_features"),
            ("market", "market_breadth"),
            ("market", "derivative_snapshots"),
            ("market", "data_quality_windows"),
            ("market", "feed_gaps"),
            ("market", "ingestion_watermarks"),
            ("market", "archive_exports"),
            ("market", "retention_registry"),
        }
        self.assertEqual(self.tables, required)

    def test_hypertables_and_continuous_aggregate_exist(self) -> None:
        expected_hypertables = {
            "market.market_ticks",
            "market.market_quotes",
            "market.market_bars",
            "market.microstructure_features",
            "market.market_breadth",
            "market.derivative_snapshots",
            "market.data_quality_windows",
        }
        actual_hypertables = set(
            re.findall(r"create_hypertable\(\s*'([^']+)'", self.sql, re.IGNORECASE)
        )
        self.assertEqual(actual_hypertables, expected_hypertables)
        self.assertIn("with (timescaledb.continuous)", self.sql.lower())
        self.assertIn("add_continuous_aggregate_policy", self.sql)

    def test_archive_gate_precedes_retention_deletion(self) -> None:
        self.assertIn("exported boolean", self.sql)
        self.assertIn("verified boolean", self.sql)
        self.assertIn("manifest_signed boolean", self.sql)
        self.assertIn("deletion_enabled boolean not null default false", self.sql)
        self.assertNotIn("add_retention_policy", self.sql)

    def test_timescale_has_no_cross_database_foreign_keys(self) -> None:
        self.assertNotRegex(self.sql.lower(), r"references\s+(reference|governance|strategy|accounting)\.")
        self.assertIn("instrument_id uuid not null", self.sql)


if __name__ == "__main__":
    unittest.main()
