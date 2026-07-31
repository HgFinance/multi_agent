#!/usr/bin/env python3
"""AI Office BFF (FastAPI). 도현 담당분 - Read Model 제공 + Hermes Agent 연결.

근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 5.2(연결 순서), 6(명령 경계)
      docs/02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md 4절 `apps/api/`

이 파일은 조립만 한다. 부서별 Agent 경로는 각 Router 파일이 소유한다
(`accounting.py`, `trading.py`). 프로세스는 하나다 - 부서별로 프로세스를 쪼개는
것은 Service Identity와 인증이 실제로 생긴 뒤에 한다.

경계 두 개를 코드로 강제한다.

1. **금융 상태는 Read-only다.** 이 서비스에는 주문 제출·분개 Posting·상태 변경 경로가 없다.
   계획 6절의 위험 Command(SET_TRADING_STATE 등)는 인증·승인·Audit가 붙기 전까지
   여기 열지 않는다. Hermes chat은 Tool을 실행할 수 있으므로 기본 비활성화한다.
2. **Agent 응답은 수치가 아니다.** `/{부서}/agent/ask`가 돌려주는 것은 Hermes CLI의
   텍스트고, 공식 Position·PnL·NAV는 오직 `/ui/snapshot`에서만 나온다
   (팀 가이드 원칙 5: 회계 수치를 LLM 문장에서 추출해 확정하지 않는다).

실행:
    uv run --python .venv/Scripts/python.exe uvicorn apps.api.main:app --reload --port 8000
자체 점검:
    python apps/api/main.py
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "departments" / "05-accounting-portfolio" / "portfolio"))
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

import accounting  # noqa: E402
import hermes_cli  # noqa: E402
import trading  # noqa: E402
from ui_read_model import build_ui_snapshot  # noqa: E402

app = FastAPI(title="AI Office BFF", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    # ponytail: 개발용 로컬 Origin 고정. 배포 Origin이 정해지면 환경변수로 뺀다.
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# 도현 파트 2개만 등록한다. 다른 본부 Agent를 우리 BFF가 대신 호출하면
# 권한 경계가 무너진다(마스터플랜 5.6).
app.include_router(accounting.router)
app.include_router(trading.router)


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
        "departments": [accounting.DEPARTMENT, trading.DEPARTMENT],
    }


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


if __name__ == "__main__":
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
    assert c.post("/risk/agent/ask", json={"query": "x"}).status_code == 404
    assert c.post("/ceo/agent/ask", json={"query": "x"}).status_code == 404
    # 공개 경로 전체를 못 박는다. Command 경로(Posting, NAV 확정, 주문 제출)가
    # 하나라도 늘면 여기서 깨진다 - 늘리려면 이 목록을 고쳐야 하고 Diff에 남는다
    assert set(c.get("/openapi.json").json()["paths"]) == {
        "/health", "/ui/snapshot", "/accounting/agent/ask", "/trading/agent/ask",
    }, c.get("/openapi.json").json()["paths"].keys()

    assert hermes_cli.timeout_of(accounting.CONFIG) == 60

    # session_id는 stderr에서 뽑는다. 없으면 None이지 빈 문자열이 아니다
    assert hermes_cli.session_id_of("\nsession_id: abc123\n") == "abc123"
    assert hermes_cli.session_id_of("") is None

    print("ok - BFF 7개 영역 점검 통과")
