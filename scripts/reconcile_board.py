#!/usr/bin/env python3
"""보드를 계약에 맞춘다 - 실행 직전에 거르는 admission 단계.

▶ 왜 이 자리인가 (문헌 + 2026-08-14 실측)
  실측: 계약 4건에 위반을 시도했더니 **4건 전부 통과**했다. 존재하지 않는
  프로필, legacy 별칭, 없는 workflow_mode, 계약에 없는 스킬 - 에이전트가 쓰는
  경로(`hermes kanban create`)는 우리 타입 경계를 지나지 않는다.

  Kubernetes admission control 의 교훈이 그대로다: **강제는 쓰기 경로에, 저장
  직전에** 있어야 하고 클라이언트 검증은 권한만 있으면 우회된다. LLM 에이전트
  가드레일 쪽 결론도 같다 - 프롬프트는 제안이고 런타임이 가로채야 한다.

  그런데 우리는 쓰기 경로(Hermes kanban)를 소유하지 않는다. 소유한 것은
  **실행 경계**(디스패처)뿐이다. 그래서 GitOps/IAM 의 세 번째 패턴을 쓴다:
  예방할 수 없으면 **탐지 + 자동교정 루프**를 돌리고 권위 있는 원본을 지정한다.
  권위 원본은 orchestration/canonical_profiles.py 다.

▶ 순서도 문헌을 따른다: mutating 먼저, validating 나중
  - 자동교정(mutating): 알려진 legacy 별칭은 정본으로 재배정한다. 오늘 22 장이
    이 경우였고, 그냥 막았다면 22 장이 blocked 로 쏟아졌을 뿐 일은 안 됐다.
  - 차단(validating): 정규화도 안 되는 이름은 blocked 로 보낸다. 지금은
    디스패처가 매 tick 조용히 건너뛰기만 해서 **이틀간 아무도 몰랐다** -
    조용한 실패가 시끄러운 실패보다 나쁘다.

사용:
  python scripts/reconcile_board.py            # 무엇을 할지만 (기본)
  python scripts/reconcile_board.py --apply    # 실제로 교정·차단
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestration.canonical_profiles import (  # noqa: E402
    CANONICAL_PROFILES,
    LEGACY_PROFILE_ALIASES,
)

# 컨테이너 안에서 돌 때는 자기 자신이 dispatcher 다. 밖에서 돌면 docker exec.
_IN_DISPATCHER = Path("/opt/data/shared-kanban").exists()
DISPATCHER = "hedgefund-kanban-dispatcher"
KANBAN_CLI = "hedgefund-qa-hermes"

# 살아 있는 상태만 본다. 이미 끝난 카드는 고쳐도 의미가 없고, archived 를 건드리면
# 과거 기록을 바꾸는 것이 된다.
_LIVE = ("todo", "ready", "running", "blocked")


def _kanban(args: list[str], timeout: int = 120) -> tuple[int, str]:
    cmd = (["hermes", "kanban", *args] if _IN_DISPATCHER
           else ["docker", "exec", "-u", "hermes", KANBAN_CLI, "hermes", "kanban", *args])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, (proc.stdout or "")


def _live_rows() -> list[dict]:
    script = chr(10).join((
        "import sys, json",
        "sys.path.insert(0, '/opt/hermes')",
        "from hermes_cli import kanban_db as kb",
        "with kb.connect_closing() as conn:",
        "    rows = conn.execute(\"SELECT id, assignee, status FROM tasks"
        f" WHERE status IN {_LIVE}\").fetchall()",
        "    print(json.dumps([dict(r) for r in rows], ensure_ascii=False))",
    ))
    cmd = (["python3", "-c", script] if _IN_DISPATCHER
           else ["docker", "exec", DISPATCHER, "python3", "-c", script])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180, check=False)
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="실제로 교정·차단한다")
    parser.add_argument("--quiet", action="store_true", help="할 일이 없으면 아무것도 찍지 않는다")
    args = parser.parse_args()

    rows = _live_rows()
    if not rows:
        if not args.quiet:
            print("보드를 읽지 못했거나 살아 있는 카드가 없다")
        return 0

    remap: list[tuple[str, str, str]] = []   # (task, 현재, 정본)
    unknown: list[tuple[str, str]] = []      # (task, 이름)
    for row in rows:
        assignee = (row.get("assignee") or "").strip()
        if not assignee or assignee in CANONICAL_PROFILES:
            continue
        target = LEGACY_PROFILE_ALIASES.get(assignee)
        (remap.append((str(row["id"]), assignee, target)) if target
         else unknown.append((str(row["id"]), assignee)))

    if not remap and not unknown:
        if not args.quiet:
            print(f"보드 정합 - 살아 있는 카드 {len(rows)}장 모두 정본 프로필")
        return 0

    verb = "교정" if args.apply else "교정 예정"
    for task_id, current, target in remap:
        print(f"  [{verb}] {task_id}: {current} -> {target}")
        if args.apply:
            _kanban(["reassign", task_id, target, "--reason",
                     f"계약 자동교정: {current} 는 legacy 별칭이다(정본 {target}). "
                     "디스패처는 정본이 아닌 assignee 를 매 tick 건너뛰기만 해서 "
                     "카드가 조용히 영원히 대기한다"])

    for task_id, name in unknown:
        print(f"  [{'차단' if args.apply else '차단 예정'}] {task_id}: {name} (정규화 불가)")
        if args.apply:
            # reason 은 위치 인자다(--reason 이 아니다). kind 를 capability 로 두는
            # 이유: 사람이 프로필을 정해 주기 전에는 어떤 재시도로도 안 풀린다.
            _kanban(["block", task_id, "--kind", "capability",
                     f"정본이 아닌 프로필 '{name}' 로 배정돼 디스패처가 실행할 수 없다. "
                     "정본 프로필로 재배정해야 한다(orchestration/canonical_profiles.py). "
                     "그냥 두면 매 tick 조용히 건너뛰어 영원히 대기한다"])

    print(f"\n자동교정 {len(remap)}장 / 차단 {len(unknown)}장"
          + ("" if args.apply else "  (--apply 로 실행)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
