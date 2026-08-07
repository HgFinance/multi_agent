"""Agent Workforce employee Worker registry: 직원 LLM 계층 없음 (부서장 + 결정론 함수)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from departments.employee_worker_runtime import (
        WorkerLLM,
        WorkerSpec,
        run_worker_registry,
        tools_for_specs,
    )
except ModuleNotFoundError:
    from employee_worker_runtime import (
        WorkerLLM,
        WorkerSpec,
        run_worker_registry,
        tools_for_specs,
    )

# ▶ 5 -> 0 통합 (2026-08-07 제안, QA 독립검증·CEO 승인 대기)
#
#   판단 기준은 두 개다. 순서가 중요하다.
#     1. 그 일을 결정론 코드가 이미 하고 있는가?  -> 워커를 지우고 함수에 맡긴다.
#     2. 그 일의 산출물을 받아갈 곳이 있는가?      -> 없으면 일 자체가 불필요하다.
#
#   ── 1번으로 제거한 셋 (일은 남고 수행 주체만 바뀜) ──────────────────────────
#     selection-performance-worker  -> scorecard/quality.py aggregate_quality()
#                                      (합산·평균, Snapshot 없으면 0 이 아니라 None 을
#                                       돌려 "결함 없음"과 "집계할 데이터 없음"을 구분)
#                                    + scorecard/cost.py assess_budget() 임계 비교
#                                    + Eval 원본은 QA 소유 audit.eval_runs (HR 이 만들지 않음)
#     lifecycle-coordination-worker -> lifecycle/access.py approve_request()/provision()/
#                                      revoke()/find_expired(). 다섯 개가 전부 거부 규칙이라
#                                      "지켜달라는 프롬프트"가 "예외를 던지는 코드"로 바뀌었다.
#     workforce-governance-worker   -> improvements/workflow.py transition()
#                                      (작성자 자기승인 차단, 독립 승인자·QA 근거 필수)
#                                    + roster/activation_evidence.py (문자열이 비었는지가 아니라
#                                       그 ID 가 DB 에 실재하는지 조회해 판정 - LLM 이 원리적으로
#                                       못 하는 검사다)
#
#   ── 2번으로 제거한 둘 (산출물의 소비자가 없음) ─────────────────────────────
#     profile-architecture-worker   Job Profile 초안을 써도 채용을 실행할 주체가 없다.
#                                   Eval Runner/Shadow Router 는 QA 소유 미구현이고
#                                   Platform/IAM 이벤트 계약도 미정이며 Roster 등재도 유예다.
#                                   workforce-management.yaml 의 승인 단계는 QA·CEO 뿐이고
#                                   required_role=USER 가 없다 - 사람이 개입하는 공식 지점은
#                                   Mandate 변경에만 있다. 실제로는 사람이 config.yaml 을
#                                   고쳐서 넣는다(이 커밋 자체가 그 예다).
#     workforce-planning-worker     인력 상황 서술의 소비자가 없다. Notion 리포트는
#                                   scripts.py 노드 3 이 결정론 템플릿(_render_report_md)으로
#                                   찍고, Scorecard 는 build_department_scorecard() 가 구조화
#                                   JSON 을 내며, 대시보드는 그 수치를 그대로 렌더링한다.
#                                   남는 소비자는 Hermes 부서장 하나인데 부서장도 LLM 이라
#                                   구조화 JSON 을 그대로 읽으면 된다 - LLM 이 LLM 에게 주려고
#                                   요약하면 정보가 줄기만 한다.
#
#   ▶ 임계값을 스스로 정하고 갱신하는 판단("Queue 10건이 맞는 기준인가")은 워커가 아니라
#     Hermes 부서장 몫이다. tool_allowlist 가 workforce.hiring_request.propose 로 제안까지만
#     허용하며, 기준값 자체는 결정론 코드의 상수이고 바꾸려면 사람이 PR 을 올린다.
#     이 경로 어디에도 직원 LLM 이 낄 자리가 없다.
#
#   ▶ 빈 registry 는 고장이 아니라 설계 상태다. run_employee_workers() 는 여전히
#     worker-context 계약을 지켜 빈 결과를 돌려주므로 dispatch 는 DEGRADED 가 아니라
#     COMPLETED 로 끝난다 - "직원이 없다"와 "직원이 실패했다"를 구분한다.
#     되살리려면 "결정론 코드가 못 하는 판정이 무엇이고 그 산출물을 누가 받는지"를
#     먼저 적는다. 역할 이름이 다르다는 것은 근거가 아니다.
WORKER_SPECS: tuple[WorkerSpec, ...] = ()


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    return run_worker_registry(WORKER_SPECS, payload, tools=tools_for_specs(WORKER_SPECS), llm=llm)
