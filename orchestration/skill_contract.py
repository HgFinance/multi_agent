"""Repository-owned contract for Kanban and direct Hermes runtime skills.

Kanban profile identities and the direct Strategy Hermes runtime identity are
kept separate: Strategy Hermes can own its research skill without becoming a
Kanban assignee or a substitute for a department profile.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from types import MappingProxyType

from orchestration.evolution_skills import (
    EvolutionSkillError,
    active_registry_bindings,
)

CANONICAL_SHARED_SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills"

# 프로필 목록은 **여기서 다시 적지 않는다.** 2026-08-14 실측: 이 모듈이 자기
# 목록을 따로 들고 있었고 그 사이 창구 2종(research-liaison·quant-liaison)이
# 추가돼 두 계약이 갈렸다 - 창구에 스킬을 배정하면 "unknown Hermes profile" 로
# 거부되는데, 정작 에이전트는 이 경계를 안 지나므로 아무도 모른 채 굴러갔다.
# 정본은 canonical_profiles.py 하나다.
from orchestration.canonical_profiles import (
    CANONICAL_PROFILES,
)

# Direct runtimes are intentionally not added to canonical_profiles.py: that
# set is the assignee contract for Kanban. They still need an explicit skill
# owner so Research HQ cannot accidentally reclaim the Strategy Hermes skill.
STRATEGY_RUNTIME_PROFILES = frozenset({"strategy-hermes"})

# Repository-owned skill names.
#
# ▶ 소스가 아직 없는 항목은 아래 PENDING_SOURCE_SKILLS 에 **명시**한다.
#   2026-08-14 정정: 이 자리 주석이 오래 "financial-* 네 개가 없다"고 적고
#   있었는데 실제로는 두 개가 이미 들어와 있었다. 산문 주석은 낡아도 아무도
#   모른다 - 감사 스크립트(scripts/audit_contracts.py)가 읽을 수 있게 집합으로
#   옮긴다. 계약에 이름만 남기는 이유는 그대로다: 소스가 생기기 전까지 카드가
#   그 스킬을 실행 가능하다고 착각하면 안 된다.
STATIC_CANONICAL_SKILLS = frozenset(
    {
        "agentic-rag",
        # 2026-08-13 공장 능동화 3종: 병목을 만난 부서가 스스로 진단·구축·기록한다.
        # 소유는 공장 개선 카드가 걸리는 부서(research/quant, wiring-audit 은 QA 포함).
        "dataset-engineering",
        "autonomous-quant-research",
        "equity-quant-assessment",
        "financial-equity-research",
        "financial-portfolio-assessment",
        "financial-research-memos",
        "financial-risk-research",
        "hermes-multi-agent-pipelines",
        "hermes-memory",
        "ls-accounting-evidence",
        "methodology-scout",
        "skill-authoring",
        # 2026-08-13: 카탈로그 우선 탐색 - "없다" 선언 전 정보원 4층 검색 규율.
        # KRX 유료 결제를 막은 실측(t3320 발견)이 탄생 계기.
        "source-catalog-first",
        "wiring-audit",
    }
)

# 계약에는 있으나 이 체크아웃에 소스가 아직 없는 스킬. 감사가 "누락"이 아니라
# "대기"로 구분해 보고한다. 소스가 들어오면 이 집합에서 빼는 것이 완료 신호다.
PENDING_SOURCE_SKILLS = frozenset(
    {
        "financial-research-memos",
        "financial-risk-research",
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
_STATIC_SKILL_OWNER_BY_NAME = {
        "agentic-rag": frozenset({"risk-management", "qa-department"}),
        "dataset-engineering": frozenset(
            {"quant-backtest-department", "research-department"}
        ),
        # Strategy Hermes invokes this directly; Research HQ only supplies
        # evidence/data contracts and must not claim the execution skill.
        "autonomous-quant-research": frozenset({"strategy-hermes"}),
        "equity-quant-assessment": frozenset({"quant-backtest-department"}),
        "financial-equity-research": frozenset({"research-department"}),
        "financial-portfolio-assessment": SHARED_PORTFOLIO_SKILL_PROFILES,
        "financial-research-memos": frozenset({"research-department"}),
        "financial-risk-research": frozenset({"risk-management"}),
        "hermes-multi-agent-pipelines": frozenset({"ceo-agent"}),
        "hermes-memory": frozenset({"ceo-agent"}),
        "ls-accounting-evidence": frozenset({"accounting-portfolio-department"}),
        "methodology-scout": frozenset({"research-department"}),
        "skill-authoring": frozenset(
            {"quant-backtest-department", "research-department"}
        ),
        # 창구도 소유자다 - "없다"고 답하기 전에 카탈로그를 뒤지는 규율은
        # 사용자 질의를 받는 쪽에 가장 필요하다(2026-08-14 창구 카드가 실제로
        # 이 스킬로 t1637 을 찾아냈다).
        "source-catalog-first": frozenset(
            {"research-department", "quant-backtest-department", "qa-department",
             "research-liaison", "quant-liaison"}
        ),
        "wiring-audit": frozenset(
            {"quant-backtest-department", "research-department", "qa-department"}
        ),
    }

EVOLUTION_SKILL_REGISTRY = Path(
    os.environ.get(
        "EVOLUTION_SKILL_REGISTRY_PATH",
        str(CANONICAL_SHARED_SKILL_ROOT / "evolution-registry.json"),
    )
).expanduser().resolve()
try:
    ACTIVE_EVOLUTION_SKILLS, _EVOLUTION_SKILL_OWNERS = active_registry_bindings(
        EVOLUTION_SKILL_REGISTRY
    )
except EvolutionSkillError as exc:
    # A corrupt registry must stop task creation rather than silently exposing
    # an unowned generated skill.
    raise RuntimeError(f"invalid evolution skill registry: {exc}") from exc

REGISTERED_EVOLUTION_SKILLS = frozenset(_EVOLUTION_SKILL_OWNERS)
CANONICAL_SKILLS = STATIC_CANONICAL_SKILLS | REGISTERED_EVOLUTION_SKILLS
SKILL_OWNER_BY_NAME = MappingProxyType(
    {**_STATIC_SKILL_OWNER_BY_NAME, **_EVOLUTION_SKILL_OWNERS}
)

# This skill is intentionally not assigned until a domain-specific owner is
# established. Keeping it out of the owner map makes task assignment fail
# closed instead of treating a QA-local copy as globally reusable.
AMBIGUOUS_CUSTOM_SKILLS = frozenset({"hermes-agent-integration"})
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")


class CanonicalSkillError(ValueError):
    """Raised before a task with an unresolvable skill can be created."""


def _live_evolution_contract() -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    """Reload the small registry so activation does not require a process restart."""

    try:
        return active_registry_bindings(EVOLUTION_SKILL_REGISTRY)
    except EvolutionSkillError as exc:
        raise CanonicalSkillError(f"invalid evolution skill registry: {exc}") from exc


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
    active_evolved, evolved_owners = _live_evolution_contract()
    known = STATIC_CANONICAL_SKILLS | frozenset(evolved_owners)
    if not _SKILL_NAME_RE.fullmatch(name) or name not in known:
        raise CanonicalSkillError(f"unknown or non-canonical skill: {skill_name!r}")
    if name in evolved_owners and name not in active_evolved:
        raise CanonicalSkillError(f"evolution skill is not active: {name}")
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
    _, evolved_owners = _live_evolution_contract()
    known = STATIC_CANONICAL_SKILLS | frozenset(evolved_owners)
    if not _SKILL_NAME_RE.fullmatch(name) or name not in known:
        raise CanonicalSkillError(f"unknown or non-canonical skill: {skill_name!r}")
    owners = evolved_owners.get(name) or _STATIC_SKILL_OWNER_BY_NAME.get(name)
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

    if profile not in CANONICAL_PROFILES | STRATEGY_RUNTIME_PROFILES:
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
    unknown_profiles = selected - (CANONICAL_PROFILES | STRATEGY_RUNTIME_PROFILES)
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
    "ACTIVE_EVOLUTION_SKILLS",
    "AMBIGUOUS_CUSTOM_SKILLS",
    "CANONICAL_PROFILES",
    "CANONICAL_SHARED_SKILL_ROOT",
    "CANONICAL_SKILLS",
    "EVOLUTION_SKILL_REGISTRY",
    "PENDING_SOURCE_SKILLS",
    "REGISTERED_EVOLUTION_SKILLS",
    "SHARED_PORTFOLIO_SKILL_PROFILES",
    "SKILL_OWNER_BY_NAME",
    "STRATEGY_RUNTIME_PROFILES",
    "STATIC_CANONICAL_SKILLS",
    "CanonicalSkillError",
    "resolve_canonical_skill",
    "skill_owners",
    "validate_required_skills",
    "validate_skill_for_profile",
    "validate_skills_for_profile",
    "validate_skills_for_profiles",
]
