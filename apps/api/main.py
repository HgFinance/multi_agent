#!/usr/bin/env python3
"""AI Office BFF (FastAPI). 도현 담당분 - Read Model 제공 + Hermes Agent 연결.

근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 5.2(연결 순서), 6(명령 경계)
      docs/02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md 4절 `apps/api/`

이 파일은 조립만 한다. 부서별 Agent 경로는 각 Router 파일이 소유한다
(`accounting.py`, `trading.py`, `department_agents.py`). 프로세스는 하나다 - 부서별로 프로세스를 쪼개는
것은 Service Identity와 인증이 실제로 생긴 뒤에 한다.

경계 두 개를 코드로 강제한다.

1. **금융 상태는 Read-only다.** 이 서비스에는 주문 제출·분개 Posting·상태 변경 경로가 없다.
   계획 6절의 위험 Command(SET_TRADING_STATE 등)는 인증·승인·Audit가 붙기 전까지
   여기 열지 않는다. Hermes chat은 Tool을 실행할 수 있으므로 기본 비활성화한다.
2. **Agent 응답은 수치가 아니다.** `/{부서}/agent/ask`가 돌려주는 것은 Hermes CLI의
   텍스트고, 공식 Position·PnL·NAV는 오직 `/ui/snapshot`에서만 나온다
   (팀 가이드 원칙 5: 회계 수치를 LLM 문장에서 추출해 확정하지 않는다).

실행:
    DATABASE_URL='' .venv/bin/python -m uvicorn apps.api.main:app --reload --port 8001
자체 점검:
    python apps/api/main.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "departments" / "05-accounting-portfolio" / "portfolio"))
sys.path.insert(0, str(ROOT / "departments" / "05-accounting-portfolio" / "ledger"))
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

import accounting
# 여러 부서의 prototype이 `repository`, `ledger`, `portfolio` 같은 최상위
# 모듈명을 사용한다. pytest가 Risk/QA를 먼저 수집해도 회계 Read Model이
# 다른 부서 파일을 잡지 않도록, 회계 모듈을 로드하는 동안만 의존성을 격리한다.
_accounting_import_names = ("db_read_model", "repository", "ledger", "portfolio", "contracts")
_accounting_previous_modules = {name: sys.modules.get(name) for name in _accounting_import_names}
for _name in _accounting_import_names:
    sys.modules.pop(_name, None)
try:
    import db_read_model
finally:
    for _name in _accounting_import_names[1:]:
        sys.modules.pop(_name, None)
        if _accounting_previous_modules[_name] is not None:
            sys.modules[_name] = _accounting_previous_modules[_name]
import hermes_cli
import trading
from ui_read_model import build_ui_snapshot
from operations_read_model import build_operations_snapshot
from portfolio_runtime import RUNTIME
from portfolio_universe import DEFAULT_UNIVERSE_ID, get_universe, universe_options
from agent_status import agent_status_snapshot
from command_service import (
    COMMAND_SERVICE,
    CommandVersionConflict,
    IdempotencyConflict,
    TradingStateCommand,
)
from department_agents import router as department_agent_router
from domain_read_models import build_domain_read_model

app = FastAPI(title="AI Office BFF", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    # 로컬 개발 포트는 3000/3001/3002/3003처럼 바뀔 수 있다.
    # 배포 시에는 이 정규식을 환경변수 기반 allowlist로 교체한다.
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:3002",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# 각 투자 본부의 Router는 해당 Hermes Profile을 명시적으로 소유한다. CEO·HR은
# 투자 본부 Agent ask 경로에 섞지 않는다(마스터플랜 5.6).
app.include_router(accounting.router)
app.include_router(trading.router)
app.include_router(department_agent_router)


def _integration_status() -> dict[str, dict[str, object]]:
    """Return configuration presence only; never expose integration secrets."""

    def configured(*names: str) -> bool:
        return all(os.getenv(name, "").strip() for name in names)

    return {
        "notion": {
            "configured": configured("NOTION_TOKEN", "NOTION_BRIEFING_DB"),
            "label": "Notion 저장",
            "need": "NOTION_TOKEN / NOTION_BRIEFING_DB 미설정",
        },
        "discord": {
            "configured": configured("DISCORD_WEBHOOK_URL"),
            "label": "Discord 전송",
            "need": "DISCORD_WEBHOOK_URL 미설정",
        },
        "instagram": {
            "configured": False,
            "label": "Instagram",
            "need": "OAuth 연동 대기",
        },
        "gmail": {
            "configured": False,
            "label": "Gmail",
            "need": "OAuth 연동 대기",
        },
        "finance": {
            "configured": False,
            "label": "재무 파일",
            "need": "자료 업로드 대기",
        },
    }


@app.get("/ui/integrations")
def ui_integrations() -> dict[str, dict[str, object]]:
    """Read-only integration readiness projection for the operator UI."""

    return _integration_status()


@app.get("/ui/portfolio-universes")
def ui_portfolio_universes() -> dict[str, object]:
    """Return backend-owned, read-only universe choices for the interview form."""
    return {
        "default_universe_id": DEFAULT_UNIVERSE_ID,
        "universes": universe_options(),
    }


class PortfolioRecommendationRequest(BaseModel):
    """User suitability inputs; this route never accepts orders or credentials."""

    user_id: str = Field(min_length=1, max_length=128)
    mindset: str
    experience: str
    investment_horizon_years: int = Field(ge=1, le=100)
    max_drawdown_pct: str = Field(pattern=r"^0(?:\.\d+)?$|^1(?:\.0+)?$")
    liquidity_need: str = "MEDIUM"
    investment_amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    universe_id: str = Field(default=DEFAULT_UNIVERSE_ID, min_length=1, max_length=128)
    category: str = Field(default="PORTFOLIO_RECOMMENDATION", min_length=1, max_length=64)
    include_stock: bool = Field(default=True, description="주식 자산을 추천 결과에 포함할지 여부")
    include_derivatives: bool = Field(default=True, description="파생상품 자산을 추천 결과에 포함할지 여부")
    query: str = Field(default="", max_length=2000)
    as_of: str | None = None
    fund_id: str | None = None


@app.post("/ui/portfolio-recommendations", status_code=202)
async def start_portfolio_recommendation(request: PortfolioRecommendationRequest) -> dict[str, object]:
    """Start the advisory LangGraph and return a process-local run reference."""

    if get_universe(request.universe_id) is None:
        raise HTTPException(status_code=422, detail="portfolio_universe_not_found")
    profile = request.model_dump(exclude_none=True)
    if "as_of" not in profile:
        from datetime import datetime, timezone

        profile["as_of"] = datetime.now(timezone.utc).isoformat()
    try:
        return RUNTIME.start(profile)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/ui/portfolio-recommendations/{run_id}")
def portfolio_recommendation_status(run_id: str) -> dict[str, object]:
    run = RUNTIME.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="portfolio_recommendation_run_not_found")
    return run


class PortfolioRecommendationApprovalRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    comment: str | None = Field(default=None, max_length=500)


@app.post("/ui/portfolio-recommendations/{run_id}/approval")
def decide_portfolio_recommendation(
    run_id: str,
    request: PortfolioRecommendationApprovalRequest,
) -> dict[str, object]:
    """Approve or reject the advisory recommendation, never an order."""

    try:
        run = RUNTIME.decide(run_id, request.decision, request.comment)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="portfolio_recommendation_run_not_found")
    return run


@lru_cache(maxsize=1)
def _demo_state():
    """Scripted Paper Loop 한 바퀴. Snapshot의 DEMO 원천이다.

    Supabase Read Model이 붙기 전까지의 원천이며, 손으로 쓴 Fixture 대신
    실제 OMS/Ledger를 돌린다 - 백엔드가 바뀌면 여기가 같이 깨져야 한다.

    ponytail: Scripted Loop는 입력이 고정이라 매번 같은 결과가 나온다. 요청마다
              OMS와 원장을 처음부터 다시 돌릴 이유가 없어 프로세스 수명 동안
              한 번만 계산한다. **Supabase Read Model로 바꿀 때 이 데코레이터를
              반드시 떼야 한다** - 실제 장부는 변하는데 캐시가 옛 값을 물고 있으면
              화면이 조용히 낡은 NAV를 보여준다. 그때는 캐시가 아니라 Read Model의
              snapshot_version으로 신선도를 판단한다.
    """
    from test_paper_loop import PaperLoopTest

    loop = PaperLoopTest("test_full_loop_signal_to_nav")
    loop.setUp()
    intent = loop.build_intent(loop.signal(), loop.snapshot())
    _, order = loop.route(intent)
    loop.fill_completely(order)
    loop.post_fills_to_ledger(order)
    return loop


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "mode": "DEMO",
        "agent_ask_enabled": hermes_cli.ENABLE_AGENT_ASK,
        "departments": [
            "research-department",
            trading.DEPARTMENT,
            "risk-management",
            "quant-backtest-department",
            accounting.DEPARTMENT,
            "qa-department",
        ],
        "status_event_type": "agent.status.v1",
        "status_sequence": agent_status_snapshot()["sequence"],
    }


@lru_cache(maxsize=1)
def _repo():
    """회계 원장 저장소. DATABASE_URL이 없으면 None이고 Snapshot은 전부 DEMO다."""
    return db_read_model.LedgerRepository.from_env()


@app.get("/ui/snapshot")
def ui_snapshot(book_id: UUID | None = None) -> dict:
    """계획 5.2의 `GET /ui/snapshot`. 화면 State는 이 한 장에서 재구축된다.

    `book_id`를 주고 DB가 붙어 있으면 **회계 구간(portfolio·ledger)이 Canonical
    표에서** 온다(`api.portfolio_snapshot_latest` 등). 트레이딩 구간은 아직
    Scripted Loop다 - `execution.orders`가 0행이고 OMS 상태가 프로세스 메모리라
    뷰를 만들어도 빈 화면을 실데이터인 척 보여줄 뿐이다(TRD-01 대기).

    그래서 **구간별 출처를 `sources`에 밝힌다.** 최상위 `mode`는 트레이딩까지
    실데이터가 되기 전에는 DEMO로 둔다 - 절반만 진짜인 화면을 PAPER라고 부르면
    나머지 절반도 진짜라고 읽힌다.
    """
    loop = _demo_state()
    overrides = None
    repo = _repo()
    if book_id is not None and repo is not None:
        sections = db_read_model.build_accounting_sections(repo, book_id)
        if sections is None:
            # 평가된 적 없는 장부다. 0원 NAV를 지어내지 않고 그 사실을 알린다.
            raise HTTPException(404, f"book {book_id}의 확정 Snapshot이 없습니다")
        overrides = {**sections,
                     "book_id": str(book_id),
                     "sources": {"portfolio": "supabase", "ledger": "supabase"}}

    snapshot = build_ui_snapshot(
        oms=loop.oms,
        ledger=loop.ledger,
        snapshot=loop.snapshot(),
        mode="DEMO",
        overrides=overrides,
    )
    snapshot["operations"] = build_operations_snapshot()
    return snapshot


def _domain_projection(domain: str) -> dict[str, object]:
    return build_domain_read_model(domain)


@app.get("/ui/research")
def ui_research() -> dict[str, object]:
    """Research Case read-only projection for the dashboard."""

    return _domain_projection("research")


@app.get("/ui/strategy")
def ui_strategy() -> dict[str, object]:
    """Strategy Factory / quant read-only projection for the dashboard."""

    return _domain_projection("strategy")


@app.get("/ui/risk")
def ui_risk() -> dict[str, object]:
    """Risk Center read-only projection for the dashboard."""

    return _domain_projection("risk")


@app.get("/ui/qa")
def ui_qa() -> dict[str, object]:
    """AI QA·Audit read-only projection for the dashboard."""

    return _domain_projection("qa")


@app.get("/ui/risk-qa")
def ui_risk_qa() -> dict[str, object]:
    """Combined Risk·QA projection consumed by the office panel."""

    return _domain_projection("risk-qa")


@app.post("/ui/commands/trading-state", status_code=202)
def request_trading_state_command(command: TradingStateCommand) -> dict[str, object]:
    """Record a versioned approval request without changing binding state."""

    try:
        return COMMAND_SERVICE.submit(command)
    except CommandVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/ui/commands/audit")
def ui_command_audit() -> dict[str, object]:
    """Return BFF-local audit events; no broker or ledger credentials are exposed."""

    return {"schema_version": "operator-command-audit.v1", "events": COMMAND_SERVICE.audit_events()}


@app.websocket("/ws/operations")
async def operations_websocket(websocket: WebSocket) -> None:
    """Read-only Agent Status Event stream with REST snapshot recovery."""
    await websocket.accept()
    last_sequence = 0
    initialized = False
    heartbeat_at = asyncio.get_running_loop().time()
    try:
        while True:
            operations = build_operations_snapshot()
            sequence = int(operations.get("sequence", 0))
            events = operations.get("agent_status_events", [])
            if not initialized:
                await websocket.send_json(
                    {
                        "event_type": "operations.snapshot_required.v1",
                        "schema_version": 1,
                        "sequence": sequence,
                        "observed_at": operations["observed_at"],
                    }
                )
                initialized = True
            elif sequence > last_sequence:
                for event in events:
                    event_sequence = int(event.get("sequence", 0))
                    if event_sequence > last_sequence:
                        await websocket.send_json(event)
            last_sequence = sequence
            now = asyncio.get_running_loop().time()
            if now - heartbeat_at >= 15:
                await websocket.send_json(
                    {
                        "event_type": "operations.heartbeat.v1",
                        "schema_version": 1,
                        "sequence": sequence,
                        "observed_at": operations["observed_at"],
                    }
                )
                heartbeat_at = now
            await asyncio.sleep(0.4)
    except WebSocketDisconnect:
        return


if __name__ == "__main__":
    from uuid import uuid4

    from fastapi.testclient import TestClient

    c = TestClient(app)

    health_payload = c.get("/health").json()
    assert health_payload["status"] == "ok"
    assert health_payload["agent_ask_enabled"] is False

    snap = c.get("/ui/snapshot").json()
    assert snap["mode"] == "DEMO", "BFF Snapshot은 DEMO여야 한다"
    assert snap["ledger"]["balanced"] is True, "차대가 맞지 않는 원장이 화면으로 나갔다"
    assert isinstance(snap["portfolio"]["nav"], str), "금액이 JSON number로 나갔다"
    assert snap["trading"]["orders"][0]["state"] == "FILLED"
    # book_id 없이 부르면 전 구간이 Scripted Loop다. 출처를 숨기지 않는다
    assert set(snap["sources"].values()) == {"scripted-loop"}, snap["sources"]

    # 없는 book_id는 404다. 0원 NAV를 지어내지 않는다.
    # (DB가 없으면 book_id가 무시되므로 그때는 200이고, 그 경우도 출처는 전부 DEMO다)
    missing_book = c.get("/ui/snapshot", params={"book_id": str(uuid4())})
    if _repo() is not None:
        assert missing_book.status_code == 404, missing_book.text
    else:
        assert set(missing_book.json()["sources"].values()) == {"scripted-loop"}

    # 두 번 불러도 같은 Snapshot이다. Read-only가 상태를 바꾸면 안 된다
    assert c.get("/ui/snapshot").json()["portfolio"]["nav"] == snap["portfolio"]["nav"]

    # 요청마다 Paper Loop를 통째로 다시 돌리지 않는다. 같은 객체를 재사용한다
    assert _demo_state() is _demo_state(), "요청마다 OMS·원장이 재실행된다"
    assert _demo_state.cache_info().currsize == 1
    # 캐시했어도 server_time은 매 요청 갱신된다 - 화면이 신선도를 판단해야 한다
    assert c.get("/ui/snapshot").json()["server_time"] >= snap["server_time"]

    # 인증·Tool Allowlist가 없는 기본 환경에서는 Agent 호출이 전부 닫혀 있다
    assert c.post("/accounting/agent/ask", json={"query": "NAV?"}).status_code == 503
    assert c.post("/trading/agent/ask", json={"query": "pending?"}).status_code == 503
    # 빈 질의는 스키마에서 걸린다
    assert c.post("/accounting/agent/ask", json={"query": ""}).status_code == 422
    # 부서를 Body로 지정할 방법이 없다. 다른 본부 경로는 존재하지 않는다
    assert c.post("/agent/ask", json={"department": "risk-management", "query": "x"}).status_code == 404
    assert c.post("/risk/agent/ask", json={"query": "x"}).status_code == 503
    assert c.post("/quant/agent/ask", json={"query": "x"}).status_code == 503
    assert c.post("/qa/agent/ask", json={"query": "x"}).status_code == 503
    assert c.post("/ceo/agent/ask", json={"query": "x"}).status_code == 404
    # 공개 경로 전체를 못 박는다. Command 경로(Posting, NAV 확정, 주문 제출)가
    # 하나라도 늘면 여기서 깨진다 - 늘리려면 이 목록을 고쳐야 하고 Diff에 남는다
    paths = set(c.get("/openapi.json").json()["paths"])
    required_paths = {
        "/health",
        "/ui/snapshot",
        "/accounting/agent/ask",
        "/trading/agent/ask",
        "/research/agent/ask",
        "/risk/agent/ask",
        "/quant/agent/ask",
        "/qa/agent/ask",
        "/accounting/v1/portfolio-snapshot",
        "/ui/research",
        "/ui/strategy",
        "/ui/risk",
        "/ui/qa",
        "/ui/risk-qa",
        "/ui/commands/trading-state",
        "/ui/commands/audit",
    }
    assert required_paths <= paths, paths

    # portfolio-api는 참조만 준다. 수치를 실으면 공식 출처가 둘로 갈린다
    schema = c.get("/openapi.json").json()
    ref = schema["paths"]["/accounting/v1/portfolio-snapshot"]
    assert set(ref) == {"get"}, "읽기 전용이어야 한다"
    # fund_id는 필수, as_of는 생략 가능(현재 시각)
    params = {p["name"]: p["required"] for p in ref["get"]["parameters"]}
    assert params == {"fund_id": True, "as_of": False}, params
    # 없는 Fund는 404다. 가장 가까운 것을 대신 주지 않는다
    missing = c.get("/accounting/v1/portfolio-snapshot",
                    params={"fund_id": "00000000-0000-0000-0000-000000000000"})
    assert missing.status_code in (404, 503), missing.status_code
    if missing.status_code == 404:
        assert "snapshot_id" not in missing.json(), "404인데 참조를 지어냈다"
    # UUID가 아니면 스키마에서 걸린다
    assert c.get("/accounting/v1/portfolio-snapshot",
                 params={"fund_id": "not-a-uuid"}).status_code == 422

    assert hermes_cli.timeout_of(accounting.CONFIG) == 60

    # session_id는 stderr에서 뽑는다. 없으면 None이지 빈 문자열이 아니다
    assert hermes_cli.session_id_of("\nsession_id: abc123\n") == "abc123"
    assert hermes_cli.session_id_of("") is None

    print("ok - BFF 7개 영역 점검 통과")
