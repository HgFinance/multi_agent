"""Discord CEO ingress wiring for the MemoHarness-lite D5 boundary."""

from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from typing import ClassVar
from unittest.mock import patch

from apps.api import ceo
from orchestration.adapters.ceo_supervisor import CeoSupervisorService
from orchestration.ceo_workflow_scope import (
    build_root_body,
    ceo_self_improvement_section_from_root,
    user_query_from_body,
)
from orchestration.experience_bank import ExperienceLookup, ExperienceWrite
from orchestration.langsmith_feedback import FeedbackLedger


class _FakeBank:
    def __init__(self, mode: str):
        self.mode = mode
        self.enabled = mode in {"shadow", "active"}
        self.lookup_calls: list[dict[str, object]] = []
        self.records = []

    def lookup(self, **kwargs):
        self.lookup_calls.append(kwargs)
        return ExperienceLookup(
            mode=self.mode,
            available=True,
            elapsed_ms=2,
            matched_count=2,
            planner_hint={
                "source": "memo_harness_d5",
                "matched_runs": 2,
                "successful_runs": 2,
                "successful_policies": [
                    {"policy": "analysis_parallel", "count": 2}
                ],
            },
            lookup_ms=1,
            hint_build_ms=1,
        )

    def record(self, record):
        self.records.append(record)
        return ExperienceWrite(self.mode, True, 1, True)


class DiscordD5IngressTest(unittest.TestCase):
    def test_shadow_emits_payload_free_timing_events(self):
        bank = _FakeBank("shadow")
        task = {"task_id": "t_shadow_observed", "status": "ready"}
        with patch.dict(
            "os.environ",
            {
                "CEO_PLANNING_WAIT_SECONDS": "0",
                "LANGSMITH_TRACING": "false",
                "LANGSMITH_API_KEY": "",
            },
            clear=False,
        ), patch.object(
            ceo.ExperienceBank, "from_env", return_value=bank
        ), patch.object(
            ceo.hermes_boundary, "create_kanban_task", return_value=task
        ), patch.object(
            ceo.hermes_boundary, "comment_root_scope", return_value=True
        ), patch.object(
            ceo.hermes_boundary, "show_kanban_task", return_value=None
        ), patch.object(ceo.logger, "warning") as warning:
            ceo.ceo_query(
                ceo.CeoAsk(query="삼성전자 시장 분석해줘", request_id="req-shadow")
            )

        messages = [str(call.args[0]) for call in warning.call_args_list]
        lookup = next(message for message in messages if "event=memo_harness_d5_lookup" in message)
        hint = next(message for message in messages if "event=memo_harness_d5_hint" in message)
        self.assertIn("root_id=%s", lookup)
        self.assertIn("lookup_ms=%d", lookup)
        self.assertIn("error_category=%s", lookup)
        self.assertIn("root_id=%s", hint)
        self.assertIn("hint_build_ms=%d", hint)
        self.assertNotIn("삼성전자 시장 분석해줘", " ".join(messages))

    def test_off_shadow_active_have_one_existing_root_boundary(self):
        bodies = {}
        for mode, expected_lookup, expected_hint in (
            ("off", 0, False),
            ("shadow", 1, False),
            ("active", 1, True),
        ):
            with self.subTest(mode=mode):
                bank = _FakeBank(mode)
                task = {"task_id": f"t_{mode}", "status": "ready"}
                with patch.dict(
                    "os.environ", {"CEO_PLANNING_WAIT_SECONDS": "0"}, clear=False
                ), patch.object(
                    ceo.ExperienceBank, "from_env", return_value=bank
                ), patch.object(
                    ceo.hermes_boundary, "create_kanban_task", return_value=task
                ) as create, patch.object(
                    ceo.hermes_boundary, "comment_root_scope", return_value=True
                ), patch.object(
                    ceo.hermes_boundary, "show_kanban_task", return_value=None
                ):
                    response = ceo.ceo_query(
                        ceo.CeoAsk(
                            query="analyze Samsung",
                            request_id="request-d5-mode",
                        )
                    )

                self.assertEqual(response["task_id"], task["task_id"])
                self.assertEqual(len(bank.lookup_calls), expected_lookup)
                body = create.call_args.kwargs["body"]
                bodies[mode] = body
                if mode == "active":
                    self.assertEqual(
                        bank.lookup_calls[0]["case_type"],
                        "discord_ceo_verified:portfolio_recommendation",
                    )
                self.assertEqual("## D5 advisory" in body, expected_hint)
                self.assertNotIn("analyze Samsung", body.split("## User request\n", 1)[0])

        self.assertEqual(
            bodies["off"],
            bodies["shadow"],
            "shadow retrieval must not mutate the existing planning input",
        )

    def test_active_path_does_not_call_a_second_planner(self):
        bank = _FakeBank("active")
        task = {"task_id": "t_active", "status": "ready"}
        with patch.dict(
            "os.environ", {"CEO_PLANNING_WAIT_SECONDS": "0"}, clear=False
        ), patch.object(
            ceo.ExperienceBank, "from_env", return_value=bank
        ), patch.object(
            ceo.hermes_boundary, "create_kanban_task", return_value=task
        ) as create, patch.object(
            ceo.hermes_boundary, "comment_root_scope", return_value=True
        ), patch.object(
            ceo.hermes_boundary, "show_kanban_task", return_value=None
        ), patch("apps.api.ceo_hermes_client.ask_ceo") as ask:
            ceo.ceo_query(ceo.CeoAsk(query="analyze Samsung", request_id="req-active"))

        ask.assert_not_called()
        body = create.call_args.kwargs["body"]
        self.assertIn("non-authoritative", body)
        self.assertIn("analysis_parallel", body)

    def test_ceo_self_improvement_is_scoped_to_synthesis_guardrails(self):
        hint = {
            "schema_version": "hgfinance.memo-harness.ceo-self-improvement.v1",
            "owner": "ceo",
            "mode": "corrective_guardrails_only",
            "verified_qa_required": True,
            "raw_payloads_sent": False,
            "guardrails": [
                {
                    "id": "CEO_TRACE_EVIDENCE_RECHECK",
                    "rule": (
                        "Treat an unavailable authoritative execution trace as unverified. "
                        "A published receipt or metadata-only record is not proof that a "
                        "trace exists."
                    ),
                }
            ],
        }
        body = build_root_body(
            "삼성전자 분석",
            "req-ceo-self-improvement",
            ceo_self_improvement_hint=hint,
        )

        section = ceo_self_improvement_section_from_root(body)
        self.assertIn("CEO_TRACE_EVIDENCE_RECHECK", section)
        self.assertIn("CEO self-improvement guardrails", section)
        self.assertEqual(user_query_from_body(body), "삼성전자 분석")
        self.assertNotIn("execute QA command", section)
        self.assertNotIn("D5_CHECK_", section)


class DiscordD5FinalizationTest(unittest.TestCase):
    def test_same_discord_root_records_once(self):
        root_id = "t_root1234"
        root_body = build_root_body(
            "analyze Samsung",
            "req-1",
            selected_primary_profiles=("research-department", "risk-management"),
            delegation_instructions={
                "research-department": "research",
                "risk-management": "risk",
            },
        )

        class Client:
            environment: ClassVar[dict[str, object]] = {}

        bank = _FakeBank("shadow")
        service = CeoSupervisorService(Client(), experience_bank=bank)
        root = {"id": root_id, "body": root_body, "status": "done"}
        workflow_tasks = (
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
        )
        projection_result = {
            "status": "persisted",
            "canonical_decision": "PASS",
            "findings": [],
        }

        service._record_discord_experience_after_qa(
            root_id=root_id,
            root_payload=root,
            qa_task={"id": "t_qa"},
            workflow_tasks=workflow_tasks,
            projection_result=projection_result,
        )
        service._record_discord_experience_after_qa(
            root_id=root_id,
            root_payload=root,
            qa_task={"id": "t_qa"},
            workflow_tasks=workflow_tasks,
            projection_result=projection_result,
        )

        self.assertEqual(len(bank.records), 1)
        self.assertEqual(
            bank.records[0].experience_identity,
            "kanban:t_root1234:qa:t_qa",
        )
        self.assertTrue(bank.records[0].success)

    def test_verified_qa_finding_enters_d5_improvement_ledger(self):
        root_id = "t_root_d5_candidate"
        root_body = build_root_body(
            "매매손익 알려줘",
            "req-d5-candidate",
            selected_primary_profiles=("accounting-portfolio-department",),
            delegation_instructions={
                "accounting-portfolio-department": "accounting",
            },
        )

        class Client:
            environment: ClassVar[dict[str, object]] = {}

        bank = _FakeBank("active")
        with TemporaryDirectory() as directory:
            ledger = FeedbackLedger(f"{directory}/feedback.sqlite3")
            service = CeoSupervisorService(
                Client(), experience_bank=bank, d5_feedback_ledger=ledger
            )
            root = {"id": root_id, "body": root_body, "status": "done"}
            workflow_tasks = (
                root,
                {
                    "id": "t_accounting",
                    "assignee": "accounting-portfolio-department",
                    "status": "done",
                    "body": "workflow_role=primary\n",
                },
            )
            projection_result = {
                "status": "persisted",
                "canonical_decision": "WARN",
                "checks": {
                    "langsmith_authoritative_execution": {"result": "WARN"},
                },
                "findings": [{"id": "QA-F-001", "severity": "HIGH"}],
            }

            service._record_discord_experience_after_qa(
                root_id=root_id,
                root_payload=root,
                qa_task={"id": "t_qa_candidate"},
                workflow_tasks=workflow_tasks,
                projection_result=projection_result,
            )

            pending = ledger.pending(10)
            self.assertEqual(len(pending), 2)
            self.assertTrue(
                all(item["metadata"]["source"] == "memo_harness_d5" for item in pending)
            )


if __name__ == "__main__":
    unittest.main()
