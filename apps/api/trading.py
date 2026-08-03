#!/usr/bin/env python3
"""트레이딩본부 Router. 이 파일은 트레이딩본부 Agent 하나만 부를 수 있다.

소유: 도현 (트레이딩)
근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 6.1(trading-api, oms-api)
      docs/HEDGE_FUND_MASTER_PLAN.md 5.6

Agent가 낼 수 있는 것은 `OrderIntent` 제안까지다. `oms.submit_order`는 Agent Tool로도
BFF 경로로도 노출하지 않는다 - Risk 승인 없이 OMS가 SUBMITTED로 갈 수 없다는 규칙이
Prompt가 아니라 "경로가 없음"으로 지켜져야 한다.
"""
from __future__ import annotations

import hermes_cli
from fastapi import APIRouter

DEPARTMENT = "trading-department"   # Hermes Profile 이름
CONFIG = "departments/02-trading/hermes/config.yaml"  # 저장소 사본

router = APIRouter(prefix="/trading", tags=["trading"])


@router.post("/agent/ask")
def agent_ask(req: hermes_cli.AgentAsk) -> dict:
    """트레이딩본부 Agent 질의. 텍스트만 돌려주고 주문 경로는 없다."""
    return hermes_cli.ask(department=DEPARTMENT, config=CONFIG, query=req.query)
