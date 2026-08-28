from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER = ROOT / "departments" / "01-research" / "api" / "mcp_server.py"


def _load_mcp_server():
    spec = importlib.util.spec_from_file_location(
        "research_mcp_holdings_evidence_test", MCP_SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_holdings_evidence_is_on_demand_and_price_only_uses_market_api(
        monkeypatch) -> None:
    # Another department owns an ``evidence`` namespace too.  Research MCP must
    # resolve its own package regardless of collection/import order.
    foreign_evidence = ModuleType("evidence")
    foreign_evidence.__path__ = []
    monkeypatch.setitem(sys.modules, "evidence", foreign_evidence)
    monkeypatch.setenv("MARKET_API_URL", "http://127.0.0.1:8036")
    mcp = _load_mcp_server()
    calls: list[tuple[str, dict]] = []
    external = ModuleType("external_sources")

    def news_search(**kwargs):
        calls.append(("news", kwargs))
        return {
            "citation": "news-snapshot",
            "searched_at": "2026-08-18T10:00:00+09:00",
            "items": [{
                "title": "삼성전자 공급 계약",
                "originallink": "https://news.example/item",
                "link": "https://search.example/item",
                "pubDate": "Tue, 18 Aug 2026 09:00:00 +0900",
            }],
        }

    def dart_search_disclosures(**kwargs):
        calls.append(("disclosures", kwargs))
        return {
            "citation": "dart-snapshot",
            "items": [{
                "rcept_no": "20260818000123",
                "report_nm": "단일판매ㆍ공급계약체결",
                "viewer_url": "https://dart.example/20260818000123",
                "rcept_dt": "20260818",
            }],
        }

    external.news_search = news_search
    external.dart_search_disclosures = dart_search_disclosures
    monkeypatch.setitem(sys.modules, "external_sources", external)

    market_urls: list[str] = []

    def market_get(url: str):
        market_urls.append(url)
        assert "/evidence/news" not in url
        assert "/evidence/disclosures" not in url
        raise RuntimeError("market fixture unavailable")

    evidence = mcp.gather_holdings_evidence("005930", get=market_get)

    assert calls == [
        ("news", {"query": "005930", "display": 10, "sort": "date"}),
        ("disclosures", {"corp": "005930", "days": 7, "page": 1}),
    ]
    # 시세성 조회는 **market-api 로만** 나간다. 2026-08-25 에 /levels 가
    # 추가돼 호출이 2건이 됐다 - 개수가 아니라 "어디로 나가는가"가 계약이다.
    assert len(market_urls) == 2, market_urls
    assert all(u.startswith("http://127.0.0.1:8036/") for u in market_urls), market_urls
    assert any("/bars/005930" in u for u in market_urls), market_urls
    assert any("/levels/005930" in u for u in market_urls), market_urls
    assert evidence["news_headlines"][0]["evidence_id"] == (
        "mcp:news_search:news-snapshot:item-1")
    assert evidence["disclosures_7d"][0]["evidence_id"] == (
        "mcp:dart_search_disclosures:dart-snapshot:20260818000123")
    assert evidence["news_headlines"][0]["observed_at"] == (
        "2026-08-18T10:00:00+09:00")
    assert evidence["disclosures_7d"][0]["observed_at"]
    assert evidence["sources"]["news"]["mode"] == "ON_DEMAND_MCP"
    assert evidence["sources"]["disclosures"]["mode"] == "ON_DEMAND_MCP"
    assert evidence["sources"]["price_context"]["status"] == "UNAVAILABLE"
    # 레벨 조회가 실패해도 근거 묶음이 죽지 않고 사유가 남는다
    assert evidence["sources"]["price_levels"]["status"] == "FAILED"
    assert "price_levels" not in evidence


def test_holdings_sources_fail_independently_and_merged_prompt_stays_bounded() -> None:
    mcp = _load_mcp_server()

    def failed_news(**_kwargs):
        raise RuntimeError("news unavailable")

    def disclosures(**_kwargs):
        return {
            "citation": "d" * 16,
            "items": [{
                "rcept_no": f"receipt-{i}",
                "report_nm": "공시" * 100,
                "viewer_url": "https://dart.example/" + "x" * 200,
                "rcept_dt": "20260818",
            } for i in range(10)],
        }

    evidence = mcp.gather_holdings_evidence(
        "005930", get=lambda _url: (_ for _ in ()).throw(RuntimeError("market down")),
        search_news=failed_news, search_disclosures=disclosures)

    assert evidence["sources"]["news"]["status"] == "FAILED"
    assert evidence["sources"]["disclosures"]["status"] == "OK"
    assert len(evidence["disclosures_7d"]) == 5

    merged = mcp.merge_holdings_evidence(
        {"holding_question": "질문" * 1000, "news": "사용자 메모" * 1000,
         "portfolio_state": "보유 상태" * 1000}, evidence)
    assert mcp._EVIDENCE_CHAR_BUDGET >= len(json.dumps(
        merged, ensure_ascii=False, sort_keys=True, default=str))
    assert merged["news"]["source_status"]["news"]["status"] == "FAILED"


def test_news_only_holdings_evidence_skips_market_reads() -> None:
    mcp = _load_mcp_server()
    calls: list[str] = []

    def market_get(url: str):
        calls.append(url)
        raise AssertionError("news-only evidence must not read market data")

    evidence = mcp.gather_holdings_evidence(
        "005930",
        get=market_get,
        search_news=lambda **_kwargs: {
            "citation": "news-only",
            "items": [{"title": "headline"}],
        },
        search_disclosures=lambda **_kwargs: {
            "citation": "disclosure-only",
            "items": [],
        },
        include_price=False,
    )

    assert calls == []
    assert evidence["sources"]["news"]["status"] == "OK"
    assert evidence["sources"]["disclosures"]["status"] == "OK"
    assert "price_levels" not in evidence
    assert "price_context" not in evidence
