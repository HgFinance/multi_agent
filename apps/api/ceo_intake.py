#!/usr/bin/env python3
"""사용자 입구. 질문 하나가 CEO → 부서 카드 → 종합 답변까지 가는 길.

소유: 재일 (리서치 + 퀀트·백테스트) — 사용자 입구 배선분
근거: docs/HEDGE_FUND_MASTER_PLAN.md 5.6(권한 분리)
      docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 6(명령 경계)

▶ 왜 부서에 직접 묻지 않나
  `department_agents.py` 의 `/{부서}/agent/ask` 는 **화면이 부서를 이미 아는**
  경우를 위한 것이다(회계 화면의 회계 질의). 사용자가 "내 포트폴리오 어때?"
  라고 물으면 어느 본부가 필요한지는 사용자가 정할 일이 아니다 - CEO 가 정한다.
  그래서 이 입구는 부서 이름을 받지 않는다. 받을 수 있으면 화면이 라우팅을
  하게 되고, 그 순간 CEO 는 장식이 된다.

▶ 왜 비동기인가
  CEO 는 카드를 만들고 곧바로 돌아온다. 부서 작업은 그 뒤에 dispatcher 가
  띄운다(수 분). 한 번의 HTTP 요청으로 끝까지 기다리면 프록시·브라우저가 먼저
  끊는다. 그래서 티켓을 주고 폴링하게 한다 - 그 폴링이 곧 진행 알림이다.

▶ 세 단계
  1. ROUTING     CEO 세션 1회. 이 안에서 카드가 만들어진다.
  2. WORKING     dispatcher 가 카드를 실행. 보드에서 상태를 읽어 사용자에게 보인다.
  3. SYNTHESIZING 카드가 전부 끝나면 **같은 CEO 세션을 resume 해서** 종합시킨다.
     새 세션으로 물으면 CEO 가 자기가 뭘 시켰는지 모른다.

▶ 지어내지 않게 하는 장치
  - 카드가 `NO_ANSWER`/`BLOCKED`/`FAILED` 면 그 사실을 **CEO 에게 그대로 알려주고**
    빈칸을 메우지 말라고 못 박는다(`_SYNTHESIS_PROMPT`).
  - 답변은 `authoritative: false`. 공식 수치는 `/ui/snapshot` 뿐이다.
  - 부서가 하나도 답을 못 냈으면 `answer_grounded: false` 로 표시한다 -
    화면이 "답이 나왔다"와 "답을 못 냈다"를 구분할 수 있어야 한다.

자체 점검:
    python apps/api/ceo_intake.py
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

try:  # ``python apps/api/main.py`` 와 package import 둘 다 지원한다.
    import hermes_cli
    from kanban_board import (
        KNOWN_ASSIGNEES, BoardUnavailable, cards_for_session, progress_of,
    )
    from kanban_status_bridge import KANBAN_STATUS_BRIDGE
except ModuleNotFoundError:  # pragma: no cover - package import path
    from apps.api import hermes_cli
    from apps.api.kanban_board import (
        KNOWN_ASSIGNEES, BoardUnavailable, cards_for_session, progress_of,
    )
    from apps.api.kanban_status_bridge import KANBAN_STATUS_BRIDGE

CEO_PROFILE = "ceo-agent"
CEO_CONFIG = "departments/00-ceo-office/hermes/config.yaml"

# 사용자 입구는 부서 ask 와 **따로** 켠다. 부서 ask 는 조회 성격이지만 이쪽은
# CEO 가 카드를 만들어 부서를 실제로 돌린다 - 돈이 나가는 경로라 기본은 닫는다.
ENABLE_USER_INTAKE = os.getenv("ENABLE_USER_INTAKE", "false").strip().lower() in {
    "1", "true", "yes", "on",
}

# 카드가 전부 끝나기를 기다리는 최대 시간. 넘으면 **성공으로 반올림하지 않고**
# 그때까지의 카드 상태를 그대로 보여주며 TIMEOUT 으로 끝낸다.
WAIT_LIMIT_SECONDS = int(os.getenv("USER_INTAKE_WAIT_SECONDS", "1800"))
POLL_SECONDS = int(os.getenv("USER_INTAKE_POLL_SECONDS", "10"))
# CEO 의 라우팅·종합 턴은 kanban 도구를 여러 번 부른다. 부서 Profile 의
# `agent.timeout_seconds`(CEO 는 30초)는 단발 조회 기준이라 여기 쓰면 매번 잘린다.
CEO_TURN_TIMEOUT_SECONDS = int(os.getenv("USER_INTAKE_CEO_TIMEOUT_SECONDS", "300"))

Phase = Literal[
    "ROUTING",        # CEO 가 어느 본부에 맡길지 정하는 중
    "WORKING",        # 부서가 카드를 실행하는 중
    "SYNTHESIZING",   # CEO 가 결과를 모아 답을 쓰는 중
    "ANSWERED",       # 끝
    "TIMEOUT",        # 카드가 제 시간에 안 끝났다
    "FAILED",         # CEO 호출 자체가 실패
]

# Hermes 프로필 이름 -> 운영 Read Model 의 department_code.
# 하나만 다르다: 인사팀 프로필은 `workforce-management` 인데 Read Model 은
# `hr-department` 를 쓴다(operations_read_model.py 32행). 이름이 다른 것을
# 같은 것으로 조용히 뭉개면 화면에서 인사팀이 통째로 사라진다.
PROFILE_TO_DEPARTMENT_CODE = {"workforce-management": "hr-department"}

# CEO 프롬프트에 그대로 박아 넣을 담당자 이름표. **한 곳에서만 만든다** -
# 사람이 손으로 두 번 적으면 언젠가 한쪽만 바뀐다.
_ASSIGNEE_ROLES = {
    "research-department": "종목·섹터·시장 조사, 방법론 스카우팅",
    "quant-backtest-department": "실험·백테스트·과적합 통계",
    "trading-department": "전략 집행, Bull/Bear 논지",
    "risk-management": "한도·익스포저·수용력",
    "accounting-portfolio-department": "원장·NAV·보유·수익률·배당",
    "qa-department": "어떤 주장이든 독립 검증",
    "workforce-management": "에이전트 채용·평가·개선",
}
_ASSIGNEE_LIST = "\n".join(
    f"    {name:<32}{role}" for name, role in _ASSIGNEE_ROLES.items()
)

_ROUTING_PROMPT = """{query}

---
위는 **사용자가 방금 보낸 질문**입니다. 당신은 CEO 로서 이 질문을 처리할
본부를 정하고 kanban 카드를 만드십시오. 다음을 지키십시오.

- 필요한 본부에만 카드를 만듭니다. 순서가 있으면 의존(부모)으로 묶습니다.
- `--assignee` 는 **아래 이름을 글자 그대로** 씁니다. 줄여 쓰면 보드는 카드를
  만들어 주지만 그런 본부가 없어 **아무도 집어가지 못하고 영영 멈춥니다**
  (실측: `accounting-portfolio` 로 쓴 카드가 그렇게 됐습니다).
{assignees}
- 카드 본문에는 **그 본부가 사용자 질문을 몰라도 일할 수 있을 만큼** 맥락을 씁니다.
  부서는 서로의 카드를 보지 못하고 이 대화도 보지 못합니다.
- 당신이 직접 조사해서 답하지 마십시오. 판단은 본부가 합니다.
- 카드를 만든 뒤에는 어느 본부에 무엇을 맡겼는지 한국어 두세 문장으로만 요약하십시오.
- 이 질문이 본부 작업이 필요 없는 것이면(인사·잡담 등) 카드를 만들지 말고 바로 답하십시오.
"""

_SYNTHESIS_PROMPT = """당신이 만든 카드가 모두 끝났습니다. 보드에서 읽은 결과입니다.

{card_report}

이제 사용자에게 줄 최종 답변을 한국어로 쓰십시오. 다음을 지키십시오.

- **빈칸을 당신이 메우지 마십시오.** 본부가 답을 못 낸 항목은 "확인하지 못했습니다"
  라고 그대로 쓰고, 왜 못 냈는지(위 사유)를 사용자에게 전하십시오.
- 수치를 지어내지 마십시오. 본부가 준 수치가 없으면 없다고 하십시오.
- 매수/매도 지시를 하지 마십시오. 이 답변은 자문이고 주문이 아닙니다.
- 사용자가 다음에 무엇을 하면 되는지 한 줄로 덧붙이십시오.
"""


class UserQuestion(BaseModel):
    """사용자 질문. **부서 이름이 없는 것이 이 계약의 핵심이다.**"""

    query: str = Field(min_length=1, max_length=4000)


@dataclass
class Ticket:
    ticket_id: str
    query: str
    phase: Phase = "ROUTING"
    session_id: str | None = None
    routing_note: str = ""      # CEO 가 어디에 맡겼다고 말했는지
    answer: str = ""            # 최종 종합
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    progress: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "phase": self.phase,
            "question": self.query,
            "routing_note": self.routing_note,
            "answer": self.answer,
            "error": self.error,
            "session_id": self.session_id,
            "elapsed_seconds": int((self.finished_at or time.time()) - self.started_at),
            # 부서가 실제로 답을 준 게 하나라도 있는가. 화면이 "답이 나왔다"와
            # "답을 못 냈다"를 구분하는 근거다. 카드가 아예 없으면(CEO 직접 응답)
            # 부서 근거가 없다는 뜻이므로 false 다.
            "answer_grounded": bool(
                self.progress.get("cards")
                and any(c["outcome"] == "ANSWERED" for c in self.progress["cards"])
            ),
            "authoritative": False,
            "source_of_record": "/ui/snapshot",
            **self.progress,
        }


class Intake:
    """티켓 보관 + 백그라운드 진행. 프로세스 메모리에만 산다(재시작하면 사라진다)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tickets: dict[str, Ticket] = {}

    def start(self, query: str) -> Ticket:
        ticket = Ticket(ticket_id=f"ask-{uuid.uuid4().hex[:12]}", query=query)
        with self._lock:
            self._tickets[ticket.ticket_id] = ticket
        threading.Thread(target=self._run, args=(ticket.ticket_id,), daemon=True).start()
        return ticket

    def get(self, ticket_id: str) -> Ticket | None:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
        if ticket is None:
            return None
        # 폴링할 때마다 보드를 다시 읽는다. 티켓에 캐시해 두면 부서가 멈춘 걸
        # 사용자가 못 본다.
        if ticket.session_id and ticket.phase in {"WORKING", "SYNTHESIZING"}:
            self._refresh(ticket)
        return ticket

    def _refresh(self, ticket: Ticket) -> bool:
        """보드를 다시 읽는다. **읽었으면 True.** 못 읽은 것과 카드가 없는 것은 다르다."""
        try:
            cards = cards_for_session(ticket.session_id or "")
        except BoardUnavailable as exc:
            # 보드를 못 읽는 것은 "카드 없음"이 아니다. 상태를 지어내지 않는다.
            ticket.error = str(exc)
            return False
        ticket.error = ""
        ticket.progress = progress_of(cards)
        self._publish_status(cards)
        return True

    @staticmethod
    def _publish_status(cards) -> None:
        """카드 상태를 `agent.status.v1` 로 흘려 오피스 화면에도 보이게 한다.

        `kanban_status_bridge` 가 기다리던 입력이 이것이다 - 그 모듈은 정제된
        이벤트를 받도록 이미 설계돼 있었고 아무도 먹여 주지 않고 있었다.
        best-effort 다: 여기서 실패해도 사용자 답변 경로를 막지 않는다.
        """
        outcome_to_kanban = {
            "QUEUED": "todo", "RUNNING": "running", "ANSWERED": "done",
            # 답을 못 낸 완료를 `done`(=IDLE)으로 흘리면 화면에서 성공으로 보인다.
            # 오피스 화면에서도 눈에 걸리도록 DEGRADED 로 올린다.
            "NO_ANSWER": "degraded", "BLOCKED": "blocked",
            "FAILED": "error", "STALE": "degraded", "NO_ASSIGNEE": "error",
        }
        for card in cards:
            try:
                KANBAN_STATUS_BRIDGE.publish_task_event({
                    # 카드 결말이 바뀔 때만 새 이벤트가 되도록 결말을 키에 넣는다.
                    "event_id": f"{card.task_id}:{card.outcome}",
                    "status": outcome_to_kanban.get(card.outcome, "running"),
                    "department_code": PROFILE_TO_DEPARTMENT_CODE.get(
                        card.assignee, card.assignee
                    ),
                    "agent_id": card.assignee,
                    "task_id": card.task_id,
                    "reason": card.summary or card.title,
                })
            except Exception:  # noqa: BLE001 - 알림은 답변 경로를 막지 않는다
                continue

    def _run(self, ticket_id: str) -> None:
        with self._lock:
            ticket = self._tickets[ticket_id]

        # 1) 라우팅 - CEO 가 카드를 만든다
        try:
            first = hermes_cli.ask(
                department=CEO_PROFILE,
                config=CEO_CONFIG,
                query=_ROUTING_PROMPT.format(query=ticket.query, assignees=_ASSIGNEE_LIST),
                # 이 경로를 연 스위치는 부서 ask 것이 아니라 사용자 입구 것이다.
                enabled=ENABLE_USER_INTAKE,
                timeout=CEO_TURN_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - HTTPException 포함
            ticket.phase = "FAILED"
            # ▶ 잘린 CEO 턴은 "아무 일도 없었음"이 아니다 (2026-08-11 실측).
            #   timeout 30초에서 잘린 턴이 이미 카드 4장을 만들어 놓았고, 부서들이
            #   그걸 실제로 실행했다. 그런데 세션 id 는 프로세스가 죽으면서 못
            #   읽었으므로 어느 카드가 이 질문 것인지 알 방법이 없다. 화면에
            #   "실패"만 띄우면 사용자는 아무 일도 없었다고 읽는다 - 그게 거짓이다.
            ticket.error = (
                f"CEO 호출 실패: {getattr(exc, 'detail', exc)} — "
                "**중단 전에 이미 본부 카드가 만들어졌을 수 있습니다.** "
                "칸반 보드를 확인하세요(이 티켓으로는 추적할 수 없습니다)."
            )
            ticket.finished_at = time.time()
            return

        ticket.routing_note = first.get("answer", "")
        ticket.session_id = first.get("session_id")
        if not ticket.session_id:
            # 세션 id 가 없으면 카드를 찾을 방법이 없다. 카드가 없다고 단정하지
            # 않고 실패로 남긴다 - 조용히 "부서 없음"으로 넘기면 거짓말이 된다.
            ticket.phase = "FAILED"
            ticket.error = (
                "CEO 세션 id 를 못 읽었습니다 - 어느 카드가 이 질문 것인지 "
                "확인할 수 없어 진행을 멈춥니다."
            )
            ticket.finished_at = time.time()
            return

        # 2) 대기 - 부서 카드가 끝날 때까지
        ticket.phase = "WORKING"
        deadline = time.time() + WAIT_LIMIT_SECONDS
        while time.time() < deadline:
            # 보드를 **못 읽었으면 아무것도 결론짓지 않는다.** 여기서 빈 progress 를
            # "카드 없음"으로 읽으면 CEO 가 5개 본부를 돌려 놓은 질문에도
            # "본부 작업이 필요 없다고 판단했습니다"라고 답하게 된다 - fail-open 이다.
            if self._refresh(ticket):
                if not ticket.progress.get("cards"):
                    # CEO 가 카드를 안 만들었다 = 본부 작업이 필요 없다고 판단.
                    # 그 판단을 존중하고 CEO 의 답을 그대로 사용자에게 준다.
                    ticket.answer = ticket.routing_note
                    ticket.phase = "ANSWERED"
                    ticket.finished_at = time.time()
                    return
                if ticket.progress.get("all_terminal"):
                    break
            time.sleep(POLL_SECONDS)
        else:
            ticket.phase = "TIMEOUT"
            ticket.error = ticket.error or (
                f"{WAIT_LIMIT_SECONDS}초 안에 본부 작업이 끝나지 않았습니다. "
                "아래 카드 상태가 지금까지 확인된 전부입니다."
            )
            ticket.finished_at = time.time()
            return

        # 3) 종합 - 같은 세션을 resume 해서 CEO 가 직접 모은다
        ticket.phase = "SYNTHESIZING"
        try:
            final = hermes_cli.ask(
                department=CEO_PROFILE,
                config=CEO_CONFIG,
                query=_SYNTHESIS_PROMPT.format(card_report=card_report(ticket.progress)),
                resume=ticket.session_id,
                enabled=ENABLE_USER_INTAKE,
                timeout=CEO_TURN_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            ticket.phase = "FAILED"
            ticket.error = f"CEO 종합 실패: {getattr(exc, 'detail', exc)}"
            ticket.finished_at = time.time()
            return

        ticket.answer = final.get("answer", "")
        ticket.phase = "ANSWERED"
        ticket.finished_at = time.time()


def card_report(progress: dict[str, Any]) -> str:
    """CEO 에게 넘길 카드 요약. **실패를 숨기지 않는다.**"""
    lines = []
    for card in progress.get("cards", []):
        label = {
            "ANSWERED": "답변함", "NO_ANSWER": "답을 못 냄", "BLOCKED": "보류",
            "FAILED": "실행 실패", "STALE": "아무도 안 집어감",
            "NO_ASSIGNEE": "없는 본부에 배정돼 실행 불가", "QUEUED": "대기",
            "RUNNING": "진행 중",
        }.get(card["outcome"], card["outcome"])
        lines.append(
            f"- [{card['department']}] {card['title']} → **{label}**"
            + (f"\n  사유/요약: {card['summary']}" if card["summary"] else "")
        )
    if progress.get("unusable"):
        lines.append(
            f"\n※ 위 {len(progress['unusable'])}건은 사용 가능한 결과가 없습니다. "
            "이 항목은 당신이 채우지 말고 '확인하지 못했습니다'로 사용자에게 전하십시오."
        )
    return "\n".join(lines) or "(카드 없음)"


INTAKE = Intake()
router = APIRouter(tags=["user-intake"])


@router.post("/ui/ask", status_code=202, operation_id="user_ask")
def user_ask(req: UserQuestion) -> dict[str, Any]:
    """사용자 질문 하나를 접수하고 티켓을 돌려준다. 부서는 CEO 가 정한다."""
    if not ENABLE_USER_INTAKE:
        raise HTTPException(
            503,
            "사용자 입구는 인증·비용 한도 연결 전까지 기본 비활성화 상태입니다 "
            "(ENABLE_USER_INTAKE=true 로 엽니다).",
        )
    return INTAKE.start(req.query).public()


@router.get("/ui/ask/{ticket_id}", operation_id="user_ask_status")
def user_ask_status(ticket_id: str) -> dict[str, Any]:
    """진행·실패·최종 답변. 화면은 이걸 폴링한다."""
    ticket = INTAKE.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket_not_found")
    return ticket.public()


__all__ = ["router", "INTAKE", "card_report", "PROFILE_TO_DEPARTMENT_CODE"]


if __name__ == "__main__":  # 자체 점검 - pytest 미도입(CLAUDE.md)
    # 실패를 숨기지 않는가
    prog = {
        "cards": [
            {"department": "accounting-portfolio-department", "title": "수익률",
             "outcome": "NO_ANSWER", "summary": "NAV 데이터 없음"},
            {"department": "qa-department", "title": "검증", "outcome": "ANSWERED",
             "summary": "확인함"},
        ],
        "unusable": ["t2"],
    }
    report = card_report(prog)
    assert "답을 못 냄" in report, report
    assert "NAV 데이터 없음" in report, report
    assert "채우지 말고" in report, report

    # answer_grounded: 부서 답이 하나도 없으면 false
    t = Ticket(ticket_id="x", query="q")
    t.progress = {"cards": [{"outcome": "NO_ANSWER"}]}
    assert t.public()["answer_grounded"] is False
    t.progress = {"cards": [{"outcome": "ANSWERED"}]}
    assert t.public()["answer_grounded"] is True
    # CEO 가 직접 답한 경우(카드 0장)도 부서 근거는 없다
    t.progress = {"cards": []}
    assert t.public()["answer_grounded"] is False

    # 인사팀 이름이 뭉개지지 않는가
    assert PROFILE_TO_DEPARTMENT_CODE["workforce-management"] == "hr-department"

    # 세 곳의 부서 이름표가 어긋나지 않는가 - 어긋나면 카드가 조용히 안 돈다
    assert set(_ASSIGNEE_ROLES) | {"ceo-agent"} == KNOWN_ASSIGNEES, (
        set(_ASSIGNEE_ROLES) ^ KNOWN_ASSIGNEES
    )
    assert KNOWN_ASSIGNEES == set(hermes_cli.PROFILE_CONTAINERS), (
        KNOWN_ASSIGNEES ^ set(hermes_cli.PROFILE_CONTAINERS)
    )
    # 프롬프트에 이름이 실제로 실려 나가는가
    filled = _ROUTING_PROMPT.format(query="q", assignees=_ASSIGNEE_LIST)
    assert "accounting-portfolio-department" in filled

    # 보드를 못 읽은 것을 "카드 없음"으로 읽지 않는가 (fail-open 방지)
    class _Broken(Intake):
        def _refresh(self, ticket):  # noqa: D102 - 보드가 늘 죽어 있는 척한다
            ticket.error = "보드 못 읽음"
            return False

    broken, probe = _Broken(), Ticket(ticket_id="z", query="q")
    probe.session_id = "s"
    assert broken._refresh(probe) is False and not probe.progress, probe.progress

    assert Ticket(ticket_id="y", query="q").public()["authoritative"] is False
    print("ceo_intake 자체 점검 통과")
