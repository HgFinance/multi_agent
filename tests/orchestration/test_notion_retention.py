from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestration.adapters.notion_retention import NotionPage, NotionRetentionWorker


def _page(
    page_id: str,
    *,
    created_at: datetime,
    key: str = "",
    last_edited_at: datetime | None = None,
    status: str = "",
) -> NotionPage:
    return NotionPage(
        page_id=page_id,
        database_id="db-1",
        database_env="NOTION_CEO_DB",
        created_at=created_at,
        title=page_id,
        input_hash=key,
        replay_id="",
        last_edited_at=last_edited_at,
        status=status,
    )


def test_retention_preserves_unidentified_active_and_recently_edited_pages() -> None:
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    pages = [
        _page("old-identified", created_at=now - timedelta(days=8), key="hash-1"),
        _page("old-unidentified", created_at=now - timedelta(days=8)),
        _page(
            "old-active",
            created_at=now - timedelta(days=8),
            key="hash-2",
            status="running",
        ),
        _page(
            "recently-edited",
            created_at=now - timedelta(days=8),
            last_edited_at=now - timedelta(days=1),
            key="hash-3",
        ),
    ]
    worker = NotionRetentionWorker(
        token="token",
        database_env_names=("NOTION_CEO_DB",),
        retention_days=7,
        request_delay_seconds=0,
    )
    worker._database_ids = lambda: [("NOTION_CEO_DB", "db-1")]
    worker._query_database = lambda _env, _database: pages
    archived: list[str] = []
    worker._archive = lambda page, dry_run: archived.append(page.page_id) or True

    summary = worker.run_once(now=now)

    assert archived == ["old-identified"]
    assert summary.archived == 1
    assert summary.skipped == 2


def test_retention_deduplicates_by_latest_activity_and_caps_external_mutations() -> None:
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    pages = [
        _page("duplicate-old", created_at=now - timedelta(days=3), key="same"),
        _page(
            "duplicate-new",
            created_at=now - timedelta(days=2),
            last_edited_at=now - timedelta(days=1),
            key="same",
        ),
        _page("second-old", created_at=now - timedelta(days=8), key="other"),
    ]
    worker = NotionRetentionWorker(
        token="token",
        database_env_names=("NOTION_CEO_DB",),
        retention_days=7,
        max_archives=1,
        request_delay_seconds=0,
    )
    worker._database_ids = lambda: [("NOTION_CEO_DB", "db-1")]
    worker._query_database = lambda _env, _database: pages
    archived: list[str] = []
    worker._archive = lambda page, dry_run: archived.append(page.page_id) or True

    summary = worker.run_once(now=now)

    assert archived == ["duplicate-old"]
    assert summary.archived == 1
    assert summary.duplicate_archived == 1
    assert summary.limit_reached is True


def test_retention_archives_only_old_resolved_ceo_briefings_without_projection_key() -> None:
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    old = NotionPage(
        page_id="old-ceo-briefing",
        database_id="db-1",
        database_env="NOTION_CEO_DB",
        created_at=now - timedelta(days=8),
        title="CEO report",
        input_hash="",
        replay_id="",
        status="보고 완료",
        category="저녁 브리핑",
        approval_count="0",
        in_progress_count="0",
        blocker_count="0",
    )
    recent = NotionPage(
        page_id="recent-ceo-briefing",
        database_id="db-1",
        database_env="NOTION_CEO_DB",
        created_at=now - timedelta(days=1),
        title="CEO report",
        input_hash="",
        replay_id="",
        status="보고 완료",
        category="저녁 브리핑",
        approval_count="0",
        in_progress_count="0",
        blocker_count="0",
    )
    unresolved = NotionPage(
        page_id="old-unresolved-ceo-briefing",
        database_id="db-1",
        database_env="NOTION_CEO_DB",
        created_at=now - timedelta(days=8),
        title="CEO report",
        input_hash="",
        replay_id="",
        status="보고 완료",
        category="저녁 브리핑",
        approval_count="1",
        in_progress_count="0",
        blocker_count="0",
    )
    worker = NotionRetentionWorker(
        token="token",
        database_env_names=("NOTION_CEO_DB",),
        retention_days=7,
        request_delay_seconds=0,
    )
    worker._database_ids = lambda: [("NOTION_CEO_DB", "db-1")]
    worker._query_database = lambda _env, _database: [old, recent, unresolved]
    archived: list[str] = []
    worker._archive = lambda page, dry_run: archived.append(page.page_id) or True

    summary = worker.run_once(now=now)

    assert archived == ["old-ceo-briefing"]
    assert summary.archived == 1
    assert summary.old_archived == 1
    assert summary.skipped == 1
