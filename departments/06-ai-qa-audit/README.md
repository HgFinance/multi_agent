# AI QA/감사본부 (AI QA & Audit)

## Mission

Evidence QA, Model Risk, 권한 검증과 Audit을 담당한다. AI 생성 신호의 환각 여부 검사, 논리적 일관성
평가, 사실적 정확성 검증을 수행하며 리서치·트레이딩·리스크 각 단계의 근거와 Decision Trace를
독립적으로 검증한다.

QA가 감사 대상 원본을 수정하거나 자기 Finding을 단독 종료하지 않는다. 인사팀 신규 채용·Agent 개선
모두 QA의 독립 권한 검증을 거쳐야 하며, "이미 배포됐다"는 이유로 이 게이트를 건너뛰지 않는다
(`CLAUDE.md` "절대 깨면 안 되는 권한 분리" 참고).

## Owner

동규님 — [TEAM_DONGGYU_RISK_QA_GUIDE](../../docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md)

## 입력·출력 계약

- 입력: `workflow`/`strategy_research_cycle`/`workforce_management_cycle`/`agent_evolution_cycle`
  각 단계의 근거와 Decision Trace
- 출력: 검증 통과/실패, Independent Model Risk 판정, 권한 검증 결과(DENY_PERMISSION 등)

## 실행법

```bash
qa-department chat -q 'Validate: [AI 신호 내용]'
```

## 테스트

없음 — prompt-only Profile 단계.

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- Evidence, Eval, Model-Risk, Audit 모듈은 아직 미구현 — 코드가 생기면
  `evidence/`, `evals/`, `model-risk/`, `audit/`에 배치
