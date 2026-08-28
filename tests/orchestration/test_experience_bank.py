"""Focused tests for the MemoHarness-lite D5 experience boundary."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

import orchestration.workflows.portfolio_recommendation as portfolio_pipeline
from orchestration.adapters.ceo_task_planner import build_task_plan
from orchestration.ceo_workflow_scope import build_root_body
from orchestration.experience_bank import (
    ExperienceBank,
    ExperienceLookup,
    ExperienceRecord,
    ExperienceWrite,
    bounded_planner_hint,
    build_discord_experience_record,
    build_experience_record,
)


class _Cursor:
    def __init__(self, rows=(), rowcount=1, scalar=0):
        self.rows = list(rows)
        self.rowcount = rowcount
        self.scalar = scalar
        self.executed = []

    def execute(self, query, args=()):
        self.executed.append((query, args))

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return (self.scalar,)

    def close(self):
        return None


class _Connection:
    def __init__(self, rows=(), rowcount=1, scalar=0):
        self.cursor_obj = _Cursor(rows, rowcount, scalar)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        return None


class ExperienceBankTest(unittest.TestCase):
    def test_off_mode_is_fail_open_without_database_access(self):
        connect = mock.Mock(side_effect=AssertionError("D5 off must not connect"))
        bank = ExperienceBank("postgresql://unused", mode="off", connect_factory=connect)
        lookup = bank.lookup(case_type="investment_analysis", binding=False)
        self.assertFalse(lookup.available)
        self.assertEqual(lookup.matched_count, 0)
        self.assertFalse(connect.called)

    def test_lookup_builds_bounded_advisory_hint(self):
        connection = _Connection(
            rows=[
                (
                    "investment_analysis",
                    False,
                    ["research", "risk"],
                    "analysis_parallel",
                    True,
                    [],
                    12800,
                    True,
                    False,
                    "research+risk analysis_parallel succeeded",
                ),
                (
                    "investment_analysis",
                    False,
                    ["research"],
                    "analysis_parallel",
                    False,
                    ["PROVIDER_QUOTA"],
                    9000,
                    True,
                    False,
                    "provider failure",
                ),
            ]
        )
        bank = ExperienceBank(
            "postgresql://test",
            mode="shadow",
            connect_factory=lambda *_args, **_kwargs: connection,
        )
        lookup = bank.lookup(case_type="investment_analysis", binding=False)
        self.assertTrue(lookup.available)
        self.assertEqual(lookup.matched_count, 2)
        self.assertEqual(lookup.planner_hint["successful_runs"], 1)
        self.assertEqual(lookup.planner_hint["success_rate"], 1.0)
        self.assertNotIn("provider failure", lookup.planner_hint.get("lessons", []))
        self.assertNotIn("failed_failure_codes", lookup.planner_hint)
        self.assertNotIn("failed_department_sets", lookup.planner_hint)
        self.assertEqual(lookup.failure_memory["matched_failures"], 1)
        self.assertEqual(
            lookup.failure_memory["failure_codes"],
            [{"code": "PROVIDER_QUOTA", "count": 1}],
        )
        self.assertEqual(
            lookup.failure_memory["failed_department_sets"],
            [{"departments": "research", "count": 1}],
        )

    def test_failed_experience_is_not_recalled_even_if_legacy_success_flag_is_wrong(self):
        connection = _Connection(
            rows=[
                (
                    "investment_analysis", False, ["research"],
                    "analysis_parallel", False, ["ROUTING_MISMATCH"],
                    9000, True, False, "failed route",
                ),
                (
                    "investment_analysis", False, ["risk"],
                    "analysis_parallel", True, ["ROUTING_MISMATCH"],
                    9000, True, False, "legacy false success",
                ),
            ]
        )
        bank = ExperienceBank(
            "postgresql://test",
            mode="active",
            connect_factory=lambda *_args, **_kwargs: connection,
        )
        lookup = bank.lookup(case_type="investment_analysis", binding=False)
        self.assertIsNone(lookup.planner_hint)

    def test_planner_hint_drops_failure_memory_and_skill_names(self):
        bounded = bounded_planner_hint(
            {
                "source": "memo_harness_d5",
                "matched_runs": 4,
                "successful_runs": 1,
                "success_rate": 0.25,
                "successful_policies": [{"policy": "analysis_parallel", "count": 1}],
                "failed_failure_codes": [{"code": "ROUTING_MISMATCH", "count": 3}],
                "failed_department_sets": [{"departments": "research+risk", "count": 3}],
                "lessons": ["failed route"],
                "skills": ["failed-skill"],
            }
        )
        self.assertIsNotNone(bounded)
        self.assertNotIn("failed_failure_codes", bounded)
        self.assertNotIn("failed_department_sets", bounded)
        self.assertNotIn("lessons", bounded)
        self.assertNotIn("skills", bounded)

    def test_record_is_structured_and_idempotent_key_is_used(self):
        connection = _Connection(rowcount=1)
        bank = ExperienceBank(
            "postgresql://test",
            mode="shadow",
            connect_factory=lambda *_args, **_kwargs: connection,
        )
        record = ExperienceRecord(
            case_type="investment_analysis",
            binding=False,
            primary_departments=("research", "risk"),
            orchestration_policy="analysis_parallel",
            success=True,
            failure_codes=(),
            latency_ms=12800,
            qa_enabled=True,
            qa_blocks_response=False,
            lesson="research+risk analysis_parallel succeeded",
            source_run_id="case-123",
        )
        result = bank.record(record)
        self.assertTrue(result.written)
        self.assertEqual(connection.commits, 1)
        query, args = connection.cursor_obj.executed[-1]
        self.assertIn("ON CONFLICT (experience_identity)", query)
        self.assertIn("experience_identity", query)
        self.assertNotIn("user prompt", str(args).lower())

    def test_record_fails_open_at_d5_write_stop_capacity(self):
        connection = _Connection(rowcount=1, scalar=48 * 1024 * 1024)
        bank = ExperienceBank(
            "postgresql://test",
            mode="shadow",
            connect_factory=lambda *_args, **_kwargs: connection,
        )
        result = bank.record(
            ExperienceRecord(
                case_type="investment_analysis",
                binding=False,
                primary_departments=("research",),
                orchestration_policy="analysis_parallel",
                success=True,
                failure_codes=(),
                latency_ms=10,
                qa_enabled=True,
                qa_blocks_response=False,
                lesson="structured outcome",
                source_run_id="capacity-test",
            )
        )
        self.assertFalse(result.written)
        self.assertEqual(result.error_code, "D5_CAPACITY_LIMIT")
        self.assertFalse(any("INSERT INTO" in query for query, _args in connection.cursor_obj.executed))

    def test_lookup_sets_bounded_statement_timeout_and_separate_timings(self):
        connection = _Connection(rows=[])
        bank = ExperienceBank(
            "postgresql://test",
            mode="shadow",
            statement_timeout_ms=1200,
            connect_factory=lambda *_args, **_kwargs: connection,
        )
        lookup = bank.lookup(
            case_type="investment_analysis",
            binding=False,
            correlation_id="request-123",
        )
        statements = [query for query, _args in connection.cursor_obj.executed]
        self.assertTrue(any("statement_timeout" in query for query in statements))
        self.assertGreaterEqual(lookup.lookup_ms, 0)
        self.assertGreaterEqual(lookup.hint_build_ms, 0)

    def test_lookup_timeout_is_fail_open(self):
        class TimeoutCursor(_Cursor):
            def execute(self, query, args=()):
                super().execute(query, args)
                if "SELECT case_type" in query:
                    raise TimeoutError("statement timeout")

        class TimeoutConnection(_Connection):
            def __init__(self):
                self.cursor_obj = TimeoutCursor()
                self.commits = 0
                self.rollbacks = 0

        connection = TimeoutConnection()
        bank = ExperienceBank(
            "postgresql://test",
            mode="active",
            connect_factory=lambda *_args, **_kwargs: connection,
        )
        lookup = bank.lookup(case_type="investment_analysis", binding=False)
        self.assertFalse(lookup.available)
        self.assertEqual(lookup.matched_count, 0)
        self.assertEqual(lookup.planner_hint, None)

    def test_discord_record_uses_non_null_kanban_identity(self):
        record = build_discord_experience_record(
            root_id="t_root1234",
            root_payload={"body": build_root_body("q", "req-1"), "status": "done"},
            task_payloads=[
                {
                    "id": "t_research",
                    "assignee": "research-department",
                    "status": "done",
                    "body": "workflow_role=primary\n",
                }
            ],
            terminal_status="done",
        )
        self.assertEqual(record.experience_identity, "kanban:t_root1234")
        self.assertEqual(record.primary_departments, ("research-department",))
        self.assertNotIn("q", record.lesson)

    def test_verified_discord_record_rejects_a_mismatched_route(self):
        root = {
            "id": "t_route_mismatch",
            "body": build_root_body(
                "오늘 매매손익 분석해줘",
                "req-route-mismatch",
                selected_primary_profiles=(
                    "research-department",
                    "risk-management",
                ),
                delegation_instructions={
                    "research-department": "research",
                    "risk-management": "risk",
                },
            ),
            "status": "done",
        }
        record = build_discord_experience_record(
            root_id="t_route_mismatch",
            root_payload=root,
            task_payloads=(
                root,
                {
                    "id": "t_research",
                    "assignee": "research-department",
                    "status": "done",
                    "body": "workflow_role=primary\n",
                },
                {
                    "id": "t_risk",
                    "assignee": "risk-management",
                    "status": "done",
                    "body": "workflow_role=primary\n",
                },
            ),
            terminal_status="done",
            qa_decision="PASS",
            qa_task_id="t_qa",
        )
        self.assertEqual(record.case_type, "discord_ceo_verified:account_status")
        self.assertFalse(record.success)
        self.assertIn("ROUTING_MISMATCH", record.failure_codes)
        self.assertIn("routing mismatch", record.lesson)

    def test_verified_discord_record_fails_closed_for_non_terminal_root(self):
        root = {
            "id": "t_root_running",
            "body": build_root_body(
                "오늘 매매손익 분석해줘",
                "req-root-running",
                selected_primary_profiles=("accounting-portfolio-department",),
                delegation_instructions={
                    "accounting-portfolio-department": "accounting",
                },
            ),
            "status": "running",
        }
        record = build_discord_experience_record(
            root_id="t_root_running",
            root_payload=root,
            task_payloads=(
                root,
                {
                    "id": "t_accounting",
                    "assignee": "accounting-portfolio-department",
                    "status": "done",
                    "body": "workflow_role=primary\n",
                },
            ),
            terminal_status="running",
            qa_decision="PASS",
            qa_task_id="t_qa_running",
        )
        self.assertFalse(record.success)
        self.assertIn("ROOT_RUNNING", record.failure_codes)

    def test_failure_record_does_not_turn_provider_failure_into_routing_hint(self):
        record = build_experience_record(
            {"category": "investment_analysis", "binding": False},
            {
                "pipeline_status": "DEGRADED",
                "task_plan": {"requested_departments": ["research", "risk", "qa", "ceo"]},
                "degraded_departments": ["research"],
                "case_id": "case-safe",
            },
            latency_ms=52_000,
        )
        self.assertFalse(record.success)
        self.assertEqual(record.orchestration_policy, "analysis_parallel")
        self.assertNotIn("case-safe", record.lesson)


class PlannerD5BoundaryTest(unittest.TestCase):
    def test_active_hint_is_passed_to_existing_planner_without_extra_llm_client(self):
        completed = mock.Mock(returncode=0, stdout='{"requested_departments":["research"],"rewritten_query":"q","rationale":"r"}')
        profile = {"query": "AAPL", "category": "investment_analysis"}
        with mock.patch.dict(os.environ, {"PORTFOLIO_CEO_TASK_PLANNER_MODE": "llm"}), \
             mock.patch("orchestration.adapters.ceo_task_planner.subprocess.run", return_value=completed) as run:
            plan = build_task_plan(
                profile,
                deterministic_fallback=lambda _: {"mode": "deterministic"},
                valid_departments=("research", "risk", "qa", "ceo"),
                experience_hint={
                    "source": "memo_harness_d5",
                    "matched_runs": 3,
                    "successful_runs": 3,
                    "successful_policies": [{"policy": "analysis_parallel", "count": 3}],
                },
            )
        self.assertEqual(plan["mode"], "llm_task_plan")
        prompt = run.call_args.args[0][-1]
        self.assertIn("experience_hint", prompt)
        self.assertEqual(run.call_count, 1)


class MemoHarnessLiteE2ETest(unittest.TestCase):
    def test_shadow_lookup_then_record_is_a_no_extra_llm_path(self):
        read_connection = _Connection(
            rows=[
                (
                    "investment_analysis", False, ["research", "risk"],
                    "analysis_parallel", True, [], 12000, True, False,
                    "research+risk analysis_parallel succeeded",
                )
            ]
        )
        write_connection = _Connection(rowcount=1)
        connections = iter([read_connection, write_connection])
        bank = ExperienceBank(
            "postgresql://test",
            mode="shadow",
            connect_factory=lambda *_args, **_kwargs: next(connections),
        )
        lookup = bank.lookup(case_type="investment_analysis", binding=False)
        write = bank.record(
            ExperienceRecord(
                case_type="investment_analysis",
                binding=False,
                primary_departments=("research", "risk"),
                orchestration_policy="analysis_parallel",
                success=True,
                failure_codes=(),
                latency_ms=12000,
                qa_enabled=True,
                qa_blocks_response=False,
                lesson="research+risk analysis_parallel succeeded",
                source_run_id="case-e2e",
            )
        )
        self.assertEqual(lookup.mode, "shadow")
        self.assertIsNotNone(lookup.planner_hint)
        self.assertTrue(write.written)
        self.assertEqual(lookup.mode, "shadow")

    def test_pipeline_wires_d5_lookup_into_existing_d4_and_logs_outcome(self):
        class FakeBank:
            mode = "active"

            def __init__(self):
                self.lookup_calls = []
                self.records = []

            def lookup(self, **kwargs):
                self.lookup_calls.append(kwargs)
                return ExperienceLookup(
                    mode="active",
                    available=True,
                    elapsed_ms=1,
                    matched_count=1,
                    planner_hint={
                        "source": "memo_harness_d5",
                        "matched_runs": 1,
                        "successful_runs": 1,
                        "successful_policies": [
                            {"policy": "analysis_parallel", "count": 1}
                        ],
                    },
                )

            def record(self, record):
                self.records.append(record)
                return ExperienceWrite("active", True, 1, True)

        fake_bank = FakeBank()
        profile = {
            "user_id": "d5-e2e-user",
            "mindset": "RISK_SEEKING",
            "experience": "INTERMEDIATE",
            "investment_horizon_years": 5,
            "max_drawdown_pct": "0.25",
            "liquidity_need": "MEDIUM",
            "as_of": "2026-08-04T00:00:00+00:00",
            "category": "investment_analysis",
        }
        candidate = {
            "portfolio_id": "d5-e2e-candidate",
            "name": "D5 candidate",
            "risk_band": "MEDIUM",
            "minimum_experience": "BEGINNER",
            "minimum_horizon_years": 3,
            "max_drawdown_pct": "0.15",
            "max_exit_days": 14,
            "target_allocations": {"GLOBAL_EQUITY": "0.60", "SHORT_TERM_BOND": "0.40"},
            "evidence_refs": ["research:portfolio-catalog:v1"],
            "as_of": "2026-08-04T00:00:00+00:00",
        }
        with mock.patch.object(
            portfolio_pipeline.ExperienceBank,
            "from_env",
            return_value=fake_bank,
        ), mock.patch.object(
            portfolio_pipeline,
            "build_task_plan",
            wraps=portfolio_pipeline.build_task_plan,
        ) as build_plan:
            result = asyncio.run(
                portfolio_pipeline.run_portfolio_recommendation_pipeline_async(
                    profile, [candidate]
                )
            )

        self.assertEqual(result["pipeline_status"], "COMPLETED")
        self.assertEqual(len(fake_bank.lookup_calls), 1)
        self.assertEqual(len(fake_bank.records), 1)
        self.assertEqual(build_plan.call_args.kwargs["experience_hint"]["source"], "memo_harness_d5")
        self.assertEqual(build_plan.call_count, 1)


if __name__ == "__main__":
    unittest.main()
