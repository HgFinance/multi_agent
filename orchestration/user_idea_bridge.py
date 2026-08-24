#!/usr/bin/env python3
"""사용자 아이디어 다리 - 사용자면의 제안을 공장면 리드 큐로 **한 방향** 옮긴다.

담당: 재일 (리서치본부 RES)

▶ 왜 다리가 따로 있어야 하나
  두 면은 물리적으로 갈려 있다 - 보드도 DB 도 dispatcher 도 다르다.
  그리고 사용자면 창구(liaison) 프로필은 `factory_submit_leads` 자체를
  **못 부른다**(MCP surface 에서 제거되고, 제거 실패 시 기동 거부). 그러니
  "사용자 카드에서 바로 리드를 넣는" 경로는 애초에 존재할 수 없다.
  건널 것은 건널목으로 건너야 한다.

▶ 무엇을 옮기나 - **검증된 것만, 한 방향으로**
    사용자 root 카드(shared-kanban, origin=user-query)
      └ 리서치 창구가 남긴 후보 코멘트  ─┐
                                        │  ① 원문 + 후보를 둘 다 읽어
                                        ▼  ② user_idea_language.verify 로 대조
                              ┌─────────────────┐
                              │   이 모듈(호스트) │
                              └─────────────────┘
        거부 ◀───────────────────┤ └──────────────▶ 통과
     root 에 되물음 코멘트                alpha-factory 에
     (사용자가 본다. 리드 없음)          origin=user-idea 카드
                                        (research-department)
                                              └▶ 그 에이전트가
                                                 factory_submit_leads 로 적재

▶ 반대 방향은 없다
  공장은 사용자면을 호출하지 않는다. 사용자에게 보이는 것은 이 다리가 root 에
  남기는 코멘트 한 줄뿐이다. `origin=user-idea` 는 `origin=user-query` 가
  아니므로 CEO supervisor 가 이 카드를 집어 자식을 만드는 일도 없다
  (`is_user_query_body()` 가 정확히 `"user-query"` 만 참으로 본다 - 실측).

▶ 지어내지 않는다
  후보 코멘트가 없으면 아무 일도 하지 않는다. 사용자의 질의를 이 모듈이
  해석해서 리드로 만들지 않는다 - 그건 결정론이 할 수 있는 일이 아니다.

사용:
    python3 user_idea_bridge.py --self-check
    python3 user_idea_bridge.py --once --dry-run
    python3 user_idea_bridge.py --once
    python3 user_idea_bridge.py --loop --interval-min 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_ROOT / "contracts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))

from orchestration.contracts.user_idea_lead import (  # noqa: E402
    CONTRACT_VERSION, IdeaRejection, UserIdeaCandidate, VerifiedIdeaLead,
)
from orchestration.user_idea_language import (  # noqa: E402
    MODULE_VERSION as LANG_VERSION, to_lead_block, verify,
)

MODULE_VERSION = "user-idea-bridge-v1"

# ── 두 면의 CLI 는 **절대 한 함수에서 분기하지 않는다** ──────────────────────
# 보드를 섞는 순간 어느 면에 썼는지 읽는 쪽이 알 수 없게 된다. 조립기를 둘로
# 두고, 각자 자기 컨테이너·자기 보드를 못박는다.
FACTORY_CONTAINER = os.getenv("KANBAN_CLI_CONTAINER",
                              "hedgefund-factory-kanban-dispatcher")
FACTORY_BOARD = os.getenv("FACTORY_KANBAN_BOARD", "alpha-factory")
USER_CONTAINER = os.getenv("USER_KANBAN_CLI_CONTAINER",
                           "hedgefund-kanban-dispatcher")

RESEARCH_ASSIGNEE = "research-department"
# 공장 카드의 발원 도장. `user-query` 가 **아니어야** 한다 - 그래야 CEO
# supervisor 가 이 카드를 사용자 워크플로로 오인해 자식을 만들지 않는다.
IDEA_ORIGIN_HEADER = (
    "origin=user-idea\n"
    "workflow_plane=alpha-factory\n"
    "user_query_routing=forbidden"
)

# 창구가 남기는 후보 코멘트의 껍데기. 여는 줄이 정확히 이것이어야 읽는다 -
# 산문 속의 비슷한 JSON 을 후보로 오인하지 않기 위해서다.
CANDIDATE_MARKER = "USER_IDEA_CANDIDATE_V1"
_CANDIDATE_RE = re.compile(
    rf"^{CANDIDATE_MARKER}\s*\n(?P<json>\{{.*\}})\s*$", re.S | re.M)
_BRIDGE_MARK = "[user-idea-bridge]"


def _run(argv: list[str], timeout: int = 60) -> tuple[int, str]:
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _factory_cli(*args: str) -> list[str]:
    """공장면 전용. 보드 slug 를 여기서 강제한다."""
    return ["docker", "exec", "-u", "1000", "-i", FACTORY_CONTAINER,
            "hermes", "kanban", "--board", FACTORY_BOARD, *args]


def _user_cli(*args: str) -> list[str]:
    """사용자면 전용. 보드 인자를 **주지 않는다**(기본 보드가 사용자면이다)."""
    return ["docker", "exec", "-u", "1000", "-i", USER_CONTAINER,
            "hermes", "kanban", *args]


# ── 후보 읽기 ───────────────────────────────────────────────────────────────
def extract_candidate(comment_body: str) -> UserIdeaCandidate | None:
    """코멘트에서 후보를 꺼낸다. 형식이 아니면 조용히 None(오탐 금지)."""
    m = _CANDIDATE_RE.search(str(comment_body or "").strip())
    if not m:
        return None
    try:
        return UserIdeaCandidate.model_validate_json(m.group("json"))
    except Exception:
        return None


def user_request_text(root_body: str) -> str:
    """root body 의 `## User request` 아래가 사용자 원문이다.

    이 구분자는 `ceo_workflow_scope.build_root_body()` 가 마지막에 붙인다.
    도장 줄들을 원문으로 착각해 해싱하면 후보가 억울하게 거부된다.
    """
    marker = "## User request"
    idx = str(root_body or "").find(marker)
    if idx < 0:
        return ""
    return root_body[idx + len(marker):].strip()


def _card_json(cli, task_id: str) -> dict:
    rc, text = _run(cli("show", task_id, "--json"))
    if rc != 0:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


# ── 행동 ────────────────────────────────────────────────────────────────────
def _reject_comment(rej: IdeaRejection) -> str:
    codes = ", ".join(c.value for c in rej.reason_codes)
    asks = "\n".join(f"  - {a}" for a in rej.needs_from_user) or "  - (없음)"
    return (f"{_BRIDGE_MARK} 아이디어를 공장 리드로 옮기지 못했습니다.\n"
            f"사유: {codes}\n필요한 것:\n{asks}\n"
            f"(검증기 {LANG_VERSION} / 계약 {CONTRACT_VERSION})")


def _idea_card_body(lead: VerifiedIdeaLead, root_task_id: str) -> str:
    """공장 카드 본문. **에이전트가 할 일은 하나** - 이 리드를 적재하는 것."""
    block = to_lead_block(lead)
    return (
        f"{IDEA_ORIGIN_HEADER}\n"
        f"factory_assignee={RESEARCH_ASSIGNEE}\n"
        f"user_idea_root_task_id={root_task_id}\n"
        f"raw_text_sha256={lead.raw_text_sha256}\n\n"
        "사용자가 낸 아이디어다. **검증기를 이미 통과했다** - 아래 블록의 모든\n"
        "주장은 사용자 원문의 실제 구간에서 나왔다(인용 대조 완료).\n\n"
        "이 카드의 임무는 **리드 적재 하나뿐이다**:\n"
        "1. 아래 블록을 그대로 `factory_submit_leads` 에 넣어라.\n"
        "   `lens=PRACTITIONER`, `source_type=COMMUNITY`,\n"
        "   `model_version=user-idea-intake-v1`,\n"
        f"   `prompt_version={CONTRACT_VERSION}`\n"
        "2. **블록을 고치지 마라.** 특히 MECHANISM·CLAIMED_EDGE 는 사용자의\n"
        "   말이다 - 더 그럴듯하게 바꾸면 그 순간 사용자 아이디어가 아니라\n"
        "   네 아이디어가 된다. 접수기가 거부하면 그 사유를 그대로 보고해라.\n"
        "3. 적재된 `lead_id` 를 카드에 적어라. 기획은 하지 마라 - 기획자 카드가\n"
        "   다음 주기에 이 리드를 재료로 집는다.\n\n"
        "▶ 이 아이디어가 이미 기각된 계열이면 **적재하지 말고** 그 사실과\n"
        "  교훈(무엇이 왜 기각됐는지)을 카드에 적어라. 같은 실험을 두 번 사는\n"
        "  것은 사용자 예산을 두 번 태우는 것이다.\n\n"
        "```\n" + block + "\n```\n"
    )


def process_root(root_task_id: str, *, dry_run: bool = False) -> dict:
    """뿌리 카드 하나를 처리한다. 반환은 무슨 일을 했는지의 기록."""
    detail = _card_json(_user_cli, root_task_id)
    if not detail:
        return {"root": root_task_id, "action": "SKIP", "why": "카드를 못 읽음"}

    task = detail.get("task") or {}
    comments = detail.get("comments") or []

    # 이미 처리했으면 다시 하지 않는다(다리는 여러 번 돌아도 카드는 하나다).
    for c in comments:
        if str(c.get("body") or "").startswith(_BRIDGE_MARK):
            return {"root": root_task_id, "action": "SKIP", "why": "이미 처리됨"}

    cand = None
    for c in reversed(comments):            # 가장 최근 후보를 쓴다
        cand = extract_candidate(c.get("body") or "")
        if cand is not None:
            break
    if cand is None:
        return {"root": root_task_id, "action": "NONE", "why": "후보 없음"}

    raw = user_request_text(task.get("body") or "")
    if not raw:
        return {"root": root_task_id, "action": "SKIP", "why": "원문 구분자 없음"}

    result = verify(raw, cand, root_task_id=root_task_id)

    if isinstance(result, IdeaRejection):
        if not dry_run:
            _run(_user_cli("comment", root_task_id, _reject_comment(result)))
        return {"root": root_task_id, "action": "REJECT",
                "codes": [c.value for c in result.reason_codes]}

    # 통과 - 공장 카드를 만든다. 멱등키는 뿌리 + 원문 해시다(같은 아이디어를
    # 두 번 걸지 않으면서, 사용자가 문장을 고치면 새 카드가 되게 한다).
    key = f"user-idea:{root_task_id}:{result.raw_text_sha256[:16]}"
    title = f"사용자 아이디어 접수: {result.title[:60]}"
    if dry_run:
        return {"root": root_task_id, "action": "WOULD_CREATE", "key": key}

    rc, out = _run(_factory_cli(
        "create", title,
        "--assignee", RESEARCH_ASSIGNEE,
        "--idempotency-key", key,
        "--created-by", MODULE_VERSION,
        "--priority", "2",
        "--body", _idea_card_body(result, root_task_id)))
    if rc != 0:
        return {"root": root_task_id, "action": "CREATE_FAILED",
                "why": out.strip()[:200]}

    card_id = ""
    m = re.search(r"\bt_[0-9a-f]{6,}\b", out)
    if m:
        card_id = m.group(0)
    _run(_user_cli("comment", root_task_id,
                   f"{_BRIDGE_MARK} 아이디어를 공장 리드 큐로 옮겼습니다.\n"
                   f"공장 카드: {card_id or '(id 미확인)'}\n"
                   f"출처로 기록된 주소: {result.source_url}\n"
                   "리드가 적재되면 기획자 카드가 다음 주기에 재료로 집습니다."))
    return {"root": root_task_id, "action": "CREATED", "card": card_id}


def find_candidate_roots(limit: int = 50) -> list[str]:
    """사용자면에서 후보 코멘트를 가진 뿌리 카드를 찾는다."""
    rc, text = _run(_user_cli("list", "--json"))
    if rc != 0:
        return []
    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(rows, dict):
        rows = rows.get("tasks") or rows.get("items") or []
    out = []
    for r in rows[:limit]:
        if not isinstance(r, dict):
            continue
        # 뿌리 카드만. 자식에는 `## User request` 원문이 없다.
        if "origin=user-query" in str(r.get("body") or ""):
            out.append(str(r.get("id")))
    return out


def run_once(*, dry_run: bool = False) -> list[dict]:
    acts = []
    for root in find_candidate_roots():
        a = process_root(root, dry_run=dry_run)
        if a.get("action") not in ("NONE", "SKIP"):
            print(f"  {a}", flush=True)
        acts.append(a)
    done = [a for a in acts if a.get("action") not in ("NONE", "SKIP")]
    print(f"  다리 주기 완료: 검사 {len(acts)}건 / 처리 {len(done)}건", flush=True)
    return acts


# ── 자체 점검 ───────────────────────────────────────────────────────────────
def _selfcheck() -> int:
    fails = 0

    def ok(name: str, cond: bool):
        nonlocal fails
        print(("  ✓ " if cond else "  ✗ ") + name)
        if not cond:
            fails += 1

    # 두 면의 조립기가 절대 안 섞인다
    f = _factory_cli("list")
    u = _user_cli("list")
    ok("공장 CLI 는 보드를 못박는다",
       "--board" in f and f[f.index("--board") + 1] == FACTORY_BOARD)
    ok("사용자 CLI 는 공장 보드를 쓰지 않는다", "--board" not in u)
    ok("두 CLI 는 다른 컨테이너를 쓴다", FACTORY_CONTAINER != USER_CONTAINER
       and FACTORY_CONTAINER in f and USER_CONTAINER in u)

    # 공장 카드 도장이 사용자 워크플로로 오인되지 않는다
    ok("공장 카드 도장은 user-query 가 아니다",
       "origin=user-idea" in IDEA_ORIGIN_HEADER
       and "origin=user-query" not in IDEA_ORIGIN_HEADER)

    # 원문 추출
    body = ("hgfinance.ceo-workflow-scope.v1\norigin=user-query\n"
            "workflow_mode=analysis\n\n## User request\n마감 전 호가가 얇아지면")
    ok("원문은 구분자 아래만 읽는다",
       user_request_text(body) == "마감 전 호가가 얇아지면")
    ok("구분자가 없으면 빈 문자열", user_request_text("origin=user-query") == "")

    # 후보 추출 - 오탐 금지
    ok("표식 없는 JSON 은 후보가 아니다",
       extract_candidate('{"title": "x"}') is None)
    ok("깨진 JSON 은 조용히 무시",
       extract_candidate(f"{CANDIDATE_MARKER}\n{{not json}}") is None)

    from orchestration.contracts.user_idea_lead import idea_text_sha256
    raw = "마감 30분 전 호가가 얇아지는 종목은 유동성 때문에 다음 날 밀린다"
    cand = UserIdeaCandidate(
        raw_text_sha256=idea_text_sha256(raw),
        title="테스트 아이디어", claimed_edge="다음 날 약세",
        mechanism="유동성 공급자가 마감 전에 물러난다" + "x" * 10)
    payload = f"{CANDIDATE_MARKER}\n{cand.model_dump_json()}"
    got = extract_candidate(payload)
    ok("표식 있는 후보는 읽는다", got is not None and got.title == "테스트 아이디어")

    # 거부 코멘트에 되물음이 실린다
    r = verify(raw, cand, root_task_id="t_x")
    ok("인용 없는 후보는 거부된다", isinstance(r, IdeaRejection))
    if isinstance(r, IdeaRejection):
        c = _reject_comment(r)
        ok("거부 코멘트에 사유 코드가 있다", "사유:" in c and "EVIDENCE" in c)
        ok("거부 코멘트에 되물음이 있다", "필요한 것:" in c)
        ok("거부 코멘트는 다리 표식으로 시작", c.startswith(_BRIDGE_MARK))

    print("자체점검 통과" if fails == 0 else f"자체점검 실패 {fails}건", flush=True)
    return fails


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument("--once", action="store_true")
    m.add_argument("--loop", action="store_true")
    m.add_argument("--self-check", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--interval-min", type=int, default=5)
    # 운영용: 한 카드만 처리한다(전수 훑기 없이 재시도·검증할 때 쓴다).
    p.add_argument("--root", default="", help="이 뿌리 카드 하나만 처리한다")
    a = p.parse_args(argv)

    if a.self_check:
        return _selfcheck()
    if a.once:
        if a.root:
            print(f"  {process_root(a.root, dry_run=a.dry_run)}", flush=True)
        else:
            run_once(dry_run=a.dry_run)
        return 0
    interval = max(1, a.interval_min) * 60
    print(f"{MODULE_VERSION} 반복 시작 - {a.interval_min}분마다", flush=True)
    while True:
        try:
            run_once(dry_run=a.dry_run)
        except Exception as e:                      # 다리가 죽어도 두 면은 산다
            print(f"  다리 주기 오류(계속): {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
