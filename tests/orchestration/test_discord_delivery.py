"""CEO synthesis Discord delivery contract tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestration.discord_delivery import DiscordFinalDelivery, correlation_from_task
from orchestration.discord_idempotency import (
    DiscordIdempotencyStore,
    canonical_discord_dedup_key,
)


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

    def test_message_id_reuses_existing_inbound_channel_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_inbound(directory)
            sent: list[dict[str, object]] = []

            def sender(
                channel: str,
                payload: str,
                _headers: dict[str, str],
            ) -> dict[str, object]:
                sent.append({"channel": channel, "payload": json.loads(payload)})
                return {"id": "response-message"}

            result = DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"},
                sender=sender,
            ).deliver(
                root_task_id="root",
                synthesis_task={"body": "discord_message_id=message"},
                content="CEO final answer",
                store=store,
            )
            self.assertEqual(result, "sent")
            self.assertEqual(sent[0]["channel"], "channel")

    def test_explicit_message_and_channel_context_sends_without_ledger_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DiscordIdempotencyStore(Path(directory))
            sent: list[dict[str, object]] = []

            def sender(channel: str, payload: str, _headers: dict[str, str]):
                sent.append({"channel": channel, "payload": json.loads(payload)})
                return {"id": "response-message"}

            result = DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"},
                sender=sender,
            ).deliver(
                root_task_id="root",
                synthesis_task={
                    "body": (
                        "discord_message_id=explicit-message\n"
                        "discord_channel_id=explicit-channel\n"
                    )
                },
                content="CEO final answer",
                store=store,
            )

            self.assertEqual(result, "sent")
            self.assertEqual(sent[0]["channel"], "explicit-channel")

    def test_session_ledger_context_is_used_when_explicit_message_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DiscordIdempotencyStore(Path(directory))
            key = canonical_discord_dedup_key("guild", "channel", "session-message")
            self.assertTrue(
                store.claim_inbound(
                    dedup_key=key,
                    message_id="session-message",
                    guild_id="guild",
                    channel_id="channel",
                    thread_id="thread",
                    profile="ceo-agent",
                    handler="live",
                    session_id="session-1",
                ).admitted
            )
            sent: list[dict[str, object]] = []

            def sender(channel: str, payload: str, _headers: dict[str, str]):
                sent.append({"channel": channel, "payload": json.loads(payload)})
                return {"id": "response-message"}

            result = DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"},
                sender=sender,
            ).deliver(
                root_task_id="root",
                synthesis_task={"body": "discord_session_id=session-1"},
                content="CEO final answer",
                store=store,
            )

            self.assertEqual(result, "sent")
            self.assertEqual(sent[0]["channel"], "channel")
            self.assertEqual(
                sent[0]["payload"]["message_reference"]["message_id"],
                "session-message",
            )

    def test_unmatched_session_does_not_use_global_or_latest_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_inbound(directory)
            sent: list[object] = []
            result = DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"},
                sender=lambda *_args: sent.append(True) or {"id": "unexpected"},
            ).deliver(
                root_task_id="root",
                synthesis_task={"body": "discord_session_id=other-session"},
                content="CEO final answer",
                store=store,
            )

            self.assertEqual(result, "missing_context")
            self.assertEqual(sent, [])
    def test_department_detail_is_chunked_into_existing_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DiscordIdempotencyStore(Path(directory))
            sent: list[dict[str, object]] = []

            def sender(channel: str, payload: str, _headers: dict[str, str]):
                sent.append(
                    {
                        "channel": channel,
                        "payload": json.loads(payload),
                    }
                )
                return {"id": f"detail-{len(sent)}"}

            delivery = DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"},
                sender=sender,
            )

            task = {
                "root_task": {
                    "body": (
                        "discord_message_id=message\n"
                        "discord_channel_id=channel\n"
                        "discord_thread_id=thread-123\n"
                        "discord_guild_id=guild\n"
                    )
                }
            }

            content = "A" * 3600

            result = delivery.deliver_to_existing_thread(
                root_task_id="root",
                source_task=task,
                content=content,
                title="Quant 상세 분석",
                store=store,
                profile="quant-backtest-department",
                response_key_suffix="department-detail:task-1",
            )

            self.assertEqual(result, "sent")
            self.assertGreaterEqual(len(sent), 2)
            self.assertTrue(
                all(item["channel"] == "thread-123" for item in sent)
            )

            # Re-delivery of the same task is idempotent.
            second = delivery.deliver_to_existing_thread(
                root_task_id="root",
                source_task=task,
                content=content,
                title="Quant 상세 분석",
                store=store,
                profile="quant-backtest-department",
                response_key_suffix="department-detail:task-1",
            )

            self.assertEqual(second, "sent")
            self.assertGreaterEqual(len(sent), 2)



if __name__ == "__main__":
    unittest.main()
