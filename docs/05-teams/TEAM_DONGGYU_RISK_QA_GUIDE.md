# 동규님 담당 가이드: 리스크본부 + AI QA/감사본부

> Override v2.0 · 기준일 2026-08-05
>
> 이 문서는 이전 Risk/QA 팀 가이드의 운영 기준을 덮어쓴다. 설계 완료, 코드 구현, 테스트 통과, 실제 Runtime 검증, 운영 승인을 같은 의미로 취급하지 않는다. 최상위 기준은 [HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md)와 [PROJECT_IMPLEMENTATION_STATUS.md](../PROJECT_IMPLEMENTATION_STATUS.md)다.

## 0. 상태 판정 규칙

| 상태 | 의미 | 금지되는 해석 |
|---|---|---|
| `DOCUMENTED` | 계약·설계·완료 조건만 정리됨 | 구현 완료로 표현하지 않음 |
| `IMPLEMENTED` | 코드와 계약이 존재함 | 다른 서비스와 통합됐다고 표현하지 않음 |
| `TEST_VERIFIED` | 현재 코드에서 결정론적 테스트가 통과함 | 운영 권한·외부 시스템 연결 완료로 표현하지 않음 |
| `RUNTIME_VERIFIED` | 실제 API·DB·Redis 입출력과 복구/멱등 조건을 확인함 | Production 승인으로 표현하지 않음 |
| `BLOCKED` | 선행 계약·Credential·데이터·권한이 없어 안전하게 진행할 수 없음 | 실패를 성공으로 fallback하지 않음 |

가이드의 체크박스는 “담당자의 산출물과 증거가 존재한다”는 뜻이다. 전체 제품 완료는 각 팀 체크박스의 합이 아니라 공통 Replay Gate를 통과해야 한다.

## 1. 책임과 절대 경계

### 리스크본부

- 결정론적 Risk Engine이 한도·PIT·staleness·유동성·집중도·kill switch를 판정한다.
- Risk Agent는 근거와 권고만 만들며 주문을 직접 제출하거나 Risk Engine의 결과를 덮어쓰지 않는다.
- Risk Trading State 변경·삭제는 서명된 Service Token과 명시적 scope를 요구한다.

### AI QA/감사본부

- Claim–Evidence–Trace–Decision–Finding을 독립적으로 검증하고, unsupported claim·허용되지 않은 Tool·재현 불가 결과를 `REJECT`, `ESCALATE`, `HOLD`로 보낸다.
- QA가 Risk 결과를 수정하거나 자기 Finding을 자기 승인·종결하지 않는다.
- `qa-check`는 운영 기본값이 DENY이며 `RISK_QA_RUNTIME=test`를 명시한 테스트에서만 테스트용 승인 경로를 연다.

### 공통 금지

- CEO·Trading·Accounting의 권한을 Risk/QA가 대신 수행하지 않는다.
- 문자열 `set_by`, `verifier`, `X-Auth-Subject`만으로 운영 권한을 인정하지 않는다.
- 실패 시 승인·승격·권한부여 방향의 fallback을 만들지 않는다.
- 실제 정책 Corpus가 `SAMPLE_PLACEHOLDER`인 상태에서 운영 Compliance 결정을 내리지 않는다.

## 2. 현재 기준선

| 항목 | 현재 판정 | 남은 조건 |
|---|---|---|
| Worker Scope | `TEST_VERIFIED` | 명시적 allow-list가 없으면 `SCOPE_DENIED`/`ESCALATE` 유지 |
| QA runtime gate | `TEST_VERIFIED` | Production 승인 계약과 실제 정책 Corpus 필요 |
| Risk/QA 명령 인증 | `IMPLEMENTED` + `TEST_VERIFIED` | Secret Manager 주입, Issuer/JWKS 또는 mTLS/IAM 매핑, 양성 Runtime 검증 필요 |
| Risk 결정론적 Gate | `TEST_VERIFIED` | Trading의 실제 OrderIntent와 DB/Event hash 연결 필요 |
| QA Evidence/Trace/Incident | `TEST_VERIFIED` | 실제 Case Replay와 운영 `agent_runs`/`tool_calls` 영속화 필요 |
| 공통 Replay 계약 | `TEST_VERIFIED` + Redis probe `RUNTIME_VERIFIED` | `departments/risk_qa_testkit/replay.py`와 실제 Redis 두 Event를 검증했지만 실제 API/DB Decision Replay는 미완료 |
| Risk↔QA Redis Event Bus | `RUNTIME_VERIFIED` snapshot | 재시작 복구·Transactional Outbox까지 공통 Pipeline에서 재검증 |
| Compose Risk/QA Runtime | `RUNTIME_VERIFIED` snapshot | Trading·Accounting을 포함한 전체 Core Compose 필요 |
| DB/Event rollback smoke | `RUNTIME_VERIFIED` snapshot | 실제 API 생성 Decision/Case의 commit·replay 증거 필요 |
| Research Packet Replay | 미완료 | Research Packet URL/Case Replay 계약이 아직 구성되지 않음 |
| `MODEL-03`, `QA-03`, `OPS-01` | `BLOCKED` | 모델 선언·Gateway·운영 Credential/권한 경계 확정 필요 |
| `RPT-01` 보존 경계 | 부분 구현 | DB Artifact hash·보존정책·Notion idempotency를 운영 계약으로 확정해야 함 |

2026-08-05 재검증에서 Replay·통합 회귀 테스트 13개가 통과했고 Risk/QA 전체 테스트의 비외부 케이스가 통과했다. 샌드박스에서는 외부 Redis 의존 테스트 8개가 skip되었으며, 승인된 외부 smoke에서는 Redis Risk·QA Event 2건과 PostgreSQL rollback이 `READY`, Research Packet `packet_contract`는 `NOT_CONFIGURED`, 전체 상태는 `PARTIAL`이었다. 이는 실행 증거이며 이후 코드 변경 때 자동으로 유지되는 보증이 아니다.

동일 재검증에서 Claude 환경(`/Users/baiohelseu/claude`)의 Risk/QA 전체 `ruff check`와 `ruff format --check`가 모두 통과했다. 참조가 없는 legacy Worker Graph·구형 Research Packet fixture·중복 Counterparty self-check도 제거했다. 실제 정책 Corpus·Issuer/JWKS·mTLS/IAM·API 생성 Decision Replay가 없다는 이유로 운영 승인은 계속 `BLOCKED`다.

## 3. Override 작업 순서

### P0-1. Risk Decision–QA Decision 공통 Replay

**담당:** 동규, 도현, 재일 공동. **선행:** `RQ-01`, `TRD-01`, `PLAT-01`.

**현재 진행:** 공통 Replay validator와 TEST pipeline 연결은 `TEST_VERIFIED`, 실제 Redis 두 Event Envelope probe는 `RUNTIME_VERIFIED`다. 실제 API가 생성한 Risk/QA Decision과 PostgreSQL row를 함께 재생한 것은 아니므로 전체 P0-1은 완료되지 않았다.

- 고정 Research Packet Fixture 하나가 `packet_id`, `trace_id`, `as_of`, `input_hash`를 유지한 채 OrderIntent로 변환되게 한다.
- Risk API의 `risk.decision.v1`, QA의 `qa.decision.v1`, OrderIntent, Fill, Journal, Event가 동일한 trace와 결정 hash를 보존하게 한다.
- 실제 DB commit 경로와 rollback 경로를 모두 실행한다. API 주입 Fill만으로 `ACC-01`을 통과시키지 않는다.
- 미래 시점 데이터, 임의 문자열 승인자, 누락 Evidence가 들어오면 `HOLD`/`REJECT`/`ESCALATE`여야 한다.

**완료 증거:** 한 trace의 API 응답·PostgreSQL row·Redis event·QA Finding을 재조회한 결과, ID/hash가 일치하고 재실행 중복이 0건이다.

### P0-2. 운영 명령 인증 경계

**담당:** 동규, 영주 공동. **선행:** Platform/IAM의 Secret Manager 또는 동등한 주입 방식.

- Risk API는 `risk.trading_state.write`, `risk.trading_state.clear`, QA API는 `qa.corrective_action.close` scope를 분리한다.
- Token의 department, service, subject, scope, expiry, issuer/audience를 검증하고 `subject == set_by/verifier`를 유지한다.
- Secret은 저장소·문서·로그·Compose 기본값에 넣지 않는다. 기본값은 반드시 fail-closed다.
- 만료, 잘못된 department, 잘못된 scope, subject 불일치, 서명 오류, Secret 미설정의 음성 테스트를 고정한다.

**완료 증거:** 테스트용 서명 Token의 양성/음성 API 결과, Secret 주입 경로, rotation/revoke 절차, 감사 로그가 모두 존재한다. 이것만으로 mTLS/IAM 전체 완료를 주장하지 않는다.

### P0-3. 실제 QA Corpus와 QA Worker 배선

**담당:** 동규, 재일 협업.

- `SAMPLE_PLACEHOLDER` 문서를 운영 정책으로 사용하지 않도록 실제 승인된 문서·source_id·version·effective_at을 등록한다.
- `model-risk-agent`, `internal-audit-agent`의 엔진/API와 LangGraph 배선 상태를 분리 기록한다.
- Evidence Retriever, citation 검증, hallucination check, retry, incident escalation의 결과를 `audit.*` append-only 구조에 기록한다.
- QA Eval 없이 Profile을 `ACTIVE`로 전환하거나, QA가 자기 Finding을 종결하는 경로를 차단한다.

**완료 증거:** 실제 정책 1건과 의도적 unsupported claim 1건에 대해 `GROUNDED`와 `ESCALATE/BLOCKED`가 재현되고, Case Replay가 가능하다.

### P1-1. `MODEL-03`·`QA-03`·`OPS-01`

- Hermes Head는 `openai-codex/gpt-5.6-luna`, 직원 Worker는 Ollama `qwen3:1.7b`라는 계층을 Profile·Worker Registry·Checker에서 일치시킨다.
- QA의 개인 GPU IP/직접 endpoint를 제거하고 승인된 Model Gateway만 사용한다.
- Risk/QA 운영 Credential, DB RLS, Service Identity, rotation, audit event, 최소 권한을 preflight로 검사한다.
- preflight 누락은 `READY`가 아니라 `BLOCKED`/`DENY`여야 한다.

### P1-2. `RPT-01`과 사고 대응

- Decision/Evidence/Finding/Incident Artifact의 hash, 보존 기간, 삭제 금지, Notion projection idempotency를 결정한다.
- Incident severity는 결정론적 threshold가 계산하고 Agent는 설명만 한다.
- Redis·DB 장애, duplicate event, stale event, credential revoke, worker restart를 포함한 rollback/runbook을 작성한다.

## 4. 인계 계약

| 보내는 팀 | Risk/QA가 받는 것 | 받기 전 거부 조건 |
|---|---|---|
| 재일 | Research Packet, PIT cutoff, source/citation, packet hash | `as_of`·source version·input hash가 없으면 거부 |
| 도현 | OrderIntent, OMS/Fills, Ledger reference, event hash | Risk 승인 전 Submit, API 주입 Fill, ID 불일치면 거부 |
| 영주 | Mandate/Approval/Actor identity/Policy version | unsigned actor, 만료 승인, SoD 위반이면 거부 |
| 동규 | Risk Decision·QA Decision·Finding | 근거 없는 `APPROVE`/`PASS`는 생성하지 않음 |

## Risk·QA 역할 분배 최적화 기준 (2026-08-05)

**2026-08-06 tool 강등**: Risk의 `core-risk-worker`(옛 `market-liquidity-worker`+`pre-trade-risk-worker`
병합)+`derivatives-counterparty-worker`, QA의 `evidence-qa-worker`+`model-and-internal-audit-worker`+
`ops-and-permission-worker`는 결정론 Engine이 이미 답을 내고 있었으므로 LLM Registry(`WORKER_SPECS`)
밖의 결정론 러너 `risk-runner`/`qa-runner`로 흡수했다. 두 러너는 매 케이스마다 항상 실행되며 `llm: False`다.
남은 LLM Worker는 전부 조건부다 — 근거가 있을 때만 팬아웃한다.

| 입력 조건 | 실행 Worker | 안전한 미실행/실패 결과 |
|---|---|---|
| 모든 포트폴리오 추천 | Risk risk-runner, QA qa-runner (결정론, LLM 없음) | 각 Engine 판정을 그대로 옮김 — 실패는 blockers/escalate로 직접 반영, HOLD/ESCALATE로 fail-closed |
| compliance evidence 존재 | Risk compliance-policy-worker (LLM) | PIT·ACL·citation 미통과 시 `ESCALATE`, 근거 없는 정책 판정 금지 |
| unsupported/contradicted claim 존재 | QA hallucination-critic-worker (LLM) | 미해결 claim이면 QA PASS 금지 |
| incident 입력 존재 | QA incident-postmortem-worker (LLM) | append-only timeline과 human review로 종료 |

기술 스택과 Worker별 성과 지표는 [`WORKER_ROLE_BOUNDARIES.md`](../02-engineering/WORKER_ROLE_BOUNDARIES.md)와 실행 메타데이터 [`departments/risk_qa_worker_profiles.py`](../../departments/risk_qa_worker_profiles.py)에 함께 정의한다. 성과는 단순 완료 수가 아니라 freshness·PIT·citation·determinism·false-clear·permission violation·latency·replay completeness를 기록한다. Ollama `qwen3:1.7b`는 구조화된 근거의 advisory 서술만 담당하고, 바인딩 판정·권한·상태 전이는 결정론적 Engine/API가 담당한다.

외부 쓰기는 이 부서 Worker의 기본 권한이 아니다. Risk/QA 도메인 API의 write scope가 존재하더라도 현재 포트폴리오 추천 실행은 `external_writes=false`이며, Worker allowlist는 read/calculation 도구만 허용한다. 실제 write를 연결할 때는 별도 command path, service token, SoD, `idempotency_key`, `expected_version`, append-only audit event와 음성 테스트가 먼저 필요하다.

## 5. 검증 명령과 보고 형식

검증 시 Secret·Cookie·DATABASE_URL·Redis URL의 credential 부분을 출력하지 않는다.

```bash
python -m pytest departments/03-risk/tests departments/06-ai-qa-audit/tests -q -p no:warnings
docker compose config --quiet
docker compose ps --all
curl -fsS http://127.0.0.1:8041/risk/v1/observability/runtime
curl -fsS http://127.0.0.1:8042/qa/v1/observability/runtime
```

외부 Redis·PostgreSQL 검증은 `scripts/run_risk_qa_integration_smoke.py`를 사용하고, 결과에 반드시 `production_enabled`, `trace_preserved`, `transaction`, `packet_contract`, skip 수를 기록한다.

주간 보고는 다음 5줄을 지킨다.

1. 이번 주 `IMPLEMENTED`/`TEST_VERIFIED`/`RUNTIME_VERIFIED`가 된 항목.
2. 실제 실행 명령과 Commit 또는 CI Run.
3. 실패·skip·환경 제약과 안전한 결과(`DENY/HOLD/ESCALATE`).
4. 다음 작업의 선행 팀과 완료 증거.
5. 운영 승인 가능 여부. 승인 불가하면 반드시 `BLOCKED` 사유를 쓴다.

## 5-A. 사용자 입력 명세 구현 현황 (Risk/QA 기준, 2026-08-05)

아래 상태는 문서만이 아니라 현재 코드와 결정론적 테스트를 기준으로 판정한다. `TEST_VERIFIED`는 운영 데이터·권한·실서비스 연결을 의미하지 않는다.

| 동규님 담당 항목 | 상태 | 현재 구현·검증 증거 | 남은 운영 조건 |
|---|---|---|---|
| 경험×성향 9개 프리셋 | `IMPLEMENTED` + `TEST_VERIFIED` | `departments/03-risk/mandate_presets.py`, 9개 조합·정렬 테스트 | Risk가 제품 기본값으로 승인하고 API/온보딩에 연결 |
| 성향↔한도 정렬 | `IMPLEMENTED` + `TEST_VERIFIED` | 프리셋보다 완화된 값은 `REQUIRES_RISK_REVIEW`; 자동 완화하지 않음 | 고급 설정 API에서 review 결과를 실제 승인 상태와 연결 |
| `max_sector_weight` | `IMPLEMENTED` + `TEST_VERIFIED` | `MandateScope`와 Risk Engine이 섹터 메타데이터·현재 섹터 노출을 검증하고 초과 주문을 축소/거부 | 실시간 PIT instrument/sector metadata와 portfolio exposure 적재 |
| `allowed_asset_classes` / `forbidden_asset_classes` | `IMPLEMENTED` + `TEST_VERIFIED` | Mandate → Risk Context → pre-trade gate 연결. 금지 자산군 또는 메타데이터 누락은 fail-closed | 기준 자산군 코드와 전체 유니버스 적재 |
| `preferred_sectors` / `excluded_sectors` | `IMPLEMENTED` + `TEST_VERIFIED` | 선호는 후보 우선순위 맥락, 제외는 신규 노출 차단 | KRX 업종 taxonomy와 종목 매핑의 PIT 품질 확보 |
| LLM 제안 → 사용자 확인 → Risk/QA → 활성화 | `IMPLEMENTED` + `TEST_VERIFIED` | `orchestration/contracts/mandate_confirmation.py`와 CEO HITL workflow. 정책 검증·Risk 승인·QA 승인·사용자 승인 모두 필요 | 실제 governance API와 인증·영속 checkpoint 연동 |
| QA evidence/hallucination/permission/audit | `IMPLEMENTED` + `TEST_VERIFIED` | Worker Registry, 계약 검증, 실패 시 `DEGRADED/HOLD`, 스킵 기술 메타데이터 기록 | `SAMPLE_PLACEHOLDER` Corpus를 승인된 실제 정책 문서로 교체 |

### 실행 상태 집계와 안전 스킵 의미

부서 fan-in 결과의 `executed`는 **완료된 Worker 수**만 의미한다. `skipped_safe`와 `not_requested`를 섞지 않는다.

| 필드 | 의미 |
|---|---|
| `completed` / `executed` | Worker가 실제 실행되어 유효한 계약 결과를 낸 수 |
| `skipped_safe` | 라우팅은 되었지만 `LIVE_DATA_NOT_READY`로 안전 차단된 수 |
| `not_requested` | CEO task plan이 해당 부서를 선택하지 않은 수 (`NOT_REQUESTED`) |
| `failed` | 실행 중 오류로 계약 결과를 만들지 못한 수 |
| `skip_reasons` | 위 사유별 집계. 운영 리뷰와 Replay에서 필수 |

따라서 데이터가 없는 실행에서 Risk 2개와 QA 3개가 모두 안전 차단되면 `executed=0`, `skipped_safe=1/3`, `failed=0`, `skip_reason=LIVE_DATA_NOT_READY`가 맞다. 이는 성공 추천이 아니며 pipeline은 `DEGRADED/HOLD`여야 한다. 라우팅 미선택은 `NOT_REQUESTED`로 별도 기록한다.

모든 파이프라인 실행은 최소 `pipeline_started`와 `pipeline_completed` 이벤트를 남긴다. 각 Worker 보고서에는 실행 여부와 무관하게 WorkerSpec의 `technology` 메타데이터를 포함해 감사·Replay가 가능해야 한다.

### 현재 Acceptance 판단

- 결정론적 계약·Risk Engine·confirmation gate: 약 **80~85%**
- QA Worker 계약·실패 전파·Replay 메타데이터: 약 **75~80%**
- 외부 운영 연결: **BLOCKED** — Supabase에 PIT 국내주식 instrument/market snapshot이 없으면 후보를 만들지 않고 HOLD한다.
- `SAMPLE_PLACEHOLDER` 정책 Corpus, 실제 service identity/JWKS·mTLS/IAM, Redis/PostgreSQL transactional outbox, Ollama 연결은 별도 운영 Gate다.

## 6. 최종 Release Gate

- [ ] Research Packet → OrderIntent → Risk → QA → Fill → Journal → Projection 전체 Replay 통과
- [ ] Risk/QA DB row와 Event hash 일치, 재시작 후 복구, consumer idempotency 통과
- [ ] 실제 정책 Corpus와 Evidence citation 검증 통과
- [ ] Service Token 양성/음성·rotation/revoke 및 DB RLS 검증 통과
- [ ] MODEL-03·QA-03·OPS-01 preflight 전부 `READY`
- [ ] Incident/rollback/runbook과 보존 정책 승인

위 항목 중 하나라도 미충족이면 Risk/QA는 운영 배포를 승인하지 않는다.
