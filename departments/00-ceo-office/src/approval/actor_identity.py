#!/usr/bin/env python3
"""GOV-02 P0-1: `actor_user_id`가 실제 `governance.user_profiles` 행을 가리키는지 검증.

소유: 영주 (CEO Office)
근거: docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md v2.0 P0-1
      ("Subject의 department, role, scope, expiry, approval target을 결정론적으로 검증한다")

## 팀 합의 (2026-08-05) — 실제 로그인 시스템이 아니다

이 저장소의 로컬 모의투자에는 로그인·세션·외부 사용자 인증이 없다
(TECH_STACK_DECISIONS.md 3.1절 "Production 목표는 Service Token과 mTLS다" - 아직 목표일 뿐
구현되지 않음). P0-1이 요구하는 "서명된 Subject" 검증을 지금 이 모듈이 혼자 만들어낼 수는
없다 - Platform/IAM 전체의 인증 아키텍처 결정이라 CEO Office 코드 하나로 지어낼 성질이
아니다(CLAUDE.md "설계 공백을 임의로 채우지 않는다").

그래서 팀은 다음으로 합의했다: **고정 데모 identity의 user_profiles 행을
로컬 주체로 간주하고, `actor_user_id`가 그 행을 실제로 가리키는지(존재 +
`status='ACTIVE'`)만 결정론적으로 검증한다.** 이건 "누가 보냈는지 서명으로 증명"이 아니라
"자칭하는 사용자가 최소한 DB에 실재하는 활성 계정인가"까지만 좁힌 검증이다 - 이전에는
`actor_user_id`가 빈 문자열만 아니면 무엇이든 통과했다(approval.py의 `MissingActorUserError`는
"비어있는지"만 봤다). 그 상태보다는 분명히 강하지만, 진짜 신원 증명은 아니다.

이 경로는 서명된 신원을 만들어내거나 대체하지 않는다. 통과한 모든 결정은 로컬 fixture
identity로 진행했다는 사실을 감사 기록(`conditions._decider`)에서 숨기지 않는다.

자체 점검: python departments/00-ceo-office/src/approval/actor_identity.py
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Any


class ActorUserStatus(str, Enum):
    """governance.user_profiles.status DDL 값 그대로 (check 제약과 동일)."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"


class UnverifiedActorUserError(Exception):
    """actor_user_id가 governance.user_profiles에 없거나 ACTIVE가 아니다.

    비어있지 않은 문자열이라는 것만으로 통과시키던 이전 상태(approval.py
    MissingActorUserError는 '비어있는지'만 검사했다)보다 좁힌 검증이다 - 실제로
    존재하고 활성 상태인 행을 가리켜야 한다.
    """


def verify_actor_user(status: ActorUserStatus | None, actor_user_id: str) -> None:
    """순수 함수 - 조회는 Repository가, 판정은 여기가 한다(approval.py와 같은 분리 원칙).

    존재하지 않으면(None) 또는 ACTIVE가 아니면 거절한다. 승인 방향으로 fallback하지
    않는다(CLAUDE.md 원칙과 동일 - 모르면 통과시키지 않는다).
    """
    if status is None:
        raise UnverifiedActorUserError(
            f"actor_user_id={actor_user_id!r}는 governance.user_profiles에 없다 - "
            "실재하지 않는 사용자로는 결정을 기록할 수 없다"
        )
    if status is not ActorUserStatus.ACTIVE:
        raise UnverifiedActorUserError(
            f"actor_user_id={actor_user_id!r}는 status={status.value}다 - ACTIVE 계정만 "
            "결정을 내릴 수 있다"
        )


class ActorIdentityRepository:
    """조회 인터페이스. 실제 구현은 governance.user_profiles를 읽는다."""

    def get_status(self, user_id: str) -> ActorUserStatus | None:
        raise NotImplementedError


class InMemoryActorIdentityRepository(ActorIdentityRepository):
    """테스트·개발용. seed()로 미리 등록해둔 user_id만 실재하는 것으로 취급한다."""

    def __init__(self) -> None:
        self._users: dict[str, ActorUserStatus] = {}

    def seed(self, user_id: str, status: ActorUserStatus = ActorUserStatus.ACTIVE) -> None:
        self._users[user_id] = status

    def get_status(self, user_id: str) -> ActorUserStatus | None:
        return self._users.get(user_id)


class ActorIdentityPersistenceError(RuntimeError):
    """governance.user_profiles 조회에 실패한 경우."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise ActorIdentityPersistenceError(
            "PostgreSQL 조회에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    return ThreadedConnectionPool


class PostgresActorIdentityRepository(ActorIdentityRepository):
    """`governance.user_profiles`에 대한 읽기 전용 구현."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresActorIdentityRepository:
        ThreadedConnectionPool = _load_postgres_driver()
        # minconn=0 - 유휴 커넥션을 잡지 않는다
        return cls(ThreadedConnectionPool(0, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def get_status(self, user_id: str) -> ActorUserStatus | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select status from governance.user_profiles where user_id = %s", (user_id,)
                )
                row = cur.fetchone()
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - uuid 형식이 아닌 값 등도 여기서 걸러 거절로 번역한다.
            conn.rollback()
            raise ActorIdentityPersistenceError(f"actor_user_id 조회 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)
        return ActorUserStatus(row[0]) if row else None


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/approval/actor_identity.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 1) 존재하지 않는 사용자는 거절.
    try:
        verify_actor_user(None, "unknown-user")
        raise AssertionError("존재하지 않는 사용자가 통과함")
    except UnverifiedActorUserError:
        pass

    # 2) SUSPENDED/CLOSED는 거절 - 존재해도 활성이 아니면 안 된다.
    for bad_status in (ActorUserStatus.SUSPENDED, ActorUserStatus.CLOSED):
        try:
            verify_actor_user(bad_status, "u1")
            raise AssertionError(f"{bad_status.value} 사용자가 통과함")
        except UnverifiedActorUserError:
            pass

    # 3) ACTIVE만 통과.
    verify_actor_user(ActorUserStatus.ACTIVE, "u1")  # raise 없으면 통과

    # 4) In-Memory Repository - seed 안 한 사용자는 None, seed 한 사용자는 실제 status.
    repo = InMemoryActorIdentityRepository()
    assert repo.get_status("ghost") is None
    repo.seed("real-user", ActorUserStatus.ACTIVE)
    assert repo.get_status("real-user") is ActorUserStatus.ACTIVE
    verify_actor_user(repo.get_status("real-user"), "real-user")
    try:
        verify_actor_user(repo.get_status("ghost"), "ghost")
        raise AssertionError("seed 안 한 사용자가 통과함")
    except UnverifiedActorUserError:
        pass

    print("ok - actor_identity 자체 점검 6개 시나리오 통과")

    import os

    from dotenv import load_dotenv

    load_dotenv()
    dsn = os.environ.get("GOVERNANCE_WORKFORCE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 실 DB 조회 검증은 건너뛴다")
        raise SystemExit(0)

    pg_repo = PostgresActorIdentityRepository.connect(dsn)
    try:
        import uuid

        assert pg_repo.get_status(str(uuid.uuid4())) is None, "존재하지 않는 uuid인데 상태가 나옴"
        # 고정 데모 identity - 존재하면 반드시 ACTIVE여야 한다
        # (seed.sql이 status='ACTIVE'로 심는다).
        placeholder_id = "00000000-0000-4000-8000-00000000cec0"
        status = pg_repo.get_status(placeholder_id)
        if status is None:
            print("SKIP - 고정 데모 user_profiles 행이 없다")
        else:
            assert status is ActorUserStatus.ACTIVE, status
            verify_actor_user(status, placeholder_id)
            print(f"ok - 실 DB 조회 검증 통과 - 플레이스홀더 테스트 Identity status={status.value}")
    finally:
        pg_repo.close()
