# Governance·Workforce Domain API 설계서

> 작성: 영주님 (CEO Office / Agent Workforce Domain Owner) · 작성일: 2026-07-31
> 상위 계약: [MINIMUM_SERVICE_UNIT_SPEC.md](../01-product/MINIMUM_SERVICE_UNIT_SPEC.md) §5/§8/§11,
> [TEAM_YOUNGJU_CEO_HR_GUIDE.md](../05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md) §8,
> [TECH_STACK_DECISIONS.md](TECH_STACK_DECISIONS.md) 307행
>
> 범위: `governance-api`·`workforce-api`·`reporting-api`의 **입출력 데이터 타입**. Transport는 확정하지 않는다(§0).
> 상태 구분은 §7 표를 따른다.

---

## 0. 통신 경계

| 경계 | 방식 | 정의 위치 |
|---|---|---|
| 부서 내 (intra-department) — 같은 부서 Specialist ↔ 자기 부서 API | 부서 프로세스 안. LangGraph 직원끼리는 **State 객체**로 넘기고, 결정론 Service 호출만 API 경로를 탄다 | §2.4 (CEO Office), §3.6 (인사팀) |
| 부서 간 (inter-department) — 다른 본부와 주고받음 | **API/MCP** 동기 호출, Domain Event, 비동기 Handoff | §5 |

근거: CLAUDE.md "부서는 Hermes, 부서 안 직원은 LangGraph" / TECH_STACK 307행 "Hermes를 Domain Backend
Python Environment에 직접 설치하지 않는다. 독립 Image와 API/MCP 경계로 통신한다".

같은 Pydantic 모델이 부서 안에서는 LangGraph State의 필드가 되고, 부서 밖으로 나갈 때는 Request/Response
body가 된다. **Transport를 바꿔도 타입은 바뀌지 않으므로** 타입을 먼저 고정한다.

### 재사용하는 타입 — API 레이어에서 새로 만들지 않는다

| 용도 | 타입 | 위치 |
|---|---|---|
| Mandate 정책 | `MandatePolicy`, `RiskBounds`, `UniversePolicy`, `ApprovalRules` | `departments/00-ceo-office/src/mandate/policy.py` |
| Mandate Version·방향 판정 | `MandateVersionRow`, `ChangeDirection`, `VersionResult` | `departments/00-ceo-office/src/mandate/service.py` |
| 개선 후보 | `ImprovementCandidate`, `CandidateStatus`, `TargetType`, `RiskClass` | `departments/07-agent-workforce/improvements/candidate.py` |
| 후보 전이 | `CandidateEvent`, `Approval` | `departments/07-agent-workforce/improvements/workflow.py` |
| Event Envelope | MSU_SPEC §8 봉투 | — |
| 멱등키·에러 봉투 규약 | RISK_QA_SPEC §1.3/§1.4 | — |

---

## 1. 공통 규약

### 1.1 경로와 버전

- Case 종속: `/investment-cases/{case_id}/...` (MSU_SPEC §11).
- 부서 단독 자원: `/governance/v1/...`, `/workforce/v1/...`, `/reporting/v1/...`.
- `v1`은 Path Version이다. `mandate_versions.version`·`agent_profile_versions.version`과 **다른 축이며 섞지 않는다.**

### 1.2 인증

- 호출자는 짧은 수명의 Service Token을 쓴다. Identity는 TEAM_YOUNGJU §7.1 (`svc_ceo_orchestrator`,
  `svc_workforce_registry`, `svc_workforce_analytics`, `svc_reporting`).
- Frontend·Browser는 이 API를 직접 호출하지 않는다. FastAPI BFF가 유일한 진입점이다 (AI_OFFICE_FRONTEND_PLAN §6).
- `governance`/`workforce` 내부 Table을 Data API로 노출하지 않는다 (TEAM_YOUNGJU §7.2).
- CEO/HR Agent에 `service_role` Credential을 주지 않는다.

### 1.3 멱등키

| 대상 | 키 | 강제 위치 |
|---|---|---|
| Mandate Version 생성 | `content_hash` | `compute_content_hash()` + DDL `unique(mandate_id, content_hash)` |
| Agent Profile Version 생성 | `artifact_hash` | DDL `unique(agent_id, artifact_hash)` |
| 개선 후보 전이 | `(candidate_id, sequence)` | DDL `unique(candidate_id, sequence)` |
| 상태 변경 Command | `idempotency_key` + `expected_version` | AI_OFFICE_FRONTEND_PLAN §6 봉투 |

### 1.4 에러 봉투

```json
{
  "error_code": "MANDATE_CONTRADICTORY_BOUNDS",
  "message": "max_instrument_weight 는 max_sector_weight 보다 클 수 없다",
  "detail": {"max_instrument_weight": "0.4", "max_sector_weight": "0.3"},
  "trace_id": "..."
}
```

`error_code`는 기존 예외·Enum 이름을 그대로 쓴다 (`SelfApprovalError`, `MissingEvidenceError`, `IllegalTransition`).

### 1.5 권한 불변식 — 엔드포인트가 강제한다

- CEO는 주문 제출·Risk 승인·Ledger 수정·NAV 확정·Finding 종결 **엔드포인트를 갖지 않는다.**
- `change_status`가 `ACTIVE`로 갈 때 QA Eval 참조와 CEO 승인을 **필수 인자로 요구**한다 (인사팀 자기활성화 금지).
- 개선 후보 승인은 작성자 ≠ 승인자를 강제한다.
- `request_access`는 **요청 기록까지만** 하고 Provisioning을 수행하지 않는다 (Platform/IAM 전용).
- Risk/QA의 거부를 CEO가 우회·해제할 수 없다.

---

## 2. governance-api

### 2.1 Mandate

| Method/Path | 감싸는 것 | 상태 |
|---|---|---|
| `GET /governance/v1/mandates/{fund_id}/current` | effective 구간의 `mandate_versions` | `get_mandate` ✅ |
| `GET /governance/v1/mandates/{mandate_id}/versions/{version}` | 특정 Version | 제안 |
| `POST /governance/v1/mandates/{mandate_id}/versions` | `MandateVersionService.propose_version()` | 제안 |
| `POST /governance/v1/mandates/{mandate_id}/versions/{version}/activate` | `MandateActivationService.activate()` | 제안 |

**`get_mandate` Response**

```json
{
  "mandate_id": "uuid",
  "fund_id": "uuid",
  "version": 3,
  "status": "ACTIVE",
  "effective_from": "2026-07-30T00:00:00Z",
  "effective_to": null,
  "content_hash": "sha256...",
  "objective_text": "장기 성장",
  "objective": {"style": "growth"},
  "policy": {
    "allowed_assets": ["A005930"],
    "forbidden_assets": ["A000660"],
    "risk_bounds": {
      "base_capital": "100000000", "currency": "KRW",
      "max_instrument_weight": "0.1", "max_sector_weight": "0.3",
      "max_gross_exposure": "1.0", "max_concurrent_positions": 10,
      "max_daily_loss": "0.03"
    },
    "universe_policy": {
      "allowed_markets": ["KRX"], "trading_start": "09:00", "trading_end": "15:30"
    },
    "approval_rules": {
      "paper_order_mode": "USER_APPROVAL",
      "risk_expansion_requires_user_approval": true
    }
  }
}
```

> **기준 자본 계약** (2026-07-31 결정, §7 참고)
>
> `risk_bounds`의 비중·손실 값은 전부 **비율**이며, 그 기준 자본은 다음과 같이 정한다.
>
> - **한도 집행 기준**: 회계본부의 공식 자산 총액 `accounting.nav_runs.total_nav`. **당일 장 시작 시점 값으로
>   고정**하고 장중에 갱신하지 않는다 — 같은 주문이면 판정도 같아야 한다.
> - **`base_capital`의 역할**: Mandate가 선언한 **Paper 시작 자본**이다. 운용 첫날 회계 초기 현금의 근거로만
>   쓰고, 이후 한도 집행의 기준으로 쓰지 않는다.
> - `mandate.currency`는 저장 시점에 `accounting.funds.base_currency`와 일치하는지 governance가 검증한다.
>
> Risk Engine은 이 응답의 비율과 회계 API의 기준 자본을 **각각 조회해서** 판정한다. `base_capital`을 분모로
> 쓰지 않는다.

**`POST .../versions` Request**

```json
{
  "objective_text": "장기 성장",
  "objective": {"style": "growth"},
  "policy": { "...": "위 policy 와 동일 구조" },
  "effective_from": "2026-07-31T00:00:00Z",
  "created_by": "uuid"
}
```

**Response** — 값 범위·상호 모순 위반은 §1.4 봉투로 400.

```json
{
  "mandate_id": "uuid", "version": 4,
  "direction": "TIGHTEN|LOOSEN|NEUTRAL",
  "requires_user_reapproval": true,
  "content_hash": "sha256..."
}
```

**`POST .../activate`**

```json
{ "approval": {"approved_by": "uuid", "trace_id": "uuid", "reason": "사용자 승인"}, "at": "2026-07-31T00:00:00Z" }
```
```json
{ "activated": false, "direction": "LOOSEN", "blocked_reason": "사용자 재승인 필요: 장중 Risk 확대(LOOSEN)" }
```

> 완화(TIGHTEN)·중립은 승인 없이 즉시 활성화된다. **확대(LOOSEN)와 최초 활성화는 승인이 없으면 활성화되지 않는다.**

### 2.2 Case / Decision / Approval

| Method/Path | 상태 | 비고 |
|---|---|---|
| `POST /governance/v1/cases` | `create_case` ✅ | `governance.cases`는 전사 Case Root. 투자 Case는 MSU_SPEC §11 `POST /investment-cases`를 쓰고 여기를 복제하지 않는다 |
| `GET /governance/v1/cases/{case_id}` | 제안 | |
| `GET /governance/v1/cases/{case_id}/timeline` | 제안 | 기존 `api.get_case_timeline` RPC를 감싼다 |
| `POST /governance/v1/cases/{case_id}/decisions` | `record_decision` ✅ | |
| `POST /governance/v1/approvals` | `request_approval` ✅ | |
| `POST /governance/v1/escalations` | 제안 | |

**`create_case` Request** — 필드는 MSU_SPEC §12 `governance.cases` 스키마를 따른다.

```json
{
  "case_type": "MANDATE_CHANGE|COMMITTEE|INCIDENT|HIRING|IMPROVEMENT",
  "priority": 2,
  "owner_department": "hr-department",
  "fund_id": "uuid",
  "due_at": "2026-08-01T00:00:00Z",
  "trace_id": "uuid"
}
```

> **부서 식별자 표기(2026-08-04 확정)** — 부서를 가리키는 모든 필드(`owner_department`,
> `department`, `department_code`, `target`, `actor_department`)는 **Hermes Profile 이름**을
> 쓴다: `ceo-agent`, `research-department`, `trading-department`, `risk-management`,
> `quant-backtest-department`, `accounting-portfolio-department`, `qa-department`,
> `hr-department`.
>
> 이 문서는 이전에 대문자 표기(`AGENT-WORKFORCE`, `RISK`, `QA`)를 예시로 썼는데, 실제 코드
> 40개 파일(프론트엔드 `riskQaBridge.ts`, `apps/api/main.py`, 리스크·QA harness와 tests,
> 등록 마이그레이션)은 전부 Profile 이름을 쓰고 있었다. 대문자 표기를 쓰는 코드는 없었으므로
> 다수 쪽으로 문서를 맞췄다. 폴더 이름(`03-risk`, `06-ai-qa-audit`)은 세 번째 체계이며
> 경로 전용이다 — 데이터 식별자로 쓰지 않는다.

**`request_approval`**

```json
{
  "object_type": "MANDATE_VERSION|AGENT_PROFILE_VERSION|IMPROVEMENT_CANDIDATE|CAPITAL_ALLOCATION",
  "object_id": "uuid",
  "required_role": "CEO|RISK|QA|OWNER",
  "reason": "...",
  "expires_at": "2026-08-01T00:00:00Z",
  "idempotency_key": "uuid"
}
```
```json
{ "approval_id": "uuid", "decision": "PENDING", "required_role": "CEO", "expires_at": "..." }
```

> 만료된 `approval_id`로 활성화를 시도하면 거절한다 (TEAM_YOUNGJU §10.2).

### 2.3 위원회

| Method/Path | 상태 |
|---|---|
| `POST /governance/v1/committee/sessions` / `.../close` | `open/close_session` ✅ |
| `POST /governance/v1/committee/sessions/{session_id}/votes` | `submit_vote` ✅ |

```json
{
  "department": "risk-management|qa-department|trading-department|research-department|quant-backtest-department|accounting-portfolio-department",
  "decision": "APPROVE|CONDITIONAL|REJECT",
  "conditions": {},
  "artifact_ids": ["uuid"]
}
```

> Quorum과 Segregation of Duties 판정은 결정론적 Service가 한다. API는 투표를 기록만 하고 정족수를
> 임의로 계산해 승인 처리하지 않는다.

### 2.4 CEO Office 부서 내 통신 (intra-department)

| 호출자 (Hermes Specialist) | 호출 대상 |
|---|---|
| `executive-orchestrator` (CEO-00 + CEO-01 합친 페르소나) | `GET /governance/v1/mandates/{fund_id}/current`, `POST /governance/v1/cases`, `POST /governance/v1/cases/{case_id}/decisions`, `POST /governance/v1/approvals`, `POST /governance/v1/committee/*` |
| Mandate 변경 Workflow (LangGraph) | `POST .../versions` → `POST .../versions/{version}/activate`. 두 노드 사이는 `VersionResult`를 State로 넘기며 API를 다시 타지 않는다 |
| 값 범위·상호 모순 검증 | `policy.py` 결정론 함수 — LLM이 판정하지 않는다 |
| 활성화 게이트 | `lifecycle.py` 결정론 함수. LOOSEN·최초 활성화는 승인 없으면 차단 |

CEO는 다른 본부의 공식 수치를 **직접 계산하지 않고 조회만** 한다 (Position/PnL/NAV는 회계, Risk State는
리스크, Finding은 QA). §5.1 참고.

---

## 3. workforce-api

### 3.1 Roster / Profile

| Method/Path | 감싸는 것 | 상태 |
|---|---|---|
| `GET /workforce/v1/roster` | `agent_profiles` + `role_templates` + 현재 version | `get_roster` ✅ |
| `GET /workforce/v1/agents/{agent_id}` | Agent 상세 | 제안 |
| `POST /workforce/v1/agents/{agent_id}/profile-versions` | `agent_profile_versions` insert | `submit_profile` ✅ |
| `POST /workforce/v1/agents/{agent_id}/status` | `employment_status` 전이 | `change_status` ✅ |

**`get_roster` Response**

```json
{
  "agents": [{
    "agent_id": "uuid", "employee_code": "HR-00",
    "display_name": "agent-workforce-supervisor",
    "department_code": "hr-department", "role_code": "HR-00",
    "employment_status": "CANDIDATE|PROBATION|ACTIVE|SUSPENDED|RETIRED",
    "current_version": 1,
    "current_profile_version": {
      "profile_version_id": "uuid", "version": 1,
      "model": {"provider": "bedrock", "model_name": "claude-deep", "model_version": "proposed"},
      "memory_namespace": "workforce/hr-00",
      "status": "DRAFT|EVALUATING|APPROVED|ACTIVE|SUSPENDED|RETIRED"
    },
    "owner_user_id": "uuid", "backup_owner_user_id": null
  }]
}
```

> `model`은 **직원 개별 모델**(`agent_profile_versions.model_id`)이다. 부서 Supervisor 모델
> (`hermes/config.yaml`의 `model:`)과 다른 레이어이므로 섞지 않는다.

**`submit_profile` Request** — `agent_profile_versions` 컬럼과 1:1.

```json
{
  "model_id": "uuid",
  "prompt_artifact_path": "departments/07-agent-workforce/hermes/config.yaml#agent-workforce-supervisor",
  "skill_manifest": {"required": ["HR-01", "HR-02"]},
  "tool_allowlist": {"read": ["capacity_snapshots"], "propose": ["hiring_requests"]},
  "data_scopes": {"workforce": "read"},
  "memory_namespace": "workforce/hr-00",
  "token_budget": {"per_case_tokens": 200000, "daily_tokens": 2000000},
  "sla": {"decision_latency_hours": 24},
  "eval_requirements": {"status": "PENDING_QA", "owner": "qa-department", "required_suites": ["golden", "adversarial"]},
  "forbidden_actions": ["investment_decision", "order", "risk_approval"],
  "effective_from": "2026-07-31T00:00:00Z"
}
```

> 이 엔드포인트는 항상 **새 Version을 만든다.** 기존 Version을 수정하는 엔드포인트는 두지 않는다
> (Prompt만 바꾸고 Version 유지 금지 — TEAM_YOUNGJU §4.3).

**`change_status` Request**

```json
{
  "to_status": "PROBATION|ACTIVE|SUSPENDED|RETIRED",
  "profile_version_id": "uuid",
  "qa_eval_run_id": "uuid",
  "ceo_approval_id": "uuid",
  "reason": "...",
  "idempotency_key": "uuid"
}
```

> `to_status="ACTIVE"`일 때 `qa_eval_run_id`와 `ceo_approval_id`가 **둘 다 없으면 거절**한다.

### 3.2 Hiring

| Method/Path | 상태 |
|---|---|
| `POST /workforce/v1/hiring-requests` | `request_hire` ✅ |
| `GET /workforce/v1/hiring-requests/{request_id}` | 제안 |
| `POST /workforce/v1/hiring-requests/{request_id}/candidates` | 제안 |

```json
{
  "department_id": "uuid",
  "business_problem": "리스크 Case 큐 적체가 3주 반복",
  "evidence": {"case_ids": ["uuid"], "capacity_snapshot_ids": ["uuid"]},
  "required_capabilities": ["stress-testing"],
  "budget": {"monthly_token_budget": 1000000},
  "trace_id": "uuid"
}
```

> `trace_id`는 `not null`이다. `department_id`는 uuid이며 code가 아니다(응답에는 code를 함께 싣는다).
> `evidence`가 비면 거절한다 — TEAM_YOUNGJU §6.1 채용 판단 6단계를 건너뛰고 요청만으로 채용하지 않는다.

### 3.3 Improvement Candidate (F19)

| Method/Path | 감싸는 함수 |
|---|---|
| `POST /workforce/v1/improvements` | `insert_candidate()` |
| `GET /workforce/v1/improvements/{candidate_id}` | `load_candidate()` |
| `POST /workforce/v1/improvements/{candidate_id}/transitions` | `ImprovementWorkflow.transition()` |
| `GET /workforce/v1/improvements/{candidate_id}/events` | `events_for()` |

**생성 Request**

```json
{
  "candidate_id": "uuid",
  "author": "qa-department-hermes",
  "target_type": "SKILL|PROFILE|WORKFLOW|AGENT",
  "target_ref": "agent-citation-checker",
  "target_current_version": 3,
  "evidence_ids": ["finding-101"],
  "expected_effect": "인용 누락 오탐 감소",
  "risk_class": "LOW|MEDIUM|HIGH",
  "rollback_target_version": 3
}
```

> 근거(`evidence_ids`)나 롤백 대상이 없으면 Pydantic과 DB check 제약 양쪽에서 거절된다.

**전이 Request**

```json
{
  "to_status": "EVALUATING|SHADOW|PENDING_APPROVAL|APPROVED|REJECTED|DEPLOYED|OBSERVING|KEPT|ROLLED_BACK|RETIRED",
  "actor": "hr", "reason": "...", "at": "2026-07-31T00:00:00Z",
  "approval": {"approver": "ceo-office-hermes", "qa_eval_run_id": "uuid", "reason": "기준 통과"}
}
```

> `to_status="APPROVED"`이고 `approval.approver == candidate.author`이면 `SelfApprovalError`로 거절한다.
> 승인엔 `qa_eval_run_id`가 필수다.

### 3.4 Scorecard / Skill Gap

| Method/Path | 상태 |
|---|---|
| `GET /workforce/v1/departments/{department_code}/scorecard` | `get_department_scorecard` ✅ |
| `GET /workforce/v1/skill-gap` | `get_skill_gap` ✅ |

```json
{
  "department_code": "03-risk",
  "window": {"window_start": "2026-07-24T00:00:00Z", "window_end": "2026-07-31T00:00:00Z"},
  "capacity": {
    "arrivals": 120,
    "queue_p95_ms": "45000.0000", "duration_p95_ms": "300000.0000",
    "retry_rate": "0.02500000", "error_rate": "0.00833333", "utilization": "0.72000000"
  },
  "cost": {
    "input_tokens": 0, "output_tokens": 0,
    "model_cost": "0", "tool_cost": "0", "infra_cost": "0",
    "case_count": 120, "currency": "USD"
  },
  "quality": {"eval_score": null, "finding_count": 2, "rework_rate": 0.05}
}
```

> 단위·타입은 DDL을 그대로 따른다 — **지연은 초가 아니라 `_ms`, 재시도·오류는 건수가 아니라 `_rate`.**
> API 레이어에서 단위를 바꾸지 않는다. `numeric`은 부동소수점 오차를 피하려고 문자열로 직렬화한다.
> `quality`의 Eval 원본은 QA/감사본부 소유(`audit.eval_runs`)이며 인사팀은 Reference만 보관하고 수정하지 않는다.
> `quality_snapshots` 테이블이 없어 현재는 `performance_reviews`로 일부만 채운다(§7).

### 3.5 Access

| Method/Path | 상태 |
|---|---|
| `POST /workforce/v1/access-requests` | `request_access` ✅ |
| `GET /workforce/v1/agents/{agent_id}/access` | 제안 |

```json
{
  "agent_id": "uuid",
  "resource": {"kind": "TOOL|DATA|ENVIRONMENT", "ref": "market-api:read"},
  "scope": {"environment": "SHADOW"},
  "expires_at": "2026-08-31T00:00:00Z",
  "justification": "..."
}
```

> **요청을 기록할 뿐 권한을 부여하지 않는다.** 실제 Identity·권한 생성은 Platform/IAM Service 만 하고,
> 그 결과를 `provisioning_ref` 로 되받아 `workforce.access_assignments` 에 기록한다.
>
> 세 테이블의 역할이 다르다 — 중복 저장하지 않는다.
> `agent_tool_permissions`(가질 수 있는 권한 선언) / `access_requests`(요청·승인 절차) /
> `access_assignments`(실제 부여·회수 증거). 도구 부여는 `tool_permission_id` 로 기존 행을 가리킨다.
>
> 만료 없는 권한 요청은 만들 수 없고(`expires_at` 필수), 부여는 요청의 `expires_at` 을 넘길 수 없다.
> 회수는 `revocation_evidence` 없이 완료되지 않는다.

### 3.6 인사팀 부서 내 통신 (intra-department)

| 호출자 (Hermes Specialist) | 호출 대상 |
|---|---|
| `agent-workforce-supervisor` (HR-00) | `GET /workforce/v1/roster`, `GET .../departments/{code}/scorecard`, `POST /workforce/v1/hiring-requests` (제안만) |
| `workforce-planning-agent` (HR-01) | `GET .../scorecard`, `GET /workforce/v1/skill-gap` — 읽기 전용. 산출물(Capacity Report·Staffing Scenario)은 저장 테이블이 없어 아직 미보관(§7) |
| `profile-architect` (HR-02) | `POST /workforce/v1/agents/{agent_id}/profile-versions`, `POST /workforce/v1/improvements` (개정안) |
| `selection-performance-agent` (HR-03) | `POST /workforce/v1/improvements` (**후보 author**), `GET .../improvements/{id}/events`. Eval 원본은 `audit.eval_runs` 조회(§5.1) |
| `lifecycle-coordinator` (HR-04) | `POST /workforce/v1/agents/{agent_id}/status`, `POST /workforce/v1/access-requests` |

권한 분리는 부서 안에서도 동일하게 강제된다 — HR-03이 만든 개선 후보를 HR-03이 승인할 수 없고
(`SelfApprovalError`), HR-02가 만든 Profile Version을 인사팀 단독으로 `ACTIVE`로 올릴 수 없다
(`qa_eval_run_id` + `ceo_approval_id` 필수).

---

## 4. reporting-api

| Method/Path | 상태 |
|---|---|
| `POST /reporting/v1/reports` | `request_report` ✅ |
| `GET /reporting/v1/reports/{report_id}` | `get_report` ✅ |
| `GET /reporting/v1/reports/{report_id}/source-snapshots` | `get_source_snapshots` ✅ |

```json
{ "type": "DAILY|WEEKLY", "as_of": "2026-07-31", "fund_id": "uuid" }
```
```json
{
  "report_id": "uuid", "type": "DAILY", "as_of": "2026-07-31",
  "status": "QUEUED|RUNNING|READY|FAILED",
  "source_snapshot_ids": ["uuid"], "template_version": "v1",
  "object_path": "private://reports/...", "hash": "sha256..."
}
```

> Report는 각 본부 Snapshot ID를 포함해 숫자를 원천까지 추적할 수 있어야 한다. `svc_reporting`은 승인된
> Snapshot으로 생성만 하고 Source 숫자를 수정하지 않는다.

---

## 5. 부서 간 통신 (inter-department)

### 5.1 동기 호출 — Hot Path

주문 경로에 있지 않으므로 CEO Office·인사팀이 **다른 부서를 동기 차단(blocking)하는 호출은 없다.**
반대로 다른 부서가 우리를 동기 호출하는 경우는 아래 하나다.

```
risk-management / trading-department → GET /governance/v1/mandates/{fund_id}/current → governance
```

Risk Engine이 한도를 판정하려면 Mandate 비율이 필요하다. 이 호출은 **읽기 전용이며 판정을 대신하지 않는다** —
승인/축소/거절은 Risk Engine이 결정한다.

§2.1 기준 자본 계약에 따라 Risk Engine은 **두 곳을 각각 조회**한다. governance는 비율만 주고 기준 자본을 주지 않는다.

```
비율     ← GET /governance/v1/mandates/{fund_id}/current   (governance)
기준 자본 ← 회계 API의 당일 장 시작 시점 nav_runs.total_nav  (accounting)
```

Mandate 저장 시 governance는 `accounting.funds.base_currency`를 조회해 통화 일치를 검증한다(§2.1).
이 조회는 Mandate 생성·변경 시점에만 발생하며 주문 경로에 있지 않다.

우리가 다른 부서에서 읽어오는 것(전부 읽기 전용, 공식 API 경유):

| 대상 | 소비자 | 용도 |
|---|---|---|
| `audit-api` — `audit.eval_runs`, Finding | HR-03, HR-00 | 활성화 게이트의 `qa_eval_run_id`, 수습·비활성화 근거 |
| `portfolio-api` — Position/Cash/PnL/NAV | CEO | Daily Report, 자본 배분 |
| `risk-api` — Trading State/Breach | CEO | Incident·Escalation |
| `strategy-registry-api` | CEO | 전략 승격 심의 |
| Workflow Telemetry / Cost Ledger | HR-01 | `capacity_snapshots`·`cost_snapshots` |

> Eval 원본과 Finding은 **QA/감사본부 소유**다. 인사팀은 Reference만 보관하고 원본을 수정하지 않는다.
> CEO는 Raw Tick·전체 뉴스 본문·전체 Agent Trace를 받지 않는다 — 공식 Snapshot과 Evidence Reference만 받는다.

### 5.2 Domain Event — Case Stream

MSU_SPEC §8 Envelope을 그대로 쓴다.

```json
{
  "event_id": "EV-0001",
  "event_type": "workforce.lifecycle_changed.v1",
  "schema_version": 1,
  "case_id": "uuid",
  "trace_id": "uuid",
  "correlation_id": "uuid",
  "occurred_at": "2026-07-31T00:00:00Z",
  "producer": "workforce-registry",
  "idempotency_key": "...",
  "payload": {}
}
```

우리가 소비하는 Case Stream Event (TEAM_YOUNGJU §8.2):

```
research.packet.v1              strategy.candidate.v1
strategy.version.approved.v1    risk.breach.v1
risk.trading_state.v1           portfolio.snapshot.v1
nav.official.v1                 qa.finding.v1
incident.opened.v1              workforce.eval.v1
```

### 5.3 Domain Event — Governance/Workforce Stream (Case에 안 묶임)

Mandate 변경, 조직 Lifecycle, 정기 Report는 하나의 투자 `case_id`에 묶이지 않는다. 별도 Stream으로 발행한다.

```
governance.mandate.changed.v1        governance.case.created.v1
governance.decision.v1               governance.capital_allocation.v1
governance.escalation.v1             report.ready.v1
workforce.hiring_request.v1          workforce.profile_candidate.v1
workforce.lifecycle_changed.v1       workforce.access_request.v1
```

**소비자**

| Event | 소비 부서 | 용도 |
|---|---|---|
| `governance.mandate.changed.v1` | 리스크(한도 재적용), 트레이딩·퀀트(제약 갱신), QA(정합 검토) | 적용 Version과 `effective_from`을 함께 전달 |
| `governance.capital_allocation.v1` | 회계/포트폴리오 | 승인 Event를 소비해 Book Allocation을 반영. **CEO가 Position·Cash를 직접 수정하지 않는다** |
| `governance.escalation.v1` | 담당 본부, QA | Owner·기한 추적 |
| `workforce.profile_candidate.v1` | QA/감사 | 독립 Eval 대상 등록 |
| `workforce.lifecycle_changed.v1` | Platform/IAM, QA | 실제 Provisioning·회수 수행(우리는 요청만), 권한 변경 감사 |
| `report.ready.v1` | 사용자 알림 Adapter | |

> CEO Summary Event에 본부별 전체 Payload를 복사하지 않는다. 공식 Artifact ID, Version, 결정, 조건과 Trace만 담는다.

### 5.4 비동기 Handoff

채용 검토, Profile 개정, 정기 Scorecard처럼 즉시 응답이 필요 없는 일은 [ADR-0001](adr/0001-hermes-kanban-agent-status-bridge.md)의
Hermes kanban Task로 나른다. Task body 최소 계약:

```json
{
  "case_id": "IC-20260731-0001",
  "from_dept": "agent-workforce",
  "to_dept": "qa-department",
  "purpose": "Agent Profile Version의 Model/Prompt/Tool 권한 독립 검증",
  "input_artifact_id": "agent-profile-version:uuid",
  "required_output_schema": "EvalRun",
  "due_time": "2026-08-01T09:00:00Z",
  "priority": "P1",
  "escalation": "due_time 초과 시 agent-workforce-supervisor -> CEO"
}
```

`--parent`/`blocked`/`--verifier` 사용 규칙은 ADR-0001 §5를 따른다. **인사팀이 만든 후보의 `--verifier`는
인사팀이 될 수 없다.**

---

## 6. Frontend

Frontend는 이 API를 직접 호출하지 않는다. FastAPI BFF가 `/ui/snapshot`·`/ws/operations`로 중계한다
(AI_OFFICE_FRONTEND_PLAN §5.2).

Agent 상태(`OFFLINE|IDLE|QUEUED|RUNNING|WAITING_APPROVAL|BLOCKED|DEGRADED|ERROR`)의 소스는
[ADR-0001](adr/0001-hermes-kanban-agent-status-bridge.md)에서 제안 중이다. 승인되면 `get_roster`의
`employment_status`(고용 상태)와 kanban 기반 `agent_status`(현재 활동)를 **다른 필드로 분리해서** 제공한다.

---

## 7. 상태표

| 항목 | 상태 |
|---|---|
| API 3개 이름과 §8.1 등재 메서드 (`get_mandate`, `create_case`, `record_decision`, `request_approval`, `open/close_session`, `submit_vote`, `get_roster`, `request_hire`, `submit_profile`, `request_access`, `change_status`, `get_department_scorecard`, `get_skill_gap`, `request_report`, `get_report`, `get_source_snapshots`) | ✅ 확정 |
| 위 메서드들의 **입출력 타입** (본문 전체) | 🟡 제안 — 승인 필요 |
| Mandate Version/Activate 엔드포인트 | 🟡 제안 |
| Improvement Candidate 엔드포인트 (§3.3) | 🟡 제안 |
| Case timeline·Escalation·Candidate 조회 등 보조 엔드포인트 | 🟡 제안 |
| `request_access` (§3.5) | ✅ 구현 — `20260731000700_workforce_access_lifecycle.sql` |
| Scorecard `quality` 일부 | ⚠️ 저장소 미구현 — `quality_snapshots` 없음 |
| Workforce Plan 저장 | ⚠️ 저장소 미구현 — `workforce_plans` 없음 |
| 위원회 (§2.3) | ⚠️ 로직 미구현 (테이블은 있음, Y2) |
| 한도 집행 기준 자본 = 당일 장 시작 시점 `nav_runs.total_nav` (§2.1) | ✅ 결정 2026-07-31 |
| `base_capital` = Paper 시작 자본. 첫날 회계 초기 현금 근거로만 사용 | ✅ 결정 2026-07-31 |
| `mandate.currency` ↔ `funds.base_currency` 검증은 governance가 저장 시점에 수행 | ✅ 결정 2026-07-31 — **F01 구현 필요** |
| Transport | 🔴 미확정 — 타입은 영향받지 않음(§0) |

### 승인이 필요한 것

1. 입출력 타입 전반 — 다른 본부가 이 모양으로 소비해도 되는지.
2. ~~`base_capital` 분모 계약~~ — 2026-07-31 결정(§2.1). 회계·리스크본부는 PR 리뷰에서 확인만 하면 된다.
3. §8.1에 이름 없는 신규 엔드포인트 (§2.1 Version/Activate, §3.3 Improvement).
