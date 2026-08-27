"""Bounded Notion retention and exact-replay cleanup for HgFinance projections.

Only the configured HgFinance projection databases are touched.  Pages are
archived through Notion's API (rather than hard-deleted), so an accidental
retention decision remains recoverable.  The worker never reads page blocks or
stores page content; it uses only page timestamps and small structured
property values needed to identify projection records and exact replay noise.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

LOG = logging.getLogger(__name__)
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# CEO and briefing are commonly aliases.  The worker de-duplicates IDs before
# querying so an alias never causes a page to be processed twice.
DATABASE_ENV_NAMES = (
    "NOTION_CEO_DB",
    "NOTION_BRIEFING_DB",
    "NOTION_RESEARCH_DB",
    "NOTION_TRADING_DB",
    "NOTION_RISK_DB",
    "NOTION_QUANT_BACKTEST_DB",
    "NOTION_ACCOUNTING_DB",
    "NOTION_QA_DB",
    "NOTION_HR_DB",
)


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 10_000) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _property_text(value: Mapping[str, Any]) -> str:
    """Read a bounded structured property, never page blocks."""

    kind = str(value.get("type") or "")
    if kind in {"title", "rich_text"}:
        parts = value.get(kind)
        if isinstance(parts, Sequence) and not isinstance(parts, (str, bytes)):
            return "".join(
                str(item.get("plain_text") or "")
                for item in parts
                if isinstance(item, Mapping)
            ).strip()[:256]
    if kind == "select":
        selected = value.get("select")
        return str(selected.get("name") or "")[:256] if isinstance(selected, Mapping) else ""
    if kind == "status":
        selected = value.get("status")
        return str(selected.get("name") or "")[:256] if isinstance(selected, Mapping) else ""
    if kind == "number":
        number = value.get("number")
        return "" if number is None else str(number)[:64]
    if kind == "date":
        date = value.get("date")
        return str(date.get("start") or "")[:64] if isinstance(date, Mapping) else ""
    if kind == "formula":
        formula = value.get("formula")
        if isinstance(formula, Mapping):
            return _property_text(formula)
    return ""


@dataclass(frozen=True)
class NotionPage:
    page_id: str
    database_id: str
    database_env: str
    created_at: datetime | None
    title: str
    input_hash: str
    replay_id: str
    last_edited_at: datetime | None = None
    status: str = ""
    projection_marker: str = ""
    category: str = ""
    approval_count: str = ""
    in_progress_count: str = ""
    blocker_count: str = ""

    @property
    def exact_replay_key(self) -> str | None:
        # input_hash is the strongest marker used by the current projections.
        # replay_id covers older Risk records that predate input_hash.
        value = self.input_hash or self.replay_id
        return value or None

    @property
    def retention_key(self) -> str | None:
        """Return a structured projection identity safe for archival decisions."""

        return self.exact_replay_key or self.projection_marker or None

    @property
    def activity_at(self) -> datetime | None:
        values = [value for value in (self.created_at, self.last_edited_at) if value]
        return max(values) if values else None

    @property
    def terminal(self) -> bool:
        # An absent status is supported for legacy projection schemas, but an
        # explicit non-terminal/unknown status is never archived automatically.
        normalized = self.status.strip().casefold()
        return not normalized or normalized in TERMINAL_STATUSES

    @property
    def resolved_ceo_briefing(self) -> bool:
        """Identify the legacy CEO report schema without reading page blocks."""

        if self.category.strip() != "저녁 브리핑" or self.status.strip() != "보고 완료":
            return False
        return all(
            _is_zero(value)
            for value in (
                self.approval_count,
                self.in_progress_count,
                self.blocker_count,
            )
        )


@dataclass(frozen=True)
class RetentionSummary:
    enabled: bool
    available: bool
    scanned: int = 0
    archived: int = 0
    old_archived: int = 0
    duplicate_archived: int = 0
    skipped: int = 0
    limit_reached: bool = False
    error_code: str | None = None


TERMINAL_STATUSES = {
    "done",
    "completed",
    "complete",
    "success",
    "succeeded",
    "failed",
    "error",
    "blocked",
    "cancelled",
    "canceled",
    "archived",
    "closed",
    "finished",
    "완료",
    "보고 완료",
    "성공",
    "실패",
    "차단",
    "취소",
    "종료",
}


def _property_value(values: Mapping[str, str], *names: str) -> str:
    aliases = {
        name.casefold().replace(" ", "").replace("_", "").replace("-", "")
        for name in names
    }
    for name, value in values.items():
        normalized = str(name).casefold().replace(" ", "").replace("_", "").replace("-", "")
        if normalized in aliases and value:
            return value
    return ""


def _is_zero(value: str) -> bool:
    try:
        return float(str(value).replace(",", "").replace("건", "").strip() or "0") == 0
    except (TypeError, ValueError):
        return False


class NotionRetentionWorker:
    """Run one bounded retention pass across the configured databases."""

    def __init__(
        self,
        *,
        token: str | None = None,
        database_env_names: Sequence[str] = DATABASE_ENV_NAMES,
        retention_days: int | None = None,
        batch_size: int | None = None,
        request_delay_seconds: float | None = None,
        enabled: bool | None = None,
        archive_duplicates: bool | None = None,
        max_archives: int | None = None,
        opener: Any | None = None,
    ) -> None:
        self.token = (token or os.getenv("NOTION_TOKEN", "")).strip()
        self.database_env_names = tuple(database_env_names)
        self.retention_days = retention_days or _env_int(
            "NOTION_RETENTION_DAYS", 7, minimum=1, maximum=3650
        )
        self.batch_size = batch_size or _env_int(
            "NOTION_RETENTION_BATCH_SIZE", 250, minimum=1, maximum=500
        )
        try:
            configured_delay = float(
                os.getenv("NOTION_RETENTION_REQUEST_DELAY_SECONDS", "0.35")
            )
        except (TypeError, ValueError):
            configured_delay = 0.35
        self.request_delay_seconds = max(
            0.0,
            configured_delay if request_delay_seconds is None else request_delay_seconds,
        )
        self.enabled = (
            _env_bool("NOTION_RETENTION_ENABLED", True)
            if enabled is None
            else bool(enabled)
        )
        self.archive_duplicates = (
            _env_bool("NOTION_RETENTION_DEDUPE_EXACT", True)
            if archive_duplicates is None
            else bool(archive_duplicates)
        )
        self.max_archives = max_archives or _env_int(
            "NOTION_RETENTION_MAX_ARCHIVES", 100, minimum=1, maximum=500
        )
        self.opener = opener or urllib.request.urlopen

    @classmethod
    def from_env(cls) -> "NotionRetentionWorker":
        return cls()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{NOTION_API}/{path}",
            data=data,
            headers=self._headers(),
            method=method,
        )
        for attempt in range(4):
            try:
                with self.opener(request, timeout=20) as response:
                    raw = response.read()
                decoded = json.loads(raw) if raw else {}
                if not isinstance(decoded, Mapping):
                    raise RuntimeError("notion_non_object_response")
                return decoded
            except urllib.error.HTTPError as exc:
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt >= 3:
                        raise
                    retry_after = exc.headers.get("Retry-After", "1")
                    try:
                        delay = min(10.0, max(0.5, float(retry_after)))
                    except (TypeError, ValueError):
                        delay = min(10.0, 0.5 * (2**attempt))
                    time.sleep(delay)
                    continue
                raise
            except urllib.error.URLError:
                if attempt >= 3:
                    raise
                time.sleep(min(10.0, 0.5 * (2**attempt)))
        raise RuntimeError("notion_request_retry_exhausted")

    def _database_ids(self) -> list[tuple[str, str]]:
        seen: set[str] = set()
        values: list[tuple[str, str]] = []
        for env_name in self.database_env_names:
            database_id = os.getenv(env_name, "").strip()
            if not database_id or database_id in seen:
                continue
            seen.add(database_id)
            values.append((env_name, database_id))
        return values

    def _query_database(self, env_name: str, database_id: str) -> list[NotionPage]:
        pages: list[NotionPage] = []
        cursor: str | None = None
        while True:
            remaining = self.batch_size - len(pages)
            if remaining <= 0:
                break
            payload: dict[str, Any] = {"page_size": min(100, remaining)}
            if cursor:
                payload["start_cursor"] = cursor
            response = self._request("POST", f"databases/{database_id}/query", payload)
            results = response.get("results", [])
            if isinstance(results, Sequence) and not isinstance(results, (str, bytes)):
                for raw in results:
                    if not isinstance(raw, Mapping):
                        continue
                    properties = raw.get("properties")
                    if not isinstance(properties, Mapping):
                        properties = {}
                    values = {
                        str(name): _property_text(prop)
                        for name, prop in properties.items()
                        if isinstance(prop, Mapping)
                    }
                    title = next(
                        (
                            value
                            for name, value in values.items()
                            if str(properties.get(name, {}).get("type")) == "title"
                        ),
                        "",
                    )
                    replay_id = next(
                        (
                            value
                            for name, value in values.items()
                            if str(name).lower() in {"risk_request_id", "replay_id"}
                            and value
                        ),
                        "",
                    )
                    input_hash = _property_value(
                        values, "input_hash", "source_input_hash"
                    )
                    if not replay_id:
                        replay_id = _property_value(
                            values, "risk_request_id", "replay_id"
                        )
                    page_id = str(raw.get("id") or "")
                    pages.append(
                        NotionPage(
                            page_id=page_id,
                            database_id=database_id,
                            database_env=env_name,
                            created_at=_parse_time(raw.get("created_time")),
                            title=title,
                            input_hash=input_hash,
                            replay_id=replay_id,
                            last_edited_at=_parse_time(raw.get("last_edited_time")),
                            status=_property_value(values, "status", "상태", "처리 상태"),
                            projection_marker=_property_value(
                                values,
                                "projection_key",
                                "projection_marker",
                                "task_id",
                                "root_task_id",
                                "synthesis_task_id",
                                "trace_id",
                                "trade_case_id",
                                "risk_plan_id",
                            ),
                            category=_property_value(values, "category", "구분"),
                            approval_count=_property_value(
                                values, "approval_count", "승인 대기"
                            ),
                            in_progress_count=_property_value(
                                values, "in_progress_count", "진행 중"
                            ),
                            blocker_count=_property_value(
                                values, "blocker_count", "차단·오류"
                            ),
                        )
                    )
                    if len(pages) >= self.batch_size:
                        break
                if len(pages) >= self.batch_size:
                    break
            if not response.get("has_more"):
                break
            cursor = str(response.get("next_cursor") or "") or None
            if not cursor:
                break
            time.sleep(self.request_delay_seconds)
        return pages

    def _archive(self, page: NotionPage, *, dry_run: bool) -> bool:
        if dry_run:
            return True
        self._request("PATCH", f"pages/{page.page_id}", {"archived": True})
        time.sleep(self.request_delay_seconds)
        return True

    def run_once(
        self,
        *,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> RetentionSummary:
        if not self.enabled:
            return RetentionSummary(enabled=False, available=False, error_code="DISABLED")
        if not self.token:
            return RetentionSummary(enabled=True, available=False, error_code="NOTION_TOKEN_MISSING")

        current = now or datetime.now(timezone.utc)
        cutoff = current - timedelta(days=self.retention_days)
        scanned = archived = old_archived = duplicate_archived = skipped = 0
        limit_reached = False
        try:
            for env_name, database_id in self._database_ids():
                pages = self._query_database(env_name, database_id)
                scanned += len(pages)
                duplicate_keep: dict[str, NotionPage] = {}
                if self.archive_duplicates:
                    for page in pages:
                        key = page.exact_replay_key
                        if not key:
                            continue
                        previous = duplicate_keep.get(key)
                        if previous is None or (
                            page.activity_at or datetime.min.replace(tzinfo=timezone.utc)
                        ) > (previous.activity_at or datetime.min.replace(tzinfo=timezone.utc)):
                            duplicate_keep[key] = page
                for page in pages:
                    if not page.page_id:
                        skipped += 1
                        continue
                    is_old = page.activity_at is not None and page.activity_at < cutoff
                    is_duplicate = bool(
                        self.archive_duplicates
                        and page.retention_key
                        and duplicate_keep.get(page.retention_key) is not page
                    )
                    if not (is_old or is_duplicate):
                        continue
                    # Legacy/unrelated pages can exist in a configured database.
                    # The CEO report schema has no projection key, so its
                    # structured terminal counters are an explicit identity
                    # substitute. Explicit active/unknown statuses are still
                    # protected because this worker does not inspect page blocks.
                    identifiable = bool(
                        page.retention_key or page.resolved_ceo_briefing
                    )
                    if not identifiable or not page.terminal:
                        skipped += 1
                        continue
                    if archived >= self.max_archives:
                        limit_reached = True
                        skipped += 1
                        continue
                    self._archive(page, dry_run=dry_run)
                    archived += 1
                    old_archived += int(is_old)
                    duplicate_archived += int(is_duplicate)
                    LOG.info(
                        "notion-retention archived database=%s reason=%s",
                        env_name,
                        "old+duplicate" if is_old and is_duplicate else "old" if is_old else "duplicate",
                    )
            LOG.info(
                "notion-retention enabled=true dry_run=%s scanned=%d archived=%d "
                "old_archived=%d duplicate_archived=%d",
                str(bool(dry_run)).lower(),
                scanned,
                archived,
                old_archived,
                duplicate_archived,
            )
            if limit_reached:
                LOG.warning(
                    "notion-retention archive-limit-reached limit=%d", self.max_archives
                )
            return RetentionSummary(
                enabled=True,
                available=True,
                scanned=scanned,
                archived=archived,
                old_archived=old_archived,
                duplicate_archived=duplicate_archived,
                skipped=skipped,
                limit_reached=limit_reached,
            )
        except Exception as exc:  # maintenance must not stop the app plane
            LOG.warning("notion-retention failed error=%s", type(exc).__name__)
            return RetentionSummary(
                enabled=True,
                available=False,
                scanned=scanned,
                archived=archived,
                old_archived=old_archived,
                duplicate_archived=duplicate_archived,
                skipped=skipped,
                limit_reached=limit_reached,
                error_code=type(exc).__name__,
            )


def _run_worker(worker: NotionRetentionWorker, *, interval: int, once: bool, dry_run: bool) -> None:
    stop = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    import signal

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop:
        worker.run_once(dry_run=dry_run)
        if once:
            return
        for _ in range(max(1, interval)):
            if stop:
                return
            time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=_env_int("NOTION_RETENTION_INTERVAL_SECONDS", 86400))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    _run_worker(NotionRetentionWorker.from_env(), interval=max(1, args.interval), once=args.once, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
