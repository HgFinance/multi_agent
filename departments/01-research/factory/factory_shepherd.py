#!/usr/bin/env python3
"""공장 셰퍼드 - 막힌 카드를 **사람이 밖에서 고치던 방식 그대로** 안에서 고친다.

▶ 왜 이게 필요한가 (2026-08-23 실측)
  PROVIDER_QUOTA 장애가 사흘을 묵는 동안 blocked 카드 69장이 쌓였고, 그중
  개선 카드 2장이 지문 억제에 걸려 **새 개선 카드까지 막았다.** 원인이 이미
  해소된 뒤에도(프로바이더 복구·GRANT 반영) 카드는 스스로 일어나지 못했다 -
  사람이 원격에서 하나씩 unblock/archive 를 눌러야 공장이 다시 돌았다.

  bottleneck_census 는 **어디서 시간을 잃는지** 이미 센다. 이 모듈은 그 다음
  반쪽이다: **셀 수 있고 검사로 풀 수 있는 것은 손으로 고치지 않는다.**

▶ 역할 분담 (공장 개발원칙 그대로)
  결정론(여기)   : 차단 사유 분류 → 전제조건을 **검사로 실측** → 통과하면
                   unblock, 낡은 시각 도장 카드는 archive, 인프라 층 원인은
                   에스컬레이션 장부에 명령어까지 적어 올린다. 추정으로
                   행동하지 않는다 - 검사를 못 만들면 행동도 없다.
  에이전트(카드) : 서명이 처음 보는 원인만 진단 카드로 받는다. 런 로그를
                   열어 원인을 분류하고 원카드에 기계판독 줄
                   (`SHEPHERD_CAUSE: kind=<k> detail=<d>`)을 남긴다.
                   다음 주기에 결정론이 그 분류로 행동한다.

▶ 건드리지 않는 것
  - alpha-factory 보드 밖(사용자/CEO 면)은 어떤 경로로도 만지지 않는다.
    모든 CLI 호출이 `--board alpha-factory` 를 강제한다(자체점검이 지킨다).
  - running/ready/review/done 카드. 셰퍼드는 **멈춘 것만** 다룬다.
  - 카드 본문·판정·원장. 수리는 상태 전이(unblock/archive)와 코멘트뿐이다.

사용:
    python3 factory_shepherd.py --self-check
    python3 factory_shepherd.py --once --dry-run
    python3 factory_shepherd.py --once
    python3 factory_shepherd.py --loop --interval-min 10
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MODULE_VERSION = "factory-shepherd-v1"

FACTORY_BOARD = os.getenv("FACTORY_KANBAN_BOARD", "alpha-factory")
KANBAN_CLI_CONTAINER = os.getenv(
    "KANBAN_CLI_CONTAINER", "hedgefund-factory-kanban-dispatcher")
DB_CONTAINER = os.getenv("SHEPHERD_DB_CONTAINER", "hedgefund-timescaledb")
CONTROL_DB = os.getenv("SHEPHERD_CONTROL_DB", "control")
RUNTIME_ROLE = os.getenv("SHEPHERD_RUNTIME_ROLE", "hgfinance_runtime")
RESEARCH_ASSIGNEE = "research-department"

# 카드 하나에 셰퍼드가 다시 걸어 주는 횟수 상한. 그 너머는 재시도로 안
# 풀리는 종류다 - 에스컬레이션으로 넘긴다(CHURN_ATTEMPTS 와 같은 철학).
MAX_SHEPHERD_RETRIES = int(os.getenv("SHEPHERD_MAX_RETRIES", "2"))
# 한 주기의 행동 상한. 폭주 방지 - 첫 주기에 잔재 수십 장을 만나도
# 보드를 한꺼번에 뒤집지 않는다.
MAX_ACTIONS_PER_TICK = int(os.getenv("SHEPHERD_MAX_ACTIONS", "30"))
# 프로바이더 생존 판정 창(초): 이 안에 완료된 카드가 있으면 살아 있다.
PROVIDER_FRESH_SECONDS = int(os.getenv("SHEPHERD_PROVIDER_FRESH", "5400"))

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]
ESCALATION_PATH = Path(os.getenv(
    "SHEPHERD_ESCALATION_PATH", str(_ROOT / "var" / "shepherd" / "escalations.md")))

# 시각 도장 카드 계열(스카우트 소집·브리더·기획자). 도장을 지우면 계열이
# 남는다 - 같은 계열의 더 새 도장이 살아 있으면 낡은 blocked 는 잔재다.
_STAMP = re.compile(r"\d{8}T\d{2}[a-z]?")


def _now() -> float:
    return time.time()


# ── CLI 실행기 ───────────────────────────────────────────────────────────────
# 검사에서 통째로 바꿔 끼우기 위해 모듈 수준 함수 하나로 모은다.

def _run(argv: list[str], timeout: int = 60) -> tuple[int, str]:
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _kanban_cli(*args: str) -> list[str]:
    """실패 닫힘: 보드 인자를 여기서 강제한다. 다른 조립 경로는 없다."""
    return ["docker", "exec", "-u", "1000", "-i", KANBAN_CLI_CONTAINER,
            "hermes", "kanban", "--board", FACTORY_BOARD, *args]


def _psql(sql: str) -> tuple[int, str]:
    return _run(["docker", "exec", DB_CONTAINER, "psql", "-U", "postgres",
                 "-d", CONTROL_DB, "-tAc", sql])


# ── 차단 사유 분류 ───────────────────────────────────────────────────────────
# 오늘 실측한 종류부터 넣는다. 처음 보는 서명은 UNKNOWN 으로 남고 진단
# 카드가 에이전트에게 간다 - 종류를 미리 다 알 필요가 없다(census 와 동일).

_REL = r"[A-Za-z_][A-Za-z0-9_.]*"
_CAUSE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("DB_PERMISSION", re.compile(
        rf"permission denied for (?:table|view|relation|schema)?\s*(?P<detail>{_REL})",
        re.I)),
    ("DB_PERMISSION", re.compile(
        rf"(?P<detail>{_REL})\s*(?:뷰|테이블)?에? 대한 권한", re.I)),
    ("PROVIDER_QUOTA", re.compile(
        r"PROVIDER_QUOTA|provider resolution failed", re.I)),
    ("MCP_DOWN", re.compile(
        r"(research )?MCP.{0,40}(도구|tools?|노출|연결|keepalive|reconnect|unreachable)",
        re.I | re.S)),
)
# 에이전트 진단 카드가 원카드에 남기는 기계판독 줄. 이게 있으면 규칙보다
# 우선한다 - 진단은 한 번만 하고 그 결과를 계속 쓴다.
_AGENT_CAUSE = re.compile(
    r"SHEPHERD_CAUSE:\s*kind=(?P<kind>[A-Z_]+)(?:\s+detail=(?P<detail>\S+))?")
_SHEPHERD_MARK = "[shepherd]"


def classify(text: str) -> tuple[str, str]:
    """차단 사유 문자열 → (kind, detail). 못 알아보면 ("UNKNOWN", 서명해시)."""
    m = _AGENT_CAUSE.search(text or "")
    if m:
        return m.group("kind"), (m.group("detail") or "")
    for kind, pat in _CAUSE_RULES:
        m = pat.search(text or "")
        if m:
            detail = (m.groupdict() or {}).get("detail") or ""
            return kind, detail.strip(".,;: ")
    sig = hashlib.sha1((text or "").strip().encode()).hexdigest()[:10]
    return "UNKNOWN", sig


# ── 전제조건 검사 ────────────────────────────────────────────────────────────
# **검사가 통과할 때만 다시 건다.** 원인이 그대로인데 걸면 시도 예산만 탄다.

def _relation_candidates(detail: str) -> list[str]:
    if "." in detail:
        return [detail]
    return [f"quant.{detail}", f"research.{detail}", f"reference.{detail}"]


def precondition_passes(kind: str, detail: str, cards: list[dict]) -> bool:
    if kind == "DB_PERMISSION":
        for rel in _relation_candidates(detail):
            rc, out = _psql(
                "select has_table_privilege("
                f"'{RUNTIME_ROLE}','{rel}','SELECT')")
            if rc == 0 and out.strip().startswith("t"):
                return True
        return False
    if kind == "PROVIDER_QUOTA":
        # 최근에 어떤 카드든 완료됐다면 프로바이더는 살아 있다 - 실측이지
        # 추정이 아니다.
        cutoff = _now() - PROVIDER_FRESH_SECONDS
        for c in cards:
            done = _parse_ts(c.get("completed_at"))
            if done and done >= cutoff:
                return True
        return False
    if kind == "MCP_DOWN":
        rc, out = _run(["docker", "exec", KANBAN_CLI_CONTAINER, "sh", "-c",
                        "curl -s -m 5 -o /dev/null -w '%{http_code}' "
                        "http://research-mcp:8037/health"])
        # 401(인증 요구)도 서버 생존이다. 000/빈 값만 죽음이다.
        return rc == 0 and out.strip() not in ("", "000")
    return False


def _parse_ts(v) -> float | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ── 보드 읽기 ────────────────────────────────────────────────────────────────

def list_cards() -> list[dict]:
    out: list[dict] = []
    for status in ("blocked", "triage", "done", "running", "ready", "todo"):
        rc, text = _run(_kanban_cli("list", "--json", "--status", status))
        if rc != 0:
            continue
        try:
            rows = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(rows, dict):
            rows = rows.get("tasks") or rows.get("items") or []
        out.extend(r for r in rows if isinstance(r, dict))
    return out


def show_card(task_id: str) -> dict:
    rc, text = _run(_kanban_cli("show", task_id, "--json"))
    if rc != 0:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def cause_text(detail: dict) -> str:
    """카드에서 차단 사유를 모은다: 요약 + 최근 코멘트(셰퍼드 것 제외)."""
    parts = [str(detail.get("latest_summary") or "")]
    for c in (detail.get("comments") or [])[-6:]:
        body = str(c.get("body") or "")
        if not body.startswith(_SHEPHERD_MARK):
            parts.append(body)
    return "\n".join(parts)


def shepherd_retry_count(detail: dict) -> int:
    return sum(1 for c in (detail.get("comments") or [])
               if str(c.get("body") or "").startswith(f"{_SHEPHERD_MARK} retry"))


# ── 계열/도장 위생 ───────────────────────────────────────────────────────────

def family_of(title: str) -> str:
    return _STAMP.sub("<stamp>", title or "").strip()


def stamp_of(title: str) -> str:
    m = _STAMP.search(title or "")
    return m.group(0) if m else ""


def superseded_ids(cards: list[dict]) -> list[str]:
    """같은 계열에서 더 새 도장이 살아 있는(blocked/triage 아님) 낡은
    blocked/triage 카드. 잔재는 재시도가 아니라 정리 대상이다."""
    newest_alive: dict[str, str] = {}
    for c in cards:
        st = stamp_of(c.get("title") or "")
        if not st:
            continue
        if c.get("status") in ("done", "running", "ready", "todo", "review"):
            fam = family_of(c.get("title") or "")
            if st > newest_alive.get(fam, ""):
                newest_alive[fam] = st
    out = []
    for c in cards:
        if c.get("status") not in ("blocked", "triage"):
            continue
        st = stamp_of(c.get("title") or "")
        if not st:
            continue
        fam = family_of(c.get("title") or "")
        if fam in newest_alive and st < newest_alive[fam]:
            out.append(str(c.get("id")))
    return out


# ── 행동 ─────────────────────────────────────────────────────────────────────

def _comment(task_id: str, text: str, dry_run: bool) -> None:
    if dry_run:
        return
    _run(_kanban_cli("comment", task_id, f"{_SHEPHERD_MARK} {text}"))


def _unblock(task_id: str, dry_run: bool) -> bool:
    if dry_run:
        return True
    rc, _ = _run(_kanban_cli("unblock", task_id))
    return rc == 0


def _archive(task_id: str, dry_run: bool) -> bool:
    if dry_run:
        return True
    rc, _ = _run(_kanban_cli("archive", task_id))
    return rc == 0


def escalate(kind: str, detail: str, task_id: str, dry_run: bool) -> bool:
    """사람 손이 필요한 것은 명령어까지 적어 장부에 올린다. 같은 지문은 한
    번만 - 장부가 소음이 되면 아무도 안 읽는다."""
    fp = f"{kind}:{detail}"
    fix = ""
    if kind == "DB_PERMISSION":
        rels = _relation_candidates(detail)
        fix = ("```\ndocker exec {db} psql -U postgres -d {dbn} -c "
               "\"GRANT SELECT ON {rel} TO {role}\"\n```").format(
                   db=DB_CONTAINER, dbn=CONTROL_DB,
                   rel=rels[0], role=RUNTIME_ROLE)
    entry = (f"- [ ] `{fp}` (예: {task_id}, {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z)"
             + (f"\n  {fix}" if fix else ""))
    if dry_run:
        return True
    ESCALATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = ESCALATION_PATH.read_text(encoding="utf-8") if ESCALATION_PATH.exists() else ""
    if f"`{fp}`" in existing:
        return False
    with ESCALATION_PATH.open("a", encoding="utf-8") as f:
        if not existing:
            f.write("# 셰퍼드 에스컬레이션 - 사람 손이 필요한 것\n\n")
        f.write(entry + "\n")
    return True


def create_diagnosis_card(sig: str, task_id: str, title: str,
                          dry_run: bool) -> bool:
    """처음 보는 원인만 에이전트에게 간다. 지시는 하나다: 로그를 열어
    분류하고 기계판독 줄을 남겨라. 고치는 것은 결정론이 한다."""
    body = (
        "origin=factory\nworkflow_plane=alpha-factory\n"
        "user_query_routing=forbidden\n"
        f"factory_assignee={RESEARCH_ASSIGNEE}\n\n"
        f"셰퍼드 진단 요청: 카드 {task_id} ({title}) 가 분류 불가 사유로 "
        "막혀 있다.\n\n"
        f"1. `hermes kanban --board {FACTORY_BOARD} log {task_id}` 와 show 로 "
        "실패의 **원문**을 읽어라.\n"
        "2. 원인을 한 종류로 분류하라: DB_PERMISSION / PROVIDER_QUOTA / "
        "MCP_DOWN / CONTRACT_MISMATCH / DATA_MISSING / OTHER.\n"
        f"3. 원카드 {task_id} 에 코멘트 **한 줄**을 남겨라 - 형식 그대로:\n"
        "   `SHEPHERD_CAUSE: kind=<종류> detail=<관계명이나 핵심어 하나>`\n"
        "4. 고치려 들지 마라. 수리는 셰퍼드 결정론이 전제조건 검사 후에 한다. "
        "추측으로 분류하지 말고, 원문이 부족하면 kind=OTHER 로 남겨라.\n")
    if dry_run:
        return True
    rc, _ = _run(_kanban_cli(
        "create", f"셰퍼드 진단: {title[:40]} 차단 원인 분류",
        "--assignee", RESEARCH_ASSIGNEE,
        "--idempotency-key", f"shepherd-diag-{sig}",
        "--created-by", MODULE_VERSION,
        "--body", body))
    return rc == 0


# ── 한 주기 ──────────────────────────────────────────────────────────────────

def run_once(dry_run: bool = False) -> dict:
    stats = {"retried": 0, "archived": 0, "escalated": 0,
             "diagnosed": 0, "skipped": 0}
    actions = 0
    cards = list_cards()

    # 1) 잔재 정리 - 더 새 도장이 살아 있는 낡은 blocked/triage
    for tid in superseded_ids(cards):
        if actions >= MAX_ACTIONS_PER_TICK:
            break
        if _archive(tid, dry_run):
            _comment(tid, "superseded - 같은 계열의 더 새 도장 카드가 살아 있어 "
                          "잔재로 정리", dry_run)
            print(f"  셰퍼드: {tid} archive (superseded)", flush=True)
            stats["archived"] += 1
            actions += 1

    # 2) 멈춘 카드 - 분류 → 전제조건 검사 → 행동
    handled = {tid for tid in superseded_ids(cards)}
    for c in cards:
        if actions >= MAX_ACTIONS_PER_TICK:
            break
        if c.get("status") not in ("blocked", "triage"):
            continue
        tid = str(c.get("id"))
        if tid in handled:
            continue
        detail = show_card(tid)
        if not detail:
            stats["skipped"] += 1
            continue
        text = cause_text(detail)
        kind, kdetail = classify(text)
        title = str(c.get("title") or "")

        if kind == "UNKNOWN":
            if create_diagnosis_card(kdetail, tid, title, dry_run):
                print(f"  셰퍼드: {tid} 진단 카드 발행 (sig={kdetail})", flush=True)
                stats["diagnosed"] += 1
                actions += 1
            continue

        if precondition_passes(kind, kdetail, cards):
            retries = shepherd_retry_count(detail)
            if retries >= MAX_SHEPHERD_RETRIES:
                if escalate(kind, f"{kdetail}(재시도 소진)", tid, dry_run):
                    stats["escalated"] += 1
                    actions += 1
                continue
            if c.get("status") == "triage":
                # triage 는 unblock 이 안 된다. 시각 도장 계열이면 정리하고
                # 다음 도장이 새 지문으로 다시 걸리게 둔다.
                if stamp_of(title) and _archive(tid, dry_run):
                    _comment(tid, f"retry #{retries + 1} kind={kind} - 전제조건 "
                                  "통과, triage 는 아카이브 후 다음 도장 카드로 "
                                  "재개", dry_run)
                    print(f"  셰퍼드: {tid} archive→재발행 대기 ({kind})", flush=True)
                    stats["retried"] += 1
                    actions += 1
                elif escalate(kind, f"{kdetail}(triage,비도장)", tid, dry_run):
                    stats["escalated"] += 1
                    actions += 1
            else:
                if _unblock(tid, dry_run):
                    _comment(tid, f"retry #{retries + 1} kind={kind} "
                                  f"detail={kdetail} - 전제조건 검사 통과",
                             dry_run)
                    print(f"  셰퍼드: {tid} unblock ({kind})", flush=True)
                    stats["retried"] += 1
                    actions += 1
        else:
            if escalate(kind, kdetail, tid, dry_run):
                print(f"  셰퍼드: {kind}:{kdetail} 에스컬레이션 기록", flush=True)
                stats["escalated"] += 1
                actions += 1

    print(f"  셰퍼드 주기 완료: {stats}", flush=True)
    return stats


# ── 자체점검 ─────────────────────────────────────────────────────────────────
# F2P 규율: 각 검사는 행동 규칙 하나를 지킨다. 여기서 깨지면 배포가 없다.

def _self_check() -> int:
    fails = 0

    def ok(name: str, cond: bool):
        nonlocal fails
        print(("  ✓ " if cond else "  ✗ ") + name)
        if not cond:
            fails += 1

    # 분류: 오늘 실측한 세 종류 + 기계판독 줄 우선
    k, d = classify("permission denied for table dataset_manifests")
    ok("DB_PERMISSION 분류+관계 추출", k == "DB_PERMISSION" and d == "dataset_manifests")
    k, d = classify("current_krx_stock_instrument_identity 뷰에 대한 권한 부족")
    ok("한국어 권한 사유 분류", k == "DB_PERMISSION"
       and d == "current_krx_stock_instrument_identity")
    k, _ = classify("PROVIDER_QUOTA: provider resolution failed")
    ok("PROVIDER_QUOTA 분류", k == "PROVIDER_QUOTA")
    k, _ = classify("research MCP 도구도 노출되지 않습니다")
    ok("MCP_DOWN 분류", k == "MCP_DOWN")
    k, d = classify("아무도 모르는 새로운 사고 서명 SHEPHERD_CAUSE: kind=DB_PERMISSION detail=quant.foo")
    ok("에이전트 기계판독 줄이 규칙보다 우선", k == "DB_PERMISSION" and d == "quant.foo")
    k, d1 = classify("완전히 새로운 실패 문장 A")
    _, d2 = classify("완전히 새로운 실패 문장 A")
    ok("UNKNOWN 서명은 결정적", k == "UNKNOWN" and d1 == d2)

    # 보드 고정: 조립 경로가 하나뿐이고 항상 alpha-factory 를 못 벗어난다
    argv = _kanban_cli("unblock", "t_x")
    ok("모든 칸반 호출에 --board 강제",
       "--board" in argv and argv[argv.index("--board") + 1] == FACTORY_BOARD)

    # 잔재 판정: 더 새 도장이 살아 있을 때만, blocked/triage 만
    cards = [
        {"id": "t_old", "status": "blocked",
         "title": "공장 스카우트 소집: 리드 수집 20260820T03"},
        {"id": "t_new", "status": "done",
         "title": "공장 스카우트 소집: 리드 수집 20260823T08"},
        {"id": "t_other", "status": "blocked",
         "title": "다른 계열 카드 20260820T03"},
        {"id": "t_run", "status": "running",
         "title": "공장 스카우트 소집: 리드 수집 20260819T01"},
    ]
    sup = superseded_ids(cards)
    ok("낡은 도장만 잔재로", sup == ["t_old"])

    # 재시도 상한: 마커 코멘트로 센다
    det = {"comments": [{"body": "[shepherd] retry #1 kind=DB_PERMISSION"},
                        {"body": "[shepherd] retry #2 kind=DB_PERMISSION"},
                        {"body": "에이전트의 일반 코멘트"}]}
    ok("셰퍼드 재시도 횟수 집계", shepherd_retry_count(det) == 2)

    # 사유 수집이 셰퍼드 자신의 코멘트를 다시 읽지 않는다(메아리 방지)
    det = {"latest_summary": "permission denied for table x",
           "comments": [{"body": "[shepherd] retry #1 kind=DB_PERMISSION"}]}
    ok("셰퍼드 코멘트는 사유에서 제외",
       "[shepherd]" not in cause_text(det))

    # 에스컬레이션 지문 중복 방지 (파일 격리)
    import tempfile
    global ESCALATION_PATH
    keep = ESCALATION_PATH
    with tempfile.TemporaryDirectory() as td:
        ESCALATION_PATH = Path(td) / "esc.md"
        first = escalate("DB_PERMISSION", "quant.x", "t_a", dry_run=False)
        second = escalate("DB_PERMISSION", "quant.x", "t_b", dry_run=False)
        ok("에스컬레이션은 지문당 한 번", first and not second)
        ok("에스컬레이션에 실행 명령 포함",
           "GRANT SELECT ON quant.x" in ESCALATION_PATH.read_text(encoding="utf-8"))
    ESCALATION_PATH = keep

    print(("자체점검 통과" if fails == 0 else f"자체점검 실패 {fails}건"), flush=True)
    return fails


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument("--once", action="store_true")
    m.add_argument("--loop", action="store_true")
    m.add_argument("--self-check", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--interval-min", type=int, default=10)
    a = p.parse_args(argv)

    if a.self_check:
        return _self_check()
    if a.once:
        run_once(dry_run=a.dry_run)
        return 0
    interval = max(2, a.interval_min) * 60
    print(f"{MODULE_VERSION} 반복 시작 - {a.interval_min}분마다", flush=True)
    while True:
        try:
            run_once(dry_run=a.dry_run)
        except Exception as e:  # 셰퍼드가 죽으면 공장 자가수리가 죽는다
            print(f"  셰퍼드 주기 오류(계속): {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
