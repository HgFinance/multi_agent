"""Canonical Hermes profile identities used at orchestration boundaries.

Logical department codes are useful inside the planner, but Hermes Kanban stores
the profile name in the assignee column.  This module is the single deterministic
mapping between those two representations.  It deliberately does not normalize
unknown aliases: a typo or legacy name must fail before a task reaches Hermes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


class CanonicalProfileError(ValueError):
    """Raised when a task would use a non-canonical Hermes profile."""


CANONICAL_PROFILE_BY_DEPARTMENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        "ceo": "ceo-agent",
        "research": "research-department",
        "quant": "quant-backtest-department",
        "trading": "trading-department",
        "accounting": "accounting-portfolio-department",
        "portfolio": "accounting-portfolio-department",
        "risk": "risk-management",
        "qa": "qa-department",
        "audit": "qa-department",
        "hr": "hr-department",
        "workforce": "hr-department",
    }
)

# 응대 창구(도서관 층) 프로필 (2026-08-13, 도서관/연구소 분리).
# 부서 본체와 같은 부서에 속하지만 **별도 assignee** 다 - dispatcher 의 유일한
# 라우팅 손잡이가 assignee→프로필이라서, 창구를 별도 프로필로 두는 것이 곧
# 큐·워커풀 분리다(Borg prod/non-prod 이식). 조회성 사용자 질의 자식 카드만
# 여기로 배정하고, 공장·실험 카드는 부서 본체로 간다. 창구 프로필의 도구 면은
# research-liaison-mcp(읽기 전용, 쓰기 도구 미등록)뿐이다.
LIAISON_PROFILE_BY_DEPARTMENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        "research": "research-liaison",
        "quant": "quant-liaison",
    }
)

CANONICAL_PROFILES: Final[frozenset[str]] = frozenset(
    CANONICAL_PROFILE_BY_DEPARTMENT.values()
) | frozenset(LIAISON_PROFILE_BY_DEPARTMENT.values())

# ▶ 실제로 카드에 찍혀 본 적 있는 **틀린 이름들** (2026-08-14 실측)
#   전부 "부서 디렉터리 이름"이나 "컨테이너 이름"을 프로필 이름으로 착각한 것이다.
#   Hermes 는 생성 시점에 assignee 를 검증하지 않으므로 이런 카드는 만들어지고,
#   디스패처가 매 tick "non-spawnable" 로 건너뛰기만 한다 - 즉 **조용히 영원히
#   안 돈다.** 실제로 22 장이 이틀간 정체했고(ai-qa-audit 17·risk-department 5),
#   독립 QA·리스크 게이트가 그동안 한 번도 실행되지 않았다.
#   여기에 적어두면 최소한 우리 코드 경로와 감사 스크립트가 잡아낸다.
LEGACY_PROFILE_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "ai-qa-audit": "qa-department",
        "ai-qa-audit-department": "qa-department",
        "risk-department": "risk-management",
        # 컨테이너 이름(hedgefund-workforce-hermes)에서 온 착각. 런타임에 3 줄짜리
        # 껍데기 프로필 디렉터리까지 생겨 있었다(모델·env·mcp 없음).
        "workforce-management": "hr-department",
        "ceo": "ceo-agent",
        "ceo-office": "ceo-agent",
        "research": "research-department",
        "quant": "quant-backtest-department",
        "trading": "trading-department",
        "accounting": "accounting-portfolio-department",
    }
)

_DEPARTMENT_BY_CANONICAL_PROFILE: Final[Mapping[str, str]] = MappingProxyType(
    {
        **{
            profile: department
            for department, profile in CANONICAL_PROFILE_BY_DEPARTMENT.items()
            if department not in {"portfolio", "audit", "workforce"}
        },
        # 창구 응답도 그 부서의 답이다 - 부서 요약(department_summaries)에
        # 본체 응답과 같은 부서 코드로 잡힌다.
        **{
            profile: department
            for department, profile in LIAISON_PROFILE_BY_DEPARTMENT.items()
        },
    }
)


def validate_canonical_profile(value: str) -> str:
    """Return ``value`` only when it is an exact canonical profile name."""

    if not isinstance(value, str) or value not in CANONICAL_PROFILES:
        raise CanonicalProfileError(
            f"unknown or non-canonical Hermes profile assignee: {value!r}; "
            f"expected one of {sorted(CANONICAL_PROFILES)}"
        )
    return value


def canonical_profile_for_department(department: str) -> str:
    """Resolve a known logical department code to its canonical profile.

    Passing an already canonical profile is idempotent.  Legacy aliases such as
    ``risk-department`` and ``ai-qa-audit-department`` are intentionally rejected.
    """

    if department in CANONICAL_PROFILES:
        return department
    try:
        return CANONICAL_PROFILE_BY_DEPARTMENT[department]
    except (KeyError, TypeError) as exc:
        raise CanonicalProfileError(
            f"unknown department code for Hermes profile resolution: {department!r}"
        ) from exc


def department_for_canonical_profile(profile: str) -> str:
    """Return the stable logical department code for a canonical profile."""

    validate_canonical_profile(profile)
    return _DEPARTMENT_BY_CANONICAL_PROFILE[profile]


# 사용자 질의 계열 카드의 대기열 우선순위. 공장이 만드는 카드는 0 이므로 이
# 값이면 사람이 기다리는 카드가 항상 앞선다. Hermes ready 큐가
# `ORDER BY priority DESC, created_at ASC` 라 이 한 값이 순서를 바꾼다.
# env 로 낮출 수 있게 둔 것은 공장 처리량을 우선하려는 운영 판단을 위해서다.
USER_QUERY_PRIORITY = int(os.getenv("KANBAN_USER_QUERY_PRIORITY", "100"))
# Research evidence requests use the same shared priority queue, but sit just
# above ordinary user-query children. This keeps one queue/claim authority and
# gives the latency-sensitive Research lane a deterministic place in it.
RESEARCH_QUERY_PRIORITY = int(
    os.getenv("KANBAN_RESEARCH_QUERY_PRIORITY", str(USER_QUERY_PRIORITY + 10))
)


@dataclass(frozen=True)
class CanonicalKanbanTaskRequest:
    """Typed create boundary used before invoking ``hermes kanban create``."""

    assignee: str
    title: str
    body: str
    idempotency_key: str
    # 대기열 순서. Hermes 의 ready 큐가 `ORDER BY priority DESC, created_at ASC`
    # 라서(kanban_db.dispatch_once) 사람이 기다리는 카드는 공장 카드보다 먼저
    # 나가야 한다. 실측 2026-08-14: 공장 카드가 슬롯을 물고 ready 23 장이 쌓인
    # 사이 사용자 질의가 6 분 넘게 대기했다. 기본 0 = 기존 동작 그대로.
    priority: int = 0

    def __post_init__(self) -> None:
        validate_canonical_profile(self.assignee)
        if not self.title.strip():
            raise ValueError("Kanban task title must not be empty")
        if not self.body.strip():
            raise ValueError("Kanban task body must not be empty")
        if not self.idempotency_key.strip():
            raise ValueError("Kanban task idempotency_key must not be empty")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise ValueError("Kanban task priority must be an int")  # noqa: TRY004


__all__ = [
    "CANONICAL_PROFILES",
    "CANONICAL_PROFILE_BY_DEPARTMENT",
    "LEGACY_PROFILE_ALIASES",
    "LIAISON_PROFILE_BY_DEPARTMENT",
    "RESEARCH_QUERY_PRIORITY",
    "USER_QUERY_PRIORITY",
    "CanonicalKanbanTaskRequest",
    "CanonicalProfileError",
    "canonical_profile_for_department",
    "department_for_canonical_profile",
    "validate_canonical_profile",
]
