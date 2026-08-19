"""quant-api - 실험 제출·조회면 (FastAPI).

담당: 재일 (퀀트·백테스트본부 QNT)
근거: 재일님 계획 2026-08-04 "Quant API와 Experiment Registry",
      지시 "퀀트 쪽은 전략 공장이기 때문에 실험도구를 무조건적으로 보장해야 함"

▶ 왜 필요한가
  퀀트 기능이 전부 독립 스크립트라 다른 본부가 실험을 위임할 통로가 없다.
  헤르메스도, 리서치도, CEO 도 "이 가설 돌려봐" 를 말할 방법이 없고 사람이
  터미널에서 --run 을 쳐야 한다. **전략 공장에 주문 창구가 없는 셈이다.**

▶ "무조건 보장" 을 어떻게 지키는가
  ① **상태는 DB 에 있다.** 서버가 죽어도 실험과 결과가 사라지지 않는다.
     이 API 는 상태를 메모리에 두지 않는다 - 프로세스가 곧 진실이면
     재시작이 곧 유실이다.
  ② **긴 작업을 요청 안에서 돌리지 않는다.** 백테스트는 수 분이 걸리므로
     제출과 실행을 분리한다. 타임아웃으로 죽는 요청은 "실패했는지 도는지"
     를 알 수 없게 만든다.
  ③ **실패를 성공으로 위장하지 않는다.** 조회가 안 되면 빈 결과가 아니라
     사유가 있는 오류다.

▶ 이 API 가 하지 않는 것
  Production 승격. 전략 상태 변경. 다른 본부 판정.
  퀀트는 실험하고 **요청**할 뿐이다(release_gate·strategy_lifecycle 와 같은 경계).
  쓰기 엔드포인트는 가설 제출과 실험 실행 요청 둘뿐이고, 둘 다 quant 스키마
  안에서만 쓴다.

자체 점검: python departments/04-quant-backtest/api/quant_api.py
"""

from __future__ import annotations

import os
import sys
import hmac
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

API_VERSION = "quant-api-v1"
KST = timezone(timedelta(hours=9))

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE / "pipeline"))
sys.path.insert(0, str(_BASE / "contracts"))
sys.path.insert(0, str(_BASE.parent / "01-research" / "collectors"))

try:  # 자체 점검은 FastAPI 없이도 돈다
    from fastapi import FastAPI, HTTPException, Query
    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - 점검 경로
    _HAS_FASTAPI = False

    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, detail: str = ""):
            super().__init__(detail)
            self.status_code, self.detail = status_code, detail


def get_conn():
    """Return a write-capable connection for the quant control plane.

    Supabase's transaction pooler can hand the API a session whose default is
    read-only.  Job submission is a write boundary, so it must use the same
    explicit READ WRITE connection helper as the experiment workers.  A
    dedicated quant role can be selected by Compose without exposing either
    database secret to Hermes.
    """
    from db_writer import connect
    from source_registry import load_project_env

    return connect(load_project_env()["DATABASE_URL"], connect_timeout=10)


def is_authorized(header: str | None, token: str | None) -> bool:
    """Validate one fail-closed Bearer credential in constant time."""
    expected = str(token or "").strip()
    if (not expected or expected.startswith("CHANGE_ME_")
            or "${" in expected):
        return False
    parts = str(header or "").split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1].strip(), expected)


def _query(sql: str, params: tuple = ()) -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        conn.rollback()          # read-only 라도 트랜잭션은 닫는다
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


# ── 순수 함수 (자체 점검 대상) ──────────────────────────────────────────────

def validate_hypothesis_payload(payload: dict) -> tuple[bool, list[str]]:
    """제출 검증. **결정론이고 LLM 을 안 쓴다.**

    빈 가설을 받아주면 실험 목록만 늘고 아무도 못 돌린다.
    """
    problems: list[str] = []
    title = str(payload.get("title") or "").strip()
    if len(title) < 5:
        problems.append("title 이 너무 짧다(5자 이상)")

    edge = payload.get("expected_edge")
    if not isinstance(edge, dict) or not edge.get("type"):
        problems.append("expected_edge.type 이 없다 - 무엇을 노리는지 없으면 "
                        "실행 가능성을 판정할 수 없다")

    fals = payload.get("falsification_tests") or []
    if not isinstance(fals, list) or not fals:
        # ▶ 반증 조건 없는 가설은 틀릴 수 없고, 틀릴 수 없으면 검증이 아니다.
        #   리서치 Insight 계약과 같은 규율이다.
        problems.append("falsification_tests 가 비었다 - 반증 조건이 없는 "
                        "가설은 검증할 수 없다")

    dp = payload.get("required_data_products") or []
    if not isinstance(dp, list) or not dp:
        problems.append("required_data_products 가 비었다")
    return (not problems), problems


def job_state(row: dict) -> str:
    """실험 행 -> 사람이 읽는 상태. **모르면 UNKNOWN 이다.**"""
    st = str(row.get("status") or "").strip().upper()
    known = {"INTAKE", "PREREGISTERED", "DATASET_CERTIFIED", "RUNNING",
             "ROBUSTNESS_REVIEW", "SUPPORTED", "REJECTED", "INCONCLUSIVE",
             "NEEDS_DATA", "PROPOSED", "APPROVED", "TESTING", "ARCHIVED"}
    return st if st in known else "UNKNOWN"


def is_terminal(state: str) -> bool:
    """더 진행할 것이 없는가. RUNNING 을 종결로 보면 멈춘 작업을 못 찾는다."""
    return state in {"SUPPORTED", "REJECTED", "INCONCLUSIVE", "ARCHIVED"}


def stuck_jobs(rows: list[dict], *, now: datetime,
               max_hours: float = 6.0) -> list[dict]:
    """멈춘 실험. **전략 공장에서 조용히 멈춘 작업이 가장 나쁘다.**

    RUNNING 인 채 오래 있으면 프로세스가 죽었거나 예외가 삼켜진 것이다.
    상태만 보면 "돌고 있다" 로 읽히므로 시간을 같이 본다.
    """
    out = []
    for r in rows:
        if job_state(r) != "RUNNING":
            continue
        # ▶ **전이 시각을 쓴다**(2026-08-04 status_changed_at 신설).
        #   created_at 을 쓰면 "만들어진 지 오래됐다" 이지 "그 상태로 오래
        #   있었다" 가 아니라, 어제 만들어 방금 RUNNING 이 된 실험이 멈춘
        #   것으로 잡히고 오늘 만들어 3시간째 멈춘 것은 안 잡힌다.
        started = r.get("status_changed_at") or r.get("created_at")
        if not isinstance(started, datetime):
            continue
        age = (now - started).total_seconds() / 3600.0
        if age > max_hours:
            out.append({"hypothesis_id": str(r.get("hypothesis_id")),
                        "title": r.get("title"), "hours": round(age, 1)})
    return out


# ── FastAPI 표면 ────────────────────────────────────────────────────────────

if _HAS_FASTAPI:
    app = FastAPI(title="quant-api", version=API_VERSION,
                  description="퀀트 실험 제출·조회. 승격 권한 없음.")

    @app.middleware("http")
    async def require_control_plane_auth(request, call_next):
        # Container/liveness checks may inspect health.  Every data and job
        # surface is authenticated, including reads that disclose hypotheses,
        # experiment status, and metrics to sibling containers.
        if request.url.path == "/health":
            return await call_next(request)
        if not is_authorized(
                request.headers.get("authorization"),
                os.getenv("MCP_RESEARCH_API_KEY")):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                {"error": "unauthorized",
                 "detail": "Bearer control-plane credential required"},
                status_code=401,
            )
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        """DB 까지 확인한다. **프로세스가 살아 있는 것과 공장이 도는 것은 다르다.**"""
        try:
            _query("select 1 as ok")
            return {"ok": True, "version": API_VERSION}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"DB 연결 실패: {type(e).__name__}")

    @app.get("/hypotheses")
    def list_hypotheses(status: Optional[str] = Query(None),
                        limit: int = Query(50, gt=0, le=500)) -> list[dict]:
        """가설 목록. 상태로 거른다."""
        if status:
            return _query(
                """select hypothesis_id, title, status, created_at,
                          preregistered_at, material_fingerprint
                          , status_changed_at
                   from quant.hypotheses where status = %s
                   order by created_at desc limit %s""", (status.upper(), limit))
        return _query(
            """select hypothesis_id, title, status, created_at,
                      preregistered_at, material_fingerprint
               from quant.hypotheses order by created_at desc limit %s""",
            (limit,))

    @app.get("/hypotheses/{hypothesis_id}")
    def get_hypothesis(hypothesis_id: str) -> dict:
        rows = _query(
            """select hypothesis_id, title, status, expected_edge,
                      required_data_products, created_at,
                      preregistered_at, material_fingerprint
               from quant.hypotheses where hypothesis_id = %s::uuid""",
            (hypothesis_id,))
        if not rows:
            raise HTTPException(404, "가설이 없다")
        h = rows[0]
        h["exp"] = _query(
            """select experiment_id, input_hash, created_at
               from quant.experiments where hypothesis_id = %s::uuid
               order by created_at desc limit 10""", (hypothesis_id,))
        return h

    @app.get("/experiments/{experiment_id}/metrics")
    def experiment_metrics(experiment_id: str) -> dict:
        rows = _query(
            """select metric, value, unit, split, dimensions,
                      cost_model_version
               from quant.experiment_metrics
               where experiment_id = %s::uuid
               order by metric, split, dimensions::text""",
            (experiment_id,))
        if not rows:
            # ▶ 행 0 을 "지표 없음" 으로 조용히 넘기지 않는다. 실험이 없는
            #   것인지 지표만 없는 것인지 호출부가 알아야 한다.
            raise HTTPException(404, "실험이 없거나 지표가 아직 없다")
        # A metric name is not a primary key.  The ledger deliberately keeps
        # the same metric across TRAIN/TEST/STRESS and across dimensional
        # slices.  Collapsing this to ``{metric: value}`` silently overwrites
        # evidence (and can hide a bad fold behind a good one), so preserve one
        # response row per governed ledger row.
        return {
            "experiment_id": experiment_id,
            "metrics": [
                {
                    **r,
                    "value": (
                        float(r["value"])
                        if r.get("value") is not None else None
                    ),
                }
                for r in rows
            ],
        }

    @app.get("/jobs/stuck")
    def jobs_stuck(max_hours: float = Query(6.0, gt=0, le=168)) -> dict:
        """멈춘 실험. **전략 공장에서 조용히 멈춘 작업이 가장 나쁘다.**"""
        rows = _query(
            """select hypothesis_id, title, status, created_at,
                      status_changed_at
               from quant.hypotheses where status in ('RUNNING', 'TESTING')""")
        return {"max_hours": max_hours,
                "stuck": stuck_jobs(rows, now=datetime.now(timezone.utc),
                                    max_hours=max_hours)}

    @app.post("/jobs")
    def submit_job(hypothesis_id: str = Query(...),
                   requested_by: str = Query("unknown")) -> dict:
        """실험 주문 접수. **즉시 반환하고 실행은 워커가 한다.**

        백테스트는 수 분 걸린다 - 요청 안에서 돌리면 타임아웃으로 죽고,
        죽은 요청은 "실패했는지 아직 도는지" 를 알 수 없게 만든다.
        """
        from job_queue import enqueue

        conn = get_conn()
        try:
            r = enqueue(conn, hypothesis_id, requested_by=requested_by)
        finally:
            conn.close()
        if not r.get("accepted"):
            # ▶ 중복을 조용히 성공으로 넘기지 않는다 - 호출부가 새 주문이
            #   들어간 줄 알면 결과를 영원히 기다린다
            raise HTTPException(409, r.get("reason") or "접수 거부")
        return r

    @app.get("/jobs/{job_id}")
    def job_status(job_id: str) -> dict:
        rows = _query(
            """select job_id, hypothesis_id, status, attempts, max_attempts,
                      failure_reason, experiment_id, leased_by,
                      created_at, updated_at
               from quant.experiment_jobs where job_id = %s::uuid""",
            (job_id,))
        if not rows:
            raise HTTPException(404, "주문이 없다")
        return rows[0]

    @app.get("/jobs")
    def list_jobs(status: Optional[str] = Query(None),
                  limit: int = Query(50, gt=0, le=500)) -> list[dict]:
        if status:
            return _query(
                """select job_id, hypothesis_id, status, attempts,
                          failure_reason, experiment_id, created_at, updated_at
                   from quant.experiment_jobs where status = %s
                   order by created_at desc limit %s""",
                (status.upper(), limit))
        return _query(
            """select job_id, hypothesis_id, status, attempts,
                      failure_reason, experiment_id, created_at, updated_at
               from quant.experiment_jobs order by created_at desc limit %s""",
            (limit,))

    @app.get("/seeds")
    def research_seeds(limit: int = Query(20, gt=0, le=100)) -> dict:
        """리서치 인사이트에서 온 가설 씨앗. 읽기 전용."""
        from research_bridge import bridge

        conn = get_conn()
        try:
            return bridge(conn=conn, limit=limit)
        finally:
            conn.close()


# ── 자체 점검 ────────────────────────────────────────────────────────────────

_OK_PAYLOAD = {"title": "20일 모멘텀 상위 종목 추종",
               "expected_edge": {"type": "momentum", "horizon_days": 20},
               "falsification_tests": ["20일 내 초과수익이 음수면 폐기"],
               "required_data_products": ["krx-basket-daily/v2"]}


def _check_payload_validation():
    ok, probs = validate_hypothesis_payload(_OK_PAYLOAD)
    assert ok and not probs, probs
    # ▶ 반증 조건 없는 가설은 틀릴 수 없고, 틀릴 수 없으면 검증이 아니다
    bad = dict(_OK_PAYLOAD, falsification_tests=[])
    ok2, p2 = validate_hypothesis_payload(bad)
    assert not ok2 and any("반증" in x for x in p2), p2
    # 빈 제출을 받아주면 목록만 늘고 아무도 못 돌린다
    ok3, p3 = validate_hypothesis_payload({})
    assert not ok3 and len(p3) >= 4, p3


def _check_unknown_state_is_unknown():
    """모르는 상태를 아는 척하지 않는다."""
    assert job_state({"status": "RUNNING"}) == "RUNNING"
    assert job_state({"status": "듣보"}) == "UNKNOWN"
    assert job_state({}) == "UNKNOWN"
    # RUNNING 은 종결이 아니다 - 종결로 보면 멈춘 작업을 못 찾는다
    assert not is_terminal("RUNNING")
    assert is_terminal("REJECTED") and is_terminal("INCONCLUSIVE")


def _check_stuck_detection():
    """**조용히 멈춘 작업이 가장 나쁘다** - 상태만 보면 도는 것처럼 읽힌다."""
    now = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
    rows = [
        {"hypothesis_id": "a", "title": "오래됨", "status": "RUNNING",
         "status_changed_at": now - timedelta(hours=9)},
        {"hypothesis_id": "b", "title": "정상", "status": "RUNNING",
         "status_changed_at": now - timedelta(hours=1)},
        {"hypothesis_id": "c", "title": "끝남", "status": "SUPPORTED",
         "status_changed_at": now - timedelta(hours=99)},
        # ▶ **전이 시각이 created_at 보다 우선한다.** 어제 만들어 방금
        #   RUNNING 이 된 실험을 멈춘 것으로 잡으면 경보가 소음이 된다.
        {"hypothesis_id": "e", "title": "어제생성 방금전이",
         "status": "RUNNING",
         "created_at": now - timedelta(hours=30),
         "status_changed_at": now - timedelta(minutes=10)},
    ]
    st = stuck_jobs(rows, now=now, max_hours=6.0)
    assert [x["hypothesis_id"] for x in st] == ["a"], st
    assert st[0]["hours"] == 9.0
    # 시각을 모르면 멈춤으로 단정하지 않는다 - 없는 근거로 경보를 내지 않는다
    assert stuck_jobs([{"hypothesis_id": "d", "status": "RUNNING"}],
                      now=now) == []


def _check_no_promotion_surface():
    """**승격·상태변경 엔드포인트가 없는가.** 퀀트는 요청만 한다."""
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    banned = ("promote", "approve", "activate", "deploy")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith(("_check_", "test_")):
                continue
            assert not any(b in node.name.lower() for b in banned), node.name
    # ▶ 다른 스키마에 쓰는 SQL 이 없어야 한다(우리 것은 quant.* 다).
    #   **소스 문자열 검색을 쓰지 않는다** - 검사에 적은 패턴 자체가 잡힌다.
    #   오늘만 세 번째 같은 실패다(method_performance, release_gate, 여기).
    #   AST 로 문자열 리터럴만 보되 이 검사 함수 안의 것은 제외한다.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith(("_check_", "test_")):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                low = sub.value.lower()
                for schema in ("strategy.", "research.", "market."):
                    for verb in ("update ", "insert into ", "delete from "):
                        assert verb + schema not in low,                             f"{node.name}: 남의 스키마에 쓴다 ({verb}{schema})"


def _check_state_not_in_memory():
    """**상태를 메모리에 두지 않는가.** 프로세스가 곧 진실이면 재시작이 유실이다."""
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and isinstance(
                        node.value, (ast.Dict, ast.List, ast.Set)):
                    assert t.id.startswith("_") or t.id.isupper(), \
                        f"모듈 수준 가변 상태: {t.id}"


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{API_VERSION} 자체 점검 (DB·네트워크 없음)")
    _check_payload_validation();     print("  제출 검증              OK")
    _check_unknown_state_is_unknown(); print("  미지 상태 = UNKNOWN     OK")
    _check_stuck_detection();        print("  멈춘 작업 탐지          OK")
    _check_no_promotion_surface();   print("  승격 표면 부재          OK")
    _check_state_not_in_memory();    print("  상태 비메모리           OK")
    print(f"quant-api 5개 영역 통과. FastAPI: {'있음' if _HAS_FASTAPI else '없음'}")
