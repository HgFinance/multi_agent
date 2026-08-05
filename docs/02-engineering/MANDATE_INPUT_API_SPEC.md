# Mandate 입력 API 명세서 (FastAPI)

> 작성: 영주님 (CEO Office) · 작성일: 2026-08-04
> 문서 상태: 구현 반영 (governance-api는 이미 존재한다 — 아래 §9 참고)
> **관련 분석 자료: [USER_INPUT_SCOPE_ANALYSIS.md](../01-product/USER_INPUT_SCOPE_ANALYSIS.md)** (확정 계약 아님) — 사용자 입력 항목의 귀속 현황과 미결정 안건 정리. 이 문서는 **Mandate Transport 계약**만 다룬다.
> 상위 계약: [GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md](GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md) §2.1(Mandate),
> [UNIFIED_DOMAIN_API_SPEC.md](UNIFIED_DOMAIN_API_SPEC.md) §3(공통 계약)
> Frontend 소비처: 아직 없다 — 화면 설계는 [USER_INPUT_SCOPE_ANALYSIS.md](../01-product/USER_INPUT_SCOPE_ANALYSIS.md) §5 결정 후 작성한다.
> 실제 구현: `departments/00-ceo-office/api/app.py`, `src/mandate/{policy,service,lifecycle,change_workflow}.py`, `src/approval/approval.py`, `src/case/case_root.py`

> **정정 메모**: 이 문서의 이전 버전은 governance-api 전체를 "미구현/제안"으로 적었다. 실제로는
> `departments/00-ceo-office/api/app.py`에 Mandate·Case·Approval·Committee·Escalation·Reporting
> Route가 전부 구현돼 있고, `change_workflow.py`의 HITL(Human-in-the-Loop) 오케스트레이션은
> Risk/QA 병렬 승인 → 사용자 승인 → 활성화까지 자체 점검 11개 시나리오(UC-1~7)와 실 DB 통합
> 검증을 통과했다. 이 버전은 실제 코드를 읽고 다시 썼다.

## 0. 통신 경계

- **`governance-api`(`departments/00-ceo-office/api/app.py`)는 독립 FastAPI 앱으로 이미 존재한다.** 아직 `apps/api`(AI Office BFF)에 연결되지 않았다 — `apps/api/main.py`는 현재 `accounting`/`trading` Router만 등록하고 governance Router가 없다.
- AI_OFFICE_FRONTEND_PLAN.md §6에 따르면 Browser는 Domain API를 직접 호출하지 않고 BFF만 호출해야 한다. **따라서 Mandate 화면을 실제로 붙이려면 `apps/api`에 governance Router를 새로 등록하는 작업이 선행돼야 한다** — 이 문서는 그 Router가 그대로 감쌀 governance-api의 실제 계약을 기술한다.
- Supabase Service Role, Broker/LS Credential은 이 경로 어디에도 없다.

## 1. Mandate 관련 실제 Route 목록

| Method/Path | 감싸는 것 | 상태 |
|---|---|---|
| `POST /governance/v1/mandates/{mandate_id}/change-requests` | `MandateChangeWorkflow.submit()` | ✅ 구현 — **Frontend가 "Mandate 제출" 버튼에서 호출할 주 경로** |
| `POST /governance/v1/cases/{case_id}/advance` | `MandateChangeWorkflow.advance()` | ✅ 구현 — Risk/QA/사용자 승인 결정 후 다음 단계로 넘긴다 |
| `POST /governance/v1/approvals/{approval_id}/decide` | `approval.decide()` | ✅ 구현 — 사용자 승인 Dialog가 호출하는 경로(§5) |
| `GET /governance/v1/approvals?object_type=MANDATE_VERSION&object_id={version_id}` | `approval_repo.list_by_object()` | ✅ 구현 — Case의 RISK/QA/USER 승인 상태 조회 |
| `POST /governance/v1/mandates/{mandate_id}/versions` | `MandateVersionService.propose_version()` | ✅ 구현 — 저수준 빌딩 블록. **Frontend는 직접 쓰지 않는다**(§3) |
| `POST /governance/v1/mandates/{mandate_id}/versions/{version}/activate` | `MandateActivationService.activate()` | ✅ 구현 — 저수준 빌딩 블록. `change-requests` 내부가 이미 호출한다 |
| `GET /governance/v1/mandates/{mandate_id}/current` | `MandateVersionRepository.get_mandate_current()` | ✅ 구현 — **응답이 최소 필드뿐이다**(§4, GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC §2.1과 다름) |
| `GET /governance/v1/cases/{case_id}` / `.../timeline` | `case_repo.get()` / `case_repo.timeline()` | ✅ 구현 — Mandate 변경 이력·감사 표시용 |

`GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md` §2.1이 그리던 "propose → activate" 2단계 수동 흐름은 여전히 유효한 API지만, **F01/TEAM_YOUNGJU §5.1이 요구하는 실제 업무 흐름(Risk·QA 검토 → 사용자 승인)은 `change-requests`/`advance`가 오케스트레이션한다.** Frontend는 이 상위 경로만 호출한다.

## 2. Mandate 데이터 입력 폼 → `POST .../change-requests` 요청 바디

이 절이 "Mandate 데이터 입력 폼 형식"의 본체다. `SubmitChangeRequestIn`(app.py)과 1:1이다.

```json
{
  "fund_id": "uuid",
  "policy": {
    "allowed_assets": ["A005930"],
    "forbidden_assets": ["A000660"],
    "risk_bounds": {
      "base_capital": "100000000",
      "currency": "KRW",
      "max_instrument_weight": "0.1",
      "max_sector_weight": "0.3",
      "max_gross_exposure": "1.0",
      "max_concurrent_positions": 10,
      "max_daily_loss": "0.03"
    },
    "universe_policy": {
      "allowed_markets": ["KRX"],
      "trading_start": "09:00",
      "trading_end": "15:30"
    },
    "approval_rules": {
      "paper_order_mode": "USER_APPROVAL",
      "risk_expansion_requires_user_approval": true
    }
  },
  "objective_text": "장기 성장",
  "objective": { "style": "growth" },
  "effective_from": "2026-08-04T00:00:00Z",
  "created_by": "user-display-name-or-id",
  "trace_id": "uuid",
  "now": "2026-08-04T00:00:00Z",
  "previous_policy": { "...": "직전 ACTIVE policy 전체(변경일 때만, 최초 생성이면 생략)" },
  "priority": 50,
  "review_expires_at": "2026-08-05T00:00:00Z",
  "user_approval_ttl_seconds": 86400,
  "version_created_by": "uuid|null"
}
```

`policy` 하위 필드의 타입·제약은 **`policy.py`가 Source of Truth**다(속성 전수 표는 [USER_INPUT_SCOPE_ANALYSIS.md](../01-product/USER_INPUT_SCOPE_ANALYSIS.md) 부록 A). 여기서는 이 Route 특유의 필드만 정리한다.

| 필드 | 타입 | 필수 | 비고 |
|---|---|---|---|
| `fund_id` | UUID string | 필수 | Case·Approval의 `fund_id`로 쓰인다 |
| `previous_policy` | `MandatePolicy` \| null | 변경 시에만 | 없으면(`null`) 최초 생성으로 취급 — `direction=NEUTRAL`이 아니라 **최초 활성화 게이트**(항상 검토 필요)가 걸린다 |
| `created_by` | string(자유 텍스트) | 필수 | `governance.cases.created_by`(text) — 사용자 uuid가 아니어도 됨. 로그인 사용자 표시명 등 |
| `version_created_by` | UUID \| null | 선택 | `mandate_versions.created_by`(FK `governance.user_profiles`) — `created_by`와 컬럼 타입이 달라 분리됨. 모르면 생략(`null` 허용) |
| `trace_id` | UUID string | 필수 | 감사 추적. Frontend가 매 제출마다 새로 생성 |
| `now` | ISO-8601 UTC | 필수 | 서버 시각을 신뢰하지 않고 클라이언트가 명시 — 자체 점검 코드가 논리 시계를 쓰기 때문(실 운영에서는 `datetime.now(UTC)`를 그대로 넣어도 된다) |
| `priority` | int 0~100 | 선택(기본 50) | Case 우선순위 |
| `review_expires_at` | ISO-8601 UTC \| null | 선택 | Risk/QA 검토 응답 기한. 없으면 무기한 대기 |
| `user_approval_ttl_seconds` | int ≥1 | 선택(기본 86400=24h) | **Risk/QA 통과 시점부터** 카운트(제출 시점 아님) — 검토가 길어져도 사용자 승인 시간이 줄지 않는다 |
| `fund_base_currency` | string \| null | 데모 전용 | In-Memory Repository일 때만 통화 seed 용도. Postgres Repository에서는 무시되고 `accounting.funds`를 실제로 조회한다. **Production 화면에서는 보내지 않는다** |

### 2.1 응답 — `ChangeRequestResult`

```json
{
  "stage": "FAST_APPLIED|AWAITING_REVIEW|REVIEW_REJECTED|AWAITING_USER_APPROVAL|USER_REJECTED|ACTIVATED",
  "mandate_id": "uuid",
  "version": 4,
  "direction": "TIGHTEN|LOOSEN|NEUTRAL",
  "case_id": "uuid|null",
  "detail": "사람이 읽는 설명 문자열"
}
```

| `stage` | 의미 | Frontend 동작 |
|---|---|---|
| `FAST_APPLIED` | TIGHTEN/NEUTRAL — Case·승인 없이 즉시 활성화 완료 | "즉시 적용됨" 안내 후 Mandate 요약 화면으로 전환. `case_id`는 `null` |
| `AWAITING_REVIEW` | LOOSEN 또는 최초 생성 — Risk/QA 승인이 동시에 요청됨 | "Risk·QA 검토 대기" 배지 표시, `case_id`로 진행 상태 추적 시작 |
| `REVIEW_REJECTED` | Risk 또는 QA가 거절/만료 | "Risk/QA 검토에서 반려되었습니다" — `detail`에 어느 쪽인지 포함. Case 종료(RESOLVED), 이전 Version 유지 |
| `AWAITING_USER_APPROVAL` | Risk+QA 모두 승인 → 사용자 승인 대기 | 재승인 Dialog 노출(§5) |
| `USER_REJECTED` | 사용자가 거절 또는 승인 만료 | "적용되지 않았습니다 — 이전 정책이 유지됩니다" |
| `ACTIVATED` | 사용자 승인 → 활성화 완료 | Mandate 요약 화면으로 전환 |

**`AWAITING_REVIEW`/`AWAITING_USER_APPROVAL`은 이 응답 하나로 끝나지 않는다.** 이 API에는 서버 Push나 WebSocket이 없으므로 Frontend는 `case_id`를 들고 아래 §2.2를 폴링하거나, 승인자가 결정할 때(§5)마다 다시 호출해야 진행 상태가 갱신된다.

### 2.2 `POST /governance/v1/cases/{case_id}/advance`

```json
{ "at": "2026-08-04T00:10:00Z" }
```

응답은 §2.1과 동일한 `ChangeRequestResult` 구조다. **상태 변화가 없으면(승인이 아직 PENDING) 조회만 하고 아무것도 쓰지 않는다** — 안전하게 반복 호출 가능. 다만 멱등 키는 없다(§7 참고).

RESOLVED/CANCELLED Case에 다시 호출하면 409 `CaseAlreadyResolvedError`.

## 3. 왜 propose/activate를 직접 쓰지 않는가

`POST .../versions`(propose)와 `POST .../versions/{version}/activate`는 `change-requests`가 내부적으로 이미 호출하는 저수준 함수다. Frontend가 이 둘을 직접 조합하면 다음을 스스로 재구현해야 한다.

- 최초 활성화 여부 판정(`current_version == 0`)과 그에 따른 강제 검토.
- Risk/QA 승인 요청 생성과 병렬 대기.
- Risk/QA 거절·만료 시 사용자 승인 단계로 넘어가지 않게 막는 것.
- 사용자 승인 만료(TTL)를 Risk/QA 완료 시점부터 다시 계산하는 것.

이건 전부 `change_workflow.py`가 결정론적으로 이미 하는 일이다. Frontend가 propose/activate를 직접 쓸 이유는 없다(테스트·디버깅 목적 외).

## 4. `GET /governance/v1/mandates/{mandate_id}/current` — 실제 응답과 한계

```json
{ "mandate_id": "uuid", "current_version": 3, "status": "ACTIVE" }
```

**`GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md` §2.1이 문서화한 `get_mandate` 응답(전체 `policy` 객체 포함, `fund_id` 기준 조회)과 다르다.** 실제 구현은:

- Path Parameter가 `fund_id`가 아니라 `mandate_id`다(`fund_id → mandate_id` 역참조 쿼리가 아직 없음 — `app.py` 상단 주석 §"스펙과 의도적으로 다른 부분" 참고).
- 응답에 `policy`/`risk_bounds`/`objective` 등 실제 정책 내용이 **없다.** `current_version`과 `status`뿐이다.

**결과적으로 Mandate 변경 화면이 폼에 기존 값을 미리 채우는 것은 현재 API만으로 불가능하다.** 이 값을 채우려면 다음 중 하나가 필요하다(§10 열린 질문에 반영).

1. `GET /governance/v1/mandates/{mandate_id}/versions/{version}` 같은 조회 Route 신규 추가(GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC §2.1에는 있지만 `app.py`에는 없음), 또는
2. `get_mandate_current`가 반환하는 `current_version`으로 별도 조회를 한 번 더 하는 방식.

## 5. Risk/QA/사용자 승인 결정 — `POST /governance/v1/approvals/{approval_id}/decide`

Mandate 전용 엔드포인트가 아니라 GOV-02 공용 승인 엔드포인트다. `change-requests`가 만든 RISK/QA/USER 승인 행을 여기로 결정한다.

```json
{
  "decision": "APPROVED|REJECTED",
  "actor_department": "risk-management|qa-department",
  "actor_user_id": "uuid",
  "at": "2026-08-04T00:15:00Z",
  "reason": "..."
}
```

- `required_role=RISK`/`QA` 승인은 `actor_department`가 필요하고, 그 값이 `required_role`과 맞아야 한다(`risk-management`만 RISK 승인 가능, CEO Office는 대신 결정 불가 — 403 `UnauthorizedDeciderError`).
- `required_role=USER`(Mandate 최종 승인)는 부서가 아니라 사람이 결정하므로 `actor_user_id`가 필수다(없으면 400 `MissingActorUserError`).
- 승인 대상(`approval_id`)을 찾으려면 먼저 `GET /governance/v1/approvals?object_type=MANDATE_VERSION&object_id={mandate_version_id}`로 조회한다. `mandate_version_id`는 §2.1 응답에는 없으므로, Case Timeline(`GET .../cases/{case_id}/timeline`)의 최초 이벤트 `payload.mandate_version_id`에서 얻는다.
- 이미 결정된 승인을 다시 결정하면 409 `AlreadyDecidedError`. 만료된 승인이면 409 `ApprovalExpiredError`.

**사용자 재승인은 이 endpoint를 `required_role=USER`로 호출한다.** Risk/QA 결정은 Mandate 화면 소관이 아니다 — Risk Center·QA 화면이 각자 `required_role=RISK`/`QA`로 호출한다(권한 분리, UNIFIED_DOMAIN_API_SPEC §8).

## 6. 값 범위·상호 모순 검증

전부 `policy.py`/`service.py`/`lifecycle.py`의 결정론 코드가 판정한다. BFF·Frontend는 재판정하지 않는다(CLAUDE.md 개발 원칙 2). `objective_text`/`objective`는 사용자 자유 입력을 그대로 저장할 뿐 LLM이 개입하지 않는다.

## 7. 멱등성 — 실제로는 부분적으로만 지켜진다

UNIFIED_DOMAIN_API_SPEC §3.2는 모든 POST Command에 `Idempotency-Key`를 요구하지만, **실제 구현은 다음과 같은 차이가 있다.**

| Route | 실제 멱등 보장 |
|---|---|
| `POST .../versions`(propose) | `content_hash` unique 제약만 있다. `idempotency_key` 파라미터 자체가 없다 — 같은 요청을 네트워크 재시도로 두 번 보내도 `content_hash`가 같으면 두 번째는 `MANDATE_DUPLICATE_CONTENT`로 **거절**된다(재반환이 아니라 에러). Frontend는 제출 버튼을 이중 클릭 방지(disable on submit)로 방어해야 한다 |
| `POST .../change-requests` | 위와 동일한 한계 + Case까지 만든다. **재시도 시 두 번째 호출은 실패한다**(첫 Version의 `content_hash`가 이미 있으므로). 재시도 UX는 "실패 시 새 제출로 취급하지 말고 사용자에게 알린다" |
| `POST .../versions/{version}/activate` | `idempotency_key` 없음. 이미 활성화된 Version에 다시 호출하면 `lifecycle.py`가 자연스럽게 별개 흐름을 타지 않고 상태를 그대로 반환하는지는 별도 검증 필요(§10) |
| `POST /governance/v1/cases` | `idempotency_key` 선택 필드 있음(`case_events.idempotency_key` unique) |
| `POST /governance/v1/approvals` | 요청 자체엔 없지만 `(object_type, object_id, required_role)` unique 제약으로 재요청 시 기존 건을 그대로 반환한다(진짜 멱등) |

**이 표는 UNIFIED_DOMAIN_API_SPEC §3.2 원칙과의 실제 괴리를 기록한 것이다** — Route를 새로 추가하는 작업(§0 BFF 연결)에서 `idempotency_key`를 보강할지 결정이 필요하다(§10).

## 8. 에러 코드 — 실제 `exception_handler` 목록

`app.py`에 등록된 실제 핸들러 기준(Mandate 화면이 마주칠 수 있는 것만 추림).

| `error_code` | HTTP | 발생 조건 |
|---|---|---|
| `MANDATE_CURRENCY_MISMATCH` | 400 | `risk_bounds.currency` ≠ Fund `base_currency` |
| `MANDATE_CONTRADICTORY_BOUNDS` | 400 | `ValueError` 전반 — 상호 모순(비중 포함관계, 거래시간 역전, 자산 교집합 등)과 동일 content_hash 재제출 둘 다 이 코드로 뭉뚱그려진다(§10 세분화 필요) |
| `FUND_NOT_FOUND` | 404 | Fund 기준 통화를 조회할 수 없음 |
| `UnauthorizedDeciderError` | 403 | `actor_department`가 `required_role`과 안 맞는 승인 결정 시도(예: CEO Office가 RISK 승인 결정) |
| `ApprovalExpiredError` | 409 | 만료된 승인 결정 시도 |
| `AlreadyDecidedError` | 409 | 이미 결정된 승인 재결정 시도 |
| `MissingActorUserError` | 400 | `required_role=USER` 결정인데 `actor_user_id` 없음 |
| `IllegalCaseTransition` | 409 | 허용되지 않는 Case 상태 전이 |
| `CaseAlreadyResolvedError` | 409 | RESOLVED/CANCELLED Case에 `advance()` 재호출 |
| `NotAMandateChangeCaseError` | 404 | `case_id`가 없거나 `case_type != MANDATE_CHANGE` |
| `ReviewApprovalMissingError` | 500 | `submit()`이 만들었어야 할 RISK/QA 승인 행이 없음(데이터 정합성 문제 — 정상 흐름에서는 발생하지 않아야 함) |
| `MANDATE_PERSISTENCE_ERROR` | 409 | Postgres Repository 저장 실패(`MandatePersistenceError`) |
| `GOVERNANCE_EVENT_BUS_ERROR` | 503 | Redis 발행 실패 — **주의**: 실제로는 이 에러가 요청을 막지 않는다(`_publish_governance_event`는 best-effort). 이 핸들러는 다른 예외 경로용으로 남아 있을 뿐 Mandate 저장 성공 여부와 무관 |

공통 에러 봉투 형식(`error_code`/`message`/`detail`/`trace_id`)은 UNIFIED_DOMAIN_API_SPEC §3.4와 대체로 같지만, `request_id` 필드는 실제 응답에 없다(추가하려면 §10).

## 9. Event 발행

활성화·제안 성공 시 `governance-api`가 (Redis가 설정돼 있으면) `governance.mandate.changed.v1`을 `hf:governance` Stream에 발행한다. `action` 필드로 `PROPOSED`/`ACTIVATED`/단계(`FAST_APPLIED` 등)를 구분한다. **Redis 미설정이거나 발행 실패해도 Mandate 저장 자체는 실패하지 않는다**(Postgres가 이미 Canonical, Event는 부가 채널 — `_publish_governance_event` 설계 원칙). notification-worker는 이 Event를 "비-알림 Event"로 인식해 조용히 넘긴다(`_KNOWN_NON_NOTIFICATION_EVENTS`) — 현재 Mandate 변경은 CEO Notification 채널로 알림을 만들지 않는다.

## 10. 구현 상태와 열린 질문

| 항목 | 상태 |
|---|---|
| `policy.py`/`service.py`/`lifecycle.py` 결정론 로직과 자체 점검 | ✅ 완료 |
| `change_workflow.py` HITL 오케스트레이션(UC-1~7) + 실 DB 통합 검증 | ✅ 완료 |
| `departments/00-ceo-office/api/app.py` governance-api (Mandate·Case·Approval·Committee·Escalation) | ✅ 구현, FastAPI TestClient 검증 |
| PostgreSQL Repository 연결(`postgres_repository.py` 등) | 🟡 조회 경로는 실 DB 검증 완료. Mandate 쓰기 경로 일부(`owner_user_id` FK)는 `auth.users` 0건이라 미검증 |
| `apps/api`(AI Office BFF) governance Router 등록 | 🔴 미구현 — Frontend가 실제로 부를 수 있는 경로가 아직 없다(§0) |
| `GET .../mandates/{mandate_id}/versions/{version}`(전체 policy 조회) | 🔴 미구현 — 변경 폼 초기값 채우기가 막힘(§4) |
| Frontend Mandate 화면 | 🔴 미구현 |

### 열린 질문

1. **BFF Router**: `apps/api`에 governance Router를 새로 추가할지, 아니면 `departments/00-ceo-office/api/app.py`를 그대로 별도 서비스로 두고 BFF가 프록시만 할지 — 담당(도현/영주) 결정 필요.
2. **전체 policy 조회 Route**: §4 문제를 풀 신규 Route 추가.
3. **`idempotency_key` 보강**: §7의 괴리를 어디까지 메울지 — 최소한 `change-requests`는 네트워크 재시도가 실패로 끝나므로 사용자 경험에 영향이 크다.
4. **`MANDATE_CONTRADICTORY_BOUNDS`가 두 가지 다른 원인(상호 모순 vs 중복 제출)을 같은 코드로 뭉뚱그리는 것**을 세분화할지 — Frontend §6 에러 메시지 매핑의 정확도에 영향.
5. Case 진행 상태를 Frontend가 어떻게 갱신할지 — 폴링 주기를 둘지, AI_OFFICE_FRONTEND_PLAN §5.2의 WebSocket Event(`agent.status.v1`류)에 `governance.mandate.changed.v1`을 얹어 실시간화할지.

## 11. 연계 문서

- [USER_INPUT_SCOPE_ANALYSIS.md](../01-product/USER_INPUT_SCOPE_ANALYSIS.md) — 사용자 입력 범위 현황과 미결정 안건
- [GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md](GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md) §2.1(주의: §4의 실제 응답 차이 참고)
- [UNIFIED_DOMAIN_API_SPEC.md](UNIFIED_DOMAIN_API_SPEC.md)
- `departments/00-ceo-office/api/app.py`
- `departments/00-ceo-office/src/mandate/{policy,service,lifecycle,change_workflow}.py`
- `departments/00-ceo-office/src/approval/approval.py`, `src/case/case_root.py`
- `docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md` §5.1
