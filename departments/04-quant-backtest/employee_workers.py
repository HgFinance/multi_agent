"""Quant/Backtest employee Worker registry: validation laboratory, no promotion authority.

2026-08-10 재편 (재일). 이전 편제는 LLM 직원 7인(hypothesis/dataset/backtest/release/ml/
cost/regime)이었다. 그런데 이 부서의 일 대부분은 **이미 결정론 코드**다 - PIT 인증,
백테스트, walk-forward, DSR, PBO(CSCV), 국면 분해, 릴리스 관문은 전부 pipeline/ 의
파이썬이 계산하고 판정한다. LLM 을 그 자리에 겹쳐 두면 두 가지가 생긴다: 계산을
두 번 하거나, 판정을 흉내 내거나. 둘 다 나쁘다.

그래서 LLM 직원은 **결정론 코드가 못 하는 일**에만 남긴다.

  ① 접수 - 리서치 기획안을 읽고 사양 초안을 만든다(어휘 사상·데이터 요구 확인).
     기획안은 자연어가 섞여 있어 코드가 혼자 읽을 수 없다.
  ② 설계 - EDA 결과를 보고 창·파라미터 범위를 제안한다. 제안일 뿐 판정이 아니다.
  ②-b 전략 코드 작성 - 기성 템플릿으로 표현 안 되는 방법론은 **시그널 코드를 쓴다.**
     이게 고정 실행면에 막히지 않는 지점이다. 코드는 사전등록 지문에 해시로
     들어가고(strategy_spec.spec_hash), 결정론·PIT·반환형 검사를 통과해야만
     스펙이 만들어진다 - 즉 **작성은 에이전트가, 승인은 결정론 코드가** 한다.
  ③ 해석 - 카드의 통계(DSR·PBO·국면)를 사람이 읽을 문장으로 만든다. **숫자는
     건드리지 않는다** - 판정은 release_gate 가 이미 내렸다.
  ④ 교훈 - 기각 사유를 통제 어휘 lesson_code 로 사상한다. 이게 리서치 환류의
     품질을 정한다. 자유 서술로 남기면 다음 가설이 기계 대조를 못 한다.

가설 "발굴"은 이제 이 부서 일이 아니다. 리서치본부가 방법론을 수집해 실험 기획안
(ExperimentProposalV1)으로 넘긴다 - 퀀트가 스스로 가설을 만들면 낸 사람이 검증까지
하는 셈이라 생성자·검증자 분리가 깨진다.
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
    WorkerSpec("result-interpretation-worker", "Backtest result, overfitting and regime interpretation analyst", ("quant.experiment_card.read",), "experiment_card", ("experiment_card", "trial_pressure", "regime_breakdown")),
    WorkerSpec("strategy-author-worker", "Custom strategy signal authoring analyst", ("quant.template_catalog.read", "quant.vocabulary.read"), "strategy_authoring", ("experiment_proposal", "methodology_leads", "template_catalog")),
)


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    # stage 는 event name 이 쓰는 이름 공간이다 - 부서 키(quant-backtest)가 아니라 quant.
    return run_worker_registry(WORKER_SPECS, payload, tools=tools_for_specs(WORKER_SPECS),
                               llm=llm, stage="quant")
