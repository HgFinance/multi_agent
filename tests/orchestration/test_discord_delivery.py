"""CEO synthesis Discord delivery contract tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

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
                    {
                        "channel": channel,
                        "payload": json.loads(payload),
                        "headers": headers,
                    }
                )
                return {"id": "response-message"}

            delivery = DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"}, sender=sender
            )
            task = {
                "root_task": {
                    "body": ("discord_message_id=message\ndiscord_channel_id=channel\n")
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

    def test_user_facing_content_translates_runtime_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_inbound(directory)
            sent: list[dict[str, object]] = []

            def sender(channel: str, payload: str, _headers: dict[str, str]):
                sent.append({"channel": channel, "payload": json.loads(payload)})
                return {"id": "response-message"}

            result = DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"}, sender=sender
            ).deliver(
                root_task_id="root-friendly",
                synthesis_task={
                    "body": ("discord_message_id=message\ndiscord_channel_id=channel\n")
                },
                content=(
                    "Mandate가 없고 Mandate를 확인할 수 없어 위험도는 MODERATE, "
                    "NAV 확인은 HIGH 차단으로 DEFER, 법률 판정은 no_breach, "
                    "담당은 **risk**. 이번 법률 조회는 PAPER만으로는 "
                    "no_breach으로 보았지만 추가 확인이 필요합니다."
                ),
                store=store,
            )

            self.assertEqual(result, "sent")
            self.assertEqual(
                sent[0]["payload"]["content"],
                (
                    "투자지침이 없고 투자지침을 확인할 수 없어 위험도는 보통, "
                    "순자산 가치 확인은 중요 차단 사유로 판단 보류, "
                    "법률 판정은 현재 입력만으로 위반을 확인하지 못함, "
                    "담당은 **리스크 부서**. 이번 법률 조회는 "
                    "법률 위반 여부를 확정할 수 없으며 추가 확인이 필요합니다."
                ),
            )

    def test_user_facing_content_renders_serialized_line_breaks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_inbound(directory)
            sent: list[dict[str, object]] = []

            def sender(channel: str, payload: str, _headers: dict[str, str]):
                sent.append({"channel": channel, "payload": json.loads(payload)})
                return {"id": "response-message"}

            result = DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"}, sender=sender
            ).deliver(
                root_task_id="root-newlines",
                synthesis_task={
                    "body": "discord_message_id=message\ndiscord_channel_id=channel\n"
                },
                content="🧠 CEO 업무 분배\\n\\n리스크 부서 PAPER\\n산술 검토",
                store=store,
            )

            self.assertEqual(result, "sent")
            rendered = sent[0]["payload"]["content"]
            self.assertEqual(
                rendered, "🧠 CEO 업무 분배\n\n리스크 부서 분석용 가상거래\n산술 검토"
            )
            self.assertNotIn("\\n", rendered)

    def test_accounting_content_humanizes_runtime_field_names(self) -> None:
        rendered = DiscordFinalDelivery._humanize_content(
            "Fund f / Book b / "
            "source_of_record=accounting.journals (Supabase) / "
            "quality_status=WARN / authoritative=false/is_official=false / "
            "instrument_id x / Long / Short / advisory snapshot / read-only"
        )

        self.assertIn("펀드", rendered)
        self.assertIn("장부", rendered)
        self.assertIn("자료 기준: 회계 시스템 원장", rendered)
        self.assertIn("자료 품질: 주의", rendered)
        self.assertIn("공식 확정 자료 아님", rendered)
        self.assertNotIn("source_of_record", rendered)
        self.assertNotIn("quality_status", rendered)
        self.assertNotIn("instrument_id", rendered)
        self.assertNotIn("Gross Exposure", rendered)
        self.assertNotIn("authoritative=false", rendered)
        self.assertNotIn("is_official=false", rendered)

    def test_humanize_preserves_one_canonical_retrieval_record(self) -> None:
        rendered = DiscordFinalDelivery._humanize_content(
            "HOLD: 원본 시계열이 없어 검증하지 못했습니다.\n"
            "retrieval_attempt:\n"
            "instrument=069500.KS\n"
            "requested_window=UNSPECIFIED\n"
            "source=LS Securities MCP\n"
            "tr=UNAVAILABLE\n"
            "status=UNAVAILABLE\n"
            "queried_at=UNAVAILABLE\n"
            "extracted_at=UNAVAILABLE\n"
            "snapshot_hash=UNAVAILABLE"
        )

        self.assertEqual(rendered.count("retrieval_attempt:"), 1)
        self.assertIn("instrument=069500.KS", rendered)
        self.assertIn("source=LS Securities MCP", rendered)
        self.assertNotIn("종목=069500.KS", rendered)

        rendered = DiscordFinalDelivery._humanize_content(
            "snapshot as_of=2026-08-27 mark_as_of=2026-08-27 "
            "PnL BREAK, 주요 인용: ls-tr:CSPAQ12200, ls-tr:t0424"
        )
        self.assertIn("조회 자료 기준 시각", rendered)
        self.assertIn("가격 기준 시각", rendered)
        self.assertIn("손익 대사 차이", rendered)
        self.assertIn("조회 근거: 증권사 조회 기록", rendered)
        self.assertNotIn("as_of", rendered)
        self.assertNotIn("mark_as_of", rendered)
        self.assertNotIn("ls-tr:", rendered)

        rendered = DiscordFinalDelivery._humanize_content(
            "Accounting / Portfolio Accounting Engine Strategy / "
            "official NAV close pending, mark_price, fees, taxes, "
            "cash_orderable, receivable, deposit, cross-check, Break"
        )
        self.assertIn("회계·포트폴리오", rendered)
        self.assertIn("회계 시스템", rendered)
        self.assertIn("공식 순자산 가치 확정 보류", rendered)
        self.assertIn("가격", rendered)
        self.assertIn("수수료", rendered)
        self.assertIn("세금", rendered)
        self.assertIn("대사 차이", rendered)
        self.assertNotIn("Accounting Engine", rendered)
        self.assertNotIn("mark_price", rendered)
        self.assertNotIn("cash_orderable", rendered)

    def test_accounting_department_card_is_compact_and_keeps_conclusion(self) -> None:
        source = (
            "📒 **회계·포트폴리오 부서**\n✅ 분석을 완료했습니다.\n\n"
            "범위: Fund f / Book b\n"
            "상태: PRELIMINARY / WARN — official NAV close 전입니다.\n"
            "- Preliminary NAV: KRW 100\n"
            "- 현금: KRW 80\n"
            "- 증권가치: KRW 20\n"
            "- 실현손익: KRW 1\n"
            "- 미실현손익: KRW 2\n"
            "- 삼성전자(005930): t0424 28주 vs t0425 29주, 차이 -1주\n"
            "결론 및 조치 상태: PAPER 읽기 전용이며 공식 NAV 확정과 원장 변경은 수행하지 않았습니다."
            + (" 추가 설명." * 500)
        )

        rendered = DiscordFinalDelivery._humanize_content(source)

        self.assertLessEqual(len(rendered), 1900)
        self.assertIn("### 핵심 수치", rendered)
        self.assertIn("### 결론", rendered)
        self.assertIn("공식 순자산 가치 확정은 수행하지 않았습니다", rendered)
        self.assertNotIn("PRELIMINARY", rendered)
        self.assertNotIn("t0424", rendered)
        self.assertNotIn("reversing/additional entry", rendered)

    def test_accounting_user_surface_humanizes_delegation_and_broker_labels(
        self,
    ) -> None:
        rendered = DiscordFinalDelivery._humanize_content(
            "Review the accounting, liquidity, fee, 순자산 가치, or portfolio-state "
            "implications relevant to the request using 읽기 전용 근거 자료. "
            "Do not mutate a ledger or confirm 순자산 가치. "
            "BROKER_POSITION_TR_MISMATCH: CSPAQ12300 vs t0424"
        )

        self.assertIn("회계·유동성·수수료·순자산 가치·포트폴리오 상태", rendered)
        self.assertIn("브로커 포지션 수량 대사 차이", rendered)
        self.assertIn("손익분기·잔고 조회", rendered)
        self.assertIn("잔고 조회", rendered)
        self.assertNotIn("Review the accounting", rendered)
        self.assertNotIn("BROKER_POSITION_TR_MISMATCH", rendered)
        self.assertNotIn("CSPAQ12300", rendered)
        self.assertNotIn("t0424", rendered)

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

    def test_explicit_message_and_channel_context_sends_without_ledger_row(
        self,
    ) -> None:
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

    def test_session_ledger_context_is_used_when_explicit_message_is_absent(
        self,
    ) -> None:
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

    def test_identical_thread_card_does_not_issue_a_second_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DiscordIdempotencyStore(Path(directory))
            sent: list[dict[str, object]] = []
            edited: list[dict[str, object]] = []

            def sender(channel: str, payload: str, _headers: dict[str, str]):
                sent.append({"channel": channel, "payload": json.loads(payload)})
                return {"id": "card-1"}

            def editor(
                channel: str,
                message_id: str,
                payload: str,
                _headers: dict[str, str],
            ):
                edited.append(
                    {
                        "channel": channel,
                        "message_id": message_id,
                        "payload": json.loads(payload),
                    }
                )
                return {"id": message_id}

            delivery = DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"},
                sender=sender,
                editor=editor,
            )
            task = {
                "body": (
                    "discord_message_id=message\n"
                    "discord_channel_id=channel\n"
                    "discord_thread_id=thread\n"
                )
            }

            self.assertEqual(
                delivery.upsert_thread_card(
                    root_task_id="root-card",
                    source_task=task,
                    root_task=None,
                    content="same card",
                    store=store,
                    profile="qa-department",
                    response_key_suffix="department-card",
                ),
                "created",
            )
            self.assertEqual(
                delivery.upsert_thread_card(
                    root_task_id="root-card",
                    source_task=task,
                    root_task=None,
                    content="same card",
                    store=store,
                    profile="qa-department",
                    response_key_suffix="department-card",
                ),
                "unchanged",
            )

            self.assertEqual(len(sent), 1)
            self.assertEqual(edited, [])

    def test_deleted_existing_thread_uses_parent_fallback_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DiscordIdempotencyStore(Path(directory) / "discord.sqlite3")

            def sender(_channel: str, _payload: str, _headers: dict[str, str]):
                raise HTTPError(
                    "https://discord.invalid/thread", 404, "missing", {}, None
                )

            delivery = DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"},
                sender=sender,
            )
            result = delivery.deliver_to_existing_thread(
                root_task_id="root",
                source_task={
                    "root_task": {
                        "body": (
                            "discord_message_id=message\n"
                            "discord_channel_id=channel\n"
                            "discord_thread_id=deleted-thread\n"
                        )
                    }
                },
                content="department full analysis",
                title="상세 분석",
                store=store,
                profile="qa-department",
                response_key_suffix="department-detail:deleted-thread",
            )

            self.assertEqual(result, "missing_thread")

    def test_department_detail_uses_starter_message_as_existing_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DiscordIdempotencyStore(Path(directory) / "discord.sqlite3")

            sent: list[dict[str, object]] = []

            def sender(channel: str, payload: str, _headers: dict[str, str]):
                sent.append(
                    {
                        "channel": channel,
                        "payload": json.loads(payload),
                    }
                )
                return {"id": "detail-message"}

            delivery = DiscordFinalDelivery(
                environment={"DISCORD_BOT_TOKEN": "test-token"},
                sender=sender,
            )

            result = delivery.deliver_to_existing_thread(
                root_task_id="root",
                source_task={
                    "root_task": {
                        "body": (
                            "discord_message_id=1539153165784584263\n"
                            "discord_channel_id=1536997434507657261\n"
                            "discord_thread_id=\n"
                        )
                    }
                },
                content="department full analysis",
                title="📊 Quant / Backtest 부서 상세 분석",
                store=store,
                profile="quant-backtest-department",
                response_key_suffix="department-detail:test",
            )

            self.assertEqual(result, "sent")
            self.assertEqual(
                sent[0]["channel"],
                "1539153165784584263",
            )


if __name__ == "__main__":
    unittest.main()
