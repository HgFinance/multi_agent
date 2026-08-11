# Research Output Advancement Strategy

> 문서 상태: 구현 기준 제안
> 적용 범위: 리서치본부의 방법론 리드, 실험 기획안, 퀀트 Handoff, 사후 환류
> 현재 기준: `ResearchCaseV2`
> 목표 기준: `MethodologyLeadV1` -> `ExperimentProposalV1` 산출물 체계
> 상위 설계: [Research-Quant Strategy Factory Framework](RESEARCH_QUANT_AGENTIC_FRAMEWORK.md)
> 구현 담당: [TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md](../05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md)
> 개정: 2026-08-10 전략 공장 재편 (재일). 이전 판의 산출물은 종목별 Research Packet 과
> `종목 x 기간 x 방향 x 확률` Outlook 이었다. 그 축을 **방법론 x 실험**으로 갈아끼웠다.
> 품질관리 설비(PIT 4시각, 인용·수치 Validator, 발행 Gate, Lifecycle·Event, 저장 원칙)는
> 그대로 이식한다 - 바뀐 것은 무엇을 만드느냐이지 어떻게 검증하느냐가 아니다.

## 1. 결론

리서치 Output 고도화의 목표는 보고서를 길게 만드는 것이 아니다. **퀀트가 질문 없이 사전
등록할 수 있는 실험 기획안**을 만드는 것이다. 리서치의 산출물은 읽히는 것이 아니라 실행된다.

1. 퀀트는 기획안을 받아 추가 협의 없이 `HypothesisSpecV2` 로 잠글 수 있다.
2. 모든 주장은 리드의 원문 출처(URL·시각·발췌)까지 되짚을 수 있다.
3. 발행 전에 같은 trial family 의 기각 이력과 대조되어, 같은 실험을 두 번 사지 않는다.
4. 실험이 끝나면 결과가 통제 어휘로 돌아와 다음 기획의 입력이 된다.

따라서 최종 산출물은 하나의 Markdown 보고서가 아니라 다음 산출물 묶음이다.

```text
MethodologyLeadV1              스카우트가 가져온 방법론 리드(출처 필수)
ExperimentProposalV1           부서의 유일한 정본 - 퀀트가 소비하는 실험 기획안
EvidenceManifestV1             사용한 증거와 시점 목록
ClaimGraphV1                   주장, 근거, 반론과 인과관계
ExperimentOutcomeV1            퀀트에서 돌아온 결과와 통제 어휘 교훈(입력이자 산출)
Consumer View                  퀀트 전용 조회 (Trading/Risk 는 승격된 전략만 소비한다)
Holding Brief                  사용자 질의 응답 - **공장 입력이 아니다**
```

**사람이 읽는 1페이지 Brief 는 부산물이다.** 이전 판은 그것을 완료 정의의 첫 항목에
두었는데, 그러면 조직의 목표가 "읽히는 문서"가 되고 실험은 부록이 된다.

## 2. 현재 구현에서 재사용할 것

현재 리서치본부에는 고도화에 필요한 기반이 상당 부분 존재한다.

| 현재 자산 | 유지하는 이유 | V3에서의 위치 |
|---|---|---|
| 결정론적 Research Packet 생성 | LLM 자유 서술에 수치가 사라지거나 변형되는 것을 방지 | `ResearchBriefV1` Renderer |
| `ResearchCaseV2` | Case, 시점, 관점, 예산을 이미 구조화 | V3 Case 입력으로 그대로 사용 |
| `AnalystFindingV1` | Claim마다 유형, Evidence, 방향과 신뢰도를 보유 | `AnalystFindingV2`로 점진 확장 |
| `ResearchPacketV2` | Macro/Micro Outlook, Thesis, Catalyst, Dissent와 Lineage 보유 | V2-to-V3 Adapter의 입력 |
| `packet_claims` | 반증 가능한 가격·국면 주장을 별도 저장 | V3 Forecast와 Invalidation의 기반 |
| `probability`, `probability_method` | 전망 확률의 산출 방법을 기록 | `ForecastEstimateV1`로 승격 |
| `method_key`, `method_calibration` | 어느 분석 방법이 실제로 유효했는지 추적 | 방법별 Weight 후보 산출 |
| `packet_outcome_scorer.py` | 미래 결과가 쌓인 뒤 자동 채점 | `ResearchOutcomeScorecardV2` 생성기 |
| Research MCP와 Market API | Agent가 DB Credential 없이 조회 | V3 생성·조회 Tool Surface |

고도화는 현재 Pipeline을 전면 재작성하는 작업이 아니다. 현재 계산과 데이터 수집을 유지하고, 산출물 계약과 검증·배포 계층을 강화하는 작업이다.

## 3. 현재 Output의 한계

| 문제 | 서비스 영향 | 해결 방향 |
|---|---|---|
| 긴 보고서 안에서 중요도 구분이 약함 | 사용자가 핵심 투자 논리를 빠르게 찾기 어렵다 | 1페이지 Brief와 상세 Evidence Appendix 분리 |
| V2 JSON과 실제 Markdown 보고서의 표현이 다름 | API, 화면과 사람이 보는 내용이 어긋날 수 있다 | V3 JSON 하나에서 모든 View를 생성 |
| 단일 `instrument_id` 중심 | Pair, Basket, 선물·옵션과 Cross-Asset Case 표현이 제한됨 | `subjects[]`, `scope_type`, `relationships[]` 도입 |
| Macro/Micro Outlook만 존재 | 1일·5일·20일 전망과 대상 변수가 섞일 수 있다 | Horizon·Target Variable별 Outlook 분리 |
| Catalyst와 Invalidation이 문자열 위주 | 자동 감시와 재평가가 어렵다 | Metric, Operator, Threshold, Deadline을 가진 Rule로 변경 |
| Confidence가 하나의 숫자 | 근거 품질과 모델 보정 수준을 구분하기 어렵다 | Evidence, Freshness, Agreement, Calibration을 분해 |
| 반대 의견이 단순 문장 목록 | 어느 Claim과 충돌하는지 추적하기 어렵다 | `opposes_claim_ids`, `counter_evidence_ids` 연결 |
| 직전 패킷과 달라진 내용이 없음 | 새로운 정보가 무엇인지 매번 전체 보고서를 다시 읽어야 한다 | `ResearchPacketDeltaV1` 추가 |
| 소비 본부별 요구가 한 Packet에 혼재 | Trading과 Quant가 자유 서술을 다시 해석한다 | 정본은 하나, Consumer Projection만 분리 |
| 결과 채점이 일부 가격·국면 주장에 한정 | 어떤 유형의 분석이 도움이 됐는지 평가가 제한됨 | Catalyst, Event, Scenario와 Claim Type별 Outcome 확장 |

## 4. 설계 원칙

### 4.1 Evidence before Narrative

요약문을 먼저 쓰고 출처를 붙이지 않는다. 먼저 Evidence와 Claim Graph를 만들고, 통과한 Claim만 사용해 요약문을 렌더링한다.

### 4.2 Fact, Inference, Forecast 분리

- `fact`: 원문이나 결정론적 수치로 직접 확인할 수 있다.
- `inference`: 여러 사실에서 도출한 해석이다.
- `forecast`: 미래에 확인하거나 반증할 수 있는 주장이다.

Fact에는 Evidence가 필수이며, Forecast에는 Horizon, Resolution Rule과 평가 방법이 필수다.

### 4.3 Point-in-Time 우선

모든 Evidence는 최소 `published_at`, `observed_at`, `available_at`, `as_known_at`을 구분한다. 해당 시각 조건을 지원하지 않는 Tool은 Historical Replay와 Backtest에서 Fail-closed 처리한다.

### 4.4 하나의 정본, 여러 View

CEO, Trading, Quant, Risk와 QA가 서로 다른 보고서를 만들지 않는다. `ResearchPacketV3` 하나에서 각 소비자용 View를 결정론적으로 Projection한다.

### 4.5 계산과 승인은 Agent 밖에 둔다

LLM은 설명, 질문 분해와 가설 제안에 사용한다. 가격, 재무 비율, 확률, 데이터 품질과 Outcome은 코드가 계산한다. 리서치본부는 주문 방향·수량·포지션 크기와 최종 전략 승인을 내리지 않는다.

### 4.6 불충분한 결과도 정상 Output이다

근거가 부족하면 내용을 채워 넣지 않고 `INSUFFICIENT_EVIDENCE`로 발행한다. 확실하지 않은 Packet을 많이 만드는 것보다 판단을 보류해야 하는 이유를 정확히 남기는 것이 더 중요하다.

## 5. 목표 산출물 구조

### 5.1 `ResearchBriefV1`: 사람이 보는 1페이지

아래 순서를 고정한다.

1. `Why now`: 이 Case가 지금 생성된 이유
2. `What changed`: 직전 Packet 이후 새로 확인된 정보
3. `Current read`: 기간별 핵심 전망과 근거
4. `Scenario map`: Base, Upside, Downside 조건과 영향
5. `Catalyst clock`: 다음 확인 일정과 관찰 항목
6. `What breaks the thesis`: 자동 감시 가능한 무효화 조건
7. `Dissent and unknowns`: 반대 의견과 해결되지 않은 질문
8. `Decision readiness`: 사용 가능 여부, 만료 시각과 데이터 품질

Brief는 V3 JSON의 필드만 사용해 생성한다. Brief에서만 존재하는 자유 문장을 허용하지 않는다.

### 5.2 `ResearchPacketV3`: 기계가 읽는 정본

V3는 다음 여덟 영역으로 나눈다.

| 영역 | 필수 내용 |
|---|---|
| Identity | Packet, Case, Schema, Parent Packet과 Trace ID |
| Time | Trigger, Published, Observed, As-known, Expiry 시각 |
| Subjects | 종목, 지수, 선물, 옵션, 통화, Pair와 Basket 관계 |
| Evidence Quality | Coverage, Freshness, Source Tier, Conflict와 PIT 상태 |
| Claims and Causal Paths | 사실·해석·전망, 근거와 사건 전달 경로 |
| Outlooks and Scenarios | 대상 변수·기간별 전망, 조건부 시나리오와 불확실성 |
| Monitoring | Catalyst, Invalidation, 재평가 일정과 Source |
| Governance | Readiness, Dissent, Lineage, Model·Prompt·Tool Version |

### 5.3 자산 범위 확장

V3는 단일 주식뿐 아니라 다음 Case를 같은 계약으로 표현한다.

| `scope_type` | 예시 |
|---|---|
| `single_instrument` | 삼성전자 실적 공시 영향 |
| `pair` | 삼성전자와 SK하이닉스 상대가치 |
| `basket` | 방산주 Basket과 지정학 이벤트 |
| `macro_cross_asset` | 금리·환율·주가지수 전달 경로 |
| `futures_basis` | 현물·선물 Basis 변화 |
| `options_surface` | 만기·행사가별 IV, Skew와 Event Premium |

리서치 Output은 Strategy를 승인하지 않지만, 어떤 자산과 변수가 연결되는지는 명확하게 표현해야 한다.

## 6. 핵심 하위 계약

### 6.1 `ClaimV2`

```yaml
claim_id: claim_...
statement: 영업이익이 전년 동기 대비 증가했다.
claim_type: fact               # fact | inference | forecast
materiality: high              # low | medium | high | critical
subject_ids: [inst_...]
horizon: 20d
target_variable: earnings
direction: supportive
evidence_ids: [dart_..., metric_...]
counter_evidence_ids: []
supports_claim_ids: []
opposes_claim_ids: []
numeric_refs: [metric_...]
method_key: yoy_operating_profit
confidence_components:
  evidence_strength: 0.92
  source_reliability: 1.00
  freshness: 0.97
  cross_source_agreement: 0.80
  historical_calibration: null
calibration_status: insufficient_sample
```

최종 Confidence는 LLM이 임의로 쓰지 않는다. 표본이 부족할 때는 구성 요소와 `uncalibrated` 상태만 보여준다. 보정 표본이 충분해진 뒤 Calibration Service가 최종 확률 또는 등급을 산출한다.

### 6.2 `CausalPathV1`

단순히 뉴스가 긍정적이라고 쓰지 않고 전달 경로를 구조화한다.

```text
Event
  -> Economic Driver
  -> Revenue/Cost/Balance-sheet/Flow Variable
  -> Valuation or Positioning Channel
  -> Target Variable
  -> Expected Horizon
```

예시는 `HBM 수요 증가 -> 판매량·가격 개선 -> 영업이익 추정치 상승 -> 밸류에이션 재평가 -> 20~60일 가격 영향`이다. 각 화살표는 Claim ID를 가져야 하며, 확인되지 않은 연결은 `hypothesis`로 표시한다.

### 6.3 `ExpectedEffectV1` (구 `OutlookByHorizonV1` 대체)

**폐기: 종목 방향·확률 예측.** 이전 판의 `OutlookByHorizonV1`은 `direction: positive`,
`forecast_probability: 0.63` 처럼 종목의 방향과 확률을 리서치가 직접 산출했다. 공장 모델에서
방향 예측은 **실험을 통과해 승격된 전략의 산출물**이고, 리서치가 그것을 내면 프레임워크가
다시 투자판단을 하게 된다. 같은 문서 4.5절이 "계산과 승인은 Agent 밖에 둔다"고 적어 놓고
6.3에서 확률을 만들던 자기모순도 여기서 해소된다.

대신 리서치는 **실험이 확인하려는 효과**를 적는다. 이것은 예측이 아니라 사전 등록될 가설이다.

```yaml
- effect_id: effect_...
  proposal_id: prop_...
  edge_type: mean_reversion        # 통제 어휘
  universe_key: above_sma20        # 통제 어휘 (자유 서술 금지)
  label: forward_return            # 실험이 예측할 대상
  baseline: equal_weight_buy_and_hold
  expected_direction_vs_baseline: positive   # 기준선 대비 초과 여부만. 종목 방향이 아니다
  source_reported_effect:          # 소스가 보고한 값 - **우리 데이터의 결과가 아니다**
    market: US equities
    period: 1993-2018
    reported_metric: annualized_excess_return
    reported_value: 0.04
  falsification_tests:
    - 하락장 초과수익이 0 미만이면 기각
  prereg_metric: [excess_return_pct, information_ratio, deflated_sharpe, pbo]
```

`source_reported_effect`와 우리 실험 결과를 **절대 같은 필드에 두지 않는다.** 남의 시장·남의
기간에서 나온 숫자가 우리 백테스트 결과처럼 읽히는 순간, 검증되지 않은 값이 근거로 승격된다.

### 6.4 `ScenarioV1`

각 Packet은 필요한 경우 Base, Upside, Downside와 Tail Scenario를 갖는다.

```yaml
scenario_id: scenario_down
name: earnings_miss_and_risk_off
weight: 0.22
weight_type: heuristic          # heuristic | calibrated
entry_conditions: [rule_...]
causal_path_ids: [path_...]
expected_impacts:
  - subject_id: inst_...
    target_variable: relative_return
    direction: negative
    horizon: 20d
contradicting_claim_ids: [claim_...]
```

보정되지 않은 Scenario Weight는 확률처럼 표현하지 않는다. `weight_type=heuristic`을 명시하고, Historical Cohort가 충분할 때만 `calibrated`로 승격한다.

### 6.5 `CatalystRuleV1`과 `InvalidationRuleV1`

```yaml
rule_id: rule_...
rule_type: invalidation
metric_ref: market.close
operator: "<="
threshold: 1546200
window: 5_trading_days
check_frequency: realtime
source_tool: market-api@v1
resolution_at: 2026-08-10T06:30:00Z
on_trigger: request_research_update
```

사람이 읽는 설명은 이 Rule에서 생성한다. Threshold가 없는 정성적 조건은 `manual_resolution`로 표시하고, 담당 Agent와 판단 기한을 지정한다.

### 6.6 `DecisionReadinessV1`

Research의 방향성과 별개로 사용 가능 상태를 평가한다.

| 상태 | 의미 |
|---|---|
| `READY` | 필수 관점, Evidence, PIT와 Validator를 모두 통과 |
| `READY_WITH_LIMITS` | 제한 사항이 명시됐고 제한된 용도로 사용 가능 |
| `INSUFFICIENT_EVIDENCE` | 중요 Claim의 근거 또는 Coverage 부족 |
| `BLOCKED_DATA_QUALITY` | 지연, 결측, 종목 매핑이나 수치 불일치 |
| `EXPIRED` | Packet의 유효 시각 경과 |
| `RETRACTED` | 원문 정정, 오류 또는 QA 결정으로 사용 금지 |

`DecisionReadiness`는 매수·매도 추천 점수가 아니다. 후속 본부가 이 Packet을 입력으로 사용해도 되는지를 나타내는 운영 상태다.

## 7. 소비 본부별 View

정본을 복제하지 않고 API Projection으로 제공한다.

**리서치의 1차 소비자는 퀀트 하나다.** 이전 판은 Trading 에 `Horizon Outlook · Catalyst
Clock` 을 직접 공급했는데, 그러면 리서치 견해가 실험을 건너뛰고 매매 근거가 된다. 트레이딩과
리스크는 이제 리서치 산출물이 아니라 **승격된 전략**을 소비한다.

| 소비자 | 보여줄 내용 | 숨기거나 금지할 내용 |
|---|---|---|
| **Quant (1차 소비자)** | Lead 출처·발췌, 경제적 근거, 경쟁 설명, 통제 어휘 사상, 데이터 요구, 반증 검사, 기각 이력 대응 | 검증되지 않은 자연어를 Feature 로 직접 사용 |
| AI QA | Claim Graph, Citation, Numeric Ref, Prompt·Model·Tool Trace | 축약 Summary 만 보고 승인 |
| CEO | 기획 포트폴리오 현황, 발행 건수, 기각 사유 분포 | 원문 전체와 Tick 상세 |
| Trading | **없음** — 승격된 전략의 시그널만 받는다 | 리서치 산출물을 매매 근거로 사용 |
| Risk | **없음(승격 심사 시 ExperimentCard 로 받는다)** | Research 방향을 Risk 승인으로 간주 |
| Frontend 사용자 | Holding Brief(보유 종목 질의 응답), 근거 펼치기, 상태와 만료 | 내부 Prompt, Secret, 권한 없는 원문. **이 답변은 공장 입력이 아니다** |

### 7.1 Quant Handoff

정본 `ExperimentProposalV1` 이 곧 Handoff 다. 이전 판의 `hypothesis_seeds[]` 가 정식 계약으로
승격된 것이고, 필드는 다음을 반드시 포함한다.

- 출처 `lead_id` 와 원문 참조(URL·발행일·접근시각·발췌)
- 경제적 근거(**반대편 주체 명시**)와 경쟁 설명(통제 어휘 코드 >=1, 독립 회의론자 서명)
- 통제 어휘로 사상된 `edge_type` / `universe_key` / `label` / `baseline`
- 데이터 요구(테이블, 최소 히스토리)와 PIT 제한
- 반증 검사 >=1
- `prior_check` — 같은 trial family 의 기각 이력과 `lessons_addressed`

Quant 는 이를 그대로 채택하지 않고 Gate 0(중복·예산 검사)을 통과시킨 뒤 `HypothesisSpecV2`
로 사전 등록하고 독립 검증한다. **기획안은 실험의 입력이지 결론이 아니다.**

## 8. 생성 Workflow

```text
1. Event Intake와 ResearchCaseV2 생성
2. Point-in-Time Cutoff Lock
3. EvidenceManifestV1 생성
4. Specialist별 AnalystFinding 생성
5. Claim 정규화와 중복 제거
6. Claim-Evidence-Citation-Numeric-Time 검증
7. Causal Path와 Horizon별 Outlook 생성
8. Scenario와 Dissent 독립 생성
9. Skeptic가 반례와 Evidence Gap 검사
10. Decision Readiness 결정
11. ResearchPacketV3 정본 발행
12. Brief와 Consumer View 결정론적 생성
13. Catalyst/Invalidation 감시 등록
14. Delta, Retraction과 Outcome 채점
```

Hermes는 Case의 우선순위, 업무 배정, 재시도, 예산, Deadline과 발행 Lifecycle을 관리한다. LangGraph는 2~10단계의 Case Workflow와 Checkpoint를 관리한다.

## 9. 직원별 Output 책임

| Agent | 책임 필드 |
|---|---|
| `RES-00` Research Editor | Lead 채택·보류·폐기, `prior_check`, 발행 판단, 기획 포트폴리오 |
| `RES-11` Academic Scout | Lead(논문·프리프린트), 원문 참조, 저자가 밝힌 실패 조건 |
| `RES-12` Practitioner Scout | Lead(서한·실무자 글), 서술된 메커니즘, 인용/추론 구분 |
| `RES-13` Community Scout | Lead(커뮤니티·영상·저장소), 독립 언급 수, 규칙 서술 가능성 |
| `RES-14` Cross-domain Scout | Lead(타 분야), 전용(轉用) 논증, 구조적 전제 |
| `RES-15` Competing Explanation | 경쟁 설명, 반증 검사 제안, 회의론자 서명 |
| `RES-16` Experiment Planner | 통제 어휘 사상, 데이터 요구, 파라미터 범위, 어휘 공백 요청 |
| `RES-17` Market Context | 유니버스 실재·히스토리 길이·유동성·DQ 공백 (**실행 가능성 재료**) |
| `RES-18` Holdings Analyst | Holding Brief, 인용, 미해결 질문 (**서비스 전용 — 공장 미연결**) |
| `RES-08` RAG/Evidence Curator | Evidence Manifest, Citation Resolution, Source Tier, Retraction, 승격 판정 |

> 구 `RES-01~07`, `RES-09`(종목 애널리스트)는 2026-08-10 운영에서 내렸다. 코드는 감사
> 계보로 남지만 이 표의 책임 필드를 갖지 않는다.

Editor 는 Scout 의 인용을 다시 쓰거나 회의론자의 반대 의견을 삭제하지 않는다. 필수 항목이
빠지면 성공한 Lead 는 보존하되 기획안은 `READY_WITH_LIMITS` 또는 `INSUFFICIENT_EVIDENCE`
로 종료한다 — **발행하지 않는 것이 해석 불가능한 실험보다 싸다.**

## 10. 발행 전 Quality Gate

| Gate | P0 규칙 | 실패 시 처리 |
|---|---|---|
| Schema | Pydantic Extra Forbid와 JSON Schema 통과 | 발행 차단 |
| PIT | 모든 중요 입력이 `as_known_at`을 만족 | Historical은 차단, Live는 제한 상태 |
| Citation | 모든 Material Fact의 Evidence ID와 원문 위치 확인 | 발행 차단 |
| Numeric | 표시 수치와 Deterministic Metric Ref가 일치 | 발행 차단 |
| Coverage | Critical·High Claim Evidence Coverage 100% | `INSUFFICIENT_EVIDENCE` |
| Freshness | 데이터 유형별 TTL과 Source Lag 표시 | `READY_WITH_LIMITS` 또는 차단 |
| Contradiction | 충돌을 해소하거나 Dissent로 명시 | 미기록 충돌은 차단 |
| Rationale | 경제적 근거가 **반대편 주체**를 명시 (`counterparty` non-null) | 발행 차단 |
| Competing Explanation | 통제 어휘 코드 >=1 + 독립 회의론자 서명(`skeptic_sign`) 존재 | 발행 차단 |
| Falsification | 반증 검사 >=1 존재 | 발행 차단 |
| Vocabulary | `edge_type`·`universe_key`가 통제 어휘로 사상됨 | `UNMAPPED_VOCAB` 반려 (자유 서술 금지) |
| Prior-art | 같은 trial family 의 기각 `lesson_codes` 마다 `lessons_addressed` 대응 존재 | `DUPLICATE_UNADDRESSED` 반려 |
| Budget | `trials_used < trial_budget` | `OVER_BUDGET` — CEO 증액 결정 없이 재접수 불가 |
| Permission | 원문 License, PII, Secret과 Tool Scope 확인 | 발행 차단 |
| Consumer Contract | Quant Fixture 로 역직렬화 | Event 발행 차단 |

Rationale·Competing Explanation·Falsification·Vocabulary 네 Gate 는 **의미 품질을 판정하지
않는다.** "그럴듯한 근거인가"는 어차피 실험이 판정한다. 이 Gate 들이 보는 것은 답해야 할
질문에 답을 적었는가 하나이고, 그래서 결정론 코드로 검사할 수 있다.

## 11. Packet Lifecycle과 Event

### 11.1 상태

```text
DRAFT
  -> VALIDATING
  -> PUBLISHED
  -> SUPERSEDED
  -> EXPIRED

VALIDATING -> INSUFFICIENT_EVIDENCE
PUBLISHED  -> RETRACTED
```

### 11.2 Event

- `research.packet.published.v3`
- `research.packet.updated.v3`
- `research.packet.superseded.v3`
- `research.packet.retracted.v3`
- `research.rule.triggered.v1`
- `research.outcome.scored.v2`

모든 Event에는 `packet_id`, `case_id`, `trace_id`, `schema_version`, `as_known_at`, `occurred_at`, `artifact_uri`와 `content_hash`를 포함한다.

### 11.3 저장 원칙

- V3 JSON 원본은 불변 Object Artifact로 저장한다.
- Supabase에는 Packet Index, 상태, Claim, Evidence Link, Rule과 Outcome을 정규화해 저장한다.
- TimescaleDB에는 시계열 원천과 계산 Feature를 저장하고 Packet에는 Metric ID만 연결한다.
- 수정은 기존 Packet 덮어쓰기가 아니라 새 `packet_id` 발행과 `parent_packet_id` 연결로 처리한다.
- 원문 정정과 Retraction은 관련 Claim, Packet, Quant Hypothesis와 Strategy Candidate까지 영향 목록을 전파한다.

## 12. 사후 평가와 Hermes 자기 개선

### 12.1 평가 단위

| 단위 | 평가 항목 |
|---|---|
| Claim | 사실 인용 정확성, 수치 일치, 반증 여부 |
| Forecast | Brier Score, Calibration Error, 방향·구간 적중 |
| Method | `method_key × event_type × horizon × regime`별 성과 |
| Analyst | Coverage, 오류, 지연, 유용한 Dissent 비율 |
| Packet | 사후 정정률, Expiry 적절성, 소비 본부 사용률 |
| Pipeline | Latency, 비용, 실패·재시도와 Tool 오류 |

### 12.2 개선 절차

```text
Outcome 축적
  -> 방법·Agent·Case Cohort별 평가
  -> Improvement Candidate 생성
  -> Historical Replay와 고정 Fixture Eval
  -> Shadow 비교
  -> AI QA와 Agent Workforce 승인
  -> Skill/Prompt/Model Version 승격
  -> 문제 발생 시 이전 Version Rollback
```

Hermes Memory에는 원문 보고서를 무제한 저장하지 않는다. 검증된 실패 패턴, Source 신뢰도 변화, 반복되는 Evidence Gap과 승인된 Skill 개선만 저장한다. Agent가 자기 Prompt나 Weight를 직접 수정하고 즉시 Production에 적용하는 것은 금지한다.

## 13. 구현 우선순위

### Phase RO-0: 계약과 Fixture 고정

목표 기간: 3~5 개발일

- `ResearchPacketV3`, `ClaimV2`, `ScenarioV1`, `RuleV1` Pydantic Schema 작성
- V2-to-V3 Adapter 작성
- 단일 주식, Pair, 공시 Event, Options Surface Fixture 각 1개 작성
- JSON Schema와 하위 호환 Contract Test 추가
- 기존 Markdown Report를 V3에서 생성하는 Renderer 설계

완료 기준은 같은 Fixture가 JSON, Markdown과 Consumer View에서 같은 Claim ID와 수치를 표시하는 것이다.

### Phase RO-1: Evidence와 발행 Gate

목표 기간: 1~2주

- Evidence Manifest와 Claim-Evidence Table 연결
- Citation, Numeric, PIT, Freshness, Coverage Validator 구현
- Materiality와 Source Tier 규칙 도입
- `DecisionReadinessV1`과 Fail-closed 발행 정책 구현
- `research.packet.published.v3` Event와 Artifact 저장 구현

완료 기준은 근거 없는 Material Fact, 다른 수치와 Cutoff 위반 Packet이 발행되지 않는 것이다.

### Phase RO-2: 전망·시나리오와 Consumer View

목표 기간: 1~2주

- Horizon·Target Variable별 Outlook 구현
- Causal Path, Scenario, Catalyst와 Invalidation Rule 구현
- CEO, Trading, Quant, Risk와 QA Projection API 구현
- Quant용 `hypothesis_seeds[]`와 Risk용 Scenario Matrix 구현
- AI Office에서 Brief, Evidence Drill-down과 Delta 표시

완료 기준은 각 본부가 자유 서술을 재해석하지 않고 V3 ID와 필드만으로 다음 업무를 시작하는 것이다.

### Phase RO-3: Delta, Monitoring과 Outcome

목표 기간: 1~2주

- `ResearchPacketDeltaV1`과 Parent/Supersede 관계 구현
- Catalyst·Invalidation 감시 Worker 구현
- Retraction Impact 전파 구현
- Outcome Scorer를 Scenario, Event와 Forecast Type까지 확장
- Brier, Calibration Error와 방법별 Cohort Dashboard 구현

완료 기준은 Packet 발행부터 갱신, 만료, 정정과 사후 채점까지 Lifecycle이 자동으로 이어지는 것이다.

### Phase RO-4: 제한된 자기 개선

선행 조건: 최소 표본, QA 승인 절차와 Replay 환경 확보

- `method_key × regime × horizon`별 성과를 Routing 후보에 반영
- 반복되는 Evidence Gap을 Retrieval Plan 후보로 생성
- Prompt, Model과 Tool 조합을 Champion/Challenger로 비교
- 성과 저하 시 자동 Rollback 요청 생성

표본이 부족한 Cohort에는 학습 Weight를 적용하지 않는다. 최신 Preprint나 LLM 평가만으로 Production Weight를 바꾸지 않는다.

## 14. 초기 SLO와 품질 지표

아래 값은 첫 Load Test 후 조정하는 초기 목표다.

| 지표 | 초기 목표 |
|---|---|
| Critical·High Fact Evidence Coverage | 100% |
| Citation Resolution 성공률 | 100% |
| Numeric Ref 불일치 | 0건 |
| PIT 필수 필드 누락 | Published Packet 0건 |
| Quick Packet Latency | p95 120초 이내 |
| Deep Packet Latency | p95 15분 이내 |
| Retraction 영향 전파 | 5분 이내 |
| Consumer Contract Fixture | 100% 통과 |
| Packet-to-Hypothesis Lineage | Quant 실험 100% |
| Forecast Calibration | Cohort 표본 100건 이상부터 공식 비교 |

정확한 전망 비율만으로 리서치 품질을 평가하지 않는다. Citation 정확성, 데이터 시점, 반론 보존, 지연, 정정률과 후속 본부 사용성도 함께 평가한다.

## 15. P0 구현 Backlog

| ID | 작업 | Owner | 의존성 | 완료 증거 |
|---|---|---|---|---|
| `RO-01` | V3 계약과 JSON Schema | 리서치본부 | 현재 V2 Contract | Schema Test |
| `RO-02` | V2-to-V3 Adapter | 리서치본부 | `RO-01` | 기존 Report Fixture 변환 |
| `RO-03` | Claim/Evidence/Numeric Validator | 리서치 + AI QA | Evidence Store | 실패 Fixture 차단 |
| `RO-04` | Readiness와 Lifecycle | 리서치 + 공통 Platform | Artifact/Event 계약 | 상태 전이 Test |
| `RO-05` | Brief Renderer | 리서치 + Frontend Platform | `RO-01` | JSON/화면 수치 일치 |
| `RO-06` | Trading/Quant/Risk/QA View | 각 소비 본부 | `RO-01`, `RO-04` | Consumer Contract Test |
| `RO-07` | Delta와 Retraction | 리서치 + AI QA | Versioned Packet | 영향 전파 Test |
| `RO-08` | Outcome Scorecard V2 | 리서치 + 퀀트 | Outcome History | Brier·Cohort Dashboard |

## 16. 최종 완료 정의

리서치 Output 고도화는 다음 조건이 모두 충족되어야 완료로 본다.

- **퀀트가 기획안을 받아 추가 협의 없이 사전 등록할 수 있다.** (완료 정의 1번은 사람이 읽는
  문서가 아니라 기계가 실행할 수 있는 기획안이다)
- 모든 중요 Fact 가 원문 위치와 시각까지 역추적된다.
- 모든 기획안이 반대편 주체·경쟁 설명·반증 검사·회의론자 서명을 갖는다.
- `edge_type`·`universe_key` 가 통제 어휘로만 표현되고, 미사상은 어휘 등재 요청으로 처리된다.
- 발행 전에 같은 trial family 의 기각 이력이 조회되고, 대응 없는 재도전이 차단된다.
- 실험 결과(성공·기각·킬)가 `ExperimentOutcomeV1` 통제 어휘로 돌아와 Gate 0 에서 기계 대조된다.
- Lead 에서 기획안, 사전등록 가설, 실험 카드까지 계보가 끊기지 않는다.
- 기획안이 갱신·만료·정정될 때 퀀트에 Event 가 전달된다.
- Hermes 의 개선안은 Replay, Shadow, QA 승인과 Rollback 을 거친 뒤에만 적용된다.
- 사용자 질의 응답(Holding Brief)은 제공되되, 어떤 기획안의 근거로도 인용되지 않는다.

이 구조가 완성되면 리서치본부의 산출물은 읽기 좋은 리포트가 아니라 **회사가 실행할 수 있는
실험 계약**이 된다. 리포트는 그 부산물로 언제든 렌더링할 수 있지만, 조직의 목표가 되지는 않는다.
