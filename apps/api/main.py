#!/usr/bin/env python3
"""AI Office BFF (FastAPI). 도현 담당분 - Read Model 제공 + Hermes Agent 연결.

근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 5.2(연결 순서), 6(명령 경계)
      docs/02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md 4절 `apps/api/`

경계 두 개를 코드로 강제한다.

1. **Read-only다.** 이 서비스에는 주문 제출·분개 Posting·상태 변경 경로가 없다.
   계획 6절의 위험 Command(SET_TRADING_STATE 등)는 인증·승인·Audit가 붙기 전까지
   여기 열지 않는다.
2. **Agent 응답은 수치가 아니다.** `/agent/ask`가 돌려주는 것은 Hermes CLI의 텍스트고,
   공식 Position·PnL·NAV는 오직 `/ui/snapshot`에서만 나온다
   (팀 가이드 원칙 5: 회계 수치를 LLM 문장에서 추출해 확정하지 않는다).

실행:
    uv run --python .venv/Scripts/python.exe uvicorn apps.api.main:app --reload --port 8000
자체 점검:
    python apps/api/main.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "departments" / "05-accounting-portfolio" / "portfolio"))
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

from ui_read_model import build_ui_snapshot  # noqa: E402

# Hermes CLI 이름 -> 저장소 Profile 경로. 이 브랜치는 도현 파트만 노출한다.
# 다른 본부 Agent를 우리 BFF가 대신 호출하면 권한 경계가 무너진다(마스터플랜 5.6).
DEPARTMENTS = {
    "trading-department": "departments/02-trading/hermes/config.yaml",
    "accounting-portfolio-department": "departments/05-accounting-portfolio/hermes/config.yaml",
}

app = FastAPI(title="AI Office BFF", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    # ponytail: 개발용 로컬 Origin 고정. 배포 Origin이 정해지면 환경변수로 뺀다.
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _demo_state():
    """Scripted Paper Loop 한 바퀴. Snapshot의 DEMO 원천이다.

    Supabase Read Model이 붙기 전까지의 원천이며, 손으로 쓴 Fixture 대신
    실제 OMS/Ledger를 돌린다 - 백엔드가 바뀌면 여기가 같이 깨져야 한다.
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
    return {"status": "ok", "mode": "DEMO"}


@app.get("/ui/snapshot")
def ui_snapshot() -> dict:
    """계획 5.2의 `GET /ui/snapshot`. 화면 State는 이 한 장에서 재구축된다."""
    loop = _demo_state()
    return build_ui_snapshot(
        oms=loop.oms,
        ledger=loop.ledger,
        snapshot=loop.snapshot(),
        mode="DEMO",
    )


class AgentAsk(BaseModel):
    department: str
    query: str = Field(min_length=1, max_length=2000)


@app.post("/agent/ask")
def agent_ask(req: AgentAsk) -> dict:
    """Hermes 부서 Agent에 질의한다. 텍스트 응답만 돌려주고 아무것도 실행하지 않는다."""
    if req.department not in DEPARTMENTS:
        raise HTTPException(404, f"알 수 없는 부서: {req.department}")

    timeout = _timeout_of(req.department)
    try:
        # shell=False. 사용자 문자열이 셸을 거치지 않게 인자 리스트로만 넘긴다.
        proc = subprocess.run(
            [req.department, "chat", "-q", req.query],
            capture_output=True, text=True, timeout=timeout, cwd=ROOT,
        )
    except FileNotFoundError:
        # Hermes Runtime은 PyPI 패키지가 아니라 별도 설치다(CLAUDE.md).
        raise HTTPException(503, f"Hermes CLI 없음: {req.department}")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"{timeout}s 초과")

    if proc.returncode != 0:
        raise HTTPException(502, proc.stderr.strip()[:500] or "agent failed")

    return {
        "department": req.department,
        "answer": proc.stdout.strip(),
        # 화면이 이 값을 수치로 쓰지 못하게 계약에 박아둔다.
        "authoritative": False,
        "source_of_record": "/ui/snapshot",
    }


def _timeout_of(department: str) -> int:
    """Profile의 agent.timeout_seconds를 그대로 쓴다. 부서마다 다르다."""
    cfg = yaml.safe_load((ROOT / DEPARTMENTS[department]).read_text(encoding="utf-8"))
    return int(cfg["agent"]["timeout_seconds"])


if __name__ == "__main__":
    from fastapi.testclient import TestClient

    c = TestClient(app)

    assert c.get("/health").json()["status"] == "ok"

    snap = c.get("/ui/snapshot").json()
    assert snap["mode"] == "DEMO", "BFF Snapshot은 DEMO여야 한다"
    assert snap["ledger"]["balanced"] is True, "차대가 맞지 않는 원장이 화면으로 나갔다"
    assert isinstance(snap["portfolio"]["nav"], str), "금액이 JSON number로 나갔다"
    assert snap["trading"]["orders"][0]["state"] == "FILLED"

    # 두 번 불러도 같은 Snapshot이다. Read-only가 상태를 바꾸면 안 된다
    assert c.get("/ui/snapshot").json()["portfolio"]["nav"] == snap["portfolio"]["nav"]

    # 다른 본부 Agent는 이 BFF로 부를 수 없다
    assert c.post("/agent/ask", json={"department": "risk-management", "query": "x"}).status_code == 404
    assert c.post("/agent/ask", json={"department": "ceo-agent", "query": "x"}).status_code == 404
    # 빈 질의는 스키마에서 걸린다
    assert c.post("/agent/ask", json={"department": "trading-department", "query": ""}).status_code == 422
    # 우리 부서는 통과하되 Hermes 미설치 환경에선 503이다 (500이 아니라)
    assert c.post(
        "/agent/ask", json={"department": "accounting-portfolio-department", "query": "NAV?"}
    ).status_code in (200, 503, 502, 504)

    assert _timeout_of("accounting-portfolio-department") == 60

    print("ok - BFF 6개 영역 점검 통과")
