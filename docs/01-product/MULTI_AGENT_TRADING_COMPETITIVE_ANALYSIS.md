# 멀티 에이전트 트레이딩 오픈소스 비교와 차별화 전략

> 기준일: 2026-07-31
> 비교 대상: 공개 GitHub 저장소의 기본 브랜치, README, 공개 문서와 코드 구조
> 목적: 투자 수익률 순위를 매기는 것이 아니라, 우리 서비스가 무엇을 만들고 어디에서 차별화해야 하는지 결정한다.
> 전사 적용: [Personal Hedge Fund Agent 전사 고도화 연구 로드맵](WHOLE_SYSTEM_ADVANCEMENT_ROADMAP.md)

## 1. 결론부터 보기

우리 서비스의 차별점은 `멀티 에이전트가 종목을 토론한다`는 사실 자체가 아니다. 이 구조는
TradingAgents, FINCON과 여러 파생 프로젝트가 이미 사용하고 있다. 실시간 데이터, 자동 주문,
장기 기억, 백테스트도 각각 잘 구현한 공개 프로젝트가 존재한다.

우리가 만들려는 제품의 차별점은 다음 기능을 **하나의 통제된 운영 시스템으로 연결하는 것**이다.

1. 한국 시장 전 종목을 실시간으로 감시한다.
2. 중요한 사건만 선별해 멀티 에이전트 투자위원회에 전달한다.
3. 조사, 반대 검토, 포트폴리오 제안, 위험 심사와 주문을 서로 다른 책임으로 분리한다.
4. LLM은 가설과 설명을 만들고, Risk Engine, OMS, Ledger는 결정론적 코드가 통제한다.
5. 하나의 투자 판단을 근거 데이터부터 체결, 손익, 사후 평가까지 `Investment Case`로 추적한다.
6. 전략과 Agent Skill을 Backtest, 독립 검증, Shadow, Paper, 승인, 배포와 Rollback 절차로 개선한다.
7. 회계, 감사, 인사와 운영 현황까지 AI Office에서 관리한다.

따라서 제품을 다음과 같이 설명하는 것이 가장 정확하다.

> **개인 투자자를 위해 헤지펀드 조직의 조사, 전략, 리스크, 집행, 회계, 감사와 개선을 운영하는
> Personal Hedge Fund Operating System**

다른 프로젝트가 여러 Agent에게 `무엇을 살지` 묻는 시스템에 가깝다면, 우리는 그 판단을 만들고
검증하고 승인하고 집행하고 회계·감사하며 개선하는 **회사 운영 구조**를 만든다.

이 조직 구조만으로는 충분하지 않다. 장기적인 제품 해자는 모든 Agent, Source, Claim과 전략이
사전에 남긴 예측을 실제 결과로 채점하고, 그 기록을 다음 조사·전략·자본 배분에 재사용하는
`Investment Intelligence Ledger`에서 만든다. 구체적인 연구 근거와 구현 순서는
아래 12장에 정리한다.

## 2. 비교할 때 주의할 점

- 공개 README에 적힌 기능과 실제 운영 안정성은 같지 않다.
- `Live Trading 지원`은 주문 경로가 있다는 뜻이지, 손실 통제와 장애 복구가 검증됐다는 뜻은 아니다.
- 논문 수익률과 Demo 결과는 데이터 누수, 거래 비용, 시장 충격과 운영 장애를 모두 증명하지 않는다.
- 아래 평가는 보안 감사, 성능 검증 또는 투자 성과 검증이 아니다.
- 우리 서비스도 아직 Production 서비스가 아니다. 아래에서 `구현`, `부분`, `계획`을 구분한다.

표의 기호는 다음 의미로 사용한다.

| 기호 | 의미 |
|---|---|
| `●` | 공개 문서와 코드에서 핵심 기능으로 확인 |
| `△` | 제한적 지원, 개발 중 또는 Roadmap |
| `-` | 핵심 범위가 아니거나 공개 근거를 확인하지 못함 |

## 3. 프로젝트 지형

### 3.1 직접 비교 대상

| 프로젝트 | 서비스의 최소 단위 | 강점 | 우리 관점의 주요 공백 |
|---|---|---|---|
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | 한 종목에 대한 분석, 토론과 거래 결정 | 실제 투자회사 역할 구조, Bull/Bear 토론, Risk·Portfolio 승인, LangGraph Checkpoint, 구조화 출력 | 회사 전체의 회계·인사·감사 운영과 한국 시장 전 종목 Event 처리 |
| [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | 유명 투자자 Persona와 분석 Agent의 포트폴리오 판단 | 이해하기 쉬운 Demo, 다양한 투자 스타일, Risk·Portfolio Agent | 현재 교육용 POC 성격이며 실제 주문을 하지 않음. Persona 의견과 공식 통제의 경계가 약함 |
| [TradingGoose](https://github.com/TradingGoose/TradingGoose.github.io) | 실시간 사건 기반 다종목 분석과 Alpaca 주문 | Full-stack UI, Supabase Auth/RBAC/RLS, Paper·Live, 승인 화면, 실시간 Workflow | TradingAgents 계열 투자 분석 제품에 가깝고 Ledger·NAV·인력 개선 조직은 제한적 |
| [AlpacaTradingAgent](https://github.com/huygiatrng/AlpacaTradingAgent) | TradingAgents 판단을 Alpaca Paper·Live로 실행 | 주식·Crypto, 예약 실행, UI, Memory와 Workflow Resume | 특정 Broker 실행 확장에 집중. 회사 운영과 독립 회계·감사 구조는 제한적 |
| [AutoHedge](https://github.com/The-Swarm-Corporation/AutoHedge) | Director → Quant → Risk → Execution 파이프라인 | 간결한 자동 실행 구조, JSON 계약, Crypto Venue 연결 | 소수 Agent의 선형 구조이며 조직 통제, 공식 원장과 데이터 거버넌스가 제한적 |
| [AgenticTrading](https://github.com/Open-Finance-Lab/AgenticTrading) | Planner가 Data·Alpha·Risk·Portfolio·Execution Agent Pool을 동적 DAG로 구성 | Agent Registry, 동적 Orchestration, Transaction Cost, Backtest, Audit와 Memory를 포괄 | 우리와 가장 가까운 개념. 한국 시장 데이터, 공식 회계 원장, 인사·QA 조직과 운영 UI가 차별화 지점 |
| [RD-Agent-Quant](https://github.com/microsoft/RD-Agent) | Agent가 Factor·Model 가설을 만들고 코드·Backtest 결과로 반복 개선 | 자동 Quant R&D, Factor·Model 공동 최적화, Feedback Loop와 Bandit Scheduler | 자동 전략 연구 자체는 더 이상 차별점이 아님. 독립 검증, 운영 원장, Production 승격과 조직 Credit Assignment가 우리 차별화 과제 |

### 3.2 연구·기술 인접 대상

| 프로젝트 | 참고할 핵심 | 직접 경쟁 대상이 아닌 이유 |
|---|---|---|
| [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | 전문 Equity Research Report, DataOps·LLMOps 계층, Smart Scheduler | 조사와 보고서 생성 중심이며 주문·원장 수명주기가 중심이 아님 |
| [FINCON 공개 구현](https://github.com/MXGao-A/FAgent) | Manager-Analyst 계층, CVaR, Episode 비교, Verbal Reinforcement와 다층 Memory | 자기개선 연구에는 중요하지만 공개 구현은 개발 중이며 Production 승격 통제가 중심이 아님 |
| [FinMem](https://github.com/pipiku915/FinMem-LLM-StockTrading) | 중요도·기간별 Memory, 거래 결과 기반 기억 갱신 | 단일 LLM Trading Agent 연구로 조직형 멀티 에이전트가 아님 |
| [FinRL-X](https://github.com/AI4Finance-Foundation/FinRL-Trading) | 동일한 전략 계약으로 Backtest, Paper와 Live를 연결하는 Quant 수명주기 | LLM 조직형 Agent보다 RL·Quant Trading Infrastructure에 초점 |
| [Polymarket Agents](https://github.com/Polymarket/agents) | RAG 조사부터 주문 서명·실행까지 연결된 Vertical Agent | Prediction Market이라는 특정 시장에 한정 |

### 3.3 Universe 비교

`Universe`는 시스템이 감시하거나 거래 대상으로 삼는 자산의 집합이다. 여러 프로젝트가
`Multi-stock` 또는 `Real-time`을 지원하더라도, 모든 종목을 계속 감시하는지 사용자가 입력한 몇
종목만 분석하는지는 다르다.

| 프로젝트 유형 | 일반적인 Universe 운영 방식 |
|---|---|
| TradingAgents·ai-hedge-fund | 사용자가 지정한 Ticker 목록을 깊게 분석 |
| TradingGoose·AlpacaTradingAgent | Broker가 지원하는 다종목 Watchlist와 Portfolio를 예약·실시간 분석 |
| AutoHedge | 연결된 Crypto Venue와 Token 중심 |
| Polymarket Agents | Polymarket의 Prediction Market 중심 |
| FinRL-X | Dataset과 전략이 정의한 주식·Crypto 자산 집합 |
| 우리 서비스 | KRX 전 종목 감시를 시작점으로 삼고, 데이터 품질·유동성·거래 권한에 따라 단계적으로 축소 |

우리 서비스에서는 다음 네 집합을 분리한다.

| 구분 | 의미 |
|---|---|
| Monitoring Universe | LS WebSocket과 보조 Source로 상태를 감시하는 전체 종목 |
| Eligible Universe | 데이터 품질, 유동성, 거래정지와 규정 조건을 통과한 종목 |
| Tradable Universe | 계좌, Broker, 공매도·파생상품 권한과 Risk Mandate상 주문 가능한 종목 |
| Active Universe | 현재 전략이 Signal을 계산하고 Position 후보로 사용하는 종목 |

`전 종목 실시간 판단`은 Monitoring Universe 전체에 LLM을 호출한다는 뜻이 아니다. 결정론적
Event Engine이 전체를 감시하고, Eligible·Tradable 조건을 통과한 중요한 사건만 Agent 판단으로
승격한다.

## 4. 기능별 비교

이 표는 각 저장소가 무엇을 **주요 제품 범위로 공개하고 있는지** 보여준다. 세부 구현 완성도를
동일하다고 가정하지 않는다.

| 프로젝트 | 다중 역할·토론 | 실시간·다종목 | Paper·Live | Backtest 수명주기 | Memory·학습 | 결정론적 Risk | 회계·대사 | QA·운영 조직 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| TradingAgents | ● | △ | △ | △ | ● | △ | - | △ |
| ai-hedge-fund | ● | △ | - | △ | △ | △ | - | - |
| TradingGoose | ● | ● | ● | △ | ● | △ | - | △ |
| AlpacaTradingAgent | ● | ● | ● | △ | ● | △ | - | △ |
| AutoHedge | ● | ● | ● | △ | △ | ● | - | △ |
| AgenticTrading | ● | ● | ● | ● | ● | ● | △ | ● |
| RD-Agent-Quant | ● | - | - | ● | ● | △ | - | △ |
| FinRobot | ● | △ | - | △ | △ | - | - | ● |
| FINCON | ● | △ | △ | ● | ● | ● | - | △ |
| FinMem | - | △ | △ | ● | ● | △ | - | - |
| FinRL-X | - | ● | ● | ● | ● | ● | △ | △ |
| 우리 서비스 목표 | ● | ● | Paper 우선 | ● | ● | ● | ● | ● |

## 5. 프로젝트별로 배울 점

### 5.1 TradingAgents

TradingAgents는 우리 서비스와 가장 직접적으로 비교되는 기준점이다. Fundamental, Sentiment,
News와 Technical Analyst가 조사하고, Bull·Bear Researcher가 논쟁하며, Trader가 거래안을 만든 뒤
Risk Team과 Portfolio Manager가 최종 결정을 내린다.

배울 점:

- Agent 출력에 명시적인 Schema와 검증을 적용한다.
- LangGraph Checkpoint와 Resume로 중단된 Case를 복구한다.
- Model Provider Registry로 Claude, Ollama와 다른 모델을 교체 가능하게 한다.
- 동일 입력을 재현할 수 있도록 Data Snapshot과 Decision Log를 고정한다.

우리의 추가 범위:

- 종목별 수동 실행이 아니라 전 종목 Event Engine이 분석 우선순위를 정한다.
- 승인 이후 OMS, 체결, Position, Ledger, NAV와 Reconciliation까지 Case를 이어간다.
- 감사와 Agent Workforce 개선을 독립 조직으로 둔다.

### 5.2 ai-hedge-fund

유명 투자자의 스타일을 Agent Persona로 표현해 금융 비전공자도 결과를 쉽게 이해하게 만든다.
하지만 유명인의 말투와 이름은 쉽게 복제할 수 있고 성과 원인을 측정하기 어렵기 때문에 제품
해자가 되기 어렵다. 우리 프로젝트에서는 Persona를 `Investment Doctrine`으로 대체한다.

배울 점:

- 투자 철학을 `평가 기준`, `금지 조건`, `시간 지평`, `허용 근거`와 `무효화 조건`으로 명시한다.
- 전략별 판단을 동일 포트폴리오 화면에서 비교한다.
- 새로운 Alpha Model을 Plugin처럼 추가한다.

적용 원칙:

- `InvestmentDoctrine`은 이름이나 말투가 아니라 Version이 있는 평가 정책이다.
- 모든 Doctrine은 동일한 Frozen Case에 사전 확률 예측을 제출하고 Brier Score, Calibration,
  Evidence Coverage와 Risk 위반으로 비교한다.
- 유명 투자자의 철학은 의사결정 권한자가 아니라 `Strategy Reviewer` 또는 `Research Lens`의
  참고 자료로만 사용한다.
- 공개 서한·인터뷰에서 검증 가능한 규칙을 추출하되, 사용권한과 원문 Citation을 기록한다.
- 저작권, 상표, 인물 오인과 투자 권유 표현을 검토해야 한다.

### 5.3 TradingGoose와 AlpacaTradingAgent

두 프로젝트는 TradingAgents식 판단을 실제 사용자 화면, Scheduler와 Broker API로 연결하는 방법을
보여준다. TradingGoose는 Supabase 기반 Auth, RBAC와 실시간 화면도 제공한다.

배울 점:

- `Paper/LIVE` Mode를 화면 전체에서 명확하게 표시한다.
- 사람 승인, 자동 실행과 Kill Switch를 같은 운영 화면에서 관리한다.
- Agent 진행 상황과 근거를 실시간으로 보여준다.

우리의 추가 범위:

- UI 상태는 Scripted Animation이 아니라 Hermes Kanban과 공식 Read Model에서 가져온다.
- Broker 응답이 아니라 내부 Ledger와 Reconciliation을 회계 기준으로 둔다.
- 주문 전 Risk Engine과 주문 후 QA·감사를 서로 독립시킨다.

### 5.4 AgenticTrading

AgenticTrading은 Planner, Orchestrator, Agent Registry와 Data·Alpha·Risk·Transaction Cost·Portfolio·
Execution·Backtest·Audit Agent Pool을 결합한다. 개념적 범위가 우리와 가장 가깝다.

배울 점:

- 고정된 Agent 대화 순서 대신 Case에 필요한 Agent만 DAG로 구성한다.
- Agent 등록, Capability, Health와 Version을 Registry에서 관리한다.
- Transaction Cost를 별도 평가 책임으로 분리한다.
- Agent 통신 계약을 Tool·Protocol 경계로 표준화한다.

우리의 차별화 방향:

- `자유로운 Agent 협업`보다 부서별 권한과 승인 가능한 상태 전이를 우선한다.
- LS증권, OpenDART와 KRX를 기반으로 한국 시장 Point-in-Time Data Plane을 만든다.
- Agent 결과와 금융 원장을 분리하고 Ledger·NAV·Reconciliation을 공식 기록으로 사용한다.
- CEO, 6개 본부와 Agent Workforce 인사팀을 실제 운영 단위와 UI로 연결한다.

이 프로젝트가 Orchestration과 Agent Pool에서 더 앞서 있을 수 있으므로, 우리 차별점은 문서상의
조직도만으로 증명되지 않는다. 전체 Investment Case를 실제로 재현하고 통제할 수 있어야 한다.

### 5.5 FINCON과 FinMem

두 연구는 Hermes Memory와 재귀적 자기개선 설계에 직접적인 참고가 된다. 성공·실패 Episode를
비교하고, 어떤 기억을 누구에게 전달할지 선택하며, 시간과 중요도에 따라 기억을 검색한다.

배울 점:

- Working, Episodic, Semantic·Procedural Memory를 구분한다.
- 거래 결과만 아니라 Risk 위반, 근거 품질과 시장 Regime별로 Episode를 평가한다.
- 모든 Agent에게 같은 Lesson을 전파하지 않고 관련 Agent와 Skill만 갱신한다.

우리의 추가 통제:

```text
실패·성과 Episode
  -> ImprovementCandidate
  -> 오프라인 평가
  -> 독립 QA·Risk 검토
  -> Shadow
  -> Paper
  -> 사람 승인
  -> Version 배포
  -> 관찰 또는 Rollback
```

Agent가 스스로 배웠다는 이유만으로 Prompt, Skill, Model 또는 전략을 Production에 바로 반영하지
않는다. 이 승격 절차가 Hermes 자기개선을 금융 서비스에 적용하는 핵심 차별점이다.

### 5.6 FinRL-X

FinRL-X는 Agent 조직보다 전략 실행 기반에서 참고 가치가 높다. 동일한 Portfolio Weight 계약을
Backtest, Paper와 Live 경로에서 사용하고, Walk-forward와 거래 비용을 공통으로 처리한다.

배울 점:

- Research와 Live가 같은 Strategy Contract를 사용한다.
- `목표 비중 → 주문 계획 → 체결 → 실제 비중`의 차이를 측정한다.
- Look-ahead 방지, 수수료, Slippage와 거래 가능성을 기본 조건으로 둔다.
- Strategy Artifact, Dataset Snapshot과 실행 환경을 함께 Versioning한다.

### 5.7 RD-Agent-Quant

[RD-Agent-Quant](https://arxiv.org/abs/2505.15155)는 Research Agent가 가설을 만들고 Development
Agent가 Factor·Model 코드를 구현한 뒤 실제 Backtest 결과를 Feedback으로 사용하는 자동 Quant
R&D 구조다. Multi-armed Bandit Scheduler로 다음 연구 방향까지 선택하므로, `Agent가 전략을
자동으로 만든다`는 문구만으로는 우리 Strategy Factory를 차별화할 수 없다.

배울 점:

- Research와 Development를 분리하고 실험 결과로 다음 가설을 선택한다.
- Factor만 또는 Model만 개선하지 않고 두 요소의 상호작용을 실험한다.
- 코드, Dataset, 실험 설정과 결과를 재현 가능한 Artifact로 남긴다.
- 연구 방향 선택에도 탐색과 활용의 균형을 적용한다.

우리의 추가 범위:

- 생성된 전략은 `StrategyPlugin` 계약 밖에서 실행되지 않는다.
- Point-in-Time, 거래 비용, Multiple Testing과 Capacity를 독립 `Strategy Court`가 심사한다.
- Shadow·Paper 성과와 실제 주문·회계 결과를 다음 연구 Feedback에 포함한다.
- 전략뿐 아니라 Source, Agent, Doctrine과 조직 Workflow의 기여도도 별도로 평가한다.
- RD-Agent와 Qlib는 즉시 Core 의존성으로 넣지 않고 별도 Docker Spike에서 KRX Dataset 호환성과
  재현성을 검증한 뒤 ADR로 채택 여부를 결정한다.

## 6. 우리 서비스의 차별점

### 6.1 개인용 헤지펀드 운영체제

기존 프로젝트의 주된 결과물은 `분석 보고서`, `매수·매도 판단` 또는 `자동 주문`이다. 우리
서비스의 결과물은 사용자 Mandate 안에서 반복 운영되는 개인형 헤지펀드다.

| 일반적인 Trading Agent | 우리 서비스 |
|---|---|
| 종목 질문에 답변 | 전 종목을 지속 감시하고 사건을 선별 |
| Agent들의 최종 매수·매도 투표 | 독립된 Research, Portfolio, Risk와 Execution 책임 |
| 주문 결과 표시 | Ledger, Position, NAV, PnL과 Reconciliation |
| 대화 기록 | 재현 가능한 Investment Case와 Audit Trail |
| Prompt 수동 수정 | 검증·승인·Rollback이 있는 Strategy·Skill 승격 |
| Agent 목록 | 채용, Version, 권한, 성과와 퇴출을 관리하는 Workforce |

### 6.2 전 종목 실시간 감시와 선택적 추론

전 종목 Tick마다 LLM을 호출하는 것은 비용, 지연과 Rate Limit 때문에 운영할 수 없다. 우리 구조는
LS증권 WebSocket 데이터를 먼저 결정론적 Feature·Event Engine에서 처리한다.

```text
LS WebSocket
  -> 정규화·중복 제거·시계열 저장
  -> Feature 계산
  -> Event 중요도·신뢰도·신선도 평가
  -> 상위 Event만 Investment Case 생성
  -> LangGraph 투자위원회
```

따라서 `전 종목 실시간 판단`은 모든 Tick을 LLM이 읽는다는 뜻이 아니다. 모든 종목을 기계적으로
감시하고, 의미 있는 변화에만 Agent 조직이 깊게 판단한다는 뜻이다.

### 6.3 LLM과 금융 통제 시스템의 분리

LLM이 담당하는 일:

- 가설 생성, 근거 검색과 요약
- Bull/Bear 반대 검토
- 포트폴리오 변경안과 실패 조건 제안
- 사건 설명과 사후 분석

결정론적 시스템이 독점하는 일:

- Risk Limit, Exposure, Margin과 Kill Switch
- 주문 상태 전이, 멱등성, 재시도와 체결 반영
- Position, Cash, Fee, Ledger, NAV와 Reconciliation
- Auth, Tool Permission과 변경 불가능한 Audit Event

이 경계는 단순한 품질 개선이 아니라 자금 손실과 책임 불명확성을 막는 제품 원칙이다.

### 6.4 Investment Case 단위의 완전한 추적

모든 부서는 같은 `case_id`, `case_version`, `evidence_id`, `strategy_version`, `trace_id`를 공유한다.

```text
Market Event
  -> Evidence Snapshot
  -> Research Thesis
  -> Bull/Bear Review
  -> Portfolio Proposal
  -> Risk Decision
  -> OrderIntent·Order·Fill
  -> Ledger·Position·NAV
  -> Outcome Evaluation
  -> ImprovementCandidate
```

사용자는 `왜 이 판단을 했는가`, `어떤 데이터 버전을 사용했는가`, `누가 승인했는가`,
`주문과 손익은 어떻게 연결되는가`, `무엇을 개선했는가`를 하나의 화면에서 확인할 수 있다.

### 6.5 회사 수준의 재귀적 자기개선

자기개선 대상은 Trading Agent 하나가 아니다.

| 개선 대상 | 예시 |
|---|---|
| Research | 검색 Query, Source 신뢰도, RAG Chunk와 Event 분류 |
| Trading | Signal 해석, 주문 계획과 Slippage 대응 |
| Risk | Limit 제안, Stress Scenario와 False Positive 분석 |
| Quant | Feature, 전략 Parameter, Dataset과 검증 절차 |
| Accounting | 대사 규칙, Fee 분류와 이상 거래 탐지 |
| QA·Audit | 환각 Pattern, Citation 검사와 Tool Permission |
| CEO | Agent Routing, 예산 배분과 우선순위 |
| Workforce | 필요한 역할 채용, Skill Version 승격·정지·퇴출 |

Hermes Memory는 공식 수치의 저장소가 아니다. Supabase, TimescaleDB, OMS, Ledger와 Risk Engine의
Record를 참조하는 조직 기억이며, 변경은 Improvement Lifecycle을 통과해야 한다.

### 6.6 AI Office 운영 Control Plane

AI Office는 Agent가 움직이는 모습을 보여주는 장식 화면이 아니다.

- Hermes Kanban의 업무 배정, 진행, 대기와 차단 상태를 보여준다.
- 시장 Event, Investment Case, 승인 Queue, Risk 위반과 Incident를 연결한다.
- `DEMO/PAPER/LIVE`, 데이터 신선도와 마지막 갱신 시각을 명확하게 표시한다.
- 허용된 승인, 중지와 Rollback 명령만 실행한다.
- 금융 상태의 Source of Truth는 공식 Backend이며 Kanban이나 Browser Memory가 아니다.

## 7. 차별점의 현재 상태

좋은 설계 문서만으로는 차별화가 되지 않는다. 현재 저장소 기준 상태를 과장 없이 구분한다.

| 차별 요소 | 현재 상태 | 차별점으로 인정받기 위한 증거 |
|---|---|---|
| LS 기반 전 종목 실시간 Data Plane | 부분 | 장중 상시 수집, 재연결, 중복 제거, Stale 감지와 Replay 검증 |
| Event 선별 후 Agent 판단 | 부분 | 실시간 Event에서 Case가 자동 생성되는 End-to-End Trace |
| 8개 Hermes 조직 Profile | 구현 | 실제 업무 배정, 권한과 성과가 Runtime에 연결 |
| 결정론적 Risk·OMS·Paper Broker | Prototype | 장애·중복·부분 체결 Test와 10거래일 Dry Run |
| Ledger·Portfolio·Reconciliation | Prototype | Broker 결과와 내부 원장의 일일 대사 및 불일치 Incident |
| Investment Case Audit Trail | 부분 | Evidence부터 PnL까지 한 `trace_id`로 Replay |
| Strategy Factory | 계획·일부 계약 | Dataset 고정, 독립 검증, Shadow·Paper 승격과 Rollback |
| Forecast·Calibration Ledger | 계획 | 사전 Forecast가 자동 Resolution되고 Agent·Source별 Calibration으로 집계 |
| Strategy Court | 계획 | Leakage·Multiple Testing·Cost·Capacity·Regime Gate를 독립 실행 |
| Temporal Market Memory Graph | 계획 | `known_at`이 있는 Event·Claim·Evidence·Outcome Graph Replay |
| Shadow Organization Lab | 계획 | 같은 Frozen Case에서 조직·Model·Prompt 후보를 주문 없이 비교 |
| Hermes 재귀적 자기개선 | Candidate Prototype | 평가 결과에 따른 Skill Version 승격·거절·복구 기록 |
| Kanban 기반 Live AI Office | 채택·연결 계획 | Scripted 상태 제거, Bridge·Read Model·WebSocket 실시간 화면 |
| 선물·옵션 Multi-Strategy | 계획 | Chain·Greeks·Margin·Multi-leg OMS와 만기 처리 |

현재 가장 큰 위험은 `기능이 많아서 차별화되는 것처럼 보이지만 핵심 Loop가 아직 연결되지 않은
상태`다. 경쟁 우위는 다음 증거가 쌓일 때 생긴다.

1. 장중 전 종목 Feed를 안정적으로 유지한다.
2. 하나의 Investment Case가 근거부터 Paper 체결과 손익까지 자동으로 완료된다.
3. 같은 Case를 재실행해 판단 입력과 상태 변화를 재현한다.
4. Risk 위반과 데이터 장애 때 자동 중단되고 운영자가 원인을 확인한다.
5. 개선 후보가 Shadow·Paper Gate를 통과하거나 거절·Rollback되는 기록을 남긴다.

## 8. 우선 도입할 설계

| 우선순위 | 도입 항목 | 참고 프로젝트 | 우리 적용 방식 |
|---|---|---|---|
| P0 | 구조화 Agent Output와 Checkpoint | TradingAgents | Pydantic Contract, Case Checkpoint, Resume |
| P0 | Backtest·Paper 공통 전략 계약 | FinRL-X | `StrategyPlugin`과 Target Portfolio Contract |
| P0 | Agent Registry와 Capability | AgenticTrading | 부서, 역할, Tool Allowlist, Version, Health |
| P0 | Paper·Live Mode와 승인 UX | TradingGoose | AI Office에 Mode, Approval, Kill Switch 고정 표시 |
| P0 | Forecast·Calibration Ledger | ForecastBench, AIA Forecaster | 사전 확률 예측, 자동 Resolution, Brier·Calibration Score |
| P0 | 적대적 Strategy Court | RD-Agent-Quant, Deflated Sharpe Ratio | 자동 연구와 독립된 Leakage·Cost·Multiple Testing 심사 |
| P0 | Adaptive Research Budget | Adaptive-RAG | Event 복잡도·가치에 따라 Ollama, 단일 Agent, 위원회로 승격 |
| P1 | Temporal Market Memory Graph | DyG-RAG, GraphRAG | 시점이 있는 Event·Claim·Evidence와 반박·무효화 관계 |
| P1 | Shadow Organization Lab | ForecastBench, Agent Market Arena | 같은 Case에서 Agent 구성·Model·Prompt를 주문 없이 비교 |
| P1 | Episode·다층 Memory | FINCON, FinMem | Case Outcome과 Regime별 Lesson, 선택적 전파 |
| P1 | Transaction Cost 독립 검토 | AgenticTrading, FinRL-X | Portfolio 제안과 주문 사이 Cost·Liquidity Gate |
| P2 | Digital Pod Shop Allocator | Risk-aware Bandit 연구 | 검증된 전략 Pod에 제한된 Shadow·Paper Risk Budget 배분 |
| P2 | 조직 Credit Assignment | Agent Lightning | Trace 단계별 기여도 평가, Offline Skill 개선 후보 생성 |
| P1 | Investment Doctrine Contract | ai-hedge-fund | 유명인 Persona가 아닌 측정 가능한 평가 정책 |
| P2 | 동적 Agent DAG | AgenticTrading | 고정 핵심 Gate를 유지하면서 Research 단계만 동적 구성 |

## 9. 구현 우선순위

### Phase A. 실제 시장에서 하나의 Case 완성

1. LS WebSocket → 정규화 → Redis Streams → TimescaleDB를 연결한다.
2. Event Engine이 중요 사건을 만들고 `Investment Case`를 시작한다.
3. LangGraph가 Research, Bull/Bear, Portfolio Proposal을 실행한다.
4. Risk Engine → Paper OMS → Paper Broker → Ledger·Portfolio를 연결한다.
5. Evidence, Decision, Order, Fill과 PnL을 같은 Trace로 조회한다.

완료 조건: 사람의 수동 DB 수정 없이 한 사건이 Paper 손익과 사후 평가까지 끝난다.

### Phase B. 운영 가능한 회사로 만들기

1. Hermes Kanban Status Bridge와 Agent Registry를 연결한다.
2. AI Office에서 Queue, 승인, Incident와 데이터 신선도를 실시간 표시한다.
3. Auth, RBAC, Tool Allowlist와 Kill Switch를 적용한다.
4. 10거래일 Paper Dry Run과 장애 복구 훈련을 수행한다.

완료 조건: 운영자가 어떤 Agent가 무엇을 하고 있고, 왜 막혔으며, 어느 Case와 연결됐는지 확인한다.

### Phase C. 전략을 만드는 회사로 확장

1. Strategy Candidate와 Dataset Snapshot을 등록한다.
2. Point-in-Time Backtest, 거래 비용, Deflated Sharpe Ratio와 독립 Strategy Court를 실행한다.
3. Shadow → Paper → 승인 → 배포 → 관찰 → Rollback을 자동화한다.
4. 성과뿐 아니라 Forecast Calibration, Regime 안정성, Capacity와 운영 가능성으로 승격한다.

완료 조건: 전략 Version 하나가 재현 가능한 근거와 승인 이력을 가지고 승격 또는 거절된다.

## 10. 피해야 할 방향

- 유명 투자자 Persona의 말투나 다수결을 Risk 승인으로 사용하지 않는다.
- LLM이 Broker API를 직접 호출하거나 주문 수량을 최종 확정하게 하지 않는다.
- 전 종목의 모든 Tick을 LLM에 전달하지 않는다.
- Backtest와 Live가 서로 다른 Feature·Strategy Code를 사용하지 않는다.
- News, X 게시물과 유료 데이터를 출처·이용권한 없이 영구 저장하지 않는다.
- Demo Animation을 실제 Agent 업무 상태처럼 표시하지 않는다.
- README의 기능 수나 수익률 주장만으로 Production 준비를 선언하지 않는다.

## 11. 오픈소스 활용과 License 주의

- 프로젝트를 참고할 때는 먼저 해당 Commit의 License와 제3자 Data License를 확인한다.
- Apache-2.0과 MIT도 Notice, 저작권 표시와 재배포 조건을 지켜야 한다.
- TradingGoose는 공개 저장소 기준 AGPL 계열 License이므로, 네트워크 서비스에 코드를 직접 결합할
  때 전체 배포 방식과 소스 공개 의무를 별도로 검토해야 한다.
- Architecture와 아이디어를 참고하는 것과 코드를 복사하는 것은 다르다.
- License가 명확하지 않은 저장소의 코드는 복사하지 않고 설계 참고로만 사용한다.

## 12. 논문·공식 기술 자료 기반 차별화 로드맵

### 12.1 조사에서 확인된 사실

1. 자동 Quant R&D는 이미 경쟁이 시작됐다. RD-Agent-Quant는 가설, Factor·Model 코드, Backtest와
   Feedback을 Multi-agent Loop로 연결한다.
2. LLM의 자신감은 그대로 믿을 수 없다. ForecastBench에서는 당시 최고 LLM도 전문 Forecaster보다
   낮은 결과를 보였고, AIA Forecaster는 검색·Supervisor·통계적 Calibration을 결합해 성능을 높였다.
3. Agent 수나 토론 횟수보다 독립적인 관점과 검증 가능한 추론이 중요하다. 통제된 Multi-agent
   Debate 연구에서는 다수 압력이 잘못된 합의를 강화할 수 있었다.
4. 금융 RAG는 Citation 표시만으로 안전하지 않다. FinanceBench와 Attribution 연구는 검색 실패,
   환각과 사후 합리화 문제를 보여준다.
5. 전략 후보를 많이 만들수록 우연히 좋은 Backtest가 나올 가능성이 커진다. Deflated Sharpe
   Ratio는 Multiple Testing과 비정규 수익률로 부풀려진 성과를 보정한다.

따라서 우리의 차별화는 `더 많은 Agent`, `더 유명한 Persona`, `더 많은 자동 생성 전략`이 아니라
다음 폐쇄 루프에 있어야 한다.

```text
시점이 고정된 Evidence
  -> 사전 확률 Forecast
  -> 독립 Strategy Court
  -> Shadow 조직 비교
  -> Paper 주문·회계 결과
  -> Agent·Source·전략 Calibration
  -> 다음 연구·Risk Budget·Skill Version 배분
```

### 12.2 평가 기준

후보별 100점은 다음 가중치로 계산한다. 각 항목은 1~5점이며 5점이 가장 좋다. 논문의 수익률
주장을 그대로 점수에 반영하지 않고, 우리 서비스에서 검증 가능한 제품 가치를 기준으로 평가한다.

| 기준 | 가중치 | 질문 |
|---|---:|---|
| 차별성과 데이터 해자 | 30 | 사용 기록이 쌓일수록 복제하기 어려워지는가 |
| 사용자·운영 가치 | 25 | 판단 품질, 설명력 또는 손실 통제를 실제로 개선하는가 |
| 현재 Architecture 적합성 | 20 | Investment Case, Hermes, LangGraph와 기존 DB를 재사용하는가 |
| Proof까지 걸리는 시간 | 15 | 12주 안에 Paper 환경에서 증명할 수 있는가 |
| 안전성과 통제 가능성 | 10 | 결정론적 Gate, 승인과 Rollback을 유지하는가 |

### 12.3 종합 순위

`종합점수`와 `구현 순서`는 다르다. Temporal Graph와 Shadow Organization은 점수가 높지만 P0
평가 계약과 Case 이력이 먼저 있어야 하므로 P1이다.

| 순위 | 아이디어 | 해자 30 | 가치 25 | 적합 20 | 속도 15 | 안전 10 | 총점 | 실행 |
|---:|---|---:|---:|---:|---:|---:|---:|---|
| 1 | Forecast·Calibration Ledger | 5 | 5 | 5 | 4 | 5 | 97 | P0 즉시 |
| 2 | Adversarial Strategy Court | 5 | 5 | 5 | 3 | 5 | 94 | P0 즉시 |
| 3 | Temporal Market Memory Graph | 5 | 5 | 4 | 3 | 5 | 90 | P0 Schema, P1 Retrieval |
| 4 | Shadow Organization Lab | 5 | 4 | 5 | 3 | 5 | 89 | P1 |
| 4 | Adaptive Research Budget | 4 | 4 | 5 | 5 | 5 | 89 | P0 즉시 |
| 6 | Digital Pod Shop Allocator | 5 | 5 | 4 | 2 | 4 | 85 | P2 |
| 7 | Organization Credit Assignment | 5 | 4 | 4 | 2 | 2 | 76 | P0 Trace, P2 학습 |
| 8 | KRX Market Digital Twin | 4 | 4 | 2 | 1 | 4 | 63 | P3 Execution 연구 |
| 9 | 유명 투자자 Persona Fine-tuning | 1 | 2 | 3 | 4 | 2 | 44 | P3 보류, Doctrine 대체 |

### 12.4 P0-1. Forecast·Calibration Ledger

가장 먼저 도입한다. 모든 Agent는 `매수`, `좋아 보인다` 같은 서술만 남기지 않고, 결과가 정해질
시점과 판정 규칙이 있는 사전 Forecast를 제출한다. 결과가 나온 뒤 문장을 그럴듯하게 고치는 것은
허용하지 않는다.

최소 계약:

```text
ForecastContract
  forecast_id
  case_id / case_version
  agent_profile_version / doctrine_version
  question / target_asset
  outcome_definition / resolution_source
  probability_distribution
  expected_return_quantiles
  horizon / known_at / resolves_at
  evidence_ids / invalidation_rules
  model_id / prompt_version / token_cost / latency_ms
```

자동 Resolution과 평가:

- 방향성·사건 발생 확률: Brier Score와 Log Score
- 수익률 구간: Quantile Loss와 구간 Coverage
- Agent·Doctrine·Source별 Reliability Diagram과 Calibration Error
- Regime, Sector, Horizon과 Event Type별 Slice
- PnL은 별도 결과로 기록하며 Forecast 정확도와 섞어 단일 Reward로 만들지 않는다.

서비스 화면:

- 각 투자안에 `예측 확률`, `무효화 조건`, `해결 예정일`을 표시한다.
- Agent Leaderboard는 수익금이 아니라 Calibration, Evidence Quality, 비용과 Risk 위반을 함께 본다.
- Source Score는 단순 조회 수가 아니라 해당 Source를 포함한 Forecast의 증분 정보로 평가한다.
- 데이터가 부족한 Score에는 최소 표본 수와 신뢰구간을 표시한다.

저장 후보:

```text
forecast_contracts
forecast_resolutions
forecast_scores
agent_calibration_daily
source_calibration_daily
doctrine_scorecards
```

Event 후보:

```text
forecast.created.v1
forecast.locked.v1
forecast.resolved.v1
forecast.score.updated.v1
```

완료 조건:

- 한 Investment Case에서 2개 이상의 독립 Forecast가 수정 불가능한 Version으로 저장된다.
- 가격·공시·Calendar Source로 Forecast가 자동 해결된다.
- Agent별 Brier Score와 Calibration을 AI Office에서 조회한다.

### 12.5 P0-2. Adversarial Strategy Court

Strategy Factory가 전략을 많이 만들수록 검증 조직은 더 공격적이어야 한다. Bull/Bear 자유 토론
대신 각 심사자가 먼저 독립 Verdict를 잠그고, 그 뒤에만 서로의 결과를 공개한다. 이는 다수 의견이
독립 수정을 억누르는 문제를 줄이기 위한 구조다.

고정 심사 역할:

| 심사자 | 검사 대상 | 결정론적 산출물 |
|---|---|---|
| Point-in-Time Prosecutor | Look-ahead, Survivorship, 수정 공시와 Universe 오염 | Leakage Report |
| Multiple-Testing Auditor | Trial 수, Selection Bias와 과적합 | DSR·PBO 계열 Report |
| Cost·Capacity Examiner | 수수료, Slippage, Turnover, 체결 가능량 | Net Performance·Capacity |
| Regime Adversary | 위기·횡보·급변 구간과 Parameter 안정성 | Regime Matrix |
| Risk Witness | Exposure, Drawdown, Liquidity와 Margin | Risk Verdict |
| Reproducibility Clerk | Dataset, Code, Seed와 Container 고정 | Reproduction Manifest |

LLM의 역할은 반례, 누락 조건과 추가 시험을 제안하는 것이다. 최종 Pass는 코드로 계산한 Gate와
사람 승인으로 결정하며 LLM Judge 단독 승인을 금지한다.

최소 Gate:

```text
PIT 검증
  -> 비용 후 Baseline 우위
  -> Multiple Testing 보정
  -> OOS·Regime 안정성
  -> Capacity·Risk 적합성
  -> 독립 재현
  -> Shadow 허용 | 기각 | 수정 요청
```

완료 조건:

- Strategy Candidate 한 건이 모든 심사 Report와 `trial_verdict`를 가진다.
- 한 심사라도 `BLOCK`이면 자동 Shadow 배포가 중단된다.
- 후보 생성 조직은 자기 후보의 최종 Release Gate를 승인할 수 없다.

### 12.6 P0-3. Adaptive Research Budget

전 종목 Event에 동일한 Agent 조직과 Bedrock 비용을 사용하지 않는다. Adaptive-RAG의 복잡도별
Routing을 시장 Event에 적용한다.

```text
Level 0  결정론적 Filter만 실행
Level 1  Ollama 분류·중복 제거·짧은 요약
Level 2  단일 Specialist + 제한 RAG
Level 3  Bull/Bear·Portfolio 위원회 + Multi-hop RAG
Level 4  사람 승인 또는 Incident 대응
```

Routing Feature:

- 예상 재무 영향과 가격 Materiality
- 현재 Position·Watchlist와의 관련성
- 새 정보인지 여부와 Source 신뢰도
- Forecast 불확실성과 기존 Agent 의견 차이
- 데이터 신선도, 거래 가능 시간과 Actionability
- 예상 Token 비용, Latency와 남은 일별 Budget

초기에는 학습형 Router보다 명시적 Rule과 Scorecard를 사용한다. 충분한 `route_decision`,
처리 비용과 사후 품질이 쌓인 뒤 작은 분류기나 Calibration 기반 Router로 교체한다.

완료 조건:

- 모든 Event에 Routing 이유와 예상 비용이 기록된다.
- 중요 Event Recall을 유지하면서 Bedrock 호출 수와 평균 Case Latency를 Baseline과 비교한다.
- High Materiality Event의 자동 하향 Routing을 금지하는 Guardrail이 있다.

### 12.7 P1-1. Temporal Market Memory Graph

Vector 검색만으로는 `그때 무엇을 알고 있었는가`와 사건의 순서를 안정적으로 설명하기 어렵다.
DyG-RAG와 Temporal GraphRAG의 아이디어를 한국 시장 Event에 적용한다.

Node:

```text
Entity, Security, Document, Event, Claim, Forecast,
Strategy, Decision, Order, Outcome, Incident
```

Edge:

```text
MENTIONS, SUPPORTS, CONTRADICTS, INVALIDATES,
PRECEDES, AFFECTS, DERIVED_FROM, RESOLVED_BY,
HYPOTHESIZED_CAUSE
```

모든 Node와 Edge에 `observed_at`, `known_at`, `valid_from`, `valid_to`, `source_id`를 둔다.
인과가 검증되지 않은 관계를 `CAUSED_BY`로 저장하지 않고 `HYPOTHESIZED_CAUSE`로 구분한다.

구현 원칙:

- P0에는 Supabase PostgreSQL의 관계 Table과 pgvector Hybrid Retrieval로 시작한다.
- Neo4j 같은 별도 Graph DB는 Query·Scale 증거가 생기기 전 도입하지 않는다.
- OpenDART 정정 공시, KRX Corporate Action과 뉴스 중복을 같은 Event Timeline에 연결한다.
- 최종 답변 Citation뿐 아니라 Claim이 실제 생성에 사용됐는지 Faithfulness를 검사한다.
- RAGChecker 계열 지표로 Retrieval과 Generation 실패를 분리한다.

이 Graph의 장기 해자는 한국 기업·사건·Claim·Forecast와 실제 Outcome이 연결된 자체 이력이다.

### 12.8 P1-2. Shadow Organization Lab

같은 Frozen Investment Case를 서로 다른 조직 구성으로 동시에 판단하되 주문 권한은 주지 않는다.
전략만 A/B Test하는 것이 아니라 `회사를 어떻게 구성해야 더 잘 판단하는가`를 실험한다.

비교 후보:

- 단일 Agent vs 역할 분리 위원회
- 동일 Model 다중 Persona vs 서로 다른 Model·Tool을 가진 독립 Specialist
- 자유 토론 vs 독립 Forecast 잠금 후 반론
- Vector RAG vs Temporal Event Graph RAG
- Ollama 1차 분석 후 Bedrock 승격 vs 전부 Bedrock
- X Social Insight 포함 vs 제외

평가 지표:

- Forecast Calibration과 Resolution 성능
- Evidence Correctness, Faithfulness와 누락률
- Risk False Pass·False Block
- Decision Stability와 오류 상관관계
- Latency, Token 비용과 실패 복구율
- 최종 Paper 결과는 보조 지표로 사용

주의:

- 관찰하지 않은 주문의 체결·시장 충격을 실제 PnL처럼 단정하지 않는다.
- Off-policy Evaluation은 Action Propensity와 Logging Policy가 기록된 이후에만 검토한다.
- 표본이 적은 조직 승자에게 Production 권한을 바로 주지 않는다.

완료 조건:

- 하나의 Case Snapshot으로 최소 2개 조직 Version을 자동 Replay한다.
- 조직별 결과가 동일 평가 계약으로 비교되고 Champion 변경 이력이 남는다.

### 12.9 P2-1. Digital Pod Shop Allocator

실제 Multi-manager Hedge Fund처럼 전략군을 독립 Pod로 운영한다. 각 Pod는 자기 Strategy,
Research Lens와 성과 기록을 가지지만 중앙 Risk가 전체 Exposure와 자본을 통제한다.

```text
Equity Directional Pod
Event Driven Pod
Market Neutral Pod
Macro·Futures Pod
Volatility Pod
  -> Central Meta Allocator
  -> Deterministic Portfolio·Risk Gate
```

Allocator 입력:

- OOS·Shadow·Paper 성과와 Forecast Calibration
- 전략 간 상관, 공통 Factor Exposure와 Crowding
- Drawdown, Tail Risk, Liquidity, Turnover와 Capacity
- Regime별 안정성과 최근 Drift
- 전략 Version의 운영 장애와 Reconciliation 품질

Risk-aware Bandit나 Thompson Sampling은 연구 후보로 사용할 수 있지만 바로 자금 배분 권한을 주지
않는다. 먼저 Shadow Research Budget, 그다음 Paper Risk Budget에 적용한다.

도입 선행 조건:

- 서로 다른 전략군 3개 이상
- 전략별 최소 60거래일 Paper·Shadow 이력
- 공통 비용·Capacity Model
- 중앙 Gross·Net·Factor·Liquidity Limit
- Allocator 중단과 Equal-Risk Fallback

### 12.10 P2-2. Organization Credit Assignment

Hermes의 자기개선을 `좋은 거래였으니 모든 Agent가 잘했다`로 처리하지 않는다. Agent Lightning의
Execution·Training 분리와 단계별 Credit Assignment 개념을 참고하되, P0에는 학습이 아니라 Trace
수집부터 시작한다.

각 Span에 기록할 Reward Vector:

```text
evidence_correctness
forecast_score
risk_compliance
decision_contribution
latency
token_cost
tool_failure
reconciliation_error
```

원칙:

- PnL 하나로 Prompt·Model을 학습하지 않는다.
- Research, Risk, Execution과 Accounting의 기여도를 같은 방식으로 채점하지 않는다.
- Offline Evaluation과 Shadow에서 유효한 Credit만 `ImprovementCandidate`로 변환한다.
- 실제 RL·Fine-tuning은 충분한 Case 수, GPU 예산과 Rollback 평가가 준비된 뒤 별도 ADR로 결정한다.
- Agent Lightning을 즉시 필수 의존성으로 추가하지 않고 Trace Schema와 분리 원칙부터 채택한다.

### 12.11 P3. Market Digital Twin과 Persona Fine-tuning

ABIDES와 JAX-LOB는 주문장, Latency, Market Impact와 Execution Agent를 시험하는 데 유용하다.
하지만 KRX 미시구조와 실제 주문 흐름에 맞춘 Calibration이 어렵기 때문에 초기 Alpha 검증 수단으로
사용하지 않는다.

도입 범위:

- 대량 주문의 Slippage·Capacity Stress
- 부분 체결, 취소 폭주, Feed Delay와 Broker 장애
- 선물·옵션 Multi-leg Execution과 Margin Scenario
- 동일 주문 알고리즘의 반복 가능한 Chaos Test

유명 투자자 Persona Fine-tuning은 보류한다. 대신 다음 `InvestmentDoctrine`을 사용한다.

```text
doctrine_id / version
objective / holding_horizon
allowed_evidence / prohibited_evidence
valuation_or_signal_rules
forecast_questions
invalidation_conditions
risk_budget
source_citations / usage_rights
calibration_history
```

Doctrine은 유명인의 정체성을 모방하지 않고 평가 가능한 투자 원칙만 표현한다. 동일 Case에서
실적이 나쁜 Doctrine은 이름의 유명세와 무관하게 비활성화한다.

### 12.12 12주 실행안

| 기간 | 구현 | 주관 조직 | 완료 산출물 |
|---|---|---|---|
| 1~2주 | Forecast Contract·Resolution Rule·DB Schema | 리서치 + QA | Forecast 저장·잠금·해결 Contract |
| 3~4주 | Calibration Job과 AI Office Scorecard | QA + 회계/포트폴리오 | Agent·Source·Doctrine Scorecard |
| 5~6주 | Strategy Court MVP | 퀀트 + Risk + QA | PIT·비용·DSR·재현성 Trial Verdict |
| 7~8주 | Adaptive Research Budget | 리서치 + CEO | Level 0~4 Routing과 비용·품질 비교 |
| 9~10주 | Temporal Event·Claim Graph MVP | 리서치 + QA | DART·News·Price Event Timeline |
| 11~12주 | Shadow Organization A/B | CEO + Agent Workforce + QA | 동일 Case의 조직 Version 비교 Report |

12주 뒤 Go/No-Go 기준:

- Forecast의 90% 이상이 자동 Resolution 가능한 명확한 규칙을 가진다.
- Agent·Source별 Calibration을 최소 표본 수와 함께 조회할 수 있다.
- Strategy Court 실패 Candidate가 Shadow로 넘어가지 않는다.
- 중요 Event Recall을 유지하면서 Case당 LLM 비용 또는 Latency가 Baseline보다 개선된다.
- 동일 Frozen Case의 조직 Version 비교가 재현된다.

### 12.13 도입하지 말아야 할 성급한 결론

- RD-Agent 논문의 수익률 수치를 우리 KRX 환경에서도 재현된다고 가정하지 않는다.
- Forecast Score가 좋다는 이유만으로 주문 권한을 높이지 않는다.
- Graph Edge를 만들었다는 이유로 사건 간 인과가 증명됐다고 표현하지 않는다.
- Shadow A/B의 가상 수익을 실제 체결 수익처럼 사용하지 않는다.
- PnL Reward만으로 Agent를 Fine-tuning하지 않는다.
- Persona 수가 많아진 것을 조직 다양성으로 측정하지 않는다.

## 13. 발표용 한 문장

> 기존 멀티 에이전트 트레이딩 프로젝트가 AI 분석가들의 투자 의견과 자동 주문에 집중한다면,
> 우리는 한국 시장 전 종목을 실시간 감시하고, 모든 Agent·근거·전략의 사전 예측을 실제 결과로
> 채점해 리스크 심사, 주문, 회계, 감사와 다음 전략 배분까지 개선하는
> **학습 가능한 개인용 헤지펀드 운영체제**를 만든다.

## 14. 공개 참고 자료

### 14.1 프로젝트

- [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
- [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund)
- [TradingGoose/TradingGoose.github.io](https://github.com/TradingGoose/TradingGoose.github.io)
- [huygiatrng/AlpacaTradingAgent](https://github.com/huygiatrng/AlpacaTradingAgent)
- [The-Swarm-Corporation/AutoHedge](https://github.com/The-Swarm-Corporation/AutoHedge)
- [Open-Finance-Lab/AgenticTrading](https://github.com/Open-Finance-Lab/AgenticTrading)
- [AI4Finance-Foundation/FinRobot](https://github.com/AI4Finance-Foundation/FinRobot)
- [MXGao-A/FAgent](https://github.com/MXGao-A/FAgent)
- [pipiku915/FinMem-LLM-StockTrading](https://github.com/pipiku915/FinMem-LLM-StockTrading)
- [AI4Finance-Foundation/FinRL-Trading](https://github.com/AI4Finance-Foundation/FinRL-Trading)
- [Polymarket/agents](https://github.com/Polymarket/agents)
- [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent)
- [microsoft/qlib](https://github.com/microsoft/qlib)
- [forecastingresearch/forecastbench](https://github.com/forecastingresearch/forecastbench)

### 14.2 논문과 공식 기술 자료

- [R&D-Agent-Quant: A Multi-Agent Framework for Data-Centric Factors and Model Joint Optimization](https://arxiv.org/abs/2505.15155)
- [ForecastBench: A Dynamic Benchmark of AI Forecasting Capabilities](https://arxiv.org/abs/2409.19839)
- [AIA Forecaster: Technical Report](https://arxiv.org/abs/2511.07678)
- [When Agents Trade: Live Multi-Market Trading Benchmark for LLM Agents](https://arxiv.org/abs/2510.11695)
- [Can LLM Agents Really Debate?](https://arxiv.org/abs/2511.07784)
- [Adaptive-RAG](https://arxiv.org/abs/2403.14403)
- [DyG-RAG](https://arxiv.org/abs/2507.13396)
- [Microsoft Research GraphRAG](https://www.microsoft.com/en-us/research/project/graphrag/overview/)
- [Microsoft Research VeriTrail](https://www.microsoft.com/en-us/research/blog/veritrail-detecting-hallucination-and-tracing-provenance-in-multi-step-ai-workflows/)
- [Correctness is not Faithfulness in RAG Attributions](https://arxiv.org/abs/2412.18004)
- [FinanceBench](https://arxiv.org/abs/2311.11944)
- [RAGChecker](https://arxiv.org/abs/2408.08067)
- [The Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
- [Optimal and Adaptive Off-policy Evaluation in Contextual Bandits](https://proceedings.mlr.press/v70/wang17a.html)
- [Adaptive Portfolio by Solving Multi-armed Bandit via Thompson Sampling](https://arxiv.org/abs/1911.05309)
- [Agent Lightning](https://arxiv.org/abs/2508.03680)
- [ABIDES](https://arxiv.org/abs/1904.12066)
- [JAX-LOB](https://arxiv.org/abs/2308.13289)

2025~2026년 자료 중 일부는 Preprint 또는 Technical Report다. 구현 아이디어의 참고 근거로
사용하되, 실제 KRX Dataset과 Paper 환경에서 재현하기 전 성과 주장으로 사용하지 않는다.
