"""Compatibility entrypoint for the governed Evolution Skills pipeline.

The implementation lives in :mod:`orchestration.evolution_skills` so proposal
creation, promotion, runtime contracts, and tests use one lifecycle definition.

Run this file for a network-free contract check. Operational proposal and
promotion commands are exposed by ``scripts/evolution_skills.py``.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestration.evolution_skills import (  # re-export legacy imports
    MAX_SKILLS_PER_RUN,
    MIN_OCCURRENCES,
    OWNED_DEPARTMENTS,
    EvolutionSkillError,
    EvolutionSkillStore,
    Occurrence,
    SkillCandidate,
    check_boundary,
    detect_candidates,
    draft_body,
    promote_proposal,
    render_skill,
    retire_skill,
    validate_artifacts,
)


def forge(candidates, llm, *, skills_dir: Path, now=None) -> list[dict]:
    """Legacy proposal helper; it never promotes into the canonical registry."""

    del now  # timestamps are owned by the lifecycle store
    store = EvolutionSkillStore(skills_dir)
    results: list[dict] = []
    for candidate in candidates:
        try:
            state = store.create_proposal(
                candidate,
                llm,
                model_metadata={
                    "model_version": "qwen2.5-14b-instruct-awq",
                    "base_model": "qwen2.5-14b-instruct-awq",
                    "adapter_id": None,
                },
            )
        except EvolutionSkillError as exc:
            results.append({"slug": candidate.slug, "written": False, "reason": str(exc)})
            continue
        proposal = store.proposal_dir(state["proposal_id"])
        results.append(
            {
                "slug": candidate.slug,
                "written": True,
                "path": str(proposal / "SKILL.md"),
                "proposal_id": state["proposal_id"],
                "status": state["status"],
            }
        )
    return results


def _self_check() -> None:
    occurrences = [
        Occurrence(kind="tool timeout", run_id=f"r{i}", department="01-research")
        for i in range(3)
    ]
    candidates = detect_candidates(occurrences, department="01-research")
    assert len(candidates) == 1 and candidates[0].count == 3
    assert not detect_candidates(occurrences[:2], department="01-research")
    assert check_boundary("You are the research-agent")
    try:
        detect_candidates(occurrences, department="03-risk")
    except PermissionError:
        pass
    else:
        raise AssertionError("department boundary did not fail closed")

    body = (
        "# tool-timeout\n\n## 왜 필요한가\n반복된 지연을 확인한다.\n\n"
        "## 작업 순서\n실패 로그를 확인하고 동일 명령을 검증한다.\n\n"
        "## 하지 않을 것\n관측되지 않은 성공을 만들지 않는다.\n"
    )
    with tempfile.TemporaryDirectory() as raw:
        out = forge(candidates, lambda _prompt: body, skills_dir=Path(raw))
        assert out[0]["written"] is True
        proposal = Path(out[0]["path"]).parent
        assert (proposal / "provenance.json").is_file()
        assert (proposal / "state.json").is_file()
    print("skill_forge governed lifecycle checks: PASS")


if __name__ == "__main__":
    _self_check()


__all__ = [
    "MAX_SKILLS_PER_RUN",
    "MIN_OCCURRENCES",
    "OWNED_DEPARTMENTS",
    "EvolutionSkillError",
    "EvolutionSkillStore",
    "Occurrence",
    "SkillCandidate",
    "check_boundary",
    "detect_candidates",
    "draft_body",
    "forge",
    "promote_proposal",
    "render_skill",
    "retire_skill",
    "validate_artifacts",
]
