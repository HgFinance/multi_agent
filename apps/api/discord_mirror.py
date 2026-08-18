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

## 에코 루프가 생기지 않는 이유

이 게시물의 작성자는 **봇**이다. Hermes gateway의 admission 판정은
`deploy/hermes-discord/gateway_patch.py`가 원본 정책을 그대로 쓰고(주석: "Do not
change permission/mention policy"), 봇 메시지를 사람 발화로 집지 않는다. 그래서
미러 게시가 CEO를 다시 부르지 않는다.

**단, 이 모듈은 그 사실에 의존하지 않는다.** 게시물 앞에 `[web-mirror]` 표시를
붙여, 나중에 admission 정책이 바뀌더라도 사람이든 코드든 "이건 미러다"를 구분할
수 있게 한다(`ceo_mirror.py`의 `mirrored=true`와 같은 취지).

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
from dataclasses import dataclass
from typing import Any

import httpx

DISCORD_API = "https://discord.com/api/v10"

# 읽기 모듈과 같은 환경변수를 쓴다. 채널을 따로 두면 "질문은 A 채널, 답변은 B
# 채널"이 되어 이력이 갈라진다.
CHANNEL_ENV = "DISCORD_CEO_CHANNEL_ID"
TOKEN_ENV = "DISCORD_BOT_TOKEN_CEO"

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


def _config() -> tuple[str, str] | None:
    """(토큰, 채널 id). 둘 중 하나라도 비어 있으면 `None`.

    **값을 로그에 남기지 않는다.** 설정 누락은 "무엇이 비었는지"만 알면 고칠 수
    있고, 토큰이 로그로 새면 그 로그를 보는 모든 사람이 발송 권한을 갖는다.
    """

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


def build_content(query: str, *, asked_by: str | None = None) -> str:
    """채널에 올릴 본문.

    질의 원문을 그대로 싣는다 - 요약하면 Discord 이력과 Kanban 카드의 질문이
    달라져서, 나중에 "무엇을 물었나"를 두 곳에서 다르게 읽게 된다.

    `asked_by`는 요청자 표시용이다. Mandate 값·한도는 **싣지 않는다** - 이
    채널은 팀원 모두가 보고, 스냅샷은 Kanban root body에 이미 있다.
    """

    header = MIRROR_TAG
    if asked_by:
        header = f"{MIRROR_TAG} {asked_by}"
    body = f"{header}\n{query.strip()}"
    if len(body) <= _DISCORD_CONTENT_LIMIT:
        return body
    # 잘렸다는 사실을 남긴다. 조용히 자르면 채널의 질문과 Kanban의 질문이
    # 다른데도 같아 보인다.
    suffix = "\n…(잘림)"
    keep = _DISCORD_CONTENT_LIMIT - len(suffix)
    return body[:keep] + suffix


def post_question(query: str, *, asked_by: str | None = None) -> MirrorPost | None:
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

    # 설정이 없으면 게시를 시도하지 않고 None. 이 경로가 예외를 올리면 질의
    # 접수 전체가 Discord 설정에 묶인다.
    saved = {name: os.environ.pop(name, None) for name in (TOKEN_ENV, CHANNEL_ENV)}
    try:
        assert post_question("설정 없음") is None
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value

    print("discord_mirror self-check ok")
