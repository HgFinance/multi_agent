"""CLI 실험 하네스 — 고정 평가셋으로 RAG 경로를 비교한다.

부서장 지시+mandate는 모든 질문·모든 Arm에 동일하게 고정한다. 기본은
golden_set.json이며, `--dataset`으로 튜닝과 분리된 holdout을 같은 평가 경로로
실행할 수 있다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arms import (
    llm_wiki_bm25_answer,
    llm_wiki_grep_bm25_answer,
    plain_rag_answer,
)
from eval_metrics import exact_match, f1_score
from llm_judge import judge

GOLDEN_SET_PATH = Path(__file__).resolve().parent / "golden_set.json"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

ARMS: dict[str, Any] = {
    "A_plain_rag": plain_rag_answer,
    "B_llm_wiki_bm25": llm_wiki_bm25_answer,
    "C_llm_wiki_grep_bm25": llm_wiki_grep_bm25_answer,
}


def load_golden_set(path: Path = GOLDEN_SET_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_evaluation_set(dataset: dict[str, Any], path: Path) -> None:
    """Reject malformed or accidentally overlapping holdout data before model calls."""

    questions = dataset.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError(f"evaluation dataset has no questions: {path}")
    ids = [str(q.get("id", "")) for q in questions if isinstance(q, dict)]
    if len(ids) != len(questions) or not all(ids) or len(set(ids)) != len(ids):
        raise ValueError(f"evaluation dataset question ids must be unique: {path}")

    if dataset.get("split") != "holdout":
        return
    tuning_questions = load_golden_set()["questions"]
    tuning_queries = {" ".join(str(q["query"]).casefold().split()) for q in tuning_questions}
    overlap = [
        q["id"]
        for q in questions
        if " ".join(str(q["query"]).casefold().split()) in tuning_queries
    ]
    if overlap:
        raise ValueError(f"holdout contains tuning-set queries: {overlap}")


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
                "query": q["query"],
                "query_kind": q.get("query_kind", "conduct_assessment"),
                "prediction": prediction,
                "gold_answer": q["gold_answer"],
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
    verdict_questions = [
        p for p, q in zip(per_question, golden["questions"], strict=True)
        if q.get("query_kind", "conduct_assessment") == "conduct_assessment"
    ]
    verdict_n = len(verdict_questions)
    return {
        "arm": arm_name,
        "avg_f1": round(sum(p["f1"] for p in per_question) / n, 4),
        "em_rate": round(sum(p["em"] for p in per_question) / n, 4),
        "verdict_accuracy": round(sum(p["verdict_match"] for p in per_question) / n, 4),
        "conduct_verdict_accuracy": round(
            sum(p["verdict_match"] for p in verdict_questions) / verdict_n, 4
        ) if verdict_n else None,
        "conduct_verdict_questions": verdict_n,
        "avg_context_chars": round(sum(p["context_chars"] for p in per_question) / n, 1),
        "avg_elapsed_ms": round(sum(p["elapsed_ms"] for p in per_question) / n, 1),
        "per_question": per_question,
    }


def run_judge_pass(arm_result: dict[str, Any]) -> None:
    """2차 평가 — LLM-as-a-Judge semantic F1/Accuracy. arm_result를 in-place로 갱신한다."""

    per_question = arm_result["per_question"]
    for p in per_question:
        verdict = judge(p["query"], p["gold_answer"], p["prediction"])
        p["semantic_f1"] = round(verdict["semantic_f1"], 4)
        p["semantic_correct"] = bool(verdict["correct"])
    n = len(per_question)
    arm_result["avg_semantic_f1"] = round(sum(p["semantic_f1"] for p in per_question) / n, 4)
    arm_result["semantic_accuracy"] = round(
        sum(p["semantic_correct"] for p in per_question) / n, 4
    )


def render_report(
    golden: dict[str, Any],
    arm_results: list[dict[str, Any]],
    judged: bool = False,
    dataset_path: Path | None = None,
) -> str:
    dataset_label = golden.get("dataset_name") or (dataset_path.stem if dataset_path else "golden")
    lines = [
        "# LLM-Wiki 부분 도입 실험 — Arm A/B/C 비교",
        "",
        f"데이터셋: {dataset_label}",
        f"질문 수: {len(golden['questions'])} / 부서장 지시+mandate 고정 / as_of={golden['as_of']}",
        "",
    ]
    header = (
        "| Arm | 평균 F1 | EM 비율 | 원시 Verdict 일치율 | 행위판정 Verdict 정확도 | "
        "평균 context 문자수 | 평균 소요(ms) |"
    )
    sep = "|---|---|---|---|---|---|---|"
    if judged:
        header += " Semantic F1(LLM judge) | Semantic 정확도(LLM judge) |"
        sep += "---|---|"
    lines += [header, sep]
    for r in arm_results:
        row = (
            f"| {r['arm']} | {r['avg_f1']} | {r['em_rate']} | {r['verdict_accuracy']} "
            f"| {r['conduct_verdict_accuracy']} ({r['conduct_verdict_questions']}문항) "
            f"| {r['avg_context_chars']} | {r['avg_elapsed_ms']} |"
        )
        if judged:
            row += f" {r['avg_semantic_f1']} | {r['semantic_accuracy']} |"
        lines.append(row)
    lines.append("")
    lines.append("## 문항별 상세")
    lines.append("")
    for r in arm_results:
        lines.append(f"### {r['arm']}")
        lines.append("")
        detail_header = "| id | F1 | EM | verdict | gold_verdict | context_chars |"
        detail_sep = "|---|---|---|---|---|---|"
        if judged:
            detail_header += " semantic_f1 | semantic_correct |"
            detail_sep += "---|---|"
        lines.append(detail_header)
        lines.append(detail_sep)
        for p in r["per_question"]:
            row = (
                f"| {p['id']} | {p['f1']} | {p['em']} | {p['verdict']} "
                f"| {p['gold_verdict']} | {p['context_chars']} |"
            )
            if judged:
                row += f" {p['semantic_f1']} | {p['semantic_correct']} |"
            lines.append(row)
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description="Run the single Risk LLM-Wiki evaluation harness.")
    parser.add_argument("--judge", action="store_true", help="run the semantic LLM judge pass")
    parser.add_argument("--dataset", type=Path, default=GOLDEN_SET_PATH)
    parser.add_argument(
        "--report-stem",
        default="comparison_report",
        help="output filename stem under the results directory",
    )
    parser.add_argument(
        "--arms",
        nargs="+",
        choices=tuple(ARMS),
        default=list(ARMS),
        help="compare only the selected existing arms (default: all)",
    )
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[4] / ".env")
    judged = args.judge
    golden = load_golden_set(args.dataset)
    validate_evaluation_set(golden, args.dataset)
    selected_arms = args.arms
    arm_results = [run_arm(name, ARMS[name], golden) for name in selected_arms]

    if judged:
        for r in arm_results:
            run_judge_pass(r)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report_name = args.report_stem + ("_judged" if judged else "")
    (RESULTS_DIR / f"{report_name}.json").write_text(
        json.dumps(arm_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = render_report(golden, arm_results, judged=judged, dataset_path=args.dataset)
    (RESULTS_DIR / f"{report_name}.md").write_text(report, encoding="utf-8")

    for r in arm_results:
        line = (
            f"{r['arm']}: F1={r['avg_f1']} EM={r['em_rate']} "
            f"verdict_acc={r['verdict_accuracy']} "
            f"conduct_verdict_acc={r['conduct_verdict_accuracy']} "
            f"avg_context_chars={r['avg_context_chars']}"
        )
        if judged:
            line += f" semantic_f1={r['avg_semantic_f1']} semantic_acc={r['semantic_accuracy']}"
        print(line)
    print(f"-> {RESULTS_DIR / f'{report_name}.md'}")


if __name__ == "__main__":
    main()
