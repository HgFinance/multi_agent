"""Prepare and optionally train one HgFinance department adapter.

The default path is CPU-safe preparation. Actual training is opt-in through
the absence of ``--dry-run`` and requires CUDA plus bitsandbytes.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from training.specialist.config import QLoRAConfig
from training.specialist.contamination import check_contamination, require_clean
from training.specialist.metadata import build_training_metadata, write_metadata
from training.specialist.mixing import (
    DatasetPool,
    load_pool,
    mix_pools,
    preserve_pool_split,
    write_jsonl,
)
from training.specialist.schema import DatasetValidationError, ValidatedExample


def adapter_name(department: str | None, version: str, *, common_only: bool = False) -> str:
    if common_only:
        department = "common"
    if department is None:
        raise DatasetValidationError("department is required outside --common-only mode")
    department = department.strip().lower().replace("_", "-")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", department):
        raise DatasetValidationError("department must be lowercase slug text")
    normalized_version = version if version.startswith("v") else f"v{version}"
    if not re.fullmatch(r"v[0-9]+(?:[.-][a-z0-9]+)*", normalized_version):
        raise DatasetValidationError("adapter version must look like v1")
    return f"hgfinance-{department}-{normalized_version}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare/train one HgFinance specialist QLoRA adapter")
    parser.add_argument("--common-only", action="store_true")
    parser.add_argument("--department")
    parser.add_argument("--adapter-version", default="v1")
    parser.add_argument("--common-dir", type=Path, required=True)
    parser.add_argument("--department-train", type=Path)
    parser.add_argument("--department-validation", type=Path)
    parser.add_argument("--general-train", type=Path)
    parser.add_argument("--general-validation", type=Path)
    parser.add_argument("--benchmark-root", type=Path, required=True)
    parser.add_argument("--target-train-size", type=int)
    parser.add_argument("--common-ratio", type=float, default=0.25)
    parser.add_argument("--general-ratio", type=float, default=0.15)
    parser.add_argument("--department-ratio", type=float, default=0.60)
    parser.add_argument("--seed", type=int, default=66)
    parser.add_argument("--allow-replacement", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-model", default="Qwen/Qwen2.5-14B-Instruct")
    parser.add_argument("--base-revision")
    parser.add_argument("--max-seq-length", type=int, default=2048)
    parser.add_argument("--num-train-epochs", type=float, default=3.0)
    parser.add_argument("--per-device-train-batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=16)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--dry-run", action="store_true", help="prepare and validate only; never import model libraries")
    return parser


def _common_pool(common_dir: Path) -> DatasetPool:
    return load_pool("common", common_dir / "common_train.jsonl", common_dir / "common_validation.jsonl")


def _validation_records(pools: list[DatasetPool]) -> list[ValidatedExample]:
    exact: set[str] = set()
    normalized: set[str] = set()
    result: list[ValidatedExample] = []
    for pool in pools:
        for example in pool.validation:
            if example.record_sha256 in exact or example.normalized_sha256 in normalized:
                raise DatasetValidationError("duplicate validation example across dataset pools")
            exact.add(example.record_sha256)
            normalized.add(example.normalized_sha256)
            result.append(example)
    return result


def _prepare_common_only(args: argparse.Namespace) -> tuple[Path, dict[str, Any], QLoRAConfig, str]:
    if args.department or args.department_train or args.department_validation:
        raise DatasetValidationError("department inputs are not allowed with --common-only")
    if args.general_train or args.general_validation:
        raise DatasetValidationError("general inputs are not allowed with --common-only")
    if args.allow_replacement:
        raise DatasetValidationError("--allow-replacement is not allowed with --common-only")

    common = _common_pool(args.common_dir)
    if args.target_train_size is not None and args.target_train_size != len(common.train):
        raise DatasetValidationError(
            "--target-train-size cannot resample Common in --common-only; "
            f"expected {len(common.train)}"
        )
    train_records = preserve_pool_split(common, "train")
    validation_records = preserve_pool_split(common, "validation")
    train_contamination = check_contamination(common.train, args.benchmark_root)
    validation_contamination = check_contamination(common.validation, args.benchmark_root)
    require_clean(train_contamination)
    require_clean(validation_contamination)
    contamination = {
        "status": "PASS",
        "train": train_contamination,
        "validation": validation_contamination,
    }
    mixture: dict[str, Any] = {
        "common_only": True,
        "seed": args.seed,
        "target_train_size": len(train_records),
        "actual_train_size": len(train_records),
        "validation_size": len(validation_records),
        "ratios_requested": {"common": 1.0},
        "effective_ratios": {"common": 1.0},
        "selected_by_pool": {"common": len(train_records)},
        "allow_replacement": False,
        "resplit": False,
        "category_counts": {
            category: sum(1 for record in train_records if record.get("category") == category)
            for category in sorted({str(record.get("category")) for record in train_records})
        },
        "deduplication": {
            "per_pool": {
                "common": {
                    "exact_duplicates_removed": 0,
                    "normalized_duplicates_removed": 0,
                }
            },
            "global": {
                "exact_duplicates_removed": 0,
                "normalized_duplicates_removed": 0,
            },
        },
        "pool_sha256": {
            "common": {
                "train": common.train_sha256,
                "validation": common.validation_sha256,
                "dataset": common.dataset_sha256,
            }
        },
        "contamination": contamination,
    }
    mixture["mixture_sha256"] = hashlib.sha256(
        "\n".join(record["record_sha256"] for record in train_records).encode()
    ).hexdigest()

    qlora = QLoRAConfig(base_model=args.base_model, base_revision=args.base_revision)
    name = adapter_name(None, args.adapter_version, common_only=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prepared = args.output_dir / "prepared"
    write_jsonl(train_records, prepared / "train.jsonl")
    write_jsonl(validation_records, prepared / "validation.jsonl")
    metadata = build_training_metadata(
        repo_root=ROOT,
        department=None,
        adapter_name=name,
        adapter_version=args.adapter_version,
        common_dataset_sha256=common.dataset_sha256,
        common_train_sha256=common.train_sha256,
        common_validation_sha256=common.validation_sha256,
        department_dataset_sha256=None,
        mixture_metadata=mixture,
        qlora=qlora,
        training_mode="common_only",
        optimizer_args={"learning_rate": args.learning_rate, "optimizer": qlora.optimizer},
        training_args={
            "max_seq_length": args.max_seq_length,
            "num_train_epochs": args.num_train_epochs,
            "per_device_train_batch_size": args.per_device_train_batch_size,
            "gradient_accumulation_steps": args.gradient_accumulation_steps,
            "optimizer": qlora.optimizer,
            "gradient_checkpointing": qlora.gradient_checkpointing,
            "gradient_checkpointing_use_reentrant": qlora.gradient_checkpointing_use_reentrant,
            "target_train_size": args.target_train_size,
            "resplit": False,
            "allow_replacement": False,
            "validation_size": len(validation_records),
        },
    )
    write_metadata(args.output_dir / "training_metadata.json", metadata)
    return prepared, metadata, qlora, name


def _prepare(args: argparse.Namespace) -> tuple[Path, dict[str, Any], QLoRAConfig, str]:
    if args.common_only:
        return _prepare_common_only(args)
    if not args.department or not args.department_train or not args.department_validation:
        raise DatasetValidationError(
            "--department, --department-train, and --department-validation are required "
            "unless --common-only is used"
        )
    if args.target_train_size is None:
        raise DatasetValidationError("--target-train-size is required for department training")
    if (args.general_train is None) != (args.general_validation is None):
        raise DatasetValidationError("general train and validation paths must be supplied together")
    name = adapter_name(args.department, args.adapter_version)
    qlora = QLoRAConfig(base_model=args.base_model, base_revision=args.base_revision)
    pools: dict[str, DatasetPool] = {
        "common": _common_pool(args.common_dir),
        "department": load_pool("department", args.department_train, args.department_validation),
    }
    ratios = {
        "common": args.common_ratio,
        "general": args.general_ratio,
        "department": args.department_ratio,
    }
    if args.general_train is not None:
        pools["general"] = load_pool("general", args.general_train, args.general_validation)
    else:
        ratios["general"] = 0.0

    train_records, mixture = mix_pools(
        pools,
        ratios=ratios,
        target_size=args.target_train_size,
        seed=args.seed,
        allow_replacement=args.allow_replacement,
        benchmark_root=args.benchmark_root,
    )
    validation = _validation_records(list(pools.values()))
    validation_check = check_contamination(validation, args.benchmark_root)
    require_clean(validation_check)
    mixture["validation_size"] = len(validation)
    mixture["validation_contamination"] = validation_check

    args.output_dir.mkdir(parents=True, exist_ok=True)
    prepared = args.output_dir / "prepared"
    write_jsonl(train_records, prepared / "train.jsonl")
    write_jsonl(
        [
            {
                **example.record,
                "messages": example.messages,
                "source_dataset": example.source_dataset,
                "source_file": example.source_file,
                "source_row": example.source_row,
                "record_sha256": example.record_sha256,
                "normalized_record_sha256": example.normalized_sha256,
            }
            for example in validation
        ],
        prepared / "validation.jsonl",
    )
    metadata = build_training_metadata(
        repo_root=ROOT,
        department=args.department,
        adapter_name=name,
        adapter_version=args.adapter_version,
        common_dataset_sha256=pools["common"].dataset_sha256,
        department_dataset_sha256=pools["department"].dataset_sha256,
        mixture_metadata=mixture,
        qlora=qlora,
        optimizer_args={"learning_rate": args.learning_rate, "optimizer": qlora.optimizer},
        training_args={
            "max_seq_length": args.max_seq_length,
            "num_train_epochs": args.num_train_epochs,
            "per_device_train_batch_size": args.per_device_train_batch_size,
            "gradient_accumulation_steps": args.gradient_accumulation_steps,
            "optimizer": qlora.optimizer,
            "gradient_checkpointing": qlora.gradient_checkpointing,
            "gradient_checkpointing_use_reentrant": qlora.gradient_checkpointing_use_reentrant,
            "validation_size": len(validation),
        },
    )
    write_metadata(args.output_dir / "training_metadata.json", metadata)
    return prepared, metadata, qlora, name


def _chat_token_ids(tokenizer: Any, messages: list[dict[str, str]]) -> list[int]:
    rendered = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=False
    )
    encoded = tokenizer(rendered, add_special_tokens=False, truncation=False)
    return list(encoded["input_ids"])


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def audit_dataset_lengths(tokenizer: Any, datasets: Any, max_seq_length: int) -> dict[str, Any]:
    lengths: list[int] = []
    over_limit: list[dict[str, Any]] = []
    example_count = 0
    for split in ("train", "validation"):
        for index, row in enumerate(datasets[split]):
            sample_id = row.get("id", f"{split}:{index}")
            token_length = len(_chat_token_ids(tokenizer, row["messages"]))
            lengths.append(token_length)
            example_count += 1
            if token_length > max_seq_length:
                over_limit.append(
                    {
                        "split": split,
                        "sample_id": sample_id,
                        "full_token_length": token_length,
                    }
                )
    if not lengths:
        raise DatasetValidationError("dataset length audit found zero examples")
    return {
        "example_count": example_count,
        "p50_token_length": _percentile(lengths, 0.50),
        "p95_token_length": _percentile(lengths, 0.95),
        "p99_token_length": _percentile(lengths, 0.99),
        "max_token_length": max(lengths),
        "max_seq_length": max_seq_length,
        "over_limit_count": len(over_limit),
        "over_limit_examples": over_limit,
    }


def _resolve_loaded_revision(tokenizer: Any, model: Any) -> str | None:
    candidates = [
        getattr(tokenizer, "_commit_hash", None),
        getattr(getattr(model, "config", None), "_commit_hash", None),
        getattr(tokenizer, "init_kwargs", {}).get("_commit_hash"),
    ]
    return next((candidate for candidate in candidates if isinstance(candidate, str) and candidate), None)


def require_base_revision(base_revision: str | None, *, dry_run: bool) -> None:
    if not dry_run and not base_revision:
        raise DatasetValidationError(
            "--base-revision is required for real training; omission is allowed only with --dry-run"
        )


def _prepare_kbit_model(model: Any, prepare_fn: Any, qlora: QLoRAConfig) -> Any:
    parameters = inspect.signature(prepare_fn).parameters
    kwargs: dict[str, Any] = {}
    if "use_gradient_checkpointing" in parameters:
        kwargs["use_gradient_checkpointing"] = qlora.gradient_checkpointing
    if "gradient_checkpointing_kwargs" in parameters:
        kwargs["gradient_checkpointing_kwargs"] = {
            "use_reentrant": qlora.gradient_checkpointing_use_reentrant
        }
    model = prepare_fn(model, **kwargs)
    if qlora.gradient_checkpointing and "use_gradient_checkpointing" not in parameters:
        enable_parameters = inspect.signature(model.gradient_checkpointing_enable).parameters
        if "gradient_checkpointing_kwargs" in enable_parameters:
            model.gradient_checkpointing_enable(
                gradient_checkpointing_kwargs={
                    "use_reentrant": qlora.gradient_checkpointing_use_reentrant
                }
            )
        else:
            model.gradient_checkpointing_enable()
    return model


class AssistantOnlyCollator:
    """Apply the Qwen template and mask every non-assistant token."""

    def __init__(self, tokenizer: Any, max_length: int) -> None:
        self.tokenizer = tokenizer
        self.max_length = max_length

    def __call__(self, features: list[dict[str, Any]]) -> dict[str, Any]:
        rows: list[dict[str, list[int]]] = []
        for feature in features:
            messages = feature["messages"]
            full_text = self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=False
            )
            prefix_text = self.tokenizer.apply_chat_template(
                messages[:-1], tokenize=False, add_generation_prompt=True
            )
            sample_id = feature.get("id", "unknown")
            full = self.tokenizer(full_text, add_special_tokens=False, truncation=False)
            full_length = len(full["input_ids"])
            if full_length > self.max_length:
                raise DatasetValidationError(
                    "assistant completion cannot be safely truncated: "
                    f"sample_id={sample_id} full_token_length={full_length} "
                    f"max_seq_length={self.max_length}"
                )
            prefix = self.tokenizer(prefix_text, add_special_tokens=False, truncation=False)
            if full["input_ids"][: len(prefix["input_ids"])] != prefix["input_ids"]:
                raise DatasetValidationError("Qwen chat template prefix is not a full-sequence prefix")
            if len(full["input_ids"]) <= len(prefix["input_ids"]):
                raise DatasetValidationError("assistant completion was truncated or empty")
            labels = [-100] * len(prefix["input_ids"]) + full["input_ids"][len(prefix["input_ids"]):]
            rows.append({"input_ids": full["input_ids"], "attention_mask": full["attention_mask"], "labels": labels})

        import torch

        batch = self.tokenizer.pad(
            [{"input_ids": row["input_ids"], "attention_mask": row["attention_mask"]} for row in rows],
            return_tensors="pt",
        )
        max_len = batch["input_ids"].shape[1]
        labels = [row["labels"] + [-100] * (max_len - len(row["labels"])) for row in rows]
        batch["labels"] = torch.tensor(labels, dtype=torch.long)
        return batch


def _train(args: argparse.Namespace, prepared: Path, metadata: dict[str, Any], qlora: QLoRAConfig, name: str) -> None:
    try:
        import torch
        from datasets import load_dataset
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, Trainer, TrainingArguments
        import bitsandbytes  # noqa: F401
    except ImportError as exc:
        raise SystemExit(f"training dependencies missing; use the Colab requirements: {exc}") from exc
    if not torch.cuda.is_available():
        raise SystemExit("QLoRA training requires CUDA; no model was loaded")

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, revision=args.base_revision, trust_remote_code=True)
    if tokenizer.chat_template is None:
        raise SystemExit("base tokenizer has no chat template")
    datasets = load_dataset(
        "json",
        data_files={"train": str(prepared / "train.jsonl"), "validation": str(prepared / "validation.jsonl")},
    )
    length_audit = audit_dataset_lengths(tokenizer, datasets, args.max_seq_length)
    metadata["length_audit"] = length_audit
    write_metadata(args.output_dir / "training_metadata.json", metadata)
    if length_audit["over_limit_count"]:
        raise DatasetValidationError(
            "dataset length audit failed; truncation policy is not enabled: "
            + json.dumps(length_audit, sort_keys=True)
        )

    compute_dtype = torch.bfloat16 if qlora.bnb_4bit_compute_dtype == "bfloat16" else torch.float16
    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=compute_dtype,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        revision=args.base_revision,
        quantization_config=quantization,
        device_map="auto",
        trust_remote_code=True,
    )
    resolved_revision = _resolve_loaded_revision(tokenizer, model)
    metadata["resolved_base_revision"] = resolved_revision
    write_metadata(args.output_dir / "training_metadata.json", metadata)
    if hasattr(model.config, "use_cache"):
        model.config.use_cache = False
    model = _prepare_kbit_model(model, prepare_model_for_kbit_training, qlora)
    model = get_peft_model(
        model,
        LoraConfig(
            r=qlora.lora_r,
            lora_alpha=qlora.lora_alpha,
            lora_dropout=qlora.lora_dropout,
            target_modules=list(qlora.target_modules),
            task_type="CAUSAL_LM",
        ),
        adapter_name=name,
    )
    training_parameters = inspect.signature(TrainingArguments).parameters
    required_training_parameters = {"optim", "gradient_checkpointing", "gradient_checkpointing_kwargs"}
    missing_training_parameters = required_training_parameters - set(training_parameters)
    if missing_training_parameters:
        raise SystemExit(
            "installed Transformers lacks explicit training controls: "
            f"{sorted(missing_training_parameters)}"
        )
    training_args = TrainingArguments(
        output_dir=str(args.output_dir / ".trainer"),
        num_train_epochs=args.num_train_epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        optim=qlora.optimizer,
        gradient_checkpointing=qlora.gradient_checkpointing,
        gradient_checkpointing_kwargs={
            "use_reentrant": qlora.gradient_checkpointing_use_reentrant
        },
        bf16=torch.cuda.is_bf16_supported(),
        fp16=not torch.cuda.is_bf16_supported(),
        eval_strategy="epoch",
        save_strategy="no",
        report_to=[],
        seed=args.seed,
        remove_unused_columns=False,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=datasets["train"],
        eval_dataset=datasets["validation"],
        data_collator=AssistantOnlyCollator(tokenizer, args.max_seq_length),
    )
    trainer.train()
    metrics = trainer.evaluate()
    model.save_pretrained(args.output_dir, safe_serialization=True)
    metadata["final_metrics"] = metrics
    write_metadata(args.output_dir / "training_metadata.json", metadata)
    expected = {"adapter_model.safetensors", "adapter_config.json", "training_metadata.json"}
    actual = {path.name for path in args.output_dir.iterdir() if path.is_file()}
    missing = expected - actual
    if missing:
        raise SystemExit(f"adapter-only artifact check failed; missing={sorted(missing)}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    require_base_revision(args.base_revision, dry_run=args.dry_run)
    prepared, metadata, qlora, name = _prepare(args)
    print(f"prepared {name}: {prepared}")
    if args.dry_run:
        print("dry-run: no tokenizer/model download, training, or inference executed")
        return 0
    _train(args, prepared, metadata, qlora, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
