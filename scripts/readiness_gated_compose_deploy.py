#!/usr/bin/env python3
"""Deploy Trading/BFF only after the candidate passes its Compose healthcheck.

This is the single application deployment entry point for the two synchronous
request services.  It starts an unpublished canary on the existing Compose
network, waits for the exact service healthcheck, and replaces the live
container only after that check succeeds.  The final ``compose up --wait`` is
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
ALLOWED_SERVICES = ("trading-api", "portfolio-bff")
DEFAULT_FILES = ("docker-compose.yml", "deploy/aws/docker-compose.paper-order.yml")


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
