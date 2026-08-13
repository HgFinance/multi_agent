from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

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
