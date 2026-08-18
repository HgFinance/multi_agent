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

## Compatibility note

Follow the canonical links above for current facts. Do not use this file to count legacy personality aliases or retired workers. A department-head Hermes profile, an employee Worker, and a deterministic runner are separate execution layers.
