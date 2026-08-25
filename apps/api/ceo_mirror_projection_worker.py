"""Background reconciler for the durable CEO UI mirror journal."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

try:
    from .ceo_kanban_read import list_tasks
    from .ceo_mirror import build_default_mirror_store
    from .ceo_mirror_projection import reconcile_workflow_projections
except ImportError:  # pragma: no cover - direct module execution compatibility
    from ceo_kanban_read import list_tasks  # type: ignore[no-redef]
    from ceo_mirror import build_default_mirror_store  # type: ignore[no-redef]
    from ceo_mirror_projection import (  # type: ignore[no-redef]
        reconcile_workflow_projections,
    )


LOG = logging.getLogger("ceo-mirror-projection-worker")
_WORKFLOW_ROOT_RE = re.compile(r"(?m)^workflow_root_task_id=(\S+)\s*$")


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _kanban_event_watermark() -> int | None:
    """Return a cheap read-only board change clock.

    The SQLite value is discovery metadata only. Projection state is still
    hydrated through the canonical Hermes CLI, exactly as before.
    """

    configured = os.getenv("HERMES_KANBAN_DB", "").strip()
    if not configured:
        home = os.getenv("HERMES_KANBAN_HOME", "").strip()
        if home:
            configured = str(Path(home) / "kanban.db")
    if not configured:
        return None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{Path(configured)}?mode=ro",
            uri=True,
            timeout=1,
        )
        row = connection.execute("select max(id) from task_events").fetchone()
        return int(row[0] or 0) if row else 0
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        if connection is not None:
            connection.close()


def _kanban_event_changes(after_id: int, *, limit: int = 2_000) -> tuple[int, set[str], bool] | None:
    """Return changed task ids since a watermark using read-only discovery."""

    configured = os.getenv("HERMES_KANBAN_DB", "").strip()
    if not configured:
        home = os.getenv("HERMES_KANBAN_HOME", "").strip()
        if home:
            configured = str(Path(home) / "kanban.db")
    if not configured:
        return None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{Path(configured)}?mode=ro", uri=True, timeout=1
        )
        rows = connection.execute(
            "select id, task_id from task_events where id > ? order by id limit ?",
            (int(after_id), int(limit) + 1),
        ).fetchall()
        overflow = len(rows) > limit
        bounded = rows[:limit]
        watermark = int(bounded[-1][0]) if bounded else int(after_id)
        return watermark, {str(row[1]) for row in bounded if row[1]}, overflow
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        if connection is not None:
            connection.close()


def _changed_request_ids(
    store: object,
    changed_task_ids: set[str],
    listed_rows: Sequence[Mapping[str, object]],
) -> list[str]:
    """Map changed Kanban task ids to existing mirror request ids."""

    changed_roots = set(changed_task_ids)
    for row in listed_rows:
        row_id = str(row.get("id") or row.get("task_id") or "").strip()
        if row_id not in changed_task_ids:
            continue
        marker = _WORKFLOW_ROOT_RE.search(str(row.get("body") or ""))
        if marker:
            changed_roots.add(marker.group(1))

    selected: list[str] = []
    for request_id in store.list_request_ids(limit=10_000):  # type: ignore[attr-defined]
        record = store.get_request(request_id)  # type: ignore[attr-defined]
        root_id = (
            str(record.response.get("task_id") or "").strip()
            if record is not None and record.response
            else ""
        )
        if root_id and root_id in changed_roots:
            selected.append(request_id)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store = build_default_mirror_store()
    # `kanban show` is bounded but not free; keep each cycle short enough that
    # fresh requests are not trapped behind a historical mirror backlog.
    limit = max(1, min(int(os.getenv("UI_MIRROR_PROJECTION_BATCH_SIZE", "25")), 10_000))
    poll = _positive_float("UI_MIRROR_PROJECTION_POLL_SECONDS", 5.0)
    full_reconcile = _positive_float(
        "UI_MIRROR_PROJECTION_FULL_RECONCILE_SECONDS",
        600.0,
    )

    if args.healthcheck:
        store.list_request_ids(limit=1)
        print("ceo-mirror-projection ready")
        return 0

    last_watermark: int | None = None
    last_reconcile_at = 0.0
    while True:
        try:
            watermark = _kanban_event_watermark()
            now = time.monotonic()
            board_changed = watermark is None or watermark != last_watermark
            full_due = now - last_reconcile_at >= full_reconcile
            if not board_changed and not full_due:
                if args.once:
                    return 0
                time.sleep(poll)
                continue
            try:
                # One authoritative board snapshot per cycle prevents every
                # request from spawning its own expensive `kanban list` call.
                listed_rows = list_tasks(include_archived=False)
            except Exception:  # noqa: BLE001 - a read outage must not stop the reconciler
                LOG.warning("kanban list snapshot unavailable; using per-workflow reads")
                listed_rows = None
            request_ids = None
            effective_watermark = watermark
            if (
                not full_due
                and last_watermark is not None
                and listed_rows is not None
            ):
                changes = _kanban_event_changes(last_watermark)
                if changes is not None:
                    effective_watermark, changed_task_ids, overflow = changes
                    if not overflow:
                        request_ids = _changed_request_ids(
                            store, changed_task_ids, listed_rows
                        )
            result = reconcile_workflow_projections(
                store,
                limit=limit,
                listed_rows=listed_rows,
                request_ids=request_ids,
            )
            LOG.info(
                "ceo mirror projection cycle scanned=%d projected=%d failed=%d "
                "watermark=%s full=%s",
                result["scanned"], result["projected"], result["failed"],
                watermark,
                str(full_due).lower(),
            )
            if effective_watermark is not None:
                last_watermark = effective_watermark
            last_reconcile_at = time.monotonic()
        except Exception:
            LOG.exception("ceo mirror projection cycle failed")
        if args.once:
            return 0
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "_changed_request_ids",
    "_kanban_event_changes",
    "_kanban_event_watermark",
    "main",
]
