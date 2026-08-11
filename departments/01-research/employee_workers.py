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

# ▶ 스카우트 4인은 같은 trigger(scout_cycle)와 같은 도구를 쓴다. 다른 것은 **렌즈**뿐이다 -
#   같은 질문을 서로 다른 데서 찾게 하는 것이 이 편제의 전부다.
# ▶ trigger 가 always 인 워커는 market-context 하나다. 스카우트·회의론자·기획자는
#   소집형이다 - 상시 켜두면 큐만 쌓이고 편집장이 읽지 못한다.
WORKER_SPECS = (
    WorkerSpec("methodology-scout-academic", "Academic methodology scout for papers and preprints", ("research.web.search", "research.web.open", "research.web.verify"), "scout_cycle", ("scout_cycle", "methodology_leads")),
    WorkerSpec("methodology-scout-practitioner", "Practitioner methodology scout for investor letters and desk notes", ("research.web.search", "research.web.open", "research.web.verify"), "scout_cycle", ("scout_cycle", "methodology_leads")),
    WorkerSpec("competing-explanation-worker", "Competing explanation and falsification analyst", ("research.outcomes.read", "research.evidence.search"), "proposal_draft", ("proposal_draft",)),
    WorkerSpec("holdings-analyst-worker", "Portfolio holdings question-answering analyst", ("research.evidence.search", "research.news.read", "research.market_snapshot.read"), "holding_question", ("holding_question", "portfolio_state", "news")),
)


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    return run_worker_registry(WORKER_SPECS, payload, tools=tools_for_specs(WORKER_SPECS), llm=llm)
