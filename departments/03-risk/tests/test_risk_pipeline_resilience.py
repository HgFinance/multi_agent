"""Risk department pipeline safety, reporting, and HR handoff contracts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

RISK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RISK_DIR))

_SPEC = importlib.util.spec_from_file_location("risk_pipeline_scripts", RISK_DIR / "scripts.py")
assert _SPEC and _SPEC.loader
risk_scripts = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(risk_scripts)
from notion_reporter import _rich_text


def test_missing_redis_is_halted_fallback(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    out = risk_scripts.check_trading_state({"scope": "fund:test"})
    assert out["trading_state"] == "HALTED"
    assert out["fallbacks"][0]["action"] == "ESCALATE"


def test_supervisor_failure_preserves_binding_verdict(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise TimeoutError("Hermes unavailable")

    monkeypatch.setattr(risk_scripts, "_hermes_chat", unavailable)
    out = risk_scripts.supervise({
        "assessment": {"verdict": "approve", "reason_codes": [], "check_results": []},
        "order_intent": {}, "trading_state": "ENABLED",
    })
    assert out["verdict"] == "approve"
    assert out["escalate"] is True
    assert out["fallbacks"][0]["stage"] == "supervisor"


def test_pipeline_build_failure_returns_reject_report(monkeypatch):
    monkeypatch.setattr(risk_scripts, "build_pipeline", lambda: (_ for _ in ()).throw(RuntimeError("graph")))
    out = risk_scripts.run_risk_department({}, {})
    assert out["verdict"] == "reject"
    assert out["trading_state"] == "HALTED"
    assert out["evaluation"]["fallback_count"] >= 1
    assert out["report_markdown"]


def test_pipeline_fallback_emits_replayable_execution_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(risk_scripts, "build_pipeline", lambda: (_ for _ in ()).throw(RuntimeError("graph")))
    log_path = tmp_path / "risk-run.jsonl"

    out = risk_scripts.run_risk_department(
        {},
        {},
        run_id="risk-run-test",
        log_path=log_path,
    )

    evidence = out["execution_evidence"]
    assert evidence["run_id"] == "risk-run-test"
    assert evidence["pipeline_status"] == "DEGRADED"
    assert evidence["safe_action"] == "HOLD"
    assert evidence["binding"] is False
    assert evidence["replay_ready"] is True
    assert len(log_path.read_text(encoding="utf-8").splitlines()) >= 4


def test_notion_report_keeps_full_markdown_as_chunks():
    markdown = "# Risk\n\n" + ("| claim | value |\n|---|---|\n" * 250)
    payload = _rich_text(markdown)
    chunks = payload["rich_text"]
    assert "".join(chunk["text"]["content"] for chunk in chunks) == markdown
    assert all(len(chunk["text"]["content"]) <= 1900 for chunk in chunks)


def test_markdown_table_escapes_untrusted_values():
    out = {
        "risk_request_id": "r1", "verdict": "reject", "approved_quantity": None,
        "calculation_version": "v1", "input_hash": "h1", "trading_state": "HALTED",
        "escalate": True, "reason_codes": ["bad|value"], "check_results": [
            {"name": "test|check", "passed": False, "detail": "line one\nline two"}],
        "counterparty": None, "compliance": None, "narrative": "fallback | narrative",
        "observability": {"trace_id": "t1", "langsmith": {"enabled": False}},
        "fallbacks": [{"stage": "supervisor", "error": "TimeoutError", "action": "ESCALATE"}],
        "evaluation": {"fallback_count": 1},
    }
    report = risk_scripts._render_report_md({"side": "BUY", "quantity": "1", "instrument_id": "A|B"}, {}, out)
    assert "test\\|check" in report
    assert "line one<br>line two" in report
    assert "## 평가 지표" in report
    assert "## Fallback / Escalation" in report
