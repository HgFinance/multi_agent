"""Contract tests for the eight-department employee Worker layer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from orchestration.employee_dispatch import load_worker_specs, run_department_workers
from orchestration.workflows.manifest import load_workflow
from orchestration.workflows.runner import execute_workflow

ROOT = Path(__file__).resolve().parents[1]
DEPARTMENTS = (
    ("ceo", "00-ceo-office"),
    ("hr", "07-agent-workforce"),
    ("research", "01-research"),
    ("trading", "02-trading"),
    ("risk", "03-risk"),
    ("quant-backtest", "04-quant-backtest"),
    ("accounting-portfolio", "05-accounting-portfolio"),
    ("qa", "06-ai-qa-audit"),
)


def _read_profile(directory: str) -> str:
    """Read a department Hermes profile as UTF-8.

    ``Path.read_text()`` without an explicit encoding uses the platform default,
    which is cp949 on a Korean Windows install — every profile carries Korean
    comments, so the bare call raises UnicodeDecodeError there while passing on
    UTF-8 hosts.  The files are UTF-8 on every platform; say so.
    """

    return (ROOT / "departments" / directory / "hermes" / "config.yaml").read_text(
        encoding="utf-8"
    )


def _fake_worker_llm(_system: str, prompt: str) -> str:
    worker_id = next(
        (line.split(":", 1)[1].strip() for line in prompt.splitlines() if line.startswith("Worker id:")),
        "unknown-worker",
    )
    return json.dumps(
        {
            "summary": f"synthetic worker context for {worker_id}",
            "confidence": 0.8,
            "evidence_refs": ["test:evidence"],
            "escalate": False,
        }
    )


def _payload() -> dict[str, Any]:
    """Provide every optional trigger so the registry contract is exercised."""

    return {
        "case_request": {"case_id": "worker-contract-test", "symbol": "AAPL", "stage": "paper"},
        "research_packet": {"status": "COMPLETED"},
        "market_snapshot": {"price": "200.00"},
        "market_features": {"rsi": 50},
        "fundamentals": {"pe": 20},
        "news_or_macro": {"headline": "test"},
        "evidence_request": {"query": "test"},
        "order_book": {"spread": "0.01"},
        "price_history": [200.0],
        "filings": {"published_at": "2026-08-03"},
        "news": {"headline": "test"},
        "macro": {"regime": "normal"},
        "geopolitical": {"risk": "low"},
        "evidence": {"source": "test"},
        "documents": [{"id": "test"}],
        "order_intent": {"side": "BUY", "quantity": 1},
        "risk_decision": {"verdict": "APPROVE"},
        "portfolio_state": {"position": 0},
        "approved_risk": {"status": "APPROVE"},
        "execution_request": {"venue": "paper"},
        "derivatives_signal": {"enabled": True},
        "assessment": {"claim_checks": [{"result": "UNSUPPORTED"}]},
        "trading_state": {"cash": "100000"},
        "compliance": {"grounded": True},
        "counterparty": {"status": "OK"},
        "derivatives": {"enabled": True},
        "strategy_hypothesis": {"id": "strategy-test"},
        "dataset": {"snapshot": "dataset-test"},
        "backtest_request": {"id": "backtest-request"},
        "backtest": {"run_id": "backtest-test"},
        "release_candidate": {"id": "release-test"},
        "ml_research": {"model": "baseline"},
        "execution": {"cost": "0"},
        "cost_sensitivity": {"slippage": 0.0},
        "regime": {"label": "normal"},
        "regime_analysis": {"label": "normal"},
        "portfolio": {"nav": "100000"},
        "ledger": {"balanced": True},
        "nav": {"value": "100000"},
        "nav_close": {"status": "ready"},
        "treasury": {"cash": "100000"},
        "treasury_signal": {"status": "normal"},
        "pnl": {"value": "0"},
        "pnl_request": {"period": "test"},
        "reporting": {"period": "test"},
        "investor_report": {"period": "test"},
        "valuation": {"as_of": "2026-08-03"},
        "corporate_action": {"type": "none"},
        "fee_tax": {"status": "review"},
        "fee_accrual": {"period": "test"},
        "queue_metrics": {"depth": 1},
        "sla_metrics": {"breaches": 0},
        "cost_metrics": {"usd": 0},
        "profile": {"id": "profile-test"},
        "role_requirements": {"skills": []},
        "evaluation": {"score": 1},
        "performance_signal": {"score": 1},
        "lifecycle_event": {"type": "test"},
        "governance_request": {"id": "governance-test"},
        "model_risk": {"status": "PASS"},
        "internal_audit": {"status": "PASS"},
        "ops_assessment": {"status": "HEALTHY"},
        "permission_check": {"status": "ALLOWED"},
        "incident": {"incident_id": "incident-test"},
        "incident_events": [],
        "hallucination_reviews": [],
    }


def test_profile_worker_registry_counts_and_models() -> None:
    expected_counts = {"00-ceo-office": 1, "07-agent-workforce": 5, "01-research": 6, "02-trading": 7, "03-risk": 4, "04-quant-backtest": 7, "05-accounting-portfolio": 8, "06-ai-qa-audit": 5}
    for _, directory in DEPARTMENTS:
        config = yaml.safe_load(_read_profile(directory))
        workers = config["workers"]
        registry = config["staff_registry"]
        assert len(workers) == expected_counts[directory]
        assert registry["worker_count"] == expected_counts[directory]
        assert (
            config["employee_runtime"]["topology"]
            == "async_fan_out_fan_in_independent_graphs"
        )
        assert config["employee_runtime"]["model_default"] == "qwen3:1.7b"
        assert config["employee_runtime"]["model_selection"]["active_model"] == "qwen3:1.7b"
        assert config["employee_runtime"]["max_retries"] == 2
        assert config["employee_runtime"]["max_attempts"] == 3
        assert config["model"]["provider"] == "openai-codex"
        assert config["model"]["default"] == "gpt-5.6-luna"
        runtime_personalities = config["agent"].get("runtime_personalities") or registry["runtime_personalities"]
        assert set(runtime_personalities) == set(workers)


def test_all_registered_workers_are_independent_graphs() -> None:
    for department, _ in DEPARTMENTS:
        result = run_department_workers(ROOT, department, _payload(), llm=_fake_worker_llm)
        assert result["binding"] is False
        assert result["failed"] == []
        assert result["degraded"] is False
        assert result["not_executed"] == []
        assert result["runtime"]["executor"] == "LangGraph"
        assert (
            result["runtime"]["topology"]
            == "async_fan_out_fan_in_independent_graphs"
        )
        assert result["runtime"]["provider"] == "ollama"
        assert result["runtime"]["model"] == "qwen3:1.7b"


def test_profile_worker_metadata_matches_runtime_specs() -> None:
    """Prevent config registry drift from the executable WorkerSpec registry."""

    for department, directory in DEPARTMENTS:
        config = yaml.safe_load(_read_profile(directory))
        runtime_specs = {
            spec.worker_id: spec for spec in load_worker_specs(ROOT, department)
        }
        configured_workers = config["workers"]
        assert set(configured_workers) == set(runtime_specs)

        for worker_id, spec in runtime_specs.items():
            entry = configured_workers[worker_id]
            assert entry["role"] == spec.role
            assert entry["trigger"] == spec.trigger
            assert tuple(entry["tools"]) == spec.tools
            expected_status = "active" if spec.trigger == "always" else "conditional"
            assert entry["status"] == expected_status


def test_worker_failure_is_degraded_and_non_binding() -> None:
    def failing_llm(_system: str, _prompt: str) -> str:
        raise TimeoutError("synthetic worker timeout")

    result = run_department_workers(ROOT, "risk", _payload(), llm=failing_llm)
    assert result["binding"] is False
    assert result["degraded"] is True
    assert result["failed"]
    assert all(item["status"] == "DEGRADED" for item in result["workers"])


def test_paper_pipeline_passes_worker_context_to_department_head(monkeypatch: Any) -> None:
    from orchestration.adapters import paper_pipeline

    dispatched: list[str] = []

    def worker_result(department: str) -> dict[str, Any]:
        return {
            "department": department,
            "status": "COMPLETED",
            "binding": False,
        "runtime": {"executor": "LangGraph", "provider": "ollama", "model": "qwen3:1.7b"},
            "executed": [f"{department}-worker"],
            "failed": [],
            "not_executed": [],
            "input_hash": "paper-test-hash",
        }

    def fake_dispatch(_repo_root: Path, department: str, _payload: dict[str, Any]) -> dict[str, Any]:
        dispatched.append(department)
        return worker_result(department)

    class FakeCeoAdapter:
        def decide(self, *, case_request: dict[str, Any], department_reports: dict[str, Any]) -> dict[str, Any]:
            assert case_request["symbol"] == "AAPL"
            for department in ("research", "trading", "risk", "qa", "accounting"):
                assert department_reports[department]["employee_context"] is not None
            return {
                "recommendation": "HOLD",
                "model_recommendation": "HOLD",
                "confidence": 1.0,
                "rationale": "synthetic paper handoff",
                "escalate": True,
                "binding_decision": "HOLD / ESCALATE",
                "binding": False,
                "runtime": {"profile": "ceo-agent", "call_status": "succeeded"},
            }

    monkeypatch.setattr(paper_pipeline, "run_department_workers", fake_dispatch)
    adapter = paper_pipeline.PaperPipelineAdapter(
        ROOT,
        research_runner=lambda _symbol: {"status": "COMPLETED", "employee_execution": {"workers": []}},
        risk_runner=lambda *_args, **_kwargs: {
            "verdict": "reject",
            "employee_workers": worker_result("risk"),
        },
        qa_runner=lambda *_args, **_kwargs: {
            "verdict": "FAIL",
            "employee_workers": worker_result("qa"),
        },
        ceo_adapter=FakeCeoAdapter(),
    )
    case = {
        "case_id": "paper-worker-handoff",
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 1,
        "order_type": "LIMIT",
        "limit_price": "200.00",
        "stage": "paper",
    }
    run = execute_workflow(
        load_workflow("investment-case"),
        mode="paper",
        handlers=adapter.handlers(),
        context={"case_request": case},
        run_id="paper-worker-handoff",
    )
    assert run.status == "COMPLETED"
    assert dispatched == ["research", "trading", "accounting-portfolio", "ceo"]

    for workflow_name in ("strategy-research", "workforce-management", "agent-evolution"):
        auxiliary_run = execute_workflow(
            load_workflow(workflow_name),
            mode="paper",
            handlers=adapter.handlers(),
            context={"case_request": case},
            run_id=f"{workflow_name}-paper-test",
        )
        assert auxiliary_run.status == "COMPLETED"


def test_final_worker_shape_has_no_duplicate_roles() -> None:
    """Keep the approved head/worker topology explicit and reviewable."""
    expected = {
        "ceo": (1, 1, 0),
        "hr": (5, 2, 3),
        "research": (6, 2, 4),
        "trading": (7, 3, 4),
        "risk": (4, 2, 2),
        "quant-backtest": (7, 2, 5),
        "accounting-portfolio": (8, 2, 6),
        "qa": (5, 1, 4),
    }
    for department, directory in DEPARTMENTS:
        config = yaml.safe_load(_read_profile(directory))
        workers = config["workers"]
        active = sum(item["trigger"] == "always" for item in workers.values())
        conditional = len(workers) - active
        assert (len(workers), active, conditional) == expected[department]
        assert len({item["role"] for item in workers.values()}) == len(workers)
