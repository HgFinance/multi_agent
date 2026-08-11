"""Repository-owned contract for skills force-loaded by Kanban tasks."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path


CANONICAL_SHARED_SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills"
CANONICAL_SKILLS = frozenset({"financial-portfolio-assessment"})
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
    "CANONICAL_SHARED_SKILL_ROOT",
    "CANONICAL_SKILLS",
    "CanonicalSkillError",
    "resolve_canonical_skill",
    "validate_required_skills",
]
