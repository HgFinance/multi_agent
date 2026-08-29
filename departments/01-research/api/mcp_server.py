#!/usr/bin/env python3
"""리서치본부 MCP 서버 - Hermes 가 부서를 실제로 돌리게 하는 다리.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-02 "헤르메스 써서 효과를 보려던 건데 혼란오네
      → 일단 시작해서 하나씩 개선해보자".
      계획서 3.3 "본부 간 호출은 HTTP", Hermes `mcp add --url` (HTTP/SSE 지원 실측)

▶ 이 다리가 없으면 헤르메스는 '대화만 되는 껍데기'다
  분석 실체는 자율 연구실의 결정론 검증기와 명시적으로 등록된 LangGraph
  직원이며, 헤르메스는 부서 인터페이스·기억·위임 계층이다. 퇴역한 종목별
  Research Packet 및 전략공장 파이프라인은 이 MCP 표면과 Runtime 이미지에
  포함하지 않는다.

▶ 노출 원칙 (권한 경계)
  - **읽기 도구가 기본이다.** 리서치본부는 주문·리스크 판정·원장에 관여하지
    않는다(config.yaml forbidden_tools). 여기 없는 것은 호출할 수 없다.
  - 이 서버는 전략·주문·승격을 쓰지 않는다. 자율 연구실이 만드는 산출물은
    별도 lab 경계에서 검증되며, Agent Decision != Order (CLAUDE.md)다.
  - 직원 LLM 작업은 수 분이 걸릴 수 있다. MCP 호출이 그동안 묶이면 대화가
    죽으므로 **비동기 시작 + 조회** 두 도구로 나눈다.

▶ 정직성
  - 도구는 결과를 요약하지 않는다. 결정론 산출물(판정·수치·근거)을 그대로
    돌려주고 해석은 호출자(페르소나)가 한다 - 중간에서 요약하면 그 자체가
    검증되지 않은 서술 계층이 하나 더 생긴다.
  - 실패는 실패로 돌려준다. 빈 결과로 위장하지 않는다.

실행
  python api/mcp_server.py            # 자체 점검 (네트워크 없음)
  python api/mcp_server.py --serve    # MCP 서버 (기본 0.0.0.0:8037/mcp)
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BASE.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_BASE))
sys.path.insert(0, str(_BASE / "collectors"))
sys.path.insert(0, str(_BASE / "evidence"))
sys.path.insert(0, str(_BASE.parent / "04-quant-backtest" / "pipeline"))

from stock_universe import governed_stock_evidence_sql

logger = logging.getLogger(__name__)

_RESEARCH_EVIDENCE_PACKAGE = "_research_mcp_evidence"
_RESEARCH_EVIDENCE_LOCK = threading.RLock()


def _research_evidence_module(name: str):
    """Load this department's evidence package without a global-name collision.

    Several departments have an ``evidence`` directory.  Importing the bare
    package therefore depends on test/import order and can silently bind the MCP
    server to another department's namespace package.  A private package alias
    keeps the research evidence modules and their relative imports deterministic.
    """
    qualified = f"{_RESEARCH_EVIDENCE_PACKAGE}.{name}"
    with _RESEARCH_EVIDENCE_LOCK:
        cached = sys.modules.get(qualified)
        if cached is not None:
            return cached
        if _RESEARCH_EVIDENCE_PACKAGE not in sys.modules:
            package_dir = _BASE / "evidence"
            spec = importlib.util.spec_from_file_location(
                _RESEARCH_EVIDENCE_PACKAGE,
                package_dir / "__init__.py",
                submodule_search_locations=[str(package_dir)],
            )
            if spec is None or spec.loader is None:
                raise ImportError("research evidence package could not be loaded")
            package = importlib.util.module_from_spec(spec)
            sys.modules[_RESEARCH_EVIDENCE_PACKAGE] = package
            try:
                spec.loader.exec_module(package)
            except Exception:
                sys.modules.pop(_RESEARCH_EVIDENCE_PACKAGE, None)
                raise
        return importlib.import_module(qualified)

MCP_VERSION = "research-mcp-v1"
KST = timezone(timedelta(hours=9))
DEFAULT_PORT = int(os.environ.get("RESEARCH_MCP_PORT", "8037"))
ACTIVE_MARKET_COLLECTOR_JOB_NAMES = frozenset({
    "market-archive",
    "universe-restrictions",
    "data-steward",
    "retention",
    "breadth",
    "derivatives",
    "vkospi",
    "style-index",
    "calendar-observed",
    "market-cap-universe",
    "label-snapshot",
    "chart-daily-universe",
})

# 진행 중·완료된 활성 직원 작업은 프로세스 메모리에서만 제한적으로 보존한다.
# 종목별 Packet 작업·리포트 경로와는 연결하지 않는다.
MAX_WORKER_JOBS_KEPT = 50
_GOVERNED_MCP_EVIDENCE = governed_stock_evidence_sql(
    experiment_alias="e", dataset_alias="m", hypothesis_alias="h")

# Retained read-only SQL fixture for historical contract tests. The old
# Historical outcome SQL is retained only for audit/tests; no legacy tool is live.
_SQL_FACTORY_OUTCOMES = """
    select o.experiment_id::text, o.trial_family_id, o.decision,
           o.lesson_codes, o.oos_summary, coalesce(o.notes, '') as notes,
           o.created_at
      from research.v_current_experiment_outcomes o
      join quant.experiments e on e.experiment_id::text = o.experiment_id
      join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
      join quant.dataset_manifests m on m.dataset_id = e.dataset_id
     where """ + _GOVERNED_MCP_EVIDENCE + """
     order by o.created_at desc limit %s"""

_SQL_LIBRARY_SIGNAL_SHELF = """
    select s.edge_type,
           count(*) as experiments,
           count(*) filter (where s.decision like 'REJECT%') as rejects,
           max(s.information_ratio) filter (
             where coalesce(s.turnover_total, 1) <> 0) as best_ir,
           max(s.signal_ic_t) as best_ic_t,
           max(s.deflated_sharpe) filter (
             where coalesce(s.turnover_total, 1) <> 0) as best_dsr,
           min(s.max_drawdown_pct) filter (
             where coalesce(s.turnover_total, 1) <> 0) as worst_mdd,
           max(s.decided_at) as last_decided,
           array_agg(distinct s.top_n) filter (
             where s.top_n is not null) as top_n_tried
      from research.v_experiment_scorecard s
      join quant.experiments e on e.experiment_id::text = s.experiment_id
      join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
      join quant.dataset_manifests m on m.dataset_id = e.dataset_id
     where s.edge_type <> ''
       and """ + _GOVERNED_MCP_EVIDENCE + """
     group by s.edge_type
     order by coalesce(max(s.signal_ic_t), -99) desc,
              coalesce(max(s.information_ratio), -99) desc"""

_SQL_LIBRARY_FAMILIES = """
    with governed as (
      select o.*
        from research.v_current_experiment_outcomes o
        join quant.experiments e on e.experiment_id::text = o.experiment_id
        join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
        join quant.dataset_manifests m on m.dataset_id = e.dataset_id
       where o.trial_family_id is not null
         and """ + _GOVERNED_MCP_EVIDENCE + """
    ), last_out as (
      select distinct on (trial_family_id)
             outcome_id, trial_family_id, decision, decided_at, lesson_codes,
             root_cause, notes, oos_summary, experiment_id
        from governed
       order by trial_family_id, decided_at desc, outcome_id desc
    ), aggregate as (
      select outcome.trial_family_id,
             count(distinct outcome.outcome_id) as outcomes,
             count(distinct outcome.outcome_id) filter (
               where outcome.decision like 'REJECT%') as rejects,
             count(distinct outcome.outcome_id) filter (
               where outcome.decision = 'GATE_HOLD') as holds,
             count(distinct outcome.outcome_id) filter (
               where outcome.decision in
                 ('PROMOTED', 'SUPPORTED', 'SUBMIT_TO_QA')) as advanced,
             min(outcome.decided_at) as first_decided,
             max(outcome.decided_at) as last_decided,
             array_agg(distinct lesson.code) filter (
               where lesson.code is not null) as all_lessons
        from governed outcome
        left join lateral unnest(
          coalesce(outcome.lesson_codes, '{}'::text[])
        ) as lesson(code) on true
       group by outcome.trial_family_id
    )
    select aggregate.trial_family_id, aggregate.outcomes,
           aggregate.rejects, aggregate.holds, aggregate.advanced,
           latest.decision as last_decision,
           latest.root_cause as last_root_cause,
           latest.lesson_codes as last_lessons,
           coalesce(latest.notes, '') as last_note,
           aggregate.all_lessons, aggregate.first_decided,
           aggregate.last_decided
      from aggregate
      left join last_out latest
        on latest.trial_family_id = aggregate.trial_family_id
     where aggregate.trial_family_id <> ''
     order by aggregate.last_decided desc nulls last limit %s"""

_SQL_LIBRARY_SCORECARD = """
    select s.experiment_id, s.edge_type, s.top_n, s.decision, s.decided_at,
           s.excess_return_pct, s.information_ratio, s.max_drawdown_pct,
           s.deflated_sharpe, s.pbo, s.m2_excess_ann_pct, s.alpha_ann_pct,
           s.appraisal_ratio, s.strategy_ann_vol_pct,
           s.benchmark_ann_vol_pct, s.signal_ic, s.signal_ic_t,
           s.turnover_total, s.lesson_codes, s.root_cause,
           coalesce(s.notes, '') as notes, s.mapping_loss,
           coalesce(s.llm_model_id, '') as llm_model_id
      from research.v_experiment_scorecard s
      join quant.experiments e on e.experiment_id::text = s.experiment_id
      join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
      join quant.dataset_manifests m on m.dataset_id = e.dataset_id
     where """ + _GOVERNED_MCP_EVIDENCE

# 직원 Worker(LangGraph) 실행 작업. 퇴역한 종목 Packet 파이프라인과 연결하지 않는다.
_WORKER_JOBS: dict[str, dict] = {}
_WORKER_JOBS_LOCK = threading.Lock()
_TASK_ID_RE = re.compile(r"^(t_[0-9A-Za-z]+)(?:$|[-:#/])")
_SKEPTIC_WORKER_ID = "competing-explanation-worker"
_SKEPTIC_CODES = frozenset({
    "BETA_EXPOSURE",
    "LIQUIDITY_PREMIUM",
    "DATA_MINING",
    "COST_UNACCOUNTED",
})
_SKEPTIC_REVIEW_CONTRACT_VERSION = "research.skeptic-review.v2"


def _writer_connection(dsn: str, *, connector=None):
    """Open a connection whose transactions are explicitly READ WRITE.

    ``DATABASE_URL`` points at Supabase's transaction pooler (port 6543).  A
    read-only service can leave ``default_transaction_read_only=on`` on a
    pooled server connection, which may then be handed to this write-capable
    MCP surface.  Transaction characteristics belong on the client
    connection, not in a session-level ``SET`` that can leak back into the
    pool.
    """
    if connector is None:
        import psycopg2

        connector = psycopg2.connect
    conn = connector(dsn, connect_timeout=15)
    try:
        conn.set_session(readonly=False)
    except Exception:
        conn.close()
        raise
    return conn


def _db():
    from source_registry import load_project_env

    return _writer_connection(load_project_env()["DATABASE_URL"])


def _result_has_valid_skeptic_worker(result: dict | None) -> bool:
    """Check the already-validated in-memory result before coalescing a job."""

    value = result or {}
    if value.get("degraded") is True or _SKEPTIC_WORKER_ID not in value.get("executed", []):
        return False
    report = next(
        (item for item in value.get("workers", [])
         if item.get("worker_id") == _SKEPTIC_WORKER_ID),
        None,
    )
    output = (report or {}).get("output") or {}
    return bool(
        report
        and report.get("status") == "COMPLETED"
        and output.get("schema_valid") is True
        and isinstance(output.get("skeptic_reviews"), list)
        and output.get("skeptic_reviews")
    )


def _reusable_skeptic_job(payload: dict, proposal_draft: str) -> dict | None:
    """Return a running/validated exact-draft job to avoid duplicate Qwen work.

    Holding questions may be present in the same MCP request, so only a pure
    proposal run can be coalesced. The raw draft digest, not a caller label,
    is the identity key.
    """

    if not proposal_draft or "holding_question" in payload:
        return None
    digest = _text_digest(proposal_draft)
    with _WORKER_JOBS_LOCK:
        for _job_id, job in reversed(list(_WORKER_JOBS.items())):
            if job.get("proposal_draft_sha256") != digest:
                continue
            if "holding_question" in (job.get("payload_fields") or []):
                continue
            if job.get("status") == "RUNNING":
                return dict(job)
            if job.get("status") == "COMPLETED" and _result_has_valid_skeptic_worker(
                job.get("result")
            ):
                return dict(job)
    return None


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 순수 로직 (자체 점검 대상)
# ---------------------------------------------------------------------------

def is_authorized(header: str | None, token: str | None) -> bool:
    """Authorization 헤더 검증.

    - 토큰이 설정돼 있지 않으면 **누구나 통과**한다(개발 편의). 대신 기동 시
      경고를 찍어 '설정을 잊은 것'과 '일부러 연 것'이 구분되게 한다.
    - 설정돼 있으면 정확히 일치해야 한다. 비교는 상수 시간으로 한다 -
      토큰을 한 글자씩 맞춰보는 타이밍 공격을 막는다.
    - 빈 문자열 토큰은 '설정 안 함'과 같게 취급한다. Hermes 가 미치환
      `${MCP_RESEARCH_API_KEY}` 를 그대로 보내는 경우도 통과시키지 않는다.
    """
    import hmac

    if not token:
        return True
    if not header:
        return False
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1].strip(), token)


def _healthcheck_headers(token: str | None) -> dict[str, str]:
    """Use the server's configured bearer for its internal liveness probe."""

    normalized = (token or "").strip()
    if not normalized:
        return {}
    return {"Authorization": f"Bearer {normalized}"}


def build_app(server, *, token: str | None):
    """MCP Starlette 앱 + Bearer 검사 미들웨어."""
    from starlette.responses import JSONResponse

    app = server.streamable_http_app()

    class _Auth:
        """Pure ASGI auth wrapper that preserves streamable HTTP responses.

        BaseHTTPMiddleware uses an in-memory receive/send bridge. With MCP's
        long-lived GET stream, a client disconnect can close that bridge before
        ``call_next`` returns and Starlette raises ``No response returned``.
        Authentication does not need request rewriting, so a direct ASGI
        wrapper avoids that extra lifecycle and leaves streaming to FastMCP.
        """

        def __init__(self, app, *, token):
            self.app = app
            self.token = token

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http":
                await self.app(scope, receive, send)
                return
            headers = {
                key.decode("latin-1"): value.decode("latin-1")
                for key, value in scope.get("headers", ())
            }
            if not is_authorized(headers.get("authorization"), self.token):
                response = JSONResponse(
                    {"error": "unauthorized",
                     "detail": "MCP_RESEARCH_API_KEY 가 필요하다"},
                    status_code=401,
                )
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)

    # ── 종목 근거 REST 조회면 ────────────────────────────────────────
    # 뉴스·공시 자격은 여기에만 있다. 다른 서비스가 자격을 갖는 대신
    # 이 경로로 물어본다(market-api /levels 와 같은 패턴).
    async def holdings_evidence(request):
        symbol = request.path_params.get("symbol", "").strip()
        if not (len(symbol) == 6 and symbol.isalnum()):
            return JSONResponse({"error": "bad_symbol",
                                 "detail": "6자리 종목코드가 필요하다"},
                                status_code=400)
        import anyio
        from functools import partial
        include_price = request.query_params.get("include_price", "true").casefold() not in {
            "0", "false", "no",
        }
        include_company = request.query_params.get("include_company", "true").casefold() not in {
            "0", "false", "no",
        }

        try:
            evidence = await anyio.to_thread.run_sync(
                partial(
                    gather_holdings_evidence,
                    symbol,
                    include_price=include_price,
                    include_company=include_company,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 경계에서 사유를 남긴다
            return JSONResponse(
                {"error": "evidence_failed",
                 "detail": f"{type(exc).__name__}: {str(exc)[:160]}"},
                status_code=502)
        # 프롬프트 예산을 먹지 않게 필요한 것만 준다. 원문 좌표는 남긴다.
        return JSONResponse({
            "symbol": symbol,
            "company": evidence.get("company", ""),
            "sources": evidence.get("sources", {}),
            "news_headlines": (evidence.get("news_headlines") or [])[:8],
            "disclosures_7d": (evidence.get("disclosures_7d") or [])[:6],
            "price_levels": evidence.get("price_levels"),
            "price_context": evidence.get("price_context"),
        })

    app.router.add_route("/evidence/holdings/{symbol}", holdings_evidence,
                         methods=["GET"], name="holdings_evidence")

    async def ownership_scan(request):
        """매집 스캔 결과(캐시). 요청 시점에 새로 돌리지 않는다 - 84초짜리다."""
        # 이 모듈은 json 을 최상단에서 import 하지 않는다(다른 함수들도
        # 지역 import 를 쓴다). 스코프 밖이면 라우트가 500 이 된다.
        import asyncio
        import json
        import os
        import time as _time

        path = os.environ.get("OWNERSHIP_SCAN_PATH", "/tmp/ownership_scan.json")
        if not os.path.exists(path):
            return JSONResponse(
                {"status": "NO_SCAN",
                 "reason": "매집 스캔 결과가 없다. scan_ownership.py 를 먼저 돌릴 것"},
                status_code=200)
        age = int(_time.time() - os.path.getmtime(path))

        def _read_scan() -> dict:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)

        try:
            data = await asyncio.to_thread(_read_scan)
        except (OSError, ValueError) as exc:
            return JSONResponse(
                {"status": "UNREADABLE",
                 "reason": f"{type(exc).__name__}: {str(exc)[:120]}"},
                status_code=200)
        top = int(request.query_params.get("top", "6"))
        return JSONResponse({
            "status": "OK",
            "age_seconds": age,
            "window": data.get("window"),
            # 유형별로 나눠 준다. 지배주주 거래와 외부 기관 거래는 성격이 다르다.
            "by_institution": (data.get("by_institution") or [])[:top],
            "by_controlling": (data.get("by_controlling") or [])[:top],
            "by_holder": [b for b in (data.get("by_holder") or [])
                          if b.get("position_count", 0) >= 2][:5],
            "note": ("지분공시는 후행 지표다(5% 룰은 5영업일 내 보고). "
                     "'기관이 샀다'가 '오른다'는 뜻이 아니며 그 관계는 "
                     "측정하지 않았다."),
        })

    app.router.add_route("/evidence/ownership-scan", ownership_scan,
                         methods=["GET"], name="ownership_scan")

    app.add_middleware(_Auth, token=token)
    return app


class _LoopStallWatchdog:
    """Exit a wedged MCP process so Compose can restart it.

    The Docker healthcheck detects a blocked event loop, but Compose does not
    restart an otherwise-running unhealthy container. A separate thread is
    therefore used only as a process-safety fuse; normal MCP requests do not
    call Docker or any external service through this class.
    """

    def __init__(self, *, stall_seconds: float) -> None:
        self.stall_seconds = max(0.0, float(stall_seconds))
        self._heartbeat = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.stall_seconds <= 0:
            return
        self._thread = threading.Thread(
            target=self._monitor,
            name="research-mcp-loop-watchdog",
            daemon=True,
        )
        self._thread.start()

    def beat(self) -> None:
        self._heartbeat = time.monotonic()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _monitor(self) -> None:
        interval = min(5.0, max(1.0, self.stall_seconds / 4.0))
        while not self._stop.wait(interval):
            stalled_for = time.monotonic() - self._heartbeat
            if stalled_for <= self.stall_seconds:
                continue
            print(
                f"{MCP_VERSION}: event-loop stall for {stalled_for:.1f}s; "
                "exiting for container restart",
                flush=True,
            )
            os._exit(75)


def _loop_watchdog_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("RESEARCH_MCP_LOOP_STALL_SECONDS", "90")))
    except ValueError:
        return 90.0


def _with_loop_watchdog(app, *, stall_seconds: float):
    """Wrap an ASGI app with an event-loop heartbeat and fail-fast fuse."""

    import asyncio

    watchdog = _LoopStallWatchdog(stall_seconds=stall_seconds)

    class _WatchdogApp:
        async def __call__(self, scope, receive, send):
            if scope.get("type") != "lifespan":
                await app(scope, receive, send)
                return
            watchdog.start()
            heartbeat = asyncio.create_task(_heartbeat())
            try:
                await app(scope, receive, send)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                watchdog.stop()

    async def _heartbeat() -> None:
        while True:
            watchdog.beat()
            await asyncio.sleep(1.0)

    return _WatchdogApp()


def normalize_symbol(symbol: str) -> str:
    """KRX 6자리 종목코드만 받는다. 지어내거나 추측하지 않는다."""
    s = str(symbol or "").strip()
    if not (len(s) == 6 and s.isdigit()):
        raise ValueError(f"종목코드는 6자리 숫자여야 한다 (받은 값: {symbol!r})")
    return s


def _text_digest(value: str) -> str:
    """Stable fingerprint for binding a review job to the submitted draft."""
    normalized = str(value or "").replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def register_worker_job(payload_fields: list[str], *, now: datetime,
                        symbol: str = "", proposal_draft: str = "") -> str:
    job_id = uuid.uuid4().hex[:12]
    with _WORKER_JOBS_LOCK:
        _WORKER_JOBS[job_id] = {
            "job_id": job_id, "kind": "employee_workers",
            "payload_fields": payload_fields, "symbol": symbol or None,
            # Keep only a digest: enough for causal validation without copying a
            # potentially long unpublished proposal into the job status surface.
            "proposal_draft_sha256": (
                _text_digest(proposal_draft)
                if "proposal_draft" in payload_fields else None),
            "status": "RUNNING",
            "started_at": now.isoformat(), "ended_at": None,
            "result": None, "model_plane": None, "evidence": None, "error": None,
        }
        if len(_WORKER_JOBS) > MAX_WORKER_JOBS_KEPT:
            # RUNNING 은 퇴거하지 않는다 - 몇 분짜리 실행 도중에 자리가
            # 사라지면 GPU 를 태운 결과가 어디에도 안 남는다(2026-08-13 리뷰).
            # RUNNING 만으로 상한을 넘는 폭주는 그대로 남긴다 - 곧 끝나고,
            # 다음 등록 때 완료분부터 치워진다.
            evictable = [k for k, v in _WORKER_JOBS.items()
                         if v.get("status") != "RUNNING"]
            for k in evictable[:len(_WORKER_JOBS) - MAX_WORKER_JOBS_KEPT]:
                _WORKER_JOBS.pop(k, None)
    return job_id


def build_tiered_answer(evidence: dict | None, result: dict | None,
                        *, now: datetime) -> dict | None:
    """근거 등급이 붙은 사용자 답변. 실패해도 job 을 죽이지 않는다.

    조립기 정본은 `departments/01-research/evidence/answer_builder.py` 다 -
    배치 추천 경로와 **같은 모듈**을 써야 같은 종목에 같은 문장이 나간다.
    """
    if not evidence or not evidence.get("symbol"):
        return None
    try:
        answer_builder = _research_evidence_module("answer_builder")
        return answer_builder.from_holdings_evidence(
            evidence, as_of=now.date().isoformat(), worker_result=result)
    except Exception as exc:  # noqa: BLE001 - 답변 조립 실패가 결과를 못 죽인다
        return {"status": "FAILED",
                "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}


def finish_worker_job(job_id: str, *, result: dict | None,
                      model_plane: dict | None, error: str | None,
                      now: datetime, evidence: dict | None = None) -> dict:
    with _WORKER_JOBS_LOCK:
        j = _WORKER_JOBS.get(job_id)
        if j is None:
            # 자리가 사라진 완료 - 결과를 조용히 버리지 않고 로그에 남긴다
            print(f"⚠ worker job {job_id} 의 자리가 사라져 결과를 버린다 "
                  f"(error={error!r})", file=sys.stderr)
            return {}
        # degraded=True 여도 작업 자체는 끝났다 - FAILED 는 registry 실행이
        # 예외로 죽은 경우만이다. degraded 는 결과 안에 그대로 보인다.
        j["status"] = "FAILED" if error else "COMPLETED"
        j["ended_at"] = now.isoformat()
        j["result"] = result
        j["model_plane"] = model_plane
        j["evidence"] = evidence
        j["tiered_answer"] = build_tiered_answer(evidence, result, now=now)
        j["error"] = error
        return dict(j)


def planner_task_id(planner_run: str) -> str | None:
    """Return the causal board task id, ignoring an agent-added run label."""
    match = _TASK_ID_RE.match(str(planner_run or "").strip())
    return match.group(1) if match else None


def verified_skeptic_reviews(skeptic_run: str,
                             planner_text: str) -> tuple[list[dict], str]:
    """Return typed reviews from the causally bound independent Worker job.

    A caller-provided label is not a signature.  The worker job is created and
    retained by this MCP process, so the proposal boundary can verify the
    actual execution, its trigger input, and its structured output before
    allowing the id to enter the ledger as ``skeptic_sign``.  The returned
    artifact, rather than agent-transcribed prose, is the canonical hand-off.
    """
    job_id = str(skeptic_run or "").strip()
    if not job_id:
        return [], "skeptic_run is required and must be a run_research_workers job_id"
    with _WORKER_JOBS_LOCK:
        job = _WORKER_JOBS.get(job_id)
        if job is not None:
            job = dict(job)
    if job is None:
        return [], "skeptic_run does not identify a worker job in this MCP process"
    if job.get("status") != "COMPLETED":
        return [], f"skeptic worker is not completed: {job.get('status')}"
    if "proposal_draft" not in (job.get("payload_fields") or []):
        return [], "skeptic worker did not review a proposal_draft"
    if job.get("proposal_draft_sha256") != _text_digest(planner_text):
        return [], "skeptic worker reviewed a different proposal_draft"
    result = job.get("result") or {}
    if result.get("degraded"):
        return [], "skeptic worker completed in degraded mode"
    if "competing-explanation-worker" not in (result.get("executed") or []):
        return [], "competing-explanation-worker did not complete"
    report = next(
        (item for item in (result.get("workers") or [])
         if item.get("worker_id") == "competing-explanation-worker"),
        None,
    )
    output = (report or {}).get("output") or {}
    if ((report or {}).get("status") != "COMPLETED"
            or output.get("schema_valid") is not True
            or not str(output.get("summary") or "").strip()):
        return [], "skeptic worker output is missing or schema-invalid"
    reviews = output.get("skeptic_reviews")
    if not isinstance(reviews, list) or not reviews:
        return [], "skeptic worker typed artifact skeptic_reviews is missing"
    required = ("title", "competing_explanation", "competing_codes",
                "verdict", "falsification_test")
    if any(not isinstance(review, dict)
           or any(review.get(field) in (None, "", []) for field in required)
           for review in reviews):
        return [], "skeptic worker typed artifact is incomplete"
    planner_titles = [match.group(1).strip() for match in re.finditer(
        r"(?m)^\s*TITLE\s*:\s*(.+?)\s*$", str(planner_text or ""))
        if match.group(1).strip()]
    if not planner_titles:
        return [], "proposal_draft has no TITLE for skeptic review pairing"
    if len(planner_titles) == 1 and len(reviews) == 1:
        # TITLE is an identifier, not analytical content.  Small local models
        # sometimes paraphrase it even when the review body is valid.  With a
        # one-to-one causal input the only safe mapping is unambiguous, so use
        # the source title.  Never guess positionally when there are many.
        normalized = [dict(reviews[0])]
        normalized[0]["title"] = planner_titles[0]
        return normalized, ""
    review_titles = [str(review.get("title") or "").strip() for review in reviews]
    if len(review_titles) != len(planner_titles) or sorted(review_titles) != sorted(planner_titles):
        return [], "skeptic review TITLE set does not exactly match proposal_draft"
    return reviews, ""


def skeptic_job_error(skeptic_run: str, planner_text: str) -> str:
    """Compatibility wrapper used by diagnostics and older callers."""
    _reviews, error = verified_skeptic_reviews(skeptic_run, planner_text)
    return error


def render_skeptic_reviews(reviews: list[dict]) -> str:
    """Deterministically bridge typed Worker output to proposal-intake blocks."""
    blocks = []
    for review in reviews:
        one_line = lambda value: " ".join(str(value).split())
        explanation = one_line(review["competing_explanation"])
        falsification = one_line(review["falsification_test"])
        codes = ", ".join(one_line(code) for code in review["competing_codes"])
        blocks.append("\n".join([
            f"TITLE: {one_line(review['title'])}",
            f"COMPETING_EXPLANATION: {explanation} FALSIFICATION_TEST: {falsification}",
            f"COMPETING_CODES: {codes}",
            f"VERDICT: {one_line(review['verdict']).upper()}",
        ]))
    return "\n\n".join(blocks)


def load_cached_skeptic_reviews(
    conn, planner_text: str
) -> tuple[list[dict], dict | None, str]:
    """Load a complete, exact-draft review cache before starting a Worker.

    The table is written only after ``verified_skeptic_reviews`` succeeds, so
    this is a reuse path for validated artifacts, not a trust shortcut. A
    partial or malformed cache is a miss and must never be joined into a new
    review.
    """

    digest = _text_digest(planner_text)
    with conn.cursor() as cur:
        cur.execute(
            """
            select title, competing_explanation, competing_codes, verdict,
                   falsification_test, planner_run, skeptic_run, created_at
             from research.proposal_review_outcomes
             where proposal_draft_sha256 = %s
               and review_contract_version = %s
             order by title
        """,
            (digest, _SKEPTIC_REVIEW_CONTRACT_VERSION),
        )
        rows = cur.fetchall()
    if not rows:
        return [], None, "no exact-draft skeptic cache"

    planner_titles = [
        match.group(1).strip()
        for match in re.finditer(
            r"(?m)^\s*TITLE\s*:\s*(.+?)\s*$", str(planner_text or "")
        )
        if match.group(1).strip()
    ]
    reviews = [
        {
            "title": str(row[0] or "").strip(),
            "competing_explanation": str(row[1] or "").strip(),
            "competing_codes": list(row[2] or []),
            "verdict": str(row[3] or "").strip().upper(),
            "falsification_test": str(row[4] or "").strip(),
        }
        for row in rows
    ]
    review_titles = [review["title"] for review in reviews]
    valid = bool(planner_titles) and (
        len(review_titles) == len(planner_titles)
        and len(set(review_titles)) == len(review_titles)
        and sorted(review_titles) == sorted(planner_titles)
        and all(
            review["competing_explanation"]
            and review["competing_codes"]
            and set(review["competing_codes"]) <= _SKEPTIC_CODES
            and review["verdict"] in {"PROCEED", "STOP"}
            and review["falsification_test"]
            for review in reviews
        )
    )
    if not valid:
        return [], None, "exact-draft skeptic cache is incomplete or invalid"
    metadata = {
        "cache_key": digest,
        "source_skeptic_runs": sorted({str(row[6]) for row in rows}),
        "source_planner_runs": sorted({str(row[5]) for row in rows}),
        "created_at": max(str(row[7]) for row in rows),
    }
    return reviews, metadata, ""


def _cached_skeptic_result(
    planner_text: str, reviews: list[dict], metadata: dict
) -> dict:
    """Build the same non-binding worker envelope from a verified cache."""

    digest = _text_digest(planner_text)
    report = {
        "worker_id": _SKEPTIC_WORKER_ID,
        "role": "Competing explanation and falsification analyst",
        "tools": ["research.outcomes.read", "research.evidence.search"],
        "status": "COMPLETED",
        "attempts": 0,
        "output": {
            "worker_id": _SKEPTIC_WORKER_ID,
            "summary": "Reused a previously verified skeptic review for the exact proposal draft.",
            "confidence": 1.0,
            "evidence_refs": [f"proposal_draft_sha256:{digest}"],
            "escalate": False,
            "schema_valid": True,
            "skeptic_reviews": reviews,
            "cache_hit": True,
        },
        "error": None,
        "output_contract": "research.worker-context.v1",
        "input_hash": digest,
        "cache_hit": True,
    }
    return {
        "runtime": {
            "executor": "verified_skeptic_cache",
            "topology": "cached_non_binding_worker_context",
            "provider": "cache",
            "model": "none",
        },
        "workers": [report],
        "executed": [_SKEPTIC_WORKER_ID],
        "failed": [],
        "not_executed": ["holdings-analyst-worker"],
        "degraded": False,
        "input_hash": digest,
        "binding": False,
        "cache_hit": True,
        "cache": metadata,
    }


def load_persisted_skeptic_reviews(conn, skeptic_run: str,
                                    planner_run: str,
                                    planner_text: str) -> tuple[list[dict], str]:
    """Recover a previously verified review after an MCP process restart.

    Rows enter this ledger only after ``verified_skeptic_reviews`` has checked
    the live worker job.  Binding the recovery to the exact draft digest and
    both run ids preserves that causal signature without making process memory
    a single point of failure.
    """
    digest = _text_digest(planner_text)
    with conn.cursor() as cur:
        cur.execute("""
            select title, competing_explanation, competing_codes, verdict,
                   falsification_test
             from research.proposal_review_outcomes
             where proposal_draft_sha256 = %s
               and review_contract_version = %s
               and skeptic_run = %s
               and planner_run = %s
             order by title
        """, (digest, _SKEPTIC_REVIEW_CONTRACT_VERSION,
              str(skeptic_run or "").strip(),
              str(planner_run or "").strip()))
        rows = cur.fetchall()
    if not rows:
        return [], "no durable skeptic review matches this draft and run binding"
    reviews = [{
        "title": row[0],
        "competing_explanation": row[1],
        "competing_codes": list(row[2] or []),
        "verdict": row[3],
        "falsification_test": row[4],
    } for row in rows]
    planner_titles = [match.group(1).strip() for match in re.finditer(
        r"(?m)^\s*TITLE\s*:\s*(.+?)\s*$", str(planner_text or ""))
        if match.group(1).strip()]
    review_titles = [str(review["title"] or "").strip() for review in reviews]
    if not planner_titles or sorted(planner_titles) != sorted(review_titles):
        return [], "durable skeptic review TITLE set does not match proposal_draft"
    return reviews, ""


def _review_blocks(planner_text: str) -> list[dict[str, str]]:
    """Extract only the fields needed to bind a skeptic review.

    This MCP boundary must not import the retired proposal-intake contract just
    to persist a review.  Full proposal parsing belongs to the autonomous lab;
    the Worker review ledger only needs exact TITLE/LEAD_IDS pairing.
    """
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in str(planner_text or "").splitlines():
        key, separator, value = raw_line.partition(":")
        if not separator:
            continue
        normalized = key.strip().upper()
        if normalized == "TITLE":
            if current is not None:
                blocks.append(current)
            current = {"TITLE": value.strip()}
        elif normalized == "LEAD_IDS" and current is not None:
            current["LEAD_IDS"] = value.strip()
    if current is not None:
        blocks.append(current)
    return blocks


def _review_lead_ids(value: str) -> list[str]:
    return [item for item in re.split(r"[,;\\s]+", str(value or "").strip())
            if item]


def persist_skeptic_reviews(conn, planner_text: str, reviews: list[dict], *,
                            case_id: str, planner_run: str,
                            skeptic_run: str, known_lead_ids: set[str]) -> int:
    """Persist causally verified reviews even when STOP blocks publication."""

    blocks = _review_blocks(planner_text)
    if not blocks or not reviews:
        return 0
    by_title = {str(r.get("title") or "").strip(): r for r in reviews}
    singleton = reviews[0] if len(blocks) == len(reviews) == 1 else None
    draft_digest = _text_digest(planner_text)
    written = 0
    with conn.cursor() as cur:
        for block in blocks:
            title = str(block.get("TITLE") or "").strip()
            review = by_title.get(title) or singleton
            if not title or review is None:
                continue
            lead_ids = sorted(lead_id for lead_id in _review_lead_ids(
                block.get("LEAD_IDS", "")) if lead_id in known_lead_ids)
            if not lead_ids:
                continue
            review_id = "review_" + hashlib.sha256(
                f"{draft_digest}|{title}".encode()
            ).hexdigest()[:16]
            cur.execute("""
                insert into research.proposal_review_outcomes
                  (review_id, case_id, lead_ids, title, proposal_draft_sha256,
                   verdict, competing_explanation, competing_codes,
                   falsification_test, planner_run, skeptic_run,
                   review_contract_version)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (proposal_draft_sha256, title) do nothing
                returning review_id
            """, (
                review_id, case_id, lead_ids, title, draft_digest,
                str(review["verdict"]).upper(),
                str(review["competing_explanation"]),
                list(review["competing_codes"]),
                str(review["falsification_test"]), planner_run, skeptic_run,
                _SKEPTIC_REVIEW_CONTRACT_VERSION,
            ))
            written += int(cur.fetchone() is not None)
    conn.commit()
    return written


def build_worker_payload(holding_question: str = "", proposal_draft: str = "",
                         portfolio_state: str = "", news: str = "") -> dict:
    """Worker 트리거 payload. 빈 인자는 싣지 않는다.

    employee_worker_runtime.should_run 은 trigger 필드가 비어 있지 않을 때만
    Worker 를 돌린다 - 빈 문자열을 실으면 '트리거됐는데 근거가 빈' 워커가
    생겨 DEGRADED 로 보인다. 트리거가 하나도 없으면 ValueError 다 -
    아무도 안 도는 실행을 RUNNING 으로 위장하지 않는다.
    """
    payload = {k: v for k, v in {
        "holding_question": holding_question.strip(),
        "proposal_draft": proposal_draft.strip(),
        "portfolio_state": portfolio_state.strip(),
        "news": news.strip(),
    }.items() if v}
    if not ({"holding_question", "proposal_draft"} & set(payload)):
        raise ValueError(
            "holding_question 또는 proposal_draft 중 하나는 있어야 한다 - "
            "리서치 Worker 2인의 트리거가 그 둘뿐이다")
    return payload


# ── Evidence First (FINAL_RUNTIME_ARCHITECTURE §11) ─────────────────────────
# Runner 는 Worker 를 부르기 전에 Tool/Evidence 부터 모은다 - Worker 가 근거
# 없이 추론하지 않게 하기 위해서다. 소스(뉴스·공시·가격)별로 **독립 시도**하고
# 실패는 status 로 정직하게 남긴다. 뉴스·공시는 저장 DB 를 읽지 않고 요청 시점
# MCP 소스를 조회한다. 가격만 market-api/Timescale 읽기를 유지한다.

HOLDINGS_EVIDENCE_PERSONA = "holdings-analyst-worker"


def _evidence_getter():
    """시장 가격 읽기면 호출자. X-Agent-Persona 를 워커 명의로 싣는다.

    research-api 가 TOOL_GATEWAY_ENFORCE=true 로 돌면 persona 없는 요청은
    403 이다 - holdings-analyst-worker 는 snapshot/bars 스코프를 가진 persona 다
    (hermes/config.yaml tool_allowlist).
    """
    get_json = _research_evidence_module("api_client").get_json

    def get(url: str):
        return get_json(url, persona=HOLDINGS_EVIDENCE_PERSONA, timeout=10)

    return get


def _on_demand_evidence_id(tool: str, citation: str, item: int,
                           native_id: str = "") -> str:
    """요청 시점 응답 해시와 항목 좌표를 인용 가능한 ID 로 묶는다."""
    coordinate = native_id.strip() or f"item-{item}"
    return f"mcp:{tool}:{citation}:{coordinate}"


def gather_holdings_evidence(symbol: str, *, get=None, search_news=None,
                             search_disclosures=None,
                             include_price: bool = True,
                             include_company: bool = True) -> dict:
    """보유 질문용 근거를 요청 시점 소스에서 모은다. 소스별 독립·정직 보고.

    뉴스는 최신 10건, 공시는 최근 7일을 직접 조회한다. 각 응답의 비영속 citation
    해시와 항목 번호/native ID 를 같은 응답에 묶어 짧은 ref(n1/d1)가 실제 조회
    좌표로 해소되게 한다. 값이 비면 빈 대로 싣는다 - "없다" 는 사실이고,
    지어내는 것보다 낫다. ``get`` 은 가격 market-api 에만 사용한다.
    """
    sources: dict[str, dict] = {}
    out: dict = {"symbol": symbol, "sources": sources}

    # 회사명은 기본 DART 경로에서만 보강한다. 테스트/호출자가 공시 조회기를
    # 주입한 경우에는 이미 독립적인 소스 계약을 받은 것이므로, 부가적인
    # corpCode.xml 요청으로 그 계약을 지연시키거나 외부 네트워크를 다시
    # 호출하지 않는다.
    if include_company and search_disclosures is None:
        try:
            from external_sources import _resolve as _resolve_corp

            hits = _resolve_corp(symbol)
            if hits:
                out["company"] = hits[0].get("corp_name") or symbol
        except Exception:  # noqa: BLE001, S110 - 이름은 부가정보다
            pass

    try:
        if search_news is None:
            from external_sources import news_search as search_news_call
        else:
            search_news_call = search_news
        response = search_news_call(query=symbol, display=10, sort="date")
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            raise TypeError("news_search response must contain an items list")
        news = response["items"]
        citation = str(response.get("citation") or "").strip()
        if not citation:
            raise ValueError("news_search response has no citation hash")
        observed_at = str(
            response.get("searched_at") or datetime.now(KST).isoformat())
        # ref('n1'..)는 LLM 용 짧은 좌표, evidence_id/citation_item 은 코드가
        # 같은 요청 응답에서 정확한 항목을 찾을 진짜 좌표다.
        out["news_headlines"] = [
            {"ref": f"n{i + 1}",
             "evidence_id": _on_demand_evidence_id(
                 "news_search", citation, i + 1),
             "citation": citation, "citation_item": i + 1,
             "title": n.get("title"), "relation": "on_demand_query",
             "url": n.get("originallink") or n.get("link"),
             "published_at": str(n.get("pubDate") or ""),
             "observed_at": observed_at}
            for i, n in enumerate(news)]
        sources["news"] = {"status": "OK", "count": len(news),
                           "mode": "ON_DEMAND_MCP", "citation": citation}
    except Exception as e:  # noqa: BLE001 - 소스 하나의 실패가 전체를 못 죽인다
        sources["news"] = {"status": "FAILED",
                           "mode": "ON_DEMAND_MCP",
                           "reason": f"{type(e).__name__}: {e}"}

    try:
        if search_disclosures is None:
            from external_sources import (
                dart_search_disclosures as search_disclosures_call,
            )
        else:
            search_disclosures_call = search_disclosures
        response = search_disclosures_call(corp=symbol, days=7, page=1)
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            raise TypeError(
                "dart_search_disclosures response must contain an items list")
        disc = response["items"][:5]
        citation = str(response.get("citation") or "").strip()
        if not citation:
            raise ValueError(
                "dart_search_disclosures response has no citation hash")
        observed_at = datetime.now(KST).isoformat()
        out["disclosures_7d"] = [
            {"ref": f"d{i + 1}",
             "evidence_id": _on_demand_evidence_id(
                 "dart_search_disclosures", citation, i + 1,
                 str(d.get("rcept_no") or "")),
             "citation": citation, "citation_item": i + 1,
             "title": d.get("report_nm"), "url": d.get("viewer_url"),
             "published_at": str(d.get("rcept_dt") or ""),
             "observed_at": observed_at}
            for i, d in enumerate(disc)]
        sources["disclosures"] = {
            "status": "OK", "count": len(disc), "mode": "ON_DEMAND_MCP",
            "citation": citation}
    except Exception as e:  # noqa: BLE001
        sources["disclosures"] = {"status": "FAILED",
                                  "mode": "ON_DEMAND_MCP",
                                  "reason": f"{type(e).__name__}: {e}"}

    if not include_price:
        # The request-time news projection does not consume price data.  Do
        # not run the two market reads below merely because the richer
        # holdings endpoint can also serve price context.
        return out

    # ── 가격 레벨 ────────────────────────────────────────────────────
    # 목표가·손절가는 **숫자**라서 LLM 이 지어내면 근거를 검증할 수 없다.
    # market-api 가 일봉에서 결정론으로 계산한 값을 실어 준다. 계산 규칙은
    # 재현되지만 수익 보장이 아니라서 caveat 를 같이 싣는다(실측: 목표
    # 선도달 31.0% vs 손절 선도달 62.1%).
    try:
        if get is None:
            get = _evidence_getter()
        base = os.environ.get("MARKET_API_URL", "http://127.0.0.1:8036").rstrip("/")
        lv = get(f"{base}/levels/{symbol}")
        if not isinstance(lv, dict) or "status" not in lv:
            raise TypeError("levels 응답 형식이 아니다")
        # 응답이 크면 워커 프롬프트 예산을 먹는다 - 필요한 것만 남긴다.
        out["price_levels"] = {
            "status": lv.get("status"),
            "last_close": lv.get("last_close"),
            "atr": lv.get("atr"),
            "supports": [{"price": x.get("price"), "touches": x.get("touches")}
                         for x in (lv.get("supports") or [])[:3]],
            "resistances": [{"price": x.get("price"), "touches": x.get("touches")}
                            for x in (lv.get("resistances") or [])[:3]],
            "entry_low": lv.get("entry_low"), "entry_high": lv.get("entry_high"),
            "target": lv.get("target"), "stop": lv.get("stop"),
            "reward_risk": lv.get("reward_risk"), "risk_pct": lv.get("risk_pct"),
            "target_basis": lv.get("target_basis"), "stop_basis": lv.get("stop_basis"),
            "reason": lv.get("reason"),
            "evidence_tier": "DERIVED",
            "caveat": lv.get("caveat"),
        }
        sources["price_levels"] = {"status": lv.get("status"),
                                   "source": "market-api /levels"}
    except Exception as e:  # noqa: BLE001 - 한 소스 실패가 전체를 못 죽인다
        sources["price_levels"] = {"status": "FAILED",
                                   "reason": f"{type(e).__name__}: {e}"}

    try:
        evidence_bundle = _research_evidence_module("bundle")
        if get is None:
            get = _evidence_getter()
        # fetch_price_context 는 실패를 스스로 UNAVAILABLE 로 기술한다 -
        # TSDB 가 빈 환경에서도 요청 시점 뉴스·공시와 독립적으로 동작한다.
        ctx = evidence_bundle.fetch_price_context(symbol, get=get)
        out["price_context"] = ctx
        sources["price_context"] = {"status": ctx.get("status", "UNKNOWN")}
    except Exception as e:  # noqa: BLE001
        sources["price_context"] = {"status": "FAILED",
                                    "reason": f"{type(e).__name__}: {e}"}
    return out


# Worker 프롬프트 예산. employee_worker_runtime._compact 가 tool_output 직렬화를
# **문자 단위 raw[:8000]** 으로 자른다 - 넘치면 JSON 이 중간에서 끊겨 '수집은
# OK 인데 수치는 없는' 오정보가 된다(2026-08-13 리뷰 실측: 뉴스 10건+공시 5건+
# price_context ≈ 5,400~7,000자, 본부장이 3KB 텍스트를 얹으면 절벽을 넘는다).
# 어댑터 래핑(worker_id·tools 키) 오버헤드 몫을 빼고 7,200자를 상한으로 잡아
# **항목 단위로** 덜어낸다 - 덜어낸 사실은 evidence_truncated 로 남긴다.
_EVIDENCE_CHAR_BUDGET = 7200
_USER_TEXT_CAP = 1000


def _clip_text(value, limit: int = _USER_TEXT_CAP):
    s = str(value)
    return s if len(s) <= limit else s[:limit] + "…(잘림)"


def merge_holdings_evidence(payload: dict, evidence: dict) -> dict:
    """수집한 근거를 Worker input_fields(news·portfolio_state)에 접어 넣는다.

    holdings-analyst-worker 의 tool 어댑터는 payload 투영이라, 여기 안 실린
    근거는 Worker 에게 영원히 안 보인다. 사용자가 준 텍스트는 user_note/
    user_state 로 보존한다(단 1,000자 캡) - 덮어쓰면 질문에 실어준 맥락이
    사라지고, 캡이 없으면 본부장이 실은 긴 텍스트가 가격 블록을 통째로
    밀어낸다. 전체 직렬화가 예산을 넘으면 오래된 뉴스·공시부터 항목 단위로
    덜어내고 그 사실을 evidence_truncated 에 남긴다.
    """
    import json

    p = dict(payload)
    # closes(일봉 21개 리스트)는 요약 통계(last_close·수익률)와 중복인 원자료라
    # 프롬프트에서 뺀다 - 수치 근거는 price_context 의 요약 필드가 이미 든다.
    ctx = evidence.get("price_context")
    if isinstance(ctx, dict) and "closes" in ctx:
        ctx = {k: v for k, v in ctx.items() if k != "closes"}
    news_block = {
        "user_note": _clip_text(p["news"]) if p.get("news") else None,
        "headlines": list(evidence.get("news_headlines") or []),
        "disclosures_7d": list(evidence.get("disclosures_7d") or []),
        "source_status": evidence.get("sources"),
    }
    p["news"] = {k: v for k, v in news_block.items() if v}
    state_block = {
        "user_state": (_clip_text(p["portfolio_state"])
                       if p.get("portfolio_state") else None),
        "price_context": ctx,
        # 지지·저항·목표·손절. **이걸 안 실으면 Worker 가 목표가를 스스로
        # 지어낸다** - 사용자가 목표가를 물으면 LLM 은 근거 없이도 숫자를
        # 답한다(2026-08-25 실측: payload 에 price_levels 가 없어 답변에
        # 목표가가 아예 빠졌다). 서버가 일봉에서 결정론으로 계산한 값을
        # 여기 실어야 Worker 는 **설명만** 하게 된다.
        "price_levels": evidence.get("price_levels"),
    }
    merged_state = {k: v for k, v in state_block.items() if v}
    if merged_state:
        p["portfolio_state"] = merged_state

    def _size(d: dict) -> int:
        # _compact 와 같은 직렬화 규칙으로 잰다(ensure_ascii=False, sort_keys)
        return len(json.dumps(d, ensure_ascii=False, sort_keys=True, default=str))

    news_dict = p.get("news") if isinstance(p.get("news"), dict) else None
    dropped = 0
    while news_dict and _size(p) > _EVIDENCE_CHAR_BUDGET:
        if news_dict.get("headlines"):
            news_dict["headlines"].pop()       # 뒤(오래된 것)부터 통째로
        elif news_dict.get("disclosures_7d"):
            news_dict["disclosures_7d"].pop()
        else:
            break
        dropped += 1
    if dropped:
        news_dict["evidence_truncated"] = {
            "dropped_items": dropped,
            "reason": f"Worker 프롬프트 예산({_EVIDENCE_CHAR_BUDGET}자) 초과",
        }
    # 항목을 다 덜어냈는데도 넘으면(사용자 텍스트가 지배하는 경우) 텍스트를
    # 단계적으로 더 죈다 - 예산 보장을 _compact 의 문자 절단에 맡기지 않는다.
    if _size(p) > _EVIDENCE_CHAR_BUDGET:
        if news_dict and news_dict.get("user_note"):
            news_dict["user_note"] = _clip_text(news_dict["user_note"], 300)
        ps = p.get("portfolio_state")
        if isinstance(ps, dict) and ps.get("user_state"):
            ps["user_state"] = _clip_text(ps["user_state"], 300)
    for key in ("holding_question", "proposal_draft"):
        if _size(p) > _EVIDENCE_CHAR_BUDGET and isinstance(p.get(key), str):
            p[key] = _clip_text(p[key], 3000)
    return p


def summarize_health(rows: list[dict]) -> dict:
    """Separate current health from retained 24-hour failure history.

    ``bad_24h`` is an audit counter, not the current state.  Treating any
    recovered failure as a live outage kept the reference desk unhealthy for
    a full day after a successful retry.  The latest status is authoritative
    for current health; historical failures remain visible as recovery history.
    """
    healthy_statuses = {"OK", "SKIP"}
    bad = [
        r
        for r in rows
        if str(r.get("last_status") or "").strip().upper()
        not in healthy_statuses | {""}
    ]
    recovered = [
        r
        for r in rows
        if (r.get("bad_24h") or 0) > 0 and r not in bad
    ]
    return {
        "jobs_seen_24h": len(rows),
        "jobs_failing": len(bad),
        "jobs_with_failures_24h": sum(
            1 for row in rows if (row.get("bad_24h") or 0) > 0
        ),
        "healthy": not bad,
        "failing": [{"job": r["job_name"], "failures_24h": r["bad_24h"],
                     "last_status": r.get("last_status"),
                     "last_ok_at": str(r.get("last_ok_at")) if r.get("last_ok_at") else None,
                     "last_error": (r.get("last_error_tail") or "")[:200]}
                    for r in sorted(bad, key=lambda x: -(x.get("bad_24h") or 0))],
        "recovered_or_skipped": [
            {
                "job": r["job_name"],
                "failures_24h": r["bad_24h"],
                "last_status": r.get("last_status"),
                "last_ok_at": (
                    str(r.get("last_ok_at")) if r.get("last_ok_at") else None
                ),
            }
            for r in sorted(
                recovered, key=lambda x: -(x.get("bad_24h") or 0)
            )
        ],
    }


# ---------------------------------------------------------------------------
# MCP 서버
# ---------------------------------------------------------------------------

def _server_class():
    """MCP SDK 진입 클래스. 버전에 따라 이름이 다르다 - 둘 다 받는다.

    실측 2026-08-02: 이미지에는 mcp 1.29.0(FastMCP)이 깔렸는데, 같은 명령을
    호스트에서 돌렸을 때는 최신판(MCPServer)이 왔다. pydantic 핀이 리졸버를
    다른 버전으로 민 것이다. 우리가 쓰는 표면(생성·tool·streamable-http)은
    양쪽이 같으므로 import 만 관용적으로 처리하고 동작은 하나로 둔다.
    """
    try:
        from mcp.server.mcpserver import MCPServer  # 신판
        return MCPServer, "mcpserver"
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # 1.x
        return FastMCP, "fastmcp"


# ── 응대 창구(도서관) 면에서 빼는 도구 (2026-08-13, 도서관/연구소 분리) ────
# **프롬프트로 금지하는 게 아니라 등록에서 뺀다** - OWASP LLM06(Excessive
# Agency)의 도구 최소화, capability 원칙(건네지 않은 권한은 우회 불가).
# 창구는 조회·설명 전용이고, 상태를 바꾸거나 파이프라인을 낳는 손은
# 실험대(부서 본체) 프로필에만 있다.
LIAISON_EXCLUDED_TOOLS = frozenset({
    "run_research_workers", "worker_model_health"})

# Keep the deny-list as a fail-closed audit guard while historical callers are
# migrated. These names are removed from every live MCP surface before return;
# no autonomous session can discover or invoke them.
RETIRED_FACTORY_TOOLS = frozenset({
    "factory_brief",
    "factory_submit_leads",
    "factory_generate_formula_population",
    "factory_submit_evolved_formulas",
    "factory_submit_proposal",
    "factory_outcomes",
})


def _remove_registered_tools(server, names: frozenset[str]) -> set[str]:
    """Remove named tools and verify the SDK no longer advertises them."""
    import asyncio

    for name in sorted(names):
        remove = getattr(server, "remove_tool", None)
        if callable(remove):
            try:
                remove(name)
                continue
            except Exception as exc:  # noqa: BLE001 - private fallback below is audited
                logger.debug(
                    "mcp_tool_remove_fallback name=%s error=%s",
                    name,
                    type(exc).__name__,
                )
        tm = getattr(server, "_tool_manager", None)
        tools = getattr(tm, "_tools", None)
        if isinstance(tools, dict):
            tools.pop(name, None)
    advertised = {tool.name for tool in asyncio.run(server.list_tools())}
    return advertised & names


def _retire_factory_surface(server) -> None:
    leaked = _remove_registered_tools(server, RETIRED_FACTORY_TOOLS)
    if leaked:
        raise RuntimeError(
            f"retired factory tools remain advertised: {sorted(leaked)}"
        )


def _restrict_to_liaison(server) -> None:
    """등록된 도구에서 쓰기 면을 **제거**한다. 검증 불능이면 기동 거부(fail-closed).

    지우는 API 가 SDK 판마다 달라 두 경로를 시도하고, 마지막에 list_tools 로
    실제 표면을 재확인한다 - "지웠다고 믿었는데 남아 있는" 것이 최악의 상태라
    재확인이 최종 판정이다.
    """
    leaked = _remove_registered_tools(server, LIAISON_EXCLUDED_TOOLS)
    if leaked:
        raise RuntimeError(
            f"liaison 면에서 제한 도구를 제거하지 못했다: {sorted(leaked)} - "
            f"창구가 제한 capability를 가진 채 뜨는 것은 금지다(기동 거부)")


def build_server(*, host: str = "0.0.0.0", port: int = DEFAULT_PORT,
                 surface: str = "full"):
    cls, flavor = _server_class()
    kwargs = {"instructions": None}
    # 신판만 title 을 받는다 - 옛판에 넘기면 TypeError 다
    if flavor == "mcpserver":
        kwargs["title"] = "리서치본부"
    else:
        kwargs.update(host=host, port=port)      # 1.x 는 생성 시 settings 로 받는다

    liaison = str(surface).strip().lower() == "liaison"
    instructions = (
        "리서치본부 응대 창구(도서관)다. 조회·설명 전용 - 실험을 만들거나 "
        "연구 상태를 변경하지 않으며, 그 도구 자체가 이 면에 없다. 사용자 "
        "질의에는 조회 도구의 결과만으로 답하고, 새 분석·실험·수집이 필요한 "
        "요청은 즉석 수행 대신 '연구소 격상 필요'를 명시해 접수 사실만 답한다. "
        "도구 결과는 결정론 산출물이므로 그대로 인용하고, 수치를 다시 계산하거나 "
        "지어내지 않는다.") if liaison else (
        "리서치본부(RES)의 도구 면이다. 시세·요청형 Evidence 조회와 자율 연구실 "
        "작업만 한다. 주문 제출·리스크 판정·원장 기록은 이 본부의 권한이 "
        "아니며 여기에 도구도 없다. 도구 결과는 결정론 산출물이므로 그대로 "
        "인용하고, 수치를 다시 계산하거나 지어내지 않는다.")
    server = cls(
        name="research-liaison" if liaison else "research-department",
        **{k: v for k, v in kwargs.items() if k != "instructions"},
        instructions=instructions,
    )

    # ── 직원 Worker 실행 (LangGraph runner) ──────────────────────────────
    # ▶ 지금까지 run_employee_workers 를 부르는 손은 파이썬 오케스트레이터
    #   (paper_pipeline, portfolio_recommendation)뿐이었다 - 본부장(Hermes)이
    #   자기 부서 Worker 를 돌릴 간선이 없었다. 이 도구가 그 간선이다.
    # ▶ Worker 는 Worker Model Gateway(departments/worker_model_gateway.py)를
    #   통해 모델을 부른다. AWS 에서는 vLLM Qwen2.5-14B FP8, DEV 에서는
    #   기존 Ollama 로 자동 해석된다. Worker 산출은 언제나 비구속
    #   worker-context 다 - 주문·판정·원장에 닿지 않는다.
    def _gateway_module():
        """repo 루트를 sys.path 에 얹고 gateway 모듈만 준다 (경량, stdlib).

        컨테이너에서 repo 루트는 /app 이다. worker_model_gateway.py 등은
        이미지에 없고 docker-compose.model.yml 이 마운트한다 - 없으면
        ImportError 가 그대로 실패로 남는다(조용한 성공 위장 금지).
        """
        repo_root = _BASE.parent.parent
        for p in (str(repo_root), str(_BASE.parent)):
            if p not in sys.path:
                sys.path.insert(0, p)
        import worker_model_gateway as gateway  # departments/ 에서 온다
        return gateway

    def _worker_modules():
        """(gateway, employee_workers). 후자는 langgraph 를 끌어와 무겁다 -
        health 처럼 gateway 만 필요한 자리는 _gateway_module 을 쓴다."""
        gateway = _gateway_module()
        import employee_workers  # _BASE 에서 온다
        return gateway, employee_workers

    @server.tool(
        name="run_research_workers",
        description="리서치 직원 Worker(LangGraph 2인)를 실행한다. "
                    "holding_question 을 주면 holdings-analyst-worker, "
                    "proposal_draft 를 주면 competing-explanation-worker 가 돈다 "
                    "(둘 다 주면 둘 다 돈다). **보유 질문이 특정 종목에 관한 "
                    "것이면 symbol(6자리 종목코드)을 반드시 함께 줘라** - 러너가 "
                    "Worker 호출 전에 부서 읽기면(research-api·market-api)에서 "
                    "최근 뉴스·공시·가격 근거를 모아 Worker 에게 준다(Evidence "
                    "First). symbol 없이 주는 portfolio_state·news 텍스트는 "
                    "보조 근거로만 실린다. 모델 추론이 수십 초 걸리므로 job_id "
                    "만 즉시 돌려준다 - get_worker_job 으로 결과(worker-context, "
                    "비구속)를 조회한다. Worker 산출은 요약하지 말고 summary·"
                    "confidence·evidence_refs·escalate 를 그대로 인용하고, "
                    "job 의 evidence.sources 에 FAILED 가 있으면 그 사실도 "
                    "함께 보고하라.")
    def run_research_workers(holding_question: str = "", proposal_draft: str = "",
                             portfolio_state: str = "", news: str = "",
                             symbol: str = "") -> dict:
        try:
            payload = build_worker_payload(holding_question, proposal_draft,
                                           portfolio_state, news)
            sym = normalize_symbol(symbol) if str(symbol or "").strip() else ""
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        reusable = _reusable_skeptic_job(
            payload, payload.get("proposal_draft", "")
        )
        if reusable is not None:
            return {
                "job_id": reusable["job_id"],
                "status": reusable["status"],
                "payload_fields": sorted(payload),
                "symbol": sym or None,
                "evidence_first": False,
                "cache_hit": True,
                "coalesced": reusable["status"] == "RUNNING",
                "note": "동일 proposal_draft_sha256의 검증된 Worker 결과를 재사용한다",
            }
        job_id = register_worker_job(
            sorted(payload), now=datetime.now(KST), symbol=sym,
            proposal_draft=payload.get("proposal_draft", ""))

        def _run():
            # try 밖에서 초기화한다 - 근거를 모은 뒤 Worker 단계에서 죽어도
            # FAILED job 에 evidence 요약이 남아야 진단이 된다.
            evidence_summary = None
            try:
                # 제출 후 원장에 보존된 exact-draft review는 이미
                # verified_skeptic_reviews를 통과한 산출물이다. 같은 원문을
                # 다시 생성하지 않고 새 job envelope에 재수화한다.
                if "proposal_draft" in payload and "holding_question" not in payload:
                    cache_conn = None
                    try:
                        cache_conn = _db()
                        cached_reviews, cache_metadata, cache_error = (
                            load_cached_skeptic_reviews(
                                cache_conn, payload["proposal_draft"]
                            )
                        )
                    except Exception as cache_exc:  # noqa: BLE001 - cache is optional
                        cached_reviews, cache_metadata = [], None
                        cache_error = f"cache_lookup:{type(cache_exc).__name__}"
                    finally:
                        if cache_conn is not None:
                            cache_conn.close()
                    if cached_reviews and cache_metadata is not None:
                        cached_result = _cached_skeptic_result(
                            payload["proposal_draft"],
                            cached_reviews,
                            cache_metadata,
                        )
                        finish_worker_job(
                            job_id,
                            result=cached_result,
                            model_plane={
                                "cache_hit": True,
                                "cache_key": cache_metadata["cache_key"],
                                "source_skeptic_runs": cache_metadata[
                                    "source_skeptic_runs"
                                ],
                            },
                            error=None,
                            now=datetime.now(KST),
                            evidence={
                                "skeptic_cache": "hit",
                                "cache_key": cache_metadata["cache_key"],
                            },
                        )
                        return
                    if cache_error and cache_error != "no exact-draft skeptic cache":
                        evidence_summary = {
                            "skeptic_cache": "miss_or_unavailable",
                            "reason": cache_error,
                        }
                gateway, employee_workers = _worker_modules()
                # Evidence First (§11) - Worker 를 부르기 전에 근거부터 모은다.
                # 실패해도 job 을 죽이지 않는다 - 소스별 상태가 evidence 에
                # 그대로 남고, Worker 는 모인 만큼의 근거로 판단한다.
                if sym and "holding_question" in payload:
                    evidence = gather_holdings_evidence(sym)
                    payload_with_evidence = merge_holdings_evidence(
                        payload, evidence)
                    evidence_summary = {"symbol": sym,
                                        "sources": evidence.get("sources")}
                else:
                    payload_with_evidence = payload
                # ▶ 워커별로 binding 을 해석한다 (2026-08-13 리뷰 반영).
                #   단일 llm 을 공유하면 registry 의 worker→adapter 해석이
                #   전달되지 않아 LoRA 승격이 조용한 no-op 이 된다.
                worker_bindings: dict[str, dict] = {}

                def llm_factory(worker_id: str):
                    llm, binding = gateway.llm_for_worker(worker_id)
                    worker_bindings[worker_id] = binding.as_metadata()
                    return llm

                result = employee_workers.run_employee_workers(
                    payload_with_evidence, llm_factory=llm_factory)
                default_binding = gateway.resolve()
                # runtime 블록의 provider/model 은 공용 런타임이 Ollama 를
                # 하드코딩한 값이다 - 실제 사용한 게이트웨이 좌표로 바로잡는다.
                # 안 그러면 본부장이 '이 분석은 ollama qwen3:1.7b' 라는 틀린
                # 계보를 그대로 인용한다.
                runtime = dict(result.get("runtime") or {})
                runtime["provider"] = default_binding.provider
                runtime["model"] = default_binding.model
                runtime["model_source"] = "worker_model_gateway"
                result["runtime"] = runtime
                finish_worker_job(job_id, result=result,
                                  model_plane={
                                      "default": default_binding.as_metadata(),
                                      "workers": worker_bindings,
                                  },
                                  error=None, now=datetime.now(KST),
                                  evidence=evidence_summary)
            except Exception as e:  # noqa: BLE001 - 실패를 실패로 남긴다
                finish_worker_job(job_id, result=None, model_plane=None,
                                  error=f"{type(e).__name__}: {e}",
                                  now=datetime.now(KST),
                                  evidence=evidence_summary)

        threading.Thread(target=_run, daemon=True).start()
        return {"job_id": job_id, "status": "RUNNING",
                "payload_fields": sorted(payload),
                "symbol": sym or None,
                "evidence_first": bool(sym and "holding_question" in payload),
                "note": "get_worker_job(job_id) 으로 결과를 조회한다"}

    @server.tool(
        name="get_worker_job",
        description="run_research_workers 가 만든 작업의 상태·결과를 조회한다. "
                    "COMPLETED 여도 result.degraded 가 true 면 일부 Worker 가 "
                    "실패한 것이다 - 그 사실을 숨기지 말고 그대로 보고하라.")
    def get_worker_job(job_id: str) -> dict:
        with _WORKER_JOBS_LOCK:
            j = _WORKER_JOBS.get(str(job_id).strip())
        if j is None:
            return {"error": "그런 job_id 가 없다(서버 재시작 시 메모리가 "
                             "비워진다). run_research_workers 로 다시 시작하라"}
        return dict(j)

    @server.tool(
        name="worker_model_health",
        description="Worker 모델 서빙 상태를 확인한다 - 어떤 게이트웨이 좌표로 "
                    "해석되는지, 모델 서버가 실제로 응답하는지, registry 에 켜진 "
                    "adapter 가 실제 서빙 목록에 있는지. Worker 실행이 계속 "
                    "실패하면 먼저 이걸 본다.")
    async def worker_model_health() -> dict:
        # async + to_thread: sync 도구는 이벤트 루프에서 그대로 돌아, vLLM 이
        # 멎어 있으면 urlopen(timeout=10) 동안 서버의 모든 MCP 세션이 함께
        # 멎는다(2026-08-13 리뷰). 진단 도구가 서버를 세우면 안 된다.
        import asyncio

        def _probe() -> dict:
            import urllib.request as _rq

            try:
                gateway = _gateway_module()   # 경량 - langgraph 를 안 끌어온다
                binding = gateway.resolve()
                enabled = gateway.enabled_adapters()
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "stage": "resolve",
                        "error": f"{type(e).__name__}: {e}"}
            meta = binding.as_metadata()
            req = _rq.Request(binding.base_url + "/models",
                              headers={"Authorization": f"Bearer {binding.api_key}"})
            try:
                with _rq.urlopen(req, timeout=10) as r:
                    import json as _json
                    served = [m.get("id")
                              for m in _json.loads(r.read()).get("data", [])]
            except Exception as e:  # noqa: BLE001 - 죽었으면 죽었다고 말한다
                return {"ok": False, "stage": "server", "binding": meta,
                        "error": f"{type(e).__name__}: {e}"}
            # registry 에 켜진 adapter 가 서빙 목록에 없으면 그 워커 호출은
            # 실패한다 - 불일치를 여기서 표면화한다
            missing = {w: a for w, a in enabled.items() if a not in served}
            notes = []
            if binding.model not in served:
                notes.append(f"서버는 살아 있는데 '{binding.model}' 이 서빙 목록에 없다")
            for w, a in missing.items():
                notes.append(f"registry 는 {w}→{a} 를 켰는데 서버에 그 adapter 가 없다")
            return {"ok": binding.model in served and not missing,
                    "binding": meta, "served_models": served,
                    "registry_enabled_adapters": enabled,
                    "note": "; ".join(notes) or None}

        return await asyncio.to_thread(_probe)

    @server.tool(
        name="collector_health",
        description="24시간 시장 수집 Job 건강 상태. 지금 무엇이 고장나 있는지 "
                    "알려준다(SKIP=휴장 등 의도된 미수집은 고장이 아니다).")
    def collector_health() -> dict:
        return summarize_health(_rows("""
            select job_name, runs_24h, ok_24h, skip_24h, bad_24h,
                   last_ok_at, last_status, last_error_tail
            from research.collector_health
            where job_name = any(%s)""",
            (sorted(ACTIVE_MARKET_COLLECTOR_JOB_NAMES),)))

    @server.tool(
        name="analyst_calibration",
        description="분석가 판정별 사후 성과(선순환 되먹임). 표본이 적으면 "
                    "숫자를 믿지 않는다 - n 을 반드시 함께 본다.")
    def analyst_calibration() -> list[dict]:
        return _rows("""
            select node, verdict, horizon_days, n, avg_forward_return_pct,
                   median_forward_return_pct, pct_positive
            from research.analyst_calibration
            order by n desc, node limit 50""")

    # Strategy generation and proposal intake are owned by the autonomous lab.
    # This MCP server intentionally exposes no legacy factory tools or imports.
    # ── Library 조회 면 (2026-08-14) ────────────────────────────────────────
    # ▶ 왜 필요했나 (코드 실측)
    #   과거 실험은 원장에 계속 쌓였는데 **읽을 면이 없었다** - legacy 테이블 위
    #   view 0건, BFF/UI 참조 0건, 창구는 최근 결과 덤프 하나뿐이었다.
    #   하나뿐. 그래서 "저변동 계열은 어디까지 갔나" 같은 질문에 답할 수
    #   없었고, 사용자 질의가 Library 를 먼저 읽는 구조가 성립하지 않았다.
    #
    # ▶ 전부 읽기 전용이라 창구(liaison) 면에도 그대로 열린다. 계산은
    #   뷰(research.v_*)가 하고 여기서는 꺼내기만 한다 - 도구가 자기 산수를
    #   하면 원장과 다른 두 번째 진실이 생긴다.

    @server.tool(
        name="library_signal_shelf",
        description="신호 서가 - 엣지 유형별로 몇 번 시험했고 최고 IR·IC t·DSR 이 "
                    "얼마였나. **부품(단일 신호)의 천장을 한눈에 본다.** "
                    "IC t 의 부호를 반드시 함께 읽어라: 음수인데 절대값이 크면 "
                    "신호가 없는 게 아니라 방향이 반대라는 뜻이다.")
    def library_signal_shelf() -> list[dict]:
        return _rows(_SQL_LIBRARY_SIGNAL_SHELF)

    @server.tool(
        name="library_families",
        description="계열 현황 - 같은 아이디어를 몇 번 시도했고 마지막 판정이 "
                    "무엇이며 어떤 교훈이 쌓였나. 새 기획 전에 읽으면 이미 산 "
                    "실험을 다시 사지 않는다.")
    def library_families(limit: int = 15) -> list[dict]:
        n = max(1, min(int(limit), 60))
        return _rows(_SQL_LIBRARY_FAMILIES, (n,))

    @server.tool(
        name="library_scorecard",
        description="실험 성적표 - 관문 지표(IR·초과·DSR·MDD)에 더해 위험조정 "
                    "계측(M²·alpha·appraisal·전략vol/벤치vol)과 부품 채점표"
                    "(IC·IC t·회전율)를 한 행으로 준다. `edge_type` 을 주면 그 "
                    "유형만. **명목 초과가 나쁜데 M² 가 다르면 그것은 신호의 "
                    "실패가 아니라 위험 수준 차이다.**")
    def library_scorecard(edge_type: str = "", limit: int = 20) -> list[dict]:
        n = max(1, min(int(limit), 60))
        et = str(edge_type or "").strip().lower()
        cond = " and s.edge_type = %s" if et else ""
        params = ((et, n) if et else (n,))
        sql = (
            _SQL_LIBRARY_SCORECARD + cond
            + " order by s.decided_at desc limit %s"
        )
        return _rows(sql, params)

    # 외부 정보원(DART·네이버·ECOS·FRED) 질의 도구 - 정성 데이터의 MCP 검색 통합
    # (재일 결정 2026-08-13, docs/02-engineering/MCP_ONDEMAND_ARCHITECTURE.md).
    # 별도 모듈인 이유: 예산·비영속 인용 해시·정직성 규약을 한 파일에서 감사한다.
    from external_global import register_global_tools
    from external_macro import register_macro_tools
    from external_sources import register_external_tools
    register_external_tools(server)
    register_macro_tools(server)
    register_global_tools(server)

    _retire_factory_surface(server)
    if liaison:
        _restrict_to_liaison(server)
    return server


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _check_symbol_guard():
    assert normalize_symbol("000660") == "000660"
    assert normalize_symbol("  000660 ") == "000660"
    for bad in ("66", "00066O", "", None, "0006601"):
        try:
            normalize_symbol(bad)
            raise AssertionError(f"잘못된 코드가 통과했다: {bad!r}")
        except ValueError:
            pass
    print("  종목코드 가드            OK")


def _check_worker_payload():
    p = build_worker_payload(holding_question="  삼성전자 실적?  ",
                             news="", portfolio_state="보유 10주")
    assert p == {"holding_question": "삼성전자 실적?", "portfolio_state": "보유 10주"}, p
    p = build_worker_payload(proposal_draft="draft-x")
    assert p == {"proposal_draft": "draft-x"}
    p = build_worker_payload(holding_question="q", proposal_draft="d")
    assert set(p) == {"holding_question", "proposal_draft"}
    # 트리거 없이 보조 필드만 주면 거부한다 - 아무도 안 도는 실행을
    # RUNNING 으로 위장하지 않는다
    for kwargs in ({}, {"news": "n"}, {"portfolio_state": "s"},
                   {"holding_question": "   "}):
        try:
            build_worker_payload(**kwargs)
            raise AssertionError(f"트리거 없는 payload 가 통과했다: {kwargs}")
        except ValueError:
            pass
    print("  Worker payload 계약      OK")


def _check_worker_job_lifecycle():
    now = datetime(2026, 8, 13, 10, tzinfo=KST)
    draft = "TITLE: Liquidity pressure reversal"
    jid = register_worker_job(["holding_question"], now=now)
    with _WORKER_JOBS_LOCK:
        assert _WORKER_JOBS[jid]["status"] == "RUNNING"
        assert _WORKER_JOBS[jid]["payload_fields"] == ["holding_question"]
    done = finish_worker_job(
        jid, result={"executed": ["holdings-analyst-worker"], "degraded": False},
        model_plane={"provider": "vllm-openai", "model_version": "m"},
        error=None, now=now)
    assert done["status"] == "COMPLETED" and done["result"]["degraded"] is False
    assert done["model_plane"]["provider"] == "vllm-openai"

    jid2 = register_worker_job(["proposal_draft"], now=now)
    fail = finish_worker_job(jid2, result=None, model_plane=None,
                             error="ImportError: worker_model_gateway", now=now)
    assert fail["status"] == "FAILED" and "ImportError" in fail["error"]
    assert skeptic_job_error(jid2, draft), \
        "failed critic was accepted as a signature"

    jid3 = register_worker_job(["proposal_draft"], now=now,
                               proposal_draft=draft)
    critic = {
        "executed": ["competing-explanation-worker"],
        "degraded": False,
        "workers": [{
            "worker_id": "competing-explanation-worker",
            "status": "COMPLETED",
            "output": {
                "summary": "The effect may be a liquidity premium.",
                "confidence": 0.7,
                "evidence_refs": [],
                "escalate": False,
                "schema_valid": True,
                "skeptic_reviews": [{
                    "title": "Paraphrased liquidity review",
                    "competing_explanation": "The return may be a liquidity premium.",
                    "competing_codes": ["LIQUIDITY_PREMIUM"],
                    "verdict": "PROCEED",
                    "falsification_test": "Neutralize spread and depth buckets.",
                }],
            },
        }],
    }
    finish_worker_job(jid3, result=critic, model_plane={"provider": "test"},
                      error=None, now=now)
    assert skeptic_job_error(jid3, draft) == ""
    reviews, error = verified_skeptic_reviews(jid3, draft)
    rendered = render_skeptic_reviews(reviews)
    assert not error and "TITLE: Liquidity pressure reversal" in rendered
    assert "COMPETING_CODES: LIQUIDITY_PREMIUM" in rendered
    assert "FALSIFICATION_TEST: Neutralize spread and depth buckets." in rendered
    assert "different proposal_draft" in skeptic_job_error(
        jid3, "TITLE: Different proposal")
    assert skeptic_job_error("invented-label", draft), \
        "unverifiable label was accepted"
    many = "TITLE: First proposal\nTITLE: Second proposal"
    jid4 = register_worker_job(["proposal_draft"], now=now, proposal_draft=many)
    finish_worker_job(jid4, result=critic, model_plane={"provider": "test"},
                      error=None, now=now)
    assert "TITLE set" in skeptic_job_error(jid4, many), \
        "ambiguous multi-proposal review was paired positionally"
    assert planner_task_id("t_59e3616a-planner-20260815-10a") == "t_59e3616a"
    assert planner_task_id("planner-20260815") is None
    assert finish_worker_job("없는아이디", result=None, model_plane=None,
                             error=None, now=now) == {}
    print("  Worker 작업 수명주기     OK")


def _check_skeptic_review_persistence():
    """STOP is durable even though it correctly publishes no proposal."""
    class _Cursor:
        def __init__(self, rows=None):
            self.calls = []
            self.rows = list(rows or [])

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchone(self):
            return ("review_test",)

        def fetchall(self):
            return list(self.rows)

    class _Conn:
        def __init__(self, rows=None):
            self.cur = _Cursor(rows)
            self.commits = 0

        def cursor(self):
            return self.cur

        def commit(self):
            self.commits += 1

    conn = _Conn()
    draft = "TITLE: Event OFI\nLEAD_IDS: lead_event_1"
    reviews = [{
        "title": "Event OFI",
        "competing_explanation": "The signal may only proxy spread costs.",
        "competing_codes": ["COST_UNACCOUNTED"],
        "verdict": "STOP",
        "falsification_test": "Condition on spread and require net markout.",
    }]
    assert persist_skeptic_reviews(
        conn, draft, reviews, case_id="card-t_test",
        planner_run="t_test-planner", skeptic_run="job_test",
        known_lead_ids={"lead_event_1"},
    ) == 1
    assert conn.commits == 1
    sql, params = conn.cur.calls[0]
    assert "proposal_review_outcomes" in sql
    assert params[2] == ["lead_event_1"] and params[5] == "STOP"

    unknown = _Conn()
    assert persist_skeptic_reviews(
        unknown, draft, reviews, case_id="card-t_test",
        planner_run="t_test-planner", skeptic_run="job_test",
        known_lead_ids=set(),
    ) == 0
    assert not unknown.cur.calls, "unknown lead id was persisted as reviewed"

    recovered = _Conn(rows=[(
        "Event OFI", "The signal may only proxy spread costs.",
        ["COST_UNACCOUNTED"], "STOP",
        "Condition on spread and require net markout.",
    )])
    durable, error = load_persisted_skeptic_reviews(
        recovered, "job_test", "t_test-planner", draft)
    assert not error and durable[0]["verdict"] == "STOP", (durable, error)
    _sql, _params = recovered.cur.calls[0]
    assert _params == (
        _text_digest(draft),
        _SKEPTIC_REVIEW_CONTRACT_VERSION,
        "job_test",
        "t_test-planner",
    )
    print("  스켑틱 심사 영구기억      OK")


def _check_holdings_evidence():
    """Evidence First - 소스별 독립 시도·정직 보고·payload 병합 계약."""
    source_calls = []

    def fake_news(**kwargs):
        source_calls.append(("news", kwargs))
        return {"citation": "news-cite-1", "searched_at": "2026-08-13T09:00:00+09:00",
                "items": [{"title": "제목", "originallink": "http://u",
                           "link": "http://proxy", "pubDate": "2026-08-13"}]}

    def failed_disclosures(**kwargs):
        source_calls.append(("disclosures", kwargs))
        raise RuntimeError("dart down")

    def fake_get(url: str):
        assert "/evidence/" not in url, "뉴스·공시 레거시 DB 경로를 읽었다"
        raise ValueError("tsdb empty")          # /bars → price_context 경로

    ev = gather_holdings_evidence(
        "005930", get=fake_get, search_news=fake_news,
        search_disclosures=failed_disclosures)
    assert ev["symbol"] == "005930"
    assert ev["sources"]["news"] == {
        "status": "OK", "count": 1, "mode": "ON_DEMAND_MCP",
        "citation": "news-cite-1"}
    assert source_calls == [
        ("news", {"query": "005930", "display": 10, "sort": "date"}),
        ("disclosures", {"corp": "005930", "days": 7, "page": 1})]
    assert ev["news_headlines"][0]["ref"] == "n1"
    assert ev["news_headlines"][0]["evidence_id"] == \
        "mcp:news_search:news-cite-1:item-1"
    assert ev["news_headlines"][0]["citation_item"] == 1
    # 한 소스의 실패가 다른 소스를 못 죽인다 - 사유는 그대로 남는다
    assert ev["sources"]["disclosures"]["status"] == "FAILED"
    assert "dart down" in ev["sources"]["disclosures"]["reason"]
    # price_context 는 자기 기술 UNAVAILABLE (fetch_price_context 내장 규율)
    assert ev["sources"]["price_context"]["status"] == "UNAVAILABLE", ev["sources"]

    merged = merge_holdings_evidence(
        {"holding_question": "q", "news": "사용자 메모", "portfolio_state": "10주"},
        ev)
    assert merged["holding_question"] == "q", "질문은 그대로 남는다"
    assert merged["news"]["user_note"] == "사용자 메모", "사용자 텍스트를 보존한다"
    assert merged["news"]["headlines"][0]["ref"] == "n1"
    assert merged["news"]["source_status"]["disclosures"]["status"] == "FAILED"
    assert merged["portfolio_state"]["user_state"] == "10주"
    # price_context 는 UNAVAILABLE 상태 그대로 실린다 - 실패를 숨기지 않는다
    assert merged["portfolio_state"]["price_context"]["status"] == "UNAVAILABLE"

    # 근거가 하나도 없으면 news 는 상태 보고만 남는다(빈 성공으로 위장 금지)
    def all_down(*_args, **_kwargs):
        raise RuntimeError("down")

    ev2 = gather_holdings_evidence(
        "005930", get=all_down, search_news=all_down,
        search_disclosures=all_down)
    merged2 = merge_holdings_evidence({"holding_question": "q"}, ev2)
    assert set(merged2["news"]) == {"source_status"}, merged2["news"]

    # ── 프롬프트 예산 경계 (2026-08-13 리뷰) ──
    # _compact 는 문자 단위 raw[:8000] 절단이라, 예산을 넘기면 JSON 이 중간에서
    # 끊겨 '수집 OK 인데 수치 없음' 오정보가 된다. 최대 픽스처로 항목 단위
    # 절단이 예산을 지키는지 잰다.
    import json as _json

    def big_news(**_kwargs):
        return {"citation": "n" * 16,
                "searched_at": "2026-08-13T09:00:00+09:00",
                "items": [{"title": "제" * 80,
                           "originallink": "http://u/" + "x" * 150,
                           "pubDate": "2026-08-13T09:00:00+09:00"}
                          for _i in range(10)]}

    def big_disclosures(**_kwargs):
        return {"citation": "d" * 16,
                "items": [{"rcept_no": f"receipt-{i}", "report_nm": "공" * 80,
                           "viewer_url": "http://d/" + "y" * 150,
                           "rcept_dt": "20260813"}
                          for i in range(10)]}

    def big_get(url: str):
        assert "/evidence/" not in url, "뉴스·공시 레거시 DB 경로를 읽었다"
        return [{"bucket_time": f"2026-08-{i:02d}T00:00:00", "close": 70000.0 + i}
                for i in range(1, 22)]          # /bars → closes 21개

    ev3 = gather_holdings_evidence(
        "005930", get=big_get, search_news=big_news,
        search_disclosures=big_disclosures)
    assert len(ev3["disclosures_7d"]) == 5, "요청 시점 DART 결과도 프롬프트 상한을 지킨다"
    assert ev3["disclosures_7d"][0]["evidence_id"] == \
        f"mcp:dart_search_disclosures:{'d' * 16}:receipt-0"
    merged3 = merge_holdings_evidence(
        {"holding_question": "질문" * 100, "news": "뉴스요약" * 800,
         "portfolio_state": "상태" * 800}, ev3)
    size = len(_json.dumps(merged3, ensure_ascii=False, sort_keys=True, default=str))
    assert size <= _EVIDENCE_CHAR_BUDGET, f"예산 초과: {size}자"
    assert merged3["news"]["evidence_truncated"]["dropped_items"] >= 1
    # 사용자 텍스트 캡 - 본부장이 실은 긴 텍스트가 가격 블록을 밀어내지 않는다
    assert len(merged3["news"]["user_note"]) <= _USER_TEXT_CAP + 8
    assert len(merged3["portfolio_state"]["user_state"]) <= _USER_TEXT_CAP + 8
    # closes 원자료는 프롬프트에서 빠지고 요약 수치는 남는다
    assert "closes" not in merged3["portfolio_state"]["price_context"]
    assert "last_close" in merged3["portfolio_state"]["price_context"], \
        merged3["portfolio_state"]["price_context"]
    # 작은 픽스처는 절단 없이 그대로 - 예산 로직이 과절단하지 않는다
    assert "evidence_truncated" not in merged["news"]
    print("  Evidence First 계약     OK (예산 경계 포함)")


def _check_health_summary():
    rows = [
        {"job_name": "vkospi", "runs_24h": 2, "ok_24h": 0, "skip_24h": 2,
         "bad_24h": 0, "last_status": "SKIP", "last_ok_at": None,
         "last_error_tail": None},
        {"job_name": "geopolitical", "runs_24h": 3, "ok_24h": 0, "skip_24h": 0,
         "bad_24h": 3, "last_status": "FAILED", "last_ok_at": None,
         "last_error_tail": "FileNotFoundError"},
        {"job_name": "label-snapshot", "runs_24h": 4, "ok_24h": 1,
         "skip_24h": 0, "bad_24h": 3, "last_status": "OK",
         "last_ok_at": "2026-08-26T07:40:32Z",
         "last_error_tail": "old timeout"},
    ]
    s = summarize_health(rows)
    assert s["jobs_failing"] == 1 and not s["healthy"]
    assert s["failing"][0]["job"] == "geopolitical", s
    assert s["jobs_with_failures_24h"] == 2, s
    assert s["recovered_or_skipped"][0]["job"] == "label-snapshot", s
    assert summarize_health([])["healthy"] is True
    print("  건강 요약(SKIP 제외)     OK")


def _check_bearer_auth():
    """토큰 검증 - 미설정이면 열고, 설정되면 정확히 일치할 때만 통과."""
    assert is_authorized(None, None) and is_authorized("Bearer x", None)
    assert is_authorized("", "") , "빈 토큰은 미설정과 같게 취급한다"

    T = "s3cr3t-value"
    assert is_authorized(f"Bearer {T}", T)
    assert is_authorized(f"bearer {T}", T), "스킴은 대소문자 무관이다"
    for bad in (None, "", "Bearer", "Bearer wrong", T, f"Basic {T}",
                "Bearer ${MCP_RESEARCH_API_KEY}"):
        assert not is_authorized(bad, T), f"통과하면 안 된다: {bad!r}"
    # 미치환 변수를 그대로 보내는 경우(Hermes 가 env 를 못 찾으면 이 모양이다)
    assert not is_authorized("Bearer ${MCP_RESEARCH_API_KEY}", T)
    print("  Bearer 인증 판정         OK")


def _check_writer_connection_mode():
    """A pooled read-only server session must be overridden for MCP writes."""
    class _Conn:
        def __init__(self, *, fail=False):
            self.fail = fail
            self.readonly = None
            self.closed = False

        def set_session(self, *, readonly):
            if self.fail:
                raise RuntimeError("cannot set transaction mode")
            self.readonly = readonly

        def close(self):
            self.closed = True

    made = []

    def _connect(dsn, *, connect_timeout):
        made.append((dsn, connect_timeout, _Conn()))
        return made[-1][2]

    conn = _writer_connection("postgresql://example/test", connector=_connect)
    assert made[0][:2] == ("postgresql://example/test", 15)
    assert conn.readonly is False, "write MCP did not force READ WRITE transactions"

    failed = _Conn(fail=True)
    try:
        _writer_connection("postgresql://example/test",
                           connector=lambda *a, **k: failed)
    except RuntimeError:
        pass
    else:
        raise AssertionError("set_session failure was hidden")
    assert failed.closed, "failed pooled connection was not closed"
    print("  write DB transaction mode       OK")


def _check_tool_surface():
    """권한 경계 - 주문·원장·리스크 도구가 노출되지 않았는가."""
    try:
        server = build_server()
    except ImportError as e:
        print(f"  ⚠ mcp 패키지 없음 - 도구 면 검사 생략 ({e})")
        return
    import asyncio

    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "collector_health" in names, names
    assert not names & RETIRED_FACTORY_TOOLS, names
    assert not {"run_research_packet", "get_packet_job", "list_recent_packets",
                "geopolitical_state"} & names, names
    # 직원 Worker 실행 간선(2026-08-13) - 이 셋이 빠지면 본부장은 다시
    # '워커를 못 부르는 껍데기'다
    for required in ("run_research_workers", "get_worker_job",
                     "worker_model_health"):
        assert required in names, f"{required} 도구가 등록되지 않았다: {names}"
    forbidden = {"submit_order", "oms_submit", "write_ledger", "risk_decision",
                 "promote_strategy"}
    assert not (names & forbidden), f"권한 밖 도구가 노출됐다: {names & forbidden}"
    print(f"  도구 면·권한 경계        OK ({len(names)}개)")


def _check_liaison_surface():
    """**창구 면에는 쓰기 손이 없어야 한다** (2026-08-13, 도서관/연구소 분리).

    레거시 전략 생성 도구는 어떤 면에도 등록하지 않는다. 창구는
    프롬프트가 아니라 capability 절단으로 검증한다.
    """
    import asyncio

    srv = build_server(surface="liaison")
    names = {t.name for t in asyncio.run(srv.list_tools())}
    leaked = names & LIAISON_EXCLUDED_TOOLS
    assert not leaked, f"창구 면에 쓰기 도구가 남았다: {sorted(leaked)}"
    # Worker 실행과 모델 plane 진단은 본부(full) 면의 capability다. 창구는
    # durable library/결과 조회만 가지며 worker runtime 파일도 배포받지 않는다.
    for required in ("collector_health", "get_worker_job",
                     # Library 조회 면 (2026-08-14) - 질의가 연구 상태를 바꾸지
                     # 않고 **먼저 읽는** 대상이다. 창구에 없으면 그 구조가
                     # 성립하지 않는다.
                     "library_signal_shelf", "library_families",
                     "library_scorecard"):
        assert required in names, f"창구 면에서 {required} 가 사라졌다: {names}"
    for full_only in ("run_research_workers", "worker_model_health"):
        assert full_only not in names, \
            f"창구 면에 본부 worker capability가 노출됐다: {full_only}"
    print(f"  창구 면 capability 절단  OK ({len(names)}개, 쓰기 0개)")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--healthcheck" in sys.argv:
        # 호스트 메모리 압박으로 이벤트 루프가 일시 응답불가 상태에 빠지면
        # (2026-08-27 실측: keepalive TimeoutError -> MCP 세션 증발) 로그를
        # 뒤져야만 감지됐다. 루프백으로 아무 HTTP 응답이든 받으면 "살아있다" -
        # 인증·도구 호출이 아니라 event loop 생존만 본다(외부 예산 소비 없음).
        import urllib.error
        import urllib.request

        token = os.environ.get("MCP_RESEARCH_API_KEY", "").strip()
        request = urllib.request.Request(
            f"http://127.0.0.1:{DEFAULT_PORT}/mcp",
            headers=_healthcheck_headers(token),
        )
        try:
            with urllib.request.urlopen(request, timeout=3):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 or (token and exc.code == 401):
                print(
                    f"research-mcp 헬스체크 실패: HTTP {exc.code}",
                    flush=True,
                )
                raise SystemExit(1)
            pass  # 404/406 은 서버가 응답했다는 뜻이다
        except Exception as exc:  # noqa: BLE001 - 타임아웃·연결거부는 그대로 실패
            print(f"research-mcp 헬스체크 실패: {type(exc).__name__}: {exc}")
            raise SystemExit(1)
        raise SystemExit(0)

    if "--serve" in sys.argv:
        import uvicorn

        token = os.environ.get("MCP_RESEARCH_API_KEY", "").strip()
        # 같은 이미지가 두 면으로 뜬다(도서관/연구소 분리, 2026-08-13):
        #   full    - 부서 본체(실험대). 자율 연구 관측·Worker capability.
        #   liaison - 응대 창구(도서관). Worker capability가 등록에서 빠진다.
        surface = os.environ.get("RESEARCH_MCP_SURFACE", "full").strip().lower()
        srv = build_server(host="0.0.0.0", port=DEFAULT_PORT, surface=surface)
        s = getattr(srv, "settings", None)
        if s is not None:
            s.host, s.port = "0.0.0.0", DEFAULT_PORT
        face = "창구(liaison·읽기 전용)" if surface == "liaison" else "본체(full)"
        if token:
            print(f"{MCP_VERSION}: 0.0.0.0:{DEFAULT_PORT}/mcp [{face}] "
                  f"(Bearer 인증 켜짐)", flush=True)
        else:
            # 잊은 것과 일부러 연 것을 구분되게 남긴다 - 조용한 무인증 금지
            print(f"{MCP_VERSION}: 0.0.0.0:{DEFAULT_PORT}/mcp [{face}] "
                  f"⚠ MCP_RESEARCH_API_KEY 미설정 - 인증 없이 연다. compose "
                  f"네트워크 밖으로 노출하지 말 것", flush=True)
        app = _with_loop_watchdog(
            build_app(srv, token=token or None),
            stall_seconds=_loop_watchdog_seconds(),
        )
        uvicorn.run(app, host="0.0.0.0", port=DEFAULT_PORT, log_level="info")
        raise SystemExit(0)

    print(f"{MCP_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_symbol_guard()
    _check_bearer_auth()
    _check_writer_connection_mode()
    _check_worker_payload()
    _check_worker_job_lifecycle()
    _check_skeptic_review_persistence()
    _check_holdings_evidence()
    _check_health_summary()
    _check_tool_surface()
    _check_liaison_surface()
    print("리서치 MCP 자체 점검 통과. 서버는 --serve")
