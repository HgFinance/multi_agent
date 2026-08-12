"""스킬 계약의 **양방향 덮개**. `test_skill_contract.py` 는 건드리지 않는다.

▶ 왜 파일을 따로 두나
  `test_skill_contract.py` 는 팀원이 관리하는 파일이고, 거기 손대지 않는 것이
  이 브랜치의 전제다. 그래서 같은 규율을 **덧붙이기만** 한다.

▶ 무엇을 지키나
  `orchestration/skill_contract.py` 의 주석이 정한 규율은 이렇다.

      "Repository-owned skill names. The four financial-* entries are retained
       in the contract even while their source is absent from this checkout:
       a task may not make them executable until the canonical repository
       source exists."

  읽어보면 금지 대상은 **"저장소에 파일을 두는 것"이 아니라 "canonical source
  가 생기기 전에 실행 가능하게 만드는 것"**이다. 그 규율은 두 방향으로 깨진다.

      (1) 계약에 선언 O / 소스 X  -> 태스크가 로드하려다 실행 시점에 죽는다
      (2) 계약에 선언 X / 소스 O  -> 소유 부서·프로필 검증을 **건너뛴** 스킬이
                                     로드될 수 있다. 계약 밖에서 실행 표면이
                                     넓어지는 유일한 경로다.

  아래 두 검사가 각각을 막는다. (1) 은 소스가 들어오면 자동으로 대상에서
  빠지도록 **계산**한다 - 하드코딩 목록은 소스가 실제로 도착한 사실을 모르므로,
  규율을 지킨 기여를 규율 위반으로 잡는다.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from orchestration.skill_contract import (
    CANONICAL_SKILLS,
    CanonicalSkillError,
    resolve_canonical_skill,
)

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"


class SkillContractCoverageTest(unittest.TestCase):
    def test_declared_without_source_is_never_resolvable(self) -> None:
        """(1) 선언은 됐는데 소스가 없는 이름은 **반드시** 예외여야 한다."""
        absent = []
        for skill in sorted(CANONICAL_SKILLS):
            try:
                resolve_canonical_skill(skill, root=SKILLS)
            except CanonicalSkillError:
                absent.append(skill)

        for skill in absent:
            with self.subTest(skill=skill):
                # 여기서 조용히 통과하면 태스크가 없는 스킬을 강제 로드하려다
                # 실행 시점에 죽는다 - 접수 때 걸러야 할 것이 실행까지 간다.
                with self.assertRaises(CanonicalSkillError):
                    resolve_canonical_skill(skill, root=SKILLS)

    def test_source_without_declaration_is_rejected(self) -> None:
        """(2) 저장소에 있는 스킬은 전부 계약에 선언돼 있어야 한다.

        정본인지 아닌지는 코드가 판정할 수 없지만, **계약에 없는 이름이 스킬처럼
        놓여 있는 것**은 판정할 수 있다. 그게 기계로 지킬 수 있는 경계다.
        """
        present = {p.parent.name for p in SKILLS.rglob("SKILL.md")}
        undeclared = sorted(present - set(CANONICAL_SKILLS))
        self.assertFalse(
            undeclared,
            f"계약(CANONICAL_SKILLS)에 없는 스킬이 저장소에 있습니다: {undeclared}. "
            f"skill_contract.py 에 등재하거나 파일을 빼야 합니다 - 계약 밖 스킬은 "
            f"소유 부서·프로필 검증을 통과하지 않은 채 로드될 수 있습니다.",
        )


if __name__ == "__main__":
    unittest.main()
