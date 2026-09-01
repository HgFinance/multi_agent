"""Apply the minimal Hermes v0.20.0 fix for explicit initial blocks.

This is deliberately an exact, fail-closed source patch rather than a copied
Kanban implementation.  Hermes remains the single owner of task creation,
event history and ready-state reconciliation.  When the pinned upstream source
changes, the image build stops so the compatibility decision is explicit.
"""

from __future__ import annotations

import argparse
from pathlib import Path

DEFAULT_TARGET = Path("/opt/hermes-agent/hermes_cli/kanban_db.py")
ANCHOR = '''                _inherit_notify_subs(conn, task_id, parents, created_at=now)
            return task_id
'''
REPLACEMENT = '''                _inherit_notify_subs(conn, task_id, parents, created_at=now)
                # `initial_status=blocked` is an explicit operator/API park,
                # not a dependency wait.  Record the same durable event that
                # `kanban block` writes so recompute_ready never promotes a
                # parentless card before an explicit `kanban unblock`.
                if task_status == "blocked":
                    _append_event(
                        conn,
                        task_id,
                        "blocked",
                        {"reason": "initial_status=blocked", "source_status": "ready"},
                    )
            return task_id
'''


def apply_patch(target: Path) -> None:
    source = target.read_text(encoding="utf-8")
    occurrences = source.count(ANCHOR)
    if occurrences != 1:
        raise SystemExit(
            f"refusing Hermes initial-block patch: expected one source anchor in {target}, "
            f"found {occurrences}"
        )
    target.write_text(source.replace(ANCHOR, REPLACEMENT, 1), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    apply_patch(args.target)


if __name__ == "__main__":
    main()
