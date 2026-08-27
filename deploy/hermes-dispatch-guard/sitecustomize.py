import os
import inspect
import sqlite3
import sys
import threading
import time
from functools import wraps


_SENSITIVE_WORKER_ENV = (
    "MCP_RESEARCH_API_KEY",
    "MCP_RISK_API_KEY",
    "MCP_TRADING_ORDER_API_KEY",
    "TIMESCALE_DATABASE_URL",
)


def _fail_closed_secret_scope(reason):
    for name in _SENSITIVE_WORKER_ENV:
        os.environ.pop(name, None)
    os.environ["HGFINANCE_DISPATCH_SECRET_SCOPE_STATUS"] = \
        f"FAIL_CLOSED:{reason}"


def _suppress_dispatcher_cwd_warning():
    """Ignore the dispatcher-owned cwd bridge in worker startup diagnostics.

    Hermes' gateway warning cannot distinguish a task-scoped ``TERMINAL_CWD``
    injected by Kanban from a stale profile ``.env`` value.  The dispatcher
    already supplies the authoritative ``HERMES_KANBAN_WORKSPACE`` marker;
    suppress the warning only for that worker scope and leave interactive
    profile diagnostics unchanged.
    """

    if not os.environ.get("HERMES_KANBAN_WORKSPACE"):
        return
    try:
        import hermes_cli.config as hermes_config
    except Exception:
        return

    original = getattr(hermes_config, "warn_deprecated_cwd_env_vars", None)
    if not callable(original) or getattr(original, "_hgfinance_dispatcher_cwd", False):
        return

    @wraps(original)
    def _dispatcher_safe_cwd_warning(*args, **kwargs):
        if os.environ.get("HERMES_KANBAN_WORKSPACE"):
            return None
        return original(*args, **kwargs)

    _dispatcher_safe_cwd_warning._hgfinance_dispatcher_cwd = True
    hermes_config.warn_deprecated_cwd_env_vars = _dispatcher_safe_cwd_warning

if os.environ.get("HGFINANCE_DISPATCH_GUARD") == "1":
    try:
        import hermes_cli.kanban_db as kb

        _suppress_dispatcher_cwd_warning()

        # The shared dispatcher is the real process that runs CEO workers.
        # The CEO gateway image has the same validator, but it is not the
        # execution boundary for cards spawned by this daemon. Reuse the
        # repository validator here so a planner cannot create a QA card as a
        # second analysis primary. This wraps the existing Kanban primitive;
        # it does not create another task path or alter non-CEO workers.
        _repo_root = "/app/repo"
        if _repo_root not in sys.path:
            sys.path.insert(0, _repo_root)
        from orchestration.primary_task_idempotency import (
            validate_primary_create,
        )

        _original_create_task = kb.create_task

        @wraps(_original_create_task)
        def _hgfinance_guarded_create_task(*args, **kwargs):
            if os.environ.get("HERMES_PROFILE", "").strip() == "ceo-agent":
                rejection = validate_primary_create(
                    kwargs.get("body"),
                    kwargs.get("assignee"),
                    kwargs.get("idempotency_key"),
                )
                if rejection:
                    raise ValueError(rejection)
            return _original_create_task(*args, **kwargs)

        kb.create_task = _hgfinance_guarded_create_task

        _original_check_respawn_guard = kb.check_respawn_guard
        _original_guard_accepts_lane = (
            "lane" in inspect.signature(_original_check_respawn_guard).parameters
        )

        def _hgfinance_check_respawn_guard(conn, task_id, *, lane="ready"):
            try:
                row = conn.execute(
                    "SELECT assignee, body FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()

                if row is not None:
                    assignee = str(row["assignee"] or "").strip()
                    body = str(row["body"] or "")

                    if (
                        lane == "ready"
                        and assignee == "ceo-agent"
                        and "workflow_role=root" in body
                        and "workflow_mode=analysis" in body
                        and "producer=ceo-hermes-direct" in body
                        and "selected_primary_profiles=" in body
                    ):
                        return "hgfinance_control_plane_root"

            except Exception:
                # Fail open to Hermes' native behavior if our guard
                # cannot inspect the task. Never break the dispatcher.
                pass

            # Hermes removed the keyword-only ``lane`` argument in newer
            # releases.  The local policy still uses its default to identify
            # ready-lane calls, but must delegate using the installed
            # signature so an upstream CLI upgrade cannot stop dispatch.
            if _original_guard_accepts_lane:
                return _original_check_respawn_guard(
                    conn,
                    task_id,
                    lane=lane,
                )
            return _original_check_respawn_guard(conn, task_id)

        kb.check_respawn_guard = _hgfinance_check_respawn_guard

        # The central dispatcher needs cross-profile operational credentials in
        # its own process so it can start different department profiles. Hermes
        # builds each worker environment from ``os.environ`` though, which would
        # otherwise hand the PAPER-order credential to research/quant workers,
        # the research credential to trading, and the market DSN to every role.
        # Scope them at the last possible boundary around Hermes' native spawn.
        #
        # Unknown profiles receive neither credential.  The dispatcher loop is
        # single-threaded, but keep the temporary process-environment change
        # lock-protected so a future upstream implementation cannot interleave
        # two spawns.
        _WORKER_SECRET_PROFILE_SCOPES = {
            "MCP_RESEARCH_API_KEY": frozenset({
                "research-department",
                "quant-backtest-department",
                "research-liaison",
                "quant-liaison",
            }),
            "MCP_TRADING_ORDER_API_KEY": frozenset({
                "trading-department",
            }),
            "MCP_RISK_API_KEY": frozenset({
                "risk-management",
            }),
            "TIMESCALE_DATABASE_URL": frozenset({
                "quant-backtest-department",
            }),
        }
        _spawn_environment_lock = threading.RLock()
        _original_default_spawn = getattr(kb, "_default_spawn", None)

        if callable(_original_default_spawn):
            _original_spawn_accepts_board = (
                "board" in inspect.signature(
                    _original_default_spawn).parameters
            )

            def _hgfinance_scoped_default_spawn(
                    task, workspace, *, board=None):
                assignee = str(getattr(task, "assignee", "") or "").strip()
                with _spawn_environment_lock:
                    original = {
                        name: (name in os.environ, os.environ.get(name))
                        for name in _WORKER_SECRET_PROFILE_SCOPES
                    }
                    try:
                        for name, allowed_profiles in \
                                _WORKER_SECRET_PROFILE_SCOPES.items():
                            if assignee not in allowed_profiles:
                                os.environ.pop(name, None)
                        if _original_spawn_accepts_board:
                            return _original_default_spawn(
                                task, workspace, board=board)
                        return _original_default_spawn(task, workspace)
                    finally:
                        for name, (was_present, value) in original.items():
                            if was_present:
                                os.environ[name] = value or ""
                            else:
                                os.environ.pop(name, None)

            def _observe_department_worker_after_exit(task, pid):
                """Attach task/model/tool metadata at the real spawn boundary.

                Some Hermes releases resolve ``HERMES_BIN`` differently inside
                the detached child. The dispatcher itself is the authoritative
                process that knows both the claimed task/run and the spawned
                PID, so keep this small fail-open observer here as the durable
                department-worker fallback. It sends no task content.
                """

                assignee = str(getattr(task, "assignee", "") or "").strip()
                if assignee not in {
                    "accounting-portfolio-department",
                    "qa-department",
                }:
                    return
                task_id = str(getattr(task, "id", "") or "").strip()
                run_id = getattr(task, "current_run_id", None)
                if not task_id or pid is None:
                    return

                def _observe():
                    max_runtime = getattr(task, "max_runtime_seconds", None)
                    try:
                        timeout = max(60.0, min(float(max_runtime or 600) + 30.0, 1800.0))
                    except (TypeError, ValueError):
                        timeout = 630.0
                    deadline = time.monotonic() + timeout
                    return_code = 0
                    process_reaped = False
                    while time.monotonic() < deadline:
                        try:
                            waited_pid, wait_status = os.waitpid(int(pid), os.WNOHANG)
                        except (ChildProcessError, OSError):
                            # The native dispatcher may reap the child before
                            # this fail-open observer wins the waitpid race.
                            # The task-run row remains the authoritative
                            # terminal signal in that case; do not lose the
                            # LangSmith join key merely because the PID was
                            # reaped by another dispatcher loop.
                            break
                        if waited_pid == int(pid):
                            try:
                                return_code = os.waitstatus_to_exitcode(wait_status)
                            except (AttributeError, ValueError):
                                return_code = 0
                            process_reaped = True
                            break
                        time.sleep(0.5)
                    if not process_reaped and time.monotonic() >= deadline:
                        return

                    try:
                        import sys
                        from pathlib import Path

                        scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
                        if scripts_dir not in sys.path:
                            sys.path.insert(0, scripts_dir)
                        from hermes_worker_observability import (
                            publish_department_worker_trace,
                        )

                        db_path = os.environ.get("HERMES_KANBAN_DB", "")
                        task_status = ""
                        latest_run_id = run_id
                        started_at = getattr(task, "started_at", None) or \
                            getattr(task, "created_at", None) or time.time()
                        ended_at = time.time()

                        def _read_task_run_state():
                            if not db_path:
                                return None
                            try:
                                connection = sqlite3.connect(
                                    f"file:{Path(db_path).resolve()}?mode=ro",
                                    uri=True,
                                    timeout=1.0,
                                )
                                try:
                                    if run_id is None:
                                        row = connection.execute(
                                            "SELECT t.status, t.started_at, t.completed_at, "
                                            "r.id, r.status, r.ended_at, r.outcome "
                                            "FROM tasks t LEFT JOIN task_runs r "
                                            "ON r.id = (SELECT id FROM task_runs "
                                            "WHERE task_id = t.id ORDER BY id DESC LIMIT 1) "
                                            "WHERE t.id = ?",
                                            (task_id,),
                                        ).fetchone()
                                    else:
                                        row = connection.execute(
                                            "SELECT t.status, t.started_at, t.completed_at, "
                                            "r.id, r.status, r.ended_at, r.outcome "
                                            "FROM tasks t LEFT JOIN task_runs r ON r.id = ? "
                                            "WHERE t.id = ?",
                                            (run_id, task_id),
                                        ).fetchone()
                                    return row
                                finally:
                                    connection.close()
                            except (OSError, TypeError, ValueError, sqlite3.Error):
                                return None

                        if not process_reaped:
                            # A task run can be terminal even when the native
                            # loop already collected the PID. Poll only the
                            # single task/run row and keep the observer off the
                            # CEO response path.
                            while time.monotonic() < deadline:
                                state = _read_task_run_state()
                                if state:
                                    (
                                        board_status,
                                        board_started_at,
                                        board_ended_at,
                                        state_run_id,
                                        run_status,
                                        run_ended_at,
                                        outcome,
                                    ) = state
                                    terminal_status = str(
                                        board_status or run_status or outcome or ""
                                    ).casefold()
                                    if terminal_status in {
                                        "done",
                                        "completed",
                                        "archived",
                                        "blocked",
                                        "gave_up",
                                        "timed_out",
                                        "crashed",
                                        "failed",
                                    } or str(run_status or "").casefold() not in {
                                        "",
                                        "running",
                                    }:
                                        task_status = str(
                                            board_status or run_status or outcome or ""
                                        )
                                        latest_run_id = state_run_id or run_id
                                        started_at = board_started_at or started_at
                                        ended_at = board_ended_at or run_ended_at or ended_at
                                        return_code = 0 if task_status.casefold() in {
                                            "done",
                                            "completed",
                                            "archived",
                                        } else 1
                                        break
                                time.sleep(0.5)
                            else:
                                return
                        else:
                            state = _read_task_run_state()
                            if state:
                                task_status = str(state[0] or state[4] or state[6] or "")
                                latest_run_id = state[3] or run_id
                                started_at = state[1] or started_at
                                ended_at = state[2] or state[5] or ended_at

                        publish_department_worker_trace(
                            task_id=task_id,
                            task_body=str(getattr(task, "body", "") or ""),
                            task_status=task_status,
                            run_id=str(latest_run_id or "unknown"),
                            return_code=int(return_code),
                            started_ms=int(float(started_at) * 1000),
                            ended_ms=int(float(ended_at) * 1000),
                            argv=["-p", assignee],
                            env=os.environ,
                        )
                    except Exception:
                        # Observability must never change dispatcher behavior.
                        return

                threading.Thread(
                    target=_observe,
                    name=f"department-trace-{task_id}",
                    daemon=True,
                ).start()

            _original_spawn_for_observation = _hgfinance_scoped_default_spawn

            def _hgfinance_scoped_default_spawn_with_observation(
                    task, workspace, *, board=None):
                pid = _original_spawn_for_observation(task, workspace, board=board)
                _observe_department_worker_after_exit(task, pid)
                return pid

            _hgfinance_scoped_default_spawn_with_observation._hgfinance_secret_scope_active = True
            kb._default_spawn = _hgfinance_scoped_default_spawn_with_observation
            os.environ["HGFINANCE_DISPATCH_SECRET_SCOPE_STATUS"] = "ACTIVE_V1"
        else:
            _fail_closed_secret_scope("NO_DEFAULT_SPAWN_HOOK")

    except Exception:
        # A dispatcher without a known spawn hook must lose capabilities, not
        # silently hand every credential to every profile.  The startup
        # preflight also rejects this state so work queues stop visibly.
        _fail_closed_secret_scope("PATCH_INSTALL_FAILED")

    # Provider fail-fast is deliberately a separate hook.  It is loaded in
    # the dispatcher and inherited by its worker subprocesses through the
    # existing PYTHONPATH; it does not modify Kanban retrieval or queue policy.
    try:
        from provider_failfast import install as _install_provider_failfast

        _install_provider_failfast()
    except Exception:
        # Provider policy is an optimization, never a reason to prevent the
        # dispatcher from starting. Native Hermes retry remains the fallback.
        pass
