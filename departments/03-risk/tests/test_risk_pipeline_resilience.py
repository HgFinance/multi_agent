"""Risk department pipeline safety, reporting, and HR handoff contracts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

RISK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RISK_DIR))

_SPEC = importlib.util.spec_from_file_location(
    "risk_pipeline_scripts", RISK_DIR / "scripts.py"
)
assert _SPEC and _SPEC.loader
risk_scripts = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(risk_scripts)
import notion_reporter
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
    out = risk_scripts.supervise(
        {
            "assessment": {
                "verdict": "approve",
                "reason_codes": [],
                "check_results": [],
            },
            "order_intent": {},
            "trading_state": "ENABLED",
        }
    )
    assert out["verdict"] == "approve"
    assert out["escalate"] is True
    assert out["fallbacks"][0]["stage"] == "supervisor"
    assert out["supervisor_call_status"] == "failed"
    assembled = risk_scripts._assemble_out(
        {
            "assessment": {
                "verdict": "approve",
                "risk_request_id": "r1",
                "approved_quantity": None,
                "reason_codes": [],
                "check_results": [],
                "calculation_version": "v1",
                "input_hash": "h1",
            },
            "order_intent": {},
            "context": {},
            "trading_state": "ENABLED",
            **out,
        }
    )
    assert assembled["agent_execution"]["failed"] == ["risk-supervisor"]
    assert "risk-supervisor" not in assembled["agent_execution"]["executed"]


def test_hermes_model_is_loaded_from_risk_profile_config():
    model = risk_scripts._hermes_model_config()
    assert model["provider"] == "openai-codex"
    assert model["model"] == "gpt-5.6-luna"


def test_pipeline_build_failure_returns_reject_report(monkeypatch):
    monkeypatch.setattr(
        risk_scripts,
        "build_pipeline",
        lambda: (_ for _ in ()).throw(RuntimeError("graph")),
    )
    out = risk_scripts.run_risk_department({}, {})
    assert out["verdict"] == "reject"
    assert out["trading_state"] == "HALTED"
    assert out["decision_status"] == "INCONCLUSIVE"
    assert out["decision_origin"] == "FALLBACK"
    assert out["safe_action"] == "HOLD"
    assert out["failure"]["node"] == "pipeline"
    assert out["failure"]["error_message"] == "graph"
    assert out["evaluation"]["fallback_count"] >= 1
    assert out["report_markdown"]
    assert "비바인딩 fallback" in out["report_markdown"]


def test_fallback_preserves_node_and_redacts_error_message():
    def broken(_state):
        raise RuntimeError("redis_url=redis://default:super-secret@example:6379")

    guarded = risk_scripts._guard_node("pre_trade_check", broken)
    with pytest.raises(risk_scripts.RiskPipelineNodeError) as caught:
        guarded({})

    details = risk_scripts._failure_details("pipeline", caught.value)
    assert details["node"] == "pre_trade_check"
    assert details["error"] == "RuntimeError"
    assert "super-secret" not in details["error_message"]
    assert "redis_url=[REDACTED]" in details["error_message"]
    assert len(details["error_fingerprint"]) == 16


def test_reject_supervisor_uses_deterministic_fast_path(monkeypatch):
    def unexpected_llm(*_args, **_kwargs):
        raise AssertionError("reject path must not call supervisor LLM")

    monkeypatch.setattr(risk_scripts, "_hermes_chat", unexpected_llm)
    out = risk_scripts.supervise(
        {
            "assessment": {
                "verdict": "reject",
                "reason_codes": ["position_cap"],
                "check_results": [],
            },
            "order_intent": {},
            "trading_state": "ENABLED",
        }
    )
    assert out["verdict"] == "reject"
    assert out["escalate"] is False
    assert out["supervisor_llm_called"] is False
    assert "position_cap" in out["narrative"]


def test_reject_counterparty_uses_deterministic_fast_path(monkeypatch):
    def unexpected_llm(*_args, **_kwargs):
        raise AssertionError("reject path must not call counterparty LLM")

    monkeypatch.setattr(risk_scripts, "_hermes_chat", unexpected_llm)
    out = risk_scripts.counterparty_check(
        {
            "assessment": {
                "verdict": "reject",
                "reason_codes": ["counterparty_unhealthy"],
                "check_results": [
                    {
                        "name": "counterparty_health",
                        "passed": False,
                        "detail": "broker DOWN",
                    }
                ],
            },
            "order_intent": {},
            "trading_state": "ENABLED",
        }
    )
    assert out["counterparty"]["escalate"] is True
    assert out["counterparty_llm_called"] is False
    assert "broker DOWN" in out["counterparty"]["counterparty_narrative"]


def test_pipeline_fallback_emits_replayable_execution_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(
        risk_scripts,
        "build_pipeline",
        lambda: (_ for _ in ()).throw(RuntimeError("graph")),
    )
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


def test_notion_schema_lookup_is_cached_for_repeated_cases(monkeypatch):
    schema_reads = 0

    def fake_get(path, token):
        nonlocal schema_reads
        assert path == "databases/db1"
        assert token == "tok"
        schema_reads += 1
        return 200, {"properties": {"제목": {"type": "title"}}}

    def fake_post(path, body, token):
        assert token == "tok"
        if path.endswith("/query"):
            return 200, {"results": []}
        return 200, {"id": "page-1", "url": "https://notion.so/page-1"}

    monkeypatch.setattr(
        notion_reporter,
        "_SCHEMA_CACHE",
        notion_reporter.BoundedNotionSchemaCache(ttl_seconds=60, max_entries=8),
    )
    monkeypatch.setattr(notion_reporter, "_get", fake_get)
    monkeypatch.setattr(notion_reporter, "_post", fake_post)

    base = {
        "verdict": "approve",
        "approved_quantity": None,
        "reason_codes": [],
        "check_results": [],
        "calculation_version": "v1",
        "input_hash": "h1",
        "trading_state": "ENABLED",
        "escalate": False,
        "narrative": "n",
        "counterparty": None,
        "compliance": None,
    }
    first = dict(base, risk_request_id="r1")
    second = dict(base, risk_request_id="r2")

    env = {"NOTION_TOKEN": "tok", "NOTION_RISK_DB": "db1"}
    assert notion_reporter.upload_case({}, {}, first, env=env)["ok"]
    assert notion_reporter.upload_case({}, {}, second, env=env)["ok"]
    assert schema_reads == 1


def test_markdown_table_escapes_untrusted_values():
    out = {
        "risk_request_id": "r1",
        "verdict": "reject",
        "approved_quantity": None,
        "calculation_version": "v1",
        "input_hash": "h1",
        "trading_state": "HALTED",
        "escalate": True,
        "reason_codes": ["bad|value"],
        "check_results": [
            {"name": "test|check", "passed": False, "detail": "line one\nline two"}
        ],
        "counterparty": None,
        "compliance": None,
        "narrative": "fallback | narrative",
        "observability": {"trace_id": "t1", "langsmith": {"enabled": False}},
        "fallbacks": [
            {"stage": "supervisor", "error": "TimeoutError", "action": "ESCALATE"}
        ],
        "evaluation": {"fallback_count": 1},
    }
    report = risk_scripts._render_report_md(
        {"side": "BUY", "quantity": "1", "instrument_id": "A|B"}, {}, out
    )
    assert "test\\|check" in report
    assert "line one<br>line two" in report
    assert "## 평가 지표" in report
    assert "## Fallback / Escalation" in report
