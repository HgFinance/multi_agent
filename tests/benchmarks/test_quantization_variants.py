import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.quantization.glossary_rag import inject, load_glossary
from benchmarks.quantization.reasoning_critic import rewrite
from benchmarks.quantization.render_variant_comparison import render
from benchmarks.quantization.variant_manifest import admit_manifest, external_gate


class _Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


class QuantizationVariantTests(unittest.TestCase):
    def test_manifest_holds_unverified_source(self):
        decision, errors = admit_manifest({"variant": "awq-finetune", "status": "candidate"})
        self.assertEqual(decision, "HOLD")
        self.assertTrue(errors)

    def test_finetune_holds_nf4_or_unknown_base(self):
        manifest = {
            "variant": "awq-finetune", "status": "validated", "source_hashes": ["abc"],
            "held_out_overlap": False,
            "checks": {name: True for name in ("contamination", "license", "format", "duplicates", "purpose_fit")},
            "adapter_compatibility": {"base_model": "qwen", "base_revision": "r1", "quantization": "nf4"},
            "served_base": {"base_model": "qwen", "base_revision": "r1", "quantization": "awq"},
        }
        decision, errors = admit_manifest(manifest)
        self.assertEqual(decision, "HOLD")
        self.assertIn("adapter/base mismatch", errors[0])

    def test_manifest_admits_exact_awq_adapter(self):
        manifest = {
            "variant": "awq-finetune", "status": "validated", "source_hashes": ["abc"],
            "held_out_overlap": False,
            "checks": {name: True for name in ("contamination", "license", "format", "duplicates", "purpose_fit")},
            "adapter_compatibility": {"base_model": "qwen", "base_revision": "r1", "quantization": "awq"},
            "served_base": {"base_model": "qwen", "base_revision": "r1", "quantization": "awq"},
        }
        self.assertEqual(admit_manifest(manifest), ("ADMIT", []))

    def test_external_gate_uses_overall_and_keeps_missing_as_hold(self):
        self.assertEqual(external_gate(overall_accuracy=0.74, fp8_overall_accuracy=0.74, critical_failures=0, new_critical_regressions=0, request_errors=0), ("PASS", []))
        status, errors = external_gate(overall_accuracy=None, fp8_overall_accuracy=0.74, critical_failures=0, new_critical_regressions=0, request_errors=0)
        self.assertEqual(status, "HOLD")
        self.assertIn("overall_accuracy", errors[0])

    def test_renderer_reports_hold_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps({"status": "HOLD", "manifest": {"name": "x", "version": "1", "sha256": "abc"}}))
            output = render({"AWQ+Finetune": path})
            self.assertIn("AWQ+Finetune", output)
            self.assertIn("HOLD: missing frozen Overall baseline", output)
            self.assertIn("x@1#abc", output)

    def test_glossary_rejects_risk_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "glossary.json"
            path.write_text(json.dumps([{"term": "LIMIT", "definition": "x", "scope": "risk"}]))
            with self.assertRaises(ValueError):
                load_glossary(path)

    def test_glossary_injection_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "glossary.json"
            path.write_text(json.dumps([{"term": "ROE", "definition": "net income / equity", "scope": "financial_arithmetic"}]))
            _, entries = load_glossary(path)
            injected, hits = inject("Calculate ROE", entries)
            self.assertEqual(hits, ["ROE"])
            self.assertIn("net income / equity", injected)

    @patch("benchmarks.quantization.reasoning_critic.request.urlopen", side_effect=TimeoutError("timeout"))
    def test_reasoning_retries_and_holds(self, _urlopen):
        result = rewrite(url="http://127.0.0.1:1", api_key="test", model="gpt-4o-mini", question="q", draft="d", max_retries=2)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["retry_count"], 2)
        self.assertEqual(result["primary_result"], "HOLD")

    @patch("benchmarks.quantization.reasoning_critic.request.urlopen")
    def test_reasoning_budget_overrun_holds(self, urlopen):
        urlopen.return_value = _Response({"choices": [{"message": {"content": "answer"}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 1000}})
        result = rewrite(url="http://example", api_key="test", model="gpt-4o-mini", question="q", draft="d", budget_usd=0.0001, input_cost_per_million=1, output_cost_per_million=1)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["primary_result"], "HOLD")
        self.assertEqual(result["usage"]["prompt_tokens"], 1000)

    def test_frozen_hash_constant(self):
        path = Path("benchmarks/quantization/internal50_v2_reasoning.json")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), "ad2bdaf5ea381c2fc151fce1f1859f7f925b86fd03b830319cd97af17709e978")


if __name__ == "__main__":
    unittest.main()
