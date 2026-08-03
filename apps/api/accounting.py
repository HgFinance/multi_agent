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

엔드포인트 두 개다.
  POST /accounting/agent/ask            회계본부 Hermes Agent 질의 (텍스트만)
  GET  /accounting/v1/portfolio-snapshot  portfolio-api. CEO Daily Report용 참조
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import hermes_cli
import psycopg2
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException

DEPARTMENT = "accounting-portfolio-department"   # Hermes Profile 이름
CONFIG = "departments/05-accounting-portfolio/hermes/config.yaml"  # 저장소 사본

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

router = APIRouter(prefix="/accounting", tags=["accounting-portfolio"])


@router.post("/agent/ask")
def agent_ask(req: hermes_cli.AgentAsk) -> dict:
    """회계·포트폴리오본부 Agent 질의.

    돌아오는 것은 텍스트뿐이다. Position·PnL·NAV는 여기서 읽지 않는다 -
    팀 가이드 원칙 5(회계 수치를 LLM 문장에서 추출해 확정하지 않는다).
    """
    return hermes_cli.ask(department=DEPARTMENT, config=CONFIG, query=req.query)


# --- portfolio-api -----------------------------------------------------------
# CEO Daily Report(departments/00-ceo-office/src/reporting/daily_report.py)의
# SnapshotRef(portfolio) 원천이다.
# 근거: docs/02-engineering/GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 5.1
#       "portfolio-api - Position/Cash/PnL/NAV | CEO | Daily Report, 자본 배분"
#
# **수치를 돌려주지 않는다.** snapshot_id와 as_of만 준다. NAV·Cash·PnL 값을 함께
# 실으면 CEO 쪽이 그 숫자를 보고서에 옮겨 적을 수 있게 되고, 그러면 공식 수치의
# 출처가 둘로 갈린다. 참조만 넘기고 값은 원장이 소유한다는 것이 이 계약의 요지다
# (팀 가이드 원칙 5, API_SPEC 5.1 "공식 Snapshot과 Evidence Reference만 받는다").


def _query_latest_snapshot(fund_id: UUID, as_of: datetime) -> tuple[str, datetime] | None:
    """기준 시각 이하의 가장 최근 확정 Snapshot 한 건.

    미래 Snapshot을 돌려주지 않는다 - 보고서가 기준일 이후 관측을 참조하면
    Point-in-Time이 깨진다(마스터플랜 9.3).
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        raise HTTPException(503, "DATABASE_URL이 설정되지 않았습니다")

    try:
        # ponytail: 요청마다 연결한다. DATABASE_URL이 Supabase Pooler를 가리키고 있어
        #           당장은 버틴다. 부하가 보이면 psycopg2 SimpleConnectionPool로 바꾼다.
        # ponytail: 지금 자격증명이 postgres(슈퍼유저)라 RLS를 우회한다. 읽기 전용
        #           서비스롤이 생기면 그걸로 바꾸고 SET app.fund_id 경로를 붙인다.
        with psycopg2.connect(url, connect_timeout=8) as conn, conn.cursor() as cur:
            cur.execute(
                """
                select portfolio_snapshot_id, as_of
                  from accounting.portfolio_snapshots
                 where fund_id = %s and as_of <= %s
                 order by as_of desc
                 limit 1
                """,
                (str(fund_id), as_of),
            )
            row = cur.fetchone()
    except psycopg2.Error as exc:
        raise HTTPException(503, f"회계 DB 조회 실패: {type(exc).__name__}")

    return (str(row[0]), row[1]) if row else None


@router.get("/v1/portfolio-snapshot")
def portfolio_snapshot(fund_id: UUID, as_of: datetime | None = None) -> dict:
    """확정된 Portfolio Snapshot의 참조를 돌려준다.

    없으면 404다. 값을 지어내거나 가장 가까운 것을 대신 주지 않는다 - 존재하지
    않는 snapshot_id가 CEO 보고서에 실리면 추적 불가능한 근거가 된다.
    """
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    found = _query_latest_snapshot(fund_id, as_of)
    if found is None:
        raise HTTPException(
            404,
            f"fund {fund_id}의 {as_of.isoformat()} 이전 확정 Snapshot이 없습니다",
        )

    snapshot_id, snapshot_as_of = found
    return {"snapshot_id": snapshot_id, "as_of": snapshot_as_of.isoformat()}
