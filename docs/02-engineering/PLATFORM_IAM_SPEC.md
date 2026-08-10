# Platform/IAM 구현 지침

**목표**: HR의 Access Request 승인 후, 실제 Postgres Role·Redis Namespace를 만들고 `provisioning_ref`를 발급하는 서비스
**일시**: 2026-08-07 작성, 2026-08-10 실제 코드·스키마 대조 후 전면 정정
**우선순위**: P0 (신규 채용·권한 부여의 필수 블로커)
**소유**: `platform_iam/` (저장소 최상위, 6개 투자본부·HR 어디에도 속하지 않음 — 마스터플랜 §19.4가 이 서비스를 6개 본부 소유 목록에 넣지 않았다)

> **2026-08-10 정정 고지**: 이 문서의 최초 판(2026-08-07)은 코드를 확인하지 않고 작성돼 두 가지가 틀렸다 — ① `governance.approvals`의 실제 컬럼은 `approval_type`이 아니라 `required_role`이다 ② `tool_gateway.py`는 이미 `config.yaml` 하나만 단일 출처로 삼는 구조가 완성돼 있어, 여기 새 동적 테이블(`service.agent_tool_allowlist`)을 추가하자는 최초 판의 제안은 그 원칙과 정면충돌한다. 아래는 실제 코드(`lifecycle/access.py`, `lifecycle/postgres_access_repository.py`, `api/tool_gateway.py`, `supabase/migrations/20260731000700_workforce_access_lifecycle.sql`)를 전부 대조한 뒤 다시 쓴 버전이다.

---

## 1. 개요

### 1.1 Platform/IAM의 정확한 역할 — HR이 이미 정의해둔 계약을 채우는 것

HR의 [`lifecycle/access.py`](../../departments/07-agent-workforce/lifecycle/access.py)가 이미 전체 상태기계를 구현해뒀다. 이 모듈의 헤더 주석이 Platform/IAM의 역할을 정확히 정의한다.

> *"인사팀은 **요청까지만** 한다. 실제 Identity·권한 생성은 Platform/IAM Service 만 하고, 그 결과를 `provisioning_ref`로 되받아 기록한다. 여기에 LLM은 없다."*

`workforce.access_assignments.provisioning_ref` 컬럼의 DDL 주석도 같은 말을 한다.

> *"Platform/IAM 이 발급한 외부 식별자. 인사팀이 만들지 않는다."*

즉 이미 있는 계약은:

```
HR: POST /workforce/v1/access-requests           → REQUESTED
HR: POST /workforce/v1/access-requests/{id}/approve → APPROVED (요청자≠승인자 강제)
    ↓ (여기서 지금까지 아무도 안 채웠다)
Platform/IAM: 실제 Postgres Role/Redis Namespace 생성 → provisioning_ref 발급
    ↓
HR: POST /workforce/v1/access-requests/{id}/provision (body.provisioning_ref) → PROVISIONED
```

`POST .../provision` 엔드포인트는 이미 구현돼 있지만 **호출자가 넘긴 `provisioning_ref`가 진짜인지 검증하지 않는다** — 비어있지만 않으면 통과한다(`AccessAssignment.__post_init__`). 지금까지 이 엔드포인트를 호출하는 쪽이 없어서 `APPROVED` 상태에서 아무 요청도 더 나아가지 못했다. **Platform/IAM이 만들어야 할 것은 이 빈 자리 하나다.**

### 1.2 tool_gateway.py와의 관계 — Platform/IAM은 이걸 대체하지 않는다

[`tool_gateway.py`](../../departments/01-research/api/tool_gateway.py)는 이미 완결된 시스템이다. 부서 `config.yaml`의 `agent.tool_allowlist`를 **유일한 단일 출처**로 삼고, `X-Agent-Persona` 헤더로 요청자를 식별해 매 호출마다 허용 여부를 판정한다. 이 모듈 자체의 원칙:

> *"선언의 단일 출처는 config.yaml 이다. 여기에 목록을 복제하지 않는다 — 복제하면 둘이 어긋나고, 어긋난 쪽이 조용히 이긴다."*

**Platform/IAM은 이 원칙을 어기지 않는다.** `service.agent_tool_allowlist` 같은 동적 테이블을 새로 만들어 tool_gateway가 그걸 읽게 하는 설계(최초 판의 실수)는 두 번째 단일 출처를 만드는 것이라 채택하지 않는다.

그럼 `resource_kind = TOOL`인 Access Request는 Platform/IAM이 뭘 하나? — **`workforce.agent_tool_permissions`에 해당 권한이 선언돼 있는지 확인하고 `provisioning_ref`만 발급한다.** 실제 강제는 여전히 `tool_gateway.py`가 `config.yaml`을 보고 한다. Platform/IAM의 TOOL 처리는 "이 Agent 인스턴스가 이 도구를 쓸 자격이 기록으로 남아 있다"는 사실을 증명하는 것이지, 새로운 강제 경로를 만드는 게 아니다.

### 1.3 제약 조건 (마스터플랜 4.3절)

```
HR이 자기 후보를 스스로 최종 승인할 수 없다.
  - 검증: QA 독립 검증 (Eval Runner)
  - 승인: CEO
  - 권한 생성: Platform/IAM Service (HR이 직접 하지 않음) ← 이 줄
```

`approve_request()`가 이미 요청자≠승인자를 코드로 차단한다. Platform/IAM은 그 다음 단계 — **승인자도 아니고 요청자도 아닌 제3의 실행자**로서 실제 자원을 만든다.

---

## 2. Platform/IAM의 책임 범위

### 2.1 인수 받는 입력 — `workforce.access_requests`, `APPROVED` 상태

`governance.approvals`를 직접 폴링하지 않는다. HR의 승인 흐름은 이미 `workforce.access_requests.status`에 `APPROVED`로 반영돼 있고, 그 행 자체가 필요한 정보(agent_id, resource_kind, resource_ref, environment, tool_id)를 전부 갖고 있다. `governance.approvals`는 `access_requests.approval_id` FK로 근거 추적용으로만 참조된다.

```sql
-- workforce.access_requests 실제 스키마 (20260731000700_workforce_access_lifecycle.sql)
-- 이 행의 status='APPROVED'가 Platform/IAM의 트리거다.
select request_id, agent_id, profile_version_id, resource_kind, tool_id,
       resource_ref, scope, environment, approval_id
from workforce.access_requests
where status = 'APPROVED';
```

`resource_kind`는 `TOOL` / `DATA` / `ENVIRONMENT` 셋뿐이다(DDL CHECK 제약). 최초 판의 `IDENTITY_GRANT`/`PERMISSION_REVOKE`/`MODEL_OVERRIDE`라는 `approval_type` 값은 실제 스키마에 존재하지 않는다 — 삭제한다.

### 2.2 처리 흐름

```
[1] GET /workforce/v1/access-requests?status=APPROVED  (HR API, 신규 - 아래 §3.3)
    ↓
[2] resource_kind별 분기
    - TOOL:        workforce.agent_tool_permissions에 해당 permission_id가 ACTIVE인지 확인
    - DATA:        resource_ref → GRANT 매핑표 조회 (모르는 resource_ref는 fail-closed, §3.1)
    - ENVIRONMENT: Redis Namespace 등록 (memory:agent:<agent_id>:*)
    ↓
[3] 실제 인프라 생성
    - DATA: CREATE ROLE agent_<agent_id>_<environment> (없으면) + GRANT
    - ENVIRONMENT: Redis 키 프리픽스 소유권 기록
    - TOOL: 인프라 생성 없음 (tool_gateway가 이미 강제) — 확인만
    ↓
[4] provisioning_ref 발급 (형식은 §3.1)
    ↓
[5] POST /workforce/v1/access-requests/{request_id}/provision
    { provisioning_ref, provisioned_by: "platform-iam", effective_from, tool_permission_id }
    → HR이 workforce.access_assignments에 기록, request.status = PROVISIONED
```

### 2.3 처리할 요청 타입 — `resource_kind` 3종 (실제 enum)

#### 2.3.1 TOOL

```python
# 처리:
# 1. workforce.agent_tool_permissions에서 request.tool_id + profile_version_id로 조회
# 2. status가 ACTIVE인지 확인 - 없으면 provisioning 보류(요청은 APPROVED에 남음)
# 3. provisioning_ref = f"tool-permission:{permission_id}"
# 4. 실제 강제 경로 변경 없음 - tool_gateway.py가 이미 config.yaml로 강제 중
```

#### 2.3.2 DATA

```python
# 처리:
# 1. resource_ref → GRANT 매핑표에서 스키마/권한 조회 (예: "market-api:read" → SELECT on workspace.market_data)
# 2. CREATE ROLE agent_<agent_id>_<environment> (이미 있으면 스킵, 멱등)
# 3. GRANT <매핑된 권한> ON <매핑된 대상> TO agent_<agent_id>_<environment>
# 4. provisioning_ref = f"postgres-role:agent_{agent_id}_{environment}"
```

#### 2.3.3 ENVIRONMENT

```python
# 처리:
# 1. Redis Namespace 등록: memory:agent:<agent_id>:*
# 2. provisioning_ref = f"redis-namespace:agent:{agent_id}"
```

**회수(REVOKE)는 별도 흐름이 아니다.** `POST /workforce/v1/access-assignments/{id}/revoke`는 이미 HR API에 구현돼 있고 `revocation_evidence`를 요구한다(`access.py`의 `revoke()`). Platform/IAM은 이 호출 이후 실제 `DROP ROLE`/Redis 정리를 수행하는 후속 작업만 맡는다 — 정책 판단(회수해도 되는가)은 여전히 HR·승인자의 몫이다.

---

## 3. 기술 명세

### 3.1 resource_ref → GRANT 매핑표 (fail-closed)

`tool_gateway.py`의 `ENDPOINT_SCOPES`와 정확히 같은 패턴이다 — **매핑에 없는 `resource_ref`는 '보호 안 됨'이 아니라 설정 오류**로 취급하고 provisioning을 거부한다(요청은 APPROVED 상태에 남아 재시도 가능, 조용히 실패하지 않음).

```python
# platform_iam/provisioning.py
RESOURCE_REF_GRANTS: dict[str, tuple[str, str]] = {
    # resource_ref -> (PostgreSQL 권한 verb, 대상 schema.table/스키마)
    # 초기 매핑은 비어 있다 - 실제 DATA 요청이 나올 때마다 도현님(회계·인프라)과
    # 합의해 채운다. 지어내지 않는다.
}
```

### 3.2 provisioning_ref 형식 (자원별)

| resource_kind | provisioning_ref 형식 | 실제로 만드는 것 |
|---|---|---|
| TOOL | `tool-permission:<permission_id>` | 없음 — 확인만 |
| DATA | `postgres-role:agent_<agent_id>_<environment>` | `CREATE ROLE` + `GRANT` |
| ENVIRONMENT | `redis-namespace:agent:<agent_id>` | Redis 키 프리픽스 등록 |

### 3.3 API — HR 쪽 신규 엔드포인트

```python
# departments/07-agent-workforce/api/app.py 추가분
@app.get("/workforce/v1/access-requests")
def list_access_requests(status: str | None = None):
    """Platform/IAM이 처리할 작업(status=APPROVED)을 발견하는 유일한 경로.
    Platform/IAM은 HR의 DB에 직접 접속하지 않는다 - 부서 경계는 API로 유지한다."""
```

### 3.4 platform_iam/ 패키지 구조

```
platform_iam/
  provisioning.py              # 결정론 핵심 - resource_kind별 provisioning 계획 수립 (순수 함수, I/O 없음)
  postgres_role_manager.py     # CREATE ROLE/GRANT/DROP ROLE 실행 (lazy psycopg2, postgres_access_repository.py와 동일 패턴)
  redis_namespace_manager.py   # Redis Namespace 등록/정리 (lazy redis)
  service.py                   # HR API 폴링 -> provisioning.py 계획 -> 실행 -> HR API에 provisioning_ref 콜백
```

**HR 부서(`departments/07-agent-workforce/`) 안에 두지 않는 이유**: §1.3의 권한 분리 원칙 자체가 "HR이 직접 하지 않음"을 요구한다. 같은 디렉터리에 두면 그 경계가 코드 구조로 드러나지 않는다.

---

## 4. 구현 체크리스트

### 4.1 기본 구조

- [x] `workforce.access_requests`/`access_assignments`/`agent_tool_permissions` DDL 확인 — 이미 존재(`20260731000700_workforce_access_lifecycle.sql`)
- [x] Platform/IAM 서비스 코드 위치 결정 — `platform_iam/` (저장소 최상위)
- [x] `list_requests_by_status()`를 `AccessRepository`에 추가 (HR 쪽, §3.3) — `access.py`/`postgres_access_repository.py`, `GET /workforce/v1/access-requests?status=`
- [x] 발견 메커니즘 — HR API 폴링 (`GET /workforce/v1/access-requests?status=APPROVED`). Redis Event나 Realtime은 채택하지 않음 — 기존 Repository 패턴 확장만으로 충분하고, 새 이벤트 스키마를 늘리지 않는다.

### 4.2 PostgreSQL Role 관리

- [x] `agent_<agent_id>_<environment>` Role 이름 생성 함수 — `provisioning.py` `_role_name()`, UUID 형식 검증 포함
- [x] `CREATE ROLE` 동적 실행 — 이미 있으면 스킵(멱등) — `postgres_role_manager.py` `apply_grant_plan()`(코드 완성, 실 DB 왕복은 §5.2 참고 — 미실행)
- [x] `GRANT` — `RESOURCE_REF_GRANTS` 매핑표 기반, 매핑 없으면 fail-closed — 코드·테스트 완료. **매핑표 자체는 여전히 빈 상태**(§8 FAQ, §9 P1)라 실 운영에서는 모든 DATA 요청이 거부된다
- [ ] 감사 로그 — provisioning 성공/실패 로그는 `ProvisioningOutcome`으로 호출부에 반환되지만 영속 기록(테이블/파일)은 없음

### 4.3 Redis Namespace

- [x] `memory:agent:<agent_id>:*` 등록 — `redis_namespace_manager.py` `register_namespace()`(Redis Hash 레지스트리 기록, 코드 완성·실 Redis 왕복은 미실행)
- [ ] 다른 Agent 네임스페이스와 충돌 검사 — 미구현. 현재는 등록만 하고 다른 Agent와 프리픽스가 겹치는지 검사하지 않는다
- [ ] **Redis ACL 실제 격리** — 여기 등록은 "이 Agent가 이 네임스페이스를 쓸 자격이 있다"는 기록일 뿐, 다른 Agent의 실제 접근을 막는 `ACL SETUSER`는 범위 밖(모듈 docstring에 명시, "없는 보호를 있다고 알리지 않는다")

### 4.4 오류 처리

- [x] Role 생성 실패 → 요청은 `APPROVED`에 남음 (재시도 가능, `PROVISIONED`로 넘어가지 않음) — `service.py` `_process_one()`, `test_unmapped_data_resource_stays_approved_not_provisioned`로 검증
- [x] `RESOURCE_REF_GRANTS`에 없는 `resource_ref` → 즉시 거부, 사람이 매핑 추가할 때까지 대기 — `test_data_without_grant_mapping_fails_closed`로 검증

### 4.5 보안

- [x] Role은 `NOLOGIN`으로 생성 — 이 저장소의 모든 부서 API는 Postgres에 공유 `DATABASE_URL` 하나로만 접속하고, Agent가 이 Role로 직접 로그인하는 경로는 시스템 어디에도 없다. Role은 "이 Agent가 이 자원에 접근 가능하다"는 GRANT 기록이지 접속 계정이 아니다
- [x] REVOKE 시 `DROP ROLE`까지 완전 정리 — `postgres_role_manager.py` `revoke_role()`(REASSIGN OWNED → DROP OWNED → DROP ROLE), `redis_namespace_manager.py` `revoke_namespace()`(레지스트리 삭제 + 프리픽스 스캔 삭제). 코드 완성, 실 DB/Redis 왕복은 미실행

---

## 5. 인수 기준

### 5.1 기능 — 신규 채용 E2E

```
[1] HR이 Job Profile 작성
[2] QA가 Eval 실행 (Eval Runner — 별도 블로커, EVAL_RUNNER_SPEC.md)
[3] CEO가 승인 (governance.approvals, required_role=CEO, APPROVED)
[4] HR이 access-request 생성 + 승인 (workforce.access_requests: REQUESTED → APPROVED)
[5] Platform/IAM이 감지 → 실제 Role/Namespace 생성 → provisioning_ref 발급
[6] HR이 provision 기록 (workforce.access_requests: APPROVED → PROVISIONED)
[7] activation_evidence.py가 실재성 확인 → Agent ACTIVE
```

- [ ] Step 5: Postgres Role 또는 Redis Namespace 실제 생성 확인 — **실 DB/Redis 미실행.** `tests/test_platform_iam_service.py`는 `apply_grant_plan`/`register_namespace`를 monkeypatch로 대체해 "HR API 계약을 올바르게 오가는지"만 검증했다(의도적 — 관리자 함수 자체의 SQL/Redis 명령 정확성은 `postgres_role_manager.py`/`redis_namespace_manager.py`의 `__main__` 자체 점검이 실 DB/Redis로 별도 검증하는 몫이며, `CREATE ROLE`이 클러스터 수준 작업이라 공유 개발 DB에 자동 실행하지 않았다)
- [x] Step 6: `workforce.access_assignments.provisioning_ref`가 Platform/IAM이 발급한 값과 일치 — HR API 종단 테스트(`test_data_request_end_to_end_reaches_provisioned` 등)로 확인. `PROVISIONED` 상태 전이와 `provisioning_ref` 일치까지 검증됨

### 5.2 테스트

```python
# tests/test_platform_iam_provisioning.py (결정론 로직 - DB 없이 전부 실행)
def test_data_request_maps_to_postgres_role_ref(): ...
def test_environment_request_maps_to_redis_namespace_ref(): ...
def test_tool_request_confirms_existing_permission_only(): ...
def test_unmapped_resource_ref_fails_closed(): ...  # tool_gateway 패턴과 동일
```

---

## 6. 제약사항

### 6.1 권한 분리 (마스터플랜 4.3절)

- HR이 `workforce.access_requests`를 직접 `PROVISIONED`로 바꾸지 않는다 — Platform/IAM만 `provisioning_ref`를 발급한다
- Platform/IAM이 승인 여부를 판정하지 않는다 — `APPROVED` 상태를 전제로만 동작한다
- Agent 자신이 자기 권한을 수정하지 않는다

### 6.2 실패 시 안전한 기본값

- Provisioning 실패 → 요청은 `APPROVED`에 머무름 (Agent 미배포, 재시도 가능)
- `resource_ref` 매핑 없음 → 거부, 조용히 넘어가지 않음(tool_gateway와 동일 원칙)

### 6.3 감사 추적

모든 provisioning 시도(성공/실패)를 기록한다 — 누가(`provisioned_by`), 언제, 무엇을(`resource_kind`/`resource_ref`), 결과.

---

## 7. 참고 자료

### 7.1 실제 코드 (2026-08-10 대조 완료)

- [`lifecycle/access.py`](../../departments/07-agent-workforce/lifecycle/access.py) — `provisioning_ref` 계약의 원본 정의
- [`lifecycle/postgres_access_repository.py`](../../departments/07-agent-workforce/lifecycle/postgres_access_repository.py) — 이번 구현이 그대로 따르는 Repository 패턴
- [`api/tool_gateway.py`](../../departments/01-research/api/tool_gateway.py) — TOOL 자원의 실제 강제 경로(그대로 유지)
- [`supabase/migrations/20260731000700_workforce_access_lifecycle.sql`](../../supabase/migrations/20260731000700_workforce_access_lifecycle.sql) — 실제 DDL

### 7.2 관련 문서

- [EVAL_RUNNER_SPEC.md](EVAL_RUNNER_SPEC.md) — 선행 블로커였으나 Platform/IAM은 독립적으로 구현 가능(2026-08-10 결정)
- [WORKER_ROLE_BOUNDARIES.md](WORKER_ROLE_BOUNDARIES.md) — HR 직원 재활성화 조건에서 Platform/IAM 참조

---

## 8. FAQ

### Q. Platform/IAM이 꼭 필요한가?
**A.** 네. `provision()` 계약이 이미 코드에 있고 `provisioning_ref`를 요구하는데, 지금까지 그걸 발급하는 주체가 없어 모든 Access Request가 `APPROVED`에서 멈춰 있었다.

### Q. tool_gateway.py를 고쳐야 하나?
**A.** 아니다. 그대로 둔다. Platform/IAM은 `config.yaml` 기반 강제 경로에 손대지 않는다.

### Q. Eval Runner가 먼저 필요한가?
**A.** 아니다(2026-08-07 판의 FAQ를 정정한다). Eval Runner는 **Candidate 평가**(HR 신규 채용 흐름의 2단계)를 막고 있고, Platform/IAM은 **이미 존재하는 Agent의 Access Request 승인 후 실제 자원 생성**을 막고 있다 — 서로 다른 블로커라 독립적으로 풀 수 있다. 다만 신규 채용 E2E 전체가 완성되려면 결국 둘 다 필요하다.

### Q. resource_ref → GRANT 매핑표는 누가 채우나?
**A.** 이 문서가 채우지 않는다. 실제 DATA 요청이 나올 때 도현님(회계·인프라 담당)과 협의해 스키마별로 채운다 — tool_gateway의 `ENDPOINT_SCOPES`도 같은 방식으로 커졌다(§7.1 코드의 커밋 이력 참고).

---

## 9. 우선순위와 일정

**P0** — 2026-08-10 전부 코드·테스트 완료:
- [x] `list_requests_by_status()` + HR API 신규 엔드포인트
- [x] `platform_iam/provisioning.py` 결정론 로직 + 테스트 (`tests/test_platform_iam_provisioning.py` 9건)
- [x] `postgres_role_manager.py`/`redis_namespace_manager.py` (코드·import 자체 점검만, 실 DB/Redis 왕복 미실행)
- [x] `service.py` 폴링 루프 + 자체 점검 (`tests/test_platform_iam_service.py` 4건, HR API 종단)

**P0 잔여 — 실 인프라 검증** (코드는 있으나 아직 아무도 실행 확인 안 함):
- [ ] `postgres_role_manager.py` 실 DB 왕복 (`DATABASE_URL` 있는 환경에서 `python platform_iam/postgres_role_manager.py`)
- [ ] `redis_namespace_manager.py` 실 Redis 왕복 (`REDIS_URL` 있는 환경에서 동일)
- [ ] `service.py`를 실제 HR API 서버(uvicorn 기동 상태) 대상으로 왕복

**P1**:
- [ ] `RESOURCE_REF_GRANTS` 매핑표 실제 채우기 (도현님과 협의) — **의도적으로 비어 있음.** 채우기 전까지 모든 DATA 요청은 fail-closed로 거부됨(이게 설계이지 미완성 버그가 아니다)
- [x] REVOKE 시 실제 `DROP ROLE`/Redis 정리 — 코드 완성(`revoke_role`/`revoke_namespace`), 실 DB/Redis 왕복은 위 P0 잔여 항목에 포함

**P2 (신규, 이번 구현 중 발견)**:
- [ ] `TOOL` 자원 처리 — `workforce.agent_tool_permissions` 조회 엔드포인트가 HR API에 없어 `tool_permission_id`를 못 구한다. HR이 `GET /workforce/v1/agents/{agent_id}/tool-permissions` 류를 추가하면 `service.py`의 TOOL 분기(현재 `SKIPPED` 고정 반환)를 채울 수 있다
- [ ] Redis ACL 실제 격리 — 지금은 네임스페이스 "등록"(레지스트리 기록)까지만. 다른 Agent의 접근을 실제로 막는 `ACL SETUSER`는 별도 작업

---

**작성자**: 영주 (CEO/HR)
**정정**: 2026-08-10, 코드 실측 대조 후 전면 재작성
**담당자**: 미정
**검토 대기**: 동규 (QA), 도현 (인프라·GRANT 매핑)
**승인 대기**: CEO
