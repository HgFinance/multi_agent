# CEO Hermes 아키텍처·라우팅·운영 정본

검토일: 2026-08-26 (UTC)
작성: CEO Office
상태: **현재 실행 경로의 정본**

이 문서는 CEO 관련 아키텍처 문서를 하나로 합친 기준 문서다. 일반 CEO 응답,
부서 역할 위임, 결정론 러너, QA 사후 감사, LangSmith·Notion 운영 연결을 함께
정의한다. 제품 전체의 최상위 기준은 여전히
[HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md)이며, 이 문서는 CEO
실행 경계 안에서만 그 기준을 구체화한다.

## 1. 결론부터

CEO의 일반 응답 경로는 다음 순서다.

```text
사용자(Web/Discord)
    ↓
portfolio-bff canonical ingress
    ↓
CEO root Kanban task + deterministic scope/plan
    ↓
필요한 primary 부서 Worker fan-out/fan-in
    ↓
CEO Hermes/CEO synthesis
    ↓
응답 저장·Discord/Web 전달
    ↓  (응답 전달 후에만)
QA post-response audit (비동기, 동일 입력 + 동일 CEO 응답)
```

단, BFF 중앙 라우터가 전략 생성 의도를 먼저 판정한다. 전략 생성·백테스트 질의는
CEO root나 Kanban을 만들지 않고 `autonomous-research-request.v1`로 독립 연구실에
등록된다. 아래 흐름은 일반 CEO 질의에 대한 정본이다.

QA는 일반 CEO 응답의 선행 단계가 아니다. QA audit은 CEO가 받은 동일한 root
입력, primary handoff, CEO synthesis 입력과 CEO 응답을 전달받아 `audit.eval_runs`
에 기록한다. QA 결과는 이미 전달된 응답을 지연·차단·재작성하지 않는다.

다음 두 종류의 별도 거버넌스 흐름은 예외적으로 QA→CEO 승인 순서를 유지한다.

- 전략 Production/Shadow/Paper 승격 심사
- HR Agent Profile·권한 lifecycle 승인

이것은 일반 대화 응답이 아니라 상태 변경·승격 권한을 다루는 별도 workflow다.
주문·Risk 승인·원장 변경·NAV 확정은 CEO 권한이 아니며, 주문 후보의 선행 안전성은
결정론적 Risk Engine과 OMS admission이 소유한다.

## 2. 책임 계층

| 계층 | 구현 | 책임 | 하지 않는 일 |
|---|---|---|---|
| CEO Head | `ceo-hermes` 공식 Hermes Profile | 대화·거버넌스 절차·위원회 소집·설명 | 주문 제출, Risk 승인, 원장/NAV 수정 |
| CEO Worker | `executive-briefing-worker` 독립 LangGraph 1명 | primary 보고서의 종합·서술·구조화 handoff | 금융 상태 확정, QA 선행 게이트 |
| CEO runner | `ceo-runner` 결정론 Python | 이미 존재하는 artifact와 blocker/missing 입력의 조회·이동 | 새 판정·LLM 호출·서술 생성 |
| CEO supervisor | `ceo-kanban-supervisor` | Kanban terminal event 감시, synthesis materialization, 전달, 사후 QA task 생성 | QA 결과로 일반 응답을 되돌리기 |
| QA audit | `qa-hermes` + `QaAuditProjection` | 응답 후 근거·인용·범위·재현성 평가 | CEO 응답 전에 개입하기 |

CEO Worker의 모델 실행과 CEO Head의 Hermes 실행은 서로 다른 계층이다. Profile의
Head provider/auth와 직원의 LangGraph Worker Model을 같은 것으로 세지 않는다.

## 3. 일반 CEO 응답 파이프라인

### 3.1 Ingress와 root

Web과 Discord는 모두 `portfolio-bff`의 canonical ingress로 들어온다.

- 일반 CEO 질의의 `POST /ui/ceo/ask`와 `POST /ui/ceo/ingress`는 같은 root Kanban·권한·멱등 경계를 쓴다.
- 전략 생성 의도는 두 진입점에서 중앙 분기되어 `POST /ui/strategy-research/ask`와
  동일한 파일 기반 intake를 사용하며 CEO root·Kanban·주문 경계에 들어가지 않는다.
- Discord 원문은 `source=discord`와 메시지 좌표를 가진 metadata로만 전달된다.
- `ceo-hermes`가 BFF를 거치지 않고 별도 질문·주문을 만들 수 있는 fallback은 없다.
- `ceo-hermes`는 `portfolio-bff: /health/ready`가 healthy일 때만 초기 gateway admission을
  시작한다. 런타임 BFF 장애는 `gateway_patch.py`의 fail-closed로 처리한다.
- Root body는 immutable user input/mandate snapshot이다. LLM이 권한이나 주문 내용을
  보충하지 않는다.

근거 구현:

- [apps/api/ceo.py](../../apps/api/ceo.py)
- [orchestration/ceo_workflow_scope.py](../../orchestration/ceo_workflow_scope.py)
- [departments/00-ceo-office/compose.yaml](../../departments/00-ceo-office/compose.yaml)
- [deploy/hermes-discord/gateway_patch.py](../../deploy/hermes-discord/gateway_patch.py)

### 3.2 라우팅

라우터는 먼저 workflow 소유권을 정하고, 그 다음 해당 workflow 안의 부서를
결정한다. 기본 경로는 결정론이며 CEO Hermes 기반 planner는 명시적 opt-in일 때만
사용한다. LLM planner가 실패하면 결정론 fallback으로 돌아간다.

| 요청 카테고리 | response-plane primary | 후속 처리 |
|---|---|---|
| `MARKET_RESEARCH` | research → ceo | QA audit은 CEO 응답 후 |
| `RISK_REVIEW` | research → risk → ceo | QA audit은 CEO 응답 후 |
| `TAX_LIQUIDITY` | research → risk → accounting → ceo | QA audit은 CEO 응답 후 |
| `PORTFOLIO_RECOMMENDATION` | research → quant → risk → ceo | QA audit은 CEO 응답 후 |
| `REBALANCING_PROPOSAL` | research → quant → trading → risk → accounting → ceo | QA audit은 CEO 응답 후 |
| `STRATEGY_PROPOSAL` | research → quant → ceo | 정식 승격은 별도 governance workflow |
| 자연어 전략 생성/백테스트 | autonomous research lab | `labs/<request_id>/`에서 Hermes가 실험·검증·계보를 반복; 후보만 출력 |

`qa`는 일반 `requested_departments`의 response-plane primary로 materialize하지
않는다. 계획과 root metadata에 감사 의도는 남기지만, QA task는 CEO response task가
완료되고 전달된 뒤 supervisor가 생성한다.

구조화된 category가 알 수 없으면 조용히 버리지 않고 `category_recognized=false`와
bounded fallback을 남긴다. 자유 질의 keyword는 부서 집합을 추가할 수 있지만
권한을 줄이지 않는다.

### 3.3 부서 handoff와 CEO synthesis

각 primary 부서는 독립 LangGraph Worker fan-out/fan-in과 계약 검증을 거친다.
CEO synthesis는 terminal primary handoff가 준비된 뒤에만 만들어진다. Worker 출력은
`DepartmentHandoff`·workflow artifact 계약으로 전달하며 자유 문자열만으로 다음
단계를 결정하지 않는다.

CEO synthesis가 수행하는 일은 다음과 같다.

- primary 결과와 deterministic runner facts를 하나의 advisory 설명으로 종합
- 결과의 누락·차단·불확실성을 숨기지 않음
- 사용자에게 전달할 최종 응답과 bounded metadata 생성

CEO가 수행하지 않는 일은 다음과 같다.

- `OrderIntent`를 `Order`로 승격
- Risk/Compliance veto 우회
- Ledger Posting, NAV 확정, Profile ACTIVE 전환
- QA가 아직 끝나지 않았다는 이유로 일반 응답을 보류

### 3.4 응답 후 QA audit

CEO synthesis terminal event에서 supervisor는 먼저 응답을 저장·전달한다. 전달이
확인된 뒤에만 다음 marker를 가진 QA child를 한 번 생성한다.

```text
qa_phase=post_response
qa_timing=after_ceo_response
response_delivered=true
ceo_input_is_identical=true
qa_blocks_response=false
evaluation_sink=audit.eval_runs
feedback_consumer=hr-department
```

QA task의 parent는 CEO response task다. QA body에는 다음을 그대로 포함한다.

- root user input
- CEO synthesis에 전달된 primary handoff
- CEO synthesis input
- CEO final response

생성은 stable idempotency key로 보호한다. QA·Notion·Discord 관찰자가 실패하거나
느려져도 응답은 이미 완료된 상태이며, 관찰자 실패는 응답 상태를 바꾸지 않는다.

## 4. 역할 위임과 결정론 러너

### 4.1 CEO Worker

현재 CEO LLM Worker는 `executive-briefing-worker` 한 명이다. 원본 로그나 여러
부서의 raw DB를 직접 탐색하지 않고, primary가 만든 구조화 report/artifact를 읽어
`ceo.worker-context.v1` advisory context를 만든다. 창작·요약은 LLM이 담당하지만
결과는 binding financial state가 아니며 하류 안전 경계를 우회하지 않는다.

### 4.2 `ceo-runner`

`ceo-runner`는 `research_packet`, `order_intent`, `risk_decision`,
`qa_assessment`, `accounting_snapshot`, `strategy_report`를 payload의 정해진
artifact 위치에서 읽는다. 이미 다른 부서가 만든 verdict를 이동하고 다음 사실만
계산한다.

- 존재하지 않는 artifact → `missing_inputs`
- Risk가 명시적으로 승인하지 않음 → `blockers`
- 입력의 expiry가 실제로 전달된 경우에만 expiry를 확인
- `decided_by=deterministic`, `authoritative=false`

일반 CEO 응답에서 사후 QA 결과는 blocker가 아니다. 전략 승격·HR lifecycle 같은
별도 governance가 QA gate를 필요로 하면 그 workflow의 결정론 gate가 소유한다.
runner는 `WORKER_SPECS` LLM registry 밖에서 직접 실행해 LLM 없음이 코드로
강제된다.

### 4.3 HR Shared Service

HR은 투자 본부의 일곱 번째 부서가 아니다. CEO 직속 Shared Service다.

- `profile-architecture-worker`는 후보 Profile 개선 제안만 만든다.
- 권한·Profile 상태 전환은 결정론 API와 IAM이 담당한다.
- 독립 QA 평가와 CEO 승인이 없으면 ACTIVE 전환하지 않는다.
- HR이 자기 후보의 QA·CEO evidence를 직접 만들 수 없다.

세부 HR 운영 절차는 [TEAM_YOUNGJU_CEO_HR_GUIDE.md](../05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md)를
따른다.

## 5. 관측성·외부 연동

### 5.1 LangSmith

현재 연결 상태는 **BFF root lifecycle, supervisor terminal close, LangGraph Worker
trace, QA/metrics reader가 연결된 상태**다.

- workflow project: `LANGSMITH_PROJECT` 기본 `First`
- metrics project: `HgFinance-Metrics`
- evals project: `HgFinance-Evals`
- root는 `start_root_trace()`에서 생성되고 terminal 시 `close_root_trace()`로 갱신
- Worker graph는 `worker_graph_trace_config()`를 `invoke/ainvoke`에 직접 전달
- input/output 원문은 숨기며 correlation id·stage·status·latency만 metadata로 보낸다.
- LangSmith 오류는 workflow를 실패시키지 않는 fail-open observer다. 동일 root의
  duplicate PATCH는 provider가 이미 받은 종료 갱신의 재시도로 간주해 idempotent
  success로 처리한다.
- `langsmith_queries.py`는 retired v1 run query가 아니라 project id를 확인한 뒤
  `/api/v2/runs/query` 경계를 사용한다.

실행 중인 컨테이너 점검 결과:

- `portfolio-bff`: LangSmith credential/config가 설정됨
- `ceo-kanban-supervisor`: LangSmith credential/config가 설정됨
- supervisor 컨테이너에서 LangSmith project/session API가 HTTP 200으로 응답함
- `/api/v2/runs/query`는 인증 실패가 아닌 payload validation HTTP 400을 반환해 endpoint
  도달·인증은 확인됨

남은 주의점은 `ceo-hermes` 공식 프로세스 자체다. 현재 CEO Hermes 컨테이너에는
LangSmith 환경변수가 없고, 공식 Hermes 내부 model turn을
`orchestration.llm_observability`가 직접 계측하지 않는다. 따라서 현재 보장되는 것은
BFF→Kanban→supervisor root lifecycle과 LangGraph Worker trace이며, **공식 Hermes
Head의 native turn까지 end-to-end trace된다고 말하면 안 된다.** 이를 연결하려면
공식 Hermes가 지원하는 callback/runtime 계측 방식을 확인한 별도 작업이 필요하다.

### 5.2 Notion

Notion은 CEO 응답의 저장소가 아니라 비동기 Projection이다.

- CEO synthesis: `CeoNotionProjection`
- 부서 terminal 결과: `DepartmentNotionProjection`
- QA 결과: `QaAuditProjection` → canonical `audit.eval_runs`
- 담당 프로세스: `ceo-kanban-supervisor`
- schema read/cache, idempotency, mismatch 재조회, 429/5xx retryable 결과를 지원
- 페이지 생성·갱신 실패가 CEO 응답을 취소하거나 다시 쓰지 않는다.

실행 중인 supervisor에서 CEO Notion DB schema API가 HTTP 200으로 응답해 token·DB
권한·네트워크 연결은 확인됐다. Notion projection 로그에도 terminal 결과가
`created`로 기록됐다.

Notion의 위치는 의도적으로 supervisor다. CEO Hermes에 Notion credential을 넣어
Head가 임의로 페이지를 만들도록 하지 않는다. Notion sync가 필요해도 canonical
Kanban/audit 상태를 먼저 바꾸지 않는다.

### 5.3 Discord/Web

Discord gateway는 BFF ingress만 호출하고, CEO 최종 답변과 부서 진행 projection은
기존 thread/message correlation을 사용한다. mirror event의 QA는 `evaluation`
lane이며 `CEO_FINAL`을 block하지 않는다. `failed_closed`는 사용자 mirror와 분리된
operations alert sink로 비동기 알림을 보낼 수 있다.

운영·migration 절차는 다음 문서가 소유한다.

- [DISCORD_WEB_CEO_MIRRORING.md](DISCORD_WEB_CEO_MIRRORING.md)
- [HERMES_DOCKER_RUNBOOK.md](HERMES_DOCKER_RUNBOOK.md)
- [HERMES_DISCORD_DOCKER_ONLY_MIGRATION.md](HERMES_DISCORD_DOCKER_ONLY_MIGRATION.md)

## 6. 병목 점검

### 6.1 현재 확인된 상태

response-plane을 QA·Notion이 선행해서 막는 구조는 제거됐다. supervisor의
`TerminalObserverQueue`는 Discord/Notion projection을 별도 bounded worker(기본 2개,
pending 128개)로 넘기며, root lock을 잡은 상태에서 외부 I/O를 수행하지 않는다.
실행 로그에서도 event queue wait가 대체로 0~4ms였고, Notion observer가 1~10초
걸린 사례가 있어도 observer lane에서 끝났다.

### 6.2 남은 병목과 영향

| 지점 | 현재 기본값/실측 | 영향 | 판정 |
|---|---:|---|---|
| primary fan-in | 카테고리별 1~6개 | 필요한 부서 결과를 모두 기다림 | 의도된 정확성 비용 |
| CEO synthesis | 실행 로그 약 2.5~2.8초 | CEO 문장 생성 지연 | 일반적인 Head/LLM 지연 |
| Kanban event poll | supervisor 1초, SQLite cursor | 최대 약 1초 감시 지연 | 허용 가능 |
| supervisor event worker | 기본 2개 | 외부 observer와 분리됨 | 현재 큐 대기는 낮음 |
| Notion/Discord observer | 실측 1~10초 | 저장/미러 지연 | 응답을 막지 않음 |
| 전체 workflow deadline | 1200초 | 멈춘 workflow가 오래 남을 수 있음 | recovery/timeout 모니터링 필요 |
| dispatcher | 기본 1초 tick, board cap 별도 | shared Kanban 포화 시 ready 대기 | 가장 큰 용량 병목 후보 |

즉 지금의 핵심 병목은 QA가 아니다. 여러 primary 결과를 기다리는 fan-in, Hermes
dispatcher의 shared board 슬롯, CEO Head의 실제 모델 지연이 response-plane의
주요 비용이다. factory 작업이 사용자 board 슬롯을 잠식하지 않도록 board 분리와
`max_in_progress` cap을 유지해야 한다.

### 6.3 운영 모니터링 기준

다음 로그·지표를 알림 대상으로 삼는다.

- `supervisor-event-loop-timing.queue_wait_ms` p95
- `supervisor-observer-queue-full`
- `supervisor-synthesis-timing.t0_t8_ms`
- workflow deadline/recovery 횟수
- `langsmith-root-close-unconfirmed`
- `failed_closed` ingress 횟수
- Notion `failed`·429·schema mismatch 횟수

`langsmith-root-close-unconfirmed`는 사용자 응답 실패가 아니지만 관측 누락이다.
특히 같은 LangSmith root에 동시 종료 갱신이 들어가면 provider가 duplicate update를
거부할 수 있으므로 root close는 idempotent/concurrency-safe하게 보강해야 한다.

## 7. 아직 구현하지 않은 범위

아래는 이 정본이 완료됐다고 주장하지 않는다.

1. `STRATEGY_PROPOSAL`을 BFF에서 실제 `strategy-research` graph로 분기하는 작업
2. 전략을 사용자의 포트폴리오에 편입하는 `USER` 승인 workflow
3. 상시 대화의 `conversation_id` 기반 문맥 저장
4. 공식 CEO Hermes Head native turn의 LangSmith callback 계측
5. provider별 LangSmith root close readback/재시도 정책의 추가 보강
6. CEO의 PM Pod/Book 비교·capital efficiency 원천 데이터와 계산 모듈

미구현 항목은 빈 fake code로 채우지 않고 해당 workflow의 상태와 backlog로 남긴다.

## 8. 코드·문서 검증

```bash
python departments/00-ceo-office/employee_workers.py
python -m pytest -q tests/orchestration/test_ceo_task_planner.py \
  tests/orchestration/test_qa_contract.py \
  tests/orchestration/test_ceo_supervisor.py
docker compose config --services
docker compose ps ceo-hermes ceo-kanban-supervisor portfolio-bff
```

문서 간 topology가 충돌하면 이 문서와
[MAS_PIPELINE_CONTRACTS.md](MAS_PIPELINE_CONTRACTS.md), 루트 `CLAUDE.md`/`AGENTS.md`의
현재 response-plane 정의를 우선한다. 과거 상태를 보존하는
[AS_IS_PIPELINE_BLUEPRINT.md](AS_IS_PIPELINE_BLUEPRINT.md)는 `HISTORICAL SNAPSHOT`이며
현재 실행 설계의 근거가 아니다.
