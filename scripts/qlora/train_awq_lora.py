#!/usr/bin/env python3
"""Train a PEFT-compatible LoRA adapter on the exact AWQ base.

This is intentionally separate from ``train_specialist_qlora.py``.  That
script is an NF4 QLoRA recipe and must never be reported as an AWQ adapter.
AutoAWQ exposes projection layers as ``WQLinear_GEMM`` modules, which PEFT's
generic injector does not accept, so this script applies a small, explicit
LoRA wrapper around the frozen AWQ projection modules and writes the standard
PEFT adapter files expected by vLLM.

The script does not quantize or rewrite the base model.  It writes only
``adapter_config.json``, ``adapter_model.safetensors`` and JSON provenance
under the requested output directory.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import random
import time
from pathlib import Path
from typing import Any

import torch
from safetensors.torch import load_file, save_file
from transformers import AutoModelForCausalLM, AutoTokenizer


TARGET_MODULES = (
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
                raise ValueError(f"invalid training record at {path}:{line_number}")
            if len(value["messages"]) < 2 or value["messages"][-1].get("role") != "assistant":
                raise ValueError(f"assistant completion missing at {path}:{line_number}")
            records.append(value)
    if not records:
        raise ValueError(f"no records in {path}")
    return records


def percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def audit_lengths(tokenizer: Any, records: list[dict[str, Any]], max_length: int) -> dict[str, Any]:
    lengths: list[int] = []
    over_limit: list[dict[str, Any]] = []
    for record in records:
        rendered = tokenizer.apply_chat_template(
            record["messages"], tokenize=False, add_generation_prompt=False
        )
        length = len(tokenizer(rendered, add_special_tokens=False, truncation=False)["input_ids"])
        lengths.append(length)
        if length > max_length:
            over_limit.append({"id": record.get("id"), "tokens": length})
    if over_limit:
        raise ValueError(
            "tokenizer no-truncation audit failed: " + json.dumps(over_limit[:10], sort_keys=True)
        )
    return {
        "records": len(lengths),
        "min_tokens": min(lengths),
        "p50_tokens": percentile(lengths, 0.50),
        "p95_tokens": percentile(lengths, 0.95),
        "max_tokens": max(lengths),
        "max_seq_length": max_length,
        "over_limit": 0,
    }


class AWQLoraWrapper(torch.nn.Module):
    """Frozen AWQ projection plus a trainable low-rank residual."""

    def __init__(self, base_layer: torch.nn.Module, rank: int, alpha: float, dropout: float) -> None:
        super().__init__()
        self.base_layer = base_layer
        for parameter in self.base_layer.parameters():
            parameter.requires_grad_(False)
        in_features = int(base_layer.in_features)
        out_features = int(base_layer.out_features)
        device = base_layer.qweight.device
        self.lora_A = torch.nn.Linear(in_features, rank, bias=False, device=device, dtype=torch.bfloat16)
        self.lora_B = torch.nn.Linear(rank, out_features, bias=False, device=device, dtype=torch.bfloat16)
        torch.nn.init.kaiming_uniform_(self.lora_A.weight, a=5**0.5)
        torch.nn.init.zeros_(self.lora_B.weight)
        self.lora_dropout = torch.nn.Dropout(dropout)
        self.scaling = alpha / rank

    def forward(self, hidden_states: torch.Tensor, *args: Any, **kwargs: Any) -> torch.Tensor:
        base_output = self.base_layer(hidden_states, *args, **kwargs)
        lora_input = self.lora_dropout(hidden_states.to(self.lora_A.weight.dtype))
        residual = self.lora_B(self.lora_A(lora_input)) * self.scaling
        return base_output + residual.to(base_output.dtype)


def _module_device(module: torch.nn.Module) -> torch.device:
    for name in ("qweight", "qzeros", "scales"):
        value = getattr(module, name, None)
        if isinstance(value, torch.Tensor):
            return value.device
    return next(module.parameters()).device


def inject_awq_lora(model: torch.nn.Module, rank: int, alpha: float, dropout: float) -> list[str]:
    targets: list[tuple[str, torch.nn.Module]] = [
        (name, module)
        for name, module in model.named_modules()
        if name.rsplit(".", 1)[-1] in TARGET_MODULES
        and module.__class__.__name__.startswith("WQLinear")
    ]
    if len(targets) != 48 * len(TARGET_MODULES):
        raise RuntimeError(f"expected 336 AWQ target modules, found {len(targets)}")
    for name, module in targets:
        parent_name, child_name = name.rsplit(".", 1)
        parent = model.get_submodule(parent_name)
        wrapped = AWQLoraWrapper(module, rank=rank, alpha=alpha, dropout=dropout)
        if _module_device(module) != wrapped.lora_A.weight.device:
            wrapped.to(_module_device(module))
        setattr(parent, child_name, wrapped)
    return [name for name, _ in targets]


def adapter_state(model: torch.nn.Module) -> dict[str, torch.Tensor]:
    state: dict[str, torch.Tensor] = {}
    for name, module in model.named_modules():
        if isinstance(module, AWQLoraWrapper):
            prefix = f"base_model.model.{name}"
            state[f"{prefix}.lora_A.weight"] = module.lora_A.weight.detach().cpu()
            state[f"{prefix}.lora_B.weight"] = module.lora_B.weight.detach().cpu()
    if not state:
        raise RuntimeError("no LoRA state was found")
    return state


def collate_one(tokenizer: Any, record: dict[str, Any], max_length: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    messages = record["messages"]
    full_text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
    prefix_text = tokenizer.apply_chat_template(messages[:-1], tokenize=False, add_generation_prompt=True)
    full = tokenizer(full_text, add_special_tokens=False, truncation=False, return_tensors="pt")
    prefix = tokenizer(prefix_text, add_special_tokens=False, truncation=False, return_tensors="pt")
    input_ids = full["input_ids"]
    attention_mask = full["attention_mask"]
    if input_ids.shape[1] > max_length:
        raise ValueError(f"record exceeds max length: {record.get('id')}")
    prefix_ids = prefix["input_ids"][0].tolist()
    if input_ids[0, : len(prefix_ids)].tolist() != prefix_ids:
        raise ValueError(f"chat template prefix mismatch: {record.get('id')}")
    labels = input_ids.clone()
    labels[:, : len(prefix_ids)] = -100
    if torch.all(labels == -100):
        raise ValueError(f"empty assistant target: {record.get('id')}")
    return input_ids.cuda(), attention_mask.cuda(), labels.cuda()


def adapter_config(base_model: str, base_revision: str, rank: int, alpha: int, dropout: float) -> dict[str, Any]:
    return {
        "peft_type": "LORA",
        "task_type": "CAUSAL_LM",
        "base_model_name_or_path": base_model,
        "revision": base_revision,
        "r": rank,
        "lora_alpha": alpha,
        "lora_dropout": dropout,
        "bias": "none",
        "target_modules": list(TARGET_MODULES),
        "modules_to_save": None,
        "use_dora": False,
        "use_rslora": False,
        "inference_mode": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-model", type=Path, required=True)
    parser.add_argument(
        "--adapter-base-model",
        default=None,
        help="canonical model ID written to adapter_config.json; defaults to the local base path",
    )
    parser.add_argument("--base-revision", required=True)
    parser.add_argument("--train-jsonl", type=Path, required=True)
    parser.add_argument("--validation-jsonl", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-seq-length", type=int, default=4096)
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=8)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--seed", type=int, default=66)
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--alpha", type=int, default=32)
    parser.add_argument("--dropout", type=float, default=0.05)
    args = parser.parse_args()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required")
    if args.base_revision == "UNKNOWN":
        raise RuntimeError("exact AWQ base revision is required")
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    train = read_jsonl(args.train_jsonl)
    validation = read_jsonl(args.validation_jsonl)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, local_files_only=True, trust_remote_code=True)
    if tokenizer.chat_template is None:
        raise RuntimeError("exact AWQ tokenizer has no chat template")
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    length_audit = audit_lengths(tokenizer, train + validation, args.max_seq_length)

    started = time.time()
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        revision=args.base_revision,
        device_map="auto",
        torch_dtype=torch.float16,
        trust_remote_code=True,
        low_cpu_mem_usage=True,
        local_files_only=True,
    )
    quantization_config = getattr(model.config, "quantization_config", None)
    quant_method = (
        getattr(quantization_config, "quant_method", None)
        if quantization_config is not None
        else None
    )
    if quant_method is None and isinstance(quantization_config, dict):
        quant_method = quantization_config.get("quant_method")
    quant_method_value = getattr(quant_method, "value", quant_method)
    if str(quant_method_value).lower() != "awq":
        raise RuntimeError("loaded base is not marked quant_method=awq")
    model.config.use_cache = False
    # AWQ projection weights are frozen, and the remaining norms/embeddings
    # must be frozen too.  Only the explicitly inserted LoRA matrices may
    # receive gradients.
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    target_names = inject_awq_lora(model, args.rank, args.alpha, args.dropout)
    for parameter in model.parameters():
        if not isinstance(parameter, torch.nn.Parameter):
            continue
        if parameter.requires_grad and parameter.dtype not in (torch.float16, torch.bfloat16, torch.float32):
            parameter.requires_grad_(False)
    if hasattr(model, "enable_input_require_grads"):
        model.enable_input_require_grads()
    if hasattr(model, "gradient_checkpointing_enable"):
        try:
            model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        except TypeError:
            model.gradient_checkpointing_enable()
    model.train()
    trainable = [p for p in model.parameters() if p.requires_grad]
    if not trainable:
        raise RuntimeError("no trainable LoRA parameters")
    optimizer = torch.optim.AdamW(trainable, lr=args.learning_rate)
    steps_per_epoch = (len(train) + args.gradient_accumulation_steps - 1) // args.gradient_accumulation_steps
    total_updates = max(1, int(args.epochs * steps_per_epoch))
    losses: list[float] = []
    optimizer.zero_grad(set_to_none=True)
    update = 0
    for epoch in range(max(1, int(args.epochs))):
        order = list(range(len(train)))
        random.Random(args.seed + epoch).shuffle(order)
        for offset, index in enumerate(order):
            input_ids, attention_mask, labels = collate_one(tokenizer, train[index], args.max_seq_length)
            outputs = model(input_ids=input_ids, attention_mask=attention_mask, labels=labels, use_cache=False)
            loss = outputs.loss / args.gradient_accumulation_steps
            loss.backward()
            losses.append(float(loss.detach().cpu()) * args.gradient_accumulation_steps)
            if (offset + 1) % args.gradient_accumulation_steps == 0 or offset + 1 == len(order):
                torch.nn.utils.clip_grad_norm_(trainable, 1.0)
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
                update += 1
                if update == 1 or update % 10 == 0:
                    print(json.dumps({"epoch": epoch, "update": update, "total_updates": total_updates, "loss": losses[-1]}), flush=True)
            del input_ids, attention_mask, labels, outputs, loss
        gc.collect()
        torch.cuda.empty_cache()

    model.eval()
    eval_losses: list[float] = []
    with torch.no_grad():
        for record in validation:
            input_ids, attention_mask, labels = collate_one(tokenizer, record, args.max_seq_length)
            outputs = model(input_ids=input_ids, attention_mask=attention_mask, labels=labels, use_cache=False)
            eval_losses.append(float(outputs.loss.detach().cpu()))
            del input_ids, attention_mask, labels, outputs
    state = adapter_state(model)
    save_file(state, str(args.output_dir / "adapter_model.safetensors"), metadata={"format": "pt"})
    config = adapter_config(
        args.adapter_base_model or str(args.base_model),
        args.base_revision,
        args.rank,
        args.alpha,
        args.dropout,
    )
    (args.output_dir / "adapter_config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    metadata = {
        "schema_version": "awq-adapter-provenance.v1",
        "status": "TRAINED",
        "base_model": args.adapter_base_model or str(args.base_model),
        "base_model_local_path": str(args.base_model),
        "base_revision": args.base_revision,
        "quantization": {"quant_method": "awq", "bits": 4, "group_size": 128, "version": "gemm", "zero_point": True},
        "target_modules": list(TARGET_MODULES),
        "adapter": {"rank": args.rank, "alpha": args.alpha, "dropout": args.dropout, "dtype": "bfloat16"},
        "dataset": {
            "train_path": str(args.train_jsonl),
            "train_sha256": sha256_file(args.train_jsonl),
            "train_count": len(train),
            "validation_path": str(args.validation_jsonl),
            "validation_sha256": sha256_file(args.validation_jsonl),
            "validation_count": len(validation),
        },
        "tokenizer_audit": length_audit,
        "training": {
            "epochs": args.epochs,
            "updates": update,
            "gradient_accumulation_steps": args.gradient_accumulation_steps,
            "learning_rate": args.learning_rate,
            "seed": args.seed,
            "mean_train_loss": sum(losses) / len(losses),
            "mean_validation_loss": sum(eval_losses) / len(eval_losses),
            "max_train_loss": max(losses),
            "min_train_loss": min(losses),
        },
        "runtime": {
            "python": __import__("platform").python_version(),
            "torch": torch.__version__,
            "cuda": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(0),
            "model_load_and_train_seconds": round(time.time() - started, 3),
        },
        "adapter_files": {
            "config": "adapter_config.json",
            "config_sha256": sha256_file(args.output_dir / "adapter_config.json"),
            "weights": "adapter_model.safetensors",
            "weights_sha256": sha256_file(args.output_dir / "adapter_model.safetensors"),
            "tensor_count": len(state),
        },
        "target_count": len(target_names),
        "save_reload": {"status": "PENDING", "reason": "vLLM serving-stack reload is required after training"},
    }
    (args.output_dir / "adapter_provenance.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "TRAINED", "output_dir": str(args.output_dir), "train_count": len(train), "validation_count": len(validation), "target_count": len(target_names), "mean_validation_loss": metadata["training"]["mean_validation_loss"]}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
