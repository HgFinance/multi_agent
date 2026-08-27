import inspect
import logging
import os
import sqlite3
import sys
import threading
import time
from functools import wraps
from pathlib import Path

_SENSITIVE_WORKER_ENV = (
    "MCP_RESEARCH_API_KEY",
    "MCP_RISK_API_KEY",
    "MCP_TRADING_ORDER_API_KEY",
    "TIMESCALE_DATABASE_URL",
)
_TERMINAL_RUN_STATES = frozenset({
    "done",
    "completed",
    "archived",
    "blocked",
    "gave_up",
    "timed_out",
    "crashed",
    "failed",
})
_OBSERVER_LOG = logging.getLogger("hgfinance.dispatch_observer")


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
    except ImportError:
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

        # Hermes' turn finalizer can reach its iteration-budget fallback after
        # a worker has already committed ``kanban_complete``.  The native
        # failure recorder then appends a misleading ``timed_out`` event to a
        # terminal card (the task itself remains done, but downstream
        # observers render a false limited-result warning).  Keep the native
        # fallback for running cards and make only this late terminal race a
        # no-op.  A read failure deliberately delegates to Hermes so this
        # observer guard can never become a new execution dependency.
        try:
            from agent import turn_finalizer as _turn_finalizer

            _original_budget_exhausted = getattr(
                _turn_finalizer, "_record_kanban_budget_exhausted", None
            )
            _terminal_budget_states = frozenset({
                "done",
                "completed",
                "archived",
                "blocked",
                "gave_up",
                "timed_out",
                "crashed",
                "failed",
                "triage",
            })
            if callable(_original_budget_exhausted) and not getattr(
                _original_budget_exhausted,
                "_hgfinance_terminal_race_guard",
                False,
            ):

                def _hgfinance_task_is_terminal(task_id):
                    db_path = os.environ.get("HERMES_KANBAN_DB", "").strip()
                    if not db_path:
                        return False
                    try:
                        connection = sqlite3.connect(
                            f"file:{Path(db_path).resolve()}?mode=ro",
                            uri=True,
                            timeout=1.0,
                        )
                        try:
                            row = connection.execute(
                                "SELECT status FROM tasks WHERE id = ?",
                                (str(task_id),),
                            ).fetchone()
                        finally:
                            connection.close()
                        return bool(
                            row
                            and str(row[0] or "").casefold()
                            in _terminal_budget_states
                        )
                    except (OSError, sqlite3.Error, TypeError, ValueError):
                        return False

                @wraps(_original_budget_exhausted)
                def _hgfinance_budget_exhausted_guard(
                    task_id, api_call_count, max_iterations, logger
                ):
                    if _hgfinance_task_is_terminal(task_id):
                        return None
                    return _original_budget_exhausted(
                        task_id, api_call_count, max_iterations, logger
                    )

                _hgfinance_budget_exhausted_guard._hgfinance_terminal_race_guard = True
                _turn_finalizer._record_kanban_budget_exhausted = (
                    _hgfinance_budget_exhausted_guard
                )

                _original_finalize_turn = getattr(
                    _turn_finalizer, "finalize_turn", None
                )
                if callable(_original_finalize_turn) and not getattr(
                    _original_finalize_turn,
                    "_hgfinance_terminal_race_guard",
                    False,
                ):

                    @wraps(_original_finalize_turn)
                    def _hgfinance_finalize_turn_guard(agent, *args, **kwargs):
                        task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
                        final_response = kwargs.get("final_response")
                        try:
                            api_call_count = int(kwargs.get("api_call_count") or 0)
                            max_iterations = int(getattr(agent, "max_iterations", 0) or 0)
                        except (TypeError, ValueError):
                            api_call_count = 0
                            max_iterations = 0
                        if (
                            task_id
                            and final_response is None
                            and max_iterations > 0
                            and api_call_count >= max_iterations
                            and _hgfinance_task_is_terminal(task_id)
                        ):
                            # The terminal tool already persisted the complete
                            # handoff. Avoid one extra summary-model call after
                            # the budget was consumed by that final tool turn.
                            kwargs["final_response"] = ""
                            kwargs["_turn_exit_reason"] = (
                                "text_response(kanban_terminal)"
                            )
                            kwargs["_pending_verification_response"] = None
                            kwargs["_pending_verification_response_previewed"] = False
                        return _original_finalize_turn(agent, *args, **kwargs)

                    _hgfinance_finalize_turn_guard._hgfinance_terminal_race_guard = True
                    _turn_finalizer.finalize_turn = _hgfinance_finalize_turn_guard
        except Exception:  # noqa: BLE001, S110 - optional compatibility guard
            pass

        # The shared dispatcher is the real process that runs CEO workers.
        # The CEO gateway image has the same validator, but it is not the
        # execution boundary for cards spawned by this daemon. Reuse the
        # repository validator here so a planner cannot create a QA card as a
        # second analysis primary. This wraps the existing Kanban primitive;
        # it does not create another task path or alter non-CEO workers.
        _repo_root = "/app/repo"
        _runtime_repo_root = str(Path(__file__).resolve().parents[2])
        for _path in (_repo_root, _runtime_repo_root):
            if _path not in sys.path:
                sys.path.insert(0, _path)
        # The worker observer imports its redacted LangSmith registry before
        # the detached observer thread can add this directory itself.  The
        # dispatcher image exposes the repository root but not ``scripts/``
        # on ``PYTHONPATH``, so register it at guard installation time.
        for _scripts_root in {
            f"{_repo_root}/scripts",
            str(Path(_runtime_repo_root, "scripts")),
        }:
            if _scripts_root not in sys.path:
                sys.path.insert(0, _scripts_root)
        from orchestration.primary_task_idempotency import (
            validate_primary_create,
        )

        _original_create_task = getattr(kb, "create_task", None)
        if callable(_original_create_task):

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

        # Strategy Hermes requests get a Kanban root for observability, but
        # that card is deliberately not an execution task.  A blocked create
        # can still be observed as ``running`` for a short interval while the
        # dispatcher races the CLI transaction, so status alone is not a safe
        # boundary.  Guard the atomic claim primitive instead: even if a
        # tracking card is briefly visible in the ready lane, this dispatcher
        # can never claim or spawn it.  Keep the card durable for dashboard /
        # correlation and leave its state untouched.
        def _is_strategy_tracking_body(body):
            return "strategy_research_tracking_only=true" in str(body or "")

        def _is_strategy_tracking_task(conn, task_id):
            try:
                row = conn.execute(
                    "SELECT body FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                return bool(row is not None and _is_strategy_tracking_body(row["body"]))
            except Exception:  # noqa: BLE001 - dispatcher guard fails open
                return False

        _original_claim_task = getattr(kb, "claim_task", None)
        if callable(_original_claim_task):

            @wraps(_original_claim_task)
            def _hgfinance_guarded_claim_task(conn, task_id, *args, **kwargs):
                if _is_strategy_tracking_task(conn, task_id):
                    return None
                return _original_claim_task(conn, task_id, *args, **kwargs)

            kb.claim_task = _hgfinance_guarded_claim_task

        _original_claim_review_task = getattr(kb, "claim_review_task", None)
        if callable(_original_claim_review_task):

            @wraps(_original_claim_review_task)
            def _hgfinance_guarded_claim_review_task(
                conn, task_id, *args, **kwargs
            ):
                if _is_strategy_tracking_task(conn, task_id):
                    return None
                return _original_claim_review_task(conn, task_id, *args, **kwargs)

            kb.claim_review_task = _hgfinance_guarded_claim_review_task

        # Strategy Hermes owns these durable tracking cards. They stay visible
        # in the shared board for correlation, but the generic dispatcher must
        # not call them spawnable work; their claim hooks above already keep
        # them unclaimed. Align the health probe with that ownership boundary
        # so a legitimate tracking backlog does not report a false stall.
        _original_has_spawnable_ready = getattr(kb, "has_spawnable_ready", None)
        if callable(_original_has_spawnable_ready):

            def _dispatcher_has_capacity(conn):
                """Keep intentional concurrency backpressure out of stuck health."""

                try:
                    max_spawn = int(
                        os.environ.get("KANBAN_DISPATCH_MAX_SPAWN", "")
                    )
                except (TypeError, ValueError):
                    return True
                if max_spawn < 1:
                    return True
                running = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
                ).fetchone()[0]
                return int(running) < max_spawn

            def _hgfinance_has_spawnable_ready(conn, *args, **kwargs):
                try:
                    rows = conn.execute(
                        "SELECT DISTINCT assignee, body FROM tasks "
                        "WHERE status = 'ready' AND assignee IS NOT NULL "
                        "AND claim_lock IS NULL"
                    ).fetchall()
                    rows = [
                        row
                        for row in rows
                        if not _is_strategy_tracking_body(row[1])
                    ]
                    if not rows:
                        return False
                    if not _dispatcher_has_capacity(conn):
                        return False
                    try:
                        from hermes_cli.profiles import profile_exists
                    except (ImportError, AttributeError):
                        return True
                    return any(profile_exists(row[0]) for row in rows)
                except Exception:  # noqa: BLE001 - preserve Hermes fallback
                    return _original_has_spawnable_ready(conn, *args, **kwargs)

            kb.has_spawnable_ready = _hgfinance_has_spawnable_ready

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
                        and (
                            "producer=ceo-hermes-direct" in body
                            or "producer=portfolio-bff-deterministic" in body
                        )
                        and "selected_primary_profiles=" in body
                    ):
                        return "hgfinance_control_plane_root"

            except Exception:  # noqa: BLE001, S110 - dispatcher guard fails open
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
                try:
                    from hermes_worker_observability import _PROFILE_SPECS
                except Exception as exc:  # noqa: BLE001 - observer is fail-open
                    _OBSERVER_LOG.warning(
                        "department-worker-trace-import-failed error=%s",
                        type(exc).__name__,
                    )
                    return
                if assignee not in _PROFILE_SPECS:
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
                                            "r.id, r.status, r.started_at, r.ended_at, "
                                            "r.outcome "
                                            "FROM tasks t LEFT JOIN task_runs r "
                                            "ON r.id = (SELECT id FROM task_runs "
                                            "WHERE task_id = t.id ORDER BY id DESC LIMIT 1) "
                                            "WHERE t.id = ?",
                                            (task_id,),
                                        ).fetchone()
                                    else:
                                        row = connection.execute(
                                            "SELECT t.status, t.started_at, t.completed_at, "
                                            "r.id, r.status, r.started_at, r.ended_at, "
                                            "r.outcome "
                                            "FROM tasks t LEFT JOIN task_runs r ON r.id = ? "
                                            "WHERE t.id = ?",
                                            (run_id, task_id),
                                        ).fetchone()
                                    return row
                                finally:
                                    connection.close()
                            except (OSError, TypeError, ValueError, sqlite3.Error):
                                return None

                        def _apply_terminal_state(state):
                            nonlocal ended_at, latest_run_id, return_code, task_status
                            nonlocal started_at
                            if not state:
                                return False
                            (
                                board_status,
                                board_started_at,
                                board_ended_at,
                                state_run_id,
                                run_status,
                                run_started_at,
                                run_ended_at,
                                outcome,
                            ) = state
                            run_state = str(run_status or outcome or "").casefold()
                            board_state = str(board_status or "").casefold()
                            # A retry can make the task row ``running`` while
                            # this exact older run is already ``crashed``.
                            # Prefer the run-owned state; otherwise a crashed
                            # attempt can be published as a successful retry.
                            terminal_status = (
                                run_state
                                if run_state in _TERMINAL_RUN_STATES
                                else board_state
                            )
                            if terminal_status not in _TERMINAL_RUN_STATES:
                                if run_state in {"", "running"}:
                                    return False
                                terminal_status = run_state
                            if run_state in _TERMINAL_RUN_STATES:
                                task_status = str(run_status or outcome)
                            else:
                                task_status = str(board_status or run_status or outcome)
                            latest_run_id = state_run_id or run_id
                            started_at = run_started_at or board_started_at or started_at
                            ended_at = run_ended_at or board_ended_at or ended_at
                            return_code = 0 if task_status.casefold() in {
                                "done",
                                "completed",
                                "archived",
                            } else 1
                            return True

                        # The native dispatcher exclusively owns child
                        # reaping. Poll only the exact task-run row so this
                        # observer cannot steal the PID and cause a false
                        # ``pid not alive`` crash/retry. This remains off the
                        # CEO response path and never creates a second run.
                        if not _apply_terminal_state(_read_task_run_state()):
                            while time.monotonic() < deadline:
                                if _apply_terminal_state(_read_task_run_state()):
                                    break
                                time.sleep(0.5)
                            else:
                                return

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
                    except Exception as exc:  # noqa: BLE001 - observability is fail open
                        # Observability must never change dispatcher behavior.
                        _OBSERVER_LOG.warning(
                            "department-worker-trace-failed assignee=%s error=%s",
                            assignee,
                            type(exc).__name__,
                        )
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

    except Exception:  # noqa: BLE001 - capability scope fails closed below
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
    except Exception:  # noqa: BLE001, S110 - provider policy is optional
        # Provider policy is an optimization, never a reason to prevent the
        # dispatcher from starting. Native Hermes retry remains the fallback.
        pass
