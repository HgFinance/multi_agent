#!/usr/bin/env python3
"""AI Office BFF (FastAPI). 도현 담당분 - Read Model 제공 + Hermes Agent 연결.

근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 5.2(연결 순서), 6(명령 경계)
      docs/02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md 4절 `apps/api/`

이 파일은 조립만 한다. 부서별 Agent 경로는 각 Router 파일이 소유한다
(`accounting.py`, `trading.py`, `department_agents.py`). 프로세스는 하나다 - 부서별로 프로세스를 쪼개는
부서별 분리는 Service Identity 경계가 필요할 때 별도 작업으로 한다. 브라우저
사용자 로그인·세션은 이 로컬 모의투자 범위에 포함하지 않는다.

경계 두 개를 코드로 강제한다.

1. **금융 상태는 Read-only다.** 이 서비스에는 주문 제출·분개 Posting·상태 변경 경로가 없다.
   계획 6절의 위험 Command(SET_TRADING_STATE 등)는 승인·Audit 경계가 갖춰지기 전까지
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
import json
import os
import sys
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from urllib.request import Request as UrlRequest
from urllib.request import urlopen
from uuid import UUID

import anyio
from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

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
from apps.api import hermes_boundary

try:
    from .ceo import router as ceo_router
    from .ceo_mirror_api import router as ceo_mirror_router
    from .conditional_rules import router as conditional_rules_router
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    from ceo import router as ceo_router
    from ceo_mirror_api import router as ceo_mirror_router
    from conditional_rules import router as conditional_rules_router
import trading
from account_snapshot import router as account_snapshot_router
from agent_status import agent_status_snapshot
from command_service import (
    COMMAND_SERVICE,
    CommandVersionConflict,
    IdempotencyConflict,
    TradingStateCommand,
)

try:
    # Keep the dependency object identical to the package-relative imports in
    # ceo/ceo_mirror_api.  The fallback is only for direct script execution.
    from .current_user import (
        active_user_profile,
        auth_mode,
        authorized_fund_memberships,
        authorized_trading_books,
        current_user,
        FIXED_DEMO_USER_ID,
        require_any_fund_membership,
        require_fund_membership,
        require_owner,
    )
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py``
    from current_user import (
        active_user_profile,
        auth_mode,
        authorized_fund_memberships,
        authorized_trading_books,
        current_user,
        FIXED_DEMO_USER_ID,
        require_any_fund_membership,
        require_fund_membership,
        require_owner,
    )
from department_agents import router as department_agent_router
from discord_ingress_auth import (
    DISCORD_INGRESS_PATH,
)
from discord_ingress_auth import (
    bearer_is_authorized as discord_ingress_bearer_is_authorized,
)
from discord_ingress_auth import (
    mark_request as mark_discord_ingress_request,
)
from discord_read import router as discord_read_router
from domain_read_models import build_domain_read_model
from governance_client import (
    GOVERNANCE_API_URL,
    GovernanceProxyError,
    GovernanceTransportError,
)
from governance_client import (
    governance_request as _governance_request,
)
from ls_account_stream import router as portfolio_live_router
from operations_read_model import build_operations_snapshot
from portfolio_profile_client import (
    PortfolioProxyError,
)
from portfolio_profile_client import (
    portfolio_request as _portfolio_request,
)
from portfolio_runtime import RUNTIME
from portfolio_schemas import (
    PortfolioRecommendationStartResponse,
    PortfolioRecommendationStatusResponse,
    PortfolioUniverseListResponse,
)
from portfolio_universe import DEFAULT_UNIVERSE_ID, get_universe, universe_options
from strategy_runtime_client import (
    StrategyRuntimeProxyError,
    strategy_runtime_request,
)

try:
    from .qa import QA_API_URL
    from .qa import router as qa_router
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    from qa import QA_API_URL
    from qa import router as qa_router
try:
    from .risk import (
        RISK_API_URL,
        activate_mandate_limits,
        validate_proposed_mandate_limits,
    )
    from .risk import router as risk_router
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    from risk import (
        RISK_API_URL,
        activate_mandate_limits,
        validate_proposed_mandate_limits,
    )
    from risk import router as risk_router
try:
    from .user_orders import router as user_orders_router
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    from user_orders import router as user_orders_router
try:
    from .workforce import WORKFORCE_API_URL
    from .workforce import router as workforce_router
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    from workforce import WORKFORCE_API_URL
    from workforce import router as workforce_router
from ui_read_model import build_ui_snapshot

app = FastAPI(
    title="AI Office BFF",
    version="0.3.0",
    description=(
        "HgFinance Frontend용 BFF. Hermes 부서 Agent와 CEO Kanban 워크플로를"
        " 프론트엔드가 쓸 수 있는 형태로 정규화한다.\n\n"
        "**계약 두 개**\n\n"
        "1. Agent 텍스트는 공식 수치가 아니다(`binding: false`). 공식 Position·PnL·"
        "NAV는 `/ui/snapshot`에서만 나온다.\n"
        "2. CEO 워크플로는 비동기다. `POST /ui/ceo/ask`는 202로 Task ID만 주고,"
        " 진행은 `GET /ui/ceo/tasks/{task_id}` polling(2~5초), 결과는"
        " `GET /ui/ceo/tasks/{task_id}/result`로 가져간다.\n\n"
        "Swagger UI: `/docs` · ReDoc: `/redoc` · 스키마: `/openapi.json`"
    ),
    openapi_tags=[
        {
            "name": "ceo-office",
            "description": (
                "CEO Kanban 워크플로. ask -> 선택 부서 실행 -> CEO 종합 + 비동기 QA 평가.\n\n"
                "`DELETE`는 의도적으로 없다 - 누가 언제 무엇을 요청했고 어느 부서가"
                " 실패했는지는 감사 추적이므로 정리는 Archive로만 한다."
            ),
        },
    ],
)
_LOCAL_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:3002",
    "http://localhost:3003",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    "http://127.0.0.1:3002",
    "http://127.0.0.1:3003",
    "http://127.0.0.1:5173",
)


def _portfolio_cors_origins() -> list[str]:
    """Return an exact origin allowlist; an empty production list denies all."""

    runtime_environment = os.getenv("APP_ENV", "production").strip().casefold()
    raw_allowlist = os.getenv("PORTFOLIO_CORS_ALLOW_ORIGINS", "")
    raw_origins = raw_allowlist.split(",") if raw_allowlist.strip() else []
    # The whole setting may be empty for a backend-only deployment.  Once any
    # origin is supplied, however, empty comma-separated entries are malformed
    # rather than silently broadening or weakening the operator's intent.
    if any(not item.strip() for item in raw_origins):
        raise RuntimeError("invalid PORTFOLIO_CORS_ALLOW_ORIGINS entry")
    # CORS remains an exact origin allowlist in every environment.  Operators
    # may explicitly allow an HTTP frontend while production is being run on a
    # private or local network; no implicit HTTP origins are added.
    allowed_schemes = {"http", "https"}
    configured: list[str] = []
    for item in raw_origins:
        origin = item.strip()
        try:
            parsed = urlsplit(origin)
            _ = parsed.port
        except ValueError as exc:
            raise RuntimeError(
                "invalid PORTFOLIO_CORS_ALLOW_ORIGINS entry"
            ) from exc
        if (
            "*" in origin
            or "\\" in origin
            or any(ord(character) < 33 or ord(character) > 126 for character in origin)
            or parsed.scheme.casefold() not in allowed_schemes
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "%" in parsed.netloc
            or parsed.netloc.endswith(":")
        ):
            raise RuntimeError("invalid PORTFOLIO_CORS_ALLOW_ORIGINS entry")
        configured.append(origin.rstrip("/"))
    origins = (
        [*_LOCAL_CORS_ORIGINS, *configured]
        if runtime_environment in {"local", "test"}
        else configured
    )
    return list(dict.fromkeys(origins))


@app.middleware("http")
async def _mark_private_discord_ingress(request: Request, call_next):
    """Mark only the authenticated Discord service hop for its route."""

    if (
        request.method == "POST"
        and request.url.path == DISCORD_INGRESS_PATH
        and discord_ingress_bearer_is_authorized(
            request.headers.get("authorization")
        )
    ):
        mark_discord_ingress_request(request)
    return await call_next(request)


# 브라우저와 uvicorn 어느 쪽에도 요청 타임아웃이 없다. uvicorn 은 핸들러에
# 데드라인을 걸지 않고, 이 EC2 배포에는 앞단 리버스 프록시가 없다(compose 가
# 8001->8000 을 그대로 노출한다). 그래서 핸들러가 한 번 멈추면 그 요청은 응답도
# 오류도 없이 영원히 pending 된다 - AWS 에서 관측된 증상이 정확히 이것이다.
# 예전 Elastic Beanstalk 배포(deploy/eb)에서는 앞단 nginx/ALB 가 60초에 504 를
# 냈기 때문에 같은 hang 이 "타임아웃"으로 보였다.
#
# 이건 근본 원인 수정이 아니라 안전망이다 - 멈춘 것이 스레드풀에서 도는 동기
# 핸들러면 취소해도 그 스레드는 회수되지 않는다. 504 가 보이기 시작하면 무엇이
# 멈췄는지 반드시 확인해야 한다.
#
# BaseHTTPMiddleware(`@app.middleware("http")`)가 아니라 순수 ASGI middleware 인
# 이유: BaseHTTPMiddleware 는 `call_next` 를 자체 task group 에서 돌리기 때문에
# 거기에 `asyncio.wait_for` 를 씌우면 취소가 그 task group 과 얽혀 정상 예외까지
# ExceptionGroup 으로 뭉개진다(실제로 tests/api 가 그렇게 깨졌다).
def _request_timeout_seconds() -> float:
    try:
        value = float(os.getenv("BFF_REQUEST_TIMEOUT_SECONDS", "30"))
    except ValueError:
        return 30.0
    return value if value > 0 else 30.0


# `POST /ui/ceo/ask`는 `ceo_mirror.execute_once()`가 첫 요청을 동기로
# 끝까지 실행한 뒤에야 응답한다(202는 "같은 request_id로 이미 처리 중인
# 다른 호출"에만 붙는 재시도 안내이지, 원 호출이 빨리 돌아온다는 뜻이 아니다).
# 실행 안에는 Trading Hermes 라우팅 등 LLM이 낀 다단계 처리가 들어 있어
# 다른 단순 조회 경로보다 정상적으로도 오래 걸린다. 그래서 이 경로만 기본
# 타임아웃보다 넉넉한 예산을 준다 - 나머지 경로는 "멈추면 바로 드러나야
# 한다"는 안전망 성격을 그대로 유지한다(위 주석 참고).
def _ceo_ask_timeout_seconds() -> float:
    try:
        value = float(os.getenv("BFF_CEO_ASK_TIMEOUT_SECONDS", "60"))
    except ValueError:
        return 60.0
    return value if value > 0 else 60.0


_CEO_ASK_PATH_SUFFIXES = ("/ui/ceo/ask",)

# SSE 는 설계상 응답을 열어 둔 채 오래 산다. 데드라인을 걸면 정상 스트림을 끊게
# 되므로 스트리밍 경로는 제외한다(스트림 수명은 핸들러 안의
# `UI_MIRROR_SSE_SECONDS` 가 이미 유한하게 제한한다).
_STREAMING_PATH_SUFFIXES = ("/events/stream",)


class RequestDeadlineMiddleware:
    """Turn an indefinitely stalled handler into an explicit 504."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] != "http" or path.endswith(_STREAMING_PATH_SUFFIXES):
            await self.app(scope, receive, send)
            return

        deadline_seconds = (
            _ceo_ask_timeout_seconds()
            if path.endswith(_CEO_ASK_PATH_SUFFIXES)
            else _request_timeout_seconds()
        )

        started = False

        async def send_wrapper(message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            with anyio.fail_after(deadline_seconds):
                await self.app(scope, receive, send_wrapper)
        except TimeoutError:
            # 이미 응답 헤더가 나갔으면 두 번째 응답을 보낼 수 없다. 그때는
            # 연결을 그대로 끊어 클라이언트가 불완전한 본문을 완성본으로
            # 오해하지 않게 한다.
            if started:
                raise
            response = JSONResponse(
                status_code=504,
                content={
                    "error_code": "BFF_REQUEST_TIMEOUT",
                    "detail": "portfolio_bff_request_timeout",
                },
            )
            await response(scope, receive, send)


app.add_middleware(RequestDeadlineMiddleware)


# CORS 는 **가장 바깥** middleware 여야 한다. Starlette 는 나중에 등록한
# middleware 를 바깥에 놓으므로, 이 호출이 위의 두 middleware 보다 뒤에 있어야
# 그 middleware 들이 만든 응답(위의 504 포함)에도 CORS 헤더가 붙는다. 안쪽에
# 두면 브라우저는 504 대신 정체불명의 CORS 오류를 보게 된다.
# CORS is independent from caller identity. Individual routes retain their
# domain-level Fund/Book checks without a global JWT/Bearer gate.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_portfolio_cors_origins(),
    # Credentials stay disabled, so wildcard+credentials cannot be introduced.
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=[
        "Accept",
        "Content-Type",
        "Last-Event-ID",
        "Idempotency-Key",
        "X-Request-Id",
        "X-User-Id",
    ],
)


# 각 투자 본부의 Router는 해당 Hermes Profile을 명시적으로 소유한다. CEO·HR은
# 투자 본부 Agent ask 경로에 섞지 않는다(마스터플랜 5.6).
app.include_router(accounting.router)
app.include_router(trading.router)
app.include_router(department_agent_router)
# `ceo_mirror_router`는 `POST /ui/ceo/ask`의 유일한 소유자다(dedup + Web/Discord
# 공용 event journal). `ceo.py`는 이제 그 경로를 스스로 등록하지 않고 순수 함수
# `ceo_query`만 제공한다 - mirror가 그 함수를 그대로 감싸므로 두 라우터가 같은
# 경로를 나눠 갖고 등록 순서로 승부하는 상태 자체가 존재하지 않는다(2026-08-14
# 사고: 그 상태에서 mirror가 파라미터를 독자적으로 재구성하다 fund_id가 유실돼
# Mandate 스냅샷이 항상 빠졌다). `ceo_router`는 PR #224 read route(`/tasks/*`)만
# 계속 제공한다. `tests/api/test_main_routes.py`가 이 앱에 같은 (path, method)
# 조합이 중복 등록되면 실패하도록 고정한다.
app.include_router(ceo_mirror_router)
app.include_router(ceo_router)
# 사실 조회는 에이전트를 거치지 않는다. "내 잔고"에 CEO 라우팅 + 부서 5곳을
# 태우면 4분이 걸리고 답도 못 낸다(2026-08-11 실측) - 결정론 조회는 직행이다.
app.include_router(account_snapshot_router)
# Discord 대화 원문 읽기. 봇 토큰이 브라우저에 내려가면 발송 권한까지 같이
# 나가므로 토큰은 이 프로세스에만 둔다.
app.include_router(discord_read_router)
# 브로커 계좌 실시간(계좌등록 → 주문상태·체결 → 잔고 확인). 브로커 푸시라
# 우리 원장이 아니다 — 응답의 `authoritative: false`가 그 경계다.
app.include_router(portfolio_live_router)
app.include_router(risk_router)
app.include_router(qa_router)
app.include_router(user_orders_router)
app.include_router(conditional_rules_router)
# HR이 6개 투자본부 Worker의 유휴 상태(ACTIVE/IDLE/UNOBSERVED/UNAVAILABLE)를 읽는
# 얇은 프록시. 판정 로직은 workforce-api(departments/07-agent-workforce)가 갖는다.
app.include_router(workforce_router)


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
# Local mock runs use one fixed demo identity and have no browser login switch.
PORTFOLIO_REQUIRE_MANDATE_BINDING = os.getenv("PORTFOLIO_REQUIRE_MANDATE_BINDING", "true").casefold() in {
    "1",
    "true",
    "yes",
    "on",
}


@app.exception_handler(GovernanceProxyError)
async def _on_governance_proxy_error(request: Request, exc: GovernanceProxyError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.payload)


@app.exception_handler(GovernanceTransportError)
async def _on_governance_transport_error(request: Request, exc: GovernanceTransportError) -> JSONResponse:
    """Return a safe, structured BFF transport failure to browser clients."""
    return JSONResponse(status_code=exc.status_code, content=exc.payload)


@app.exception_handler(PortfolioProxyError)
async def _on_portfolio_proxy_error(request: Request, exc: PortfolioProxyError) -> JSONResponse:
    """accounting-api 오류 본문을 접지 않고 그대로 넘긴다(governance와 같은 이유)."""
    return JSONResponse(status_code=exc.status_code, content=exc.payload)


@app.exception_handler(StrategyRuntimeProxyError)
async def _on_strategy_runtime_proxy_error(request: Request, exc: StrategyRuntimeProxyError) -> JSONResponse:
    """strategy-runtime-control sidecar 오류 본문을 접지 않고 그대로 넘긴다."""
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
    """소유권 판정을 `current_user.require_owner`에 위임한다.

    판정을 라우트마다 흩어놓지 않는 이유는 `apps/api/current_user.py` 머리말에
    적어뒀다 - 요약하면 `X-User-Id`는 로그인 세션이 아니라 고정 fixture 식별자다.
    이 래퍼는 기존 호출부(3곳)를 그대로 두기 위해 남긴 얇은 껍데기다.
    """

    require_owner(owner_id, expected_user_id, required=False)


_CALLER_IDENTITY_BODY_FIELDS = frozenset(
    {
        "actor_user_id",
        "approved_by",
        "created_by",
        "owner_user_id",
        "requested_by",
        "updated_by",
        "user_id",
        "version_created_by",
    }
)


def _identity_bound_body(
    body: Mapping[str, object],
    owner_id: str | None,
    *,
    inject: tuple[str, ...] = (),
) -> dict[str, object]:
    """Reject mismatched fields and pin them to the local fixture identity."""

    bound = dict(body)
    if owner_id is None:
        return bound
    for field in _CALLER_IDENTITY_BODY_FIELDS:
        value = bound.get(field)
        if value is not None and str(value).strip() and str(value).strip() != owner_id:
            raise HTTPException(status_code=403, detail="portfolio_identity_body_mismatch")
    for field in inject:
        bound[field] = owner_id
    return bound


async def _require_fund_access(owner_id: str | None, fund_id: object) -> None:
    await run_in_threadpool(
        require_fund_membership,
        owner_id,
        str(fund_id).strip() if fund_id is not None else None,
    )


def _canonical_fund_ids(payload: object) -> set[str]:
    """Collect explicit canonical fund bindings from a trusted service response."""

    fund_ids: set[str] = set()
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if key == "fund_id" and value is not None and str(value).strip():
                fund_ids.add(str(value).strip())
            elif isinstance(value, (Mapping, list, tuple)):
                fund_ids.update(_canonical_fund_ids(value))
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            fund_ids.update(_canonical_fund_ids(value))
    return fund_ids


async def _require_canonical_fund_access(
    owner_id: str | None,
    payload: object,
    *,
    submitted_fund_id: object | None = None,
) -> str | None:
    """Authorize an opaque resource by its trusted canonical fund binding."""

    if auth_mode() == "fixture":
        return None
    fund_ids = _canonical_fund_ids(payload)
    if not fund_ids:
        raise HTTPException(
            status_code=503, detail="portfolio_canonical_fund_binding_unavailable"
        )
    if len(fund_ids) != 1:
        raise HTTPException(
            status_code=409, detail="portfolio_canonical_fund_binding_ambiguous"
        )
    canonical_fund_id = next(iter(fund_ids))
    if (
        submitted_fund_id is not None
        and str(submitted_fund_id).strip() != canonical_fund_id
    ):
        raise HTTPException(
            status_code=409, detail="portfolio_canonical_fund_binding_mismatch"
        )
    await _require_fund_access(owner_id, canonical_fund_id)
    return canonical_fund_id


async def _canonical_mandate(mandate_id: str) -> object:
    return await _governance_request(
        "GET", f"/governance/v1/mandates/{mandate_id}/current"
    )


async def _authorized_mandate(
    mandate_id: str, owner_id: str | None
) -> object:
    payload = await _canonical_mandate(mandate_id)
    await _require_canonical_fund_access(owner_id, payload)
    return payload


async def _canonical_case(case_id: str) -> object:
    return await _governance_request(
        "GET", f"/governance/v1/cases/{case_id}"
    )


async def _authorized_case(case_id: str, owner_id: str | None) -> object:
    payload = await _canonical_case(case_id)
    await _require_canonical_fund_access(owner_id, payload)
    return payload


async def _authorized_approval(
    approval_id: str, owner_id: str | None
) -> object:
    payload = await _governance_request(
        "GET", f"/governance/v1/approvals/{approval_id}"
    )
    await _require_canonical_fund_access(owner_id, payload)
    return payload


@app.post("/ui/mandates/{mandate_id}/change-requests")
async def ui_submit_mandate_change(
    mandate_id: str,
    body: dict[str, object],
    owner_id: str | None = Depends(current_user),
) -> object:
    bound = _identity_bound_body(body, owner_id, inject=("created_by",))
    canonical = await _canonical_mandate(mandate_id)
    await _require_canonical_fund_access(
        owner_id, canonical, submitted_fund_id=bound.get("fund_id")
    )
    return await _governance_request("POST", f"/governance/v1/mandates/{mandate_id}/change-requests", body=bound)


@app.get("/ui/mandates/{mandate_id}/current")
async def ui_get_current_mandate(
    mandate_id: str,
    owner_id: str | None = Depends(current_user),
) -> object:
    return await _authorized_mandate(mandate_id, owner_id)


@app.post("/ui/mandate-cases/{case_id}/advance")
async def ui_advance_mandate_case(
    case_id: str,
    body: dict[str, object],
    owner_id: str | None = Depends(current_user),
) -> object:
    bound = _identity_bound_body(body, owner_id)
    canonical = await _canonical_case(case_id)
    await _require_canonical_fund_access(
        owner_id, canonical, submitted_fund_id=bound.get("fund_id")
    )
    return await _governance_request("POST", f"/governance/v1/cases/{case_id}/advance", body=bound)


@app.get("/ui/mandate-cases/{case_id}/timeline")
async def ui_get_mandate_case_timeline(
    case_id: str,
    owner_id: str | None = Depends(current_user),
) -> object:
    await _authorized_case(case_id, owner_id)
    return await _governance_request("GET", f"/governance/v1/cases/{case_id}/timeline")


# ── 온보딩 경로 (USER_INPUT_API_SPEC.md 6.1 #2) ────────────────────────────────
# governance-api·accounting-api에 구현은 있었지만 BFF에 안 뚫려 있어서 프론트가
# 호출할 수 없던 5개다(AI_OFFICE_FRONTEND_PLAN §6: Browser는 Domain API를 직접
# 부르지 않는다). 여기서 하는 일은 전달뿐이고 정책 검증·version 할당·
# effective_risk_band 계산은 전부 상류 도메인이 소유한다.


@app.post("/ui/mandates", status_code=201)
async def ui_create_mandate(
    body: dict[str, object],
    owner_id: str | None = Depends(current_user),
) -> object:
    """Mandate 부모 행 생성. 온보딩의 시작점이다(2026-08-12 신설).

    그 전까지 `governance.mandates` INSERT 경로가 API로 없어서 첫 사용자는
    Mandate를 만들 수 없었다 - Version 제안 경로는 전부 `mandate_id`를 받는다.
    """

    bound = _identity_bound_body(body, owner_id, inject=("owner_user_id",))
    await _require_fund_access(owner_id, bound.get("fund_id"))
    return await _governance_request("POST", "/governance/v1/mandates", body=bound)


@app.put("/ui/mandates/{mandate_id}")
async def ui_replace_mandate(
    mandate_id: str,
    body: dict[str, object],
    owner_id: str | None = Depends(current_user),
) -> object:
    """Replace the current Mandate metadata; no version row is created."""

    bound = _identity_bound_body(body, owner_id, inject=("created_by",))
    canonical = await _canonical_mandate(mandate_id)
    await _require_canonical_fund_access(
        owner_id, canonical, submitted_fund_id=bound.get("fund_id")
    )
    return await _governance_request(
        "PUT", f"/governance/v1/mandates/{mandate_id}", body=bound
    )


@app.get("/ui/mandates/by-fund/{fund_id}/current")
async def ui_get_current_mandate_by_fund(
    fund_id: str,
    owner_id: str | None = Depends(current_user),
) -> object:
    """Fund 하나의 현재 Mandate. 화면이 `mandate_id`를 손으로 받지 않게 한다.

    상류가 모호하면(한 Fund에 Mandate 2개 이상) 409를 그대로 통과시킨다 -
    임의로 하나를 고르지 않는다(USER_INPUT_API_SPEC 2.1).
    """

    await _require_fund_access(owner_id, fund_id)
    params = {"owner_user_id": owner_id} if owner_id else None
    return await _governance_request(
        "GET",
        f"/governance/v1/mandates/by-fund/{fund_id}/current",
        params=params,
    )


@app.post("/ui/mandates/{mandate_id}/versions")
async def ui_propose_mandate_version(
    mandate_id: str,
    body: dict[str, object],
    owner_id: str | None = Depends(current_user),
) -> object:
    """정책 Version 제안. 저장은 여기가 아니라 활성화 단계에서 확정된다."""

    bound = _identity_bound_body(body, owner_id, inject=("created_by",))
    risk_profile = bound.pop("risk_profile", None)
    if not isinstance(risk_profile, dict):
        raise HTTPException(status_code=422, detail="risk_profile_binding_required")
    canonical = await _canonical_mandate(mandate_id)
    canonical_fund_id = await _require_canonical_fund_access(
        owner_id, canonical, submitted_fund_id=bound.get("fund_id")
    )
    proposed_policy = bound.get("policy")
    if not isinstance(proposed_policy, dict):
        raise HTTPException(status_code=422, detail="mandate_policy_required")
    await validate_proposed_mandate_limits(
        {
            "mindset": risk_profile.get("mindset"),
            "experience": risk_profile.get("experience"),
            "preset_version": risk_profile.get("preset_version"),
            "risk_bounds": proposed_policy.get("risk_bounds"),
        }
    )
    result = await _governance_request(
        "POST", f"/governance/v1/mandates/{mandate_id}/versions", body=bound
    )
    if not isinstance(result, dict) or not result.get("activated"):
        return result
    mandate_version_id = result.get("mandate_version_id")
    if not mandate_version_id:
        raise HTTPException(
            status_code=503, detail="canonical_mandate_version_binding_unavailable"
        )
    policy = proposed_policy
    await activate_mandate_limits(
        {
            "fund_id": canonical_fund_id,
            "mandate_id": mandate_id,
            "mandate_version_id": mandate_version_id,
            "mandate_version": result.get("version"),
            "mindset": risk_profile.get("mindset"),
            "experience": risk_profile.get("experience"),
            "preset_version": risk_profile.get("preset_version"),
            "risk_bounds": policy.get("risk_bounds"),
            "universe_policy": policy.get("universe_policy"),
            "allowed_assets": policy.get("allowed_assets", []),
            "effective_from": bound.get("effective_from"),
            "trace_id": result.get("activation_trace_id") or str(mandate_version_id),
        }
    )
    return {**result, "risk_limits_activated": True}


@app.post("/ui/mandate-assistant/suggest")
async def ui_mandate_assistant_suggest(
    body: dict[str, object],
    owner_id: str | None = Depends(current_user),
) -> object:
    """온보딩 챗봇 제안. **Stateless이며 아무것도 저장하지 않는다.**

    응답의 `requires_user_confirmation`은 항상 `true`다(USER_INPUT_API_SPEC 2.4
    불변식 1) - 이 경로만으로 확정되는 값은 없고, 저장은 §2.2(`versions`)·
    §2.3(`investor-profiles`) 경로로만 일어난다. allow-list 밖 필드는
    `dropped_fields`에 남고 조용히 사라지지 않는다.
    """

    bound = _identity_bound_body(body, owner_id)
    await _require_fund_access(owner_id, bound.get("fund_id"))
    return await _governance_request(
        "POST", "/governance/v1/mandate-assistant/suggest", body=bound
    )


@app.post("/ui/investor-profiles", status_code=201)
async def ui_create_investor_profile(
    body: dict[str, object],
    owner_id: str | None = Depends(current_user),
) -> object:
    """적합성 프로필 저장(항상 새 version). 성향·경험은 화면이 받은 값 그대로다.

    요청자와 바디의 `user_id`가 다르면 403이다 - 남의 프로필을 쓰지 못하게 한다.
    """

    bound = _identity_bound_body(body, owner_id, inject=("user_id",))
    _require_portfolio_owner(owner_id, str(bound.get("user_id") or ""))
    await _require_fund_access(owner_id, bound.get("fund_id"))
    return await _portfolio_request("POST", "/portfolio/v1/investor-profiles", body=bound)


@app.get("/ui/investor-profiles/current")
async def ui_get_current_investor_profile(
    user_id: str,
    fund_id: str,
    owner_id: str | None = Depends(current_user),
) -> object:
    """현재 version 하나. 없으면 상류 404를 그대로 통과시킨다."""

    _require_portfolio_owner(owner_id, user_id)
    await _require_fund_access(owner_id, fund_id)
    return await _portfolio_request(
        "GET",
        "/portfolio/v1/investor-profiles/current",
        params={"user_id": user_id, "fund_id": fund_id},
    )


@app.get("/ui/mandate-approvals")
async def ui_list_mandate_approvals(
    object_type: str,
    object_id: str,
    owner_id: str | None = Depends(current_user),
) -> object:
    payload = await _governance_request(
        "GET",
        "/governance/v1/approvals",
        params={"object_type": object_type, "object_id": object_id},
    )
    await _require_canonical_fund_access(owner_id, payload)
    return payload


@app.post("/ui/mandate-approvals/{approval_id}/decide")
async def ui_decide_mandate_approval(
    approval_id: str,
    body: dict[str, object],
    owner_id: str | None = Depends(current_user),
) -> object:
    bound = _identity_bound_body(body, owner_id, inject=("approved_by",))
    await _authorized_approval(approval_id, owner_id)
    return await _governance_request("POST", f"/governance/v1/approvals/{approval_id}/decide", body=bound)


# ── 페이퍼 전략 컨테이너(mlpipe-paper) ──────────────────────────────────────────
# 실제 docker 조작·파일 읽기는 이 프로세스가 아니라 `strategy-runtime-control`
# sidecar가 한다(`strategy_runtime_client.py` 머리말) - `portfolio-bff`는 이
# 저장소에서 유일하게 외부에 노출되는 서비스라 docker 소켓을 직접 쥐지 않는다.


@app.get("/ui/strategy-runtime/spike-fade")
async def ui_strategy_runtime_snapshot(
    owner_id: str | None = Depends(current_user),
) -> object:
    """채택된 페이퍼 전략 1개의 실시간 상태 - 읽기 전용.

    Fund별로 나뉘지 않는다 - 지금 떠 있는 전략 프로세스가 이 배포 전체에 하나뿐이고
    `strategy.signals` 밖에서는 Fund와 묶는 컬럼 자체가 없다(스키마 조사 결과).
    """
    return await strategy_runtime_request("GET", "/snapshot")


class StrategyPowerRequest(BaseModel):
    action: Literal["start", "stop"]


@app.post("/ui/strategy-runtime/spike-fade/power")
async def ui_strategy_runtime_power(
    body: StrategyPowerRequest,
    owner_id: str | None = Depends(current_user),
) -> object:
    """`strategy-spike-fade` 컨테이너 하나만 시작/정지한다(sidecar에 위임).

    기본은 꺼져 있다(sidecar의 `ENABLE_STRATEGY_CONTAINER_CONTROL`). 컨테이너를
    새로 만들거나 세션 날짜를 바꾸지 않으므로 되돌리기 쉽지만, 그래도 누가
    눌렀는지는 최소한 남긴다.
    """
    result = await strategy_runtime_request("POST", "/power", body={"action": body.action})
    # 정식 감사 원장에 넣을 사건은 아니라 stdout 기록으로 최소화한다 - 그래도
    # "누가 눌렀는지 전혀 안 남는다"보다는 낫다.
    print(f"[strategy-runtime] owner={owner_id or 'unknown'} action={body.action}")
    return result


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

    discord_bot_mirror = (
        os.getenv("DISCORD_MIRROR_ENABLED", "").strip().casefold()
        in {"1", "true", "yes", "on"}
        and configured("DISCORD_BOT_TOKEN_CEO", "DISCORD_CEO_CHANNEL_ID")
    )
    discord_webhook = configured("DISCORD_WEBHOOK_URL")

    return {
        "notion": {
            "configured": configured("NOTION_TOKEN", "NOTION_BRIEFING_DB"),
            "label": "Notion 저장",
            "need": "NOTION_TOKEN / NOTION_BRIEFING_DB 미설정",
            "database_count": len(notion_databases),
            "database_scope": "projection_only",
        },
        "discord": {
            "configured": discord_bot_mirror or discord_webhook,
            "label": "Discord 전송",
            "transport": "bot_mirror" if discord_bot_mirror else "webhook_compat",
            "need": (
                "DISCORD_MIRROR_ENABLED / DISCORD_BOT_TOKEN_CEO / "
                "DISCORD_CEO_CHANNEL_ID 미설정"
                if not discord_bot_mirror and not discord_webhook
                else ""
            ),
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


@app.get("/ui/me")
def ui_current_user(
    owner_id: str | None = Depends(current_user),
) -> dict[str, object]:
    """Return the fixed demo subject and currently effective fund grants."""
    owner_id = owner_id or FIXED_DEMO_USER_ID
    if auth_mode() == "fixture":
        profile = {"display_name": owner_id, "status": "ACTIVE"}
        memberships: list[dict[str, object]] = []
    else:
        profile = active_user_profile(owner_id)
        memberships = authorized_fund_memberships(owner_id)
    trading_books = authorized_trading_books(owner_id)
    books_by_fund: dict[str, list[dict[str, str]]] = {}
    for book in trading_books:
        books_by_fund.setdefault(str(book["fund_id"]), []).append(
            {"book_id": str(book["book_id"]), "name": str(book["name"])}
        )
    roles_by_fund: dict[str, set[str]] = {}
    for membership in memberships:
        roles_by_fund.setdefault(str(membership["fund_id"]), set()).add(
            str(membership["role"])
        )
    if auth_mode() == "fixture":
        for book in trading_books:
            roles_by_fund.setdefault(str(book["fund_id"]), set()).add("TRADER")
    funds = [
        {
            "fund_id": fund_id,
            "roles": sorted(roles),
            "books": books_by_fund.get(fund_id, []),
        }
        for fund_id, roles in sorted(roles_by_fund.items())
    ]
    return {
        "schema_version": "portfolio.current-user.v1",
        "user_id": owner_id,
        "display_name": profile["display_name"],
        "status": profile["status"],
        "funds": funds,
        "onboarding_required": not funds,
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
    owner_id: str | None = Depends(current_user),
) -> dict[str, object]:
    """Start the advisory LangGraph and return a run reference."""
    _require_portfolio_owner(owner_id, request.user_id)
    await _require_fund_access(owner_id, request.fund_id)

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
    owner_id: str | None = Depends(current_user),
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
    owner_id: str | None = Depends(current_user),
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
        "agent_ask_enabled": hermes_boundary.agent_ask_enabled(),
        # 부서 Agent 호출이 어느 런타임으로 나가는지. BFF 가 컨테이너로 뜨면
        # `hermes` 바이너리가 그 안에 없어 `docker exec` 로 나가야 한다 - 그 상태를
        # 화면에서 구분할 수 있어야 "열려 있는 줄 알았다"가 없다.
        # (`ceo_transport` 는 CEO 입구가 Task 기반으로 재설계되면서 사라졌다.)
        "hermes_exec_mode": hermes_boundary.HERMES_EXEC_MODE,
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


def _model_plane_readiness() -> dict[str, object]:
    """Check the configured Worker model, without leaking its endpoint.

    ``deterministic_test`` is deliberately network-free.  For real workers,
    readiness checks the OpenAI-compatible ``/models`` contract and verifies
    that the configured served model is actually present.  This catches the
    host/container DNS split and model-name drift before an E2E pipeline spends
    its retry budget on a doomed generation request.
    """

    runtime = os.getenv("PORTFOLIO_WORKER_RUNTIME", "deterministic_test").strip().lower()
    configured_base = (
        os.getenv("WORKER_MODEL_BASE_URL", "").strip()
        or os.getenv("OLLAMA_BASE_URL", "").strip()
    )
    if runtime == "deterministic_test" and not configured_base:
        return {
            "status": "READY",
            "provider": "deterministic_test",
            "reachable": True,
        }

    if not configured_base:
        return {
            "status": "NOT_CONFIGURED",
            "provider": runtime or "unknown",
            "reachable": False,
        }

    try:
        from departments.worker_model_gateway import resolve

        binding = resolve()
        base_url = binding.base_url.rstrip("/")
        models_url = f"{base_url}/models"
        request = UrlRequest(
            models_url,
            headers={"Authorization": f"Bearer {binding.api_key}"},
        )
        timeout = max(
            0.2,
            min(float(os.getenv("WORKER_MODEL_HEALTH_TIMEOUT_SECONDS", "2")), 5.0),
        )
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        model_ids = {
            str(item.get("id"))
            for item in (payload.get("data") or [])
            if isinstance(item, dict) and item.get("id")
        }
        expected = {binding.model, binding.base_model}
        if not model_ids.intersection(expected):
            return {
                "status": "UNAVAILABLE",
                "provider": binding.provider,
                "model": binding.model,
                "reachable": True,
                "reason": "configured_model_not_served",
            }
        return {
            "status": "READY",
            "provider": binding.provider,
            "model": binding.model,
            "reachable": True,
        }
    except Exception as exc:  # noqa: BLE001 - readiness must never expose internals
        return {
            "status": "UNAVAILABLE",
            "provider": "configured_model",
            "reachable": False,
            "reason": type(exc).__name__,
        }


@app.get("/health/ready")
def health_ready(response: Response) -> dict[str, object]:
    """Expose dependency readiness without secrets or claiming operational durability."""

    model_plane = _model_plane_readiness()
    dependencies = {
        "bff": {"status": "READY"},
        "governance": {"status": "READY" if GOVERNANCE_API_URL else "NOT_CONFIGURED"},
        # ▶ risk·qa 를 여기 넣는 이유 (2026-08-12)
        #   전에는 이 둘이 빠져 있어서 **부서 API 가 죽어 있어도 BFF 가 ready 라고
        #   답했다.** 실제로 그날 risk 는 컨테이너가 아예 안 떠 있었고 qa 는
        #   QA_API_URL 이 빈 문자열이라 항상 503 이었는데, 이 응답만 보면 정상이었다.
        #   governance 와 같은 방식으로 **설정 유무**를 본다 - 여기서 실제 HTTP 를
        #   찌르면 readiness 가 남의 서비스 지연에 묶인다(그건 각 부서 /health/ready 몫).
        "risk": {"status": "READY" if RISK_API_URL else "NOT_CONFIGURED"},
        "qa": {"status": "READY" if QA_API_URL else "NOT_CONFIGURED"},
        "workforce": {"status": "READY" if WORKFORCE_API_URL else "NOT_CONFIGURED"},
        "control_database": {
            "status": "READY"
            if (
                os.getenv("CONTROL_DATABASE_URL", "").strip()
                or os.getenv("DATABASE_URL", "").strip()
            )
            else "NOT_CONFIGURED"
        },
        "model_plane": model_plane,
        # Backward-compatible read-only alias for dashboards that used the
        # pre-Qwen key.  Both names point to the same measured dependency.
        "ollama": model_plane,
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
    payload = {
        "status": status,
        "dependencies": dependencies,
        "external_writes": False,
    }
    # Readiness is a traffic-admission contract.  A standard HTTP probe must
    # reject a degraded candidate without parsing this service-specific body;
    # liveness remains the separate always-200 ``/health`` endpoint.
    if status != "ready":
        response.status_code = 503
    return payload


@lru_cache(maxsize=1)
def _repo():
    """회계 원장 저장소. DATABASE_URL이 없으면 None이고 Snapshot은 전부 DEMO다."""
    # The deterministic E2E mode intentionally has no database.  Do not let a
    # production PAPER_DB setting inherited from the developer's .env turn a
    # test-only read path into a persistence exception; real worker modes still
    # fail closed through LedgerRepository.from_env().
    if (
        os.getenv("PORTFOLIO_WORKER_RUNTIME", "").strip().lower() == "deterministic_test"
        and not os.getenv("DATABASE_URL", "").strip()
    ):
        return None
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


def _accounting_sections(
    book_id: UUID | None,
    fund_id: UUID | None = None,
) -> tuple[UUID, dict | None] | None:
    """(장부, 회계 구간). DB가 없거나 장부를 못 고르면 None - 호출자가 DEMO로 떨어진다.

    안쪽 `sections`가 None인 것은 다른 뜻이다 - **그 장부는 있는데 평가된 적이 없다.**
    둘을 한 값으로 합치면 "DB 없음"과 "NAV 없음"을 호출자가 구분할 수 없다.
    """
    repo = _repo()
    if repo is None:
        return None
    if book_id is not None:
        resolved = book_id
    elif fund_id is not None:
        resolve_for_fund = getattr(repo, "book_for_fund", None)
        resolved = resolve_for_fund(fund_id) if callable(resolve_for_fund) else None
    else:
        resolved = _default_book_id(repo)
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
def ui_snapshot(
    book_id: UUID | None = None,
    fund_id: UUID | None = None,
    owner_id: str | None = Depends(current_user),
) -> dict:
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
    require_fund_membership(
        owner_id, str(fund_id) if fund_id is not None else None
    )
    loop = _demo_state()
    overrides = None
    resolved = _accounting_sections(book_id, fund_id)
    if resolved is not None:
        chosen, sections = resolved
        if sections is None and book_id is not None:
            # 평가된 적 없는 장부다. 0원 NAV를 지어내지 않고 그 사실을 알린다.
            raise HTTPException(404, f"book {book_id}의 확정 Snapshot이 없습니다")
        if sections is not None:
            # 출처는 **실제로 갈아끼운 구간에만** 붙인다. 목록을 손으로 적어두면
            # 뷰가 구간을 하나 더 내놓거나 덜 내놨을 때 화면이 Scripted Loop 값을
            # private control DB의 canonical accounting projection으로 읽는다.
            overrides = {**sections,
                         "book_id": str(chosen),
                         "sources": {name: "control-db" for name in sections}}

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
def ui_research(
    owner_id: str | None = Depends(current_user),
) -> dict[str, object]:
    """Research Case read-only projection for the dashboard."""

    require_any_fund_membership(owner_id)
    return _domain_projection("research")


@app.get("/ui/strategy")
def ui_strategy(
    owner_id: str | None = Depends(current_user),
) -> dict[str, object]:
    """Strategy Factory / quant read-only projection for the dashboard."""

    require_any_fund_membership(owner_id)
    return _domain_projection("strategy")


@app.get("/ui/risk")
def ui_risk(
    owner_id: str | None = Depends(current_user),
) -> dict[str, object]:
    """Risk Center read-only projection for the dashboard."""

    require_any_fund_membership(owner_id)
    return _domain_projection("risk")


@app.get("/ui/qa")
def ui_qa(
    owner_id: str | None = Depends(current_user),
) -> dict[str, object]:
    """AI QA·Audit read-only projection for the dashboard."""

    require_any_fund_membership(owner_id)
    return _domain_projection("qa")


@app.get("/ui/risk-qa")
def ui_risk_qa(
    owner_id: str | None = Depends(current_user),
) -> dict[str, object]:
    """Combined Risk·QA projection consumed by the office panel."""

    require_any_fund_membership(owner_id)
    return _domain_projection("risk-qa")


@app.post("/ui/commands/trading-state", status_code=202)
def request_trading_state_command(
    command: TradingStateCommand,
    owner_id: str | None = Depends(current_user),
) -> dict[str, object]:
    """Record a versioned approval request without changing binding state."""

    require_fund_membership(owner_id, str(command.target.fund_id))
    try:
        return COMMAND_SERVICE.submit(command)
    except CommandVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/ui/commands/audit")
def ui_command_audit(
    owner_id: str | None = Depends(current_user),
) -> dict[str, object]:
    """Return BFF-local audit events; no broker or ledger credentials are exposed."""

    require_any_fund_membership(owner_id)
    events = COMMAND_SERVICE.audit_events()
    return {"schema_version": "operator-command-audit.v1", "events": events}


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
    # 회계 구간은 private control DB와 Canonical 장부가 있으면 control-db,
    # 없으면 scripted-loop -
    # 어느 쪽이든 **화면이 출처를 모르는 상태로 나가지 않는다**는 게 계약이다.
    assert set(snap["sources"]) == {"portfolio", "trading", "ledger", "treasury"}, \
        snap["sources"]
    assert snap["sources"]["trading"] == "scripted-loop", snap["sources"]
    assert snap["sources"]["portfolio"] == snap["sources"]["ledger"], \
        f"회계 두 구간의 출처가 갈라졌다: {snap['sources']}"
    assert snap["sources"]["portfolio"] in ("control-db", "scripted-loop"), snap["sources"]
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
        assert wired["sources"]["portfolio"] == "control-db"
        assert wired["sources"]["ledger"] == "control-db"
        assert wired["sources"]["treasury"] == "control-db"
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

    from apps.api import hermes_boundary as _hermes_boundary

    _saved_flag = _hermes_boundary.ENABLE_AGENT_ASK
    _hermes_boundary.ENABLE_AGENT_ASK = True
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
        _orig_ask = _accounting_router.hermes_boundary.ask

        def _fake_ask(*, department, config, query):
            _called.append(query)
            return {"department": department, "answer": "stub", "session_id": "s1",
                    "authoritative": False, "source_of_record": "/ui/snapshot"}

        _accounting_router.hermes_boundary.ask = _fake_ask
        try:
            heavy = c.post("/accounting/agent/ask",
                           json={"query": "마감 확정해도 되는지 감사 근거와 함께 설명"}).json()
            assert heavy["routing"]["level"] == "L3" and heavy["routing"]["tier"] == "heavy"
            assert heavy["routing"]["calls_model"] is True and len(_called) == 1
            assert heavy["authoritative"] is False, "라우팅이 공식 수치 계약을 깼다"
        finally:
            _accounting_router.hermes_boundary.ask = _orig_ask
    finally:
        _hermes_boundary.ENABLE_AGENT_ASK = _saved_flag
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
        # 2026-08-12~14 온보딩 경로(USER_INPUT_API_SPEC 6.1 #2). Mandate 생성·현재
        # metadata 교체·레거시 Version 제안·챗봇 제안·적합성 프로필. 챗봇
        # (`mandate-assistant/suggest`)은 Stateless라
        # 아무것도 저장하지 않고, 나머지는 정책 검증을 상류 도메인이 소유한다.
        "/ui/mandates",
        "/ui/mandates/{mandate_id}",
        "/ui/mandates/by-fund/{fund_id}/current",
        "/ui/mandates/{mandate_id}/versions",
        "/ui/mandate-assistant/suggest",
        "/ui/investor-profiles",
        "/ui/investor-profiles/current",
        "/ui/mandates/{mandate_id}/current",
        "/ui/risk/mandates/{mandate_id}/assess",
        "/ui/mandate-cases/{case_id}/advance",
        "/ui/mandate-cases/{case_id}/timeline",
        "/ui/mandate-approvals",
        "/ui/mandate-approvals/{approval_id}/decide",
        # 2026-08-12 CEO Kanban 워크플로 경로. ask/archive를 뺀 나머지는 읽기 전용이고,
        # archive는 기록을 지우지 않는다(감사 추적 유지). DELETE는 만들지 않는다.
        "/ui/ceo/ask",
        "/ui/ceo/kanban",
        "/ui/ceo/tasks",
        "/ui/ceo/tasks/{task_id}",
        "/ui/ceo/tasks/{task_id}/graph",
        "/ui/ceo/tasks/{task_id}/result",
        "/ui/ceo/tasks/{task_id}/archive",
        # Web/Discord 공용 mirror. ingress는 ask와 같은 dedup 경계를 타고,
        # events 계열은 sanitized 이벤트 저널 조회·발행이라 원장을 건드리지 않는다.
        "/ui/ceo/ingress",
        "/ui/ceo/events",
        "/ui/ceo/events/stream",
        # 준비 상태 조회. /health와 달리 의존성까지 확인한다.
        "/health/ready",
        # 대시보드 Domain Read Model(문서 10.4). 전부 읽기 전용 projection이다.
        "/ui/research",
        "/ui/strategy",
        "/ui/risk",
        "/ui/qa",
        "/ui/risk-qa",
        # 안전 Command 접수·감사. trading-state는 PENDING_APPROVAL/NOT_EXECUTED만
        # 돌려주고 OMS·Risk Engine·Broker·Ledger를 바꾸지 않는다.
        "/ui/commands/trading-state",
        "/ui/commands/audit",
        # Local-fixture user directives are the highest-priority PAPER lane.
        # The BFF authorizes fund/book ownership and the trading domain owns OMS.
        "/ui/paper-orders",
        "/ui/paper-orders/sell-all",
        "/ui/paper-orders/cancel-all",
        "/ui/paper-orders/{directive_id}",
        "/ui/paper-orders/{directive_id}/status",
        "/trading/agent/order",
        # Broker(LS) 조회 projection. authoritative=false이며 공식 NAV가 아니다.
        "/ui/account/snapshot",
        # QA 도메인 위임 조회. 판정은 qa-api가 소유한다.
        "/ui/qa/verifications/{verification_id}/assess",
    }
    # ▶ 2026-08-14 수정: 예전에는 위 집합 뒤에 `, c.get(...).keys()` 가 붙어 있어
    #   `required_paths` 가 (set, keys) 튜플이 되고 **비교 자체가 없었다.** 그래서
    #   "경로가 늘면 여기서 깨진다"는 위 주석이 실제로는 아무것도 막지 못했고,
    #   선언 33개 / 실제 46개까지 벌어진 뒤에야 발견됐다. 이제 정확히 비교한다.
    assert paths == required_paths, (
        f"OpenAPI 경로가 선언과 다르다.\n"
        f"  선언에 없는 실제 경로: {sorted(paths - required_paths)}\n"
        f"  실제에 없는 선언 경로: {sorted(required_paths - paths)}"
    )

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

    assert hermes_boundary.timeout_of(accounting.CONFIG) == 60

    # session_id는 stderr에서 뽑는다. 없으면 None이지 빈 문자열이 아니다
    assert hermes_boundary.session_id_of("\nsession_id: abc123\n") == "abc123"
    assert hermes_boundary.session_id_of("") is None

    print("ok - BFF 8개 영역 점검 통과 (회계 구간 Supabase 배선 + TTL 캐시 포함)")
