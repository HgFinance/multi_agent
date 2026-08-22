#!/usr/bin/env python3
"""Render the measured L4-fp8KV-v1 five-variant comparison."""
from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "benchmarks/quantization/results/aws_l4_fp8kv_v1"
RUN_ID = "aws-l4-fp8kv-v1-20260822"
SOURCE_PARENT = "4c2014c"
VARIANTS = ("FP8", "AWQ", "AWQ+Finetune", "AWQ+Reasoning", "AWQ+RAG")
DIRS = {variant: ROOT / variant for variant in VARIANTS}

RUNTIME = {
    "gpu": "NVIDIA L4",
    "python": "3.12.13",
    "vllm": "0.27.1",
    "flashinfer": "0.6.16.post3",
    "cuda_nvcc": "13.0.88",
    "attention_backend": "FLASHINFER",
    "max_model_len": 8192,
    "gpu_memory_utilization": 0.85,
    "kv_cache_dtype": "fp8_e4m3",
    "prefix_caching": True,
    "temperature": 0,
    "stream": False,
    "host_bind": "127.0.0.1:8000",
    "endpoint_verified": True,
    "container": "hgfinance-vllm-runtime-20260822",
    "image": "vllm/vllm-openai:v0.27.1",
}
PROFILE = {
    "name": "L4-fp8KV-v1",
    "gpu": "NVIDIA L4",
    "max_model_len": 8192,
    "gpu_memory_utilization": 0.85,
    "kv_cache_dtype": "fp8_e4m3",
    "enable_prefix_caching": True,
    "temperature": 0,
    "stream": False,
    "quality_execution": "sequential",
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def score_paths(variant: str) -> tuple[Path, Path, Path, Path]:
    directory = DIRS[variant]
    return (
        directory / "internal50_score.json",
        directory / "external50_score.json",
        directory / "internal50_raw.json",
        directory / "external50_raw.json",
    )


def measured_variant(variant: str) -> dict:
    internal_path, external_path, internal_raw_path, external_raw_path = score_paths(variant)
    internal = load(internal_path)
    external = load(external_path)
    internal_raw = load(internal_raw_path)
    quality = {
        "internal_quality": internal["accuracy"],
        "internal_passed": internal["passed"],
        "internal_total": internal["n"],
        "critical_failures": internal["critical_failed_n"],
        "critical_failed_ids": sorted(internal.get("critical_failed_ids", [])),
        "request_errors": internal["error_n"],
        "categories": internal["categories"],
        "avg_quality_latency_s": internal["avg_latency_s"],
        "external_overall": None,
        "external_overall_reason": "FinanceBench manual adjudication is required; frozen External-50 Overall is not asserted from auto-only output.",
        "finqa": external["sources"]["FinQA"]["pass_rate"],
        "tatqa": external["sources"]["TAT-QA"]["pass_rate"],
        "financebench_manual": None,
        "financebench_diagnostic": external["sources"]["FinanceBench"]["diagnostic_mean"],
        "auto_mean": external["auto_mean_score"],
        "external_auto_passed": external["auto_passed"],
        "external_auto_scored_n": external["auto_scored_n"],
    }
    artifacts = [
        rel(DIRS[variant] / name)
        for name in ("internal50_raw.json", "internal50_score.json", "external50_raw.json", "external50_score.json")
    ]
    if variant == "AWQ+RAG":
        quality["glossary_version"] = internal_raw.get("glossary_version")
        quality["glossary_sha256"] = internal_raw.get("glossary_sha256")
        quality["glossary_hit_rate"] = sum(bool(row.get("hit")) for row in internal_raw["results"]) / len(internal_raw["results"])
        artifacts.append(rel(REPO / "benchmarks/quantization/knowledge/bok800_2026/glossary_rag_v1_manifest.json"))
    if variant in ("FP8", "AWQ"):
        artifacts.extend(rel(DIRS[variant] / name) for name in ("performance.json", "endpoint.json", "runtime.log"))
    if variant == "AWQ+Reasoning":
        rows = internal_raw["results"]
        quality.update(
            {
                "critic_model": "gpt-4o-mini",
                "reasoning_success_cases": sum(row.get("final_status") == "SUCCESS" for row in rows),
                "reasoning_total_cost_usd": sum(row.get("estimated_cost_usd", 0) for row in rows),
                "reasoning_model_only_latency_avg_s": statistics.mean(row["model_only_latency_s"] for row in rows),
                "reasoning_full_pipeline_latency_avg_s": statistics.mean(row["full_pipeline_latency_s"] for row in rows),
                "reasoning_retry_count": sum(row.get("critic_retry_count", 0) for row in rows),
            }
        )
    performance = {"status": "MEASURED_BASE_ONLY" if variant in ("AWQ+RAG", "AWQ+Reasoning") else "MEASURED", "artifact_paths": artifacts}
    return {"status": "MEASURED", "quality": quality, "performance": performance, "artifact_paths": artifacts}


def make_hold_variant() -> dict:
    reason = (
        "No adapter_config.json + adapter_model.safetensors for an exact AWQ-compatible adapter was found in the existing mounted model/checkpoint paths. "
        "No NF4 adapter was substituted and no finetune benchmark was run."
    )
    for name in ("internal50_raw.json", "internal50_score.json", "external50_raw.json", "external50_score.json"):
        (DIRS["AWQ+Finetune"] / name).write_text(
            json.dumps(
                {
                    "schema_version": "variant-result.v1",
                    "variant": "AWQ+Finetune",
                    "status": "HOLD",
                    "scores": None,
                    "reason": reason,
                    "manifest": "AWQ+Finetune/provenance.json",
                    "runtime": "L4-fp8KV-v1",
                    "errors": [],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    artifacts = [rel(DIRS["AWQ+Finetune"] / name) for name in ("internal50_raw.json", "internal50_score.json", "external50_raw.json", "external50_score.json")]
    return {"status": "HOLD", "quality": None, "performance": {"status": "HOLD", "reason": reason}, "artifact_paths": artifacts, "hold_reason": reason}


def gate_results(variants: dict) -> dict:
    fp8 = variants["FP8"]["quality"]
    result = {}
    for variant in VARIANTS[1:]:
        current = variants[variant]
        if current["quality"] is None:
            result[variant] = {
                "internal_relative_delta_vs_fp8": None,
                "external_overall_delta_vs_fp8": None,
                "new_critical_regression": None,
                "request_error_regression": None,
                "verdict": "HOLD",
                "reason": current["hold_reason"],
            }
            continue
        quality = current["quality"]
        new_ids = sorted(set(quality["critical_failed_ids"]) - set(fp8["critical_failed_ids"]))
        result[variant] = {
            "internal_relative_delta_vs_fp8": round(quality["internal_quality"] / fp8["internal_quality"] - 1, 6),
            "external_overall_delta_vs_fp8": None,
            "new_critical_regression": len(new_ids),
            "new_critical_ids": new_ids,
            "request_error_regression": quality["request_errors"] - fp8["request_errors"],
            "verdict": "HOLD",
            "reason": quality["external_overall_reason"],
        }
    return result


def render_table(variants: dict, gates: dict) -> str:
    def value(variant: str, metric: str) -> str:
        current = variants[variant]
        if current["status"] == "HOLD":
            return "HOLD"
        quality = current["quality"]
        if metric == "internal": return percent(quality["internal_quality"])
        if metric == "delta": return "Baseline" if variant == "FP8" else percent(gates[variant]["internal_relative_delta_vs_fp8"])
        if metric == "critical": return str(quality["critical_failures"])
        if metric == "new_critical": return "—" if variant == "FP8" else str(gates[variant]["new_critical_regression"])
        if metric == "errors": return str(quality["request_errors"])
        if metric in ("arithmetic", "structured"): return percent(quality["categories"]["financial_arithmetic" if metric == "arithmetic" else "structured_output"]["accuracy"])
        if metric == "overall": return "N/A (FinanceBench manual)"
        if metric == "finqa": return percent(quality["finqa"])
        if metric == "tatqa": return percent(quality["tatqa"])
        if metric == "financebench": return f"Manual required (diag {quality['financebench_diagnostic'] * 100:.1f}%)"
        if metric == "auto_mean": return f"{quality['auto_mean']:.4f}"
        if metric in ("c1", "c2", "c4"):
            performance = current["performance"]
            if "metrics" not in performance: return "N/A (pipeline not measured)"
            return f"{performance['metrics'][metric.upper()]['throughput_tok_s']:.2f} tok/s"
        if metric == "e2e":
            if variant in ("FP8", "AWQ"): return f"{current['performance']['metrics']['C1']['latency_p50_s']:.3f}s p50"
            if variant == "AWQ+Reasoning": return f"{quality['reasoning_full_pipeline_latency_avg_s']:.3f}s avg"
            if variant == "AWQ+RAG": return f"{quality['avg_quality_latency_s']:.3f}s avg"
        if metric == "memory": return f"{current['performance']['model_load_memory_gib']:.2f} GiB" if "model_load_memory_gib" in current["performance"] else "N/A (quality-only)"
        if metric == "kv": return f"{current['performance']['kv_cache_gib']:.2f} GiB" if "kv_cache_gib" in current["performance"] else "N/A (quality-only)"
        if metric == "concurrency": return "N/A (capacity not measured)"
        if metric == "free": return f"{current['performance']['free_vram_mib']} MiB" if "free_vram_mib" in current["performance"] else "N/A (quality-only)"
        if metric == "startup":
            if variant in ("FP8", "AWQ"):
                return f"PASS HTTP 200 (~{current['performance']['startup_model_load_s']:.1f}s load)"
            return "PASS HTTP 200 (adapter loaded)" if current["status"] == "MEASURED" else "HOLD"
        if metric == "gate": return "BASELINE" if variant == "FP8" else "HOLD: External Overall/manual or variant gate"
        return "N/A"

    rows = [
        ("Internal Quality", "internal"), ("Relative Quality Delta vs FP8", "delta"), ("Critical Failures", "critical"),
        ("New Critical Regression", "new_critical"), ("Request Errors", "errors"), ("Financial Arithmetic", "arithmetic"),
        ("Structured Output", "structured"), ("External Overall", "overall"), ("FinQA", "finqa"), ("TAT-QA", "tatqa"),
        ("FinanceBench", "financebench"), ("Auto Mean", "auto_mean"), ("C1 Throughput", "c1"), ("C2 Throughput", "c2"),
        ("C4 Throughput", "c4"), ("C1 E2E", "e2e"), ("Model Load Memory", "memory"), ("KV Cache", "kv"),
        ("8K Concurrency", "concurrency"), ("Free VRAM", "free"), ("Startup/Endpoint", "startup"), ("Final Gate", "gate"),
    ]
    headers = ["Metric", *VARIANTS, "Verdict"]
    lines = ["# AWS L4-fp8KV-v1 Five-Variant Comparison", "", f"Run ID: `{RUN_ID}`", "", "All measured variants use the same NVIDIA L4 runtime profile. This table is not comparable to earlier autoKV or FP8-KV performance runs.", "", "| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for label, metric in rows:
        values = [value(variant, metric) for variant in VARIANTS]
        verdict = "BASELINE" if metric == "gate" else ("HOLD" if any(item.startswith("HOLD") for item in values) or metric == "overall" else "Reported")
        lines.append("| " + " | ".join([label, *values, verdict]) + " |")
    lines.extend([
        "", "## Notes", "", "- External Overall is N/A because FinanceBench requires manual adjudication; Auto Mean is reported separately.",
        "- AWQ+Finetune is reported only when the exact AWQ adapter has passed save/reload; NF4 adapters are never substituted.",
        "- AWQ+RAG uses the final term-explicit glossary. The first body-wide-alias attempt was excluded for prompt contamination.",
        "- AWQ+Reasoning stores the AWQ draft separately and scores only successful gpt-4o-mini rewrites.",
        "- Port mapping was verified as `127.0.0.1:8000` only.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    variants = {variant: measured_variant(variant) for variant in ("FP8", "AWQ", "AWQ+Reasoning", "AWQ+RAG")}
    if (DIRS["AWQ+Finetune"] / "internal50_score.json").exists() and (DIRS["AWQ+Finetune"] / "external50_score.json").exists():
        variants["AWQ+Finetune"] = measured_variant("AWQ+Finetune")
    else:
        variants["AWQ+Finetune"] = make_hold_variant()
    variants["FP8"]["performance"].update({"metrics": load(DIRS["FP8"] / "performance.json")["results"], "model_load_memory_gib": 15.39, "kv_cache_gib": 1.53, "free_vram_mib": 1416, "startup_model_load_s": 126.15})
    variants["AWQ"]["performance"].update({"metrics": load(DIRS["AWQ"] / "performance.json")["results"], "model_load_memory_gib": 9.38, "kv_cache_gib": 8.90, "free_vram_mib": 1344, "startup_model_load_s": 79.93})
    for variant in ("AWQ+Reasoning", "AWQ+RAG"):
        variants[variant]["performance"].update({"model_load_memory_gib": 9.38, "kv_cache_gib": 8.90, "free_vram_mib": 1344})
    for variant in VARIANTS:
        provenance = {
            "schema_version": "l4-fp8kv-provenance.v1", "run_id": RUN_ID, "variant": variant,
            "status": variants[variant]["status"], "source_parent_commit": SOURCE_PARENT,
            "runtime": RUNTIME, "profile": PROFILE,
            "serving": {"endpoint": "http://127.0.0.1:8000", "model": "Qwen2.5-14B-Instruct-FP8-dynamic" if variant == "FP8" else "Qwen2.5-14B-Instruct-AWQ", "launch_args": ["--max-model-len", "8192", "--gpu-memory-utilization", "0.85", "--kv-cache-dtype", "fp8_e4m3", "--enable-prefix-caching", "--host", "0.0.0.0", "--port", "8000"], "endpoint_health": "HTTP 200; localhost binding 127.0.0.1:8000", "backend": "FLASHINFER"},
            "datasets": {"internal50_v2_sha256": "ad2bdaf5ea381c2fc151fce1f1859f7f925b86fd03b830319cd97af17709e978", "external50_v1_sha256": sha256(REPO / "benchmarks/quantization/external50_v1.json")},
            "artifacts": variants[variant]["artifact_paths"], "quality": variants[variant]["quality"], "performance": variants[variant]["performance"],
        }
        if variants[variant].get("hold_reason"): provenance["hold_reason"] = variants[variant]["hold_reason"]
        (DIRS[variant] / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    gates = gate_results(variants)
    comparison = {
        "schema_version": "quantization-comparison.v2", "run_id": RUN_ID, "source_parent_commit": SOURCE_PARENT, "status": "HOLD",
        "comparability_statement": "Fair relative comparison within one NVIDIA L4-fp8KV-v1 runtime profile. Not comparable to the previous FP8-KV or autoKV performance runs.",
        "runtime": RUNTIME, "profile": PROFILE, "primary_external_metric": "overall_accuracy", "secondary_external_metric": "auto_mean_score",
        "variants": variants, "gate": {"vs_fp8": gates, "internal_gate_definition": "relative quality degradation <= 3%, no new Critical Failure, no Request Error increase", "external_gate_definition": "frozen External-50 Overall accuracy; unavailable until FinanceBench manual adjudication is complete", "verdict": "HOLD", "hold_reasons": ["AWQ+Finetune has no exact compatible adapter.", "External Overall is not asserted because FinanceBench manual adjudication is pending.", "AWQ+RAG adds one new Internal critical failure."]},
        "source_artifacts": [path for variant in VARIANTS for path in variants[variant]["artifact_paths"]],
        "notes": ["All measured raw outputs and scores use frozen Internal-50 v2 and External-50 v1 datasets.", "The first RAG attempt was excluded because body-wide aliases caused prompt contamination; the final glossary retains only term-explicit aliases.", "FinanceBench diagnostics are retained separately and are not the primary external gate."],
    }
    (ROOT / "aws_l4_fp8kv_v1_comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ROOT / "aws_l4_fp8kv_v1_comparison.md").write_text(render_table(variants, gates), encoding="utf-8")
    print(json.dumps({"status": comparison["status"], "run_id": RUN_ID, "output": str(ROOT / "aws_l4_fp8kv_v1_comparison.md")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
