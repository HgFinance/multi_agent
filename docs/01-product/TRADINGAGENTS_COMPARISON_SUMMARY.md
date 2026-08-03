# TradingAgents 대비 기술적 차별점 요약

> 비교 기준일: 2026-08-02  
> 비교 대상: [TradingAgents](https://github.com/TauricResearch/TradingAgents), [TradingAgents 논문](https://arxiv.org/abs/2412.20138)  
> 판정 원칙: 설계 목표와 현재 구현을 분리한다.

## 결론

TradingAgents는 **한 종목·한 시점의 투자 판단을 만드는 연구용 멀티에이전트 프레임워크**다. 우리 프로젝트는 이를 넘어 **투자 조직의 데이터·판단·통제·회계·감사·전략 수명주기를 운영하는 플랫폼**을 목표로 한다.

다만 현재 우리 프로젝트의 차별점은 완성된 제품 기능보다 **아키텍처와 통제 설계의 깊이**에 있다. 실시간 데이터 수집과 결정론적 핵심 모듈은 검증됐지만, 전 종목 이벤트부터 Paper 체결·원장·NAV까지의 End-to-End 운영 경로는 아직 완성되지 않았다.

## 핵심 차이

| 영역 | TradingAgents | 우리 프로젝트의 차별점 | 현재 판정 |
|---|---|---|---|
| 문제 단위 | 종목별 분석·토론·거래 판단 | 전 종목 Data Plane과 Investment Case 운영 | 수집·분석 부분 구현, 전체 경로 미완료 |
| 실행 통제 | Agent가 거래 결정을 만드는 연구 흐름 | `Agent Decision → Strategy Signal → OrderIntent → Order` 분리, 결정론적 Risk/OMS 적용 | 계약·Risk·OMS·Paper Broker 구현, 통합 진행 중 |
| 회계 책임 | 핵심 범위가 아님 | Fill에서 Ledger·Position·Reconciliation·NAV로 이어지는 공식 수치 경계 | Ledger·대사 구현, Canonical 거래/체결 데이터는 감사 시 0건 |
| 조직 구조 | Analyst → Debate → Trader → Risk/Portfolio의 단일 흐름 | CEO, Research, Trading, Risk, Quant, Accounting, QA, HR의 권한 분리와 Hermes/LangGraph 2계층 | Profile·워크플로우 설계, 실제 직원 Runtime 연결은 부분적 |
| 전략 수명주기 | 판단 생성·실험 중심 | PIT Backtest → Walk-Forward → QA → CEO Gate → Shadow/Paper → Rollback | Quant 실험·PIT·Walk-Forward 부분 구현, 배포/드리프트 운영 미완료 |
| 신뢰성 | 연구 실행 재현성 중심 | Fail-closed, Audit/Replay, Kill Switch, stale·중복·브로커 불명확 상태 차단 | 핵심 규칙과 일부 API/테스트 구현, 운영 E2E 미완료 |
| 시장 특화 | 미국 주식·암호화폐 데이터 중심의 공개 예제 | KRX·LS API·KRW·국내 공시/뉴스 데이터 계약 | LS 수집·Timescale 적재 검증, 전체 KRX Universe 운영은 미완료 |

## 우리 프로젝트의 실제 강점

1. **통제면(Control Plane)이 명확하다.** LLM은 근거와 제안을 만들고, 한도·상태 전이·멱등성·회계 확정은 결정론적 코드가 담당한다. Risk Agent가 최종 권한을 갖지 않고 Risk Engine이 강제하는 점이 핵심이다.

2. **실패 방향이 안전하다.** Retry가 소진되거나 데이터가 stale이거나 브로커 상태가 불명확하면 승인·주문 확대가 아니라 `HOLD`, `REJECT`, `DENY`, `ESCALATE`, `ROLLBACK`으로 끝나도록 설계돼 있다.

3. **판단과 금융 사실을 분리한다.** OrderIntent와 Order를 구분하고, Position·PnL·NAV는 Fill과 Ledger/Valuation 경로에서만 확정한다. 이는 자연어 답변을 금융 원장으로 오인하는 위험을 줄인다.

4. **조직의 독립 검증이 가능하다.** QA/감사본부가 Risk·Trading과 분리되어 근거, 재현성, 데이터 누수, 환각과 Model Risk를 검토하도록 설계돼 있다. `compliance-policy-agent`의 PIT·인용 검증 baseline도 존재한다.

5. **전 종목을 모두 LLM으로 호출하지 않는 구조를 지향한다.** Feed·Feature·Priority Queue가 전체 Universe를 감시하고, 중요한 이벤트만 Agent 분석으로 승격한다. 이 방향은 비용과 지연을 관리하는 데 유리하지만, 현재 Priority/Event Engine은 남은 구현 과제다.

6. **한국 시장 운영 맥락을 제품 경계에 포함한다.** KRX 거래 단위·호가·거래일·LS API·국내 공시와 원화 회계를 처음부터 계약에 넣을 수 있다. 단순히 미국 데이터 어댑터를 교체하는 것보다 실제 운용 차이에 대응하기 쉽다.

## 냉정한 평가

- **개념적 차별성: 높음.** “멀티에이전트가 종목을 토론한다”는 차별점이 아니다. 차별점은 조직 권한, 결정론적 금융 통제, 회계 확정, 감사와 전략 승격을 하나의 계약 체계로 묶는 데 있다.
- **현재 제품 완성도: 중간 이하.** 리서치·데이터 수집, Quant 실험, Risk/OMS/Ledger의 개별 조각은 강해졌지만, Canonical DB의 실행·Risk·회계 데이터와 본부 간 공식 Event 연결이 병목이다.
- **기술적 방어력: 통합되면 높음.** 개별 기능인 RAG, Backtest, Risk, OMS, WebSocket은 각각 대체재가 있다. 방어력은 기능 목록이 아니라 `계약 버전 + 권한 경계 + 감사 추적 + 재현 가능한 승격 Gate`가 함께 작동할 때 생긴다.
- **복잡성 비용: 높음.** TradingAgents보다 운영면이 넓어 장애 지점, 테스트 부담, 데이터 품질 책임, 비용과 지연이 모두 커진다. 따라서 “기능이 많다”보다 “안전한 하나의 Case를 끝까지 재현한다”를 우선해야 한다.

## 비교문에서 낮춰 써야 하는 표현

- “전 종목 실시간 헤지펀드 플랫폼” → **전 종목 실시간 운용을 목표로 하는 초기 구현 플랫폼**
- “TradingAgents에는 Risk Engine/OMS/Ledger가 없다” → **TradingAgents 공개 baseline의 핵심은 연구 판단 흐름이며, 우리처럼 독립적인 결정론적 Risk·OMS·Ledger Control Plane을 제품 경계로 두지는 않는다**
- “2~10초 판단 지연” → **목표 SLO**. 실측 전에는 성능 차별점으로 주장하지 않는다.
- “Bedrock Production + Ollama” → 2026-08-03 Git에는 Nous Profile 6개와 미승인 OpenAI-Codex Risk·QA Profile 2개가 섞여 있다. Profile Contract Check도 실패하므로 실제 운영 Provider가 확정되기 전에는 목표 스택으로만 표기한다.
- “전 종목 실시간 파이프라인 구현” → LS 실시간 수집과 적재 검증 및 설계는 있으나, 전 종목 Feature·Priority Queue·Agent Router·Paper 체결까지 연결됐다고 쓰지 않는다.

## 차별성을 증명할 최소 Acceptance Scenario

동일한 하나의 Case를 다음 경로로 재현할 수 있어야 한다.

```text
시장 이벤트
  → Research Packet(versioned)
  → OrderIntent
  → RiskDecision(input hash / calculation version)
  → OMS 멱등 상태 전이
  → Paper Fill
  → Ledger / Position / Reconciliation
  → PnL·NAV Read Model
  → QA·Audit Trace / Replay
```

이 시나리오에 데이터 단절, Risk Engine timeout, 중복 Intent, 브로커 `UNKNOWN`, 부분 체결, Ledger Break를 넣었을 때 항상 신규 진입 차단과 감사 가능한 결과가 나와야 한다. 이 통과 전까지는 TradingAgents보다 “운용 가능하다”고 결론 내리지 않고, **운용 가능성을 입증하는 설계와 부분 구현을 보유하고 있다**고 표현한다.

## 최종 판단

우리 에이전트의 가장 큰 장점은 더 많은 Agent나 더 그럴듯한 투자 의견이 아니다. **LLM의 불확실성을 금융 통제 시스템 안에 가두고, 조직의 판단을 회계·감사·재현성까지 연결하려는 점**이다.

TradingAgents가 좋은 Case-level Research Engine이라면, 우리 프로젝트의 목표는 **운용 가능한 Personal Hedge Fund Operating System**이다. 승부처는 새로운 페르소나를 추가하는 것이 아니라, 위 Acceptance Scenario를 실제 Event·DB·Worker·UI까지 끊김 없이 통과시키는 것이다.

근거: [프로젝트 구현 현황](../PROJECT_IMPLEMENTATION_STATUS.md), [기존 오픈소스 비교 문서](MULTI_AGENT_TRADING_COMPETITIVE_ANALYSIS.md), [Risk Engine](../../departments/03-risk/engine/risk_engine.py), [OMS](../../departments/02-trading/oms/oms.py), [Ledger](../../departments/05-accounting-portfolio/ledger/ledger.py), [워크플로우 정의](../../multi-agent-workflow.yaml)
