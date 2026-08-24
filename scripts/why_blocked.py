#!/usr/bin/env python3
"""멈춘 카드가 왜 멈췄는지 분류해 보여준다.

카드에 남는 오류 문구는 대개 포괄 문구라(`worker exited cleanly ... protocol
violation`) 그것만 보면 원인이 안 보인다. 그래서 **런 로그까지 함께 읽어**
분류한다 - 2026-08-14 에 190 장을 한 장씩 열어보다 만든 도구다.

사용:
  python scripts/why_blocked.py                # blocked 전체 분류 요약
  python scripts/why_blocked.py t_abc123       # 한 장 상세
  python scripts/why_blocked.py --recoverable  # 되살릴 가치가 있는 카드 ID 만 출력
"""

from __future__ import annotations

import argparse
import collections
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestration.failure_taxonomy import (  # noqa: E402
    FailureKind,
    classify_failure,
)

DISPATCHER = "hedgefund-kanban-dispatcher"
KANBAN_CLI = "hedgefund-qa-hermes"


def _run(cmd: list[str], timeout: int = 120) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout or ""


def _blocked_rows() -> list[dict]:
    """보드에서 blocked 카드와 그 오류 문구를 읽는다(DB 직접, CLI 왕복 절약)."""
    script = chr(10).join((
        "import sys, json",
        "sys.path.insert(0, '/opt/hermes')",
        "from hermes_cli import kanban_db as kb",
        "with kb.connect_closing() as conn:",
        "    rows = conn.execute(\"SELECT id, assignee, title, last_failure_error,"
        " block_kind FROM tasks WHERE status='blocked'\").fetchall()",
        "    print(json.dumps([dict(r) for r in rows], ensure_ascii=False))",
    ))
    out = _run(["docker", "exec", DISPATCHER, "python3", "-c", script], timeout=180)
    try:
        import json
        return json.loads(out.strip().splitlines()[-1]) if out.strip() else []
    except Exception:
        return []


def _run_log(task_id: str, tail: int = 40) -> str:
    out = _run(["docker", "exec", "-u", "hermes", KANBAN_CLI,
                "hermes", "kanban", "log", task_id], timeout=180)
    return "\n".join(out.splitlines()[-tail:])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", nargs="?", help="한 장만 상세히 본다")
    parser.add_argument("--recoverable", action="store_true",
                        help="되살릴 가치가 있는 카드 ID 만 줄바꿈으로 출력")
    parser.add_argument("--deep", action="store_true",
                        help="카드마다 런 로그까지 읽는다(느리지만 정확하다)")
    args = parser.parse_args()

    if args.task_id:
        log = _run_log(args.task_id)
        verdict = classify_failure(log)
        print(f"{args.task_id}: {verdict.kind.value} "
              f"(되살릴 가치 {'있음' if verdict.recoverable else '없음'})")
        if verdict.evidence:
            print(f"  근거: {verdict.evidence}")
        print(f"  처방: {verdict.prescription}")
        return 0

    rows = _blocked_rows()
    if not rows:
        print("blocked 카드가 없거나 보드를 읽지 못했다")
        return 1

    buckets: dict[FailureKind, list[str]] = collections.defaultdict(list)
    for row in rows:
        texts = [row.get("last_failure_error") or "", row.get("block_kind") or ""]
        if args.deep:
            texts.append(_run_log(str(row.get("id")), tail=25))
        verdict = classify_failure(*texts)
        buckets[verdict.kind].append(str(row.get("id")))

    if args.recoverable:
        for kind, ids in buckets.items():
            if classify_failure(kind.value).recoverable or kind in {
                FailureKind.CREDENTIALS, FailureKind.CAPACITY,
                FailureKind.TIMEOUT, FailureKind.SKILL_MISSING, FailureKind.PROTOCOL,
            }:
                for task_id in ids:
                    print(task_id)
        return 0

    print(f"blocked {len(rows)}장")
    for kind, ids in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        mark = "회복대상" if kind in {
            FailureKind.CREDENTIALS, FailureKind.CAPACITY, FailureKind.TIMEOUT,
            FailureKind.SKILL_MISSING, FailureKind.PROTOCOL,
        } else ("사람대기" if kind is FailureKind.NEEDS_HUMAN else "계약수정")
        print(f"  {len(ids):4d}  {kind.value:16s} [{mark}]")
    print("\n한 장 상세: python scripts/why_blocked.py <task_id>")
    print("정확도를 높이려면 --deep (런 로그까지 읽는다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
