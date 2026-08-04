# AI QA/감사본부 (AI QA & Audit)

부서장 `qa-audit-supervisor`는 Hermes(Codex/Claude Code)이고 직원은 [QA Worker Graph](qa_employee_workers.py)의 Ollama `qwen3:1.7b` LangGraph Worker다. 결정론적 Evidence QA Engine이 바인딩 판정을 소유한다.

## 현재 승인 상태 (2026-08-04)

- Evidence QA `qa-check`는 Evidence QA Gate v1로 승인됐고, production은 `QA_CHECK_CONTRACT_APPROVED=true`일 때만 활성화된다.
- Model Risk/Internal Audit는 governed 입력 신호가 있을 때만 결정론 엔진과 `model-and-internal-audit-worker`가 실행되며, PASS가 아니면 에스컬레이션한다.
- 5개 Worker의 ACTIVE Profile·운영 Trace는 migration 적용과 `QA_TRACE_PERSIST=true`가 필요하다. `SAMPLE_PLACEHOLDER` 정책 Corpus는 운영 근거가 아니며 실제 문서·임베딩·pgvector 적재 전에는 ESCALATE한다.

## P1 현재 상태 (2026-08-03)

- `model_risk.py`는 모델·프롬프트·데이터셋 계보와 평가량/Calibration/Drift 지표를 결정론적으로 검사하고, 근거가 없으면 `ESCALATE`한다.
- `internal_audit.py`와 `/qa/v1/internal-audit/evaluate`는 Trace·권한·ACTIVE Profile·부서 경계·금지 Tool을 검사한다. QA가 Risk/OMS/원장 권한을 스스로 승인하지 않는다.
- `/qa/v1/model-risk/evaluate`와 `/qa/v1/internal-audit/evaluate`는 설명용 Agent와 분리된 안전한 P1 API다. `qa-check` 상위 계약은 여전히 명시적 승인 전까지 production `503`으로 차단된다.
- 실제 정책 원문이 없어 `SAMPLE_PLACEHOLDER`는 적재하지 않는다. 실제 Corpus/pgvector, ACTIVE Profile과 운영 `agent_runs/tool_calls`, 상위 계약 승인·E2E가 남은 운영 조건이다.
- 2026-08-03 감사에서 Self-check 5개가 통과했고 QA Decision 2, Incident Event 2, Corrective Action
  1건을 확인했다. Compose Service와 `agent_runs`, `tool_calls`, `audit.run_log_events`는 아직 0건이다.
- 결정론적 Markdown 보고서와 Notion Block Projection을 추가했고 현재 QA 보고서 9개가 있다.
  Projection 실패는 QA Verdict를 바꾸지 않으며 최신 Reporter·Pipeline 회귀 Test 18개가 통과했다.

현재 실행 상태와 동규님 2주 계획·Daily Scrum은 [실행 현황과 통합 계획 v2.2](../../docs/PROJECT_IMPLEMENTATION_STATUS.md#43-동규님-리스크본부와-ai-qa감사본부)을 따른다.

## Skill Harness

`harness/manifest.py`가 QA Evidence/Model-Risk/Internal-Audit/Ops/Trace 스킬과 허용 Tool을 고정한다. `harness/core.py`는 trace·비밀값·권한을 preflight하고, RAG가 `grounded=false`이면 `ESCALATE`한다. 실패 fallback은 `ESCALATE + manual_review_required`다.

`harness/journal.py`는 Hermes QA 오케스트레이터와 LangGraph 직원 실행을 `run_id`로 묶어 `InputSnapshot → AgentOutput → Validation → Decision`을 기록한다. QA는 Order/Fill을 소유하지 않으며 공통 계약의 별도 이벤트 타입만 감사 스키마에서 허용한다. `RunJournal.replay()`와 `RunJournal.review()`로 입력 해시·버전·검증 실패·폴백 사유를 재현·집계하고, 운영 적재 스키마는 `audit.run_log_events`다.

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.

## Worker Registry 수와 실제 실행 수

- Registry에 등록된 실제 Worker는 5개다.
- 기본 입력에서 항상 실행되는 Worker는 1개(`evidence-qa-worker`)다.
- 조건부 Worker는 4개(`hallucination-critic-worker`, `model-and-internal-audit-worker`, `ops-and-permission-worker`, `incident-postmortem-worker`)이며, 근거·모델·운영·Incident 신호가 있을 때 호출된다.
- 한 케이스의 최대 실행 수는 5개다. `agent.personalities`의 기존 8개 역할명은 감사·FK 호환 Alias이며 실행 직원 수에 포함하지 않는다.
직원 Worker의 실제 모델은 `OLLAMA_CHAT_MODEL`로 주입되는 `qwen3:1.7b`이며, `agent-qa`는 수동 호환 Alias일 뿐 `scripts.py`의 실행 경로가 아니다. Hermes Profile은 `qa-department`다. Build·Eval·권한 기준은 [Ollama Department Modelfile Guide](../../docs/02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)를 따른다.

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
  보조 도구일 뿐이다. `hallucination-critic`도 같은 `corpus/evidence/`를 재사용해 확장 완료(2026-08-02) —
  `scripts.py`의 조건부 노드 `hallucination_review`가 UNSUPPORTED/CONTRADICTED로 이미 플래그된 claim만
  대상으로 호출하며, 판정을 뒤집지 않고 유형 분류·인용 근거만 덧붙인다. 기법 배정 전체 결정(Neo4j/Hypergraph 포함)은 `hermes/config.yaml`의
  `rag_technique_assignment:` 참고.
- Eval, Model-Risk 모듈은 아직 미구현(P1 tier: model-risk-agent, internal-audit-agent,
  incident-postmortem-agent) — 코드가 생기면 `evals/`, `model-risk/`에 배치.
- 미착수(기술적으로 지금 불가능한 것과 범위 밖인 것을 구분해서 기록): 전 본부 Agent/Tool Trace 실제 저장과
  Tool Allowlist 실제 판정(Workforce의 Profile/Version Seed는 있으나 공식 Read API·실제 Permission 할당 없음),
  Sprint K3 나머지·K4, LLM-as-a-Judge(의도적으로 P0 이후 백로그), 실시간
  Telemetry·부하 테스트(관찰·테스트할 실제 서비스가 없어 지금은 불가능). 자세한 진행 상태는
  `hermes/config.yaml`의 `implementation:` 블록 참고.
- `audit.qa_decisions.calculation_version/input_hash`는
  `20260731001000_qa_decisions_reproducibility.sql`로 실제 DB 적용까지 확인했다. 다음 단계는 API 판정을
  해당 Row와 `qa.decision.v1`에 같은 Hash로 기록하는 것이다.

## 안전한 단독 실행

QA 실행 소스는 `ai-office/` 아래에 있지 않다. 저장소 루트에서 실행하고, `scripts.py --run` 결과의 `run_id`, `input_hash`, `execution_evidence`, JSONL 원장을 QA 감사 근거로 사용한다.

```bash
cd /Users/baiohelseu/Desktop/Project/multi_agent
source ~/claude/bin/activate
python departments/06-ai-qa-audit/scripts.py --run --fail --log-path /tmp/hg-qa-run.jsonl
```

`execution_evidence.pipeline_status`가 `DEGRADED`이거나 `safe_action`이 `ESCALATE`이면 PASS/승인으로 승격하지 않는다. 실제 정책 Corpus, `DATABASE_URL`, 상위 `qa-check` 계약 승인이 없는 상태는 운영 완료가 아니다.

## P1/P2 검증 기록 (2026-08-03)

QA의 Redis 통합 검증은 Risk의 Trading State와 함께 실행하는 이 부서의 이벤트·RAG 캐시 수용 기준이다. 다른 부서 테스트에는 적용하지 않는다.

```bash
cd /Users/baiohelseu/Desktop/Project/multi_agent
source ~/claude/bin/activate
which python
python -m pytest \
  departments/03-risk/tests/test_trading_state_store.py \
  departments/06-ai-qa-audit/tests/test_redis_event_bus_integration.py \
  -q -rs
```

`which python`이 `/Users/baiohelseu/claude/bin/python`이고 결과가 `11 passed`이며 `skipped`가 없어야 실제 Redis PING, 중복 이벤트 제거, 재시작 후 pending 이벤트 회수, RAG 캐시 TTL을 통합 검증한 것으로 본다.

- P1: Evidence QA, Model-Risk, Internal-Audit, Trace/Tool Permission, Incident Timeline의 결정론적 코드·API·폴백·RLS baseline은 구현·단위 검증 완료. `model-risk-agent`와 `internal-audit-agent`의 LangGraph 직원 활성화, 실제 정책 Corpus/pgvector, ACTIVE Profile FK와 운영 `agent_runs/tool_calls`, 상위 `qa-check` 계약 승인은 별도 운영 조건이다.
- Incident 부모 자동 생성은 `audit.incident_events` 또는 Incident 연결 Corrective Action INSERT와 같은 DB 트랜잭션에서 보장한다. DB 저장 실패 시 메모리 Timeline/Action도 남지 않도록 write-through 순서를 유지한다.
- P2: 현재 QA에는 별도 P2 기능을 임의로 활성화하지 않는다. 실제 운영 Corpus·Trace·계약 승인 이후에만 Model-Risk/내부감사 평가 범위와 그래프 인덱스 도입을 재평가한다.
