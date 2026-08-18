# HgFinance Internal-50 v1

Dataset:
- benchmarks/quantization/internal50_v1.json
- SHA256: 368d1b0be88c2b13864a8e9cd3fd269aa781f877392a7cb563840fd99583dfef

Status:
- Frozen before FP8/AWQ Internal-50 inference.
- Must not be modified after model results are observed.
- Must remain excluded from future LoRA train/dev data.

Generation:
- OpenAI-compatible /v1/chat/completions
- temperature = 0
- max_tokens = 384
- stream = false
- sequential execution
- identical runner for FP8 and AWQ

Scoring:
- numeric: deterministic numerical comparison, relative tolerance 1e-4
- exact: normalized exact answer
- json_schema/json_semantic: expected JSON must be a recursive subset of returned JSON
- contains_all: all expected values must be present
- request errors are failures

Promotion comparison:
1. Overall Internal-50 accuracy / mean score
2. Critical failure count
3. Category-level regressions
4. Request error count
5. Latency

AWQ promotion gate:
- Internal mean quality relative degradation <= 3% vs FP8
- No new critical failures attributable to AWQ
- No reliability regression
- External benchmark considered separately
- Serving performance considered separately

Never tune scorer or dataset after seeing AWQ results.
