"""Read-only paper adapter for the complete investment-case handoff.

The adapter invokes the existing Research, Risk, and QA department entry
points when their runtime dependencies are available. Trading contract
creation, OMS/Fill, and Accounting are in-memory projections. Notion,
Postgres, Redis writes, broker submission, and ledger posting are disabled or
omitted. Any department failure becomes an explicit degraded report and the
CEO receives a safe HOLD/ESCALATE case summary.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, MutableMapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from ..employee_dispatch import run_department_workers
from .ceo import CeoAdapterError, LunaCeoAdapter

PaperHandler = Callable[[str, str, MutableMapping[str, object]], str]
DepartmentRunner = Callable[..., Mapping[str, object]]
WorkerLLM = Callable[[str, str], str]
_PAPER_NAMESPACE = UUID("d8ce7fb7-4b08-4e4f-8524-8a516a6f0b8d")

AUXILIARY_WORKER_DEPARTMENT = {
    # 2026-08-10 (재일): 퀀트 환류 단계. 기존 항목은 건드리지 않는다.
    "quant-outcome-feedback": "quant-backtest",
    "quant-backtest": "quant-backtest",
    "qa-release-review": "qa",
    "ceo-promotion-review": "ceo",
    "hr-profile": "hr",
    "hr-candidate-eval": "hr",
    "qa-permission-review": "qa",
    "ceo-org-approval": "ceo",
    "hr-lifecycle": "hr",
    "hr-improvement-signal": "hr",
    "hr-profile-revision": "hr",
    "qa-revision-review": "qa",
    "ceo-revision-approval": "ceo",
    "hr-shadow-rollback": "hr",
}


class PaperPipelineAdapter:
    """Build handlers that pass typed paper artifacts through all boundaries."""

    def __init__(
        self,
        repo_root: Path,
        *,
        research_runner: DepartmentRunner | None = None,
        risk_runner: DepartmentRunner | None = None,
        qa_runner: DepartmentRunner | None = None,
        ceo_adapter: LunaCeoAdapter | None = None,
        worker_llm: WorkerLLM | None = None,
    ) -> None:
        self.repo_root = repo_root
        self._research_runner = research_runner
        self._risk_runner = risk_runner
        self._qa_runner = qa_runner
        self._ceo_adapter = ceo_adapter or LunaCeoAdapter(repo_root)
        self._worker_llm = worker_llm
        self._log_dir = Path(tempfile.mkdtemp(prefix="hgfinance-paper-"))

    def handlers(self) -> dict[str, PaperHandler]:
        handlers: dict[str, PaperHandler] = {
            "research": self.research,
            "trading": self.trading,
            "risk": self.risk,
            "qa": self.qa,
            "oms-fill-gate": self.oms_fill_gate,
            "accounting": self.accounting,
            "ceo": self.ceo,
        }
        for step_id, department in AUXILIARY_WORKER_DEPARTMENT.items():
            handlers[step_id] = self._auxiliary_handler(department)
        return handlers

    def _auxiliary_handler(self, department: str) -> PaperHandler:
        def handler(
            input_contract: str,
            output_contract: str,
            context: MutableMapping[str, object],
        ) -> str:
            return self.auxiliary(department, input_contract, output_contract, context)

        return handler

    def auxiliary(
        self,
        department: str,
        input_contract: str,
        output_contract: str,
        context: MutableMapping[str, object],
    ) -> str:
        """Run Quant/HR governance-cycle Workers with no production side effect."""

        payload = {
            "case_request": context.get("case_request", {}),
            "input_contract": input_contract,
            "workflow_context": dict(context),
        }
        employee_context = self._run_employee_workers(department, payload, context)
        artifact = {
            "status": "PAPER_CONTEXT_ONLY",
            "producer": f"{department}-department",
            "input_contract": input_contract,
            "output_contract": output_contract,
            "binding": False,
            "production_promotion": False,
            "permission_grant": False,
            "employee_context": employee_context,
        }
        report = {
            "department": department,
            "status": "COMPLETED" if employee_context.get("status") == "COMPLETED" else "DEGRADED",
            "binding": False,
            "paper_only": True,
            "employee_context": employee_context,
        }
        _store(context, output_contract, artifact, report)
        return _detail(department, report, output_contract)

    def _run_employee_workers(
        self,
        department: str,
        payload: Mapping[str, object],
        context: MutableMapping[str, object],
    ) -> dict[str, object]:
        """Run non-binding Worker context and make it visible to the head."""

        try:
            if self._worker_llm is None:
                result = run_department_workers(self.repo_root, department, payload)
            else:
                result = run_department_workers(
                    self.repo_root,
                    department,
                    payload,
                    llm=self._worker_llm,
                )
        except Exception as exc:  # noqa: BLE001 - Worker boundary is fail-closed
            result = {
                "department": department,
                "status": "DEGRADED",
                "binding": False,
                "error": type(exc).__name__,
                "error_message": str(exc)[:240],
                "executed": [],
                "failed": ["employee_dispatch"],
                "not_executed": [],
            }
        context.setdefault("employee_context", {})[department] = result
        return result

    def research(
        self, _input_contract: str, _output_contract: str, context: MutableMapping[str, object]
    ) -> str:
        case = _case(context)
        employee_context = self._run_employee_workers(
            "research", {"case_request": case, "symbol": case["symbol"]}, context
        )
        report: dict[str, object]
        try:
            runner = self._research_runner or _default_research_runner(self.repo_root)
            with _disable_research_persistence(runner):
                raw = dict(runner(str(case["symbol"])))
            report = _department_summary("research", raw)
            packet_status = "COMPLETED" if report["status"] == "COMPLETED" else "DEGRADED"
        except Exception as exc:  # noqa: BLE001 - paper boundary stays fail-closed
            packet_status = "DEGRADED"
            report = _failure_report("research", exc, safe_action="HOLD")
            raw = {}
        packet = {
            "case_id": case["case_id"],
            "symbol": case["symbol"],
            "stage": "paper",
            "status": packet_status,
            "producer": "research-department",
            "summary": _summary_text(raw),
            "evidence_available": bool(raw),
            "employee_context": employee_context,
        }
        _store(context, "research_packet", packet, report)
        return _detail("research", report, "research_packet")

    def trading(
        self, _input_contract: str, _output_contract: str, context: MutableMapping[str, object]
    ) -> str:
        case = _case(context)
        self._run_employee_workers(
            "trading",
            {"case_request": case, "research_packet": _artifact(context, "research_packet")},
            context,
        )
        now = datetime.now(timezone.utc)
        instrument_id = uuid5(_PAPER_NAMESPACE, f"instrument:{case['symbol']}")
        fund_id = uuid5(_PAPER_NAMESPACE, "fund:paper")
        book_id = uuid5(_PAPER_NAMESPACE, "book:paper")
        strategy_id = uuid5(_PAPER_NAMESPACE, "strategy:paper")
        trade_case_id = uuid5(_PAPER_NAMESPACE, str(case["case_id"]))
        snapshot = {
            "market_snapshot_id": f"paper-snapshot-{case['symbol']}",
            "as_of": now.isoformat(),
            "bid": str(Decimal(str(case["limit_price"])) - Decimal("0.01")),
            "ask": str(case["limit_price"]),
            "quality": "ok",
        }
        order_intent = {
            "order_intent_id": str(uuid5(_PAPER_NAMESPACE, f"intent:{case['case_id']}")),
            "trade_case_id": str(trade_case_id),
            "fund_id": str(fund_id),
            "book_id": str(book_id),
            "strategy_id": str(strategy_id),
            "instrument_id": str(instrument_id),
            "side": case["side"],
            "order_type": case["order_type"],
            "quantity": str(case["quantity"]),
            "limit_price": str(case["limit_price"]),
            "time_in_force": "DAY",
            "valid_until": (now + timedelta(hours=1)).isoformat(),
            "snapshot": snapshot,
            "idempotency_key": f"paper-{case['case_id']}-intent",
            "schema_version": "trading-contracts-v1",
            "created_by": "paper-trading-adapter",
            "trace_id": str(uuid5(_PAPER_NAMESPACE, f"trace:{case['case_id']}")),
            "created_at": now.isoformat(),
        }
        report = {
            "status": "COMPLETED",
            "binding": False,
            "employees": ["trader-pm-agent"],
            "order_intent_id": order_intent["order_intent_id"],
            "paper_only": True,
        }
        _store(context, "order_intent", order_intent, report)
        return _detail("trading", report, "order_intent")

    def risk(
        self, _input_contract: str, _output_contract: str, context: MutableMapping[str, object]
    ) -> str:
        case = _case(context)
        order_intent = _artifact(context, "order_intent")
        risk_context = _risk_context(case, order_intent)
        try:
            runner = self._risk_runner or _default_risk_runner(self.repo_root)
            with _disable_notion(runner):
                raw = dict(
                    runner(
                        order_intent,
                        risk_context,
                        "fund:paper",
                        run_id=str(context.get("workflow_run_id", "paper-risk")),
                        log_path=self._log_dir / "risk.jsonl",
                    )
                )
            raw.setdefault("employee_context", raw.get("employee_workers", {}))
            report = _department_summary("risk", raw)
        except Exception as exc:  # noqa: BLE001 - reject on adapter failure
            raw = _risk_failure(exc)
            report = _failure_report("risk", exc, safe_action="HOLD")
        raw.setdefault("binding", False)
        _store(context, "risk_decision", raw, report)
        return _detail("risk", report, "risk_decision")

    def qa(
        self, _input_contract: str, _output_contract: str, context: MutableMapping[str, object]
    ) -> str:
        case = _case(context)
        risk_decision = _artifact(context, "risk_decision")
        artifact, evidence_store, decision_time = _qa_input(case, risk_decision)
        try:
            runner = self._qa_runner or _default_qa_runner(self.repo_root)
            with _disable_notion(runner):
                raw = dict(
                    runner(
                        artifact,
                        evidence_store,
                        decision_time,
                        run_id=str(context.get("workflow_run_id", "paper-qa")),
                        log_path=self._log_dir / "qa.jsonl",
                    )
                )
            raw.setdefault("employee_context", raw.get("employee_workers", {}))
            report = _department_summary("qa", raw)
        except Exception as exc:  # noqa: BLE001 - escalate on QA failure
            raw = _qa_failure(exc)
            report = _failure_report("qa", exc, safe_action="ESCALATE")
        raw.setdefault("binding", False)
        _store(context, "qa_assessment", raw, report)
        return _detail("qa", report, "qa_assessment")

    def oms_fill_gate(
        self, _input_contract: str, _output_contract: str, context: MutableMapping[str, object]
    ) -> str:
        risk = _artifact(context, "risk_decision")
        qa = _artifact(context, "qa_assessment")
        risk_verdict = str(risk.get("verdict", risk.get("candidate_verdict", "reject"))).lower()
        qa_verdict = str(qa.get("verdict", "FAIL")).upper()
        approved = risk_verdict in {"approve", "resize"} and qa_verdict in {"PASS", "WARN"}
        result = {
            "status": "PAPER_NOT_SUBMITTED" if approved else "BLOCKED",
            "submitted": False,
            "filled": False,
            "broker": "paper",
            "reason": "paper adapter never submits orders"
            if approved
            else "Risk or QA gate did not permit paper submission",
            "risk_verdict": risk_verdict,
            "qa_verdict": qa_verdict,
            "binding": False,
        }
        report = {
            "status": result["status"],
            "binding": False,
            "submitted": False,
            "filled": False,
            "safe_action": "HOLD",
        }
        _store(context, "execution_result", result, report)
        return _detail("oms-fill-gate", report, "execution_result")

    def accounting(
        self, _input_contract: str, _output_contract: str, context: MutableMapping[str, object]
    ) -> str:
        execution = _artifact(context, "execution_result")
        employee_context = self._run_employee_workers(
            "accounting-portfolio", {"execution_result": execution}, context
        )
        snapshot = {
            "status": "PAPER_NOT_POSTED",
            "position_delta": "0",
            "cash_delta": "0",
            "ledger_posted": False,
            "reconciled": False,
            "source_execution_status": execution.get("status"),
            "binding": False,
            "employee_context": employee_context,
        }
        report = {
            "status": "PAPER_NOT_POSTED",
            "binding": False,
            "ledger_posted": False,
            "reconciled": False,
        }
        _store(context, "accounting_snapshot", snapshot, report)
        return _detail("accounting", report, "accounting_snapshot")

    def ceo(
        self, _input_contract: str, _output_contract: str, context: MutableMapping[str, object]
    ) -> str:
        case = _case(context)
        reports = context.get("department_reports", {})
        if not isinstance(reports, Mapping):
            reports = {}
        reports = dict(reports)
        employee_context = self._run_employee_workers(
            "ceo",
            {
                "case_request": case,
                "department_reports": reports,
                "artifacts": dict(context.get("artifacts", {})),
            },
            context,
        )
        try:
            decision = self._ceo_adapter.decide(
                case_request=case,
                department_reports=reports,
            )
        except CeoAdapterError as exc:
            decision = {
                "recommendation": "HOLD",
                "binding_decision": "HOLD / ESCALATE",
                "binding": False,
                "escalate": True,
                "rationale": f"CEO Luna adapter unavailable: {exc}",
                "runtime": {"profile": "ceo-agent", "call_status": "failed"},
            }
            context.setdefault("domain_failures", []).append(f"ceo:{exc}")
        decision["employee_context"] = employee_context
        report = {
            "status": "COMPLETED" if decision.get("runtime", {}).get("call_status") == "succeeded" else "DEGRADED",
            "binding": False,
            "recommendation": decision.get("recommendation"),
            "binding_decision": decision.get("binding_decision"),
            "escalate": decision.get("escalate"),
            "runtime": decision.get("runtime"),
        }
        _store(context, "ceo_case_summary", decision, report)
        context["workflow_metadata"] = {
            "paper_domain_reports": dict(reports),
            "ceo_decision": decision,
            "external_writes": False,
            "orders_submitted": False,
            "ledger_posted": False,
        }
        return _detail("ceo", report, "ceo_case_summary")


def build_paper_handlers(repo_root: Path, **kwargs: object) -> dict[str, PaperHandler]:
    """Return complete paper handlers; dependency runners remain injectable."""

    return PaperPipelineAdapter(repo_root, **kwargs).handlers()


def _store(
    context: MutableMapping[str, object],
    contract: str,
    artifact: Mapping[str, object],
    report: Mapping[str, object],
) -> None:
    context.setdefault("artifacts", {})[contract] = dict(artifact)  # type: ignore[index]
    report_for_head = dict(report)
    employee_contexts = context.get("employee_context", {})
    stage = report_name(report, contract)
    employee_stage = {"accounting": "accounting-portfolio"}.get(stage, stage)
    worker_context = artifact.get("employee_context") if isinstance(artifact, Mapping) else None
    if not isinstance(worker_context, Mapping) and isinstance(employee_contexts, Mapping):
        worker_context = employee_contexts.get(employee_stage)
    if isinstance(worker_context, Mapping):
        report_for_head["employee_context"] = _bounded_worker_context(worker_context)
    context.setdefault("department_reports", {})[stage] = report_for_head  # type: ignore[index]


def _bounded_worker_context(value: Mapping[str, object]) -> dict[str, object]:
    """Keep Worker evidence useful to the head without unbounded prompt growth."""

    bounded: dict[str, object] = {}
    for key in ("department", "status", "runtime", "executed", "failed", "not_executed", "input_hash", "binding"):
        if key in value:
            bounded[key] = value[key]
    workers = value.get("workers")
    if isinstance(workers, list):
        bounded["workers"] = [
            {
                key: item[key]
                for key in ("worker_id", "role", "status", "attempts", "error", "output_contract")
                if key in item
            }
            for item in workers
            if isinstance(item, Mapping)
        ]
    return bounded


def report_name(report: Mapping[str, object], contract: str) -> str:
    return str(report.get("department") or {
        "research_packet": "research",
        "order_intent": "trading",
        "risk_decision": "risk",
        "qa_assessment": "qa",
        "execution_result": "oms-fill-gate",
        "accounting_snapshot": "accounting",
        "ceo_case_summary": "ceo",
    }.get(contract, contract))


def _detail(stage: str, report: Mapping[str, object], contract: str) -> str:
    return (
        f"paper_department={stage} status={report.get('status', 'UNKNOWN')} "
        f"output={contract} binding={report.get('binding', False)} "
        f"external_writes=false"
    )


def _case(context: Mapping[str, object]) -> MutableMapping[str, object]:
    value = context.get("case_request")
    if not isinstance(value, MutableMapping) or value.get("stage") not in {"paper", "test"}:
        raise ValueError("paper/test case_request is required")
    return value


def _artifact(context: Mapping[str, object], contract: str) -> dict[str, object]:
    artifacts = context.get("artifacts")
    if not isinstance(artifacts, Mapping) or not isinstance(artifacts.get(contract), Mapping):
        raise TypeError(f"missing paper artifact: {contract}")
    return dict(artifacts[contract])


def _summary_text(raw: Mapping[str, object]) -> str:
    for key in ("packet", "narrative", "summary", "thesis"):
        if raw.get(key):
            return str(raw[key])[:600]
    return "paper department output received"


def _department_summary(department: str, raw: Mapping[str, object]) -> dict[str, object]:
    status = str(raw.get("pipeline_status", raw.get("status", "COMPLETED"))).upper()
    if status not in {"COMPLETED", "DEGRADED", "FAILED", "HALTED"}:
        status = "COMPLETED"
    agent_execution = raw.get("agent_execution")
    hermes_runtime = raw.get("hermes_runtime")
    execution_evidence = raw.get("execution_evidence")
    if not isinstance(agent_execution, Mapping):
        agent_execution = {
            "executed": list(raw.get("employees_executed", []))
            if isinstance(raw.get("employees_executed"), list)
            else [],
            "failed": list(raw.get("employees_failed", []))
            if isinstance(raw.get("employees_failed"), list)
            else [],
            "not_executed": list(raw.get("employees_not_executed", []))
            if isinstance(raw.get("employees_not_executed"), list)
            else [],
        }
    if not isinstance(hermes_runtime, Mapping):
        hermes_runtime = {}
    if not isinstance(execution_evidence, Mapping):
        execution_evidence = {}
    evidence_status = str(execution_evidence.get("pipeline_status", "")).upper()
    if evidence_status in {"DEGRADED", "FAILED", "HALTED"}:
        status = evidence_status
    safe_action = raw.get("safe_action") or execution_evidence.get("safe_action")
    trace_id = raw.get("trace_id") or execution_evidence.get("trace_id")
    return {
        "department": department,
        "status": status,
        "verdict": raw.get("verdict", raw.get("candidate_verdict")),
        "safe_action": safe_action,
        "binding": False,
        "employees": dict(agent_execution),
        "agent_execution": dict(agent_execution),
        "execution_evidence": dict(execution_evidence),
        "langgraph": {
            "used": bool(raw.get("pipeline_version") or execution_evidence),
            "pipeline_version": raw.get("pipeline_version"),
            "trace_id": trace_id,
        },
        "decision_origin": raw.get("decision_origin"),
        "decision_status": raw.get("decision_status"),
        "input_hash": raw.get("input_hash"),
        "reason_codes": raw.get("reason_codes", []),
        "fallbacks": raw.get("fallbacks", []),
        "hermes_runtime": dict(hermes_runtime),
        "model": hermes_runtime.get("model"),
        "error": raw.get("error"),
        "error_message": raw.get("error_message"),
    }


def _failure_report(department: str, exc: Exception, *, safe_action: str) -> dict[str, object]:
    return {
        "department": department,
        "status": "DEGRADED",
        "verdict": "INCONCLUSIVE",
        "safe_action": safe_action,
        "binding": False,
        "error": type(exc).__name__,
        "error_message": str(exc)[:240],
        "fallbacks": ["paper_adapter_failure"],
    }


def _risk_failure(exc: Exception) -> dict[str, object]:
    return {
        "verdict": "reject",
        "candidate_verdict": "reject",
        "decision_origin": "PAPER_ADAPTER_FALLBACK",
        "decision_status": "DEGRADED",
        "safe_action": "HOLD",
        "reason_codes": ["paper_adapter_failure"],
        "narrative": f"Risk adapter failed closed: {type(exc).__name__}",
        "fallbacks": [{"stage": "paper_risk", "error": type(exc).__name__}],
        "binding": False,
    }


def _qa_failure(exc: Exception) -> dict[str, object]:
    return {
        "verdict": "FAIL",
        "safe_action": "ESCALATE",
        "reason_codes": ["paper_adapter_failure"],
        "narrative": f"QA adapter failed closed: {type(exc).__name__}",
        "fallbacks": [{"stage": "paper_qa", "error": type(exc).__name__}],
        "binding": False,
    }


def _risk_context(case: Mapping[str, object], order_intent: Mapping[str, object]) -> dict[str, object]:
    fund_id = str(order_intent["fund_id"])
    instrument_id = str(order_intent["instrument_id"])
    now = str(order_intent["snapshot"]["as_of"])
    return {
        "mandate": {
            "fund_id": fund_id,
            "allowed_instrument_ids": [instrument_id],
            "min_order_notional": "1",
            "max_order_notional": "1000000",
        },
        "limits": {
            "soft_single_issuer_pct": "0.05",
            "hard_single_issuer_pct": "0.10",
            "max_daily_turnover_notional": "1000000",
            "max_daily_order_count": 100,
            "max_daily_loss": "100000",
            "max_drawdown_pct": "0.20",
        },
        "restricted_items": [],
        "portfolio": {
            "fund_id": fund_id,
            "cash": "1000000",
            "buying_power": "1000000",
            "gross_exposure": "0",
            "positions": {},
            "issuer_of": {instrument_id: str(case["symbol"])},
            "issuer_exposure": {},
            "realized_pnl_today": "0",
            "unrealized_pnl_today": "0",
            "peak_equity": "1000000",
            "equity": "1000000",
            "orders_today": 0,
            "notional_traded_today": "0",
        },
        "market_status": {"tradable": True, "reason": "paper snapshot only"},
        "counterparty": {"broker_adapter": "paper", "health": "ok"},
        "trading_state": "ENABLED",
        "as_of": now,
    }


def _qa_input(
    case: Mapping[str, object], risk_decision: Mapping[str, object]
) -> tuple[dict[str, object], dict[str, object], str]:
    evidence_id = uuid5(_PAPER_NAMESPACE, f"evidence:{case['case_id']}")
    artifact_id = uuid5(_PAPER_NAMESPACE, f"artifact:{case['case_id']}")
    trace_id = uuid5(_PAPER_NAMESPACE, f"trace:{case['case_id']}")
    fund_id = uuid5(_PAPER_NAMESPACE, "fund:paper")
    as_of = datetime.now(timezone.utc).isoformat()
    claim_text = (
        f"Paper order intent for {case['symbol']} is {case['side']} "
        f"{case['quantity']} units at limit {case['limit_price']}"
    )
    artifact = {
        "artifact_version_id": str(artifact_id),
        "artifact_type": "order_intent",
        "producer": "paper-trading-adapter",
        "fund_id": str(fund_id),
        "trace_id": str(trace_id),
        "claims": [
            {
                "claim_index": 0,
                "text": claim_text,
                "kind": "FACT",
                "subject": str(case["symbol"]),
                "numeric_value": str(case["limit_price"]),
                "unit": "USD",
                "evidence_ids": [str(evidence_id)],
                "acknowledges_uncertainty": True,
            }
        ],
        "tool_results": [],
    }
    evidence_store = {
        str(evidence_id): {
            "source": "paper-order-intent",
            "published_at": as_of,
            "observed_at": as_of,
            "excerpt": claim_text,
            "numeric_value": str(case["limit_price"]),
            "unit": "USD",
        }
    }
    return artifact, evidence_store, as_of


def _load_script(repo_root: Path, name: str, relative_path: str) -> Any:
    path = repo_root / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {relative_path}")
    department_root = path.parent
    import_paths = _department_import_paths(repo_root, department_root)

    # Risk and QA both intentionally use small, local top-level modules named
    # ``reporting`` (and Risk lazily imports ``app``).  Loading both scripts
    # with one global sys.path otherwise makes the second department import the
    # first department's module.  Evict only department-local modules while
    # loading and keep the module references on the runner for call-time
    # isolation.  Shared packages under apps/src are never evicted.
    saved_modules = _evict_department_modules(repo_root)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    previous_path = sys.path[:]
    sys.path[:0] = import_paths
    try:
        spec.loader.exec_module(module)
    finally:
        loaded_local_modules = {
            module_name: loaded
            for module_name, loaded in list(sys.modules.items())
            if _is_department_module(repo_root, loaded)
            and module_name != name
        }
        sys.path[:] = previous_path
        for module_name in list(sys.modules):
            if module_name == name:
                continue
            loaded = sys.modules[module_name]
            if _is_department_module(repo_root, loaded):
                sys.modules.pop(module_name, None)
        sys.modules.update(saved_modules)

    module.__paper_repo_root__ = repo_root
    module.__paper_import_paths__ = import_paths
    module.__paper_local_modules__ = loaded_local_modules
    module.__paper_hermes_profile__ = _hermes_profile_for_path(relative_path)
    return module


def _default_research_runner(repo_root: Path) -> DepartmentRunner:
    module = _load_script(repo_root, "paper_research_scripts", "departments/01-research/scripts.py")
    return _bind_department_runner(module, "run_research_department")


def _default_risk_runner(repo_root: Path) -> DepartmentRunner:
    module = _load_script(repo_root, "paper_risk_scripts", "departments/03-risk/scripts.py")
    return _bind_department_runner(module, "run_risk_department")


def _default_qa_runner(repo_root: Path) -> DepartmentRunner:
    module = _load_script(repo_root, "paper_qa_scripts", "departments/06-ai-qa-audit/scripts.py")
    return _bind_department_runner(module, "run_qa_department")


def _bind_department_runner(module: Any, function_name: str) -> DepartmentRunner:
    function = getattr(module, function_name)

    @wraps(function)
    def isolated_runner(*args: object, **kwargs: object) -> Mapping[str, object]:
        with _activate_department_runtime(module):
            result = function(*args, **kwargs)
        enriched = dict(result)
        enriched.setdefault("pipeline_version", module.__dict__.get("PIPELINE_VERSION"))
        enriched.setdefault("langgraph_used", True)
        return enriched

    isolated_runner.__paper_module__ = module
    return isolated_runner


@contextmanager
def _activate_department_runtime(module: Any):
    repo_root = Path(module.__paper_repo_root__)
    saved_modules = _evict_department_modules(repo_root)
    for module_name, local_module in module.__paper_local_modules__.items():
        sys.modules[module_name] = local_module
    previous_path = sys.path[:]
    sys.path[:0] = list(module.__paper_import_paths__)
    previous_hermes_home = os.environ.get("HERMES_HOME")
    hermes_profile = module.__paper_hermes_profile__
    os.environ["HERMES_HOME"] = str(Path.home() / ".hermes" / "profiles" / hermes_profile)
    try:
        yield
    finally:
        if previous_hermes_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous_hermes_home
        sys.path[:] = previous_path
        for module_name in list(sys.modules):
            if module_name == module.__name__:
                continue
            loaded = sys.modules[module_name]
            if _is_department_module(repo_root, loaded):
                sys.modules.pop(module_name, None)
        sys.modules.update(saved_modules)


def _department_import_paths(repo_root: Path, department_root: Path) -> list[str]:
    candidates = (
        department_root,
        department_root / "agents",
        department_root / "collectors",
        department_root / "engine",
        department_root / "api",
        department_root / "evidence",
        department_root.parent / "02-trading" / "contracts",
        repo_root / "skills" / "agentic-rag",
    )
    return [str(path) for path in candidates if path.is_dir()]


def _hermes_profile_for_path(relative_path: str) -> str:
    if "03-risk" in relative_path:
        return "risk-management"
    if "06-ai-qa-audit" in relative_path:
        return "qa-department"
    return "default"


def _evict_department_modules(repo_root: Path) -> dict[str, Any]:
    saved: dict[str, Any] = {}
    for module_name, loaded in list(sys.modules.items()):
        if _is_department_module(repo_root, loaded):
            saved[module_name] = loaded
            sys.modules.pop(module_name, None)
    return saved


def _is_department_module(repo_root: Path, loaded: object) -> bool:
    file_name = getattr(loaded, "__file__", None)
    if not file_name:
        return False
    try:
        file_path = Path(file_name).resolve()
        return file_path.is_relative_to((repo_root / "departments").resolve())
    except (OSError, ValueError):
        return False


@contextmanager
def _disable_notion(runner: DepartmentRunner):
    module = getattr(runner, "__self__", None) or _module_from_runner(runner)
    original = getattr(module, "notion_report", None)
    if original is None:
        yield
        return
    module.notion_report = lambda _state: {
        "notion_upload": {"ok": False, "reason": "paper_adapter_disabled"}
    }
    try:
        yield
    finally:
        module.notion_report = original


@contextmanager
def _disable_research_persistence(runner: DepartmentRunner):
    module = _module_from_runner(runner)
    original = getattr(module, "_record_pipeline_run", None)
    if original is None:
        yield
        return
    module._record_pipeline_run = lambda **_kwargs: None
    try:
        yield
    finally:
        module._record_pipeline_run = original


def _module_from_runner(runner: DepartmentRunner) -> Any:
    module = getattr(runner, "__paper_module__", None)
    if module is not None:
        return module
    return __import__(runner.__module__)
