"""Research employee Worker registry: hypothesis supply, no trade decision.

2026-08-10 전면 재편 (재일). 이전 편제는 종목 애널리스트 6인(universe/microstructure/
technical/fundamental/news-macro/evidence-rag)이었고 산출물은 종목별 Research Packet 이었다.
그 편제는 **프레임워크 자체가 투자판단을 내리는** 구조를 전제한다 - 리서치가 종목 견해를
내면 그것이 곧 매매 근거가 된다.

전략 공장에서 리서치본부는 **가설 공급 조직**이다. 웹(논문·실무자 글·커뮤니티·타 분야)에서
방법론을 수집해 반증 가능한 실험 기획안으로 만들어 퀀트본부에 넘긴다. 종목 방향·확률
예측은 하지 않는다 - 그것은 실험을 통과해 승격된 전략의 몫이다.

▶ 왜 스카우트를 렌즈별로 나누는가
  한 워커에게 "웹에서 방법론을 찾아라"라고 하면 검색이 한 방향으로 쏠린다. 렌즈(학술·
  실무·커뮤니티·타분야)를 나눠 **서로의 결과를 보지 않고** 동시에 뒤지면, 한 관점이
  놓치는 광맥을 다른 관점이 잡는다. 이것이 직원을 두는 이유(병렬성 + 맥락 격리)다.

▶ 회의론자를 분리하는 이유
  경쟁 설명("그냥 베타 아닌가")을 기획안 작성자와 같은 맥락에서 쓰게 하면 반드시
  앵커링된다. 회의론자는 채택 사유를 보지 않고 초안만 본다 - 독립성은 프롬프트가 아니라
  **입력 격리**로만 만들어진다.

▶ 시장을 보는 자리를 둘로 나눈 이유
  ① market-context-worker: 실험 기획은 "이 유니버스가 실재하는가, 데이터가 그만큼
     있는가, 지금 시장 상태가 어떤가"를 알아야 성립한다. 방향 예측이 아니라
     **실행 가능성 판정 재료**다. 공장 쪽 자리다.
  ② holdings-analyst-worker: 사용자가 자기 포트폴리오의 개별 종목을 물어볼 때 답하는
     자리다. **서비스 쪽 자리**이고 공장 흐름에 들어가지 않는다 - 이 산출물은
     기획안의 근거가 되지 않고 주문 경로에도 닿지 않는다. 둘을 한 워커로 합치면
     "사용자에게 설명한 견해"가 "실험의 근거"로 새어 들어간다.

기존 종목 애널리스트 편제는 **운영에서 내린다.** 코드(agents/, scripts.py)는 삭제하지
않지만 런타임 직원 계층에서 빠지고 어떤 공장 흐름에도 연결되지 않는다 - 나중에 실험을
돌려보고 쓸 만하다고 판단되면 그때 하나의 전략 후보로 다시 올리면 되고, 그것은 지금
결정할 일이 아니다. 지금은 공장 하나에만 집중한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from departments.employee_worker_runtime import (
        WorkerLLM,
        WorkerLLMFactory,
        WorkerSpec,
        run_worker_registry,
        tools_for_specs,
    )
except ModuleNotFoundError:
    from employee_worker_runtime import (
        WorkerLLM,
        WorkerLLMFactory,
        WorkerSpec,
        run_worker_registry,
        tools_for_specs,
    )

# ▶ 방법론 스카우트는 여기 없다. 웹 검색·열람·검증 도구가 **본부장(Hermes)에만**
#   있어서 로컬 모델 워커로는 조사 자체가 성립하지 않기 때문이다 - 자리를 남겨두면
#   "조사했는데 아무것도 못 찾았다"가 조용히 정상처럼 보인다. 공장 쪽 조사는
#   본부장이 직접 하고, 필요하면 서브에이전트를 부른다.
#
# ▶ `holdings-analyst-worker` 는 **서비스 쪽 자리**라 남는다(위 ② 참고).
#   빼놨다가 되살렸다 - 이 워커는 포트폴리오 자문 경로(`/ui/portfolio-recommendations`)
#   가 실제로 부르는 유일한 리서치 워커다. 없애면 그 화면의 리서치 구간이
#   "0개 Worker 결과"로 조용히 비고, 사용자는 그게 실패인지 원래 그런 건지 모른다.
#   도구가 `research.*` **읽기 API** 라 본부장 전용 웹 도구와 사정이 다르다 -
#   로컬 모델로도 성립한다(스카우트를 뺀 근거가 이 워커에는 해당하지 않는다).
WORKER_SPECS = (
    WorkerSpec("competing-explanation-worker", "Competing explanation and falsification analyst", ("research.outcomes.read", "research.evidence.search"), "proposal_draft", ("proposal_draft",)),
    WorkerSpec("holdings-analyst-worker", "Portfolio holdings question-answering analyst", ("research.evidence.search", "research.news.read", "research.market_snapshot.read"), "holding_question", ("holding_question", "portfolio_state", "news")),
)


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None,
                         llm_factory: WorkerLLMFactory | None = None) -> dict[str, Any]:
    """llm_factory 가 있으면 워커별로 모델 좌표를 해석한다(부서 LoRA 경로).

    단일 llm 은 워커 정체를 모른 채 공유되므로 Worker Model Gateway 의
    worker_id→adapter 해석이 전달되지 않는다 - MCP 경로는 factory 를 쓴다.
    """
    return run_worker_registry(WORKER_SPECS, payload,
                               tools=tools_for_specs(WORKER_SPECS),
                               llm=llm, llm_factory=llm_factory)
