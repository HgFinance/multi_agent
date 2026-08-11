#!/usr/bin/env python3
"""Hermes 부서 Agent CLI 실행기. 부서별 Router가 공유한다.

소유: 도현 (트레이딩 + 회계·포트폴리오)
근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 6(명령 경계)
      docs/HEDGE_FUND_MASTER_PLAN.md 5.6(권한 분리)

**부서는 Router가 정한다 - 요청 Body로 받지 않는다.** 클라이언트가 부서 이름을
보낼 수 있으면 서버 화이트리스트가 유일한 방어선이지만, 경로로 고정하면 회계
화면에서 트레이딩 Agent를 부를 방법 자체가 없다. 화이트리스트 항목을 하나
늘리는 실수로 5.6이 무너지지 않는다.

응답 계약 두 개는 여기서 한 번만 정의한다.

1. `authoritative: false` - Agent 텍스트는 공식 수치가 아니다.
2. `source_of_record: /ui/snapshot` - 화면이 수치를 가져갈 곳은 여기뿐이다.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml
from fastapi import HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]

# Hermes chat은 응답이 문자열이어도 Profile의 Tool을 실행할 수 있다. 인증, 사용자별
# 권한과 Tool Allowlist가 붙기 전에는 명시적인 로컬 개발 Opt-in 없이는 열지 않는다.
ENABLE_AGENT_ASK = os.getenv("ENABLE_AGENT_ASK", "false").strip().lower() in {
    "1", "true", "yes", "on",
}


class AgentAsk(BaseModel):
    """부서 Agent 질의 Body. 부서 이름이 없는 것이 이 계약의 핵심이다."""

    query: str = Field(min_length=1, max_length=2000)


# ▶ 어느 런타임에 붙을 것인가 (2026-08-11 추가)
#   원래 이 모듈은 BFF 와 Hermes 가 같은 호스트에 있다고 가정했다. 로컬 시험에서는
#   Hermes 가 부서마다 **컨테이너 안**에 있고 호스트(윈도우)에는 `hermes` 가 없다.
#   그래서 실행 방식을 env 로 고른다 - AWS 로 올릴 때 `local` 로 두면 원래 동작 그대로다.
#     HERMES_EXEC_MODE=local  : `hermes -p <profile> ...`         (기본, 지금까지의 동작)
#     HERMES_EXEC_MODE=docker : `docker exec <컨테이너> hermes ...`
#   컨테이너 이름은 프로필 이름에서 짓지 않고 표에 적는다 - 이름 규칙이 어긋난 게
#   하나라도 있으면(인사팀처럼) 규칙으로 지은 이름은 조용히 틀린 곳에 붙는다.
HERMES_EXEC_MODE = os.getenv("HERMES_EXEC_MODE", "local").strip().lower()
PROFILE_CONTAINERS = {
    "ceo-agent": "hedgefund-ceo-hermes",
    "research-department": "hedgefund-research-hermes",
    "trading-department": "hedgefund-trading-hermes",
    "risk-management": "hedgefund-risk-hermes",
    "quant-backtest-department": "hedgefund-quant-hermes",
    "accounting-portfolio-department": "hedgefund-accounting-hermes",
    "qa-department": "hedgefund-qa-hermes",
    "workforce-management": "hedgefund-workforce-hermes",
}


def argv_for(department: str, tail: list[str]) -> list[str]:
    """실행 argv 를 만든다. shell 을 거치지 않으므로 사용자 문자열이 안전하다."""
    if HERMES_EXEC_MODE != "docker":
        return ["hermes", "-p", department, *tail]
    container = PROFILE_CONTAINERS.get(department)
    if container is None:
        # 규칙으로 지어내지 않는다. 모르는 부서는 못 부르는 게 맞다.
        raise HTTPException(503, f"컨테이너를 모르는 프로필입니다: {department}")
    # 컨테이너 안에서는 /opt/data 가 그 부서 프로필 자체라 `-p` 를 붙이지 않는다.
    # 붙이면 `/opt/data/profiles/<이름>` 을 다시 찾아 들어가 memory·session 이
    # 부서 본체가 아니라 이름표 디렉터리에 쌓인다.
    return ["docker", "exec", "-i", container, "hermes", *tail]


def ask(
    *,
    department: str,
    config: str,
    query: str,
    resume: str | None = None,
    enabled: bool | None = None,
    timeout: int | None = None,
) -> dict:
    """Hermes 부서 Profile에 질의하고 텍스트만 돌려준다. 아무 상태도 바꾸지 않는다.

    `hermes -p <profile>`로 부른다. `hermes profile create`가 만들어주는 부서 이름
    Wrapper(`accounting-portfolio-department.bat`)는 쓰지 않는다 - 그 Wrapper는
    `~/.local/bin`에 생기고 PATH에 없을 수 있으며, 내용도 이 명령 한 줄이다.

    `resume` 는 이전 세션을 이어받는다. 사용자 질의 하나가 여러 부서를 거칠 때
    CEO 가 **자기가 뭘 시켰는지 기억한 채로** 종합해야 하기 때문이다(`ceo_intake`).

    `enabled` 는 어느 스위치가 이 호출을 허가했는지 호출자가 명시하는 자리다.
    기본은 지금까지처럼 `ENABLE_AGENT_ASK` 다.
    """
    if not (ENABLE_AGENT_ASK if enabled is None else enabled):
        raise HTTPException(
            503,
            "Agent 질의는 인증·Tool Allowlist 연결 전까지 기본 비활성화 상태입니다.",
        )

    # ▶ 기본은 부서 Profile 의 `agent.timeout_seconds` 다. 그 값은 **한 번 묻고 한 번
    #   답하는** 조회용으로 잡혀 있다(CEO 는 30초). 사용자 입구의 라우팅·종합 턴은
    #   도구를 여러 번 부르는 다른 작업이라 같은 초를 쓰면 매번 잘린다
    #   (2026-08-11 실측: 30초에서 CEO 라우팅이 통째로 끊겼다). 그래서 그런 호출은
    #   자기 timeout 을 명시한다 - 저장소의 부서 설정을 건드리지 않으려는 것이다.
    timeout = timeout_of(config) if timeout is None else timeout
    tail = ["chat", "-Q"]
    if resume:
        tail += ["--resume", resume]
    tail += ["-q", query]
    try:
        # ponytail: 요청마다 CLI 프로세스를 새로 띄운다(호출당 ~20s, 대화 이어짐 없음).
        # 상시 연결이 필요해지면 `hermes serve`(JSON-RPC/WebSocket, 기본 9119)로 바꾼다.
        # shell=False. 사용자 문자열이 셸을 거치지 않게 인자 리스트로만 넘긴다.
        # -Q: 배너·스피너·Tool Preview 없이 최종 답변만. 이게 없으면 ANSI 색코드와
        # 박스 문자가 그대로 화면까지 흘러간다.
        proc = subprocess.run(
        argv_for(department, tail),
        check=False, capture_output=True, text=True,
        # 컨테이너 안 출력은 UTF-8 이다. 윈도우 기본(cp949)으로 디코드하면
        # 한국어 답변이 깨진 채로 화면까지 간다.
        encoding="utf-8", errors="replace",
        timeout=timeout, cwd=ROOT,
        )
    except FileNotFoundError:
        # Hermes Runtime은 PyPI 패키지가 아니라 별도 설치다(CLAUDE.md).
        raise HTTPException(503, f"Hermes CLI 없음: hermes -p {department}")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"{timeout}s 초과")

    if proc.returncode != 0:
        raise HTTPException(502, (proc.stderr or "").strip()[:500] or "agent failed")

    return {
        "department": department,
        # -Q 여도 stdout 에 박스 테두리가 한 줄 섞여 나온다(2026-08-11 실측:
        # `┌─ Reasoning ───┐` 머리글만 stdout, 본문은 stderr). 그대로 두면
        # 사용자 화면에 테두리 문자가 찍힌다.
        "answer": clean_answer(proc.stdout),
        # 어느 Hermes Session이 이 문장을 만들었는지. 감사 추적에 필요하다.
        "session_id": session_id_of(proc.stderr or ""),
        # 화면이 이 값을 수치로 쓰지 못하게 계약에 박아둔다.
        "authoritative": False,
        "source_of_record": "/ui/snapshot",
    }


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# 한 줄 전체가 박스 chrome 인 경우만 지운다. 답변 안에 표(│)가 들어 있을 수 있으므로
# "│ 로 시작한다" 같은 넓은 규칙은 쓰지 않는다 - 내용을 지우는 쪽이 더 나쁘다.
_BOX_LINE = re.compile(r"^[┌└├┬┴┼]─.*$|^[│─┌┐└┘├┤┬┴┼\s]*$")
_REASONING_OPEN = re.compile(r"^┌─\s*Reasoning")
# 추론 헤드라인은 **한 줄 전체가** 굵은 글씨 토막들로만 이루어져 있다
# (실측: `**A****A****B**` 처럼 같은 문구가 두 번씩 붙어 나온다).
_REASONING_LINE = re.compile(r"^(?:\s*\*\*[^*]+\*\*\s*)+$")


def clean_answer(stdout: str) -> str:
    """`-Q` 로도 남는 터미널 chrome 과 추론 스트림을 걷어낸다.

    ▶ 실측 형태 (2026-08-11, 도구를 쓰는 턴):
        \\n┌─ Reasoning ────────┐\\n**A****A****B**\\n...\\n13\\n
      **닫는 테두리가 없다.** 그래서 "여는 테두리~닫는 테두리 사이를 버린다"는
      규칙은 쓸 수 없고, 추론 헤드라인 줄을 모양으로 알아본 뒤 **처음으로 그
      모양이 아닌 줄에서 멈춘다** - 그 아래는 전부 답변이다. 답변 안에
      `**결론**` 같은 줄이 있어도 그때는 이미 스트립을 끝낸 뒤라 살아남는다.
    """
    lines: list[str] = []
    in_reasoning = False
    stripping = True  # 답변 본문이 시작되기 전까지만 걷어낸다
    for raw in _ANSI.sub("", stdout).replace("\r", "\n").split("\n"):
        text = raw.strip()
        if stripping:
            if _REASONING_OPEN.match(text):
                in_reasoning = True
                continue
            if _BOX_LINE.match(text):
                continue
            if in_reasoning and _REASONING_LINE.match(text):
                continue
            if not text:
                continue
            stripping = False  # 여기서부터 답변이다
        lines.append(raw.rstrip())
    return "\n".join(lines).strip()


def session_id_of(stderr: str) -> str | None:
    """Hermes가 stderr에 찍는 `session_id: ...`를 뽑는다. 없으면 None."""
    for line in stderr.splitlines():
        if line.startswith("session_id: "):
            return line[len("session_id: "):].strip()
    return None


def timeout_of(config: str) -> int:
    """저장소 Profile의 agent.timeout_seconds를 그대로 쓴다. 부서마다 다르다."""
    cfg = yaml.safe_load((ROOT / config).read_text(encoding="utf-8"))
    return int(cfg["agent"]["timeout_seconds"])


if __name__ == "__main__":  # 자체 점검 - pytest 미도입(CLAUDE.md)
    # 실측 형태(2026-08-11): 추론 헤드라인 뒤에 답이 온다
    real = (
        "\n┌─ Reasoning ─────────┐\n"
        "**Searching repo****Searching repo****Inspecting CLI**\n"
        "**Inspecting CLI****Clarifying criteria**\n"
        "13\n"
    )
    assert clean_answer(real) == "13", repr(clean_answer(real))
    # 추론이 없으면 그대로
    assert clean_answer("\n입구 연결 확인\n") == "입구 연결 확인"
    # 답변 안의 굵은 제목은 지우지 않는다 - 본문이 시작된 뒤엔 스트립을 멈춘다
    kept = clean_answer("┌─ Reasoning ─┐\n**Thinking**\n결론입니다.\n**요약**\n- 한 줄\n")
    assert kept == "결론입니다.\n**요약**\n- 한 줄", repr(kept)
    # 표 문자는 살린다
    assert clean_answer("| a | b |\n│ 표 │ 유지 │\n") == "| a | b |\n│ 표 │ 유지 │"

    # 컨테이너 모드에서 모르는 프로필은 이름을 지어내지 않고 거절한다
    saved = HERMES_EXEC_MODE
    try:
        globals()["HERMES_EXEC_MODE"] = "docker"
        assert argv_for("ceo-agent", ["chat"])[:3] == ["docker", "exec", "-i"]
        try:
            argv_for("없는-부서", ["chat"])
        except HTTPException:
            pass
        else:
            raise AssertionError("모르는 프로필인데 argv 를 만들었다")
    finally:
        globals()["HERMES_EXEC_MODE"] = saved

    assert session_id_of("session_id: 20260811_x\n") == "20260811_x"
    assert session_id_of("아무것도 없음") is None
    print("hermes_cli 자체 점검 통과")
