# Platform/IAM 구현 지침

**목표**: Agent와 Human User의 Identity·권한을 중앙에서 관리하고, 각자가 접근 가능한 Resource를 제한하는 서비스  
**일시**: 2026-08-07  
**우선순위**: P0 (신규 채용·권한 부여의 필수 블로커)  
**소유**: 미정 (지침만 먼저 작성, 담당자는 추후 결정)

---

## 1. 개요

### 1.1 Platform/IAM의 역할

**지금까지의 문제**:
```
HR이 CEO 승인 후 "이 Agent에 이 권한 줘" 요청
    ↓ (누가 받는가?)
    ↓ (누가 실제로 DB 권한을 설정하는가?)
    ↓ (누가 Identity를 생성하는가?)
    ↓ ⚠️ 아무도 처리 안 함 → 권한 부여 불가
```

**Platform/IAM이 해야 할 일**:
```
governance.approvals에 "Agent A에 도구 X 권한 줌" 기록
    ↓
Platform/IAM이 그 기록을 보고 실행:
  1. Agent A를 위한 Database Role 생성 (Schema X 접근 권한)
  2. Memory Namespace 초기화 (독립 context)
  3. tool_gateway에서 검증 가능하도록 설정
    ↓
Agent A가 도구 X를 부르면 tool_gateway가 "OK" 반환
```

**Platform/IAM은 무엇인가 (우리 프로젝트에서)**:
- 외부 클라우드 IAM (AWS/GCP/Azure) 아님
- 내부 서비스 Identity 매니저
- 각 Agent/User가 독립된 권한 경계를 가지도록 관리
- Supabase + PostgreSQL Role + Redis를 조합

---

### 1.2 제약 조건 (마스터플랜 4.3절)

```
HR이 자기 후보를 스스로 최종 승인할 수 없다.
  - 검증: QA 독립 검증 (Eval Runner)
  - 승인: CEO
  - 권한 생성: Platform/IAM Service (HR이 직접 하지 않음) ← 이 줄
```

**왜 이렇게 분리하나?**
- **권한 상승 방지**: HR이 자기 부서원에게 무제한 권한을 주지 못하도록
- **감사 추적**: 누가 언제 누구에게 뭘 줬는지 기록
- **비활성화 보호**: 나갈 때 권한을 깔끔하게 회수할 수 있어야 함

---

## 2. Platform/IAM의 책임 범위

### 2.1 인수 받는 입력

`governance.approvals` 테이블에 기록된 승인:

```sql
-- CEO가 입력한 레코드
INSERT INTO governance.approvals (approval_id, requester_id, approval_type, object_type, object_id, decision, created_at)
VALUES (
  'uuid-001',
  'hr-department',  -- HR이 요청함
  'IDENTITY_GRANT',
  'AGENT_PROFILE_VERSION',
  'profile-version-001',  -- 이 Agent에
  'APPROVED',  -- CEO가 승인
  now()
);

-- Platform/IAM이 읽어야 할 정보:
-- - 이 Agent는 누구인가? (workforce.agent_profiles.agent_id)
-- - 뭘 해야 하나? (approval_type)
-- - 도구·권한·스킬 목록은? (workflow.agent_profile_versions.tool_allowlist)
```

### 2.2 처리 흐름

```
[1] governance.approvals 폴링
    ↓
[2] approval_type 확인 (IDENTITY_GRANT / PERMISSION_REVOKE 등)
    ↓
[3] 해당 Agent의 프로필 로드
    - agent_id
    - tool_allowlist (뭘 쓸 수 있나)
    - skill_bundle (어떤 LangGraph)
    - model (Ollama qwen3:1.7b 또는 다른 모델)
    ↓
[4] Database Identity 생성
    - CREATE ROLE agent_<agent_id>_prod
    - GRANT SELECT ON workspace.* TO agent_<agent_id>_prod
    - (tool_allowlist에 따라 권한 세분화)
    ↓
[5] Memory Namespace 할당
    - Redis 키 범위: memory:agent:<agent_id>:*
    - 다른 Agent는 접근 불가
    ↓
[6] Tool Gateway 설정 업데이트
    - tool_gateway.py가 읽는 메타데이터 갱신
    - X-Agent-Persona: agent_id → tool_allowlist 매핑
    ↓
[7] 감사 기록 (선택)
    - 누가 / 언제 / 뭘 / 결과 기록
    ↓
[8] HR 부서에 완료 알림 (workforce.identity_created 이벤트)
```

### 2.3 처리할 요청 타입

#### 2.3.1 IDENTITY_GRANT (신규 채용)

```python
{
  "approval_id": str,
  "approval_type": "IDENTITY_GRANT",
  "object_type": "AGENT_PROFILE_VERSION",
  "object_id": str,  # profile_version_id
  "decision": "APPROVED",
  "decision_by": str,  # "ceo-agent"
  "created_at": timestamp
}

# 처리:
# 1. workforce.agent_profile_versions[object_id]에서 tool_allowlist 읽기
# 2. CREATE ROLE agent_<agent_id>_prod (처음 이 Agent를 본다면)
# 3. GRANT 권한 설정
# 4. memory:agent:<agent_id>:* Redis 범위 초기화
# 5. tool_gateway 메타 갱신
```

#### 2.3.2 PERMISSION_REVOKE (비활성화·분리)

```python
{
  "approval_id": str,
  "approval_type": "PERMISSION_REVOKE",
  "object_type": "AGENT_PROFILE_VERSION",
  "object_id": str,
  "decision": "APPROVED",
  "revocation_reason": "AGENT_DEACTIVATED" / "LEFT_ORG" / "SECURITY_INCIDENT",
  "created_at": timestamp
}

# 처리:
# 1. REVOKE 권한 제거
# 2. memory:agent:<agent_id>:* Redis 데이터 아카이브 (감사용)
# 3. DROP ROLE agent_<agent_id>_prod (마지막 Permission이라면)
# 4. tool_gateway 메타 갱신 (blocked: true)
```

#### 2.3.3 MODEL_OVERRIDE (모델 변경)

```python
{
  "approval_type": "MODEL_OVERRIDE",
  "object_type": "AGENT_PROFILE_VERSION",
  "object_id": str,
  "new_model": "qwen2.5:14b",  # Ollama 모델명
  "created_at": timestamp
}

# 처리:
# 1. $HOME/.hermes/profiles/<agent_id>/config.yaml의 model 필드 갱신
# 2. Hermes에 신호 전송 (모델 재로드)
```

---

## 3. 기술 명세

### 3.1 DB 스키마

```sql
-- governance.approvals: 모든 승인 기록 (마스터플랜 4.4절)
CREATE TABLE governance.approvals (
  approval_id UUID PRIMARY KEY,
  requester_id TEXT NOT NULL,      -- 누가 요청 (e.g., "hr-department")
  approval_type TEXT NOT NULL,     -- IDENTITY_GRANT / PERMISSION_REVOKE / etc
  object_type TEXT NOT NULL,       -- AGENT_PROFILE_VERSION / STRATEGY_VERSION
  object_id UUID NOT NULL,         -- 무엇을 (profile_version_id / strategy_id)
  decision TEXT NOT NULL,          -- APPROVED / REJECTED
  decision_by TEXT,                -- CEO인지 다른 누구인지
  notes TEXT,                      -- "Tool X 권한 추가" 등 설명
  created_at TIMESTAMPTZ DEFAULT now(),
  CHECK (decision IN ('APPROVED', 'REJECTED')),
  UNIQUE(object_type, object_id, approval_type)  -- 같은 대상에 같은 타입 중복 방지
);

-- PostgreSQL Role 생성 (SQL로 실행, Platform/IAM이 관리)
-- 각 Agent별 1개 Role
-- CREATE ROLE agent_<agent_id>_prod WITH LOGIN PASSWORD '<secure_pw>';
-- GRANT SELECT ON workspace.research_data TO agent_<agent_id>_prod;
-- GRANT SELECT, INSERT ON workspace.research_backtest TO agent_<agent_id>_prod;
-- (tool_allowlist에 따라 다름)

-- Redis: Memory Namespace
-- Key pattern: memory:agent:<agent_id>:sessions:<session_id>
-- Key pattern: memory:agent:<agent_id>:context:<trace_id>
-- 각 Agent는 자신의 agent_id 범위만 접근 가능 (Redis ACL로 강제)
```

### 3.2 Supabase Row Level Security (선택사항)

```sql
-- Agent가 자신의 정보만 볼 수 있게
ALTER TABLE workforce.agent_profile_versions ENABLE ROW LEVEL SECURITY;

CREATE POLICY agent_can_read_own_profile ON workforce.agent_profile_versions
  FOR SELECT
  USING (agent_id = current_user_id());  -- JWT의 agent_id와 매칭

-- tool_gateway.py가 사용할 메타 테이블
CREATE TABLE IF NOT EXISTS service.agent_tool_allowlist (
  agent_id TEXT PRIMARY KEY,
  tool_allowlist TEXT[] NOT NULL,  -- ["workspace.research_data.read", ...]
  forbidden_tools TEXT[],          -- (선택) ["db.write_production", ...]
  updated_at TIMESTAMPTZ DEFAULT now()
);
```

### 3.3 API/함수

Platform/IAM이 제공할 것:

```python
# departments/06-ai-qa-audit/platform_iam.py (또는 별도 서비스)

async def grant_identity(
    agent_id: str,
    profile_version_id: str,
    tool_allowlist: list[str],
    approval_id: str  # governance.approvals.approval_id
) -> IdentityGrantResult:
    """
    Agent에게 Identity를 부여한다.
    
    1. PostgreSQL Role 생성 (첫 기부여라면)
    2. tool_allowlist에 따라 GRANT 권한 설정
    3. Redis Namespace 초기화
    4. service.agent_tool_allowlist 메타 갱신
    5. governance.approvals에 완료 기록
    """
    pass

async def revoke_identity(
    agent_id: str,
    approval_id: str,
    reason: str = "AGENT_DEACTIVATED"
) -> IdentityRevokeResult:
    """
    Agent의 모든 권한을 회수한다.
    """
    pass

def get_agent_tool_allowlist(agent_id: str) -> list[str]:
    """
    Agent의 현재 도구 허용 목록을 반환한다.
    
    tool_gateway.py가 이 함수를 호출해 X-Agent-Persona 헤더 검증.
    """
    pass

async def update_agent_model(
    agent_id: str,
    new_model: str,
    approval_id: str
) -> ModelUpdateResult:
    """
    Agent의 실행 모델을 변경한다.
    """
    pass
```

### 3.4 tool_gateway.py와의 연계

현재 tool_gateway.py (research-api):

```python
@app.post("/research-api/v1/...")
async def some_endpoint(headers: dict):
    persona = headers.get("X-Agent-Persona")
    
    # Platform/IAM의 메타를 조회
    tool_allowlist = platform_iam.get_agent_tool_allowlist(persona)
    
    if tool_allowlist is None:
        return HTTPException(403, "Unknown agent")  # Fail-closed
    
    if requested_tool not in tool_allowlist:
        return HTTPException(403, "Tool not allowed")  # Fail-closed
```

**Platform/IAM의 책임**:
- service.agent_tool_allowlist 테이블 최신 유지
- governance.approvals가 업데이트되면 즉시 반영 (또는 캐시 TTL 짧게)

---

## 4. 구현 체크리스트

### 4.1 기본 구조

- [ ] governance.approvals 테이블 확인 (DDL 존재함)
- [ ] Platform/IAM 서비스 코드 위치 결정
  - [ ] 옵션 1: departments/06-ai-qa-audit/platform_iam.py (QA 담당)
  - [ ] 옵션 2: orchestration/platform_iam.py (중앙)
  - [ ] 옵션 3: 별도 마이크로서비스 (미래)
- [ ] service.agent_tool_allowlist 메타 테이블 생성
- [ ] 폴링 또는 Event-Driven 메커니즘 선택
  - [ ] 옵션 A: governance.approvals 5초마다 폴링
  - [ ] 옵션 B: HR이 Redis Event 발행 (workforce.identity_request.v1)
  - [ ] 옵션 C: Supabase Realtime 구독

### 4.2 PostgreSQL Role 관리

- [ ] Agent 이름 → Role 이름 매핑 함수
- [ ] CREATE ROLE 동적 실행
  - [ ] 안전한 암호 생성 (SecretManager 통합)
  - [ ] 원자성 보장 (이미 존재하면 스킵)
- [ ] GRANT/REVOKE 권한 설정
  - [ ] tool_allowlist 파싱
  - [ ] 권한 → PostgreSQL GRANT 매핑 테이블
- [ ] 감사용 로그 기록 (누가 언제 뭘 했는지)

### 4.3 Redis Namespace

- [ ] Memory Namespace 범위 정의
  - [ ] 패턴: `memory:agent:<agent_id>:*`
- [ ] Redis ACL 설정 (연결 시)
  - [ ] 각 Agent는 자신의 범위만 읽기/쓰기
  - [ ] Admin (Platform/IAM)은 모든 범위 접근
- [ ] 초기화 스크립트
  - [ ] Session store 준비
  - [ ] Context cache 준비

### 4.4 메타 데이터 관리

- [ ] service.agent_tool_allowlist 테이블 INSERT/UPDATE
- [ ] tool_gateway.py가 읽을 캐시 또는 API 제공
  - [ ] 캐시 TTL: 60초 (빠른 반영)
  - [ ] 또는 Supabase Realtime (실시간)
- [ ] governance.approvals 변경 감지
  - [ ] 폴링: `SELECT * FROM governance.approvals WHERE processed = false`
  - [ ] Event: Redis PubSub 또는 Supabase webhook

### 4.5 오류 처리

- [ ] Role 생성 실패 → 재시도 로직
- [ ] 권한 설정 실패 → 롤백 (이미 만든 Role 삭제)
- [ ] Redis 연결 실패 → Fallback (메모리 저장소)
- [ ] 감사 기록 쓰기 실패 → 경고 (권한 부여는 계속)

### 4.6 보안

- [ ] Password는 SecretManager에 저장 (코드에 하드코딩 금지)
- [ ] tool_allowlist 검증 (XSS/Injection 방지)
- [ ] REVOKE 시 완전 정리 (좀비 Role 방지)
- [ ] 감사 로그는 Append-only

---

## 5. 인수 기준

### 5.1 기능

**신규 채용 E2E**:
```
[1] HR이 Job Profile 작성
[2] QA가 Eval 실행 (Eval Runner)
[3] CEO가 승인 (governance.approvals에 INSERT)
[4] Platform/IAM이 감지 → Identity 생성
[5] HR이 activation_evidence.py 게이트 통과 확인
[6] Agent ACTIVE 배포
```

- [ ] Step 3: governance.approvals에 IDENTITY_GRANT 레코드 존재
- [ ] Step 4: Platform/IAM이 레코드 감지
  - [ ] PostgreSQL Role agent_<agent_id>_prod 생성 확인
  - [ ] service.agent_tool_allowlist 메타 갱신 확인
- [ ] Step 5: activation_evidence.py가 권한 실재성 검증
- [ ] tool_gateway.py: X-Agent-Persona 헤더로 권한 검증

**Agent 개선 E2E**:
```
[1] HR이 프롬프트 변경안 작성
[2] QA가 Champion 대비 Eval
[3] CEO 승인
[4] Platform/IAM이 Revision → Champion 권한 교체 (같은 agent_id)
[5] HR이 Shadow 배포
```

- [ ] 같은 agent_id는 Role 재생성 안 함 (중복 CREATE ROLE 방지)
- [ ] tool_allowlist 변경만 GRANT/REVOKE

### 5.2 테스트

```python
def test_identity_grant_workflow():
    """신규 채용 권한 부여"""
    # governance.approvals에 APPROVED 기록
    # Platform/IAM 폴링/감지
    # PostgreSQL Role 생성 확인
    # tool_allowlist 메타 갱신 확인
    pass

def test_identity_revoke_workflow():
    """Agent 비활성화"""
    # PERMISSION_REVOKE 기록
    # 권한 제거 확인
    # Redis Namespace 정리 확인
    pass

def test_tool_gateway_authorization():
    """tool_gateway.py 권한 검증"""
    # X-Agent-Persona 헤더
    # service.agent_tool_allowlist에서 조회
    # Tool 호출 허용/거부 검증
    pass

def test_redis_namespace_isolation():
    """Redis Namespace 격리"""
    # Agent A: memory:agent:a:* 접근 가능
    # Agent B: memory:agent:b:* 접근 불가
    # Admin: 모든 범위 접근 가능
    pass
```

---

## 6. 제약사항

### 6.1 권한 분리 (마스터플랜 4.3절)

**절대 금지**:
- HR이 governance.approvals 직접 INSERT (CEO만 승인)
- Platform/IAM이 승인 판정 (CEO만 판정)
- Agent 자신이 자기 권한 수정 (Platform/IAM만 수행)

### 6.2 실패 시 안전한 기본값

- Identity 생성 실패 → Agent는 배포되지 않음 (비활성 유지)
- 권한 설정 실패 → 보수적 거부 (기본값: 아무것도 안 함)
- Redis 실패 → 메모리 Namespace 대체 (감사 기록 없지만 동작)

### 6.3 감사 추적

모든 작업 기록:
- 누가 (requester_id)
- 언제 (timestamp)
- 무엇을 (approval_type, object_id)
- 결과 (성공/실패)

---

## 7. 참고 자료

### 7.1 마스터플랜 관련 섹션

- [Master Plan 4.3절](../HEDGE_FUND_MASTER_PLAN.md) — 권한 분리 원칙
- [Master Plan 4.4절](../HEDGE_FUND_MASTER_PLAN.md) — governance.approvals 테이블

### 7.2 현재 코드

**이미 존재하는 것**:
- `governance.approvals` DDL (supabase/migrations/)
- `tool_gateway.py` (departments/01-research/api/tool_gateway.py)
- `workspace.*` Schema (supabase/migrations/)

**작성할 것**:
- Platform/IAM 서비스 (위치 미정)
- service.agent_tool_allowlist 메타 테이블

### 7.3 관련 문서

- [HR 신규 채용 E2E](EVAL_RUNNER_SPEC.md) — Eval Runner의 출력
- [HR 개선 E2E](../../orchestration/workflows/agent-evolution.yaml)
- [Tool Gateway 권한 검증](../../departments/01-research/api/tool_gateway.py)

---

## 8. FAQ

### Q. Platform/IAM이 꼭 필요한가?
**A.** 네. 마스터플랜 4.3절이 "실제 Identity와 권한 생성은 Platform/IAM Service만" 했다. CEO가 승인해도 실제 권한이 없으면 Agent가 도구를 부를 수 없다.

### Q. 외부 IAM (AWS IAM, GCP IAM)을 써도 되나?
**A.** 가능하지만 나중 문제다. 지금은 내부 Supabase + PostgreSQL Role + Redis로 충분하다. 나중에 AWS 마이그레이션 시 이 레이어를 바꾸면 된다.

### Q. Redis가 없으면 어떻게 되나?
**A.** Memory Namespace를 프로세스 메모리에 저장하면 되지만, 그럼 Agent가 재시작되면 context가 사라진다. 현재는 Redis 필수.

### Q. 언제 구현해야 하나?
**A.** Eval Runner 이후. Eval이 없으면 Identity를 줄 Agent가 없다.

---

## 9. 우선순위와 일정

**P0 (Critical)**:
- [ ] service.agent_tool_allowlist 메타 테이블: 1일
- [ ] governance.approvals 폴링 루프: 2-3일
- [ ] PostgreSQL Role 생성: 2-3일
- [ ] tool_gateway.py 연계: 1-2일

**P1 (High)**:
- [ ] Redis ACL 설정: 2-3일
- [ ] 감사 로그: 1-2일

**P2 (Normal)**:
- [ ] 모델 변경 (MODEL_OVERRIDE): 2-3일

---

**작성자**: 영주 (CEO/HR)  
**담당자**: 미정  
**검토 대기**: 동규 (QA)  
**승인 대기**: CEO

