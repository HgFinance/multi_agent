from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.pinecone_client import (
    RISK_POLICY_NAMESPACE,
    PineconeConfigurationError,
    PineconeEvidenceClient,
)


def test_risk_client_forces_risk_namespace_and_filters_stale_metadata() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "matches": [
                    {
                        "id": "current",
                        "score": 0.91,
                        "metadata": {
                            "chunk_id": "chunk-current",
                            "document_id": "policy-001",
                            "version": "v2",
                            "clause_id": "12",
                            "effective_from": "2026-01-01",
                            "effective_to": None,
                            "text": "Current policy",
                        },
                    },
                    {
                        "id": "stale",
                        "score": 0.99,
                        "metadata": {
                            "chunk_id": "chunk-stale",
                            "document_id": "policy-001",
                            "version": "v1",
                            "effective_from": "2025-01-01",
                            "effective_to": "2025-12-31",
                        },
                    },
                ]
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with PineconeEvidenceClient(
        api_key="test-key",
        index_host="https://pinecone.example",
        client=http_client,
    ) as client:
        matches = client.query([0.1, 0.2], as_of=date(2026, 8, 7))

    assert [match["id"] for match in matches] == ["current"]
    assert requests[0].url.path == "/query"
    assert requests[0].content
    assert requests[0].headers["Api-Key"] == "test-key"
    assert requests[0].read().decode()  # request body is present but not logged
    assert RISK_POLICY_NAMESPACE in requests[0].content.decode()


def test_risk_client_rejects_missing_configuration() -> None:
    with (
        PineconeEvidenceClient(api_key="", index_host="") as client,
        pytest.raises(PineconeConfigurationError),
    ):
        client.query([0.1], as_of="2026-08-07")
