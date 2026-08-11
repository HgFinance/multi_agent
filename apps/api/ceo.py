"""CEO Office query boundary for the operator UI."""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    from . import fact_router, hermes_cli
except ImportError:  # pragma: no cover - direct ``python apps/api/main.py`` path
    import fact_router  # type: ignore[no-redef]
    import hermes_cli  # type: ignore[no-redef]

from fastapi import APIRouter, HTTPException

from orchestration.canonical_profiles import canonical_profile_for_department

from apps.api.ceo_hermes_client import ask_ceo

try:
    from kanban_board import BoardUnavailable, cards_for_root, progress_of
    from kanban_status_bridge import KANBAN_STATUS_BRIDGE
except ModuleNotFoundError:  # pragma: no cover - package import path
    from apps.api.kanban_board import BoardUnavailable, cards_for_root, progress_of
    from apps.api.kanban_status_bridge import KANBAN_STATUS_BRIDGE


router = APIRouter(prefix="/ui/ceo", tags=["ceo-office"])

# ▶ CEO 턴 timeout 을 Profile 값(30초)에서 떼어낸다 (2026-08-11 실측)
#   `agent.timeout_seconds` 는 **한 번 묻고 한 번 답하는** 조회 기준이다. 이 경로의
#   CEO 턴은 카드를 만들며 kanban 도구를 여러 번 부르는 다른 작업이라 90~180초가
#   걸렸고, 30초로는 통째로 잘렸다. 더 나쁜 것은 **잘려도 카드는 이미 만들어져
#   있었다는 것**이다 - 부서 4곳이 실제로 돌았는데 사용자에게는 실패로 보였다.
#   저장소의 부서 설정(영주님 소유)을 건드리지 않으려고 여기서 따로 준다.
CEO_TURN_TIMEOUT_SECONDS = int(os.getenv("CEO_TURN_TIMEOUT_SECONDS", "300"))

# ▶ CEO 를 어떻게 부를 것인가
#   `api`(기본) - `ceo-hermes` 의 OpenAI 호환 엔드포인트. AWS 배포 경로다.
#   `cli`        - 컨테이너 안 `hermes chat`. `hermes serve` 인증 설정이 선행
#                  조건이라 로컬에서는 아직 그 엔드포인트가 없어서 쓰는 길이다
#                  (HERMES_DOCKER_RUNBOOK 4-2절).
#   **조용히 넘어가지 않는다.** URL 이 없다고 알아서 CLI 로 떨어지면, 배포에서
#   URL 을 빠뜨렸을 때 "그래도 돌아가네"가 되어 경계가 무너진다. 스위치가 명시적일 때만 바꾼다.
CEO_TRANSPORT = os.getenv("CEO_TRANSPORT", "api").strip().lower()
CEO_CONFIG = "departments/00-ceo-office/hermes/config.yaml"
ENABLE_CEO_CLI = os.getenv("ENABLE_CEO_CLI", "false").strip().lower() in {
    "1", "true", "yes", "on",
}


def _won(value: object) -> str:
    """원 단위 문자열을 사람이 읽는 형태로. **값을 바꾸지 않는다** - 자리수만 넣는다."""
    text = str(value or "").strip()
    if not text:
        return "(값 없음)"
    try:
        return f"{int(Decimal(text)):,}원"
    except (InvalidOperation, ValueError):
        return text


def _direct_answer_text(direct: dict[str, object]) -> str:
    """직행 조회 결과를 한국어 한 문단으로. **LLM 을 쓰지 않는다.**

    여기서 요약 모델을 부르면 0.5초짜리가 다시 몇십 초가 되고, 무엇보다
    숫자가 한 번 더 옮겨 적힌다 - 그 지점에서 값이 틀어지는 것을 실측했다.
    """
    if direct.get("unavailable"):
        return (
            f"조회하지 못했습니다 — {direct.get('reason')}\n"
            "본부에 맡기지 않았습니다. 같은 자료가 없어 결과가 같기 때문입니다."
        )
    fact = direct.get("fact") or {}
    assert isinstance(fact, dict)
    kind = fact.get("kind")
    observed = fact.get("observed_at", "")
    if kind == "broker_account":
        return (
            f"브로커 계좌 기준입니다({fact.get('environment', '')}).\n"
            f"- 평가금액 {_won(fact.get('equity'))}\n"
            f"- 현금 {_won(fact.get('cash'))} · 주문가능 {_won(fact.get('buying_power'))}\n"
            f"- 보유 종목 {fact.get('position_count')}개\n"
            f"- 조회시각 {observed}\n"
            "공식 NAV 가 아니라 브로커 조회값입니다. 원장 확정치와 다를 수 있고, "
            "차이 자체는 회계본부가 대사로 확인합니다."
        )
    if kind == "market_quote":
        return (
            f"{fact.get('symbol')} 현재가 {_won(fact.get('price'))} "
            f"(조회시각 {observed}).\n"
            "시세 조회값이며 매수·매도 권고가 아닙니다."
        )
    return str(fact)


_ARTIFACT_ROOTS = tuple(
    Path(p) for p in (
        os.getenv("KANBAN_ATTACH_ROOT",
                  str(Path.home() / ".hermes-shared-kanban" / "kanban" / "attachments")),
        os.getenv("KANBAN_WORKSPACE_ROOT",
                  str(Path.home() / ".hermes-shared-kanban" / "kanban" / "workspaces")),
    ))

# 종합 프롬프트에 실을 부서당 최대 글자. 넘치면 CEO 턴이 컨텍스트로 죽는다.
_FINDING_CHARS = int(os.getenv("CEO_FINDING_CHARS", "3000"))


def _artifact_text(task_id: str) -> str:
    """그 카드에서 부서가 남긴 것. **`result` 만 보지 않는다.**

    ▶ 완료 카드 21장이 전부 `result` 가 비어 있었고 산출물은 첨부나 작업공간
      파일로만 있었다(2026-08-11 실측). `result` 만 읽으면 부서가 몇 분씩 일한
      결과가 통째로 없는 것으로 보인다 - 실제로 그렇게 읽고 "부서가 아무것도
      안 했다"고 판단했다.
    """
    parts: list[str] = []
    for root in _ARTIFACT_ROOTS:
        folder = root / task_id
        if not folder.is_dir():
            continue
        for f in sorted(folder.rglob("*")):
            if f.is_file() and f.suffix in {".md", ".txt", ".json"}:
                try:
                    parts.append(f.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
    return "\n\n".join(parts)


def _child_findings(cards) -> list[dict[str, str]]:
    """뿌리 아래 부서 카드가 실제로 남긴 것. 뿌리 카드는 뺀다."""
    out: list[dict[str, str]] = []
    for c in cards:
        if getattr(c, "is_root", False):
            continue
        text = (_artifact_text(c.task_id) or c.result or c.summary or "").strip()
        out.append({
            "assignee": c.assignee or "(미배정)",
            "title": c.title,
            "outcome": str(c.outcome),
            # **빈손을 빈손이라고 적는다.** 여기서 조용히 빼면 CEO 는 그 본부가
            # 검토한 줄 알고 종합한다 - 안 한 것과 이상 없던 것이 섞인다.
            "text": text[:_FINDING_CHARS] if text else "",
        })
    return out


def _synthesis_prompt(query: str, findings: list[dict[str, str]]) -> str:
    lines = [
        "부서 검토가 끝났다. 아래는 각 본부가 **실제로 남긴 것**이다.",
        "이것만 근거로 사용자 질문에 답하라.\n",
        f"[사용자 질문]\n{query}\n",
        "[본부별 산출]",
    ]
    for f in findings:
        lines.append(f"\n── {f['assignee']} ({f['outcome']}) — {f['title']}")
        lines.append(f["text"] if f["text"] else
                     "  (산출 없음 - 이 본부는 검토 결과를 남기지 않았다. "
                     "검토했다고 쓰지 마라.)")
    lines.append(
        "\n[규칙]\n"
        "- 산출이 없는 본부를 '이상 없음'으로 읽지 마라. 안 한 것과 이상 없던 것은 다르다.\n"
        "- 수치는 본부가 적은 값을 **그대로** 옮겨라. 반올림·환산·요약하지 마라.\n"
        "- 근거가 모자라면 결론을 만들지 말고 **무엇이 없어서 못 정하는지** 적어라.\n"
        "- 너는 주문 제출·리스크 승인·원장 수정·NAV 확정 권한이 없다.")
    return "\n".join(lines)


def synthesize_root(root_task_id: str, query: str, cards) -> dict[str, object]:
    """자식 산출을 모아 **두 번째 CEO 턴**으로 종합한다.

    ▶ 왜 두 번째 턴인가 (2026-08-11 실측)
      CEO 는 카드를 만든 **같은 턴에** 답을 냈다. 자식이 아직 시작도 안 한
      시점이라 읽을 것이 없었고, 그래서 답변 안의 "리스크 HIGH/BLOCKING" 같은
      문장은 본부가 보고한 것이 아니라 CEO 가 혼자 쓴 것이었다. 본부 7곳이
      각각 몇 분씩 일한 결과는 한 글자도 답에 들어가지 않았다.
      카드를 만드는 턴과 종합하는 턴은 **다른 시점**이어야 한다.
    """
    findings = _child_findings(cards)
    if not findings:
        return {"answer": "", "grounded": False, "reason": "부서 카드가 없다"}
    result = _run_ceo_turn(query=_synthesis_prompt(query, findings))
    return {
        "answer": result.get("answer", ""),
        "session_id": result.get("session_id"),
        # 한 본부라도 실제 산출이 있어야 "근거 있는 종합"이다
        "grounded": any(f["text"] for f in findings),
        "departments_with_output": [f["assignee"] for f in findings if f["text"]],
        "departments_silent": [f["assignee"] for f in findings if not f["text"]],
    }


def _run_ceo_turn(query: str) -> dict[str, object]:
    """CEO 한 턴. transport 만 다르고 계약은 같다."""
    if CEO_TRANSPORT == "cli":
        return hermes_cli.ask(
            department=canonical_profile_for_department("ceo"),
            config=CEO_CONFIG,
            query=query,
            timeout=CEO_TURN_TIMEOUT_SECONDS,
            enabled=ENABLE_CEO_CLI,
        )
    return ask_ceo(query=query, timeout=CEO_TURN_TIMEOUT_SECONDS)

# Hermes 프로필 이름 -> 운영 Read Model 의 department_code.
# 둘이 어긋나는 이름이 생기면 화면에서 그 본부가 통째로 사라지므로 표로 둔다.
PROFILE_TO_DEPARTMENT_CODE: dict[str, str] = {}


@router.post("/ask", operation_id="ceo_query")
def ceo_query(req: hermes_cli.AgentAsk) -> dict[str, object]:
    """Send a non-binding natural-language query to the CEO Hermes Head.

    ▶ 사실 조회는 여기서 끝난다 (2026-08-11)
      "내 계좌 잔고" 같은 질문은 CEO 를 거치지 않는다. 실측에서 그 한 마디가
      CEO 라우팅 90~180초 + 부서 5곳(각 3~6분) + 종합을 태우고 5/5 "산출 불가"로
      끝났다. 같은 입력에 같은 답이 나오는 질문에 LLM 7번을 쓸 이유가 없다.
      분류는 결정론이고(`fact_router.classify`), **애매하면 에이전트 경로로 간다.**
    """
    direct = fact_router.answer(req.query)
    if direct is not None:
        # 카드를 만들지 않는다 - 부서가 할 일이 없는 질문이다.
        return {
            "schema_version": "ceo.query-result.v1",
            "department": "ceo-agent",
            "binding": False,
            "task": None,
            "answer": _direct_answer_text(direct),
            "session_id": None,
            # 화면이 수치를 여기서 가져가게 하려고 원본을 같이 싣는다.
            # 에이전트 문장에서 숫자를 옮겨 적지 않는다(로컬 모델이 5억을
            # "500만 원"으로 옮겨 쓴 것을 실측했다).
            "direct": direct,
        }

    task = hermes_cli.create_kanban_task(
        assignee=canonical_profile_for_department("ceo"),
        title=f"사용자 질의: {req.query[:120]}",
        body=req.query,
        idempotency_key=req.request_id,
    )
    if not task or not task.get("task_id"):
        # The root task is the durable anchor for the closed-loop workflow.
        # Never call the CEO after this boundary failed: otherwise the user
        # receives an answer with no Kanban graph to supervise.
        raise HTTPException(
            status_code=503,
            detail="CEO root Kanban task를 생성하지 못했습니다. Hermes Kanban runtime을 확인하세요.",
        )

    result = _run_ceo_turn(
        query=(
            "Closed-loop Kanban context: the durable CEO root task is "
            f"{task['task_id']}. Use this task as the parent for every "
            "dynamic child task and keep the workflow closed-loop.\n\n"
            # 이름을 줄여 쓰면 카드는 만들어지지만 아무도 집어가지 못한다
            # (2026-08-11 실측: `accounting-portfolio` 로 만든 카드가 그렇게 됐다).
            "Use these exact assignee names, verbatim:\n"
            "  research-department / quant-backtest-department / trading-department /\n"
            "  risk-management / accounting-portfolio-department / qa-department /\n"
            "  hr-department\n\n"
            # ▶ **산출을 남기라고 시켜야 남는다** (2026-08-11 실측). 완료 카드
            #   21장 중 20장이 회수 가능한 산출이 하나도 없었다 - 본부가 몇 분씩
            #   일하고 텍스트를 냈지만 어디에도 안 남아 종합에 못 쓰였다.
            "Every child task body MUST end with this instruction, verbatim:\n"
            "  '[산출 규칙] 검토 결과를 반드시 파일로 남겨라(예: findings.md).\n"
            "   남기지 않으면 종합 단계에서 이 본부는 「산출 없음」으로 처리되고,\n"
            "   네가 한 검토는 답변에 반영되지 않는다.\n"
            "   수치는 출처와 함께 적고, 없는 값을 0 으로 채우지 마라.\n"
            "   확인 못 한 것은 확인 못 했다고 적어라.'\n\n"
            # 이 턴은 계획이다. 답은 본부가 끝난 뒤 두 번째 턴에서 만든다.
            "This turn is PLANNING ONLY. Do not state conclusions about the "
            "user's question yet - the departments have not run. Reply with the "
            "plan and which departments you tasked.\n\n"
            f"Original user request:\n{req.query}"
        ),
    )
    return {
        "schema_version": "ceo.query-result.v1",
        "department": "ceo-agent",
        "binding": False,
        "task": task,
        "answer": result["answer"],
        "session_id": result.get("session_id"),
    }


@router.get("/ask/{root_task_id}", operation_id="ceo_query_progress")
def ceo_query_progress(root_task_id: str) -> dict[str, object]:
    """뿌리 카드에 매달린 본부 카드들의 진행·실패. 화면은 이걸 폴링한다.

    ▶ 왜 Kanban 대시보드 임베드로 충분하지 않은가
      임베드는 **보드 원본**을 보여준다. 거기서 회계 카드는 `done` 이었지만
      결과 본문이 비어 있었다 - "NAV 데이터가 없어 산출할 수 없습니다"를 완료로
      기록한 것이다. 원본만 보면 그건 성공으로 읽힌다. 이 경로는 같은 카드를
      "답이 됐는가" 기준으로 다시 판정해서 준다(`kanban_board` 의 fail-closed 규칙).
    """
    try:
        cards = cards_for_root(root_task_id)
    except BoardUnavailable as exc:
        # 못 읽은 것을 "카드 없음"으로 바꾸지 않는다. 빈 목록을 주면 화면이
        # "아무 일도 없었음"으로 읽는다.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not cards:
        raise HTTPException(status_code=404, detail="ceo_root_task_not_found")

    _publish_status(cards)
    return {
        "schema_version": "ceo.query-progress.v1",
        "root_task_id": root_task_id,
        # **본부가** 실제로 답을 준 게 하나라도 있는가. 뿌리 카드는 세지 않는다 -
        # CEO 가 자기 카드에 뭘 적었다고 본부 근거가 생기는 것은 아니다.
        "answer_grounded": any(
            c.outcome == "ANSWERED" and not c.is_root for c in cards
        ),
        "authoritative": False,
        "source_of_record": "/ui/snapshot",
        **progress_of(cards),
    }


@router.post("/ask/{root_task_id}/synthesize", operation_id="ceo_query_synthesize")
def ceo_query_synthesize(root_task_id: str, query: str = "") -> dict[str, object]:
    """부서 검토가 끝난 뒤 **종합한 최종 답**. 이게 없으면 부서 일이 버려진다.

    ▶ 왜 별도 호출인가
      카드를 만드는 턴에는 자식이 아직 시작도 안 했다. 같은 턴에 답하면 그 답은
      부서 산출이 아니라 CEO 의 추측이다(실측: 본부 7곳이 각각 몇 분씩 일한
      결과가 답에 한 글자도 안 들어갔다).

    ▶ 아직 안 끝났으면 **종합하지 않는다.** 진행 중인 본부를 빼고 종합하면
      그 본부는 영영 '의견 없음'으로 굳는다.
    """
    try:
        cards = cards_for_root(root_task_id)
    except BoardUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not cards:
        raise HTTPException(status_code=404, detail="ceo_root_task_not_found")

    children = [c for c in cards if not c.is_root]
    pending = [c for c in children if not c.is_terminal]
    if pending:
        return {
            "schema_version": "ceo.query-synthesis.v1",
            "root_task_id": root_task_id,
            "ready": False,
            "answer": "",
            "pending": [{"task_id": c.task_id, "assignee": c.assignee,
                         "outcome": str(c.outcome)} for c in pending],
        }

    root_query = query.strip() or next(
        (c.title for c in cards if c.is_root), "")
    out = synthesize_root(root_task_id, root_query, cards)
    return {
        "schema_version": "ceo.query-synthesis.v1",
        "root_task_id": root_task_id,
        "ready": True,
        "authoritative": False,
        **out,
    }


def _publish_status(cards) -> None:
    """카드 상태를 `agent.status.v1` 로 흘려 오피스 화면에도 보이게 한다.

    `kanban_status_bridge` 가 기다리던 입력이 이것이다 - 그 모듈은 정제된 이벤트를
    받도록 이미 설계돼 있었고 아무도 먹여 주지 않고 있었다. best-effort 다:
    여기서 실패해도 사용자 답변 경로를 막지 않는다.
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


__all__ = ["router", "CEO_TURN_TIMEOUT_SECONDS"]
