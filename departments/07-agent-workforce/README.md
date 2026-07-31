# Agent Workforce 인사팀 (HR)

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
Local Model은 [`Modelfile`](Modelfile)의 `qwen2.5` 기반 `hr-department`이며, Build·Eval·권한 기준은 [Ollama Department Modelfile Guide](../../docs/02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)를 따른다.

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
  - `repository.py` — asyncpg 실 저장 계층(`PostgresImprovementRepository`). 위 도메인 타입을
    `workforce.improvement_candidates`/`improvement_candidate_events` 컬럼과 1:1 매핑. `.env` 의
    `DATABASE_URL` 사용, 비밀번호/service_role Key 는 로그에 남기지 않는다.

**실 DB 검증 미완**: `repository.py` 는 import·구조까지 확인했으나, 대상 테이블(migration 600)이
아직 이 DB에 적용되지 않아 live round-trip 검증은 보류 상태다. `supabase db push`(또는 동등한
방법)로 20260730000600 을 적용한 뒤 검증한다. asyncpg 는 `requirements.txt` 에 있으므로 팀원은
각자 프로젝트 환경(Hermes Runtime venv 아님)에 `pip install -r requirements.txt` 로 설치한다.

미구현(후속): 위 검증, Eval Runner/Shadow Router 실체 연결(QA·audit 소유), CEO 예산·조직 승인과
Scorecard 관찰의 실제 API 배선.

## Profile Seed

- `supabase/seed.sql`은 HR-00~HR-04 5명의 DRAFT Profile Version을 멱등 등록한다.
  P0는 HR-00·HR-01·HR-04, P1은 HR-02·HR-03이다. 모델 티어는 판단이 산출물인 역할
  (HR-00·02·03)이 Deep(Bedrock), 결정론 인접 역할(HR-01·04)이 Quick(Ollama)이다.
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
    (GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC §3.4)

주의 두 가지:
- **통제 부서(03-risk, 06-ai-qa-audit)는 예산을 초과해도 기능 축소를 권고하지 않는다.**
  CEO Escalation 으로 보낸다 — 비용 절감이 Risk/QA 독립성을 없애면 안 된다(팀 가이드 10.3).
- **Snapshot 이 없으면 0으로 채우지 않는다.** `UNKNOWN`으로 두고 측정 누락을 조사한다 —
  0으로 채우면 "예산 여유 있음"으로 잘못 보인다.

`quality.eval_score`는 QA/감사본부 소유(`audit.eval_runs`)라 항상 `None`으로 두고 audit-api가 채운다.

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
python departments/07-agent-workforce/lifecycle/access.py        # 권한 요청·부여·회수 불변식
```

`__main__` assert 자체 점검 (F01 CEO Office 모듈과 동일 관례).

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- Profile, Eval, Deployment, Lifecycle 모듈은 아직 미구현 — 코드가 생기면
  `profiles/`, `evals/`, `deployments/`, `lifecycle/`에 배치
  (8.1절 Hermes 자기 개선 Artifact 경계 참고)
