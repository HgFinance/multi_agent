"""Prepare and optionally train one HgFinance department adapter.

The default path is CPU-safe preparation. Actual training is opt-in through
the absence of ``--dry-run`` and requires CUDA plus bitsandbytes.
"""

from __future__ import annotations

import argparse
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
from training.specialist.mixing import DatasetPool, load_pool, mix_pools, write_jsonl
from training.specialist.schema import DatasetValidationError, ValidatedExample


def adapter_name(department: str, version: str) -> str:
    department = department.strip().lower().replace("_", "-")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", department):
        raise DatasetValidationError("department must be lowercase slug text")
    normalized_version = version if version.startswith("v") else f"v{version}"
    if not re.fullmatch(r"v[0-9]+(?:[.-][a-z0-9]+)*", normalized_version):
        raise DatasetValidationError("adapter version must look like v1")
    return f"hgfinance-{department}-{normalized_version}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare/train one HgFinance specialist QLoRA adapter")
    parser.add_argument("--department", required=True)
    parser.add_argument("--adapter-version", default="v1")
    parser.add_argument("--common-dir", type=Path, required=True)
    parser.add_argument("--department-train", type=Path, required=True)
    parser.add_argument("--department-validation", type=Path, required=True)
    parser.add_argument("--general-train", type=Path)
    parser.add_argument("--general-validation", type=Path)
    parser.add_argument("--benchmark-root", type=Path, required=True)
    parser.add_argument("--target-train-size", type=int, required=True)
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


def _prepare(args: argparse.Namespace) -> tuple[Path, dict[str, Any], QLoRAConfig, str]:
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
        optimizer_args={"learning_rate": args.learning_rate, "optimizer": "paged_adamw_8bit"},
        training_args={
            "max_seq_length": args.max_seq_length,
            "num_train_epochs": args.num_train_epochs,
            "per_device_train_batch_size": args.per_device_train_batch_size,
            "gradient_accumulation_steps": args.gradient_accumulation_steps,
            "validation_size": len(validation),
        },
    )
    write_metadata(args.output_dir / "training_metadata.json", metadata)
    return prepared, metadata, qlora, name


class AssistantOnlyCollator:
    """Apply the Qwen template and mask every non-assistant token."""

    def __init__(self, tokenizer: Any, max_length: int) -> None:
        self.tokenizer = tokenizer
        self.max_length = max_length

    def __call__(self, features: list[dict[str, Any]]) -> dict[str, Any]:
        import torch

        rows: list[dict[str, list[int]]] = []
        for feature in features:
            messages = feature["messages"]
            full_text = self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=False
            )
            prefix_text = self.tokenizer.apply_chat_template(
                messages[:-1], tokenize=False, add_generation_prompt=True
            )
            full = self.tokenizer(full_text, add_special_tokens=False, truncation=True, max_length=self.max_length)
            prefix = self.tokenizer(prefix_text, add_special_tokens=False, truncation=False)
            if full["input_ids"][: len(prefix["input_ids"])] != prefix["input_ids"]:
                raise DatasetValidationError("Qwen chat template prefix is not a full-sequence prefix")
            if len(full["input_ids"]) <= len(prefix["input_ids"]):
                raise DatasetValidationError("assistant completion was truncated or empty")
            labels = [-100] * len(prefix["input_ids"]) + full["input_ids"][len(prefix["input_ids"]):]
            rows.append({"input_ids": full["input_ids"], "attention_mask": full["attention_mask"], "labels": labels})

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
    model = prepare_model_for_kbit_training(model)
    model = get_peft_model(
        model,
        LoraConfig(
            r=qlora.lora_r,
            lora_alpha=qlora.lora_alpha,
            lora_dropout=qlora.lora_dropout,
            target_modules=list(qlora.target_modules),
            task_type="CAUSAL_LM",
        ),
    )
    datasets = load_dataset(
        "json",
        data_files={"train": str(prepared / "train.jsonl"), "validation": str(prepared / "validation.jsonl")},
    )
    training_args = TrainingArguments(
        output_dir=str(args.output_dir / ".trainer"),
        num_train_epochs=args.num_train_epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
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
    prepared, metadata, qlora, name = _prepare(args)
    print(f"prepared {name}: {prepared}")
    if args.dry_run:
        print("dry-run: no tokenizer/model download, training, or inference executed")
        return 0
    _train(args, prepared, metadata, qlora, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
