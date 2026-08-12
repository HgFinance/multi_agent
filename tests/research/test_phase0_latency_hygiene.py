"""Phase 0 Research fetch/cache/observability contracts."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

RESEARCH_ROOT = Path(__file__).resolve().parents[2] / "departments" / "01-research"
if str(RESEARCH_ROOT) not in sys.path:
    sys.path.insert(0, str(RESEARCH_ROOT))

from agents.article_reader import ArticleReader  # noqa: E402
from evidence.api_client import get_json  # noqa: E402
from evidence.bundle import _dedup_source_records, evidence_index  # noqa: E402
from evidence.cache import EvidenceCache, activate_cache, canonical_url  # noqa: E402
from evidence.handoff import (  # noqa: E402
    build_evidence_handoff,
    reusable_evidence_refs,
)
from evidence.llm_client import chat  # noqa: E402
from evidence.observability import ResearchRunMetrics, activate_metrics  # noqa: E402


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        import json

        return json.dumps(self.payload).encode("utf-8")


class Phase0LatencyHygieneTest(unittest.TestCase):
    def test_same_api_url_fetches_once_and_hits_task_cache(self) -> None:
        calls: list[str] = []
        metrics = ResearchRunMetrics(trace_id="trace-cache")
        cache = EvidenceCache(metrics=metrics)

        def opener(request: object, timeout: int) -> _Response:
            calls.append(str(request))
            return _Response({"document_id": "doc-1", "value": 42})

        with activate_metrics(metrics), activate_cache(cache):
            first = get_json(
                "https://research.test/evidence?b=2&a=1",
                persona="research-worker",
                opener=opener,
            )
            second = get_json(
                "https://research.test/evidence?a=1&b=2",
                persona="research-worker",
            )

        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(metrics.network_fetch_count, 1)
        self.assertEqual(metrics.cache_hit_count, 1)
        self.assertEqual(metrics.duplicate_evidence_avoided_count, 1)
        self.assertEqual(canonical_url("https://Research.Test/evidence?b=2&a=1"),
                         "https://research.test/evidence?a=1&b=2")

    def test_article_reader_reuses_normalized_evidence(self) -> None:
        calls: list[str] = []
        metrics = ResearchRunMetrics(trace_id="trace-article")
        cache = EvidenceCache(metrics=metrics)

        def fetch(url: str) -> str:
            calls.append(url)
            return "<html><body><p>" + ("삼성전자 영업이익 개선과 현금흐름 점검 자료 " * 8) + "</p></body></html>"

        reader = ArticleReader(fetch=fetch, robots_ok=lambda _url: True, clock=lambda: 0.0)
        with activate_metrics(metrics), activate_cache(cache):
            first = reader.read("https://news.test/article?b=2&a=1")
            second = reader.read("https://news.test/article?a=1&b=2")

        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(metrics.network_fetch_count, 1)
        self.assertEqual(metrics.cache_hit_count, 1)

    def test_handoff_exposes_reusable_provenance_for_risk_and_qa(self) -> None:
        handoff = build_evidence_handoff(
            {
                "news_headlines": [
                    {
                        "ref": "n1",
                        "evidence_id": "doc-1",
                        "title": "IR release",
                        "canonical_url": "https://ir.test/release/1",
                        "content_hash": "sha256:" + "a" * 64,
                        "fetched_at": "2026-08-11T00:00:00+00:00",
                    }
                ],
                "disclosures_7d": [],
            },
            trace_id="trace-handoff",
            as_of="2026-08-11T00:00:00+00:00",
        )

        self.assertEqual(handoff["evidence_refs"], ["doc-1"])
        self.assertEqual(reusable_evidence_refs(handoff), ("doc-1",))
        self.assertEqual(handoff["schema_version"], "research.evidence-handoff.v1")

    def test_handoff_derives_record_hash_when_api_has_no_raw_body_hash(self) -> None:
        handoff = build_evidence_handoff(
            {
                "news_headlines": [
                    {
                        "evidence_id": "doc-1",
                        "title": "IR release",
                        "url": "https://ir.test/release/1",
                        "observed_at": "2026-08-11T00:00:00+00:00",
                    }
                ],
                "disclosures_7d": [],
            }
        )
        provenance = handoff["provenance"][0]
        self.assertTrue(provenance["content_hash"].startswith("sha256:"))
        self.assertEqual(reusable_evidence_refs(handoff), ("doc-1",))

        self.assertEqual(
            reusable_evidence_refs(
                handoff,
                as_of="2026-08-12T00:00:00+00:00",
                freshness_seconds=3600,
            ),
            (),
        )
        handoff["provenance"][0]["fetch_status"] = "corrupt"
        self.assertEqual(reusable_evidence_refs(handoff), ())

    def test_citation_index_deduplicates_same_canonical_source(self) -> None:
        records = _dedup_source_records(
            [
                {"document_id": "doc-1", "url": "https://ir.test/1"},
                {"document_id": "doc-2", "url": "https://IR.TEST/1#fragment"},
            ]
        )
        self.assertEqual([record["document_id"] for record in records], ["doc-1"])

        index = evidence_index(
            {
                "news_headlines": [
                    {"ref": "n1", "evidence_id": "doc-1", "canonical_url": "https://ir.test/1"},
                    {"ref": "n2", "evidence_id": "doc-2", "canonical_url": "https://IR.TEST/1#fragment"},
                ],
                "disclosures_7d": [],
            }
        )
        self.assertEqual(index["n1"]["evidence_id"], "doc-1")
        self.assertEqual(index["n2"]["evidence_id"], "doc-1")

    def test_dispatch_wait_is_measured_without_changing_dispatcher_interval(self) -> None:
        queued = datetime(2026, 8, 11, tzinfo=timezone.utc)
        claimed = queued + timedelta(seconds=7)
        metrics = ResearchRunMetrics(trace_id="trace-dispatch")
        metrics.mark("queued_at", queued)
        metrics.mark("claimed_at", claimed)
        metrics.mark("research_started_at", claimed)
        metrics.finish(completed_at=claimed + timedelta(seconds=1))

        payload = metrics.as_dict(status="COMPLETED")
        self.assertEqual(payload["dispatch_wait_ms"], 7000)
        self.assertEqual(payload["total_duration_ms"], 1000)

    def test_llm_duration_and_generation_timestamps_are_recorded(self) -> None:
        metrics = ResearchRunMetrics(trace_id="trace-llm")
        response = _Response({"choices": [{"message": {"content": "{}"}}]})
        with patch("evidence.llm_client.urllib.request.urlopen", return_value=response):
            with activate_metrics(metrics):
                self.assertEqual(
                    chat("system", "user", base="http://ollama.test", model="model", timeout=1),
                    "{}",
                )
        self.assertIsNotNone(metrics.generation_started_at)
        self.assertIsNotNone(metrics.generation_finished_at)
        self.assertGreaterEqual(metrics.llm_duration_ms, 0)


if __name__ == "__main__":
    unittest.main()
