#!/usr/bin/env python3
"""계약이 **실제로 강제되는지** 위반을 시도해서 잰다.

▶ 왜 필요한가 (2026-08-14)
  계약이 "있다"와 "작동한다"는 다르다. 오늘 실측이 그 증거다:
  `validate_canonical_profile` 은 정확히 동작하는데 **에이전트가 그 경로를 안
  지나서** 존재하지 않는 프로필로 만든 카드 22 장이 통과했고, 이틀간 조용히
  정체했다. 독립 QA·리스크 게이트가 그동안 한 번도 실행되지 않았다.

  선언 대조(tests)와 런타임 대조(audit_contracts.py)는 "있는가"를 본다.
  이 스크립트는 "**우회할 수 있는가**"를 본다 - 에이전트가 실제로 쓰는 경로로
  위반을 시도해 보고, 막히면 강제되는 것이고 통과하면 구멍이다.

▶ 파괴적이지 않다
  만든 카드는 즉시 아카이브한다. 기본은 `--dry-run` 이라 아무것도 만들지 않고
  무엇을 시도할지만 보여 준다.

사용:
  python scripts/probe_contract_enforcement.py            # 계획만 (기본)
  python scripts/probe_contract_enforcement.py --execute  # 실제로 시도하고 정리
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

KANBAN_CLI = "hedgefund-qa-hermes"
TITLE_PREFIX = "계약강제 시험(자동정리)"


@dataclass
class Probe:
    name: str
    contract: str
    attempt: str          # 무엇을 위반하려 하는가
    expectation: str      # 강제된다면 어떻게 돼야 하는가


PROBES = (
    Probe(
        "존재하지 않는 프로필로 카드 생성",
        "canonical_profiles.validate_canonical_profile",
        "assignee=ai-qa-audit (부서 디렉터리 이름, 정본 아님)",
        "생성이 거부돼야 한다 - 통과하면 그 카드는 영원히 안 돈다",
    ),
    Probe(
        "정본 아닌 legacy 별칭으로 카드 생성",
        "canonical_profiles.LEGACY_PROFILE_ALIASES",
        "assignee=risk-department (legacy alias)",
        "생성이 거부되거나 정본으로 정규화돼야 한다",
    ),
    Probe(
        "없는 워크플로 모드를 본문에 심기",
        "ceo_workflow_scope.WORKFLOW_MODES",
        "workflow_mode=user_query (유효값은 analysis|binding)",
        "생성 시점에 거부돼야 한다 - 통과하면 감독관이 뒤늦게 abort 한다",
    ),
    Probe(
        "계약에 없는 스킬을 강제 로드",
        "skill_contract.CANONICAL_SKILLS",
        "--skill nonexistent-skill-probe",
        "생성이 거부돼야 한다 - 통과하면 실행 시점에 즉사한다",
    ),
)


def _kanban(args: list[str], timeout: int = 120) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["docker", "exec", "-u", "hermes", KANBAN_CLI, "hermes", "kanban", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _create(assignee: str, body: str, skill: str | None) -> tuple[bool, str, str]:
    """카드 생성을 시도한다. (성공?, task_id, 메시지)"""
    args = [
        "create", f"{TITLE_PREFIX} {uuid.uuid4().hex[:8]}",
        "--assignee", assignee,
        "--created-by", "contract-enforcement-probe",
        "--body", body,
        "--json",
    ]
    if skill:
        args.extend(["--skill", skill])
    code, out, err = _kanban(args)
    if code != 0:
        return False, "", (err or out).strip().splitlines()[-1] if (err or out).strip() else "거부"
    try:
        payload = json.loads(out[out.index("{"):])
        return True, str(payload.get("task_id") or payload.get("id") or ""), "생성됨"
    except Exception:
        return True, "", "생성됐으나 ID 파싱 실패"


def _cleanup(task_ids: list[str]) -> int:
    done = 0
    for task_id in task_ids:
        if task_id:
            code, _, _ = _kanban(["archive", task_id])
            done += 1 if code == 0 else 0
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="실제로 위반을 시도한다(만든 카드는 즉시 아카이브)")
    args = parser.parse_args()

    if not args.execute:
        print("계획 (실행하려면 --execute)")
        for probe in PROBES:
            print(f"\n  {probe.name}")
            print(f"    계약 : {probe.contract}")
            print(f"    시도 : {probe.attempt}")
            print(f"    기대 : {probe.expectation}")
        return 0

    created: list[str] = []
    holes = 0
    print(f"계약 강제 실측 - 위반 {len(PROBES)}건 시도\n")

    attempts = (
        ("ai-qa-audit", "probe body", None),
        ("risk-department", "probe body", None),
        ("research-department", "workflow_mode=user_query\nprobe body", None),
        ("research-department", "probe body", "nonexistent-skill-probe"),
    )
    for probe, (assignee, body, skill) in zip(PROBES, attempts):
        ok, task_id, message = _create(assignee, body, skill)
        if task_id:
            created.append(task_id)
        if ok:
            holes += 1
            print(f"  [구멍] {probe.name}")
            print(f"         계약 {probe.contract} 을 안 지나고 통과했다 ({task_id or '?'})")
            print(f"         {probe.expectation}")
        else:
            print(f"  [강제] {probe.name} - 거부됨: {message[:90]}")

    cleaned = _cleanup(created)
    print(f"\n시험 카드 {len(created)}장 생성 -> {cleaned}장 정리")
    print(f"결과: 계약 {len(PROBES)}건 중 **{holes}건이 우회 가능**")
    if holes:
        print("\n우회 가능한 계약은 '있다'고 말할 수 없다. 강제 지점을 만들거나,")
        print("최소한 감사(scripts/audit_contracts.py)가 사후에 잡도록 해야 한다.")
    return 1 if holes else 0


if __name__ == "__main__":
    raise SystemExit(main())
