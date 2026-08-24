from __future__ import annotations

import threading
import time
from uuid import uuid4

from orchestration.adapters.notion_idempotency import NotionIdempotency


def test_local_claim_serializes_same_projection_and_creates_once() -> None:
    projection = NotionIdempotency({}, namespace=f"test-{uuid4()}")
    pages: list[dict[str, str]] = []
    create_count = 0
    count_lock = threading.Lock()

    def lookup() -> list[dict[str, str]]:
        with count_lock:
            return list(pages)

    def create() -> dict[str, str]:
        nonlocal create_count
        with count_lock:
            create_count += 1
            page = {"id": "page-1"}
            pages.append(page)
        time.sleep(0.02)
        return page

    results = []

    def run() -> None:
        results.append(
            projection.execute(
                "db-1",
                "projection-1",
                lookup=lookup,
                create=create,
            )
        )

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert create_count == 1
    assert sorted(result.duplicate for result in results) == [False, True]
    assert all(result.page_id == "page-1" for result in results)

