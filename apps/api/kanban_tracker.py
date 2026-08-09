#!/usr/bin/env python3
"""Fire-and-forget bridge from the portfolio pipeline to the real Hermes Kanban board.

Department-level granularity (not per-worker -- one real Task per department
per portfolio run, not one per LangGraph Worker, to avoid flooding the board).
Every call here is best-effort: it must never raise into `_record_event`, and
it must never block the caller's thread for long, since `_record_event` holds
`PortfolioRuntime._lock` while it runs. Each tracker call therefore spawns a
short-lived daemon thread and returns immediately.

`hermes gateway`/`daemon` is never started by this module. As long as no
dispatcher process is running, `hermes kanban create` only records board
metadata -- it does not trigger a real Agent run or cost. Starting a
dispatcher is an explicit, separate, human decision outside this module's
scope.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable

KANBAN_TRACKING_ENABLED = os.getenv("PORTFOLIO_KANBAN_TRACKING_ENABLED", "false").casefold() in {
    "1",
    "true",
    "yes",
    "on",
}
_HERMES_BIN = os.environ.get("HERMES_BIN", "hermes")
_TIMEOUT_SECONDS = float(os.environ.get("PORTFOLIO_KANBAN_CLI_TIMEOUT_SECONDS", "10"))


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [_HERMES_BIN, *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:  # noqa: BLE001 - best-effort tracking, never propagates.
        return None


def _ensure_task_id(*, department: str, idempotency_key: str, title: str) -> str | None:
    """Create the department Task, or return the existing id for the same key.

    `--idempotency-key` makes this safe to call more than once for the same
    (job, department) pair: the CLI returns the existing task id instead of
    creating a duplicate.
    """

    process = _run_cli(
        [
            "kanban",
            "create",
            title,
            "--assignee",
            department,
            "--initial-status",
            "running",
            "--idempotency-key",
            idempotency_key,
            "--json",
        ]
    )
    if process is None or process.returncode != 0:
        return None
    try:
        payload = json.loads(process.stdout)
    except (TypeError, ValueError):
        return None
    task_id = payload.get("id") if isinstance(payload, dict) else None
    return str(task_id) if task_id else None


def _spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True).start()


def track_department_started(
    job_id: str,
    department: str,
    title: str,
    *,
    on_task_id: Callable[[str], None] | None = None,
) -> None:
    if not KANBAN_TRACKING_ENABLED:
        return

    def _do() -> None:
        task_id = _ensure_task_id(
            department=department,
            idempotency_key=f"{job_id}:{department}",
            title=title,
        )
        if task_id and on_task_id is not None:
            on_task_id(task_id)

    _spawn(_do)


def track_department_completed(job_id: str, department: str, status: str, summary: str) -> None:
    if not KANBAN_TRACKING_ENABLED:
        return

    def _do() -> None:
        task_id = _ensure_task_id(
            department=department,
            idempotency_key=f"{job_id}:{department}",
            title=f"{department} — job {job_id}",
        )
        if task_id is None:
            return
        if status == "COMPLETED":
            _run_cli(["kanban", "complete", task_id, "--result", summary])
        else:
            _run_cli(["kanban", "block", task_id, summary or "안전 보류", "--kind", "needs_input"])

    _spawn(_do)


def track_department_blocked(job_id: str, department: str, reason: str) -> None:
    if not KANBAN_TRACKING_ENABLED:
        return

    def _do() -> None:
        task_id = _ensure_task_id(
            department=department,
            idempotency_key=f"{job_id}:{department}",
            title=f"{department} — job {job_id}",
        )
        if task_id is None:
            return
        _run_cli(["kanban", "block", task_id, reason or "실행 입력 미준비", "--kind", "needs_input"])

    _spawn(_do)
