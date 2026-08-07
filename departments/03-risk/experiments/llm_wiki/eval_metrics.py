"""SQuAD 스타일 토큰 단위 F1/EM — hand-rolled, 신규 의존성 없음.

영어 SQuAD normalize_answer(소문자화, 구두점/관사 제거)는 한국어에 그대로 안 맞으므로
관사 제거 규칙은 뺀다. 토큰화는 bm25.py의 `tokenize`(unicode `\\w+`, casefold)를
그대로 재사용해 두 모듈의 "단어"에 대한 정의를 어긋나지 않게 맞춘다.
"""

from __future__ import annotations

import re
from collections import Counter

from bm25 import tokenize

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_answer(text: str) -> str:
    text = text.casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def exact_match(prediction: str, gold: str) -> bool:
    return normalize_answer(prediction) == normalize_answer(gold)


def f1_score(prediction: str, gold: str) -> float:
    pred_tokens = tokenize(normalize_answer(prediction))
    gold_tokens = tokenize(normalize_answer(gold))
    if not pred_tokens or not gold_tokens:
        return float(pred_tokens == gold_tokens)

    common = Counter(pred_tokens) & Counter(gold_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0

    precision = num_same / len(pred_tokens)
    recall = num_same / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


if __name__ == "__main__":
    assert exact_match("제178조 부정거래행위 금지", "제178조 부정거래행위 금지")
    assert exact_match(" 제178조  부정거래행위 금지 ", "제178조 부정거래행위 금지")
    assert not exact_match("제178조 부정거래행위", "제178조 시세조종행위")

    assert f1_score("a b c", "a b c") == 1.0
    assert f1_score("", "a b c") == 0.0
    assert f1_score("a b c", "") == 0.0
    partial = f1_score("부정거래행위 금지 조항", "부정거래행위 금지")
    assert 0.0 < partial < 1.0, partial

    print("eval_metrics self-check OK:", partial)
