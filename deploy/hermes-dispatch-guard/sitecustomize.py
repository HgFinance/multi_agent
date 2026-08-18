import os
import inspect

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

    except Exception:
        # Do not prevent Hermes from starting because of an HgFinance patch.
        pass
