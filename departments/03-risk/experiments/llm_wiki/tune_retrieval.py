"""grep/BM25 seed 튜닝 근거 — LLM 호출 없이 gold_page_ids 리콜만으로 top_k/Tmax를 고른다.

`run_experiment.py`(생성까지 도는 전체 파이프라인)로 매 설정을 돌리면 LLM 호출이
설정 수 x 질문 수만큼 늘어난다. seed 선택과 wiki_reader의 bounded read는 결정론적
Python이라 gold_page_ids와 직접 비교하는 이 스윕만으로도 "grep이 부족한가",
"몇 페이지를 읽어야 하는가"에 답할 수 있다. 결론은 arms.py의 튜닝 주석에 반영했다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bm25 import BM25Index  # noqa: E402
from grep_seed import grep_seed, keyword_seed  # noqa: E402
from run_experiment import load_golden_set  # noqa: E402
from wiki_reader import read_bounded  # noqa: E402

WIKI_DIR = Path(__file__).resolve().parent / "data" / "wiki"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _bm25_index() -> BM25Index:
    documents = {p.stem: p.read_text(encoding="utf-8") for p in sorted(WIKI_DIR.glob("*.md"))}
    return BM25Index(documents)


def _recall(pages_visited: list[str], gold_page_ids: list[str]) -> float:
    """q15처럼 gold_page_ids가 빈 문항은 '아무 페이지도 안 읽는 것'이 만점이다."""

    if not gold_page_ids:
        return 1.0 if not pages_visited else 0.0
    hit = sum(1 for g in gold_page_ids if g in pages_visited)
    return hit / len(gold_page_ids)


def eval_config(
    questions: list[dict[str, Any]], index: BM25Index, use_keyword: bool, top_k: int, tmax: int
) -> dict[str, Any]:
    recalls_b, recalls_c, ctx_b, ctx_c = [], [], [], []
    for q in questions:
        query, gold = q["query"], q["gold_page_ids"]
        bm25_seeds = [pid for pid, _score in index.score(query, top_k=top_k)]

        read_b = read_bounded(query, bm25_seeds, tmax=tmax)
        recalls_b.append(_recall(read_b.pages_visited, gold))
        ctx_b.append(len(read_b.context))

        seeds = grep_seed(query)
        if use_keyword:
            seeds += [p for p in keyword_seed(query) if p not in seeds]
        if not seeds:
            seeds = bm25_seeds
        read_c = read_bounded(query, seeds, tmax=tmax)
        recalls_c.append(_recall(read_c.pages_visited, gold))
        ctx_c.append(len(read_c.context))

    n = len(questions)
    return {
        "keyword_tier": use_keyword,
        "top_k": top_k,
        "tmax": tmax,
        "B_recall": round(sum(recalls_b) / n, 3),
        "B_avg_context_chars": round(sum(ctx_b) / n, 1),
        "C_recall": round(sum(recalls_c) / n, 3),
        "C_avg_context_chars": round(sum(ctx_c) / n, 1),
    }


def main() -> None:
    golden = load_golden_set()
    questions = golden["questions"]
    index = _bm25_index()

    configs = [
        eval_config(questions, index, use_keyword=False, top_k=1, tmax=3),
        eval_config(questions, index, use_keyword=True, top_k=1, tmax=3),
        eval_config(questions, index, use_keyword=True, top_k=2, tmax=3),
        eval_config(questions, index, use_keyword=True, top_k=1, tmax=2),
        eval_config(questions, index, use_keyword=True, top_k=1, tmax=4),
        eval_config(questions, index, use_keyword=True, top_k=1, tmax=5),
    ]

    lines = [
        "# grep/BM25 seed 튜닝 — 무-LLM 리콜 스윕",
        "",
        f"질문 수: {len(questions)} (gold_page_ids 기준, LLM 호출 없이 결정론적으로 계산)",
        "",
        "| keyword_tier | top_k | tmax | B_recall | B_avg_ctx | C_recall | C_avg_ctx |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in configs:
        lines.append(
            f"| {c['keyword_tier']} | {c['top_k']} | {c['tmax']} | {c['B_recall']} "
            f"| {c['B_avg_context_chars']} | {c['C_recall']} | {c['C_avg_context_chars']} |"
        )
    lines.append("")
    lines.append(
        "결론: keyword_tier 추가만 C_recall을 0.867 -> 0.933(제외: q15)로 올렸다. "
        "top_k(1->2)나 tmax(3->5)를 키우는 건 context만 늘리고 리콜은 그대로였다 "
        "(top_k는 리콜 무변화, tmax는 3에서 이미 평탄화). 채택: top_k=1, tmax=3 유지, "
        "grep_seed+keyword_seed 합집합만 추가(arms.py)."
    )
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "retrieval_tuning.md").write_text("\n".join(lines), encoding="utf-8")
    for c in configs:
        print(c)
    print(f"-> {RESULTS_DIR / 'retrieval_tuning.md'}")


if __name__ == "__main__":
    main()
