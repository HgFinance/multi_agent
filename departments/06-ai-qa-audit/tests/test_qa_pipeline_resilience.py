"""QA department pipeline safety, reporting, and HR handoff contracts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

QA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QA_DIR))

_SPEC = importlib.util.spec_from_file_location("qa_pipeline_scripts", QA_DIR / "scripts.py")
assert _SPEC and _SPEC.loader
qa_scripts = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(qa_scripts)
from notion_reporter import _rich_text


def test_invalid_input_becomes_fail_closed_assessment():
    out = qa_scripts.check_evidence({"artifact": {}, "evidence_store": {}, "decision_time": "invalid"})
    assert out["assessment"]["decision"] == "FAIL"
    assert out["assessment"]["reason_codes"] == ["pipeline_fallback"]
    assert out["fallbacks"][0]["action"] == "ESCALATE"


def test_ollama_failure_keeps_deterministic_claim_result(monkeypatch):
    def unavailable(_prompt):
        raise TimeoutError("Ollama unavailable")

    monkeypatch.setattr(qa_scripts, "_call_internal_llm", unavailable)
    out = qa_scripts.draft_claim_narrative({
        "assessment": {"decision": "FAIL", "claim_checks": [
            {"claim_index": 0, "result": "UNSUPPORTED", "reason": "근거 없음"}]}
    })
    assert "UNSUPPORTED" in out["claim_narrative"]
    assert out["fallbacks"][0]["stage"] == "claim_narrative"


def test_supervisor_failure_escalates_without_changing_qa_decision(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise TimeoutError("Hermes unavailable")

    monkeypatch.setattr(qa_scripts, "_hermes_chat", unavailable)
    out = qa_scripts.supervise({
        "assessment": {"decision": "FAIL", "reason_codes": [], "claim_checks": [], "findings": []},
        "claim_narrative": "deterministic summary",
    })
    assert out["verdict"] == "FAIL"
    assert out["escalate"] is True
    assert out["fallbacks"][0]["stage"] == "supervisor"


def test_pipeline_build_failure_returns_fail_report(monkeypatch):
    monkeypatch.setattr(qa_scripts, "build_pipeline", lambda: (_ for _ in ()).throw(RuntimeError("graph")))
    out = qa_scripts.run_qa_department({}, {}, "invalid")
    assert out["verdict"] == "FAIL"
    assert out["escalate"] is True
    assert out["evaluation"]["fallback_count"] >= 1
    assert out["report_markdown"]


def test_notion_report_keeps_full_markdown_as_chunks():
    markdown = "# QA\n\n" + ("| claim | result |\n|---|---|\n" * 250)
    payload = _rich_text(markdown)
    chunks = payload["rich_text"]
    assert "".join(chunk["text"]["content"] for chunk in chunks) == markdown
    assert all(len(chunk["text"]["content"]) <= 1900 for chunk in chunks)


def test_markdown_report_contains_metrics_and_observability():
    out = {
        "qa_decision_id": "q1", "verdict": "FAIL", "calculation_version": "v1", "input_hash": "h1",
        "escalate": True, "claim_checks": [{"claim_index": 0, "claim": "a|b", "result": "UNSUPPORTED",
                                               "reason": "line one\nline two"}],
        "findings": [{"finding_id": "f1", "finding_type": "x", "severity": "HIGH", "description": "d"}],
        "reason_codes": ["pipeline_fallback"], "claim_narrative": "n", "narrative": "n",
        "observability": {"trace_id": "t1", "langsmith": {"enabled": False}},
        "fallbacks": [{"stage": "supervisor", "error": "TimeoutError", "action": "ESCALATE"}],
        "evaluation": {"fallback_count": 1},
    }
    report = qa_scripts._render_report_md({"trace_id": "t1"}, "2026-08-02T00:00:00+00:00", out)
    assert "a\\|b" in report
    assert "line one<br>line two" in report
    assert "## 평가 지표" in report
    assert "## LangSmith / HR 관측성 전달" in report
