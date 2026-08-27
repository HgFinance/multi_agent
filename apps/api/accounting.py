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
  POST /accounting/agent/ask            회계본부 질의 (L0 즉시, L1~L3 CEO/Kanban 접수)
  GET  /accounting/v1/portfolio-snapshot  portfolio-api. CEO Daily Report용 참조

**질의는 Level 로 분류한 뒤 처리한다 (2026-08-05).** 난이도 편차가 커서 다 같은 값으로
태우면 한쪽은 낭비고 한쪽은 부족하다. 분류는 결정론이며
`departments/05-accounting-portfolio/query_router.py` 가 한다.
L0(결정론 조회)은 **모델을 아예 안 부르고** 원장 읽기 경로를 알려준다 - 제일 싼 모델은
안 부르는 모델이고, 덤으로 원장 수치가 LLM 문장을 거치지 않으니 원칙 5 도 같이 지켜진다.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

try:
    from . import hermes_boundary
except ImportError:
    import hermes_boundary
import psycopg2
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException

try:
    from .current_user import current_user, require_fund_membership
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    from current_user import current_user, require_fund_membership

DEPARTMENT = "accounting-portfolio-department"   # Hermes Profile 이름
CONFIG = "departments/05-accounting-portfolio/hermes/config.yaml"  # 저장소 사본

_DEPT_DIR = Path(__file__).resolve().parents[2] / "departments" / "05-accounting-portfolio"
if str(_DEPT_DIR) not in sys.path:
    sys.path.append(str(_DEPT_DIR))

from query_router import classify, routing_note  # noqa: E402 - sys.path 조정 뒤
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

router = APIRouter(prefix="/accounting", tags=["accounting-portfolio"])


def _enqueue_accounting_via_ceo(req: hermes_boundary.AgentAsk) -> dict[str, object]:
    """Send model-backed accounting questions through the canonical workflow.

    The production BFF intentionally owns neither a department profile nor its
    provider credentials.  The old synchronous CLI call therefore returned
    502.  Reuse the existing CEO root producer instead: Kanban dispatch owns
    the Accounting Hermes process and the supervisor attaches Accounting
    Engine plus LS broker evidence at that boundary.
    """

    try:
        from .ceo import CeoAsk, ceo_query
    except ImportError:  # pragma: no cover - direct apps/api script path
        from ceo import CeoAsk, ceo_query

    try:
        from orchestration.ceo_query_routing import build_deterministic_bff_plan
    except ImportError:  # pragma: no cover - direct apps/api script path
        from ceo_query_routing import build_deterministic_bff_plan

    routing_plan = build_deterministic_bff_plan(
        req.query,
        selected_departments=("accounting",),
    )

    return ceo_query(
        CeoAsk(
            query=req.query,
            request_id=req.request_id,
            source="accounting-agent-alias",
        ),
        owner_id=None,
        deterministic_routing_plan=routing_plan,
    )


@router.post("/agent/ask")
def agent_ask(req: hermes_boundary.AgentAsk) -> dict:
    """회계·포트폴리오본부 Agent 질의.

    Position·PnL·NAV는 여기서 읽지 않는다 -
    팀 가이드 원칙 5(회계 수치를 LLM 문장에서 추출해 확정하지 않는다).

    질의 Level 을 먼저 정하고, L0 은 모델을 부르지 않고 결정론 원천으로 돌려보낸다.
    L1~L3은 BFF가 부서 인증을 소유하지 않는 원칙을 지키며 CEO/Kanban에 접수하고,
    완료 결과는 반환된 `result_url`에서 읽는다.
    응답에 `routing` 이 항상 붙어 왜 그 등급이었는지가 감사에서 설명된다.
    """
    # **게이트가 라우팅보다 먼저다.** L0 이 모델을 안 부른다고 해서 비활성 엔드포인트가
    # 일부만 열리면, 최적화가 조용히 보안 경계를 깎은 것이 된다. 라우팅은 게이트 안쪽의
    # 비용 최적화일 뿐이다.
    if not hermes_boundary.agent_ask_enabled():
        raise HTTPException(
            503,
            "Agent 질의는 인증·Tool Allowlist 연결 전까지 기본 비활성화 상태입니다.",
        )

    routing = classify(req.query)
    if not routing.calls_model:
        # 모델을 안 부른다. 답을 지어내지도 않는다 - 어디서 읽으면 되는지만 알려준다.
        return {
            "department": DEPARTMENT,
            "answer": (f"이 질의는 {routing.level}({routing.level_name})입니다. "
                       f"모델을 호출하지 않았습니다. 수치는 {routing.deterministic_source} "
                       "에서 읽으십시오."),
            "session_id": None,
            "authoritative": False,
            "source_of_record": "/ui/snapshot",
            "routing": routing.as_dict(),
            "routing_note": routing_note(routing),
        }
    accepted = _enqueue_accounting_via_ceo(req)
    task_id = str(accepted.get("task_id") or "").strip() or None
    return {
        "department": DEPARTMENT,
        "answer": "회계 질의를 CEO → Kanban → Accounting Hermes 경로로 접수했습니다.",
        "session_id": None,
        "task_id": task_id,
        "status": accepted.get("status") or "accepted",
        "result_url": f"/ui/ceo/tasks/{task_id}/result" if task_id else None,
        "authoritative": False,
        "source_of_record": "/accounting/v1/ledgers/{book_id}/advisory-snapshot",
        "execution_path": "CEO_KANBAN_ACCOUNTING_HERMES",
        "routing": routing.as_dict(),
        "routing_note": routing_note(routing),
    }


# --- portfolio-api -----------------------------------------------------------
# CEO Daily Report(departments/00-ceo-office/src/reporting/daily_report.py)의
# SnapshotRef(portfolio) 원천이다.
# 근거: docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md 5.5(Accounting·Portfolio)
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
        raise HTTPException(
            503, f"회계 DB 조회 실패: {type(exc).__name__}"
        ) from exc

    return (str(row[0]), row[1]) if row else None


@router.get("/v1/portfolio-snapshot")
def portfolio_snapshot(
    fund_id: UUID,
    as_of: datetime | None = None,
    owner_id: str | None = Depends(current_user),
) -> dict:
    """확정된 Portfolio Snapshot의 참조를 돌려준다.

    없으면 404다. 값을 지어내거나 가장 가까운 것을 대신 주지 않는다 - 존재하지
    않는 snapshot_id가 CEO 보고서에 실리면 추적 불가능한 근거가 된다.
    """
    require_fund_membership(owner_id, str(fund_id))
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
