#!/usr/bin/env python3
"""웹에서 들어온 CEO 질의를 Discord 채널에 **거울처럼 게시**한다.

소유: 영주. 짝이 되는 읽기 모듈은 `apps/api/discord_read.py`(발송하지 않는다).

## 왜 읽기 모듈과 나눠 놓나

`discord_read.py`는 머리말에 "발송하지 않는다. GET 두 개만 부른다"를 계약으로
박아 뒀다. 그 파일에 POST를 넣으면 그 계약이 조용히 깨지고, 읽기 전용이라고
믿고 그 모듈을 쓰는 쪽(부서 Inspector 등)이 발송 권한을 함께 얻게 된다. 그래서
쓰기는 이 파일 하나로 모은다 - 발송 코드가 어디 있는지 찾을 곳이 하나다.

## 이 게시물이 하는 두 가지 일

1. **사람이 읽는 이력.** 프론트의 CEO 입력란은 대화를 쌓지 않는다(2026-08-18,
   PR #265 되돌림). 지난 대화는 이 채널이 보관한다.
2. **상관관계 앵커.** 이게 더 중요하다. `orchestration/discord_delivery.py`의
   `deliver()`는 `channel_id`와 `message_id`가 **둘 다** 있어야 동작하고, 없으면
   `missing_context`로 조용히 반환한다. 디스코드에서 들어온 요청은 그 값을
   `gateway_patch.py`가 본문에 심어 주지만 웹 요청에는 없었다 - 그래서 웹 질의는
   부서 진행 상황도 최종 답변도 Discord에 뜨지 않았다.

   여기서 게시한 **그 메시지의 id**를 root task body에 적으면, 이후 부서 진행과
   CEO 최종 답변이 같은 코드 경로로 그 메시지에 붙는다. 즉 미러 게시는 곁다리가
   아니라 웹 경로가 Discord와 이어지는 지점이다.

## 에코 루프

이 게시물의 작성자는 봇이다. Hermes gateway의 admission 판정은
`deploy/hermes-discord/gateway_patch.py`가 원본 정책을 그대로 쓰므로
(주석: "Do not change permission/mention policy"), 봇 메시지를 사람 발화로
집지 않을 **것으로 보인다** - 그러나 그 정책은 이 저장소가 소유하지 않으며,
확인되기 전까지 사실로 취급하지 않는다.

방어선은 셋이다:
  1. `DISCORD_MIRROR_ENABLED`(아래) - 켠다고 적지 않으면 아무 글도 안 쓴다.
  2. `[web-mirror]` 접두어 - 사람이든 코드든 미러를 구분할 수 있다
     (`ceo_mirror.py`의 `mirrored=true`와 같은 취지).
  3. 게시는 **요청 하나당 한 번**이고 답변·진행 상황 발송은 별도 경로
     (`discord_delivery.py`)가 dedup 키로 막는다.

채널이 미러 글로 넘치면 1번을 끄는 것이 즉시 정지 스위치다.

## 실패하면 조용히 넘어간다

Discord가 죽어도 CEO 워크플로는 돌아가야 한다 - Mandate 조회
(`governance_client.fetch_current_mandate_by_fund`)와 같은 정책이다. 이 모듈의
모든 함수는 **예외를 올리지 않고** `None`을 준다. 그 경우 root body에 상관관계
줄이 안 들어가고, 동작은 이 기능이 없던 때와 정확히 같아진다.

자체 점검:
    python apps/api/discord_mirror.py
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx

DISCORD_API = "https://discord.com/api/v10"

# 읽기 모듈과 같은 환경변수를 쓴다. 채널을 따로 두면 "질문은 A 채널, 답변은 B
# 채널"이 되어 이력이 갈라진다.
CHANNEL_ENV = "DISCORD_CEO_CHANNEL_ID"
TOKEN_ENV = "DISCORD_BOT_TOKEN_CEO"

# **명시적 opt-in.** 토큰과 채널이 환경에 있다는 이유만으로 글을 쓰지 않는다.
#
# 왜 필요한가 (2026-08-18 실측 사고): 단위 테스트가 `ceo_query`를 부르면 이
# 모듈이 그대로 실행된다. 테스트 프로세스에 토큰이 들어 있으면(다른 모듈의
# `load_dotenv()` 한 번이면 충분하다) **테스트가 실제 팀 채널에 글을 쓴다.**
# 실제로 그렇게 됐다 - 픽스처 질의 "q"가 채널에 올라갔다.
#
# 그래서 "설정이 있으니 켠다"가 아니라 "켠다고 적어야 켠다"로 뒤집는다. 배포는
# docker-compose.yml에서 명시적으로 true를 준다(개발 원칙 9: 위험한 기능은
# 실패 시 확대가 아니라 차단 방향으로).
ENABLED_ENV = "DISCORD_MIRROR_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# 실행기가 테스트 러너면 켜져 있어도 게시하지 않는다.
#
# `ENABLED_ENV` 하나만으로는 부족하다: 배포와 같은 `.env`를 쓰는 개발 머신에서
# 테스트를 돌리면 플래그가 켜진 채 실행되고, 그대로 팀 채널에 글이 나간다.
# 설정은 "어느 환경인가"를 나누지만 "지금 테스트 중인가"는 나누지 못한다.
#
# **러너 자체만 본다** - `"unittest" in sys.modules` 같은 판정은 쓰지 않는다.
# 그건 어떤 의존성이 unittest를 import하기만 해도 참이 되어, 운영 프로세스에서
# 미러가 조용히 꺼진다. 조용한 비활성화는 조용한 발송만큼 나쁘다.
_TEST_RUNNER_TOKENS = ("unittest", "pytest", "py.test")

# 미러 게시물임을 드러내는 접두어. 사람이 채널만 봐도 구분할 수 있어야 한다.
MIRROR_TAG = "[web-mirror]"

# Discord 메시지 본문 상한은 2000자다. 질의 자체도 상한이 2000자라
# (`CanonicalIngress.query`) 접두어를 붙이면 넘칠 수 있어 잘라낸다.
_DISCORD_CONTENT_LIMIT = 2000

_LOGGER = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class MirrorPost:
    """게시된 미러 메시지의 좌표. root body에 그대로 적힌다."""

    channel_id: str
    message_id: str
    guild_id: str | None = None


def mirror_enabled() -> bool:
    """미러 게시가 켜져 있는가. **기본값은 꺼짐이다.**

    매 호출마다 환경변수를 읽는다 - 테스트가 켜고 끌 수 있어야 하고, 모듈 상수로
    굳히면 import 순서에 따라 테스트가 서로를 오염시킨다(`current_user.auth_required()`
    와 같은 이유).
    """

    return os.getenv(ENABLED_ENV, "").strip().casefold() in _TRUTHY


def test_runner_active() -> bool:
    """이 프로세스가 테스트 러너로 떠 있는가.

    pytest는 실행 중인 테스트 이름을 `PYTEST_CURRENT_TEST`에 넣는다. `python -m
    unittest`/`python -m pytest`는 `__main__` 모듈 경로와 `sys.argv[0]`에 러너
    이름이 남는다. 둘 다 **러너로 실행됐다**는 신호이지, 단순 import가 아니다.
    """

    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    candidates = [str(sys.argv[0] or "")]
    main_module = sys.modules.get("__main__")
    candidates.append(str(getattr(main_module, "__file__", "") or ""))
    for candidate in candidates:
        normalized = candidate.replace("\\", "/").casefold()
        if any(f"/{token}/" in normalized or normalized.endswith(token)
               for token in _TEST_RUNNER_TOKENS):
            return True
        # `python -m unittest`는 argv[0]이 .../unittest/__main__.py 다.
        if any(f"/{token}/__main__.py" in normalized for token in _TEST_RUNNER_TOKENS):
            return True
    return False


def _config() -> tuple[str, str] | None:
    """(토큰, 채널 id). 꺼져 있거나 둘 중 하나라도 비어 있으면 `None`.

    **값을 로그에 남기지 않는다.** 설정 누락은 "무엇이 비었는지"만 알면 고칠 수
    있고, 토큰이 로그로 새면 그 로그를 보는 모든 사람이 발송 권한을 갖는다.
    """

    if not mirror_enabled():
        return None
    if test_runner_active():
        # 조용히 넘어가지 않고 남긴다 - 운영에서 이 줄이 보이면 판정이 잘못된
        # 것이므로 즉시 드러나야 한다.
        _LOGGER.warning("discord-mirror status=blocked reason=test_runner_detected")
        return None
    token = os.getenv(TOKEN_ENV, "").strip()
    channel_id = os.getenv(CHANNEL_ENV, "").strip()
    if not token or not channel_id:
        missing = [
            name
            for name, value in ((TOKEN_ENV, token), (CHANNEL_ENV, channel_id))
            if not value
        ]
        _LOGGER.info(
            "discord-mirror status=skipped reason=not_configured missing=%s",
            ",".join(missing),
        )
        return None
    return token, channel_id


def _asked_by_label(asked_by: object) -> str | None:
    """요청자 표시 문자열. **문자열이 아니면 버린다.**

    `ceo_query(req)`를 FastAPI 없이 직접 부르면 `owner_id`의 기본값이 FastAPI의
    `Depends(...)` 객체 그대로다. 그걸 그대로 찍어서 채널에 `[web-mirror]
    Depends(dependency=<function optional_current_user ...>)`가 올라갔다
    (2026-08-18 실측). 표시용 값 하나 때문에 내부 객체가 새면 안 된다.

    ## 왜 멘션(`<@id>`)으로 바꾸나

    테스트 계정 uuid를 그대로 찍으면(`[web-mirror] 00000000-...cec2`) 채널을 보는
    사람이 누가 물었는지 알 수 없다. `DISCORD_ACTOR_MAP`에 그 계정과 이어진
    Discord 사용자가 **정확히 한 명**이면 멘션으로 렌더한다 - Discord가 그 사람의
    표시 이름으로 보여준다.

    **알림은 가지 않는다**: `post_question()`이 `allowed_mentions={"parse": []}`로
    보내므로 멘션이 렌더만 되고 ping은 안 된다. 미러 게시물이 사람을 호출하면
    질문 하나에 알림이 하나씩 쌓인다.

    매핑이 없거나 여러 명이 같은 계정을 쓰면 uuid를 그대로 둔다 - 남의 이름으로
    보이는 것보다 못 읽는 편이 낫다.
    """

    if not isinstance(asked_by, str):
        return None
    label = asked_by.strip()
    if not label:
        return None
    discord_user_id = _discord_id_for_user(label)
    return f"<@{discord_user_id}>" if discord_user_id else label


def _discord_id_for_user(user_id: str) -> str | None:
    """`discord_actor_map`의 역방향 조회. 실패는 `None`(표시만 못 할 뿐이다).

    지연 import: 이 모듈은 발송만 담당하고 매핑표를 소유하지 않는다. 표시 이름
    하나 때문에 import 실패가 게시 자체를 막으면 안 된다.
    """

    try:
        try:
            from .discord_actor_map import discord_id_for_user
        except ImportError:  # pragma: no cover - 직접 실행 경로
            from discord_actor_map import discord_id_for_user  # type: ignore[no-redef]

        return discord_id_for_user(user_id)
    except Exception:  # noqa: BLE001 - 표시용 값 하나가 게시를 막지 않는다.
        return None


def build_content(query: str, *, asked_by: object = None) -> str:
    """채널에 올릴 본문.

    질의 원문을 그대로 싣는다 - 요약하면 Discord 이력과 Kanban 카드의 질문이
    달라져서, 나중에 "무엇을 물었나"를 두 곳에서 다르게 읽게 된다.

    `asked_by`는 요청자 표시용이다. Mandate 값·한도는 **싣지 않는다** - 이
    채널은 팀원 모두가 보고, 스냅샷은 Kanban root body에 이미 있다.
    """

    label = _asked_by_label(asked_by)
    header = f"{MIRROR_TAG} {label}" if label else MIRROR_TAG
    body = f"{header}\n{query.strip()}"
    if len(body) <= _DISCORD_CONTENT_LIMIT:
        return body
    # 잘렸다는 사실을 남긴다. 조용히 자르면 채널의 질문과 Kanban의 질문이
    # 다른데도 같아 보인다.
    suffix = "\n…(잘림)"
    keep = _DISCORD_CONTENT_LIMIT - len(suffix)
    return body[:keep] + suffix


def post_question(query: str, *, asked_by: object = None) -> MirrorPost | None:
    """질의를 채널에 게시하고 그 메시지의 좌표를 준다. 실패하면 `None`.

    예외를 올리지 않는다 - 이 게시가 실패했다고 사용자의 질문 접수까지 실패하면,
    Discord 장애가 곧 CEO 장애가 된다.
    """

    config = _config()
    if not config:
        return None
    token, channel_id = config

    try:
        response = httpx.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={
                "content": build_content(query, asked_by=asked_by),
                # 미러 게시물이 채널의 모든 사람을 호출하지 않게 한다. 질의 본문에
                # @everyone 이나 역할 멘션이 들어 있어도 알림이 나가지 않는다.
                "allowed_mentions": {"parse": []},
            },
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        _LOGGER.warning(
            "discord-mirror status=failed reason=transport exception_type=%s",
            type(exc).__name__,
        )
        return None

    if response.status_code == 401:
        _LOGGER.warning("discord-mirror status=failed reason=token_rejected_401")
        return None
    if response.status_code == 403:
        _LOGGER.warning(
            "discord-mirror status=failed reason=missing_send_messages_403 channel=%s",
            channel_id,
        )
        return None
    if response.status_code == 429:
        _LOGGER.warning("discord-mirror status=failed reason=rate_limited_429")
        return None
    if response.status_code >= 400:
        _LOGGER.warning(
            "discord-mirror status=failed reason=http_%s", response.status_code
        )
        return None

    try:
        payload: Any = response.json()
    except ValueError:
        _LOGGER.warning("discord-mirror status=failed reason=non_json_response")
        return None
    if not isinstance(payload, dict):
        _LOGGER.warning("discord-mirror status=failed reason=unexpected_payload")
        return None

    message_id = str(payload.get("id") or "").strip()
    if not message_id:
        # id가 없으면 상관관계 앵커로 쓸 수 없다. 게시는 됐을지 몰라도 이후
        # 답변을 붙일 수 없으므로 "성공"이라고 하지 않는다.
        _LOGGER.warning("discord-mirror status=failed reason=missing_message_id")
        return None

    guild_id = str(payload.get("guild_id") or "").strip() or None
    _LOGGER.info(
        "discord-mirror status=posted channel=%s message=%s", channel_id, message_id
    )
    return MirrorPost(
        channel_id=str(payload.get("channel_id") or channel_id),
        message_id=message_id,
        guild_id=guild_id,
    )


if __name__ == "__main__":
    from unittest.mock import patch
    # 네트워크를 타지 않는 자체 점검. 저장소 관례(`__main__` assert)에 맞춘다.
    short = build_content("삼성전자 리스크 알려줘", asked_by="user-1")
    assert short.startswith(f"{MIRROR_TAG} user-1\n"), short
    assert "삼성전자 리스크 알려줘" in short

    plain = build_content("  질문  ")
    assert plain == f"{MIRROR_TAG}\n질문", plain

    long_query = "가" * 2000
    clipped = build_content(long_query, asked_by="user-1")
    assert len(clipped) <= _DISCORD_CONTENT_LIMIT, len(clipped)
    assert clipped.endswith("…(잘림)"), clipped[-20:]

    # Depends 객체 같은 비문자열은 표시하지 않는다(2026-08-18 실측 유출).
    class _Sentinel:
        def __repr__(self) -> str:
            return "Depends(dependency=<function optional_current_user>)"

    leaked = build_content("q", asked_by=_Sentinel())
    assert leaked == MIRROR_TAG + chr(10) + "q", leaked
    assert "Depends" not in leaked, leaked

    # **꺼져 있으면 아무것도 하지 않는다.** 토큰·채널이 환경에 있어도 마찬가지 -
    # 테스트가 실제 채널에 글을 쓰는 사고를 막는 기본 방어선이다.
    from unittest.mock import patch

    with patch.dict(
        os.environ,
        {ENABLED_ENV: "", TOKEN_ENV: "dummy-token", CHANNEL_ENV: "123"},
    ):
        assert mirror_enabled() is False
        assert post_question("꺼져 있음") is None

    # 켜져 있어도 설정이 없으면 None. 예외를 올리면 질의 접수 전체가 묶인다.
    with patch.dict(os.environ, {ENABLED_ENV: "true", TOKEN_ENV: "", CHANNEL_ENV: ""}):
        assert mirror_enabled() is True
        assert post_question("설정 없음") is None

    # 표시 이름: 매핑된 계정은 멘션으로 렌더된다(알림은 allowed_mentions로 차단).
    from discord_actor_map import ACTOR_MAP_ENV

    user3 = "00000000-0000-4000-8000-00000000cec2"
    with patch.dict(os.environ, {ACTOR_MAP_ENV: f"123456789012345678:{user3}"}):
        named = build_content("q", asked_by=user3)
        assert named.startswith(MIRROR_TAG + " <@123456789012345678>"), named

    # 매핑이 없으면 uuid 그대로 - 남의 이름으로 보이는 것보다 낫다.
    with patch.dict(os.environ, {ACTOR_MAP_ENV: ""}):
        plain_uuid = build_content("q", asked_by=user3)
        assert plain_uuid.startswith(MIRROR_TAG + " " + user3), plain_uuid

    # 러너 판정: pytest 환경변수만으로도 차단된다.
    with patch.dict(
        os.environ,
        {
            ENABLED_ENV: "true",
            TOKEN_ENV: "dummy-token",
            CHANNEL_ENV: "123",
            "PYTEST_CURRENT_TEST": "tests/x.py::test_y (call)",
        },
    ):
        assert test_runner_active() is True
        assert post_question("테스트 중") is None

    # 이 파일을 직접 실행한 지금은 러너가 아니다 - 판정이 과하게 넓으면
    # 운영에서도 미러가 꺼진다.
    assert test_runner_active() is False

    print("discord_mirror self-check ok")
