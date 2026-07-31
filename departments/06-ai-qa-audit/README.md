# AI QA/감사본부 (AI QA & Audit)

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
Local Model은 [`Modelfile`](Modelfile)의 `hermes3` 기반 `qa-department`이며, Build·Eval·권한 기준은 [Ollama Department Modelfile Guide](../../docs/02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)를 따른다.

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

- 입력: Research/Trading Artifact(Claim + Evidence 인용), Agent Health Metrics(에러율·지연·비용),
  Agent/Tool 실행 이벤트(Run 시작·Tool Call·완료) — `EvidenceStore`/`AgentHealthMetrics`/
  `TraceRecorder`로 표현하며, rag-librarian-evidence-curator나 Model Gateway Telemetry가
  실연동되기 전까지는 호출자가 값을 채워 넣는 스텁이다.
- 출력: `QaAssessment`(PASS/WARN/FAIL + Claim별 결과 + Finding 초안, `audit.claim_checks`/
  `audit.qa_decisions`/`audit.findings` 컬럼과 이름을 맞췄다), `OpsAssessment`(HEALTHY/DEGRADED/
  CRITICAL + Incident 초안, `audit.incidents`와 이름을 맞췄다), `AgentRunRecord`/`ToolCallRecord`
  (`audit.agent_runs`/`audit.tool_calls`와 이름을 맞췄다) → `workflow` step 4에서 근거로 전달
- 이 결정론적 서비스를 FastAPI로 감싸는 API 설계와 부서 내·부서 간 통신 계약은
  [RISK_QA_DOMAIN_API_SPEC.md](../../docs/02-engineering/RISK_QA_DOMAIN_API_SPEC.md) 참고 —
  Case 단위 QA 게이트(`qa-check`)는 아직 팀 승인 대기 중인 제안이다.

## 실행법

```bash
qa-department chat -q 'Validate: [AI 신호 내용]'
python departments/06-ai-qa-audit/evidence/evidence_qa_engine.py
python departments/06-ai-qa-audit/audit/ops_health_monitor.py
python departments/06-ai-qa-audit/audit/trace_recorder.py
python departments/06-ai-qa-audit/audit/tool_permission_check.py
python departments/06-ai-qa-audit/audit/incident_timeline.py
python3 skills/agentic-rag/main.py --persona evidence-qa-agent \
  --query "SYMBOL_A Q2 2026 revenue grew 14.2% year-over-year" --as-of 2026-07-29
```

## 테스트

- `evidence/evidence_qa_engine.py` — Evidence QA 8단계 검사 18개 시나리오 자체 점검(팀 가이드 7.1
  전부 + Fact/Inference 구분 + 부분 무효 근거의 PARTIAL 격상 + Finding 자동 생성 + Pydantic 생성
  시점 검증 + input_hash/calculation_version 재현성).
- `audit/ops_health_monitor.py` — Ops Threshold 9개 시나리오 자체 점검(Soft/Critical 분리,
  동시 Critical의 SEV1 격상, 무트래픽 오탐 방지, 재현성).
- `audit/trace_recorder.py` — Agent/Tool Trace 기록 9개 시나리오 자체 점검(Run 멱등 시작, 종료된
  Run에 Tool Call 추가 차단, 미해결 Tool Call 있으면 Run 완료 차단, 상태 전이 규칙, TIMED_OUT과
  FAILED 구분).
- `audit/tool_permission_check.py` — Tool Allowlist 위반 탐지 6개 시나리오 자체 점검(허용/거부
  판정, Trace 기록과 동시 판정, Unauthorized Tool Call 집계).
- `audit/incident_timeline.py` — Incident Timeline/Corrective Action 9개 시나리오 자체 점검
  (Fact/Inference 분리, occurred_at 순 재현, 상태 순서 강제, 본인 검증 금지).
- `bandit`/`pip-audit` 스캔 완료 — 실제 이슈 0건 (2026-07-30).
- `skills/agentic-rag/` `--persona evidence-qa-agent` — `corpus/evidence/`의 SAMPLE_PLACEHOLDER 문서
  (Earnings Release, Analyst Note, PIT 만료가 있는 News Article)로 실제 OpenAI 호출까지 포함한
  End-to-End 샘플 테스트 완료(2026-07-30): 근거로 뒷받침되는 주장 → SUPPORTED, 근거 없는 주장 →
  UNSUPPORTED(escalate), `effective_to`를 지난 `as_of`로 조회 → 해당 문서가 PIT 필터에 걸러져
  UNSUPPORTED. 기존 `compliance-policy-agent` 페르소나도 리팩터 후 회귀 없음 확인.

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- `evidence/evidence_qa_engine.py` — Sprint K2 P0 Evidence QA Gate. 팀 가이드 7.1(8단계 검사) 구현.
  LLM 호출 없음. `Claim`/`Artifact`/`EvidenceChunk`는 Pydantic으로 검증(LLM 출력 신뢰 경계,
  트레이딩본부 `contracts.py`와 같은 원칙 — CLAUDE.md 개발 원칙 2번). UNSUPPORTED/CONTRADICTED
  Claim마다 Finding 초안을 자동 생성하며, 원 작성 본부는 이 Finding을 수정·종료할 수 없다.
  `QaAssessment.input_hash`/`calculation_version`으로 판정 재현성도 증명한다(DoD 2번).
- `audit/ops_health_monitor.py` — agent-ops-monitor P0 Threshold 판정. Soft 초과는 DEGRADED(SEV3),
  Critical 초과는 CRITICAL(SEV2), 서로 다른 두 지표가 동시에 Critical이면 SEV1로 격상한다.
- `audit/trace_recorder.py` — Sprint K2 P0 Agent/Tool Trace 기록. 완료된 Tool Call은
  `as_tool_result_output_values()`로 `evidence_qa_engine.py` 7단계 검사의 입력 모양으로 바꿀 수 있다.
- `audit/tool_permission_check.py` — DoD 9번("Agent와 Tool의 권한 위반을 Trace에서 탐지한다").
  Tool Allowlist 소속 여부만 보는 결정론적 판정 — LLM 미사용(집합 멤버십 확인일 뿐이라 LLM을 쓰면
  오히려 재현성이 깨진다). `record_and_check_tool_call()`로 `trace_recorder.py` 기록과 동시에
  판정하고, `count_unauthorized_calls()`로 11.4절 핵심 Metric을 집계한다. 실제 Allowlist는
  `workforce.agent_profiles`(인사팀 영역, 아직 비어있음)에서 와야 하므로 지금은 스텁 Policy로 검증.
- `audit/incident_timeline.py` — DoD 10번. `audit.incident_events`(FACT/INFERENCE 분리 기록,
  발생 시각 순 재현), `audit.corrective_actions`(OPEN→IN_PROGRESS→VERIFYING→COMPLETED만 허용,
  담당자 본인이 스스로 검증·종료 불가 — "QA 검증 후 Close" 불변식).
- `evidence-qa-agent`의 Agentic RAG는 `skills/agentic-rag/`(공용 skills 경계 유지, Risk가 Domain Owner,
  QA는 재사용)에 구현됨 — `corpus/evidence/`의 SAMPLE_PLACEHOLDER 근거 문서를 검색해 Claim의 출처를
  인용한다. 최종 PASS/WARN/FAIL 판정은 여전히 `evidence_qa_engine.py`가 하며, 이 RAG는 근거 인용
  보조 도구일 뿐이다. `hallucination-critic`은 이 grounded 판정을 재사용할 예정이라 별도 corpus 없이
  미착수 상태. 기법 배정 전체 결정(Neo4j/Hypergraph 포함)은 `hermes/config.yaml`의
  `rag_technique_assignment:` 참고.
- Eval, Model-Risk 모듈은 아직 미구현(P1 tier: model-risk-agent, internal-audit-agent,
  incident-postmortem-agent) — 코드가 생기면 `evals/`, `model-risk/`에 배치.
- 미착수(기술적으로 지금 불가능한 것과 범위 밖인 것을 구분해서 기록): Agent/Tool Trace 실제 저장과
  Tool Allowlist 실제 판정(`workforce.agent_profiles`가 비어 있어 FK/실데이터 없음, 인사팀 영역),
  `audit.qa_decisions`에 calculation_version/input_hash 컬럼 자체를 추가하는 건 스키마 변경이라
  별도 Migration PR 필요, Sprint K3 나머지·K4, LLM-as-a-Judge(의도적으로 P0 이후 백로그), 실시간
  Telemetry·부하 테스트(관찰·테스트할 실제 서비스가 없어 지금은 불가능). 자세한 진행 상태는
  `hermes/config.yaml`의 `implementation:` 블록 참고.
