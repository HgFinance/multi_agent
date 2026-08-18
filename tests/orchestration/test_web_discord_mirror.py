"""웹 질의가 Discord 미러 게시물과 이어지는지 고정한다.

이 배선이 없으면 웹에서 물은 질문은 Kanban 카드만 만들고 **Discord에는
아무것도 뜨지 않는다** - 부서 진행 상황도, CEO 최종 답변도. 2026-08-18 이전이
그 상태였다.

이어지는 지점은 딱 하나다: root body의 `discord_channel_id=` /
`discord_message_id=` 두 줄을 `orchestration/discord_delivery.py`의
`correlation_from_task()`가 읽고, `deliver()`가 그 좌표로 발송한다. 그래서
여기서 지키는 것도 그 두 줄이다 - 발송 자체(HTTP)는 이 테스트의 범위가 아니다.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from apps.api.discord_actor_map import ACTOR_MAP_ENV, resolve as resolve_actor
from apps.api.discord_mirror import MIRROR_TAG, build_content
from orchestration.ceo_workflow_scope import build_root_body
from orchestration.discord_delivery import correlation_from_task


class RootBodyCarriesDiscordCorrelationTest(unittest.TestCase):
    def test_delivery_can_read_the_mirror_coordinates_back(self) -> None:
        """`build_root_body`가 쓴 줄을 `correlation_from_task`가 그대로 읽는다.

        두 모듈이 각자 다른 형식을 쓰면(한쪽은 `channel_id=`, 다른 쪽은
        `discord.channel_id`) 조용히 어긋난다 - 카드에는 값이 보이는데 발송은
        `missing_context`로 끝난다. 그래서 쓰는 쪽과 읽는 쪽을 한 테스트에서
        맞물려 본다.
        """

        body = build_root_body(
            "삼성전자 리스크",
            "req-1",
            discord_channel_id="chan-1",
            discord_message_id="msg-1",
            discord_guild_id="guild-1",
        )
        correlation = correlation_from_task({"body": body})

        self.assertEqual(correlation.channel_id, "chan-1")
        self.assertEqual(correlation.message_id, "msg-1")
        self.assertEqual(correlation.guild_id, "guild-1")
        # request_id는 원래 있던 줄에서 계속 읽힌다 - 미러 배선이 그걸 덮지 않는다.
        self.assertEqual(correlation.request_id, "req-1")

    def test_guild_is_optional(self) -> None:
        """guild가 없어도 발송은 된다 - `deliver()`가 'unknown'으로 채운다."""

        body = build_root_body(
            "q", "req-1", discord_channel_id="chan-1", discord_message_id="msg-1"
        )
        correlation = correlation_from_task({"body": body})

        self.assertEqual(correlation.channel_id, "chan-1")
        self.assertEqual(correlation.message_id, "msg-1")
        self.assertIsNone(correlation.guild_id)

    def test_partial_coordinates_are_not_written_at_all(self) -> None:
        """channel만, 또는 message만 있으면 **아무 줄도** 쓰지 않는다.

        `deliver()`는 둘 다 요구한다. 하나만 실으면 카드에는 좌표가 있는 것처럼
        보이는데 발송은 안 되는 상태가 되고, 그 차이를 카드만 봐서는 알 수 없다.
        """

        only_channel = build_root_body("q", "req-1", discord_channel_id="chan-1")
        only_message = build_root_body("q", "req-1", discord_message_id="msg-1")

        for body in (only_channel, only_message):
            self.assertNotIn("discord_channel_id=", body)
            self.assertNotIn("discord_message_id=", body)

    def test_mirror_absent_body_is_unchanged(self) -> None:
        """미러 게시가 실패하면 이 기능이 없던 때와 같은 body가 나온다.

        Discord 장애가 질의 접수까지 막으면 안 되므로, 실패 경로는 '조용히
        예전 동작'이어야 한다.
        """

        self.assertEqual(build_root_body("q", "req-1"), build_root_body("q", "req-1"))
        self.assertNotIn("discord_", build_root_body("q", "req-1"))


class MirrorContentTest(unittest.TestCase):
    def test_question_is_carried_verbatim(self) -> None:
        """질문을 요약하지 않는다 - Discord 이력과 Kanban 카드가 갈라지면 안 된다."""

        content = build_content("삼성전자 리스크 알려줘", asked_by="user-1")

        self.assertIn("삼성전자 리스크 알려줘", content)
        self.assertTrue(content.startswith(f"{MIRROR_TAG} user-1"))

    def test_long_question_is_clipped_and_says_so(self) -> None:
        """Discord 본문 상한(2000자)을 넘기면 자르되, 잘렸다는 사실을 남긴다."""

        content = build_content("가" * 2000, asked_by="user-1")

        self.assertLessEqual(len(content), 2000)
        self.assertTrue(content.endswith("…(잘림)"))



class DiscordActorMappingTest(unittest.TestCase):
    """Discord 작성자 id를 테스트 계정으로 바꾸는 표(`DISCORD_ACTOR_MAP`).

    이 표가 없으면 Discord 질의의 `requested_by=`에 Discord 숫자 id가 박히고
    `fund_id`가 비어 Mandate 스냅샷이 붙지 않는다 - 웹에서 물으면 붙고
    Discord에서 물으면 안 붙는 상태가 된다.
    """

    USER3 = "00000000-0000-4000-8000-00000000cec2"
    FUND3 = "3838f7d6-0c7c-4e54-85f3-316a451e7eeb"
    DISCORD_ID = "123456789012345678"

    def test_mapped_author_resolves_to_the_test_account(self) -> None:
        entry = f"{self.DISCORD_ID}:{self.USER3}:{self.FUND3}"
        with patch.dict(os.environ, {ACTOR_MAP_ENV: entry}):
            binding = resolve_actor(self.DISCORD_ID)

        self.assertIsNotNone(binding)
        self.assertEqual(binding.user_id, self.USER3)
        self.assertEqual(binding.fund_id, self.FUND3)

    def test_unmapped_author_is_left_alone(self) -> None:
        """매핑이 없으면 기본 계정으로 채우지 않는다(개발 원칙 9).

        임의의 계정으로 채우면 그 사람이 정하지 않은 한도가 판단 근거가 된다.
        `None`이면 Mandate 없이 진행하고, 그게 정확한 사실이다.
        """

        entry = f"{self.DISCORD_ID}:{self.USER3}:{self.FUND3}"
        with patch.dict(os.environ, {ACTOR_MAP_ENV: entry}):
            self.assertIsNone(resolve_actor("999999999999999999"))

    def test_malformed_entry_does_not_kill_the_rest(self) -> None:
        """오타 한 줄로 표 전체가 죽지 않는다 - BFF 기동을 막으면 안 된다."""

        raw = f"@홍길동:{self.USER3}:{self.FUND3},{self.DISCORD_ID}:{self.USER3}:{self.FUND3}"
        with patch.dict(os.environ, {ACTOR_MAP_ENV: raw}):
            self.assertIsNotNone(resolve_actor(self.DISCORD_ID))

    def test_unset_map_is_empty_not_an_error(self) -> None:
        with patch.dict(os.environ, {ACTOR_MAP_ENV: ""}):
            self.assertIsNone(resolve_actor(self.DISCORD_ID))

if __name__ == "__main__":
    unittest.main()
