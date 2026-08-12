#!/usr/bin/env python3
"""요청 하나가 "누구"의 것인지 판정하는 **단일 지점**.

근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 6(명령 경계)
      docs/01-product/USER_INPUT_SPEC.md (Mandate 소유자 = 사용자)

## 왜 모듈 하나로 모으나

`X-User-Id` 헤더는 **인증이 아니다.** 서명도 만료도 없어서 누구나 아무 UUID나
보낼 수 있다. 지금은 폐쇄망 팀 테스트 단계라 이걸 감수하지만, 진짜 인증(JWT
등)이 붙을 때 헤더를 읽는 곳이 라우트마다 흩어져 있으면 한 곳을 빠뜨리고
그 경로만 무인증으로 남는다.

그래서 **user_id를 읽는 곳은 이 모듈 하나**로 둔다. 나중에 교체할 때 고칠
지점이 `current_user()`/`optional_current_user()` 두 함수뿐이라는 것이 이 파일의
존재 이유다 - 라우트는 `Depends`로만 이 값을 받고, 직접 헤더를 읽지 않는다.

## 지금 무엇을 검증하고 무엇을 못 하나

| 검증 | 여기서 하나 |
|---|---|
| 헤더가 있는지 | ✔ (`PORTFOLIO_AUTH_REQUIRED`가 켜져 있을 때) |
| 헤더 값과 리소스 소유자가 같은지 | ✔ (`require_owner`) |
| 그 사람이 **정말 그 사람인지** | ✘ - 인증이 없다 |

세 번째가 없으므로 **이 API를 공개망에 노출하면 남의 Mandate를 읽고 쓸 수 있다.**
CORS allowlist(`main.py`)와 함께 배포 전 필수 조건으로 남겨둔다.
"""
from __future__ import annotations

import os

from fastapi import Header, HTTPException

# 기본 true. 끄는 것은 로컬 개발 편의를 위한 명시적 opt-out이어야 하고,
# 기본값이 false면 "인증을 켜는 것을 잊는" 방향으로 실패한다(개발 원칙 9).
_AUTH_REQUIRED_DEFAULT = "true"


def auth_required() -> bool:
    """매 호출 시 환경변수를 읽는다 - 모듈 로드 시점에 고정하지 않는다.

    테스트가 `patch.dict(os.environ, ...)`로 켜고 끌 수 있어야 하고, 모듈 상수로
    굳히면 import 순서에 따라 테스트가 서로를 오염시킨다.
    """

    return os.getenv("PORTFOLIO_AUTH_REQUIRED", _AUTH_REQUIRED_DEFAULT).casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def current_user(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> str | None:
    """FastAPI 의존성. 헤더에서 요청자를 읽는다.

    `PORTFOLIO_AUTH_REQUIRED`가 켜져 있고 헤더가 없으면 401이다 - 익명 요청을
    "소유자 없음"으로 통과시키면 그 뒤의 소유권 검사가 전부 무의미해진다.
    꺼져 있으면 `None`을 주고, 호출부는 소유권 검사를 건너뛴다(로컬 개발).
    """

    owner_id = (x_user_id or "").strip()
    if not owner_id:
        if auth_required():
            raise HTTPException(status_code=401, detail="portfolio_authentication_required")
        return None
    return owner_id


def optional_current_user(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> str | None:
    """헤더가 있으면 읽고, 없으면 `None`. **401을 내지 않는다.**

    `/ui/ceo/ask` 전용 과도기 의존성이다. 그 경로는 2026-08-12까지 요청자를 아예
    받지 않았고 프론트엔드(`ai-office/app/lib/ceoClient.ts`)도 이 헤더를 보내지
    않는다 - `current_user`(강제)를 바로 붙이면 이미 동작하는 CEO 흐름이 전부
    401로 죽는다.

    **이건 영구 예외가 아니다.** 프론트가 계정 전환과 함께 헤더를 싣기 시작하면
    이 의존성을 `current_user`로 바꾼다. 그때까지 이 경로는 "요청자를 알면
    기록하고, 모르면 익명으로 진행"한다 - CEO 산출물은 `binding: false`라 익명
    질의가 상태를 바꾸지 못하므로 감수할 수 있는 범위다.
    """

    owner_id = (x_user_id or "").strip()
    return owner_id or None


def require_owner(
    owner_id: str | None,
    expected_user_id: str | None = None,
    *,
    required: bool | None = None,
) -> None:
    """요청자가 그 리소스의 주인인지 확인한다.

    `expected_user_id`가 비어 있으면(리소스에 소유자 기록이 없으면) 통과시킨다 -
    소유자를 모르는 리소스에 대해 "너는 주인이 아니다"라고 단정할 근거가 없다.
    소유자가 기록돼 있고 다르면 403이다.

    `main.py`의 `_require_portfolio_owner`가 하던 판정을 그대로 옮긴 것이다 -
    동작을 바꾸지 않았다(401/403 코드와 detail 문자열까지 같다).

    `required`를 인자로 받는 이유: `main.py`는 `PORTFOLIO_AUTH_REQUIRED`를 모듈
    상수로 굳혀 두고 테스트가 그 속성을 patch한다
    (`patch("apps.api.main.PORTFOLIO_AUTH_REQUIRED", True)`). 여기서 환경변수만
    읽으면 그 patch가 무력화되고, 실제로 인증 강제 테스트 2건이 202로 통과해
    **인증이 꺼진 것을 아무도 모르게 된다.** 호출부가 자기 플래그를 넘길 수 있게
    두고, 안 넘기면 환경변수로 떨어진다.
    """

    required = auth_required() if required is None else required
    if required and not owner_id:
        raise HTTPException(status_code=401, detail="portfolio_authentication_required")
    if owner_id and expected_user_id and owner_id != expected_user_id:
        raise HTTPException(status_code=403, detail="portfolio_recommendation_forbidden")


__all__ = ["auth_required", "current_user", "optional_current_user", "require_owner"]
