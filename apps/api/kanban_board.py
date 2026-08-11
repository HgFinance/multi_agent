#!/usr/bin/env python3
"""공용 Kanban 보드 **읽기 전용** 조회. 사용자에게 진행·실패를 보이기 위한 것이다.

소유: 재일 (리서치 + 퀀트·백테스트) — 사용자 입구 배선분
근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 5.2(연결 순서)
      docs/HEDGE_FUND_MASTER_PLAN.md 5.6(권한 분리)

▶ 왜 SQLite 를 직접 읽나
  `hermes kanban list --json` 은 카드 목록만 준다. 사용자에게 필요한 것은
  **왜 멈췄는지**이고 그건 `task_events` 의 `completed`/`blocked` payload 에 있다.
  CLI 로는 카드마다 `show` 를 한 번씩 더 띄워야 해서 폴링 한 번에 프로세스가
  N개 뜬다. 보드는 SQLite 파일 하나이므로 read-only 로 여는 게 정직하다.

▶ **윈도우 호스트에서는 파일을 직접 열면 안 된다** (2026-08-11 실측, 비싼 교훈)
  보드는 WAL 모드다. 윈도우 호스트에서 `mode=ro` 로 열기만 해도 bind mount 위에
  `-shm` 매핑이 생기고, 그 순간부터 **컨테이너 쪽 쓰기가 전부
  `disk I/O error` 로 죽는다.** 부서 워커가 3분 12초짜리 조사를 마치고도
  `kanban_complete` 를 못 써서 카드가 `running` 에 영원히 남았다 - 화면에는
  "작업 중"으로 보였다. 원인은 dispatcher 도 권한도 아니고 **이 모듈의 읽기**였다.
  그래서 `KANBAN_ACCESS_MODE=docker` 면 같은 SQL 을 컨테이너 안에서 돌린다.
  리눅스(AWS)에서는 파일 직접 읽기가 정상이므로 기본은 `file` 이다.

▶ 여기서 하지 않는 것
  **쓰기를 하지 않는다.** 카드 생성·할당·완료·차단은 전부 에이전트의 일이다.
  이 모듈이 카드를 만들 수 있게 되는 순간 "누가 이 작업을 시켰나"가 흐려진다.
  연결도 `mode=ro` 로 연다 - 실수로 UPDATE 를 써도 SQLite 가 거부한다.
  또한 이 원본을 브라우저로 내보내지 않는다(`kanban_status_bridge` 의 규칙).
  화면에 나가는 것은 아래 `Card` 로 정제한 것뿐이다.

▶ fail-closed 규칙 셋 (2026-08-11 실측에서 나왔다)
  1. **보드를 못 읽는 것은 "카드 없음"이 아니다.** 파일이 없으면 예외를 던진다.
     빈 목록을 돌려주면 화면이 "아무 일도 없었음"으로 읽고, 그건 거짓말이다.
  2. **`done` 이 곧 답이 아니다.** 회계 카드 t_71c5969f 는 `done` 인데
     `result_len: 0` 이었다 - "NAV 데이터가 없어 산출할 수 없습니다"를 완료로
     기록한 것이다. 부서가 정직하게 실패한 것이므로 카드 상태로는 맞지만,
     화면이 `done` 만 보면 성공으로 표시한다. 그래서 결과가 비면 `NO_ANSWER`.
  3. **아무도 안 집어간 카드는 진행 중이 아니다.** dispatcher 가 죽어 있으면
     카드는 `ready` 로 영원히 앉아 있는다(실측 20분). 오래된 ready 는 `STALE` 로
     올려서 사람이 보게 한다 - 영원한 스피너가 가장 나쁜 실패다.

자체 점검:
    python apps/api/kanban_board.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# 사용자에게 보이는 카드 결말. Kanban 의 `status` 와 **일부러 다르다** -
# status 는 보드의 사정이고, 이것은 "사용자 질문에 답이 됐는가"다.
CardOutcome = Literal[
    "QUEUED",     # 만들어졌지만 아직 차례가 아니다(선행 카드 대기 포함)
    "RUNNING",    # 부서가 실제로 작업 중
    "ANSWERED",   # 결과 본문이 있다
    "NO_ANSWER",  # 완료했지만 답을 못 냈다 - 성공이 아니다
    "BLOCKED",    # 사람 입력·자료가 없어 멈췄다
    "FAILED",     # 크래시·타임아웃·연속 실패
    "STALE",      # ready 인데 아무도 집어가지 않았다(dispatcher 문제)
    "NO_ASSIGNEE",  # 없는 본부에 배정됐다 - **영원히 안 돈다**
]

# `STALE` 은 끝난 게 아니다(dispatcher 가 살아나면 돈다). `NO_ASSIGNEE` 는 끝났다 -
# 없는 이름이라 누구도 집어갈 수 없다. 둘을 같이 묶으면 사용자는 30분을 기다린다.
TERMINAL_OUTCOMES = frozenset({"ANSWERED", "NO_ANSWER", "BLOCKED", "FAILED", "NO_ASSIGNEE"})

# 실재하는 본부 프로필 이름. **`hermes_cli.PROFILE_CONTAINERS` 와 같아야 한다**
# (`ceo_intake` 자체 점검이 두 표가 어긋나지 않는지 확인한다).
# CEO 가 `accounting-portfolio` 처럼 줄여 쓰면 카드가 만들어지긴 하지만 아무도
# 집어가지 못한다 - 보드는 없는 이름도 받아 주기 때문이다(2026-08-11 실측).
KNOWN_ASSIGNEES = frozenset({
    "ceo-agent",
    "research-department",
    "trading-department",
    "risk-management",
    "quant-backtest-department",
    "accounting-portfolio-department",
    "qa-department",
    "workforce-management",
})

# ready 상태로 이만큼 지나면 "대기"가 아니라 "아무도 안 집어감"으로 본다.
# dispatcher tick 이 15~60초이므로 그 몇 배. 짧으면 정상 대기를 고장으로 부른다.
STALE_READY_SECONDS = int(os.getenv("KANBAN_STALE_READY_SECONDS", "300"))


class BoardUnavailable(RuntimeError):
    """보드 파일을 못 읽는다. **빈 결과로 바꿔치지 않는다**(규칙 1)."""


@dataclass(frozen=True)
class Card:
    """화면에 나가는 카드 한 장. 원본 행이 아니라 정제된 Projection 이다."""

    task_id: str
    title: str
    assignee: str
    status: str          # 보드 원래 상태(진단용). 화면 표시는 outcome 을 쓴다.
    outcome: CardOutcome
    summary: str = ""    # 부서가 남긴 한 줄. 실패 사유도 여기 담긴다.
    result: str = ""     # 실제 결과 본문. 비면 NO_ANSWER 다.
    depends_on: list[str] = field(default_factory=list)
    created_at: int = 0
    finished_at: int | None = None

    @property
    def is_terminal(self) -> bool:
        return self.outcome in TERMINAL_OUTCOMES

    def to_public(self) -> dict[str, Any]:
        """브라우저로 나가는 형태. 워크스페이스 경로·PID·claim lock 은 뺀다."""
        return {
            "task_id": self.task_id,
            "title": self.title,
            "department": self.assignee,
            "outcome": self.outcome,
            "summary": self.summary,
            "has_result": bool(self.result),
            "depends_on": self.depends_on,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


def board_path() -> Path:
    """보드 파일 위치. env 우선, 그다음 로컬 관례 경로."""
    explicit = os.getenv("HERMES_KANBAN_DB")
    if explicit:
        return Path(explicit)
    home = os.getenv("HERMES_KANBAN_HOME")
    if home:
        return Path(home) / "kanban.db"
    return Path.home() / ".hermes-shared-kanban" / "kanban.db"


# 보드를 어떻게 읽을 것인가. 위 경고 참고 - 윈도우에서는 `docker` 를 쓴다.
KANBAN_ACCESS_MODE = os.getenv("KANBAN_ACCESS_MODE", "file").strip().lower()
# 읽기를 대신 돌려 줄 컨테이너. 아무 부서나 되지만(보드는 공용) 이름은 명시한다.
KANBAN_READER_CONTAINER = os.getenv("KANBAN_READER_CONTAINER", "hedgefund-qa-hermes")
KANBAN_READER_DB = os.getenv("KANBAN_READER_DB", "/opt/kanban/kanban.db")

# 두 모드가 **같은 SQL** 을 쓴다. 하나만 고치면 두 경로가 어긋나므로 여기 모은다.
_SQL_TASKS = (
    "select id, title, assignee, status, result, created_at, completed_at, "
    "       last_failure_error, block_kind "
    "from tasks where session_id = ? order by created_at"
)
_SQL_EVENTS = (
    "select task_id, kind, payload from task_events "
    "where task_id in ({marks}) "
    "  and kind in ('completed','blocked','crashed','timed_out','gave_up','spawn_failed') "
    "order by id"
)
_SQL_LINKS = "select parent_id, child_id from task_links where child_id in ({marks})"

# 컨테이너 안에서 돌릴 조회 스크립트. **읽기 전용으로만 연다.**
_READER_SCRIPT = f'''
import json, sqlite3, sys
db, session = sys.argv[1], sys.argv[2]
conn = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=5.0)
conn.row_factory = sqlite3.Row
try:
    tasks = [dict(r) for r in conn.execute({_SQL_TASKS!r}, (session,))]
    ids = [t["id"] for t in tasks]
    events, links = [], []
    if ids:
        marks = ",".join("?" * len(ids))
        events = [dict(r) for r in conn.execute({_SQL_EVENTS!r}.format(marks=marks), ids)]
        links = [dict(r) for r in conn.execute({_SQL_LINKS!r}.format(marks=marks), ids)]
finally:
    conn.close()
sys.stdout.write(json.dumps({{"tasks": tasks, "events": events, "links": links}}))
'''


def _fetch_file(session_id: str) -> dict[str, list[dict[str, Any]]]:
    path = board_path()
    if not path.exists():
        # 규칙 1. 여기서 빈 목록을 돌려주면 "부서가 아무것도 안 했다"로 읽힌다.
        raise BoardUnavailable(f"kanban 보드를 찾을 수 없습니다: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        tasks = [dict(r) for r in conn.execute(_SQL_TASKS, (session_id,))]
        ids = [t["id"] for t in tasks]
        events: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        if ids:
            marks = ",".join("?" * len(ids))
            events = [dict(r) for r in conn.execute(_SQL_EVENTS.format(marks=marks), ids)]
            links = [dict(r) for r in conn.execute(_SQL_LINKS.format(marks=marks), ids)]
    finally:
        # **반드시 닫는다.** `with sqlite3.connect(...)` 는 커밋만 하고 닫지 않는다 -
        # 그렇게 새어 나간 연결이 WAL 매핑을 붙들어 컨테이너 쓰기를 막았다.
        conn.close()
    return {"tasks": tasks, "events": events, "links": links}


def _fetch_docker(session_id: str) -> dict[str, list[dict[str, Any]]]:
    """같은 조회를 컨테이너 안에서 돌린다. 호스트는 파일을 열지 않는다."""
    try:
        proc = subprocess.run(
            ["docker", "exec", "-u", "hermes", "-i", KANBAN_READER_CONTAINER,
             "python3", "-", KANBAN_READER_DB, session_id],
            input=_READER_SCRIPT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except FileNotFoundError as exc:
        raise BoardUnavailable("docker 명령을 찾을 수 없습니다") from exc
    except subprocess.TimeoutExpired as exc:
        raise BoardUnavailable("보드 조회가 30초를 넘겼습니다") from exc
    if proc.returncode != 0:
        # 못 읽은 것을 "카드 없음"으로 바꾸지 않는다(규칙 1).
        raise BoardUnavailable(
            f"{KANBAN_READER_CONTAINER} 에서 보드를 못 읽었습니다: "
            f"{(proc.stderr or '').strip()[:300]}"
        )
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise BoardUnavailable(f"보드 응답을 해석하지 못했습니다: {proc.stdout[:200]}") from exc


def _fetch(session_id: str) -> dict[str, list[dict[str, Any]]]:
    return _fetch_docker(session_id) if KANBAN_ACCESS_MODE == "docker" else _fetch_file(session_id)


def _event_payloads(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """카드별 마지막 종결 이벤트의 payload. 실패 사유가 여기 있다."""
    out: dict[str, dict[str, Any]] = {}
    for row in events:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["_kind"] = row["kind"]
        out[row["task_id"]] = payload  # 뒤에 온 것이 이긴다(마지막 이벤트)
    return out


def _classify(row: dict[str, Any], event: dict[str, Any], now: int) -> tuple[CardOutcome, str]:
    """카드 한 장의 결말과 한 줄 사유. 규칙 2·3이 여기 들어 있다."""
    status = (row["status"] or "").strip().lower()
    summary = str(event.get("summary") or event.get("reason") or "").strip()
    kind = event.get("_kind")

    if kind in {"crashed", "timed_out", "gave_up", "spawn_failed"} or row["last_failure_error"]:
        detail = summary or str(row["last_failure_error"] or "").strip()
        return "FAILED", detail or f"실행 실패({kind or 'unknown'})"

    if status == "blocked":
        block_kind = (row["block_kind"] or "").strip()
        label = f"[{block_kind}] " if block_kind else ""
        return "BLOCKED", f"{label}{summary}".strip() or "부서가 보류했습니다(사유 미기록)"

    if status == "done":
        # 규칙 2 — 완료 != 답. result 본문이 비면 답을 못 낸 것이다.
        if (row["result"] or "").strip():
            return "ANSWERED", summary
        return "NO_ANSWER", summary or "완료로 기록됐지만 결과 본문이 비어 있습니다"

    if status == "running":
        return "RUNNING", summary

    # 아직 안 끝난 카드라면, 애초에 돌 수 있는 카드인지 먼저 본다.
    assignee = (row["assignee"] or "").strip()
    if assignee not in KNOWN_ASSIGNEES:
        return "NO_ASSIGNEE", (
            f"'{assignee}' 라는 본부가 없어 아무도 이 작업을 맡지 못합니다"
            " — CEO 가 본부 이름을 줄여 썼을 수 있습니다"
        )

    if status == "ready":
        # 규칙 3 — ready 인데 오래되면 아무도 안 집어간 것이다.
        age = now - int(row["created_at"] or now)
        if age > STALE_READY_SECONDS:
            return "STALE", f"{age}초째 배정을 기다리고 있습니다 — dispatcher 를 확인하세요"
        return "QUEUED", summary

    # todo/triage/scheduled — 선행 카드 대기 등 정상 대기
    return "QUEUED", summary


def cards_for_session(session_id: str) -> list[Card]:
    """한 사용자 질의(= CEO 세션)에서 갈라져 나온 카드 전부.

    상관키를 새로 만들지 않는다. Hermes 가 세션 안에서 만들어진 카드에
    `tasks.session_id` 를 이미 박아 준다(2026-08-11 실측: CEO 가 만든 두 장이
    같은 `20260811_053455_2c23b7` 을 달고 있었다). 우리가 발명한 키는
    에이전트가 지켜 줄 이유가 없지만, 이건 런타임이 채운다.
    """
    if not session_id.strip():
        raise ValueError("session_id 가 비었습니다")
    now = int(time.time())
    raw = _fetch(session_id)
    rows = raw["tasks"]
    events = _event_payloads(raw["events"])
    links: dict[str, list[str]] = {r["id"]: [] for r in rows}
    for link in raw["links"]:
        links.setdefault(link["child_id"], []).append(link["parent_id"])

    cards = []
    for row in rows:
        outcome, summary = _classify(row, events.get(row["id"], {}), now)
        cards.append(
            Card(
                task_id=row["id"],
                title=row["title"] or "",
                assignee=row["assignee"] or "",
                status=row["status"] or "",
                outcome=outcome,
                summary=summary,
                result=(row["result"] or ""),
                depends_on=links.get(row["id"], []),
                created_at=int(row["created_at"] or 0),
                finished_at=int(row["completed_at"]) if row["completed_at"] else None,
            )
        )
    return cards


def progress_of(cards: list[Card]) -> dict[str, Any]:
    """카드 묶음을 사용자에게 보일 한 덩어리로. **성공으로 반올림하지 않는다.**"""
    total = len(cards)
    done = sum(1 for c in cards if c.is_terminal)
    unusable = [c for c in cards if c.outcome in {"NO_ANSWER", "BLOCKED", "FAILED", "NO_ASSIGNEE"}]
    stalled = [c for c in cards if c.outcome == "STALE"]
    return {
        "total": total,
        "finished": done,
        "all_terminal": total > 0 and done == total,
        # 화면 문구가 아니라 상태다. "일부는 답을 못 냈다"를 성공 옆에 같이 둔다.
        "unusable": [c.task_id for c in unusable],
        "stalled": [c.task_id for c in stalled],
        "cards": [c.to_public() for c in cards],
    }


if __name__ == "__main__":  # 자체 점검 - pytest 미도입(CLAUDE.md)
    import tempfile

    # 규칙 1: 없는 보드는 빈 결과가 아니라 예외
    os.environ["HERMES_KANBAN_DB"] = str(Path(tempfile.gettempdir()) / "no-such-board.db")
    try:
        cards_for_session("x")
    except BoardUnavailable:
        pass
    else:
        raise AssertionError("보드가 없는데 예외가 안 났다 - 규칙 1 위반")

    # 규칙 2·3: 분류
    fake = Path(tempfile.mkdtemp()) / "kanban.db"
    con = sqlite3.connect(fake)
    con.executescript(
        "create table tasks(id text primary key, title text, assignee text, status text,"
        " result text, created_at int, completed_at int, last_failure_error text,"
        " block_kind text, session_id text);"
        "create table task_events(id integer primary key, task_id text, kind text, payload text);"
        "create table task_links(parent_id text, child_id text);"
    )
    now = int(time.time())
    con.executemany(
        "insert into tasks values (?,?,?,?,?,?,?,?,?,?)",
        [
            ("t1", "답한 카드", "research-department", "done", "본문 있음", now, now, None, None, "s1"),
            ("t2", "빈 완료", "accounting-portfolio-department", "done", "", now, now, None, None, "s1"),
            ("t3", "오래된 ready", "qa-department", "ready", "", now - 9999, None, None, None, "s1"),
            ("t4", "막 만든 ready", "risk-management", "ready", "", now, None, None, None, "s1"),
            ("t5", "없는 본부", "accounting-portfolio", "ready", "", now, None, None, None, "s1"),
        ],
    )
    con.execute(
        "insert into task_events values (1,'t2','completed',?)",
        (json.dumps({"result_len": 0, "summary": "NAV 데이터가 없어 산출 불가"}),),
    )
    con.commit()
    con.close()
    os.environ["HERMES_KANBAN_DB"] = str(fake)

    got = {c.task_id: c.outcome for c in cards_for_session("s1")}
    assert got["t1"] == "ANSWERED", got
    assert got["t2"] == "NO_ANSWER", got   # 규칙 2 - done 이지만 성공 아님
    assert got["t3"] == "STALE", got       # 규칙 3
    assert got["t4"] == "QUEUED", got
    # 없는 본부는 기다릴 이유가 없다 - 즉시, 그리고 **끝난 것으로** 본다
    assert got["t5"] == "NO_ASSIGNEE", got

    summary = {c.task_id: c.summary for c in cards_for_session("s1")}
    assert "NAV" in summary["t2"], summary  # 사유가 사용자까지 간다

    prog = progress_of(cards_for_session("s1"))
    assert prog["all_terminal"] is False, prog       # STALE·QUEUED 는 끝난 게 아니다
    assert prog["unusable"] == ["t2", "t5"], prog
    assert prog["stalled"] == ["t3"], prog
    print("kanban_board 자체 점검 통과")
