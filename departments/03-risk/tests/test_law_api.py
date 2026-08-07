from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.law_api import (
    LawApiClient,
    LawApiConfig,
    LawApiConfigurationError,
    credential_status,
)


def test_law_api_config_reads_env_without_exposing_oc() -> None:
    config = LawApiConfig.from_env(
        {
            "LAW_API_ENABLED": "true",
            "LAW_API_BASE_URL": "https://example.test/",
            "LAW_API_OC": "secret-oc",
            "LAW_API_TIMEOUT_SECONDS": "7",
        }
    )

    assert config.enabled is True
    assert config.base_url == "https://example.test"
    assert config.timeout_seconds == 7
    status = credential_status({"LAW_API_ENABLED": "true", "LAW_API_OC": "secret-oc"})
    assert status == {
        "enabled": True,
        "configured": True,
        "secret_values_exposed": False,
    }
    assert "secret-oc" not in str(status)


def test_enabled_law_api_requires_oc() -> None:
    with pytest.raises(LawApiConfigurationError):
        LawApiConfig.from_env({"LAW_API_ENABLED": "true"})


def test_search_and_document_requests_are_read_only_json_calls() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/DRF/lawSearch.do":
            return httpx.Response(200, json={"LawSearch": {"totalCnt": "1"}})
        if request.url.path == "/DRF/lawService.do":
            return httpx.Response(200, json={"법령": {"법령명_한글": "테스트"}})
        return httpx.Response(404)

    config = LawApiConfig(
        enabled=True,
        base_url="https://example.test",
        oc="secret-oc",
    )
    with LawApiClient(
        config,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ) as client:
        search = client.search_laws("자본시장법")
        document = client.get_law("TEST-ID")

    assert search["LawSearch"]["totalCnt"] == "1"
    assert document["법령"]["법령명_한글"] == "테스트"
    assert [request.method for request in requests] == ["GET", "GET"]
    assert all(request.url.path.startswith("/DRF/") for request in requests)
    assert all(request.url.params["type"] == "JSON" for request in requests)
    assert all(request.url.params["OC"] == "secret-oc" for request in requests)
