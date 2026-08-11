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

CANONICAL_PROFILES: Final[frozenset[str]] = frozenset(
    CANONICAL_PROFILE_BY_DEPARTMENT.values()
)

_DEPARTMENT_BY_CANONICAL_PROFILE: Final[Mapping[str, str]] = MappingProxyType(
    {
        profile: department
        for department, profile in CANONICAL_PROFILE_BY_DEPARTMENT.items()
        if department not in {"portfolio", "audit", "workforce"}
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
    "CANONICAL_PROFILES",
    "CanonicalKanbanTaskRequest",
    "CanonicalProfileError",
    "canonical_profile_for_department",
    "department_for_canonical_profile",
    "validate_canonical_profile",
]
