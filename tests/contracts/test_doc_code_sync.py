"""문서에 적힌 **편제 수**가 코드와 같은지 대조한다.

▶ 왜 이 테스트가 있나 (2026-08-12)
  이 저장소는 편제를 네 곳에 적는다.
    1. 각 부서 `hermes/config.yaml` 의 `workers`   (프로필 정본)
    2. 각 부서 `employee_workers.py` 의 WORKER_SPECS (런타임 정본)
    3. `docs/02-engineering/WORKER_ROLE_BOUNDARIES.md` 의 표와 합계 문장
    4. `CLAUDE.md` 의 아키텍처 문단

  1·2 는 `test_worker_architecture.py` 가 이미 대조한다. 그런데 3·4 는 아무도
  안 봤다. 그 결과 2026-08-10~11 에 Research 6->2, Quant 7->2 로 줄인 변경이
  코드·프로필·테스트에는 반영됐는데 **문서 두 곳에는 19 로 남았고**, 그 19 를
  CLAUDE.md 가 물려받아 "LLM Worker 19명"으로 계속 안내하고 있었다.
  러너도 실제 5개인데 CLAUDE.md 는 4개라고 적고 있었다.

  문서를 또 쓰는 것으로는 안 풀린다 - 그 문서도 낡는다. `evidence/methods.py` 가
  "방법론을 문서에만 적으면 구현과 어긋난다"고 한 것과 같은 이유로, 숫자를
  **실행되는 대조**에 건다. 편제를 바꾸면 여기서 걸리고, 네 곳을 같이 고치게 된다.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
BOUNDARIES = ROOT / "docs" / "02-engineering" / "WORKER_ROLE_BOUNDARIES.md"
CLAUDE_MD = ROOT / "CLAUDE.md"

# 부서 키 -> hermes 프로필 경로. test_worker_architecture 와 같은 목록이다.
DEPARTMENT_DIRS = {
    "ceo": "00-ceo-office",
    "research": "01-research",
    "trading": "02-trading",
    "risk": "03-risk",
    "quant-backtest": "04-quant-backtest",
    "accounting-portfolio": "05-accounting-portfolio",
    "qa": "06-ai-qa-audit",
    "hr": "07-agent-workforce",
}

# 결정론 러너. 코드에서 RUNNER_ID 로 확인한다.
RUNNER_MODULES = {
    "desk-runner": "departments/02-trading/employee_workers.py",
    "risk-runner": "departments/03-risk/risk_employee_workers.py",
    "qa-runner": "departments/06-ai-qa-audit/qa_employee_workers.py",
    "back-office-runner": "departments/05-accounting-portfolio/employee_workers.py",
    "ceo-runner": "departments/00-ceo-office/employee_workers.py",
}


def profile_worker_count(directory: str) -> int:
    """부서 프로필이 선언한 LLM Worker 수(정본)."""
    path = ROOT / "departments" / directory / "hermes" / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    return len(config.get("workers") or {})


def code_worker_count() -> int:
    """전 부서 WORKER_SPECS 합계. 파일을 읽어 센다 - import 하지 않는다.

    import 하면 langgraph·ollama 같은 런타임 의존이 딸려와 계약 테스트가
    무거워진다. 여기서 필요한 것은 **개수**뿐이다.
    """
    total = 0
    for directory in DEPARTMENT_DIRS.values():
        folder = ROOT / "departments" / directory
        for name in ("employee_workers.py", "risk_employee_workers.py",
                     "qa_employee_workers.py"):
            path = folder / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            block = re.search(
                r"^WORKER_SPECS[^=]*=\s*\((.*?)^\)", text, re.S | re.M
            )
            if block is None:
                continue
            total += len(re.findall(r"\bWorkerSpec\(", block.group(1)))
    return total


class WorkerHeadcountDocSyncTest(unittest.TestCase):
    """문서의 숫자가 프로필·코드와 같은가."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.profile_total = sum(
            profile_worker_count(d) for d in DEPARTMENT_DIRS.values()
        )
        cls.boundaries = BOUNDARIES.read_text(encoding="utf-8")
        cls.claude = CLAUDE_MD.read_text(encoding="utf-8")

    def test_profile_and_code_agree(self) -> None:
        """프로필 선언과 실제 WORKER_SPECS 가 같은 수여야 한다."""
        self.assertEqual(
            self.profile_total,
            code_worker_count(),
            "부서 hermes/config.yaml 의 workers 수와 WORKER_SPECS 수가 다릅니다 - "
            "둘 중 하나만 고친 상태입니다",
        )

    def test_boundaries_doc_total_matches_code(self) -> None:
        """WORKER_ROLE_BOUNDARIES.md 의 합계 문장이 실제와 같아야 한다."""
        match = re.search(r"LLM Worker\s*\*{0,2}(\d+)\s*개", self.boundaries)
        self.assertIsNotNone(
            match, "WORKER_ROLE_BOUNDARIES.md 에서 'LLM Worker N개' 문장을 못 찾았습니다"
        )
        assert match is not None
        self.assertEqual(
            int(match.group(1)),
            self.profile_total,
            f"WORKER_ROLE_BOUNDARIES.md 는 {match.group(1)}명이라고 적었는데 "
            f"부서 프로필 합계는 {self.profile_total}명입니다 - 편제를 바꾸고 "
            f"문서를 안 고쳤습니다",
        )

    def test_claude_md_total_matches_code(self) -> None:
        """CLAUDE.md 의 안내 숫자가 실제와 같아야 한다."""
        match = re.search(r"LLM Worker\s*\*{0,2}(\d+)\s*명", self.claude)
        self.assertIsNotNone(
            match, "CLAUDE.md 에서 'LLM Worker N명' 문장을 못 찾았습니다"
        )
        assert match is not None
        self.assertEqual(
            int(match.group(1)),
            self.profile_total,
            f"CLAUDE.md 는 {match.group(1)}명이라고 적었는데 실제는 "
            f"{self.profile_total}명입니다",
        )

    def test_runner_count_matches_code(self) -> None:
        """결정론 러너 수가 문서·코드에서 같아야 한다."""
        present = []
        for runner_id, rel in RUNNER_MODULES.items():
            text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
            if re.search(rf'RUNNER_ID\s*=\s*["\']{re.escape(runner_id)}["\']', text):
                present.append(runner_id)
        self.assertEqual(
            sorted(present),
            sorted(RUNNER_MODULES),
            "RUNNER_ID 상수가 없는 러너가 있습니다 - 러너를 이름으로 셀 수 없습니다",
        )

        for doc_name, text, pattern in (
            ("WORKER_ROLE_BOUNDARIES.md", self.boundaries, r"결정론 Worker\s*(\d+)\s*개"),
            ("CLAUDE.md", self.claude, r"결정론 러너\s*\*{0,2}(\d+)\s*개"),
        ):
            match = re.search(pattern, text)
            self.assertIsNotNone(match, f"{doc_name} 에서 러너 수 문장을 못 찾았습니다")
            assert match is not None
            self.assertEqual(
                int(match.group(1)),
                len(present),
                f"{doc_name} 는 러너를 {match.group(1)}개라고 적었는데 코드에는 "
                f"{len(present)}개 있습니다: {sorted(present)}",
            )


if __name__ == "__main__":
    unittest.main()
