#!/usr/bin/env python3
"""막힌 카드를 풀어 준다 - **한 본부가 넘어져도 나머지가 답을 내게.**

담당: 재일 (리서치/퀀트) — 흐름 전체에 걸리는 장치라 CEO 조회면에 둔다
계약: apps/api/kanban_board.py 의 Card·CardOutcome 을 그대로 쓴다

▶ 왜 필요한가 (2026-08-11 실측)
  `task_links` 는 부모가 끝나야 자식이 `ready` 가 된다. 그런데 부모가 죽으면
  (`blocked`/`FAILED`) 자식은 `todo` 에 **무기한** 앉는다. 타임아웃도, 우회도,
  "그 본부 없이 진행" 경로도 없다.

  그날 리서치 카드 4장이 OOM 으로 죽자 QA·CEO·퀀트·리스크·트레이딩이 세 시간을
  그대로 멈춰 있었다. 사람이 손으로 `unblock` 할 때까지 아무 장치도 작동하지
  않았고, CEO 종합은 `ready:false` 로 영원히 돌아왔다. **부서 하나의 장애가
  서비스 전체의 무응답이 됐다.**

▶ 무엇을 하는가
  기한을 넘긴 죽은 부모를 **"산출 없음"을 명시한 채 닫는다.** 그러면 자식이
  풀리고 흐름이 이어진다.

▶ 없는 답을 지어내지 않는다 (fail-closed)
  닫을 때 결과 본문에 그 본부의 입력이 **없었다는 사실**을 적는다. 자식과 CEO
  종합은 그것을 읽고 "리서치 입력 없음"을 전제로 답해야 한다. 조용히 건너뛰면
  읽는 쪽은 모든 본부가 답한 줄로 안다 - 그게 이 저장소가 반복해 막아 온
  사고다(`done` != 답, 조회 실패 != 잔고 0).

  그래서 `complete` 하면서 본문을 남긴다. kanban_board._classify 는 본문이 비면
  `NO_ANSWER` 로 부르는데, 여기서는 본문이 있으므로 `ANSWERED` 가 된다 - 그
  본문의 내용이 "산출 없음"이다. 즉 **답이 없다는 것이 답으로 기록된다.**

▶ 기한을 부서가 아니라 카드 종류로 나눈다
  사용자 질의는 사람이 기다리고 있으므로 짧게, 공장 카드는 급하지 않으므로
  길게. 부서별로 나누면 느린 부서가 생길 때마다 표를 고쳐야 한다.

사용
  python apps/api/card_watchdog.py --check          # 자체 점검(보드 접근 없음)
  python apps/api/card_watchdog.py --scan           # 무엇을 풀지만 보고
  python apps/api/card_watchdog.py --release        # 실제로 푼다
  python apps/api/card_watchdog.py --loop --interval-min 3
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# ▶ **감시자는 언제나 컨테이너 밖에서 돈다.** 그래서 두 모드를 여기서 못박는다.
#   기본값(file/local)으로 두면 호스트가 kanban.db 를 직접 연다. Windows 바인드
#   마운트에서 그것은 `mode=ro` 여도 `-shm` 매핑을 만들고, 그 순간부터 컨테이너
#   안의 모든 보드 쓰기가 `disk I/O error` 로 죽는다(2026-08-11 실측).
#
#   **보드를 지키자고 만든 장치가 보드를 망가뜨리는 것이 최악이다.** import 전에
#   세워야 kanban_board 의 모듈 상수가 이 값을 읽는다. 이미 다르게 설정돼
#   있으면 사람이 의도한 것이므로 덮지 않고 그대로 둔다.
os.environ.setdefault("KANBAN_ACCESS_MODE", "docker")
os.environ.setdefault("HERMES_EXEC_MODE", "docker")

from apps.api.kanban_board import (  # noqa: E402
    KANBAN_ACCESS_MODE,
    Card,
    board_rows_all,
    cards_from_rows,
)

MODULE_VERSION = "card-watchdog-v1"

# 질의 카드: 사람이 화면 앞에서 기다린다. 공장 카드: 다음 주기가 있으니 급하지 않다.
QUERY_DEADLINE_SECONDS = int(os.getenv("WATCHDOG_QUERY_DEADLINE", "900"))      # 15분
FACTORY_DEADLINE_SECONDS = int(os.getenv("WATCHDOG_FACTORY_DEADLINE", "3600"))  # 1시간

# 공장 카드를 알아보는 표식. factory_autopilot 이 제목 앞에 붙인다.
FACTORY_TITLE_PREFIX = "공장 주기"

# 죽은 것으로 보는 결말. STALE 은 넣지 않는다 - dispatcher 가 살아나면 도는
# 카드라, 죽었다고 닫으면 멀쩡한 작업을 버린다.
DEAD_OUTCOMES = frozenset({"BLOCKED", "FAILED", "NO_ASSIGNEE"})


@dataclass(frozen=True)
class Release:
    """풀어 줄 카드 하나와 그 이유."""

    dead: Card                 # 닫을 죽은 부모
    waiting: tuple[str, ...]   # 그것 때문에 묶여 있던 자식들
    age_seconds: int
    deadline: int

    @property
    def note(self) -> str:
        """죽은 카드에 남길 본문. **읽는 쪽이 없다는 걸 알아야 한다.**"""
        why = self.dead.summary.strip() or f"결말 {self.dead.outcome}(사유 미기록)"
        return (
            f"[산출 없음 - {MODULE_VERSION} 가 기한 초과로 닫음]\n"
            f"본부: {self.dead.assignee}\n"
            f"원래 결말: {self.dead.outcome} — {why}\n"
            f"경과: {self.age_seconds}초 (기한 {self.deadline}초)\n"
            f"묶여 있던 후속 카드: {', '.join(self.waiting) or '(없음)'}\n"
            "\n"
            "이 본부는 이번 건에 **아무 결과도 내지 못했다.** 후속 카드와 CEO "
            "종합은 이 본부의 입력이 없는 상태로 판단해야 하며, 없는 값을 "
            "추정해 채우거나 '이상 없음'으로 읽어서는 안 된다."
        )


def deadline_for(card: Card) -> int:
    """이 카드에 허용할 대기 시간(초)."""
    return (FACTORY_DEADLINE_SECONDS
            if card.title.startswith(FACTORY_TITLE_PREFIX)
            else QUERY_DEADLINE_SECONDS)


def find_releases(cards: list[Card], *, now: int) -> list[Release]:
    """지금 풀어야 할 것들. **자식이 실제로 묶여 있는 경우만** 고른다.

    자식이 없는 죽은 카드는 건드리지 않는다 - 아무도 안 기다리는데 닫으면
    사람이 나중에 사유를 보려 할 때 원래 결말이 지워져 있다.
    """
    by_id = {c.task_id: c for c in cards}
    waiting_on: dict[str, list[str]] = {}
    for card in cards:
        if card.is_terminal:
            continue                       # 이미 끝난 자식은 기다리는 게 아니다
        for parent_id in card.depends_on:
            parent = by_id.get(parent_id)
            if parent is not None and parent.outcome in DEAD_OUTCOMES:
                waiting_on.setdefault(parent_id, []).append(card.task_id)

    out: list[Release] = []
    for parent_id, children in sorted(waiting_on.items()):
        dead = by_id[parent_id]
        # 죽은 시점을 모르면 생성 시각을, 그것도 없으면 **지금**을 쓴다.
        #   `or` 로 쓰면 안 된다 - epoch 0 이 falsy 라 "시각을 안다"와 "모른다"가
        #   같은 가지로 떨어진다. 모르면 age=0 이 되어 **개입하지 않는 쪽**으로
        #   기울어야 한다: 나이를 모르는 카드를 죽었다고 닫는 것이 더 나쁘다.
        if dead.finished_at is not None:
            since = dead.finished_at
        elif dead.created_at:
            since = dead.created_at
        else:
            since = now
        age = max(0, now - int(since))
        limit = deadline_for(dead)
        if age >= limit:
            out.append(Release(dead=dead, waiting=tuple(sorted(children)),
                               age_seconds=age, deadline=limit))
    return out


def _run(argv: list[str], *, timeout: int = 60) -> tuple[int, str]:
    proc = subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def apply_release(rel: Release, *, dry_run: bool = False) -> bool:
    """죽은 카드를 산출 없음으로 닫는다. 실패는 조용히 넘기지 않는다."""
    from apps.api.hermes_cli import argv_for  # noqa: PLC0415 - 선택적 의존

    if dry_run:
        print(f"  [dry-run] {rel.dead.task_id} ({rel.dead.assignee}) "
              f"{rel.dead.outcome} {rel.age_seconds}s -> 산출 없음으로 닫는다. "
              f"묶인 카드 {len(rel.waiting)}장")
        return True

    # blocked 는 먼저 풀어야 complete 가 먹는다(보드가 상태 전이를 검사한다).
    if rel.dead.status.strip().lower() == "blocked":
        code, out = _run(argv_for(None, [
            "kanban", "unblock", "--reason",
            f"기한 {rel.deadline}초 초과 - 후속 카드 {len(rel.waiting)}장이 "
            "묶여 있어 산출 없음으로 닫는다", rel.dead.task_id]))
        if code != 0:
            print(f"  !! unblock 실패({rel.dead.task_id}): {out[:200]}", flush=True)
            return False

    code, out = _run(argv_for(None, [
        "kanban", "complete", rel.dead.task_id, "--result", rel.note]))
    if code != 0:
        print(f"  !! complete 실패({rel.dead.task_id}): {out[:200]}", flush=True)
        return False
    print(f"  풀림: {rel.dead.task_id} ({rel.dead.assignee}) "
          f"{rel.dead.outcome} {rel.age_seconds}초 -> 산출 없음. "
          f"후속 {len(rel.waiting)}장 진행 가능", flush=True)
    return True


def scan(*, dry_run: bool = True) -> int:
    """보드 전체를 훑어 막힌 것을 푼다. 반환: 푼 개수."""
    if KANBAN_ACCESS_MODE != "docker":
        # 조용히 진행하면 훑는 동안 보드 쓰기가 죽는다. 안 도는 편이 낫다.
        raise RuntimeError(
            f"KANBAN_ACCESS_MODE={KANBAN_ACCESS_MODE!r} 로는 돌 수 없다. "
            "호스트가 kanban.db 를 직접 열면 -shm 매핑이 생겨 컨테이너 쓰기가 "
            "전부 실패한다. docker 모드로만 돈다.")
    rows = board_rows_all()
    cards = cards_from_rows(rows)
    now = int(time.time())
    releases = find_releases(cards, now=now)
    stuck = sum(1 for c in cards if not c.is_terminal)
    print(f"[{MODULE_VERSION}] 카드 {len(cards)}장 / 미종결 {stuck}장 / "
          f"풀 대상 {len(releases)}건", flush=True)
    if not releases:
        # 막힌 게 없는 것과 못 읽은 것은 다르다 - 위에서 카드 수를 이미 찍었다.
        return 0
    return sum(1 for rel in releases if apply_release(rel, dry_run=dry_run))


# ---------------------------------------------------------------------------
# 자체 점검 - 보드·도커 없이
# ---------------------------------------------------------------------------

def _card(task_id, *, outcome="ANSWERED", assignee="research-department",
          depends_on=(), title="본부 검토", created_at=0, finished_at=None,
          status="done", summary="", result="x") -> Card:
    return Card(task_id=task_id, title=title, assignee=assignee, status=status,
                outcome=outcome, summary=summary, result=result,
                depends_on=list(depends_on), created_at=created_at,
                finished_at=finished_at)


def _check_only_releases_when_a_child_actually_waits():
    """자식이 안 기다리는 죽은 카드는 **건드리지 않는다.**

    아무도 안 기다리는데 닫으면, 사람이 나중에 사유를 보려 할 때 원래 결말이
    '산출 없음'으로 덮여 있다. 진단 정보를 지우면서 얻는 게 없다.
    """
    now = 10_000
    lonely = _card("t_dead", outcome="FAILED", finished_at=0)
    assert find_releases([lonely], now=now) == []

    child = _card("t_child", outcome="QUEUED", status="todo", result="",
                  depends_on=["t_dead"])
    rel = find_releases([lonely, child], now=now)
    assert len(rel) == 1 and rel[0].waiting == ("t_child",), rel

    # 자식이 이미 끝났으면 기다리는 게 아니다
    done_child = _card("t_child", outcome="NO_ANSWER", result="",
                       depends_on=["t_dead"])
    assert find_releases([lonely, done_child], now=now) == []

    # 나이를 **모르는** 카드는 건드리지 않는다. epoch 0 을 falsy 로 흘려보내면
    # "모른다"가 "아주 오래됐다"로 뒤집힌다 - 개입은 안전한 쪽으로 기운다.
    ageless = _card("t_x", outcome="FAILED", created_at=0, finished_at=None)
    kid = _card("t_xc", outcome="QUEUED", status="todo", result="",
                depends_on=["t_x"])
    assert find_releases([ageless, kid], now=99_999_999) == [], \
        "죽은 시점을 모르는 카드를 닫았다"
    print("  기다리는 자식이 있을 때만  OK")


def _check_deadline_is_not_reached_yet():
    """기한 전에는 풀지 않는다 - 느린 것을 죽은 것으로 부르면 멀쩡한 일을 버린다."""
    dead = _card("t_dead", outcome="BLOCKED", status="blocked", finished_at=1_000)
    child = _card("t_child", outcome="QUEUED", status="todo", result="",
                  depends_on=["t_dead"])
    assert find_releases([dead, child], now=1_000 + QUERY_DEADLINE_SECONDS - 1) == []
    assert len(find_releases([dead, child], now=1_000 + QUERY_DEADLINE_SECONDS)) == 1
    print("  기한 전 미개입            OK")


def _check_factory_gets_a_longer_rope():
    """공장 카드는 다음 주기가 있으니 질의보다 오래 기다린다."""
    assert FACTORY_DEADLINE_SECONDS > QUERY_DEADLINE_SECONDS
    factory = _card("t_f", title=f"{FACTORY_TITLE_PREFIX} [기획자]: 다음 실험 기획안",
                    outcome="FAILED", finished_at=0)
    query = _card("t_q", title="삼성전자 300주 리스크 검토", outcome="FAILED",
                  finished_at=0)
    assert deadline_for(factory) == FACTORY_DEADLINE_SECONDS
    assert deadline_for(query) == QUERY_DEADLINE_SECONDS

    at = QUERY_DEADLINE_SECONDS + 1
    kids = [_card("t_qc", outcome="QUEUED", status="todo", result="",
                  depends_on=["t_q"]),
            _card("t_fc", outcome="QUEUED", status="todo", result="",
                  depends_on=["t_f"])]
    got = {r.dead.task_id for r in find_releases([factory, query, *kids], now=at)}
    assert got == {"t_q"}, f"공장 카드를 질의 기한으로 잘랐다: {got}"
    print("  카드 종류별 기한          OK")


def _check_note_says_there_was_no_output():
    """**없는 답을 지어내지 않는다.** 본문이 '입력 없음'을 분명히 말해야 한다.

    조용히 건너뛰면 읽는 쪽은 모든 본부가 답한 줄로 안다 - `done` != 답,
    조회 실패 != 잔고 0 과 같은 종류의 사고다.
    """
    dead = _card("t_dead", outcome="FAILED", assignee="research-department",
                 summary="pid 6134 killed by signal 9", finished_at=0)
    child = _card("t_child", outcome="QUEUED", status="todo", result="",
                  depends_on=["t_dead"])
    note = find_releases([dead, child], now=99_999)[0].note
    for must in ("산출 없음", "research-department", "signal 9",
                 "아무 결과도 내지 못했다", "추정해 채우거나"):
        assert must in note, f"본문에 {must!r} 가 없다:\n{note}"
    # 빈 요약이어도 결말은 남는다 - 사유 미기록을 사유 없음으로 쓰지 않는다
    silent = _card("t_dead", outcome="BLOCKED", status="blocked", finished_at=0)
    assert "BLOCKED" in find_releases([silent, child], now=99_999)[0].note
    print("  산출 없음을 사실로 기록   OK")


def _check_stale_is_not_treated_as_dead():
    """STALE 은 dispatcher 가 살아나면 도는 카드다 - 닫으면 멀쩡한 작업을 버린다."""
    assert "STALE" not in DEAD_OUTCOMES
    stale = _card("t_s", outcome="STALE", status="ready", result="", finished_at=0)
    child = _card("t_c", outcome="QUEUED", status="todo", result="",
                  depends_on=["t_s"])
    assert find_releases([stale, child], now=99_999) == []
    # RUNNING 도 아니다 - 지금 일하고 있다
    running = _card("t_r", outcome="RUNNING", status="running", result="",
                    finished_at=0)
    child2 = _card("t_c2", outcome="QUEUED", status="todo", result="",
                   depends_on=["t_r"])
    assert find_releases([running, child2], now=99_999) == []
    print("  STALE/RUNNING 은 살려둔다 OK")


def _check_chain_releases_one_layer_at_a_time():
    """손자까지 한 번에 풀지 않는다 - 부모가 풀려야 자식이 돌고, 그다음이다.

    한 번에 다 닫으면 중간 본부가 **실제로 답할 기회**를 잃는다.
    """
    dead = _card("t_a", outcome="FAILED", finished_at=0)
    mid = _card("t_b", outcome="QUEUED", status="todo", result="",
                depends_on=["t_a"])
    tail = _card("t_c", outcome="QUEUED", status="todo", result="",
                 depends_on=["t_b"])
    rel = find_releases([dead, mid, tail], now=99_999)
    assert [r.dead.task_id for r in rel] == ["t_a"], rel
    print("  연쇄는 한 층씩             OK")


def _selfcheck() -> int:
    print(f"{MODULE_VERSION} 자체 점검 (보드·도커 없음)")
    _check_only_releases_when_a_child_actually_waits()
    _check_deadline_is_not_reached_yet()
    _check_factory_gets_a_longer_rope()
    _check_note_says_there_was_no_output()
    _check_stale_is_not_treated_as_dead()
    _check_chain_releases_one_layer_at_a_time()
    print("감시자 6개 영역 통과. 실행은 --scan / --release / --loop")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="막힌 카드 해제기")
    ap.add_argument("--check", action="store_true", help="자체 점검만")
    ap.add_argument("--scan", action="store_true", help="무엇을 풀지만 보고")
    ap.add_argument("--release", action="store_true", help="실제로 푼다")
    ap.add_argument("--loop", action="store_true", help="주기마다 계속 푼다")
    ap.add_argument("--interval-min", type=float, default=3.0)
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if a.check or not (a.scan or a.release or a.loop):
        return _selfcheck()
    if a.loop:
        print(f"{MODULE_VERSION} 반복 - {a.interval_min}분마다", flush=True)
        while True:
            try:
                scan(dry_run=False)
            except KeyboardInterrupt:
                return 0
            except Exception as exc:  # noqa: BLE001 - 한 번 실패가 감시를 멈추지 않는다
                print(f"  !! 훑기 실패: {type(exc).__name__}: {str(exc)[:200]}",
                      flush=True)
            time.sleep(max(30.0, a.interval_min * 60))
    scan(dry_run=not a.release)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
