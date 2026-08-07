# Worker 모델 배치 기준

검토일: 2026-08-07 (KST)

이 문서는 8개 Hermes Profile 안에서 실행되는 20개 LangGraph Worker(LLM)의 모델 정책이다(HR 5 -> 0 통합 제안 반영, QA 독립검증·CEO 승인 대기). 현재 Worker 모델은 역할과 무관하게 임시 저메모리 테스트용 Ollama `qwen3:1.7b`로 고정한다. 결정론 Worker(`desk-runner`, `risk-runner`, `qa-runner`, `back-office-runner`)는 모델을 부르지 않으므로 이 표에 포함하지 않는다.

HR은 LLM Worker도 결정론 Worker도 없다. 타 부서의 tool 강등은 LLM을 결정론 러너로 **바꾼** 것이지만, HR은 그 판정을 이미 일반 모듈(`scorecard/quality.py`, `lifecycle/access.py`, `improvements/workflow.py`)이 갖고 있어 러너를 새로 만들 필요도 없었다.

## 실행 계층

| 계층 | 런타임 | 모델 | 책임 |
|---|---|---|---|
| 부서장 | Hermes Agent | `openai-codex/gpt-5.6-luna` 기본, 승인된 Claude Code 대체 | Worker Context 종합, 누락·충돌 설명, 에스컬레이션 |
| 직원 | 독립 LangGraph Graph | Ollama `qwen3:1.7b` | allow-listed tool 호출, 역할별 Evidence와 비바인딩 Context 생성 |
| 통제 엔진 | 결정론적 Python | 해당 없음 | Risk/QA 판정, PIT·스키마·권한·상태 전이 |

## 현재 고정값

모든 부서의 `employee_runtime.model_default`, `active_model`, `OLLAMA_CHAT_MODEL` fallback은 임시 테스트 기준 `qwen3:1.7b`다. `qwen3:8b`, `qwen2.5`, `qwen2.5-coder`, `qwen3:14b`는 이전 기준·Modelfile 또는 실험 문서의 값이며 현재 Worker 기본값으로 해석하지 않는다.

`light`·`standard`·`heavy`는 미래 교체를 위한 분류일 뿐 현재 서로 다른 모델을 배치한다는 뜻이 아니다.

| Tier | 후보 업무 | 현재 모델 |
|---|---|---|
| light | 라우팅, 검색, 단순 상태·포맷 검증 | `qwen3:1.7b` |
| standard | 도메인 분석, 근거 요약, 조건부 검토 | `qwen3:1.7b` |
| heavy | 충돌 조정, 다중 근거 합성, 복합 시나리오 검토 | `qwen3:1.7b` |

## 부서별 배치

| 부서 | Worker 수(LLM) | 항상 / 조건부 |
|---|---:|---:|
| CEO | 1 | 1 / 0 |
| HR | 0 | 0 / 0 |
| Research | 6 | 2 / 4 |
| Trading | 2 (+결정론 1) | 2 / 0 |
| Risk | 1 (+결정론 1) | 0 / 1 |
| Quant / Backtest | 7 | 2 / 5 |
| Accounting / Portfolio | 1 (+결정론 1) | 1 / 0 |
| QA | 2 (+결정론 1) | 0 / 2 |

세부 Worker ID·역할·통합 판정은 [WORKER_ROLE_BOUNDARIES.md](WORKER_ROLE_BOUNDARIES.md)에 둔다. 현재 도현님 담당 부서는 Trading의 `bull-thesis-worker`·`bear-thesis-worker`와 Accounting/Portfolio의 `exception-investigation-worker`만 LLM 모델을 사용하고, `desk-runner`·`back-office-runner`는 결정론 경로다. 실제 실행 메타데이터는 `config.yaml`, `employee_workers.py`와 결정론 Worker Registry에서 읽으며, `agent.personalities`는 호환 Alias다.

## 모델 변경 승인 절차

1. `ollama list`로 설치된 후보 모델과 digest를 고정한다.
2. Worker별 Golden/Adversarial 평가에서 정확성·환각·지연·비용을 비교한다.
3. HR이 변경 제안과 Rollback 모델을 등록한다.
4. QA가 독립 회귀·권한·결정론 검증을 수행한다.
5. CEO가 승인한 뒤 Profile, `OLLAMA_*_MODEL`, 테스트 Fixture를 함께 변경한다.

자동 교체와 부서 간 임의 모델 공유는 허용하지 않는다.
