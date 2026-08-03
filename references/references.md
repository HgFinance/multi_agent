# References

## GitHub Repositories

### [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
LLM 기반 멀티 에이전트 주식 트레이딩 프레임워크. 실제 트레이딩 조직의 협업 구조를 시뮬레이션한다.
- **Analyst Team**: Fundamentals(재무·내재가치), Sentiment(뉴스·소셜), News(거시·이벤트), Technical(MACD·RSI 등 기술적 지표) 4개 분석가
- **Researcher Team**: Bull/Bear 리서처가 분석가 인사이트를 두고 토론하며 기회와 리스크의 균형을 맞춤
- **Trader Agent**: 종합 분석을 바탕으로 진입 타이밍과 포지션 크기 결정
- **Risk Management & Portfolio Manager**: 포트폴리오 변동성·유동성을 평가해 거래 제안을 승인/거부
- LangGraph 기반 구현, OpenAI/Google/Anthropic/DeepSeek/Ollama/Azure 등 다중 LLM 프로바이더 지원, 의사결정 로깅과 체크포인트 기반 복구 지원
- 관련 논문: 아래 **TradingAgents (2412.20138)** — `2412.20138v7.pdf`
- **프로젝트 관련성**: HEDGE_FUND_MASTER_PLAN.md의 Analyst/Bull-Bear/Trader/Risk/Portfolio Manager 구조가 이 프레임워크를 직접 참고한 것(마스터플랜 5.2절)

### [LLMQuant/Magents](https://github.com/LLMQuant/Magents)
멀티 전략 헤지펀드 백테스팅을 위한 오픈소스 Python 프레임워크. 독립적인 트레이딩 전략들을 공유 환경 안에서 동시 실행되는 에이전트로 시뮬레이션한다.
- **Pod 구조**: Long Biased, Event Driven, Quant, Macro, Equity Long/Short 등 전문화된 Pod 단위로 전략을 조직
- Pod 내부에는 Signal Agent(신호 생성), Execution Agent(주문 집행), Risk Agent(포지션 한도 집행) 배치
- **중앙 Team 모듈**: 포트폴리오 단위 데이터 관리와 레버리지·Drawdown·Exposure 한도 등 중앙 리스크 제약을 전체 Pod에 적용
- 이벤트 기반 백테스팅 엔진이 시장 데이터와 주문 라이프사이클 처리, CLI로 파라미터 구성 가능
- README에 연관 학술 논문 링크 없음(구현 중심 저장소)
- **프로젝트 관련성**: 마스터플랜의 PM Pod/Strategy Book 구조와 유사한 "Pod 단위 전략 격리 + 중앙 리스크 통제" 패턴 참고 가능

## Papers

### TradingAgents: Multi-Agents LLM Financial Trading Framework
- **저자**: Yijia Xiao, Edward Sun, Di Luo, Wei Wang
- **arXiv**: [2412.20138](https://arxiv.org/abs/2412.20138) · 로컬 파일: `2412.20138v7.pdf`
- 위 TauricResearch/TradingAgents 저장소의 근거 논문. Fundamentals/Sentiment/Technical 분석가, Bull/Bear 리서처, Trader, Risk Management Team으로 구성된 멀티 에이전트 트레이딩 프레임워크를 제안하고, 누적 수익률·Sharpe Ratio·최대 낙폭에서 베이스라인 모델 대비 우수한 성능을 보고

### FinMem: A Performance-Enhanced LLM Trading Agent with Layered Memory and Character Design
- **저자**: Yangyang Yu, Haohang Li, Zhi Chen, Yuechen Jiang, Yang Li, Denghui Zhang, Rong Liu, Jordan W. Suchow, Khaldoun Khashanah
- **arXiv**: [2311.13743](https://arxiv.org/abs/2311.13743) · 로컬 파일: `2311.13743v2.pdf`
- Profiling(에이전트 성격 커스터마이징), Layered Memory(인간 트레이더의 인지 과정을 모사하는 계층적 메모리), Decision-making 3개 모듈로 구성된 LLM 트레이딩 에이전트. 사람의 지각 범위를 넘어서는 조절 가능한 "인지 범위(cognitive span)"로 정보를 보존해 실제 금융 데이터셋에서 알고리즘 대비 우수한 트레이딩 성과를 시연
- **프로젝트 관련성**: 마스터플랜 9절(RAG/메모리 설계)의 Decision Memory·계층적 저장소 설계에 참고할 만한 메모리 아키텍처

### Design and Empirical Study of a Large Language Model-Based Multi-Agent Investment System for Chinese Public REITs
- **저자**: Zheng Li
- **arXiv**: [2602.00082](https://arxiv.org/abs/2602.00082) · 로컬 파일: `2602.00082v1.pdf`
- 중국 REIT 시장을 대상으로 한 LLM 멀티 에이전트 투자 프레임워크. Announcement/Event/Price Momentum/Market 4개 분석 에이전트가 신호를 생성하고, Prediction Agent가 이를 종합해 방향성 확률 분포를 만들며, Decision Agent가 예측과 리스크 통제를 바탕으로 포지션 조정 신호를 생성. DeepSeek-R1 직접 사용 vs 파인튜닝된 Qwen3-8B(SFT+RL) 비교 실험 포함, 2024년 10월~2025년 10월 백테스트에서 Buy-and-Hold 대비 우수한 성과
- **프로젝트 관련성**: 마스터플랜 8절의 분석가→예측→결정 파이프라인과 유사한 구조이며, 소형 파인튜닝 모델이 범용 대형 모델과 유사하거나 더 나은 성능을 낼 수 있다는 결과는 Level 2 경량 모델(7.2절) 설계에 참고할 만함

### Agentic Retrieval-Augmented Generation: A Survey on Agentic RAG
- **저자**: Aditi Singh, Abul Ehtesham, Saket Kumar, Tala Talaei Khoei, Athanasios V. Vasilakos
- **arXiv**: [2501.09136](https://arxiv.org/abs/2501.09136) · 로컬 파일: `2501.09136.pdf`
- 전통 RAG의 정적 워크플로우 한계를 자율 에이전트(Reflection, Planning, Tool Use, Multi-agent Collaboration)로 극복하는 Agentic RAG를 서베이. Agent Cardinality와 Control Structure 기준 taxonomy 제시, 헬스케어·금융 도메인 적용 사례와 평가·조정·거버넌스 관련 열린 연구 질문 정리
- **프로젝트 관련성**: `evidence-qa-agent`, `hallucination-critic`(QA), `compliance-policy-agent`(Risk) 3명에게 적용할 LangGraph Agentic RAG 설계의 핵심 참고 자료 — retrieve→grade→generate→retry 루프의 이론적 근거

### Improving Factuality and Reasoning in Language Models through Multiagent Debate
- **저자**: Yilun Du, Shuang Li, Antonio Torralba, Joshua B. Tenenbaum, Igor Mordatch
- **arXiv**: [2305.14325](https://arxiv.org/abs/2305.14325) · 로컬 파일: `2305.14325.pdf`
- 여러 LLM 인스턴스가 여러 라운드에 걸쳐 서로의 답변과 추론을 주고받아 하나의 결론에 도달하는 "Society of Minds" 방법론. 수학·전략 문제 해결 성능을 높이고 환각·허위 진술을 줄임을 시연한 멀티에이전트 토론의 원조격 논문
- **프로젝트 관련성**: 트레이딩본부의 Bull/Bear Researcher 토론 구조(마스터플랜 5.2, 8절)의 이론적 기반 — 토론 라운드 수, 합의 도출 방식 설계에 직접 참고 가능

### Large Language Models Hallucination: A Comprehensive Survey
- **저자**: Aisha Alansari, Hamzah Luqman
- **arXiv**: [2510.06265](https://arxiv.org/abs/2510.06265) · 로컬 파일: `2510.06265.pdf`
- LLM 환각을 데이터 수집부터 추론까지 라이프사이클 전반에 걸쳐 분석하고, 탐지 방법론과 완화 기법을 체계적 프레임워크로 정리, 기존 벤치마크·평가지표를 검토
- **프로젝트 관련성**: QA부서 `hallucination-critic` 페르소나의 탐지 기준(불확실성 은폐, 모순, Tool 오사용) 설계와 Golden/Adversarial Eval(인사팀 Selection/Performance Agent) 기준 수립에 직접 참고

### FinRobot: An Open-Source AI Agent Platform for Financial Applications using Large Language Models
- **저자**: Hongyang Yang, Boyu Zhang, Neng Wang, Cheng Guo, Xiaoli Zhang, Likun Lin, Junlin Wang, Tianyu Zhou, Mao Guan, Runjia Zhang, Christina Dan Wang
- **arXiv**: [2405.14767](https://arxiv.org/abs/2405.14767) · 로컬 파일: `2405.14767.pdf`
- 금융 특화 멀티 에이전트 오픈소스 플랫폼. Financial AI Agents(Financial CoT 분해) → Financial LLM Algorithms(태스크별 모델 전략 구성) → LLMOps/DataOps(Fine-tuning) → Multi-source LLM Foundation Models 4계층 구조
- **프로젝트 관련성**: 마스터플랜 13절의 계층형 플랫폼(Agent층/결정론적 Service층/Foundation Model층 분리) 설계와 유사한 4-Layer 아키텍처 — 본부별 Agent가 여러 LLM Provider를 어떻게 추상화해 쓸지에 대한 참고 사례

### Standard Benchmarks Fail — Auditing LLM Agents in Finance Must Prioritize Risk
- **저자**: Zichen Chen, Jiaao Chen, Jianda Chen, Misha Sra
- **arXiv**: [2502.15865](https://arxiv.org/abs/2502.15865) · 로컬 파일: `2502.15865.pdf`
- 금융 LLM 에이전트를 정확도 중심 벤치마크가 아니라 리스크 프로파일(환각, Stale Data, Adversarial Prompt 취약성) 중심으로 감사해야 한다고 주장. Model/Workflow/System 3계층 프레임워크로 6개 LLM 에이전트를 3개 금융 태스크에서 감사, "Safety Budget"을 성공 기준으로 제안
- **프로젝트 관련성**: AI QA/감사본부의 감사 기준 설계와 리스크본부의 Stress Test 접근에 직접 참고 — 특히 "정확도 높다고 안전한 게 아니다"라는 논지가 Risk Supervisor의 approve/resize/reject 근거 설계에 유용

### Nexus: An LLM Agent Framework for Multi-Source Time Series Forecasting
- **arXiv**: [2605.14389](https://arxiv.org/abs/2605.14389) · 로컬 파일: `2605.14389v1.pdf`
- 여러 수치·텍스트 Source를 시간순 맥락으로 구조화하고, Macro/Micro 해상도의 전망을 독립 생성한 뒤 합성과 Calibration Loop로 보정하는 Agent Framework
- **프로젝트 관련성**: Research의 Context Timeline, Macro/Micro Outlook, Synthesis와 Outcome Calibration의 직접 근거. 한국어 설명은 `NEXUS_FRAMEWORK_EXPLAINED.md`, 번역은 `NEXUS_PAPER_KO_TRANSLATION.md`

### MimirRAG: A Multi-Agent Retrieval-Augmented Generation Framework for Financial Question Answering
- **arXiv**: [2605.25030](https://arxiv.org/abs/2605.25030)
- 금융 문서의 구조와 표를 보존하는 Parsing, Metadata 추출, Agent Query Planning, Hybrid Retrieval과 숫자 검증을 결합
- **프로젝트 관련성**: DART 공시·재무 RAG에서 절·표·정정·보고기간 Metadata를 보존하고 Citation/Numeric Validator를 두는 설계 근거

### FinSAgent: Building Financial Agents by Equipping Their Proprietary Knowledge Bases
- **arXiv**: [2607.18102](https://arxiv.org/abs/2607.18102)
- 로컬 Corpus 구조와 일반 모델의 사전지식이 어긋나는 문제를 다루며, 역할별 Agent, Corpus-aware Query Decomposition, 다중 검색 경로와 Evidence-validity Rerank를 제안
- **프로젝트 관련성**: 최신 Preprint이므로 P1 Spike로만 채택. 모든 직원에게 같은 Evidence Bundle을 주지 않고 역할별 Retrieval Plan을 만드는 근거

### STORM: Synthesis of Topic Outlines through Retrieval and Multi-perspective Question Asking
- **arXiv**: [2402.14207](https://arxiv.org/abs/2402.14207)
- 다양한 관점을 먼저 발견하고 Source-grounded 질문을 구성한 뒤 개요를 만드는 연구 시스템
- **프로젝트 관련성**: Research 시작 전 Perspective/Question Planner에 제한 채택. 투자 판단이나 최종 Packet 합성은 맡기지 않음

### TimeSeriesScientist: A General-Purpose AI Agent for Time Series Analysis
- **arXiv**: [2510.01538](https://arxiv.org/abs/2510.01538) · [공식 구현](https://github.com/Y-Research-SBU/TimeSeriesScientist)
- Curator, Planner, Forecaster와 Reporter를 분리해 데이터 진단, 모델 후보 축소, 검증·선택·Ensemble과 보고를 수행
- **프로젝트 관련성**: Quant를 Data Curator, Hypothesis Planner, Deterministic Runner, Independent Validator와 Reporter로 분리하는 구조의 핵심 근거

### AlphaCast: A Multi-Agent Framework for Financial Time Series Forecasting
- **arXiv**: [2511.08947](https://arxiv.org/abs/2511.08947)
- Feature, 도메인 지식, 시간 창별 Context와 유사 Case를 준비한 뒤 생성 추론과 Reflection을 수행
- **프로젝트 관련성**: 과거 유사 Regime·실패 Case를 다음 가설에 연결하되, 검증 전 자동 Prompt 수정은 금지하는 Case Memory 설계 참고

### Synapse: A Multi-Agent Framework for Adaptive Time Series Forecasting
- **arXiv**: [2511.05460](https://arxiv.org/abs/2511.05460)
- 여러 시계열 Foundation Model Specialist의 가중치를 Context와 Rolling 성능에 따라 조정하는 Forecasting Framework
- **프로젝트 관련성**: Regime/Horizon별 Model Arbitration과 Ensemble Challenger의 P2 근거. 단순 Baseline을 반복해서 이긴 뒤에만 채택

### Position: Beyond Model-Centric Prediction — Agentic Time Series Forecasting
- **arXiv**: [2602.01776](https://arxiv.org/abs/2602.01776)
- 예측을 단일 모델 호출이 아니라 Perception, Planning, Action, Reflection과 Memory를 포함한 Workflow로 재정의
- **프로젝트 관련성**: Research-Quant를 모델 선택 문제가 아닌 데이터·가설·검증·학습의 상태 Workflow로 설계하는 상위 원칙

### FinCon: A Synthesized LLM Multi-Agent System with Conceptual Verbal Reinforcement for Enhanced Financial Decision Making
- **arXiv**: [2407.06567](https://arxiv.org/abs/2407.06567)
- Manager-Analyst 계층과 위험 기반 회고를 결합하고, 교훈을 관련 역할에 선택적으로 전파
- **프로젝트 관련성**: Hermes 본부장과 전문 직원의 계층, 부서별 Memory Namespace, 실패 교훈을 관련 역할에만 전달하는 개선 후보 설계 참고

### MLAgentBench: Evaluating Language Agents on Machine Learning Experimentation
- **arXiv**: [2310.03302](https://arxiv.org/abs/2310.03302)
- ML Experiment를 수행하는 자율 Agent의 장기 계획, 실험 반복과 신뢰성 한계를 평가
- **프로젝트 관련성**: Quant Agent가 무제한으로 코드를 고치고 좋은 결과를 선택하지 못하게 하고, Agent는 Spec·설명, Runner와 Gate는 결정론적 Service로 분리하는 근거

### The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality
- **SSRN**: [2460551](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
- 여러 전략을 시험한 뒤 가장 높은 Sharpe를 고르는 Selection Bias와 수익률의 비정규성을 보정
- **프로젝트 관련성**: Quant Trial Family Ledger와 Promotion Gate의 P1 통계 검증 항목

### A Comparison of Backtest Overfitting Prevention Methods in a Synthetic Environment
- **Journal**: [Knowledge-Based Systems, 2024](https://www.sciencedirect.com/science/article/pii/S0950705124011110)
- Holdout, Walk-Forward, CSCV/CPCV 계열 방법의 Backtest Overfitting 방지 성능을 비교
- **프로젝트 관련성**: Walk-Forward 하나를 만능 Gate로 보지 않고 CPCV와 PBO를 연구 단계의 보조 검증으로 추가하는 근거

## PDF Files

| 파일 | 논문 |
|---|---|
| `2311.13743v2.pdf` | FinMem |
| `2412.20138v7.pdf` | TradingAgents |
| `2602.00082v1.pdf` | Chinese Public REITs Multi-Agent Investment System |
| `2501.09136.pdf` | Agentic RAG Survey |
| `2305.14325.pdf` | Multiagent Debate (Factuality/Reasoning) |
| `2510.06265.pdf` | LLM Hallucination Comprehensive Survey |
| `2405.14767.pdf` | FinRobot |
| `2502.15865.pdf` | Auditing LLM Agents in Finance (Risk-First) |
| `2605.14389v1.pdf` | Nexus Multi-Source Time Series Forecasting Framework |
