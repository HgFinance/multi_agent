"""Canonical shared Hermes skill contract tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from orchestration.skill_contract import (
    AMBIGUOUS_CUSTOM_SKILLS,
    PENDING_SOURCE_SKILLS,
    SKILL_OWNER_BY_NAME,
    CanonicalSkillError,
    active_task_skills_for_profile,
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
    def test_active_evolved_skill_is_task_injected_only_for_its_owner(self) -> None:
        import orchestration.skill_contract as contract

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "skills/evolved/ceo-bounded-react/SKILL.md"
            source.parent.mkdir(parents=True)
            source.write_text(
                "---\n"
                "name: ceo-bounded-react\n"
                "description: QA verified CEO procedure\n"
                "version: 1.0.0\n"
                "metadata:\n"
                "  hermes:\n"
                "    task_activation: owner-task\n"
                "---\n\n"
                "# ceo-bounded-react\n\n"
                "## 왜 필요한가\nQA 검증 절차입니다.\n\n"
                "## 작업 순서\n증거를 확인합니다.\n\n"
                "## 하지 않을 것\n권한을 바꾸지 않습니다.\n",
                encoding="utf-8",
            )
            registry = root / "skills/evolution-registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "registry_version": "hgfinance.evolution-skill-registry.v1",
                        "skills": {
                            "ceo-bounded-react": {
                                "classification": "evolved",
                                "status": "active",
                                "owner_profiles": ["ceo-agent"],
                                "current_version": 1,
                                "source": "skills/evolved/ceo-bounded-react/SKILL.md",
                                "content_hash": hashlib.sha256(
                                    source.read_bytes()
                                ).hexdigest(),
                                "approved_by": "discord:test",
                                "qa_verdict": "PASS",
                                "activated_at": "2026-08-31T00:00:00+00:00",
                                "replacement": None,
                                "proposal_id": "ceo-bounded-react-v1-aaaaaaaaaaaa",
                            }
                        },
                        "project_skills": {},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(contract, "EVOLUTION_SKILL_REGISTRY", registry):
                self.assertEqual(
                    active_task_skills_for_profile("ceo-agent", root=root / "skills"),
                    ("ceo-bounded-react",),
                )
                self.assertEqual(
                    active_task_skills_for_profile(
                        "research-department", root=root / "skills"
                    ),
                    (),
                )

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
            "autonomous-quant-research",
            "financial-portfolio-assessment",
            "hermes-multi-agent-pipelines",
            "hermes-memory",
            "methodology-scout",
        ):
            with self.subTest(skill=skill):
                self.assertEqual(
                    resolve_canonical_skill(skill, root=ROOT / "skills").parent.name,
                    skill,
                )

    def test_owner_contract_rejects_wrong_profile_skill_pairs(self) -> None:
        valid = (
            ("hermes-memory", "ceo-agent"),
            ("methodology-scout", "research-department"),
            ("autonomous-quant-research", "strategy-hermes"),
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
            ("hermes-memory", "research-department"),
            ("methodology-scout", "quant-backtest-department"),
            ("autonomous-quant-research", "research-department"),
            ("agentic-rag", "ceo-agent"),
            ("financial-portfolio-assessment", "trading-department"),
        )
        for skill, profile in invalid:
            with self.subTest(skill=skill, profile=profile), self.assertRaises(
                CanonicalSkillError
            ):
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
            with self.subTest(skill=skill), self.assertRaises(CanonicalSkillError):
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
