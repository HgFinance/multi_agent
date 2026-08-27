from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "llm_wiki"))

from llm_judge import judge


def test_judge_fails_closed_on_empty_prediction() -> None:
    result = judge("아무 질문", "정답", "")

    assert result == {"semantic_f1": 0.0, "correct": False, "reasoning": "empty prediction"}
