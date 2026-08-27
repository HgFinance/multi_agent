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
import re
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import requests
from integrations.pinecone_client import PineconeEvidenceClient, PineconeQueryError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from tools.legal_wiki_tool import (
    COMPLIANCE_MANDATE_CONTEXT,
    LegalWikiAnswerFn,
    LegalWikiQueryInput,
    LegalWikiQueryOutput,
    query_legal_wiki,
)
from tools.order_tools import OrderComplianceInput, evaluate_order_compliance
from tools.policy_tools import RiskPolicyQueryInput, query_pinecone_risk_policy

from orchestration.contracts.mas import build_agent_task_result

RiskTolerance = Literal["CONSERVATIVE", "BALANCED", "AGGRESSIVE"]
OrderMode = Literal["AUTO_EXECUTION", "MANUAL_APPROVAL"]
AssetPermission = Literal["ALLOWED", "PROHIBITED"]
# compliance-policy-worker 전용 실행 계약(RISK_MANDATE_WORKER_FLOW.md §11) — risk-runner는
# 같은 문자열을 쓰지 않고 별도 RISK_CHECK 경로를 쓴다. 기본값은 기존 Pinecone evidence
# 흐름과 동일하게 RISK_POLICY_REVIEW로 둬서 이 필드를 안 보내는 기존 호출도 그대로 동작한다.
QueryMode = Literal[
    "MANDATE_REVIEW", "RISK_POLICY_REVIEW", "LEGAL_QUERY", "MIXED_REVIEW", "NOT_APPLICABLE"
]


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

    @field_validator(
        "base_capital",
        "max_single_stock_weight",
        "max_total_exposure",
        "max_drawdown_limit",
        mode="before",
    )
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
    asset_class: Literal[
        "SINGLE_STOCK", "ETF", "LEVERAGE", "FUTURES", "OPTIONS", "DERIVATIVES", "CRYPTO"
    ]
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
    trace_id: str | None = Field(default=None, max_length=128)
    timestamp: str | None = None
    investor_profile: InvestorProfile
    portfolio_constraints: PortfolioConstraints
    asset_policy: AssetPolicy
    order_mode: OrderMode
    query_mode: QueryMode = "RISK_POLICY_REVIEW"
    portfolio_snapshot: PortfolioSnapshot | None = None
    compliance_query: str | None = Field(default=None, max_length=4000)
    policy_query_vector: list[float] | None = Field(default=None, min_length=1)
    compliance_evidence: list[ComplianceEvidence] = Field(default_factory=list)
    order_compliance: OrderComplianceInput | None = None
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
    canonical = json.dumps(
        _jsonable(request), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def classify_compliance_query_mode(question: str) -> tuple[str, str]:
    """Classify a question without calling a model.

    This is both the model-router fallback and the guard in front of Hermes'
    on-demand legal tool. Keeping one classifier prevents the two entry paths
    from drifting into different definitions of a legal request.
    """

    text = question.casefold()
    legal = any(
        token in text
        for token in (
            "법률",
            "법령",
            "규정",
            "위반",
            "조항",
            "판례",
            "자본시장법",
            "금융투자업규정",
            "미공개중요정보",
            "미공개 정보",
            "시세조종",
            "부정거래",
            "내부자거래",
            "단기매매차익",
            "공시의무",
            "금융위원회",
            "대법원",
        )
    ) or bool(
        re.search(r"제\s*\d+(?:의\d+)?조", text)
        or re.search(r"\b(?:law|legal|statute|regulation|case law)\b", text)
    )
    policy = any(
        token in text
        for token in ("내부정책", "정책", "제한목록", "restricted", "한도", "policy")
    )
    mandate = any(
        token in text
        for token in ("투자목표", "위험성향", "레버리지", "투자경험", "자산정책", "mandate")
    )
    if legal and policy:
        return "MIXED_REVIEW", "local_fallback_detected_policy_and_legal_terms"
    if legal:
        return "LEGAL_QUERY", "local_fallback_detected_legal_terms"
    if policy:
        return "RISK_POLICY_REVIEW", "local_fallback_detected_policy_terms"
    if mandate:
        return "MANDATE_REVIEW", "local_fallback_detected_mandate_terms"
    return "NOT_APPLICABLE", "local_fallback_no_compliance_scope_detected"


class _SafeComplianceLLM:
    """Use the configured Ollama model and expose a transparent safe fallback."""

    uses_model = True

    def __init__(self, delegate: Any | None = None) -> None:
        self._delegate = delegate

    def __call__(self, system: str, prompt: str) -> str:
        if self._delegate is None:
            from risk_employee_workers import default_worker_llm

            delegate = default_worker_llm
        else:
            delegate = self._delegate
        try:
            raw = delegate(system, prompt)
        except Exception:  # noqa: BLE001 - worker boundary must fail closed
            raw = None
        if isinstance(raw, str) and raw.strip():
            self.uses_model = True
            return raw

        self.uses_model = False
        if "routing classifier" in system:
            question = prompt.split("Question:", 1)[-1].strip()
            mode, rationale = classify_compliance_query_mode(question)
            return json.dumps(
                {"query_mode": mode, "routing_rationale": rationale},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "summary": "compliance 모델을 사용할 수 없어 사람 검토로 전환합니다.",
                "confidence": 0.0,
                "evidence_refs": [],
                "escalate": True,
            },
        )


def _run_compliance_employee_graph(
    request: RiskMandateAssessmentRequest,
    risk_report: Mapping[str, Any] | None = None,
    *,
    employee_llm: Any | None = None,
    legal_answer_fn: LegalWikiAnswerFn | None = None,
) -> dict[str, Any]:
    """Run route → tool → answer → validation and return its trace envelope."""

    from risk_employee_workers import run_employee_workers

    payload = {
        "mandate_id": request.mandate_id,
        "case_id": request.mandate_id,
        # §5.1.1: worker-context.trace_id must equal the Task/Head trace_id.
        "trace_id": request.trace_id or request.event_id or _input_hash(request)[:16],
        "as_of": request.as_of,
        "input_hash": _input_hash(request),
        "assessment": dict(risk_report or {}),
        "context": request.model_dump(mode="json"),
        "compliance": {
            "query": request.compliance_query or "",
            "question": request.compliance_query or "",
            "evidence": [item.model_dump(mode="json") for item in request.compliance_evidence],
            "grounded": bool(request.compliance_evidence),
        },
    }
    # Structured routing wins over model classification, exactly as the flow
    # specification requires.  If absent, the worker must classify the query.
    if "query_mode" in request.model_fields_set:
        payload["query_mode"] = request.query_mode
    runtime_llm = employee_llm or _SafeComplianceLLM()
    runtime = run_employee_workers(
        payload,
        llm=runtime_llm,
        legal_answer_fn=legal_answer_fn,
        include_deterministic_runner=False,
    )
    runtime["model_available"] = bool(getattr(runtime_llm, "uses_model", True))
    runtime["generation_degraded"] = not runtime["model_available"]
    return runtime


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
    order_compliance_result = None
    if request.order_compliance is not None:
        order_compliance_result = evaluate_order_compliance(request.order_compliance)
        if order_compliance_result.verdict == "REJECT":
            reasons.append("ORDER_COMPLIANCE_REJECT")
        elif order_compliance_result.verdict == "RESIZE":
            reasons.append("ORDER_COMPLIANCE_RESIZE")

    if snapshot is None:
        missing_observations.extend(
            [
                "current_var",
                "var_limit",
                "total_exposure",
                "current_drawdown",
                "positions",
            ]
        )
        checks.append({"check": "observed_portfolio_state", "status": "MISSING_INPUT"})
    else:
        if snapshot.current_var is None or snapshot.var_limit is None:
            missing_observations.append("var_current_or_limit")
        elif snapshot.current_var > snapshot.var_limit:
            reasons.append("VAR_LIMIT_BREACH")
            actions.append(
                {
                    "type": "HEDGE_OR_UNWIND",
                    "target": "DELTA_EXPOSURE",
                    "mode": request.order_mode,
                }
            )
            checks.append(
                {
                    "check": "portfolio_var",
                    "status": "BREACH",
                    "current": _decimal(snapshot.current_var),
                    "limit": _decimal(snapshot.var_limit),
                }
            )
        else:
            checks.append(
                {
                    "check": "portfolio_var",
                    "status": "PASS",
                    "current": _decimal(snapshot.current_var),
                    "limit": _decimal(snapshot.var_limit),
                }
            )

        if snapshot.total_exposure is None:
            missing_observations.append("total_exposure")
        elif snapshot.total_exposure > request.portfolio_constraints.max_total_exposure:
            reasons.append("TOTAL_EXPOSURE_LIMIT_BREACH")
            actions.append(
                {
                    "type": "REDUCE_EXPOSURE",
                    "target": "TOTAL_EXPOSURE",
                    "mode": request.order_mode,
                }
            )
            checks.append(
                {
                    "check": "total_exposure",
                    "status": "BREACH",
                    "current": _decimal(snapshot.total_exposure),
                    "limit": _decimal(request.portfolio_constraints.max_total_exposure),
                }
            )
        else:
            checks.append(
                {
                    "check": "total_exposure",
                    "status": "PASS",
                    "current": _decimal(snapshot.total_exposure),
                    "limit": _decimal(request.portfolio_constraints.max_total_exposure),
                }
            )

        if snapshot.current_drawdown is None:
            missing_observations.append("current_drawdown")
        elif (
            snapshot.current_drawdown < request.portfolio_constraints.max_drawdown_limit
        ):
            reasons.append("DRAWDOWN_LIMIT_BREACH")
            actions.append(
                {
                    "type": "ENTRY_BLOCK_OR_REDUCE",
                    "target": "DRAWDOWN",
                    "mode": request.order_mode,
                }
            )
            checks.append(
                {
                    "check": "drawdown",
                    "status": "BREACH",
                    "current": _decimal(snapshot.current_drawdown),
                    "limit": _decimal(request.portfolio_constraints.max_drawdown_limit),
                }
            )
        else:
            checks.append(
                {
                    "check": "drawdown",
                    "status": "PASS",
                    "current": _decimal(snapshot.current_drawdown),
                    "limit": _decimal(request.portfolio_constraints.max_drawdown_limit),
                }
            )

        for position in snapshot.positions:
            permission = getattr(
                request.asset_policy, _policy_key(position.asset_class)
            )
            if permission == "PROHIBITED":
                reasons.append(f"PROHIBITED_ASSET:{position.asset_class}")
                actions.append(
                    {
                        "type": "REJECT_POSITION",
                        "instrument_id": position.instrument_id,
                        "mode": request.order_mode,
                    }
                )
            if position.asset_class == "SINGLE_STOCK" and position.weight is None:
                missing_observations.append(f"position_weight:{position.instrument_id}")
            elif (
                position.asset_class == "SINGLE_STOCK"
                and position.weight
                > request.portfolio_constraints.max_single_stock_weight
            ):
                reasons.append(f"SINGLE_STOCK_LIMIT_BREACH:{position.instrument_id}")
                actions.append(
                    {
                        "type": "REBALANCE_SELL",
                        "instrument_id": position.instrument_id,
                        "target_weight": _decimal(
                            request.portfolio_constraints.max_single_stock_weight
                        ),
                        "mode": request.order_mode,
                    }
                )

    if reasons:
        if order_compliance_result and order_compliance_result.verdict == "REJECT":
            verdict = "REJECT"
        else:
            verdict = (
                "RESIZE"
                if not any(reason.startswith("PROHIBITED_ASSET") for reason in reasons)
                else "REJECT"
            )
        severity = (
            "HIGH"
            if any(
                reason in {"VAR_LIMIT_BREACH", "DRAWDOWN_LIMIT_BREACH"}
                for reason in reasons
            )
            else "MEDIUM"
        )
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
        "tool_calls": (
            ["evaluate_order_compliance"] if order_compliance_result is not None else []
        ),
        "order_compliance": (
            order_compliance_result.model_dump(mode="json")
            if order_compliance_result is not None
            else None
        ),
        "input_hash": _input_hash(request),
        "summary": "Deterministic RiskEngine-style mandate checks completed; no order was submitted.",
    }


class LegacyPineconeEvidenceClient:
    """Minimal Pinecone data-plane client using environment-only credentials."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        index_host: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.api_key = api_key or os.getenv("PINECONE_API_KEY", "").strip()
        self.index_host = (
            index_host or os.getenv("PINECONE_INDEX_HOST", "").strip()
        ).rstrip("/")
        self.timeout_seconds = timeout_seconds

    def query(
        self, vector: list[float], *, namespace: str | None = None, top_k: int = 8
    ) -> list[dict[str, Any]]:
        if not self.api_key or not self.index_host:
            raise RuntimeError("PINECONE_NOT_CONFIGURED")
        body: dict[str, Any] = {
            "vector": vector,
            "topK": top_k,
            "includeMetadata": True,
        }
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


def _as_of_date(request: RiskMandateAssessmentRequest) -> date:
    return date.fromisoformat(
        (request.as_of or datetime.now(timezone.utc).date().isoformat())[:10]
    )


def _worker_envelope(
    request: RiskMandateAssessmentRequest,
    *,
    status: str,
    verdict: str,
    severity: str,
    action_required: bool,
    reason_codes: list[str],
    tool_calls: list[str],
    suggested_actions: list[dict[str, Any]],
    summary: str,
    error: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "worker_id": "compliance-policy-worker",
        "department": "RISK",
        "event_id": request.event_id,
        "timestamp": request.timestamp or request.as_of,
        "query_mode": request.query_mode,
        "authoritative": False,
        "binding": False,
        "status": status,
        "verdict": verdict,
        "severity": severity,
        "action_required": action_required,
        "reason_codes": reason_codes,
        "tool_calls": tool_calls,
        "suggested_actions": suggested_actions,
        "error": error,
        "order_mode": request.order_mode,
        "input_hash": _input_hash(request),
        "summary": summary,
        **extra,
    }


def _not_applicable_review(request: RiskMandateAssessmentRequest) -> dict[str, Any]:
    """정책·법률 검토가 이번 요청과 무관함을 기록한다 — 검색은 수행하지 않는다."""

    return _worker_envelope(
        request,
        status="COMPLETED",
        verdict="NOT_APPLICABLE",
        severity="LOW",
        action_required=False,
        reason_codes=[],
        tool_calls=[],
        suggested_actions=[],
        summary="Current request is not a policy/legal review target; no evidence search was run.",
        role="Not applicable for this query_mode",
    )


# ponytail: 아래 두 패턴은 RISK_MANDATE_WORKER_FLOW.md §11의 예시(레버리지+무경험,
# 목표-성향 불일치)만 감지하는 키워드/수치 휴리스틱이다. investment_goal은 자유 텍스트라
# 진짜 의미 모호성 판정에는 LLM 추론이 필요하다 — 지금은 명시된 두 패턴만 결정론적으로
# 잡고, 그 외 자유 텍스트 해석은 사람이 확인하도록 남겨둔다(과잉판정보다 미탐이 안전한
# 방향). 업그레이드 시점: 이 휴리스틱의 false negative가 실무에서 반복 관측될 때.
_GOAL_PRESERVATION_KEYWORDS = ("원금 보존", "원금보존", "손실 없이", "자본 보존")


def _mandate_findings(request: RiskMandateAssessmentRequest) -> list[dict[str, Any]]:
    profile = request.investor_profile
    policy = request.asset_policy
    findings: list[dict[str, Any]] = []

    if (
        profile.financial_experience_years == 0
        and profile.risk_tolerance == "CONSERVATIVE"
        and policy.leverage == "ALLOWED"
    ):
        findings.append(
            {
                "code": "EXPERIENCE_LEVERAGE_MISMATCH",
                "severity": "MEDIUM",
                "field_paths": [
                    "investor_profile.financial_experience_years",
                    "asset_policy.leverage",
                ],
                "message": "투자 경험이 없고 보수적 성향인데 레버리지가 허용되어 있습니다.",
                "question": "레버리지를 실제로 허용하려는 것이 맞습니까?",
            }
        )

    if profile.risk_tolerance == "AGGRESSIVE" and any(
        keyword in profile.investment_goal for keyword in _GOAL_PRESERVATION_KEYWORDS
    ):
        findings.append(
            {
                "code": "GOAL_RISK_TOLERANCE_MISMATCH",
                "severity": "MEDIUM",
                "field_paths": ["investor_profile.investment_goal", "investor_profile.risk_tolerance"],
                "message": "투자 목표는 원금 보존을 언급하지만 위험 성향은 공격적으로 설정되어 있습니다.",
                "question": "어느 값을 우선할지 확인해 주세요.",
            }
        )

    if policy.leverage == "ALLOWED" and policy.futures == "PROHIBITED":
        findings.append(
            {
                "code": "LEVERAGE_INSTRUMENT_UNSPECIFIED",
                "severity": "LOW",
                "field_paths": ["asset_policy.leverage", "asset_policy.futures"],
                "message": "레버리지는 허용하지만 선물은 금지되어 있습니다.",
                "question": "허용하려는 레버리지의 대상이 ETF·마진·기타 상품 중 무엇인지 확인해 주세요.",
            }
        )

    return findings


def _mandate_review(request: RiskMandateAssessmentRequest) -> dict[str, Any]:
    """Mandate 자연어 의미의 모호성·확인 필요성만 자문한다 — 법률 위반 판단은 안 한다."""

    findings = _mandate_findings(request)
    return _worker_envelope(
        request,
        status="COMPLETED",
        verdict="NEEDS_CLARIFICATION" if findings else "READY",
        severity="MEDIUM" if findings else "LOW",
        action_required=bool(findings),
        reason_codes=[f["code"] for f in findings],
        tool_calls=[],
        suggested_actions=[],
        summary="Mandate readiness review only; no legal or numeric limit determination was made.",
        role="Mandate setup readiness advisor",
        findings=findings,
        questions=[f["question"] for f in findings],
    )


def _risk_policy_review(
    request: RiskMandateAssessmentRequest,
    *,
    pinecone: PineconeEvidenceClient | None,
) -> dict[str, Any]:
    """내부 Risk 정책(Pinecone risk-compliance-policy namespace) 근거만 조회한다."""

    evidence = [item.model_dump(mode="json") for item in request.compliance_evidence]
    source = "request"
    error: str | None = None
    if not evidence and request.policy_query_vector is not None:
        try:
            query_result = query_pinecone_risk_policy(
                RiskPolicyQueryInput(
                    mandate_id=request.mandate_id,
                    query=request.compliance_query or "Risk mandate policy compliance",
                    query_vector=request.policy_query_vector,
                    as_of=_as_of_date(request),
                ),
                client=pinecone or PineconeEvidenceClient.from_env(),
            )
            source = "pinecone"
            evidence = [
                {
                    "evidence_id": match.match_id,
                    "title": match.metadata.title or "Pinecone policy evidence",
                    "text": match.text,
                    "source": "pinecone",
                    "score": match.score,
                    "violation": False,
                    "metadata": match.metadata.model_dump(mode="json"),
                }
                for match in query_result.matches
            ]
            if query_result.status != "OK":
                error = query_result.error_code or query_result.status
        except (PineconeQueryError, RuntimeError, ValueError) as exc:
            error = type(exc).__name__

    violations = [item for item in evidence if item.get("violation") is True]
    if violations:
        verdict, severity, action_required = "ESCALATE", "MEDIUM", True
        suggested_actions = [
            {
                "type": "REBALANCE_TO_POLICY",
                "evidence_id": item.get("evidence_id"),
                "mode": request.order_mode,
            }
            for item in violations
        ]
    elif evidence:
        verdict, severity, action_required = "PASS", "LOW", False
        suggested_actions = []
    else:
        verdict, severity, action_required = "ESCALATE", "MEDIUM", True
        suggested_actions = []

    return _worker_envelope(
        request,
        status="DEGRADED" if not evidence else "COMPLETED",
        verdict=verdict,
        severity=severity,
        action_required=action_required,
        reason_codes=[
            item.get("reason_code") or "POLICY_EVIDENCE_VIOLATION" for item in violations
        ],
        tool_calls=(
            ["query_pinecone_risk_policy"] if request.policy_query_vector is not None else []
        ),
        suggested_actions=suggested_actions,
        error=error,
        summary="Internal Risk policy evidence was evaluated as advisory; policy evidence never submits an order.",
        role="Point-in-time internal policy evidence monitor",
        evidence_refs=[item.get("evidence_id") for item in evidence],
        evidence_source=source,
    )


def _legal_query(
    request: RiskMandateAssessmentRequest,
    *,
    legal_answer_fn: LegalWikiAnswerFn | None,
    legal_result: LegalWikiQueryOutput | None = None,
) -> dict[str, Any]:
    """법령·행정규칙·법령해석례·판례 근거를 LLM-Wiki(Arm C)로 조회한다."""

    result = legal_result or query_legal_wiki(
        LegalWikiQueryInput(
            query=request.compliance_query or "Risk mandate legal compliance",
            mandate_context=COMPLIANCE_MANDATE_CONTEXT,
            as_of=_as_of_date(request),
        ),
        answer_fn=legal_answer_fn,
    )

    if result.status in ("UNAVAILABLE", "NO_EVIDENCE") or result.escalate:
        verdict, severity, action_required = "ESCALATE", "MEDIUM", True
    elif result.verdict == "breach":
        verdict, severity, action_required = "ESCALATE", "HIGH", True
    else:
        verdict, severity, action_required = "PASS", "LOW", False

    return _worker_envelope(
        request,
        status="DEGRADED" if result.status != "OK" else "COMPLETED",
        verdict=verdict,
        severity=severity,
        action_required=action_required,
        reason_codes=(["NO_DIRECT_LEGAL_BASIS"] if result.status == "NO_EVIDENCE" else []),
        tool_calls=["llm_wiki_grep_bm25_answer"],
        suggested_actions=[],
        error=result.error_code,
        summary="Direct legal evidence was searched via LLM-Wiki; absence of evidence is not treated as compliance.",
        role="Legal evidence analyst (law/admrul/expc/prec)",
        legal_status=result.status,
        legal_verdict=result.verdict,
        legal_rationale=result.rationale,
        cited_documents=result.cited_documents,
        pages_visited=result.pages_visited,
        confidence=result.confidence,
    )


def run_compliance_policy_worker(
    request: RiskMandateAssessmentRequest,
    *,
    pinecone: PineconeEvidenceClient | None = None,
    legal_answer_fn: LegalWikiAnswerFn | None = None,
    legal_result: LegalWikiQueryOutput | None = None,
) -> dict[str, Any]:
    """Route to the query_mode-specific advisory check; never authoritative, never binding.

    ``query_mode`` is a worker-specific execution contract set by the Risk Head
    (RISK_MANDATE_WORKER_FLOW.md §11) — risk-runner uses a separate RISK_CHECK
    contract and never shares this literal.
    """

    mode = request.query_mode
    if mode == "NOT_APPLICABLE":
        return _not_applicable_review(request)
    if mode == "MANDATE_REVIEW":
        return _mandate_review(request)
    if mode == "RISK_POLICY_REVIEW":
        return _risk_policy_review(request, pinecone=pinecone)
    if mode == "LEGAL_QUERY":
        return _legal_query(
            request,
            legal_answer_fn=legal_answer_fn,
            legal_result=legal_result,
        )

    # MIXED_REVIEW: 내부 정책과 법률 근거를 모두 조회하고, 둘 중 하나라도 확인이
    # 필요하면 ESCALATE로 합친다. 수치 한도 계산은 risk-runner가 별도로 수행한다.
    policy_report = _risk_policy_review(request, pinecone=pinecone)
    legal_report = _legal_query(
        request,
        legal_answer_fn=legal_answer_fn,
        legal_result=legal_result,
    )
    escalate = policy_report["verdict"] == "ESCALATE" or legal_report["verdict"] == "ESCALATE"
    return _worker_envelope(
        request,
        status="COMPLETED" if policy_report["status"] == "COMPLETED" and legal_report["status"] == "COMPLETED" else "DEGRADED",
        verdict="ESCALATE" if escalate else "PASS",
        severity="HIGH" if legal_report["verdict"] == "ESCALATE" else policy_report["severity"],
        action_required=bool(policy_report["action_required"] or legal_report["action_required"]),
        reason_codes=sorted(set(policy_report["reason_codes"]) | set(legal_report["reason_codes"])),
        tool_calls=sorted(set(policy_report["tool_calls"]) | set(legal_report["tool_calls"])),
        suggested_actions=[*policy_report["suggested_actions"], *legal_report["suggested_actions"]],
        error=policy_report["error"] or legal_report["error"],
        summary="Internal policy and direct legal evidence were reviewed together; risk-runner remains the sole numeric authority.",
        role="Combined internal policy + legal evidence analyst",
        sub_reports={"risk_policy_review": policy_report, "legal_query": legal_report},
    )


def synthesize_risk_head(
    risk_report: Mapping[str, Any],
    compliance_report: Mapping[str, Any],
    request: RiskMandateAssessmentRequest,
) -> dict[str, Any]:
    """Create a Hermes-head-ready fan-in without granting execution authority."""

    reports = [dict(risk_report), dict(compliance_report)]
    high_or_actionable = [report for report in reports if report.get("action_required")]
    if any(report.get("verdict") == "REJECT" for report in reports):
        decision = "REJECT"
    elif high_or_actionable:
        decision = "ESCALATE"
    else:
        decision = "APPROVE"
    manual_approval_required = request.order_mode == "MANUAL_APPROVAL" or bool(high_or_actionable)
    trace_id = request.trace_id or request.event_id or _input_hash(request)[:16]
    # docs/02-engineering/FINAL_RUNTIME_ARCHITECTURE.md §5.1.1: the Risk Head's
    # own decision vocabulary (APPROVE/ESCALATE/REJECT) already matches Task
    # decision, so this only adds the surrounding agent-task-result.v1 envelope.
    agent_task_result = build_agent_task_result(
        department="risk-management",
        worker="risk-department-head",
        case_id=request.mandate_id,
        task_id=f"{request.mandate_id}-risk-head",
        trace_id=trace_id,
        decision=decision,
        escalate=manual_approval_required,
        summary=f"Risk Head fan-in decision={decision} (manual_approval_required={manual_approval_required}).",
        reports=reports,
        model_version="deterministic:risk-head-fan-in-v1",
        reason_codes=sorted(
            {code for report in reports for code in report.get("reason_codes", [])}
        ),
    ).model_dump(mode="json", exclude_none=True)
    return {
        "decision": decision,
        "execution_mode": request.order_mode,
        "manual_approval_required": manual_approval_required,
        "binding": False,
        "safe_action": "HOLD" if decision != "APPROVE" else "NO_ACTION",
        "recommended_actions": [
            action
            for report in reports
            for action in report.get("suggested_actions", [])
        ],
        "reports": reports,
        "agent_task_result": agent_task_result,
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
        "trace_id": request.trace_id or request.event_id or digest[:16],
        "input_hash": digest,
        "worker_inputs": {
            "risk-runner": {"mandate": mandate, "input_hash": digest},
            "compliance-policy-worker": {"mandate": mandate, "input_hash": digest},
        },
        "mutation_allowed": False,
    }


def _build_risk_head_state(
    request: RiskMandateAssessmentRequest,
    dispatch: Mapping[str, Any],
    risk_report: Mapping[str, Any],
    compliance_report: Mapping[str, Any],
    risk_head: Mapping[str, Any],
    employee_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Persistable Risk Head state assembled after independent worker fan-in."""

    trace_id = str(dispatch.get("trace_id") or _input_hash(request)[:16])
    route = compliance_report.get("routing", {})
    runtime_workers = {
        str(worker.get("worker_id")): worker
        for worker in employee_runtime.get("workers", [])
        if isinstance(worker, Mapping)
    }
    return {
        "schema_version": "risk-head-state.v1",
        "run_id": f"RISK-{_input_hash(request)[:16]}",
        "trace_id": trace_id,
        "request": {
            "intent": "PRE_TRADE" if request.order_compliance else "MANDATE_REVIEW",
            "user_question": request.compliance_query or "Mandate Risk assessment",
        },
        "mandate": request.model_dump(mode="json"),
        "context": {
            "as_of": request.as_of,
            "order": request.order_compliance.model_dump(mode="json")
            if request.order_compliance
            else None,
            "portfolio_snapshot": request.portfolio_snapshot.model_dump(mode="json")
            if request.portfolio_snapshot
            else None,
        },
        "routing": route,
        "worker_tasks": {
            "risk-runner": {
                "schema_version": "risk-runner.task.v1",
                "input_hash": risk_report.get("input_hash"),
                "dispatch": "REQUIRED",
                "mode": "RISK_CHECK",
            },
            "compliance-policy-worker": {
                "schema_version": "compliance-policy.task.v1",
                "input_hash": compliance_report.get("input_hash"),
                "query_mode": compliance_report.get("query_mode"),
                "dispatch": "REQUIRED",
            },
        },
        "worker_results": {
            "risk-runner": dict(risk_report),
            "compliance-policy-worker": dict(compliance_report),
            "employee_runtime": runtime_workers,
        },
        "decision": dict(risk_head),
        "escalation": {
            "required": risk_head.get("decision") != "APPROVE",
            "reason_codes": sorted(
                set(risk_report.get("reason_codes", []))
                | set(compliance_report.get("reason_codes", []))
            ),
        },
        "audit": {
            "input_hash": _input_hash(request),
            "dispatch": dict(dispatch),
            "runtime": employee_runtime.get("runtime", {}),
            "trace": {
                worker_id: worker.get("trace", {})
                for worker_id, worker in runtime_workers.items()
            },
        },
    }


def assess_mandate(
    request: RiskMandateAssessmentRequest | Mapping[str, Any],
    *,
    pinecone: PineconeEvidenceClient | None = None,
    employee_llm: Any | None = None,
    legal_answer_fn: LegalWikiAnswerFn | None = None,
) -> dict[str, Any]:
    """Run both Risk employees and return their independent reports plus fan-in."""

    normalized = (
        request
        if isinstance(request, RiskMandateAssessmentRequest)
        else RiskMandateAssessmentRequest.model_validate(request)
    )
    dispatch = build_risk_head_dispatch(normalized)
    fanout_started = time.perf_counter()

    def _run_timed_risk() -> tuple[dict[str, Any], float]:
        started = time.perf_counter()
        report = run_risk_runner(normalized)
        return report, (time.perf_counter() - started) * 1000

    def _run_timed_compliance() -> tuple[dict[str, Any], float]:
        started = time.perf_counter()
        runtime = _run_compliance_employee_graph(
            normalized,
            None,
            employee_llm=employee_llm,
            legal_answer_fn=legal_answer_fn,
        )
        return runtime, (time.perf_counter() - started) * 1000

    # The deterministic mandate/risk checks and the compliance worker (which
    # owns LLM-Wiki retrieval when the route requires it) have no data
    # dependency at this boundary. Keep them in the same two-way fan-out so a
    # slow legal/model path cannot serialize the numeric risk path.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="risk-mandate") as pool:
        risk_future = pool.submit(_run_timed_risk)
        compliance_future = pool.submit(_run_timed_compliance)
        risk_report, risk_runner_ms = risk_future.result()
        employee_runtime, compliance_worker_ms = compliance_future.result()

    fanout_wall_ms = (time.perf_counter() - fanout_started) * 1000
    timing = {
        "risk_runner_ms": round(risk_runner_ms, 2),
        "compliance_worker_ms": round(compliance_worker_ms, 2),
        "fanout_wall_ms": round(fanout_wall_ms, 2),
        "estimated_serial_ms": round(risk_runner_ms + compliance_worker_ms, 2),
        "estimated_parallel_saving_ms": round(
            max(0.0, risk_runner_ms + compliance_worker_ms - fanout_wall_ms),
            2,
        ),
    }
    runtime_worker = next(
        (
            worker
            for worker in employee_runtime.get("workers", [])
            if worker.get("worker_id") == "compliance-policy-worker"
        ),
        {},
    )
    generated = runtime_worker.get("output", {})
    resolved_mode = generated.get("query_mode") or normalized.query_mode
    routed_request = normalized.model_copy(update={"query_mode": resolved_mode})

    # The compliance graph already ran the legal tool for LEGAL_QUERY and
    # MIXED_REVIEW. Rehydrate that validated tool result instead of issuing a
    # second LLM-Wiki request while building the public compliance envelope.
    cached_legal_result: LegalWikiQueryOutput | None = None
    tool_output = runtime_worker.get("tool_output")
    if isinstance(tool_output, Mapping) and isinstance(tool_output.get("legal"), Mapping):
        try:
            cached_legal_result = LegalWikiQueryOutput.model_validate(tool_output["legal"])
        except Exception:  # noqa: BLE001 - malformed observability/cache data is fail-open
            cached_legal_result = None
    compliance_report = run_compliance_policy_worker(
        routed_request,
        pinecone=pinecone,
        legal_answer_fn=legal_answer_fn,
        legal_result=cached_legal_result,
    )
    legal_route = resolved_mode in ("LEGAL_QUERY", "MIXED_REVIEW")
    timing["legal_lookup_reused"] = bool(cached_legal_result)
    timing["legal_lookup_count"] = 1 if legal_route else 0
    employee_runtime["timing"] = timing
    compliance_report["routing"] = {
        "query_mode": resolved_mode,
        "routing_rationale": generated.get("routing_rationale"),
        "routing_by_llm": generated.get("routing_by_llm", False),
        "source": "compliance-policy-worker",
    }
    generation_degraded = bool(employee_runtime.get("generation_degraded"))
    compliance_report["generation_status"] = (
        "DEGRADED"
        if generation_degraded
        else runtime_worker.get("status", "DEGRADED")
    )
    compliance_report["generated_answer"] = generated.get("summary", "")
    compliance_report["generation_trace"] = runtime_worker.get("trace", {})
    if generated.get("escalate") or generation_degraded:
        compliance_report["verdict"] = "ESCALATE"
        compliance_report["action_required"] = True
        compliance_report.setdefault("reason_codes", []).append(
            "COMPLIANCE_ANSWER_REQUIRES_ESCALATION"
        )
    if runtime_worker.get("status") != "COMPLETED":
        compliance_report["status"] = "DEGRADED"
        compliance_report["verdict"] = "ESCALATE"
        compliance_report["action_required"] = True
        compliance_report.setdefault("reason_codes", []).append(
            "COMPLIANCE_ANSWER_GENERATION_FAILED"
        )
    risk_head = synthesize_risk_head(risk_report, compliance_report, normalized)
    risk_head_state = _build_risk_head_state(
        normalized,
        dispatch,
        risk_report,
        compliance_report,
        risk_head,
        employee_runtime,
    )
    pipeline_status = (
        "COMPLETED"
        if risk_report["status"] == "COMPLETED"
        and compliance_report["status"] == "COMPLETED"
        and not employee_runtime.get("degraded")
        else "DEGRADED"
    )
    return {
        "schema_version": "risk-assessment.v1",
        "mandate_id": normalized.mandate_id,
        "trace_id": normalized.trace_id
        or normalized.event_id
        or _input_hash(normalized)[:16],
        "pipeline_status": pipeline_status,
        "status": pipeline_status,
        "decision": risk_head["decision"],
        "authoritative_source": "risk-runner",
        "dispatch": dispatch,
        "employees": {
            "risk-runner": risk_report,
            "compliance-policy-worker": compliance_report,
        },
        "tool_calls": sorted(
            set(risk_report.get("tool_calls", []))
            | set(compliance_report.get("tool_calls", []))
        ),
        "risk_head": risk_head,
        "risk_head_state": risk_head_state,
        "employee_runtime": employee_runtime,
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
    "classify_compliance_query_mode",
    "run_compliance_policy_worker",
    "run_risk_runner",
    "synthesize_risk_head",
]
