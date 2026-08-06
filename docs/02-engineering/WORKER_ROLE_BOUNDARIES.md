# HgFinance Worker 역할·통합 판정

검토일: 2026-08-03 (KST)  
상태: **최종 확정**

이 문서는 직원 수를 늘리거나 줄일 때 사용하는 역할 경계와 통합 판정의 기준이다. 실행 기준은 각 부서의 `hermes/config.yaml`과 `employee_workers.py`의 `WORKER_SPECS`이며, `agent.personalities`의 예전 역할명은 호환용 Alias로만 취급한다.

## 공통 실행 구조

```text
Hermes Department Head (Codex 기본 / 승인된 Claude Code 대체)
  └─ 독립 LangGraph Worker Graph × Worker Registry
       ├─ allow-listed read/calculation tools
       ├─ Ollama qwen3:1.7b (임시 테스트용 현재 모든 Worker 고정값)
       ├─ schema validation + 최대 2회 재시도(총 3회 시도)
       └─ non-binding worker-context.v1 → Hermes context
```

Hermes는 직원 Context를 종합·에스컬레이션한다. 주문 제출, Risk 승인, QA 판정, 원장 Posting, NAV 확정, 감사 Finding 종결은 각각 결정론적 서비스와 독립 통제 부서의 권한이다.

## 확정 Worker Registry

| 부서 | 전체 | 항상 실행 | 조건부 | 현재 통합 판정 |
|---|---:|---:|---:|---|
| CEO | 1 | 1 | 0 | `executive-briefing-worker` 유지 |
| HR | 5 | 2 | 3 | 업무량·Profile·성과·Lifecycle·SoD 유지 |
| Research | 6 | 2 | 4 | 데이터·미시구조·기술·가치·뉴스/매크로·Evidence 유지 |
| Trading | 6 | 2 | 4 | Thesis·OrderIntent·제약·집행·비용·파생 유지 |
| Risk | 4 | 2 | 2 | 기존 통합 완료; 추가 감원 없음 |
| Quant / Backtest | 7 | 2 | 5 | 가설·Dataset·Backtest·Release·ML·비용·Regime 유지 |
| Accounting / Portfolio | 8 | 2 | 6 | Position·Ledger·NAV·유동성·PnL·보고·평가·Accrual 유지 |
| QA | 5 | 1 | 4 | 기존 통합 완료; 추가 감원 없음 |

총 42개 Worker와 8개 Hermes Profile이다. 조건부 Worker는 Registry에 존재하지만 해당 입력 신호가 없으면 호출하지 않는다.

## 부서별 역할과 병합 판정

- **CEO**: `executive-briefing-worker` — 각 부서 보고서와 차단 사유를 종합해 최종 Case Summary를 작성한다. 주문·Risk 승인·원장 수정·NAV 확정 권한은 없다.
- **HR**: `workforce-planning-worker`, `profile-architecture-worker`, `selection-performance-worker`, `lifecycle-coordination-worker`, `workforce-governance-worker`. 계획·Profile 설계·평가·Lifecycle·승인/SoD는 서로 다른 상태 전이를 다루므로 유지한다.
- **Research**: `research-data-worker`, `microstructure-worker`, `technical-signal-worker`, `fundamental-valuation-worker`, `news-macro-worker`, `evidence-rag-worker`. 데이터 정본·유동성 증거·지표·가치·이벤트·인용 검증은 서로 다른 입력과 Evidence 책임이므로 유지한다.
- **Trading**: `market-thesis-worker`, `trade-proposal-worker`, `order-constraint-worker`, `execution-planning-worker`, `venue-cost-worker`, `derivatives-structure-worker`. OrderIntent 이전의 논리, 제약 매핑, Risk 승인 후 집행계획, 거래비용, 파생 구조는 권한과 실행 시점이 달라 유지한다.
- **Risk**: `core-risk-worker`, `compliance-policy-worker`, `derivatives-counterparty-worker`. 시장·유동성과 사전 Risk Gate는 `core-risk-worker`로, 파생·Margin과 Counterparty·Operational 위험은 `derivatives-counterparty-worker`로 이미 통합되었다. 최종 판정은 결정론적 Risk Engine이 한다.
- **Quant**: `strategy-hypothesis-worker`, `dataset-feature-worker`, `backtest-optimization-worker`, `strategy-release-worker`, `ml-quant-worker`, `execution-cost-worker`, `regime-robustness-worker`. 연구 가설, PIT Dataset, Backtest, Release, ML, 비용, Regime의 실패 원인을 독립적으로 재현해야 하므로 유지한다.
- **Accounting**: `portfolio-control-worker`, `ledger-reconciliation-worker`, `nav-close-worker`, `treasury-liquidity-worker`, `pnl-attribution-worker`, `investor-reporting-worker`, `valuation-corporate-actions-worker`, `fee-accrual-tax-worker`. 공식 원장·NAV·대사와 설명용 분석은 분리해야 하므로 유지한다.
- **QA**: `evidence-qa-worker`, `hallucination-critic-worker`, `model-and-internal-audit-worker`, `ops-and-permission-worker`, `incident-postmortem-worker`. Model Risk와 Internal Audit, Agent Ops와 Tool Permission은 이미 각각 통합되었다. Evidence QA Gate가 최종 판정을 한다.

### 추가 병합을 승인하지 않은 이유

현재는 **추가 병합 없음**으로 확정한다. 다음 경계는 이름이 비슷해도 합치지 않는다.

| 경계 | 분리 이유 |
|---|---|
| Research microstructure ↔ Trading venue-cost ↔ Risk liquidity | 시장 증거, 주문 비용, Risk 한도라는 서로 다른 소유권 |
| Trading order-constraint ↔ Risk pre-trade | 전자는 비바인딩 제약 매핑, 후자는 바인딩 결정론적 Gate |
| Trading execution-planning ↔ Quant execution-cost | 단일 주문 계획과 역사적 비용·민감도 검증 |
| Research fundamental-valuation ↔ Accounting valuation/corporate-actions | 투자 근거와 공식 평가·기업행동 원장 |
| Accounting reconciliation ↔ QA internal audit | 원장 대사와 독립 통제 감사 |
| HR governance ↔ QA ops/permission | 조직 승인 라우팅과 독립 권한 검증 |

향후 통합을 제안하려면 중복 실행률·품질·지연·권한 영향을 Worker별로 측정하고, HR 제안 → QA 독립 검증 → CEO 승인 → Rollback 계획 순서를 거쳐야 한다.

## 모델과 연동 상태

- **현재 고정**: 모든 Worker는 임시 테스트용 Ollama `qwen3:1.7b`; `qwen3:8b`, `qwen2.5`, `qwen2.5-coder`, `qwen3:14b`는 과거 기준·Modelfile/실험 표기이며 현재 Worker 기본값이 아니다.
- **향후 모델 변경**: `ollama list` 확인 → Worker별 Golden/Adversarial benchmark → HR 제안 → QA 검증 → CEO 승인 후 Profile과 `OLLAMA_*_MODEL`을 함께 변경한다.
- **Notion**: 부서별 Reporter와 Markdown-to-Notion block 변환기는 어댑터다. 실제 업로드는 `NOTION_TOKEN`과 부서별 DB ID가 설정되고 API 호출이 성공한 경우에만 `upload_succeeded=true`로 본다. Notion은 Projection이며 원본 판정을 소유하지 않는다.
- **LangSmith**: 일부 부서의 handoff 필드는 존재하지만 기본 tracing은 꺼져 있다. 환경변수·자격증명·DNS·네트워크가 모두 확인되고 민감 필드 마스킹을 통과한 실제 run만 trace 성공으로 본다. 코드나 API Key의 존재만으로 연결 완료로 표시하지 않는다.
- **PIKE-RAG / Light-RAG**: 현재 전사 적용 완료가 아니다. Risk의 Policy RAG, Research의 Evidence RAG, QA의 Evidence/Hallucination Audit 범위를 우선 유지하고, PIKE/LightRAG는 corpus·평가셋·운영비용이 준비될 때 제한적으로 도입한다.

## Risk·QA Worker 기술 스택·역할·성과 계약

이 절은 역할 문서와 실행 코드의 드리프트를 막기 위한 명시적 계약이다. 상세 메타데이터의 Source of Truth는 [`departments/risk_qa_worker_profiles.py`](../../departments/risk_qa_worker_profiles.py)이며, 각 부서의 `WORKER_SPECS`가 해당 프로필을 반드시 참조한다. 문서에 적힌 기술은 권한을 확장하지 않는다. Risk·QA Worker의 출력은 `worker-context.v1` advisory이고, 주문·Risk 승인·원장·QA Finding 종결을 직접 수행하지 않는다.

공통 실행 경로는 `allow-listed read/calculation tool → 결정론적 guard/skill → Pydantic context/result 검증 → 필요한 경우 Ollama qwen3:1.7b advisory → trace/replay`다. Risk의 `core-risk-worker`와 Risk Engine 사이에는 RAG·외부 HTTP·재시도형 LLM을 넣지 않는다. Compliance와 Hallucination만 증거가 필요한 경우 Agentic RAG 경로를 사용하며, PIT·ACL·citation·provenance 검증이 실패하면 `DEGRADED/HOLD/ESCALATE`로 끝낸다.

### Risk 본부

| Worker | 역할과 실행 조건 | 기술 스택과 사용 방식 | 입력·도구 | 성과 지표 |
|---|---|---|---|---|
| `core-risk-worker` | 시장·유동성 상태와 사전 Risk Gate 분석; 항상 실행 (2026-08-06, `market-liquidity-worker`+`pre-trade-risk-worker` 병합) | LangGraph StateGraph, Pydantic `RiskSkillContext/Result`·`OrderIntent/RiskContext`, TradingState·P1 snapshot adapter, Redis read model, `RiskEngine`, idempotency·fail-closed guard, Ollama advisory. PIT/freshness를 먼저 확인하고 HALTED·stale·timeout은 신규 진입 차단 방향으로 요약하며, LLM/RAG 없이 같은 입력에 같은 결과를 만들고 설명만 advisory로 생성 | `risk.trading_state.read`, `risk.p1.snapshot`, `risk.case.check`, 시장/포트폴리오 snapshot | freshness pass rate, stale-block rate, snapshot fan-in latency, replay completeness, deterministic decision consistency, gate latency, invalid-intent rejection rate, fail-closed coverage |
| `compliance-policy-worker` | PIT 정책 근거 분석; compliance evidence가 있을 때만 실행 | LangGraph conditional RAG, PIT/ACL filter, pgvector·BM25 hybrid retrieval, rerank, citation/provenance verifier, Ollama grounded summary. PIKE/LightRAG는 평가 후 도입할 후보 | `risk.compliance.check`, research documents/policies read-only | citation coverage, grounded rate, PIT violation catch rate, escalation precision, RAG latency |
| `derivatives-counterparty-worker` | 파생·상대방·증거금 노출 분석; 관련 신호가 있을 때만 실행 | LangGraph StateGraph, TradingState record adapter, deterministic exposure/state-uncertainty check, Broker/FCM reconciliation, Ollama advisory. Greeks·margin·counterparty 상태 누락은 승인으로 보간하지 않음 | `risk.trading_state.record.read` | missing-state detection rate, exposure reconciliation coverage, counterparty escalation rate, tool latency |

### AI QA·감사 본부

| Worker | 역할과 실행 조건 | 기술 스택과 사용 방식 | 입력·도구 | 성과 지표 |
|---|---|---|---|---|
| `evidence-qa-worker` | 인용·근거 품질의 1차 검사; 항상 실행 | LangGraph `EvidenceQAEngine`, Pydantic QA context/result, PIT·provenance·numeric temporal checker, RunJournal. PASS/WARN/FAIL은 결정론적 엔진이 정하고 LLM은 설명만 생성 | `qa.evidence.check`, claim checks, research packet, evidence refs | citation coverage, claim verification rate, unsupported claim rate, QA latency |
| `hallucination-critic-worker` | unsupported/contradicted claim 비판; 해당 claim이 있을 때만 실행 | LangGraph conditional graph, Agentic RAG retrieval/rerank, contradiction·prompt-injection·provenance guard, Ollama critique. 새 자료를 임의 수집하지 않고 제출된 evidence를 우선 비교하며 미해결이면 ESCALATE | `qa.evidence.rag`, claim checks, source evidence | unsupported detection rate, contradiction detection rate, false-clear rate, critique latency |
| `model-and-internal-audit-worker` | 모델 위험·내부통제 점검; audit input이 있을 때만 실행 | LangGraph, deterministic `ModelRiskEngine`·`InternalAuditEngine`, SoD/권한 검사, RunJournal append-only trace, Ollama 설명 | `qa.model_risk.evaluate`, `qa.internal_audit.evaluate` | model drift detection, audit finding coverage, SoD violation catch rate, audit latency |
| `ops-and-permission-worker` | 운영 건강성·도구 권한 점검; ops/permission input이 있을 때만 실행 | LangGraph, `OpsHealthMonitor`, `ToolPermissionCheck`, allowlist·scope·department·SoD·cost/latency 검사, Ollama 설명 | `qa.ops.evaluate`, `qa.tool_permission.check` | permission violation catch rate, unauthorized-call rate, queue/model health, check latency |
| `incident-postmortem-worker` | Incident timeline·재발방지 분석; incident가 있을 때만 실행 | LangGraph, `IncidentTimeline`, `RunJournal`, FACT/INFERENCE 분리, replay metadata, Ollama 요약. incident 기록은 append-only이며 Finding 종결 권한은 없음 | `qa.incident.record`, incident events | timeline completeness, root-cause evidence coverage, recurrence-control coverage, postmortem latency |

조건부 Worker는 자유 텍스트만으로 실행하지 않는다. 해당 구조화 입력과 trigger가 함께 있어야 실행하며, 입력이 없으면 `not_executed/SKIPPED_SAFE`로 남긴다. 이는 성능 저하가 아니라 근거 없는 QA·Risk 판정을 막는 안전장치다. 외부 쓰기(`risk.trading_state.write`, QA corrective-action close 등)는 별도 인증·SoD·idempotency API 경계에 있으며 이 Worker Registry와 포트폴리오 추천 파이프라인에서는 사용하지 않는다.

## 검증 명령

```bash
source ~/claude/bin/activate
python -m pytest tests/test_worker_architecture.py -q -rs
python -m pytest tests -q -rs
```

이 검증은 Registry 수, 독립 Graph topology, Worker 모델, Profile 메타데이터(`role`·`trigger`·`tools`) 일치를 확인한다. 외부 Notion/LangSmith/Redis 연결 성공은 별도 자격증명·네트워크 Smoke 결과로 기록한다.
