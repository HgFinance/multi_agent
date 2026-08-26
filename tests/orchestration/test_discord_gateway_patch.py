from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
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
    def test_ceo_repeat_command_replays_prior_answer_without_handler(self) -> None:
        class Sent:
            id = "9004"

        class Prior:
            content = "기존 CEO 답변"

        class Thread:
            id = 9002

            async def fetch_message(self, message_id):
                self.requested = message_id
                return Prior()

        class Client:
            user = type("Bot", (), {"id": 42})()

            def __init__(self):
                self.thread = Thread()

            def get_channel(self, channel_id):
                return self.thread if channel_id == 9002 else None

        class Channel:
            id = 9001
            parent_id = None

        class Guild:
            id = 9000

        class Author:
            bot = False

        class Message:
            id = "9003"
            content = "<@42> 대답"
            channel = Channel()
            guild = Guild()
            author = Author()

            def __init__(self):
                self.replies = []

            async def reply(self, content, mention_author=False):
                self.replies.append((content, mention_author))
                return Sent()

        class Adapter:
            def __init__(self):
                self._client = Client()

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {"HERMES_HOME": directory, "HERMES_PROFILE": "ceo-agent"},
            clear=False,
        ):
            adapter = Adapter()
            store = gateway_patch._store(adapter)
            prior_key = gateway_patch.canonical_discord_dedup_key(9000, 9001, "8999")
            store.claim_inbound(
                dedup_key=prior_key,
                message_id="8999",
                guild_id="9000",
                channel_id="9001",
                thread_id="9002",
                profile="ceo-agent",
                handler="live",
            )
            prior_response_key = "discord:9000:9002:8999:synthesis-detail:t_prior"
            store.claim_outbound(
                response_key=prior_response_key,
                dedup_key="discord:9000:9002:8999",
                profile="ceo-agent",
            )
            store.mark_outbound(
                prior_response_key,
                "COMPLETED",
                "ceo-agent",
                "8998",
            )
            current_key = gateway_patch.canonical_discord_dedup_key(9000, 9001, "9003")
            store.claim_inbound(
                dedup_key=current_key,
                message_id="9003",
                guild_id="9000",
                channel_id="9001",
                thread_id=None,
                profile="ceo-agent",
                handler="live",
            )
            message = Message()

            handled = asyncio.run(
                gateway_patch._maybe_handle_ceo_repeat_message(adapter, message)
            )

            self.assertTrue(handled)
            self.assertEqual(len(message.replies), 1)
            self.assertIn("기존 CEO 답변", message.replies[0][0])
            self.assertIn("현재 주문 상태를 다시 조회한 결과는 아닙니다", message.replies[0][0])
            self.assertEqual(store.inbound_state(current_key, "ceo-agent"), "COMPLETED")

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
                "    async def _dispatch_discord_message(self):\n"
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
            gateway_patch.INGRESS_ALERT_WEBHOOK_ENV: "",
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
            self.replies: list[tuple[str, bool]] = []

        async def reply(self, content: str, *, mention_author: bool = False) -> None:
            self.replies.append((content, mention_author))

    def _message(self, content: str = "리서치 브리핑해줘", **kwargs: object):
        author = kwargs.pop("author", self._Author("123456789012345678"))
        return self._Message(content, author, **kwargs)  # type: ignore[arg-type]

    def test_disabled_when_url_is_unset(self) -> None:
        """URL이 없으면 기능이 꺼진다 - 이 코드가 없던 때와 같은 동작이다."""

        with patch.dict("os.environ", self._env(**{gateway_patch.INGRESS_URL_ENV: ""})):
            self.assertFalse(gateway_patch._forward_to_ingress(self._message(), None))

    def test_ingress_timeout_is_bounded_and_invalid_values_use_default(self) -> None:
        with patch.dict(
            "os.environ",
            {gateway_patch.INGRESS_TIMEOUT_ENV: "invalid"},
            clear=False,
        ):
            self.assertEqual(gateway_patch._ingress_timeout_seconds(), 5.0)
        with patch.dict(
            "os.environ",
            {gateway_patch.INGRESS_TIMEOUT_ENV: "nan"},
            clear=False,
        ):
            self.assertEqual(gateway_patch._ingress_timeout_seconds(), 5.0)
        with patch.dict(
            "os.environ",
            {gateway_patch.INGRESS_TIMEOUT_ENV: "0.1"},
            clear=False,
        ):
            self.assertEqual(gateway_patch._ingress_timeout_seconds(), 1.0)
        with patch.dict(
            "os.environ",
            {gateway_patch.INGRESS_TIMEOUT_ENV: "90"},
            clear=False,
        ):
            self.assertEqual(gateway_patch._ingress_timeout_seconds(), 30.0)

    def test_enabled_url_without_private_credential_fails_closed(self) -> None:
        with patch.dict(
            "os.environ",
            self._env(**{gateway_patch.INGRESS_SECRET_ENV: ""}),
        ):
            self.assertTrue(gateway_patch._forward_to_ingress(self._message(), None))

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

    def test_successful_forward_completes_gateway_inbound_lease(self) -> None:
        """BFF가 소유권을 받으면 gateway PROCESSING lease를 끝낸다."""

        class _Response:
            status = 202

            def __enter__(self):  # noqa: ANN204 - 컨텍스트 매니저 대역
                return self

            def __exit__(self, *args: object) -> bool:
                return False

        class _Adapter:
            pass

        message = self._message(message_id="handoff-1")
        env = self._env()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {**env, "HERMES_HOME": directory},
        ), patch("urllib.request.urlopen", return_value=_Response()):
            adapter = _Adapter()
            store = gateway_patch._store(adapter)
            dedup_key = gateway_patch.canonical_discord_dedup_key(
                "guild-1", "chan-1", "handoff-1"
            )
            store.claim_inbound(
                dedup_key=dedup_key,
                message_id="handoff-1",
                guild_id="guild-1",
                channel_id="chan-1",
                thread_id=None,
                profile="ceo-agent",
                handler="live",
            )
            store.mark_inbound(dedup_key, "PROCESSING", "ceo-agent")

            self.assertTrue(gateway_patch._forward_to_ingress(message, adapter))
            duplicate = store.claim_inbound(
                dedup_key=dedup_key,
                message_id="handoff-1",
                guild_id="guild-1",
                channel_id="chan-1",
                thread_id=None,
                profile="ceo-agent",
                handler="live",
            )
            self.assertFalse(duplicate.admitted)
            self.assertEqual(duplicate.state, "COMPLETED")

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

    def test_transport_failure_is_consumed_to_prevent_ambiguous_replay(self) -> None:
        """동일 request id로 한정 재시도한 뒤에도 실패하면 닫힌다."""

        calls = 0

        def boom(request, timeout=None):  # noqa: ANN001
            nonlocal calls
            calls += 1
            raise OSError("connection refused")

        env = self._env()
        message = self._message()
        with patch.dict("os.environ", env), patch(
            "urllib.request.urlopen", boom
        ), patch.object(gateway_patch.time, "sleep"):
            self.assertTrue(gateway_patch._forward_to_ingress(message, None))
        self.assertEqual(calls, 4)
        self.assertEqual(
            getattr(message, gateway_patch._INGRESS_FAILURE_ATTRIBUTE), "transport"
        )

    def test_failed_closed_alert_is_scheduled_without_blocking_ingress(self) -> None:
        """운영 알림 전송은 fail-closed 판정과 별도 백그라운드 작업이어야 한다."""

        class _Response:
            def __init__(self, status: int) -> None:
                self.status = status

            def __enter__(self):  # noqa: ANN204 - 컨텍스트 매니저 대역
                return self

            def __exit__(self, *args: object) -> bool:
                return False

        scheduled: list[object] = []

        class _Thread:
            def __init__(self, **kwargs: object) -> None:
                self.target = kwargs["target"]
                self.args = kwargs.get("args", ())
                self.kwargs = kwargs.get("kwargs", {})

            def start(self) -> None:
                scheduled.append(self)

        responses = iter((_Response(400), _Response(204)))
        opened: list[tuple[object, float | None]] = []

        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            opened.append((request, timeout))
            return next(responses)

        alert_webhook = "https://alerts.example.test/discord-webhook"
        env = self._env(
            **{
                gateway_patch.INGRESS_ALERT_WEBHOOK_ENV: alert_webhook,
                gateway_patch.INGRESS_ALERT_COOLDOWN_ENV: "0",
            }
        )
        with gateway_patch._ingress_alert_lock:
            gateway_patch._ingress_alert_in_flight = False
            gateway_patch._ingress_alert_last_attempt.clear()
        try:
            with patch.dict("os.environ", env), patch(
                "urllib.request.urlopen", fake_urlopen
            ), patch.object(gateway_patch.threading, "Thread", _Thread):
                message = self._message()
                self.assertTrue(gateway_patch._forward_to_ingress(message, None))

                # Only the BFF request ran on the ingress caller. The alert
                # worker is scheduled and is not awaited here.
                self.assertEqual(len(opened), 1)
                self.assertEqual(len(scheduled), 1)
                scheduled_job = scheduled[0]
                scheduled_job.target(
                    *scheduled_job.args,
                    **scheduled_job.kwargs,
                )

            self.assertEqual(len(opened), 2)
            alert_request, alert_timeout = opened[1]
            self.assertEqual(alert_timeout, 1.0)
            self.assertEqual(
                alert_request.get_full_url(),
                alert_webhook,
            )
            alert_payload = alert_request.data.decode("utf-8")
            self.assertIn("message_id=991", alert_payload)
            self.assertNotIn("리서치 브리핑해줘", alert_payload)
            self.assertNotIn("Authorization", str(alert_request.header_items()))
        finally:
            with gateway_patch._ingress_alert_lock:
                gateway_patch._ingress_alert_in_flight = False
                gateway_patch._ingress_alert_last_attempt.clear()

    def test_transport_retry_uses_one_idempotent_payload_and_completes_lease(self) -> None:
        class _Response:
            status = 202

            def __enter__(self):  # noqa: ANN204
                return self

            def __exit__(self, *args: object) -> bool:
                return False

        class _Adapter:
            pass

        calls = 0

        def recover(request, timeout=None):  # noqa: ANN001
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("connection refused")
            return _Response()

        message = self._message(message_id="retry-handoff-1")
        env = self._env()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {**env, "HERMES_HOME": directory}
        ), patch("urllib.request.urlopen", recover), patch.object(
            gateway_patch.time, "sleep"
        ):
            adapter = _Adapter()
            store = gateway_patch._store(adapter)
            dedup_key = gateway_patch.canonical_discord_dedup_key(
                "guild-1", "chan-1", "retry-handoff-1"
            )
            store.claim_inbound(
                dedup_key=dedup_key,
                message_id="retry-handoff-1",
                guild_id="guild-1",
                channel_id="chan-1",
                thread_id=None,
                profile="ceo-agent",
                handler="live",
            )
            store.mark_inbound(dedup_key, "PROCESSING", "ceo-agent")

            self.assertTrue(gateway_patch._forward_to_ingress(message, adapter))
            duplicate = store.claim_inbound(
                dedup_key=dedup_key,
                message_id="retry-handoff-1",
                guild_id="guild-1",
                channel_id="chan-1",
                thread_id=None,
                profile="ceo-agent",
                handler="live",
            )

        self.assertEqual(calls, 2)
        self.assertFalse(duplicate.admitted)
        self.assertEqual(duplicate.state, "COMPLETED")

    def test_auth_rejection_is_consumed_instead_of_bypassing_bff(self) -> None:
        import urllib.error

        def unauthorized(request, timeout=None):  # noqa: ANN001
            raise urllib.error.HTTPError(  # type: ignore[arg-type]
                "u", 401, "unauthorized", {}, None
            )

        env = self._env()
        with patch.dict("os.environ", env), patch(
            "urllib.request.urlopen", unauthorized
        ):
            self.assertTrue(gateway_patch._forward_to_ingress(self._message(), None))

    def test_duplicate_is_treated_as_forwarded(self) -> None:
        """409(이미 받은 메시지)를 실패로 보면 Hermes가 중복 실행한다."""

        import urllib.error

        def conflict(request, timeout=None):  # noqa: ANN001
            raise urllib.error.HTTPError("u", 409, "conflict", {}, None)  # type: ignore[arg-type]

        env = self._env()
        with patch.dict("os.environ", env), patch("urllib.request.urlopen", conflict):
            self.assertTrue(gateway_patch._forward_to_ingress(self._message(), None))


class AsyncForwardToIngressTests(unittest.IsolatedAsyncioTestCase):
    class _Author:
        bot = False

    class _Starter:
        id = "100"
        content = "삼성전자 이거 4분 뒤에 1주 매수해줘"
        author = None

        def __init__(self) -> None:
            self.author = AsyncForwardToIngressTests._Author()

    class _ParentChannel:
        id = "parent-10"

        def __init__(self) -> None:
            self.fetch_count = 0

        async def fetch_message(self, message_id: int):
            self.fetch_count += 1
            self.requested_id = message_id
            return AsyncForwardToIngressTests._Starter()

    class _ThreadChannel:
        id = "100"
        parent_id = "10"

    class _Message:
        id = "101"
        content = "지금 위 질문 다시 분석해줘"
        channel = None
        author = None

        def __init__(self) -> None:
            self.channel = AsyncForwardToIngressTests._ThreadChannel()
            self.author = AsyncForwardToIngressTests._Author()

    class _Client:
        def __init__(self, parent: object) -> None:
            self.parent = parent

        def get_channel(self, channel_id: int):
            return self.parent if channel_id == 10 else None

    class _Adapter:
        def __init__(self, parent: object) -> None:
            self._client = AsyncForwardToIngressTests._Client(parent)

    async def test_referential_followup_freezes_exact_thread_starter(self) -> None:
        parent = self._ParentChannel()
        message = self._Message()
        adapter = self._Adapter(parent)

        with patch.dict("os.environ", {"HERMES_PROFILE": "ceo-agent"}):
            resolved = await gateway_patch._resolve_thread_followup_context(
                adapter,
                message,
            )

        self.assertIsNot(resolved, message)
        self.assertEqual(message.content, "지금 위 질문 다시 분석해줘")
        self.assertEqual(resolved.content, "지금 위 질문 다시 분석해줘")
        self.assertEqual(
            getattr(resolved, "_hgfinance_previous_question_context"),
            "삼성전자 이거 4분 뒤에 1주 매수해줘",
        )
        self.assertEqual(
            getattr(resolved, "_hgfinance_previous_question_context_source_message_id"),
            "100",
        )
        self.assertEqual(parent.fetch_count, 1)
        self.assertEqual(parent.requested_id, 100)

    async def test_ordinary_thread_message_skips_context_fetch(self) -> None:
        parent = self._ParentChannel()
        message = self._Message()
        message.content = "삼성전자 밸류에이션도 확인해줘"

        with patch.dict("os.environ", {"HERMES_PROFILE": "ceo-agent"}):
            resolved = await gateway_patch._resolve_thread_followup_context(
                self._Adapter(parent),
                message,
            )

        self.assertIs(resolved, message)
        self.assertEqual(parent.fetch_count, 0)

    async def test_thread_context_timeout_preserves_current_request(self) -> None:
        class SlowParent:
            async def fetch_message(self, message_id: int):  # noqa: ARG002
                await asyncio.sleep(1)

        message = self._Message()
        with patch.dict(
            "os.environ",
            {"HERMES_PROFILE": "ceo-agent"},
        ), patch.object(
            gateway_patch,
            "_THREAD_CONTEXT_FETCH_TIMEOUT_SECONDS",
            0.01,
        ):
            resolved = await gateway_patch._resolve_thread_followup_context(
                self._Adapter(SlowParent()),
                message,
            )

        self.assertIs(resolved, message)

    async def test_slow_ingress_does_not_block_discord_event_loop(self) -> None:
        release = threading.Event()

        def slow_forward(message, adapter):  # noqa: ANN001, ARG001
            release.wait(timeout=1)
            return True

        timer = threading.Timer(0.5, release.set)
        timer.start()
        try:
            with patch.object(gateway_patch, "_forward_to_ingress", slow_forward):
                forward = asyncio.create_task(
                    gateway_patch._forward_to_ingress_async(object(), object())
                )
                started = time.monotonic()
                await asyncio.sleep(0.01)
                self.assertLess(time.monotonic() - started, 0.2)
                self.assertTrue(await forward)
        finally:
            release.set()
            timer.cancel()

    async def test_fail_closed_ingress_notifies_user_without_replaying(self) -> None:
        class _Message:
            id = "failed-ingress-1"

            def __init__(self) -> None:
                self.replies: list[tuple[str, bool]] = []

            async def reply(self, content: str, *, mention_author: bool = False) -> None:
                self.replies.append((content, mention_author))

        message = _Message()
        gateway_patch._mark_ingress_failure(message, "http_503")
        await gateway_patch._notify_ingress_failure(message)

        self.assertEqual(len(message.replies), 1)
        self.assertIn("자동으로 다시 실행하지 않았습니다", message.replies[0][0])
        self.assertFalse(message.replies[0][1])


class DiscordPreAcceptTelemetryTests(unittest.IsolatedAsyncioTestCase):
    class _Author:
        def __init__(self, *, bot: bool = False, author_id: str = "author-1") -> None:
            self.bot = bot
            self.id = author_id

    class _Channel:
        id = "channel-1"
        parent_id = None

    class _Guild:
        id = "guild-1"

    class _Message:
        def __init__(self) -> None:
            self.id = "message-1"
            self.content = "private user content with token=should-not-be-logged"
            self.type = None
            self.mentions: list[object] = []
            self.channel = DiscordPreAcceptTelemetryTests._Channel()
            self.guild = DiscordPreAcceptTelemetryTests._Guild()
            self.author = DiscordPreAcceptTelemetryTests._Author()

    class _Dedup:
        def __init__(self, *, contains: bool = False) -> None:
            self._contains = contains

        def contains(self, message_id: str) -> bool:  # noqa: ARG002
            return self._contains

        def discard(self, message_id: str) -> None:  # noqa: ARG002
            return None

    def _assert_log_contains(self, records: list[object], text: str) -> None:
        self.assertTrue(any(text in str(record.getMessage()) for record in records))  # type: ignore[union-attr]

    async def test_raw_callback_logs_metadata_and_accepted_correlation(self) -> None:
        class Adapter:
            def __init__(self) -> None:
                self._dedup = DiscordPreAcceptTelemetryTests._Dedup()
                self._client = type("Client", (), {"user": object()})()

            def _self_is_raw_mentioned(self, message):  # noqa: ANN001
                return False

            def _discord_message_admission(self, message, *, claim):  # noqa: ANN001, ARG002
                return True, False

            async def _dispatch_discord_message(self, message):  # noqa: ANN001
                admitted, _role_authorized = self._discord_message_admission(
                    message, claim=True
                )
                return admitted

        gateway_patch._wrap_admission(Adapter)
        gateway_patch._wrap_dispatch(Adapter)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {"HERMES_HOME": directory, "HERMES_PROFILE": "ceo-agent"},
            clear=False,
        ), self.assertLogs(gateway_patch.logger, level="INFO") as captured:
            result = await Adapter()._dispatch_discord_message(self._Message())

        self.assertTrue(result)
        self._assert_log_contains(captured.records, "discord-raw-message")
        self._assert_log_contains(captured.records, "discord_gateway_event")
        combined = "\n".join(record.getMessage() for record in captured.records)
        self.assertNotIn("private user content with token=should-not-be-logged", combined)
        self.assertNotIn("token=should-not-be-logged", combined)
        self.assertIn('"message_id":"message-1"', combined)
        self.assertIn('"author_kind":"human"', combined)

    async def test_bot_drop_logs_bot_reason_without_content(self) -> None:
        class Adapter:
            def __init__(self) -> None:
                self._dedup = DiscordPreAcceptTelemetryTests._Dedup()
                self._client = type("Client", (), {"user": object()})()

            def _discord_message_admission(self, message, *, claim):  # noqa: ANN001, ARG002
                return False, False

            def _get_allow_bots(self):
                return "none"

        message = self._Message()
        message.author = self._Author(bot=True)
        gateway_patch._wrap_admission(Adapter)
        with self.assertLogs(gateway_patch.logger, level="INFO") as captured:
            self.assertEqual(Adapter()._discord_message_admission(message, claim=True), (False, False))
        self._assert_log_contains(captured.records, '"reason":"BOT_AUTHOR"')
        self._assert_log_contains(captured.records, "discord-pre-filter-drop")

    async def test_webhook_drop_logs_webhook_reason(self) -> None:
        class Adapter:
            def __init__(self) -> None:
                self._dedup = DiscordPreAcceptTelemetryTests._Dedup()
                self._client = type("Client", (), {"user": object()})()

            def _discord_message_admission(self, message, *, claim):  # noqa: ANN001, ARG002
                return False, False

        message = self._Message()
        message.webhook_id = "webhook-1"
        gateway_patch._wrap_admission(Adapter)
        with self.assertLogs(gateway_patch.logger, level="INFO") as captured:
            self.assertEqual(Adapter()._discord_message_admission(message, claim=True), (False, False))
        self._assert_log_contains(captured.records, '"reason":"WEBHOOK"')

    async def test_channel_policy_drop_logs_channel_reason(self) -> None:
        class Adapter:
            def __init__(self) -> None:
                self._dedup = DiscordPreAcceptTelemetryTests._Dedup()
                self._client = type("Client", (), {"user": object()})()
                self._allowed_user_ids = set()
                self._allowed_role_ids = set()

            def _discord_message_admission(self, message, *, claim):  # noqa: ANN001, ARG002
                return False, False

            def _discord_allow_all_users(self):
                return False

            def _gateway_allow_all_users(self):
                return False

        gateway_patch._wrap_admission(Adapter)
        with self.assertLogs(gateway_patch.logger, level="INFO") as captured:
            self.assertEqual(Adapter()._discord_message_admission(self._Message(), claim=True), (False, False))
        self._assert_log_contains(captured.records, '"reason":"CHANNEL_POLICY"')

    async def test_mention_policy_drop_logs_mention_reason(self) -> None:
        class Adapter:
            def __init__(self) -> None:
                self._dedup = DiscordPreAcceptTelemetryTests._Dedup()
                self._client = type("Client", (), {"user": object()})()

            def _discord_message_admission(self, message, *, claim):  # noqa: ANN001, ARG002
                return False, False

            def _discord_allow_all_users(self):
                return True

            def _gateway_allow_all_users(self):
                return False

            def _self_is_explicitly_mentioned(self, message):  # noqa: ANN001
                return False

            def _self_is_raw_mentioned(self, message):  # noqa: ANN001
                return False

            def _discord_free_response_channels(self):
                return set()

            def _discord_channel_keys(self, message):  # noqa: ANN001
                return {"channel-1"}

        gateway_patch._wrap_admission(Adapter)
        with patch.dict("os.environ", {"DISCORD_IGNORE_NO_MENTION": "true"}), self.assertLogs(
            gateway_patch.logger, level="INFO"
        ) as captured:
            self.assertEqual(Adapter()._discord_message_admission(self._Message(), claim=True), (False, False))
        self._assert_log_contains(captured.records, '"reason":"MENTION_POLICY"')

    async def test_dedup_drop_logs_dedup_reason(self) -> None:
        class Adapter:
            def __init__(self) -> None:
                self._dedup = DiscordPreAcceptTelemetryTests._Dedup(contains=True)
                self._client = type("Client", (), {"user": object()})()

            def _discord_message_admission(self, message, *, claim):  # noqa: ANN001, ARG002
                raise AssertionError("dedup must short-circuit the original admission")

        gateway_patch._wrap_admission(Adapter)
        with self.assertLogs(gateway_patch.logger, level="INFO") as captured:
            self.assertEqual(Adapter()._discord_message_admission(self._Message(), claim=True), (False, False))
        self._assert_log_contains(captured.records, '"reason":"DEDUP"')

    async def test_telemetry_does_not_log_auth_or_payload_values(self) -> None:
        class Adapter:
            def _self_is_raw_mentioned(self, message):  # noqa: ANN001
                return False

        message = self._Message()
        message.content = "Authorization: Bearer secret-value"
        with self.assertLogs(gateway_patch.logger, level="INFO") as captured:
            gateway_patch._log_raw_message(Adapter(), message)
        combined = "\n".join(record.getMessage() for record in captured.records)
        self.assertNotIn("Authorization", combined)
        self.assertNotIn("secret-value", combined)
        self.assertNotIn("private user content", combined)
