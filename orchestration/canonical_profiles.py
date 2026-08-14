"""Canonical Hermes profile identities used at orchestration boundaries.

Logical department codes are useful inside the planner, but Hermes Kanban stores
the profile name in the assignee column.  This module is the single deterministic
mapping between those two representations.  It deliberately does not normalize
unknown aliases: a typo or legacy name must fail before a task reaches Hermes.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping


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


@dataclass(frozen=True)
class CanonicalKanbanTaskRequest:
    """Typed create boundary used before invoking ``hermes kanban create``."""

    assignee: str
    title: str
    body: str
    idempotency_key: str

    def __post_init__(self) -> None:
        validate_canonical_profile(self.assignee)
        if not self.title.strip():
            raise ValueError("Kanban task title must not be empty")
        if not self.body.strip():
            raise ValueError("Kanban task body must not be empty")
        if not self.idempotency_key.strip():
            raise ValueError("Kanban task idempotency_key must not be empty")


__all__ = [
    "CANONICAL_PROFILE_BY_DEPARTMENT",
    "LIAISON_PROFILE_BY_DEPARTMENT",
    "CANONICAL_PROFILES",
    "CanonicalKanbanTaskRequest",
    "CanonicalProfileError",
    "canonical_profile_for_department",
    "department_for_canonical_profile",
    "validate_canonical_profile",
]
