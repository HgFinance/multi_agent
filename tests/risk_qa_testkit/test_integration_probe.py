from __future__ import annotations

from departments.risk_qa_testkit import integration


def test_research_health_without_packet_contract_is_partial(monkeypatch) -> None:
    monkeypatch.setattr(integration, "_http_json", lambda url: {"status": "ok"})

    result = integration._check_research_api(
        {"RESEARCH_API_URL": "http://research.test"}
    )

    assert result["health"] == "READY"
    assert result["packet_contract"] == "NOT_CONFIGURED"
    assert result["status"] == "PARTIAL"


def test_probe_does_not_promote_partial_research_to_ready(monkeypatch) -> None:
    monkeypatch.setattr(integration, "_http_json", lambda url: {"status": "ok"})
    monkeypatch.setattr(
        integration,
        "_check_redis",
        lambda environ: {"configured": True, "status": "READY"},
    )
    monkeypatch.setattr(
        integration,
        "_check_supabase_event",
        lambda environ: {"configured": True, "status": "READY"},
    )

    report = integration.run_external_integration_probe(
        {"RESEARCH_API_URL": "http://research.test"}
    )

    assert report["research_api"]["status"] == "PARTIAL"
    assert report["status"] == "PARTIAL"
