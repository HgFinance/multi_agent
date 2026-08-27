from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestration.discord_idempotency import (
    DiscordIdempotencyStore,
    canonical_discord_dedup_key,
)


class DiscordIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = DiscordIdempotencyStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claim(self, message_id: str, profile: str = "hr-department") -> bool:
        result = self.store.claim_inbound(
            dedup_key=canonical_discord_dedup_key("guild", "channel", message_id),
            message_id=message_id,
            guild_id="guild",
            channel_id="channel",
            thread_id=None,
            profile=profile,
            handler="live",
        )
        return result.admitted

    def test_same_message_id_is_claimed_once(self) -> None:
        self.assertTrue(self._claim("m-1"))
        self.assertFalse(self._claim("m-1"))

    def test_session_correlation_round_trips_in_existing_inbound_ledger(self) -> None:
        key = canonical_discord_dedup_key("guild", "channel", "m-session")
        result = self.store.claim_inbound(
            dedup_key=key,
            message_id="m-session",
            guild_id="guild",
            channel_id="channel",
            thread_id="thread",
            profile="ceo-agent",
            handler="live",
            session_id="session-1",
        )

        self.assertTrue(result.admitted)
        self.assertEqual(
            self.store.inbound_key_for_session("session-1", "ceo-agent"),
            key,
        )
        self.assertEqual(
            self.store.inbound_context(key, "ceo-agent"),
            {
                "guild_id": "guild",
                "channel_id": "channel",
                "thread_id": "thread",
                "message_id": "m-session",
                "session_id": "session-1",
            },
        )

    def test_session_lookup_is_profile_local_and_exact(self) -> None:
        key = canonical_discord_dedup_key("guild", "channel", "m-session-2")
        self.store.claim_inbound(
            dedup_key=key,
            message_id="m-session-2",
            guild_id="guild",
            channel_id="channel",
            thread_id=None,
            profile="ceo-agent",
            handler="live",
            session_id="session-2",
        )

        self.assertIsNone(
            self.store.inbound_key_for_session("session-2", "qa-department")
        )
        self.assertIsNone(
            self.store.inbound_key_for_session("session-2-other", "ceo-agent")
        )

    def test_existing_ledger_is_migrated_in_place_for_session_correlation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            gateway = home / "gateway"
            gateway.mkdir()
            path = gateway / "discord_message_recovery.db"
            # sqlite3.Connection's context manager commits/rolls back but does
            # not close the handle.  Keep Windows/OneDrive test cleanup from
            # racing an open database file by owning the close explicitly.
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE discord_idempotency_inbound (
                        dedup_key TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        guild_id TEXT,
                        channel_id TEXT,
                        thread_id TEXT,
                        profile TEXT NOT NULL,
                        handler TEXT NOT NULL,
                        state TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (profile, dedup_key)
                    )
                    """
                )

            store = DiscordIdempotencyStore(home)
            key = canonical_discord_dedup_key("guild", "channel", "m-migrated")
            self.assertTrue(
                store.claim_inbound(
                    dedup_key=key,
                    message_id="m-migrated",
                    guild_id="guild",
                    channel_id="channel",
                    thread_id=None,
                    profile="ceo-agent",
                    handler="live",
                    session_id="session-migrated",
                ).admitted
            )
            self.assertEqual(
                store.inbound_key_for_session("session-migrated", "ceo-agent"),
                key,
            )

    def test_existing_outbound_ledger_backfills_indexed_source_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            gateway = home / "gateway"
            gateway.mkdir()
            path = gateway / "discord_message_recovery.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    """CREATE TABLE discord_idempotency_outbound (
                    response_key TEXT NOT NULL, dedup_key TEXT NOT NULL,
                    profile TEXT NOT NULL, state TEXT NOT NULL,
                    response_message_id TEXT, attempts INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL, PRIMARY KEY (profile, response_key))"""
                )
                conn.execute(
                    """INSERT INTO discord_idempotency_outbound
                    VALUES (?, ?, ?, 'COMPLETED', ?, 1, ?)""",
                    (
                        "discord:g:thread:message-old:synthesis-detail:t_old",
                        "discord:g:thread:message-old",
                        "ceo-agent",
                        "reply-old",
                        "2026-08-25T00:00:00+00:00",
                    ),
                )
                conn.commit()

            store = DiscordIdempotencyStore(home)
            store.outbound_message_id(
                "discord:g:thread:message-old:synthesis-detail:t_old",
                "ceo-agent",
            )
            with closing(sqlite3.connect(path)) as conn:
                row = conn.execute(
                    "SELECT source_message_id FROM discord_idempotency_outbound"
                ).fetchone()
            self.assertEqual(row[0], "message-old")

    def test_history_backfill_and_live_delivery_share_claim(self) -> None:
        key = canonical_discord_dedup_key("guild", "channel", "m-backfill")
        self.assertTrue(
            self.store.claim_inbound(
                dedup_key=key,
                message_id="m-backfill",
                guild_id="guild",
                channel_id="channel",
                thread_id=None,
                profile="research-department",
                handler="history_backfill",
            ).admitted
        )
        self.assertFalse(
            self.store.claim_inbound(
                dedup_key=key,
                message_id="m-backfill",
                guild_id="guild",
                channel_id="channel",
                thread_id=None,
                profile="research-department",
                handler="live",
            ).admitted
        )

    def test_concurrent_same_message_id_is_claimed_once(self) -> None:
        home = Path(self._tmp.name)

        def claim_from_independent_store(_: int) -> bool:
            return DiscordIdempotencyStore(home).claim_inbound(
                dedup_key=canonical_discord_dedup_key("guild", "channel", "m-2"),
                message_id="m-2",
                guild_id="guild",
                channel_id="channel",
                thread_id=None,
                profile="hr-department",
                handler="live",
            ).admitted

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim_from_independent_store, range(8)))
        self.assertEqual(sum(results), 1)

    def test_same_content_with_different_message_ids_is_not_deduped(self) -> None:
        self.assertTrue(self._claim("m-3"))
        self.assertTrue(self._claim("m-4"))

    def test_profiles_have_independent_ledgers(self) -> None:
        key = canonical_discord_dedup_key("guild", "channel", "m-5")
        first = self.store.claim_inbound(
            dedup_key=key,
            message_id="m-5",
            guild_id="guild",
            channel_id="channel",
            thread_id=None,
            profile="qa-department",
            handler="live",
        )
        # Profile is metadata, not a cross-profile key. Profile isolation is
        # provided by the profile-scoped Hermes home/database.
        second = self.store.claim_inbound(
            dedup_key=key,
            message_id="m-5",
            guild_id="guild",
            channel_id="channel",
            thread_id=None,
            profile="qa-department",
            handler="live",
        )
        self.assertTrue(first.admitted)
        self.assertFalse(second.admitted)

        # A multiplexed Hermes home may contain multiple profiles. Their
        # claims must remain independent even when Discord metadata matches.
        other_profile = self.store.claim_inbound(
            dedup_key=key,
            message_id="m-5",
            guild_id="guild",
            channel_id="channel",
            thread_id=None,
            profile="research-department",
            handler="live",
        )
        self.assertTrue(other_profile.admitted)

        other_home = tempfile.TemporaryDirectory()
        try:
            other_store = DiscordIdempotencyStore(Path(other_home.name))
            self.assertTrue(
                other_store.claim_inbound(
                    dedup_key=key,
                    message_id="m-5",
                    guild_id="guild",
                    channel_id="channel",
                    thread_id=None,
                    profile="other-profile",
                    handler="live",
                ).admitted
            )
        finally:
            other_home.cleanup()

    def test_final_response_is_published_once(self) -> None:
        key = canonical_discord_dedup_key("guild", "channel", "m-6")
        self.assertTrue(
            self.store.claim_inbound(
                dedup_key=key,
                message_id="m-6",
                guild_id="guild",
                channel_id="channel",
                thread_id=None,
                profile="qa-department",
                handler="live",
            ).admitted
        )
        response_key = f"{key}:final"
        self.assertTrue(
            self.store.claim_outbound(
                response_key=response_key,
                dedup_key=key,
                profile="qa-department",
            ).admitted
        )
        self.store.mark_outbound(response_key, "COMPLETED", "qa-department", "reply-1")
        duplicate = self.store.claim_outbound(
            response_key=response_key,
            dedup_key=key,
            profile="qa-department",
        )
        self.assertFalse(duplicate.admitted)
        self.assertTrue(duplicate.dedup_hit)
        self.assertEqual(duplicate.response_message_id, "reply-1")

    def test_latest_completed_response_uses_parent_thread_correlation(self) -> None:
        key = canonical_discord_dedup_key("guild", "parent", "m-answer")
        self.store.claim_inbound(
            dedup_key=key,
            message_id="m-answer",
            guild_id="guild",
            channel_id="parent",
            thread_id="thread",
            profile="ceo-agent",
            handler="live",
        )
        response_key = "discord:guild:thread:m-answer:synthesis-detail:t_synth"
        self.store.claim_outbound(
            response_key=response_key,
            dedup_key="discord:guild:thread:m-answer",
            profile="ceo-agent",
        )
        self.store.mark_outbound(
            response_key,
            "COMPLETED",
            "ceo-agent",
            "reply-previous",
        )

        self.assertEqual(
            self.store.latest_completed_response(
                profile="ceo-agent",
                guild_id="guild",
                channel_id="parent",
            ),
            ("reply-previous", "thread"),
        )

    def test_failed_claim_is_bounded(self) -> None:
        key = canonical_discord_dedup_key("guild", "channel", "m-7")
        self.assertTrue(
            self.store.claim_inbound(
                dedup_key=key,
                message_id="m-7",
                guild_id="guild",
                channel_id="channel",
                thread_id=None,
                profile="research-department",
                handler="live",
            ).admitted
        )

    def test_stale_reconciliation_repairs_only_proven_delivery(self) -> None:
        now = datetime.now(timezone.utc)
        completed_key = canonical_discord_dedup_key(
            "guild", "channel", "m-reconcile-completed"
        )
        expired_key = canonical_discord_dedup_key(
            "guild", "channel", "m-reconcile-expired"
        )
        for key, message_id in (
            (completed_key, "m-reconcile-completed"),
            (expired_key, "m-reconcile-expired"),
        ):
            self.store.claim_inbound(
                dedup_key=key,
                message_id=message_id,
                guild_id="guild",
                channel_id="channel",
                thread_id=None,
                profile="qa-department",
                handler="qa_feedback_agent",
            )
            self.store.mark_inbound(key, "PROCESSING", "qa-department")

        response_key = f"{completed_key}:final"
        self.store.claim_outbound(
            response_key=response_key,
            dedup_key=completed_key,
            profile="qa-department",
        )
        self.store.mark_outbound(
            response_key,
            "COMPLETED",
            "qa-department",
            "reply-reconcile",
        )

        stale_at = (now - timedelta(hours=2)).isoformat()
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.execute(
                "UPDATE discord_idempotency_inbound SET updated_at=? "
                "WHERE profile=? AND state='PROCESSING'",
                (stale_at, "qa-department"),
            )
            conn.commit()

        result = self.store.reconcile_stale_inbound(
            profile="qa-department",
            now=now,
        )

        self.assertEqual(result["scanned"], 2)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["expired"], 1)
        self.assertEqual(
            self.store.inbound_state(completed_key, "qa-department"),
            "COMPLETED",
        )
        self.assertEqual(
            self.store.inbound_state(expired_key, "qa-department"),
            "EXPIRED",
        )
        replay = self.store.claim_inbound(
            dedup_key=expired_key,
            message_id="m-reconcile-expired",
            guild_id="guild",
            channel_id="channel",
            thread_id=None,
            profile="qa-department",
            handler="qa_feedback_agent",
        )
        self.assertFalse(replay.admitted)
        self.assertEqual(replay.state, "EXPIRED")
        self.store.mark_inbound(key, "FAILED", "research-department")
        self.assertTrue(
            self.store.claim_inbound(
                dedup_key=key,
                message_id="m-7",
                guild_id="guild",
                channel_id="channel",
                thread_id=None,
                profile="research-department",
                handler="live",
            ).admitted
        )


if __name__ == "__main__":
    unittest.main()
