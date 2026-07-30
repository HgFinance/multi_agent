# Agent Workforce 인사팀 (HR)

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
    하며 근거/롤백 없는 후보는 만들 수 없다. **스키마 격차**: `workforce.improvement_candidates`
    테이블이 아직 없다 (배포 대상 `agent_profile_versions`, QA Eval `audit.eval_runs`만 존재). 이
    계약대로 후속 마이그레이션에서 테이블을 만든다.
  - `workflow.py` — 후보 생명주기 상태 머신 + **권한 분리 게이트**. 작성자는 자기 후보를 단독
    승인할 수 없고(자기승인 차단), 승인엔 독립 승인자 + QA Eval 근거가 필요하다. 모든 전이는 같은
    `candidate_id`로 Append-only Event 에 기록(같은 ID 재현). Event Ledger 는 현재 In-Memory.

미구현(후속): `workforce.improvement_candidates` 마이그레이션, Eval Runner/Shadow Router 실체
연결(QA·audit 소유), CEO 예산·조직 승인과 Scorecard 관찰의 실제 API 배선.

## 테스트

```bash
python departments/07-agent-workforce/improvements/candidate.py  # 후보 계약·근거·롤백 검증
python departments/07-agent-workforce/improvements/workflow.py   # 상태 머신·자기승인 차단·감사
```

`__main__` assert 자체 점검 (F01 CEO Office 모듈과 동일 관례).

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- Profile, Eval, Deployment, Lifecycle 모듈은 아직 미구현 — 코드가 생기면
  `profiles/`, `evals/`, `deployments/`, `lifecycle/`에 배치
  (8.1절 Hermes 자기 개선 Artifact 경계 참고)
