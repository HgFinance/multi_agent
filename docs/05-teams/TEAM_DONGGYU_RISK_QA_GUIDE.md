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

## 6. 최종 Release Gate

- [ ] Research Packet → OrderIntent → Risk → QA → Fill → Journal → Projection 전체 Replay 통과
- [ ] Risk/QA DB row와 Event hash 일치, 재시작 후 복구, consumer idempotency 통과
- [ ] 실제 정책 Corpus와 Evidence citation 검증 통과
- [ ] Service Token 양성/음성·rotation/revoke 및 DB RLS 검증 통과
- [ ] MODEL-03·QA-03·OPS-01 preflight 전부 `READY`
- [ ] Incident/rollback/runbook과 보존 정책 승인

위 항목 중 하나라도 미충족이면 Risk/QA는 운영 배포를 승인하지 않는다.
