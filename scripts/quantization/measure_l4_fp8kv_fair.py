#!/usr/bin/env python3
"""Measure reproducible model-only performance for the L4-fp8KV comparison.

This runner uses only the existing vLLM container and writes a new performance
artifact. It intentionally does not replace the earlier performance files,
whose original prompt body was not stored.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "benchmarks/quantization/results/aws_l4_fp8kv_v1"
TMP_ROOT = Path("/tmp/hgfinance-l4-fp8kv-v2")
CONTAINER = "hgfinance-vllm-runtime-20260822"
ENDPOINT = "http://127.0.0.1:8000"

PROFILE = {
    "max_model_len": 8192,
    "gpu_memory_utilization": 0.85,
    "kv_cache_dtype": "fp8_e4m3",
    "enable_prefix_caching": True,
    "temperature": 0,
    "stream": False,
    "warmup_requests": 2,
    "sample_batches": 5,
    "concurrency_levels": [1, 2, 4],
    "measurement_method": "sequential C1 and ThreadPoolExecutor batches for C2/C4",
}

MESSAGES = [
    {
        "role": "system",
        "content": (
            "You are a deterministic financial benchmark assistant. Read the supplied "
            "values, perform the calculation carefully, and return only the final "
            "numeric answer with no explanation."
        ),
    },
    {
        "role": "user",
        "content": (
            "A portfolio allocates 60% to an asset returning 8% and 40% to an asset "
            "returning -3%. Return the weighted portfolio return as a percentage."
        ),
    },
]
PROMPT_HASH = hashlib.sha256(
    json.dumps(MESSAGES, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
).hexdigest()

VARIANTS = {
    "FP8": {"mode": "fp8", "model": "Qwen2.5-14B-Instruct-FP8-dynamic"},
    "AWQ": {"mode": "awq", "model": "Qwen2.5-14B-Instruct-AWQ"},
    "AWQ+Finetune": {"mode": "awq-finetune", "model": "hgfinance-awq-finetune"},
}


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, capture_output=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def health(expected_model: str) -> dict:
    request = urllib.request.Request(f"{ENDPOINT}/v1/models", method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        body = json.loads(response.read().decode("utf-8"))
        models = [item["id"] for item in body.get("data", [])]
        if expected_model not in models:
            raise RuntimeError(f"expected model {expected_model!r} not found: {models!r}")
        return {"status": response.status, "models": models}


def wait_ready(expected_model: str, timeout_s: int = 360) -> tuple[dict, float]:
    started = time.monotonic()
    last_error = "endpoint not ready"
    while time.monotonic() - started < timeout_s:
        try:
            return health(expected_model), time.monotonic() - started
        except (OSError, ValueError, KeyError, RuntimeError, urllib.error.URLError) as exc:
            last_error = str(exc)
            time.sleep(5)
    raise RuntimeError(f"readiness timeout: {last_error}")


def request_once(model: str) -> dict:
    payload = {
        "model": model,
        "messages": MESSAGES,
        "temperature": 0,
        "max_tokens": 16,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{ENDPOINT}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
        elapsed = time.perf_counter() - started
        usage = result.get("usage", {})
        return {
            "ok": True,
            "status": 200,
            "latency_s": elapsed,
            "completion_tokens": int(usage.get("completion_tokens", 0)),
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "response": result.get("choices", [{}])[0].get("message", {}).get("content", ""),
        }
    except (OSError, ValueError, KeyError, IndexError, urllib.error.HTTPError) as exc:
        return {"ok": False, "status": getattr(exc, "code", None), "error": str(exc)}


def percentile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def measure(model: str) -> dict:
    warmup = [request_once(model) for _ in range(PROFILE["warmup_requests"])]
    if not all(item["ok"] for item in warmup):
        raise RuntimeError(f"warmup failed for {model}: {warmup!r}")

    measurements = {}
    for concurrency in PROFILE["concurrency_levels"]:
        responses: list[dict] = []
        batch_elapsed: list[float] = []
        for _ in range(PROFILE["sample_batches"]):
            started = time.perf_counter()
            if concurrency == 1:
                batch = [request_once(model)]
            else:
                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    batch = list(executor.map(lambda _: request_once(model), range(concurrency)))
            batch_elapsed.append(time.perf_counter() - started)
            responses.extend(batch)
        if not all(item["ok"] for item in responses):
            raise RuntimeError(f"measurement failed for {model}, C{concurrency}: {responses!r}")
        latencies = [item["latency_s"] for item in responses]
        completion_tokens = sum(item["completion_tokens"] for item in responses)
        duration_s = sum(batch_elapsed)
        measurements[f"C{concurrency}"] = {
            "requests": len(responses),
            "batches": len(batch_elapsed),
            "duration_s": duration_s,
            "latency_p50_s": percentile(latencies, 0.50),
            "latency_p95_s": percentile(latencies, 0.95),
            "throughput_tok_s": completion_tokens / duration_s,
            "completion_tokens": completion_tokens,
            "batch_elapsed_s": batch_elapsed,
            "responses": [
                {
                    "latency_s": item["latency_s"],
                    "prompt_tokens": item["prompt_tokens"],
                    "completion_tokens": item["completion_tokens"],
                }
                for item in responses
            ],
        }
    return {"warmup": warmup, "results": measurements}


def runtime_logs(since: str) -> str:
    result = run(["docker", "logs", "--since", since, CONTAINER], check=False)
    return result.stdout + result.stderr


def parse_log_metrics(logs: str) -> dict:
    load = re.findall(r"Model loading took ([0-9.]+) GiB and ([0-9.]+) seconds", logs)
    kv = re.findall(r"Available KV cache memory: ([0-9.]+) GiB", logs)
    kv_tokens = re.findall(r"GPU KV cache size: ([0-9,]+) tokens", logs)
    free = re.findall(r"Free memory on device \([^/]+/[^)]+ GiB\)", logs)
    result: dict = {}
    if load:
        result["model_load_memory_gib"] = float(load[-1][0])
        result["model_load_s"] = float(load[-1][1])
    if kv:
        result["kv_cache_gib"] = float(kv[-1])
    if kv_tokens:
        result["kv_cache_tokens"] = int(kv_tokens[-1].replace(",", ""))
    if free:
        result["startup_memory_log"] = free[-1]
    return result


def gpu_snapshot() -> dict:
    result = run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.used,memory.free",
            "--format=csv,noheader,nounits",
        ]
    )
    name, total, used, free = [part.strip() for part in result.stdout.strip().split(",")]
    return {
        "name": name,
        "memory_total_mib": int(total),
        "memory_used_mib": int(used),
        "memory_free_mib": int(free),
    }


def set_mode(mode: str) -> None:
    code = (
        "from pathlib import Path; "
        f"Path('/tmp/hgfinance_serve_variant').write_text({mode!r})"
    )
    run(["docker", "exec", CONTAINER, "python3", "-c", code])


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    prompt_artifact = {
        "schema_version": "l4-performance-prompt.v1",
        "prompt_sha256": PROMPT_HASH,
        "messages": MESSAGES,
        "request_parameters": {"temperature": 0, "max_tokens": 16, "stream": False},
        "scope": "FP8, AWQ, and AWQ+Finetune model-only performance",
    }
    write_json(ROOT / "performance_prompt_fair_v2.json", prompt_artifact)
    write_json(TMP_ROOT / "performance_prompt_fair_v2.json", prompt_artifact)

    results = {}
    try:
        for variant, config in VARIANTS.items():
            set_mode(config["mode"])
            restart_started = utc_now()
            run(["docker", "restart", CONTAINER], check=True)
            endpoint, ready_s = wait_ready(config["model"])
            performance = measure(config["model"])
            logs = runtime_logs(restart_started.isoformat())
            log_metrics = parse_log_metrics(logs)
            gpu = gpu_snapshot()
            artifact = {
                "schema_version": "performance-profile-v2",
                "profile": "L4-fp8KV-v1",
                "variant": variant,
                "model": config["model"],
                "endpoint": f"{ENDPOINT}/v1/chat/completions",
                "container": CONTAINER,
                "runtime": PROFILE,
                "prompt_sha256": PROMPT_HASH,
                "endpoint_health": endpoint,
                "startup_endpoint_s": ready_s,
                "gpu_after_load": gpu,
                "log_metrics": log_metrics,
                **performance,
            }
            repo_path = ROOT / variant / "performance_fair_v2.json"
            tmp_path = TMP_ROOT / variant / "performance_fair_v2.json"
            write_json(repo_path, artifact)
            write_json(tmp_path, artifact)
            results[variant] = {
                "path": str(repo_path),
                "startup_endpoint_s": ready_s,
                "gpu": gpu,
                "log_metrics": log_metrics,
                "C1": artifact["results"]["C1"],
                "C2": artifact["results"]["C2"],
                "C4": artifact["results"]["C4"],
            }
            print(json.dumps({"variant": variant, **results[variant]}, ensure_ascii=False), flush=True)
    finally:
        # Leave the existing container serving the validated Finetune endpoint.
        set_mode("awq-finetune")
        run(["docker", "restart", CONTAINER], check=False)

    write_json(TMP_ROOT / "summary.json", {"prompt_sha256": PROMPT_HASH, "results": results})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
