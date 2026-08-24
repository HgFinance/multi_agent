"""Atomic-ish claim coordination for Notion projections.

Notion's ``pages`` endpoint does not provide an idempotency-key primitive.  A
query-then-create pair is therefore racy when two workers finish the same
projection at once.  This module adds a Redis ``SET NX`` claim (with a
process-local lock as a zero-dependency fallback for isolated tests) around
that pair.  A completed claim is retained for a bounded TTL, so a retry can
return the already-created page without posting again.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from hashlib import sha256
from threading import Lock
from typing import Any
from uuid import uuid4


class NotionIdempotencyError(RuntimeError):
    """The projection cannot safely determine whether it owns the create."""


class NotionIdempotencyResult:
    def __init__(self, *, duplicate: bool, page_id: str | None = None) -> None:
        self.duplicate = duplicate
        self.page_id = page_id


_LOCKS_GUARD = Lock()
_LOCAL_LOCKS: dict[str, Lock] = {}


def _local_lock(key: str) -> Lock:
    with _LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, Lock())


def _page_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, Mapping):
        raw = value.get("id")
        return str(raw) if raw else None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            found = _page_id(item)
            if found:
                return found
    return None


def _claim_key(namespace: str, database_id: str, projection_key: str) -> str:
    digest = sha256(
        f"{namespace}\0{database_id}\0{projection_key}".encode("utf-8")
    ).hexdigest()
    return f"hgfinance:notion:idempotency:{namespace}:{digest}"


class _Claim(AbstractContextManager["_Claim"]):
    def __init__(
        self,
        owner: "NotionIdempotency",
        key: str,
        lookup: Callable[[], Any],
    ) -> None:
        self.owner = owner
        self.key = key
        self.lookup = lookup
        self.local_lock = _local_lock(key)
        self.redis: Any | None = None
        self.token = f"in-progress:{uuid4()}"
        self.duplicate = False
        self.page_id: str | None = None
        self._redis_owned = False

    def __enter__(self) -> "_Claim":
        self.local_lock.acquire()
        try:
            self.redis = self.owner._redis()
            if self.redis is None:
                return self
            self._acquire_distributed()
            return self
        except Exception:
            self.local_lock.release()
            raise

    def _acquire_distributed(self) -> None:
        assert self.redis is not None
        deadline = time.monotonic() + self.owner.wait_seconds
        while True:
            current = self.redis.get(self.key)
            if current:
                try:
                    state = json.loads(current)
                except (TypeError, ValueError):
                    state = {}
                if isinstance(state, Mapping) and state.get("state") == "done":
                    self.duplicate = True
                    self.page_id = str(state.get("page_id") or "") or None
                    return

                # The owner may have created the page but lost the response
                # before writing the done marker.  Re-query before waiting.
                found = self.lookup()
                found_id = _page_id(found)
                if found_id:
                    self._mark_done(found_id)
                    self.duplicate = True
                    self.page_id = found_id
                    return
                if time.monotonic() >= deadline:
                    raise NotionIdempotencyError("notion_projection_claim_in_progress")
                time.sleep(0.1)
                continue

            if self.redis.set(
                self.key,
                self.token,
                nx=True,
                ex=self.owner.lock_ttl_seconds,
            ):
                self._redis_owned = True
                return
            if time.monotonic() >= deadline:
                raise NotionIdempotencyError("notion_projection_claim_unavailable")
            time.sleep(0.05)

    def _mark_done(self, page_id: str | None) -> None:
        if self.redis is None:
            self.page_id = page_id
            return
        value = json.dumps({"state": "done", "page_id": page_id})
        self.redis.set(self.key, value, ex=self.owner.dedupe_ttl_seconds)
        self._redis_owned = False
        self.page_id = page_id

    def complete(self, page: Any) -> None:
        self._mark_done(_page_id(page))

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if exc_type is not None and self.redis is not None and self._redis_owned:
                # Never delete a claim that expired and was re-owned by a
                # different worker.
                self.redis.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('del', KEYS[1]) else return 0 end",
                    1,
                    self.key,
                    self.token,
                )
        finally:
            self.local_lock.release()
        return None


class NotionIdempotency:
    """Coordinate one query/create pair per database and projection key."""

    def __init__(self, env: Mapping[str, str] | None = None, *, namespace: str) -> None:
        self.env = env if env is not None else os.environ
        self.namespace = namespace
        self.lock_ttl_seconds = max(
            10,
            int(self.env.get("NOTION_IDEMPOTENCY_LOCK_TTL_SECONDS", "120")),
        )
        self.dedupe_ttl_seconds = max(
            self.lock_ttl_seconds,
            int(self.env.get("NOTION_IDEMPOTENCY_TTL_SECONDS", "604800")),
        )
        self.wait_seconds = max(
            0.5,
            min(float(self.env.get("NOTION_IDEMPOTENCY_WAIT_SECONDS", "10")), 30.0),
        )

    def _redis(self) -> Any | None:
        url = str(
            self.env.get("NOTION_IDEMPOTENCY_REDIS_URL")
            or self.env.get("REDIS_URL")
            or self.env.get("UI_MIRROR_REDIS_URL")
            or ""
        ).strip()
        if not url:
            return None
        try:
            import redis

            client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=3.0,
            )
            client.ping()
            return client
        except Exception as exc:  # noqa: BLE001 - fail closed for a write path
            raise NotionIdempotencyError(
                f"notion_projection_idempotency_store_unavailable:{type(exc).__name__}"
            ) from exc

    def claim(
        self,
        database_id: str,
        projection_key: str,
        *,
        lookup: Callable[[], Any],
    ) -> AbstractContextManager[_Claim]:
        return _Claim(
            self,
            _claim_key(self.namespace, database_id, projection_key),
            lookup,
        )

    def execute(
        self,
        database_id: str,
        projection_key: str,
        *,
        lookup: Callable[[], Any],
        create: Callable[[], Any],
    ) -> NotionIdempotencyResult:
        with self.claim(database_id, projection_key, lookup=lookup) as claim:
            if claim.duplicate:
                return NotionIdempotencyResult(duplicate=True, page_id=claim.page_id)
            existing = lookup()
            existing_id = _page_id(existing)
            if existing_id:
                claim.complete(existing_id)
                return NotionIdempotencyResult(duplicate=True, page_id=existing_id)
            page = create()
            claim.complete(page)
            return NotionIdempotencyResult(
                duplicate=False,
                page_id=_page_id(page),
            )
