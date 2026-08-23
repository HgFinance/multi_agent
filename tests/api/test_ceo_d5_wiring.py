"""Discord CEO ingress wiring for the MemoHarness-lite D5 boundary."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from apps.api import ceo
from orchestration.adapters.ceo_supervisor import CeoSupervisorService
from orchestration.ceo_workflow_scope import build_root_body
from orchestration.experience_bank import ExperienceLookup, ExperienceWrite


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
                ceo.CeoAsk(query="raw prompt must not be logged", request_id="req-shadow")
            )

        messages = [str(call.args[0]) for call in warning.call_args_list]
        lookup = next(message for message in messages if "event=memo_harness_d5_lookup" in message)
        hint = next(message for message in messages if "event=memo_harness_d5_hint" in message)
        self.assertIn("root_id=%s", lookup)
        self.assertIn("lookup_ms=%d", lookup)
        self.assertIn("error_category=%s", lookup)
        self.assertIn("root_id=%s", hint)
        self.assertIn("hint_build_ms=%d", hint)
        self.assertNotIn("raw prompt must not be logged", " ".join(messages))

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


class DiscordD5FinalizationTest(unittest.TestCase):
    def test_same_discord_root_records_once(self):
        root_id = "t_root1234"
        root_body = build_root_body("analyze Samsung", "req-1")

        class Client:
            environment = {}

            def workflow(self, task_id):
                return task_id, (
                    {
                        "id": "t_research",
                        "assignee": "research-department",
                        "status": "done",
                        "body": "workflow_role=primary\n",
                    },
                )

        bank = _FakeBank("shadow")
        service = CeoSupervisorService(Client(), experience_bank=bank)
        root = {"id": root_id, "body": root_body, "status": "done"}

        service._record_discord_experience_once(root_id=root_id, root_payload=root)
        service._record_discord_experience_once(root_id=root_id, root_payload=root)

        self.assertEqual(len(bank.records), 1)
        self.assertEqual(bank.records[0].experience_identity, "kanban:t_root1234")


if __name__ == "__main__":
    unittest.main()
