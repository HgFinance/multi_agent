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
from apps.api import discord_mirror
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

    USER1 = "00000000-0000-4000-8000-00000000cec0"
    FUND1 = "3838f7d6-0c7c-4e54-85f3-316a451e7eeb"
    DISCORD_ID = "123456789012345678"

    def test_mapped_author_resolves_to_the_test_account(self) -> None:
        entry = f"{self.DISCORD_ID}:{self.USER1}:{self.FUND1}"
        with patch.dict(os.environ, {ACTOR_MAP_ENV: entry}):
            binding = resolve_actor(self.DISCORD_ID)

        self.assertIsNotNone(binding)
        self.assertEqual(binding.user_id, self.USER1)
        self.assertEqual(binding.fund_id, self.FUND1)

    def test_unmapped_author_is_left_alone(self) -> None:
        """매핑이 없으면 기본 계정으로 채우지 않는다(개발 원칙 9).

        임의의 계정으로 채우면 그 사람이 정하지 않은 한도가 판단 근거가 된다.
        `None`이면 Mandate 없이 진행하고, 그게 정확한 사실이다.
        """

        entry = f"{self.DISCORD_ID}:{self.USER1}:{self.FUND1}"
        with patch.dict(os.environ, {ACTOR_MAP_ENV: entry}):
            self.assertIsNone(resolve_actor("999999999999999999"))

    def test_malformed_entry_does_not_kill_the_rest(self) -> None:
        """오타 한 줄로 표 전체가 죽지 않는다 - BFF 기동을 막으면 안 된다."""

        raw = f"@홍길동:{self.USER1}:{self.FUND1},{self.DISCORD_ID}:{self.USER1}:{self.FUND1}"
        with patch.dict(os.environ, {ACTOR_MAP_ENV: raw}):
            self.assertIsNotNone(resolve_actor(self.DISCORD_ID))

    def test_unset_map_is_empty_not_an_error(self) -> None:
        with patch.dict(os.environ, {ACTOR_MAP_ENV: ""}):
            self.assertIsNone(resolve_actor(self.DISCORD_ID))


class UserToFundReverseLookupTest(unittest.TestCase):
    """`user_id -> fund_id` 역참조가 매핑표의 fund를 대신한다.

    `governance.fund_memberships`가 0건이던 동안에는 이 조회가 불가능해서
    프론트엔드가 fund를 계정과 쌍으로 하드코딩했다. 2026-08-18 seed로 소유
    관계가 채워지며 서버가 직접 풀 수 있게 됐고, 매핑표의 fund 칸은 선택이 됐다.
    """

    USER1 = "00000000-0000-4000-8000-00000000cec0"

    def test_two_field_entry_leaves_fund_to_the_lookup(self) -> None:
        with patch.dict(os.environ, {ACTOR_MAP_ENV: f"123456789012345678:{self.USER1}"}):
            binding = resolve_actor("123456789012345678")

        self.assertIsNotNone(binding)
        self.assertEqual(binding.user_id, self.USER1)
        self.assertIsNone(binding.fund_id)

    def test_declared_fund_still_wins(self) -> None:
        """3칸으로 적으면 그 값이 역참조보다 우선한다.

        governance-api가 없는 환경을 위한 명시적 우회다 - 적어 놓은 값을 서버
        추론이 덮으면 무엇이 쓰였는지 알 수 없다.
        """

        fund = "3838f7d6-0c7c-4e54-85f3-316a451e7eeb"
        with patch.dict(
            os.environ, {ACTOR_MAP_ENV: f"123456789012345678:{self.USER1}:{fund}"}
        ):
            self.assertEqual(resolve_actor("123456789012345678").fund_id, fund)

    def test_lookup_failure_is_not_an_exception(self) -> None:
        """governance-api가 없으면 `None`이다 - 질의 접수를 막지 않는다.

        `importlib.reload`로 모듈을 다시 읽지 않는다. reload는 그 모듈 객체를
        **프로세스 전역으로** 갈아치워서, 같은 실행 안의 다른 테스트가 들고 있던
        참조까지 함께 바뀐다(실측: tests/api 6건이 이 한 줄 때문에 깨졌다).
        모듈 상수만 잠시 비운다.
        """

        from apps.api import governance_client

        with patch.object(governance_client, "GOVERNANCE_API_URL", ""):
            self.assertIsNone(governance_client.fetch_fund_id_by_user(self.USER1))

class MirrorIsOffDuringTestsTest(unittest.TestCase):
    """테스트 실행 중에는 절대 Discord에 글이 나가지 않는다.

    2026-08-18 사고: 단위 테스트가 `ceo_query`를 부르면 미러 게시가 그대로
    실행됐고, 개발 머신의 환경에 토큰이 있어서 **픽스처 질의 "q"가 실제 팀
    채널에 올라갔다.** 방어선을 둘로 나눈다 - 설정(`DISCORD_MIRROR_ENABLED`)은
    "어느 환경인가"를, 러너 판정은 "지금 테스트 중인가"를 가른다.
    """

    def test_runner_is_detected_in_this_very_process(self) -> None:
        """이 테스트가 도는 프로세스는 러너로 판정돼야 한다.

        판정이 틀리면 이 테스트만 통과하는 게 아니라 **다음 사고가 그대로 다시
        난다.** 그래서 모의값이 아니라 실제 실행 환경을 본다.
        """

        self.assertTrue(discord_mirror.test_runner_active())

    def test_posting_is_blocked_even_when_fully_configured(self) -> None:
        """플래그가 켜지고 토큰·채널이 다 있어도 러너 안에서는 나가지 않는다."""

        with patch.dict(
            os.environ,
            {
                discord_mirror.ENABLED_ENV: "true",
                discord_mirror.TOKEN_ENV: "dummy-token",
                discord_mirror.CHANNEL_ENV: "123456789012345678",
            },
        ):
            self.assertTrue(discord_mirror.mirror_enabled())
            self.assertIsNone(discord_mirror.post_question("이 글은 나가면 안 된다"))

    def test_disabled_by_default(self) -> None:
        """플래그를 안 적으면 꺼짐 - 토큰이 있어도 게시하지 않는다."""

        with patch.dict(
            os.environ,
            {
                discord_mirror.ENABLED_ENV: "",
                discord_mirror.TOKEN_ENV: "dummy-token",
                discord_mirror.CHANNEL_ENV: "123456789012345678",
            },
        ):
            self.assertFalse(discord_mirror.mirror_enabled())


class SourceDecidesWhetherWeRepostTest(unittest.TestCase):
    """Discord에서 온 요청은 다시 게시하지 않는다.

    사용자가 채널에 쓴 원본이 이미 있는데 봇이 같은 내용을 한 번 더 올리면
    원본과 `[web-mirror]` 복사본이 나란히 뜬다. 출처를 아는 곳
    (`ceo_mirror_api._ceo_query`)에서만 게시 여부를 판단한다.
    """

    def test_ceo_query_never_posts_by_itself(self) -> None:
        """`ceo.ceo_query`는 좌표를 받기만 하고 스스로 게시하지 않는다.

        한때 이 함수가 직접 게시해서, 이 함수를 부르는 단위 테스트가 전부 실제
        채널로 나갔다(2026-08-18). 함수 시그니처로 그 구조를 고정한다.
        """

        import inspect

        from apps.api import ceo

        parameters = inspect.signature(ceo.ceo_query).parameters
        for name in ("discord_channel_id", "discord_message_id", "discord_guild_id"):
            self.assertIn(name, parameters)
        # 발송 함수를 이 모듈이 들고 있지 않다는 사실을 본다. 소스 문자열을
        # 훑으면 docstring의 설명까지 걸려서 "왜 그렇게 했는지"를 적을 수 없다.
        self.assertFalse(hasattr(ceo, "post_question"))

    def test_ingress_carries_discord_coordinates(self) -> None:
        """Discord 어댑터가 보낸 좌표가 계약에 있다."""

        from apps.api.ceo_mirror import CanonicalIngress

        ingress = CanonicalIngress(
            query="q",
            source="discord",
            source_message_id="991",
            discord_channel_id="chan-1",
            discord_message_id="991",
            discord_guild_id="guild-1",
        )

        self.assertEqual(ingress.discord_channel_id, "chan-1")
        self.assertEqual(ingress.discord_message_id, "991")


class MirrorLabelTest(unittest.TestCase):
    """요청자 표시. uuid를 그대로 찍으면 채널에서 누가 물었는지 알 수 없다."""

    USER1 = "00000000-0000-4000-8000-00000000cec0"

    def test_mapped_user_renders_as_a_mention(self) -> None:
        with patch.dict(os.environ, {ACTOR_MAP_ENV: f"123456789012345678:{self.USER1}"}):
            content = build_content("q", asked_by=self.USER1)

        self.assertTrue(content.startswith(f"{MIRROR_TAG} <@123456789012345678>"))

    def test_shared_account_falls_back_to_the_uuid(self) -> None:
        """한 계정을 둘이 쓰면 아무나 고르지 않는다 - 남이 물은 것처럼 보인다."""

        shared = f"123456789012345678:{self.USER1},234567890123456789:{self.USER1}"
        with patch.dict(os.environ, {ACTOR_MAP_ENV: shared}):
            content = build_content("q", asked_by=self.USER1)

        self.assertTrue(content.startswith(f"{MIRROR_TAG} {self.USER1}"))


class ThreadIsRequiredForDepartmentDetailTest(unittest.TestCase):
    """스레드가 없으면 부서 진행 상세가 하나도 안 나간다.

    2026-08-18 실측: 미러링된 요청에 부서 작업 내용이 전혀 보이지 않았다.
    `discord_delivery.deliver_to_existing_thread()`가 `thread_id`를 요구하고
    없으면 `status=missing_thread`로 조용히 반환하는데, 미러 게시는 스레드를
    만들지 않았다. 최종 답변(`deliver()`)은 `message_reference` 답글이라
    스레드 없이도 나가므로 "일부만 안 보이는" 상태였다.
    """

    def test_thread_id_round_trips_into_delivery(self) -> None:
        body = build_root_body(
            "q",
            "req-1",
            discord_channel_id="chan-1",
            discord_message_id="msg-1",
            discord_thread_id="thread-1",
        )

        self.assertEqual(correlation_from_task({"body": body}).thread_id, "thread-1")

    def test_thread_is_optional(self) -> None:
        """스레드 생성이 실패해도 좌표는 실린다 - 최종 답변은 나가야 한다."""

        body = build_root_body(
            "q", "req-1", discord_channel_id="chan-1", discord_message_id="msg-1"
        )
        correlation = correlation_from_task({"body": body})

        self.assertIsNone(correlation.thread_id)
        self.assertEqual(correlation.message_id, "msg-1")

    def test_mirror_post_carries_thread_id(self) -> None:
        """`MirrorPost`가 스레드 id를 들고 다녀야 root body까지 전달된다."""

        post = discord_mirror.MirrorPost(
            channel_id="c", message_id="m", guild_id="g", thread_id="t"
        )

        self.assertEqual(post.thread_id, "t")
        self.assertIsNone(
            discord_mirror.MirrorPost(channel_id="c", message_id="m").thread_id
        )


if __name__ == "__main__":
    unittest.main()
