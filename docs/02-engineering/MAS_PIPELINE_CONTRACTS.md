# MAS 부서 연결·계약·리플레이 기준

## 실행 경로의 범위

`portfolio-recommendation-full`은 모든 부서를 매 요청마다 호출하는 고정 fan-out이 아니다. CEO task plan이 요청 의도와 카테고리를 해석하고, 업무에 필요한 부서만 실행한다.

| 경로 | 실행 부서 | 안전 스킵/분리 정책 |
| --- | --- | --- |
| 기본 포트폴리오 추천 | research → risk → qa → ceo | trading/accounting은 주문·원장 변경이 없어 `SKIPPED_SAFE` |
| 전체 투자 검토(`REBALANCING_PROPOSAL` 또는 구조화 테스트 입력) | research → trading → risk → qa → accounting → ceo | 각 단계는 독립 Worker fan-out/fan-in |
| 전략 연구 | quant-backtest → qa → ceo | `strategy-research` 별도 체인, Trading 승격·주문 제출 금지 |
| HR/Agent 생명주기 | `workforce-management`/`agent-evolution` | 투자 포트폴리오 파이프라인과 혼합하지 않음 |

따라서 “전체 부서 연결”의 검증 기준은 모든 경로를 무조건 실행하는 것이 아니라, 각 경로의 선택·스킵·실패 전파가 계약대로 동작하는 것이다. Quant와 HR은 포트폴리오 추천 그래프에 암묵적으로 끼워 넣지 않고 각 선언된 Workflow에서 별도로 검증한다.

이 문서는 사용자 적합성 포트폴리오 파이프라인의 내부 연결 기준이다. 현재
파이프라인은 국내 주식 Watchlist를 기본 유니버스로 사용하며, 결과는 자문용
Projection이다. 주문·승인·원장 변경·Broker 호출은 이 흐름에 포함되지 않는다.

## 책임 경계

```text
CEO router
  └─ department head handoff
       └─ independent LangGraph Worker fan-out
            └─ department fan-in + contract gate
                 └─ next department head
```

- 사용자의 자유 질의와 카테고리는 CEO task plan의 입력이다. `requested_departments`
  에서 선택되지 않은 부서는 `SKIPPED_SAFE`로 기록하고 실행하지 않는다.
- 부서 간 전달자는 `department:head`이고, Worker끼리 다른 부서를 직접 호출하지
  않는다. `DepartmentHandoff`는 `mas.department-handoff.v1` 계약으로 검증된다.
- Worker는 `WorkerContextOutput`(`summary`, `confidence`, `evidence_refs`,
  `escalate`, `schema_valid`)만 반환한다. 근거가 없으면 `escalate=true`가 필수다.
- 하나의 Worker라도 계약 검증에 실패하면 해당 부서는 `DEGRADED`가 되고 다음
  게이트의 안전 동작은 `HOLD`다. 실패를 성공으로 간주하는 fallback은 없다.

## 분석·예측·결정 계약

`orchestration/contracts/mas.py`가 공통 Pydantic 계약의 단일 진입점이다.

- `AnalysisOutput` / `Signal`: 방향·신뢰도·근거를 구조화하며 분석 단계에서
  주문·비중 액션을 만들지 않는다.
- `PredictionOutput`: `T1`, `T5`, `T20` 각각 `up/down/side` 확률을 요구하고 합이
  `1 ± 0.001`인지 검증한다.
- `DecisionOutput`: `close`, `reduce_40`, `reduce_20`, `hold`, `increase_20`,
  `increase_40`, `increase_upper_limit`만 허용한다. 이 프로젝트에서는 이 값을
  주문으로 번역하지 않고 advisory decision으로만 보관한다.
- `resolve_signal_conflict`: 공시/이벤트/가격/매크로 신호를 명시적 가중치로
  합의하며, 근거 누락·낮은 신뢰도·약한 합의는 `HOLD_ON_WEAK_CONSENSUS`로
  떨어뜨린다.

## 관측성과 리플레이

각 파이프라인 결과는 `mas.pipeline-event.v1` 이벤트 목록을 가진다.

필수 추적 값은 `run_id`, `event_id`, stage/department/worker, input hash,
output contract, retry count, status, safe action, timestamp, payload hash다.
프로세스 로컬 BFF Projection도 이 이벤트를 보관하므로 실행 중에는 Worker
시작·완료와 부서 handoff를 관찰할 수 있다.

`mas.replay.v1`은 credential-free 입력·출력 hash와 replay scope를 남긴다.
TEST fixture는 계약/결정론적 replay 대상이며, Supabase·실시간 시장 데이터는
완전 replay 대상이라고 표시하지 않는다. 실제 시장 데이터를 연결할 때도
입력 snapshot, 데이터 시점(PIT), 모델/프롬프트 버전을 별도 이벤트로 추가해야
한다.

## 현재 실행 순서

기본 `PORTFOLIO_RECOMMENDATION` 요청은 `research → risk → qa → ceo`다.
트레이딩과 회계는 주문·원장 작업이 필요한 카테고리에서만 task plan에 포함된다.
모든 부서 노드는 그래프에 존재하지만, 선택되지 않은 노드를 Worker 실행으로
오인하지 않도록 `department_skipped`와 `SKIPPED_SAFE`를 남긴다.

현재 저장소의 실행은 TEST fixture 또는 read-only Supabase 입력에 한정된다.
Hermes 외부 Queue/Stream과 운영 Agent Status 저장소를 연결하기 전까지 BFF의
실행 상태는 운영 금융 상태의 Source of Truth가 아니다.

로컬에서 실제 Ollama Worker LLM을 연결해 검증할 때는
`PORTFOLIO_WORKER_RUNTIME=ollama`와 기존 `OLLAMA_BASE_URL`/
`OLLAMA_CHAT_MODEL` 설정을 사용한다. 기본값 `deterministic_test`는 네트워크
없는 계약·E2E 테스트용이며, 캐시 결과를 의미하지 않는다. Ollama 호출 실패는
Worker 계약 실패와 동일하게 `DEGRADED`/`HOLD`로 처리한다.
