"""Mandate-driven Risk employee execution.

The mandate form is a user-owned input contract.  It is evaluated by the two
Risk employees independently:

* ``risk-runner`` performs deterministic limit checks and is authoritative for
  numbers, but is never binding for order execution.
* ``compliance-policy-worker`` evaluates policy evidence.  Pinecone is used
  only when its connection settings and a query vector are supplied; a missing
  evidence path escalates instead of pretending that compliance passed.

This module intentionally returns recommendations and a Hermes-ready context.
It does not create an Order or call a broker.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import requests
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RiskTolerance = Literal["CONSERVATIVE", "BALANCED", "AGGRESSIVE"]
OrderMode = Literal["AUTO_EXECUTION", "MANUAL_APPROVAL"]
AssetPermission = Literal["ALLOWED", "PROHIBITED"]


class InvestorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investment_goal: str = Field(min_length=1, max_length=2000)
    risk_tolerance: RiskTolerance
    financial_experience_years: int = Field(ge=0, le=100)
    perceived_risk_awareness: bool


class PortfolioConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_capital: Decimal = Field(gt=0)
    max_single_stock_weight: Decimal = Field(ge=0, le=1)
    max_total_exposure: Decimal = Field(ge=0)
    max_drawdown_limit: Decimal = Field(ge=-1, le=0)

    @field_validator("base_capital", "max_single_stock_weight", "max_total_exposure", "max_drawdown_limit", mode="before")
    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal:
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("must be a finite decimal") from exc


class AssetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    single_stocks: AssetPermission
    etf: AssetPermission
    leverage: AssetPermission
    futures: AssetPermission
    options: AssetPermission
    derivatives: AssetPermission = "PROHIBITED"
    crypto: AssetPermission


class PositionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instrument_id: str = Field(min_length=1, max_length=100)
    asset_class: Literal["SINGLE_STOCK", "ETF", "LEVERAGE", "FUTURES", "OPTIONS", "DERIVATIVES", "CRYPTO"]
    weight: Decimal | None = Field(default=None, ge=0)
    issuer: str | None = Field(default=None, max_length=200)

    @field_validator("weight", mode="before")
    @classmethod
    def parse_weight(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("weight must be a finite decimal") from exc


class PortfolioSnapshot(BaseModel):
    """Optional observed state; the mandate alone cannot produce VaR."""

    model_config = ConfigDict(extra="forbid")

    current_var: Decimal | None = None
    var_limit: Decimal | None = None
    total_exposure: Decimal | None = Field(default=None, ge=0)
    current_drawdown: Decimal | None = None
    positions: list[PositionSnapshot] = Field(default_factory=list)
    as_of: str | None = None

    @field_validator("current_var", "var_limit", "current_drawdown", mode="before")
    @classmethod
    def parse_observed_decimal(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("observed value must be a finite decimal") from exc


class ComplianceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=10000)
    source: str | None = Field(default=None, max_length=500)
    as_of: str | None = None
    violation: bool = False
    reason_code: str | None = Field(default=None, max_length=100)


class RiskMandateAssessmentRequest(BaseModel):
    """Shared input delivered separately to the two Risk employees."""

    model_config = ConfigDict(extra="forbid")

    mandate_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    event_id: str | None = Field(default=None, max_length=100)
    timestamp: str | None = None
    investor_profile: InvestorProfile
    portfolio_constraints: PortfolioConstraints
    asset_policy: AssetPolicy
    order_mode: OrderMode
    portfolio_snapshot: PortfolioSnapshot | None = None
    compliance_query: str | None = Field(default=None, max_length=4000)
    policy_query_vector: list[float] | None = Field(default=None, min_length=1)
    compliance_evidence: list[ComplianceEvidence] = Field(default_factory=list)
    as_of: str | None = None

    @model_validator(mode="after")
    def validate_policy_query(self) -> RiskMandateAssessmentRequest:
        if self.policy_query_vector is not None and not all(
            isinstance(item, (int, float)) for item in self.policy_query_vector
        ):
            raise ValueError("policy_query_vector must contain numbers")
        return self


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    return value


def _input_hash(request: RiskMandateAssessmentRequest) -> str:
    canonical = json.dumps(_jsonable(request), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decimal(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _policy_key(asset_class: str) -> str:
    return {
        "SINGLE_STOCK": "single_stocks",
        "ETF": "etf",
        "LEVERAGE": "leverage",
        "FUTURES": "futures",
        "OPTIONS": "options",
        "DERIVATIVES": "derivatives",
        "CRYPTO": "crypto",
    }[asset_class]


def run_risk_runner(request: RiskMandateAssessmentRequest) -> dict[str, Any]:
    """Run authoritative deterministic checks for the mandate and snapshot."""

    snapshot = request.portfolio_snapshot
    reasons: list[str] = []
    actions: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    missing_observations: list[str] = []

    if snapshot is None:
        missing_observations.extend(["current_var", "var_limit", "total_exposure", "current_drawdown", "positions"])
        checks.append({"check": "observed_portfolio_state", "status": "MISSING_INPUT"})
    else:
        if snapshot.current_var is None or snapshot.var_limit is None:
            missing_observations.append("var_current_or_limit")
        elif snapshot.current_var > snapshot.var_limit:
            reasons.append("VAR_LIMIT_BREACH")
            actions.append({"type": "HEDGE_OR_UNWIND", "target": "DELTA_EXPOSURE", "mode": request.order_mode})
            checks.append({"check": "portfolio_var", "status": "BREACH", "current": _decimal(snapshot.current_var), "limit": _decimal(snapshot.var_limit)})
        else:
            checks.append({"check": "portfolio_var", "status": "PASS", "current": _decimal(snapshot.current_var), "limit": _decimal(snapshot.var_limit)})

        if snapshot.total_exposure is None:
            missing_observations.append("total_exposure")
        elif snapshot.total_exposure > request.portfolio_constraints.max_total_exposure:
            reasons.append("TOTAL_EXPOSURE_LIMIT_BREACH")
            actions.append({"type": "REDUCE_EXPOSURE", "target": "TOTAL_EXPOSURE", "mode": request.order_mode})
            checks.append({"check": "total_exposure", "status": "BREACH", "current": _decimal(snapshot.total_exposure), "limit": _decimal(request.portfolio_constraints.max_total_exposure)})
        else:
            checks.append({"check": "total_exposure", "status": "PASS", "current": _decimal(snapshot.total_exposure), "limit": _decimal(request.portfolio_constraints.max_total_exposure)})

        if snapshot.current_drawdown is None:
            missing_observations.append("current_drawdown")
        elif snapshot.current_drawdown < request.portfolio_constraints.max_drawdown_limit:
            reasons.append("DRAWDOWN_LIMIT_BREACH")
            actions.append({"type": "ENTRY_BLOCK_OR_REDUCE", "target": "DRAWDOWN", "mode": request.order_mode})
            checks.append({"check": "drawdown", "status": "BREACH", "current": _decimal(snapshot.current_drawdown), "limit": _decimal(request.portfolio_constraints.max_drawdown_limit)})
        else:
            checks.append({"check": "drawdown", "status": "PASS", "current": _decimal(snapshot.current_drawdown), "limit": _decimal(request.portfolio_constraints.max_drawdown_limit)})

        for position in snapshot.positions:
            permission = getattr(request.asset_policy, _policy_key(position.asset_class))
            if permission == "PROHIBITED":
                reasons.append(f"PROHIBITED_ASSET:{position.asset_class}")
                actions.append({"type": "REJECT_POSITION", "instrument_id": position.instrument_id, "mode": request.order_mode})
            if position.asset_class == "SINGLE_STOCK" and position.weight is None:
                missing_observations.append(f"position_weight:{position.instrument_id}")
            elif position.asset_class == "SINGLE_STOCK" and position.weight > request.portfolio_constraints.max_single_stock_weight:
                reasons.append(f"SINGLE_STOCK_LIMIT_BREACH:{position.instrument_id}")
                actions.append({"type": "REBALANCE_SELL", "instrument_id": position.instrument_id, "target_weight": _decimal(request.portfolio_constraints.max_single_stock_weight), "mode": request.order_mode})

    if reasons:
        verdict = "RESIZE" if not any(reason.startswith("PROHIBITED_ASSET") for reason in reasons) else "REJECT"
        severity = "HIGH" if any(reason in {"VAR_LIMIT_BREACH", "DRAWDOWN_LIMIT_BREACH"} for reason in reasons) else "MEDIUM"
        action_required = True
    elif missing_observations:
        verdict, severity, action_required = "HOLD", "MEDIUM", True
    else:
        verdict, severity, action_required = "APPROVE", "LOW", False

    return {
        "worker_id": "risk-runner",
        "department": "RISK",
        "event_id": request.event_id,
        "timestamp": request.timestamp or request.as_of,
        "role": "Deterministic portfolio limit and exposure runner",
        "authoritative": True,
        "binding": False,
        "status": "COMPLETED",
        "verdict": verdict,
        "severity": severity,
        "action_required": action_required,
        "reason_codes": sorted(set(reasons)),
        "missing_observations": sorted(set(missing_observations)),
        "checks": checks,
        "suggested_actions": actions,
        "order_mode": request.order_mode,
        "input_hash": _input_hash(request),
        "summary": "Deterministic RiskEngine-style mandate checks completed; no order was submitted.",
    }


class PineconeEvidenceClient:
    """Minimal Pinecone data-plane client using environment-only credentials."""

    def __init__(self, *, api_key: str | None = None, index_host: str | None = None, timeout_seconds: float = 5.0) -> None:
        self.api_key = api_key or os.getenv("PINECONE_API_KEY", "").strip()
        self.index_host = (index_host or os.getenv("PINECONE_INDEX_HOST", "").strip()).rstrip("/")
        self.timeout_seconds = timeout_seconds

    def query(self, vector: list[float], *, namespace: str | None = None, top_k: int = 8) -> list[dict[str, Any]]:
        if not self.api_key or not self.index_host:
            raise RuntimeError("PINECONE_NOT_CONFIGURED")
        body: dict[str, Any] = {"vector": vector, "topK": top_k, "includeMetadata": True}
        if namespace:
            body["namespace"] = namespace
        response = requests.post(
            f"{self.index_host}/query",
            headers={"Api-Key": self.api_key, "Content-Type": "application/json"},
            json=body,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        matches = response.json().get("matches", [])
        return [item for item in matches if isinstance(item, dict)]


def run_compliance_policy_worker(request: RiskMandateAssessmentRequest, *, pinecone: PineconeEvidenceClient | None = None) -> dict[str, Any]:
    """Produce non-authoritative policy findings from supplied/Pinecone evidence."""

    evidence = [item.model_dump(mode="json") for item in request.compliance_evidence]
    source = "request"
    error: str | None = None
    if not evidence and request.policy_query_vector is not None:
        try:
            matches = (pinecone or PineconeEvidenceClient()).query(request.policy_query_vector, namespace=os.getenv("PINECONE_NAMESPACE"))
            source = "pinecone"
            evidence = [
                {
                    "evidence_id": str(match.get("id", "pinecone-match")),
                    "title": str((match.get("metadata") or {}).get("title", "Pinecone policy evidence")),
                    "text": str((match.get("metadata") or {}).get("text", "")),
                    "source": "pinecone",
                    "score": match.get("score"),
                    "violation": bool((match.get("metadata") or {}).get("violation", False)),
                }
                for match in matches
            ]
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            error = type(exc).__name__

    violations = [item for item in evidence if item.get("violation") is True]
    if violations:
        verdict, severity, action_required = "ESCALATE", "MEDIUM", True
        suggested_actions = [{"type": "REBALANCE_TO_POLICY", "evidence_id": item.get("evidence_id"), "mode": request.order_mode} for item in violations]
    elif evidence:
        verdict, severity, action_required = "PASS", "LOW", False
        suggested_actions = []
    else:
        verdict, severity, action_required = "ESCALATE", "MEDIUM", True
        suggested_actions = []

    return {
        "worker_id": "compliance-policy-worker",
        "department": "RISK",
        "event_id": request.event_id,
        "timestamp": request.timestamp or request.as_of,
        "role": "Point-in-time policy evidence monitor",
        "authoritative": False,
        "binding": False,
        "status": "DEGRADED" if not evidence else "COMPLETED",
        "verdict": verdict,
        "severity": severity,
        "action_required": action_required,
        "reason_codes": [item.get("reason_code") or "POLICY_EVIDENCE_VIOLATION" for item in violations],
        "evidence_refs": [item.get("evidence_id") for item in evidence],
        "evidence_source": source,
        "suggested_actions": suggested_actions,
        "error": error,
        "order_mode": request.order_mode,
        "input_hash": _input_hash(request),
        "summary": "Policy evidence was evaluated as advisory; policy evidence never submits an order.",
    }


def synthesize_risk_head(risk_report: Mapping[str, Any], compliance_report: Mapping[str, Any], request: RiskMandateAssessmentRequest) -> dict[str, Any]:
    """Create a Hermes-head-ready fan-in without granting execution authority."""

    reports = [dict(risk_report), dict(compliance_report)]
    high_or_actionable = [report for report in reports if report.get("action_required")]
    if any(report.get("verdict") == "REJECT" for report in reports):
        decision = "REJECT"
    elif high_or_actionable:
        decision = "ESCALATE"
    else:
        decision = "APPROVE"
    return {
        "decision": decision,
        "execution_mode": request.order_mode,
        "manual_approval_required": request.order_mode == "MANUAL_APPROVAL" or bool(high_or_actionable),
        "binding": False,
        "safe_action": "HOLD" if decision != "APPROVE" else "NO_ACTION",
        "recommended_actions": [action for report in reports for action in report.get("suggested_actions", [])],
        "reports": reports,
        "hermes_context": {
            "mandate_id": request.mandate_id,
            "employee_reports": reports,
            "instruction": "Summarize evidence and route any recommendation for Risk Engine and human approval. Never submit an order.",
        },
    }


def build_risk_head_dispatch(request: RiskMandateAssessmentRequest) -> dict[str, Any]:
    """Build the immutable fan-out envelope owned by the Risk Head."""

    mandate = request.model_dump(mode="json")
    digest = _input_hash(request)
    return {
        "dispatcher": "risk-head",
        "mandate_id": request.mandate_id,
        "input_hash": digest,
        "worker_inputs": {
            "risk-runner": {"mandate": mandate, "input_hash": digest},
            "compliance-policy-worker": {"mandate": mandate, "input_hash": digest},
        },
        "mutation_allowed": False,
    }


def assess_mandate(request: RiskMandateAssessmentRequest | Mapping[str, Any]) -> dict[str, Any]:
    """Run both Risk employees and return their independent reports plus fan-in."""

    normalized = request if isinstance(request, RiskMandateAssessmentRequest) else RiskMandateAssessmentRequest.model_validate(request)
    dispatch = build_risk_head_dispatch(normalized)
    risk_report = run_risk_runner(normalized)
    compliance_report = run_compliance_policy_worker(normalized)
    return {
        "mandate_id": normalized.mandate_id,
        "pipeline_status": "COMPLETED" if risk_report["status"] == "COMPLETED" and compliance_report["status"] == "COMPLETED" else "DEGRADED",
        "dispatch": dispatch,
        "employees": {"risk-runner": risk_report, "compliance-policy-worker": compliance_report},
        "risk_head": synthesize_risk_head(risk_report, compliance_report, normalized),
    }


__all__ = [
    "AssetPolicy",
    "ComplianceEvidence",
    "InvestorProfile",
    "PineconeEvidenceClient",
    "PortfolioConstraints",
    "PortfolioSnapshot",
    "PositionSnapshot",
    "RiskMandateAssessmentRequest",
    "assess_mandate",
    "build_risk_head_dispatch",
    "run_compliance_policy_worker",
    "run_risk_runner",
    "synthesize_risk_head",
]
