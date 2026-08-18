from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260818001200_quant_stock_identity_projection.sql"
)
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
VIEW = "quant.current_krx_stock_instrument_identity"


class QuantStockIdentityProjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        cls.runtime = "\n".join(
            (PIPELINE / name).read_text(encoding="utf-8").lower()
            for name in (
                "intraday_experiment_runner.py",
                "stock_universe.py",
            )
        )

    def test_projection_is_owner_evaluated_security_barrier(self) -> None:
        self.assertRegex(
            self.sql,
            rf"create\s+or\s+replace\s+view\s+{re.escape(VIEW)}\s+"
            r"with\s*\(\s*security_barrier\s*=\s*true,\s*"
            r"security_invoker\s*=\s*false\s*\)\s+as",
        )
        for column in (
            "instrument.instrument_id",
            "instrument.instrument_type",
            "instrument.asset_class",
            "instrument.market",
            "instrument.venue",
            "instrument.status",
            "instrument.listed_from",
            "instrument.listed_to",
            "false::boolean as is_spac",
        ):
            self.assertIn(column, self.sql)

    def test_projection_is_strictly_current_stock_only(self) -> None:
        for predicate in (
            "upper(instrument.instrument_type) = 'stock'",
            "upper(instrument.asset_class) = 'equity'",
            "upper(instrument.market) = 'krx'",
            "upper(instrument.status) = 'active'",
            "instrument.metadata->>'is_spac'",
            "not in ('1', 't', 'true', 'yes')",
        ):
            self.assertIn(predicate, self.sql)

    def test_svc_quant_gets_only_projection_select(self) -> None:
        compact = " ".join(self.sql.split())
        self.assertIn(
            f"revoke all on {VIEW} from public, svc_quant", compact
        )
        self.assertIn(f"grant select on {VIEW} to svc_quant", compact)
        self.assertNotRegex(
            compact,
            rf"grant\s+(?:insert|update|delete|truncate|all)\s+on\s+"
            rf"{re.escape(VIEW)}\s+to\s+svc_quant",
        )
        self.assertIn(
            "revoke select (metadata) on reference.instruments from "
            "svc_quant",
            compact,
        )
        self.assertRegex(
            self.sql,
            r"has_column_privilege\(\s*'svc_quant',\s*"
            r"'reference\.instruments',\s*'metadata',\s*'select'\)",
        )

    def test_runtime_uses_projection_without_raw_metadata_access(self) -> None:
        self.assertGreaterEqual(self.runtime.count(VIEW), 8)
        self.assertNotIn("reference.instruments", self.runtime)
        self.assertNotIn("metadata->>'is_spac'", self.runtime)

    def test_migration_is_transactional_and_self_auditing(self) -> None:
        self.assertTrue(self.sql.lstrip().startswith("begin;"))
        self.assertTrue(self.sql.rstrip().endswith("commit;"))
        self.assertIn("do $quant_stock_identity_projection_audit$", self.sql)
        self.assertIn("has_table_privilege", self.sql)


if __name__ == "__main__":
    unittest.main()
