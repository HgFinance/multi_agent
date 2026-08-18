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
            session_id = "session-1"
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
            key = gateway_patch.canonical_discord_dedup_key("guild", "channel", "message-1")
            self.assertEqual(
                gateway_patch._store(adapter).inbound_context(key, "qa-department")[
                    "session_id"
                ],
                "session-1",
            )

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


class ForwardToIngressTests(unittest.TestCase):
    """Discord 메시지를 BFF canonical ingress로 넘기는 경로.

    이 배선이 없으면 Discord 질의는 CEO Agent가 직접 root 카드를 만들고, 그
    경로는 `build_root_body()`를 지나지 않아 **Mandate 스냅샷도 `requested_by=`도
    붙지 않는다** - 웹에서 물으면 붙고 Discord에서 물으면 안 붙는 상태였다.
    """

    class _Author:
        def __init__(self, author_id: str, bot: bool = False) -> None:
            self.id = author_id
            self.bot = bot

    _INGRESS_SECRET = "discord-ingress-test-key-0123456789abcdef"

    def _env(self, **updates: str) -> dict[str, str]:
        values = {
            gateway_patch.INGRESS_URL_ENV: "http://bff/ui/ceo/ingress",
            gateway_patch.INGRESS_SECRET_ENV: self._INGRESS_SECRET,
            "HERMES_PROFILE": "ceo-agent",
        }
        values.update(updates)
        return values

    class _Message:
        def __init__(self, content: str, author: object | None, message_id: str = "991") -> None:
            self.id = message_id
            self.content = content
            self.author = author
            self.channel = type("C", (), {"id": "chan-1", "parent_id": None})()
            self.guild = type("G", (), {"id": "guild-1"})()

    def _message(self, content: str = "리서치 브리핑해줘", **kwargs: object):
        author = kwargs.pop("author", self._Author("123456789012345678"))
        return self._Message(content, author, **kwargs)  # type: ignore[arg-type]

    def test_disabled_when_url_is_unset(self) -> None:
        """URL이 없으면 기능이 꺼진다 - 이 코드가 없던 때와 같은 동작이다."""

        with patch.dict("os.environ", self._env(**{gateway_patch.INGRESS_URL_ENV: ""})):
            self.assertFalse(gateway_patch._forward_to_ingress(self._message(), None))

    def test_enabled_url_without_private_credential_fails_closed(self) -> None:
        with patch.dict(
            "os.environ",
            self._env(**{gateway_patch.INGRESS_SECRET_ENV: ""}),
        ):
            self.assertFalse(gateway_patch._forward_to_ingress(self._message(), None))

    def test_bot_authored_message_is_not_forwarded(self) -> None:
        """미러 게시물(`[web-mirror]`)은 봇이 쓴다. 그걸 사람 발화로 오인하면
        웹 질문 하나가 워크플로를 다시 만들고 답변이 또 올라가 순환한다."""

        env = self._env()
        with patch.dict("os.environ", env):
            message = self._message(author=self._Author("999", bot=True))
            self.assertFalse(gateway_patch._forward_to_ingress(message, None))

    def test_unknown_author_is_treated_as_a_bot(self) -> None:
        """판단할 수 없으면 봇으로 본다 - 확신 없이 사람으로 치면 순환이 생긴다."""

        env = self._env()
        with patch.dict("os.environ", env):
            self.assertFalse(gateway_patch._forward_to_ingress(self._message(author=None), None))

    def test_payload_carries_author_and_delivery_coordinates(self) -> None:
        """작성자 id와 발송 좌표가 실려야 Mandate와 답변 위치가 정해진다."""

        captured: dict[str, object] = {}

        class _Response:
            status = 202

            def __enter__(self):  # noqa: ANN204 - 컨텍스트 매니저 대역
                return self

            def __exit__(self, *args: object) -> bool:
                return False

        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            import json

            captured.update(json.loads(request.data.decode("utf-8")))
            captured["authorization"] = request.get_header("Authorization")
            return _Response()

        env = self._env()
        with patch.dict("os.environ", env), patch("urllib.request.urlopen", fake_urlopen):
            self.assertTrue(gateway_patch._forward_to_ingress(self._message(), None))

        self.assertEqual(captured["source"], "discord")
        self.assertEqual(captured["actor_id"], "123456789012345678")
        self.assertEqual(captured["actor_type"], "user")
        self.assertEqual(captured["discord_channel_id"], "chan-1")
        self.assertEqual(captured["discord_message_id"], "991")
        # `discord_delivery._message_id_from_request_id()`가 이 접두어를 보고
        # 뒤를 잘라 쓴다 - 형식이 바뀌면 답변이 원본 메시지에 못 붙는다.
        self.assertEqual(captured["request_id"], "discord:991")
        self.assertEqual(
            captured["authorization"], f"Bearer {self._INGRESS_SECRET}"
        )

    def test_trading_profile_uses_the_same_governed_ingress(self) -> None:
        """Trading-channel orders still create a CEO root and Trading child."""

        class _Response:
            status = 202

            def __enter__(self):  # noqa: ANN204
                return self

            def __exit__(self, *args: object) -> bool:
                return False

        env = self._env(HERMES_PROFILE="trading-department")
        with patch.dict("os.environ", env), patch(
            "urllib.request.urlopen", return_value=_Response()
        ) as opened:
            self.assertTrue(gateway_patch._forward_to_ingress(self._message(), None))
        self.assertEqual(opened.call_count, 1)

    def test_unrelated_department_cannot_use_order_ingress(self) -> None:
        env = self._env(HERMES_PROFILE="research-department")
        with patch.dict("os.environ", env), patch(
            "urllib.request.urlopen"
        ) as opened:
            self.assertFalse(gateway_patch._forward_to_ingress(self._message(), None))
        opened.assert_not_called()

    def test_transport_failure_falls_back_to_hermes(self) -> None:
        """전달 실패는 조용히 버리지 않고 기존 경로로 되돌린다."""

        def boom(request, timeout=None):  # noqa: ANN001
            raise OSError("connection refused")

        env = self._env()
        with patch.dict("os.environ", env), patch("urllib.request.urlopen", boom):
            self.assertFalse(gateway_patch._forward_to_ingress(self._message(), None))

    def test_duplicate_is_treated_as_forwarded(self) -> None:
        """409(이미 받은 메시지)를 실패로 보면 Hermes가 중복 실행한다."""

        import urllib.error

        def conflict(request, timeout=None):  # noqa: ANN001
            raise urllib.error.HTTPError("u", 409, "conflict", {}, None)  # type: ignore[arg-type]

        env = self._env()
        with patch.dict("os.environ", env), patch("urllib.request.urlopen", conflict):
            self.assertTrue(gateway_patch._forward_to_ingress(self._message(), None))
