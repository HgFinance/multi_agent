from __future__ import annotations

import importlib.util
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.api import discord_mirror
from apps.api.ceo_mirror import (
    InMemoryMirrorStore,
    MirrorStoreUnavailable,
    build_default_mirror_store,
)
from orchestration import discord_delivery, discord_idempotency

_PATCH_PATH = (
    Path(__file__).parents[2] / "deploy" / "hermes-discord" / "gateway_patch.py"
)
_PATCH_SPEC = importlib.util.spec_from_file_location(
    "hgfinance_gateway_patch_controls", _PATCH_PATH
)
assert _PATCH_SPEC and _PATCH_SPEC.loader
gateway_patch = importlib.util.module_from_spec(_PATCH_SPEC)
_PATCH_SPEC.loader.exec_module(gateway_patch)


class _DiscordResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload


class DiscordDeliveryControlTests(unittest.TestCase):
    def test_production_does_not_fall_back_to_in_memory_store(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "UI_MIRROR_REDIS_URL": "",
                "REDIS_URL": "",
            },
            clear=False,
        ):
            with self.assertRaises(MirrorStoreUnavailable):
                build_default_mirror_store()

    def test_web_mirror_reuses_the_claimed_discord_post(self) -> None:
        store = InMemoryMirrorStore()
        calls: list[str] = []

        def post(url: str, **_kwargs: object) -> _DiscordResponse:
            calls.append(url)
            if url.endswith("/threads"):
                return _DiscordResponse({"id": "thread-1"})
            return _DiscordResponse(
                {"id": "message-1", "channel_id": "channel-1", "guild_id": "guild-1"}
            )

        with (
            patch.dict(
                os.environ,
                {
                    discord_mirror.ENABLED_ENV: "true",
                    discord_mirror.TOKEN_ENV: "test-token",
                    discord_mirror.CHANNEL_ENV: "channel-1",
                },
                clear=False,
            ),
            patch.object(discord_mirror, "test_runner_active", return_value=False),
            patch.object(discord_mirror.httpx, "post", side_effect=post),
        ):
            first = discord_mirror.post_question(
                "삼성전자 분석",
                mirror_store=store,
                mirror_key="web:stable-request",
                request_id="request-1",
            )
            second = discord_mirror.post_question(
                "삼성전자 분석",
                mirror_store=store,
                mirror_key="web:stable-request",
                request_id="request-2",
            )

        self.assertEqual(first, second)
        self.assertIsNotNone(first)
        self.assertEqual(
            calls,
            [
                "https://discord.com/api/v10/channels/channel-1/messages",
                "https://discord.com/api/v10/channels/channel-1/messages/message-1/threads",
            ],
        )

    def test_missing_discord_message_id_remains_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = discord_idempotency.DiscordIdempotencyStore(directory)
            responses = iter(({}, {"id": "response-1"}))

            def sender(
                _channel: str, _payload: str, _headers: dict[str, str]
            ) -> object:
                return next(responses)

            delivery = discord_delivery.DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"},
                sender=sender,
            )
            task = {
                "body": "discord_message_id=message\ndiscord_channel_id=channel\n"
            }

            first = delivery.deliver(
                root_task_id="root-missing-id",
                synthesis_task=task,
                content="CEO final answer",
                store=store,
            )
            second = delivery.deliver(
                root_task_id="root-missing-id",
                synthesis_task=task,
                content="CEO final answer",
                store=store,
            )

        self.assertEqual(first, "failed")
        self.assertEqual(second, "sent")

    def test_missing_thread_card_message_id_remains_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = discord_idempotency.DiscordIdempotencyStore(directory)
            responses = iter(({}, {"id": "card-1"}))

            def sender(
                _channel: str, _payload: str, _headers: dict[str, str]
            ) -> object:
                return next(responses)

            delivery = discord_delivery.DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"},
                sender=sender,
            )
            task = {
                "body": (
                    "discord_message_id=message\n"
                    "discord_channel_id=channel\n"
                    "discord_thread_id=thread\n"
                )
            }

            first = delivery.upsert_thread_card(
                root_task_id="root-missing-card-id",
                source_task=task,
                root_task=None,
                content="Department update",
                store=store,
                profile="qa-department",
                response_key_suffix="department-card",
            )
            second = delivery.upsert_thread_card(
                root_task_id="root-missing-card-id",
                source_task=task,
                root_task=None,
                content="Department update",
                store=store,
                profile="qa-department",
                response_key_suffix="department-card",
            )

        self.assertEqual(first, "failed")
        self.assertEqual(second, "created")


class DiscordIngressConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingress_rejects_without_queueing_when_slots_are_full(self) -> None:
        slots = threading.BoundedSemaphore(1)
        self.assertTrue(slots.acquire(blocking=False))
        message = SimpleNamespace(
            id="ingress-busy-1",
            author=SimpleNamespace(id="user-1", bot=False),
        )
        with (
            patch.object(gateway_patch, "_ingress_slots", slots),
            patch.dict(
                os.environ,
                {
                    gateway_patch.INGRESS_URL_ENV: "http://bff:8000/ui/ceo/ingress",
                    "HERMES_PROFILE": "ceo-agent",
                },
                clear=False,
            ),
            patch.object(gateway_patch, "_forward_to_ingress") as forward,
            patch.object(gateway_patch, "_log_ingress_failed_closed"),
        ):
            admitted = await gateway_patch._forward_to_ingress_async(message, None)

        slots.release()
        self.assertTrue(admitted)
        self.assertEqual(
            getattr(message, gateway_patch._INGRESS_FAILURE_ATTRIBUTE),
            "concurrency_limit",
        )
        forward.assert_not_called()


if __name__ == "__main__":
    unittest.main()
