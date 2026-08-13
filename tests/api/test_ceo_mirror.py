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
