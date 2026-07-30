# CEO Office

## Mission

전사 조정, Mandate 관리, 위원회와 Escalation을 담당한다. 리서치·트레이딩·리스크·QA·회계 결과를
통합해 최종 의사결정과 사용자 설명을 만든다. 예산·조직 변경(신규 채용, 역할 변경, 비활성화)과
전략 Paper Champion 승격, Agent Profile 개정을 승인한다.

CEO는 주문 제출, 리스크 승인, 원장 수정, NAV 확정, Audit Finding 종결 권한이 **없다**
(`CLAUDE.md` "절대 깨면 안 되는 권한 분리" 참고).

## Owner

영주님 — [TEAM_YOUNGJU_CEO_HR_GUIDE](../../docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md)

## 입력·출력 계약

- 입력: `workflow`(step 6), `strategy_research_cycle`(step 3), `workforce_management_cycle`(step 4),
  `agent_evolution_cycle`(step 4)의 각 본부 산출물 (`multi-agent-workflow.yaml`)
- 출력: 통합 의사결정 요약, 예산/조직 승인, Production 승격·Profile 개정 승인 여부

## 실행법

```bash
ceo-agent chat -q 'Summarize current portfolio decisions and open risks'
```

## 테스트

없음 — prompt-only Profile 단계 (`CLAUDE.md` "Hermes(부서) vs LangGraph(직원) 실행 계층" 참고).

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본 (`config.yaml`, `SOUL.md`). 로컬 Runtime 반영은
  `scripts/sync_hermes_profiles.sh` 참고.
