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
are otherwise untouched. Dispatcher-owned user-query planning, response
synthesis, and ``fast_advisory`` tasks receive task-scoped turn/reasoning
budgets; standard analysis and experiment tasks do not.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

REAL_HERMES = os.environ.get(
    "HGFINANCE_QA_REAL_HERMES",
    "/opt/hermes/.venv/bin/hermes",
)
QA_PROFILE = "qa-department"
RISK_PROFILE = "risk-management"
RISK_USER_PRIMARY_TOOLSETS = "kanban,risk-legal"
_TASK_ID_RE = re.compile(r"\bt_[A-Za-z0-9_-]+\b")
FAST_ADVISORY_MODE = "analysis_mode=fast_advisory"
DEFAULT_FAST_ADVISORY_MAX_TURNS = 12
MIN_FAST_ADVISORY_MAX_TURNS = 8
MAX_FAST_ADVISORY_MAX_TURNS = 64
DEFAULT_USER_RESPONSE_MAX_TURNS = 12
MIN_USER_RESPONSE_MAX_TURNS = 8
MAX_USER_RESPONSE_MAX_TURNS = 32
DEFAULT_USER_RESPONSE_REASONING = "medium"
_REASONING_LEVELS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
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


def _task_id_from_argv(argv: Sequence[str]) -> str | None:
    """Recover the Kanban task when Hermes did not export its task marker."""

    for value in reversed(tuple(str(item) for item in argv)):
        match = _TASK_ID_RE.fullmatch(value.strip())
        if match:
            return match.group(0)
    return None


def _latest_run_id(
    db_path: str | os.PathLike[str] | None,
    task_id: str | None,
) -> int | None:
    """Read the last attempt ID without relying on worker environment state."""

    if not db_path or not task_id:
        return None
    db_uri = f"file:{Path(db_path).resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True, timeout=1.0)
    except (OSError, sqlite3.Error):
        return None
    try:
        row = conn.execute(
            "SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except (TypeError, ValueError, sqlite3.Error):
        return None
    finally:
        conn.close()


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
        current_run_id = int(task[1]) if task[1] is not None else None
        outcome = str(run[0]) if run and run[0] is not None else None
        return str(task[0]) if task[0] is not None else None, current_run_id, outcome
    except sqlite3.Error:
        return None, None, None
    finally:
        conn.close()


def _task_body(
    db_path: str | os.PathLike[str] | None,
    task_id: str | None,
) -> str:
    """Read only the assigned task body without mutating the Kanban board.

    The body is used only for exact control markers. It is never logged or
    copied into observability metadata.
    """

    if not db_path or not task_id:
        return ""
    db_uri = f"file:{Path(db_path).resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True, timeout=1.0)
    except (OSError, sqlite3.Error):
        return ""
    try:
        row = conn.execute(
            "SELECT body FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    return row[0] if row and isinstance(row[0], str) else ""


def _task_is_fast_advisory(
    db_path: str | os.PathLike[str] | None,
    task_id: str | None,
) -> bool:
    """Return whether the task carries the exact fast-advisory marker."""

    return FAST_ADVISORY_MODE in _task_body(db_path, task_id)


def _fast_advisory_max_turns() -> int:
    """Return a bounded, operator-tunable fast-advisory turn budget."""

    raw = os.environ.get(
        "HGFINANCE_FAST_ADVISORY_MAX_TURNS",
        str(DEFAULT_FAST_ADVISORY_MAX_TURNS),
    ).strip()
    try:
        configured = int(raw)
    except ValueError:
        configured = DEFAULT_FAST_ADVISORY_MAX_TURNS
    return min(
        MAX_FAST_ADVISORY_MAX_TURNS,
        max(MIN_FAST_ADVISORY_MAX_TURNS, configured),
    )


def _user_response_max_turns() -> int:
    raw = os.environ.get(
        "HGFINANCE_USER_RESPONSE_MAX_TURNS",
        str(DEFAULT_USER_RESPONSE_MAX_TURNS),
    ).strip()
    try:
        configured = int(raw)
    except ValueError:
        configured = DEFAULT_USER_RESPONSE_MAX_TURNS
    return min(
        MAX_USER_RESPONSE_MAX_TURNS,
        max(MIN_USER_RESPONSE_MAX_TURNS, configured),
    )


def _user_response_reasoning() -> str:
    configured = (
        os.environ.get(
            "HGFINANCE_USER_RESPONSE_REASONING",
            DEFAULT_USER_RESPONSE_REASONING,
        )
        .strip()
        .casefold()
    )
    return (
        configured
        if configured in _REASONING_LEVELS
        else DEFAULT_USER_RESPONSE_REASONING
    )


def _response_task_kind(body: str, *, profile: str = "") -> str | None:
    """Classify only bounded tasks on the user-facing response plane."""

    if FAST_ADVISORY_MODE in body:
        return "fast_advisory"
    if "origin=user-query" in body and "root_task_role=scope_and_planning" in body:
        return "user_query_planning"
    if "workflow_role=synthesis" in body and "workflow_plane=response" in body:
        return "response_synthesis"
    if (
        profile == RISK_PROFILE
        and "origin=user-query" in body
        and "workflow_role=primary" in body
    ):
        return "risk_user_primary"
    return None


def _profile_from_argv(argv: Sequence[str]) -> str:
    """Read the dispatcher-selected profile before Hermes consumes ``-p``."""

    for index, value in enumerate(argv):
        if value in {"-p", "--profile"} and index + 1 < len(argv):
            return str(argv[index + 1]).strip()
        if value.startswith("--profile="):
            return value.partition("=")[2].strip()
    return ""


def _bounded_worker_argv(
    argv: Sequence[str],
    *,
    db_path: str | os.PathLike[str] | None = None,
    task_id: str | None = None,
    profile: str = "",
) -> list[str]:
    """Bound only dispatcher-owned tasks on the user response plane."""

    args = list(argv)
    body = _task_body(db_path, task_id)
    task_kind = _response_task_kind(body, profile=profile)
    if task_kind is None:
        return args
    try:
        chat_index = args.index("chat")
    except ValueError:
        return args
    has_turn_budget = any(
        arg == "--max-turns" or arg.startswith("--max-turns=") for arg in args
    )
    has_reasoning = any(
        arg == "--reasoning" or arg.startswith("--reasoning=") for arg in args
    )
    toolsets_index = next(
        (
            index
            for index, arg in enumerate(args)
            if arg in {"-t", "--toolsets"} or arg.startswith("--toolsets=")
        ),
        None,
    )
    additions: list[str] = []
    if not has_turn_budget:
        additions.extend(
            [
                "--max-turns",
                str(
                    _fast_advisory_max_turns()
                    if task_kind == "fast_advisory"
                    else _user_response_max_turns()
                ),
            ]
        )
    if not has_reasoning:
        additions.extend(["--reasoning", _user_response_reasoning()])
    if task_kind == "risk_user_primary" and toolsets_index is None:
        # The Risk response plane needs only the Kanban handoff and the
        # on-demand Legal Wiki edge. Excluding general skills, shell/code and
        # web tools prevents mandatory-skill expansion and arithmetic shell
        # fallbacks without weakening the legal evidence path.
        additions.extend(["--toolsets", RISK_USER_PRIMARY_TOOLSETS])
    elif task_kind == "risk_user_primary" and toolsets_index is not None:
        option = args[toolsets_index]
        if option in {"-t", "--toolsets"} and toolsets_index + 1 < len(args):
            args[toolsets_index + 1] = RISK_USER_PRIMARY_TOOLSETS
        elif option.startswith("--toolsets="):
            args[toolsets_index] = f"--toolsets={RISK_USER_PRIMARY_TOOLSETS}"
    if not additions:
        return args
    return [
        *args[: chat_index + 1],
        *additions,
        *args[chat_index + 1 :],
    ]


def _still_owned_and_unfinished(
    db_path: str | os.PathLike[str],
    task_id: str,
    run_id: int,
) -> bool:
    status, current_run_id, outcome = _read_live_run_state(db_path, task_id, run_id)
    return status == "running" and current_run_id == run_id and outcome is None


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
                return match.group(1).strip().strip("\"'")
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
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _run_real_worker(argv: Sequence[str]) -> int:
    """Run the unmodified Hermes worker and inherit its task log streams."""

    env = _real_worker_environment()
    # The dispatcher points HERMES_BIN at this wrapper.  The delegated Hermes
    # process must see the real binary so any nested Hermes resolution cannot
    # recurse back into the wrapper.
    env["HERMES_BIN"] = REAL_HERMES
    worker_profile = _profile_from_argv(argv) or env.get("HERMES_PROFILE", "")
    worker_argv = _bounded_worker_argv(
        argv,
        db_path=env.get("HERMES_KANBAN_DB"),
        task_id=env.get("HERMES_KANBAN_TASK"),
        profile=worker_profile,
    )
    task_id, run_id = _task_context()
    task_id = task_id or _task_id_from_argv(worker_argv)
    run_id = run_id or _latest_run_id(env.get("HERMES_KANBAN_DB"), task_id)
    started_ms = int(time.time() * 1000)
    completed = subprocess.run(
        [REAL_HERMES, *worker_argv],
        check=False,
        env=env,
    )
    ended_ms = int(time.time() * 1000)

    # The central dispatcher, not the department container, owns this worker
    # process. Attach the Accounting task/root IDs at this exact boundary so
    # LangSmith can correlate the model/tool observation with the Kanban task.
    # This is a fail-open observer and never changes Hermes' return code.
    if (
        task_id
        and run_id is not None
        and (
            worker_profile == "accounting-portfolio-department"
            or env.get("HERMES_PROFILE", "").strip()
            == "accounting-portfolio-department"
        )
    ):
        try:
            task_status, _current_run_id, outcome = _read_live_run_state(
                env.get("HERMES_KANBAN_DB", ""), task_id, run_id
            )
            from hermes_worker_observability import publish_accounting_worker_trace

            published = publish_accounting_worker_trace(
                task_id=task_id,
                task_body=_task_body(env.get("HERMES_KANBAN_DB"), task_id),
                task_status=task_status or outcome or "",
                run_id=str(run_id),
                return_code=int(completed.returncode),
                started_ms=started_ms,
                ended_ms=ended_ms,
                argv=worker_argv,
                env=env,
            )
            if not published:
                print(
                    f"accounting-observability task={task_id} status=not_published",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001 - observer is strictly fail-open.
            # LangSmith is optional and must not turn a valid terminal handoff
            # into a worker failure.
            print(
                "accounting-observability "
                f"task={task_id or 'unknown'} status=failed "
                f"error={type(exc).__name__}",
                file=sys.stderr,
            )
    return int(completed.returncode)


def _real_worker_environment() -> dict[str, str]:
    """Preserve Kanban's workspace without its false deprecated-env warning.

    Hermes' dispatcher starts this wrapper with ``cwd`` set to the task's
    resolved workspace and also exports the same path as ``TERMINAL_CWD``.
    Current Hermes then mistakes that task-scoped export for a deprecated
    profile ``.env`` entry.  The terminal and file tools already fall back to
    the process cwd, so removing only this dispatcher-owned duplicate keeps
    per-task isolation and avoids pinning a changing scratch path in a static
    profile config.
    """

    env = os.environ.copy()
    if env.get("HERMES_KANBAN_TASK") and env.get("HERMES_KANBAN_RUN_ID"):
        env.pop("TERMINAL_CWD", None)
    return env


def _drop_dispatcher_terminal_cwd() -> None:
    """Remove the dispatcher's duplicate cwd from this dedicated worker.

    QA workers retain this wrapper as their parent process while the real
    Hermes process runs.  Removing the duplicate from the wrapper too keeps
    nested subprocesses from rediscovering it and emitting the same false
    deprecation warning.  The dispatcher has already changed the process cwd
    to the task workspace before invoking this entry point.
    """

    if os.environ.get("HERMES_KANBAN_TASK"):
        os.environ.pop("TERMINAL_CWD", None)


def _is_qa_kanban_worker() -> bool:
    task_id, run_id = _task_context()
    return (
        task_id is not None
        and run_id is not None
        and os.environ.get("HERMES_PROFILE", "").strip() == QA_PROFILE
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    _drop_dispatcher_terminal_cwd()

    # Ordinary QA CLI/gateway commands are byte-for-byte delegated to Hermes;
    # only dispatcher-owned QA workers get the terminal-contract guard.
    if not _is_qa_kanban_worker():
        env = _real_worker_environment()
        env["HERMES_BIN"] = REAL_HERMES
        worker_argv = _bounded_worker_argv(
            args,
            db_path=env.get("HERMES_KANBAN_DB"),
            task_id=env.get("HERMES_KANBAN_TASK"),
            profile=(
                _profile_from_argv(args) or env.get("HERMES_PROFILE", "")
            ),
        )
        os.execvpe(REAL_HERMES, [REAL_HERMES, *worker_argv], env)
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
        and not _block_owned_task(task_id)
    ):
        # Do not claim success when the safety handoff itself failed.  The
        # dispatcher will retain its existing crash/recovery semantics.
        return 78

    return child_rc


if __name__ == "__main__":
    raise SystemExit(main())
