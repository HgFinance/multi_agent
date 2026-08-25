"""Background reconciler for the durable CEO UI mirror journal."""

from __future__ import annotations

import argparse
import logging
import os
import time

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


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


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
    limit = max(1, min(int(os.getenv("UI_MIRROR_PROJECTION_BATCH_SIZE", "2")), 10_000))
    poll = _positive_float("UI_MIRROR_PROJECTION_POLL_SECONDS", 5.0)

    if args.healthcheck:
        store.list_request_ids(limit=1)
        print("ceo-mirror-projection ready")
        return 0

    while True:
        try:
            try:
                # One authoritative board snapshot per cycle prevents every
                # request from spawning its own expensive `kanban list` call.
                listed_rows = list_tasks(include_archived=False)
            except Exception:  # noqa: BLE001 - a read outage must not stop the reconciler
                LOG.warning("kanban list snapshot unavailable; using per-workflow reads")
                listed_rows = None
            result = reconcile_workflow_projections(
                store, limit=limit, listed_rows=listed_rows
            )
            LOG.info(
                "ceo mirror projection cycle scanned=%d projected=%d failed=%d",
                result["scanned"], result["projected"], result["failed"],
            )
        except Exception:
            LOG.exception("ceo mirror projection cycle failed")
        if args.once:
            return 0
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
