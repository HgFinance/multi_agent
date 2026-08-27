"""계약이 한 곳에서만 정의되는지 고정한다.

2026-08-14 실측: 같은 개념이 두 모듈에 각각 적혀 있으면 조용히 갈라진다.
`skill_contract.py` 가 자기 프로필 목록을 들고 있었고, 그 사이 창구 2종이
추가돼 두 계약이 어긋났다 - 창구에 스킬을 배정하면 "unknown Hermes profile"
로 거부되는데 에이전트는 그 경계를 안 지나므로 아무도 모른 채 굴러갔다.

여기서 막는 것은 "선언끼리의 불일치"다. 선언과 **런타임**의 불일치는
`scripts/audit_contracts.py --runtime` 이 본다(컨테이너가 필요해 테스트에 넣지 않는다).
"""

from __future__ import annotations

import unittest
from pathlib import Path

from orchestration.canonical_profiles import (
    CANONICAL_PROFILES,
    LEGACY_PROFILE_ALIASES,
    CanonicalProfileError,
    canonical_profile_for_department,
    validate_canonical_profile,
)
from orchestration.skill_contract import (
    CANONICAL_PROFILES as SKILL_CONTRACT_PROFILES,
)
from orchestration.skill_contract import (
    CANONICAL_SKILLS,
    PENDING_SOURCE_SKILLS,
    SKILL_OWNER_BY_NAME,
    STRATEGY_RUNTIME_PROFILES,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class ProfileContractIsSingleSourceTest(unittest.TestCase):
    def test_skill_contract_reuses_the_canonical_profile_set(self) -> None:
        # 값이 같은 것으로는 부족하다 - **같은 객체**여야 재정의가 다시 생기지 않는다.
        self.assertIs(SKILL_CONTRACT_PROFILES, CANONICAL_PROFILES)

    def test_liaison_profiles_are_in_the_contract(self) -> None:
        for profile in ("research-liaison", "quant-liaison"):
            self.assertIn(profile, CANONICAL_PROFILES)

    def test_legacy_aliases_never_collide_with_canonical_names(self) -> None:
        self.assertFalse(set(LEGACY_PROFILE_ALIASES) & set(CANONICAL_PROFILES))

    def test_legacy_alias_targets_are_canonical(self) -> None:
        for alias, target in LEGACY_PROFILE_ALIASES.items():
            self.assertIn(target, CANONICAL_PROFILES, f"{alias} -> {target}")

    def test_known_bad_names_are_rejected(self) -> None:
        # 실제로 카드에 찍혔던 이름들이다(22 장이 이틀간 정체했다).
        for bad in ("ai-qa-audit", "risk-department", "workforce-management"):
            with self.assertRaises(CanonicalProfileError):
                validate_canonical_profile(bad)

    def test_department_codes_still_resolve(self) -> None:
        self.assertEqual(canonical_profile_for_department("qa"), "qa-department")
        self.assertEqual(canonical_profile_for_department("risk"), "risk-management")


class SkillContractConsistencyTest(unittest.TestCase):
    def test_every_skill_has_an_owner(self) -> None:
        self.assertEqual(set(CANONICAL_SKILLS) - set(SKILL_OWNER_BY_NAME), set())

    def test_every_owner_is_a_canonical_profile(self) -> None:
        for skill, owners in SKILL_OWNER_BY_NAME.items():
            self.assertEqual(
                set(owners) - (set(CANONICAL_PROFILES) | set(STRATEGY_RUNTIME_PROFILES)),
                set(),
                f"skill={skill}",
            )

    def test_declared_skills_exist_unless_explicitly_pending(self) -> None:
        available = {p.parent.name for p in (REPO_ROOT / "skills").rglob("SKILL.md")}
        missing = set(CANONICAL_SKILLS) - available - set(PENDING_SOURCE_SKILLS)
        self.assertEqual(missing, set(), "계약에 있는데 소스가 없다(대기 목록에도 없다)")

    def test_pending_set_shrinks_when_source_arrives(self) -> None:
        # 소스가 들어왔는데 대기 목록에 남아 있으면 그 자체가 낡은 선언이다.
        available = {p.parent.name for p in (REPO_ROOT / "skills").rglob("SKILL.md")}
        self.assertEqual(
            set(PENDING_SOURCE_SKILLS) & available, set(),
            "소스가 들어왔으니 PENDING_SOURCE_SKILLS 에서 빼라",
        )


if __name__ == "__main__":
    unittest.main()
