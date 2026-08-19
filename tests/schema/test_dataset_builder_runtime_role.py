from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260819000300_dataset_builder_runtime_role.sql"
)
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"


class DatasetBuilderRuntimeRoleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        cls.spec_builder = (PIPELINE / "spec_dataset_builder.py").read_text(
            encoding="utf-8"
        )
        cls.pit_builder = (PIPELINE / "pit_dataset.py").read_text(
            encoding="utf-8"
        )

    def test_role_is_non_login_non_inheriting_and_audited(self) -> None:
        self.assertIn("create role svc_dataset_builder", self.sql)
        self.assertIn("nologin nosuperuser nocreatedb nocreaterole noinherit", self.sql)
        self.assertIn("noreplication nobypassrls", self.sql)
        self.assertIn("do $dataset_builder_privilege_audit$", self.sql)
        self.assertIn("dataset builder crosses sealed identity boundary", self.sql)

    def test_only_dataset_publication_relations_are_mutable(self) -> None:
        normalized = " ".join(self.sql.split())
        for relation in (
            "quant.universe_versions",
            "quant.universe_members",
            "quant.dataset_manifests",
            "quant.dataset_partitions",
        ):
            self.assertIn(relation, normalized)
        self.assertIn("grant delete on quant.dataset_partitions", normalized)
        self.assertNotRegex(
            normalized,
            r"grant delete on (?:quant\.)?(?:dataset_manifests|universe_)",
        )
        self.assertIn("grant update (rules)", normalized)
        self.assertIn("grant update ( as_of, quality_summary", normalized)
        self.assertIn("grant update ( object_path, row_count", normalized)
        self.assertNotIn("grant service_role", normalized)
        self.assertNotIn("to svc_quant", normalized)

    def test_rls_and_governed_reference_reads_are_explicit(self) -> None:
        for policy in (
            "dataset_manifests_svc_dataset_builder_insert",
            "dataset_manifests_svc_dataset_builder_update",
            "dataset_partitions_svc_dataset_builder_delete",
            "universe_versions_svc_dataset_builder_insert",
            "universe_members_svc_dataset_builder_insert",
            "market_sessions_svc_dataset_builder_krx_select",
        ):
            self.assertIn(f"create policy {policy}", self.sql)
        self.assertIn("quant.current_krx_stock_instrument_identity", self.sql)
        self.assertIn("reference.market_sessions", self.sql)
        self.assertIn("reference.market_calendar_versions", self.sql)
        self.assertIn("revoke all on reference.instruments", normalized := " ".join(self.sql.split()))
        self.assertNotRegex(
            normalized,
            r"grant select(?:\s*\([^)]*\))? on reference\.instruments",
        )

    def test_both_builders_select_the_dedicated_role_per_connection(self) -> None:
        for source in (self.spec_builder, self.pit_builder):
            self.assertIn('runtime_role="svc_dataset_builder"', source)
        self.assertRegex(
            self.spec_builder,
            re.compile(r"connect_writer\([\s\S]*?runtime_role=\"svc_dataset_builder\""),
        )

    def test_migration_is_transactional(self) -> None:
        self.assertTrue(self.sql.lstrip().startswith("begin;"))
        self.assertTrue(self.sql.rstrip().endswith("commit;"))


if __name__ == "__main__":
    unittest.main()
