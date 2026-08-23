#!/usr/bin/env python3
"""QA terminal-contract boundary for dispatcher-spawned Hermes workers.

The Kanban dispatcher owns the worker subprocess, but the Hermes CLI can exit
successfully before calling a Kanban terminal tool (for example when the
configured provider has no credential).  That leaves a running claim behind
and the dispatcher correctly reports a protocol violation.  This wrapper is
used as the dispatcher's worker executable and turns that narrow, observable
condition into the existing typed ``kanban_block`` handoff for the QA profile.
Non-QA profiles are delegated to the unchanged Hermes executable.

Normal Hermes execution, provider selection, retry policy, and terminal tools
are otherwise untouched.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import json
from pathlib import Path
from typing import Sequence


REAL_HERMES = os.environ.get(
    "HGFINANCE_QA_REAL_HERMES",
    "/opt/hermes/.venv/bin/hermes",
)
QA_PROFILE = "qa-department"
BLOCK_KIND = "capability"
BLOCK_REASON = (
    "QA worker exited successfully without kanban_complete or kanban_block; "
    "the worker did not satisfy the terminal contract."
)
AUTH_BLOCK_REASON = (
    "QA worker cannot start: the configured provider has no stored "
    "credential for this profile."
)


def _task_context() -> tuple[str | None, int | None]:
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip() or None
    raw_run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
    try:
        run_id = int(raw_run_id) if raw_run_id else None
    except ValueError:
        run_id = None
    return task_id, run_id


def _read_live_run_state(
    db_path: str | os.PathLike[str],
    task_id: str,
    run_id: int,
) -> tuple[str | None, int | None, str | None]:
    """Read only the task/run state owned by this worker attempt."""

    db_uri = f"file:{Path(db_path).resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True, timeout=1.0)
    except (OSError, sqlite3.Error):
        return None, None, None
    try:
        task = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run = conn.execute(
            "SELECT outcome FROM task_runs WHERE id = ? AND task_id = ?",
            (run_id, task_id),
        ).fetchone()
        if task is None:
            return None, None, None
        current_run_id = (
            int(task[1]) if task[1] is not None else None
        )
        outcome = str(run[0]) if run and run[0] is not None else None
        return str(task[0]) if task[0] is not None else None, current_run_id, outcome
    except sqlite3.Error:
        return None, None, None
    finally:
        conn.close()


def _still_owned_and_unfinished(
    db_path: str | os.PathLike[str],
    task_id: str,
    run_id: int,
) -> bool:
    status, current_run_id, outcome = _read_live_run_state(
        db_path, task_id, run_id
    )
    return (
        status == "running"
        and current_run_id == run_id
        and outcome is None
    )


def _configured_provider(home: str | os.PathLike[str]) -> str | None:
    """Read only the profile's model.provider without importing Hermes."""

    try:
        lines = Path(home, "config.yaml").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    in_model = False
    for line in lines:
        if line.strip() == "model:":
            in_model = True
            continue
        if in_model and line and not line[0].isspace():
            break
        if in_model:
            match = re.match(r"^\s+provider:\s*([^\s#]+)", line)
            if match:
                return match.group(1).strip().strip('"\'')
    return None


def _provider_override(argv: Sequence[str]) -> str | None:
    for index, arg in enumerate(argv):
        if arg == "--provider" and index + 1 < len(argv):
            return str(argv[index + 1]).strip()
        if arg.startswith("--provider="):
            return arg.split("=", 1)[1].strip()
    return None


def _codex_credential_is_deterministically_missing(
    argv: Sequence[str],
) -> bool:
    """Recognize only the observed QA openai-codex empty-pool condition."""

    home = os.environ.get("HERMES_HOME", "").strip()
    if not home:
        return False
    provider = _provider_override(argv) or _configured_provider(home)
    if provider != "openai-codex":
        return False
    try:
        auth = json.loads(Path(home, "auth.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(auth, dict):
        return False
    providers = auth.get("providers")
    if isinstance(providers, dict) and providers.get(provider):
        # A non-empty provider state may be a valid credential representation
        # in a newer Hermes auth schema. Let Hermes decide rather than
        # preempting it based on an incomplete local parser.
        return False
    pool = auth.get("credential_pool")
    if not isinstance(pool, dict) or provider not in pool:
        return False
    credentials = pool.get(provider)
    return isinstance(credentials, list) and not credentials


def _block_owned_task(task_id: str, *, reason: str = BLOCK_REASON) -> bool:
    """Use Hermes' existing CLI handoff, preserving expected_run_id env."""

    env = os.environ.copy()
    # The wrapper is HERMES_BIN for the dispatcher.  Prevent the handoff
    # command from recursively invoking this wrapper.
    env["HERMES_BIN"] = REAL_HERMES
    command = [
        REAL_HERMES,
        "kanban",
        "block",
        "--kind",
        BLOCK_KIND,
        task_id,
        reason,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _run_real_worker(argv: Sequence[str]) -> int:
    """Run the unmodified Hermes worker and inherit its task log streams."""

    env = os.environ.copy()
    # The dispatcher points HERMES_BIN at this wrapper.  The delegated Hermes
    # process must see the real binary so any nested Hermes resolution cannot
    # recurse back into the wrapper.
    env["HERMES_BIN"] = REAL_HERMES
    completed = subprocess.run(
        [REAL_HERMES, *argv],
        check=False,
        env=env,
    )
    return int(completed.returncode)


def _is_qa_kanban_worker() -> bool:
    task_id, run_id = _task_context()
    return (
        task_id is not None
        and run_id is not None
        and os.environ.get("HERMES_PROFILE", "").strip() == QA_PROFILE
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # Ordinary QA CLI/gateway commands are byte-for-byte delegated to Hermes;
    # only dispatcher-owned QA workers get the terminal-contract guard.
    if not _is_qa_kanban_worker():
        env = os.environ.copy()
        env["HERMES_BIN"] = REAL_HERMES
        os.execvpe(REAL_HERMES, [REAL_HERMES, *args], env)
        raise AssertionError("os.execvpe returned unexpectedly")

    task_id, run_id = _task_context()
    assert task_id is not None and run_id is not None

    # The observed QA failure is deterministic and can be identified before
    # starting Hermes: this profile selects openai-codex and its stored
    # credential pool is empty. Do not alter provider auth or retry policy;
    # just use the existing terminal handoff once.
    if _codex_credential_is_deterministically_missing(args):
        if _block_owned_task(task_id, reason=AUTH_BLOCK_REASON):
            return 0
        return 78

    child_rc = _run_real_worker(args)

    # A legitimate complete/block handoff changes task status or ends the run.
    # Only mutate when this exact run still owns a running task, so a late
    # successor/recovery cannot be touched by the old worker process.
    db_path = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if (
        child_rc == 0
        and db_path
        and _still_owned_and_unfinished(db_path, task_id, run_id)
    ):
        if not _block_owned_task(task_id):
            # Do not claim success when the safety handoff itself failed.  The
            # dispatcher will retain its existing crash/recovery semantics.
            return 78

    return child_rc


if __name__ == "__main__":
    raise SystemExit(main())
