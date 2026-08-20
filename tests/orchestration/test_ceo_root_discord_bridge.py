from __future__ import annotations

import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
