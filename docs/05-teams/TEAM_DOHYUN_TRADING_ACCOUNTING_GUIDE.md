# 도현님 담당 가이드: 트레이딩본부 + 회계/포트폴리오본부

> Override v2.0 · 기준일 2026-08-05
>
> This dated worker roster is a historical team snapshot. Current worker IDs, counts, and authority boundaries are defined by the department registries and [CURRENT_PROJECT_ARCHITECTURE.md](../CURRENT_PROJECT_ARCHITECTURE.md).
> 이 문서는 이전 Trading/Accounting 팀 가이드의 운영 기준을 덮어쓴다. Trading의 Paper 구현과 Accounting의 API 주입 Fill 검증을 Production E2E로 해석하지 않는다. 최상위 기준은 [HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md), [PROJECT_IMPLEMENTATION_STATUS.md](../PROJECT_IMPLEMENTATION_STATUS.md), [UNIFIED_DOMAIN_API_SPEC.md](../02-engineering/UNIFIED_DOMAIN_API_SPEC.md)다.

## 0. 상태 판정 규칙

| 상태 | 의미 | 이 팀에서의 대표 예 |
|---|---|---|
| `DOCUMENTED` | 계약과 완료 조건만 있음 | `PLAT-01`, `PLAT-02`, `UI-02` |
| `IMPLEMENTED` | 코드·계약·자체 검증이 있음 | Trading D0-D2, `TRD-01` baseline |
| `TEST_VERIFIED` | 현재 Commit의 결정론적 테스트 통과 | 계약·OMS·Paper Broker 불변식 |
| `RUNTIME_VERIFIED` | 실제 API·DB·Event·재시작을 확인 | API 주입 Fill 기반 `ACC-01` snapshot |
| `BLOCKED` | 선행 서비스·Credential·데이터·보안 조건 미충족 | Production Submit, 공식 NAV |

Paper Fixture나 Frontend Demo는 실제 Broker·공식 원장·운영 NAV를 대신하지 않는다.

## 0.1 현재 직원 구성과 역할

구조조정 이후 이 팀의 실행 직원은 Trading 3명, Accounting/Portfolio 2명이다. 아래 표의 LLM Worker는
비바인딩 Context와 예외 설명을 만들고, 결정론 Runner는 계약·수치·상태를 계산하거나 조회한다.

| 부서 | Worker | 방식 | 현재 역할 | 권한 경계 |
|---|---|---|---|---|
| Trading | `bull-thesis-worker` | LLM | Research Packet 근거만 사용해 Bull thesis, 촉매와 기대수익 가설 작성 | 주문·수량 확정 금지, Bear 출력 미참조 |
| Trading | `bear-thesis-worker` | LLM | Research Packet 근거만 사용해 Bear thesis, 반증과 하락 위험 작성 | 주문·수량 확정 금지, Bull 출력 미참조 |
| Trading | `desk-runner` | 결정론 | Intent Builder, 계약 상태 전이, 실행 가능성·Venue Cost·파생 Certification 처리 | Risk 승인 대체·Broker Submit 금지 |
| Accounting/Portfolio | `exception-investigation-worker` | LLM | Reconciliation Break, 미설명 PnL, 마감 준비 예외의 원인 후보 조사와 근거 연결 | 수치 계산·수정, Break 종결, Official NAV 확정 금지 |
| Accounting/Portfolio | `back-office-runner` | 결정론 | Position·Cash·PnL·Reporting·Valuation·Corporate Action·Fee/Tax 결과 조회·투영 | LLM 호출, 공식 수치 임의 작성·수정 금지 |

기존 `trader-pm-agent`, `execution-agent`, `portfolio-controller`, `reconciliation-agent` 등은 현재 추가 실행
직원이 아니라 Profile·DB·감사 추적용 호환 Alias 또는 결정론 Domain 기능이다. 실제 Worker 수와 trigger는 각 부서
`hermes/config.yaml`, `employee_workers.py`, [WORKER_ROLE_BOUNDARIES.md](../02-engineering/WORKER_ROLE_BOUNDARIES.md)를 따른다.

## 1. 책임과 절대 경계

### 트레이딩본부

- Research Packet과 Mandate를 바탕으로 `OrderIntent`를 제안한다.
- Risk 승인 없는 Submit을 만들지 않으며, `trader-pm-agent`는 Broker에 직접 주문하지 않는다.
- OMS가 Intent 상태와 Broker Order 상태를 분리하고, 모호한 Broker 응답은 `BROKER_STATE_AMBIGUOUS`/`FAILED_SAFE`로 보낸다.
- Paper Broker는 테스트 전용이다. 실제 Broker Credential·Live Order는 이 가이드의 범위가 아니다.

### 회계/포트폴리오본부

- 승인된 Fill만 Journal, Position, Cash, PnL, NAV Projection으로 반영한다.
- Posted Journal은 수정하지 않고 Reversal로 정정한다.
- UI·CEO·Risk·QA가 Ledger/NAV를 직접 수정하지 못하게 한다.
- 공식 NAV와 Live Portfolio 상태를 계산하려면 승인된 Mark·FX·Corporate Action·Fee/Tax source가 필요하다.

### 공통 금지

- OrderIntent, Order, Fill, Journal, Position을 하나의 객체나 하나의 승인으로 합치지 않는다.
- API body 문자열을 신뢰해 Risk 승인이나 회계 Posting을 우회하지 않는다.
- Frontend가 Supabase Service Role, Broker Credential, Risk 계산, Ledger Posting을 소유하지 않는다.
- `DEMO`와 `PAPER`를 `LIVE`로 표현하지 않는다.

## 2. 현재 기준선

| 항목 | 현재 판정 | 남은 조건 |
|---|---|---|
| OrderIntent 계약·Decimal·DQ Gate | `IMPLEMENTED` + `TEST_VERIFIED` | 실제 Research Packet과 Gate hash 연결 |
| Risk 승인 없는 Submit 차단 | `IMPLEMENTED` | Risk API·OMS·DB의 실제 E2E 필요 |
| Paper Broker·중복 Fill 방지 | `TEST_VERIFIED` | Redis/Event 재시작 복구와 실제 `execution.fills` 경로 필요 |
| Multi-leg·Derivatives Gate | `IMPLEMENTED` + `TEST_VERIFIED` | 실제 실행 Plan/Child Order와 Broker Certification 필요 |
| 상태 머신 분리 | `IMPLEMENTED` 진행 중 | `CREATED/CANCEL_PENDING/BROKER_STATE_AMBIGUOUS`의 DB migration·Replay 필요 |
| `TRD-01` | `IMPLEMENTED` | 현재 Runtime·DB·Event hash 완료 조건 미충족 |
| `ACC-01` Fill→Journal→Position | `RUNTIME_VERIFIED` snapshot | 체결 원천이 API 주입이며 `execution.fills` Consumer 연결이 남음 |
| D3 Valuation/PnL/NAV | 계산 `IMPLEMENTED` | `market-api` Mark·FX 공급원 연결, 공식 NAV 승인 조건 필요 |
| Long/Short·Borrow·Financing | 진행 중 | Position/계정 모델과 비용 rule 확정 필요 |
| Strategy allocation·Attribution | 진행 중/미구현 | CEO governance allocation과 Journal lineage 연결 |
| Performance Fee/HWM | 미착수 | Mandate·Fee Policy 승인 전 계산 금지 |
| `PLAT-01/02/03` | 문서/부분 구현 | Event Envelope·Compose Core·Outbox/Relay 공통화 필요 |
| `UI-01` | `IMPLEMENTED` baseline | 공식 Snapshot/WebSocket과 Gap 복구 E2E 필요 |
| `UI-02` | `DOCUMENTED` | `agent.status.v1` Bridge·Projector·BFF 연결 필요 |
| `UI-03` | `BLOCKED` | Frontend High 취약점과 clean build 회귀 해결 |

특히 `ACC-01`은 2026-08-04 실 Supabase 왕복 증거가 있으나, 체결 원천이 `execution.fills`가 아니라 API 주입이라는 제한을 반드시 함께 기록한다.

## 3. Override 작업 순서

### P0-1. Canonical Paper E2E

**담당:** 도현. **선행:** 재일 `RQ-01`, 동규 `RSK-01/QA-01`, 영주 `GOV-01`.

다음 순서의 단일 Fixture를 만든다.

`ResearchPacket → Strategy/Signal → OrderIntent → Risk Decision → OMS Submit → Broker Order → Fill → Journal → Position/Cash → PnL/NAV → QA Trace`

- 모든 단계가 `case_id`, `trace_id`, `event_id`, `event_type`, `schema_version`, `occurred_at`, `input_hash`를 보존한다.
- Risk 승인 전에는 `execution.order_intents`가 Submit으로 넘어가지 않는다.
- Fill은 실제 Event/Consumer 경로에서 생성한다. 테스트 API가 DB에 Fill을 직접 넣는 방식은 별도 unit fixture로만 표시한다.
- 재실행 시 Order, Fill, Journal, Projection이 중복 생성되지 않아야 한다.
- 중간 장애·timeout·broker ambiguous 상태는 확대가 아니라 `HOLD`, `FAILED_SAFE`, `RECONCILIATION_REQUIRED`로 끝난다.

**완료 증거:** 같은 `trace_id`로 Canonical DB row와 Redis Event를 재조회하고, 재시작 전후 상태·hash·수량·금액이 일치한다.

### P0-2. 상태 머신·Persistence·Outbox

- Intent 상태와 Broker Order 상태를 별도 enum·transition table로 확정한다.
- `CANCEL_PENDING`, `BROKER_STATE_AMBIGUOUS`, 부분 체결, 초과 체결, duplicate broker event를 모두 테스트한다.
- Supabase Migration을 canonical schema와 맞추고 Alembic은 임의의 두 번째 권위가 되지 않게 한다.
- Transactional Outbox와 Relay, consumer idempotency를 `PLAT-03` 계약에 맞춰 구현한다.
- DB connection loss·process restart·Redis reconnect를 포함한 복구 Test를 만든다.

### P0-3. Accounting 공식 경계

- `execution.fills`에서 Ledger Consumer가 Journal을 만들고, Journal에서 Position/Cash를 재구축한다.
- `market-api`의 승인된 Mark·FX·DQ·as_of를 받아 Preliminary NAV를 계산한다.
- Mark 하나라도 stale/missing이면 부분 NAV를 만들지 않고 `BLOCKED`/`INCONCLUSIVE`로 끝낸다.
- `is_official`은 Mandate·Governance 승인 없이 `true`가 되지 않는다.
- Long/Short, Borrow/Financing, Fee/Tax, Corporate Action의 계정과 Rule Version을 명시한다.

### P1-1. Reconciliation·Attribution

- Internal Position/Cash와 External Statement를 동일 cutoff·source hash로 대조한다.
- Break의 severity·owner·due date·resolution evidence를 저장하고 자동 해소하지 않는다.
- Journal 또는 OrderIntent와 연결되는 `strategy_version_id` lineage를 확정한 뒤 PnL attribution을 계산한다.
- Performance Fee/HWM는 Mandate·Fee Policy가 승인되기 전에는 계산기를 활성화하지 않는다.

### P1-2. UI와 Kanban Projection

- `ai-office`와 `apps/api`는 Read-only Projection만 제공한다.
- `DEMO/PAPER/LIVE`, 연결 상태, 마지막 갱신 시각을 명확히 표시하고 Scripted Data를 실시간 금융 상태처럼 표시하지 않는다.
- ADR-0001의 `agent.status.v1` Bridge·Projector·BFF/WebSocket을 구현하되, Kanban 상태가 Risk·OMS·Ledger를 변경하지 못하게 한다.
- Frontend dependency High 취약점, clean install/build/test, secret leakage를 별도 Gate로 처리한다.

## 4. 인계 계약

| 상대 팀 | 도현이 받아야 하는 것 | 없으면 |
|---|---|---|
| 재일 | Packet ID, strategy version, PIT cutoff, source hash | Intent 생성 금지, `HOLD` |
| 영주 | 활성 Mandate/Allocation/Approval/expiry | Intent Submit 금지 |
| 동규 | Risk Decision ID/hash와 허용 범위 | OMS Submit 금지 |
| 동규 QA | QA trace 요구사항·Replay key | Fill/Journal Release 금지 |
| 도현 → 회계 | 실제 Fill event와 fee/FX/mark source | Journal Posting 금지 |

## 5. 검증

```bash
python departments/02-trading/contracts/contracts.py
python departments/02-trading/oms/oms.py
python departments/02-trading/broker/paper_broker.py
python departments/05-accounting-portfolio/ledger/ledger.py
python departments/05-accounting-portfolio/reconciliation/reconciliation.py
docker compose config --quiet
```

통합 검증 보고서에는 반드시 `trace_id`, OrderIntent ID, Risk Decision ID, Broker Order ID, Fill ID, Journal ID, Position/Cash snapshot ID, 재실행 결과, DB/Event hash를 남긴다. Broker·DB Credential은 로그에 출력하지 않는다.

## 6. 최종 Release Gate

- [ ] Research Packet부터 QA Trace까지 하나의 trace로 Replay
- [ ] Risk 승인 전 Submit 0건, Risk 결과와 OMS 상태 hash 일치
- [ ] 실제 `execution.fills` Event → Ledger Consumer → Journal/Position/Cash 연결
- [ ] 재시작·중복 Event·ambiguous Broker 상태 복구
- [ ] Mark/FX/DQ 기반 Preliminary NAV와 Official NAV 승인 분리
- [ ] RLS·Service Identity·Credential이 UI/CEO/Agent에 노출되지 않음
- [ ] `PLAT-01/02/03`, `TRD-01`, `ACC-01`, `UI-02`, `UI-03` 증거를 현재 Commit에서 재현

하나라도 미충족이면 Live·Official NAV·운영 Submit을 승인하지 않는다.
