"""SQuAD 스타일 토큰 단위 F1/EM — hand-rolled, 신규 의존성 없음.

영어 SQuAD normalize_answer(소문자화, 구두점/관사 제거)는 한국어에 그대로 안 맞으므로
관사 제거 규칙은 뺀다. 토큰화는 bm25.py의 `tokenize`(unicode `\\w+`, casefold)를
그대로 재사용해 두 모듈의 "단어"에 대한 정의를 어긋나지 않게 맞춘다.

# ponytail: f1_score만 흔한 조사(을/를/이/가/의/에서 등)를 토큰 끝에서 하나 잘라내는
# 얕은 휴리스틱을 쓴다(형태소 분석기 없이) — "부정거래행위"와 "부정거래행위를"이
# 조사 차이만으로 다른 토큰 취급되는 걸 줄이기 위함. 용언 활용(-하다/-된다 등)은
# 다루지 않는다 — 그건 진짜 형태소 분석이 필요해서 손대면 오탐(과잉 절단)이 더
# 커진다. 이 리스트로도 재현율이 부족하면 mecab/konlpy 추가를 검토한다. retrieval에
# 쓰는 bm25.tokenize는 그대로 둔다 — 검색 정밀도까지 흔들고 싶지 않다.
"""

from __future__ import annotations

import re
from collections import Counter

from bm25 import tokenize

_WHITESPACE_RE = re.compile(r"\s+")

_PARTICLES = (
    "으로부터", "에서부터", "에게서", "한테서", "이라도", "이라는", "이나마",
    "라도", "라는", "이라고", "라고", "께서", "에서", "에게", "한테", "으로",
    "부터", "까지", "처럼", "보다", "마다", "이나", "은", "는", "이", "가",
    "을", "를", "의", "에", "로", "와", "과", "도", "만", "나",
)


def normalize_answer(text: str) -> str:
    text = text.casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _strip_particle(token: str) -> str:
    """토큰 끝의 조사 하나만 제거한다(재귀 없음) — 짧은 순수 조사 토큰은 그대로 둔다."""

    for particle in _PARTICLES:
        if len(token) > len(particle) and token.endswith(particle):
            return token[: -len(particle)]
    return token


def exact_match(prediction: str, gold: str) -> bool:
    return normalize_answer(prediction) == normalize_answer(gold)


def f1_score(prediction: str, gold: str) -> float:
    pred_tokens = [_strip_particle(t) for t in tokenize(normalize_answer(prediction))]
    gold_tokens = [_strip_particle(t) for t in tokenize(normalize_answer(gold))]
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

    assert _strip_particle("부정거래행위를") == "부정거래행위"
    assert _strip_particle("제443조에서") == "제443조"
    assert _strip_particle("가") == "가"  # 순수 조사 토큰은 그대로 둔다
    particle_only_diff = f1_score("부정거래행위를 금지한다", "부정거래행위 금지한다")
    assert particle_only_diff == 1.0, particle_only_diff

    print("eval_metrics self-check OK:", partial, particle_only_diff)
