#!/usr/bin/env python3
"""Run the CEO closed-loop supervisor beside the standalone Hermes daemon."""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestration.adapters.ceo_supervisor import (
    CeoSupervisorService,
    HermesKanbanClient,
    HermesKanbanCommandError,
)


WATCH_LINE = re.compile(
    r"^\[[^]]+\]\s+(?P<task_id>\S+)\s+(?P<kind>\S+)\s+\(@(?P<assignee>[^)]*)\)(?P<payload>.*)$"
)


def parse_watch_line(line: str) -> dict[str, object] | None:
    match = WATCH_LINE.match(line.strip())
    if match is None:
        return None
    payload_text = match.group("payload").strip()
    payload: object = {}
    if payload_text:
        try:
            payload = ast.literal_eval(payload_text)
        except (SyntaxError, ValueError):
            payload = {"raw": payload_text}
    event: dict[str, object] = {
        "task_id": match.group("task_id"),
        "kind": match.group("kind"),
        "assignee": match.group("assignee") or None,
    }
    if isinstance(payload, dict):
        event.update(payload)
    return event


def watch_events(
    *,
    executable: str,
    interval: float,
    environment: dict[str, str],
) -> Iterator[dict[str, object]]:
    process = subprocess.Popen(
        [
            executable,
            "kanban",
            "watch",
            "--kinds",
            "completed,blocked,gave_up,crashed,timed_out,spawn_failed,reclaimed",
            "--interval",
            str(interval),
        ],
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        env=environment,
    )
    if process.stdout is None:
        raise RuntimeError("hermes kanban watch did not provide stdout")
    try:
        for line in process.stdout:
            event = parse_watch_line(line)
            if event is not None:
                yield event
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--max-wakeups", type=int, default=8)
    args = parser.parse_args()
    environment = dict(os.environ)
    client = HermesKanbanClient(environment=environment)
    service = CeoSupervisorService(
        client,
        max_retries=args.max_retries,
        max_wakeups=args.max_wakeups,
    )
    for event in watch_events(
        executable=client.executable,
        interval=args.interval,
        environment=environment,
    ):
        try:
            decision = service.handle_terminal_event(event)
            if decision is not None:
                print(
                    f"ceo-supervisor action={decision.action.value} "
                    f"parent={decision.parent_task_id} reason={decision.reason}",
                    flush=True,
                )
        except (HermesKanbanCommandError, ValueError) as exc:
            # A malformed task or failed CLI call must be visible and must not
            # silently turn into a different assignee or an unbounded retry.
            print(f"ceo-supervisor error={type(exc).__name__}: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
