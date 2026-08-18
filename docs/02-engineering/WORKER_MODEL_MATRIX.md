# Worker 모델 배치 기준

> **Historical/target matrix:** 현재 worker 이름·분류·개수의 정본은
> [CURRENT_PROJECT_ARCHITECTURE.md](../CURRENT_PROJECT_ARCHITECTURE.md)와
> 각 부서 `hermes/config.yaml`/`employee_workers.py`다. 이 문서의 Research·Quant
> 목표 편제와 기준 커밋은 현재 checkout과 다를 수 있으며, serving model 전환의
> runtime 증거로 사용하지 않는다.

검토 기준: 2026-08-11 (KST)

이 문서는 8개 Hermes Profile 안에서 실행되는 부서장과 직원 Worker의 모델 및 배치 기준을 정의한다. 직원 수는 Profile의 호환용 `agent.personalities`가 아니라 각 부서의 Worker Registry와 `employee_workers.py`를 기준으로 센다.

## 기준 커밋

Research·Quant의 최신 목표 편제는 `e2ed21e`(`research/quant: 전략 공장으로 재편`)를 기준으로 한다.

- Research: 고정 Worker 6명에서 8명으로 재편한다. 기존 6개 역할은 감사·계보 추적을 위해 남길 수 있지만 최신 런타임 편제에는 포함하지 않는다.
- Quant/Backtest: 7명에서 4명으로 축소한다. 가설 발굴은 Research로 이동하고, Quant는 제안 접수·실험 설계·결과 해석·교훈 축적에 집중한다.
- 이 기준 커밋은 현재 `qa-department` 브랜치에 아직 병합되지 않았다. 따라서 아래 표는 **최신 목표 기준**이며, 현재 브랜치의 실제 Research 6명·Quant 7명과 다를 수 있다.

## 수와 실행 계층의 정의

- `head_count`: Hermes 부서장 수. LLM Worker 수에 포함하지 않는다.
- `LLM Worker`: `employee_workers.py`에 등록된 독립 LangGraph Worker. 아래 표의 Worker 수와 모델 정책의 대상이다.
- `deterministic_runner`: LLM을 호출하지 않는 결정론적 실행기. LLM Worker 수에서 제외하고 별도로 표시한다.
- `active_worker_count`: 모든 해당 요청에서 기본적으로 고려되는 Worker 수.
- `conditional_worker_count`: trigger나 작업 유형이 맞을 때만 실행되는 Worker 수.
- `default_execution_count`: 요청당 기본 실행 수다. 직원 총원과 같은 의미가 아니다.

## 모델 정책

| 실행 계층 | 런타임 | 모델 및 기준 |
|---|---|---|
| 부서장 | Hermes Agent | 기본 `openai-codex/gpt-5.6-luna`; 승인된 Claude Code 런타임을 대체로 허용 |
| LLM Worker | 독립 LangGraph Graph | Ollama `qwen3:1.7b` (`OLLAMA_CHAT_MODEL`) |
| 결정론 Runner | Python 결정론 모듈 | 모델 호출 없음. PIT 필터, 한도, 계약, 상태 전이, 산술 검증을 담당 |

부서장과 Worker의 Credential·Memory Namespace·Tool Allowlist는 부서별로 분리한다. Worker의 모델은 부서장 모델과 분리하며, 모델 변경은 평가·QA·CEO 승인 후 Profile과 환경변수를 함께 변경한다.

## 최신 목표 인원 및 배치

| 부서 | Head | LLM Worker | 상시 | 조건부 | 결정론 Runner | 최신 목표 역할 |
|---|---:|---:|---:|---:|---:|---|
| CEO Office | 1 | 1 | 1 | 0 | 1 | `executive-briefing-worker` + `ceo-runner`(결정론, 표의 모델 배정 대상 아님) |
| Agent Workforce (HR) | 1 | 1 | 0 | 1 | 0 | `profile-architecture-worker` |
| Research | 1 | 8 | 1 | 7 | 0 | 방법론 스카우트 4명, 경쟁 설명, 실험 기획, 시장 맥락, 보유종목 분석 |
| Trading | 1 | 0 | 0 | 0 | 1 | `desk-runner`; 전략별 임시 Worker는 고정 직원 수에 포함하지 않음 |
| Risk | 1 | 1 | 0 | 1 | 1 | `compliance-policy-worker` + `risk-runner` |
| Quant / Backtest | 1 | 4 | 1 | 3 | 0 | 제안 접수, 실험 설계, 결과 해석, 결과·교훈 축적 |
| Accounting / Portfolio | 1 | 1 | 1 | 0 | 1 | `exception-investigation-worker` + `back-office-runner` |
| AI QA / Audit | 1 | 2 | 0 | 2 | 1 | `hallucination-critic-worker`, `incident-postmortem-worker` + `qa-runner` |
| **합계** | **8** | **18** | **4** | **14** | **4** | **총 실행 인원 30명(Head + LLM Worker + Runner)** |

Trading의 `desk-runner`, Risk의 `risk-runner`, Accounting의 `back-office-runner`, QA의 `qa-runner`는 결정론 경로다. 이들은 LLM Worker로 세지 않지만 해당 부서의 안전·계약·감사 흐름에서는 요청에 따라 항상 또는 선행 단계로 실행될 수 있다.

## Research 최신 Worker ID

최신 목표의 Research Worker는 다음 8개다.

1. `methodology-scout-academic`
2. `methodology-scout-practitioner`
3. `methodology-scout-community`
4. `methodology-scout-crossdomain`
5. `competing-explanation-worker`
6. `experiment-planner-worker`
7. `market-context-worker`
8. `holdings-analyst-worker`

## Quant 최신 Worker ID

최신 목표의 Quant Worker는 다음 4개다.

1. `proposal-intake-worker`
2. `experiment-design-worker`
3. `result-interpretation-worker`
4. `outcome-lesson-worker`

Research와 Quant 모두 최신 목표에서는 요청당 기본 실행 수를 1로 둔다. 나머지 Worker는 trigger와 작업 유형에 따라 조건부로 실행한다. Worker 수가 많다는 이유만으로 한 요청에서 전부 실행하지 않는다.

## 현재 브랜치와의 차이

현재 `qa-department` 브랜치의 설정은 다음과 같다.

| 부서 | 현재 브랜치 | 최신 목표 | 차이 |
|---|---:|---:|---:|
| Research | 6 | 8 | 최신 목표가 2명 많음 |
| Quant / Backtest | 7 | 4 | 최신 목표가 3명 적음 |

최신 목표를 실제 런타임에 적용하려면 `e2ed21e`의 Research·Quant 코드, Profile, 테스트 변경이 현재 실행 브랜치에 병합되어야 한다. 이 문서의 목표 표만으로 현재 브랜치의 Worker가 자동 변경되지는 않는다.

세부 역할·Trigger·Tool 권한은 [WORKER_ROLE_BOUNDARIES.md](WORKER_ROLE_BOUNDARIES.md)와 각 부서의 `hermes/config.yaml`, `employee_workers.py`를 함께 확인한다.

## 모델 승인 절차

1. `ollama list`로 설치된 후보 모델과 digest를 고정한다.
2. Worker별 Golden·Adversarial 평가에서 정확성·환각·지연·비용을 비교한다.
3. Agent Workforce가 제안 모델과 Rollback 모델을 등록한다.
4. AI QA / Audit가 독립 회귀·권한·결정론 검증을 수행한다.
5. CEO 승인 후 Profile, `OLLAMA_*_MODEL`, 테스트 Fixture를 함께 변경한다.

자동 모델 교체와 부서 간 임의 모델 공유는 허용하지 않는다. LLM은 관련성 판단과 서술 작성에만 사용하며, PIT 필터·인용 검증·한도 검사·상태 전이는 결정론 코드가 소유한다.
