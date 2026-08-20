#!/usr/bin/env python3
"""Trading Domain API — contracts.py/oms.py/paper_broker.py를 감싸는 FastAPI 래퍼.

소유: 도현 (트레이딩본부)
근거: docs/02-engineering/TRADING_DOMAIN_API_SPEC.md
      docs/02-engineering/TECH_STACK_DECISIONS.md 7절(Hermes는 Domain 서비스를 API/MCP
      경계로만 부른다 - 같은 프로세스에 직접 import하지 않는다)
      docs/01-product/MINIMUM_SERVICE_UNIT_SPEC.md 11절(Case API 경로 이름)
      docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md (v1.2) 4.2, 4.3

여기엔 새 주문 판정 로직이 없다. 상태 전이·멱등·수량 검증은 전부 `oms.py`가 하고
이 파일은 JSON <-> 도메인 객체 변환과 에러 매핑만 한다. 리스크본부 판정은 만들지
않고 받아서 기록만 한다(`apply_risk_decision`).

**권한 경계 — 이 API가 하지 않는 것** (CLAUDE.md, 팀 가이드 4.3):
  - Risk 판정을 스스로 만들지 않는다. `POST .../risk-decision`은 리스크본부가 준
    RiskDecision을 그대로 받아 검증·기록만 한다. verdict를 여기서 계산하지 않는다.
  - 브로커 응답을 추정하지 않는다. 상태는 `POST .../broker-events`로 들어온 사실로만
    바뀐다. Fill/Cancel을 우리가 지어내는 경로가 없다.
  - 원장(Journal/Position/NAV)을 쓰지 않는다. 회계본부 API의 몫이다.

**Agent는 이 API의 직접 호출자가 아니다.** Hermes 페르소나가 부를 수 있는 것은 MCP
도구 면(예정)이고, 거기에는 `propose_order_intent`만 노출한다 - `submit`은 주지 않는다
(CLAUDE.local.md 원칙 1). 이 API는 서비스 호출자(BFF, 부서 워커)용이며, 그래도 안전한
이유는 불변식이 HTTP 계층이 아니라 `oms.py`에 있기 때문이다 - Risk 판정 없는 submit은
누가 부르든 OMSError다.

⚠ **상태는 프로세스 메모리다.** `OMS(store=OrderStore())`가 dict 기반이라 이 API를
재시작하면 주문이 사라지고, 다른 프로세스(BFF·회계)는 이 상태를 볼 수 없다.
`execution.order_intents`/`orders` 테이블은 Supabase에 이미 있고 행이 0이다 -
psycopg OrderStore 구현은 팀장 확인 대기 항목이다(CLAUDE.local.md "팀장님께 여쭤볼 것").
그 전까지 이 API는 **단일 프로세스 Paper 용도**이며, 응답의 `authoritative` 필드가
그 사실을 계약으로 표시한다.

실행: uvicorn app:app --app-dir departments/02-trading/api
자체 점검: python departments/02-trading/api/app.py
"""
from __future__ import annotations

import sys
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

_DEPT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DEPT))
for _sub in ("contracts", "oms", "broker", "multileg", "capability"):
    sys.path.insert(0, str(_DEPT / _sub))

from contracts import (
    LOT_SIZE,
    BrokerOrderState,
    ExecutionAuthority,
    MarketSnapshot,
    OrderIntent,
    OrderType,
    RiskDecision,
    Side,
    TimeInForce,
    tick_size,
)
from oms import OMS, BrokerOrder, OMSError, OrderIntentRecord
from oms import OrderStore
from store_postgres import OrderStorePersistenceError, PostgresOrderStore
from paper_broker import PaperBroker, Quote
from directive_routes import configure_directive_runtime, router as directive_router
from conditional_rule_routes import router as conditional_rule_router
from directives.service import DirectiveServiceError, require_paper_execution_mode
from internal_service_auth import (
    BROKER_EVENT_POLICY,
    CASE_PAPER_ORDER_POLICY,
    INTENT_WRITER_POLICY,
    ORDER_CANCEL_POLICY,
    ORDER_SUBMIT_POLICY,
    RISK_DECISION_POLICY,
    InternalServiceAuthError,
    InternalServiceIdentity,
    MutationAuthPolicy,
    authenticate_internal_service,
    required_internal_auth_config,
)

API_VERSION = "v1"

app = FastAPI(title="Trading Domain API", version=API_VERSION)
app.include_router(directive_router)
app.include_router(conditional_rule_router)


@app.on_event("startup")
def _validate_trading_runtime() -> None:
    """Fail startup unless the authenticated order plane is strictly PAPER."""
    require_paper_execution_mode()
    if os.environ.get("APP_ENV", "").strip().lower() in {"prod", "production"}:
        required_internal_auth_config()
    configure_directive_runtime()

# ── 저장 모드 ──────────────────────────────────────────────────────────────────
# PAPER_DB is an explicit opt-in.  A missing/failed durable dependency never
# falls back to an authoritative in-memory store.
def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() not in {"", "0", "false", "no", "off"}

_paper_db_value = os.environ.get("PAPER_DB")
_paper_db_required = _truthy(_paper_db_value)
_paper_db_error: str | None = None
if _paper_db_required:
    _dsn = (
        _paper_db_value
        if _paper_db_value and "://" in _paper_db_value
        else os.environ.get("PAPER_DATABASE_URL") or os.environ.get("DATABASE_URL")
    )
    if not _dsn or _dsn.strip().lower() in {"1", "true", "yes", "on"}:
        _paper_db_error = "PAPER_DB가 요구됐지만 PAPER_DATABASE_URL/DATABASE_URL이 없습니다"
        _oms = OMS(adapter="paper")
    else:
        try:
            _oms = OMS(
                store=PostgresOrderStore.connect(_dsn, adapter="paper"),
                adapter="paper",
            )
        except Exception as exc:
            _paper_db_error = f"PAPER_DB 저장소를 초기화할 수 없습니다: {exc}"
            _oms = OMS(adapter="paper")
else:
    _oms = OMS(store=OrderStore(), adapter="paper")

_paper_db_durable = _paper_db_required and _paper_db_error is None
_broker = PaperBroker()
# OrderIntent 원본(frozen 계약)은 durable store가 재구축하며, offline mode에서는
# OMS의 저장소가 함께 보존한다.
_intents: dict[UUID, OrderIntent] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── 에러 봉투 ─────────────────────────────────────────────────────────────────
# 스펙 1.4. error_code 는 호출자가 분기할 수 있는 안정된 값이고, message 는 사람용이다.


def _envelope(code: str, message: str, **extra) -> dict:
    return {"error_code": code, "message": message, **extra}


def _legacy_offline_fixture_enabled() -> bool:
    return (
        os.environ.get("APP_ENV", "").strip().lower() in {"local", "test"}
        and os.environ.get("TRADING_LEGACY_OFFLINE_MODE", "").strip().lower() == "fixture"
    )


def _require_paper_store() -> None:
    if _paper_db_error is not None:
        raise HTTPException(
            status_code=503,
            detail=_envelope(
                "TRADING_PAPER_DB_UNAVAILABLE",
                _paper_db_error,
                action="HOLD",
            ),
        )
    if not _paper_db_durable and not _legacy_offline_fixture_enabled():
        raise HTTPException(
            status_code=503,
            detail=_envelope(
                "TRADING_DURABLE_STORE_REQUIRED",
                "legacy Trading mutations require the durable PAPER database; "
                "only an explicit local/test fixture may use memory",
                action="HOLD",
            ),
        )


def _require_mutation_identity(
    authorization: str | None,
    policy: MutationAuthPolicy,
) -> InternalServiceIdentity:
    try:
        return authenticate_internal_service(authorization, policy=policy)
    except InternalServiceAuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=_envelope(exc.code, str(exc), action="HOLD"),
        ) from exc


def _require_intent_writer(
    authorization: str | None = Header(default=None),
) -> InternalServiceIdentity:
    return _require_mutation_identity(authorization, INTENT_WRITER_POLICY)


def _require_risk_decision_writer(
    authorization: str | None = Header(default=None),
) -> InternalServiceIdentity:
    return _require_mutation_identity(authorization, RISK_DECISION_POLICY)


def _require_order_submitter(
    authorization: str | None = Header(default=None),
) -> InternalServiceIdentity:
    return _require_mutation_identity(authorization, ORDER_SUBMIT_POLICY)


def _require_broker_event_writer(
    authorization: str | None = Header(default=None),
) -> InternalServiceIdentity:
    return _require_mutation_identity(authorization, BROKER_EVENT_POLICY)


def _require_order_canceller(
    authorization: str | None = Header(default=None),
) -> InternalServiceIdentity:
    return _require_mutation_identity(authorization, ORDER_CANCEL_POLICY)


def _require_case_paper_executor(
    authorization: str | None = Header(default=None),
) -> InternalServiceIdentity:
    return _require_mutation_identity(authorization, CASE_PAPER_ORDER_POLICY)


def _bind_declared_actor(identity: InternalServiceIdentity, declared_actor: str) -> None:
    if declared_actor.strip() != identity.subject:
        raise HTTPException(
            status_code=403,
            detail=_envelope(
                "TRADING_INTERNAL_AUTH_SUBJECT_MISMATCH",
                "authenticated service subject must match the declared actor",
                action="HOLD",
            ),
        )


@app.exception_handler(OrderStorePersistenceError)
def _on_persistence_error(request, exc: OrderStorePersistenceError):
    return JSONResponse(
        status_code=503,
        content=_envelope(
            "TRADING_PAPER_DB_UNAVAILABLE",
            str(exc),
            action="HOLD",
        ),
    )


@app.exception_handler(DirectiveServiceError)
def _on_directive_error(request, exc: DirectiveServiceError):
    body = _envelope(exc.code, str(exc))
    if exc.directive_id is not None:
        body["directive_id"] = str(exc.directive_id)
    return JSONResponse(status_code=exc.status_code, content=body)
@app.exception_handler(OMSError)
def _on_oms_error(request, exc: OMSError):
    """OMS 불변식 위반. 400이지 500이 아니다 - 호출자가 고칠 수 있는 요청이다."""
    return JSONResponse(status_code=400,
                        content=_envelope("TRADING_OMS_REJECTED", str(exc)))


@app.exception_handler(StarletteHTTPException)
def _on_http_error(request, exc: StarletteHTTPException):
    """에러 봉투를 한 모양으로 만든다.

    FastAPI 기본 HTTPException 은 본문을 `detail` 아래에 넣는데, 위 예외 핸들러들은
    최상위에 넣는다. 그대로 두면 호출자가 error_code 를 두 군데서 찾아야 한다 -
    "봉투가 하나"라는 스펙 1.4 가 코드에서는 안 지켜진 상태가 된다.
    """
    body = exc.detail
    if isinstance(body, dict) and "error_code" in body:
        return JSONResponse(status_code=exc.status_code, content=body)
    return JSONResponse(status_code=exc.status_code,
                        content=_envelope("TRADING_HTTP_ERROR", str(body)))


@app.exception_handler(RequestValidationError)
def _on_validation_error(request, exc: RequestValidationError):
    return JSONResponse(status_code=422,
                        content=_envelope("TRADING_INVALID_REQUEST", "요청 본문이 계약과 다릅니다",
                                          detail=jsonable_encoder(exc.errors())))


def _intent_record(order_intent_id: UUID) -> OrderIntentRecord:
    _require_paper_store()
    rec = _oms.store.get_intent(order_intent_id)
    if rec is None:
        raise HTTPException(404, _envelope("TRADING_INTENT_NOT_FOUND",
                                           f"그런 order_intent_id 가 없습니다: {order_intent_id}"))
    return rec


def _intent_body(order_intent_id: UUID) -> OrderIntent:
    """Reconstruct the immutable intent from durable evidence after restart."""
    _require_paper_store()
    if _paper_db_durable:
        intent = _oms.store.reconstruct_intent(order_intent_id)
        if intent is None:
            raise HTTPException(
                409,
                _envelope(
                    "TRADING_INTENT_RECONCILIATION_REQUIRED",
                    "durable Intent 원본을 재구축할 수 없습니다",
                    action="HOLD",
                ),
            )
        return intent
    intent = _intents.get(order_intent_id)
    if intent is None:
        intent = _oms.store.reconstruct_intent(order_intent_id)
    if intent is None:
        raise HTTPException(409, _envelope(
            "TRADING_INTENT_BODY_LOST",
            "이 프로세스에 Intent 원본이 없습니다(재시작됨). order-intents 로 다시 등록하세요",
        ))
    return intent


def _order(order_id: UUID) -> BrokerOrder:
    _require_paper_store()
    order = _oms.store.get_order(order_id)
    if order is None:
        raise HTTPException(404, _envelope("TRADING_ORDER_NOT_FOUND",
                                           f"그런 order_id 가 없습니다: {order_id}"))
    return order


# ── 응답 모델 ─────────────────────────────────────────────────────────────────
# 도메인 객체를 그대로 직렬화하지 않는다. 내부 필드명이 바뀌어도 API 계약은 유지한다.


def _intent_view(rec: OrderIntentRecord) -> dict:
    return {
        "order_intent_id": str(rec.order_intent_id),
        "fund_id": str(rec.fund_id),
        "idempotency_key": rec.idempotency_key,
        "intent_status": rec.state.value,
        "requested_quantity": str(rec.requested_quantity),
        "valid_until": rec.valid_until.isoformat(),
        "risk_decision_id": str(rec.risk_decision_id) if rec.risk_decision_id else None,
        "risk_approved_quantity": str(rec.risk_approved_qty) if rec.risk_approved_qty is not None else None,
        "risk_expires_at": rec.risk_expires_at.isoformat() if rec.risk_expires_at else None,
        "version": rec.version,
        "authoritative": _paper_db_durable,
        "source_of_record": (
            "execution.order_intents (Supabase)"
            if _paper_db_durable else "execution.order_intents (프로세스 메모리)"
        ),
    }


def _order_view(order: BrokerOrder) -> dict:
    return {
        "order_id": str(order.order_id),
        "order_intent_id": str(order.order_intent_id),
        "client_order_id": order.client_order_id,
        "broker_order_id": order.broker_order_id,
        "broker_adapter": order.broker_adapter,
        "state": order.state.value,
        "requested_quantity": str(order.requested_quantity),
        "filled_quantity": str(order.filled_quantity),
        "leaves_quantity": str(order.leaves_quantity),
        "average_fill_price": str(order.average_fill_price) if order.average_fill_price is not None else None,
        "is_terminal": order.is_terminal,
        "version": order.version,
        "authoritative": _paper_db_durable,
        "source_of_record": (
            "execution.orders (Supabase)"
            if _paper_db_durable else "execution.orders (프로세스 메모리)"
        ),
    }


# ── 요청 모델 ─────────────────────────────────────────────────────────────────


class SnapshotIn(BaseModel):
    market_snapshot_id: str
    as_of: datetime
    bid: Decimal
    ask: Decimal


class OrderIntentIn(BaseModel):
    """Agent/전략이 제안한 주문 후보. 이것 자체로는 아무것도 체결되지 않는다."""

    trade_case_id: UUID
    fund_id: UUID
    book_id: UUID
    strategy_id: UUID
    instrument_id: UUID
    side: Side
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    valid_until: datetime
    snapshot: SnapshotIn
    idempotency_key: str = Field(min_length=8, max_length=128)
    created_by: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    # F31 접수 게이트. 선언이 없으면 EQUITY 로 본다 - 파생은 Capability Profile 이
    # 있어야 통과한다(oms._check_capability).
    asset_class: str = "EQUITY"

    def to_contract(self) -> OrderIntent:
        return OrderIntent(
            trade_case_id=self.trade_case_id,
            fund_id=self.fund_id,
            book_id=self.book_id,
            strategy_id=self.strategy_id,
            instrument_id=self.instrument_id,
            side=self.side,
            order_type=self.order_type,
            quantity=self.quantity,
            limit_price=self.limit_price,
            time_in_force=self.time_in_force,
            valid_until=self.valid_until,
            snapshot=MarketSnapshot(**self.snapshot.model_dump()),
            idempotency_key=self.idempotency_key,
            created_by=self.created_by,
            trace_id=self.trace_id,
        )


class RiskDecisionIn(BaseModel):
    """리스크본부가 돌려준 판정. **우리가 만드는 값이 아니다.**"""

    risk_decision_id: UUID
    order_intent_id: UUID
    verdict: str
    approved_quantity: Decimal | None = None
    max_price: Decimal | None = None
    expires_at: datetime
    reason: str = ""
    decided_by: str = Field(min_length=1)


class CancelIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class BrokerEventIn(BaseModel):
    """브로커가 보낸 사실. 우리가 지어낸 값을 넣는 경로가 아니다."""

    event_type: str = Field(pattern="^(ack|fill|reject|cancel|expire)$")
    broker_event_id: str = Field(min_length=1)
    event_time: datetime
    payload: dict = Field(default_factory=dict)
    # UNKNOWN 탈출은 Reconciliation 확정 결과로만 한다(팀 가이드 4.3).
    reconciled: bool = False


class QuoteIn(BaseModel):
    """Paper 체결에 쓸 호가. **잔량(size)이 필수다.**

    체결 수량이 `잔량 x 참여율`로 정해지므로(paper_broker.try_fill) 잔량 없이
    가격만 주면 체결 모델이 성립하지 않는다. 넷 중 하나라도 빠지면 400 이지,
    "잔량을 무한대로 가정"하지 않는다 - 그게 실제보다 낙관적인 체결을 만든다.
    """

    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal


class BrokerOrderIn(BaseModel):
    """Broker Order 생성. 이 경로는 체결하지 않으므로 호가를 받지 않는다."""

    order_intent_id: UUID


class PaperOrderIn(BrokerOrderIn):
    """Case 에서 Paper 주문을 만든다. Risk 판정이 이미 있어야 한다.

    `quote` 를 주면 체결까지 시도하고, 없으면 ack 까지만 하고 미체결로 남긴다.
    시세는 리서치본부 market-api 소관이라 여기서 조회하지 않는다 - 호출자가 준
    값으로만 체결한다(CLAUDE.local.md "만들지 않는 것": 별도 Collector 금지).
    """

    quote: QuoteIn | None = None


# ── 부서 단독 자원: /trading/v1/... ───────────────────────────────────────────


@app.post(f"/trading/{API_VERSION}/order-intents", status_code=201)
def create_order_intent(
    body: OrderIntentIn,
    identity: InternalServiceIdentity = Depends(_require_intent_writer),
) -> dict:
    """OrderIntent 를 DRAFT 로 등록한다. Agent 가 만들 수 있는 유일한 산출물이다.

    같은 idempotency_key 로 다시 부르면 기존 기록을 그대로 돌려준다(불변식 2) -
    네트워크 재시도로 주문이 두 배 나가지 않는다. 그래서 201 이어도 새로 만들어진
    것이 아닐 수 있고, 그 구분은 응답의 intent_status 와 version 으로 한다.
    """
    _require_paper_store()
    _bind_declared_actor(identity, body.created_by)
    if body.asset_class != "EQUITY":
        raise HTTPException(
            409,
            _envelope(
                "TRADING_PAPER_EQUITY_ONLY",
                "PAPER 단일 Leg 경로는 EQUITY만 허용합니다",
                action="HOLD",
            ),
        )
    try:
        intent = body.to_contract()
    except ValidationError as e:
        raise HTTPException(422, _envelope("TRADING_INVALID_INTENT",
                                           "OrderIntent 계약 위반", detail=jsonable_encoder(e.errors())))
    rec = _oms.register_intent(intent, asset_class=body.asset_class)
    _intents[rec.order_intent_id] = intent
    return _intent_view(rec)


@app.get(f"/trading/{API_VERSION}/order-intents/{{order_intent_id}}")
def get_order_intent(order_intent_id: UUID) -> dict:
    return _intent_view(_intent_record(order_intent_id))


@app.post(f"/trading/{API_VERSION}/order-intents/{{order_intent_id}}/risk-review")
def request_risk_review(
    order_intent_id: UUID,
    _identity: InternalServiceIdentity = Depends(_require_intent_writer),
) -> dict:
    """RISK_PENDING 으로 넘긴다. 심사 요청일 뿐 승인이 아니다."""
    return _intent_view(_oms.request_risk_review(_intent_record(order_intent_id)))


@app.post(f"/trading/{API_VERSION}/order-intents/{{order_intent_id}}/risk-decision")
def apply_risk_decision(
    order_intent_id: UUID,
    body: RiskDecisionIn,
    identity: InternalServiceIdentity = Depends(_require_risk_decision_writer),
) -> dict:
    """리스크본부 판정을 반영한다.

    **판정 권한은 리스크본부에 있다.** 여기서 verdict 를 계산하지 않고, 받은 값을
    RiskDecision 계약으로 검증한 뒤 기록만 한다(reject 인데 승인수량이 있으면 계약
    자체가 거부한다). approve/resize 면 OMS 가 READY_TO_SUBMIT 까지 진행시킨다.
    """
    _bind_declared_actor(identity, body.decided_by)
    if body.order_intent_id != order_intent_id:
        raise HTTPException(400, _envelope("TRADING_INTENT_MISMATCH",
                                           "경로와 본문의 order_intent_id 가 다릅니다"))
    try:
        decision = RiskDecision(**body.model_dump())
    except ValidationError as e:
        raise HTTPException(422, _envelope("TRADING_INVALID_RISK_DECISION",
                                           "RiskDecision 계약 위반", detail=jsonable_encoder(e.errors())))
    return _intent_view(_oms.apply_risk_decision(_intent_record(order_intent_id), decision))


@app.post(f"/trading/{API_VERSION}/orders", status_code=201)
def create_broker_order(
    body: BrokerOrderIn,
    _identity: InternalServiceIdentity = Depends(_require_order_submitter),
) -> dict:
    """READY_TO_SUBMIT Intent 로 Broker Order 를 만든다. 아직 전송 전(CREATED)이다.

    두 상태 머신의 유일한 접점이자 Risk Gate 다. 상태 전이가 아니라 새 객체 생성인
    것이 v1.2 의 핵심이다 - Intent 와 Broker Order 는 서로 전이하지 않는다.
    """
    return _order_view(_oms.create_broker_order(_intent_record(body.order_intent_id),
                                                _intent_body(body.order_intent_id)))


@app.get(f"/trading/{API_VERSION}/orders/{{order_id}}")
def get_order(order_id: UUID) -> dict:
    return _order_view(_order(order_id))


@app.post(f"/trading/{API_VERSION}/orders/{{order_id}}/submit")
def submit_order(
    order_id: UUID,
    _identity: InternalServiceIdentity = Depends(_require_order_submitter),
) -> dict:
    """브로커 전송. Risk Gate 의 마지막 관문이다.

    생성 때 통과했다고 전송 때도 통과라고 가정하지 않는다 - 판정 만료·수량 초과·
    Intent 만료를 여기서 다시 본다(oms.submit).
    """
    order = _order(order_id)
    rec = _intent_record(order.order_intent_id)
    return _order_view(_oms.submit(order, rec))


@app.post(f"/trading/{API_VERSION}/orders/{{order_id}}/cancel")
def request_cancel(
    order_id: UUID,
    body: CancelIn,
    _identity: InternalServiceIdentity = Depends(_require_order_canceller),
) -> dict:
    """취소 요청. **아직 취소된 것이 아니다** - CANCEL_PENDING 이다.

    브로커가 cancel 이벤트를 돌려주기 전까지 CANCELLED 로 쓰지 않는다. 취소 요청과
    체결이 교차하면 취소 대신 체결이 올 수도 있다.
    """
    return _order_view(_oms.request_cancel(_order(order_id), body.reason))


@app.post(f"/trading/{API_VERSION}/orders/{{order_id}}/broker-events")
def on_broker_event(
    order_id: UUID,
    body: BrokerEventIn,
    _identity: InternalServiceIdentity = Depends(_require_broker_event_writer),
) -> dict:
    """브로커가 보낸 사실을 반영한다. 상태를 바꾸는 유일한 입구다.

    같은 broker_event_id 를 두 번 받으면 두 번째는 무시한다(불변식 4).
    UNKNOWN 상태에서는 reconciled=true 만 받는다 - 상태를 모르는 채로 흘러든
    이벤트로 확정하면 UNKNOWN 을 둔 의미가 없다.
    """
    return _order_view(_oms.on_broker_event(
        _order(order_id), body.event_type, body.broker_event_id,
        body.event_time, body.payload, reconciled=body.reconciled))


@app.post(f"/trading/{API_VERSION}/orders/{{order_id}}/unknown")
def mark_unknown(
    order_id: UUID,
    body: CancelIn,
    _identity: InternalServiceIdentity = Depends(_require_broker_event_writer),
) -> dict:
    """브로커 응답이 없을 때. FILLED 나 CANCELLED 로 추정하지 않는다.

    UNKNOWN 은 종료 상태가 아니며, 이후 같은 Fund 의 신규 주문이 막힌다.
    """
    return _order_view(_oms.mark_unknown(_order(order_id), body.reason))


@app.get(f"/trading/{API_VERSION}/orders/{{order_id}}/events")
def order_events(order_id: UUID) -> dict:
    """Event Store 원문과 재구축 상태. 둘이 다르면 Projection 이 깨진 것이다."""
    order = _order(order_id)
    events = _oms.store.events_for("broker_order", order.order_id)
    return {
        "order_id": str(order.order_id),
        "state": order.state.value,
        "rebuilt_state": _oms.rebuild_state("broker_order", order.order_id),
        "events": [
            {"sequence": e.sequence, "event_type": e.event_type,
             "from_state": e.from_state, "to_state": e.to_state,
             "event_time": e.event_time.isoformat(),
             "received_at": e.received_at.isoformat(),
             "broker_event_id": e.broker_event_id}
            for e in events
        ],
    }


@app.get(f"/trading/{API_VERSION}/market-rules/tick-size")
def get_tick_size(price: Decimal) -> dict:
    """KRX 호가 단위. 브로커가 아니라 거래소 규칙이라 계약 계층에 있다.

    이 값이 틀리면 지정가가 거래소에서 거부된다 - 실거래 전 최신 규정 대조 필요.
    """
    return {"price": str(price), "tick_size": str(tick_size(price)), "lot_size": str(LOT_SIZE)}


# ── Case 종속 경로 (MINIMUM_SERVICE_UNIT_SPEC 11절이 이미 이름 지음) ──────────


@app.post("/investment-cases/{case_id}/paper-orders", status_code=201)
def create_paper_order(
    case_id: UUID,
    body: PaperOrderIn,
    _identity: InternalServiceIdentity = Depends(_require_case_paper_executor),
) -> dict:
    """Case 에서 Paper 주문을 만들고 전송까지 한다. 체결은 별개다.

    Broker Order 생성 -> submit -> Paper Broker accept(ack) 까지가 한 호출이다.
    체결은 호가가 있어야 하므로 quote_bid/ask 를 준 경우에만 시도하고, 그 결과도
    브로커 이벤트로 들어간다 - 우리가 체결을 지어내는 경로는 없다.

    경로의 case_id 와 Intent 의 trade_case_id 가 같아야 한다. 다르면 그 주문은 남의
    Case 승인 위에 올라타고, 접수한 Case 의 `/cancel` 은 자기가 낸 주문을 못 본다.
    """
    rec = _intent_record(body.order_intent_id)
    intent = _intent_body(body.order_intent_id)
    if intent.trade_case_id != case_id:
        raise HTTPException(400, _envelope(
            "TRADING_CASE_MISMATCH",
            f"이 Intent 는 다른 Case 의 것입니다: {intent.trade_case_id}"))

    order = _oms.create_broker_order(rec, intent)
    _oms.submit(order, rec)

    ack = _broker.accept(order)
    _oms.on_broker_event(order, "ack", ack.broker_event_id, ack.event_time, ack.payload)

    filled = None
    if body.quote is not None:
        ev = _broker.try_fill(order, Quote(**body.quote.model_dump()))
        # None 이면 가격 미도달이다. 미체결로 남긴다 - 추정 체결을 만들지 않는다.
        if ev is not None:
            _oms.on_broker_event(order, "fill", ev.broker_event_id, ev.event_time, ev.payload)
            filled = ev.payload

    return {"case_id": str(case_id), "order": _order_view(order), "fill": filled}


@app.post("/investment-cases/{case_id}/cancel")
def cancel_case_orders(
    case_id: UUID,
    body: CancelIn,
    _identity: InternalServiceIdentity = Depends(_require_order_canceller),
) -> dict:
    """Case 의 미종료 주문에 취소를 요청한다. 요청이지 취소 완료가 아니다.

    ponytail: Case -> 주문 역인덱스가 없어 전체 주문을 훑는다. 주문이 수천 건이
    되면 store 에 case_id 인덱스를 추가한다(DB store 로 가면 자연히 해결된다).
    """
    _require_paper_store()
    targets: list[BrokerOrder] = []
    unresolved: list[str] = []
    already: list[str] = []
    for order in _oms.store.list_orders():
        if order.is_terminal:
            continue
        if order.state is BrokerOrderState.CANCEL_PENDING:
            # 이미 취소 요청이 나가 있다. 다시 요청하면 CANCEL_PENDING -> CANCEL_PENDING
            # 전이 위반이라 **sweep 전체가 400** 이 된다 - 원하는 상태에 이미 도달한
            # 주문 하나가 나머지 주문의 취소를 막는 것은 방향이 반대다.
            already.append(str(order.order_id))
            continue
        try:
            intent = _intent_body(order.order_intent_id)
        except HTTPException:
            # **Intent 원본 하나를 못 읽었다고 Case 전체 취소를 막지 않는다.**
            # 취소는 안전한 방향이다(개발 원칙 9) - 여기서 409를 올리면 이 Case 의
            # 멀쩡한 미체결 주문까지 끊지 못한 채 남는다.
            #
            # 대신 **조용히 건너뛰지 않는다.** 판정하지 못한 주문을 응답에 실어
            # 호출자가 "전부 취소됐다"고 오해하지 못하게 한다 - 취소되지 않은 주문이
            # 있다는 사실 자체가 Reconciliation 대상이다.
            unresolved.append(str(order.order_id))
            continue
        if intent.trade_case_id == case_id:
            targets.append(order)
    return {
        "case_id": str(case_id),
        "requested": [_order_view(_oms.request_cancel(o, body.reason)) for o in targets],
        # 이미 취소 요청이 나가 있어 이번 호출이 건드리지 않은 주문. 원하는 방향에
        # 이미 있으므로 실패가 아니다.
        "already_cancelling": already,
        # Case 소속 여부를 판정하지 못한 미종료 주문. 비어 있지 않으면 이 응답은
        # "Case 의 전부"가 아니다.
        "unresolved_orders": unresolved,
    }


@app.get("/health")
def health() -> dict:
    """Liveness. **저장소가 죽어도 200이다.**

    여기서 503을 내면 오케스트레이터(EB/ECS 헬스체크)가 DB 순단마다 멀쩡한 인스턴스를
    교체한다 - 프로세스는 살아 있고 주문을 올바르게 거절하고 있는데 죽었다고 판정하는
    것이다. 거절은 도메인 엔드포인트와 `/health/ready` 가 한다.

    **저장소를 건드리지 않는다.** 여기서 store 를 조회하면 DB 가 죽었을 때 이 경로도
    같이 503이 되어 분리한 의미가 없어진다. 수치는 `/health/ready` 에 있다.
    """
    return {
        "status": "degraded" if _paper_db_error else "ok",
        "api_version": API_VERSION,
        # Strategy OMS and authenticated direct-user directives intentionally
        # have separate adapter selections.  Expose both so a healthy local
        # strategy simulator cannot hide a miswired LS PAPER direct lane.
        "adapter": _oms.adapter,
        "user_directive_adapter": os.environ.get(
            "TRADING_BROKER_ADAPTER", "paper"
        ).strip().lower(),
        "store": "supabase execution.*" if _paper_db_durable else "in-memory (offline/test)",
        "store_available": _paper_db_error is None,
        "store_error": _paper_db_error,
        "authoritative": _paper_db_durable,
    }


@app.get("/health/ready")
def health_ready() -> dict:
    """Readiness. 저장소에 실제로 닿아 보고, 못 닿으면 503이다.

    Load Balancer 가 트래픽을 끊을 판단은 이쪽을 본다(`apps/api/main.py` 와 같은 규약).
    """
    _require_paper_store()
    return {
        "status": "ready",
        "api_version": API_VERSION,
        "user_directive_adapter": os.environ.get(
            "TRADING_BROKER_ADAPTER", "paper"
        ).strip().lower(),
        "store": "supabase execution.*" if _paper_db_durable else "in-memory (offline/test)",
        "intents": len(_oms.store.list_intents()),
        "orders": len(_oms.store.list_orders()),
        "authoritative": _paper_db_durable,
    }


# ── 자체 점검 ─────────────────────────────────────────────────────────────────
# TestClient 로 실제 요청을 태운다. 네트워크·DB 없음.

if __name__ == "__main__":
    import base64
    import hashlib
    import hmac
    import json
    from datetime import timedelta

    from fastapi.testclient import TestClient

    os.environ.setdefault("TRADING_EXECUTION_MODE", "PAPER")
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("TRADING_LEGACY_OFFLINE_MODE", "fixture")
    os.environ.setdefault("TRADING_AUTH_MODE", "fixture")
    os.environ.setdefault("TRADING_DIRECTIVE_REPOSITORY", "memory")
    os.environ.setdefault(
        "TRADING_INTERNAL_SERVICE_AUTH_SECRET",
        "self-check-internal-trading-auth-secret-32-bytes",
    )
    os.environ.setdefault("TRADING_INTERNAL_SERVICE_AUTH_ISSUER", "self-check-service-issuer")
    os.environ.setdefault("TRADING_INTERNAL_SERVICE_AUTH_AUDIENCE", "trading-api")
    now = _now()

    def _self_check_token(
        *, subject: str, department: str, service: str, scopes: list[str]
    ) -> str:
        def encode(value: dict) -> str:
            raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        issued_at = now.timestamp()
        header = encode({"alg": "HS256", "typ": "JWT"})
        payload = encode(
            {
                "iss": os.environ["TRADING_INTERNAL_SERVICE_AUTH_ISSUER"],
                "aud": os.environ["TRADING_INTERNAL_SERVICE_AUTH_AUDIENCE"],
                "sub": subject,
                "department": department,
                "service": service,
                "scopes": scopes,
                "jti": f"self-check-{uuid4()}",
                "iat": issued_at,
                "nbf": issued_at,
                "exp": issued_at + 60,
            }
        )
        signature = hmac.new(
            os.environ["TRADING_INTERNAL_SERVICE_AUTH_SECRET"].encode(),
            f"{header}.{payload}".encode(),
            hashlib.sha256,
        ).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        return f"Bearer {header}.{payload}.{encoded_signature}"

    _self_check_headers = {
        "intent": {
            "Authorization": _self_check_token(
                subject="svc_selfcheck",
                department="trading-department",
                service="trading-hermes",
                scopes=["trading.intent.write"],
            )
        },
        "risk": {
            "Authorization": _self_check_token(
                subject="svc_risk_engine",
                department="risk-management",
                service="risk-api",
                scopes=["trading.risk_decision.write"],
            )
        },
        "submit": {
            "Authorization": _self_check_token(
                subject="svc_trading_oms",
                department="trading-department",
                service="trading-oms",
                scopes=["trading.order.submit", "trading.broker_event.write"],
            )
        },
        "broker": {
            "Authorization": _self_check_token(
                subject="svc_paper_broker",
                department="trading-department",
                service="paper-broker-adapter",
                scopes=["trading.broker_event.write"],
            )
        },
        "cancel": {
            "Authorization": _self_check_token(
                subject="svc_trading_oms",
                department="trading-department",
                service="trading-oms",
                scopes=["trading.order.cancel"],
            )
        },
    }

    class _AuthenticatedSelfCheckClient:
        def __init__(self) -> None:
            self._client = TestClient(app)

        def get(self, path: str, **kwargs):
            return self._client.get(path, **kwargs)

        def post(self, path: str, **kwargs):
            if path.endswith("/risk-decision"):
                role = "risk"
            elif path.endswith("/broker-events") or path.endswith("/unknown"):
                role = "broker"
            elif path.endswith("/cancel"):
                role = "cancel"
            elif "/orders" in path or path.endswith("/paper-orders"):
                role = "submit"
            else:
                role = "intent"
            headers = dict(_self_check_headers[role])
            headers.update(kwargs.pop("headers", {}))
            response = self._client.post(path, headers=headers, **kwargs)
            # The legacy self-check models an already promoted automated
            # strategy. Production callers must persist a real promotion;
            # this explicit local/test fixture merely keeps the historical
            # API state-machine check executable after the authority gate.
            if (
                role == "risk"
                and response.status_code == 200
                and response.json().get("intent_status") == "READY_TO_SUBMIT"
            ):
                intent_id = UUID(path.split("/")[-2])
                record = _intent_record(intent_id)
                record.authority = ExecutionAuthority.STRATEGY
                record.authority_ref = "SELF-CHECK-STRATEGY"
                _oms.strategy_switchboard.enable(
                    record.authority_ref,
                    actor="user:self-check",
                    reason="explicit local/test fixture",
                )
            return response

    c = _AuthenticatedSelfCheckClient()
    ids = {k: str(uuid4()) for k in ("case", "fund", "book", "strategy", "instrument")}

    def intent_body(key: str, qty: str = "100") -> dict:
        return {
            "trade_case_id": ids["case"], "fund_id": ids["fund"], "book_id": ids["book"],
            "strategy_id": ids["strategy"], "instrument_id": ids["instrument"],
            "side": "BUY", "order_type": "LIMIT", "quantity": qty, "limit_price": "70000",
            "valid_until": (now + timedelta(hours=6)).isoformat(),
            "snapshot": {"market_snapshot_id": "snap_01", "as_of": now.isoformat(),
                         "bid": "70000", "ask": "70100"},
            "idempotency_key": key, "created_by": "svc_selfcheck", "trace_id": "trace_selfcheck",
        }

    def risk_body(intent_id: str, verdict: str = "approve", qty: str = "100") -> dict:
        return {
            "risk_decision_id": str(uuid4()), "order_intent_id": intent_id,
            "verdict": verdict, "approved_quantity": qty,
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "reason": "self check", "decided_by": "svc_risk_engine",
        }

    # 1. Intent 등록과 멱등
    r = c.post("/trading/v1/order-intents", json=intent_body("idem_api_0001"))
    assert r.status_code == 201, r.text
    oid = r.json()["order_intent_id"]
    assert r.json()["intent_status"] == "DRAFT"
    again = c.post("/trading/v1/order-intents", json=intent_body("idem_api_0001"))
    assert again.json()["order_intent_id"] == oid, "같은 idempotency_key 가 두 건이 됐다"

    # 2. Risk 승인 없이는 Broker Order 가 안 생긴다
    blocked = c.post("/trading/v1/orders", json={"order_intent_id": oid})
    assert blocked.status_code == 400, blocked.text
    assert blocked.json()["error_code"] == "TRADING_OMS_REJECTED"

    # 3. 심사 요청 -> 판정 반영 -> READY_TO_SUBMIT
    assert c.post(f"/trading/v1/order-intents/{oid}/risk-review").json()["intent_status"] == "RISK_PENDING"
    approved = c.post(f"/trading/v1/order-intents/{oid}/risk-decision", json=risk_body(oid))
    assert approved.json()["intent_status"] == "READY_TO_SUBMIT", approved.text
    assert approved.json()["risk_decision_id"] is not None

    # 4. 경로/본문 order_intent_id 불일치는 거부
    other = c.post("/trading/v1/order-intents", json=intent_body("idem_api_0002"))
    mismatch = c.post(f"/trading/v1/order-intents/{oid}/risk-decision",
                      json=risk_body(other.json()["order_intent_id"]))
    assert mismatch.status_code == 400 and \
        mismatch.json()["error_code"] == "TRADING_INTENT_MISMATCH", mismatch.text

    # 5. reject 인데 승인 수량이 있으면 계약이 거부한다(우리가 판정을 만들지 않는 증거)
    o3 = c.post("/trading/v1/order-intents", json=intent_body("idem_api_0003")).json()["order_intent_id"]
    c.post(f"/trading/v1/order-intents/{o3}/risk-review")
    bad = c.post(f"/trading/v1/order-intents/{o3}/risk-decision",
                 json=risk_body(o3, verdict="reject", qty="50"))
    assert bad.status_code == 422 and bad.json()["error_code"] == "TRADING_INVALID_RISK_DECISION", bad.text

    # 6. Broker Order 생성 -> submit -> ack. Intent 하나에 주문 하나(불변식 3)
    created = c.post("/trading/v1/orders", json={"order_intent_id": oid})
    assert created.status_code == 201 and created.json()["state"] == "CREATED", created.text
    order_id = created.json()["order_id"]
    assert c.post("/trading/v1/orders", json={"order_intent_id": oid}).json()["order_id"] == order_id
    submitted = c.post(f"/trading/v1/orders/{order_id}/submit")
    assert submitted.status_code == 200 and submitted.json()["state"] == "SUBMITTED", submitted.text
    ack = c.post(f"/trading/v1/orders/{order_id}/broker-events",
                 json={"event_type": "ack", "broker_event_id": "bev_ack_1",
                       "event_time": now.isoformat(), "payload": {"broker_order_id": "B-1"}})
    assert ack.json()["state"] == "ACKNOWLEDGED", ack.text

    # 7. 같은 broker_event_id 재수신은 무시(불변식 4), 부분체결 수량은 누적
    fill = {"event_type": "fill", "broker_event_id": "bev_fill_1", "event_time": now.isoformat(),
            "payload": {"quantity": "40", "price": "70000", "fee": "0", "tax": "0"}}
    assert c.post(f"/trading/v1/orders/{order_id}/broker-events", json=fill).json()["state"] == "PARTIALLY_FILLED"
    dup = c.post(f"/trading/v1/orders/{order_id}/broker-events", json=fill)
    assert dup.json()["filled_quantity"] == "40", f"중복 이벤트가 두 번 잡혔다: {dup.text}"

    # 8. Event Store 재구축 상태가 Projection 과 같다(불변식 6)
    ev = c.get(f"/trading/v1/orders/{order_id}/events").json()
    assert ev["state"] == ev["rebuilt_state"], ev
    assert [e["event_type"] for e in ev["events"]][:2] == ["order_created", "submitted"], ev

    # 9. 취소는 CANCEL_PENDING 이지 CANCELLED 가 아니다
    cancelled = c.post(f"/trading/v1/orders/{order_id}/cancel", json={"reason": "self check"})
    assert cancelled.json()["state"] == "CANCEL_PENDING" and not cancelled.json()["is_terminal"]

    # 10. Case 경로 - Paper 주문과 체결. 호가를 안 주면 체결도 없다
    o5 = c.post("/trading/v1/order-intents", json=intent_body("idem_api_0005")).json()["order_intent_id"]
    c.post(f"/trading/v1/order-intents/{o5}/risk-review")
    c.post(f"/trading/v1/order-intents/{o5}/risk-decision", json=risk_body(o5))
    paper = c.post(f"/investment-cases/{ids['case']}/paper-orders",
                   json={"order_intent_id": o5,
                         "quote": {"bid": "69900", "ask": "70000",
                                   "bid_size": "1000", "ask_size": "1000"}})
    assert paper.status_code == 201, paper.text
    # 참여율 상한이 걸린다. ask_size 1000 x max_participation 0.05 = 50 이라
    # 100주 주문이 한 번에 다 체결되지 않는다 - 호가 잔량을 다 먹는 낙관적
    # 체결을 막는 장치이며, 여기서 FILLED 가 나오면 그 상한이 죽은 것이다.
    assert paper.json()["order"]["state"] == "PARTIALLY_FILLED", paper.text
    assert paper.json()["fill"]["quantity"] == "50", paper.text

    # 호가를 안 주면 ack 까지만. 체결을 지어내지 않는다
    o7 = c.post("/trading/v1/order-intents", json=intent_body("idem_api_0007")).json()["order_intent_id"]
    c.post(f"/trading/v1/order-intents/{o7}/risk-review")
    c.post(f"/trading/v1/order-intents/{o7}/risk-decision", json=risk_body(o7))
    noquote = c.post(f"/investment-cases/{ids['case']}/paper-orders", json={"order_intent_id": o7})
    assert noquote.json()["order"]["state"] == "ACKNOWLEDGED", noquote.text
    assert noquote.json()["fill"] is None, "호가 없이 체결이 만들어졌다"

    # 11. 남의 Case 경로로는 접수되지 않는다. 통과시키면 그 주문은 접수한 Case 의
    #     /cancel 에 안 잡히고(취소 불가), 남의 Case 승인 위에 올라탄다
    o8 = c.post("/trading/v1/order-intents", json=intent_body("idem_api_0008")).json()["order_intent_id"]
    c.post(f"/trading/v1/order-intents/{o8}/risk-review")
    c.post(f"/trading/v1/order-intents/{o8}/risk-decision", json=risk_body(o8))
    cross = c.post(f"/investment-cases/{uuid4()}/paper-orders", json={"order_intent_id": o8})
    assert cross.status_code == 400 and cross.json()["error_code"] == "TRADING_CASE_MISMATCH", cross.text

    # 12. 존재하지 않는 자원은 404 봉투
    missing = c.get(f"/trading/v1/orders/{uuid4()}")
    assert missing.status_code == 404 and \
        missing.json()["error_code"] == "TRADING_ORDER_NOT_FOUND", missing.text

    # 13. KRX 호가 단위 - 가격대별로 다르다
    assert c.get("/trading/v1/market-rules/tick-size", params={"price": "1500"}).json()["tick_size"] == "1"
    assert c.get("/trading/v1/market-rules/tick-size", params={"price": "70000"}).json()["tick_size"] != "1"

    # 14. health 가 in-memory 사실을 숨기지 않는다
    assert c.get("/health").json()["store"].startswith("in-memory")

    # 14-1. **Liveness 는 저장소를 건드리지 않는다.** store 를 조회하면 DB 가 죽었을 때
    #       이 경로도 같이 503이 되고, 그러면 EB/ECS 헬스체크가 멀쩡한 인스턴스를
    #       교체한다. 판정은 /health/ready 가 한다.
    live = c.get("/health")
    assert live.status_code == 200 and live.json()["store_available"] is True
    assert "intents" not in live.json(), "liveness 가 저장소를 조회했다"
    ready = c.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["status"] == "ready", ready.text
    assert "intents" in ready.json(), "readiness 가 저장소 수치를 안 냈다"

    # 14-2. 저장소가 죽으면 liveness 는 200 degraded, readiness 만 503이다.
    # `import app` 으로는 안 된다 - 이 파일은 __main__ 으로 돌고 있어서 별개 모듈
    # 객체가 생기고, health() 가 읽는 전역은 그대로 남는다. 여기가 모듈 최상위라
    # 직접 대입이 곧 그 전역이다.
    _saved_error = _paper_db_error
    _paper_db_error = "PAPER_DB 저장소를 초기화할 수 없습니다: 점검용"
    try:
        degraded = c.get("/health")
        assert degraded.status_code == 200, "저장소 장애에 liveness 가 죽었다"
        assert degraded.json()["status"] == "degraded", degraded.text
        assert degraded.json()["store_available"] is False
        assert c.get("/health/ready").status_code == 503, "readiness 가 장애를 숨겼다"
        # 도메인 경로도 같이 닫혀 있어야 한다 - degraded 가 "그래도 주문은 받는다"가 되면 안 된다
        assert c.get(f"/trading/v1/orders/{uuid4()}").status_code == 503
    finally:
        _paper_db_error = _saved_error

    # 14-3. **Intent 원본 하나를 못 읽어도 Case 취소가 멈추지 않는다.**
    #       취소는 안전한 방향이라 여기서 409를 올리면 멀쩡한 미체결 주문까지 남는다.
    #       대신 판정 못 한 주문을 응답에 실어 "전부 취소됐다"는 오해를 막는다.
    lost_order_id = noquote.json()["order"]["order_id"]
    _intents.pop(UUID(o7), None)
    _oms.store._intent_bodies.pop(UUID(o7), None)
    swept = c.post(f"/investment-cases/{ids['case']}/cancel", json={"reason": "점검"})
    assert swept.status_code == 200, swept.text
    assert lost_order_id in swept.json()["unresolved_orders"], \
        "원본을 잃은 주문이 조용히 사라졌다"
    assert lost_order_id not in [o["order_id"] for o in swept.json()["requested"]]

    # 15. UNKNOWN — 맨 뒤에 둔다. 같은 Fund 의 신규 주문을 막으므로(불변식 8)
    #     이걸 앞에 두면 뒤따르는 모든 검사가 이 차단에 걸린다. 실제로 처음
    #     작성했을 때 그렇게 걸렸고, 그게 불변식이 살아있다는 증거였다.
    o4 = c.post("/trading/v1/order-intents", json=intent_body("idem_api_0004")).json()["order_intent_id"]
    c.post(f"/trading/v1/order-intents/{o4}/risk-review")
    c.post(f"/trading/v1/order-intents/{o4}/risk-decision", json=risk_body(o4))
    ord4 = c.post("/trading/v1/orders", json={"order_intent_id": o4}).json()["order_id"]
    c.post(f"/trading/v1/orders/{ord4}/submit")
    unk = c.post(f"/trading/v1/orders/{ord4}/unknown", json={"reason": "브로커 응답 없음"})
    assert unk.json()["state"] == "UNKNOWN" and not unk.json()["is_terminal"]

    # 확정 안 된 이벤트로는 UNKNOWN 을 탈출할 수 없다
    stuck = c.post(f"/trading/v1/orders/{ord4}/broker-events",
                   json={"event_type": "fill", "broker_event_id": "bev_x",
                         "event_time": now.isoformat(),
                         "payload": {"quantity": "10", "price": "70000", "fee": "0", "tax": "0"}})
    assert stuck.status_code == 400, "확정 안 된 이벤트로 UNKNOWN 을 탈출했다"

    # UNKNOWN 이 남아 있는 동안 같은 Fund 의 신규 주문이 막힌다
    o6 = c.post("/trading/v1/order-intents", json=intent_body("idem_api_0006")).json()["order_intent_id"]
    c.post(f"/trading/v1/order-intents/{o6}/risk-review")
    c.post(f"/trading/v1/order-intents/{o6}/risk-decision", json=risk_body(o6))
    blocked_new = c.post("/trading/v1/orders", json={"order_intent_id": o6})
    assert blocked_new.status_code == 400 and \
        blocked_new.json()["error_code"] == "TRADING_OMS_REJECTED", \
        "UNKNOWN 미해소 상태에서 신규 주문이 통과했다"

    print("ok - Trading Domain API 15개 영역 점검 통과 (Risk 우회 차단·Case 교차 차단·멱등·Event Store 포함)")
