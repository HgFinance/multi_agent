# AI QA/감사본부 (AI QA & Audit)

## Mission

Evidence QA, Model Risk, 권한 검증과 Audit을 담당한다. AI 생성 신호의 환각 여부 검사, 논리적 일관성
평가, 사실적 정확성 검증을 수행하며 리서치·트레이딩·리스크 각 단계의 근거와 Decision Trace를
독립적으로 검증한다.

QA가 감사 대상 원본을 수정하거나 자기 Finding을 단독 종료하지 않는다. 인사팀 신규 채용·Agent 개선
모두 QA의 독립 권한 검증을 거쳐야 하며, "이미 배포됐다"는 이유로 이 게이트를 건너뛰지 않는다
(`CLAUDE.md` "절대 깨면 안 되는 권한 분리" 참고).

`qa-audit-supervisor`를 비롯한 Hermes 페르소나는 근거와 권고(해석·Escalation)만 만든다. 실제
PASS/WARN/FAIL 판정과 Ops Incident 심각도는 `evidence/evidence_qa_engine.py`,
`audit/ops_health_monitor.py`의 결정론적 엔진이 한다 — LLM은 관련성 판단과 서술 작성에만 쓴다.

## Owner

동규님 — [TEAM_DONGGYU_RISK_QA_GUIDE](../../docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md)

## 입력·출력 계약

- 입력: Research/Trading Artifact(Claim + Evidence 인용), Agent Health Metrics(에러율·지연·비용) —
  둘 다 `EvidenceStore`/`AgentHealthMetrics`로 표현하며, rag-librarian-evidence-curator나 Model
  Gateway Telemetry가 실연동되기 전까지는 호출자가 값을 채워 넣는 스텁이다.
- 출력: `QaAssessment`(PASS/WARN/FAIL + Claim별 결과 + Finding 초안, `audit.claim_checks`/
  `audit.qa_decisions`/`audit.findings` 컬럼과 이름을 맞췄다), `OpsAssessment`(HEALTHY/DEGRADED/
  CRITICAL + Incident 초안, `audit.incidents`와 이름을 맞췄다) → `workflow` step 4에서 근거로 전달

## 실행법

```bash
qa-department chat -q 'Validate: [AI 신호 내용]'
python departments/06-ai-qa-audit/evidence/evidence_qa_engine.py
python departments/06-ai-qa-audit/audit/ops_health_monitor.py
```

## 테스트

- `evidence/evidence_qa_engine.py` — Evidence QA 8단계 검사 16개 시나리오 자체 점검(팀 가이드 7.1
  전부 + Fact/Inference 구분 + 부분 무효 근거의 PARTIAL 격상 + Finding 자동 생성 + 재현성).
- `audit/ops_health_monitor.py` — Ops Threshold 9개 시나리오 자체 점검(Soft/Critical 분리,
  동시 Critical의 SEV1 격상, 무트래픽 오탐 방지, 재현성).

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- `evidence/evidence_qa_engine.py` — Sprint K2 P0 Evidence QA Gate. 팀 가이드 7.1(8단계 검사) 구현.
  LLM 호출 없음. UNSUPPORTED/CONTRADICTED Claim마다 Finding 초안을 자동 생성하며, 원 작성 본부는
  이 Finding을 수정·종료할 수 없다.
- `audit/ops_health_monitor.py` — agent-ops-monitor P0 Threshold 판정. Soft 초과는 DEGRADED(SEV3),
  Critical 초과는 CRITICAL(SEV2), 서로 다른 두 지표가 동시에 Critical이면 SEV1로 격상한다.
- Eval, Model-Risk 모듈은 아직 미구현(P1 tier: model-risk-agent, internal-audit-agent,
  tool-permission-security-reviewer, incident-postmortem-agent) — 코드가 생기면 `evals/`,
  `model-risk/`에 배치.
- 미착수: Sprint K0(Supabase 실배선 — 스키마는 이미 존재), K3(Stress/Kill Switch/Incident
  Correlation), K4(Release/Access Audit). 자세한 진행 상태는 `hermes/config.yaml`의
  `implementation:` 블록 참고.
