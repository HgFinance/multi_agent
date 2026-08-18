# -*- coding: utf-8 -*-
"""F2P: 막힘 신고 계수기가 **없는 되풀이를 만들어 내지 않는가**.

커널 사실(hermes_cli/kanban_db.py, 실측):
  * 첫 막힘은 언제나 `block_recurrences = 1` 이다
    (`recurrences = prev + 1 if same_cause else 1`, 최초 prev=0).
  * `BLOCK_RECURRENCE_LIMIT = 2` 이므로 **진짜 되풀이(>=2)는 `blocked` 가
    아니라 `triage` 로 간다.**

따라서 `status='blocked'` 만 보는 계수기가 보는 행의 `block_recurrences` 는
**구조적으로 항상 1** 이고, 그것들을 더한 `재발 N회` 는 카드 수(`cost`)와
같은 수를 더 무서운 이름으로 다시 적은 것에 불과하다. 그리고 정작 진짜로
되풀이돼 루프 차단기가 사람에게 올린 신고(triage)는 **한 건도 안 보인다.**

이 검사는 트리를 인자로 받아 같은 검사를 두 트리에 대고 돌린다:
    cd /sc; CENSUS_TREE=work         python3 probe_block_recurrence_truth.py
    cd /sc; CENSUS_TREE=work_patched python3 probe_block_recurrence_truth.py
"""
import os
import sqlite3
import sys

_DEFAULT_TREE = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
TREE = os.environ.get("CENSUS_TREE") or _DEFAULT_TREE
if not os.path.isabs(TREE):
    TREE = os.path.join(os.getcwd(), TREE)
FACTORY = os.path.join(TREE, "departments", "01-research", "factory")
sys.path.insert(0, os.path.join(TREE, "departments", "04-quant-backtest", "pipeline"))
sys.path.insert(0, os.path.join(TREE, "departments", "01-research", "collectors"))
sys.path.insert(0, FACTORY)

import factory_autopilot as fa          # noqa: E402
import bottleneck_census as bc          # noqa: E402

assert bc.__file__.startswith(FACTORY), bc.__file__

import json                             # noqa: E402


def _board(rows, events):
    """진짜 sqlite 보드를 만든다.

    ▶ 술어를 지키지 않는 스텁은 검사를 죽인다 (2026-08-14 교훈).
      `_board_rows` 를 SQL 을 무시하는 lambda 로 바꾸면 `where status=...` 가
      사라져 **지금 재려는 바로 그 메커니즘**이 없어진다. 그래서 여기서는
      실제 SQL 을 실제 테이블에 대고 실행한다.
    """
    c = sqlite3.connect(":memory:")
    c.execute("create table tasks (id text primary key, assignee text, "
              "title text, status text, block_kind text, block_recurrences int)")
    c.execute("create table task_events (id integer primary key autoincrement, "
              "task_id text, kind text, payload text)")
    c.executemany("insert into tasks values (?,?,?,?,?,?)", rows)
    c.executemany("insert into task_events (task_id, kind, payload) values (?,?,?)",
                  events)
    return lambda sql, params=(): [tuple(r) for r in
                                   c.execute(sql, list(params)).fetchall()]


def _blocked_event(tid, reason, kind="capability", rec=1):
    return (tid, "blocked", json.dumps({"reason": reason, "kind": kind,
                                        "recurrences": rec}, ensure_ascii=False))


FAILED = []


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  RED  ") + name + ("" if cond else "  <- " + str(detail)[:200]))
    if not cond:
        FAILED.append(name)


saved = fa._board_rows
try:
    # ── A. 되풀이가 없는데 '재발' 이라고 적지 않는다 ───────────────────────
    reasons = ["원장 리드 excerpt 가 500자를 넘어 검증에서 거절된다",
               "제출 API 가 독립 회의론자 산출을 인식하지 않는다",
               "capability discovery 검사 실행이 승인 거부로 막혔다"]
    rows = [(f"t_a{i}", "research-department", f"카드 {i}", "blocked",
             "capability", 1) for i in range(3)]
    events = [_blocked_event(f"t_a{i}", reasons[i]) for i in range(3)]
    fa._board_rows = _board(rows, events)
    got = bc._capability_blocks(None)
    check("A0 3건이 한 줄로 묶인다", len(got) == 1 and got[0].cost == 3.0, got)
    ev = got[0].evidence if got else ""
    check("A1 되풀이 0회인데 '재발' 이라고 적지 않는다", "재발" not in ev, ev)

    # 보호 장치(양쪽 다 초록이어야 한다): 신고 원문이 카드까지 간다
    check("G1 신고 원문이 카드에 실린다", "신고 원문" in ev or "같은 답을" in ev, ev)
    check("G2 답을 받았으면 다시 묻지 않는다",
          got and got[0].hint == getattr(bc, "_HINT_HEARD", object()), "")

    # ── B. 진짜 되풀이는 triage 에 있고, 그것이 보여야 한다 ────────────────
    rows_b = [("t_b0", "research-department", "되풀이된 신고", "triage",
               "capability", 2),
              ("t_b1", "research-department", "한 번 신고", "blocked",
               "capability", 1)]
    events_b = [_blocked_event("t_b0", "같은 실행면 결함으로 두 번째 막힌다",
                               rec=2),
                _blocked_event("t_b1", "다른 사유로 한 번 막혔다")]
    fa._board_rows = _board(rows_b, events_b)
    got_b = bc._capability_blocks(None)
    check("B1 루프 차단기가 사람에게 올린 신고(triage)가 보인다",
          got_b and got_b[0].cost == 2.0,
          [(b.key, b.cost) for b in got_b])
    ev_b = got_b[0].evidence if got_b else ""
    check("B2 진짜 되풀이는 1회로 적는다(2회 막힘 = 되풀이 1회)",
          "재발 1회" in ev_b and "카드 2건" in ev_b, ev_b)

    # ── C. 보드를 못 읽으면 지어내지 않는다 (기존 보호 장치) ───────────────
    def _boom(*a, **k):
        raise RuntimeError("보드 조회 실패")

    fa._board_rows = _boom
    check("G3 보드를 못 읽으면 안 적는다", bc._capability_blocks(None) == [])
finally:
    fa._board_rows = saved

print()
if FAILED:
    print("F2P RED  (%d/%d 실패): %s" % (len(FAILED), 6 + 1, ", ".join(FAILED)))
    raise SystemExit(1)
print("F2P GREEN (전부 통과)")
