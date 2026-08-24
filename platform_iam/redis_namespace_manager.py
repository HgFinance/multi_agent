#!/usr/bin/env python3
"""Platform/IAM 실행 계층 — RedisNamespacePlan을 실제 Redis 등록으로 집행한다.

소유: 영주 (CEO/HR, Platform/IAM 담당자 미정 상태에서 최초 구현)
근거: docs/02-engineering/PLATFORM_IAM_SPEC.md 3.1·4.3

## 이 파일이 실제로 하는 것과 안 하는 것 — 과장하지 않는다

**한다**: `memory:agent:<agent_id>:*` 네임스페이스를 Platform/IAM의 등록 레지스트리
(`platform_iam:namespaces` Redis Hash)에 기록한다. 이 등록은 "이 Agent가 이
네임스페이스를 쓸 자격이 있다"는 사실을 남기는 것이다 - activation_evidence.py류의
실재성 검증이 조회할 수 있는 기록.

**안 한다**: Redis ACL(`ACL SETUSER`)로 다른 Agent의 접근을 실제로 차단하는 것은
이 구현의 범위 밖이다. Redis 6+ ACL은 별도의 운영 설정(기본 사용자 잠금, 각
Agent별 ACL 사용자 발급과 자격증명 배포)이 필요하고, 이 세션에서 검증할 수
없다(로컬에 ACL을 켠 Redis가 없다). PLATFORM_IAM_SPEC.md 체크리스트의
"Redis ACL 설정"은 이 파일이 아니라 별도 작업으로 남는다 - 여기서 됐다고
주장하면 실제로 없는 격리를 있다고 알리는 것이 된다(CLAUDE.md: 없는 보호를
있다고 알리지 않는다).

자체 점검: python platform_iam/redis_namespace_manager.py
  - REDIS_URL 없으면 import만 확인한다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

try:
    from .provisioning import RedisNamespacePlan
except ImportError:  # direct/standalone execution
    from platform_iam.provisioning import RedisNamespacePlan

_REGISTRY_KEY = "platform_iam:namespaces"


class NamespaceManagerError(RuntimeError):
    """Namespace 등록/정리 실패. provisioning_ref를 발급하지 못했다는 뜻이다."""


@lru_cache(maxsize=1)
def _load_redis_driver() -> Any:
    try:
        import redis
    except ModuleNotFoundError as exc:
        raise NamespaceManagerError("Redis Namespace 관리에는 redis 패키지가 필요합니다.") from exc
    return redis


def register_namespace(plan: RedisNamespacePlan, *, redis_url: str) -> str:
    """네임스페이스 소유권을 레지스트리에 기록한다(멱등 - HSET 재호출은 덮어쓰기).

    반환값은 plan.provisioning_ref 그대로다.
    """

    redis = _load_redis_driver()
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        client.hset(
            _REGISTRY_KEY,
            plan.namespace_prefix,
            datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        raise NamespaceManagerError(f"Namespace 등록 실패 ({plan.namespace_prefix}): {exc}") from exc
    finally:
        client.close()
    return plan.provisioning_ref


def revoke_namespace(namespace_prefix: str, *, redis_url: str) -> None:
    """등록을 지우고, 그 프리픽스 아래 실제 키들도 정리한다(회수 시 데이터 잔존 방지)."""

    redis = _load_redis_driver()
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        client.hdel(_REGISTRY_KEY, namespace_prefix)
        cursor = 0
        while True:
            cursor, keys = client.scan(cursor=cursor, match=namespace_prefix, count=200)
            if keys:
                client.delete(*keys)
            if cursor == 0:
                break
    except Exception as exc:
        raise NamespaceManagerError(f"Namespace 회수 실패 ({namespace_prefix}): {exc}") from exc
    finally:
        client.close()


def is_namespace_registered(namespace_prefix: str, *, redis_url: str) -> bool:
    redis = _load_redis_driver()
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        return client.hexists(_REGISTRY_KEY, namespace_prefix)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 자체 점검 (python platform_iam/redis_namespace_manager.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    print("ok - import 확인 (redis lazy load)")

    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print("REDIS_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    plan = RedisNamespacePlan(
        namespace_prefix="memory:agent:selfcheck-iam:*",
        provisioning_ref="redis-namespace:agent:selfcheck-iam",
    )
    ref = register_namespace(plan, redis_url=redis_url)
    assert ref == plan.provisioning_ref
    assert is_namespace_registered(plan.namespace_prefix, redis_url=redis_url)
    print("ok - Namespace 등록 왕복 완료")

    revoke_namespace(plan.namespace_prefix, redis_url=redis_url)
    assert not is_namespace_registered(plan.namespace_prefix, redis_url=redis_url)
    print("ok - Namespace 회수 완료 (정리)")
