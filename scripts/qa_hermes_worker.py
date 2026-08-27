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
synthesis, QA reviews, and ``fast_advisory`` tasks receive task-scoped
turn/reasoning budgets; standard analysis and experiment tasks do not.
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

REAL_HERMES = (
    os.environ.get("HGFINANCE_QA_REAL_HERMES") or "/opt/hermes/.venv/bin/hermes"
).strip()
QA_PROFILE = "qa-department"
RISK_PROFILE = "risk-management"
RISK_USER_PRIMARY_TOOLSETS = "kanban,risk-legal"
_TASK_ID_RE = re.compile(r"\bt_[A-Za-z0-9_-]+\b")
FAST_ADVISORY_MODE = "analysis_mode=fast_advisory"
DEFAULT_FAST_ADVISORY_MAX_TURNS = 12
MIN_FAST_ADVISORY_MAX_TURNS = 8
MAX_FAST_ADVISORY_MAX_TURNS = 64
DEFAULT_QA_PRIMARY_MAX_TURNS = 8
MIN_QA_PRIMARY_MAX_TURNS = 6
MAX_QA_PRIMARY_MAX_TURNS = 16
DEFAULT_QA_PRIMARY_REASONING = "high"
DEFAULT_USER_RESPONSE_MAX_TURNS = 12
MIN_USER_RESPONSE_MAX_TURNS = 8
MAX_USER_RESPONSE_MAX_TURNS = 32
DEFAULT_USER_RESPONSE_REASONING = "medium"
DEFAULT_QA_AUDIT_MAX_TURNS = 16
MIN_QA_AUDIT_MAX_TURNS = 8
MAX_QA_AUDIT_MAX_TURNS = 32
DEFAULT_QA_AUDIT_REASONING = "high"
QA_AUDIT_TOOLSETS = "kanban"
QA_PRIMARY_TOOLSETS = "kanban,terminal"
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


def _qa_audit_max_turns() -> int:
    raw = os.environ.get(
        "HGFINANCE_QA_AUDIT_MAX_TURNS",
        str(DEFAULT_QA_AUDIT_MAX_TURNS),
    ).strip()
    try:
        configured = int(raw)
    except ValueError:
        configured = DEFAULT_QA_AUDIT_MAX_TURNS
    return min(MAX_QA_AUDIT_MAX_TURNS, max(MIN_QA_AUDIT_MAX_TURNS, configured))


def _qa_primary_max_turns() -> int:
    raw = os.environ.get(
        "HGFINANCE_QA_PRIMARY_MAX_TURNS",
        str(DEFAULT_QA_PRIMARY_MAX_TURNS),
    ).strip()
    try:
        configured = int(raw)
    except ValueError:
        configured = DEFAULT_QA_PRIMARY_MAX_TURNS
    return min(MAX_QA_PRIMARY_MAX_TURNS, max(MIN_QA_PRIMARY_MAX_TURNS, configured))


def _qa_primary_reasoning() -> str:
    configured = os.environ.get(
        "HGFINANCE_QA_PRIMARY_REASONING", DEFAULT_QA_PRIMARY_REASONING
    ).strip().casefold()
    return configured if configured in _REASONING_LEVELS else DEFAULT_QA_PRIMARY_REASONING


def _qa_audit_reasoning() -> str:
    configured = os.environ.get(
        "HGFINANCE_QA_AUDIT_REASONING", DEFAULT_QA_AUDIT_REASONING
    ).strip().casefold()
    return configured if configured in _REASONING_LEVELS else DEFAULT_QA_AUDIT_REASONING


def _response_task_kind(body: str, *, profile: str = "") -> str | None:
    """Classify only bounded tasks on the user-facing response plane."""

    if profile == QA_PROFILE and (
        "workflow_role=primary" in body
        and "origin=user-query" in body
    ):
        return "qa_primary"
    if profile == QA_PROFILE and (
        (
            "workflow_role=qa" in body
            and "workflow_plane=governance" in body
        )
        or (
            "workflow_role=primary" in body
            and (
                "selected_primary_profiles=qa-department" in body
                or "delegation_instruction.qa-department=" in body
            )
        )
    ):
        return "qa_audit"
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
                    else _qa_primary_max_turns()
                    if task_kind == "qa_primary"
                    else _qa_audit_max_turns()
                    if task_kind == "qa_audit"
                    else _user_response_max_turns()
                ),
            ]
        )
    if not has_reasoning:
        additions.extend(
            [
                "--reasoning",
                _qa_primary_reasoning()
                if task_kind == "qa_primary"
                else _qa_audit_reasoning()
                if task_kind == "qa_audit"
                else _user_response_reasoning(),
            ]
        )
    if task_kind in {"qa_audit", "qa_primary"}:
        # Post-response QA receives the exact bounded audit input in the
        # Kanban task body. Delegated specialists, shell/file exploration and
        # full-history reads only duplicate that evidence and can serialize
        # into multi-minute LLM waits. Keep lifecycle tools available while
        # making this audit one bounded, non-delegating review.
        toolsets = (
            QA_PRIMARY_TOOLSETS if task_kind == "qa_primary" else QA_AUDIT_TOOLSETS
        )
        if toolsets_index is None:
            additions.extend(["--toolsets", toolsets])
        else:
            option = args[toolsets_index]
            if option in {"-t", "--toolsets"} and toolsets_index + 1 < len(args):
                args[toolsets_index + 1] = toolsets
            elif option.startswith("--toolsets="):
                args[toolsets_index] = f"--toolsets={toolsets}"
    elif task_kind == "risk_user_primary" and toolsets_index is None:
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


def _qa_audit_worker_argv(
    argv: Sequence[str],
    *,
    task_body: str,
) -> list[str]:
    """Give post-response QA its bounded evidence without board re-expansion.

    The Kanban worker prompt normally asks Hermes to call ``kanban_show``. That
    tool returns the task, parent handoffs, comments, events, runs, and a
    pre-formatted context block; repeating it can dominate a QA audit that
    already contains the exact response payload in its task body. Keep the
    same task-scoped evidence, but provide it once in the worker query so the
    audit can reserve its calls for the actual verdict and terminal handoff.
    """

    body = str(task_body or "").strip()
    prompt = (
        "Review this complete post-response QA task payload as the authoritative "
        "evidence boundary. Perform one independent audit and preserve unknowns; "
        "do not call kanban_show or kanban_list, do not delegate, and do not use "
        "shell/file tools. After the audit, call kanban_complete with the structured "
        "PASS/WARN/FAIL checks, findings, limitations, and PAPER/read-only safety "
        "status. The QA result is asynchronous and must not rewrite or delay the "
        "already delivered CEO response.\n\n"
        "TASK PAYLOAD:\n"
        + body
    )
    args = list(argv)
    for index, arg in enumerate(args):
        if arg in {"-q", "--query"} and index + 1 < len(args):
            args[index + 1] = prompt
            return args
        if arg.startswith("--query="):
            args[index] = f"--query={prompt}"
            return args
    return [*args, "--query", prompt]


def _qa_primary_worker_argv(
    argv: Sequence[str],
    *,
    task_body: str,
) -> list[str]:
    """Give a primary QA E2E task one bounded diagnostic pass.

    Primary QA tasks need terminal access to inspect existing redacted logs, but
    they do not need the general skill catalog, delegation, or repeated board
    expansion. Keeping the task payload inline also avoids spending turns
    rediscovering the same root/task identifiers.
    """

    body = str(task_body or "").strip()
    prompt = (
        "Perform one bounded PAPER/read-only QA E2E diagnostic using only the "
        "authoritative task payload below. Inspect existing local redacted logs "
        "and state as needed, then report concrete evidence and unknowns. Do "
        "not create or modify tasks, call skill_view/skill_manage, delegate, "
        "write files, submit orders, change ledgers, or change configuration. "
        "Do not repeat a failed command; if sqlite3, heredoc, or shell -c is "
        "unavailable, continue with an available read-only command and record "
        "the limitation. Use kanban_complete exactly once with structured "
        "summary/result/error/block_reason and a user-ready final_answer. QA is "
        "post-analysis governance and must not delay or rewrite any CEO/user "
        "response.\n\nTASK PAYLOAD:\n"
        + body
    )
    args = list(argv)
    for index, arg in enumerate(args):
        if arg in {"-q", "--query"} and index + 1 < len(args):
            args[index + 1] = prompt
            return args
        if arg.startswith("--query="):
            args[index] = f"--query={prompt}"
            return args
    return [*args, "--query", prompt]


def _is_post_response_qa(body: str) -> bool:
    """Return whether QA is the non-blocking audit after CEO delivery."""

    return (
        "qa_phase=post_response" in body
        and "qa_timing=after_ceo_response" in body
        and "response_delivered=true" in body
    )


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
    if worker_profile == QA_PROFILE and task_id:
        task_body = _task_body(env.get("HERMES_KANBAN_DB"), task_id)
        task_kind = _response_task_kind(task_body, profile=worker_profile)
        if _is_post_response_qa(task_body):
            worker_argv = _qa_audit_worker_argv(
                worker_argv,
                task_body=task_body,
            )
        elif task_kind == "qa_primary":
            worker_argv = _qa_primary_worker_argv(
                worker_argv,
                task_body=task_body,
            )
    started_ms = int(time.time() * 1000)
    completed = subprocess.run(
        [REAL_HERMES, *worker_argv],
        check=False,
        env=env,
    )
    ended_ms = int(time.time() * 1000)

    # The central dispatcher, not the department container, owns this worker
    # process. Attach the task/root IDs for every profile supported by the
    # one shared observer. This is a fail-open observer and never changes
    # Hermes' return code or the CEO response dependency.
    if task_id and run_id is not None:
        try:
            task_status, _current_run_id, outcome = _read_live_run_state(
                env.get("HERMES_KANBAN_DB", ""), task_id, run_id
            )
            from hermes_worker_observability import publish_department_worker_trace

            published = publish_department_worker_trace(
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
                    f"worker-observability task={task_id} status=not_published",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001 - observer is strictly fail-open.
            # LangSmith is optional and must not turn a valid terminal handoff
            # into a worker failure.
            print(
                "worker-observability "
                f"task={task_id or 'unknown'} status=failed "
                f"error={type(exc).__name__}",
                file=sys.stderr,
            )

    # ▶ 부서장 카드 1장의 Langfuse span 트리 (2026-08-27 신규)
    #
    #   위 LangSmith 관측과 **별도 블록**이다. 하나가 실패했다고 다른 하나를
    #   건너뛰면, 관측이 반쯤 있는 상태가 조용히 만들어진다.
    #
    #   위 블록과 달리 프로필 화이트리스트가 없다. 기존 관측은 accounting/qa 두
    #   프로필만 보고 있어서 **나머지 6개 부서장은 어디에도 안 찍혔다** - 사용자
    #   질의가 CEO 루트 카드로만 들어오는 구조라(apps/api/ceo.py), 그 카드에서
    #   갈라지는 CEO·부서·QA·종합 턴이 전부 이 지점을 지나간다.
    #
    #   run_id 가 없어도 발행한다. 그건 카드가 없다는 뜻이 아니라 attempt 행을
    #   아직 못 읽었다는 뜻이고, 그때도 지연·토큰은 재진다.
    if task_id:
        try:
            from head_card_trace import publish_card_trace

            task_body = _task_body(env.get("HERMES_KANBAN_DB"), task_id)
            task_status, _current, outcome = _read_live_run_state(
                env.get("HERMES_KANBAN_DB", ""), task_id, run_id or 0
            )
            from hermes_worker_observability import _request_id, _root_id, _status

            root_id = _root_id(task_id=task_id, task_body=task_body)
            status, _error_code = _status(
                task_status=task_status or outcome or "",
                return_code=int(completed.returncode),
            )
            outcome_line = publish_card_trace(
                profile=worker_profile,
                task_id=task_id,
                root_id=root_id,
                request_id=_request_id(root_id=root_id, task_body=task_body),
                run_id=str(run_id or ""),
                status=status.upper(),
                started_ms=started_ms,
                ended_ms=ended_ms,
                attempts=_task_attempt_count(env.get("HERMES_KANBAN_DB"), task_id),
                env=env,
            )
            if not outcome_line.startswith("published"):
                # 계측 코드가 자기 실패를 관측하지 못하면 관측이 없는 것과 같다
                # (2026-08-23 에 langfuse 미설치로 몇 시간을 태운 뒤 얻은 규칙).
                print(
                    f"head-card-trace task={task_id} profile={worker_profile} "
                    f"reason={outcome_line}",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001 - observer is strictly fail-open.
            print(
                f"head-card-trace task={task_id} status=failed "
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
    # The dispatcher always pins ``HERMES_KANBAN_WORKSPACE``.  Older daemon
    # builds did not export the run id early enough, so requiring both task
    # and run markers allowed the duplicate CWD to leak into Hermes startup.
    if env.get("HERMES_KANBAN_TASK") or env.get("HERMES_KANBAN_WORKSPACE"):
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

    if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get(
        "HERMES_KANBAN_WORKSPACE"
    ):
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
