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

## 테스트

없음 — prompt-only Profile 단계.

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- Profile, Eval, Improvement, Deployment, Lifecycle 모듈은 아직 미구현 — 코드가 생기면
  `profiles/`, `evals/`, `improvements/`, `deployments/`, `lifecycle/`에 배치
  (8.1절 Hermes 자기 개선 Artifact 경계 참고)
