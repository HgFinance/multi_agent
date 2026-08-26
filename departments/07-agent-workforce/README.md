# Agent Workforce 인사팀 (HR)

> 현재 활성 역할과 권한은 [Worker Role Boundaries](../../docs/02-engineering/WORKER_ROLE_BOUNDARIES.md), 모델·fallback은 [Worker Model Matrix](../../docs/02-engineering/WORKER_MODEL_MATRIX.md)가 소유한다.

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
Hermes Profile은 `hr-department`이고 현재 활성 LLM Worker는 `profile-architecture-worker`다. 구 5개 역할명과 Modelfile alias는 역사적 분류이며 현재 Worker 수의 기준이 아니다.
현재 실행 상태와 영주님 2주 계획·Daily Scrum은 [실행 현황과 통합 계획 v2.2](../../docs/PROJECT_IMPLEMENTATION_STATUS.md#44-영주님-ceo-office와-agent-workforce-인사팀)을 따른다.

## Mission

CEO 직속 Shared Service로 Agent 채용·평가·Lifecycle을 담당한다. **제7의 투자 본부가 아니다** — 투자
본부는 리서치·트레이딩·리스크·퀀트/백테스트·회계/포트폴리오·AI QA/감사 6개뿐이다.

`workforce_management_cycle`(신규 채용)과 `agent_evolution_cycle`(기존 Agent Profile 개선)은 다른
목적이며 둘 다 QA 독립검증과 CEO 승인 게이트를 거친다. 인사팀은 자기 후보를 스스로 최종 승인할 수
없다 — 권한 독립 검증은 AI QA/감사본부, 예산·조직 승인은 CEO, 실제 Identity/권한 생성은 Platform/IAM
Service만 한다(`CLAUDE.md` "절대 깨면 안 되는 권한 분리" 참고).

## Owner

영주님 — [TEAM_YOUNGJU_CEO_HR_GUIDE](../../docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md)

## 입력·출력 계약

- 입력: 6개 본부의 Queue·SLA·비용·Eval·Incident, Finding 누적
- 출력: Hiring Requisition/Job Profile, Agent Profile 개정안 → QA 독립 검증 → CEO 승인 → Lifecycle 반영

## 실행법

```bash
hr-department chat -q 'Build the weekly workforce plan from department Queue/SLA/cost signals'
```

## improvements/

- `improvements/` — **F19 승인형 Hermes 자기 개선** 앱 레이어 (agent_evolution_cycle).
  - `candidate.py` — `ImprovementCandidate` 계약. 근거·대상·예상효과·위험·롤백 대상을 갖춰야
    하며 근거/롤백 없는 후보는 만들 수 없다. 대응 테이블 `workforce.improvement_candidates`
    (`supabase/migrations/20260730000600_...`)의 DDL check 제약과 동일 규칙을 강제한다.
  - `workflow.py` — 후보 생명주기 상태 머신 + **권한 분리 게이트**. 작성자는 자기 후보를 단독
    승인할 수 없고(자기승인 차단), 승인엔 독립 승인자 + QA Eval 근거가 필요하다. 모든 전이는 같은
    `candidate_id`로 Append-only Event(`workforce.improvement_candidate_events`)에 기록.
    `OBSERVING -> KEPT/ROLLED_BACK`은 해당 후보의 Scorecard가 최소 1건 있어야 통과한다
    (`MissingScorecardEvidenceError` → 409, 2026-08-25). 어느 쪽으로 종료할지는 여전히
    호출자 판단이고, 이 게이트는 그 판단이 관찰 기록에 근거했는지만 본다.
  - `repository.py` — asyncpg 실 저장 계층(`PostgresImprovementRepository`). 위 도메인 타입을
    `workforce.improvement_candidates`/`improvement_candidate_events` 컬럼과 1:1 매핑. `.env` 의
    `DATABASE_URL` 사용, 비밀번호/service_role Key 는 로그에 남기지 않는다.
  - `observation.py` — P1-1 후보별 관찰 Scorecard 계약. 비용·품질·안전·회귀 지표를
    후보 ID에 append-only로 귀속하며, 원천 판정(QA Eval·Platform 비용 측정)은 소유하지 않는다.

**실 DB 상태**: `workforce.improvement_candidates`, `access_requests`와 관련 Migration의 적용을
2026-08-01 실제 DB에서 확인했다. 현재 두 Table은 0건이므로 Repository의 Live Create→Transition→Read
Round-trip은 아직 검증되지 않았다. asyncpg는 `requirements.txt`에 있으며 Department API Container에서
같은 Test를 수행해야 한다.

후속: 위 검증, QA Eval Runner에 후보 Runner를 등록하는 교차 프로세스 경로,
Shadow Router·CEO 예산/조직 승인과 Scorecard 관찰의 실제 API 배선. Eval Runner 자체와
`audit.eval_runs` 기록 API는 구현돼 있으므로 미구현 항목으로 세지 않는다.

## Profile Seed

- `supabase/seed.sql`은 HR-00~HR-04 5명의 DRAFT Profile Version을 멱등 등록한다.
  P0는 HR-00·HR-01·HR-04, P1은 HR-02·HR-03이다. 모델 티어는 판단이 산출물인 역할
  (HR-00·02·03)이 `Deep`, 결정론 인접 역할(HR-01·04)이 `Quick`으로 seed된다. 이는
  Profile의 역사적 tier label이며 현재 Worker provider/model 선택은
  [Worker Model Matrix](../../docs/02-engineering/WORKER_MODEL_MATRIX.md)가 소유한다.
- `prompt_artifact_path`의 Anchor는 직원 코드가 아니라 `hermes/config.yaml`의 실제 personality 이름인
  `display_name`을 사용한다.
- Supervisor `model` 설정과 개별 직원의 `agent_profile_versions.model_id`는 다른 계층이다. 어느 쪽도
  QA Eval과 CEO 승인 없이 Production 활성화하지 않는다.

## scorecard/

- `scorecard/cost.py` — **F27 LLM Budget** 중 인사팀 담당분.
  F27은 두 부서가 나눠 맡는다. **플랫폼/인프라**가 토큰 측정·과금·성능저하 차단(집행),
  **인사팀**이 에이전트별 예산(`agent_profile_versions.token_budget`) 설정과 비용 귀속
  (`workforce.cost_snapshots`), Scorecard, 조치 **권고**를 맡는다. 인사팀은 집행하지 않는다.
  - `assess_budget()` — 예산 대비 사용률과 조치 권고
  - `build_department_scorecard()` — `get_department_scorecard` 응답 조립
    (UNIFIED_DOMAIN_API_SPEC §5.4 — 응답 모양 자체는 `cost.py`가 정본)
  - `append_cost_snapshot()`(`postgres_scorecard_repository.py`) — 플랫폼 과금 계측이
    보고한 비용 1건을 적는다. **집행이 아니라 보고 수납이다** — 토큰·금액은 여전히
    플랫폼이 만들고 인사팀은 계산하지 않는다. 그래서 `recorded_by`(2026-08-25 추가,
    `supabase/migrations/20260825000300_...`)를 필수로 요구한다 — 보고자 없이 적힌
    행은 인사팀이 지어낸 값과 구별되지 않는다.
    같은 `(agent, profile version, window)` 재보고는 **행을 늘리지 않고 갱신한다** —
    reader 가 창 안의 행을 합산하므로 중복 행은 곧 사용량 2배이고 예산 판정이 뒤집힌다.
    창구는 `POST/GET /workforce/v1/agents/{agent_id}/cost-snapshots`.

주의 두 가지:
- **통제 부서(03-risk, 06-ai-qa-audit)는 예산을 초과해도 기능 축소를 권고하지 않는다.**
  CEO Escalation 으로 보낸다 — 비용 절감이 Risk/QA 독립성을 없애면 안 된다(팀 가이드 10.3).
- **Snapshot 이 없으면 0으로 채우지 않는다.** `UNKNOWN`으로 두고 측정 누락을 조사한다 —
  0으로 채우면 "예산 여유 있음"으로 잘못 보인다.

`quality.eval_score`는 QA/감사본부 소유(`audit.eval_runs`)라 항상 `None`으로 두고 audit-api가 채운다.

- `scorecard/quality.py` — **P1-2 HR-04 Quality Snapshot**. `get_department_scorecard`의
  `quality.finding_count`/`quality.rework_rate`를 실제로 채우는 저장소(대응 테이블
  `workforce.quality_snapshots`, `supabase/migrations/20260731000800_...` +
  `20260806000200_...`의 `recorded_by`). `eval_score`는 여기서도 QA 소유라 만들지 않는다 —
  이 모듈이 직접 집계하는 값은 `finding_count`/`rework_rate`뿐이다.
  - `aggregate_quality()` — Snapshot 목록을 합산/평균한다. **Snapshot이 없으면 `(None, None)`이다
    (0건으로 채우지 않는다)** — cost.py의 `UNKNOWN`과 같은 원칙.
  - `collect_quality_references()` — `eval_run_id`와 `role_kpi`를 **집계하지 않고** 출처와 함께
    모아 Scorecard `quality` 블록의 `eval_run_ids`/`role_kpi`로 싣는다(2026-08-25).
    - `eval_run_id`는 `audit.eval_runs` 참조다. 인사팀은 `eval_score` 값을 복제하지 않고
      Reference만 보관하는데, Scorecard가 그 참조를 안 실으면 소비자는 `eval_score: null`만
      보고 **어느 Eval을 열어야 할지** 알 수 없다. 값을 만들지 않는 것과 참조를 전달하는 것은
      배타적이지 않다.
    - `role_kpi`는 역할별 KPI다. 이름은 역할마다 다르다
      ([AGENT_EMPLOYEE_PROFILES](../../docs/04-organization/AGENT_EMPLOYEE_PROFILES.md)의 각
      직원 프로필 `KPI:` 줄 — 예: HR-01은 "SLA 예측 오차, 과잉·과소 배치율, 비용 대비 처리량…").
      **부서 단위로 합치지 않는다** — 역할마다 KPI 이름이 다르고 같은 이름이라도 비율·건수·SLA가
      섞여 있어 합치는 규칙이 어디에도 정의돼 있지 않다. 출처(`agent_id`/`profile_version_id`)를
      붙여 그대로 넘기고, 해석은 그 KPI 정의를 아는 쪽(HR-03 성과 평가)이 한다.
  - 실제 조회/기록은 `postgres_scorecard_repository.py`의 `append_quality_snapshot()`/
    `list_quality_snapshots_by_department()`/`list_quality_snapshots_by_agent()`가 맡는다.
    cost 와 다른 점은 **수치를 누가 만드느냐**다 — quality 의 `finding_count`/`rework_rate`는
    인사팀이 직접 집계하고, cost/capacity 는 플랫폼이 만든 것을 받아 적기만 한다
    (`append_cost_snapshot`/`append_capacity_snapshot`, 2026-08-25). `capacity_snapshots`도
    cost 와 같은 계약이다 — `recorded_by` 필수, 같은 `(department, agent, window)` 재보고는
    갱신(`supabase/migrations/20260825000400_...`). `department_id`/`agent_id`는 DDL check 상
    하나만 있어도 되므로 unique index 는 `nulls not distinct`를 쓴다(일반 unique 는 null 을
    서로 다른 값으로 봐서 같은 부서 단위 재보고를 막지 못한다). 창구는
    `POST/GET /workforce/v1/capacity-snapshots`. 여전히 `scorecard/observability.py`가
    Langfuse 실행 이벤트를 직접 집계해 capacity 를 메우는 우회 경로도 남아 있다 — DB
    Snapshot 쪽에 보고를 넣는 호출자가 아직 없어서다. 창구는 통합 엔드포인트의
    `capacity` 필드다(아래).

- `scorecard/observability.py` — `check_worker_trigger_rates()`(2026-08-25). 실행기 셋이
  발행하는 `llm.opportunity.v1`(trigger 미충족 1건) 이벤트를 읽어 `fire_rate = 실행 /
  (실행 + 미발화)`를 계산한다. 분모 0(이 창에 기회 자체가 없었다)은 `fire_rate` `0.0`이
  아니라 `None` — cost.py 불변식 3과 같은 원칙. 창구는 통합 엔드포인트의
  `trigger_rates` 필드다(아래).

- `scorecard/observability.py` — `check_department_llm_usage()`(2026-08-25). capacity와
  같은 실행 이벤트를 읽지만 latency/재시도가 아니라 `llm_calls`/`model_name`/
  `prompt_tokens`/`completion_tokens`/`attempts`/`status`를 집계한다. 이 넷 중 앞의
  셋은 `begin_worker_metric()` 컨텍스트가 열려 있었던 실행에서만 나오므로
  `arrivals > 0`이어도 `None`일 수 있다. 창구는 통합 엔드포인트의 `llm_usage` 필드다(아래).

- `scorecard/observability.py` — `WindowedActivityReader` / `collect_workforce_observability()`(2026-08-26 통합).
  위 네 집계(유휴·Capacity·LLM 사용량·발화율)를 **한 창·한 reader** 로 묶어
  `GET /workforce/v1/departments/observability` 하나로 돌려준다. 그 전에는 넷이
  각각 엔드포인트였고 각자 reader 를 만들어 **같은 실행 이벤트를 네 번** 읽었다 —
  Worker 8명 기준 화면 1회당 Langfuse 왕복 40회, 그중 capacity 와 llm-usage 는
  event_name·창·limit 이 글자 그대로 같은 질의였다(집계 축만 달랐다). 60초 폴링이라
  그게 그대로 분당 부하가 됐다. 지금은 Worker 당 최대 2회(실행 이벤트 1 + 미발화
  건수 1)다. 왕복 수는 `tests/test_hr_shared_activity_reader.py`가 직접 센다 —
  값은 맞는데 왕복만 늘어나는 회귀는 화면으로 보이지 않아서다.

  같은 변경에서 건수 포화도 고쳤다. 이전 `count_events()`는 `len(page.data)`를
  돌려줘서 창 안에 limit(200) 이상이 쌓이면 실행·미발화 둘 다 200으로 포화됐고,
  `fire_rate`가 실제와 무관하게 0.5로 수렴했다. 지금은 서버 `meta.total_items`를
  쓰고 레코드는 페이지 끝까지 모은다.

## roster/ (생명주기 이벤트)

- `roster/lifecycle_event.py` — Agent 상태 전이 감사 기록(`workforce.lifecycle_events`).
  `change_status()`가 `employment_status`를 바꾸면서 그 전이를 어디에도 남기지 않던 공백을 메운다 —
  "승인 없는 활성화 0"(HR-04 KPI)을 현재 상태가 아니라 **이벤트로** 확인할 수 있어야 한다.
  - **상태 변경과 이벤트 기록은 한 트랜잭션이다**(`postgres_roster_repository.change_status`).
    나눠 쓰면 상태는 바뀌었는데 이벤트가 없는 창이 생기고, 그게 이 표가 막으려는 감사 공백이다.
  - **ACTIVE 전이 이벤트는 근거(`approvals`) 없이 남길 수 없다** — `qa_eval_run_id`/`ceo_approval_id`가
    `approvals`에 함께 실린다. 없는 근거를 빈 값으로 채우지 않는다.
  - `trace_id`는 호출자가 준다(`POST .../status`의 필수 필드). 없을 때 만들어 채우지 않는다 —
    지어낸 `trace_id`는 아무것과도 이어지지 않으면서 상관관계가 있는 것처럼 보인다.
  - ⚠ 이 표에는 **append-only 트리거**가 걸려 있다(`improvement_candidate_events`와 같은 취급,
    `cost_snapshots`/`capacity_snapshots`와는 다르다). update/delete가 거부되므로 한번 쓴 이벤트는
    정정할 수 없다 — 그래서 `postgres_roster_repository` 자체 점검은 실 DB에서 상태를 바꾸지 않는다.
  - 조회: `GET /workforce/v1/agents/{agent_id}/lifecycle-events`.

## performance/

- `performance/` — **HR-03 성과 평가와 조치**. `scorecard/quality.py`의 종착지다 —
  `quality_snapshots`의 `role_kpi`는 집계되지 않고 출처만 붙어 Scorecard로 나가는데, 그 값을
  **해석**해 평가로 만드는 쪽이 HR-03이고 그 결과가 `performance_reviews.role_metrics`다.
  - `review.py` — `PerformanceReview` 계약. **조치를 제안하는 평가는 역할 KPI 없이 만들 수 없다**
    (`MissingRoleMetricsError`) — 역할 축소·비활성화 제안은 되돌리기 어려운 결정이라 근거를 요구한다.
    `decision` 어휘는 새로 짓지 않고 `performance_actions.action_type` 4개 + `CONTINUE`를 쓴다
    (`supabase/migrations/20260825000500_...`가 같은 값으로 DDL check를 건다).
  - `action.py` — `PerformanceAction` 상태 머신. `OPEN → IN_PROGRESS → VERIFIED/CANCELLED`,
    `OVERDUE`는 **종료가 아니다**(기한 넘김이 조용한 면제가 되면 안 된다). `VERIFIED`는
    `verification` 없이 통과하지 않고(DDL check와 같은 규칙), `review_id`를 붙이면 그 평가의
    `decision`과 조치 종류가 같아야 한다(`ActionReviewMismatchError`).
  - `postgres_performance_repository.py` — psycopg2 저장 계층. 같은 (agent, profile version,
    period) 재평가는 새 행이 아니라 갱신이다.
  - **제안까지만 한다.** `decision=DEACTIVATION`이거나 `DEACTIVATION` 조치가 `VERIFIED`가 돼도
    Agent의 employment status는 바뀌지 않는다 — 실제 비활성화는 CEO 승인과 roster 전이
    게이트(P0-3)를 따로 거친다. 두 모듈 다 `roster`를 import하지 않고, 자체 점검이 그걸 고정한다.
  - `probation.py` — `ProbationPeriod`(수습 기간). **종료 조건 없이 수습을 시작할 수 없다**
    (`MissingSuccessMetricsError`) — HR-03이 "채용 **전에** Pass/Fail을 고정하고"라고 못박은 것이
    정확히 "관찰이 끝난 뒤 기준을 만드는 것"을 막으려는 규칙이다. 그 이빨로 **판정 시 기준을
    바꿀 수 없다** — `close_probation`도 `ProbationCloseIn`도 `success_metrics`를 받을 자리가
    아예 없고, 자체 점검이 그 부재를 고정한다. `EXTENDED`는 이 행을 닫고 다음 관찰은 새 행으로
    연다. 같은 Agent에 **열린 수습은 하나뿐**이다 — 행 하나만 보는 DDL check로는 못 막아
    부분 unique index로 강제한다(`supabase/migrations/20260825000600_...`).
    `stage`(SHADOW/PAPER) 순서는 제약하지 않는다 — DDL도 문서도 순서를 정한 곳이 없다.
  - 창구: `POST/GET /workforce/v1/agents/{agent_id}/performance-reviews`,
    `POST/GET .../performance-actions`, `POST /workforce/v1/performance-actions/{id}/transitions`,
    `POST/GET .../probations`, `POST /workforce/v1/probations/{id}/close`.

## planning/

- `planning/workforce_plan.py` — **P1-2 HR-04 Workforce Plan** 상태 머신. HR-01
  (workforce-planning-agent)의 Capacity Report/Staffing Scenario 산출물을 저장한다
  (대응 테이블 `workforce.workforce_plans`, `supabase/migrations/20260731000800_...`).
  - `DRAFT -> APPROVED -> ACTIVE -> RETIRED`. **DRAFT -> APPROVED는 이 `plan_id`를 대상으로
    한 실재 CEO 승인(`governance.approvals`, `object_type=WORKFORCE_PLAN`,
    `decision=APPROVED`) 없이는 통과하지 않는다** — 인사팀이 자기 계획을 스스로
    `ACTIVE`로 올리지 못하게 막는다(roster.py `verify_activation_evidence`와 같은
    조회-판정 분리 원칙).
  - `postgres_plan_repository.py` — 실 저장 계층. `PostgresPlanRepository`(CRUD) +
    `PostgresPlanApprovalEvidenceRepository`(`governance.approvals` 읽기 전용 조회).

## lifecycle/

- `lifecycle/access.py` — **Y4 Access Lifecycle** (HR-04 Lifecycle Coordinator).
  대응 테이블 `workforce.access_requests`·`access_assignments`
  (`supabase/migrations/20260731000700_...`).
  - `approve_request()` / `provision()` / `revoke()` / `find_expired()`

세 테이블의 역할이 다르다 — 중복 저장하지 않는다.

| 테이블 | 의미 |
|---|---|
| `agent_tool_permissions` | Profile Version이 **가질 수 있는** 도구 권한 선언 (설계) |
| `access_requests` | 권한 요청과 승인 워크플로 (절차) |
| `access_assignments` | Platform/IAM이 **실제로 부여·회수한** 사실 (증거) |

**인사팀은 요청까지만 한다.** Identity·권한 생성은 Platform/IAM Service만 하고, 그 결과를
`provisioning_ref`로 되받아 기록한다. 만료 없는 권한 요청은 만들 수 없고, 부여는 요청의
`expires_at`을 넘길 수 없으며, 회수는 `revocation_evidence` 없이 완료되지 않는다.

## 테스트

```bash
python departments/07-agent-workforce/improvements/candidate.py  # 후보 계약·근거·롤백 검증
python departments/07-agent-workforce/improvements/workflow.py   # 상태 머신·자기승인 차단·감사
python departments/07-agent-workforce/scorecard/cost.py          # 예산·비용 Scorecard
python departments/07-agent-workforce/scorecard/quality.py        # Quality Snapshot 집계 불변식
python departments/07-agent-workforce/lifecycle/access.py        # 권한 요청·부여·회수 불변식
python departments/07-agent-workforce/planning/workforce_plan.py # Workforce Plan 상태 머신·승인 게이트
```

`__main__` assert 자체 점검 (F01 CEO Office 모듈과 동일 관례).

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- Profile Seed와 Access Lifecycle은 구현됐다. 남은 것은 Workforce API, 실제 Tool Permission Assignment,
  Eval·Shadow·Deployment·Rollback Runner이며 `profiles/`, `evals/`, `deployments/` 경계를 사용한다
  (8.1절 Hermes 자기 개선 Artifact 경계 참고).
