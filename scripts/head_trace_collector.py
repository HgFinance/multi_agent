#!/usr/bin/env python3
"""완료된 카드들을 훑어 부서장 span 트리를 Langfuse 로 보낸다. dispatcher 컨테이너 전용.

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
from scripts.head_card_trace import (
    DEPARTMENT_BY_PROFILE,
    head_persona,
    state_db_path,
)

DEFAULT_KANBAN_DB = "/opt/data/shared-kanban/kanban.db"
DEFAULT_LOOKBACK_SECONDS = 900
DEFAULT_INTERVAL_SECONDS = 60
# 한 번의 POST 에 담을 span 수. 카드 하나가 도구 구간까지 20~40 span 을 만들 수
# 있어서, 창이 밀렸을 때 한 번에 수 MB 를 보내지 않도록 끊는다.
_MAX_SPANS_PER_REQUEST = 400

# task_runs.metadata 에서 **이 두 개만** 읽는다. 나머지(final_answer, findings,
# result, limitations...)는 전부 업무 원문이다.
_METADATA_KEYS = ("worker_session_id", "workflow_root_task_id")

_TERMINAL_OUTCOMES = frozenset(
    {"completed", "failed", "blocked", "gave_up", "timed_out", "crashed"}
)


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
    """최근에 끝난 카드 실행들. 원문 컬럼(summary/error)은 SELECT 하지 않는다."""

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
    except (OSError, sqlite3.Error):
        return []
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


def spans_for_run(run: CardRun, *, env: Mapping[str, str]) -> list[dict[str, Any]]:
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
    counters = {"runs": 0, "skipped": 0, "spans": 0, "requests": 0, "failed": 0}
    if not langfuse_enabled(runtime):
        return counters

    runs = recent_runs(
        kanban_db=runtime.get("HERMES_KANBAN_DB") or DEFAULT_KANBAN_DB,
        now_s=time.time() if now_s is None else now_s,
        lookback_seconds=lookback_seconds,
    )
    batch: list[dict[str, Any]] = []
    for run in runs:
        if run.outcome and run.outcome.casefold() not in _TERMINAL_OUTCOMES:
            counters["skipped"] += 1
            continue
        spans = spans_for_run(run, env=runtime)
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
            "runs": 0, "skipped": 0, "spans": 0, "requests": 0, "failed": 0
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

    if not langfuse_enabled(os.environ):
        # 조용히 도는 것보다 왜 안 도는지 말하는 쪽이 낫다 - 계측이 자기 부재를
        # 관측하지 못하면 관측이 없는 것과 같다.
        print("head-trace-collector langfuse=disabled", file=sys.stderr)

    while True:
        started = time.perf_counter()
        counters = collect_once(lookback_seconds=args.lookback)
        print(
            "head-trace-collector "
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
