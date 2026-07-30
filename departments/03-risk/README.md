# 리스크본부 (Risk Management)

## Mission

Pre/Post-Trade Risk, Compliance와 Kill State를 담당한다. 포지션 리스크 평가, 포트폴리오 노출도·변동성
영향 분석을 수행하고 approve/resize/reject 중 하나를 반환한다.

`risk-management` 에이전트는 근거와 권고(approve/resize/reject)만 만든다. 바인딩 집행·한도 관리는
결정론적 Risk Engine이 한다 — Risk Engine 자체는 아직 이 저장소에 구현돼 있지 않다
(`CLAUDE.md` "절대 깨면 안 되는 권한 분리" 참고).

## Owner

동규님 — [TEAM_DONGGYU_RISK_QA_GUIDE](../../docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md)

## 입력·출력 계약

- 입력: 트레이딩본부 OrderIntent, 실시간 베타·변동성 데이터, Tavily 뉴스 검색 결과
- 출력: RiskDecision(approve/resize/reject) → `workflow` step 4 QA본부로 전달

## 실행법

```bash
risk-management chat -q 'Assess risk of AAPL long position'
python3 skills/agentic-rag/main.py --persona compliance-policy-agent \
  --query "Can we open a new long position in SYMBOL_A today?" --as-of 2026-07-29
```

## 테스트

없음 — Risk Engine 자체 점검 스크립트는 아직 없다. `compliance-policy-agent`의 Agentic RAG baseline은
`skills/agentic-rag/`(공용 skills 경계 유지, 이 본부가 Domain Owner) 참고.

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- Risk Engine, Compliance, Stress 모듈은 아직 미구현 — 코드가 생기면 `engine/`, `compliance/`, `stress/`에 배치
