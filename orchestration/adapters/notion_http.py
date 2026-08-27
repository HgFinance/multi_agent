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
from collections.abc import Callable, Mapping
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


__all__ = ["NotionHttpError", "request_json"]
