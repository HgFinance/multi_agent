#!/usr/bin/env python3
"""`strategy-runtime-control` sidecar 진입점.

▶ 왜 `portfolio-bff`에 바로 넣지 않았는가
  `portfolio-bff`는 이 저장소에서 유일하게 외부에 노출되는 서비스다
  (`docker-compose.yml` 주석 "프런트(ai-office)가 붙는 유일한 관문"). 거기에
  docker 소켓을 물리면 그 서비스 하나가 뚫렸을 때 호스트의 모든 컨테이너를
  살릴 수 있다. 정확히 그 이유로 `portfolio-bff`는 HTTP 경계만 소유하고
  Docker-exec transport는 갖지 않도록 이미 결정돼 있었다(`docker-compose.yml`
  133번째 줄 근처 주석).

  같은 저장소에 이미 있는 "docker-outside-of-docker" 선례
  (`factory-autopilot`/`card-watchdog`)를 그대로 따른다 - 소켓이 필요한 일은
  외부에 노출되지 않는 별도 컨테이너에서 하고, 노출된 BFF는 내부 네트워크로만
  그 컨테이너를 호출한다. 이 서비스는 host 포트를 publish하지 않는다.

자체 점검은 `strategy_runtime.py` 쪽에 있다 - 여기는 얇은 FastAPI 배선뿐이다.
"""
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import strategy_runtime

app = FastAPI(
    title="strategy-runtime-control",
    description="mlpipe-paper 컨테이너 상태·전원 제어. 내부 전용 - 인터넷에 노출되지 않는다.",
)


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
