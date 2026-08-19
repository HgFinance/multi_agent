#!/usr/bin/env python3
"""Accounting/Portfolio Domain API — 원장·평가·대사·기업행위·일일보고 FastAPI 래퍼.

소유: 도현 (회계·포트폴리오본부)
근거: docs/02-engineering/ACCOUNTING_PORTFOLIO_DOMAIN_API_SPEC.md
      docs/02-engineering/TECH_STACK_DECISIONS.md 7절(Hermes는 Domain 서비스를 API/MCP
      경계로만 부른다 - 같은 프로세스에 직접 import하지 않는다)
      docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md (v1.2) 4.4, 4.5, 8.2

여기엔 새 회계 판정 로직이 없다. 차대 균형·멱등·평가 게이트·Break Severity는 전부
`ledger.py`/`portfolio.py`/`reconciliation.py`/`corporate_actions.py`/`daily_report.py`가
하고, 이 파일은 JSON <-> 도메인 객체 변환과 에러 매핑만 한다.

**권한 경계 — 이 API가 하지 않는 것** (CLAUDE.md, 팀 가이드 4.4/8.2):
  - **Posted Journal을 수정·삭제하지 않는다.** 분개에 PUT/PATCH/DELETE가 없다.
    정정 경로는 `POST .../reverse` 하나뿐이고 원본은 그대로 남는다(불변식 2).
  - **평균원가를 호출자에게서 받지 않는다.** 원장에서 재계산해 쓴다. 받으면 실현손익이
    호출자가 정하는 값이 되고, 그건 회계 수치를 외부 문장에서 확정하는 것과 같다(원칙 5).
  - **평가가격을 우리가 만들지 않는다.** market-api가 준 Mark를 받아 쓸 뿐이며,
    보유 종목 중 하나라도 신선한 Mark가 없으면 NAV 자체를 만들지 않는다.
  - **NAV를 확정하지 않는다.** Daily Report는 항상 `is_official: false`다.
  - **Break를 종결하지 않는다.** 대사는 Break를 만들기만 한다. 종결은 AI QA/감사본부다.
  - 주문을 내지 않는다. OrderIntent·Broker Order는 트레이딩본부 API의 몫이다.

**Agent는 이 API의 직접 호출자가 아니다.** Hermes 페르소나가 부를 수 있는 것은 MCP
도구 면(예정)이고, 거기에는 읽기만 노출한다 - 분개 Posting과 Reversal은 주지 않는다.
이 API는 서비스 호출자(BFF, 부서 워커)용이며, 그래도 안전한 이유는 불변식이 HTTP
계층이 아니라 도메인 모듈에 있기 때문이다 - 불균형 분개는 누가 부르든 LedgerError다.

**저장소 모드:**
  - `ACCOUNTING_MODE=OFFLINE`이면 인메모리 fixture 저장소를 쓴다.
  - `ACCOUNTING_MODE=PAPER_DB`(또는 durable mode)이면 `DATABASE_URL`과 DB 드라이버가
    필수다. 연결 실패는 503으로 fail closed 하며 memory로 후퇴하지 않는다.

응답의 `authoritative`는 두 모드 모두 `false`다. 저장 위치가 바뀌었을 뿐 NAV 확정·
Close 승인 절차는 아직 없기 때문이다 - `source_of_record` 필드가 실제 저장 위치를 말한다.

실행: uvicorn app:app --app-dir departments/05-accounting-portfolio/api
자체 점검: python departments/05-accounting-portfolio/api/app.py
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

_DEPT = Path(__file__).resolve().parent.parent
for _sub in ("ledger", "portfolio", "reconciliation", "corporate_actions", "reporting"):
    sys.path.insert(0, str(_DEPT / _sub))
sys.path.insert(0, str(_DEPT.parent / "02-trading" / "contracts"))

from contracts import Side
from corporate_actions import (
    ActionStatus,
    ActionType,
    CorporateAction,
    CorporateActionError,
    apply_corporate_action,
)
import financial_statements  # noqa: E402
from daily_report import ReportError, build_daily_report
from fill_consumer import project
from ledger import Journal, Ledger, LedgerError, Position, decimal_str
from investor_profile_repository import (
    InvestorProfilePersistenceError,
    InvestorProfileRepository,
)
from portfolio import MarkPrice, PortfolioSnapshot, ValuationError, value_portfolio
from suitability import (
    ExperienceLevel,
    InvestmentMindset,
    InvestorProfile,
    LiquidityNeed,
    effective_risk_band,
)
from recon_repository import ReconRun, open_breaks, save_reconciliation
from repository import (
    LedgerConflictError,
    LedgerPersistenceError,
    LedgerRepository,
    durable_required_from_env,
)
from reconciliation import (
    FillRecord,
    ReconResult,
    reconcile_cash,
    reconcile_fills,
    reconcile_positions,
)

API_VERSION = "v1"

app = FastAPI(title="Accounting/Portfolio Domain API", version=API_VERSION)

# ── 저장소 ────────────────────────────────────────────────────────────────────
# DATABASE_URL이 없는 경우는 명시적 offline 모드일 때만 memory를 사용한다.
# PAPER_DB/durable mode의 연결 실패는 `_store_error`로 남기고 모든 mutation/read를
# 503으로 fail closed 한다.
_store_error: LedgerPersistenceError | None = None
try:
    _repo: LedgerRepository | None = LedgerRepository.from_env(
        required=durable_required_from_env()
    )
except LedgerPersistenceError as exc:
    _repo = None
    _store_error = exc
# 인메모리 모드 전용. key 는 book_id 다(DB 모드의 ledger_id 규약과 같게 맞춘다).
_ledgers: dict[UUID, Ledger] = {}
# 확정된 스냅샷 이력. Daily Report 가 기초·기말 최소 2개를 요구한다.
_snapshots: dict[UUID, list[PortfolioSnapshot]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── 에러 봉투 ─────────────────────────────────────────────────────────────────
# 스펙 1.4. error_code 는 호출자가 분기할 수 있는 안정된 값이고, message 는 사람용이다.


def _envelope(code: str, message: str, **extra) -> dict:
    return {"error_code": code, "message": message, **extra}


def _domain_error(code: str):
    """도메인 예외를 400 봉투로. 500이 아니다 - 호출자가 고칠 수 있는 요청이다."""

    def handler(request, exc):
        return JSONResponse(status_code=400, content=_envelope(code, str(exc)))

    return handler


app.add_exception_handler(LedgerError, _domain_error("ACCOUNTING_LEDGER_REJECTED"))
app.add_exception_handler(ValuationError, _domain_error("ACCOUNTING_VALUATION_REJECTED"))
app.add_exception_handler(CorporateActionError,
                          _domain_error("ACCOUNTING_CORPORATE_ACTION_REJECTED"))
app.add_exception_handler(ReportError, _domain_error("ACCOUNTING_REPORT_REJECTED"))


@app.exception_handler(LedgerPersistenceError)
def _on_store_error(request, exc: LedgerPersistenceError):
    """저장소에 닿지 못했다. **성공으로 응답하지 않는다** - 기록되지 않은 분개를
    기록된 것처럼 돌려주면 그 뒤의 모든 잔고가 틀어진다."""
    return JSONResponse(status_code=503,
                        content=_envelope("ACCOUNTING_STORE_UNAVAILABLE", str(exc)))


@app.exception_handler(InvestorProfilePersistenceError)
def _on_investor_profile_store_error(request, exc: InvestorProfilePersistenceError):
    """InvestorProfile 저장 실패도 503이다 - 500으로 두면 호출자가 재시도해도 되는
    상황인지 알 수 없다. `InvestorProfileConflictError`(version 경합)도 이 핸들러가
    받는다: 하위 클래스이고, 호출자 대응이 같은 '잠시 후 재시도'이기 때문이다."""
    return JSONResponse(status_code=503,
                        content=_envelope("INVESTOR_PROFILE_STORE_UNAVAILABLE", str(exc)))


@app.exception_handler(LedgerConflictError)
def _on_store_conflict(request, exc: LedgerConflictError):
    """같은 원천 이벤트를 다른 장부가 이미 썼다. 재시도해도 같으므로 409다."""
    return JSONResponse(status_code=409,
                        content=_envelope("ACCOUNTING_SOURCE_EVENT_CONFLICT", str(exc)))


@app.exception_handler(StarletteHTTPException)
def _on_http_error(request, exc: StarletteHTTPException):
    """에러 봉투를 한 모양으로 만든다.

    FastAPI 기본 HTTPException 은 본문을 `detail` 아래에 넣는데, 위 도메인 핸들러들은
    최상위에 넣는다. 그대로 두면 호출자가 error_code 를 두 군데서 찾아야 한다.
    """
    body = exc.detail
    if isinstance(body, dict) and "error_code" in body:
        return JSONResponse(status_code=exc.status_code, content=body)
    return JSONResponse(status_code=exc.status_code,
                        content=_envelope("ACCOUNTING_HTTP_ERROR", str(body)))


@app.exception_handler(RequestValidationError)
def _on_validation_error(request, exc: RequestValidationError):
    return JSONResponse(status_code=422,
                        content=_envelope("ACCOUNTING_INVALID_REQUEST",
                                          "요청 본문이 계약과 다릅니다",
                                          detail=jsonable_encoder(exc.errors())))


def _ledger(ledger_id: UUID) -> Ledger:
    """ledger_id 로 원장을 연다. DB 모드에서 ledger_id 는 book_id 다.

    ponytail: DB 모드는 요청마다 그 장부의 분개를 전부 읽어 원장을 복원한다. Paper
              규모에서는 문제가 없고, 느려지면 Position/Cash projection 을 기점으로
              삼는 증분 복원으로 바꾼다(`repository.load()` 주석).
    """
    if _store_error is not None:
        raise _store_error
    if _repo is not None:
        fund_id = _repo.fund_of_book(ledger_id)
        if fund_id is None:
            raise HTTPException(404, _envelope("ACCOUNTING_LEDGER_NOT_FOUND",
                                               f"그런 ledger_id(book_id) 가 없습니다: {ledger_id}"))
        return _repo.load(fund_id, ledger_id)
    led = _ledgers.get(ledger_id)
    if led is None:
        raise HTTPException(404, _envelope("ACCOUNTING_LEDGER_NOT_FOUND",
                                           f"그런 ledger_id 가 없습니다: {ledger_id}"))
    return led


def _snapshot_history(led: Ledger) -> list[PortfolioSnapshot]:
    if _repo is not None:
        return _repo.load_snapshots(led.fund_id, led.book_id)
    return _snapshots.get(led.book_id, [])


def _position(led: Ledger, instrument_id: UUID) -> Position:
    """현재 포지션. **평균원가를 호출자에게서 받지 않는다.**

    받으면 실현손익((체결가 - 평균원가) x 수량)이 호출자가 정하는 값이 된다.
    원장이 소유한 수치이므로 원장에서 재계산한다(불변식 4).
    """
    positions, _ = led.rebuild()
    return positions.get(instrument_id, Position(instrument_id))


# ── 응답 모델 ─────────────────────────────────────────────────────────────────
# 도메인 객체를 그대로 직렬화하지 않는다. 금액은 전부 문자열이다 - JSON number 는
# IEEE754 double 이라 Decimal 이 깨진다(ui_read_model 과 같은 규칙).

def _provenance() -> dict:
    """이 수치가 어디서 왔는지. **화면·에이전트가 공식 값으로 쓰지 못하게 하는 계약이다.**

    `authoritative` 는 저장소가 Supabase 여도 여전히 false 다 - 저장 위치가 아니라
    확정 절차(NAV Close 승인)의 문제이고 그건 아직 없다. 대신 `source_of_record` 가
    실제 저장 위치를 말한다. 둘을 한 필드로 합치면 "DB 에 있으니 공식"이라는
    잘못된 읽기가 나온다.
    """
    return {
        "authoritative": False,
        "source_of_record": ("accounting.journals (Supabase)" if _repo is not None
                             else "accounting.journals (미연결 - 프로세스 메모리)"),
    }


def _d(v: Decimal | None) -> str | None:
    """금액·수량의 유일한 직렬화 경로. 저장소 모드에 따라 문자열이 달라지지 않는다.

    DB 에서 읽은 `20.0000000000` 과 메모리에서 계산한 `20` 이 같은 응답을 내야
    화면·대사·해시가 갈라지지 않는다(`ledger.decimal_str` 주석).
    """
    return None if v is None else decimal_str(v)


def _journal_view(j: Journal) -> dict:
    return {
        "journal_id": str(j.journal_id),
        "event_type": j.event_type,
        "source_event_id": j.source_event_id,
        "effective_at": j.effective_at.isoformat(),
        "accounting_date": j.accounting_date.isoformat(),
        "status": j.status,
        "reversal_of": str(j.reversal_of) if j.reversal_of else None,
        "lines": [
            {"account_code": l.account_code, "debit": _d(l.debit), "credit": _d(l.credit),
             "instrument_id": str(l.instrument_id) if l.instrument_id else None,
             "quantity": _d(l.quantity), "unit_price": _d(l.unit_price)}
            for l in j.lines
        ],
        "metadata": j.metadata,
        **_provenance(),
    }


def _snapshot_view(s: PortfolioSnapshot) -> dict:
    return {
        "fund_id": str(s.fund_id),
        "book_id": str(s.book_id),
        "as_of": s.as_of.isoformat(),
        "cash": _d(s.cash),
        "receivable": _d(s.receivable),
        "payable": _d(s.payable),
        "realized_pnl": _d(s.realized_pnl),
        "unrealized_pnl": _d(s.unrealized_pnl),
        "fees": _d(s.fees),
        "taxes": _d(s.taxes),
        "securities_value": _d(s.securities_value),
        "nav": _d(s.nav),
        "gross_exposure": _d(s.gross_exposure),
        "net_exposure": _d(s.net_exposure),
        "positions": [
            {"instrument_id": str(p.instrument_id), "quantity": _d(p.quantity),
             "average_cost": _d(p.average_cost), "mark_price": _d(p.mark_price),
             "mark_as_of": p.mark_as_of.isoformat(), "mark_is_final": p.mark_is_final,
             "cost_basis": _d(p.cost_basis), "market_value": _d(p.market_value),
             "unrealized_pnl": _d(p.unrealized_pnl)}
            for p in s.positions
        ],
        # WARN 이면 미확정 봉으로 평가된 NAV 다. 숨기지 않고 수치와 함께 낸다.
        "quality_status": s.quality_status,
        # NAV 를 냈다고 확정한 것이 아니다. 공식 확정은 승인 절차다.
        "is_official": False,
        **_provenance(),
    }


def _recon_view(r: ReconResult, run: ReconRun | None = None) -> dict:
    """대사 결과. `run` 이 있으면 canonical 표에 남은 대사다.

    `breaks[].break_id` 는 저장했다면 DB 의 id 다(`recon_repository._relabel`).
    응답의 id 와 DB 의 id 가 갈라지면 화면에서 본 Break 를 찾을 수 없다.
    """
    return {
        "reconciliation_id": str(run.reconciliation_id) if run else None,
        "statement_id": str(run.statement_id) if run else None,
        "persisted": run is not None,
        "recon_type": r.recon_type,
        "rule_version": r.rule_version,
        "as_of": r.as_of.isoformat(),
        "result": r.result,
        "items": [
            {"match_method": str(i.match_method), "internal_ref": i.internal_ref,
             "external_ref": i.external_ref, "internal_value": _d(i.internal_value),
             "external_value": _d(i.external_value), "difference": _d(i.difference),
             "is_confirmed": i.is_confirmed, "detail": i.detail}
            for i in r.items
        ],
        "breaks": [
            {"break_id": str(b.break_id), "kind": b.kind, "severity": str(b.severity),
             "detail": b.detail, "internal_ref": b.internal_ref,
             "external_ref": b.external_ref, "status": b.status, "escalates": b.escalates}
            for b in r.breaks
        ],
        "material_break_count": len(r.material_breaks),
        # Break 종결 권한은 AI QA/감사본부에 있다. 이 API 에 종결 경로가 없다.
        "closable_here": False,
        **_provenance(),
    }


# ── 요청 모델 ─────────────────────────────────────────────────────────────────


class LedgerIn(BaseModel):
    fund_id: UUID
    book_id: UUID


class CapitalIn(BaseModel):
    amount: Decimal = Field(gt=0)
    effective_at: datetime
    source_event_id: str = Field(min_length=1)


class FillIn(BaseModel):
    """트레이딩본부가 준 체결 사실. **주문 의도가 아니라 체결에서만 회계가 움직인다**(원칙 4).

    `broker_fill_id` 가 멱등 키다. 같은 체결이 두 번 와도 분개는 한 번만 생긴다.
    평균원가를 받지 않는 것이 의도다 - `_position()` 주석 참고.
    """

    instrument_id: UUID
    side: Side
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Field(default=Decimal(0), ge=0)
    tax: Decimal = Field(default=Decimal(0), ge=0)
    event_time: datetime
    broker_fill_id: str = Field(min_length=1)
    fill_id: UUID = Field(default_factory=uuid4)


class ReverseIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class MarkIn(BaseModel):
    """market-api 가 준 평가 기준가. **우리가 만들지 않는다.**

    `is_final` 은 market-api 응답의 `is_final`(= `market.market_bars.is_final`)을
    그대로 넘기는 자리다. 안 주면 **미확정으로 본다** - 진행 중인 봉을 종가로 써서
    NAV 가 조용히 틀리는 것을 막는 기본값이다(`portfolio.MarkPrice` 주석).
    NAV 를 막지는 않고 스냅샷의 `quality_status` 를 WARN 으로 만든다.
    """

    instrument_id: UUID
    price: Decimal
    as_of: datetime
    source: str = "market-api"
    is_final: bool = False


class ValuationIn(BaseModel):
    as_of: datetime
    marks: list[MarkIn]
    # 기본 5분. 종가 평가처럼 정당하게 더 긴 창이 필요한 경우가 있어 열어둔다.
    max_staleness_seconds: int | None = Field(default=None, gt=0)


class CorporateActionIn(BaseModel):
    """참조 데이터가 준 기업행위. `action_id` 가 멱등 키다."""

    action_id: str = Field(min_length=1)
    action_type: ActionType
    instrument_id: UUID
    record_date: datetime
    effective_at: datetime
    status: ActionStatus = ActionStatus.ANNOUNCED
    mandatory: bool = True
    approval_id: str | None = None
    amount_per_share: Decimal = Decimal(0)
    withholding_tax: Decimal = Decimal(0)
    ratio: Decimal = Decimal(0)
    new_instrument_id: UUID | None = None
    # 기준일 보유 수량. 배당은 이 수량으로 계산한다 - 현재 수량을 쓰면 배당락 이후
    # 매매가 배당금액에 섞인다. 우리가 과거 수량을 알 수 없어 호출자가 준다.
    record_date_quantity: Decimal | None = None


class FillRecordIn(BaseModel):
    """대사용 체결 한 줄. 내부·외부를 같은 모양으로 놓고 비교한다."""

    instrument_id: UUID
    side: Side
    quantity: Decimal
    price: Decimal
    event_time: datetime
    broker_fill_id: str | None = None
    client_order_id: str | None = None
    fee: Decimal = Decimal(0)
    tax: Decimal = Decimal(0)
    ref: str = ""

    def to_record(self) -> FillRecord:
        return FillRecord(**self.model_dump())


class StatementSourceIn(BaseModel):
    """외부 명세서가 어디서 왔는지. 대사를 canonical 표에 남길 때 필요하다.

    **원문은 받지 않는다.** `object_path` 는 Storage 포인터이며 우리는 해시만 남긴다
    (규약: Event Payload 에 전체 Statement 를 넣지 않는다).
    """

    provider: str = Field(default="paper-broker", min_length=1)
    account_ref: str = Field(default="", max_length=200)
    object_path: str | None = None


class FillReconIn(BaseModel):
    internal: list[FillRecordIn]
    external: list[FillRecordIn]
    as_of: datetime | None = None
    time_window_seconds: int | None = Field(default=None, gt=0)
    # 이 경로만 Ledger 종속이 아니라서(양쪽 체결을 다 받는다) Fund 를 모른다.
    # 주면 canonical 표에 남기고, 안 주면 계산만 해서 돌려준다.
    ledger_id: UUID | None = None
    statement: StatementSourceIn = Field(default_factory=StatementSourceIn)


class PositionReconIn(BaseModel):
    """외부(브로커) 잔고만 받는다. 내부는 원장에서 재계산한다.

    내부 수량까지 호출자가 주면 호출자가 양쪽을 다 정하는 셈이라 대사가 성립하지 않는다.
    """

    external: dict[UUID, Decimal]
    as_of: datetime | None = None
    statement: StatementSourceIn = Field(default_factory=StatementSourceIn)


class CashReconIn(BaseModel):
    external: Decimal
    as_of: datetime | None = None
    tolerance: Decimal | None = Field(default=None, ge=0)
    statement: StatementSourceIn = Field(default_factory=StatementSourceIn)


class DailyReportIn(BaseModel):
    accounting_date: date
    # 원장에 전략 차원이 없어서(분개는 fund/book 까지만 안다) 호출자가 OMS 쪽
    # 연결 정보를 줘야 전략별 분해가 나온다. 안 주면 전부 UNATTRIBUTED 다.
    strategy_of: dict[str, str] | None = None


# ── 원장 ──────────────────────────────────────────────────────────────────────


@app.post(f"/accounting/{API_VERSION}/ledgers", status_code=201)
def create_ledger(body: LedgerIn) -> dict:
    """Fund/Book 원장을 연다. 같은 Fund/Book 이면 기존 원장을 그대로 돌려준다.

    멱등이 아니면 같은 Fund/Book 에 원장이 둘 생기고 NAV 가 갈린다. 그래서 201 이어도
    새로 만들어진 것이 아닐 수 있다.
    """
    if _store_error is not None:
        raise _store_error
    if _repo is not None:
        # DB 모드에서는 Fund/Book 을 여기서 만들지 않는다. Fund 를 여는 것은 자본 구조
        # 결정이라 주문 처리 중에 일어날 일이 아니다(`repository.bootstrap()` 이 한다).
        fund_id = _repo.fund_of_book(body.book_id)
        if fund_id is None:
            raise HTTPException(404, _envelope(
                "ACCOUNTING_BOOK_NOT_FOUND",
                f"accounting.books 에 그런 book_id 가 없습니다: {body.book_id}. "
                "Fund/Book 은 bootstrap 으로 먼저 개설합니다"))
        if fund_id != body.fund_id:
            raise HTTPException(400, _envelope(
                "ACCOUNTING_FUND_MISMATCH",
                f"이 Book 은 다른 Fund 의 것입니다: {fund_id}"))
    elif body.book_id not in _ledgers:
        _ledgers[body.book_id] = Ledger(fund_id=body.fund_id, book_id=body.book_id)
        _snapshots[body.book_id] = []
    # ledger_id 는 book_id 다. 별도 매핑표를 두면 재시작 후 같은 장부를 못 연다.
    return get_ledger(body.book_id)


@app.get(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}")
def get_ledger(ledger_id: UUID) -> dict:
    led = _ledger(ledger_id)
    positions, cash = led.rebuild()
    balances = led.trial_balance()
    return {
        "ledger_id": str(ledger_id),
        "fund_id": str(led.fund_id),
        "book_id": str(led.book_id),
        "journal_count": len(led.journals),
        "cash": _d(cash),
        "position_count": len(positions),
        "snapshot_count": len(_snapshot_history(led)),
        # 이중분개가 살아있으면 항상 0이다. 0이 아니면 원장이 깨진 것이다.
        "trial_balance_total": _d(sum(balances.values(), Decimal(0))),
        **_provenance(),
    }


@app.post(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/capital", status_code=201)
def post_capital(ledger_id: UUID, body: CapitalIn) -> dict:
    """자본 납입.  차) 현금 / 대) 자본금"""
    led = _ledger(ledger_id)
    return _journal_view(led.post_capital(body.amount, body.effective_at, body.source_event_id))


@app.post(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/fills", status_code=201)
def post_fill(ledger_id: UUID, body: FillIn) -> dict:
    """Offline fixture-only Fill ingress.

    Durable PAPER_DB accounting accepts fills only from the canonical SENT
    ``trading.fill.v1`` consumer. This endpoint remains for explicit offline
    tests and is never treated as runtime evidence.
    """
    led = _ledger(ledger_id)
    if _repo is not None or durable_required_from_env():
        raise HTTPException(409, _envelope(
            "ACCOUNTING_CANONICAL_FILL_REQUIRED",
            "API-injected Fill is fixture-only; canonical SENT trading.fill.v1 is required",
        ))
    journal = led.post_fill(
        body, body.side, body.instrument_id, _position(led, body.instrument_id),
        metadata={
            "evidence_class": "fixture_only",
            "canonical": False,
            "source": "accounting-api",
        },
    )
    return _journal_view(journal)


@app.post(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/corporate-actions", status_code=201)
def post_corporate_action(ledger_id: UUID, body: CorporateActionIn) -> dict:
    """확정·발효된 기업행위를 분개로 만든다.

    **공시(ANNOUNCED)로는 분개하지 않는다**(팀 가이드 8.2). EFFECTIVE 이고 발효일이
    지났으며, 선택형이면 승인까지 있어야 통과한다 - 게이트는 전부 도메인 모듈에 있다.
    """
    payload = body.model_dump(exclude={"record_date_quantity"})
    try:
        action = CorporateAction(**payload)
    except ValidationError as e:
        raise HTTPException(422, _envelope("ACCOUNTING_INVALID_CORPORATE_ACTION",
                                           "CorporateAction 계약 위반",
                                           detail=jsonable_encoder(e.errors())))
    led = _ledger(ledger_id)
    return _journal_view(apply_corporate_action(
        led, action, _position(led, body.instrument_id),
        record_date_quantity=body.record_date_quantity))


@app.get(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/journals")
def list_journals(ledger_id: UUID) -> dict:
    led = _ledger(ledger_id)
    return {"ledger_id": str(ledger_id),
            "journals": [_journal_view(j) for j in led.journals]}


@app.post(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/journals/{{journal_id}}/reverse",
          status_code=201)
def reverse_journal(ledger_id: UUID, journal_id: UUID, body: ReverseIn) -> dict:
    """반대 분개. **원본은 손대지 않는다**(불변식 2).

    이 API 에서 분개를 바꿀 수 있는 유일한 경로다. PUT/PATCH/DELETE 가 없는 것이 의도다.
    이미 반대분개된 분개를 또 뒤집는 것은 도메인이 막는다.
    """
    led = _ledger(ledger_id)
    if not any(j.journal_id == journal_id for j in led.journals):
        raise HTTPException(404, _envelope("ACCOUNTING_JOURNAL_NOT_FOUND",
                                           f"이 원장에 그런 journal_id 가 없습니다: {journal_id}"))
    return _journal_view(led.reverse(journal_id, body.reason))


@app.get(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/trial-balance")
def trial_balance(ledger_id: UUID) -> dict:
    """계정별 잔액. `total` 이 0이 아니면 원장이 깨진 것이다."""
    balances = _ledger(ledger_id).trial_balance()
    return {
        "ledger_id": str(ledger_id),
        "balances": {code: _d(amount) for code, amount in sorted(balances.items())},
        "total": _d(sum(balances.values(), Decimal(0))),
        **_provenance(),
    }


@app.get(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/statements")
def financial_statements_view(ledger_id: UUID, until: date | None = None) -> dict:
    """재무상태표·손익계산서. **취득원가 기준이다** - 시가 순자산은 NAV 쪽을 본다.

    항등식이 깨지면 여기서 200을 주지 않는다. 맞춰서 내보내는 재무제표는
    숫자가 아니라 거짓말이다.
    """
    try:
        return {"ledger_id": str(ledger_id),
                **financial_statements.statements(_ledger(ledger_id), until=until),
                **_provenance()}
    except financial_statements.StatementError as exc:
        raise HTTPException(status_code=409, detail=_envelope(
            "ACCOUNTING_STATEMENTS_UNBALANCED", str(exc), action="ESCALATE")) from exc


@app.get(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/positions")
def positions(ledger_id: UUID) -> dict:
    """분개만으로 재계산한 Position/Cash(불변식 4).

    평가금액은 여기 없다 - Mark 가 필요하고, 그건 `/valuations` 의 몫이다.
    """
    pos, cash = _ledger(ledger_id).rebuild()
    return {
        "ledger_id": str(ledger_id),
        "cash": _d(cash),
        "positions": [
            {"instrument_id": str(i), "quantity": _d(p.quantity),
             "average_cost": _d(p.average_cost), "cost_basis": _d(p.cost_basis)}
            for i, p in sorted(pos.items(), key=lambda kv: str(kv[0]))
        ],
        **_provenance(),
    }


# ── 평가 / NAV ────────────────────────────────────────────────────────────────


@app.post(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/valuations", status_code=201)
def create_valuation(ledger_id: UUID, body: ValuationIn) -> dict:
    """원장에 Mark 를 얹어 스냅샷을 만든다. **NAV 확정이 아니다**(`is_official: false`).

    보유 종목 중 하나라도 Mark 가 없거나 낡았으면 400 이고 부분 결과를 주지 않는다 -
    일부만 평가한 NAV 는 틀린 NAV 이고, 그걸로 주문을 내면 비중이 조용히 어긋난다.

    **시세를 여기서 조회하지 않는다.** market-api 소관이라 호출자가 준 값만 쓴다.
    """
    led = _ledger(ledger_id)
    marks = {m.instrument_id: MarkPrice(m.instrument_id, m.price, m.as_of, m.source,
                                        m.is_final)
             for m in body.marks}
    staleness = (timedelta(seconds=body.max_staleness_seconds)
                 if body.max_staleness_seconds else None)
    if _repo is not None:
        snapshot = project(_repo, led, marks, body.as_of, staleness)
    else:
        snapshot = (value_portfolio(led, marks, body.as_of, staleness) if staleness
                    else value_portfolio(led, marks, body.as_of))
        _snapshots[ledger_id].append(snapshot)
    return _snapshot_view(snapshot)


@app.get(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/valuations")
def list_valuations(ledger_id: UUID) -> dict:
    return {"ledger_id": str(ledger_id),
            "valuations": [_snapshot_view(s) for s in _snapshot_history(_ledger(ledger_id))]}


# ── 대사 ──────────────────────────────────────────────────────────────────────
# **Break 는 응답에만 있으면 안 된다.** 프로세스가 죽으면 사라지는 불일치는 없었던
# 것과 같다. DB 모드에서는 external_statements -> reconciliations ->
# reconciliation_items -> breaks 4단 사슬에 남기고, 리스크·QA 가 그 표를 읽는다.
# 이벤트 전송로(Redis)는 PLAT-02 대기다.


def _persist_recon(led: Ledger, result: ReconResult, source: StatementSourceIn,
                   external_payload) -> ReconRun | None:
    if _repo is None:
        return None
    return save_reconciliation(
        _repo, led.fund_id, result,
        provider=source.provider,
        account_ref=source.account_ref or str(led.book_id),
        external_payload=external_payload,
        object_path=source.object_path)


@app.post(f"/accounting/{API_VERSION}/reconciliations/fills")
def recon_fills(body: FillReconIn) -> dict:
    """내부 체결과 브로커 명세서를 대사한다.

    양쪽을 다 받는 유일한 대사다 - 내부 체결을 FillRecord 로 보관하지 않기 때문이다.
    Position/Cash 대사는 내부를 원장에서 재계산한다.

    `ledger_id` 를 주면 canonical 표에 남긴다. Fund 를 모르면 남길 수 없어서
    (`reconciliations.fund_id` 가 not null) 계산 결과만 돌려준다.
    """
    kwargs = {}
    if body.time_window_seconds:
        kwargs["time_window"] = timedelta(seconds=body.time_window_seconds)
    result = reconcile_fills(
        [r.to_record() for r in body.internal],
        [r.to_record() for r in body.external],
        as_of=body.as_of, **kwargs)
    run = None
    if body.ledger_id is not None:
        run = _persist_recon(_ledger(body.ledger_id), result, body.statement,
                             [r.model_dump(mode="json") for r in body.external])
    return _recon_view(result, run)


@app.post(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/reconciliations/positions")
def recon_positions(ledger_id: UUID, body: PositionReconIn) -> dict:
    """원장 projection 과 브로커 잔고를 대사한다. 수량 불일치는 항상 material 이다."""
    led = _ledger(ledger_id)
    pos, _ = led.rebuild()
    internal = {i: p.quantity for i, p in pos.items()}
    result = reconcile_positions(internal, body.external, as_of=body.as_of)
    return _recon_view(result, _persist_recon(
        led, result, body.statement,
        {str(k): _d(v) for k, v in sorted(body.external.items(), key=lambda kv: str(kv[0]))}))


@app.post(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/reconciliations/cash")
def recon_cash(ledger_id: UUID, body: CashReconIn) -> dict:
    """현금 대사. 반올림 수준 차이는 Break 로 올리지 않는다."""
    led = _ledger(ledger_id)
    _, cash = led.rebuild()
    kwargs = {"tolerance": body.tolerance} if body.tolerance is not None else {}
    result = reconcile_cash(cash, body.external, as_of=body.as_of, **kwargs)
    return _recon_view(result, _persist_recon(
        led, result, body.statement, {"cash": _d(body.external)}))


@app.get(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/breaks")
def list_open_breaks(ledger_id: UUID) -> dict:
    """미종결 Break. **리스크·QA 가 읽는 자리다.**

    이벤트 전송로가 붙기 전까지 여기가 전달 경로이며, 붙은 뒤에도 재동기화용으로
    남는다. `escalates: true` 가 팀 가이드 4.5 5번의 "리스크본부와 QA 로 전달"
    대상이다. **종결 경로는 없다** - 이 API 에 Break 를 닫는 메서드가 없는 것이 의도다.
    """
    led = _ledger(ledger_id)
    if _repo is None:
        raise HTTPException(503, _envelope(
            "ACCOUNTING_STORE_UNAVAILABLE",
            "인메모리 모드에는 저장된 Break 가 없습니다. DATABASE_URL 이 필요합니다"))
    breaks = open_breaks(_repo, led.fund_id)
    return {
        "ledger_id": str(ledger_id),
        "breaks": breaks,
        "escalating_count": sum(1 for b in breaks if b["escalates"]),
        "closable_here": False,
        **_provenance(),
    }


# ── 일일 보고 ─────────────────────────────────────────────────────────────────


@app.post(f"/accounting/{API_VERSION}/ledgers/{{ledger_id}}/daily-reports", status_code=201)
def create_daily_report(ledger_id: UUID, body: DailyReportIn) -> dict:
    """Preliminary 일일 보고서. **확정 수치가 아니다**(`is_official: false`).

    저장된 스냅샷을 쓰므로 `/valuations` 를 최소 2번(기초·기말) 부른 뒤에 가능하다.
    중간 스냅샷이 많을수록 Drawdown 이 정확해진다 - 기초·기말만 있으면 장중 저점을
    못 봐서 Drawdown 이 과소평가된다.

    `unexplained_pnl` 이 0이 아니면 원장·평가·자본유출입 중 어딘가 어긋난 것이다.
    반올림해서 없애지 않는다 - 그 값이 Break 의 근거다.
    """
    led = _ledger(ledger_id)
    report = build_daily_report(
        snapshots=_snapshot_history(led),
        ledger=led,
        accounting_date=body.accounting_date,
        strategy_of=body.strategy_of,
    )
    return {**report.to_dict(), **_provenance()}


# ── InvestorProfile (USER_INPUT_API_SPEC.md 2.3) ──────────────────────────────
# 온보딩 계층 1(USER_INPUT_SPEC.md 2절)의 성향·경험·기간·유동성을 저장한다.
# **이 테이블이 없던 동안** `POST /ui/portfolio-recommendations`가 매 요청 body로
# mindset/experience를 받아왔다 - 저장된 값을 읽는 게 아니라 매번 다시 받는
# 구조였고 "최초 1회 입력 후 재사용"이 불가능했다. 이 경로가 그 재사용의 근거다.
#
# **여기서 성향을 추론하지 않는다.** LLM이 mindset/experience를 정하는 것은
# `suitability.py` 계약과 USER_INPUT_SPEC 4.1이 영구 금지한 항목이다 - 이 Route는
# 화면이 사용자에게 직접 받은 값만 저장한다.


class InvestorProfileIn(BaseModel):
    """API_SPEC 2.3 Request. `suitability.InvestorProfile`의 저장 대상 필드만."""

    model_config = {"extra": "forbid"}

    user_id: str = Field(min_length=1, max_length=128)
    fund_id: str = Field(min_length=1, max_length=128)
    mindset: InvestmentMindset
    experience: ExperienceLevel
    investment_horizon_years: int = Field(ge=1, le=100)
    # 0~1 분수다(0.15 = 15%). `_pct` 접미사인데 분수인 것은 기존 계약
    # (apps/api/main.py PortfolioRecommendationRequest)과 맞춘 것이다.
    max_drawdown_pct: Decimal = Field(gt=0, le=Decimal(1))
    liquidity_need: LiquidityNeed = LiquidityNeed.MEDIUM
    as_of: datetime


def _investor_profile_repo() -> InvestorProfileRepository:
    """저장소가 없으면 503. **인메모리로 후퇴하지 않는다.**

    프로필은 "최초 1회 입력하고 계속 쓰는" 값이라 메모리에 저장하면 재기동 때
    조용히 사라지고 사용자는 온보딩을 다시 해야 한다. 저장이 안 되면 안 된다고
    말하는 편이 낫다(개발 원칙 9).
    """

    repo = InvestorProfileRepository.from_env()
    if repo is None:
        raise HTTPException(
            status_code=503,
            detail=_envelope(
                "INVESTOR_PROFILE_STORE_UNAVAILABLE",
                "DATABASE_URL이 없어 InvestorProfile을 저장·조회할 수 없습니다.",
            ),
        )
    return repo


def _effective_risk(profile: InvestorProfileIn) -> tuple[str, str]:
    """`suitability.py`의 `min(mindset, experience)`를 그대로 노출한다.

    API_SPEC 2.3: **화면이 재계산하지 않는다.** 등급 매핑을 여기서 다시 쓰지 않고
    `effective_risk_band()`를 부르는 이유가 그것이다.
    """

    normalized = InvestorProfile(
        user_id=profile.user_id,
        mindset=profile.mindset,
        experience=profile.experience,
        investment_horizon_years=profile.investment_horizon_years,
        max_drawdown_pct=profile.max_drawdown_pct,
        liquidity_need=profile.liquidity_need,
        as_of=profile.as_of,
    )
    band = effective_risk_band(normalized)
    if profile.experience.value != profile.mindset.value and str(band) != str(
        _band_of_mindset(profile.mindset)
    ):
        reason = (
            f"경험({profile.experience.value})이 성향({profile.mindset.value})보다 "
            "낮아 상한이 됩니다"
        )
    else:
        reason = f"성향({profile.mindset.value})과 경험({profile.experience.value}) 기준입니다"
    return str(band), reason


def _band_of_mindset(mindset: InvestmentMindset) -> str:
    """성향만 봤을 때의 등급. `_effective_risk`의 사유 문장 판정에만 쓴다."""

    return {
        InvestmentMindset.SAFETY_FIRST: "LOW",
        InvestmentMindset.BALANCED: "MEDIUM",
        InvestmentMindset.RISK_SEEKING: "HIGH",
    }[mindset]


@app.post("/portfolio/v1/investor-profiles", status_code=201)
def create_investor_profile(body: InvestorProfileIn) -> dict:
    """항상 새 `version`으로 저장한다(API_SPEC 2.3).

    **수정(PUT/PATCH)이 없다.** "그때 어떤 성향으로 추천했는가"가 감사 대상이라
    과거 버전이 덮이면 과거 추천의 근거가 사라진다(개발 원칙 5). Mandate와 달리
    Risk/QA 승인 절차는 없다 - advisory 입력이다.
    """

    repo = _investor_profile_repo()
    band, reason = _effective_risk(body)
    try:
        saved = repo.save(
            user_id=body.user_id,
            fund_id=body.fund_id,
            mindset=body.mindset.value,
            experience=body.experience.value,
            investment_horizon_years=body.investment_horizon_years,
            max_drawdown_pct=str(body.max_drawdown_pct),
            liquidity_need=body.liquidity_need.value,
            as_of=body.as_of.isoformat(),
            created_by=body.user_id,
        )
    finally:
        repo.close()
    return {
        "investor_profile_id": saved["investor_profile_id"],
        "version": saved["version"],
        "effective_risk_band": band,
        "effective_risk_reason": reason,
        **_provenance(),
    }


@app.get("/portfolio/v1/investor-profiles/current")
def get_current_investor_profile(user_id: str, fund_id: str) -> dict:
    """가장 높은 version 하나. 없으면 404 - 빈 프로필을 지어내지 않는다."""

    repo = _investor_profile_repo()
    try:
        row = repo.current(user_id=user_id, fund_id=fund_id)
    finally:
        repo.close()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=_envelope(
                "INVESTOR_PROFILE_NOT_FOUND",
                f"user_id={user_id} fund_id={fund_id}에 저장된 InvestorProfile이 없습니다.",
            ),
        )
    band, reason = _effective_risk(InvestorProfileIn(**{
        "user_id": row["user_id"],
        "fund_id": row["fund_id"],
        "mindset": row["mindset"],
        "experience": row["experience"],
        "investment_horizon_years": row["investment_horizon_years"],
        "max_drawdown_pct": Decimal(row["max_drawdown_pct"]),
        "liquidity_need": row["liquidity_need"],
        "as_of": row["as_of"],
    }))
    return {**row, "effective_risk_band": band, "effective_risk_reason": reason, **_provenance()}


@app.get("/health")
def health() -> dict:
    """Liveness. **저장소가 죽어도 200이다.**

    여기서 503을 내면 오케스트레이터(EB/ECS 헬스체크)가 DB 순단마다 멀쩡한 인스턴스를
    교체한다 - 프로세스는 살아 있고 분개를 올바르게 거절하고 있는데 죽었다고 판정하는
    것이다. 거절은 도메인 엔드포인트와 `/health/ready` 가 한다.

    **저장소를 건드리지 않는다.** `_repo.counts()` 는 DB I/O 라 여기 두면 DB 가 죽었을
    때 이 경로도 같이 503이 되어 분리한 의미가 없어진다. 수치는 `/health/ready` 에 있다.
    """
    return {
        "status": "degraded" if _store_error is not None else "ok",
        "api_version": API_VERSION,
        "store": ("control-db accounting.*" if _repo is not None
                  else "in-memory (accounting.* 미연결)"),
        "store_available": _store_error is None,
        "store_error": str(_store_error) if _store_error is not None else None,
    }


@app.get("/health/ready")
def health_ready() -> dict:
    """Readiness. 저장소에 실제로 닿아 보고, 못 닿으면 503이다.

    Load Balancer 가 트래픽을 끊을 판단은 이쪽을 본다(`apps/api/main.py` 와 같은 규약).
    """
    if _store_error is not None:
        raise _store_error
    # 인메모리 모드의 값들이 재시작마다 0으로 돌아간다는 사실을 숨기지 않는다.
    ledgers, journals = (_repo.counts() if _repo is not None
                         else (len(_ledgers), sum(len(l.journals) for l in _ledgers.values())))
    return {
        "status": "ready",
        "api_version": API_VERSION,
        "ledgers": ledgers,
        "journals": journals,
        "store": ("control-db accounting.*" if _repo is not None
                  else "in-memory (accounting.* 미연결)"),
    }


# ── 자체 점검 ─────────────────────────────────────────────────────────────────
# TestClient 로 실제 요청을 태운다. 네트워크·DB 없음.

if __name__ == "__main__":
    from fastapi.testclient import TestClient

    # 인메모리로 고정한다. DATABASE_URL 이 환경에 있어도 자체 점검이 실 장부에
    # 점검용 분개를 남기면 안 된다. DB 모드 왕복 검증은 ledger/repository.py 와
    # ledger/fill_consumer.py 가 자기 Fixture 장부에서 한다.
    _repo = None
    _store_error = None

    c = TestClient(app)
    now = _now()
    D = Decimal
    fund, book, stock = str(uuid4()), str(uuid4()), str(uuid4())

    def fill(bfid: str, side: str, qty: str, price: str, fee="0", tax="0") -> dict:
        return {"instrument_id": stock, "side": side, "quantity": qty, "price": price,
                "fee": fee, "tax": tax, "event_time": now.isoformat(), "broker_fill_id": bfid}

    # 1. 원장 생성은 Fund/Book 에 멱등하다
    r = c.post("/accounting/v1/ledgers", json={"fund_id": fund, "book_id": book})
    assert r.status_code == 201, r.text
    lid = r.json()["ledger_id"]
    again = c.post("/accounting/v1/ledgers", json={"fund_id": fund, "book_id": book})
    assert again.json()["ledger_id"] == lid, "같은 Fund/Book 에 원장이 둘 생겼다"
    # ledger_id == book_id. 매핑표가 없어야 재시작 후에도 같은 id 로 같은 장부를 연다
    assert lid == book, f"ledger_id 가 book_id 와 다르다: {lid}"

    # 2. 자본 납입 -> 현금
    cap = c.post(f"/accounting/v1/ledgers/{lid}/capital",
                 json={"amount": "1000000000", "effective_at": now.isoformat(),
                       "source_event_id": "seed_capital"})
    assert cap.status_code == 201, cap.text
    assert c.get(f"/accounting/v1/ledgers/{lid}/positions").json()["cash"] == "1000000000"

    # 3. 매수 분개. 차대가 맞고 시산표 합계는 0이다
    buy = c.post(f"/accounting/v1/ledgers/{lid}/fills", json=fill("bf_1", "BUY", "100", "70000", "1050"))
    assert buy.status_code == 201, buy.text
    tb = c.get(f"/accounting/v1/ledgers/{lid}/trial-balance").json()
    assert tb["total"] == "0", tb

    # 4. 같은 broker_fill_id 재수신은 분개를 두 번 만들지 않는다(불변식 3)
    dup = c.post(f"/accounting/v1/ledgers/{lid}/fills", json=fill("bf_1", "BUY", "100", "70000", "1050"))
    assert dup.json()["journal_id"] == buy.json()["journal_id"], "중복 체결로 분개가 두 건 생겼다"
    pos = c.get(f"/accounting/v1/ledgers/{lid}/positions").json()["positions"]
    assert pos[0]["quantity"] == "100", f"중복 분개로 포지션이 두 배가 됐다: {pos}"

    # 5. 보유보다 많은 매도는 400 봉투로 막힌다
    over = c.post(f"/accounting/v1/ledgers/{lid}/fills", json=fill("bf_bad", "SELL", "999", "75000"))
    assert over.status_code == 400 and \
        over.json()["error_code"] == "ACCOUNTING_LEDGER_REJECTED", over.text

    # 6. 평균원가를 호출자가 못 준다 - 실현손익은 원장이 정한다.
    #    40주를 75,000에 팔면 (75000-70000)*40 = 200,000 이 나와야 한다.
    sell = c.post(f"/accounting/v1/ledgers/{lid}/fills",
                  json=fill("sf_1", "SELL", "40", "75000", "450", "4500"))
    assert sell.status_code == 201, sell.text
    tb = c.get(f"/accounting/v1/ledgers/{lid}/trial-balance").json()["balances"]
    assert tb["4000"] == "-200000", f"실현손익이 원장 평균원가로 계산되지 않았다: {tb}"
    assert tb["5000"] == "1500" and tb["5100"] == "4500", f"비용이 손익에 섞였다: {tb}"

    # 7. Mark 가 없으면 NAV 를 만들지 않는다. 부분 평가를 주지 않는다
    noval = c.post(f"/accounting/v1/ledgers/{lid}/valuations",
                   json={"as_of": now.isoformat(), "marks": []})
    assert noval.status_code == 400 and \
        noval.json()["error_code"] == "ACCOUNTING_VALUATION_REJECTED", noval.text

    # 8. 낡은 Mark 도 거부한다(기본 5분)
    stale = c.post(f"/accounting/v1/ledgers/{lid}/valuations",
                   json={"as_of": now.isoformat(),
                         "marks": [{"instrument_id": stock, "price": "75000",
                                    "as_of": (now - timedelta(hours=1)).isoformat()}]})
    assert stale.status_code == 400, "낡은 가격으로 NAV 가 나왔다"

    # 9. 신선한 Mark 로 스냅샷. NAV = 현금 + 평가금액, 그리고 확정이 아니다
    def value(price: str, at: datetime) -> dict:
        return c.post(f"/accounting/v1/ledgers/{lid}/valuations",
                      json={"as_of": at.isoformat(),
                            "marks": [{"instrument_id": stock, "price": price,
                                       "as_of": at.isoformat()}]}).json()

    open_snap = value("75000", now)
    assert open_snap["is_official"] is False, "API 가 NAV 를 확정했다"
    assert open_snap["securities_value"] == "4500000", open_snap   # 60주 x 75,000
    expected_nav = D(open_snap["cash"]) + D("4500000")
    assert open_snap["nav"] == str(expected_nav), open_snap

    # 10. Posted Journal 은 수정 경로가 없다. Reversal 만 있고 원본은 남는다(불변식 2).
    #     URL 하나를 찔러보는 대신 라우팅 표 전체를 본다 - 이 API 에 수정·삭제
    #     메서드가 아예 없다는 것이 검사 대상이고, 그래야 나중에 누가 추가하면 걸린다.
    mutating = sorted(
        f"{m} {r.path}" for r in app.routes for m in getattr(r, "methods", set())
        if m in {"PUT", "PATCH", "DELETE"}
    )
    assert not mutating, f"수정·삭제 경로가 생겼다: {mutating}"
    before = len(c.get(f"/accounting/v1/ledgers/{lid}/journals").json()["journals"])
    rev = c.post(f"/accounting/v1/ledgers/{lid}/journals/{cap.json()['journal_id']}/reverse",
                 json={"reason": "자체 점검"})
    assert rev.status_code == 201 and rev.json()["reversal_of"] == cap.json()["journal_id"], rev.text
    journals = c.get(f"/accounting/v1/ledgers/{lid}/journals").json()["journals"]
    assert len(journals) == before + 1, "원본을 지웠다"
    assert next(j for j in journals if j["journal_id"] == cap.json()["journal_id"])["status"] == "reversed"
    dup_rev = c.post(f"/accounting/v1/ledgers/{lid}/journals/{cap.json()['journal_id']}/reverse",
                     json={"reason": "again"})
    assert dup_rev.status_code == 400, "이중 반대분개가 통과했다"

    # 11. 공시(ANNOUNCED)로는 분개하지 않는다. EFFECTIVE 만 반영한다
    l2 = c.post("/accounting/v1/ledgers",
                json={"fund_id": str(uuid4()), "book_id": str(uuid4())}).json()["ledger_id"]
    c.post(f"/accounting/v1/ledgers/{l2}/capital",
           json={"amount": "10000000", "effective_at": now.isoformat(), "source_event_id": "s2"})
    c.post(f"/accounting/v1/ledgers/{l2}/fills", json=fill("b2", "BUY", "10", "70000"))
    ca = {"action_id": "CA-1", "action_type": "CASH_DIVIDEND", "instrument_id": stock,
          "record_date": (now - timedelta(days=2)).isoformat(),
          "effective_at": (now - timedelta(days=1)).isoformat(),
          "amount_per_share": "100", "withholding_tax": "15.4"}
    announced = c.post(f"/accounting/v1/ledgers/{l2}/corporate-actions",
                       json={**ca, "status": "ANNOUNCED"})
    assert announced.status_code == 400 and \
        announced.json()["error_code"] == "ACCOUNTING_CORPORATE_ACTION_REJECTED", announced.text
    effective = c.post(f"/accounting/v1/ledgers/{l2}/corporate-actions",
                       json={**ca, "status": "EFFECTIVE"})
    assert effective.status_code == 201, effective.text
    # action_id 가 멱등 키다
    assert c.post(f"/accounting/v1/ledgers/{l2}/corporate-actions",
                  json={**ca, "status": "EFFECTIVE"}).json()["journal_id"] == \
        effective.json()["journal_id"], "같은 Action 으로 분개가 두 번 생겼다"

    # 12. Position 대사 - 내부는 원장에서 재계산한다. 불일치는 항상 material
    matched = c.post(f"/accounting/v1/ledgers/{lid}/reconciliations/positions",
                     json={"external": {stock: "60"}})
    assert matched.json()["result"] == "matched", matched.text
    mismatch = c.post(f"/accounting/v1/ledgers/{lid}/reconciliations/positions",
                      json={"external": {stock: "59"}})
    assert mismatch.json()["material_break_count"] == 1, mismatch.text
    assert mismatch.json()["closable_here"] is False, "API 가 Break 종결 권한을 주장한다"

    # 13. Cash 대사 - 반올림 차이는 Break 가 아니다
    cash_now = D(c.get(f"/accounting/v1/ledgers/{lid}/positions").json()["cash"])
    assert c.post(f"/accounting/v1/ledgers/{lid}/reconciliations/cash",
                  json={"external": str(cash_now - 1)}).json()["result"] == "matched"
    big = c.post(f"/accounting/v1/ledgers/{lid}/reconciliations/cash",
                 json={"external": str(cash_now - 5000)})
    assert big.json()["breaks"], "큰 현금 차이가 Break 없이 통과했다"

    # 14. 체결 대사 - 브로커에만 있는 체결은 Break 다
    only_theirs = {"instrument_id": stock, "side": "BUY", "quantity": "10", "price": "70000",
                   "event_time": now.isoformat(), "broker_fill_id": "ghost", "ref": "ext_1"}
    ghost = c.post("/accounting/v1/reconciliations/fills",
                   json={"internal": [], "external": [only_theirs]})
    assert ghost.json()["breaks"], "우리 원장에 없는 브로커 체결이 조용히 통과했다"

    # 15. Daily Report - 스냅샷 2개 필요, 그리고 확정 수치가 아니다
    one_only = c.post(f"/accounting/v1/ledgers/{l2}/daily-reports",
                      json={"accounting_date": now.date().isoformat()})
    assert one_only.status_code == 400 and \
        one_only.json()["error_code"] == "ACCOUNTING_REPORT_REJECTED", one_only.text
    value("76000", now + timedelta(hours=1))
    rep = c.post(f"/accounting/v1/ledgers/{lid}/daily-reports",
                 json={"accounting_date": now.date().isoformat()})
    assert rep.status_code == 201, rep.text
    assert rep.json()["is_official"] is False, "API 가 NAV 를 공식 확정했다"
    assert rep.json()["authoritative"] is False

    # 16. health 가 in-memory 사실을 숨기지 않는다. 없는 원장은 404 봉투
    assert c.get("/health").json()["store"].startswith("in-memory")
    missing = c.get(f"/accounting/v1/ledgers/{uuid4()}")
    assert missing.status_code == 404 and \
        missing.json()["error_code"] == "ACCOUNTING_LEDGER_NOT_FOUND", missing.text

    # 16-1. **Liveness 는 저장소를 건드리지 않는다.** `_repo.counts()` 는 DB I/O 라
    #       여기 두면 DB 가 죽었을 때 liveness 도 같이 503이 되고, 그러면 EB/ECS
    #       헬스체크가 멀쩡한 인스턴스를 교체한다. 수치와 판정은 /health/ready 가 한다.
    live = c.get("/health")
    assert live.status_code == 200 and live.json()["store_available"] is True
    assert "journals" not in live.json(), "liveness 가 저장소를 조회했다"
    ready = c.get("/health/ready")
    assert ready.status_code == 200 and "journals" in ready.json(), ready.text

    # 16-2. 저장소가 죽으면 liveness 는 200 degraded, readiness 와 도메인 경로만 503.
    #       `import app` 으로는 안 된다 - 이 파일은 __main__ 으로 돌고 있어서 별개
    #       모듈 객체가 생긴다. 여기가 모듈 최상위라 직접 대입이 곧 그 전역이다.
    _saved_store_error = _store_error
    _store_error = LedgerPersistenceError("durable accounting mode requires DATABASE_URL: 점검용")
    try:
        degraded = c.get("/health")
        assert degraded.status_code == 200, "저장소 장애에 liveness 가 죽었다"
        assert degraded.json()["status"] == "degraded", degraded.text
        assert degraded.json()["store_available"] is False
        assert c.get("/health/ready").status_code == 503, "readiness 가 장애를 숨겼다"
        # degraded 가 "그래도 분개는 받는다"가 되면 안 된다
        assert c.get(f"/accounting/v1/ledgers/{lid}").status_code == 503
    finally:
        _store_error = _saved_store_error

    print("ok - Accounting/Portfolio Domain API 16개 영역 점검 통과 "
          "(차대균형·멱등·Reversal 전용 정정·NAV 미확정 포함)")
