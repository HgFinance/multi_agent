"""Canonical shared Hermes skill contract tests."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from orchestration.skill_contract import (
    AMBIGUOUS_CUSTOM_SKILLS,
    PENDING_SOURCE_SKILLS,
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

    def test_unavailable_aws_only_sources_are_not_faked_in_repo(self) -> None:
        self.assertEqual(
            PENDING_SOURCE_SKILLS,
            {"financial-research-memos", "financial-risk-research"},
        )
        for skill in PENDING_SOURCE_SKILLS:
            with self.subTest(skill=skill):
                with self.assertRaises(CanonicalSkillError):
                    resolve_canonical_skill(skill, root=ROOT / "skills")

        for skill in ("financial-equity-research", "equity-quant-assessment"):
            with self.subTest(skill=skill):
                self.assertEqual(
                    resolve_canonical_skill(skill, root=ROOT / "skills").parent.name,
                    skill,
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
