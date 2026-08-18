from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260818001100_stock_supported_transition_guard.sql"
)


class StockSupportedTransitionGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        cls.compact = " ".join(cls.sql.split())

    def test_migration_is_additive_transactional_and_repeatable(self) -> None:
        self.assertTrue(self.sql.lstrip().startswith("begin;"))
        self.assertTrue(self.sql.rstrip().endswith("commit;"))
        self.assertNotRegex(
            self.sql,
            re.compile(r"(?m)^\s*update\s+(?:quant|reference)\."),
        )
        self.assertIn("create or replace function", self.sql)
        self.assertIn("drop trigger if exists", self.sql)
        self.assertIn("if not exists (", self.sql)
        self.assertIn("constraint_row.conname =", self.sql)
        self.assertIn("validate constraint", self.sql)
        self.assertIn("do $stock_supported_guard_audit$", self.sql)

    def test_null_or_nonpass_intraday_qa_cannot_support(self) -> None:
        self.assertIn(
            "audit.intraday_forward_qa_hypothesis_authority"
            "(p_hypothesis_id uuid)",
            self.compact,
        )
        self.assertIn("report.decision = 'pass'", self.sql)
        self.assertIn("where verdict = 'fail'", self.sql)
        self.assertIn("verdict is null", self.sql)
        self.assertIn("verdict = 'inconclusive'", self.sql)
        self.assertIn("verdict = 'pass'", self.sql)
        self.assertIn("current_stock_scope_valid", self.sql)
        self.assertIn(
            "v_authoritative_status is distinct from 'supported'", self.sql
        )
        self.assertIn("coalesce(v_authoritative_status, 'null')", self.sql)
        self.assertIn("all-qa-pass", self.sql)

    def test_qa_apply_revalidates_exact_current_forward_stock_scope(self) -> None:
        self.assertIn(
            "audit.intraday_forward_rung_has_current_stock_scope", self.sql
        )
        for identity_check in (
            "upper(instrument.instrument_type), '') <> 'stock'",
            "upper(instrument.asset_class), '') <> 'equity'",
            "upper(instrument.market), '') <> 'krx'",
            "upper(instrument.status), '') <> 'active'",
            "instrument.metadata->>'is_spac'",
            "instrument.listed_from > planned_session.session_date",
            "instrument.listed_to < planned_session.session_date",
        ):
            self.assertIn(identity_check, self.sql)
        self.assertIn(
            "forward_rung.experiment_id = report.experiment_id", self.sql
        )
        self.assertIn("forward_rung.rung = 'forward'", self.sql)
        self.assertIn(
            "candidate.report_revision_id = report.report_revision_id",
            self.sql,
        )
        self.assertIn(
            "candidate.outcome_revision_id = report.outcome_revision_id",
            self.sql,
        )

    def test_raw_insert_and_update_supported_paths_are_guarded(self) -> None:
        self.assertRegex(
            self.sql,
            r"create trigger intraday_forward_qa_support_guard\s+"
            r"before update of status, expected_edge, preregistered_at, "
            r"material_fingerprint\s+on quant\.hypotheses",
        )
        self.assertRegex(
            self.sql,
            r"create trigger stock_supported_insert_guard\s+"
            r"before insert on quant\.hypotheses",
        )
        self.assertIn("when (new.status = 'supported')", self.sql)
        self.assertIn("new.expected_edge->>'research_lane'", self.sql)
        self.assertIn("new.expected_edge ? 'intraday_signal_expr'", self.sql)
        self.assertIn(
            "new.expected_edge is distinct from old.expected_edge", self.sql
        )
        self.assertIn(
            "new.preregistered_at is distinct from old.preregistered_at",
            self.sql,
        )
        self.assertIn(
            "new.material_fingerprint is distinct from old.material_fingerprint",
            self.sql,
        )
        self.assertGreaterEqual(
            self.sql.count(
                "execute function audit.guard_intraday_forward_qa_support()"
            ),
            2,
        )

    def test_daily_support_requires_frozen_governed_stock_evidence(self) -> None:
        self.assertIn(
            "audit.experiment_has_governed_daily_stock_evidence", self.sql
        )
        self.assertIn(
            "audit.hypothesis_has_governed_daily_stock_evidence", self.sql
        )
        for required in (
            "experiment.status = 'completed'",
            "dataset.content_hash ~ '^[0-9a-f]{64}$'",
            "'walk-forward-rolling-6m'",
            "'daily-walk-forward-plan-v1'",
            "'daily_walk_forward'",
            "'evaluation_plan_fingerprint'",
            "'session_boundary_fingerprint'",
            "'evaluation_identity_complete'",
            "'krx_active_stock_only'",
            "'krx-active-stock-only-v1'",
            "split_policy->'cost_model'->>'version'",
            "evidence_metric.split = 'walk_forward'",
            "evidence_metric.metric = 'total_return'",
            "claimed_daily_metric.dimensions->>'evaluation_scope'",
            "count(distinct window_spec->>'window')",
            "pg_input_is_valid(window_spec->>'test_start', 'date')",
            "instrument.listed_from > expected_bounds.test_start",
            "instrument.listed_to < expected_bounds.test_end",
        ):
            self.assertIn(required, self.sql)
        self.assertIn(
            "daily hypothesis support requires complete governed", self.sql
        )

    def test_reusable_evidence_is_bound_to_one_experiment_not_a_sibling(self) -> None:
        self.assertIn(
            "audit.experiment_has_governed_daily_stock_evidence( "
            "p_experiment_id uuid)",
            self.compact,
        )
        self.assertIn(
            "where experiment.experiment_id = p_experiment_id", self.compact
        )
        self.assertRegex(
            self.sql,
            r"create or replace function "
            r"audit\.hypothesis_has_governed_daily_stock_evidence\([\s\S]*?"
            r"where experiment\.hypothesis_id = p_hypothesis_id[\s\S]*?"
            r"audit\.experiment_has_governed_daily_stock_evidence\(\s*"
            r"experiment\.experiment_id\)",
        )

    def test_quant_selector_gets_only_experiment_boolean_execute(self) -> None:
        self.assertIn(
            "quant.experiment_has_governed_daily_stock_evidence( "
            "p_experiment_id uuid)",
            self.compact,
        )
        self.assertRegex(
            self.sql,
            r"grant execute on function\s+"
            r"quant\.experiment_has_governed_daily_stock_evidence\(uuid\)\s+"
            r"to svc_quant;",
        )
        self.assertIn(
            "'quant.experiment_has_governed_daily_stock_evidence(uuid)'",
            self.sql,
        )
        self.assertIn(
            "'audit.experiment_has_governed_daily_stock_evidence(uuid)'",
            self.sql,
        )
        self.assertIn(
            "'audit.hypothesis_has_governed_daily_stock_evidence(uuid)'",
            self.sql,
        )
        self.assertIn(
            "svc_quant stock evidence function privileges are not least-privilege",
            self.sql,
        )
        self.assertIn("revoke usage on schema audit from svc_quant", self.sql)
        self.assertIn(
            "has_schema_privilege(\n           'svc_quant', 'audit', 'usage')",
            self.sql,
        )

    def test_daily_identity_hashes_are_recomputed_from_authoritative_rows(self) -> None:
        self.assertIn(
            "audit.python_ascii_json_string(p_value text)", self.compact
        )
        self.assertIn("audit.canonical_jsonb_text(p_value jsonb)", self.compact)
        self.assertIn("audit.canonical_jsonb_sha256(p_value jsonb)", self.compact)
        self.assertIn("v_codepoint - 65536", self.sql)
        self.assertIn("55296 + v_non_bmp / 1024", self.sql)
        self.assertIn("56320 + v_non_bmp % 1024", self.sql)
        self.assertIn("sha256(convert_to(", self.sql)
        self.assertIn(
            "experiment.split_policy -\n                   "
            "'evaluation_plan_fingerprint'",
            self.sql,
        )
        self.assertIn("planned_window.ordinality", self.sql)
        self.assertIn(
            "jsonb_agg(to_jsonb(governed_member.instrument_id::text)",
            self.sql,
        )
        for authoritative_hash in (
            "frozen_plan_identity.evaluation_plan_fingerprint",
            "frozen_plan_identity.session_boundary_fingerprint",
            "governed_universe_identity.instrument_ids_fingerprint",
            "governed_evaluation_identity.evaluation_fingerprint",
        ):
            self.assertGreaterEqual(self.sql.count(authoritative_hash), 2)

    def test_null_or_nonfinite_daily_returns_cannot_be_evidence(self) -> None:
        self.assertGreaterEqual(
            self.sql.count("evidence_metric.value is not null"), 2
        )
        self.assertGreaterEqual(
            self.sql.count("('nan', 'infinity', '-infinity')"), 2
        )

    def test_daily_total_return_evidence_is_immutable(self) -> None:
        self.assertIn(
            "audit.guard_daily_total_return_immutability()", self.sql
        )
        self.assertRegex(
            self.sql,
            r"create trigger daily_total_return_immutability_guard\s+"
            r"before insert or update or delete on quant\.experiment_metrics",
        )
        self.assertIn("old.split = 'walk_forward'", self.sql)
        self.assertIn("new.split = 'walk_forward'", self.sql)
        self.assertIn(
            "old.dimensions->>'evaluation_scope' = 'daily_walk_forward'",
            self.sql,
        )
        self.assertIn(
            "new.dimensions->>'evaluation_scope' = 'daily_walk_forward'",
            self.sql,
        )
        self.assertIn("hypothesis.status = 'supported'", self.sql)

    def test_preexisting_supported_rows_fail_closed_without_rewrite(self) -> None:
        self.assertIn("do $stock_supported_preflight$", self.sql)
        self.assertIn(
            "pre-existing supported hypothesis lacks current governed stock evidence",
            self.sql,
        )
        self.assertNotRegex(
            self.sql,
            re.compile(r"(?m)^\s*update\s+quant\.hypotheses"),
        )

    def test_spac_flag_has_a_persistent_database_invariant(self) -> None:
        self.assertIn("do $spac_invariant_install$", self.sql)
        self.assertIn(
            "chk_reference_instruments_spac_identity", self.sql
        )
        self.assertIn("metadata->>'is_spac'", self.sql)
        self.assertIn(
            "coalesce(upper(instrument_type), '') = 'spac'", self.sql
        )
        self.assertIn("not valid", self.sql)
        self.assertIn(
            "validate constraint chk_reference_instruments_spac_identity",
            self.sql,
        )

    def test_security_definer_helpers_are_not_runtime_entrypoints(self) -> None:
        self.assertGreaterEqual(self.sql.count("security definer"), 6)
        self.assertGreaterEqual(self.sql.count("security invoker"), 2)
        self.assertGreaterEqual(
            self.sql.count("set search_path = pg_catalog"), 8
        )
        self.assertRegex(
            self.sql,
            r"revoke all on function[\s\S]*?from public, anon, "
            r"authenticated, service_role, svc_quant,[\s\S]*?"
            r"svc_qa_worker, svc_audit_api, svc_qa_reproducer;",
        )


if __name__ == "__main__":
    unittest.main()
