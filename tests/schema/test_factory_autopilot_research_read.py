from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260819000200_factory_autopilot_research_read.sql"
)
AUTOPILOT = (
    ROOT
    / "departments"
    / "01-research"
    / "factory"
    / "factory_autopilot.py"
)
READ_INPUTS = (
    "research.methodology_leads",
    "research.experiment_proposals",
    "research.proposal_review_outcomes",
)


class FactoryAutopilotResearchReadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        cls.runtime = AUTOPILOT.read_text(encoding="utf-8").lower()

    def test_runtime_inputs_are_read_only(self) -> None:
        for relation in READ_INPUTS:
            self.assertRegex(self.runtime, rf"(?:from|join)\s+{re.escape(relation)}\b")
            self.assertNotRegex(
                self.runtime,
                rf"(?:insert\s+into|update|delete\s+from)\s+{re.escape(relation)}\b",
            )

    def test_migration_grants_only_select_and_audits_it(self) -> None:
        normalized = " ".join(self.sql.split())
        for relation in READ_INPUTS:
            self.assertIn(relation, normalized)
        self.assertIn("grant select on", normalized)
        self.assertIn("to svc_quant", normalized)
        self.assertIn("revoke insert, update, delete, truncate on", normalized)
        self.assertIn("has_table_privilege", normalized)
        self.assertIn("svc_quant can mutate factory planning input", normalized)
        self.assertNotIn("grant service_role", normalized)

    def test_migration_is_transactional(self) -> None:
        self.assertTrue(self.sql.lstrip().startswith("begin;"))
        self.assertTrue(self.sql.rstrip().endswith("commit;"))


if __name__ == "__main__":
    unittest.main()
