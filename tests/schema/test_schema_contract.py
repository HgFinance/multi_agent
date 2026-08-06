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
            r"(?im)^create\s+table\s+(?:if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)",
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
        expected = [
                "20260729000100_foundation_reference.sql",
                "20260729000200_governance_workforce.sql",
                "20260729000300_research_quant_strategy.sql",
                "20260729000400_execution_risk_accounting.sql",
                "20260729000500_audit_api_security.sql",
                "20260730000600_workforce_improvement_candidates.sql",
                "20260731000700_workforce_access_lifecycle.sql",
                "20260731000701_news_ingest_latency.sql",
                "20260731000800_workforce_plan_quality_probation.sql",
                "20260731000801_news_recency_weight.sql",
                "20260731000900_public_dashboard_views.sql",
                "20260731001000_qa_decisions_reproducibility.sql",
                "20260731001100_dash_news_published_date.sql",
                "20260801001200_evidence_chunk_embedding_1024.sql",
                "20260801001300_research_pipeline_runs.sql",
                "20260802001400_research_packet_outcomes.sql",
                "20260802001500_research_collector_runs.sql",
                "20260802001600_risk_qa_runtime_registration.sql",
                "20260802001700_evidence_embedding_1024_match.sql",
                "20260802001800_risk_qa_p1_rls.sql",
                "20260802001900_research_daily_labels.sql",
                "20260802002000_research_symbol_restrictions.sql",
                "20260802002100_risk_qa_run_log_replay.sql",
                # 리서치 (재일, 2026-08-02~03)
                "20260802002200_research_as_known_at.sql",
                "20260803002300_research_claim_forecast.sql",
                "20260803002400_research_document_revisions.sql",
                "20260803002500_research_production_authorized.sql",
                "20260804000100_align_current_runtime_models.sql",
                # CEO Office / 인사팀 (영주, 2026-08-04)
                "20260804000200_governance_case_status.sql",
                "20260804000300_unify_department_code.sql",
                "20260804000400_risk_qa_runtime_activation.sql",
                "20260804000500_api_accounting_read_views.sql",
                "20260804001000_quant_hypothesis_inconclusive.sql",
                "20260804001100_order_events_broker_id_unique.sql",
                "20260804001200_risk_qa_production_read_paths.sql",
                # CEO Office (영주, 2026-08-05) - P0-2 GOV-02 Replay가 실 DB로 잡은 버그 수정
                "20260805000100_notifications_dedup_key_per_channel.sql",
                # 위 마이그레이션의 ADD CONSTRAINT가 재실행에 안전하지 않아 병합 후
                # Supabase 자동 적용이 "already exists"로 실패한 것을 고치는 후속 마이그레이션
                "20260805000200_notifications_dedup_key_constraint_idempotent.sql",
                # 000200 병합 후에도 재발 - 진짜 원인은 supabase_migrations.schema_migrations
                # 이력 누락이었다(수동 수복 완료). 방어적 재확인 + 사고 기록
                "20260805000300_notifications_dedup_key_history_repair_note.sql",
                # HR-03 P1-1: Eval HOLD 종료와 후보별 관찰 Scorecard
                "20260806000100_workforce_improvement_hold_and_scorecards.sql",
                # HR-04 P1-2: quality_snapshots에 누락됐던 recorded_by 추가
                "20260806000200_workforce_quality_snapshot_recorded_by.sql",
        ]
        self.assertEqual([path.name for path, _ in self.files], expected)
        for path, sql in self.files:
            with self.subTest(path=path.name):
                self.assertRegex(sql.lstrip().lower(), r"^begin;")
                self.assertRegex(sql.rstrip().lower(), r"commit;$")

    def test_migration_versions_are_unique(self) -> None:
        """버전 접두사(타임스탬프)가 겹치면 Supabase 가 적용을 꼬아 Preview 가
        깨진다 - 2026-07-31 실측: 같은 날 두 사람이 각각 000700/000800 을 잡아
        'access_requests already exists' 로 전 커밋이 실패했다. 파일 이름이
        달라도 접두사가 같으면 같은 버전이다."""
        versions = [path.name.split("_", 1)[0] for path, _ in self.files]
        dup = {v for v in versions if versions.count(v) > 1}
        self.assertFalse(
            dup,
            f"마이그레이션 버전 접두사 중복: {sorted(dup)} - 새 파일을 만들기 전에 "
            f"`ls supabase/migrations | tail` 로 마지막 번호를 확인할 것",
        )

    def test_order_event_constraint_migration_is_idempotent(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name == "20260804001100_order_events_broker_id_unique.sql"
        ).lower()
        self.assertIn(
            "drop constraint if exists order_events_broker_event_unique",
            migration,
        )

    def test_notifications_dedup_key_constraint_migration_is_idempotent(self) -> None:
        """2026-08-05 실측: ADD CONSTRAINT만 있고 DROP CONSTRAINT IF EXISTS가 없는
        마이그레이션을 개발 DB에 먼저 수동 적용한 뒤 그대로 커밋했더니, 병합 후
        Supabase 자동 마이그레이션 적용기가 재실행하면서 "already exists"로 실패했다
        (order_events_broker_id_unique.sql과 같은 종류의 실수 - 위 테스트와 같은
        이유로 재발 방지)."""
        migration = next(
            sql
            for path, sql in self.files
            if path.name == "20260805000200_notifications_dedup_key_constraint_idempotent.sql"
        ).lower()
        self.assertIn(
            "drop constraint if exists notifications_dedup_key_channel_unique",
            migration,
        )

    def test_improvement_hold_constraint_migration_is_idempotent(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name == "20260806000100_workforce_improvement_hold_and_scorecards.sql"
        ).lower()
        self.assertIn("drop constraint if exists improvement_candidates_status_check", migration)
        self.assertIn("drop trigger if exists improvement_candidate_scorecards_append_only", migration)
        self.assertIn("'hold'", migration)

    def test_domain_schemas_and_table_counts(self) -> None:
        expected_counts = {
            "accounting": 18,
            "audit": 21,
            "execution": 12,
            "governance": 20,
            "quant": 12,
            "reference": 9,
            # +2 (재일, 2026-08-03): claim_evidence(주장↔근거 인용 링크),
            # document_revisions(뉴스 정정 이력 - 저장본은 PIT 상 최초 관측
            # 문장을 유지하므로 정정 사실은 이 테이블이 유일한 흔적이다)
            "research": 23,
            "risk": 19,
            "strategy": 9,
            "workforce": 25,
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
            ("audit", "run_log_events"),
            ("risk", "run_log_events"),
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
            # 회계·포트폴리오 읽기 뷰 (20260804000500). `/ui/snapshot`의 회계 구간 원천이다.
            "portfolio_snapshot_latest",
            "position_holdings",
            "ledger_balances",
            "open_breaks",
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
