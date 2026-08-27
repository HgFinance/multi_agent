"""Quant/Backtest employee Worker registry: validation laboratory, no promotion authority.

2026-08-27 현재 LLM Worker는 두 명뿐입니다. 전략 코드 작성과 결과 해석만 담당하며,
PIT 인증·백테스트·walk-forward·DSR·PBO·국면 분석·릴리스 관문은 `pipeline/`의
결정론 코드가 소유합니다. 가설 발굴·데이터셋 구축·최적화·승격은 이 레지스트리에
등록하지 않습니다.

두 Worker 모두 읽기/제안 경계에서만 동작합니다. 수치 계산, 관문 변경, 결과 적재,
주문과 승격은 결정론 서비스와 독립 통제 부서가 담당합니다.
"""

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

# ▶ 상시(always)는 접수 하나다. 실험은 접수에서 시작하고, 나머지는 그 단계에
#   도달했을 때만 켠다 - 카드도 없는데 해석 워커를 돌릴 이유가 없다.
# ▶ 도구가 전부 .read 인 것은 의도다. 적재·판정은 pipeline/ 결정론 코드가 한다.
WORKER_SPECS = (
    WorkerSpec(
        "result-interpretation-worker",
        "Backtest result, overfitting and regime interpretation analyst",
        ("quant.experiment_card.read",),
        "experiment_card",
        ("experiment_card", "trial_pressure", "regime_breakdown"),
    ),
    WorkerSpec(
        "strategy-author-worker",
        "Custom strategy signal authoring analyst",
        ("quant.template_catalog.read", "quant.vocabulary.read"),
        "strategy_authoring",
        ("experiment_proposal", "methodology_leads", "template_catalog"),
    ),
)


def run_employee_workers(
    payload: Mapping[str, Any], *, llm: WorkerLLM | None = None
) -> dict[str, Any]:
    # stage 는 event name 이 쓰는 이름 공간이다 - 부서 키(quant-backtest)가 아니라 quant.
    return run_worker_registry(
        WORKER_SPECS,
        payload,
        tools=tools_for_specs(WORKER_SPECS),
        llm=llm,
        stage="quant",
    )
