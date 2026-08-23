# AWS L4-fp8KV-v1 — rollback baseline

이 파일은 7축 최종 재실행 전에 보존한 롤백 기준이다. 이 기준에 포함되지 않은 결과는 최종 비교에 자동으로 편입하지 않는다.

## Repository state

- HEAD: `34b2ca53c49bde3452904c260c7c369d82eaa743`
- Worktree changes intentionally present before the rerun:
  - `benchmarks/quantization/run_hybrid_generic.py`
  - `benchmarks/quantization/run_hybrid_sixaxis.py`
  - `benchmarks/quantization/results/aws_l4_fp8kv_v1/aws_l4_fp8kv_v1_7axis_current_status.md`
- No reset, clean, stash, worktree creation, or destructive operation is authorized by this record.

## Frozen inputs

| File | SHA256 |
|---|---|
| `benchmarks/quantization/internal50_v2_reasoning.json` | `ad2bdaf5ea381c2fc151fce1f1859f7f925b86fd03b830319cd97af17709e978` |
| `benchmarks/quantization/external50_v1.json` | `197f0828fee37a8a0ca7551304efa722b005a3d26379a2cc613ece36b315956f` |

## Current implementation fingerprints

| File | SHA256 at baseline capture |
|---|---|
| `benchmarks/quantization/run_hybrid_generic.py` | `4a51d2ba631e88871b641ca812c718dcd55716cf83314ef2bbce7c0739adf341` |
| `benchmarks/quantization/run_hybrid_sixaxis.py` | `b6e981a73d357f928dfed5b33f0f2f2b2f2b5a74d85b1d58bac84e4afe382f11d7ea` |

## Existing controlled comparison

The previous controlled table remains at:

`benchmarks/quantization/results/aws_l4_fp8kv_v1/aws_l4_fp8kv_v1_7axis_comparison.md`

Its official base pair is FP8 vs AWQ under `aws-l4-fp8kv-v1-fair-v2-20260822`:

- NVIDIA L4
- vLLM 0.27.1
- Python 3.12.13
- FlashInfer 0.6.16.post3
- CUDA nvcc 13.0.88
- `max_model_len=8192`
- `gpu_memory_utilization=0.85`
- `kv_cache_dtype=fp8_e4m3`
- prefix caching enabled
- sequential quality execution
- localhost endpoint only

The latest status table, which explicitly labels partial paired screening, is:

`benchmarks/quantization/results/aws_l4_fp8kv_v1/aws_l4_fp8kv_v1_7axis_current_status.md`

## Existing container snapshot

- Container: `hgfinance-vllm-runtime-20260822`
- Image: `vllm/vllm-openai:v0.27.1`
- Current served base model: `Qwen2.5-14B-Instruct-AWQ`
- Current adapter: `hgfinance-awq-arithmetic-2epoch=/tmp/adapter`
- Current serving profile: `max_model_len=8192`, `gpu_memory_utilization=0.85`, `kv_cache_dtype=fp8_e4m3`, prefix caching enabled, port 8000
- Current model bind: `/opt/hgfinance/models/Qwen2.5-14B-Instruct-AWQ:/models/Qwen2.5-14B-Instruct-AWQ:ro`
- Current adapter bind: `/tmp/hgfinance-awq-specialists-v1/arithmetic/adapter-2epoch:/tmp/adapter:ro`
- Host FP8 model exists at `/opt/hgfinance/models/Qwen2.5-14B-Instruct-FP8-dynamic`, but it is not mounted into this existing container.

## Rollback rule

If the new experiment is blocked or produces an invalid comparison, retain this baseline and use the existing controlled table. Do not replace FP8/AWQ values with partial or mixed-run values. Any future full comparison must create a new run directory and include all seven variants, frozen hashes, runtime evidence, endpoint evidence, raw outputs, scores, and provenance.
