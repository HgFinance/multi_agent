from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260818000800_quant_runtime_rls_repair.sql"
)


class QuantRuntimeRlsRepairTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        pipeline = ROOT / "departments" / "04-quant-backtest" / "pipeline"
        cls.runtime_sql = "\n".join(
            (pipeline / name).read_text(encoding="utf-8").lower()
            for name in (
                "experiment_worker.py",
                "experiment_orchestrator.py",
                "backtest_runner.py",
                "intraday_experiment_runner.py",
                "factory_bridge.py",
                "data_resolution.py",
                "stock_universe.py",
            )
        )

    def test_immutable_catalog_is_select_only(self) -> None:
        for table in (
            "universe_versions",
            "universe_members",
            "dataset_manifests",
            "dataset_partitions",
        ):
            self.assertIn(
                f"create policy {table}_svc_quant_select", self.sql
            )
            self.assertRegex(
                self.sql,
                rf"on\s+quant\.{table}\s+for\s+select\s+to\s+svc_quant"
                rf"\s+using\s*\(true\)",
            )

        self.assertRegex(
            self.sql,
            r"revoke\s+insert,\s*update,\s*delete,\s*truncate\s+on\s+"
            r"quant\.universe_versions,\s*quant\.universe_members,\s*"
            r"quant\.dataset_manifests,\s*quant\.dataset_partitions\s+"
            r"from\s+svc_quant",
        )
        self.assertNotIn("universe_versions_svc_quant_insert", self.sql)
        self.assertNotIn("universe_members_svc_quant_insert", self.sql)
        self.assertNotIn("dataset_manifests_svc_quant_insert", self.sql)
        self.assertNotIn("dataset_partitions_svc_quant_insert", self.sql)

    def test_exact_factory_write_surface_is_restored(self) -> None:
        expected_policies = {
            "hypotheses_svc_quant_insert": "insert",
            "experiments_svc_quant_insert": "insert",
            "experiments_svc_quant_update": "update",
            "backtest_runs_svc_quant_select": "select",
            "backtest_runs_svc_quant_insert": "insert",
            "backtest_trades_svc_quant_insert": "insert",
        }
        for policy, command in expected_policies.items():
            self.assertIn(f"create policy {policy}", self.sql)
            table = policy.removesuffix(f"_svc_quant_{command}")
            self.assertRegex(
                self.sql,
                rf"create\s+policy\s+{policy}\s+on\s+quant\.{table}\s+"
                rf"for\s+{command}\s+to\s+svc_quant",
            )

        # These pre-existing policies are also mandatory for the same runtime
        # chain and are fail-closed audited by the repair migration.
        for existing_policy in (
            "hypotheses_svc_quant_select",
            "hypotheses_svc_quant_update",
            "experiments_svc_quant_select",
            "experiment_metrics_svc_quant_select",
            "experiment_metrics_svc_quant_insert",
            "experiment_metrics_svc_quant_update",
        ):
            self.assertIn(f"'{existing_policy}'", self.sql)

        self.assertIn(
            "grant select, insert on research.experiment_outcomes to "
            "svc_quant",
            " ".join(self.sql.split()),
        )
        self.assertIn(
            "revoke update, delete, truncate on "
            "research.experiment_outcomes from svc_quant",
            " ".join(self.sql.split()),
        )
        self.assertNotIn("backtest_trades_svc_quant_select", self.sql)
        self.assertRegex(
            self.sql,
            r"revoke\s+select,\s*update,\s*delete,\s*truncate\s+on\s+"
            r"quant\.backtest_trades\s+from\s+svc_quant",
        )

    def test_lifecycle_updates_are_column_scoped(self) -> None:
        self.assertRegex(
            self.sql,
            r"revoke\s+update\s+on\s+quant\.hypotheses,\s*"
            r"quant\.experiments\s+from\s+svc_quant",
        )
        self.assertRegex(
            self.sql,
            r"grant\s+update\s*\(\s*status,\s*status_changed_at,\s*"
            r"expected_edge,\s*preregistered_at,\s*material_fingerprint\s*\)"
            r"\s+on\s+quant\.hypotheses\s+to\s+svc_quant",
        )
        self.assertRegex(
            self.sql,
            r"grant\s+update\s*\(\s*status,\s*started_at,\s*ended_at,\s*"
            r"trace_id,\s*trial_family_id,\s*trial_number\s*\)\s+on\s+"
            r"quant\.experiments\s+to\s+svc_quant",
        )
        self.assertNotRegex(
            self.sql,
            r"grant\s+(?:select,\s*insert,\s*)?update\s+on\s+"
            r"quant\.(?:hypotheses|experiments)",
        )
        for sealed_column in ("config", "dataset_id", "input_hash"):
            self.assertRegex(
                self.sql,
                rf"has_column_privilege\(\s*'svc_quant',\s*"
                rf"'quant\.experiments',\s*'{sealed_column}',\s*'update'\)",
            )
        self.assertIn(
            "svc_quant can rewrite sealed experimental identity", self.sql
        )

    def test_policy_surface_matches_static_runtime_sql(self) -> None:
        # The scoped worker only reads immutable catalog metadata.  Dataset
        # builders are separate factory-autopilot commands and must not acquire
        # write access merely because the experiment image contains their code.
        # These three relations appear in the execution SQL.  Universe
        # versions are still allowed as read-only FK catalog metadata, even
        # though the selected worker modules currently reach membership by ID.
        for table in (
            "universe_members",
            "dataset_manifests",
            "dataset_partitions",
        ):
            self.assertIn(f"quant.{table}", self.runtime_sql)

        for table in (
            "universe_versions",
            "universe_members",
            "dataset_manifests",
            "dataset_partitions",
        ):
            self.assertNotRegex(
                self.runtime_sql,
                rf"(?:insert\s+into|update|delete\s+from)\s+quant\.{table}\b",
            )

        for statement in (
            "insert into quant.hypotheses",
            "update quant.hypotheses",
            "insert into quant.experiments",
            "update quant.experiments",
            "insert into quant.backtest_runs",
            "insert into quant.backtest_trades",
            "insert into research.experiment_outcomes",
        ):
            self.assertIn(statement, self.runtime_sql)

        self.assertRegex(
            self.runtime_sql,
            r"(?:from|join)\s+quant\.backtest_runs\b",
        )
        self.assertNotRegex(
            self.runtime_sql,
            r"(?:from|join)\s+quant\.backtest_trades\b",
        )

    def test_stock_and_qa_boundaries_are_preserved(self) -> None:
        self.assertIn(
            "reference_instruments_svc_quant_stock_only_select", self.sql
        )
        self.assertIn(
            "the governed krx active stock reference boundary is missing",
            self.sql,
        )
        self.assertIn(
            "svc_quant exceeds the quant/qa separation boundary", self.sql
        )
        for forbidden in (
            "grant service_role",
            "to service_role",
            "database_runtime_role:-service_role",
            "intraday_forward_qa_outbox_svc_quant_insert",
            "intraday_forward_qa_delivery_state_svc_quant_update",
            "intraday_forward_qa_dispatches_svc_quant_insert",
        ):
            self.assertNotIn(forbidden, self.sql)

    def test_migration_audits_effective_privileges_and_is_transactional(self) -> None:
        self.assertTrue(self.sql.lstrip().startswith("begin;"))
        self.assertTrue(self.sql.rstrip().endswith("commit;"))
        self.assertIn("do $quant_runtime_rls_audit$", self.sql)
        self.assertIn("from pg_policies", self.sql)
        self.assertIn("has_table_privilege", self.sql)
        self.assertIn("has_column_privilege", self.sql)
        self.assertIn(
            "svc_quant can mutate immutable catalog table", self.sql
        )
        self.assertIn(
            "svc_quant retains destructive scientific-table access", self.sql
        )
        self.assertNotRegex(
            self.sql,
            re.compile(r"grant\s+(?:all|delete|truncate)\b"),
        )


if __name__ == "__main__":
    unittest.main()
