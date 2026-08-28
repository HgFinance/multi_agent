"""Web/Discord CEO mirror contract tests without Hermes or Redis."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from starlette.requests import Request

from apps.api import ceo_mirror_api
from apps.api.ceo_mirror import (
    CanonicalIngress,
    InMemoryMirrorStore,
    MirrorEvent,
    MirrorRequestConflict,
    RedisMirrorStore,
    execute_once,
)
from apps.api.discord_ingress_auth import mark_request as mark_discord_ingress_request


def _http_request(*, internal_discord: bool = False) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/ui/ceo/ingress",
            "headers": [],
        }
    )
    if internal_discord:
        mark_discord_ingress_request(request)
    return request


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True


def _fake_redis_mirror_store() -> RedisMirrorStore:
    store = object.__new__(RedisMirrorStore)
    store.client = _FakeRedis()
    store.ttl_seconds = 60
    store.request_prefix = "test:request:"
    store.source_prefix = "test:source:"
    return store


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
            side_effect=lambda request, **_: (
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
                ),
                _http_request(),
            )
            second = ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query="web query",
                    request_id="request-web-1",
                    source="web",
                    source_message_id="web:1",
                    actor_id="web-user",
                ),
                _http_request(),
            )

        self.assertEqual(calls, ["request-web-1"])
        self.assertEqual(first.task_id, "t_web")
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)

    def test_failed_execution_releases_only_the_unfinished_exact_claim(self) -> None:
        request = CanonicalIngress(
            query="conditional order",
            request_id="request-recoverable-failure",
            source="discord",
            source_message_id="discord:recoverable-failure",
            actor_id="discord-user",
        )

        with self.assertRaisesRegex(RuntimeError, "temporary failure"):
            execute_once(
                request,
                store=self.store,
                execute=lambda: (_ for _ in ()).throw(
                    RuntimeError("temporary failure")
                ),
            )

        recovered = execute_once(
            request,
            store=self.store,
            execute=lambda: {"task_id": "t_recovered"},
        )
        replay = execute_once(
            request,
            store=self.store,
            execute=lambda: {"task_id": "must-not-run"},
        )

        self.assertEqual(recovered.response, {"task_id": "t_recovered"})
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.response, {"task_id": "t_recovered"})

    def test_request_id_cannot_be_rebound_across_any_authority_field(self) -> None:
        original_values = {
            "query": "삼성전자 10주 시장가 매수",
            "request_id": "request-authority-1",
            "source": "web",
            "source_message_id": "web:authority:1",
            "actor_id": "user-a",
            "actor_type": "user",
            "fund_id": "fund-a",
            "book_id": "book-a",
            "mirrored": False,
        }
        variants = {
            "query": "삼성전자 11주 시장가 매수",
            "source": "discord",
            "source_message_id": "web:authority:2",
            "actor_id": "user-b",
            "actor_type": "system",
            "fund_id": "fund-b",
            "book_id": "book-b",
            "mirrored": True,
        }

        for store_name, factory in (
            ("memory", InMemoryMirrorStore),
            ("redis", _fake_redis_mirror_store),
        ):
            for field, changed_value in variants.items():
                with self.subTest(store=store_name, field=field):
                    store = factory()
                    original = CanonicalIngress(**original_values)
                    store.claim_request(original)
                    rebound = CanonicalIngress(
                        **(original_values | {field: changed_value})
                    )
                    with self.assertRaises(MirrorRequestConflict):
                        store.claim_request(rebound)

    def test_discord_runs_ceo_once_and_bot_echo_is_ignored(self) -> None:
        calls: list[str] = []

        with patch.object(
            ceo_mirror_api,
            "_ceo_query",
            side_effect=lambda request, **_: (
                calls.append(request.request_id) or {"task_id": "t_discord"}
            ),
        ):
            accepted = ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query="discord query",
                    request_id="discord:111111111111111111",
                    source="discord",
                    source_message_id="111111111111111111",
                    actor_id="discord-user",
                    discord_channel_id="channel-1",
                    discord_message_id="111111111111111111",
                ),
                _http_request(internal_discord=True),
            )
            ignored = ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query="discord mirror",
                    request_id="discord:222222222222222222",
                    source="discord",
                    source_message_id="222222222222222222",
                    actor_id="ceo-agent",
                    actor_type="bot",
                    mirrored=True,
                    discord_channel_id="channel-1",
                    discord_message_id="222222222222222222",
                ),
                _http_request(internal_discord=True),
            )

        self.assertEqual(calls, ["discord:111111111111111111"])
        self.assertEqual(accepted.task_id, "t_discord")
        self.assertTrue(ignored.ignored)
        self.assertEqual(ignored.reason, "bot_mirror_ignored")

    def test_discord_ingress_rejects_missing_reply_coordinates(self) -> None:
        with (
            patch.object(ceo_mirror_api, "_ceo_query") as ceo_query,
            self.assertRaises(ceo_mirror_api.HTTPException) as raised,
        ):
            ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query="discord query",
                    request_id="discord:333333333333333333",
                    source="discord",
                    source_message_id="333333333333333333",
                    actor_id="discord-user",
                ),
                _http_request(internal_discord=True),
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(
            raised.exception.detail["code"],
            "discord_correlation_metadata_required",
        )
        ceo_query.assert_not_called()

    def test_discord_ingress_rejects_request_id_not_bound_to_message(self) -> None:
        with (
            patch.object(ceo_mirror_api, "_ceo_query") as ceo_query,
            self.assertRaises(ceo_mirror_api.HTTPException) as raised,
        ):
            ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query="discord query",
                    request_id="request-without-discord-binding",
                    source="discord",
                    source_message_id="444444444444444444",
                    actor_id="discord-user",
                    discord_channel_id="channel-1",
                    discord_message_id="444444444444444444",
                ),
                _http_request(internal_discord=True),
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(
            raised.exception.detail["code"],
            "discord_correlation_request_id_mismatch",
        )
        ceo_query.assert_not_called()

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

    def test_mirror_ask_forwards_fund_and_book_to_ceo_query(self) -> None:
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
                    book_id="book-abc",
                ),
                x_source_message_id=None,
                x_actor_id=None,
                owner_id=None,
            )

        self.assertEqual(response["task_id"], "t_mandate")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].fund_id, "fund-abc")
        self.assertEqual(captured[0].book_id, "book-abc")

    def test_central_router_sends_strategy_queries_to_the_lab_for_web_and_discord(
        self,
    ) -> None:
        from apps.api.ceo import CeoAsk

        accepted = ceo_mirror_api.StrategyResearchAccepted(
            request_id="strategy-central-1",
            lab_id="strategy-central-1",
            message="연구실에 등록했습니다.",
            status_url="/ui/strategy-research/requests/strategy-central-1",
        )
        with (
            patch.object(
                ceo_mirror_api, "accept_strategy_research_query", return_value=accepted
            ) as admit,
            patch.object(ceo_mirror_api, "_ceo_query") as ceo_query,
        ):
            web = ceo_mirror_api.mirror_ask(
                CeoAsk(
                    query="코스피 단기 반전 전략을 연구하고 백테스트해줘",
                    request_id="central-web-1",
                ),
                x_source_message_id=None,
                x_actor_id=None,
                owner_id=None,
            )
            discord = ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query="Find and backtest a robust momentum strategy",
                    request_id="discord:555555555555555555",
                    source="discord",
                    source_message_id="555555555555555555",
                    actor_id="discord-user",
                    discord_channel_id="channel-1",
                    discord_message_id="555555555555555555",
                ),
                _http_request(internal_discord=True),
            )

        self.assertEqual(web["schema_version"], "autonomous-research-request.v1")
        self.assertEqual(
            discord.ceo["schema_version"], "autonomous-research-request.v1"
        )
        self.assertEqual(admit.call_count, 2)
        self.assertEqual(ceo_query.call_count, 0)
        self.assertTrue(
            admit.call_args_list[0].kwargs["request_id"].startswith("strategy-")
        )
        self.assertEqual(
            admit.call_args_list[1].kwargs["discord_channel_id"], "channel-1"
        )
        self.assertEqual(
            admit.call_args_list[1].kwargs["discord_message_id"], "555555555555555555"
        )

    def test_explicit_quant_strategy_queries_stay_on_quant_pipeline(self) -> None:
        from apps.api.ceo import CeoAsk

        response = {"task_id": "t_quant_precedence", "status": "accepted"}
        query = (
            "Quant 부서에서 069500.KS 전략의 수익률·변동성·샤프·MDD를 검증하고 "
            "주문·승격 없이 HOLD 여부를 판단해줘"
        )
        with (
            patch.object(ceo_mirror_api, "_strategy_query") as strategy_query,
            patch.object(
                ceo_mirror_api, "_ceo_query", return_value=response
            ) as ceo_query,
        ):
            web = ceo_mirror_api.mirror_ask(
                CeoAsk(query=query, request_id="quant-precedence-web-1"),
                x_source_message_id=None,
                x_actor_id=None,
                owner_id=None,
            )
            discord = ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query=query,
                    request_id="discord:quant-precedence-1",
                    source="discord",
                    source_message_id="quant-precedence-1",
                    actor_id="discord-user",
                    discord_channel_id="channel-quant",
                    discord_message_id="quant-precedence-1",
                ),
                _http_request(internal_discord=True),
            )

        self.assertEqual(web["task_id"], "t_quant_precedence")
        self.assertEqual(discord.task_id, "t_quant_precedence")
        self.assertEqual(strategy_query.call_count, 0)
        self.assertEqual(ceo_query.call_count, 2)

    def test_central_router_sends_human_strategy_deployment_to_release_boundary(
        self,
    ) -> None:
        from apps.api.ceo import CeoAsk

        accepted = ceo_mirror_api.StrategyDeploymentAccepted(
            request_id="strategy-central-2",
            deployment_id="deployment-central-2",
            mode="paper",
            symbols=["000660"],
            status="REVIEW_REQUIRED",
            research_status="COMPLETED",
            message="배포 요청을 릴리스 게이트에 등록했습니다.",
        )
        with (
            patch.object(
                ceo_mirror_api,
                "request_strategy_deployment_from_text",
                return_value=accepted,
            ) as deploy,
            patch.object(ceo_mirror_api, "_ceo_query") as ceo_query,
        ):
            web = ceo_mirror_api.mirror_ask(
                CeoAsk(
                    query="하이닉스 전략 배포해줘",
                    request_id="central-deploy-web-1",
                ),
                x_source_message_id=None,
                x_actor_id=None,
                owner_id="user-a",
            )
            discord = ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query="하이닉스 전략 배포해줘",
                    request_id="discord:deploy-555555555555555555",
                    source="discord",
                    source_message_id="deploy-555555555555555555",
                    actor_id="discord-user",
                    discord_channel_id="channel-1",
                    discord_message_id="deploy-555555555555555555",
                ),
                _http_request(internal_discord=True),
            )

        self.assertEqual(web["schema_version"], "autonomous-strategy-deployment.v1")
        self.assertEqual(
            discord.ceo["schema_version"], "autonomous-strategy-deployment.v1"
        )
        self.assertEqual(deploy.call_count, 2)
        self.assertEqual(ceo_query.call_count, 0)

    def test_central_router_sends_explicit_strategy_approval_to_approval_boundary(
        self,
    ) -> None:
        from apps.api.ceo import CeoAsk

        accepted = ceo_mirror_api.StrategyDeploymentAccepted(
            request_id="strategy-central-approval-1",
            deployment_id="deployment-111111111111111111111111",
            mode="paper",
            symbols=["000660"],
            status="ACTIVE",
            research_status="CANDIDATE",
            approval_required=True,
            approved_by="user-a",
            runtime_status="RUNNING",
            execution_status="SIGNAL_ONLY",
            message="PAPER 컨테이너가 실행 중입니다.",
        )
        with (
            patch.object(
                ceo_mirror_api,
                "approve_strategy_deployment_from_text",
                return_value=accepted,
            ) as approve,
            patch.object(
                ceo_mirror_api, "request_strategy_deployment_from_text"
            ) as deploy,
            patch.object(ceo_mirror_api, "_ceo_query") as ceo_query,
        ):
            web = ceo_mirror_api.mirror_ask(
                CeoAsk(
                    query="전략 배포 승인해줘 deployment-111111111111111111111111",
                    request_id="central-approval-web-1",
                ),
                x_source_message_id=None,
                x_actor_id=None,
                owner_id="user-a",
            )
            discord = ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query="전략 배포 승인해줘 deployment-111111111111111111111111",
                    request_id="discord:approval-555555555555555555",
                    source="discord",
                    source_message_id="approval-555555555555555555",
                    actor_id="discord-user",
                    discord_channel_id="channel-1",
                    discord_message_id="approval-555555555555555555",
                ),
                _http_request(internal_discord=True),
            )

        self.assertEqual(web["status"], "ACTIVE")
        self.assertEqual(discord.reason, "strategy_deployment_approved")
        self.assertEqual(approve.call_count, 2)
        self.assertEqual(deploy.call_count, 0)
        self.assertEqual(ceo_query.call_count, 0)

    def test_central_router_sends_top_level_strategy_exception_to_same_approval_boundary(
        self,
    ) -> None:
        from apps.api.ceo import CeoAsk

        accepted = ceo_mirror_api.StrategyDeploymentAccepted(
            request_id="strategy-central-override-1",
            deployment_id="deployment-333333333333333333333333",
            mode="paper",
            symbols=["000660"],
            status="ACTIVE",
            research_status="COMPLETED",
            override_review_required=True,
            approval_required=True,
            approved_by="user-a",
            runtime_status="RUNNING",
            execution_status="SIGNAL_ONLY",
            message="최상위 사람 예외 승인으로 PAPER 컨테이너가 실행 중입니다.",
        )
        query = "하이닉스 전략 배포 예외 승인해줘 deployment-333333333333333333333333"
        with (
            patch.object(
                ceo_mirror_api,
                "approve_strategy_deployment_from_text",
                return_value=accepted,
            ) as approve,
            patch.object(
                ceo_mirror_api, "request_strategy_deployment_from_text"
            ) as deploy,
            patch.object(ceo_mirror_api, "_ceo_query") as ceo_query,
        ):
            web = ceo_mirror_api.mirror_ask(
                CeoAsk(query=query, request_id="central-override-web-1"),
                x_source_message_id=None,
                x_actor_id=None,
                owner_id="user-a",
            )
            discord = ceo_mirror_api.mirror_ingress(
                CanonicalIngress(
                    query=query,
                    request_id="discord:override-555555555555555555",
                    source="discord",
                    source_message_id="override-555555555555555555",
                    actor_id="discord-user",
                    discord_channel_id="channel-1",
                    discord_message_id="override-555555555555555555",
                ),
                _http_request(internal_discord=True),
            )

        self.assertTrue(web["override_review_required"])
        self.assertTrue(discord.ceo["override_review_required"])
        self.assertEqual(approve.call_count, 2)
        self.assertEqual(deploy.call_count, 0)
        self.assertEqual(ceo_query.call_count, 0)

    def test_central_router_sends_strategy_container_power_command_to_lifecycle_boundary(
        self,
    ) -> None:
        from apps.api.ceo import CeoAsk

        accepted = ceo_mirror_api.StrategyDeploymentAccepted(
            request_id="strategy-central-power-1",
            deployment_id="deployment-222222222222222222222222",
            mode="paper",
            symbols=["000660"],
            status="PAUSED",
            research_status="CANDIDATE",
            runtime_status="STOPPED",
            execution_status="SIGNAL_ONLY",
            message="PAPER 컨테이너를 중지했습니다.",
        )
        with (
            patch.object(
                ceo_mirror_api,
                "power_strategy_deployment_from_text",
                return_value=accepted,
            ) as power,
            patch.object(ceo_mirror_api, "_ceo_query") as ceo_query,
        ):
            response = ceo_mirror_api.mirror_ask(
                CeoAsk(
                    query="하이닉스 전략 컨테이너 중지해줘 deployment-222222222222222222222222",
                    request_id="central-power-web-1",
                ),
                x_source_message_id=None,
                x_actor_id=None,
                owner_id="user-a",
            )

        self.assertEqual(response["status"], "PAUSED")
        self.assertEqual(power.call_count, 1)
        self.assertEqual(ceo_query.call_count, 0)

    def test_non_binding_ceo_query_forwards_source_to_ceo_boundary(self) -> None:
        response = {
            "task_id": "t_trace_root",
            "status": "planned",
            "binding": False,
            "planning": {"workflow_mode": "analysis"},
        }
        with (
            patch.object(ceo_mirror_api, "resolve_discord_actor", return_value=None),
            patch("apps.api.ceo.ceo_query", return_value=response) as ceo_query,
        ):
            actual = ceo_mirror_api._ceo_query(
                CanonicalIngress(
                    query="분석 질의",
                    request_id="discord:trace-root-1",
                    source="discord",
                    source_message_id="trace-root-1",
                    actor_id="discord-user",
                )
            )

        self.assertIs(actual, response)
        forwarded = ceo_query.call_args.args[0]
        self.assertEqual(forwarded.source, "discord")

    def test_bff_forwards_the_shared_deterministic_route_to_ceo_boundary(self) -> None:
        response = {"task_id": "t_deterministic_route", "status": "accepted"}
        with (
            patch.object(ceo_mirror_api, "post_question", return_value=None),
            patch("apps.api.ceo.ceo_query", return_value=response) as ceo_query,
        ):
            actual = ceo_mirror_api._ceo_query(
                CanonicalIngress(
                    query="삼성전자 시장 위험을 분석해줘",
                    request_id="request-deterministic-route",
                    source="web",
                    source_message_id="web:deterministic-route",
                    actor_id="web-user",
                )
            )

        self.assertIs(actual, response)
        route = ceo_query.call_args.kwargs["deterministic_routing_plan"]
        self.assertEqual(
            route["selected_primary_profiles"],
            ("research-department", "risk-management"),
        )
        self.assertEqual(route["producer"], "portfolio-bff-deterministic")
        self.assertEqual(route["routing_basis"], "bounded_query_intent_router")

    def test_ceo_query_carries_previous_question_context_in_request(self) -> None:
        response = {"task_id": "t_context", "status": "planned", "binding": False}
        with (
            patch.object(ceo_mirror_api, "resolve_discord_actor", return_value=None),
            patch("apps.api.ceo.ceo_query", return_value=response) as ceo_query,
        ):
            actual = ceo_mirror_api._ceo_query(
                CanonicalIngress(
                    query="이어서",
                    request_id="discord:context-message-1",
                    source="discord",
                    source_message_id="context-message-1",
                    actor_id="discord-user",
                    previous_question_context="삼성전자 리스크를 분석해줘",
                    previous_question_context_source_message_id="starter-message-1",
                )
            )

        self.assertIs(actual, response)
        forwarded = ceo_query.call_args.args[0]
        self.assertEqual(
            forwarded.previous_question_context, "삼성전자 리스크를 분석해줘"
        )
        self.assertEqual(
            forwarded.previous_question_context_source_message_id,
            "starter-message-1",
        )
        self.assertNotIn("previous_question_context", ceo_query.call_args.kwargs)
        self.assertEqual(
            ceo_query.call_args.kwargs["deterministic_routing_plan"]["routing_basis"],
            "previous_question_context",
        )

    def test_binding_ceo_query_does_not_emit_langsmith_root_trace(self) -> None:
        response = {
            "task_id": "order-root",
            "status": "accepted",
            "binding": True,
        }
        with (
            patch.object(ceo_mirror_api, "resolve_discord_actor", return_value=None),
            patch("apps.api.ceo.ceo_query", return_value=response),
            patch("orchestration.llm_observability.publish_root_trace") as publish,
        ):
            actual = ceo_mirror_api._ceo_query(
                CanonicalIngress(
                    query="주문 실행 요청",
                    request_id="discord:trace-binding-1",
                    source="discord",
                    source_message_id="trace-binding-1",
                    actor_id="discord-user",
                )
            )

        self.assertIs(actual, response)
        publish.assert_not_called()

    def test_web_scope_resolves_one_active_trading_book_when_book_is_omitted(
        self,
    ) -> None:
        from apps.api.ceo import CeoAsk

        user_id = str(uuid4())
        fund_id = str(uuid4())
        book_id = str(uuid4())
        captured: list[CeoAsk] = []

        def fake_ceo_query(
            req: CeoAsk, owner_id: str | None = None, **_coordinates: object
        ) -> dict[str, object]:
            del owner_id
            captured.append(req)
            return {"task_id": "t_web_order", "status": "accepted"}

        ingress = CanonicalIngress(
            query="삼성전자 분봉 120일선 돌파시 매수해줘",
            request_id="request-web-book-fallback",
            source="web",
            source_message_id="request-web-book-fallback",
            actor_id=user_id,
            actor_type="user",
            fund_id=fund_id,
        )
        with (
            patch.object(
                ceo_mirror_api,
                "authorized_trading_books",
                return_value=[{"fund_id": fund_id, "book_id": book_id, "name": "MAIN"}],
            ),
            patch.object(ceo_mirror_api, "post_question", return_value=None),
            patch("apps.api.ceo.ceo_query", side_effect=fake_ceo_query),
        ):
            response = ceo_mirror_api._ceo_query(ingress)

        self.assertEqual(response["task_id"], "t_web_order")
        self.assertEqual(captured[0].fund_id, fund_id)
        self.assertEqual(captured[0].book_id, book_id)

    def test_discord_actor_mapping_resolves_one_active_trading_book(self) -> None:
        """A Discord author can enter the same exact PAPER account boundary."""

        from apps.api.ceo import CeoAsk

        user_id = str(uuid4())
        fund_id = str(uuid4())
        book_id = str(uuid4())
        captured: list[tuple[CeoAsk, str | None]] = []

        def fake_ceo_query(
            req: CeoAsk, owner_id: str | None = None, **_coordinates: object
        ) -> dict[str, object]:
            captured.append((req, owner_id))
            return {"task_id": "t_discord_order", "status": "accepted"}

        ingress = CanonicalIngress(
            query="삼성전자 2주 시장가 매수해",
            request_id="discord:123456789012345678",
            source="discord",
            source_message_id="123456789012345678",
            actor_id="123456789012345678",
            actor_type="user",
        )
        with (
            patch.object(
                ceo_mirror_api,
                "resolve_discord_actor",
                return_value=SimpleNamespace(user_id=user_id, fund_id=fund_id),
            ),
            patch.object(
                ceo_mirror_api,
                "authorized_trading_books",
                return_value=[{"fund_id": fund_id, "book_id": book_id, "name": "MAIN"}],
            ),
            patch("apps.api.ceo.ceo_query", side_effect=fake_ceo_query),
        ):
            response = ceo_mirror_api._ceo_query(ingress)

        self.assertEqual(response["task_id"], "t_discord_order")
        self.assertEqual(len(captured), 1)
        request, owner = captured[0]
        self.assertEqual(owner, user_id)
        self.assertEqual(request.fund_id, fund_id)
        self.assertEqual(request.book_id, book_id)

    def test_discord_order_does_not_guess_between_multiple_books(self) -> None:
        from apps.api.ceo import CeoAsk

        user_id = str(uuid4())
        fund_id = str(uuid4())
        captured: list[CeoAsk] = []

        def fake_ceo_query(
            req: CeoAsk, owner_id: str | None = None, **_coordinates: object
        ) -> dict[str, object]:
            del owner_id
            captured.append(req)
            return {"task_id": "t_ambiguous", "status": "accepted"}

        ingress = CanonicalIngress(
            query="삼성전자 2주 시장가 매수해",
            request_id="discord:223456789012345678",
            source="discord",
            source_message_id="223456789012345678",
            actor_id="223456789012345678",
            actor_type="user",
        )
        with (
            patch.object(
                ceo_mirror_api,
                "resolve_discord_actor",
                return_value=SimpleNamespace(user_id=user_id, fund_id=fund_id),
            ),
            patch.object(
                ceo_mirror_api,
                "authorized_trading_books",
                return_value=[
                    {"fund_id": fund_id, "book_id": str(uuid4()), "name": "A"},
                    {"fund_id": fund_id, "book_id": str(uuid4()), "name": "B"},
                ],
            ),
            patch("apps.api.ceo.ceo_query", side_effect=fake_ceo_query),
        ):
            ceo_mirror_api._ceo_query(ingress)

        self.assertIsNone(captured[0].book_id)

    def test_discord_account_scope_ignores_caller_supplied_fund_and_book(self) -> None:
        """Only the server-owned actor binding may choose Discord account scope."""

        from apps.api.ceo import CeoAsk

        user_id = str(uuid4())
        mapped_fund_id = str(uuid4())
        mapped_book_id = str(uuid4())
        captured: list[CeoAsk] = []

        def fake_ceo_query(
            req: CeoAsk, owner_id: str | None = None, **_coordinates: object
        ) -> dict[str, object]:
            del owner_id
            captured.append(req)
            return {"task_id": "t_bound_scope", "status": "accepted"}

        ingress = CanonicalIngress(
            query="삼성전자 2주 시장가 매수해",
            request_id="discord:323456789012345678",
            source="discord",
            source_message_id="323456789012345678",
            actor_id="123456789012345678",
            actor_type="user",
            fund_id=str(uuid4()),
            book_id=str(uuid4()),
        )
        with (
            patch.object(
                ceo_mirror_api,
                "resolve_discord_actor",
                return_value=SimpleNamespace(user_id=user_id, fund_id=mapped_fund_id),
            ),
            patch.object(
                ceo_mirror_api,
                "authorized_trading_books",
                return_value=[
                    {
                        "fund_id": mapped_fund_id,
                        "book_id": mapped_book_id,
                        "name": "MAIN",
                    }
                ],
            ),
            patch("apps.api.ceo.ceo_query", side_effect=fake_ceo_query),
        ):
            ceo_mirror_api._ceo_query(ingress)

        self.assertEqual(captured[0].fund_id, mapped_fund_id)
        self.assertEqual(captured[0].book_id, mapped_book_id)

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
                owner_id=None,
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
                owner_id="user-a",
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
                owner_id=None,
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
