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
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", override=False)
# Backend-only integration readiness. Never return these values to the browser.
load_dotenv(ROOT / "ai-office" / ".dev.vars", override=False)
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
try:
    from . import ceo
    from .ceo import router as ceo_router
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    import ceo
    from ceo import router as ceo_router
import trading
from agent_status import agent_status_snapshot
from command_service import (
    COMMAND_SERVICE,
    CommandVersionConflict,
    IdempotencyConflict,
    TradingStateCommand,
)
from department_agents import router as department_agent_router
from domain_read_models import build_domain_read_model
from governance_client import (
    GOVERNANCE_API_URL,
    GovernanceProxyError,
    governance_request as _governance_request,
)
from operations_read_model import build_operations_snapshot
from portfolio_runtime import RUNTIME
from portfolio_schemas import (
    PortfolioRecommendationStartResponse,
    PortfolioRecommendationStatusResponse,
    PortfolioUniverseListResponse,
)
from portfolio_universe import DEFAULT_UNIVERSE_ID, get_universe, universe_options

try:
    from .qa import router as qa_router
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    from qa import router as qa_router
try:
    from .risk import router as risk_router
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    from risk import router as risk_router
from ui_read_model import build_ui_snapshot

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
app.include_router(ceo_router)
app.include_router(risk_router)
app.include_router(qa_router)


# Browser는 Domain API를 직접 호출하지 않는다. Mandate 변경은 CEO Office가 소유하므로
# 이 BFF가 얇게 전달하고, 정책 검증·Risk/QA/사용자 승인·영속화는 governance-api가 한다.
# Canonical mandate verification is an explicit deployment opt-in. Deterministic
# tests leave this disabled; an empty Governance URL can never be treated as ready.
PORTFOLIO_GOVERNANCE_BINDING_ENABLED = os.getenv(
    "PORTFOLIO_GOVERNANCE_BINDING_ENABLED", "false"
).casefold() in {"1", "true", "yes", "on"}
PORTFOLIO_GOVERNANCE_BINDING_PATH = (
    os.getenv(
        "PORTFOLIO_GOVERNANCE_BINDING_PATH",
        "/governance/v1/mandates/{mandate_id}/current",
    ).strip()
    or "/governance/v1/mandates/{mandate_id}/current"
)
# The deployment must provide a trusted authenticated subject header. Local
# deterministic tests explicitly opt out; missing identity is never accepted in
# the production default.
PORTFOLIO_AUTH_REQUIRED = os.getenv("PORTFOLIO_AUTH_REQUIRED", "true").casefold() in {"1", "true", "yes", "on"}
PORTFOLIO_REQUIRE_MANDATE_BINDING = os.getenv("PORTFOLIO_REQUIRE_MANDATE_BINDING", "true").casefold() in {
    "1",
    "true",
    "yes",
    "on",
}


@app.exception_handler(GovernanceProxyError)
async def _on_governance_proxy_error(request: Request, exc: GovernanceProxyError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.payload)


_PORTFOLIO_BINDING_FIELDS = ("mandate_id", "case_id", "mandate_version_id", "policy_hash")


def _canonical_binding_matches(
    submitted: Mapping[str, object],
    payload: object,
) -> bool:
    """Match every binding field against one explicit Governance response object."""

    if not isinstance(payload, Mapping):
        return False
    candidates: list[Mapping[str, object]] = [payload]
    for key in ("binding", "mandate_binding", "canonical_binding", "data"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            candidates.append(nested)
    for candidate in candidates:
        if any(
            field not in candidate or candidate[field] != submitted.get(field)
            for field in _PORTFOLIO_BINDING_FIELDS
        ):
            continue
        for marker in ("valid", "binding_valid"):
            if marker in candidate and candidate.get(marker) is not True:
                break
        else:
            return True
    return False


async def _verify_portfolio_governance_binding(request: PortfolioRecommendationRequest) -> None:
    if not PORTFOLIO_GOVERNANCE_BINDING_ENABLED:
        return
    binding = {field: getattr(request, field) for field in _PORTFOLIO_BINDING_FIELDS}
    for field, value in binding.items():
        if field != "case_id" and not value:
            raise HTTPException(status_code=422, detail=f"{field}_binding_required")
    if not GOVERNANCE_API_URL:
        raise HTTPException(status_code=503, detail="governance_binding_unavailable")
    try:
        path = PORTFOLIO_GOVERNANCE_BINDING_PATH.replace(
            "{mandate_id}", str(binding["mandate_id"])
        )
        canonical = await _governance_request("GET", path)
    except HTTPException as exc:
        raise HTTPException(status_code=503, detail="governance_binding_unavailable") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="governance_binding_unavailable") from exc
    if not _canonical_binding_matches(binding, canonical):
        raise HTTPException(status_code=409, detail="governance_mandate_binding_mismatch")


def _require_portfolio_owner(owner_id: str | None, expected_user_id: str | None = None) -> None:
    if PORTFOLIO_AUTH_REQUIRED and not owner_id:
        raise HTTPException(status_code=401, detail="portfolio_authentication_required")
    if owner_id and expected_user_id and owner_id != expected_user_id:
        raise HTTPException(status_code=403, detail="portfolio_recommendation_forbidden")


@app.post("/ui/mandates/{mandate_id}/change-requests")
async def ui_submit_mandate_change(mandate_id: str, body: dict[str, object]) -> object:
    return await _governance_request("POST", f"/governance/v1/mandates/{mandate_id}/change-requests", body=body)


@app.get("/ui/mandates/{mandate_id}/current")
async def ui_get_current_mandate(mandate_id: str) -> object:
    return await _governance_request("GET", f"/governance/v1/mandates/{mandate_id}/current")


@app.post("/ui/mandate-cases/{case_id}/advance")
async def ui_advance_mandate_case(case_id: str, body: dict[str, object]) -> object:
    return await _governance_request("POST", f"/governance/v1/cases/{case_id}/advance", body=body)


@app.get("/ui/mandate-cases/{case_id}/timeline")
async def ui_get_mandate_case_timeline(case_id: str) -> object:
    return await _governance_request("GET", f"/governance/v1/cases/{case_id}/timeline")


@app.get("/ui/mandate-approvals")
async def ui_list_mandate_approvals(object_type: str, object_id: str) -> object:
    return await _governance_request(
        "GET",
        "/governance/v1/approvals",
        params={"object_type": object_type, "object_id": object_id},
    )


@app.post("/ui/mandate-approvals/{approval_id}/decide")
async def ui_decide_mandate_approval(approval_id: str, body: dict[str, object]) -> object:
    return await _governance_request("POST", f"/governance/v1/approvals/{approval_id}/decide", body=body)


def _integration_status() -> dict[str, dict[str, object]]:
    """Return configuration presence only; never expose integration secrets."""

    def configured(*names: str) -> bool:
        return all(os.getenv(name, "").strip() for name in names)

    notion_databases = tuple(
        name
        for name in (
            "NOTION_BRIEFING_DB",
            "NOTION_RESEARCH_DB",
            "NOTION_TRADING_DB",
            "NOTION_RISK_DB",
            "NOTION_QUANT_BACKTEST_DB",
            "NOTION_ACCOUNTING_DB",
            "NOTION_QA_DB",
            "NOTION_HR_DB",
        )
        if os.getenv(name, "").strip()
    )

    return {
        "notion": {
            "configured": configured("NOTION_TOKEN", "NOTION_BRIEFING_DB"),
            "label": "Notion 저장",
            "need": "NOTION_TOKEN / NOTION_BRIEFING_DB 미설정",
            "database_count": len(notion_databases),
            "database_scope": "projection_only",
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


@app.get("/ui/portfolio-universes", response_model=PortfolioUniverseListResponse)
def ui_portfolio_universes() -> dict[str, object]:
    """Return backend-owned, read-only universe choices for the interview form."""
    return {
        "default_universe_id": DEFAULT_UNIVERSE_ID,
        "universes": universe_options(),
    }


class PortfolioRecommendationRequest(BaseModel):
    """User suitability inputs; this route never accepts orders or credentials."""

    user_id: str = Field(min_length=1, max_length=128)
    mindset: Literal["SAFETY_FIRST", "BALANCED", "RISK_SEEKING"]
    experience: Literal["BEGINNER", "INTERMEDIATE", "EXPERIENCED"]
    investment_horizon_years: int = Field(ge=1, le=100)
    max_drawdown_pct: str = Field(
        pattern=r"^0(?:\.\d+)?$|^1(?:\.0+)?$",
        description=(
            "허용 가능한 최대 손실률의 비율값입니다. 10%는 '0.10'으로 입력하며 "
            "'10'은 허용하지 않습니다. 범위는 0 초과 1 이하입니다."
        ),
        examples=["0.10"],
    )

    @field_validator("max_drawdown_pct")
    @classmethod
    def validate_max_drawdown_ratio(cls, value: str) -> str:
        try:
            ratio = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("max_drawdown_pct must be a decimal ratio") from exc
        if ratio <= 0 or ratio > 1:
            raise ValueError("max_drawdown_pct must be greater than 0 and at most 1")
        return value
    liquidity_need: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    investment_amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    universe_id: str = Field(default=DEFAULT_UNIVERSE_ID, min_length=1, max_length=128)
    category: str = Field(default="PORTFOLIO_RECOMMENDATION", min_length=1, max_length=64)
    include_stock: bool = Field(default=True, description="주식 자산을 추천 결과에 포함할지 여부")
    include_derivatives: bool = Field(default=False, description="파생상품 자산을 추천 결과에 포함할지 여부(현재 국내 주식 전용 범위에서는 기본 OFF)")
    query: str = Field(default="", max_length=2000)
    max_sector_weight_pct: Decimal | None = Field(default=None, ge=0, le=100, max_digits=6, decimal_places=2)
    max_gross_exposure_pct: Decimal | None = Field(default=None, ge=0, le=500, max_digits=7, decimal_places=2)
    max_daily_loss_pct: Decimal | None = Field(default=None, ge=0, le=100, max_digits=6, decimal_places=2)
    allowed_asset_classes: list[str] = Field(default_factory=list, max_length=32)
    forbidden_asset_classes: list[str] = Field(default_factory=list, max_length=32)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)
    mandate_id: str | None = Field(default=None, min_length=1, max_length=128)
    case_id: str | None = Field(default=None, min_length=1, max_length=128)
    mandate_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    policy_hash: str | None = Field(default=None, min_length=1, max_length=128)
    # Governance binding is mandatory for bound mandate execution paths. A
    # local draft may explicitly request advisory-only analysis; this path
    # never grants approval or mutates orders, positions, or the ledger.
    advisory_only: bool = False
    as_of: str | None = None
    fund_id: str | None = None


@app.post(
    "/ui/portfolio-recommendations",
    status_code=202,
    response_model=PortfolioRecommendationStartResponse,
)
async def start_portfolio_recommendation(
    request: PortfolioRecommendationRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    owner_id: str | None = Header(default=None, alias="X-User-Id"),
) -> dict[str, object]:
    """Start the advisory LangGraph and return a run reference."""
    _require_portfolio_owner(owner_id, request.user_id)

    if get_universe(request.universe_id) is None:
        raise HTTPException(status_code=422, detail="portfolio_universe_not_found")
    if request.advisory_only and (
        request.mandate_version_id or request.policy_hash
    ):
        # An advisory draft may carry a local mandate label, but a canonical
        # version/hash pair must never be accepted without Governance
        # verification. Otherwise advisory_only becomes a binding-check bypass.
        raise HTTPException(
            status_code=422,
            detail="advisory_only_cannot_include_mandate_binding",
        )
    if (
        PORTFOLIO_REQUIRE_MANDATE_BINDING
        and not request.mandate_version_id
        and not request.advisory_only
    ):
        raise HTTPException(status_code=422, detail="mandate_version_binding_required")
    if (
        PORTFOLIO_REQUIRE_MANDATE_BINDING
        and not request.policy_hash
        and not request.advisory_only
    ):
        raise HTTPException(status_code=422, detail="mandate_policy_binding_required")
    if request.mandate_version_id and not request.policy_hash:
        raise HTTPException(status_code=422, detail="mandate_policy_binding_required")
    if request.policy_hash and not request.mandate_version_id:
        raise HTTPException(status_code=422, detail="mandate_version_binding_required")
    if not request.advisory_only:
        await _verify_portfolio_governance_binding(request)
    profile = request.model_dump(exclude_none=True)
    # ``advisory_only`` is a BFF authorization mode, not a LangGraph input.
    # Keep it out of the strict worker profile so downstream schemas cannot
    # mistake the transport flag for investor data.
    profile.pop("advisory_only", None)
    if idempotency_key:
        profile["idempotency_key"] = idempotency_key
    if "as_of" not in profile:
        from datetime import datetime, timezone

        profile["as_of"] = datetime.now(timezone.utc).isoformat()
    try:
        result = RUNTIME.start(profile)
        run = RUNTIME.get(result["run_id"])
        if run is None:
            raise HTTPException(status_code=503, detail="portfolio_recommendation_projection_unavailable")
        return {
            **result,
            "trace_id": run.get("trace_id", ""),
            "case_id": run.get("case_id"),
            "mandate_id": run.get("mandate_id"),
            "mandate_version_id": run.get("mandate_version_id"),
            "policy_hash": run.get("policy_hash"),
            "input_hash": run["input_hash"],
        }
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get(
    "/ui/portfolio-recommendations/{run_id}",
    response_model=PortfolioRecommendationStatusResponse,
)
def portfolio_recommendation_status(
    run_id: str,
    owner_id: str | None = Header(default=None, alias="X-User-Id"),
) -> dict[str, object]:
    run = RUNTIME.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="portfolio_recommendation_run_not_found")
    _require_portfolio_owner(owner_id, str(run.get("profile_user_id", "")))
    return run


class PortfolioRecommendationApprovalRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    comment: str | None = Field(default=None, max_length=500)


@app.post(
    "/ui/portfolio-recommendations/{run_id}/approval",
    response_model=PortfolioRecommendationStatusResponse,
)
def decide_portfolio_recommendation(
    run_id: str,
    request: PortfolioRecommendationApprovalRequest,
    owner_id: str | None = Header(default=None, alias="X-User-Id"),
) -> dict[str, object]:
    """Approve or reject the advisory recommendation, never an order."""

    current = RUNTIME.get(run_id)
    if current is None:
        raise HTTPException(status_code=404, detail="portfolio_recommendation_run_not_found")
    _require_portfolio_owner(owner_id, str(current.get("profile_user_id", "")))
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
              한 번만 계산한다.

              **2026-08-10: 회계 구간은 여기서 빠졌다.** portfolio·ledger는 이제
              Supabase 뷰에서 오고(`_accounting_sections`) 짧은 TTL 캐시를 쓴다 -
              변하는 장부를 프로세스 수명 캐시에 물리면 화면이 조용히 낡은 NAV를
              보여준다는 옛 경고가 그 구간에 해당했다. 여기 남은 것은 트레이딩
              구간뿐이고 그건 입력이 고정된 Scripted Loop라 계속 무기한 캐시다.
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
        # CEO 입구가 어느 런타임에 붙어 있는지. 로컬 시험(cli/docker)과
        # AWS(api/local)를 화면에서 구분할 수 있어야 "열려 있는 줄 알았다"가 없다.
        "ceo_transport": ceo.CEO_TRANSPORT,
        "hermes_exec_mode": hermes_cli.HERMES_EXEC_MODE,
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
@app.get("/health/ready")
def health_ready() -> dict[str, object]:
    """Expose dependency readiness without secrets or claiming operational durability."""

    dependencies = {
        "bff": {"status": "READY"},
        "governance": {"status": "READY" if GOVERNANCE_API_URL else "NOT_CONFIGURED"},
        "supabase": {"status": "READY" if os.getenv("DATABASE_URL", "").strip() else "NOT_CONFIGURED"},
        "ollama": {
            "status": "READY"
            if os.getenv("PORTFOLIO_WORKER_RUNTIME", "deterministic_test") == "deterministic_test"
            or os.getenv("OLLAMA_BASE_URL", "").strip()
            else "NOT_CONFIGURED"
        },
        "pipeline": {"status": "READY" if RUNTIME is not None else "UNAVAILABLE"},
        "runtime_store": {"status": "READY" if RUNTIME.durable else "NOT_CONFIGURED"},
        "mandate_binding": {
            "status": (
                "READY"
                if (PORTFOLIO_REQUIRE_MANDATE_BINDING or PORTFOLIO_GOVERNANCE_BINDING_ENABLED)
                and GOVERNANCE_API_URL
                else "NOT_CONFIGURED"
            ),
            "canonical_verification": (
                "READY"
                if PORTFOLIO_GOVERNANCE_BINDING_ENABLED and GOVERNANCE_API_URL
                else "NOT_CONFIGURED"
            ),
        },
    }
    status = "ready" if all(item["status"] == "READY" for item in dependencies.values()) else "degraded"
    return {"status": status, "dependencies": dependencies, "external_writes": False}


@lru_cache(maxsize=1)
def _repo():
    """회계 원장 저장소. DATABASE_URL이 없으면 None이고 Snapshot은 전부 DEMO다."""
    return db_read_model.LedgerRepository.from_env()


# 브라우저는 Book UUID를 모른다. `/ui/snapshot`을 파라미터 없이 부르고(bffClient.tsx),
# 그게 맞다 - 어느 장부가 Canonical인지는 서버가 아는 사실이지 화면이 고를 일이 아니다.
# 화면이 초 단위로 다시 묻는다. 같은 뷰를 그 주기로 때리면 Supabase가 그 부하를 다 받는다.
UI_SNAPSHOT_CACHE_SECONDS = float(os.getenv("UI_SNAPSHOT_CACHE_SECONDS", "2"))

# ponytail: 프로세스 로컬 dict. book_id -> (읽은 시각, sections). 여러 프로세스로
#           늘어나면 Redis(P0 확정)로 옮긴다. **캐시가 신선도를 속이지 못한다** -
#           화면이 보는 시각은 fetch 시각이 아니라 payload의 `portfolio.as_of`이고
#           그건 평가 시각 그대로다. 캐시는 그 값을 잠깐 재사용할 뿐이다.
_SECTIONS_CACHE: dict[UUID, tuple[float, dict | None]] = {}


def _default_book_id(repo) -> UUID | None:
    """파라미터가 없을 때 쓸 장부. 규칙은 `LedgerRepository.default_book()`이 소유한다.

    **여기서 다시 고르지 않는다** - 마감 스케줄러도 같은 답을 써야 하고, 두 곳이
    각자 고르면 화면과 보고서가 다른 장부를 말한다. 못 고르면 None이고 회계 구간은
    Scripted Loop로 남는다(출처에 그대로 드러난다).
    """
    chosen = repo.default_book()
    return chosen[1] if chosen else None


def _accounting_sections(book_id: UUID | None) -> tuple[UUID, dict | None] | None:
    """(장부, 회계 구간). DB가 없거나 장부를 못 고르면 None - 호출자가 DEMO로 떨어진다.

    안쪽 `sections`가 None인 것은 다른 뜻이다 - **그 장부는 있는데 평가된 적이 없다.**
    둘을 한 값으로 합치면 "DB 없음"과 "NAV 없음"을 호출자가 구분할 수 없다.
    """
    repo = _repo()
    if repo is None:
        return None
    resolved = book_id or _default_book_id(repo)
    if resolved is None:
        return None

    hit = _SECTIONS_CACHE.get(resolved)
    now = time.monotonic()
    if hit is not None and now - hit[0] < UI_SNAPSHOT_CACHE_SECONDS:
        return resolved, hit[1]
    sections = db_read_model.build_accounting_sections(repo, resolved)
    _SECTIONS_CACHE[resolved] = (now, sections)
    return resolved, sections


@app.get("/ui/snapshot")
def ui_snapshot(book_id: UUID | None = None) -> dict:
    """계획 5.2의 `GET /ui/snapshot`. 화면 State는 이 한 장에서 재구축된다.

    DB가 붙어 있으면 **회계 구간(portfolio·ledger)이 Canonical 표에서** 온다
    (`api.portfolio_snapshot_latest` 등). `book_id`는 생략할 수 있고, 그때는
    서버가 Canonical 장부를 고른다(`_default_book_id`). 트레이딩 구간은 아직
    Scripted Loop다 - `execution.orders`가 0행이고 OMS 상태가 프로세스 메모리라
    뷰를 만들어도 빈 화면을 실데이터인 척 보여줄 뿐이다(TRD-01 대기).

    그래서 **구간별 출처를 `sources`에 밝힌다.** 최상위 `mode`는 트레이딩까지
    실데이터가 되기 전에는 DEMO로 둔다 - 절반만 진짜인 화면을 PAPER라고 부르면
    나머지 절반도 진짜라고 읽힌다.

    **명시한 장부와 고른 장부는 실패 방향이 다르다.** `book_id`를 직접 준 요청은
    그 장부의 NAV가 없으면 404다(다른 값을 대신 주지 않는다). 생략한 요청은 아직
    아무것도 평가되지 않은 초기 상태일 수 있으므로 대시보드를 통째로 죽이지 않고
    Scripted Loop로 남되 `sources`가 그 사실을 밝힌다.
    """
    loop = _demo_state()
    overrides = None
    resolved = _accounting_sections(book_id)
    if resolved is not None:
        chosen, sections = resolved
        if sections is None and book_id is not None:
            # 평가된 적 없는 장부다. 0원 NAV를 지어내지 않고 그 사실을 알린다.
            raise HTTPException(404, f"book {book_id}의 확정 Snapshot이 없습니다")
        if sections is not None:
            # 출처는 **실제로 갈아끼운 구간에만** 붙인다. 목록을 손으로 적어두면
            # 뷰가 구간을 하나 더 내놓거나 덜 내놨을 때 화면이 Scripted Loop 값을
            # supabase라고 읽는다.
            overrides = {**sections,
                         "book_id": str(chosen),
                         "sources": {name: "supabase" for name in sections}}

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
    # 구간마다 출처가 반드시 있다. 트레이딩은 아직 Scripted Loop다(TRD-01 대기).
    # 회계 구간은 DB와 Canonical 장부가 있으면 supabase, 없으면 scripted-loop -
    # 어느 쪽이든 **화면이 출처를 모르는 상태로 나가지 않는다**는 게 계약이다.
    assert set(snap["sources"]) == {"portfolio", "trading", "ledger", "treasury"}, \
        snap["sources"]
    assert snap["sources"]["trading"] == "scripted-loop", snap["sources"]
    assert snap["sources"]["portfolio"] == snap["sources"]["ledger"], \
        f"회계 두 구간의 출처가 갈라졌다: {snap['sources']}"
    assert snap["sources"]["portfolio"] in ("supabase", "scripted-loop"), snap["sources"]
    # 결제 사다리. **원장 현금과 가용 현금이 같은 값으로 나간다** - 화면이 현금을
    # 두 군데서 다르게 말하면 어느 쪽으로 주문을 잡을지 알 수 없다.
    assert snap["treasury"]["available_cash"] == snap["portfolio"]["cash"], snap["treasury"]
    assert snap["treasury"]["buckets"], snap["treasury"]

    # 없는 book_id는 404다. 0원 NAV를 지어내지 않는다.
    # (DB가 없으면 book_id가 무시되므로 그때는 200이고, 그 경우도 출처는 전부 DEMO다)
    missing_book = c.get("/ui/snapshot", params={"book_id": str(uuid4())})
    if _repo() is not None:
        assert missing_book.status_code == 404, missing_book.text
    else:
        assert set(missing_book.json()["sources"].values()) == {"scripted-loop"}

    # ── 회계 구간 배선: DB 없이도 검사한다 ────────────────────────────────
    # 실 DB 유무로 검사가 사라지면 CI에서 이 경로는 영원히 안 돌아본다.
    _saved = (_repo, _default_book_id, db_read_model.build_accounting_sections)
    _BOOK = uuid4()
    reads: list[UUID] = []

    def _fake_sections(repo, book_id):
        reads.append(book_id)
        return {"portfolio": {"nav": "12345", "as_of": "2026-08-10T06:00:00+00:00"},
                "ledger": {"balanced": True, "journal_count": 3},
                "treasury": {"available_cash": "12345", "buckets": [],
                             "overdue_count": 0, "overdue": []}}

    globals()["_repo"] = lambda: object()      # DB가 있는 척 - 뷰 호출은 위에서 가로챈다
    globals()["_default_book_id"] = lambda repo: _BOOK
    db_read_model.build_accounting_sections = _fake_sections
    try:
        _SECTIONS_CACHE.clear()
        # 1. book_id 없이 불러도 Canonical 장부의 회계 구간이 실린다
        wired = c.get("/ui/snapshot").json()
        assert wired["portfolio"]["nav"] == "12345", wired["portfolio"]
        assert wired["book_id"] == str(_BOOK), wired["book_id"]
        assert wired["sources"]["portfolio"] == "supabase"
        assert wired["sources"]["ledger"] == "supabase"
        assert wired["sources"]["treasury"] == "supabase"
        # 갈아끼우지 않은 구간의 출처는 그대로 남는다
        assert wired["sources"]["trading"] == "scripted-loop", wired["sources"]
        # mode는 여전히 DEMO다. 트레이딩이 Scripted Loop인 한 절반만 진짜다
        assert wired["mode"] == "DEMO", "절반만 실데이터인 화면이 PAPER로 나갔다"

        # 2. TTL 안에서는 뷰를 다시 읽지 않는다. 화면이 초 단위로 물어도 DB는 한 번이다
        before = len(reads)
        c.get("/ui/snapshot")
        c.get("/ui/snapshot")
        assert len(reads) == before, f"캐시가 안 먹었다 - {len(reads) - before}번 더 읽었다"

        # 3. TTL이 지나면 다시 읽는다. 캐시가 낡은 NAV를 영구히 물고 있으면 안 된다
        _SECTIONS_CACHE[_BOOK] = (time.monotonic() - UI_SNAPSHOT_CACHE_SECONDS - 1,
                                  _SECTIONS_CACHE[_BOOK][1])
        c.get("/ui/snapshot")
        assert len(reads) == before + 1, "TTL이 지났는데 다시 안 읽었다"

        # 4. 장부가 여럿이면 아무거나 고르지 않는다 - 남의 펀드 NAV를 보여주느니 DEMO다
        globals()["_default_book_id"] = lambda repo: None
        _SECTIONS_CACHE.clear()
        ambiguous = c.get("/ui/snapshot").json()
        assert set(ambiguous["sources"].values()) == {"scripted-loop"}, ambiguous["sources"]

        # 5. 평가된 적 없는 장부: 명시하면 404, 생략하면 DEMO로 남는다(대시보드를 안 죽인다)
        globals()["_default_book_id"] = lambda repo: _BOOK
        db_read_model.build_accounting_sections = lambda repo, book_id: None
        _SECTIONS_CACHE.clear()
        assert c.get("/ui/snapshot", params={"book_id": str(_BOOK)}).status_code == 404
        _SECTIONS_CACHE.clear()
        never = c.get("/ui/snapshot").json()
        assert set(never["sources"].values()) == {"scripted-loop"}, never["sources"]
    finally:
        globals()["_repo"], globals()["_default_book_id"] = _saved[0], _saved[1]
        db_read_model.build_accounting_sections = _saved[2]
        _SECTIONS_CACHE.clear()

    # 두 번 불러도 같은 Snapshot이다. Read-only가 상태를 바꾸면 안 된다
    assert c.get("/ui/snapshot").json()["portfolio"]["nav"] == snap["portfolio"]["nav"]

    # 요청마다 Paper Loop를 통째로 다시 돌리지 않는다. 같은 객체를 재사용한다
    assert _demo_state() is _demo_state(), "요청마다 OMS·원장이 재실행된다"
    assert _demo_state.cache_info().currsize == 1
    # 캐시했어도 server_time은 매 요청 갱신된다 - 화면이 신선도를 판단해야 한다
    assert c.get("/ui/snapshot").json()["server_time"] >= snap["server_time"]

    # 인증·Tool Allowlist가 없는 기본 환경에서는 Agent 호출이 전부 닫혀 있다.
    # **Level 라우팅이 이 게이트를 못 뚫는다** - L0(모델 호출 없음)도 여전히 503이다.
    assert c.post("/accounting/agent/ask", json={"query": "NAV?"}).status_code == 503
    assert c.post("/accounting/agent/ask", json={"query": "현재 현금 잔고"}).status_code == 503
    assert c.post("/trading/agent/ask", json={"query": "pending?"}).status_code == 503

    # 게이트를 열면 L0는 모델을 부르지 않고 결정론 원천으로 돌려보낸다(비용 0).
    import accounting as _accounting_router
    import hermes_cli as _hermes_cli

    _saved_flag = _hermes_cli.ENABLE_AGENT_ASK
    _hermes_cli.ENABLE_AGENT_ASK = True
    try:
        cheap = c.post("/accounting/agent/ask", json={"query": "현재 NAV와 현금 잔고"})
        assert cheap.status_code == 200, cheap.text
        body = cheap.json()
        assert body["routing"]["level"] == "L0" and body["routing"]["calls_model"] is False
        assert body["session_id"] is None and body["authoritative"] is False
        assert body["source_of_record"] == "/ui/snapshot"
        # 수치를 지어내지 않는다 - 어디서 읽으라는 안내만 한다
        assert "모델을 호출하지 않았습니다" in body["answer"]

        # 마감·감사 질의는 등급이 올라가고 실제로 Hermes를 부른다(여기선 스텁으로 확인)
        _called: list = []
        _orig_ask = _accounting_router.hermes_cli.ask

        def _fake_ask(*, department, config, query):
            _called.append(query)
            return {"department": department, "answer": "stub", "session_id": "s1",
                    "authoritative": False, "source_of_record": "/ui/snapshot"}

        _accounting_router.hermes_cli.ask = _fake_ask
        try:
            heavy = c.post("/accounting/agent/ask",
                           json={"query": "마감 확정해도 되는지 감사 근거와 함께 설명"}).json()
            assert heavy["routing"]["level"] == "L3" and heavy["routing"]["tier"] == "heavy"
            assert heavy["routing"]["calls_model"] is True and len(_called) == 1
            assert heavy["authoritative"] is False, "라우팅이 공식 수치 계약을 깼다"
        finally:
            _accounting_router.hermes_cli.ask = _orig_ask
    finally:
        _hermes_cli.ENABLE_AGENT_ASK = _saved_flag
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
        # 2026-08-04 포트폴리오 추천 경로. 전부 읽기 또는 추천 실행이며 Posting·NAV 확정·
        # 주문 제출이 아니다. 승인 경로(/approval)는 추천 상태만 바꾸고 원장을 건드리지 않는다.
        "/ui/integrations", "/ui/portfolio-universes", "/ui/portfolio-recommendations",
        "/ui/portfolio-recommendations/{run_id}",
        "/ui/portfolio-recommendations/{run_id}/approval",
        "/ui/mandates/{mandate_id}/change-requests",
        "/ui/mandates/{mandate_id}/current",
        "/ui/risk/mandates/{mandate_id}/assess",
        "/ui/mandate-cases/{case_id}/advance",
        "/ui/mandate-cases/{case_id}/timeline",
        "/ui/mandate-approvals",
        "/ui/mandate-approvals/{approval_id}/decide",
    }, c.get("/openapi.json").json()["paths"].keys()

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

    print("ok - BFF 8개 영역 점검 통과 (회계 구간 Supabase 배선 + TTL 캐시 포함)")
