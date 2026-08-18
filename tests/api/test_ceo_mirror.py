"""Web/Discord CEO mirror contract tests without Hermes or Redis."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from apps.api import ceo_mirror_api
from apps.api.ceo_mirror import (
    CanonicalIngress,
    InMemoryMirrorStore,
    MirrorEvent,
    execute_once,
)


class CeoMirrorExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryMirrorStore()
        self.patch_store = patch.object(ceo_mirror_api, "MIRROR_STORE", self.store)
        self.patch_store.start()
        self.addCleanup(self.patch_store.stop)

    def test_web_runs_ceo_once_and_replay_is_duplicate(self) -> None:
        calls: list[str] = []

        with patch.object(
            ceo_mirror_api,
            "_ceo_query",
            side_effect=lambda request: (
                calls.append(request.request_id) or {"task_id": "t_web"}
            ),
        ):
            first = ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query="web query",
                    request_id="request-web-1",
                    source="web",
                    source_message_id="web:1",
                    actor_id="web-user",
                )
            )
            second = ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query="web query",
                    request_id="request-web-1",
                    source="web",
                    source_message_id="web:1",
                    actor_id="web-user",
                )
            )

        self.assertEqual(calls, ["request-web-1"])
        self.assertEqual(first.task_id, "t_web")
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)

    def test_discord_runs_ceo_once_and_bot_echo_is_ignored(self) -> None:
        calls: list[str] = []

        with patch.object(
            ceo_mirror_api,
            "_ceo_query",
            side_effect=lambda request: (
                calls.append(request.request_id) or {"task_id": "t_discord"}
            ),
        ):
            accepted = ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query="discord query",
                    request_id="request-discord-1",
                    source="discord",
                    source_message_id="discord:channel:1",
                    actor_id="discord-user",
                )
            )
            ignored = ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query="discord mirror",
                    request_id="request-discord-mirror",
                    source="discord",
                    source_message_id="discord:channel:2",
                    actor_id="ceo-agent",
                    actor_type="bot",
                    mirrored=True,
                )
            )

        self.assertEqual(calls, ["request-discord-1"])
        self.assertEqual(accepted.task_id, "t_discord")
        self.assertTrue(ignored.ignored)
        self.assertEqual(ignored.reason, "bot_mirror_ignored")

    def test_event_id_is_published_once_and_lanes_are_independent(self) -> None:
        request = CanonicalIngress(
            query="q",
            request_id="request-lanes-1",
            source="web",
            source_message_id="web:lanes",
        )
        execution = MirrorEvent(
            event_id="event-ceo-final-1",
            request_id=request.request_id,
            task_id="t_root",
            source="web",
            source_message_id="web:lanes",
            actor_id="ceo-agent",
            actor_type="agent",
            lane="execution",
            event_type="CEO_FINAL",
            status="completed",
            summary="final",
        )
        qa = execution.model_copy(
            update={
                "event_id": "event-qa-result-1",
                "actor_id": "qa-department",
                "lane": "evaluation",
                "event_type": "QA_RESULT",
                "status": "WARN",
                "summary": "evaluation",
            }
        )
        self.assertTrue(self.store.publish_event(execution))
        self.assertFalse(self.store.publish_event(execution))
        self.assertTrue(self.store.publish_event(qa))
        events = self.store.read_events(request.request_id)
        self.assertEqual(
            [event.event_id for event in events], [execution.event_id, qa.event_id]
        )
        self.assertEqual([event.lane for event in events], ["execution", "evaluation"])

    def test_qa_result_does_not_block_ceo_final(self) -> None:
        request = CanonicalIngress(
            query="q",
            request_id="request-qa-parallel",
            source="discord",
            source_message_id="discord:qa",
        )
        store = InMemoryMirrorStore()
        execute_once(request, store=store, execute=lambda: {"task_id": "t_root"})
        store.publish_event(
            MirrorEvent(
                event_id="event-ceo-final-2",
                request_id=request.request_id,
                task_id="t_root",
                source="discord",
                source_message_id="discord:qa",
                actor_id="ceo-agent",
                actor_type="agent",
                lane="execution",
                event_type="CEO_FINAL",
                status="completed",
                summary="final before QA evaluation",
            )
        )
        store.publish_event(
            MirrorEvent(
                event_id="event-qa-result-2",
                request_id=request.request_id,
                task_id="t_qa",
                parent_task_id="t_root",
                source="discord",
                source_message_id="discord:qa",
                actor_id="qa-department",
                actor_type="agent",
                lane="evaluation",
                event_type="QA_RESULT",
                status="FAIL",
                summary="evaluation failed",
            )
        )
        events = store.read_events(request.request_id)
        self.assertEqual(events[0].event_type, "USER_MESSAGE")
        self.assertEqual(events[3].event_type, "CEO_FINAL")
        self.assertEqual(events[4].event_type, "QA_RESULT")

    def test_mirror_ask_forwards_fund_id_to_ceo_query(self) -> None:
        """`/ui/ceo/ask`가 실제로 처리되는 곳은 여기다 - `ceo_router`가 아니라
        `ceo_mirror_router`가 `main.py`에서 먼저 등록돼 같은 경로를 가로챈다.

        `mirror_ask`가 `fund_id` 없는 `AgentAsk`로 요청을 받아 새 `AgentAsk`를
        만들어 `ceo.ceo_query`에 넘기면, 프론트가 `fund_id`를 정확히 보내도
        Mandate 조회가 항상 스킵된다 - 2026-08-14 AWS에서 실측된 회귀다.
        """

        from apps.api.ceo import CeoAsk

        captured: list[CeoAsk] = []

        # `**_coordinates`: 2026-08-18에 `ceo_query`가 Discord 발송 좌표를 받게 되면서
        # 이 대역이 그 kwargs로 TypeError를 냈다. 좌표 자체는 이 테스트의 관심사가
        # 아니라(전용 테스트는 tests/orchestration/test_web_discord_mirror.py) 받아서 버린다.
        def fake_ceo_query(
            req: CeoAsk, owner_id: str | None = None, **_coordinates: object
        ) -> dict[str, object]:
            captured.append(req)
            return {"task_id": "t_mandate", "status": "accepted"}

        with patch("apps.api.ceo.ceo_query", side_effect=fake_ceo_query):
            response = ceo_mirror_api.mirror_ask(
                CeoAsk(
                    query="mandate 인식 확인",
                    request_id="request-fund-1",
                    fund_id="fund-abc",
                ),
                x_source_message_id=None,
                x_actor_id=None,
                x_user_id=None,
            )

        self.assertEqual(response["task_id"], "t_mandate")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].fund_id, "fund-abc")

    def test_mirror_ask_without_fund_id_still_works(self) -> None:
        from apps.api.ceo import CeoAsk

        captured: list[CeoAsk] = []

        # `**_coordinates`: 2026-08-18에 `ceo_query`가 Discord 발송 좌표를 받게 되면서
        # 이 대역이 그 kwargs로 TypeError를 냈다. 좌표 자체는 이 테스트의 관심사가
        # 아니라(전용 테스트는 tests/orchestration/test_web_discord_mirror.py) 받아서 버린다.
        def fake_ceo_query(
            req: CeoAsk, owner_id: str | None = None, **_coordinates: object
        ) -> dict[str, object]:
            captured.append(req)
            return {"task_id": "t_no_fund", "status": "accepted"}

        with patch("apps.api.ceo.ceo_query", side_effect=fake_ceo_query):
            response = ceo_mirror_api.mirror_ask(
                CeoAsk(query="fund 없는 질의", request_id="request-no-fund-1"),
                x_source_message_id=None,
                x_actor_id=None,
                x_user_id=None,
            )

        self.assertEqual(response["task_id"], "t_no_fund")
        self.assertIsNone(captured[0].fund_id)

    def test_mirror_ask_forwards_x_user_id_as_owner_id(self) -> None:
        """`X-User-Id`도 `fund_id`와 같은 이유로 유실되던 값이다.

        `mirror_ask`가 헤더를 받지 않으면 그 값은 실제로 요청을 실행하는
        `ceo.ceo_query`(그리고 root body의 `requested_by=`)까지 도달할 수 없고,
        계정별 이력 조회가 애초에 불가능해진다.
        """

        from apps.api.ceo import CeoAsk

        captured: list[str | None] = []

        # `**_coordinates`: 2026-08-18에 `ceo_query`가 Discord 발송 좌표를 받게 되면서
        # 이 대역이 그 kwargs로 TypeError를 냈다. 좌표 자체는 이 테스트의 관심사가
        # 아니라(전용 테스트는 tests/orchestration/test_web_discord_mirror.py) 받아서 버린다.
        def fake_ceo_query(
            req: CeoAsk, owner_id: str | None = None, **_coordinates: object
        ) -> dict[str, object]:
            captured.append(owner_id)
            return {"task_id": "t_owner", "status": "accepted"}

        with patch("apps.api.ceo.ceo_query", side_effect=fake_ceo_query):
            ceo_mirror_api.mirror_ask(
                CeoAsk(query="계정 이력 확인", request_id="request-owner-1"),
                x_source_message_id=None,
                x_actor_id=None,
                x_user_id="user-a",
            )

        self.assertEqual(captured, ["user-a"])

    def test_missing_x_user_id_stays_unknown_rather_than_defaulting(self) -> None:
        """헤더가 없으면 익명 fallback을 계정으로 둔갑시키지 않는다(개발 원칙 9)."""

        from apps.api.ceo import CeoAsk

        captured: list[str | None] = []

        # `**_coordinates`: 2026-08-18에 `ceo_query`가 Discord 발송 좌표를 받게 되면서
        # 이 대역이 그 kwargs로 TypeError를 냈다. 좌표 자체는 이 테스트의 관심사가
        # 아니라(전용 테스트는 tests/orchestration/test_web_discord_mirror.py) 받아서 버린다.
        def fake_ceo_query(
            req: CeoAsk, owner_id: str | None = None, **_coordinates: object
        ) -> dict[str, object]:
            captured.append(owner_id)
            return {"task_id": "t_anon", "status": "accepted"}

        with patch("apps.api.ceo.ceo_query", side_effect=fake_ceo_query):
            ceo_mirror_api.mirror_ask(
                CeoAsk(query="익명 질의", request_id="request-anon-1"),
                x_source_message_id=None,
                x_actor_id=None,
                x_user_id=None,
            )

        self.assertEqual(captured, [None])

    def test_web_and_discord_views_read_the_same_ceo_final(self) -> None:
        request = CanonicalIngress(
            query="shared result",
            request_id="request-shared-final",
            source="web",
            source_message_id="web:shared-final",
            actor_id="web-user",
        )
        final = MirrorEvent(
            event_id="event-shared-ceo-final",
            request_id=request.request_id,
            task_id="t_shared",
            source=request.source,
            source_message_id=request.source_message_id,
            actor_id="ceo-agent",
            actor_type="agent",
            lane="execution",
            event_type="CEO_FINAL",
            status="completed",
            summary="same final",
        )
        self.assertTrue(self.store.publish_event(final))

        web_view = self.store.read_events(request.request_id)
        discord_view = self.store.read_events(request.request_id)
        self.assertEqual(web_view, discord_view)
        self.assertEqual(web_view[0].task_id, "t_shared")


if __name__ == "__main__":
    unittest.main()
