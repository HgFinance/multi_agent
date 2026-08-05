# 영주님 담당 가이드: CEO Office + Agent Workforce

> Override v2.0 · 기준일 2026-08-05
>
> 이 문서는 이전 CEO/HR 팀 가이드의 운영 기준을 덮어쓴다. Mandate·Workforce API 코드가 있다는 사실을 실제 승인자 인증, 자기개선 운영, IAM Provisioning 완료로 해석하지 않는다. 최상위 기준은 [HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md), [PROJECT_IMPLEMENTATION_STATUS.md](../PROJECT_IMPLEMENTATION_STATUS.md), [GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md](../02-engineering/GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md)다.

## 0. 상태 판정 규칙

| 상태 | 의미 | 대표 해석 |
|---|---|---|
| `DOCUMENTED` | Governance/HR 계약과 완료 조건만 있음 | `GOV-02`, `HR-03`의 일부 |
| `IMPLEMENTED` | API·Repository·결정론적 불변식이 있음 | `GOV-01`, `HR-01`, `HR-02` baseline |
| `TEST_VERIFIED` | 현재 Commit의 승인·SoD·Lifecycle 테스트 통과 | 실제 IAM·운영 Actor 인증과 분리 |
| `RUNTIME_VERIFIED` | 실제 DB/API/Worker/Event가 왕복함 | Production 권한 승인과 분리 |
| `BLOCKED` | 승인자·Credential·Profile·선행 QA가 없음 | 자동 승인 fallback 금지 |

CEO/HR은 다른 부서의 Risk 거부권, QA 감사권, 주문 제출권, Ledger 수정권, NAV 확정권을 갖지 않는다.

## 1. 책임과 절대 경계

### CEO Office

- 사용자 Mandate, Governance Decision, Investment Case, Approval, Committee, Escalation, Notification의 정책·상태 전이를 관리한다.
- `LOOSEN`·최초 활성화·운영 확대는 사용자 또는 승인된 위원회 절차 없이는 활성화하지 않는다.
- 정족수 미달은 `DEFER`, Risk veto는 문턱과 무관하게 `REJECT`, 알 수 없는 권한·주체는 `DENY`/`BLOCKED`다.
- CEO Agent가 Risk/QA 결과를 수정하거나, 자기 결정만으로 주문·NAV·Ledger를 확정하지 않는다.

### Agent Workforce

- 공식 Roster, Profile Version, Tool Permission, Model Assignment, Access Lifecycle, QA Eval을 관리한다.
- Profile 작성자 자기승인, QA 없는 `ACTIVE`, 만료 Access의 재사용, 자동 Promotion을 차단한다.
- HR은 Shared Service다. 투자본부의 제7 부서나 Risk/QA 권한 대체가 아니다.

### 공통 금지

- `actor_agent_id`·`verifier` 문자열만으로 승인자를 인증하지 않는다.
- `governance.approvals`의 상태만 보고 실제 Subject, Department, Role, expiry, SoD를 생략하지 않는다.
- Notion·Discord·Email 연동이 없을 때 이를 성공으로 표현하지 않는다.
- 승인 실패를 APPROVE·ACTIVE·PROMOTE로 fallback하지 않는다.

## 2. 현재 기준선

| 항목 | 현재 판정 | 남은 조건 |
|---|---|---|
| Mandate Policy/Version/Activation | `IMPLEMENTED` + DB 왕복 snapshot | 실제 Actor 인증·운영 사용자·승인 Audit 경계 필요 |
| `GOV-01` | `IMPLEMENTED` | 현재 `RUNTIME_VERIFIED`와 운영 권한을 분리 기록 |
| Approval API | 구현 baseline | 실제 주체 인증, Role/Department SoD, 만료·revoke E2E 필요 |
| Committee Quorum/Veto | 결정론적 모듈·테스트 baseline | API/DB append-only 왕복과 committee type 정책 승인 필요 |
| Case/Escalation | 부분 구현 | Repository 모든 상태 전이와 Notification 연결 필요 |
| Notification | 심각도·dedup 규칙 있음 | Notification Repository/실제 수신자 Routing Table 미완료 |
| `GOV-02` | `DOCUMENTED`/부분 구현 | Investment Case→Approval→Committee→Escalation 전체 API Replay 필요 |
| `HR-01` | `IMPLEMENTED` | 독립 승인 DB Replay와 운영 Access 경계 필요 |
| `HR-02` | `IMPLEMENTED` | QA Eval·CEO 승인·Profile/Tool Version의 실제 ACTIVE 검증 필요 |
| Workforce Registry | `IMPLEMENTED` baseline | Quality Snapshot·Workforce Plan 집계/저장 로직 필요 |
| Access Lifecycle | 구현 baseline | Platform/IAM 이벤트·Provisioning Worker 연결 필요 |
| `HR-03` | `DOCUMENTED` | Eval Runner·Shadow Router·Promotion·Rollback 실체화 필요 |
| `HR-04` | `BLOCKED` | Draft Profile 13개 Review와 Tool Allowlist 보완 필요 |
| HR LangGraph Sub-graph | 미완료 | prompt-only 5개 Persona의 State Machine·Worker Graph 필요 |

## 3. Override 작업 순서

### P0-1. 승인자 인증과 SoD

**담당:** 영주. **협업:** 동규, Platform/IAM.

> **⚠️ 팀 합의 (2026-08-05) — 이건 실제 로그인 인증이 아니다.** 이 저장소에는 서명된
> Subject 인증(mTLS/JWT, Supabase Auth 로그인)이 아직 없다 - Platform/IAM이 전체 인증
> 아키텍처를 결정하기 전까지는 CEO Office 혼자 이 항목을 완전히 닫을 수 없다(설계
> 공백을 임의로 채우지 않는다). 그래서 팀은 **`supabase/seed.sql`에 심어둔 테스트 회원
> 행을 "로그인된 사용자"로 간주**하기로 합의했다: `actor_user_id`가 `governance.
> user_profiles`에 실재하고 `status='ACTIVE'`인지만 결정론적으로 검증한다
> (`departments/00-ceo-office/src/approval/actor_identity.py`,
> `UnverifiedActorUserError` → 403). 이건 "서명으로 신원을 증명"이 아니라 "최소한 DB에
> 실재하는 활성 계정인가"까지만 좁힌 검증이다 - **Production 배포 전 실제 Auth로 반드시
> 교체해야 한다.** 아래 §6 Release Gate의 "승인자 Identity가 서명/검증됨"은 이 상태로는
> 체크할 수 없다.

- Approval/Committee/Case API는 서명된 Subject 또는 검증 가능한 사용자 Identity를 받는다. → **현재는 위 팀 합의로 대체(실재성+ACTIVE만 검증), 서명 검증은 BLOCKED**
- Subject의 department, role, scope, expiry, approval target을 결정론적으로 검증한다. → department/role/expiry는 기존대로 검증(`_ROLE_DECIDERS`, `is_expired`). scope(Fund 단위 권한 범위)는 `governance.fund_memberships`가 비어 있어 여전히 미검증
- Risk/QA는 자체 업무의 독립 veto/verification 권한을 유지하며 CEO가 대신 결정하지 않는다.
- `actor_agent_id`가 NULL이거나 Profile/Role과 매핑되지 않으면 `DENY`다. → **BLOCKED로 유지.** 2026-08-04 팀 결정("Agent Roster 등재는 전체 Prototype까지 미룬다")과 정면 충돌한다 - 지금 이 규칙을 적용하면 Roster 미등재 상태인 현재 모든 결정이 막힌다. Roster 등재가 끝난 뒤에 켠다.
- 최초 Mandate, `LOOSEN`, 상한 확대, LIVE 관련 변경은 승인 증거 없이는 저장·활성화하지 않는다.

**완료 증거:** 올바른 Actor, 잘못된 Department, 만료 Approval, 자기승인, Risk veto, quorum 부족, revoke 후 재사용 각각의 결과가 고정 테스트와 DB Audit row에 남는다. **actor_user_id 실재성 검증(신규)**도 고정 테스트로 남는다(`api/app.py` 자체점검 15c번 시나리오 - 실재하지 않는 actor_user_id는 403).

### P0-2. GOV-02 전체 상태 Replay

다음 그래프를 실제 DB Repository와 API로 재현한다.

`Investment Case → Approval Request → Committee Open/Vote/Close → Governance Decision → Escalation → Notification → Case Resolve/Cancel`

- 모든 상태 전이는 append-only event와 `trace_id`를 보존한다.
- 정족수 미달은 `DEFER`, veto는 `REJECT`, 의존 서비스 오류는 `BLOCKED`/`ESCALATE`다.
- `GET /governance/v1/mandates/{fund_id}/current` 같은 공식 Read Model을 누락한 채 downstream이 임의로 Mandate를 조회하지 않는다.
- Notification 수신자·채널이 결정되지 않으면 발송 성공으로 만들지 않고 `PENDING`/`ESCALATE`한다.

### P0-3. HR-02 Active Gate와 Profile Review

- Profile Version, Tool Allowlist, Model Assignment, QA Eval, CEO Approval의 version/hash를 함께 저장한다.
- 작성자와 승인자를 분리하고, QA Eval 또는 CEO 승인 하나라도 없으면 `ACTIVE` 전환을 409/deny한다.
- Draft Profile 13개를 역할·trigger·tool·data boundary·model tier별로 review하고, 미승인은 `DRAFT`로 유지한다.
- Tool Allowlist가 없는 Persona는 실행 권한을 주지 않는다.

### P1-1. HR-03 자기개선 폐쇄 루프

`Candidate → Independent QA Eval → Shadow → Approval → Promotion → Rollback`

- Eval Runner와 Shadow Router를 구현한다.
- 비용·품질·안전·회귀 지표를 Scorecard에 저장한다.
- Promotion과 Rollback은 동일 Agent/작성자가 단독 수행하지 못하게 한다.
- 실패한 Eval은 기존 Profile 유지와 `HOLD`로 끝낸다.

### P1-2. HR-04 Access Lifecycle와 Workforce Plan

- Access Request → 승인 → IAM Provisioning → 검증 → 회수의 이벤트 계약을 Platform/IAM과 확정한다.
- Provisioning 성공을 API flag만으로 간주하지 말고 실제 권한 조회로 확인한다.
- Quality Snapshot과 Workforce Plan을 실제 데이터에서 집계·저장한다. 빈 집계를 정상 운영 상태로 표시하지 않는다.
- Budget/Scorecard의 추천은 승인 명령이 아니라 설명·권고다.

### P1-3. 공통 CI·Frontend 경계

- `CI-01` 중복 Smoke 수집 문제를 도현님과 해결하고, 전체 Test Collection 성공을 Merge Gate로 만든다.
- AI Office는 `DEMO/PAPER/LIVE`를 분리하고, Agent status는 Read-only Projection으로만 표시한다.
- Kanban/Notification UI가 Governance API를 우회해 상태를 바꾸지 못하게 한다.

## 4. 인계 계약

| 상대 팀 | 영주가 제공/확인할 것 | 없으면 |
|---|---|---|
| 재일 | Strategy/Research Release 상태와 승인 가능한 Universe | Production 승격·Mandate 확대 금지 |
| 도현 | 활성 Mandate, allocation, approval, expiry, governance decision | OrderIntent/Journal/NAV 진행 금지 |
| 동규 | Actor identity, Policy version, 승인·veto·revoke evidence | Risk/QA 운영 승인 금지 |
| HR → 전 부서 | Profile/Tool/Model Version과 Access 상태 | Worker `ACTIVE` 금지 |
| Platform/IAM → HR | Provisioning·회수·검증 이벤트 | Access를 부여된 것으로 표시 금지 |

## 5. 검증

CEO/HR 검증은 DB row·Event·API response의 `trace_id`, actor, version, expiry, decision, reason을 함께 확인한다.

```bash
python -m pytest tests/contracts/test_unified_api_contract.py tests/e2e/test_async_worker_registry.py tests/orchestration/test_workflows.py -q -p no:warnings
python -m unittest discover -s tests/schema -p 'test_*.py' -v
docker compose config --quiet
```

Ollama가 준비된 경우에만 CEO/HR self-check를 별도로 실행한다.

```bash
python departments/00-ceo-office/scripts/test_ceo_ollama_agent.py
python departments/07-agent-workforce/scripts/test_hr_ollama_agent.py
```

외부 DB를 사용하는 경우 실제 운영 테이블을 오염시키지 않도록 transaction rollback 또는 전용 test fund/profile을 사용하고, 테스트 Actor·Secret을 로그에 출력하지 않는다.

## 6. 최종 Release Gate

- [ ] 승인자 Identity·Role·Department·expiry·scope가 서명/검증됨
- [ ] Risk veto·QA 독립성·CEO SoD가 API/DB 양쪽에서 강제됨
- [ ] GOV-02 Case/Approval/Committee/Escalation/Notification Replay 통과
- [ ] Mandate current Read Model과 allocation lineage가 downstream에서 조회됨
- [ ] HR-02 ACTIVE는 QA Eval·CEO 승인·Profile/Tool/Model version 없이는 불가
- [ ] HR-03 Shadow/Promotion/Rollback과 HR-04 IAM Provisioning/회수 검증 완료
- [ ] Draft Profile Review와 Allowlist 누락이 0건
- [ ] `CI-01`와 Frontend/Projection의 clean test가 통과함

위 조건 전에는 Governance를 통한 Trading·Risk·Ledger·NAV의 운영 승인을 선언하지 않는다.
