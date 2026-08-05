# Baseline Audit

## 사용자 요청

각 부서끼리 연결되어 있는지, 직원이 LangGraph에 연결되어 있는지, 시작부터 결과까지 전체 파이프라인이 통과하는지, 대시보드를 고도화할 수 있는지 확인한다.

## 조사 결과

### 1. 부서 연결

코드 수준의 advisory 파이프라인은 연결되어 있다.

`validate_profile → Research → Trading → Risk precheck → Risk → QA precheck → QA → Accounting → CEO → finalize` 순서의 LangGraph가 있고, 각 부서는 `Send` fan-out과 fan-in barrier를 사용한다. 다만 CEO task plan이 요청 의도에 따라 부서를 선택하므로 모든 요청이 모든 부서를 호출하지 않는다. 기본 포트폴리오 추천 질의는 보통 Research·Risk·QA·CEO만 호출하고 Trading·Accounting은 SKIPPED가 정상이다. Quant와 HR은 이 추천 그래프의 stage 목록에 포함되지 않는다.

근거:

- `orchestration/workflows/portfolio_recommendation.py`
- `docs/02-engineering/DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md`
- `docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md`

### 2. 직원 LangGraph 연결

부서별 `WORKER_SPECS`와 공통 `departments/employee_worker_runtime.py`가 존재한다. 독립 Worker graph는 `tool → worker_llm → validate` 단계이며 비동기 실행 경계는 `app.ainvoke()`가 아니라 `app.ainvoke()`의 async 경로인 `await app.ainvoke(...)`로 구현되어 있다. 기본 모델 설정은 Profile상 `qwen3:1.7b`이고, Risk·QA는 별도 Worker graph와 도구·RAG 경계를 갖는다.

그러나 “등록된 직원 수”와 “이번 요청에서 실제 실행된 직원 수”는 다르다. 각 부서에 항상 실행되는 직원과 조건부 직원이 분리되어 있고, live 입력이 준비되지 않으면 전체 Worker가 안전하게 `SKIPPED_SAFE`/`HOLD`될 수 있다.

### 3. 시작부터 결과까지

로컬 BFF의 TEST catalog를 명시적으로 사용하면 프론트 요청 → BFF 202 응답 → process-local background task → async LangGraph → runtime event projection → `/ui/snapshot`/WebSocket → Kanban 결과 흐름이 있다. 실제 API에서 Governance mandate는 Risk·QA 동시 승인 대기 상태까지 확인되었다.

다만 이것은 “비구속적 추천 결과” 파이프라인이다. `binding: false`, `production_enabled: false`, `external_writes: false`이며 주문 제출, Fill, Ledger Posting, 공식 NAV 변경을 수행하지 않는다. 따라서 “전체 파이프라인을 거쳐 투자 결과물이 나온다”를 실제 주문·체결·원장까지의 의미로 해석하면 아직 완료되지 않았다.

현재 환경에서 관련 acceptance test는 6개 통과, 1개 실패했다. 실패는 BFF가 설정된 Supabase DSN을 사용하다 DNS 연결에 실패했을 때 TEST catalog로 전환하지 않고 `SUPABASE_UNAVAILABLE`로 안전 보류되어 `COMPLETED` 기대와 충돌한 경우다. 안전 방향의 동작이지만 실행 환경 계약과 테스트 기대가 어긋난 상태다.

추가로 `apps/api/portfolio_runtime.py`에는 `_event()`의 `return` 뒤에 도달하지 않는 예전 이벤트 처리 코드가 남아 있어, 연결 판정 자체를 바꾸지는 않지만 정리 대상이다.

### 4. 대시보드 고도화 가능성

가능하다. 현재 대시보드는 이미 BFF snapshot을 기준으로 runtime 상태, 부서, active worker, handoff, 메시지, 결과를 투영하고 WebSocket sequence gap 발생 시 REST snapshot으로 복구한다. 다음 고도화는 새 백엔드 권한을 만들지 않고 이 read model을 확장하는 방식이 가장 안전하다.

1. 실행 타임라인: run id, 현재 stage, 경과 시간, 마지막 heartbeat, 지연·실패 이유
2. 부서·직원 상세: 실행/조건부/스킵 구분, worker id, contract validation, attempts, evidence refs
3. Gate·승인 흐름: Risk/QA/User 승인과 HOLD 사유를 하나의 timeline으로 표시
4. 결과 근거: 추천 결과와 source/evidence/replay hash를 연결하고 advisory/non-binding을 명확히 표시
5. 연결 상태: BFF, runtime, event bridge, DB/Redis/Ollama, snapshot freshness를 별도 표시
6. 재현 화면: trace id와 pipeline event를 기준으로 replay/debug 화면 제공

## 확인이 필요한 사용자 결정

- 최우선 결과가 자문 결과인지, 실제 Paper 주문·Fill·Ledger까지의 폐쇄 루프인지
- 대시보드가 운영 관제 중심인지, 투자 결과·근거 중심인지
- 다음 구현 범위를 로컬 TEST 안정화로 시작할지, Postgres·Redis·실제 Worker runtime 통합까지 바로 확장할지

## Round 1 — 사용자 결정

- 최우선 결과: 투자 추천·근거와 Paper 주문·체결·원장 폐쇄 루프를 모두 단계적으로 추진
- 대시보드 우선순위: 직원·부서 실행 관제와 투자 결과·리스크·근거를 모두 제공
- 백엔드 통합 순서: 로컬 TEST 안정화 후 Postgres·Redis·Ollama 실제 연결로 확장

### 다음 확인 질문

1. Paper 폐쇄 루프의 완료 기준은 어디까지인가? `OrderIntent → Risk/QA Gate → Paper Order → Fill → Ledger/Position/NAV projection`까지를 기본 acceptance로 제안한다.
2. 대시보드의 주 사용자는 대표(결과·승인)와 운영자(실행·장애) 중 누구인가? 둘 다라면 대표 화면과 상세 운영 화면을 분리할지 확인이 필요하다.

## Round 2 — 사용자 결정

- Paper acceptance: `OrderIntent → Risk/QA Gate → Paper Order → Fill → Ledger/Position/NAV Projection`까지 검증
- 화면 구조: 대표용 Dashboard와 운영자용 Operations Console을 분리

### 구현 원칙

- 대표 Dashboard는 추천 결과, 리스크, 근거, 승인 대기와 최종 상태를 우선 표시한다.
- Operations Console은 run/trace, 부서 handoff, Worker, gate, event sequence, retry/error를 우선 표시한다.
- Paper Order도 Risk·QA 통과 전에는 제출하지 않고, 사용자 승인 없이는 제출하지 않는다.
- Paper 결과는 실제 금융 상태와 구분하고 `PAPER`·`NON-BINDING`을 항상 표시한다.

## Round 3 — 최종 결정

- Paper Order 제출은 대표의 명시적 승인 후에만 허용한다.

## Final acceptance scope

### Phase 1 — Local TEST / Paper

- 프론트에서 작업을 시작한다.
- 전체 advisory pipeline의 진행 이벤트와 handoff를 확인한다.
- Risk·QA Gate를 통과하지 못하면 Paper Order를 만들지 않는다.
- 대표 승인 전에는 Paper Order를 제출하지 않는다.
- 승인 후 Paper Broker의 Order·Fill과 Ledger/Position/NAV projection을 trace id로 연결한다.
- 실패·지연·재시작 시 `HOLD` 또는 `ERROR`로 남기고 자동 승인하지 않는다.

### Phase 2 — Runtime integration

- Postgres를 canonical persistence로 연결한다.
- Redis/Event bridge와 sequence/gap recovery를 연결한다.
- 실제 Ollama `qwen3:1.7b` Worker 실행을 검증한다.
- BFF process-local runtime을 영속·재시작 복구 가능한 runtime projection으로 교체한다.

### Dashboard split

- Representative Dashboard: 결과, 근거, Risk/QA 상태, 승인, Paper 최종 상태
- Operations Console: run/trace, department handoff, Worker, gate, events, latency, retry/error, connection health

## Round 4 — UI 및 초기 실행 요청

- 채팅 본문 글씨를 키우고, 고급 설정 필드를 읽기 쉬운 입력 그룹으로 정리한다.
- 화면의 초기값은 브라우저 로컬 초안/기본값과 Governance 백엔드의 현재 버전·상태를 구분해 표시한다.
- 분석 시작은 Mandate Governance 재제출과 분리한다. 승인 대기 중에도 주문·원장 변경이 없는 비구속 자문 분석은 시작할 수 있어야 한다.
- Governance 승인 대기 폴링에서 이미 진행 중인 Case를 반복 `advance`하지 않아야 한다. 반복 재개가 LangGraph `InvalidUpdateError`와 `governance_api_http_500`의 원인이다.

## Round 5 — 실행 오류 및 전체 부서 관제 요청

- `LangGraph 실행이 실패해 추천 결과를 확정하지 않았습니다.` 오류를 수정한다.
- 대표 Dashboard와 Operations Console의 부서·이벤트 표시가 서로 중복되지 않도록 중첩을 줄인다.
- Risk·QA 전용 직원 패널이 아니라 CEO Office, HR, Research, Trading, Risk, Quant/Backtest, Accounting/Portfolio, AI QA/Audit의 8개 부서를 한 화면에서 확인한다.

## Round 6 — 내부 통신 및 직원 추적 요청

- `portfolio_full_pipeline_risk_workers`의 `WORKER_SPECS` AttributeError를 수정한다.
- 부서 간 통신만이 아니라 같은 부서 안에서 부서장과 직원·직원 간 실행 흐름을 확인한다.
- 직원별로 현재 `일하는 중`, `대기`, `완료`, `오류/차단` 상태를 실제 BFF runtime event 기준으로 추적한다.

## Round 7 — LangSmith 관찰성 연결 요청

- 직원 추적 UI가 실제로 동작하는지 확인한다.
- 기존 실행 프로세스에 남은 `WORKER_SPECS` 오류를 해결하고 새 코드가 BFF에 반영되었는지 검증한다.
- 저장소 `.env`의 LangSmith 설정을 확인해 API 연결에 사용할 수 있는 안전한 상태 정보와 프론트 관제 표시를 추가한다.
- API Key 원문은 프론트·로그·문서에 노출하지 않는다.
