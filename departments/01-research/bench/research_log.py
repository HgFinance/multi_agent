#!/usr/bin/env python3
"""연구 로그 - **계보를 지문이 아니라 지식으로 쌓는다.**

담당: 재일 (리서치본부 RES)

▶ 왜 이게 따로 있나 (2026-08-25 진단)
  공장은 계보를 `parent_ast_fingerprint` 로 기록했다. "B 가 A 에서 나왔다" 는
  남지만 **"A 에서 무엇을 알았기에 B 를 이렇게 만들었는지"** 는 안 남는다.
  그래서 실패가 `UNDERPOWERED_DATA` 한 단어로 눌리고, 다음 실험이 그 단어에서
  아무것도 못 배운다. 반면 `ml_pipeline/audit_*.py` 85개는 **코드 자체가**
  계보였다 - 프로젝트가 알아낸 것은 전부 거기서 나왔다.

  이 로그는 그 audit 방식을 무인 루프에 옮긴 것이다. 한 줄이 실험 하나이고,
  다음 실험은 **이전 줄의 원문**을 재료로 설계된다(교훈 코드가 아니라).

▶ 무엇을 강제하나 - 딱 셋
  1. 숫자에는 출처가 있어야 한다 (`script` 없이 `numbers` 만 있으면 거부).
     "에이전트가 기억으로 채운 숫자" 를 막는 유일한 장치다.
  2. 발견은 다음 질문을 낳아야 한다 (`next_questions` 비면 경고).
     루프가 스스로 돌려면 다음 재료가 항상 있어야 한다.
  3. 홀드아웃은 탐색이 건드리지 않는다 (`sessions_used` 를 대조).

▶ 무엇을 강제하지 **않나**
  사전등록, 시도 예산, AST 문법, 통제 어휘. 탐색은 원래 p-해킹적이어야 하고
  그래도 된다 - **승격만 안 하면** 된다. 승격은 확증 경로(공장)가 홀드아웃에서
  판정한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

LOG_VERSION = "research-log-v1"

# 컨테이너와 호스트 양쪽에서 같은 파일을 본다(quant-data 는 두 곳에 마운트).
ROOT = Path(os.getenv("RESEARCH_ROOT", "/app/quant-data/research"))
LOG_PATH = ROOT / "log.jsonl"
SCRIPTS_DIR = ROOT / "scripts"
OUT_DIR = ROOT / "out"
IDEA_QUEUE = ROOT / "ideas.jsonl"          # 사람이 던지는 아이디어 입구

# ── 홀드아웃 ────────────────────────────────────────────────────────────────
# **탐색은 이 세션들을 절대 안 본다.** 두 속도 구조를 안전하게 만드는 유일한
# 장치다 - 탐색이 아무리 p-해킹을 해도 홀드아웃이 깨끗하면 승격 판정은 정직하다.
# 최근 12세션을 예비로 뺀다(2026-08-06 이후).
HOLDOUT_FROM = os.getenv("RESEARCH_HOLDOUT_FROM", "2026-08-06")


@dataclass
class Entry:
    """실험 하나. **한 줄이 한 실험이고, 다음 실험의 재료다.**"""

    id: str
    ts: str
    question: str
    status: str                      # OPEN | DONE | FAILED
    origin: str = "auto"             # auto | user
    parent: str = ""                 # 계보 - 어느 실험에서 나온 질문인가
    card: str = ""                   # 공장 카드 id
    script: str = ""                 # 숫자를 만든 스크립트(출처)
    numbers: dict = field(default_factory=dict)
    finding: str = ""                # **산문**. 코드로 누르지 않는다.
    next_questions: list = field(default_factory=list)
    # ▶ 문헌 질문을 따로 둔다 (2026-08-25)
    #   벽에 부딪혔을 때 "다르게 재보자" 만 나오면 같은 우물만 판다.
    #   "남들은 이 벽을 어떻게 넘었나" 는 다른 종류의 질문이고, 답을
    #   찾는 도구도 다르다(agent-reach·arXiv·yt-dlp). 섞으면 측정
    #   카드가 웹을 뒤지다 시간을 버린다.
    lit_questions: list = field(default_factory=list)
    kind: str = "measure"           # measure | literature | confirm
    # ▶ 다리 ① - 탐색이 지목한 승격 후보 (2026-08-25)
    #   {"name","script","params","claim","expected"} 형태. 이게 있으면
    #   다음 주기에 **확증 카드**가 열리고 홀드아웃에서 다시 잰다.
    candidate: dict = field(default_factory=dict)
    prereg_sha256: str = ""         # 확증 카드가 동결한 사양의 지문
    confirm_result: dict = field(default_factory=dict)
    citations: list = field(default_factory=list)
    sessions_used: list = field(default_factory=list)
    error: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_dirs() -> None:
    for d in (ROOT, SCRIPTS_DIR, OUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def read_log() -> list[Entry]:
    if not LOG_PATH.exists():
        return []
    out: list[Entry] = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(Entry(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            continue                       # 깨진 줄은 건너뛴다(로그는 못 잃는다)
    return out


def next_id(entries: list[Entry] | None = None) -> str:
    entries = read_log() if entries is None else entries
    n = 0
    for e in entries:
        m = re.fullmatch(r"r(\d+)", str(e.id))
        if m:
            n = max(n, int(m.group(1)))
    return f"r{n + 1:04d}"


def append(entry: Entry) -> Entry:
    _ensure_dirs()
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
    return entry


def open_entry(question: str, *, origin: str = "auto", parent: str = "",
               card: str = "", kind: str = "measure",
               candidate: dict | None = None,
               prereg_sha256: str = "") -> Entry:
    e = Entry(id=next_id(), ts=_now(), question=question.strip(),
              status="OPEN", origin=origin, parent=parent, card=card,
              kind=kind, candidate=dict(candidate or {}),
              prereg_sha256=prereg_sha256)
    return append(e)


def close_entry(entry_id: str, *, script: str, numbers: dict, finding: str,
                next_questions: list, sessions_used: list | None = None,
                status: str = "DONE", error: str = "",
                lit_questions: list | None = None,
                citations: list | None = None,
                candidate: dict | None = None,
                confirm_result: dict | None = None) -> dict:
    """실험을 닫는다. **검사를 통과해야 닫힌다** - 출처 없는 숫자는 거부."""
    # 확증 카드는 홀드아웃을 **써야** 하므로 검사 방향이 반대다.
    src_kind_probe = next((e for e in read_log() if e.id == entry_id), None)
    problems = []
    if status == "DONE":
        if numbers and not str(script).strip():
            problems.append("NUMBERS_WITHOUT_SOURCE")     # 기억으로 채운 숫자 금지
        if not str(finding).strip():
            problems.append("NO_FINDING")
        if not next_questions and not (lit_questions or []):
            problems.append("NO_NEXT_QUESTION")           # 루프가 멎는다
        leaked = [s for s in (sessions_used or []) if str(s) >= HOLDOUT_FROM]
        if leaked and str(getattr(src_kind_probe, "kind", "measure")) != "confirm":
            problems.append(f"HOLDOUT_TOUCHED:{','.join(map(str, leaked[:3]))}")
        # ▶ 후보를 지목하려면 **재현 가능해야 한다.** 스크립트도 주장도 없이
        #   "이거 좋아 보인다" 는 후보가 아니다 - 확증 카드가 무엇을 동결해야
        #   할지 모른다.
        if candidate:
            missing = [k for k in ("name", "script", "claim")
                       if not str(candidate.get(k) or "").strip()]
            if missing:
                problems.append("CANDIDATE_INCOMPLETE:" + ",".join(missing))
    if problems:
        return {"ok": False, "problems": problems}

    entries = read_log()
    src = next((e for e in entries if e.id == entry_id), None)
    if src is None:
        return {"ok": False, "problems": ["UNKNOWN_ENTRY"]}

    closed = Entry(id=src.id, ts=_now(), question=src.question, status=status,
                   origin=src.origin, parent=src.parent, card=src.card,
                   script=str(script), numbers=dict(numbers or {}),
                   finding=str(finding), next_questions=list(next_questions),
                   lit_questions=list(lit_questions or []),
                   kind=getattr(src, 'kind', 'measure'),
                   citations=list(citations or []),
                   candidate=dict(candidate or getattr(src, 'candidate', {}) or {}),
                   prereg_sha256=getattr(src, 'prereg_sha256', ''),
                   confirm_result=dict(confirm_result or {}),
                   sessions_used=list(sessions_used or []), error=str(error))
    append(closed)
    return {"ok": True, "entry": asdict(closed)}


def latest_by_id() -> dict[str, Entry]:
    """같은 id 가 여러 줄이면 **마지막 줄이 현재 상태**다(추가 전용 로그)."""
    out: dict[str, Entry] = {}
    for e in read_log():
        out[e.id] = e
    return out


def open_questions() -> list[Entry]:
    return [e for e in latest_by_id().values() if e.status == "OPEN"]


def recent_findings(limit: int = 5) -> list[Entry]:
    """다음 카드에 실어 보낼 **원문 발견들**. 최신이 앞."""
    done = [e for e in latest_by_id().values() if e.status == "DONE"]
    done.sort(key=lambda e: e.ts, reverse=True)
    return done[:limit]


def prereg_fingerprint(candidate: dict) -> str:
    """후보 사양의 지문. **홀드아웃을 보기 전에** 박는다.

    정렬된 canonical JSON 을 해싱한다 - 키 순서가 달라도 같은 사양이면 같은
    지문이어야 하고, 값이 한 글자라도 바뀌면 달라야 한다.
    """
    payload = json.dumps(candidate or {}, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def pending_candidates() -> list[Entry]:
    """확증을 기다리는 후보 - 지목됐고 아직 확증 카드가 안 열린 것."""
    entries = latest_by_id()
    confirmed_parents = {e.parent for e in entries.values()
                         if e.kind == "confirm"}
    return [e for e in entries.values()
            if e.candidate and e.status == "DONE"
            and e.kind != "confirm" and e.id not in confirmed_parents]


def confirmed_passing() -> list[Entry]:
    """홀드아웃을 통과한 확증 - 승격 다리(③)의 재료."""
    return [e for e in latest_by_id().values()
            if e.kind == "confirm" and e.status == "DONE"
            and bool((e.confirm_result or {}).get("pass"))]


def pending_ideas() -> list[dict]:
    """사람이 던진 아이디어 큐. 한 줄 = {question, ts, consumed}"""
    if not IDEA_QUEUE.exists():
        return []
    out = []
    for line in IDEA_QUEUE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not d.get("consumed"):
            out.append(d)
    return out


def consume_idea(question: str) -> None:
    """소비 표시. 큐를 다시 써도 원본 순서는 유지한다."""
    if not IDEA_QUEUE.exists():
        return
    lines = []
    for line in IDEA_QUEUE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            lines.append(line)
            continue
        if d.get("question") == question and not d.get("consumed"):
            d["consumed"] = True
            d["consumed_at"] = _now()
        lines.append(json.dumps(d, ensure_ascii=False))
    IDEA_QUEUE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_idea(question: str) -> None:
    """사람이 아이디어를 던지는 입구."""
    _ensure_dirs()
    with IDEA_QUEUE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"question": question.strip(), "ts": _now(),
                            "consumed": False}, ensure_ascii=False) + "\n")


# ── 자체 점검 ───────────────────────────────────────────────────────────────
def _selfcheck() -> int:
    import tempfile
    global ROOT, LOG_PATH, SCRIPTS_DIR, OUT_DIR, IDEA_QUEUE
    fails = 0

    def ok(name, cond):
        nonlocal fails
        print(("  ✓ " if cond else "  ✗ ") + name)
        if not cond:
            fails += 1

    with tempfile.TemporaryDirectory() as td:
        ROOT = Path(td)
        LOG_PATH = ROOT / "log.jsonl"
        SCRIPTS_DIR = ROOT / "scripts"
        OUT_DIR = ROOT / "out"
        IDEA_QUEUE = ROOT / "ideas.jsonl"

        e1 = open_entry("스프레드가 진입을 막는가?", origin="user")
        ok("첫 항목 id 는 r0001", e1.id == "r0001")
        ok("OPEN 으로 시작", open_questions()[0].id == "r0001")

        bad = close_entry("r0001", script="", numbers={"x": 1},
                          finding="뭔가 알았다", next_questions=["다음"])
        ok("출처 없는 숫자는 거부",
           not bad["ok"] and "NUMBERS_WITHOUT_SOURCE" in bad["problems"])

        bad2 = close_entry("r0001", script="s.py", numbers={"x": 1},
                           finding="알았다", next_questions=[])
        ok("다음 질문이 없으면 거부",
           not bad2["ok"] and "NO_NEXT_QUESTION" in bad2["problems"])

        bad3 = close_entry("r0001", script="s.py", numbers={"x": 1},
                           finding="알았다", next_questions=["또"],
                           sessions_used=["2026-07-01", "2026-08-20"])
        ok("홀드아웃을 만지면 거부",
           not bad3["ok"] and any(p.startswith("HOLDOUT_TOUCHED")
                                  for p in bad3["problems"]))

        good = close_entry("r0001", script="scripts/r0001.py",
                           numbers={"spread_bp": 13.2},
                           finding="스프레드 13.2bp 가 23bp 허들의 57%를 먹는다",
                           next_questions=["지평선을 늘리면 허들 비중이 주는가?"],
                           sessions_used=["2026-07-01", "2026-07-02"])
        ok("정상 종료는 통과", good["ok"])
        ok("종료 후 OPEN 이 없다", len(open_questions()) == 0)
        ok("발견 원문이 남는다",
           "13.2bp" in recent_findings()[0].finding)
        ok("계보용 다음 질문이 남는다",
           recent_findings()[0].next_questions[0].startswith("지평선"))

        e2 = open_entry("지평선을 늘리면?", parent="r0001")
        ok("계보가 이어진다", e2.parent == "r0001" and e2.id == "r0002")

        add_idea("장 마감 전 호가 소멸을 봐라")
        ok("아이디어 큐에 들어간다", len(pending_ideas()) == 1)
        consume_idea("장 마감 전 호가 소멸을 봐라")
        ok("소비하면 큐에서 빠진다", len(pending_ideas()) == 0)

    print("자체점검 통과" if fails == 0 else f"자체점검 실패 {fails}건")
    return fails


def _cli(argv: list[str]) -> int:
    """에이전트가 발견을 기록하는 입구. **검사를 통과해야 기록된다.**"""
    import argparse

    p = argparse.ArgumentParser(prog="research_log")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("close", help="실험 하나를 닫는다")
    c.add_argument("--id", required=True)
    c.add_argument("--script", default="")
    c.add_argument("--numbers", default="{}")
    c.add_argument("--finding", default="")
    c.add_argument("--next", action="append", default=[],
                   help="다음에 **재볼** 것(측정 질문)")
    c.add_argument("--next-lit", action="append", default=[],
                   dest="next_lit",
                   help="다음에 **읽어볼** 것(문헌 질문) - 벽에 부딪혔을 때")
    c.add_argument("--cite", action="append", default=[],
                   help="근거로 쓴 출처 URL(문헌 카드면 필수)")
    c.add_argument("--candidate", default="",
                   help='승격 후보 지목(JSON): {"name","script","params",'
                        '"claim","expected"} - 홀드아웃에서 다시 잰다')
    c.add_argument("--confirm-result", default="", dest="confirm_result",
                   help='확증 카드 결과(JSON): {"pass":bool, ...}')
    c.add_argument("--sessions", action="append", default=[])
    c.add_argument("--status", default="DONE", choices=["DONE", "FAILED"])
    c.add_argument("--error", default="")

    sub.add_parser("show", help="로그를 사람이 읽게 출력")
    i = sub.add_parser("idea", help="아이디어를 큐에 넣는다")
    i.add_argument("question")

    a = p.parse_args(argv)

    if a.cmd == "idea":
        add_idea(a.question)
        print("아이디어 접수")
        return 0
    if a.cmd == "show":
        for e in sorted(latest_by_id().values(), key=lambda x: x.id):
            print(f"{e.id} [{e.status}] {e.question[:70]}")
            if e.finding:
                print(f"    발견: {e.finding[:100]}")
        return 0

    try:
        numbers = json.loads(a.numbers or "{}")
        if not isinstance(numbers, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        print("--numbers 는 JSON 객체여야 한다. 예: '{\"spread_bp\": 13.2}'")
        return 2

    def _json_arg(raw, label):
        if not str(raw or '').strip():
            return {}
        try:
            v = json.loads(raw)
            if not isinstance(v, dict):
                raise ValueError
            return v
        except (json.JSONDecodeError, ValueError):
            print(f'--{label} 는 JSON 객체여야 한다')
            raise SystemExit(2)

    r = close_entry(a.id, script=a.script, numbers=numbers,
                    finding=a.finding, next_questions=a.next,
                    lit_questions=a.next_lit, citations=a.cite,
                    candidate=_json_arg(a.candidate, 'candidate'),
                    confirm_result=_json_arg(a.confirm_result,
                                             'confirm-result'),
                    sessions_used=a.sessions, status=a.status, error=a.error)
    if not r.get("ok"):
        problems = ", ".join(r.get("problems") or [])
        print(f"기록 거부: {problems}")
        print("  NUMBERS_WITHOUT_SOURCE: 숫자를 냈으면 --script 로 출처를 대라")
        print("  NO_NEXT_QUESTION      : --next 로 다음에 볼 것을 남겨라")
        print("  HOLDOUT_TOUCHED       : 홀드아웃 세션은 탐색에 쓸 수 없다")
        return 1
    print(f"기록됨: {a.id}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(_cli(sys.argv[1:]))
    raise SystemExit(_selfcheck())
