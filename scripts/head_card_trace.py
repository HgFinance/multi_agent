#!/usr/bin/env python3
"""카드 1장이 끝난 자리에서 부서장 span 트리를 Langfuse 로 보낸다.

## 여기가 유일한 계측 지점인 이유

사용자 질의는 `/ui/ceo/ask` 로만 들어오고 그 경로는 **CEO 루트 카드만 만든다**
(apps/api/ceo.py 머리말). 계획·부서 카드 생성·QA·최종 종합은 전부 CEO Supervisor 가
카드로 만들고, 그 카드들을 실행하는 프로세스가 dispatcher 가 띄우는 이 wrapper 다.
그래서 CEO·부서장·QA·종합 턴이 **예외 없이** 이 지점을 지나간다.

## 왜 여기서 비용을 계산하지 않나

상각 단가의 분모는 **전사 관측 토큰**인데(orchestration/model_cost.py), 이 프로세스는
자기 부서 state.db 만 본다. 여기서 부서별로 계산하면 부서마다 구독료 전액을 자기
몫으로 잡아 총합이 8배가 된다. 그래서 토큰만 정확히 실어 보내고, 비용은 전사
집계를 아는 쪽(HR Scorecard)이 나중에 붙인다 - 값이 없는 것과 틀린 값 중에는
없는 쪽이 낫다.

## 실패는 전부 삼킨다

Hermes 의 반환 코드도, 카드의 terminal 계약도 이 함수 때문에 바뀌지 않는다.
기존 LangSmith 관측(hermes_worker_observability)과 같은 계약이다.

자체 점검: python scripts/head_card_trace.py
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    # dispatcher 는 이 파일을 HERMES_BIN 경유로 띄우므로 sys.path[0] 이
    # `/app/repo/scripts` 다. 저장소 루트를 넣어야 orchestration/* 이 보인다
    # (컨테이너에 `.:/app/repo:ro` 로 저장소 전체가 마운트돼 있다).
    sys.path.insert(0, str(_REPO_ROOT))

from orchestration.head_span_builder import build_card_spans, match_session_id
from orchestration.hermes_session_usage import read_turn
from orchestration.langfuse_otlp import enabled as langfuse_enabled
from orchestration.langfuse_otlp import publish

# canonical profile -> 부서 코드. orchestration/canonical_profiles.py 의 표와 같은
# 값이다. 여기서 규칙으로 짓지 않고 적어 두는 이유는 그 모듈이 pydantic 등을 끌고
# 오는데 Hermes 이미지에 그게 있다는 보장이 없기 때문이다. 값이 어긋나면
# tests/orchestration/test_head_card_trace.py 가 잡는다.
DEPARTMENT_BY_PROFILE: dict[str, str] = {
    "ceo-agent": "ceo",
    "research-department": "research",
    "research-liaison": "research",
    "trading-department": "trading",
    "risk-management": "risk",
    "quant-backtest-department": "quant",
    "quant-liaison": "quant",
    "accounting-portfolio-department": "accounting",
    "qa-department": "qa",
    "hr-department": "hr",
}

# `agent: head_persona: <이름>`. yaml 을 import 하지 않는다 - 이 이미지에 pyyaml 이
# 있다는 보장이 없고, 계측 때문에 의존성을 늘리지 않는다(_MODEL_RE 와 같은 방식).
_HEAD_PERSONA_RE = re.compile(r"^\s*head_persona:\s*([^#\s]+)", re.MULTILINE)

# 세션 후보를 훑을 창. 매칭 자체는 head_span_builder 가 시작 근접으로 판정하고,
# 여기서는 그보다 넉넉히 긁어 와서 **경쟁 후보가 있었는지**까지 보이게 한다 -
# 좁게 긁으면 모호한 경우가 유일 후보처럼 보인다.
_CANDIDATE_WINDOW_MS = 30_000


def state_db_path(*, profile: str, env: Mapping[str, str] | None = None) -> Path | None:
    """그 프로필의 세션 스토어. 못 찾으면 None.

    dispatcher 는 `HERMES_HOME=/opt/data` 에 전체 홈을 물고 있어 프로필이
    `profiles/<이름>/` 아래에 있고, 부서 전용 컨테이너는 프로필 자체가 `/opt/data`
    다. 둘 다 지원한다(_model_info 와 같은 후보 순서).
    """

    source = env if env is not None else os.environ
    home = Path(str(source.get("HERMES_HOME") or "/opt/data"))
    for candidate in (home / "profiles" / profile / "state.db", home / "state.db"):
        if candidate.is_file():
            return candidate
    return None


def head_persona(*, profile: str, env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    home = Path(str(source.get("HERMES_HOME") or "/opt/data"))
    for candidate in (
        home / "profiles" / profile / "config.yaml",
        home / "config.yaml",
    ):
        try:
            match = _HEAD_PERSONA_RE.search(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        if match:
            return match.group(1).strip()
    return ""


def session_candidates(
    db_path: str | os.PathLike[str], *, started_ms: int, window_ms: int = _CANDIDATE_WINDOW_MS
) -> list[dict[str, Any]]:
    """창 안의 세션 행들. 원문 컬럼은 읽지 않는다(id/source/started_at 뿐)."""

    low = (started_ms - window_ms) / 1000.0
    high = (started_ms + window_ms) / 1000.0
    # `with sqlite3.connect(...)` 는 연결을 닫지 않는다(트랜잭션만 끝낸다).
    # 카드마다 도는 프로세스라 핸들이 새면 그대로 쌓인다.
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True, timeout=1.0
        )
        rows = connection.execute(
            "select id, source, started_at from sessions "
            "where started_at between ? and ?",
            (low, high),
        ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    finally:
        if connection is not None:
            connection.close()
    return [
        {
            "id": str(row[0]),
            "source": str(row[1] or ""),
            "started_ms": int(float(row[2] or 0) * 1000),
        }
        for row in rows
    ]


def publish_card_trace(
    *,
    profile: str,
    task_id: str,
    root_id: str,
    request_id: str,
    run_id: str,
    status: str,
    started_ms: int,
    ended_ms: int,
    attempts: int = 1,
    source: str = "kanban",
    env: Mapping[str, str] | None = None,
) -> str:
    """카드 1장을 발행한다. 결과 사유 문자열을 돌려준다(로그용, 실패도 문자열).

    호출자는 이 반환값으로 아무 판단도 하지 않는다 - 관측이 카드의 terminal 계약을
    바꾸면 안 된다.
    """

    runtime = dict(env) if env is not None else dict(os.environ)
    if not langfuse_enabled(runtime):
        return "disabled"
    department = DEPARTMENT_BY_PROFILE.get(profile, "")
    if not department:
        # 모르는 프로필의 부서 코드를 이름으로 지어내지 않는다 - 틀린 코드로 나간
        # span 은 조회되지 않으면서 있는 것처럼 보인다.
        return "unknown_profile"
    if not root_id:
        return "no_root"

    usage = None
    confidence = "no_state_db"
    db_path = state_db_path(profile=profile, env=runtime)
    if db_path is not None:
        session_id, confidence = match_session_id(
            source=source,
            started_ms=started_ms,
            candidates=session_candidates(db_path, started_ms=started_ms),
        )
        if session_id:
            usage = read_turn(session_id, db_path=db_path)
            if usage is None:
                confidence = "session_unreadable"

    spans = build_card_spans(
        root_id=root_id,
        task_id=task_id,
        department=department,
        head_persona=head_persona(profile=profile, env=runtime),
        profile=profile,
        status=status,
        started_ms=started_ms,
        ended_ms=ended_ms,
        usage=usage,
        session_confidence=confidence,
        # 비용은 여기서 계산하지 않는다(모듈 머리말 참고) - 분모가 전사 토큰이라
        # 부서 프로세스가 낼 수 있는 값이 아니다.
        attempts=attempts,
        run_id=run_id,
        request_id=request_id,
        environment=str(runtime.get("LANGFUSE_ENVIRONMENT") or ""),
    )
    if not spans:
        return "no_spans"
    ok = publish(spans, env=runtime, service_name="hgfinance-head")
    return f"published:{confidence}:{len(spans)}" if ok else f"send_failed:{confidence}"


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        profile_dir = home / "profiles" / "research-department"
        profile_dir.mkdir(parents=True)

        # 1. head_persona 를 yaml 없이 뽑는다.
        (profile_dir / "config.yaml").write_text(
            "model:\n  provider: openai-codex\n  default: gpt-5.6-luna\n"
            "agent:\n  head_persona: autonomous-quant-researcher\n  max_turns: 100\n",
            encoding="utf-8",
        )
        env = {"HERMES_HOME": str(home)}
        assert head_persona(profile="research-department", env=env) == (
            "autonomous-quant-researcher"
        )
        assert head_persona(profile="nope", env=env) == ""

        # 2. state.db 가 없으면 None(예외가 아니다).
        assert state_db_path(profile="research-department", env=env) is None

        # 실제 스키마를 그대로 세운다 - 컬럼이 빠진 fixture 로는 read_turn 이
        # 언제나 실패해서 "세션을 못 읽었다"와 "정말 없다"를 구분하지 못한다.
        from orchestration.hermes_session_usage import (
            _MESSAGE_COLUMNS,
            _SESSION_COLUMNS,
        )

        db = profile_dir / "state.db"
        connection = sqlite3.connect(db)
        connection.execute(
            "create table sessions (%s)"
            % ", ".join(f"{name} text" for name in _SESSION_COLUMNS)
        )
        connection.execute(
            "create table messages (%s, session_id text, content text)"
            % ", ".join(f"{name} text" for name in _MESSAGE_COLUMNS)
        )
        blank = {name: None for name in _SESSION_COLUMNS}
        rows = [
            {**blank, "id": "s-hit", "source": "kanban", "model": "gpt-5.6-luna",
             "started_at": 1_787_795_186.4, "ended_at": 1_787_795_237.5,
             "input_tokens": 64449, "output_tokens": 1590,
             "billing_mode": "subscription_included"},
            # +20s. 같은 조회 창 안이지만 시작이 붙지 않아 다른 실행이다.
            {**blank, "id": "s-late", "source": "kanban", "started_at": 1_787_795_206.0},
            {**blank, "id": "s-cli", "source": "cli", "started_at": 1_787_795_186.5},
        ]
        connection.executemany(
            "insert into sessions values (%s)" % ", ".join("?" * len(_SESSION_COLUMNS)),
            [tuple(row[name] for name in _SESSION_COLUMNS) for row in rows],
        )
        connection.executemany(
            "insert into messages values (%s, ?, ?)"
            % ", ".join("?" * len(_MESSAGE_COLUMNS)),
            [
                (1, "user", None, 1_787_795_186.4, None, "s-hit", "비밀 본문"),
                (2, "assistant", None, 1_787_795_190.0, "tool_calls", "s-hit", "비밀"),
                (3, "tool", "research.evidence.search", 1_787_795_191.2, None, "s-hit", "비밀"),
                (4, "assistant", None, 1_787_795_237.0, "stop", "s-hit", "비밀"),
            ],
        )
        connection.commit()
        connection.close()

        assert state_db_path(profile="research-department", env=env) == db

        # 3. 후보는 넓게 긁고(경쟁자가 보이도록), 판정은 시작 근접으로 한다.
        candidates = session_candidates(db, started_ms=1_787_795_186_000)
        assert {c["id"] for c in candidates} == {"s-hit", "s-late", "s-cli"}
        assert match_session_id(
            source="kanban", started_ms=1_787_795_186_000, candidates=candidates
        ) == ("s-hit", "window")

        # 4. 스위치가 꺼져 있으면 아무것도 하지 않는다.
        assert publish_card_trace(
            profile="research-department", task_id="t_1", root_id="t_0",
            request_id="r", run_id="1", status="COMPLETED",
            started_ms=1_787_795_186_000, ended_ms=1_787_795_237_000, env=env,
        ) == "disabled"

        # 5. 모르는 프로필·루트 없음은 각각 다른 사유로 접힌다(같은 실패로 뭉치면
        #    원인을 못 가린다). 스위치는 켜되 호스트를 못 가게 해 전송은 실패시킨다.
        live = {
            **env,
            "LANGFUSE_TRACING": "true",
            "LANGFUSE_PUBLIC_KEY": "pk-test",
            "LANGFUSE_SECRET_KEY": "sk-test",
            "LANGFUSE_HOST": "http://127.0.0.1:1",
        }
        assert publish_card_trace(
            profile="made-up-profile", task_id="t_1", root_id="t_0", request_id="r",
            run_id="1", status="COMPLETED", started_ms=1, ended_ms=2, env=live,
        ) == "unknown_profile"
        assert publish_card_trace(
            profile="research-department", task_id="t_1", root_id="", request_id="r",
            run_id="1", status="COMPLETED", started_ms=1, ended_ms=2, env=live,
        ) == "no_root"

        # 6. 전송이 실패해도 예외가 아니라 사유 문자열이다. 세션도 함께 찾는다.
        result = publish_card_trace(
            profile="research-department", task_id="t_1", root_id="t_0", request_id="r",
            run_id="1", status="COMPLETED",
            started_ms=1_787_795_186_000, ended_ms=1_787_795_237_000, env=live,
        )
        assert result == "send_failed:window", result

        # 7. 세션을 못 찾아도 head span 은 나간다(토큰만 빠진다).
        far = publish_card_trace(
            profile="research-department", task_id="t_1", root_id="t_0", request_id="r",
            run_id="1", status="COMPLETED",
            started_ms=1_600_000_000_000, ended_ms=1_600_000_001_000, env=live,
        )
        assert far == "send_failed:missing", far

    print("ok - 부서장 카드 trace 발행부 점검 통과")
