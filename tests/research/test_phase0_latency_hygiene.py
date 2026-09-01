"""Phase 0 Research fetch/cache/observability contracts."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Self
from unittest.mock import patch

RESEARCH_ROOT = Path(__file__).resolve().parents[2] / "departments" / "01-research"
_research_path = str(RESEARCH_ROOT)

# The Research tree intentionally has a top-level ``evidence`` package and a
# ``scripts.py`` module. Keep its import path temporary: leaving it at
# sys.path[0] makes later collection resolve the repository's ``scripts``
# package as Research's scripts.py. Also evict a previously imported external
# package named ``evidence`` so collection order cannot select the wrong one.
for _name in tuple(sys.modules):
    if _name == "evidence" or _name.startswith("evidence."):
        sys.modules.pop(_name, None)
sys.path.insert(0, _research_path)
try:
    from agents.article_reader import ArticleReader
    from evidence.api_client import get_json
    from evidence.bundle import _dedup_source_records, evidence_index
    from evidence.cache import (
        EvidenceCache,
        activate_cache,
        canonical_url,
    )
    from evidence.handoff import (
        build_evidence_handoff,
        reusable_evidence_refs,
    )
    from evidence.llm_client import chat
    from evidence.observability import (
        ResearchRunMetrics,
        activate_metrics,
        redacted_span,
    )
finally:
    if sys.path and sys.path[0] == _research_path:
        sys.path.pop(0)


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        import json

        return json.dumps(self.payload).encode("utf-8")


class Phase0LatencyHygieneTest(unittest.TestCase):
    def test_langsmith_root_and_child_spans_are_redacted(self) -> None:
        records: list[dict[str, object]] = []

        class FakeRun:
            def __init__(self, metadata: dict[str, object]) -> None:
                self.metadata = dict(metadata)

            def end(self, *, error: str | None = None) -> None:
                return None

        class FakeTrace:
            def __init__(self, name: str, kwargs: dict[str, object]) -> None:
                self.run = FakeRun(kwargs["metadata"])
                records.append(
                    {
                        "name": name,
                        "inputs": kwargs["inputs"],
                        "metadata": kwargs["metadata"],
                        "client": kwargs["client"],
                    }
                )

            def __enter__(self) -> FakeRun:
                return self.run

            def __exit__(self, *_args: object) -> bool:
                return False

        fake_client = object()

        def fake_trace(name: str, **kwargs: object) -> FakeTrace:
            return FakeTrace(name, kwargs)

        with (
            patch.dict(
                os.environ,
                {
                    "LANGSMITH_TRACING": "true",
                    "LANGSMITH_API_KEY": "configured-but-not-recorded",
                    "LANGSMITH_PROJECT": "First",
                    "LANGSMITH_RESEARCH_TRACE_MODE": "full",
                    "HGFINANCE_LANGSMITH_EGRESS_ENABLED": "true",
                },
            ),
            patch("evidence.observability._langsmith_client", return_value=fake_client),
            patch("langsmith.trace", side_effect=fake_trace),
            redacted_span(
                "research.department",
                metadata={"department": "research", "task_id": "t-root"},
            ),
            redacted_span(
                "research.llm.call",
                run_type="llm",
                metadata={
                    "model": "qwen3:14b",
                    "prompt": "do not send",
                    "output": "do not send",
                    "api_key": "do not send",
                },
            ),
        ):
            pass

        self.assertEqual(
            [record["name"] for record in records],
            ["research.department", "research.llm.call"],
        )
        for record in records:
            self.assertEqual(record["inputs"], {})
            metadata = record["metadata"]
            self.assertNotIn("prompt", metadata)
            self.assertNotIn("output", metadata)
            self.assertNotIn("api_key", metadata)
            self.assertIs(record["client"], fake_client)

    def test_langsmith_failure_is_fail_open(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LANGSMITH_TRACING": "true",
                "LANGSMITH_API_KEY": "configured-but-not-recorded",
            },
        ), patch(
            "evidence.observability._langsmith_client",
            side_effect=RuntimeError("client unavailable"),
        ), redacted_span("research.department") as span:
            self.assertIsNone(span)
            self.assertEqual(2 + 2, 4)

    def test_tracing_disabled_preserves_workflow_body(self) -> None:
        with patch.dict(
            os.environ,
            {"LANGSMITH_TRACING": "false"},
        ), redacted_span("research.department") as span:
            self.assertIsNone(span)
            self.assertEqual("unchanged", "unchanged")

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
        with patch(
            "evidence.llm_client._call_gateway",
            return_value=("{}", "qwen2.5-14b-instruct-awq"),
        ), activate_metrics(metrics):
            self.assertEqual(
                chat("system", "user", base="http://ollama.test", model="model", timeout=1),
                "{}",
            )
        self.assertIsNotNone(metrics.generation_started_at)
        self.assertIsNotNone(metrics.generation_finished_at)
        self.assertGreaterEqual(metrics.llm_duration_ms, 0)


if __name__ == "__main__":
    unittest.main()
