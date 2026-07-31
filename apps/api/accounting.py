#!/usr/bin/env python3
"""회계·포트폴리오본부 Router. 이 파일은 회계본부 Agent 하나만 부를 수 있다.

소유: 도현 (회계·포트폴리오)
근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 6.1(portfolio-api, accounting-api)
      docs/HEDGE_FUND_MASTER_PLAN.md 5.6

부서 이름과 Profile 경로가 이 파일 안에 상수로 있다. 중앙 화이트리스트 Dict가
없는 것이 의도다 - 부서를 늘리려면 파일과 Router를 추가해야 하고, 그러면
누가 어느 본부를 여는지가 Diff에 드러난다.

여기에 절대 두지 않는 것: 분개 Posting, NAV 확정, Break 종결. 전부 Command이며
인증·승인·Audit가 붙기 전까지 BFF에 열지 않는다(계획 6절).
"""
from __future__ import annotations

from fastapi import APIRouter

import hermes_cli

DEPARTMENT = "accounting-portfolio-department"   # Hermes Profile 이름
CONFIG = "departments/05-accounting-portfolio/hermes/config.yaml"  # 저장소 사본

router = APIRouter(prefix="/accounting", tags=["accounting-portfolio"])


@router.post("/agent/ask")
def agent_ask(req: hermes_cli.AgentAsk) -> dict:
    """회계·포트폴리오본부 Agent 질의.

    돌아오는 것은 텍스트뿐이다. Position·PnL·NAV는 여기서 읽지 않는다 -
    팀 가이드 원칙 5(회계 수치를 LLM 문장에서 추출해 확정하지 않는다).
    """
    return hermes_cli.ask(department=DEPARTMENT, config=CONFIG, query=req.query)
