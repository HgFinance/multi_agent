"""순수 Python BM25Okapi — grep seed 매칭이 실패했을 때 쓰는 2차 폴백 스코어링.

신규 의존성(rank_bm25 등) 대신 hand-roll한다 — 코퍼스가 실험 규모(수십 페이지)라
표준 BM25Okapi 공식 하나면 충분하고, 한국어 형태소 분석기(mecab/konlpy)도 추가하지
않는다. 토크나이저는 `\\w+`(Hangul 포함 unicode word char) 기반 단순 분절이다.

# ponytail: 형태소 분석 없이 어절 단위 토큰화라 조사가 붙은 동일 단어가 다른 토큰으로
# 갈릴 수 있다(예: "부정거래행위" vs "부정거래행위를"). 코퍼스가 커지고 이 오차가
# 실제로 재현율을 깎으면 mecab/konlpy 추가를 검토한다.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.casefold())


@dataclass
class BM25Document:
    doc_id: str
    tokens: list[str]


class BM25Index:
    """희귀 키워드(높은 IDF)를 우대하는 표준 BM25Okapi 스코어러."""

    def __init__(self, documents: dict[str, str]):
        self.docs: list[BM25Document] = [
            BM25Document(doc_id=doc_id, tokens=tokenize(text))
            for doc_id, text in documents.items()
        ]
        self.doc_lengths = [len(d.tokens) for d in self.docs]
        self.avg_doc_length = (
            sum(self.doc_lengths) / len(self.doc_lengths) if self.docs else 0.0
        )
        self.term_freqs: list[Counter[str]] = [Counter(d.tokens) for d in self.docs]
        self.doc_freq: Counter[str] = Counter()
        for tf in self.term_freqs:
            self.doc_freq.update(tf.keys())
        self.n_docs = len(self.docs)

    def _idf(self, term: str) -> float:
        df = self.doc_freq.get(term, 0)
        # BM25+ 스타일 smoothing: df==n_docs여도 음수가 되지 않는다.
        return math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))

    def score(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        query_terms = tokenize(query)
        if not query_terms or not self.docs:
            return []
        scores = []
        for i, doc in enumerate(self.docs):
            tf = self.term_freqs[i]
            doc_len = self.doc_lengths[i]
            score = 0.0
            for term in query_terms:
                if term not in tf:
                    continue
                freq = tf[term]
                idf = self._idf(term)
                denom = freq + K1 * (1 - B + B * doc_len / (self.avg_doc_length or 1))
                score += idf * (freq * (K1 + 1)) / denom
            if score > 0:
                scores.append((doc.doc_id, score))
        scores.sort(key=lambda pair: pair[1], reverse=True)
        return scores[:top_k]


if __name__ == "__main__":
    corpus = {
        "a": "미공개중요정보 이용행위 금지 조항입니다",
        "b": "시세조종행위 등의 금지에 관한 조항입니다",
        "c": "부정거래행위 등의 금지, 부정한 수단과 계획",
    }
    index = BM25Index(corpus)
    top = index.score("부정거래 부정한 수단")
    assert top, "BM25가 빈 결과를 냄"
    assert top[0][0] == "c", top  # 희귀 키워드("부정한", "부정거래")가 c에 몰려있어야 함

    empty = BM25Index({}).score("아무거나")
    assert empty == []

    print("bm25 self-check OK:", top)
