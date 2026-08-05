# 재일님 담당 가이드: 리서치본부 + 퀀트/백테스트본부

> Override v2.0 · 기준일 2026-08-05
>
> 이 문서는 이전 Research/Quant 팀 가이드의 운영 기준을 덮어쓴다. Collector가 실행되고 DQ row가 존재한다는 사실을 Research Packet Canonical Artifact, Quant Promotion, Production 전략 승인으로 해석하지 않는다. 최상위 기준은 [HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md), [PROJECT_IMPLEMENTATION_STATUS.md](../PROJECT_IMPLEMENTATION_STATUS.md), [RESEARCH_DATA_SOURCES_AND_LIBRARIES.md](../03-data/RESEARCH_DATA_SOURCES_AND_LIBRARIES.md), [RESEARCH_QUANT_AGENTIC_FRAMEWORK.md](../02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md)다.

## 0. 상태 판정 규칙

| 상태 | 의미 | 이 팀에서의 대표 예 |
|---|---|---|
| `DOCUMENTED` | 계약·설계·완료 조건만 있음 | `RQ-02`, `RQ-03`, `RQ-05`, `RQF-*` |
| `IMPLEMENTED` | Collector/API/Contract 코드가 있음 | Research API·MCP baseline |
| `TEST_VERIFIED` | PIT·DQ·Citation·Replay 결정론 테스트 통과 | 현재 Commit에서 재실행한 테스트만 인정 |
| `RUNTIME_VERIFIED` | 실제 API/DB/Event/DQ 왕복 확인 | `RQ-04`의 3,910행 snapshot |
| `PARTIAL` | 일부 경로만 존재하거나 연결되지 않음 | `RQ-01` ResearchPacket v1 |
| `BLOCKED` | 데이터·Schema·Credential·선행 Gate 미충족 | Quant Promotion·실전 승격 |

LLM은 관련성·가설·서술을 보조할 뿐이다. PIT cutoff, 데이터 시점, 수치, Citation 존재, leakage, release 상태는 결정론적 코드가 판정한다.

## 1. 책임과 절대 경계

### 리서치본부

- 승인된 source registry와 `as_of` 기준으로 Snapshot·Feature·Document·Evidence를 수집한다.
- Research Packet은 주장·근거·데이터 시점·source version·input hash·불확실성을 포함하는 Canonical Artifact다.
- Research Agent는 OrderIntent·Risk Decision·Production 전략을 직접 만들거나 승인하지 않는다.
- Collector 오류·PIT 불명확·출처 충돌은 `INCONCLUSIVE`/`ESCALATE`이며 빈 결과를 정상으로 만들지 않는다.

### 퀀트/백테스트본부

- Hypothesis → Dataset → Backtest → Robustness → Candidate → Release Gate의 산출물을 버전·hash·lineage로 관리한다.
- 미래 데이터·survivorship bias·look-ahead·leakage·overfit을 차단한다.
- Quant는 Production 승격을 직접 수행하지 않는다. CEO·Risk·QA와 운영 Gate를 통과한 Shadow/Paper 후보만 전달한다.
- Backtest 결과는 투자 권고나 실거래 승인과 동일하지 않다.

### 공통 금지

- `ResearchPacket v1`과 `ResearchPacketV2/V3`의 schema를 임의로 섞지 않는다.
- source licensing, rate limit, cache provenance가 없는 데이터를 공식 Evidence로 승격하지 않는다.
- Feature가 0건일 때 모델·백테스트를 성공으로 표시하지 않는다.
- 실제 Broker Credential·Live Strategy Code를 연구/Backtest 환경에 넣지 않는다.

## 2. 현재 기준선

| 항목 | 현재 판정 | 남은 조건 |
|---|---|---|
| Research API `/health`·MCP baseline | `RUNTIME_VERIFIED` snapshot | Packet Canonical DB/Event와 full Replay 필요 |
| Worker Registry | Research 6명 구조·Worker Graph baseline | conditional worker 실제 trigger/tool test 필요 |
| `RQ-01` ResearchPacket | `PARTIAL` | API·Event·DB에서 같은 Packet ID 조회되는 Canonical Artifact 필요 |
| `RQ-02` Feature/Event/Priority Queue | `DOCUMENTED` | 급변 Fixture를 Stream에 한 번만 생성하는 Runtime 필요 |
| `RQ-03` Quant API/Worker | `DOCUMENTED` | Dataset→Experiment→Candidate Job과 재시작 복구 필요 |
| `RQ-04` DQ/파생 첫 적재 | `RUNTIME_VERIFIED` snapshot | 연속성·결측·수정 이력 검증 필요 |
| `RQ-05` Microstructure Feature | `DOCUMENTED` | 현재 `microstructure_features` 0건, Worker/Replay 필요 |
| Research V2 PIT/Citation | `DOCUMENTED`/부분 구현 | Fact Claim 100% citation과 미래 조회 0건 필요 |
| Evidence Graph/Branch/Fan-in | `DOCUMENTED` | Evidence-linked output과 branch provenance 필요 |
| SearXNG/Playwright/Web research | `DOCUMENTED` | 403·사용권·source snapshot·replay 조건 필요 |
| Quant release gate | `IMPLEMENTED` 부분 | walk-forward·overfit·cost·regime·Risk/QA/CEO 승인 연결 필요 |
| `CI-06` Migration contract | `BLOCKED` | 신규 Migration을 Schema Contract 순서에 반영하고 전체 Test 통과 |
| `MODEL-04` Host Proxy | `IMPLEMENTED`/미검증 | Commit·보안·429/timeout/fallback·구독 한도 검증 필요 |

3,910행 DQ 적재는 데이터 연속성·실전 품질·전체 Feature Coverage를 의미하지 않는다.

## 3. Override 작업 순서

### P0-1. Canonical Research Packet

**담당:** 재일. **선행/협업:** 도현 `TRD-01`, 동규 `RSK-01/QA-01`, 영주 `GOV-01`.

- Packet의 canonical schema와 Version을 하나로 고정한다.
- `packet_id`, `trace_id`, `case_id`, `as_of`, PIT cutoff, source IDs/versions, evidence IDs, input hash, artifact hash, generated_at을 필수화한다.
- Research API/MCP 생성, PostgreSQL Artifact, Redis Event가 같은 Packet ID와 hash를 반환하게 한다.
- URL/Case Replay API를 제공해 downstream이 임의 payload를 만들지 않게 한다.
- Packet 생성 실패·source 충돌·PIT 불명확은 `INCONCLUSIVE`로 남기고 Trading으로 전달하지 않는다.

**완료 증거:** 고정 Fixture를 API에서 만들고 DB/Event에서 재조회한 뒤, 재실행·재시작에도 동일한 artifact hash와 중복 0건을 확인한다.

### P0-2. PIT·Citation·Source Governance

- 모든 numerical claim과 narrative claim에 source, observation time, effective time, retrieval time, cutoff를 연결한다.
- `SAMPLE_PLACEHOLDER` Corpus를 운영 근거로 사용하지 않는다. 실제 승인 문서는 version·effective_at·source owner를 가진다.
- Retrieval 결과는 Citation 검증을 통과해야 하고, unsupported claim은 `ESCALATE`/`BLOCKED`다.
- Source registry에 license, retention, rate limit, fallback, outage 상태를 기록한다.
- 수정된 과거 데이터는 원본 hash와 correction event를 보존한다.

### P0-3. Feature/Event/Microstructure Pipeline

- Market Snapshot/Feature를 Event Envelope로 발행하고 `event_id`/idempotency key를 보존한다.
- 급변·stale·missing·duplicate Fixture를 만든다.
- Microstructure feature가 실제 source와 as_of를 가지며 0건이면 downstream 전략을 자동 승인하지 않는다.
- `RQ-04` 연속성·DQ·수정 이력을 DB와 Replay에서 확인한다.

### P0-4. Quant Job·Backtest·Release

- `Dataset → Experiment → Candidate` Job Contract와 lease, retry, restart recovery를 만든다.
- Walk-forward, purged/embargo split, look-ahead/leakage, survivorship, transaction cost, slippage, borrow, liquidity, regime stress를 포함한다.
- 결과에 Dataset hash, code commit, parameter hash, model digest, seed, cutoff, cost assumption을 기록한다.
- Release 상태는 `DRAFT → BACKTESTED → QA_REVIEW → SHADOW → PAPER_ELIGIBLE → REJECTED/PAUSED`처럼 단방향 안전 전이를 사용한다.
- Quant가 `PAPER/LIVE`를 직접 활성화하지 않는다. Risk·QA·CEO 승인 전에는 Candidate로만 보존한다.

### P1-1. Quant API/Worker와 Worker Model

- 현재 문서 상태인 Quant API·Worker를 실제 Job/Artifact/Replay 경계로 구현한다.
- Worker는 Ollama `qwen3:1.7b`, Head는 Profile의 `openai-codex/gpt-5.6-luna` 계층을 지킨다.
- 조건부 Worker는 trigger와 Tool Allowlist를 실제 테스트하고, 실패 시 `HOLD_ESCALATE`한다.
- LLM이 수치 계산·PIT 통과·Release 승인을 직접 결정하지 못하게 한다.

### P1-2. `CI-06`·`MODEL-04`·Recovery

- `20260802002200` Migration과 Schema Contract 기대 목록을 일치시키고 전체 schema test를 통과시킨다.
- Claude Host Proxy의 Credential 경계, single point of failure, concurrency, quota, 429, timeout, fallback을 검증한다.
- Archive export·restore·replay drill을 만들고, 복원된 Packet hash가 원본과 일치하는지 확인한다.

## 4. 인계 계약

| 상대 팀 | 재일이 제공해야 하는 것 | 없으면 |
|---|---|---|
| 도현 | Canonical Packet ID/hash, PIT cutoff, strategy version, invalidation | OrderIntent 생성 금지 |
| 동규 | Risk/QA가 사용할 Snapshot·Feature·Evidence source reference | Risk/QA가 `INCONCLUSIVE` |
| 영주 | Mandate/Universe/Strategy allocation의 승인된 version | Release 승격 금지 |
| 재일 → QA | Claim–Evidence graph, source/cutoff, artifact hash | QA Pass 생성 금지 |

## 5. 검증

```bash
python -m unittest discover -s tests/schema -p 'test_*.py' -v
python -m pytest tests/contracts/test_unified_api_contract.py tests/orchestration/test_paper_case.py tests/orchestration/test_paper_pipeline.py -q -p no:warnings
```

Collector 또는 외부 데이터 검증은 필요한 API key와 사용권이 준비된 경우에만 실행한다.

```bash
python departments/01-research/collectors/news.py 'AAPL Apple stock'
python departments/01-research/scripts/check_gateway_coverage.py
```

외부 API·DB를 쓰는 검증은 source·credential·응답 원문을 로그에 남기지 말고, `packet_id`, `trace_id`, source version, cutoff, hash, row/event count만 보고한다.

## 6. 최종 Release Gate

- [ ] Research Packet API·DB·Event가 동일 ID/hash로 Replay됨
- [ ] 모든 Claim이 PIT cutoff와 Citation을 가짐; unsupported claim은 차단됨
- [ ] DQ 연속성·stale/missing·correction·microstructure feature가 검증됨
- [ ] Quant Job 재시작·중복·leakage·cost·regime 검증 통과
- [ ] Dataset/code/parameter/model/seed hash가 저장됨
- [ ] Release는 Shadow/Paper까지만 자동화되고 Live는 외부 승인 필요
- [ ] `CI-06`, `RQ-01`, `RQ-02`, `RQ-03`, `RQ-05`, `RQF-01` 증거가 현재 Commit에서 재현됨

위 조건 전에는 Research 결과를 OrderIntent·Production Strategy·Live 승인으로 전달하지 않는다.
