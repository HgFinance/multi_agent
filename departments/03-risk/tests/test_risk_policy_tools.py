from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.pinecone_client import PineconeEvidenceClient
from tools.policy_tools import (
    RiskPolicyQueryInput,
    query_pinecone_risk_policy,
)


def test_risk_policy_tool_returns_contract_metadata() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "matches": [
                    {
                        "id": "match-1",
                        "score": 0.88,
                        "metadata": {
                            "chunk_id": "chunk-1",
                            "document_id": "policy-001",
                            "version": "v3",
                            "clause_id": "12-3",
                            "effective_from": "2026-01-01",
                            "effective_to": None,
                            "authority": "INTERNAL",
                            "document_type": "CONCENTRATION_POLICY",
                            "title": "Single issuer limit",
                            "source_url": "https://example.test/policy-001",
                            "text": "Single issuer exposure must remain below the mandate limit.",
                        },
                    }
                ]
            },
        )

    with PineconeEvidenceClient(
        api_key="test-key",
        index_host="https://pinecone.example",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ) as client:
        result = query_pinecone_risk_policy(
            RiskPolicyQueryInput(
                mandate_id="MND-1",
                query="single issuer limit",
                query_vector=[0.1, 0.2],
                as_of=date(2026, 8, 7),
            ),
            client=client,
        )

    assert result.status == "OK"
    assert result.namespace == "risk-compliance-policy"
    assert result.matches[0].metadata.document_id == "policy-001"
    assert result.matches[0].metadata.clause_id == "12-3"


def test_risk_policy_tool_escalates_without_vector_or_embedder() -> None:
    result = query_pinecone_risk_policy(
        RiskPolicyQueryInput(
            query="single issuer limit",
            as_of=date(2026, 8, 7),
        )
    )

    assert result.status == "INCONCLUSIVE"
    assert result.error_code == "EMBEDDING_PROVIDER_NOT_CONFIGURED"
