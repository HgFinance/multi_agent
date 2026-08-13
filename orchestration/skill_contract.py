"""Repository-owned contract for skills force-loaded by Kanban tasks."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from types import MappingProxyType


CANONICAL_SHARED_SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills"
CANONICAL_PROFILES = frozenset(
    {
        "ceo-agent",
        "hr-department",
        "research-department",
        "quant-backtest-department",
        "trading-department",
        "accounting-portfolio-department",
        "risk-management",
        "qa-department",
    }
)

# Repository-owned skill names. The four financial-* entries are retained in
# the contract even while their source is absent from this checkout: a task
# may not make them executable until the canonical repository source exists.
CANONICAL_SKILLS = frozenset(
    {
        "agentic-rag",
        # 2026-08-13 공장 능동화 3종: 병목을 만난 부서가 스스로 진단·구축·기록한다.
        # 소유는 공장 개선 카드가 걸리는 부서(research/quant, wiring-audit 은 QA 포함).
        "dataset-engineering",
        "equity-quant-assessment",
        "experiment-factory",
        "financial-equity-research",
        "financial-portfolio-assessment",
        "financial-research-memos",
        "financial-risk-research",
        "hermes-multi-agent-pipelines",
        "methodology-scout",
        "skill-authoring",
        # 2026-08-13: 카탈로그 우선 탐색 - "없다" 선언 전 정보원 4층 검색 규율.
        # KRX 유료 결제를 막은 실측(t3320 발견)이 탄생 계기.
        "source-catalog-first",
        "wiring-audit",
    }
)

SHARED_PORTFOLIO_SKILL_PROFILES = frozenset(
    {
        "ceo-agent",
        "research-department",
        "quant-backtest-department",
        "risk-management",
        "accounting-portfolio-department",
        "qa-department",
    }
)

# A skill may be used only by its semantic owner. Shared skills list every
# profile that is explicitly part of the shared contract. Skills not present
# here are not silently assigned to a profile.
SKILL_OWNER_BY_NAME = MappingProxyType(
    {
        "agentic-rag": frozenset({"risk-management", "qa-department"}),
        "dataset-engineering": frozenset(
            {"quant-backtest-department", "research-department"}
        ),
        "equity-quant-assessment": frozenset({"quant-backtest-department"}),
        "experiment-factory": frozenset({"quant-backtest-department"}),
        "financial-equity-research": frozenset({"research-department"}),
        "financial-portfolio-assessment": SHARED_PORTFOLIO_SKILL_PROFILES,
        "financial-research-memos": frozenset({"research-department"}),
        "financial-risk-research": frozenset({"risk-management"}),
        "hermes-multi-agent-pipelines": frozenset({"ceo-agent"}),
        "methodology-scout": frozenset({"research-department"}),
        "skill-authoring": frozenset(
            {"quant-backtest-department", "research-department"}
        ),
        "source-catalog-first": frozenset(
            {"research-department", "quant-backtest-department", "qa-department"}
        ),
        "wiring-audit": frozenset(
            {"quant-backtest-department", "research-department", "qa-department"}
        ),
    }
)

# This skill is intentionally not assigned until a domain-specific owner is
# established. Keeping it out of the owner map makes task assignment fail
# closed instead of treating a QA-local copy as globally reusable.
AMBIGUOUS_CUSTOM_SKILLS = frozenset({"hermes-agent-integration"})
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")


class CanonicalSkillError(ValueError):
    """Raised before a task with an unresolvable skill can be created."""


def _candidate_roots(root: Path | None = None) -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in (
        root,
        os.environ.get("HERMES_SHARED_SKILLS_ROOT"),
        Path("/opt/shared-skills"),
        CANONICAL_SHARED_SKILL_ROOT,
    ):
        if value:
            candidate = Path(value).expanduser().resolve()
            if candidate not in roots:
                roots.append(candidate)
    return tuple(roots)


def resolve_canonical_skill(skill_name: str, *, root: Path | None = None) -> Path:
    """Resolve one canonical skill without searching another profile."""

    name = str(skill_name or "").strip()
    if not _SKILL_NAME_RE.fullmatch(name) or name not in CANONICAL_SKILLS:
        raise CanonicalSkillError(f"unknown or non-canonical skill: {skill_name!r}")
    matches = [
        skill_md
        for candidate_root in _candidate_roots(root)
        if candidate_root.is_dir()
        for skill_md in candidate_root.rglob("SKILL.md")
        if skill_md.parent.name == name
    ]
    if not matches:
        raise CanonicalSkillError(
            f"canonical skill is not available in the shared runtime: {name}"
        )
    return matches[0]


def skill_owners(skill_name: str) -> frozenset[str]:
    """Return the explicit semantic owner set for one canonical skill."""

    name = str(skill_name or "").strip()
    if not _SKILL_NAME_RE.fullmatch(name) or name not in CANONICAL_SKILLS:
        raise CanonicalSkillError(f"unknown or non-canonical skill: {skill_name!r}")
    owners = SKILL_OWNER_BY_NAME.get(name)
    if not owners:
        raise CanonicalSkillError(f"skill ownership is unresolved: {name}")
    return owners


def validate_skill_for_profile(
    skill_name: str,
    profile: str,
    *,
    root: Path | None = None,
) -> str:
    """Validate source availability and semantic owner before task creation."""

    if profile not in CANONICAL_PROFILES:
        raise CanonicalSkillError(f"unknown Hermes profile: {profile!r}")
    name = str(skill_name or "").strip()
    owners = skill_owners(name)
    if profile not in owners:
        raise CanonicalSkillError(
            f"skill/profile ownership mismatch: {name!r} cannot be assigned to {profile!r}"
        )
    resolve_canonical_skill(name, root=root)
    return name


def validate_skills_for_profile(
    skills: Iterable[str] | None,
    profile: str,
    *,
    root: Path | None = None,
) -> tuple[str, ...]:
    """Validate all required skills against one canonical profile owner."""

    if skills is None:
        return ()
    if isinstance(skills, (str, bytes)):
        raise CanonicalSkillError("required_skills must be an array of names")
    result: list[str] = []
    seen: set[str] = set()
    for raw_name in skills:
        name = validate_skill_for_profile(str(raw_name).strip(), profile, root=root)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


def validate_skills_for_profiles(
    skills: Iterable[str] | None,
    profiles: Iterable[str],
    *,
    root: Path | None = None,
) -> tuple[str, ...]:
    """Validate a flat planner skill list against selected profile owners."""

    selected = {str(profile).strip() for profile in profiles}
    unknown_profiles = selected - CANONICAL_PROFILES
    if unknown_profiles:
        raise CanonicalSkillError(f"unknown Hermes profiles: {sorted(unknown_profiles)}")
    if skills is None:
        return ()
    if isinstance(skills, (str, bytes)):
        raise CanonicalSkillError("required_skills must be an array of names")
    result: list[str] = []
    seen: set[str] = set()
    for raw_name in skills:
        name = str(raw_name).strip()
        owners = skill_owners(name)
        if not owners.intersection(selected):
            raise CanonicalSkillError(
                f"required skill has no selected owner profile: {name!r}"
            )
        resolve_canonical_skill(name, root=root)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


def validate_required_skills(
    skills: Iterable[str] | None,
    *,
    root: Path | None = None,
) -> tuple[str, ...]:
    """Validate and deduplicate skill names before Kanban task creation."""

    if skills is None:
        return ()
    if isinstance(skills, (str, bytes)):
        raise CanonicalSkillError("required_skills must be an array of names")
    result: list[str] = []
    seen: set[str] = set()
    for raw_name in skills:
        name = str(raw_name).strip()
        resolve_canonical_skill(name, root=root)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


__all__ = [
    "AMBIGUOUS_CUSTOM_SKILLS",
    "CANONICAL_SHARED_SKILL_ROOT",
    "CANONICAL_PROFILES",
    "CANONICAL_SKILLS",
    "CanonicalSkillError",
    "SHARED_PORTFOLIO_SKILL_PROFILES",
    "SKILL_OWNER_BY_NAME",
    "resolve_canonical_skill",
    "skill_owners",
    "validate_required_skills",
    "validate_skill_for_profile",
    "validate_skills_for_profile",
    "validate_skills_for_profiles",
]
