#!/usr/bin/env python3
"""선언(코드 계약)과 런타임(프로필·마운트·보드)을 대조한다.

▶ 왜 만들었나 (2026-08-14)
  하루에 여섯 가지 원인으로 공장이 멎었는데, 그중 넷이 같은 모양이었다 -
  **선언은 있는데 런타임에 없다.**

    · 스킬 계약에 wiring-audit 이 있는데 컨테이너에는 skills/finance 만 마운트돼
      있어 카드가 65초 만에 죽었다.
    · 프로필 검증기가 risk-department 를 거부하는데, 에이전트가 그 이름으로 만든
      카드 22 장이 이틀간 조용히 정체했다(디스패처는 매 tick 건너뛰기만 한다).
    · 프로필 config 에 ANTHROPIC_API_KEY 가 적혀 있는데 hermes 는 프로세스
      환경변수를 봐서, 컨테이너 재생성 후 모든 에이전트가 첫 호출에 즉사했다.
    · 창구 프로필에 skills.external_dirs 가 없어 공유 스킬을 못 찾았다.

  전부 "카드가 안 돈다"로만 보이고 원인은 로그 깊숙이 있다. 그래서 대조를
  자동화한다. 이 스크립트가 통과하면 최소한 그 네 가지는 아니다.

사용:
  python scripts/audit_contracts.py                 # 저장소 계약만 (컨테이너 불요)
  python scripts/audit_contracts.py --runtime       # 컨테이너 런타임까지 대조
종료코드: 0 정상 / 1 불일치 발견
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
from orchestration.skill_contract import (  # noqa: E402
    CANONICAL_PROFILES as SKILL_CONTRACT_PROFILES,
)
from orchestration.skill_contract import (  # noqa: E402
    CANONICAL_SKILLS,
    PENDING_SOURCE_SKILLS,
    SKILL_OWNER_BY_NAME,
)

DISPATCHER = "hedgefund-kanban-dispatcher"
KANBAN_CLI = "hedgefund-qa-hermes"


class Findings:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def fail(self, area: str, message: str) -> None:
        self.rows.append((area, message))

    def report(self) -> int:
        if not self.rows:
            print("계약 감사 통과 - 선언과 런타임이 일치한다")
            return 0
        print(f"불일치 {len(self.rows)}건")
        for area, message in self.rows:
            print(f"  [{area}] {message}")
        return 1


def _docker(container: str, command: list[str], timeout: int = 60) -> str:
    """컨테이너 안에서 한 줄 명령. 실패는 빈 문자열로 돌려 감사가 계속되게 한다."""
    try:
        proc = subprocess.run(
            ["docker", "exec", container, *command],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


# ── 1. 저장소 안에서 닫히는 검사 (컨테이너 불요) ──────────────────────────────
def audit_repository(f: Findings) -> None:
    # 프로필 계약이 한 곳인가. 두 모듈이 각자 목록을 들면 조용히 갈라진다.
    if set(SKILL_CONTRACT_PROFILES) != set(CANONICAL_PROFILES):
        f.fail("profiles", "skill_contract 와 canonical_profiles 의 프로필 목록이 다르다: "
                           f"{sorted(set(SKILL_CONTRACT_PROFILES) ^ set(CANONICAL_PROFILES))}")

    # legacy 별칭이 정본과 겹치면 안 된다(겹치면 거부해야 할 이름을 통과시킨다).
    overlap = set(LEGACY_PROFILE_ALIASES) & set(CANONICAL_PROFILES)
    if overlap:
        f.fail("profiles", f"legacy 별칭이 정본 이름과 겹친다: {sorted(overlap)}")
    # 별칭의 목적지는 반드시 정본이어야 한다.
    for alias, target in LEGACY_PROFILE_ALIASES.items():
        if target not in CANONICAL_PROFILES:
            f.fail("profiles", f"legacy 별칭 {alias!r} 의 목적지 {target!r} 가 정본이 아니다")

    # 스킬 소유자가 정본 프로필인가.
    for skill, owners in SKILL_OWNER_BY_NAME.items():
        unknown = set(owners) - set(CANONICAL_PROFILES)
        if unknown:
            f.fail("skills", f"스킬 {skill!r} 의 소유 프로필이 정본이 아니다: {sorted(unknown)}")

    # 선언한 스킬이 저장소에 실재하는가.
    available = {p.parent.name for p in (ROOT / "skills").rglob("SKILL.md")}
    for skill in sorted(CANONICAL_SKILLS):
        if skill not in available and skill not in PENDING_SOURCE_SKILLS:
            f.fail("skills", f"계약에 있으나 저장소에 SKILL.md 가 없다: {skill}")
        if skill not in SKILL_OWNER_BY_NAME:
            f.fail("skills", f"소유 프로필이 정해지지 않았다: {skill}")
    arrived = set(PENDING_SOURCE_SKILLS) & available
    if arrived:
        f.fail("skills", f"대기 목록에 있는데 소스가 이미 들어와 있다(집합에서 빼라): {sorted(arrived)}")


# ── 2. 런타임 대조 (컨테이너 필요) ────────────────────────────────────────────
def audit_runtime(f: Findings) -> None:
    listing = _docker(DISPATCHER, ["sh", "-c", "ls /opt/data/profiles/"])
    if not listing:
        f.fail("runtime", f"{DISPATCHER} 에서 프로필 목록을 못 읽었다 - 컨테이너 확인")
        return
    runtime_profiles = {line.strip() for line in listing.splitlines() if line.strip()}

    missing = set(CANONICAL_PROFILES) - runtime_profiles
    if missing:
        f.fail("runtime", f"정본 프로필인데 런타임에 없다: {sorted(missing)}")
    # `_` 로 시작하면 은퇴 표시다 - 데이터는 남기되 assignee 로 못 쓰게 개명한 것.
    stray = {p for p in runtime_profiles - set(CANONICAL_PROFILES)
             if not p.startswith("_")}
    if stray:
        # 껍데기 프로필은 카드를 받을 수 있어서 위험하다(2026-08-14 workforce-management).
        f.fail("runtime", f"정본이 아닌 프로필 디렉터리가 있다(카드를 받을 수 있다): {sorted(stray)}")

    # 공유 스킬이 실제로 마운트돼 있는가 - 계약에 있는 것이 전부 보여야 한다.
    found = _docker(DISPATCHER, ["sh", "-c",
                                 "find /opt/shared-skills -name SKILL.md -printf '%h\\n'"])
    mounted = {Path(line).name for line in found.splitlines() if line.strip()}
    not_mounted = set(CANONICAL_SKILLS) - mounted - set(PENDING_SOURCE_SKILLS)
    if not_mounted:
        f.fail("skills", f"계약에 있으나 컨테이너에 마운트되지 않았다: {sorted(not_mounted)}")

    # 에이전트가 뜨는 곳에 자격이 있는가(프로필 config 의 env: 로는 안 된다).
    creds = _docker(DISPATCHER, ["sh", "-c", "env | grep -c '^ANTHROPIC_API_KEY='"])
    if creds.strip() not in {"1"}:
        f.fail("runtime", "dispatcher 프로세스 환경에 ANTHROPIC_API_KEY 가 없다 - "
                          "spawn 된 에이전트가 첫 호출에서 즉사한다")

    # 공유 스킬을 쓰려면 프로필마다 external_dirs 가 있어야 한다.
    for profile in sorted(CANONICAL_PROFILES):
        if profile not in runtime_profiles:
            continue
        cfg = _docker(DISPATCHER, ["sh", "-c",
                                   f"grep -c external_dirs /opt/data/profiles/{profile}/config.yaml"])
        if cfg.strip() in {"", "0"}:
            f.fail("runtime", f"{profile}: skills.external_dirs 없음 - "
                              "--skill 카드가 'Unknown skill(s)' 로 죽는다")


# ── 3. 보드 대조 (카드가 실제로 정본 이름을 쓰는가) ───────────────────────────
def audit_board(f: Findings) -> None:
    out = _docker(KANBAN_CLI, ["hermes", "kanban", "assignees"], timeout=120)
    if not out:
        f.fail("board", f"{KANBAN_CLI} 에서 assignee 목록을 못 읽었다")
        return
    for line in out.splitlines()[1:]:
        name = line.split()[0] if line.split() else ""
        if not name or name in {"NAME", "default"}:
            continue
        if name in CANONICAL_PROFILES:
            continue
        hint = LEGACY_PROFILE_ALIASES.get(name)
        suffix = f" (→ {hint} 로 재배정해야 한다)" if hint else ""
        f.fail("board", f"보드에 정본이 아닌 assignee 가 있다: {name}{suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", action="store_true",
                        help="컨테이너 런타임·보드까지 대조한다")
    parser.add_argument("--json", action="store_true", help="결과를 JSON 으로")
    args = parser.parse_args()

    f = Findings()
    audit_repository(f)
    if args.runtime:
        audit_runtime(f)
        audit_board(f)

    if args.json:
        print(json.dumps({"findings": [{"area": a, "message": m} for a, m in f.rows]},
                         ensure_ascii=False, indent=2))
        return 1 if f.rows else 0
    return f.report()


if __name__ == "__main__":
    raise SystemExit(main())
