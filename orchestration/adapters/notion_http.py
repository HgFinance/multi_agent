"""Small, bounded HTTP client shared by the Notion projections.

Notion does not expose an idempotency key for page creation or block append.
Those write operations are therefore deliberately not retried after an
ambiguous transport failure.  Read requests, database queries, and idempotent
page/block updates may be retried with a short backoff for transient rate-limit
and server failures.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from email.message import Message
from typing import Any


class NotionHttpError(RuntimeError):
    """A Notion request failed after its bounded retry policy."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


NOTION_MAX_CHILDREN = 100


def notion_children_chunks(
    children: Sequence[Mapping[str, Any]],
    *,
    limit: int = NOTION_MAX_CHILDREN,
) -> list[list[Mapping[str, Any]]]:
    """Split page children at Notion's request limit."""

    if limit <= 0:
        raise ValueError("Notion children limit must be positive")
    return [
        list(children[index : index + limit])
        for index in range(0, len(children), limit)
    ]


def notion_block_signature(block: Mapping[str, Any]) -> str:
    """Return a stable signature that ignores Notion-assigned block metadata."""

    block_type = str(block.get("type") or "").strip()
    if block_type:
        comparable: Mapping[str, Any] = {
            "type": block_type,
            block_type: block.get(block_type),
        }
    else:
        comparable = {
            key: value
            for key, value in block.items()
            if key not in {"id", "object", "parent", "created_time", "last_edited_time"}
        }
    return json.dumps(
        comparable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def missing_notion_block_suffix(
    existing: Sequence[Mapping[str, Any]],
    desired: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return the desired suffix not already present at the page tail.

    This makes a retry after an ambiguous append response safe when Notion's
    read-after-write view includes the blocks that were actually appended.
    """

    if not desired:
        return []
    existing_signatures = [notion_block_signature(block) for block in existing]
    desired_signatures = [notion_block_signature(block) for block in desired]
    max_overlap = min(len(existing_signatures), len(desired_signatures))
    for overlap in range(max_overlap, 0, -1):
        if existing_signatures[-overlap:] == desired_signatures[:overlap]:
            return list(desired[overlap:])
    return list(desired)


def _response_detail(response: Any) -> Any:
    try:
        raw = response.read()
    except (OSError, TypeError, AttributeError):
        return str(response)
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)


def _retry_after(headers: Message | Mapping[str, Any] | None) -> float | None:
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), 5.0))
    except (TypeError, ValueError):
        return None


def _retryable_request(method: str, path: str) -> bool:
    """Return whether repeating this request cannot create duplicate content."""

    method = method.upper()
    if method in {"GET", "HEAD", "OPTIONS", "DELETE"}:
        return True
    if method == "POST" and path.rstrip("/").endswith("/query"):
        return True
    # Updating a page or deleting one block is idempotent.  Appending children
    # is not: a timeout after a successful append must not append them again.
    return method == "PATCH" and not path.rstrip("/").endswith("/children")


def request_json(
    method: str,
    path: str,
    token: str,
    *,
    body: Mapping[str, Any] | None = None,
    version: str = "2022-06-28",
    timeout_seconds: float = 10.0,
    max_attempts: int = 3,
    opener: Callable[..., Any] | None = None,
) -> Mapping[str, Any]:
    """Issue a Notion JSON request with bounded, write-safe retry behavior."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    max_attempts = max(1, min(int(max_attempts), 3))
    method = method.upper()
    data = None if body is None else json.dumps(body).encode("utf-8")
    can_retry = _retryable_request(method, path)
    open_url = opener or urllib.request.urlopen

    for attempt in range(max_attempts):
        request = urllib.request.Request(
            f"https://api.notion.com/v1/{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with open_url(request, timeout=timeout_seconds) as response:
                decoded = _response_detail(response)
                status = int(getattr(response, "status", 200))
            if not isinstance(decoded, Mapping):
                raise NotionHttpError(
                    "Notion returned a non-object response",
                    status=status,
                    detail=decoded,
                )
            return decoded
        except urllib.error.HTTPError as exc:
            detail = _response_detail(exc)
            transient = exc.code == 429 or 500 <= exc.code < 600
            if can_retry and transient and attempt + 1 < max_attempts:
                delay = _retry_after(exc.headers)
                if delay is None:
                    delay = min(1.5, 0.25 * (2**attempt))
                time.sleep(delay)
                continue
            raise NotionHttpError(
                str(detail), status=exc.code, detail=detail
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if can_retry and attempt + 1 < max_attempts:
                time.sleep(min(1.5, 0.25 * (2**attempt)))
                continue
            raise NotionHttpError(str(exc), detail=str(exc)) from exc

    raise AssertionError("unreachable Notion retry loop")


def request_json_status(
    method: str,
    path: str,
    token: str,
    *,
    body: Mapping[str, Any] | None = None,
    version: str = "2022-06-28",
    timeout_seconds: float = 10.0,
    max_attempts: int = 3,
) -> tuple[int, dict[str, Any]]:
    """Keep the legacy ``(status, body)`` reporter contract on shared HTTP."""

    try:
        response = request_json(
            method,
            path,
            token,
            body=body,
            version=version,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
    except NotionHttpError as exc:
        detail = exc.detail
        if isinstance(detail, Mapping):
            return int(exc.status or 599), dict(detail)
        return int(exc.status or 599), {"message": str(detail or exc)}
    return 200, dict(response)


__all__ = [
    "NOTION_MAX_CHILDREN",
    "NotionHttpError",
    "missing_notion_block_suffix",
    "notion_block_signature",
    "notion_children_chunks",
    "request_json",
    "request_json_status",
]
