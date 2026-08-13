from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import asyncio
import importlib.util
from unittest.mock import patch


_MODULE_PATH = Path(__file__).parents[2] / "deploy" / "hermes-discord" / "install_patch.py"
_SPEC = importlib.util.spec_from_file_location("hgfinance_install_patch", _MODULE_PATH)
assert _SPEC and _SPEC.loader
install_patch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(install_patch)

_PATCH_PATH = Path(__file__).parents[2] / "deploy" / "hermes-discord" / "gateway_patch.py"
_PATCH_SPEC = importlib.util.spec_from_file_location("hgfinance_gateway_patch", _PATCH_PATH)
assert _PATCH_SPEC and _PATCH_SPEC.loader
gateway_patch = importlib.util.module_from_spec(_PATCH_SPEC)
_PATCH_SPEC.loader.exec_module(gateway_patch)


class DiscordGatewayPatchTests(unittest.TestCase):
    def test_live_and_history_claims_reach_fake_adapter_once(self) -> None:
        class Dedup:
            def __init__(self) -> None:
                self.ids: set[str] = set()

            def contains(self, message_id: str) -> bool:
                return message_id in self.ids

            def is_duplicate(self, message_id: str) -> bool:
                if message_id in self.ids:
                    return True
                self.ids.add(message_id)
                return False

            def discard(self, message_id: str) -> None:
                self.ids.discard(message_id)

        class FakeAdapter:
            def __init__(self) -> None:
                self._dedup = Dedup()
                self.calls = 0

            def _discord_message_admission(self, message, *, claim):
                if claim and self._dedup.is_duplicate(str(message.id)):
                    return False, False
                self.calls += 1
                return True, False

            async def on_processing_complete(self, event, outcome):
                return None

            async def send(self, *args, **kwargs):
                return None

        class Channel:
            id = "channel"
            parent_id = None

        class Guild:
            id = "guild"

        class Message:
            id = "message-1"
            channel = Channel()
            guild = Guild()

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {"HERMES_HOME": directory, "HERMES_PROFILE": "qa-department"},
            clear=False,
        ):
            gateway_patch.install(FakeAdapter)
            adapter = FakeAdapter()
            self.assertEqual(adapter._discord_message_admission(Message(), claim=True), (True, False))
            self.assertEqual(adapter._discord_message_admission(Message(), claim=True), (False, False))
            self.assertEqual(adapter.calls, 1)

    def test_final_publish_wrapper_calls_original_send_once(self) -> None:
        class Result:
            success = True
            message_id = "reply-1"
            error = None

        class FakeSendAdapter:
            def __init__(self) -> None:
                self.calls = 0

            async def send(self, chat_id, content, reply_to=None, metadata=None):
                self.calls += 1
                return Result()

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {"HERMES_HOME": directory, "HERMES_PROFILE": "qa-department"},
            clear=False,
        ):
            store = gateway_patch.DiscordIdempotencyStore(Path(directory))
            key = gateway_patch.canonical_discord_dedup_key("guild", "channel", "message-2")
            store.claim_inbound(
                dedup_key=key,
                message_id="message-2",
                guild_id="guild",
                channel_id="channel",
                thread_id=None,
                profile="qa-department",
                handler="live",
            )
            gateway_patch._wrap_send(FakeSendAdapter)
            adapter = FakeSendAdapter()
            with patch.object(
                gateway_patch,
                "_send_result",
                return_value=Result(),
            ):
                asyncio.run(
                    adapter.send(
                        "channel",
                        "answer",
                        reply_to="message-2",
                        metadata={"notify": True},
                    )
                )
                asyncio.run(
                    adapter.send(
                        "channel",
                        "answer",
                        reply_to="message-2",
                        metadata={"notify": True},
                    )
                )
            self.assertEqual(adapter.calls, 1)

    def test_install_hook_is_contract_checked_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = Path(directory) / "adapter.py"
            adapter.write_text(
                "class DiscordAdapter:\n"
                "    def _discord_message_admission(self):\n"
                "        return True\n"
                "    async def send(self):\n"
                "        return None\n",
                encoding="utf-8",
            )
            with patch.object(install_patch, "find_adapter", return_value=adapter):
                install_patch.main()
                first = adapter.read_text(encoding="utf-8")
                install_patch.main()
                second = adapter.read_text(encoding="utf-8")
            self.assertEqual(first, second)
            self.assertEqual(first.count(install_patch.MARKER), 1)
            self.assertIn("_hgfinance_install_discord_idempotency(DiscordAdapter)", first)
            compile(first, str(adapter), "exec")


if __name__ == "__main__":
    unittest.main()
