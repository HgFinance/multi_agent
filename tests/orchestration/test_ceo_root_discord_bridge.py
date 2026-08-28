from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from orchestration.adapters.ceo_supervisor import CeoSupervisorService


class FakeClient:
    def __init__(self, home: str) -> None:
        self.environment = {"HERMES_HOME": home}


class DeliverySpy:
    def __init__(self) -> None:
        self.cards = []
        self.details = []

    def upsert_thread_card(self, **kwargs):
        self.cards.append(kwargs)
        return "created"

    def deliver_to_existing_thread(self, **kwargs):
        self.details.append(kwargs)
        return "sent"


def root_body(extra: str = "") -> str:
    return (
        "hgfinance.ceo-workflow-scope.v1\n"
        "workflow_scope=fresh\n"
        "workflow_mode=analysis\n"
        "origin=user-query\n"
        "root_task_role=scope_and_planning\n"
        "planning_terminal_state=done_after_child_creation\n"
        "discord_message_id=111\n"
        "discord_thread_id=111\n"
        "discord_channel_id=222\n"
        "discord_guild_id=333\n"
        f"{extra}"
    )


class CeoRootDiscordBridgeTest(unittest.TestCase):
    def service(self, home: str, delivery: DeliverySpy):
        os.makedirs(
            os.path.join(home, "profiles", "ceo-agent"),
            exist_ok=True,
        )
        return CeoSupervisorService(
            FakeClient(home),
            discord_delivery=delivery,
        )

    def test_direct_answer_reuses_existing_thread_delivery_without_planning(self):
        with tempfile.TemporaryDirectory() as home:
            delivery = DeliverySpy()
            service = self.service(home, delivery)

            root = {
                "id": "root-direct",
                "assignee": "ceo-agent",
                "status": "done",
                "body": root_body(),
                "runs": [
                    {
                        "status": "done",
                        "metadata": {
                            "final_answer": "현재 조직은 Research, Quant, Trading 등으로 구성됩니다."
                        },
                    }
                ],
            }

            result = service._bridge_root_completion_to_discord(
                root_task_id="root-direct",
                root_payload=root,
            )

            self.assertEqual(result, "sent")
            self.assertEqual(delivery.cards, [])
            self.assertEqual(len(delivery.details), 1)
            self.assertEqual(delivery.details[0]["title"], "🧠 CEO 답변")
            self.assertIn(
                "현재 조직은 Research",
                delivery.details[0]["content"],
            )

    def test_delegation_reuses_existing_plan_and_thread_card_delivery(self):
        with tempfile.TemporaryDirectory() as home:
            delivery = DeliverySpy()
            service = self.service(home, delivery)

            root = {
                "id": "root-delegated",
                "assignee": "ceo-agent",
                "status": "done",
                "body": root_body(
                    "selected_primary_profiles="
                    "risk-management,accounting-portfolio-department\n"
                    "analysis_mode=fast_advisory\n"
                    "delegation_instruction.risk-management="
                    "하방 위험과 투자 제한을 검토하십시오.\n"
                    "delegation_instruction.accounting-portfolio-department="
                    "NAV와 편입 적합성을 검토하십시오.\n"
                ),
                "runs": [
                    {
                        "status": "done",
                        "metadata": {
                            "final_answer": "이 내용은 direct 답변으로 보내면 안 됩니다."
                        },
                    }
                ],
            }

            result = service._bridge_root_completion_to_discord(
                root_task_id="root-delegated",
                root_payload=root,
                materialized_primary_profiles=(
                    "risk-management",
                    "accounting-portfolio-department",
                ),
            )

            self.assertEqual(result, "created")
            self.assertEqual(delivery.details, [])
            self.assertEqual(len(delivery.cards), 1)

            content = delivery.cards[0]["content"]

            self.assertIn("CEO 업무 분배", content)
            self.assertIn("risk", content.casefold())
            self.assertIn("accounting", content.casefold())
            self.assertIn("하방 위험과 투자 제한", content)
            self.assertIn("NAV와 편입 적합성", content)
            self.assertNotIn(
                "이 내용은 direct 답변으로 보내면 안 됩니다.",
                content,
            )

    def test_direct_root_without_user_answer_sends_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            delivery = DeliverySpy()
            service = self.service(home, delivery)

            root = {
                "id": "root-empty",
                "assignee": "ceo-agent",
                "status": "done",
                "body": root_body(),
            }

            result = service._bridge_root_completion_to_discord(
                root_task_id="root-empty",
                root_payload=root,
            )

            self.assertEqual(result, "empty")
            self.assertEqual(delivery.cards, [])
            self.assertEqual(delivery.details, [])

    def test_root_trace_close_is_idempotent_within_supervisor_instance(self):
        with tempfile.TemporaryDirectory() as home:
            delivery = DeliverySpy()
            service = self.service(home, delivery)
            root = {
                "id": "root-trace",
                "status": "done",
                "body": root_body("langsmith_trace_context=trace-root\n"),
            }

            with patch(
                "orchestration.llm_observability.close_root_trace",
                return_value=True,
            ) as close:
                self.assertTrue(
                    service._close_root_trace(
                        root_id="root-trace",
                        root_payload=root,
                        status="completed",
                    )
                )
                self.assertTrue(
                    service._close_root_trace(
                        root_id="root-trace",
                        root_payload=root,
                        status="completed",
                    )
                )

            close.assert_called_once()

    def test_root_trace_close_can_attribute_single_primary_department(self):
        with tempfile.TemporaryDirectory() as home:
            service = self.service(home, DeliverySpy())
            root = {
                "id": "root-hr-trace",
                "status": "done",
                "body": root_body("langsmith_trace_context=trace-root\n"),
            }

            with patch(
                "orchestration.llm_observability.close_root_trace",
                return_value=True,
            ) as close:
                self.assertTrue(
                    service._close_root_trace(
                        root_id="root-hr-trace",
                        root_payload=root,
                        status="completed",
                        department="hr-department",
                        task_id="t_hr_primary",
                    )
                )

            kwargs = close.call_args.kwargs
            self.assertEqual(kwargs["department"], "hr-department")
            self.assertEqual(kwargs["task_id"], "t_hr_primary")

    def test_root_trace_close_records_full_workflow_latency(self):
        with tempfile.TemporaryDirectory() as home:
            service = self.service(home, DeliverySpy())
            root = {
                "id": "root-e2e-latency",
                "status": "done",
                "created_at": 1_000,
                "body": root_body("langsmith_trace_context=trace-root\n"),
            }
            terminal = {
                "id": "t-synthesis",
                "status": "done",
                "completed_at": 1_012,
                "final_answer": "완료된 답변",
            }

            with patch(
                "orchestration.llm_observability.close_root_trace",
                return_value=True,
            ) as close:
                self.assertTrue(
                    service._close_root_trace(
                        root_id="root-e2e-latency",
                        root_payload=root,
                        terminal_payload=terminal,
                        status="completed",
                    )
                )

            metadata = close.call_args.kwargs["terminal_metadata"]
            self.assertEqual(metadata["latency_ms"], 12_000)
            self.assertEqual(metadata["latency_scope"], "end_to_end")
            self.assertEqual(
                metadata["observation_point"], "ceo-response-delivered"
            )

    def test_failed_root_trace_close_is_queued_for_same_run_retry(self):
        with tempfile.TemporaryDirectory() as home:
            service = self.service(home, DeliverySpy())
            service.client.environment.update(
                {
                    "HGFINANCE_LANGSMITH_PUBLISH_ENABLED": "true",
                    "LANGSMITH_API_KEY": "key-not-printed",
                }
            )
            root = {
                "id": "root-retry",
                "status": "done",
                "created_at": 1_000,
                "body": root_body("langsmith_trace_context=trace-root\n"),
            }
            terminal = {
                "id": "t-synthesis-retry",
                "status": "done",
                "completed_at": 1_012,
                "final_answer": "완료된 답변",
            }

            with patch(
                "orchestration.llm_observability.close_root_trace",
                return_value=False,
            ):
                self.assertFalse(
                    service._close_root_trace(
                        root_id="root-retry",
                        root_payload=root,
                        terminal_payload=terminal,
                        task_id=terminal["id"],
                        status="completed",
                    )
                )

            pending = service._pending_langsmith_root_closures["root-retry"]
            self.assertEqual(pending["task_id"], terminal["id"])
            self.assertEqual(pending["attempts"], 1)

    def test_pending_root_trace_retry_reuses_terminal_task_without_replaying_delivery(self):
        with tempfile.TemporaryDirectory() as home:
            service = self.service(home, DeliverySpy())
            root = {
                "id": "root-retry-lane",
                "status": "done",
                "created_at": 1_000,
                "body": root_body("langsmith_trace_context=trace-root\n"),
            }
            terminal = {
                "id": "t-synthesis-retry-lane",
                "status": "done",
                "completed_at": 1_012,
                "final_answer": "완료된 답변",
            }
            service.client.environment.update(
                {
                    "HGFINANCE_LANGSMITH_PUBLISH_ENABLED": "true",
                    "LANGSMITH_API_KEY": "key-not-printed",
                }
            )
            service._pending_langsmith_root_closures[root["id"]] = {
                "task_id": terminal["id"],
                "attempts": 1,
                "next_attempt_at": 0,
            }
            service.client.show = Mock(side_effect=[root, terminal])

            with patch.object(service, "_close_root_trace", return_value=True) as close:
                self.assertEqual(service._retry_pending_langsmith_root_closures(), 1)

            close.assert_called_once_with(
                root_id=root["id"],
                root_payload=root,
                terminal_payload=terminal,
                task_id=terminal["id"],
                status="completed",
                error_class=None,
            )
            self.assertNotIn(root["id"], service._pending_langsmith_root_closures)


class CeoRootFastPathTest(unittest.TestCase):
    class FastClient(FakeClient):
        def __init__(self, home: str, root: dict):
            super().__init__(home)
            self.root = root
            self.workflow_calls = 0
            self.show_calls = []
            self.created = []

        def workflow(self, task_id: str):
            self.workflow_calls += 1
            raise AssertionError("workflow() must not run on completed CEO root fast-path")

        def show(self, task_id: str):
            self.show_calls.append(task_id)
            return dict(self.root)

        def create_task(self, **kwargs):
            self.created.append(kwargs)
            return {"id": f"created-{len(self.created)}"}

    def _service(self, home: str, root: dict, delivery: DeliverySpy):
        os.makedirs(
            os.path.join(home, "profiles", "ceo-agent"),
            exist_ok=True,
        )
        client = self.FastClient(home, root)
        service = CeoSupervisorService(
            client,
            discord_delivery=delivery,
        )
        return client, service

    def test_completed_direct_ceo_root_uses_zero_workflow_calls(self):
        with tempfile.TemporaryDirectory() as home:
            delivery = DeliverySpy()

            root = {
                "id": "root-direct-fast",
                "assignee": "ceo-agent",
                "status": "done",
                "body": root_body(),
                "runs": [
                    {
                        "status": "done",
                        "metadata": {
                            "final_answer": "현재 조직은 Research, Quant, Trading 등으로 구성됩니다."
                        },
                    }
                ],
            }

            client, service = self._service(home, root, delivery)

            service.handle_terminal_event(
                {
                    "task_id": "root-direct-fast",
                    "kind": "completed",
                    "assignee": "ceo-agent",
                    "event_id": "direct-fast-event",
                }
            )

            self.assertEqual(client.workflow_calls, 0)
            self.assertEqual(client.show_calls, ["root-direct-fast"])
            self.assertEqual(len(delivery.details), 1)
            self.assertIn("현재 조직은 Research", delivery.details[0]["content"])

    def test_completed_delegated_ceo_root_materializes_without_workflow(self):
        with tempfile.TemporaryDirectory() as home:
            delivery = DeliverySpy()

            root = {
                "id": "root-delegated-fast",
                "assignee": "ceo-agent",
                "status": "done",
                "body": root_body(
                    "selected_primary_profiles="
                    "risk-management,accounting-portfolio-department\n"
                    "analysis_mode=fast_advisory\n"
                    "delegation_instruction.risk-management="
                    "하방 위험을 검토하십시오.\n"
                    "delegation_instruction.accounting-portfolio-department="
                    "NAV와 편입 적합성을 검토하십시오.\n"
                ),
            }

            client, service = self._service(home, root, delivery)

            service.handle_terminal_event(
                {
                    "task_id": "root-delegated-fast",
                    "kind": "completed",
                    "assignee": "ceo-agent",
                    "event_id": "delegated-fast-event",
                }
            )

            self.assertEqual(client.workflow_calls, 0)
            self.assertEqual(client.show_calls, ["root-delegated-fast"])
            self.assertEqual(
                [item["assignee"] for item in client.created],
                ["risk-management", "accounting-portfolio-department"],
            )
            self.assertEqual(len(delivery.cards), 1)

    def test_department_terminal_does_not_pay_root_fast_show(self):
        with tempfile.TemporaryDirectory() as home:
            delivery = DeliverySpy()

            root = {
                "id": "risk-task",
                "assignee": "risk-management",
                "status": "done",
                "body": "workflow_root_task_id=root\\nworkflow_role=primary",
            }

            client, service = self._service(home, root, delivery)

            # Normal department handling is allowed to enter workflow().
            # We only prove the CEO-root optimization did not pre-call show().
            def workflow(task_id: str):
                self.assertEqual(client.show_calls, [])
                raise RuntimeError("stop-after-fast-path-check")

            client.workflow = workflow

            with self.assertRaises(RuntimeError):
                service.handle_terminal_event(
                    {
                        "task_id": "risk-task",
                        "kind": "completed",
                        "assignee": "risk-management",
                        "event_id": "risk-terminal-event",
                    }
                )

            self.assertEqual(client.show_calls, [])



if __name__ == "__main__":
    unittest.main()
