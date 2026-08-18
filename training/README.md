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

External-50, Internal-v1, and Internal-v2 are held out. Exact and conservative
near-duplicate contamination checks are performed against the benchmark
directory and block preparation on any match.

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
the collator masks all non-assistant tokens.

Colab setup and run:

```bash
pip install -r training/qlora/requirements-colab.txt
python scripts/qlora/train_specialist_qlora.py \
  --department risk \
  --adapter-version v1 \
  --common-dir hgfinance_common_training_v1 \
  --department-train datasets/risk/train.jsonl \
  --department-validation datasets/risk/validation.jsonl \
  --benchmark-root benchmarks/quantization \
  --target-train-size 4000 \
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
