#!/usr/bin/env python3
"""Discord 작성자를 테스트 계정에 잇는 매핑.

소유: 영주. 근거: docs/02-engineering/DISCORD_WEB_CEO_MIRRORING.md

## 무엇을 푸는가

Discord로 들어온 질의는 "누가 물었는지"를 테스트 계정 기준으로 알 수 없다.
그래서 `requested_by`(계정별 이력 필터)도, Mandate 스냅샷(사용자 한도)도 붙지
않는다 - 웹에서 물으면 붙고 Discord에서 물으면 안 붙는 상태였다.

이 표는 **Discord 작성자 id → 테스트 계정**을 잇는다. 팀원 각자가 자기
Discord 계정으로 질문하면 배정된 테스트 유저의 Mandate로 판단된다.

## 왜 채널이 아니라 작성자인가 (2026-08-18 결정)

처음에는 유저별 채널을 파는 안이었다. 취소했다 - 채널은 누구나 들어가 쓸 수
있어 오히려 덜 정확하고, 채널을 3개 더 만들면 `discord_read.py`가 읽을 대상도
갈라진다. 작성자 id는 표시 이름과 달리 **바뀌지 않는다**(`discord_read.py`가
부서를 이름 대신 봇 user id로 가르는 것과 같은 이유).

## fund_id는 선택이다

**권장 형식은 2칸(`discord_id:user_uuid`)이다.** `fund_id`는
`GET /governance/v1/users/{user_id}/fund`가 `governance.fund_memberships`에서
풀어 준다(2026-08-18 추가). 표에 fund를 또 적으면 소유 관계가 바뀌었을 때
두 곳을 고쳐야 하고, 한쪽만 고치면 조용히 어긋난다.

3칸(`discord_id:user_uuid:fund_uuid`)도 받는다 - governance-api가 없거나
역참조가 아직 안 되는 환경을 위한 명시적 우회다. 3칸으로 적으면 그 값이
역참조보다 우선한다(`ceo_mirror_api._ceo_query`).

## 형식

    DISCORD_ACTOR_MAP=<discord_user_id>:<test_user_uuid>

여러 명은 쉼표로 잇는다. 공백·줄바꿈은 무시한다. 사람마다 uuid가 다르다.

    DISCORD_ACTOR_MAP=111:0000...cec2,222:0000...cec0

## 매핑이 없으면

`None`이다. 호출부는 `owner_id`/`fund_id` 없이 진행하고, 그러면 Mandate 스냅샷도
붙지 않는다 - **"이 요청에는 사용자 한도가 없다"가 정확한 사실**이고, 임의의
기본 계정으로 채우면 사용자가 정하지 않은 한도가 판단 근거가 된다(개발 원칙 9).

## 이건 인증이 아니다

Discord 작성자 id는 그 사람이 그 계정으로 로그인했다는 것만 뜻한다. 이 매핑은
"표시·조회 필터와 Mandate 선택의 근거"이지 접근 통제가 아니다 - `X-User-Id`와
같은 수준이다(`apps/api/current_user.py` 머리말).

자체 점검:
    python apps/api/discord_actor_map.py
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

ACTOR_MAP_ENV = "DISCORD_ACTOR_MAP"

# 테스트 계정·Fund는 UUID다. 모양이 틀린 항목은 조용히 쓰지 않고 걸러낸다 -
# 오타 하나로 존재하지 않는 계정이 요청자로 기록되면 이력 필터가 빈 결과를 준다.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Discord snowflake는 17~20자리 숫자다. 표시 이름(`@홍길동`)을 잘못 넣는 실수를
# 여기서 잡는다.
_SNOWFLAKE_RE = re.compile(r"^\d{15,25}$")

_LOGGER = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class ActorBinding:
    """Discord 작성자 한 명이 가리키는 테스트 계정.

    `fund_id`가 `None`이면 호출부가 `user_id`로 역참조한다 - 그게 기본 경로다.
    """

    discord_user_id: str
    user_id: str
    fund_id: str | None = None


def _parse(raw: str) -> dict[str, ActorBinding]:
    """`DISCORD_ACTOR_MAP` 문자열을 표로 만든다. 잘못된 항목은 버린다.

    예외를 올리지 않는다 - 환경변수 오타 하나로 BFF가 기동에 실패하면 Discord와
    무관한 모든 경로가 같이 멈춘다.
    """

    table: dict[str, ActorBinding] = {}
    for entry in re.split(r"[,\s]+", raw.strip()):
        if not entry:
            continue
        parts = [part.strip() for part in entry.split(":")]
        if len(parts) == 2:
            parts.append("")  # fund는 선택 - 없으면 역참조로 푼다.
        if len(parts) != 3:
            _LOGGER.warning(
                "discord-actor-map status=skipped reason=expected_2_or_3_fields entry=%s",
                entry,
            )
            continue
        discord_user_id, user_id, fund_id = parts
        if not _SNOWFLAKE_RE.match(discord_user_id):
            _LOGGER.warning(
                "discord-actor-map status=skipped reason=not_a_discord_id value=%s",
                discord_user_id,
            )
            continue
        if not _UUID_RE.match(user_id):
            _LOGGER.warning(
                "discord-actor-map status=skipped reason=not_a_uuid discord_id=%s",
                discord_user_id,
            )
            continue
        if fund_id and not _UUID_RE.match(fund_id):
            # fund가 적혔는데 모양이 틀렸으면 그 항목을 버린다. 조용히 무시하고
            # 역참조로 넘어가면, 사용자는 자기가 적은 fund가 쓰인 줄 안다.
            _LOGGER.warning(
                "discord-actor-map status=skipped reason=not_a_uuid_fund discord_id=%s",
                discord_user_id,
            )
            continue
        if discord_user_id in table:
            # 같은 사람을 두 계정에 이으면 어느 Mandate로 판단할지가 순서에
            # 좌우된다. 먼저 온 것을 남기고 뒤엣것을 버린다(결정적).
            _LOGGER.warning(
                "discord-actor-map status=skipped reason=duplicate discord_id=%s",
                discord_user_id,
            )
            continue
        table[discord_user_id] = ActorBinding(
            discord_user_id=discord_user_id,
            user_id=user_id,
            fund_id=fund_id or None,
        )
    return table


def actor_table() -> dict[str, ActorBinding]:
    """현재 매핑표. **호출할 때마다 환경변수를 읽는다.**

    모듈 로드 시점에 굳히지 않는 이유는 `current_user.auth_required()`와 같다 -
    테스트가 `patch.dict(os.environ, ...)`로 갈아끼울 수 있어야 하고, 상수로
    굳히면 import 순서에 따라 테스트가 서로를 오염시킨다. 항목이 몇 개뿐이라
    파싱 비용은 문제가 되지 않는다.
    """

    return _parse(os.getenv(ACTOR_MAP_ENV, ""))


def resolve(discord_user_id: str | None) -> ActorBinding | None:
    """Discord 작성자 id로 테스트 계정을 찾는다. 없으면 `None`."""

    key = str(discord_user_id or "").strip()
    if not key:
        return None
    return actor_table().get(key)


if __name__ == "__main__":
    from unittest.mock import patch

    user3 = "00000000-0000-4000-8000-00000000cec2"
    fund3 = "3838f7d6-0c7c-4e54-85f3-316a451e7eeb"
    user1 = "00000000-0000-4000-8000-00000000cec0"
    fund1 = "b13f5cd1-5df0-4025-92cf-9be03b1a0296"

    with patch.dict(os.environ, {ACTOR_MAP_ENV: f"123456789012345678:{user3}:{fund3}"}):
        binding = resolve("123456789012345678")
        assert binding is not None
        assert binding.user_id == user3 and binding.fund_id == fund3, binding
        assert resolve("999999999999999999") is None
        assert resolve("") is None
        assert resolve(None) is None

    # 여러 명 + 공백·줄바꿈 혼용
    raw = f"  123456789012345678:{user3}:{fund3} ,\n 234567890123456789:{user1}:{fund1}  "
    with patch.dict(os.environ, {ACTOR_MAP_ENV: raw}):
        assert len(actor_table()) == 2
        assert resolve("234567890123456789").fund_id == fund1

    # 잘못된 항목은 버리되 나머지는 살린다 - 오타 하나로 전체가 죽지 않는다.
    broken = f"not-a-snowflake:{user3}:{fund3},123456789012345678:{user3}:{fund3},999:x:y"
    with patch.dict(os.environ, {ACTOR_MAP_ENV: broken}):
        table = actor_table()
        assert list(table) == ["123456789012345678"], list(table)

    # 2칸 형식(권장) - fund는 역참조로 푼다.
    with patch.dict(os.environ, {ACTOR_MAP_ENV: f"123456789012345678:{user3}"}):
        binding = resolve("123456789012345678")
        assert binding is not None and binding.user_id == user3, binding
        assert binding.fund_id is None, binding

    # fund 모양이 틀리면 그 항목을 버린다(조용히 역참조로 넘어가지 않는다).
    with patch.dict(os.environ, {ACTOR_MAP_ENV: f"123456789012345678:{user3}:not-a-uuid"}):
        assert resolve("123456789012345678") is None

    # 미설정이면 빈 표. 예외를 올리지 않는다.
    with patch.dict(os.environ, {ACTOR_MAP_ENV: ""}):
        assert actor_table() == {}
        assert resolve("123456789012345678") is None

    print("discord_actor_map self-check ok")
