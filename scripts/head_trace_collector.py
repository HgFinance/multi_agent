#!/usr/bin/env python3
"""완료된 카드들을 훑어 부서장 span 트리를 Langfuse 로 보낸다.

## 왜 dispatcher 안이 아니라 별도 컨테이너인가

읽는 파일만 보면 dispatcher 안이 맞다 - 거기에 `/home/ubuntu/.hermes` 가 통째로
붙어 있어 `kanban.db` 와 8개 부서 `state.db` 가 전부 있고, `HERMES_KANBAN_DB` 값도
이 파일의 기본값과 같다. 그런데도 밖으로 뺀 이유는 **수명주기**다.

- dispatcher 의 command 자리는 `hermes kanban daemon` 이 쓰고 있다. 여기를 함께
  쓰려면 수집기를 background 로 띄워야 하는데, `restart: unless-stopped` 는 PID 1
  만 본다. 백그라운드 수집기가 죽으면 **dispatcher 는 멀쩡한 채 trace 만 끊긴다** -
  이 파일이 `BoardUnreadable` 과 `langfuse=on|disabled` 로 없애려는 바로 그 실패
  모드를 배포 층에서 다시 만드는 셈이다.
- 수집기를 고칠 때마다 dispatcher 를 재생성하게 된다. 그건 SIGTERM→SIGKILL 취소
  계약을 타서 **실행 중인 카드가 취소된다.** 관측 도구가 카드 실행을 중단시킬 수
  있어야 할 이유가 없다.
- 수집기의 행/누수가 카드 디스패치 경로에 얹힌다. 분리하면 mem 256m·cpu 0.25·
  pids 64 로 가둘 수 있다.

새 이미지는 아니다 - `hedgefund-operations-runtime:latest` 는 card-watchdog 이
이미 쓰고 있어 호스트에 존재한다. 늘어나는 것은 컨테이너 하나뿐이다.

## 왜 wrapper 가 아니라 수집기인가

`scripts/qa_hermes_worker.py` 의 `main()` 은 QA 카드가 아니면 `os.execvpe` 로
프로세스를 통째로 교체한다. 그래서 그 뒤에 붙인 계측은 **QA 프로필에서만** 실행된다
(기존 LangSmith 관측의 accounting 스펙이 실질적으로 죽어 있던 이유이기도 하다).
`execvpe` 를 subprocess 로 바꾸면 dispatcher 의 SIGTERM→SIGKILL 취소 계약이
wrapper 에 걸려 카드 취소가 깨진다 - 계측 때문에 건드릴 자리가 아니다.

이 컨테이너에는 `kanban.db`(카드·시각·상태·프로필)와 8개 부서의 `state.db`
(세션·토큰·메시지)가 **둘 다** 있다. 그래서 밖에서 훑는 쪽이 wrapper 를 전혀
건드리지 않고 부서장 전원을 덮는다(실측 카드 분포: ceo 520, qa 181, trading 83,
hr 80, risk 52, research 40, accounting 35, quant 27).

## 카드 -> 세션은 추측이 아니라 조회다

`task_runs.metadata` 에 `worker_session_id` 와 `workflow_root_task_id` 가 들어 있다
(2026-08-27 실측). 시간 창 매칭은 그게 없는 오래된 run 을 위한 뒷문일 뿐이다.

⚠ 같은 `metadata` 에 최종 답변·findings 원문이 통째로 들어 있다. **두 키만 뽑고
나머지는 즉시 버린다.** 이 JSON 을 통으로 span 에 실으면 그날로 원문 유출이다.

## watermark 를 두지 않는다

최근 창(기본 15분)을 매번 다시 훑어 재발행한다. span id 가 결정론이라 재발행은
중복이 아니라 덮어쓰기이고(orchestration/trace_identity), 그래서 프로세스가 죽었다
살아나도 그 사이 구멍이 저절로 메워진다. 상태 파일이 없으니 그 파일이 깨져
관측이 조용히 멈추는 실패 모드도 없다.

실행: python scripts/head_trace_collector.py --once
      python scripts/head_trace_collector.py --loop --interval 60
자체 점검: python scripts/head_trace_collector.py --self-check
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestration.head_span_builder import build_card_spans
from orchestration.hermes_session_usage import read_turn
from orchestration.langfuse_otlp import enabled as langfuse_enabled
from orchestration.langfuse_otlp import publish
from orchestration.model_cost import (
    PLUS_SEAT_USD_PER_MONTH,
    UNPRICED,
    CostBasis,
    UnitRate,
    amortized_rate,
    subscription_invoice_usd,
)
from scripts.head_card_trace import (
    DEPARTMENT_BY_PROFILE,
    head_persona,
    state_db_path,
)

DEFAULT_KANBAN_DB = "/opt/data/shared-kanban/kanban.db"
DEFAULT_LOOKBACK_SECONDS = 900
DEFAULT_INTERVAL_SECONDS = 60

# 상각 분모를 세는 창. **후행 30일**이다.
#
# 달력 월을 쓰면 월초에 분모가 작아 단가가 치솟는다 - 1일에 실행한 턴이 31일에
# 실행한 같은 턴보다 몇 배 비싸게 기록되고, 그건 관측이 아니라 왜곡이다. 후행
# 창은 항상 꽉 차 있어 그 문제가 없고, 뜻도 분명하다: "한 달치 사용량 중 이 턴의
# 몫".
COST_WINDOW_DAYS = 30
# 분모를 매 회차(기본 60초)마다 다시 세지 않는다. 8개 부서 state.db 를 훑는
# 일이라 싸긴 하지만, 1분마다 바뀌는 값도 아니다.
_COST_CACHE_TTL_SECONDS = 3600
_cost_cache: dict[str, Any] = {"expires_at": 0.0, "rate": None}
# 한 번의 POST 에 담을 span 수. 카드 하나가 도구 구간까지 20~40 span 을 만들 수
# 있어서, 창이 밀렸을 때 한 번에 수 MB 를 보내지 않도록 끊는다.
_MAX_SPANS_PER_REQUEST = 400

# task_runs.metadata 에서 **이 두 개만** 읽는다. 나머지(final_answer, findings,
# result, limitations...)는 전부 업무 원문이다.
_METADATA_KEYS = ("worker_session_id", "workflow_root_task_id")

_TERMINAL_OUTCOMES = frozenset(
    {"completed", "failed", "blocked", "gave_up", "timed_out", "crashed"}
)


class BoardUnreadable(RuntimeError):
    """kanban.db 를 열지 못했다.

    빈 목록으로 접지 않는다. 접으면 "이 창에 끝난 카드가 없다"와 글자 그대로 같은
    값이 되고, 그 둘은 다른 사실이다 - 경로·권한이 어긋난 컨테이너가 60초마다
    `runs=0` 을 찍으며 건강해 보인다. `observed_tokens()` 가 부서 하나를 못 읽을 때
    이미 같은 이유로 소리를 낸다.
    """


@dataclass(frozen=True)
class CardRun:
    """`task_runs` 한 행에서 계측에 필요한 것만."""

    run_id: str
    task_id: str
    profile: str
    status: str
    outcome: str
    started_ms: int
    ended_ms: int
    session_id: str
    root_id: str
    attempts: int

    @property
    def department(self) -> str:
        return DEPARTMENT_BY_PROFILE.get(self.profile, "")

    @property
    def normalized_status(self) -> str:
        outcome = (self.outcome or self.status or "").casefold()
        return "COMPLETED" if outcome == "completed" else (outcome.upper() or "UNKNOWN")


def _safe_metadata(raw: object) -> dict[str, str]:
    """metadata JSON -> 허용된 두 키만. 나머지는 읽는 즉시 버린다."""

    try:
        payload = json.loads(str(raw or "") or "{}")
    except ValueError:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    return {
        key: str(payload.get(key) or "").strip()
        for key in _METADATA_KEYS
        if str(payload.get(key) or "").strip()
    }


def recent_runs(
    *,
    kanban_db: str | os.PathLike[str],
    now_s: float,
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
) -> list[CardRun]:
    """최근에 끝난 카드 실행들. 원문 컬럼(summary/error)은 SELECT 하지 않는다.

    보드를 못 읽으면 `BoardUnreadable` 이다 - 빈 목록이 아니다.
    """

    since = int(now_s - lookback_seconds)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{Path(kanban_db).resolve().as_posix()}?mode=ro", uri=True, timeout=2.0
        )
        rows = connection.execute(
            "select id, task_id, profile, status, outcome, started_at, ended_at, "
            "metadata from task_runs where ended_at is not null and ended_at >= ? "
            "order by ended_at asc",
            (since,),
        ).fetchall()
        attempts_by_task: dict[str, int] = {
            str(task_id): int(count)
            for task_id, count in connection.execute(
                "select task_id, count(*) from task_runs group by task_id"
            )
        }
    except (OSError, sqlite3.Error) as exc:
        raise BoardUnreadable(f"{type(exc).__name__}:{exc}") from exc
    finally:
        if connection is not None:
            connection.close()

    runs: list[CardRun] = []
    for run_id, task_id, profile, status, outcome, started_at, ended_at, metadata in rows:
        meta = _safe_metadata(metadata)
        runs.append(
            CardRun(
                run_id=str(run_id),
                task_id=str(task_id or ""),
                profile=str(profile or ""),
                status=str(status or ""),
                outcome=str(outcome or ""),
                started_ms=int(float(started_at or 0) * 1000),
                ended_ms=int(float(ended_at or 0) * 1000),
                session_id=meta.get("worker_session_id", ""),
                # 루트가 없으면 이 카드 자신이 루트다(CEO 루트 카드가 그렇다).
                root_id=meta.get("workflow_root_task_id") or str(task_id or ""),
                attempts=attempts_by_task.get(str(task_id), 1),
            )
        )
    return runs


def observed_tokens(
    *,
    env: Mapping[str, str],
    now_s: float,
    window_days: int = COST_WINDOW_DAYS,
) -> int:
    """후행 창의 **전사** 관측 토큰. 상각 단가의 분모다.

    이 프로세스가 8개 부서의 state.db 를 전부 보는 유일한 자리라 여기서 센다.
    부서별로 세면 부서마다 구독료 전액을 자기 몫으로 잡아 총합이 8배가 된다.

    캐시 읽기까지 더한다 - 어느 계기가 구독 쿼터를 얼마나 먹는지는 공개돼 있지
    않으므로 가중치를 지어내는 대신 TurnUsage.total_tokens 와 같은 정의를 쓴다
    (분해값은 span 에 그대로 남아 나중에 다시 계산할 수 있다).
    """

    since = now_s - window_days * 86_400
    total = 0
    seen: set[str] = set()
    for profile in DEPARTMENT_BY_PROFILE:
        db_path = state_db_path(profile=profile, env=env)
        if db_path is None:
            continue
        key = str(db_path)
        if key in seen:
            # 부서 프로필 두 개가 같은 홈을 가리킬 수 있다(liaison). 같은 파일을
            # 두 번 세면 분모가 부풀고 단가가 그만큼 싸게 나온다.
            continue
        seen.add(key)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=2.0
            )
            row = connection.execute(
                "select coalesce(sum(coalesce(input_tokens,0) + coalesce(output_tokens,0) "
                "+ coalesce(cache_read_tokens,0) + coalesce(cache_write_tokens,0) "
                "+ coalesce(reasoning_tokens,0)), 0) from sessions where started_at >= ?",
                (since,),
            ).fetchone()
            total += int(row[0] or 0)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            # 한 부서를 못 읽으면 분모가 작아져 단가가 **비싸게** 나온다. 조용히
            # 넘어가지 않고 그 사실을 남긴다 - 비용이 갑자기 뛰면 이 줄을 본다.
            print(
                f"head-trace-collector cost-denominator profile={profile} status=unreadable",
                file=sys.stderr,
            )
        finally:
            if connection is not None:
                connection.close()
    return total


def current_rate(*, env: Mapping[str, str], now_s: float) -> UnitRate:
    """상각 단가. 청구액을 못 정하면 UNPRICED(0 이 아니다).

    부서장은 Codex(ChatGPT) 구독으로 돌고 우리 기준은 Plus 다(2026-08-27 결정).
    그 기본값을 **여기서** 넘긴다 - `subscription_invoice_usd` 자체의 기본값은
    "모름" 그대로 둬서, 이 함수 말고 다른 데서 부를 때 월 20 달러가 조용히
    끼어들지 않게 한다. `.env` 에 실제 청구액이 있으면 그쪽이 이긴다.
    """

    if now_s < float(_cost_cache["expires_at"]) and _cost_cache["rate"] is not None:
        return _cost_cache["rate"]

    invoice = subscription_invoice_usd(env, default_seat_usd=PLUS_SEAT_USD_PER_MONTH)
    if invoice is None:
        rate = UNPRICED
    else:
        rate = amortized_rate(
            invoice_usd=invoice,
            observed_tokens=observed_tokens(env=env, now_s=now_s),
            basis=CostBasis.AMORTIZED_SUBSCRIPTION,
            window_label=f"trailing-{COST_WINDOW_DAYS}d",
        )
    _cost_cache["rate"] = rate
    _cost_cache["expires_at"] = now_s + _COST_CACHE_TTL_SECONDS
    return rate


def spans_for_run(
    run: CardRun, *, env: Mapping[str, str], rate: UnitRate = UNPRICED
) -> list[dict[str, Any]]:
    """카드 실행 1건 -> span 목록. 못 만들면 빈 목록."""

    if not run.department or not run.task_id or not run.started_ms:
        return []
    usage = None
    confidence = "no_session_id"
    if run.session_id:
        db_path = state_db_path(profile=run.profile, env=env)
        if db_path is None:
            confidence = "no_state_db"
        else:
            usage = read_turn(run.session_id, db_path=db_path)
            # 카드가 끝난 시각과 세션 행이 쓰이는 시각에 차이가 있어, 아주 최근
            # 카드는 아직 못 읽을 수 있다. 다음 회차에 같은 창을 다시 훑으면서
            # 같은 span id 로 덮어쓴다 - 그래서 여기서 실패해도 영구 손실이 아니다.
            confidence = "exact" if usage is not None else "session_not_ready"
    return build_card_spans(
        root_id=run.root_id,
        task_id=run.task_id,
        department=run.department,
        head_persona=head_persona(profile=run.profile, env=env),
        profile=run.profile,
        status=run.normalized_status,
        started_ms=run.started_ms,
        ended_ms=max(run.ended_ms, run.started_ms),
        usage=usage,
        session_confidence=confidence,
        rate=rate,
        attempts=run.attempts,
        run_id=run.run_id,
        environment=str(env.get("LANGFUSE_ENVIRONMENT") or ""),
    )


def collect_once(
    *,
    env: Mapping[str, str] | None = None,
    now_s: float | None = None,
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
) -> dict[str, int]:
    """한 회차. 돌려주는 값은 로그·점검용 카운터다."""

    runtime = dict(env) if env is not None else dict(os.environ)
    counters = {"runs": 0, "skipped": 0, "spans": 0, "requests": 0, "failed": 0, "unreadable": 0}
    if not langfuse_enabled(runtime):
        return counters

    kanban_db = runtime.get("HERMES_KANBAN_DB") or DEFAULT_KANBAN_DB
    try:
        runs = recent_runs(
            kanban_db=kanban_db,
            now_s=time.time() if now_s is None else now_s,
            lookback_seconds=lookback_seconds,
        )
    except BoardUnreadable as exc:
        # 여기서 멈춘다. 이 회차에 대해 우리가 아는 것은 "모른다" 뿐이고, 그걸
        # runs=0 으로 적으면 다음 사람이 관측 결과로 읽는다.
        counters["unreadable"] = 1
        print(
            f"head-trace-collector board=unreadable db={kanban_db} detail={exc}",
            file=sys.stderr,
            flush=True,
        )
        return counters
    rate = current_rate(env=runtime, now_s=time.time() if now_s is None else now_s)
    batch: list[dict[str, Any]] = []
    for run in runs:
        if run.outcome and run.outcome.casefold() not in _TERMINAL_OUTCOMES:
            counters["skipped"] += 1
            continue
        spans = spans_for_run(run, env=runtime, rate=rate)
        if not spans:
            counters["skipped"] += 1
            continue
        counters["runs"] += 1
        batch.extend(spans)
        if len(batch) >= _MAX_SPANS_PER_REQUEST:
            counters["requests"] += 1
            counters["spans"] += len(batch)
            if not publish(batch, env=runtime, service_name="hgfinance-head"):
                counters["failed"] += 1
            batch = []
    if batch:
        counters["requests"] += 1
        counters["spans"] += len(batch)
        if not publish(batch, env=runtime, service_name="hgfinance-head"):
            counters["failed"] += 1
    return counters


def _self_check() -> None:
    import tempfile

    from orchestration.hermes_session_usage import _MESSAGE_COLUMNS, _SESSION_COLUMNS

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        kanban_db = home / "kanban.db"
        connection = sqlite3.connect(kanban_db)
        connection.execute(
            "create table task_runs (id integer, task_id text, profile text, "
            "status text, outcome text, started_at real, ended_at real, "
            "metadata text, summary text, error text)"
        )
        # 실측 run 을 본뜬다. metadata 에 업무 원문이 함께 들어 있는 것도 그대로다.
        leaky_metadata = json.dumps(
            {
                "workflow_root_task_id": "t_9166abfd",
                "worker_session_id": "20260827_030239_85f520",
                "final_answer": "고객 포트폴리오의 삼성전자 비중을 줄이십시오",
                "findings": [{"title": "민감한 발견", "description": "원문"}],
            },
            ensure_ascii=False,
        )
        connection.executemany(
            "insert into task_runs values (?,?,?,?,?,?,?,?,?,?)",
            [
                (1868, "t_b10c917d", "qa-department", "done", "completed",
                 1787799757, 1787799807, leaky_metadata, "요약 원문", None),
                (1866, "t_9166abfd", "ceo-agent", "done", "completed",
                 1787799651, 1787799677,
                 json.dumps({"worker_session_id": "20260827_030054_c7efad"}),
                 None, None),
                # 아직 안 끝난 run 은 창에 안 들어온다(ended_at is null).
                (1869, "t_new", "research-department", "running", None,
                 1787799900, None, None, None, None),
            ],
        )
        connection.commit()
        connection.close()

        # 1. metadata 에서 두 키만 나온다 - 원문은 어디로도 새지 않는다.
        meta = _safe_metadata(leaky_metadata)
        assert meta == {
            "worker_session_id": "20260827_030239_85f520",
            "workflow_root_task_id": "t_9166abfd",
        }, meta
        assert "final_answer" not in json.dumps(meta, ensure_ascii=False)

        runs = recent_runs(kanban_db=kanban_db, now_s=1787799900, lookback_seconds=900)
        # 2. 끝난 run 만, 오래된 순서로.
        assert [r.run_id for r in runs] == ["1866", "1868"], [r.run_id for r in runs]
        qa_run = runs[1]
        assert qa_run.session_id == "20260827_030239_85f520"
        assert qa_run.root_id == "t_9166abfd"
        assert qa_run.department == "qa"
        assert qa_run.normalized_status == "COMPLETED"

        # 3. root 가 metadata 에 없으면 자기 자신이 루트다(CEO 루트 카드).
        ceo_run = runs[0]
        assert ceo_run.root_id == "t_9166abfd" == ceo_run.task_id

        # 4. 세션 스토어가 있으면 토큰이 붙는다.
        profile_dir = home / "profiles" / "qa-department"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text(
            "agent:\n  head_persona: qa-lead\n", encoding="utf-8"
        )
        state = sqlite3.connect(profile_dir / "state.db")
        state.execute(
            "create table sessions (%s)"
            % ", ".join(f"{name} text" for name in _SESSION_COLUMNS)
        )
        state.execute(
            "create table messages (%s, session_id text, content text)"
            % ", ".join(f"{name} text" for name in _MESSAGE_COLUMNS)
        )
        blank = {name: None for name in _SESSION_COLUMNS}
        row = {
            **blank, "id": "20260827_030239_85f520", "source": "kanban",
            "model": "gpt-5.6-luna", "started_at": 1787799757.0,
            "ended_at": 1787799807.0, "input_tokens": 64449, "output_tokens": 1590,
            "billing_mode": "subscription_included",
        }
        state.execute(
            "insert into sessions values (%s)" % ", ".join("?" * len(_SESSION_COLUMNS)),
            tuple(row[name] for name in _SESSION_COLUMNS),
        )
        state.executemany(
            "insert into messages values (%s, ?, ?)" % ", ".join("?" * len(_MESSAGE_COLUMNS)),
            [
                (1, "user", None, 1787799757.0, None, "20260827_030239_85f520", "비밀"),
                (2, "assistant", None, 1787799760.0, "tool_calls", "20260827_030239_85f520", "비밀"),
                (3, "tool", "kanban_show", 1787799761.0, None, "20260827_030239_85f520", "비밀"),
                (4, "assistant", None, 1787799807.0, "stop", "20260827_030239_85f520", "비밀"),
            ],
        )
        state.commit()
        state.close()

        env = {"HERMES_HOME": str(home)}
        spans = spans_for_run(qa_run, env=env)
        names = [s["name"] for s in spans]
        assert names[0] == "head.qa"
        assert "codex.session" in names
        assert "tool.kanban_show" in names, names
        blob = json.dumps(spans, ensure_ascii=False)
        for secret in ("삼성전자", "final_answer", "민감한 발견", "요약 원문"):
            assert secret not in blob, secret

        # 5. 세션 스토어가 없는 부서는 head span 만 나가고 사유가 남는다.
        ceo_spans = spans_for_run(ceo_run, env=env)
        assert len(ceo_spans) == 1
        assert "no_state_db" in json.dumps(ceo_spans[0], ensure_ascii=False)

        # 6. 두 번 돌려도 같은 span id 다(재발행이 중복을 만들지 않는다).
        assert [s["spanId"] for s in spans] == [
            s["spanId"] for s in spans_for_run(qa_run, env=env)
        ]

        # 7. 스위치가 꺼져 있으면 아무 일도 하지 않는다.
        assert collect_once(env={"HERMES_KANBAN_DB": str(kanban_db)}, now_s=1787799900) == {
            "runs": 0, "skipped": 0, "spans": 0, "requests": 0, "failed": 0, "unreadable": 0
        }

        # 8. 전송 실패는 카운터로만 남고 예외가 아니다.
        live = {
            "HERMES_HOME": str(home), "HERMES_KANBAN_DB": str(kanban_db),
            "LANGFUSE_TRACING": "true", "LANGFUSE_PUBLIC_KEY": "pk",
            "LANGFUSE_SECRET_KEY": "sk", "LANGFUSE_HOST": "http://127.0.0.1:1",
        }
        counters = collect_once(env=live, now_s=1787799900)
        assert counters["runs"] == 2 and counters["failed"] == 1, counters
        assert counters["spans"] == len(spans) + len(ceo_spans)
        assert counters["unreadable"] == 0, counters

        # 9. 보드를 못 읽는 것은 "카드 0장"이 아니다. AWS 에서 경로·권한이 어긋나는
        #    형태가 정확히 이것이고, 빈 목록으로 접으면 60초마다 runs=0 을 찍으며
        #    건강해 보인다.
        try:
            recent_runs(kanban_db=home / "does-not-exist.db", now_s=1787799900)
        except BoardUnreadable as exc:
            # 사유가 실려야 로그만 보고 경로인지 권한인지 갈라낼 수 있다.
            assert "OperationalError" in str(exc), exc
        else:  # pragma: no cover - 회귀 시에만 도달
            raise AssertionError("없는 보드를 읽고도 예외가 없다")

        blind = collect_once(
            env={**live, "HERMES_KANBAN_DB": str(home / "does-not-exist.db")},
            now_s=1787799900,
        )
        assert blind["unreadable"] == 1, blind
        assert blind["runs"] == 0 and blind["spans"] == 0, blind
        # 꺼져 있을 때(테스트 7)와 카운터가 달라야 한다 - 그래야 로그로 구분된다.
        assert blind != {
            "runs": 0, "skipped": 0, "spans": 0, "requests": 0, "failed": 0, "unreadable": 0
        }

    print("ok - 부서장 카드 수집기 점검 통과")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="한 회차만 돌고 끝낸다")
    parser.add_argument("--loop", action="store_true", help="주기 실행")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_SECONDS)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)

    if args.self_check:
        _self_check()
        return 0
    if not args.once and not args.loop:
        parser.error("--once 또는 --loop 중 하나가 필요하다")

    while True:
        started = time.perf_counter()
        # 매 회차에 스위치 상태를 같이 찍는다. 꺼져 있을 때의 0 과 켜져 있는데
        # 카드가 없을 때의 0 은 로그에서 글자 그대로 같아서, 켠 줄 알았는데 안 켜진
        # 상태를 카운터만으로는 알아낼 수 없다. 시작 한 줄로는 부족하다 - 컨테이너가
        # 며칠 돌면 그 줄은 스크롤 밖이다.
        switch = "on" if langfuse_enabled(os.environ) else "disabled"
        counters = collect_once(lookback_seconds=args.lookback)
        print(
            f"head-trace-collector langfuse={switch} "
            + " ".join(f"{key}={value}" for key, value in counters.items())
            + f" elapsed_ms={int((time.perf_counter() - started) * 1000)}",
            file=sys.stderr,
            flush=True,
        )
        if args.once:
            return 0
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
