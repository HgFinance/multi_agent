#!/usr/bin/env python3
"""Prepare a contamination-checked SFT set for the AWQ finance experiment.

This script only prepares data and manifests.  It does not train an adapter and
it deliberately does not claim that an NF4 QLoRA adapter is compatible with an
AWQ serving model.

The default selection is 1,500 examples total: 1,350 train and 150 validation.
The selected sources are FinQA, finance-relevant Glaive function-calling
examples, and deterministic locally generated arithmetic/JSON examples.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import random
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from training.specialist.contamination import check_contamination, require_clean
from training.specialist.schema import validate_record


FINQA_URL = "https://huggingface.co/datasets/ibm-research/finqa"
FINQA_SOURCE_URL = "https://github.com/czyssrs/FinQA"
GLAIVE_URL = "https://huggingface.co/datasets/glaiveai/glaive-function-calling-v2"
GLAIVE_REVISION = "e7f4b6456019f5d8bcb991ef0dd67d8ff23221ac"
GLAIVE_LICENSE = "apache-2.0"
FINQA_LICENSE = "cc-by-4.0"
SYNTHETIC_VERSION = "hgfinance-arithmetic-structured-v1"

FINANCE_FUNCTION_TERMS = (
    "currency",
    "exchange",
    "stock",
    "share",
    "price",
    "discount",
    "loan",
    "mortgage",
    "tax",
    "invoice",
    "interest",
    "profit",
    "revenue",
    "payment",
    "average",
    "percentage",
    "sum",
    "total",
    "expense",
    "dividend",
    "portfolio",
    "financial",
    "market",
)
FINANCE_FUNCTION_EXCLUDES = (
    "temperature",
    "fuel",
    "tip",
    "car",
    "rental",
    "image",
    "body",
    "gpa",
    "age",
    "distance",
    "area",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with path.open("wb") as handle:
        for record in records:
            line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            handle.write(line)
            digest.update(line)
    return digest.hexdigest()


def table_to_text(table: Any) -> str:
    if not isinstance(table, list):
        return ""
    rows: list[str] = []
    for row in table:
        if isinstance(row, list):
            rows.append(" | ".join(str(cell) for cell in row))
    return "\n".join(rows)


def finqa_to_record(raw: dict[str, Any], split: str, ordinal: int) -> dict[str, Any] | None:
    qa = raw.get("qa")
    if not isinstance(qa, dict):
        return None
    steps = qa.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    last_step = steps[-1]
    if not isinstance(last_step, dict):
        return None
    answer = str(last_step.get("res", "")).strip()
    question = str(qa.get("question", "")).strip()
    if not answer or not question:
        return None

    evidence_parts: list[str] = []
    for key in ("pre_text", "post_text"):
        value = raw.get(key)
        if isinstance(value, list):
            evidence_parts.extend(str(item).strip() for item in value if str(item).strip())
    table = table_to_text(raw.get("table"))
    if table:
        evidence_parts.append("TABLE:\n" + table)
    evidence = "\n".join(evidence_parts)
    user = (
        "Use only the supplied financial evidence.\n\n"
        f"EVIDENCE:\n{evidence}\n\n"
        f"TASK: {question}\n\n"
        "OUTPUT CONTRACT: Return only the final numeric result, with no explanation."
    )
    raw_id = str(raw.get("id", ordinal))
    return {
        "id": f"finqa-{split}-{raw_id}",
        "messages": [
            {
                "role": "system",
                "content": "You are a careful financial arithmetic assistant. Follow the output contract exactly.",
            },
            {"role": "user", "content": user},
            {"role": "assistant", "content": answer},
        ],
        "category": "financial_arithmetic",
        "behavior_themes": ["financial_arithmetic", "deterministic", "evidence_grounded"],
        "source_dataset": "FinQA",
        "source_split": split,
        "source_id": raw_id,
        "source_row": ordinal,
        "source_raw_sha256": sha256_json(raw),
    }


def is_finance_function(name: str) -> bool:
    lowered = name.lower()
    return (
        any(term in lowered for term in FINANCE_FUNCTION_TERMS)
        and not any(term in lowered for term in FINANCE_FUNCTION_EXCLUDES)
    )


def glaive_to_record(raw: dict[str, Any], ordinal: int) -> dict[str, Any] | None:
    chat = raw.get("chat")
    if not isinstance(chat, str):
        return None
    marker = chat.find("<functioncall>")
    if marker < 0:
        return None
    prefix = chat[:marker]
    user_marker = prefix.rfind("USER:")
    if user_marker < 0:
        return None
    user = prefix[user_marker + len("USER:") :].strip()
    if not user:
        return None
    call_text = chat[marker + len("<functioncall>") :]
    call_text = call_text.split("<|endoftext|>", 1)[0].strip()
    try:
        call = ast.literal_eval(call_text)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(call, dict):
        return None
    name = str(call.get("name", "")).strip()
    if not name or not is_finance_function(name):
        return None
    arguments = call.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None
    system = str(raw.get("system", "")).strip()
    if system.startswith("SYSTEM:"):
        system = system[len("SYSTEM:") :].strip()
    if not system:
        system = "Use the declared function when it matches the user's request."
    system += " Return exactly one JSON object with keys name and arguments."
    target = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False, sort_keys=True)
    return {
        "id": f"glaive-finance-{ordinal}",
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": user + "\n\nOUTPUT CONTRACT: Return only the function-call JSON object.",
            },
            {"role": "assistant", "content": target},
        ],
        "category": "structured_output",
        "behavior_themes": ["structured_output", "deterministic", "financial_arithmetic"],
        "source_dataset": "Glaive Function Calling v2",
        "source_split": "train",
        "source_id": str(ordinal),
        "source_row": ordinal,
        "source_raw_sha256": sha256_json(raw),
        "function_name": name,
    }


def synthetic_record(ordinal: int, rng: random.Random) -> dict[str, Any]:
    if ordinal % 2 == 0:
        revenue = rng.randint(10_000, 900_000)
        costs = rng.randint(1_000, revenue - 1)
        margin = round((revenue - costs) / revenue * 100, 2)
        question = (
            f"A synthetic finance case reports revenue of {revenue} USD and operating costs of {costs} USD. "
            "Calculate the operating margin as a percentage, rounded to two decimals."
        )
        answer = f"{margin:.2f}"
        category = "financial_arithmetic"
        themes = ["financial_arithmetic", "deterministic", "synthetic_holdout_safe"]
    else:
        asset = f"SYN-{ordinal:04d}"
        units = rng.randint(10, 900)
        unit_price = rng.randint(20, 900)
        total = units * unit_price
        payload = {"asset": asset, "units": units, "unit_price": unit_price, "total_value": total}
        question = (
            f"Create a valuation record for asset {asset}. It has {units} units at {unit_price} USD each. "
            'Return exactly the JSON keys "asset", "units", "unit_price", and "total_value".'
        )
        answer = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        category = "structured_output"
        themes = ["structured_output", "financial_arithmetic", "synthetic_holdout_safe"]
    return {
        "id": f"synthetic-finance-{ordinal:04d}",
        "messages": [
            {
                "role": "system",
                "content": "Solve the supplied synthetic finance task deterministically and obey its output contract.",
            },
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ],
        "category": category,
        "behavior_themes": themes,
        "source_dataset": "HgFinance synthetic arithmetic/structured v1",
        "source_split": "generated",
        "source_id": f"{ordinal:04d}",
        "source_row": ordinal,
        "source_raw_sha256": sha256_json({"ordinal": ordinal, "question": question, "answer": answer}),
    }


def load_json_list(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"Expected a JSON list: {path}")
    return [item for item in value if isinstance(item, dict)]


def load_glaive(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        rows = value.get("train", value.get("data"))
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    raise ValueError(f"Expected a JSON list or data/train object: {path}")


def validate_and_deduplicate(records: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    counters = Counter()
    for record in records:
        try:
            validate_record(
                record,
                source_dataset=str(record.get("source_dataset", "unknown")),
                source_file=str(record.get("source_file", record.get("source_dataset", "generated"))),
                source_row=int(record.get("source_row", 0)),
            )
        except Exception:
            counters["schema_rejected"] += 1
            continue
        normalized = json.dumps(
            [message["content"].strip().lower() for message in record["messages"]],
            ensure_ascii=False,
            sort_keys=True,
        )
        key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if key in seen:
            counters["duplicate_rejected"] += 1
            continue
        seen.add(key)
        record["normalized_record_sha256"] = key
        accepted.append(record)
        counters["accepted"] += 1
    return accepted, dict(counters)


def choose(records: list[dict[str, Any]], count: int, seed: str) -> list[dict[str, Any]]:
    if len(records) < count:
        raise RuntimeError(f"Need {count} records but only found {len(records)} for {seed}")
    selected = list(records)
    random.Random(seed).shuffle(selected)
    return selected[:count]


def contamination_report(records: list[dict[str, Any]], benchmark_root: Path) -> dict[str, Any]:
    validated = [
        validate_record(
            record,
            source_dataset=str(record.get("source_dataset", "unknown")),
            source_file=str(record.get("source_file", record.get("source_dataset", "generated"))),
            source_row=int(record.get("source_row", 0)),
        )
        for record in records
    ]
    findings = check_contamination(validated, benchmark_root)
    require_clean(findings)
    return {"records_checked": len(records), **findings}


def distribution(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(record["category"] for record in records).items()))


def source_counts(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(record["source_dataset"] for record in records).items()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--finqa-train", type=Path, required=True)
    parser.add_argument("--finqa-validation", type=Path, required=True)
    parser.add_argument("--glaive", type=Path, required=True)
    parser.add_argument("--benchmark-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", default="hgfinance-awq-finance-v1")
    parser.add_argument("--finqa-train-count", type=int, default=700)
    parser.add_argument("--finqa-validation-count", type=int, default=100)
    parser.add_argument("--glaive-train-count", type=int, default=400)
    parser.add_argument("--glaive-validation-count", type=int, default=25)
    parser.add_argument("--synthetic-train-count", type=int, default=250)
    parser.add_argument("--synthetic-validation-count", type=int, default=25)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir
    prepared_dir = output_dir / "prepared"

    finqa_train_raw = load_json_list(args.finqa_train)
    finqa_validation_raw = load_json_list(args.finqa_validation)
    glaive_raw = load_glaive(args.glaive)

    finqa_train, finqa_train_stats = validate_and_deduplicate(
        record
        for ordinal, raw in enumerate(finqa_train_raw)
        if (record := finqa_to_record(raw, "train", ordinal)) is not None
    )
    finqa_validation, finqa_validation_stats = validate_and_deduplicate(
        record
        for ordinal, raw in enumerate(finqa_validation_raw)
        if (record := finqa_to_record(raw, "dev", ordinal)) is not None
    )
    glaive_candidates = [
        record
        for ordinal, raw in enumerate(glaive_raw)
        if (record := glaive_to_record(raw, ordinal)) is not None
    ]
    glaive_candidates, glaive_stats = validate_and_deduplicate(glaive_candidates)

    synthetic_all = [synthetic_record(i, random.Random(f"{args.seed}:synthetic:{i}")) for i in range(275)]
    synthetic_all, synthetic_stats = validate_and_deduplicate(synthetic_all)

    finqa_train_selected = choose(finqa_train, args.finqa_train_count, args.seed + ":finqa-train")
    finqa_validation_selected = choose(
        finqa_validation, args.finqa_validation_count, args.seed + ":finqa-validation"
    )
    glaive_selected = choose(
        glaive_candidates,
        args.glaive_train_count + args.glaive_validation_count,
        args.seed + ":glaive",
    )
    glaive_validation_selected = glaive_selected[: args.glaive_validation_count]
    glaive_train_selected = glaive_selected[args.glaive_validation_count :]
    synthetic_selected = choose(
        synthetic_all,
        args.synthetic_train_count + args.synthetic_validation_count,
        args.seed + ":synthetic",
    )
    synthetic_validation_selected = synthetic_selected[: args.synthetic_validation_count]
    synthetic_train_selected = synthetic_selected[args.synthetic_validation_count :]

    train = finqa_train_selected + glaive_train_selected + synthetic_train_selected
    validation = finqa_validation_selected + glaive_validation_selected + synthetic_validation_selected
    train, train_stats = validate_and_deduplicate(train)
    validation, validation_stats = validate_and_deduplicate(validation)
    train_hashes = {record["normalized_record_sha256"] for record in train}
    validation = [record for record in validation if record["normalized_record_sha256"] not in train_hashes]
    if len(train) != args.finqa_train_count + args.glaive_train_count + args.synthetic_train_count:
        raise RuntimeError("Unexpected train count after final deduplication")
    if len(validation) != args.finqa_validation_count + args.glaive_validation_count + args.synthetic_validation_count:
        raise RuntimeError("Unexpected validation count after train/validation deduplication")

    train_contamination = contamination_report(train, args.benchmark_root)
    validation_contamination = contamination_report(validation, args.benchmark_root)

    train_path = prepared_dir / "train.jsonl"
    validation_path = prepared_dir / "validation.jsonl"
    train_hash = write_jsonl(train_path, train)
    validation_hash = write_jsonl(validation_path, validation)

    source_manifest = {
        "schema_version": "awq-finance-sft-selection.v1",
        "status": "PREPARED",
        "purpose": "Target the observed financial_arithmetic and structured_output weaknesses without benchmark leakage.",
        "selection": {
            "seed": args.seed,
            "total_examples": len(train) + len(validation),
            "train_examples": len(train),
            "validation_examples": len(validation),
            "mix_ratio_total": {
                "FinQA": (args.finqa_train_count + args.finqa_validation_count) / (len(train) + len(validation)),
                "Glaive Function Calling v2": (args.glaive_train_count + args.glaive_validation_count)
                / (len(train) + len(validation)),
                "HgFinance synthetic arithmetic/structured v1": (
                    args.synthetic_train_count + args.synthetic_validation_count
                )
                / (len(train) + len(validation)),
            },
        },
        "sources": [
            {
                "name": "FinQA",
                "version": "1.0.0",
                "source_url": FINQA_URL,
                "upstream_url": FINQA_SOURCE_URL,
                "license": FINQA_LICENSE,
                "license_evidence_url": FINQA_URL,
                "local_files": {
                    "train": {"path": str(args.finqa_train), "sha256": sha256_file(args.finqa_train)},
                    "validation": {"path": str(args.finqa_validation), "sha256": sha256_file(args.finqa_validation)},
                },
                "candidate_count": len(finqa_train) + len(finqa_validation),
                "selected_train": args.finqa_train_count,
                "selected_validation": args.finqa_validation_count,
                "category": "financial_arithmetic",
            },
            {
                "name": "Glaive Function Calling v2",
                "version": GLAIVE_REVISION,
                "source_url": GLAIVE_URL,
                "license": GLAIVE_LICENSE,
                "license_evidence_url": GLAIVE_URL,
                "local_files": {"path": str(args.glaive), "sha256": sha256_file(args.glaive)},
                "candidate_count": len(glaive_candidates),
                "selected_train": args.glaive_train_count,
                "selected_validation": args.glaive_validation_count,
                "filter": "Finance-relevant function names only; one user request and first function call; no function response.",
                "category": "structured_output",
            },
            {
                "name": "HgFinance synthetic arithmetic/structured v1",
                "version": SYNTHETIC_VERSION,
                "source_url": "local generator in scripts/qlora/prepare_awq_finance_sft.py",
                "license": "internal-generated",
                "license_evidence": "Generated locally; no third-party content is copied into this source.",
                "generator_sha256": sha256_file(ROOT / "scripts/qlora/prepare_awq_finance_sft.py"),
                "candidate_count": len(synthetic_all),
                "selected_train": args.synthetic_train_count,
                "selected_validation": args.synthetic_validation_count,
                "category": "financial_arithmetic + structured_output",
            },
        ],
        "candidate_validation": {
            "finqa_train": finqa_train_stats,
            "finqa_validation": finqa_validation_stats,
            "glaive": glaive_stats,
            "synthetic": synthetic_stats,
            "final_train": train_stats,
            "final_validation": validation_stats,
        },
        "distribution": {
            "train": distribution(train),
            "validation": distribution(validation),
            "all": distribution(train + validation),
            "source_train": source_counts(train),
            "source_validation": source_counts(validation),
        },
        "contamination": {
            "benchmark_root": str(args.benchmark_root),
            "train": train_contamination,
            "validation": validation_contamination,
            "frozen_datasets_not_used_as_training_sources": [
                "benchmarks/quantization/internal50_v2_reasoning.json",
                "benchmarks/quantization/external50_v1.json",
            ],
        },
        "artifacts": {
            "train": {"path": str(train_path), "sha256": train_hash},
            "validation": {"path": str(validation_path), "sha256": validation_hash},
        },
    }
    write_json(output_dir / "selection_manifest.json", source_manifest)

    training_manifest = {
        "schema_version": "awq-finetune-training.v1",
        "status": "PREPARED_NOT_TRAINED",
        "runtime_profile": "L4-fp8KV-v1",
        "training_scope": ["financial_arithmetic", "structured_output"],
        "dataset": {
            "selection_manifest": str(output_dir / "selection_manifest.json"),
            "train": str(train_path),
            "validation": str(validation_path),
            "train_count": len(train),
            "validation_count": len(validation),
        },
        "tokenizer_audit": {
            "status": "PENDING_RUNTIME_AUDIT",
            "reason": "Run the exact served AWQ tokenizer with the Qwen chat template before training.",
            "max_seq_length": 4096,
            "serving_context_limit": 8192,
        },
        "runtime_training_preflight": {
            "status": "PENDING_TRAINING_ENV",
            "reason": "The vLLM serving image is not a complete PEFT training environment.",
            "required_packages": ["peft", "transformers", "accelerate", "bitsandbytes"],
        },
        "served_awq_base": {
            "model_name": "Qwen2.5-14B-Instruct-AWQ",
            "quantization": "awq",
            "revision": "must be resolved from the served model before adapter training",
        },
        "staging_training_recipe": {
            "purpose": "configuration scaffold only",
            "base_model": "Qwen/Qwen2.5-14B-Instruct",
            "base_revision": "must be pinned before training",
            "quantization": "nf4 staging recipe; not an AWQ+Finetune result",
            "lora_r": 16,
            "lora_alpha": 32,
            "lora_dropout": 0.05,
            "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            "max_seq_length": 4096,
            "gradient_checkpointing": True,
            "optimizer": "paged_adamw_8bit",
        },
        "awq_adapter_gate": {
            "status": "HOLD",
            "reason": "The existing training recipe is NF4 QLoRA. No adapter is being labeled AWQ-compatible until exact base model, revision, AWQ quantization, target modules, metadata, and save/reload are verified.",
            "required_before_valid_comparison": [
                "resolve exact served AWQ model revision and tokenizer",
                "use a training path that produces an adapter compatible with that exact AWQ base",
                "verify target modules and adapter metadata",
                "save and reload the adapter in the same serving stack",
                "run tokenizer no-truncation audit",
            ],
            "prohibited_substitution": "existing NF4 Risk QLoRA adapter",
        },
        "next_training_outputs": [
            "adapter_config.json",
            "adapter_model.safetensors",
            "adapter_save_reload_report.json",
            "adapter_provenance.json",
        ],
    }
    write_json(output_dir / "training_manifest.json", training_manifest)

    print(json.dumps({
        "status": "PREPARED",
        "train_count": len(train),
        "validation_count": len(validation),
        "train_sha256": train_hash,
        "validation_sha256": validation_hash,
        "train_distribution": distribution(train),
        "validation_distribution": distribution(validation),
        "output_dir": str(output_dir),
        "awq_adapter_gate": "HOLD until exact AWQ-compatible adapter is trained and save/reload verified",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
