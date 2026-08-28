from __future__ import annotations

from pathlib import Path
import sys
import urllib.error

import pytest


COLLECTORS_DIR = Path(__file__).resolve().parents[2] / "departments/01-research/collectors"
if str(COLLECTORS_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTORS_DIR))

from ls_client import LsApiError, LsEnvironment, LsRestClient  # noqa: E402


def _client() -> LsRestClient:
    return LsRestClient(
        LsEnvironment(
            name="PAPER",
            app_key="key",
            app_secret="secret",
            rest_base_url="https://example.test",
        ),
        timeout=7,
    )


def test_transport_normalizes_direct_socket_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*_args: object, **_kwargs: object):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", timeout)

    with pytest.raises(LsApiError, match=r"/stock/high-item timeout"):
        _client()._post("/stock/high-item", data=b"{}", headers={})


def test_transport_normalizes_urlerror_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*_args: object, **_kwargs: object):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr("urllib.request.urlopen", timeout)

    with pytest.raises(LsApiError, match=r"/stock/high-item timeout"):
        _client()._post("/stock/high-item", data=b"{}", headers={})
