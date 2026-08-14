"""CEO synthesis Discord delivery contract tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestration.discord_delivery import DiscordFinalDelivery, correlation_from_task
from orchestration.discord_idempotency import DiscordIdempotencyStore


class DiscordDeliveryTests(unittest.TestCase):
    def _store_with_inbound(self, directory: str) -> DiscordIdempotencyStore:
        store = DiscordIdempotencyStore(Path(directory))
        result = store.claim_inbound(
            dedup_key="discord:guild:channel:message",
            message_id="message",
            guild_id="guild",
            channel_id="channel",
            thread_id="thread",
            profile="ceo-agent",
            handler="live",
        )
        self.assertTrue(result.admitted)
        return store

    def test_root_correlation_is_read_from_nested_root_task(self) -> None:
        correlation = correlation_from_task(
            {
                "root_task": {
                    "body": (
                        "discord_request_id=discord:message\n"
                        "discord_message_id=message\n"
                        "discord_channel_id=channel\n"
                    )
                }
            }
        )
        self.assertEqual(correlation.message_id, "message")
        self.assertEqual(correlation.channel_id, "channel")

    def test_synthesis_completion_is_sent_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_inbound(directory)
            sent: list[dict[str, object]] = []

            def sender(channel: str, payload: str, headers: dict[str, str]):
                sent.append(
                    {"channel": channel, "payload": json.loads(payload), "headers": headers}
                )
                return {"id": "response-message"}

            delivery = DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"}, sender=sender
            )
            task = {
                "root_task": {
                    "body": (
                        "discord_message_id=message\n"
                        "discord_channel_id=channel\n"
                    )
                }
            }

            self.assertEqual(
                delivery.deliver(
                    root_task_id="root",
                    synthesis_task=task,
                    content="CEO final answer",
                    store=store,
                ),
                "sent",
            )
            self.assertEqual(
                delivery.deliver(
                    root_task_id="root",
                    synthesis_task=task,
                    content="CEO final answer",
                    store=store,
                ),
                "deduped",
            )
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["channel"], "channel")
            self.assertEqual(
                sent[0]["payload"]["message_reference"]["message_id"],
                "message",
            )

    def test_missing_correlation_fails_closed_without_send(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DiscordIdempotencyStore(Path(directory))
            sent: list[object] = []
            delivery = DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"},
                sender=lambda *_args: sent.append(True) or {"id": "unexpected"},
            )

            result = delivery.deliver(
                root_task_id="root",
                synthesis_task={"body": "no Discord context"},
                content="CEO final answer",
                store=store,
            )

            self.assertEqual(result, "missing_context")
            self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
