#!/usr/bin/env python3
"""Bounded runtime stress and user-query E2E evidence runner.

The runner is intentionally standard-library only.  It measures the existing
HTTP boundaries and never creates PAPER orders unless an operator explicitly
passes ``--allow-workflow`` together with an E2E query.  The default matrix is
read-only readiness traffic, so it is safe to run against a local stack.

Examples:

    python scripts/stress_test.py --scenario all --requests 32 --concurrency 32
    python scripts/stress_test.py --scenario ceo_readonly_e2e \
      --e2e-query '현재 시스템 상태를 요약해줘' --allow-workflow
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from uuid import uuid4


@dataclass(frozen=True)
class Scenario:
    name: str
    path: str
    default_base_url: str | None = None
    method: str = "GET"
    body: dict[str, Any] | None = None
    e2e: bool = False
    default_slo_p95_ms: float = 500.0


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("ceo_readonly_e2e", "/ui/ceo/ask", method="POST", e2e=True, default_slo_p95_ms=120_000),
    Scenario("research_health", "/health/ready", default_base_url="http://127.0.0.1:8035"),
    Scenario("market_deep_readiness", "/ready", default_base_url="http://127.0.0.1:8036"),
    Scenario("quant_health", "/health", default_base_url="http://127.0.0.1:8037"),
    Scenario(
        "risk_observability",
        "/risk/v1/observability/runtime",
        default_base_url="http://127.0.0.1:8041",
    ),
    Scenario("audit_readiness", "/health/ready", default_base_url="http://127.0.0.1:8042"),
    Scenario("trading_readiness", "/health/ready", default_base_url="http://127.0.0.1:8045"),
    Scenario("accounting_readiness", "/health/ready", default_base_url="http://127.0.0.1:8046"),
    Scenario("governance_readiness", "/health/ready", default_base_url="http://127.0.0.1:8043"),
    Scenario("workforce_readiness", "/health/ready", default_base_url="http://127.0.0.1:8044"),
)

SCENARIO_BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _json_body(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> tuple[int, float, dict[str, Any], str | None]:
    encoded = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = Request(url, data=encoded, headers=request_headers, method=method)
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (time.perf_counter() - started) * 1000, _json_body(raw), None
    except HTTPError as exc:
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        return exc.code, (time.perf_counter() - started) * 1000, _json_body(raw), f"http_{exc.code}"
    except (OSError, URLError, TimeoutError) as exc:
        return 0, (time.perf_counter() - started) * 1000, {}, type(exc).__name__


def _parse_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return None


def _task_id(payload: dict[str, Any]) -> str | None:
    for key in ("task_id", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    nested = payload.get("ceo")
    if isinstance(nested, dict):
        return _task_id(nested)
    return None


def _run_e2e(
    scenario: Scenario,
    *,
    base_url: str,
    query: str,
    headers: dict[str, str],
    timeout: float,
    poll_interval: float,
    workflow_timeout: float,
    allow_workflow: bool,
) -> dict[str, Any]:
    if not allow_workflow:
        return {"status": "SKIPPED", "error": "--allow-workflow is required for E2E task creation"}
    request_id = f"stress-e2e-{uuid4().hex}"
    started = time.perf_counter()
    status, accept_ms, payload, error = _request(
        urljoin(base_url.rstrip("/") + "/", scenario.path.lstrip("/")),
        method="POST",
        body={"query": query, "request_id": request_id},
        headers=headers,
        timeout=timeout,
    )
    task_id = _task_id(payload)
    if status not in {200, 202} or not task_id:
        return {
            "status": "ERROR",
            "http_status": status,
            "accept_ms": round(accept_ms, 3),
            "e2e_ms": round((time.perf_counter() - started) * 1000, 3),
            "error": error or "task_id_missing",
        }

    first_poll_ms: float | None = None
    remote_created: float | None = None
    remote_completed: float | None = None
    terminal_status = "processing"
    deadline = time.monotonic() + workflow_timeout
    while time.monotonic() < deadline:
        status_code, _poll_ms, task, poll_error = _request(
            urljoin(base_url.rstrip("/") + "/", f"ui/ceo/tasks/{task_id}"),
            headers=headers,
            timeout=timeout,
        )
        if first_poll_ms is None:
            first_poll_ms = (time.perf_counter() - started) * 1000
        if status_code not in {200, 202}:
            return {
                "status": "ERROR",
                "task_id": task_id,
                "http_status": status_code,
                "accept_ms": round(accept_ms, 3),
                "queue_wait_ms": round(max(0.0, first_poll_ms - accept_ms), 3),
                "e2e_ms": round((time.perf_counter() - started) * 1000, 3),
                "error": poll_error or "status_poll_failed",
            }
        terminal_status = str(task.get("status") or "processing").casefold()
        remote_created = remote_created or _parse_epoch(task.get("created_at"))
        remote_completed = _parse_epoch(task.get("completed_at")) or remote_completed
        if terminal_status in {"completed", "failed", "blocked", "archived"}:
            break
        time.sleep(max(0.05, poll_interval))

    final_result_ms: float | None = None
    final_result_status: int | None = None
    final_result_error: str | None = None
    if terminal_status in {"completed", "failed", "blocked", "archived"}:
        final_result_status, final_result_ms, final_payload, final_result_error = _request(
            urljoin(base_url.rstrip("/") + "/", f"ui/ceo/tasks/{task_id}/result"),
            headers=headers,
            timeout=timeout,
        )
        if final_result_status not in {200, 202}:
            terminal_status = "result_unavailable"

    e2e_ms = (time.perf_counter() - started) * 1000
    result_status = "PASS" if terminal_status in {"completed", "blocked"} else "TIMEOUT"
    server_workflow_ms = None
    if remote_created is not None and remote_completed is not None:
        server_workflow_ms = max(0.0, (remote_completed - remote_created) * 1000)
    return {
        "status": result_status,
        "task_id": task_id,
        "terminal_status": terminal_status,
        "accept_ms": round(accept_ms, 3),
        "queue_wait_ms": round(max(0.0, (first_poll_ms or e2e_ms) - accept_ms), 3),
        "server_workflow_ms": round(server_workflow_ms, 3) if server_workflow_ms is not None else None,
        "final_result_status": final_result_status,
        "final_result_ms": round(final_result_ms, 3) if final_result_ms is not None else None,
        "e2e_ms": round(e2e_ms, 3),
        "error": final_result_error,
    }


def _run_one(
    scenario: Scenario,
    *,
    base_url: str,
    service_urls: dict[str, str],
    headers: dict[str, str],
    timeout: float,
    e2e_query: str | None,
    poll_interval: float,
    workflow_timeout: float,
    allow_workflow: bool,
) -> dict[str, Any]:
    if scenario.e2e:
        if not e2e_query:
            return {"status": "SKIPPED", "error": "--e2e-query is required"}
        return _run_e2e(
            scenario,
            base_url=base_url,
            query=e2e_query,
            headers=headers,
            timeout=timeout,
            poll_interval=poll_interval,
            workflow_timeout=workflow_timeout,
            allow_workflow=allow_workflow,
        )
    url = _scenario_url(scenario, base_url=base_url, service_urls=service_urls)
    status, elapsed_ms, _payload, error = _request(url, headers=headers, timeout=timeout)
    return {"status": "PASS" if 200 <= status < 300 else "ERROR", "http_status": status, "latency_ms": round(elapsed_ms, 3), "error": error}


def _headers(raw_headers: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in raw_headers:
        name, separator, value = raw.partition(":")
        if not separator or not name.strip():
            raise ValueError("headers must use Name: value format")
        result[name.strip()] = value.strip()
    return result


def _load_scenarios(name: str) -> list[Scenario]:
    if name == "all":
        return list(SCENARIOS)
    if name == "read_only":
        return [scenario for scenario in SCENARIOS if not scenario.e2e]
    scenario = SCENARIO_BY_NAME.get(name)
    if scenario is not None:
        return [scenario]
    raise ValueError(f"unknown scenario: {name}")


def _validate_base_url(value: Any, *, label: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise ValueError(f"{label} must be an http(s) URL")
    return normalized


def _service_urls(raw_json: str, raw_entries: list[str]) -> dict[str, str]:
    """Parse service targets once for both local CLI and GitHub Actions."""

    result: dict[str, str] = {}
    if raw_json.strip():
        try:
            decoded = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError("--service-urls-json must contain a JSON object") from exc
        if not isinstance(decoded, dict):
            raise ValueError("--service-urls-json must contain a JSON object")
        for name, value in decoded.items():
            scenario = SCENARIO_BY_NAME.get(str(name))
            if scenario is None or scenario.e2e:
                raise ValueError(f"service URL override is not a read-only scenario: {name}")
            result[scenario.name] = _validate_base_url(value, label=f"service URL for {name}")
    for raw in raw_entries:
        name, separator, value = raw.partition("=")
        scenario = SCENARIO_BY_NAME.get(name)
        if not separator or scenario is None or scenario.e2e:
            raise ValueError("--service-url must use read_only_scenario=http(s)://host")
        result[name] = _validate_base_url(value, label=f"service URL for {name}")
    return result


def _require_service_urls(scenarios: list[Scenario], service_urls: dict[str, str]) -> None:
    missing = [
        scenario.name
        for scenario in scenarios
        if not scenario.e2e and scenario.name not in service_urls
    ]
    if missing:
        raise ValueError(
            "missing service URL overrides for GitHub Actions: " + ", ".join(missing)
        )


def _scenario_url(
    scenario: Scenario,
    *,
    base_url: str,
    service_urls: dict[str, str],
) -> str:
    target_base_url = service_urls.get(
        scenario.name,
        base_url if scenario.e2e else scenario.default_base_url,
    )
    if not target_base_url:
        raise ValueError(f"no base URL configured for scenario: {scenario.name}")
    return urljoin(
        _validate_base_url(target_base_url, label=f"base URL for {scenario.name}") + "/",
        scenario.path.lstrip("/"),
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    scenarios = _load_scenarios(args.scenario)
    base_url = _validate_base_url(args.base_url, label="--base-url")
    service_urls = _service_urls(args.service_urls_json, args.service_url)
    if args.require_service_urls:
        _require_service_urls(scenarios, service_urls)
    headers = _headers(args.header)
    results: list[dict[str, Any]] = []
    exit_failure = False
    for scenario in scenarios:
        if scenario.e2e and not args.e2e_query:
            results.append({"scenario": scenario.name, "status": "SKIPPED", "reason": "e2e_query_missing"})
            continue
        started = time.perf_counter()
        attempts = max(1, args.requests)
        if args.duration_seconds > 0:
            attempts = 0

        def invoke(_index: int) -> dict[str, Any]:
            return _run_one(
                scenario,
                base_url=base_url,
                service_urls=service_urls,
                headers=headers,
                timeout=args.timeout,
                e2e_query=args.e2e_query,
                poll_interval=args.poll_interval,
                workflow_timeout=args.workflow_timeout,
                allow_workflow=args.allow_workflow,
            )

        rows: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            if args.duration_seconds > 0:
                futures = []
                while time.perf_counter() - started < args.duration_seconds:
                    futures.append(executor.submit(invoke, len(futures)))
                    if len(futures) >= args.max_requests:
                        break
                rows = [future.result() for future in futures]
            else:
                rows = list(executor.map(invoke, range(attempts)))
        elapsed = max(0.001, time.perf_counter() - started)
        latency_values = [
            float(row.get("latency_ms"))
            for row in rows
            if isinstance(row.get("latency_ms"), (int, float))
        ]
        if scenario.e2e:
            latency_values = [
                float(row.get("e2e_ms"))
                for row in rows
                if isinstance(row.get("e2e_ms"), (int, float))
            ]
        completed = sum(row.get("status") == "PASS" for row in rows)
        errors = sum(row.get("status") in {"ERROR", "TIMEOUT"} for row in rows)
        skipped = sum(row.get("status") == "SKIPPED" for row in rows)
        p95 = percentile(latency_values, 0.95)
        p95_limit = args.slo_p95_ms if args.slo_p95_ms is not None else scenario.default_slo_p95_ms
        error_rate = errors / len(rows) if rows else 0.0
        passed = not errors and (p95 is None or p95 <= p95_limit)
        if errors or (p95 is not None and p95 > p95_limit):
            exit_failure = True
        results.append(
            {
                "scenario": scenario.name,
                "workload": "user_query_to_result" if scenario.e2e else "read_only_http_probe",
                "requests": len(rows),
                "concurrency": args.concurrency,
                "duration_seconds": round(elapsed, 3),
                "sla_p95_ms": p95_limit,
                "p50_ms": round(percentile(latency_values, 0.50), 3) if percentile(latency_values, 0.50) is not None else None,
                "p95_ms": round(p95, 3) if p95 is not None else None,
                "p99_ms": round(percentile(latency_values, 0.99), 3) if percentile(latency_values, 0.99) is not None else None,
                "throughput_per_second": round(len(rows) / elapsed, 3) if rows else 0.0,
                "error_rate": round(error_rate, 6),
                "completed": completed,
                "errors": errors,
                "skipped": skipped,
                "recovery_result": "NOT_INJECTED",
                "status": "PASS" if passed else "FAIL",
                "samples": rows if args.include_samples else None,
            }
        )
    return {
        "schema_version": "hgfinance.stress-evidence.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "runner": "scripts/stress_test.py",
        "stress_failure": exit_failure,
        "recovery_note": "No fault was injected; recovery_result remains NOT_INJECTED.",
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        default="all",
        choices=[scenario.name for scenario in SCENARIOS] + ["all", "read_only"],
    )
    parser.add_argument("--base-url", default=os.getenv("HGFINANCE_STRESS_BASE_URL", "http://127.0.0.1:8001"))
    parser.add_argument(
        "--service-urls-json",
        default=os.getenv("HGFINANCE_STRESS_SERVICE_URLS_JSON", "{}"),
        help="JSON map of read-only scenario names to reachable service base URLs",
    )
    parser.add_argument(
        "--service-url",
        action="append",
        default=[],
        help="override one read-only service URL as scenario=http(s)://host",
    )
    parser.add_argument(
        "--require-service-urls",
        action="store_true",
        help="fail unless every selected read-only scenario has an explicit service URL",
    )
    parser.add_argument("--requests", type=int, default=32)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--duration-seconds", type=float, default=0.0)
    parser.add_argument("--max-requests", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--workflow-timeout", type=float, default=1200.0)
    parser.add_argument("--e2e-query")
    parser.add_argument("--allow-workflow", action="store_true")
    parser.add_argument("--slo-p95-ms", type=float)
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-samples", action="store_true")
    args = parser.parse_args(argv)
    if args.requests < 1 or args.concurrency < 1 or args.max_requests < 1:
        parser.error("requests, concurrency, and max-requests must be positive")
    try:
        report = run(args)
    except ValueError as exc:
        parser.error(str(exc))
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 1 if report["stress_failure"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
