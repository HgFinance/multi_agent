from __future__ import annotations

import io
import urllib.error
from email.message import Message

import pytest

from orchestration.adapters.notion_http import NotionHttpError, request_json


class _Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def read(self) -> bytes:
        import json

        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _http_error(status: int, payload: bytes = b'{"code":"temporary"}') -> urllib.error.HTTPError:
    headers = Message()
    return urllib.error.HTTPError(
        "https://api.notion.com/v1/test",
        status,
        "temporary",
        headers,
        io.BytesIO(payload),
    )


def test_query_retries_transient_failure_with_bounded_backoff(monkeypatch) -> None:
    calls = 0
    sleeps: list[float] = []

    def opener(_request, timeout):
        nonlocal calls
        assert timeout == 10.0
        calls += 1
        if calls < 3:
            raise _http_error(503)
        return _Response({"results": []})

    monkeypatch.setattr(
        "orchestration.adapters.notion_http.time.sleep", sleeps.append
    )
    result = request_json(
        "POST",
        "databases/db/query",
        "token",
        body={"page_size": 1},
        opener=opener,
    )

    assert result == {"results": []}
    assert calls == 3
    assert sleeps == [0.25, 0.5]


def test_page_creation_is_not_retried_after_ambiguous_server_failure() -> None:
    calls = 0

    def opener(_request, timeout):
        nonlocal calls
        calls += 1
        raise _http_error(500)

    with pytest.raises(NotionHttpError) as caught:
        request_json("POST", "pages", "token", body={}, opener=opener)

    assert caught.value.status == 500
    assert calls == 1
