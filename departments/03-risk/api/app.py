#!/usr/bin/env python3
"""Risk Domain API — risk_engine.py/trading_state_store.py/agentic-rag를 감싸는 FastAPI 래퍼.

소유: 동규 (리스크본부)
근거: docs/02-engineering/RISK_QA_DOMAIN_API_SPEC.md 2절, 8절 "다음 작업 제안 순서" (2)
      docs/02-engineering/TECH_STACK_DECISIONS.md 7절(Hermes는 Domain 서비스를 API/MCP
      경계로만 부른다 - 같은 프로세스에 직접 import하지 않는다)

여기엔 새 판정 로직이 없다. `RiskContextIn.to_context()`는 JSON을 `RiskContext`
데이터클래스로 바꾸는 변환일 뿐이고, 승인/축소/거부는 전부 `RiskEngine.check_order`가 한다
(CLAUDE.md "모든 주문은 결정론적 Risk Engine을 통과한다").

`/risk/v1/trading-state/*`는 REDIS_URL이, `/risk/v1/compliance/check`는 OPENAI_API_KEY와
네트워크가 필요하다 - trading_state_store.py/skills/agentic-rag 자체 점검과 같은 이유로
이 파일의 __main__ 점검에서는 뺐다(각 모듈 자체 점검이 이미 그 경로를 검증한다).

`PUT /risk/v1/trading-state/{scope}`의 `set_by` 인증(Authorized Operator 대조)은 Service
Token 발급 주체가 아직 미정이라(스펙 6절) 여기서 검증하지 않는다 - 지금은 reason 필수
기록까지만 한다.

실행: uvicorn app:app --app-dir departments/03-risk/api
자체 점검: python departments/03-risk/api/app.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

_RISK_DIR = Path(__file__).resolve().parent.parent
_ENGINE_DIR = _RISK_DIR / "engine"
_CONTRACTS_DIR = (
    Path(__file__).resolve().parent.parent.parent / "02-trading" / "contracts"
)
_AGENTIC_RAG_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent / "skills" / "agentic-rag"
)


def _configured_policy_corpus() -> Path:
    """Resolve the real policy corpus configured for the Risk compliance gate."""

    configured = (
        os.environ.get("RISK_POLICY_CORPUS_DIR", "").strip()
        or os.environ.get("QA_POLICY_CORPUS_DIR", "").strip()
    )
    return (
        Path(configured).expanduser().resolve()
        if configured
        else _AGENTIC_RAG_DIR / "corpus" / "compliance"
    )


for _p in (_RISK_DIR, _ENGINE_DIR, _CONTRACTS_DIR, _AGENTIC_RAG_DIR):
    sys.path.insert(0, str(_p))

from contracts import OrderIntent
from p1.analytics import (
    KillSwitchState,
    P1GateDecision,
    P1RiskSnapshot,
    evaluate_p1_gate,
)
from p1_runtime_api import router as p1_runtime_router
from p2_derivatives_api import router as p2_derivatives_router
from risk_context_repository import PostgresRiskContextRepository, RiskContextLoadError
from risk_engine import (
    CounterpartyHealth,
    CounterpartyStatus,
    LimitSet,
    MandateScope,
    MarketStatus,
    PortfolioState,
    RestrictedItem,
    RestrictionType,
    RiskContext,
    RiskEngine,
    RiskEngineError,
    TradingState,
)

# --- Request 모델 (RiskContext 데이터클래스 트리를 JSON에서 그대로 재구성) ------------------
from risk_events.redis_event_bus import (
    RedisEventPublisher,
    RiskEventBusError,
    decision_event_id,
)
from risk_mandate_workers import RiskMandateAssessmentRequest, assess_mandate
from risk_repository import RiskDecisionPersistenceError, RiskDecisionRepository
from trading_state_store import (
    RedisTradingStateStore,
    TradingStateStoreError,
)


class MandateScopeIn(BaseModel):
    fund_id: UUID
    allowed_instrument_ids: list[UUID] | None = None
    min_order_notional: Decimal
    max_order_notional: Decimal
    max_instrument_weight: Decimal | None = None
    max_sector_weight: Decimal | None = None
    max_gross_exposure: Decimal | None = None
    allowed_asset_classes: list[str] | None = None
    forbidden_asset_classes: list[str] = []
    preferred_sectors: list[str] = []
    excluded_sectors: list[str] = []


class LimitSetIn(BaseModel):
    soft_single_issuer_pct: Decimal
    hard_single_issuer_pct: Decimal
    max_daily_turnover_notional: Decimal
    max_daily_order_count: int
    max_daily_loss: Decimal
    max_drawdown_pct: Decimal


class RestrictedItemIn(BaseModel):
    instrument_id: UUID
    restriction_type: RestrictionType
    effective_from: datetime
    effective_to: datetime | None = None


class PortfolioStateIn(BaseModel):
    fund_id: UUID
    cash: Decimal
    buying_power: Decimal
    gross_exposure: Decimal
    positions: dict[UUID, Decimal] = {}
    issuer_of: dict[UUID, str] = {}
    issuer_exposure: dict[str, Decimal] = {}
    instrument_asset_class: dict[UUID, str] = {}
    instrument_sector: dict[UUID, str] = {}
    sector_exposure: dict[str, Decimal] = {}
    realized_pnl_today: Decimal = Decimal(0)
    unrealized_pnl_today: Decimal = Decimal(0)
    peak_equity: Decimal = Decimal(0)
    equity: Decimal = Decimal(0)
    orders_today: int = 0
    notional_traded_today: Decimal = Decimal(0)


class MarketStatusIn(BaseModel):
    tradable: bool
    reason: str = ""


class CounterpartyStatusIn(BaseModel):
    broker_adapter: str
    health: CounterpartyHealth


class RiskContextIn(BaseModel):
    mandate: MandateScopeIn
    limits: LimitSetIn
    restricted_items: list[RestrictedItemIn] = []
    portfolio: PortfolioStateIn
    market_status: MarketStatusIn
    counterparty: CounterpartyStatusIn
    trading_state: TradingState
    as_of: datetime

    def to_context(self) -> RiskContext:
        return RiskContext(
            mandate=MandateScope(
                fund_id=self.mandate.fund_id,
                allowed_instrument_ids=(
                    frozenset(self.mandate.allowed_instrument_ids)
                    if self.mandate.allowed_instrument_ids is not None
                    else None
                ),
                min_order_notional=self.mandate.min_order_notional,
                max_order_notional=self.mandate.max_order_notional,
                max_instrument_weight=self.mandate.max_instrument_weight,
                max_sector_weight=self.mandate.max_sector_weight,
                max_gross_exposure=self.mandate.max_gross_exposure,
                allowed_asset_classes=(
                    frozenset(self.mandate.allowed_asset_classes)
                    if self.mandate.allowed_asset_classes is not None
                    else None
                ),
                forbidden_asset_classes=frozenset(self.mandate.forbidden_asset_classes),
                preferred_sectors=frozenset(self.mandate.preferred_sectors),
                excluded_sectors=frozenset(self.mandate.excluded_sectors),
            ),
            limits=LimitSet(**self.limits.model_dump()),
            restricted_items=tuple(
                RestrictedItem(**item.model_dump()) for item in self.restricted_items
            ),
            portfolio=PortfolioState(**self.portfolio.model_dump()),
            market_status=MarketStatus(**self.market_status.model_dump()),
            counterparty=CounterpartyStatus(**self.counterparty.model_dump()),
            trading_state=self.trading_state,
            as_of=self.as_of,
        )


class P1RiskSnapshotIn(BaseModel):
    fund_id: UUID
    book_id: UUID | None = None
    strategy_version_id: UUID | None = None
    as_of: datetime
    gross_exposure: float
    net_exposure: float
    value_at_risk: float | None = None
    expected_shortfall: float | None = None
    stress_losses: dict[str, float] = {}
    correlation_shock_loss: float | None = None
    correlation_max: float | None = None
    quality_status: str
    input_hash: str = Field(min_length=1)
    calculation_version: str = Field(min_length=1)
    kill_switch_state: KillSwitchState
    breaches: list[str] = []
    exposure_components: list[dict] = []

    def to_snapshot(self) -> P1RiskSnapshot:
        return P1RiskSnapshot(
            fund_id=self.fund_id,
            book_id=self.book_id,
            strategy_version_id=self.strategy_version_id,
            as_of=self.as_of,
            gross_exposure=self.gross_exposure,
            net_exposure=self.net_exposure,
            value_at_risk=self.value_at_risk,
            expected_shortfall=self.expected_shortfall,
            stress_losses=self.stress_losses,
            correlation_shock_loss=self.correlation_shock_loss,
            correlation_max=self.correlation_max,
            quality_status=self.quality_status,
            input_hash=self.input_hash,
            calculation_version=self.calculation_version,
            kill_switch_state=self.kill_switch_state,
            breaches=tuple(self.breaches),
            exposure_components=tuple(self.exposure_components),
        )


class RiskCheckRequest(BaseModel):
    risk_request_id: UUID | None = None
    order_intent: OrderIntent
    context: RiskContextIn | None = None
    p1_snapshot: P1RiskSnapshotIn | None = None


class TradingStateBody(BaseModel):
    state: TradingState
    reason: str = Field(min_length=1)
    set_by: str = Field(min_length=1)


def _require_service_token(
    authorization: str | None,
    *,
    required_scope: str,
    expected_subject: str | None = None,
):
    from apps.security.service_auth import ServiceAuthError, authenticate_service_token

    try:
        return authenticate_service_token(
            authorization,
            required_scope=required_scope,
            expected_department="risk-management",
            expected_service="risk-api",
            expected_subject=expected_subject,
            secret_env="RISK_SERVICE_AUTH_SECRET",
            issuer_env="RISK_SERVICE_AUTH_ISSUER",
            audience_env="RISK_SERVICE_AUTH_AUDIENCE",
        )
    except ServiceAuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.code, "message": str(exc)},
        ) from exc


class ComplianceCheckRequest(BaseModel):
    query: str = Field(min_length=1)
    as_of: str


class MandateAssessmentResponse(BaseModel):
    schema_version: str
    mandate_id: str
    trace_id: str
    pipeline_status: str
    status: str
    decision: str
    authoritative_source: str
    dispatch: dict[str, object]
    employees: dict[str, dict[str, object]]
    tool_calls: list[str]
    risk_head: dict[str, object]


# --- App -------------------------------------------------------------------------


app = FastAPI(title="Risk Domain API", version="v1")
app.include_router(p1_runtime_router)
app.include_router(p2_derivatives_router)
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi.responses import Response

from apps.observability.risk_qa import get_runtime_telemetry

RISK_TELEMETRY = get_runtime_telemetry("risk")
engine = RiskEngine()
_state_store: RedisTradingStateStore | None = None
_decision_repository: RiskDecisionRepository | None = None
_event_publisher: RedisEventPublisher | None = None


def _canonical_database_url() -> str:
    """Select a writable Risk/QA DSN without using the portfolio read-only DSN."""

    return (
        os.environ.get("RISK_QA_DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )


def _risk_decision_repository() -> RiskDecisionRepository | None:
    """DATABASE_URL이 주입된 Domain Runtime에서만 Canonical DB를 사용한다."""

    global _decision_repository
    dsn = _canonical_database_url()
    if not dsn:
        return None
    if _decision_repository is None:
        try:
            _decision_repository = RiskDecisionRepository.connect(dsn)
        except Exception as exc:
            raise RiskDecisionPersistenceError(
                "Canonical Risk DB connection failed; decision was not persisted"
            ) from exc
    return _decision_repository


def _risk_event_publisher() -> RedisEventPublisher | None:
    """Risk→QA Stream publisher를 지연 생성한다."""

    global _event_publisher
    redis_url = os.environ.get("RISK_QA_EVENT_REDIS_URL") or os.environ.get("REDIS_URL")
    if not redis_url:
        return None
    if _event_publisher is None:
        import redis

        _event_publisher = RedisEventPublisher(
            redis.Redis.from_url(redis_url),
            stream=os.environ.get("RISK_QA_EVENT_STREAM", "risk-qa-events"),
        )
    return _event_publisher


def _persist_risk_decision(case_id: str, trace_id: UUID, assessment) -> None:
    """DB 기록과 Event 발행을 모두 마쳐야 Risk API가 성공한다."""

    repository = _risk_decision_repository()
    if repository is None:
        return
    risk_decision_id = repository.save(assessment)
    publisher = _risk_event_publisher()
    if publisher is None:
        raise RiskEventBusError(
            "Canonical Risk DB는 연결됐지만 Risk→QA Event Bus가 없습니다"
        )
    publisher.publish(
        event_id=decision_event_id(
            risk_request_id=assessment.risk_request_id,
            input_hash=assessment.input_hash,
            calculation_version=assessment.calculation_version,
        ),
        trace_id=trace_id,
        payload={
            "case_id": case_id,
            "risk_decision_id": str(risk_decision_id),
            "risk_request_id": str(assessment.risk_request_id),
            "order_intent_id": str(assessment.decision.order_intent_id),
            "decision": assessment.decision.verdict.value,
            "approved_quantity": assessment.decision.approved_quantity,
            "input_hash": assessment.input_hash,
            "calculation_version": assessment.calculation_version,
            "trace_id": str(trace_id),
        },
    )


def _redis_store() -> RedisTradingStateStore:
    global _state_store
    if _state_store is None:
        import os

        import redis as redis_lib

        _state_store = RedisTradingStateStore(
            redis_lib.Redis.from_url(os.environ["REDIS_URL"])
        )
    return _state_store


@app.exception_handler(RiskEngineError)
def _on_risk_engine_error(request, exc: RiskEngineError):
    return JSONResponse(
        status_code=422,
        content={
            "error_code": type(exc).__name__,
            "message": str(exc),
            "detail": {},
            "trace_id": None,
        },
    )


@app.exception_handler(TradingStateStoreError)
def _on_trading_state_store_error(request, exc: TradingStateStoreError):
    return JSONResponse(
        status_code=503,
        content={
            "error_code": type(exc).__name__,
            "message": str(exc),
            "detail": {},
            "trace_id": None,
        },
    )


@app.exception_handler(RiskDecisionPersistenceError)
def _on_risk_persistence_error(request, exc: RiskDecisionPersistenceError):
    return JSONResponse(
        status_code=503,
        content={
            "error_code": type(exc).__name__,
            "message": str(exc),
            "detail": {},
            "trace_id": None,
        },
    )


@app.exception_handler(RiskEventBusError)
def _on_risk_event_bus_error(request, exc: RiskEventBusError):
    return JSONResponse(
        status_code=503,
        content={
            "error_code": type(exc).__name__,
            "message": str(exc),
            "detail": {},
            "trace_id": None,
        },
    )


@app.exception_handler(RequestValidationError)
def _on_validation_error(request, exc: RequestValidationError):
    # 스펙 1.4 에러 봉투를 요청 스키마 검증 실패에도 그대로 적용 — FastAPI 기본 {"detail": [...]}
    # 형태를 그대로 내보내면 Hermes 클라이언트가 에러 봉투를 두 가지로 파싱해야 한다.
    return JSONResponse(
        status_code=422,
        content={
            "error_code": "RequestValidationError",
            "message": "요청 스키마 검증 실패",
            "detail": {"errors": jsonable_encoder(exc.errors())},
            "trace_id": None,
        },
    )


@app.post("/investment-cases/{case_id}/risk-check")
def risk_check(case_id: str, body: RiskCheckRequest):
    use_database_context = (
        os.environ.get("RISK_QA_RUNTIME", "").strip().lower() == "production"
        or os.environ.get("RISK_CONTEXT_SOURCE", "request").strip().lower()
        == "database"
    )
    if use_database_context:
        dsn = _canonical_database_url()
        broker_adapter = os.environ.get("RISK_BROKER_ADAPTER", "").strip()
        if not dsn or not broker_adapter:
            raise HTTPException(
                status_code=503,
                detail={
                    "error_code": "RISK_CANONICAL_CONTEXT_NOT_CONFIGURED",
                    "reason": "DATABASE_URL and RISK_BROKER_ADAPTER are required",
                },
            )
        try:
            trading_state = _redis_store().get_state_fail_closed(
                f"fund:{body.order_intent.fund_id}"
            )
            context = PostgresRiskContextRepository.connect(dsn).load(
                fund_id=body.order_intent.fund_id,
                book_id=body.order_intent.book_id,
                instrument_id=body.order_intent.instrument_id,
                broker_adapter=broker_adapter,
                as_of=body.order_intent.snapshot.as_of,
                trading_state=trading_state,
            )
        except RiskContextLoadError as exc:
            raise HTTPException(
                status_code=503,
                detail={"error_code": "RISK_CANONICAL_CONTEXT_UNAVAILABLE"},
            ) from exc
    elif body.context is not None:
        context = body.context.to_context()
    else:
        raise HTTPException(
            status_code=503, detail={"error_code": "RISK_CONTEXT_REQUIRED"}
        )

    assessment = engine.check_order(body.order_intent, context, body.risk_request_id)
    if os.environ.get("RISK_REQUIRE_P1_ANALYTICS", "false").strip().lower() == "true":
        if body.p1_snapshot is None:
            raise HTTPException(
                status_code=503, detail="P1 risk analytics snapshot is required"
            )
        if evaluate_p1_gate(body.p1_snapshot.to_snapshot()) is not P1GateDecision.PASS:
            raise HTTPException(
                status_code=409, detail="P1 risk analytics gate rejected entry"
            )
    _persist_risk_decision(case_id, body.order_intent.trace_id, assessment)
    return assessment


@app.get("/risk/v1/trading-state/{scope}")
def get_trading_state(scope: str):
    return {"scope": scope, "state": _redis_store().get_state_fail_closed(scope)}


@app.get("/risk/v1/trading-state/{scope}/record")
def get_trading_state_record(scope: str):
    record = _redis_store().get_record(scope)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"'{scope}'는 설정된 적 없는 Trading State입니다"
        )
    return record


@app.put("/risk/v1/trading-state/{scope}")
def put_trading_state(
    scope: str,
    body: TradingStateBody,
    authorization: str | None = Header(default=None),
):
    _require_service_token(
        authorization,
        required_scope="risk.trading_state.write",
        expected_subject=body.set_by,
    )
    return _redis_store().set_state(scope, body.state, body.reason, body.set_by)


@app.delete("/risk/v1/trading-state/{scope}")
def delete_trading_state(scope: str, authorization: str | None = Header(default=None)):
    identity = _require_service_token(
        authorization,
        required_scope="risk.trading_state.clear",
    )
    _redis_store().clear_state(scope)
    return {"scope": scope, "cleared": True, "cleared_by": identity.subject}


@app.post("/risk/v1/compliance/check")
def compliance_check(body: ComplianceCheckRequest):
    from src.graph import (
        run_compliance_check,  # 지연 import - langgraph/OpenAI는 호출 시점에만 필요
    )

    corpus_dir = _configured_policy_corpus()
    if os.environ.get("RISK_QA_RUNTIME", "").strip().lower() == "production":
        paths = tuple(path for path in corpus_dir.glob("*.md") if path.is_file())
        if not paths or any(
            b"SAMPLE_PLACEHOLDER" in path.read_bytes() for path in paths
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "error_code": "RISK_POLICY_CORPUS_NOT_READY",
                    "reason": "real policy corpus is required before production compliance checks",
                },
            )
    return run_compliance_check(
        body.query,
        body.as_of,
        corpus_dir=corpus_dir,
        persona="compliance-policy-agent",
    )


@app.post(
    "/risk/v1/mandates/{mandate_id}/assess", response_model=MandateAssessmentResponse
)
def assess_mandate_for_risk_head(mandate_id: str, body: RiskMandateAssessmentRequest):
    """Dispatch one immutable user mandate to both Risk employees.

    The Risk Head receives the two independent reports only after both
    employee boundaries have run. This endpoint is advisory and never creates
    an Order or calls a broker.
    """

    if mandate_id != body.mandate_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "MANDATE_ID_MISMATCH",
                "path_mandate_id": mandate_id,
                "body_mandate_id": body.mandate_id,
            },
        )
    return assess_mandate(body)


@app.get("/risk/v1/observability/rag")
def rag_observability():
    from src.resilience import latency_summary

    return {
        "nodes": {
            node: latency_summary(node)
            for node in ("retrieve", "grade", "generate", "hallucination_check")
        }
    }


@app.get("/risk/v1/observability/runtime")
def runtime_observability():
    return RISK_TELEMETRY.snapshot()


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics():
    return Response(
        content=RISK_TELEMETRY.prometheus_text(),
        media_type="text/plain; version=0.0.4",
    )


if __name__ == "__main__":
    from datetime import timedelta, timezone
    from uuid import uuid4

    from contracts import RiskVerdict
    from fastapi.testclient import TestClient

    now = datetime.now(timezone.utc)
    fund, book, strategy, aapl = uuid4(), uuid4(), uuid4(), uuid4()

    def order_intent_payload(qty="100", side="BUY") -> dict:
        return {
            "trade_case_id": str(uuid4()),
            "fund_id": str(fund),
            "book_id": str(book),
            "strategy_id": str(strategy),
            "instrument_id": str(aapl),
            "side": side,
            "order_type": "LIMIT",
            "quantity": qty,
            "limit_price": "70000",
            "time_in_force": "DAY",
            "valid_until": (now + timedelta(hours=1)).isoformat(),
            "snapshot": {
                "market_snapshot_id": "s1",
                "as_of": now.isoformat(),
                "bid": "69900",
                "ask": "70000",
            },
            "idempotency_key": "idem_api_001",
            "created_by": "trader-pm-agent",
            "trace_id": "t1",
        }

    def context_payload(**overrides) -> dict:
        payload = {
            "mandate": {
                "fund_id": str(fund),
                "allowed_instrument_ids": None,
                "min_order_notional": "100000",
                "max_order_notional": "50000000",
            },
            "limits": {
                "soft_single_issuer_pct": "0.20",
                "hard_single_issuer_pct": "0.30",
                "max_daily_turnover_notional": "100000000",
                "max_daily_order_count": 50,
                "max_daily_loss": "10000000",
                "max_drawdown_pct": "0.20",
            },
            "restricted_items": [],
            "portfolio": {
                "fund_id": str(fund),
                "cash": "100000000",
                "buying_power": "100000000",
                "gross_exposure": "100000000",
                "peak_equity": "1000000000",
                "equity": "1000000000",
            },
            "market_status": {"tradable": True},
            "counterparty": {"broker_adapter": "paper", "health": "ok"},
            "trading_state": "ENABLED",
            "as_of": now.isoformat(),
        }
        payload.update(overrides)
        return payload

    client = TestClient(app)

    # 1. 정상 주문 -> 200, APPROVE
    r1 = client.post(
        "/investment-cases/case-1/risk-check",
        json={"order_intent": order_intent_payload(), "context": context_payload()},
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["decision"]["verdict"] == RiskVerdict.APPROVE.value, body1
    assert body1["calculation_version"] == "risk-p0-v1"
    assert len(body1["check_results"]) == 10

    # 2. HALTED 상태 -> 200, REJECT + reason_codes에 사유가 남는다 (엔진이 REJECT를 그대로 반환)
    r2 = client.post(
        "/investment-cases/case-1/risk-check",
        json={
            "order_intent": order_intent_payload(),
            "context": context_payload(trading_state="HALTED"),
        },
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["decision"]["verdict"] == RiskVerdict.REJECT.value, body2
    assert "trading_state_blocked" in body2["reason_codes"]

    # 3. 잘못된 trading_state 값 -> 422 (Pydantic이 Enum 검증에서 이미 막음)
    r3 = client.post(
        "/investment-cases/case-1/risk-check",
        json={
            "order_intent": order_intent_payload(),
            "context": context_payload(trading_state="NOT_A_STATE"),
        },
    )
    assert r3.status_code == 422, r3.text
    assert r3.json()["error_code"] == "RequestValidationError", (
        r3.json()
    )  # 스펙 1.4 봉투 확인

    # 4. risk_request_id를 지정하면 응답에 그대로 반영된다 (멱등키)
    fixed_id = str(uuid4())
    r4 = client.post(
        "/investment-cases/case-1/risk-check",
        json={
            "risk_request_id": fixed_id,
            "order_intent": order_intent_payload(qty="101"),
            "context": context_payload(),
        },
    )
    assert r4.json()["risk_request_id"] == fixed_id

    print(
        "ok - Risk Domain API 4개 시나리오 점검 통과 (trading-state/compliance는 REDIS_URL/OPENAI_API_KEY 필요 - 제외)"
    )
