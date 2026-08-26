"""Supervise the existing Governance and Workforce event consumers.

The consumers intentionally remain separate Python processes.  Their API
modules use department-local import roots with overlapping module names, so
loading both into one interpreter would weaken the existing ownership boundary.
Redis streams, consumer groups, handlers, acknowledgements, and deduplication
remain owned by the original worker modules; this file only consolidates the
container lifecycle.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_HEALTH_PATH = "/tmp/hgfinance-control-event-worker-health.json"


@dataclass(frozen=True)
class ChildSpec:
    name: str
    script: str


CHILDREN = (
    ChildSpec(
        "governance-notification",
        "/app/departments/00-ceo-office/governance_events/worker.py",
    ),
    ChildSpec(
        "workforce-improvement",
        "/app/departments/07-agent-workforce/workforce_events/worker.py",
    ),
)


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def healthcheck(
    path: Path,
    *,
    now: float | None = None,
    heartbeat_max_age_seconds: float = 10.0,
) -> bool:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        current = time.time() if now is None else now
        if current - float(state["heartbeat"]) > heartbeat_max_age_seconds:
            return False
        children = dict(state["children"])
        if set(children) != {child.name for child in CHILDREN}:
            return False
        for child in children.values():
            pid = int(child["pid"])
            if pid <= 1 or child.get("status") != "running":
                return False
            os.kill(pid, 0)
        return True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def _stop_children(processes: dict[str, subprocess.Popen[Any]]) -> None:
    for process in processes.values():
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 15.0
    for process in processes.values():
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
    for process in processes.values():
        if process.poll() is None:
            process.wait(timeout=5.0)


def run(*, health_path: Path) -> int:
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    processes = {
        child.name: subprocess.Popen(
            [sys.executable, child.script],
            env=os.environ.copy(),
        )
        for child in CHILDREN
    }
    try:
        while not stopping:
            state = {
                "heartbeat": time.time(),
                "children": {
                    name: {
                        "pid": process.pid,
                        "status": "running" if process.poll() is None else "exited",
                        "exit_code": process.poll(),
                    }
                    for name, process in processes.items()
                },
            }
            _atomic_write(health_path, state)
            if any(process.poll() is not None for process in processes.values()):
                return 1
            time.sleep(1.0)
        return 0
    finally:
        _stop_children(processes)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument(
        "--health-path",
        default=os.getenv("CONTROL_EVENT_WORKER_HEALTH_PATH", DEFAULT_HEALTH_PATH),
    )
    args = parser.parse_args(argv)
    path = Path(args.health_path)
    if args.healthcheck:
        return 0 if healthcheck(path) else 1
    return run(health_path=path)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
