# HgFinance specialist QLoRA pipeline

This directory contains training-only preparation and QLoRA tooling. It does
not change the production AWQ serving plane and does not run inference.

## Dataset contract

The existing Common package remains the single source of truth at
`hgfinance_common_training_v1/`. It is not copied into another dataset tree.
It contains 2,545 train and 223 validation records. Preferred records use:

```json
{
  "id": "...",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "category": "...",
  "behavior_themes": ["..."],
  "sample_sha256": "..."
}
```

Department datasets use the same schema and must provide separate
`train.jsonl` and `validation.jsonl` files. Legacy `instruct`/`input`/`output`
records are accepted and converted to an equivalent messages envelope during
preparation.

## Deterministic preparation

The default mixture is Common 25%, optional general/finance pool 15%, and
department 60%. Ratios are CLI inputs, not policy. Sampling uses seed 66,
largest-remainder allocation, and a pool-specific deterministic PRNG. Exact
and normalized duplicates are rejected within splits and across train/
validation boundaries. Every output record contains source path/row and raw
and normalized record SHA256 values.

External-50, Internal-v1, and Internal-v2 / EmployeeReasoning are held out.
The benchmark directory must contain the explicit manifest files
`external50_v1.json`, `internal50_v1.json`, and `internal50_v2_reasoning.json`;
missing, empty, malformed-only, or unreadable roots fail closed. Exact and
conservative near-duplicate contamination checks block preparation on any
match.

Legacy `instruct`/`input`/`output` records treat `instruct` as user content,
not system content. `instruct` and `output` are required; `input` is optional.
When present, input is appended to instruct with a stable separator. A single
reusable HgFinance default system policy is used when legacy `system` is absent.
Train/validation isolation checks canonical user-question hashes in addition
to full-record hashes.

## CPU-safe validation

```bash
python scripts/qlora/train_specialist_qlora.py --help
python scripts/qlora/train_specialist_qlora.py \
  --department risk \
  --adapter-version v1 \
  --common-dir hgfinance_common_training_v1 \
  --department-train datasets/risk/train.jsonl \
  --department-validation datasets/risk/validation.jsonl \
  --benchmark-root benchmarks/quantization \
  --target-train-size 4000 \
  --output-dir training_runs/hgfinance-risk-v1 \
  --dry-run
```

The command requires real department files. `--dry-run` prepares and validates
data only; it does not import model-training libraries or download Qwen
weights.

## QLoRA recipe

The prepared training entrypoint uses the original
`Qwen/Qwen2.5-14B-Instruct` lineage, 4-bit NF4 with double quantization,
BF16/FP16 compute, LoRA rank 16, alpha 32, dropout 0.05, and the seven
attention/MLP projection target modules. Rank is validated against the
production `max-lora-rank=32` limit. The Qwen chat template is required and
the collator masks all non-assistant tokens. Full rendered sequences are
audited for p50/p95/p99/max length before the base model is loaded; any
over-limit sample fails closed instead of truncating an assistant answer.
Gradient checkpointing is explicit, `model.config.use_cache=False` is applied,
and `paged_adamw_8bit` is configured both in `TrainingArguments` and metadata.
Real training requires `--base-revision`; omission remains allowed only for
`--dry-run`. Metadata records requested and resolved revisions separately.

Colab setup and run:

```bash
pip install -r training/qlora/requirements-colab.txt
export QWEN_BASE_REVISION="<resolved Hugging Face commit SHA>"
python scripts/qlora/train_specialist_qlora.py \
  --department risk \
  --adapter-version v1 \
  --common-dir hgfinance_common_training_v1 \
  --department-train datasets/risk/train.jsonl \
  --department-validation datasets/risk/validation.jsonl \
  --benchmark-root benchmarks/quantization \
  --target-train-size 4000 \
  --base-revision "$QWEN_BASE_REVISION" \
  --output-dir training_runs/hgfinance-risk-v1
```

Expected adapter-only artifacts:

```text
training_runs/hgfinance-risk-v1/
├── adapter_model.safetensors
├── adapter_config.json
├── training_metadata.json
└── prepared/
    ├── train.jsonl
    └── validation.jsonl
```

Training is not executed by repository tests. The Common package has been
mechanically cleaned and deduplicated, but its finance answers are not an
independently expert-certified regulatory or accounting gold set.

## Common-only smoke test

The first experiment can validate one shared behavior adapter without a
department dataset. This mode preserves the Common 2,545/223 train/validation
splits exactly; it does not re-split, oversample, or require a target size.

```bash
python scripts/qlora/train_specialist_qlora.py \
  --common-only \
  --adapter-version v1 \
  --common-dir hgfinance_common_training_v1 \
  --benchmark-root benchmarks/quantization \
  --output-dir training_runs/hgfinance-common-v1 \
  --base-model Qwen/Qwen2.5-14B-Instruct \
  --base-revision "$QWEN_BASE_REVISION" \
  --dry-run
```

The resolved adapter name is `hgfinance-common-v1`. Department specialist
training remains available by omitting `--common-only` and supplying the
department train/validation files and target size.
