"""Agent Workforce employee Worker registry: proposals only, no self-approval or IAM grant."""

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

# ▶ 5 -> 2 통합 (2026-08-06 제안, QA 독립검증·CEO 승인 대기)
#   판단 기준: 워커가 "판정"을 하는가, 아니면 이미 결정론 코드가 내린 판정을 서술만 하는가.
#   employee_worker_runtime.build_independent_worker_graph 는 다섯 워커에 같은 3노드
#   (tool -> LLM -> validate)를 준다 - 분기도 루프도 도구 선택도 없다. 따라서 워커를
#   나누는 근거는 "역할이 다르다"가 아니라 "생성해야 할 자연어가 다르다"여야 한다.
#
#   제거한 셋과 그 판정을 이미 소유한 결정론 코드:
#     selection-performance-worker  -> scorecard/quality.py + audit.eval_runs(QA 소유)
#     lifecycle-coordination-worker -> lifecycle/access.py (요청·승인·부여·회수 전 구간)
#     workforce-governance-worker   -> improvements/workflow.py(자기승인 차단) +
#                                      roster/activation_evidence.py(증거 실재성 403/409)
#   셋 다 input_fields 가 전부 구조화 데이터였고 판정은 위 모듈이 이미 하고 있었다.
#   서술이 필요한 부분만 남은 두 워커가 흡수한다 - 성과 서술은 planning 이,
#   권한 경계 문장은 profile 이 가져간다(Job Profile 의 prohibited authorities 와 같은 산출물).
#
#   ▶ 통합의 유일한 비용: planning 워커의 읽기 범위가 workforce.evaluation.read 만큼
#     넓어진다. 읽기 전용이고 판정 권한은 아니지만 최소권한 원칙상 QA 검토 대상이다.
WORKER_SPECS = (
    WorkerSpec("workforce-planning-worker", "Queue, SLA, cost and performance narration analyst", ("workforce.queue.read", "workforce.sla.read", "workforce.evaluation.read"), "always", ("queue_metrics", "sla_metrics", "cost_metrics", "evaluation", "scorecard", "probation")),
    WorkerSpec("profile-architecture-worker", "Agent profile, role-boundary and prohibited-authority architect", ("workforce.profile.read", "workforce.governance.read"), "always", ("profile", "role_requirements", "tool_catalog", "separation_of_duties", "department_boundary")),
)


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    return run_worker_registry(WORKER_SPECS, payload, tools=tools_for_specs(WORKER_SPECS), llm=llm)
