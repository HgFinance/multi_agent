# Worker Model Matrix

> Compatibility index. This path is retained because repository documents and department profiles link to it. It does not own current worker counts, role definitions, serving defaults, or runtime contracts.

## Canonical sources

| Concern | Canonical source |
|---|---|
| Current organization, worker overview, and serving summary | [CURRENT_PROJECT_ARCHITECTURE.md](../CURRENT_PROJECT_ARCHITECTURE.md) |
| Runtime, gateway, adapter, retry, and environment contracts | [FINAL_RUNTIME_ARCHITECTURE.md](FINAL_RUNTIME_ARCHITECTURE.md) |
| Worker IDs, triggers, tools, authority, and deterministic boundaries | [WORKER_ROLE_BOUNDARIES.md](WORKER_ROLE_BOUNDARIES.md) |
| Executable registry and profile data | departments/*/hermes/config.yaml and employee worker registries |
| Readiness and verification status | [PROJECT_IMPLEMENTATION_STATUS.md](../PROJECT_IMPLEMENTATION_STATUS.md) |
| Benchmark protocol and held-out artifacts | benchmarks/quantization/ |

## Current tracked serving compatibility

| Base model | Served name | Context | GPU utilization | KV cache | LoRA capacity |
|---|---|---:|---:|---|---|
| Qwen2.5-14B-Instruct-AWQ | qwen2.5-14b-instruct-awq | 8192 | 0.85 | fp8 | enabled; max loras 4, rank 32, CPU loras 8 |

This is tracked configuration, not proof that an AWS process is currently healthy. FP8 and older 16K/0.90 values are historical benchmark or rollback references only.

## Qwen AWQ v1 operational profile

The production employee-Worker default is `qwen-awq-v1`:

| Item | Fixed value |
|---|---|
| Base model | `Qwen2.5-14B-Instruct-AWQ` |
| OpenAI-compatible base alias | `qwen2.5-14b-instruct-awq` |
| Compatibility alias | `Qwen2.5-14B-Instruct-AWQ` |
| vLLM image | `vllm/vllm-openai@sha256:0a51ea5b4ae2dc5d81890e5173f54203d2a3ae0cfffe51b8fd2afd4391bfd967` |
| Runtime entrypoint | `scripts/model_plane/vllm_runtime.sh` |
| Worker gateway | `WORKER_MODEL_BASE_URL=http://vllm:8000/v1` |
| Arithmetic adapter | `hgfinance-awq-arithmetic-2epoch`, available for explicit route only |
| Quality status | Serving applied; FinanceBench quality gate remains `HOLD` |

All ten LLM Worker IDs are present in
`departments/01-research/config/worker_model_registry.json`. The registry
keeps them on the base model unless an adapter route is explicitly enabled;
this prevents a benchmark-specialist LoRA from silently changing risk, QA,
CEO, or workforce behavior. Deterministic runners and embedding-only APIs do
not receive Worker model credentials.

Team rule: do not use `docker run` to start a vLLM server or raw
`docker compose ... up ... vllm`. The fetch/quantization helpers may use an
isolated `docker run --rm --entrypoint` for offline artifact preparation; that
is not a serving container.
`vllm_runtime.sh check` is the acceptance check and rejects manual containers,
duplicate model containers, image drift, missing `hedgefund_default`/`vllm`
network wiring, and non-loopback host exposure.

## Compatibility note

Follow the canonical links above for current facts. Do not use this file to count legacy personality aliases or retired workers. A department-head Hermes profile, an employee Worker, and a deterministic runner are separate execution layers.
