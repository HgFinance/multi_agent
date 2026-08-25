#!/usr/bin/env python3
"""Summarize repeated AWQ Hybrid baseline vs BOK800 Wiki fallback runs."""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


CATEGORY_LABELS = {
    "financial_arithmetic": "Financial Arithmetic",
    "risk_reasoning": "Risk Reasoning",
    "portfolio_trading_reasoning": "Portfolio / Trading",
    "accounting_reasoning": "Accounting",
    "quant_reasoning": "Quant",
    "evidence_reasoning": "Evidence",
    "structured_output": "Structured Output",
    "uncertainty_fail_closed": "Uncertainty / Fail-Closed",
}
HIGHER_IS_BETTER = [
    "Internal Quality",
    *CATEGORY_LABELS.values(),
    "FinQA",
    "TAT-QA",
    "FinanceBench diagnostic",
    "Auto Mean",
]


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_run(directory: Path) -> dict[str, Any]:
    internal = _load(directory / "internal50_score.json")
    external = _load(directory / "external50_score.json")
    internal_raw = _load(directory / "internal50_raw.json")
    external_raw = _load(directory / "external50_raw.json")
    metrics: dict[str, float] = {
        "Internal Quality": internal["accuracy"],
        "Critical Failures": float(internal["critical_failed_n"]),
        "Request Errors": float(
            sum(bool(row.get("error")) for row in internal_raw["results"])
            + sum(bool(row.get("error")) for row in external_raw["results"])
        ),
        "FinQA": external["sources"]["FinQA"]["pass_rate"],
        "TAT-QA": external["sources"]["TAT-QA"]["pass_rate"],
        "FinanceBench diagnostic": external["sources"]["FinanceBench"]["diagnostic_mean"],
        "Auto Mean": external["auto_mean_score"],
    }
    for key, label in CATEGORY_LABELS.items():
        metrics[label] = internal["categories"][key]["accuracy"]

    wiki_rows = [*internal_raw["results"], *external_raw["results"]]
    wiki_attempted = [row for row in wiki_rows if row.get("wiki_planner") is not None]
    wiki_candidates = [row for row in wiki_rows if row.get("wiki_candidate_hit")]
    wiki_hits = [row for row in wiki_rows if row.get("wiki_hit")]
    exact_hits = [
        row for row in wiki_hits
        if isinstance(row.get("wiki_planner"), dict) and row["wiki_planner"].get("exact_term")
    ]
    related_hits = [
        row for row in wiki_hits
        if any(page.get("relation", "").startswith("related_from:") for page in row.get("wiki_pages", []))
    ]
    candidate_related_hits = [
        row for row in wiki_candidates
        if any(
            page.get("relation", "").startswith("related_from:")
            for page in row.get("wiki_candidates", [])
        )
    ]
    planner_latency = [
        float(row["wiki_planner"]["latency_s"])
        for row in wiki_attempted
        if row["wiki_planner"].get("latency_s") is not None
    ]
    grade_latency = [
        float(row["wiki_grade"]["latency_s"])
        for row in wiki_candidates
        if isinstance(row.get("wiki_grade"), dict) and row["wiki_grade"].get("latency_s") is not None
    ]
    retrieval_latency = [float(row.get("wiki_retrieval_latency_ms", 0.0)) for row in wiki_attempted]
    retrieval = {
        "eligible_cases": len(wiki_attempted),
        "wiki_hits": len(wiki_hits),
        "hit_rate": len(wiki_hits) / len(wiki_attempted) if wiki_attempted else 0.0,
        "candidate_hits": len(wiki_candidates),
        "candidate_hit_rate": len(wiki_candidates) / len(wiki_attempted) if wiki_attempted else 0.0,
        "exact_term_hits": len(exact_hits),
        "related_traversal_hits": len(related_hits),
        "candidate_related_traversal_hits": len(candidate_related_hits),
        "avg_context_chars": statistics.mean(
            [float(row.get("wiki_context_chars", 0)) for row in wiki_hits]
        ) if wiki_hits else 0.0,
        "avg_retrieval_latency_ms": statistics.mean(retrieval_latency) if retrieval_latency else 0.0,
        "avg_planner_latency_s": statistics.mean(planner_latency) if planner_latency else 0.0,
        "avg_grade_latency_s_per_candidate": statistics.mean(grade_latency) if grade_latency else 0.0,
        "avg_total_retrieval_overhead_s_per_eligible_case": (
            (
                sum(planner_latency)
                + sum(grade_latency)
                + sum(retrieval_latency) / 1000.0
            ) / len(wiki_attempted)
            if wiki_attempted else 0.0
        ),
        "candidate_pages": [
            {
                "id": row["id"],
                "question": row.get("question"),
                "search_query": row.get("wiki_planner", {}).get("search_query"),
                "pages": row.get("wiki_candidates", []),
                "grade": row.get("wiki_grade"),
            }
            for row in wiki_candidates
        ],
        "pages": [
            {
                "id": row["id"],
                "question": row.get("question"),
                "search_query": row.get("wiki_planner", {}).get("search_query"),
                "pages": row.get("wiki_pages", []),
            }
            for row in wiki_hits
        ],
    }
    return {"directory": str(directory), "metrics": metrics, "retrieval": retrieval}


def mean_metrics(runs: list[dict[str, Any]]) -> dict[str, float]:
    return {
        metric: statistics.mean(run["metrics"][metric] for run in runs)
        for metric in runs[0]["metrics"]
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    baseline_dirs = sorted(args.root.glob("baseline_run*"))
    wiki_dirs = sorted(args.root.glob("wiki_run*"))
    baseline = [load_run(path) for path in baseline_dirs if (path / "external50_score.json").exists()]
    wiki = [load_run(path) for path in wiki_dirs if (path / "external50_score.json").exists()]
    if not baseline or not wiki:
        raise SystemExit("at least one scored baseline and Wiki run are required")
    paired_n = min(len(baseline), len(wiki))
    baseline = baseline[:paired_n]
    wiki = wiki[:paired_n]
    baseline_mean = mean_metrics(baseline)
    wiki_mean = mean_metrics(wiki)
    deltas = {metric: wiki_mean[metric] - baseline_mean[metric] for metric in baseline_mean}
    all_quality_lower = all(deltas[metric] < 0 for metric in HIGHER_IS_BETTER)
    result = {
        "schema_version": "awq-hybrid-bok800-wiki-paired.v1",
        "paired_runs": paired_n,
        "baseline_runs": baseline,
        "wiki_runs": wiki,
        "baseline_mean": baseline_mean,
        "wiki_mean": wiki_mean,
        "delta": deltas,
        "early_stop_after_two": paired_n == 2 and all_quality_lower,
        "all_higher_is_better_metrics_lower": all_quality_lower,
        "early_stop_rule": "after two paired runs, stop only if every higher-is-better quality metric is below baseline",
        "retrieval_mean": {
            key: statistics.mean(run["retrieval"][key] for run in wiki)
            for key in (
                "eligible_cases", "candidate_hits", "candidate_hit_rate", "wiki_hits", "hit_rate",
                "exact_term_hits", "related_traversal_hits", "avg_context_chars",
                "candidate_related_traversal_hits",
                "avg_retrieval_latency_ms", "avg_planner_latency_s",
                "avg_grade_latency_s_per_candidate", "avg_total_retrieval_overhead_s_per_eligible_case",
            )
        },
    }
    output = args.output or args.root / "summary.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "paired_runs": paired_n,
        "baseline_mean": baseline_mean,
        "wiki_mean": wiki_mean,
        "delta": deltas,
        "early_stop_after_two": result["early_stop_after_two"],
    }, ensure_ascii=False, indent=2))
    return 10 if result["early_stop_after_two"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
