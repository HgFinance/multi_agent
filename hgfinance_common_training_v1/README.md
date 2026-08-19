# HgFinance Common Training Dataset v1

- Source: `hgfinance_common_v2.jsonl`
- Total: 2768
- Train: 2545
- Validation: 223
- Seed: 66
- Validation ratio target: 8%

## Preferred files

- `common_train.jsonl`: Qwen chat/messages format
- `common_validation.jsonl`: Qwen chat/messages format

Alternative trainer schema:

- `common_train_sft.jsonl`
- `common_validation_sft.jsonl`

## Important

This is the **Common** portion only. The intended specialist training set is:

`Common + Department-specific data -> one department specialist adapter`

Do not stack a Common LoRA and Department LoRA at runtime.

Keep External-50, Internal-v1, and Internal-v2 held out.

Before training in the repository, run:

```bash
python check_benchmark_contamination.py common_train.jsonl /path/to/repo/benchmarks
python check_benchmark_contamination.py common_validation.jsonl /path/to/repo/benchmarks
```

Any exact or near match should block training until reviewed.
