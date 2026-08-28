"""`strategy-runtime-control` sidecar 진입점.

▶ 왜 `portfolio-bff`에 바로 넣지 않았는가
  `portfolio-bff`는 이 저장소에서 유일하게 외부에 노출되는 서비스다
  (`docker-compose.yml` 주석 "프런트(ai-office)가 붙는 유일한 관문"). 거기에
  docker 소켓을 물리면 그 서비스 하나가 뚫렸을 때 호스트의 모든 컨테이너를
  살릴 수 있다. 정확히 그 이유로 `portfolio-bff`는 HTTP 경계만 소유하고
  Docker-exec transport는 갖지 않도록 이미 결정돼 있었다(`docker-compose.yml`
  133번째 줄 근처 주석).

  같은 저장소에 이미 있는 "docker-outside-of-docker" 선례
  (retained operational control services)를 그대로 따른다 - 소켓이 필요한 일은
  외부에 노출되지 않는 별도 컨테이너에서 하고, 노출된 BFF는 내부 네트워크로만
  그 컨테이너를 호출한다. 이 서비스는 host 포트를 publish하지 않는다.

자체 점검은 `strategy_runtime.py` 쪽에 있다 - 여기는 얇은 FastAPI 배선뿐이다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from uuid import uuid4
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

try:
    from strategy_runtime_client import (
        runtime_service_authorized,
        runtime_service_token_configured,
    )
except ModuleNotFoundError:  # pragma: no cover - package import in tests
    from .strategy_runtime_client import (
        runtime_service_authorized,
        runtime_service_token_configured,
    )

try:
    import strategy_runtime
except ModuleNotFoundError:  # pragma: no cover - package import in tests
    from . import strategy_runtime

app = FastAPI(
    title="strategy-runtime-control",
    description="mlpipe-paper 컨테이너 상태·전원 제어. 내부 전용 - 인터넷에 노출되지 않는다.",
)


def _paper_order_token(deployment_id: str) -> str | None:
    secret = os.getenv("STRATEGY_PAPER_ORDER_TOKEN", "").strip()
    if len(secret) < 32:
        return None
    return hmac.new(secret.encode(), deployment_id.encode(), hashlib.sha256).hexdigest()


def _paper_order_request_authorized(request: Request) -> bool:
    prefix = "/deployments/"
    suffix = "/orders"
    path = request.url.path
    if not (path.startswith(prefix) and path.endswith(suffix)):
        return False
    deployment_id = path[len(prefix) : -len(suffix)]
    expected = _paper_order_token(deployment_id)
    supplied = request.headers.get("authorization", "")
    return bool(expected) and hmac.compare_digest(supplied, f"Bearer {expected}")


@app.middleware("http")
async def require_internal_service_auth(request: Request, call_next):
    if request.url.path != "/health":
        if _paper_order_request_authorized(request):
            return await call_next(request)
        if not runtime_service_token_configured():
            return JSONResponse(
                status_code=503,
                content={"detail": "strategy_runtime_auth_unconfigured"},
            )
        if not runtime_service_authorized(request.headers.get("authorization")):
            return JSONResponse(
                status_code=401,
                content={"detail": "strategy_runtime_auth_required"},
            )
    return await call_next(request)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/snapshot")
def snapshot() -> dict:
    try:
        return strategy_runtime.strategy_snapshot()
    except strategy_runtime.StrategyRuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class PowerRequest(BaseModel):
    action: Literal["start", "stop"]


@app.post("/power")
def power(body: PowerRequest) -> dict:
    try:
        return {"container": strategy_runtime.set_power(body.action)}
    except strategy_runtime.StrategyRuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class PaperDeployRequest(BaseModel):
    """Narrow internal handoff; Docker options are never caller-controlled."""

    deployment_id: str
    request_id: str
    bundle_path: str
    bundle_hash: str


@app.post("/deploy")
def deploy(body: PaperDeployRequest) -> dict:
    try:
        return strategy_runtime.deploy_paper_bundle(
            deployment_id=body.deployment_id,
            request_id=body.request_id,
            bundle_path=body.bundle_path,
            bundle_hash=body.bundle_hash,
        )
    except strategy_runtime.StrategyRuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/deployments/{deployment_id}")
def deployment_status(deployment_id: str) -> dict:
    try:
        return strategy_runtime.paper_deployment_snapshot(deployment_id)
    except strategy_runtime.StrategyRuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class PaperPowerRequest(BaseModel):
    action: Literal["start", "stop"]


class StrategyPaperOrderRequest(BaseModel):
    """Child-to-sidecar request; the sidecar supplies the Trading proof."""

    deployment_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: str
    signal_key: str


def _b64(value: dict[str, object]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _trading_service_token() -> str:
    secret = os.getenv("TRADING_INTERNAL_SERVICE_AUTH_SECRET", "").strip()
    issuer = os.getenv("TRADING_INTERNAL_SERVICE_AUTH_ISSUER", "").strip()
    audience = os.getenv("TRADING_INTERNAL_SERVICE_AUTH_AUDIENCE", "").strip()
    if len(secret) < 32 or not issuer or not audience:
        raise HTTPException(status_code=503, detail="strategy_paper_internal_auth_unconfigured")
    issued_at = time.time()
    header = _b64({"alg": "HS256", "typ": "JWT"})
    payload = _b64(
        {
            "iss": issuer,
            "aud": audience,
            "sub": "strategy-runtime-control",
            "department": "trading-department",
            "service": "strategy-runtime-control",
            "scopes": ["trading.strategy-paper.execute"],
            "jti": f"strategy-paper-{uuid4()}",
            "iat": issued_at,
            "nbf": issued_at,
            "exp": issued_at + 60,
        }
    )
    signature = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"Bearer {header}.{payload}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


@app.post("/deployments/{deployment_id}/orders", status_code=202)
def strategy_paper_order(deployment_id: str, body: StrategyPaperOrderRequest) -> dict:
    """Relay a deployment-bound signal to Trading's PAPER-only order route."""

    if body.deployment_id != deployment_id:
        raise HTTPException(status_code=409, detail="strategy_paper_deployment_mismatch")
    container = strategy_runtime.container_status(
        strategy_runtime._deployment_container_name(deployment_id)
    )
    if not container.get("running"):
        raise HTTPException(status_code=409, detail="strategy_paper_runtime_not_running")
    trading_url = os.getenv("STRATEGY_PAPER_TRADING_API_URL", "http://trading-api:8000").rstrip("/")
    try:
        response = httpx.post(
            f"{trading_url}/trading/v1/internal/strategy-paper/orders",
            json=body.model_dump(mode="json"),
            headers={"Authorization": _trading_service_token()},
            timeout=8.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="strategy_paper_trading_api_unreachable") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {"error_code": "TRADING_INVALID_RESPONSE", "message": "Trading API returned non-JSON"}
    if response.status_code >= 400:
        if isinstance(payload, dict):
            raise HTTPException(status_code=response.status_code, detail=payload)
        raise HTTPException(status_code=response.status_code, detail="strategy_paper_order_rejected")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="strategy_paper_trading_response_invalid")
    return payload


@app.post("/deployments/{deployment_id}/power")
def deployment_power(deployment_id: str, body: PaperPowerRequest) -> dict:
    try:
        return strategy_runtime.power_paper_deployment(
            deployment_id=deployment_id, action=body.action
        )
    except strategy_runtime.StrategyRuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/deployments/{deployment_id}/remove")
def deployment_remove(deployment_id: str) -> dict:
    try:
        return strategy_runtime.remove_paper_deployment(deployment_id=deployment_id)
    except strategy_runtime.StrategyRuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
