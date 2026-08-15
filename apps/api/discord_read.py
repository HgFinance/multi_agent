#!/usr/bin/env python3
"""Discord 채널 대화 읽기. **Read-only이고, 봇 토큰은 서버에만 있다.**

소유: 도현
근거: ai-office/CLAUDE.md "브라우저에서 Broker API·Hermes 내부 DB를 직접
      호출하지 않는다", CLAUDE.local.md "키를 YAML이나 코드에 넣지 말 것"

▶ 왜 BFF를 거치나
  프론트가 Discord API를 직접 부르면 봇 토큰이 번들에 들어간다. 그 토큰은
  채널 읽기뿐 아니라 **발송 권한**도 갖고 있어서, 브라우저에 내려가는 순간
  누구나 회사 채널에 HERMES 이름으로 글을 쓸 수 있다. 그래서 토큰은 여기
  프로세스 환경변수에만 두고, 화면에는 정규화된 메시지만 내려보낸다.

▶ 이 값이 무엇이고 무엇이 아닌가
  Discord가 보관하는 **대화 원문**이다. 부서 Worker가 실행되며 남긴
  `agent.status.v1` 내부 메시지가 아니고, 회계·주문 상태의 근거도 아니다.
  그래서 `source: "discord"`를 항상 싣는다 - 이 라벨이 빠지면 잡담이
  Worker 실행 기록처럼 보인다(개발원칙 5).

▶ 부서를 **이름이 아니라 봇 user id로 가른다**
  봇 표시 이름은 서버에서 바뀐다 - 실제로 CEO 봇은 `HERMES-CEO`가 아니라
  `홍진표`로 보인다. 이름을 하드코딩하면 개명 한 번에 그 부서 카드가 조용히
  빈다. 그래서 부서 키(`ceo`, `trading` …)로 그 부서 **토큰**을 고르고,
  토큰이 `/users/@me`로 알려주는 user id로 거른다. id는 안 바뀐다.

▶ 여기서 하지 않는 것
  발송하지 않는다. GET 두 개(@me, messages)만 부른다. 요약·해석하지 않는다.

자체 점검:
    python apps/api/discord_read.py
"""

from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

DISCORD_API = "https://discord.com/api/v10"

# 부서 키 → 토큰 환경변수. 화면은 이 키만 보내고 토큰은 모른다.
DEPARTMENT_TOKEN_ENV = {
    "ceo": "DISCORD_BOT_TOKEN_CEO",
    "hr": "DISCORD_BOT_TOKEN_HR",
    "research": "DISCORD_BOT_TOKEN_RESEARCH",
    "quant": "DISCORD_BOT_TOKEN_QUANT",
    "accounting": "DISCORD_BOT_TOKEN_ACCOUNTING",
    "trading": "DISCORD_BOT_TOKEN_TRADING",
    "risk": "DISCORD_BOT_TOKEN_RISK",
    "qa": "DISCORD_BOT_TOKEN_QA",
}

# `/ui/snapshot`이 이미 쓰는 department_code도 그대로 받는다. 프론트가 자기
# 쪽에 매핑표를 하나 더 두면 부서를 늘릴 때 한쪽만 고쳐져 조용히 빈 목록이 된다.
DEPARTMENT_CODE_ALIAS = {
    "ceo-agent": "ceo",
    "hr-department": "hr",
    "research-department": "research",
    "quant-backtest-department": "quant",
    "accounting-portfolio-department": "accounting",
    "trading-department": "trading",
    "risk-management": "risk",
    "qa-department": "qa",
}

router = APIRouter(prefix="/ui/discord", tags=["discord"])


class DiscordMessage(BaseModel):
    schema_version: Literal["ui.discord-message.v1"] = "ui.discord-message.v1"
    id: str
    author: str
    author_id: str
    is_bot: bool
    """이 부서 봇이 쓴 글인가. 화면이 좌/우 정렬을 가르는 데 쓴다."""
    is_department_bot: bool
    text: str
    created_at: str


class DiscordMessagesResponse(BaseModel):
    schema_version: Literal["ui.discord-messages.v1"] = "ui.discord-messages.v1"
    source: Literal["discord"] = "discord"
    authoritative: bool = False
    department: str
    channel_id: str
    bot_id: str
    messages: list[DiscordMessage]


def resolve_department(department: str) -> str:
    """`ceo`도 `ceo-agent`도 같은 부서로 본다."""
    key = DEPARTMENT_CODE_ALIAS.get(department, department)
    if key not in DEPARTMENT_TOKEN_ENV:
        raise HTTPException(
            404, f"알 수 없는 부서 키입니다. 허용: {', '.join(sorted(DEPARTMENT_TOKEN_ENV))}"
        )
    return key


@lru_cache(maxsize=len(DEPARTMENT_TOKEN_ENV))
def bot_user_id(token: str) -> str:
    """토큰이 가리키는 봇의 user id. 표시 이름과 달리 바뀌지 않아 캐시해도 된다."""
    response = httpx.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bot {token}"},
        timeout=10.0,
    )
    if response.status_code == 401:
        raise HTTPException(502, "Discord 봇 토큰이 거부됐습니다(401). 토큰을 재발급하세요.")
    if response.status_code != 200:
        raise HTTPException(502, f"봇 식별 실패(HTTP {response.status_code}).")
    return str(response.json().get("id", ""))


# ponytail: 프로세스 메모리 TTL 캐시. 부서 카드를 옮겨 다닐 때마다 Discord를
# 때리면 429가 뜨고, 429는 채널 읽기 전체를 잠근다. 워커를 늘리면 캐시가
# 워커 수만큼 생기므로, 그때는 Redis로 옮긴다(mirror store가 이미 거기 있다).
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_TTL_SECONDS = max(5, int(os.getenv("DISCORD_READ_CACHE_SECONDS", "30")))


def fetch_messages(token: str, channel_id: str, limit: int) -> list[dict[str, Any]]:
    """Discord 원본 메시지 목록. 캐시 히트면 네트워크를 타지 않는다."""
    key = f"{channel_id}:{limit}"
    cached = _CACHE.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < _TTL_SECONDS:
        return cached[1]

    response = httpx.get(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        params={"limit": limit},
        headers={"Authorization": f"Bot {token}"},
        timeout=10.0,
    )
    if response.status_code == 401:
        raise HTTPException(502, "Discord 봇 토큰이 거부됐습니다(401). 토큰을 재발급하세요.")
    if response.status_code == 403:
        raise HTTPException(502, "봇에 이 채널의 Read Message History 권한이 없습니다(403).")
    if response.status_code == 429:
        raise HTTPException(503, "Discord rate limit(429). 잠시 후 다시 시도하세요.")
    if response.status_code != 200:
        raise HTTPException(502, f"Discord 조회 실패(HTTP {response.status_code}).")

    payload = response.json()
    if not isinstance(payload, list):
        raise HTTPException(502, "Discord 응답이 메시지 목록이 아닙니다.")
    _CACHE[key] = (now, payload)
    return payload


def normalize(raw: list[dict[str, Any]], bot_id: str) -> list[DiscordMessage]:
    """Discord 원본을 화면 계약으로 줄인다. 오래된 것이 위로 가게 뒤집는다.

    **다른 부서 봇의 글은 뺀다.** 8개 봇이 같은 채널에 보고를 쏟아서, 안 거르면
    어느 카드를 열어도 똑같은 목록이 나온다. 남기는 것은 이 부서 봇과 사람이고,
    그 둘이 실제로 주고받은 대화다.

    본문이 빈 메시지(첨부·임베드만 있는 것)는 버리지 않고 표시만 비워 둔다 -
    지우면 대화에 구멍이 생겨 맥락이 끊긴다.
    """
    messages: list[DiscordMessage] = []
    for item in raw:
        if not item.get("id"):
            continue
        author = item.get("author") or {}
        author_id = str(author.get("id", ""))
        is_bot = bool(author.get("bot", False))
        if is_bot and author_id != bot_id:
            continue
        messages.append(
            DiscordMessage(
                id=str(item["id"]),
                # 화면에 뭐라고 적을지만 정한다. 서버 별명 > 표시 이름 > 계정명
                # 순으로, Discord에서 보이는 것과 같은 이름을 쓴다.
                # **판정에는 안 쓴다** - 이름은 바뀌고 id는 안 바뀐다.
                author=str(
                    (item.get("member") or {}).get("nick")
                    or author.get("global_name")
                    or author.get("username")
                    or "unknown"
                ),
                author_id=author_id,
                is_bot=is_bot,
                is_department_bot=author_id == bot_id,
                text=str(item.get("content") or ""),
                created_at=str(item.get("timestamp") or ""),
            )
        )
    return list(reversed(messages))


@router.get("/messages", response_model=DiscordMessagesResponse)
def read_messages(
    department: str = Query(description="부서 키. ceo, trading, risk …"),
    limit: int = Query(default=50, ge=1, le=100),
) -> DiscordMessagesResponse:
    key = resolve_department(department)
    token = os.getenv(DEPARTMENT_TOKEN_ENV[key], "").strip()
    channel_id = os.getenv("DISCORD_CEO_CHANNEL_ID", "").strip()
    if not token or not channel_id:
        # 자격증명이 없으면 빈 목록을 주지 않는다. 빈 목록은 "대화가 없다"로
        # 읽히는데 실제로는 "못 읽었다"이고, 둘은 화면에서 구분돼야 한다.
        raise HTTPException(
            503,
            f"{DEPARTMENT_TOKEN_ENV[key]} / DISCORD_CEO_CHANNEL_ID가 설정되지 않았습니다.",
        )
    bot_id = bot_user_id(token)
    return DiscordMessagesResponse(
        department=key,
        channel_id=channel_id,
        bot_id=bot_id,
        messages=normalize(fetch_messages(token, channel_id, limit), bot_id),
    )


if __name__ == "__main__":
    # 네트워크 없이 도는 자체 점검. 정규화 규칙만 본다.
    sample = [
        {"id": "4", "author": {"id": "other-bot", "username": "HERMES-QA", "bot": True}, "content": "남의 부서 보고", "timestamp": "2026-08-14T08:00:00+00:00"},
        {"id": "3", "author": {"id": "our-bot", "username": "홍진표", "bot": True}, "content": "보고", "timestamp": "2026-08-14T07:00:00+00:00"},
        {"id": "2", "author": {"id": "a", "username": "doyyn_", "global_name": "도현"}, "content": "", "timestamp": "2026-08-14T06:00:00+00:00"},
        {"id": "1", "author": {"id": "a", "username": "doyyn_", "global_name": "도현"}, "content": "안녕", "timestamp": "2026-08-14T05:00:00+00:00"},
        {"author": {"id": "a"}, "content": "id 없는 것은 버린다"},
    ]
    out = normalize(sample, "our-bot")
    assert [m.id for m in out] == ["1", "2", "3"], "오래된 것이 위로, 남의 부서 봇은 제외"
    assert out[0].author == "도현" and not out[0].is_department_bot, "global_name 우선, 사람은 부서봇 아님"
    assert out[2].is_bot and out[2].is_department_bot, "이 부서 봇은 이름이 아니라 id로 판정한다"
    assert out[1].text == "", "본문 빈 메시지도 남긴다"

    # 이름이 통째로 바뀌어도 id가 같으면 그대로 잡힌다.
    renamed = normalize([{"id": "9", "author": {"id": "our-bot", "username": "완전다른이름", "bot": True}, "content": "x", "timestamp": "t"}], "our-bot")
    assert len(renamed) == 1 and renamed[0].is_department_bot, "개명해도 id로 잡아야 한다"

    assert resolve_department("ceo-agent") == "ceo", "department_code도 받는다"
    assert resolve_department("trading") == "trading", "짧은 키도 받는다"
    assert set(DEPARTMENT_CODE_ALIAS.values()) == set(DEPARTMENT_TOKEN_ENV), "부서 8개가 어긋나면 안 된다"

    _CACHE["c:1"] = (time.monotonic(), [{"id": "cached"}])
    assert fetch_messages("t", "c", 1) == [{"id": "cached"}], "TTL 안이면 네트워크를 안 탄다"

    print("discord_read 자체 점검 통과")
