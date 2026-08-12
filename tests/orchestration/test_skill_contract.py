"""Canonical shared Hermes skill contract tests."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from orchestration.skill_contract import (
    AMBIGUOUS_CUSTOM_SKILLS,
    CANONICAL_SKILLS,
    CanonicalSkillError,
    SKILL_OWNER_BY_NAME,
    resolve_canonical_skill,
    validate_required_skills,
    validate_skill_for_profile,
)


ROOT = Path(__file__).resolve().parents[2]
REQUIRED_PROFILES = (
    "00-ceo-office",
    "01-research",
    "03-risk",
    "04-quant-backtest",
    "05-accounting-portfolio",
    "06-ai-qa-audit",
)
ALL_PROFILE_SOURCES = {
    "ceo-agent": "00-ceo-office",
    "research-department": "01-research",
    "trading-department": "02-trading",
    "risk-management": "03-risk",
    "quant-backtest-department": "04-quant-backtest",
    "accounting-portfolio-department": "05-accounting-portfolio",
    "qa-department": "06-ai-qa-audit",
    "hr-department": "07-agent-workforce",
}


class SharedSkillContractTest(unittest.TestCase):
    def test_canonical_skill_resolves_from_project_source(self) -> None:
        path = resolve_canonical_skill(
            "financial-portfolio-assessment",
            root=ROOT / "skills",
        )
        self.assertEqual(path, ROOT / "skills/finance/financial-portfolio-assessment/SKILL.md")

    def test_unknown_skill_fails_before_task_creation(self) -> None:
        with self.assertRaises(CanonicalSkillError):
            validate_required_skills(["does-not-exist"], root=ROOT / "skills")

    def test_repository_custom_sources_are_present(self) -> None:
        for skill in (
            "agentic-rag",
            "experiment-factory",
            "financial-portfolio-assessment",
            "hermes-multi-agent-pipelines",
            "methodology-scout",
        ):
            with self.subTest(skill=skill):
                self.assertEqual(
                    resolve_canonical_skill(skill, root=ROOT / "skills").parent.name,
                    skill,
                )

    def test_owner_contract_rejects_wrong_profile_skill_pairs(self) -> None:
        valid = (
            ("methodology-scout", "research-department"),
            ("experiment-factory", "quant-backtest-department"),
            ("agentic-rag", "risk-management"),
            ("financial-portfolio-assessment", "qa-department"),
        )
        for skill, profile in valid:
            with self.subTest(skill=skill, profile=profile):
                self.assertEqual(
                    validate_skill_for_profile(skill, profile, root=ROOT / "skills"),
                    skill,
                )

        invalid = (
            ("methodology-scout", "quant-backtest-department"),
            ("experiment-factory", "research-department"),
            ("agentic-rag", "ceo-agent"),
            ("financial-portfolio-assessment", "trading-department"),
        )
        for skill, profile in invalid:
            with self.subTest(skill=skill, profile=profile):
                with self.assertRaises(CanonicalSkillError):
                    validate_skill_for_profile(skill, profile, root=ROOT / "skills")

    def test_ceo_does_not_own_specialist_financial_skills(self) -> None:
        for skill in (
            "financial-research-memos",
            "financial-equity-research",
            "equity-quant-assessment",
            "financial-risk-research",
        ):
            self.assertNotIn("ceo-agent", SKILL_OWNER_BY_NAME[skill])

    def test_declared_but_absent_sources_are_not_resolvable(self) -> None:
        """소스가 **아직 없는** canonical skill 은 해결되면 안 된다.

        ▶ 2026-08-12 병합에서 이름과 목록을 함께 고쳤다.
          원래 이름은 `test_unavailable_aws_only_sources_are_not_faked_in_repo`
          였고 financial-* 4개를 **하드코딩**해 "해결되면 실패"로 두었다. 그런데
          `skill_contract.py` 의 주석이 정한 규율은 "저장소에 두지 마라"가 아니라
          **"canonical repository source 가 생기기 전에는 실행 가능하게 하지 마라"**
          이다("Repository-owned skill names ... a task may not make them
          executable until the canonical repository source exists").

          그 사이 실제로 소스가 들어왔다 - financial-equity-research(skills/
          research/), equity-quant-assessment(skills/quant/), financial-
          portfolio-assessment(skills/finance/). 하드코딩 목록은 그 사실을
          모르니 **규율을 지킨 기여가 규율 위반으로 잡혔다.**

          그래서 목록을 지우고 **계산**한다. 소스가 오면 자동으로 대상에서 빠지고,
          소스 없이 계약에만 올린 이름은 계속 걸린다 - 규율은 오히려 강해진다.
        """
        absent = []
        for skill in sorted(CANONICAL_SKILLS):
            try:
                resolve_canonical_skill(skill, root=ROOT / "skills")
            except CanonicalSkillError:
                absent.append(skill)

        self.assertTrue(
            absent,
            "계약에 올라간 skill 이 전부 해결된다면 이 검사는 아무것도 지키지 않는다 - "
            "검사가 장식이 되었는지 확인할 것",
        )
        for skill in absent:
            with self.subTest(skill=skill):
                # 소스가 없는 이름은 **반드시** 예외여야 한다. 여기서 조용히
                # 통과하면 태스크가 없는 스킬을 강제 로드하려다 실행 시점에 죽는다.
                with self.assertRaises(CanonicalSkillError):
                    resolve_canonical_skill(skill, root=ROOT / "skills")

    def test_repo_skills_are_all_declared_in_contract(self) -> None:
        """저장소에 있는 스킬은 **전부 계약에 선언돼 있어야** 한다.

        위 검사(선언됐는데 소스 없음)의 반대 방향이다. 원본 테스트 이름이
        말하던 "저장소에 가짜로 만들지 마라"를 기계로 지킬 수 있는 형태가 이쪽이다 -
        정본인지 아닌지는 코드가 판정할 수 없지만, **계약에 없는 이름이 스킬처럼
        놓여 있는 것**은 판정할 수 있다. 그게 태스크가 강제 로드할 수 있는
        표면을 계약 밖에서 넓히는 유일한 경로다.
        """
        present = {p.parent.name for p in (ROOT / "skills").rglob("SKILL.md")}
        undeclared = sorted(present - set(CANONICAL_SKILLS))
        self.assertFalse(
            undeclared,
            f"계약(CANONICAL_SKILLS)에 없는 스킬이 저장소에 있습니다: {undeclared}. "
            f"skill_contract.py 에 등재하거나 파일을 빼야 합니다 - 계약 밖 스킬은 "
            f"소유 부서·프로필 검증을 통과하지 않은 채 로드될 수 있습니다.",
        )

    def test_ambiguous_skill_is_not_assigned_by_contract(self) -> None:
        self.assertIn("hermes-agent-integration", AMBIGUOUS_CUSTOM_SKILLS)
        self.assertNotIn("hermes-agent-integration", SKILL_OWNER_BY_NAME)

    def test_required_profiles_use_shared_external_root(self) -> None:
        for profile in REQUIRED_PROFILES:
            with self.subTest(profile=profile):
                config = yaml.safe_load(
                    (ROOT / "departments" / profile / "hermes/config.yaml").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    config["skills"]["external_dirs"], ["/opt/shared-skills"]
                )

    def test_all_eight_profiles_have_isolated_source_directories(self) -> None:
        for profile, folder in ALL_PROFILE_SOURCES.items():
            with self.subTest(profile=profile):
                source_dir = ROOT / "departments" / folder
                self.assertTrue((source_dir / "hermes/config.yaml").is_file())
                self.assertTrue((source_dir / "hermes/SOUL.md").is_file())


if __name__ == "__main__":
    unittest.main()
