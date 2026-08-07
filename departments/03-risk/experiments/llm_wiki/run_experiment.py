"""CLI 실험 하네스 — golden_set.json 고정 입력으로 Arm A/B/C를 돌려 F1/EM을 비교한다.

부서장 지시+mandate는 모든 질문·모든 Arm에 동일하게 고정한다(golden_set.json의
`department_head_instruction`+`mandate`). 결과는 results/comparison_report.md(사람이
읽는 표)와 results/comparison_report.json(원본 데이터)에 남긴다.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arms import (  # noqa: E402
    llm_wiki_bm25_answer,
    llm_wiki_grep_bm25_answer,
    plain_rag_answer,
)
from eval_metrics import exact_match, f1_score  # noqa: E402

GOLDEN_SET_PATH = Path(__file__).resolve().parent / "golden_set.json"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

ARMS: dict[str, Any] = {
    "A_plain_rag": plain_rag_answer,
    "B_llm_wiki_bm25": llm_wiki_bm25_answer,
    "C_llm_wiki_grep_bm25": llm_wiki_grep_bm25_answer,
}


def load_golden_set(path: Path = GOLDEN_SET_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_arm(arm_name: str, arm_fn: Any, golden: dict[str, Any]) -> dict[str, Any]:
    mandate = f"{golden['department_head_instruction']}\n\n{golden['mandate']}"
    as_of = golden["as_of"]
    per_question: list[dict[str, Any]] = []
    for q in golden["questions"]:
        started = time.monotonic()
        result = arm_fn(q["query"], as_of, mandate=mandate)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        answer = result["answer"]
        prediction = str(answer.get("rationale", ""))
        per_question.append(
            {
                "id": q["id"],
                "f1": round(f1_score(prediction, q["gold_answer"]), 4),
                "em": exact_match(prediction, q["gold_answer"]),
                "verdict": answer.get("verdict"),
                "gold_verdict": q["gold_verdict"],
                "verdict_match": answer.get("verdict") == q["gold_verdict"],
                "context_chars": result["context_chars"],
                "pages_visited": result["pages_visited"],
                "elapsed_ms": elapsed_ms,
            }
        )
    n = len(per_question)
    return {
        "arm": arm_name,
        "avg_f1": round(sum(p["f1"] for p in per_question) / n, 4),
        "em_rate": round(sum(p["em"] for p in per_question) / n, 4),
        "verdict_accuracy": round(sum(p["verdict_match"] for p in per_question) / n, 4),
        "avg_context_chars": round(sum(p["context_chars"] for p in per_question) / n, 1),
        "avg_elapsed_ms": round(sum(p["elapsed_ms"] for p in per_question) / n, 1),
        "per_question": per_question,
    }


def render_report(golden: dict[str, Any], arm_results: list[dict[str, Any]]) -> str:
    lines = [
        "# LLM-Wiki 부분 도입 실험 — Arm A/B/C 비교",
        "",
        f"질문 수: {len(golden['questions'])} / 부서장 지시+mandate 고정 / as_of={golden['as_of']}",
        "",
        "| Arm | 평균 F1 | EM 비율 | Verdict 정확도 | 평균 context 문자수 | 평균 소요(ms) |",
        "|---|---|---|---|---|---|",
    ]
    for r in arm_results:
        lines.append(
            f"| {r['arm']} | {r['avg_f1']} | {r['em_rate']} | {r['verdict_accuracy']} "
            f"| {r['avg_context_chars']} | {r['avg_elapsed_ms']} |"
        )
    lines.append("")
    lines.append("## 문항별 상세")
    lines.append("")
    for r in arm_results:
        lines.append(f"### {r['arm']}")
        lines.append("")
        lines.append("| id | F1 | EM | verdict | gold_verdict | context_chars |")
        lines.append("|---|---|---|---|---|---|")
        for p in r["per_question"]:
            lines.append(
                f"| {p['id']} | {p['f1']} | {p['em']} | {p['verdict']} "
                f"| {p['gold_verdict']} | {p['context_chars']} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[4] / ".env")
    golden = load_golden_set()
    arm_results = [run_arm(name, fn, golden) for name, fn in ARMS.items()]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "comparison_report.json").write_text(
        json.dumps(arm_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = render_report(golden, arm_results)
    (RESULTS_DIR / "comparison_report.md").write_text(report, encoding="utf-8")

    for r in arm_results:
        print(
            f"{r['arm']}: F1={r['avg_f1']} EM={r['em_rate']} "
            f"verdict_acc={r['verdict_accuracy']} avg_context_chars={r['avg_context_chars']}"
        )
    print(f"-> {RESULTS_DIR / 'comparison_report.md'}")


if __name__ == "__main__":
    main()
