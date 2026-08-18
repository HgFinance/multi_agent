#!/usr/bin/env python3
"""Run the CEO closed-loop supervisor beside the standalone Hermes daemon."""

from __future__ import annotations

import argparse
import ast
import hashlib
import logging
import os
import re
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestration.adapters.ceo_supervisor import (
    CeoSupervisorService,
    HermesKanbanClient,
    HermesKanbanCommandError,
    SupervisorWorkflowError,
)
from orchestration.adapters.ceo_notion_projection import CeoNotionProjection
from orchestration.adapters.qa_audit_projection import QaAuditProjection
from orchestration.discord_delivery import DiscordFinalDelivery


WATCH_LINE = re.compile(
    r"^\[(?P<timestamp>[^]]+)\]\s+(?P<task_id>\S+)\s+"
    r"(?P<kind>\S+)\s+\(@(?P<assignee>[^)]*)\)(?P<payload>.*)$"
)


class WatchOutputError(RuntimeError):
    """The Hermes watch output no longer matches its supported contract."""


class WatchProcessError(RuntimeError):
    """The Hermes watch subprocess ended without an intentional shutdown."""


class GracefulShutdown(Exception):
    """Internal signal used to distinguish normal container shutdown."""


def parse_watch_line(line: str) -> dict[str, object] | None:
    stripped = line.strip()
    if not stripped or stripped == "Watching kanban events. Ctrl-C to stop." or stripped == "(stopped)":
        return None
    match = WATCH_LINE.match(stripped)
    if match is None:
        raise WatchOutputError(f"malformed hermes kanban watch line: {stripped[:240]!r}")
    payload_text = match.group("payload").strip()
    payload: object = {}
    if payload_text:
        try:
            payload = ast.literal_eval(payload_text)
        except (SyntaxError, ValueError) as exc:
            raise WatchOutputError("watch payload is not a Python-literal dict") from exc
        if not isinstance(payload, dict):
            raise WatchOutputError("watch payload is not a dict")
    event_id = hashlib.sha256(
        f"{match.group('timestamp')}|{match.group('task_id')}|{match.group('kind')}|{payload_text}".encode()
    ).hexdigest()[:24]
    event: dict[str, object] = {
        "task_id": match.group("task_id"),
        "kind": match.group("kind"),
        "assignee": match.group("assignee") or None,
        "event_id": event_id,
    }
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key not in {"task_id", "kind", "assignee", "event_id"}:
                event[key] = value
    return event


def watch_events(
    *,
    executable: str,
    interval: float,
    environment: dict[str, str],
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> Iterator[dict[str, object]]:
    try:
        process = popen_factory(
            [
                executable,
                "kanban",
                "watch",
                "--kinds",
                "claimed,spawned,completed,blocked,gave_up,crashed,timed_out,spawn_failed",
                "--interval",
                str(interval),
            ],
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WatchProcessError("could not start hermes kanban watch") from exc
    if process.stdout is None:
        raise WatchProcessError("hermes kanban watch did not provide stdout")
    saw_stopped = False
    try:
        for line in process.stdout:
            if line.strip() == "(stopped)":
                saw_stopped = True
                continue
            event = parse_watch_line(line)
            if event is not None:
                yield event
        returncode = process.wait()
        if returncode != 0:
            raise WatchProcessError(f"hermes kanban watch exited with code {returncode}")
        if not saw_stopped:
            raise WatchProcessError("hermes kanban watch reached unexpected EOF")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--max-wakeups", type=int, default=8)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    def shutdown_handler(signum: int, _frame: Any) -> None:
        raise GracefulShutdown(f"signal {signum}")

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    environment = dict(os.environ)
    client = HermesKanbanClient(environment=environment)
    service = CeoSupervisorService(
        client,
        max_retries=args.max_retries,
        max_wakeups=args.max_wakeups,
        synthesis_projection=CeoNotionProjection(kanban_client=client),
        qa_projection=QaAuditProjection(kanban_client=client),
        discord_delivery=DiscordFinalDelivery(environment=environment),
    )
    try:
        try:
            for decision in service.reconcile_existing_workflows():
                print(
                    f"ceo-supervisor reconcile action={decision.action.value} "
                    f"parent={decision.parent_task_id} reason={decision.reason}",
                    flush=True,
                )
        except (SupervisorWorkflowError, HermesKanbanCommandError) as exc:
            # Reconciliation is a recovery aid; a transient read/projection
            # failure must not prevent the normal watch loop from starting.
            print(f"ceo-supervisor reconcile-error={exc}", file=sys.stderr, flush=True)
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
            except SupervisorWorkflowError as exc:
                # A single workflow must not take down the event consumer.
                print(f"ceo-supervisor workflow-error={exc}", file=sys.stderr, flush=True)
    except GracefulShutdown as exc:
        print(f"ceo-supervisor normal-shutdown={exc}", flush=True)
        return 0
    except (WatchOutputError, WatchProcessError) as exc:
        print(f"ceo-supervisor fatal-watch-error={exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
