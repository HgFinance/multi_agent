import os
import inspect
import threading


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

if os.environ.get("HGFINANCE_DISPATCH_GUARD") == "1":
    try:
        import hermes_cli.kanban_db as kb

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

            _hgfinance_scoped_default_spawn._hgfinance_secret_scope_active = True
            kb._default_spawn = _hgfinance_scoped_default_spawn
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
