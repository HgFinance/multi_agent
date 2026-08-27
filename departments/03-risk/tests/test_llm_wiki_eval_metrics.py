from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "llm_wiki"))

from eval_metrics import exact_match, f1_score, normalize_answer


def test_normalize_answer_strips_punctuation_and_extra_whitespace() -> None:
    assert normalize_answer("  제178조,  부정거래행위!  ") == "제178조 부정거래행위"


def test_exact_match_ignores_whitespace_and_punctuation_differences() -> None:
    assert exact_match("제178조 부정거래행위 금지", "제178조, 부정거래행위 금지.")
    assert not exact_match("제178조 부정거래행위", "제176조 시세조종행위")


def test_f1_score_perfect_and_zero_overlap() -> None:
    assert f1_score("a b c", "a b c") == 1.0
    assert f1_score("a b c", "x y z") == 0.0


def test_f1_score_empty_prediction_or_gold_is_zero_unless_both_empty() -> None:
    assert f1_score("", "a b c") == 0.0
    assert f1_score("a b c", "") == 0.0
    assert f1_score("", "") == 1.0


def test_f1_score_partial_overlap_is_between_zero_and_one() -> None:
    score = f1_score("부정거래행위 금지 조항", "부정거래행위 금지")
    assert 0.0 < score < 1.0
