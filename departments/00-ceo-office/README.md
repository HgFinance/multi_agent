# CEO Office

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
Local Ollama Alias는 [`Modelfile`](Modelfile)의 `hermes3` 기반 `agent-ceo`이고 Hermes Profile은 `ceo-agent`다. Build·Eval·권한 기준은 [Ollama Department Modelfile Guide](../../docs/02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)를 따른다.

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

## src/

- `src/mandate/` — **F01 사용자 Mandate** 앱 레이어 (Sprint Y0~Y1).
  - `policy.py` — 구조화 정책 Pydantic 계약. `supabase/migrations/20260729000200_governance_workforce.sql`
    의 `governance.mandate_versions.risk_bounds / universe_policy / approval_rules` jsonb 컬럼 **내부
    형태를 정의**하고, 값 범위와 상호 모순을 결정론적으로 검증한다 (F01 완료조건: 잘못된 한도 조합 저장 불가).
  - `service.py` — Version/Effective Time 발급, `content_hash` 중복 방지, 장중 변경 방향 판정
    (`TIGHTEN`=완화 / `LOOSEN`=확대 / `NEUTRAL`). `MandateVersionRepository` 는 인터페이스이며
    현재 In-Memory 구현만 있다. asyncpg 실 구현은 Y1 에서 붙인다.
  - `lifecycle.py` — Version 활성화 상태 전이 (§5.1). 완화/중립은 즉시 활성화, 확대와 최초
    활성화는 사용자 승인 필요. 활성화 시 이전 Version `effective_to` 종료 + `mandates.current_version`
    갱신 + `mandate_decisions` APPROVE 기록.

자본(base_capital) 위치: README(docs) 상 "얼마의 자본을 운용할지"는 Mandate 입력이므로 `risk_bounds`
안에 **선언 값**으로 둔다. 비율 한도의 **live 집행 분모**(NAV/allocated)는 회계 API 소유이며 Risk Engine 이
결정 시점에 조회한다 — 이 계약은 트레이딩(도현)·리스크(동규)와 별도 확정 대상.

미구현(Y1 잔여): asyncpg Repository, §5.1 변경 Workflow 의 Risk/QA 사전 검토 단계와 사용자 승인
Interrupt/Resume(LangGraph), F01 완료조건 2("Signal/Risk Decision 이 mandate_version_id 기록")는
트레이딩·리스크 본부 의존.

## 테스트

```bash
python departments/00-ceo-office/src/mandate/policy.py     # 정책 검증·상호 모순
python departments/00-ceo-office/src/mandate/service.py    # Version·방향 판정·hash
python departments/00-ceo-office/src/mandate/lifecycle.py  # 활성화·재승인 게이트·감사
```

`__main__` assert 자체 점검 (`CLAUDE.md` 명령어 절의 트레이딩·회계 모듈 관례와 동일). pytest 이전은
`TECH_STACK_DECISIONS.md` 도입 시점에 맞춘다.

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본 (`config.yaml`, `SOUL.md`). 로컬 Runtime 반영은
  `scripts/sync_hermes_profiles.sh` 참고.
