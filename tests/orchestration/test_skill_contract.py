"""Canonical shared Hermes skill contract tests."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from orchestration.skill_contract import (
    CanonicalSkillError,
    resolve_canonical_skill,
    validate_required_skills,
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


if __name__ == "__main__":
    unittest.main()
