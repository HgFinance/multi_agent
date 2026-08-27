#!/usr/bin/env python3
"""부서장 턴 1건의 토큰·비용·구간을 Hermes 세션 스토어에서 읽는다. 읽기 전용.

## 왜 여기서 읽나 (2026-08-27 AWS 실측으로 확정)

부서장은 `hermes chat` 프로세스 안에서 끝나고, 우리는 그 안에 계측을 넣을 수 없다.
후보를 넷 재봤고 셋이 탈락했다:

  1. kanban task 로그(`⚡ tool 0.7s`)  - 도구 이름·소요는 있으나 **토큰이 없고
     타임스탬프도 없다.** grep 무출력로 확인.
  2. `profiles/<부서>/sessions/sessions.json` - discord 게이트웨이 전용이었다
     (6건 전부 `agent:main:discord`, `output_tokens>0` 0건). CLI/kanban 턴은
     여기 안 남는다.
  3. `hermes --usage-file PATH`            - RC=0 으로 턴은 정상 실행됐는데 파일이
     생기지 않았다. 용도가 다른 플래그로 보고 쫓지 않는다.
  4. `profiles/<부서>/state.db`  ← **정본.** 아래 전부가 여기 있다.

`hermes insights` 가 같은 DB 를 읽어 "Included: 452 session(s) (subscription —
no provider invoice)" 를 보여준다. 즉 **런타임 스스로 구독이라 청구서가 없다고
분류한다** - 우리 비용 상각 설계가 추측이 아니라 런타임 분류와 같은 전제 위에 있다.

## 원문을 안 읽는다

`messages` 에는 `content`, `reasoning`, `reasoning_content`, `codex_reasoning_items`
(암호화된 추론 페이로드)가 있다. 이 모듈은 **컬럼 목록을 고정해서** 그것들을 아예
SELECT 하지 않는다. `select *` 를 쓰면 스키마가 늘 때마다 원문이 조용히 딸려온다.

## 구간 계산

메시지 타임스탬프가 epoch float 로 남아서 실측 waterfall 을 그릴 수 있다(합성
누적합이 아니다). 인접한 두 메시지 사이를 한 구간으로 보고 뒤 메시지의 역할로
이름을 붙인다:

    user 447.43 -> assistant 451.32   = 모델 3.88s
                -> tool      451.35   = 도구 0.03s
                ...
    실측 1턴 합계: 모델 11.21s(98.6%) / 도구 0.16s(1.4%)

이 비율이 W6 병목 개선안의 입력이다 - 도구가 느린 것과 모델이 오래 생각하는 것은
전혀 다른 처방을 부른다.

자체 점검: python orchestration/hermes_session_usage.py
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 읽을 컬럼을 **고정**한다. 원문 계열(content/reasoning/codex_*/title/
# system_prompt/last_activity_description)은 목록에 없다.
_SESSION_COLUMNS = (
    "id",
    "source",
    "model",
    "started_at",
    "ended_at",
    "end_reason",
    "message_count",
    "tool_call_count",
    "api_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "billing_provider",
    "billing_mode",
    "estimated_cost_usd",
    "actual_cost_usd",
    "cost_status",
    "cost_source",
    "system_prompt_hash",
    "parent_session_id",
)
_MESSAGE_COLUMNS = ("id", "role", "tool_name", "timestamp", "finish_reason")

# 구독 정액제. 이 값이면 provider 청구서가 없다 - 비용은 상각으로만 낼 수 있다
# (sessions.cost_status='included', cost_source='none' 와 함께 관측됨).
BILLING_MODE_SUBSCRIPTION = "subscription_included"

DEFAULT_STATE_DB = "/opt/data/state.db"
_READ_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class TurnSegment:
    """턴 안의 한 구간. `kind` 는 'model' 또는 'tool'."""

    kind: str
    name: str
    start_ms: int
    end_ms: int
    index: int

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass(frozen=True)
class TurnUsage:
    """부서장 턴(=세션) 1건. 안 잰 값은 None 이고 0 으로 채우지 않는다."""

    session_id: str
    source: str
    model_name: str
    started_ms: int
    ended_ms: int
    end_reason: str
    message_count: int | None
    tool_call_count: int | None
    api_call_count: int | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    billing_provider: str
    billing_mode: str
    cost_status: str
    cost_source: str
    system_prompt_hash: str
    parent_session_id: str
    segments: tuple[TurnSegment, ...] = field(default_factory=tuple)

    @property
    def latency_ms(self) -> int:
        return max(0, self.ended_ms - self.started_ms)

    @property
    def total_tokens(self) -> int | None:
        """다섯 계기의 합. 하나도 없으면 None(관측 없음과 0 을 구분한다).

        캐시 읽기가 입력의 3배가 되는 턴이 흔하다(실측: input 23,523 /
        cache_read 66,048). 상각 분모를 나중에 다시 정하더라도 분해값이 함께
        남아 있어야 재수집 없이 다시 계산할 수 있다 - 그래서 합만 저장하지 않는다.
        """

        parts = [
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.reasoning_tokens,
        ]
        measured = [value for value in parts if value is not None]
        return sum(measured) if measured else None

    @property
    def model_ms(self) -> int:
        return sum(s.duration_ms for s in self.segments if s.kind == "model")

    @property
    def tool_ms(self) -> int:
        return sum(s.duration_ms for s in self.segments if s.kind == "tool")

    @property
    def tool_wait_ratio(self) -> float | None:
        """도구 대기가 턴에서 차지하는 비율. 구간이 없으면 None.

        병목 처방이 갈리는 지점이다 - 높으면 도구·MCP 를 손대고, 낮으면 모델
        turn 수·reasoning 예산을 손댄다.
        """

        span = self.model_ms + self.tool_ms
        return (self.tool_ms / span) if span > 0 else None

    @property
    def usage_complete(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None

    @property
    def is_subscription(self) -> bool:
        return self.billing_mode == BILLING_MODE_SUBSCRIPTION


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ms(value: Any) -> int:
    try:
        return int(float(value) * 1000)
    except (TypeError, ValueError):
        return 0


def _connect(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    # mode=ro. 계측이 에이전트의 세션 스토어를 건드리면 안 된다 -
    # hermes_worker_observability 가 task_runs 를 읽는 방식과 같다.
    return sqlite3.connect(
        f"file:{Path(db_path).resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=_READ_TIMEOUT_SECONDS,
    )


def build_segments(messages: Sequence[Mapping[str, Any]]) -> tuple[TurnSegment, ...]:
    """메시지 시퀀스 -> 겹치지 않는 구간들.

    각 구간은 [앞 메시지 시각, 이 메시지 시각]이고 이름은 **뒤 메시지**가 정한다:
    assistant 가 끝났다는 것은 그 앞 구간이 모델 시간이었다는 뜻이고, tool 결과가
    도착했다는 것은 그 앞 구간이 도구 시간이었다는 뜻이다.
    """

    ordered = [m for m in messages if m.get("timestamp") is not None]
    ordered.sort(key=lambda m: (float(m["timestamp"]), int(m.get("id") or 0)))
    segments: list[TurnSegment] = []
    previous_ms: int | None = None
    for message in ordered:
        current_ms = _ms(message.get("timestamp"))
        role = str(message.get("role") or "")
        if previous_ms is None:
            # 첫 메시지(보통 user)는 시작 표식이지 구간이 아니다.
            previous_ms = current_ms
            continue
        if role == "assistant":
            kind, name = "model", "model.generate"
        elif role == "tool":
            kind = "tool"
            name = f"tool.{str(message.get('tool_name') or 'unknown')}"
        else:
            # user 후속 메시지(멀티턴 세션)는 새 구간의 시작으로만 쓴다.
            previous_ms = current_ms
            continue
        segments.append(
            TurnSegment(
                kind=kind,
                name=name,
                start_ms=previous_ms,
                end_ms=max(previous_ms, current_ms),
                index=len(segments),
            )
        )
        previous_ms = current_ms
    return tuple(segments)


def read_turn(
    session_id: str,
    *,
    db_path: str | os.PathLike[str] = DEFAULT_STATE_DB,
) -> TurnUsage | None:
    """세션 1건을 읽는다. 못 읽으면 None - 계측 부재가 실행을 죽이지 않는다."""

    session_id = str(session_id or "").strip()
    if not session_id:
        return None
    session_sql = (
        f"select {', '.join(_SESSION_COLUMNS)} from sessions where id = ? limit 1"
    )
    message_sql = (
        f"select {', '.join(_MESSAGE_COLUMNS)} from messages where session_id = ? "
        "order by id asc"
    )
    try:
        with _connect(db_path) as connection:
            row = connection.execute(session_sql, (session_id,)).fetchone()
            if row is None:
                return None
            session = dict(zip(_SESSION_COLUMNS, row))
            messages = [
                dict(zip(_MESSAGE_COLUMNS, values))
                for values in connection.execute(message_sql, (session_id,))
            ]
    except (OSError, sqlite3.Error):
        return None

    return TurnUsage(
        session_id=str(session["id"]),
        source=str(session.get("source") or ""),
        model_name=str(session.get("model") or ""),
        started_ms=_ms(session.get("started_at")),
        # 진행 중인 세션은 ended_at 이 비어 있다 - 마지막 메시지 시각으로 닫는다.
        ended_ms=_ms(session.get("ended_at"))
        or (_ms(messages[-1]["timestamp"]) if messages else 0),
        end_reason=str(session.get("end_reason") or ""),
        message_count=_int_or_none(session.get("message_count")),
        tool_call_count=_int_or_none(session.get("tool_call_count")),
        api_call_count=_int_or_none(session.get("api_call_count")),
        input_tokens=_int_or_none(session.get("input_tokens")),
        output_tokens=_int_or_none(session.get("output_tokens")),
        cache_read_tokens=_int_or_none(session.get("cache_read_tokens")),
        cache_write_tokens=_int_or_none(session.get("cache_write_tokens")),
        reasoning_tokens=_int_or_none(session.get("reasoning_tokens")),
        billing_provider=str(session.get("billing_provider") or ""),
        billing_mode=str(session.get("billing_mode") or ""),
        cost_status=str(session.get("cost_status") or ""),
        cost_source=str(session.get("cost_source") or ""),
        system_prompt_hash=str(session.get("system_prompt_hash") or ""),
        parent_session_id=str(session.get("parent_session_id") or ""),
        segments=build_segments(messages),
    )


if __name__ == "__main__":
    # 실측 턴(20260827_022401_791ba0)을 그대로 재현한 fixture 로 점검한다.
    # 숫자는 AWS 에서 읽은 값이다 - 테스트가 곧 그날 관측의 기록이다.
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "create table sessions (%s)"
        % ", ".join(f"{name} text" for name in _SESSION_COLUMNS)
    )
    connection.execute(
        "create table messages (id integer, session_id text, role text, "
        "tool_name text, timestamp real, finish_reason text, content text, "
        "reasoning text)"
    )
    connection.execute(
        "insert into sessions values (%s)" % ", ".join("?" * len(_SESSION_COLUMNS)),
        (
            "20260827_022401_791ba0", "cli", "gpt-5.6-luna",
            1787797447.478085, 1787797458.8264277, "cli_close",
            8, 3, 4, 23523, 162, 66048, 0, 69,
            "openai-codex", "subscription_included",
            0.0, None, "included", "none",
            "6be71ac74753ff919daf3bcf9ee2b16deaef7fcc5969d148936e6c1c9637ddd5", None,
        ),
    )
    rows = [
        (16283, "user", None, 1787797447.4345324, None),
        (16284, "assistant", None, 1787797451.3171763, "tool_calls"),
        (16285, "tool", "execute_code", 1787797451.3483925, None),
        (16286, "assistant", None, 1787797454.24563, "tool_calls"),
        (16287, "tool", "terminal", 1787797454.2852178, None),
        (16288, "assistant", None, 1787797457.1441545, "tool_calls"),
        (16289, "tool", "terminal", 1787797457.2269108, None),
        (16290, "assistant", None, 1787797458.8076508, "stop"),
    ]
    connection.executemany(
        "insert into messages values (?,?,?,?,?,?,?,?)",
        [
            (i, "20260827_022401_791ba0", role, tool, ts, fr, "비밀 본문", "비밀 추론")
            for i, role, tool, ts, fr in rows
        ],
    )
    connection.commit()

    # read_turn 은 파일 DB 를 열므로 여기서는 조립 함수들을 직접 점검한다.
    messages = [
        {"id": i, "role": role, "tool_name": tool, "timestamp": ts, "finish_reason": fr}
        for i, role, tool, ts, fr in rows
    ]
    segments = build_segments(messages)

    # 1. user 는 구간을 만들지 않고, 나머지 7개가 구간이 된다.
    assert len(segments) == 7, segments
    assert [s.kind for s in segments] == [
        "model", "tool", "model", "tool", "model", "tool", "model"
    ]
    assert segments[1].name == "tool.execute_code"

    # 2. 구간이 겹치지 않고 이어진다.
    for earlier, later in zip(segments, segments[1:]):
        assert earlier.end_ms == later.start_ms

    turn = TurnUsage(
        session_id="20260827_022401_791ba0", source="cli", model_name="gpt-5.6-luna",
        started_ms=_ms(1787797447.478085), ended_ms=_ms(1787797458.8264277),
        end_reason="cli_close", message_count=8, tool_call_count=3, api_call_count=4,
        input_tokens=23523, output_tokens=162, cache_read_tokens=66048,
        cache_write_tokens=0, reasoning_tokens=69,
        billing_provider="openai-codex", billing_mode="subscription_included",
        cost_status="included", cost_source="none",
        system_prompt_hash="6be71ac7", parent_session_id="", segments=segments,
    )

    # 3. 실측과 같은 결론이 나와야 한다 - 이 턴의 병목은 도구가 아니라 모델이다.
    assert turn.latency_ms == 11348, turn.latency_ms
    assert 11_000 <= turn.model_ms <= 11_400, turn.model_ms
    assert turn.tool_ms <= 200, turn.tool_ms
    assert turn.tool_wait_ratio is not None and turn.tool_wait_ratio < 0.02

    # 4. 토큰 분해가 살아 있고 합이 맞는다(상각 분모를 나중에 다시 정할 수 있게).
    assert turn.total_tokens == 23523 + 162 + 66048 + 0 + 69
    assert turn.usage_complete is True
    assert turn.is_subscription is True

    # 5. 안 잰 값은 0 이 아니라 None 이다.
    blank = TurnUsage(
        session_id="s", source="cli", model_name="m", started_ms=0, ended_ms=0,
        end_reason="", message_count=None, tool_call_count=None, api_call_count=None,
        input_tokens=None, output_tokens=None, cache_read_tokens=None,
        cache_write_tokens=None, reasoning_tokens=None, billing_provider="",
        billing_mode="", cost_status="", cost_source="", system_prompt_hash="",
        parent_session_id="",
    )
    assert blank.total_tokens is None and blank.usage_complete is False
    assert blank.tool_wait_ratio is None

    # 6. 원문 컬럼은 읽을 목록에 아예 없다.
    for banned in ("content", "reasoning", "reasoning_content", "codex_reasoning_items",
                   "system_prompt", "title"):
        assert banned not in _MESSAGE_COLUMNS and banned not in _SESSION_COLUMNS

    # 7. 없는 세션·못 여는 DB 는 None(예외가 아니다).
    assert read_turn("", db_path="/nonexistent/state.db") is None
    assert read_turn("nope", db_path="/nonexistent/state.db") is None

    print("ok - Hermes 세션 사용량 리더 점검 통과")
