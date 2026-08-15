"""카드가 왜 멈췄는지 분류한다.

▶ 왜 필요한가 (2026-08-14, 이 분류를 만드느라 쓴 시간이 곧 근거다)
  Hermes 는 카드가 죽으면 대개 한 문장만 남긴다:

      worker exited cleanly (rc=0) without calling kanban_complete — protocol violation

  이 문장은 **원인이 아니라 증상이다.** 에이전트가 무엇 때문에 죽었든 프로세스가
  rc=0 으로 끝나기만 하면 전부 이 한 줄로 기록된다. 실제로 blocked 190 장 중
  74 장(39%)이 이 메시지였는데, 런 로그를 열어보니 최소 세 갈래였다.

      · 자격 누락    - No Anthropic credentials found (첫 호출에서 즉사)
      · 용량 경합    - HTTP 429 동시 실행 상한 (재시도 3회로는 못 버팀)
      · 스킬 미마운트 - Unknown skill(s) (65초 만에 pid not alive)

  성격이 다르면 처방도 다르다. 자격은 배포를 고치고, 용량은 기다리면 되고,
  스킬은 마운트를 고친다. 그런데 카드만 봐서는 셋을 구분할 수 없어서, 한 장씩
  `hermes kanban log` 를 열어야 했다. 190 장에는 쓸 수 없는 방법이다.

▶ 또 하나: blocked 가 전부 실패는 아니다
  에이전트가 일을 마치고 **사람 검토를 요청하며** blocked 하는 경우가 있다
  (`review-required: ... 패치 첨부`). 이것을 실패로 세면 회복 대상이 부풀고,
  되살리면 이미 한 일을 다시 시키면서 검토 요청까지 지워진다. 실제로 11 장이
  그 상태였다.

▶ 쓰는 곳
  - `scripts/why_blocked.py` (운영자: 왜 멈췄나)
  - 배치 회복 판단 (recoverable=True 인 것만 되살린다)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class FailureKind(str, Enum):
    """처방이 다른 만큼만 나눈다 - 더 잘게 쪼개면 분류 자체가 일이 된다."""

    CREDENTIALS = "credentials"          # 배포 설정: 자격이 프로세스 환경에 없다
    CAPACITY = "capacity"                # 용량: 프록시 슬롯·상한 경합
    TIMEOUT = "timeout"                  # 시간 초과: 호출이 상한을 넘었다
    SKILL_MISSING = "skill_missing"      # 배포 설정: 스킬이 마운트/등록되지 않았다
    BAD_ASSIGNEE = "bad_assignee"        # 계약 위반: 존재하지 않는 프로필
    SCOPE_VIOLATION = "scope_violation"  # 계약 위반: 워크플로 마커 값이 틀렸다
    NEEDS_HUMAN = "needs_human"          # 실패 아님: 사람 판단을 기다린다
    # ▶ 아래 셋은 block_kind=capability 로 뭉쳐 있던 것을 갈랐다 (2026-08-14).
    #   "능력이 없다" 가 아니라 대부분 **계약이 어긋난** 것이라, 재시도로는 절대
    #   안 풀리고 배선·명세를 고쳐야 한다. 셋의 처방이 서로 다르다.
    CONTRACT_CONFLICT = "contract_conflict"  # 두 검증이 서로 배타적이다
    MISSING_TOOL = "missing_tool"            # 실행 도구·엔드포인트가 없다
    PERMISSION = "permission"                # 고칠 수 있는데 권한이 없다
    PROTOCOL = "protocol"                # 진짜 종료 계약 위반(위를 다 배제한 뒤)
    UNKNOWN = "unknown"                  # 분류 불가 - 로그를 사람이 봐야 한다


# 판정은 **구체적인 것부터** 본다. 'without calling kanban_complete' 는 다른
# 원인을 덮는 포괄 문구라 반드시 맨 뒤에 둔다 - 순서를 바꾸면 오늘의 오분류가
# 그대로 재현된다(74 장을 한 갈래로 뭉갰다).
_RULES: tuple[tuple[FailureKind, re.Pattern[str], bool], ...] = (
    # (분류, 패턴, 인프라 수리 후 되살릴 가치가 있는가)
    (FailureKind.NEEDS_HUMAN,
     re.compile(r"review-required|needs[_\s-]?input|사람.{0,6}(검토|판단|확인)", re.I), False),
    (FailureKind.CREDENTIALS,
     re.compile(r"No Anthropic credentials|ANTHROPIC_(API_KEY|TOKEN)|credentials? not found"
                r"|authenticat", re.I), True),
    (FailureKind.SKILL_MISSING,
     re.compile(r"Unknown skill\(s\)|canonical skill is not available", re.I), True),
    (FailureKind.BAD_ASSIGNEE,
     re.compile(r"non-spawnable|unknown or non-canonical Hermes profile|프로필", re.I), False),
    (FailureKind.SCOPE_VIOLATION,
     re.compile(r"workflow_scope_validation|unknown workflow_mode|WorkflowScopeViolation", re.I),
     False),
    # 계약 모순: 한쪽을 맞추면 다른 쪽이 깨지는 상태. 에이전트가 두 번 시도하고
    # 갇혔다는 서술이 남는다(실측: "excerpt 500자 초과" -> "리드 ID 제거하니
    # LEAD_IDS 필수 누락으로 재차 거절").
    (FailureKind.CONTRACT_CONFLICT,
     re.compile(r"재차 거절|다시 거절|다른 쪽이|배타|모순|규칙에 따라.{0,40}대체.{0,40}"
                r"(기대|점검)|자체점검은 이를", re.I), False),
    (FailureKind.MISSING_TOOL,
     re.compile(r"실행 가능한 도구|엔드포인트가 연결되면|도구가 없|미큐레이션|미구현"
                r"|executable_tool", re.I), False),
    (FailureKind.PERMISSION,
     re.compile(r"권한 부족|권한이 없|승인 차단|not authorized|permission denied", re.I), False),
    (FailureKind.CAPACITY,
     re.compile(r"동시 실행 상한|rate_limit|HTTP 429|slot|per_profile_capped", re.I), True),
    (FailureKind.TIMEOUT,
     re.compile(r"HTTP 504|초 초과|TimeoutExpired|timed?[_\s-]?out", re.I), True),
    (FailureKind.PROTOCOL,
     re.compile(r"without calling kanban_complete|protocol violation", re.I), True),
)


@dataclass(frozen=True)
class FailureVerdict:
    kind: FailureKind
    recoverable: bool
    evidence: str = ""

    @property
    def prescription(self) -> str:
        return {
            FailureKind.CREDENTIALS:
                "dispatcher 프로세스 환경에 자격이 있는지 확인(프로필 config 의 env: 로는 안 된다). "
                "scripts/audit_contracts.py --runtime",
            FailureKind.CAPACITY:
                "프록시 슬롯 경합. 호출 길이(중앙값 수 분)에 견주어 대기 상한이 충분한지 확인하고, "
                "필요하면 CLAUDE_PROXY_CONCURRENCY 를 조정(과금 직결이라 소유자 결정)",
            FailureKind.TIMEOUT:
                "CLAUDE_PROXY_TIMEOUT 이 실제 호출 길이보다 짧은지 확인. 프록시 로그의 소요 분포를 본다",
            FailureKind.SKILL_MISSING:
                "공유 스킬 마운트와 프로필의 skills.external_dirs 확인. "
                "scripts/audit_contracts.py --runtime",
            FailureKind.BAD_ASSIGNEE:
                "정본 프로필로 재배정(LEGACY_PROFILE_ALIASES 참고). 되살려도 같은 자리에서 멈춘다",
            FailureKind.SCOPE_VIOLATION:
                "카드 본문의 워크플로 마커 값을 확인(WORKFLOW_MODES/WORKFLOW_ROLES)",
            FailureKind.NEEDS_HUMAN:
                "실패가 아니다. 산출물을 보고 사람이 결정해야 넘어간다 - 되살리면 한 일을 다시 시킨다",
            FailureKind.PROTOCOL:
                "위 원인을 다 배제한 뒤의 진짜 종료 계약 위반. 답을 만들고도 kanban_complete 를 "
                "안 불렀는지 런 로그 끝부분을 본다",
            FailureKind.CONTRACT_CONFLICT:
                "두 검증이 서로 배타적이다. 재시도로 안 풀린다 - 명세 둘 중 무엇이 옳은지 정하고 한쪽을 고쳐야 한다(실측: 인용문 길이 상한과 리드 참조 필수가 충돌)",
            FailureKind.MISSING_TOOL:
                "실행 도구·엔드포인트가 없다. 카탈로그에 있으나 큐레이션 전인지 확인하고(ls_tr_catalog 의 executable_tool), 없으면 배선을 만들어야 한다",
            FailureKind.PERMISSION:
                "고칠 방법은 아는데 권한이 없다. 산출물(패치·진단)을 사람이 받아 적용해야 한다",
            FailureKind.UNKNOWN:
                "자동 분류 실패. hermes kanban log <카드> 의 마지막 20줄을 사람이 본다",
        }[self.kind]


def classify_failure(*texts: str) -> FailureVerdict:
    """카드 오류·블록 사유·런 로그를 함께 넣는다. 구체적인 신호가 이긴다.

    인자 순서는 상관없다 - 규칙이 구체성 순으로 정렬돼 있다. 런 로그를 같이
    넣는 것이 중요하다: 카드에 남는 문구는 대개 포괄 문구라 그것만으로는
    PROTOCOL 로만 보인다.
    """

    blob = "\n".join(str(t or "") for t in texts)
    if not blob.strip():
        return FailureVerdict(FailureKind.UNKNOWN, False)
    for kind, pattern, recoverable in _RULES:
        match = pattern.search(blob)
        if match:
            line = next(
                (ln.strip() for ln in blob.splitlines() if pattern.search(ln)),
                match.group(0),
            )
            return FailureVerdict(kind, recoverable, line[:180])
    return FailureVerdict(FailureKind.UNKNOWN, False)


__all__ = ["FailureKind", "FailureVerdict", "classify_failure"]
