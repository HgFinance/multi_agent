#!/usr/bin/env python3
"""Deploy application processes only after candidates pass their healthchecks.

This is the single deployment entry point for supported synchronous services
and durable workers. It starts an unpublished canary on the existing Compose
network, waits for the exact service healthcheck, and replaces the live
container only after that check succeeds. The final ``compose up --wait`` is
also bounded, so automation cannot report success while the replacement is
unready.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_SERVICES = (
    "trading-api",
    "trading-directive-worker",
    "accounting-ledger-consumer",
    "portfolio-bff",
    "ai-office",
)
DEFAULT_FILES = ("docker-compose.yml", "deploy/aws/docker-compose.paper-order.yml")
_PORTFOLIO_BFF_ORDER_LANGUAGE_CANARY = """
from orchestration.ceo_request_classifier import classify_ceo_request
from orchestration.dynamic_universe_orders import parse_dynamic_universe_order

query = (
    "현재 KRX 시가총액 상위 10개 종목을 각각 최대 300만원씩 "
    "PAPER 시장가로 매수해줘."
)
plan = parse_dynamic_universe_order(query)
assert plan is not None
assert plan.market_scope == "KRX"
assert plan.ranking_kind == "market_cap"
assert plan.top_n == 10
assert plan.notional_krw == 3_000_000
route = classify_ceo_request(query)
assert route.lane == "immediate_order"
assert route.reason_codes == ("order_grammar.dynamic_universe",)
""".strip()
_TRADING_WORKER_RECONCILIATION_CANARY = """
import inspect

from broker.ls_paper_broker import LSPaperBroker
from directives.service import UserDirectiveService

history_source = inspect.getsource(LSPaperBroker._order_history_rows)
fallback_source = inspect.getsource(LSPaperBroker._today_execution_status_rows)
sync_source = inspect.getsource(UserDirectiveService._sync_external_leg)
assert "LS_PAPER_QUERY_REJECTED" in history_source
assert "_today_execution_status_rows" in history_source
assert '"t0425"' in fallback_source
assert "leg.state is DirectiveLegState.UNKNOWN" in sync_source
assert "acknowledge_broker_leg" in sync_source
""".strip()
_ACCOUNTING_FILL_ORDER_CANARY = """
import inspect
import sys

sys.path.insert(0, "/app/departments/05-accounting-portfolio/ledger")
from fill_consumer import pending_fill_events

source = " ".join(inspect.getsource(pending_fill_events).split()).lower()
assert "order by delivered.event_time, delivered.outbox_id" in source
assert "order by delivered.outbox_id" not in source
""".strip()
_AI_OFFICE_ACTIVE_ORDER_CANARY = """
const fs = require("node:fs");
const path = require("node:path");

function scripts(root) {
  return fs.readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const target = path.join(root, entry.name);
    if (entry.isDirectory()) return scripts(target);
    return entry.name.endsWith(".js") ? [fs.readFileSync(target, "utf8")] : [];
  });
}

const bundle = scripts("/app/ai-office/dist").join("");
for (const marker of ["수정 저장", "조건주문을 철회했습니다", "rule-edit:"]) {
  if (!bundle.includes(marker)) throw new Error(`missing active-order UI marker: ${marker}`);
}
""".strip()


def _run(command: list[str], *, capture: bool = False) -> str:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return (completed.stdout or "").strip()


def _compose(files: list[str]) -> list[str]:
    command = ["docker", "compose"]
    for value in files:
        command.extend(("-f", value))
    return command


def _wait_healthy(name: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = _run(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}",
                name,
            ],
            capture=True,
        )
        if status == "healthy":
            return
        if status == "unhealthy":
            raise RuntimeError(f"candidate {name} failed readiness")
        time.sleep(1)
    raise TimeoutError(f"candidate {name} did not become healthy in {timeout_seconds}s")


def _verify_service_semantics(service: str, container_name: str) -> None:
    """Refuse a healthy process that still carries a stale business contract."""

    if service == "portfolio-bff":
        _run(
            [
                "docker",
                "exec",
                container_name,
                "python",
                "-c",
                _PORTFOLIO_BFF_ORDER_LANGUAGE_CANARY,
            ]
        )
    elif service == "trading-directive-worker":
        _run(
            [
                "docker",
                "exec",
                container_name,
                "python",
                "-c",
                _TRADING_WORKER_RECONCILIATION_CANARY,
            ]
        )
    elif service == "accounting-ledger-consumer":
        _run(
            [
                "docker",
                "exec",
                container_name,
                "python",
                "-c",
                _ACCOUNTING_FILL_ORDER_CANARY,
            ]
        )
    elif service == "ai-office":
        _run(
            [
                "docker",
                "exec",
                container_name,
                "node",
                "-e",
                _AI_OFFICE_ACTIVE_ORDER_CANARY,
            ]
        )


def deploy(service: str, files: list[str], timeout_seconds: int) -> None:
    if service not in ALLOWED_SERVICES:
        raise ValueError(f"unsupported service: {service}")
    compose = _compose(files)
    _run([*compose, "config", "--quiet"])
    # Compose readiness proves only that the replacement process is alive.  A
    # release must also refuse the known cross-department contract regressions
    # before it builds or replaces a live container.  Keep this mandatory and
    # unskippable: a green healthcheck cannot override a failed contract.
    _run([sys.executable, "-m", "pytest", "-q", "tests/contracts"])
    _run([*compose, "build", service])

    canary = f"hgfinance-{service}-readiness-canary-{os.getpid()}"
    try:
        _run(
            [
                *compose,
                "run",
                "-d",
                "--no-deps",
                "--name",
                canary,
                service,
            ]
        )
        _wait_healthy(canary, timeout_seconds)
        _verify_service_semantics(service, canary)
    finally:
        # The name is locally constructed and exact; no glob or broad target is
        # accepted.  A failed candidate never replaces the live container.
        subprocess.run(
            ["docker", "rm", "-f", canary],
            cwd=ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    _run(
        [
            *compose,
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            str(timeout_seconds),
            service,
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=ALLOWED_SERVICES)
    parser.add_argument("--file", action="append", dest="files")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--print-command", action="store_true")
    args = parser.parse_args()
    files = args.files or list(DEFAULT_FILES)
    if args.timeout < 10 or args.timeout > 900:
        parser.error("--timeout must be between 10 and 900 seconds")
    if args.print_command:
        print(" ".join(_compose(files)))
        return 0
    deploy(args.service, files, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
