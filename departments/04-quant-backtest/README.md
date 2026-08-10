# 퀀트/백테스트본부 (Quant / Backtest)

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
직원 런타임은 독립 LangGraph Worker와 Ollama `qwen3:1.7b`이며 Hermes Profile은
`quant-backtest-department`(본부장 모델 `claude-opus-5`)다. Build·Eval·권한 기준은 [Ollama Department Modelfile Guide](../../docs/02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)를 따른다.

실제 실행 상태와 재일님 2주 계획·Daily Scrum은 [실행 현황과 통합 계획 v2.2](../../docs/PROJECT_IMPLEMENTATION_STATUS.md#41-재일님-리서치본부와-퀀트백테스트본부)을 기준으로 한다.
Research Evidence를 전략 가설과 독립 검증으로 연결하는 목표 Graph와 계약은
[Research-Quant Evidence-to-Strategy Framework](../../docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md)를 따른다.
투자자 Persona를 측정 가능한 투자 원칙과 조건부 Fine-tuned Reviewer로 만드는 설계는
[Investment Doctrine Model Factory](../../docs/02-engineering/INVESTMENT_DOCTRINE_MODEL_FACTORY.md)를 따른다.

## Mission

**실험 공장이다.** 리서치본부의 실험 기획안(`ExperimentProposalV1`)을 접수해 결과를 보기 전에
사전 등록하고, Point-in-Time 데이터로 결정론 실험을 돌리고, 시도 압력·DSR·PBO·국면 분해로
과적합을 검사해 `ExperimentCardV1`을 낸다. 그리고 **성공·기각·킬을 가리지 않고**
`ExperimentOutcomeV1`으로 리서치에 환류한다 — 환류 적재가 실험 종결의 전제 조건이다.

**가설 발굴은 이 부서 일이 아니다**(2026-08-10 이관). 스스로 낸 가설을 스스로 검증하면
제안자와 승인자가 같아져 생성자·검증자 분리가 조직 안에서 무너진다. 발굴은 리서치, 검증은 퀀트다.

`quant-backtest-department`는 Production 승격을 직접 하지 않는다. QA 재현 검증, Risk Capability와
**사람의 최종 서명**이 필요하다. Backtest 수익률이 좋아도 미래 데이터, 거래비용, 과적합 또는 필요한
거래 기능이 검증되지 않으면 후보를 거절한다.

## Owner

재일님 — [TEAM_JAEIL_RESEARCH_QUANT_GUIDE](../../docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md)

## 입력·출력 계약

- 입력: **`ExperimentProposalV1`**(리서치본부), TimescaleDB 시장 시계열, Versioned Universe
- 내부 기록: `quant.dataset_*`, `hypotheses`, `experiments`, `backtest_*`, `experiment_metrics`
- 출력: `ExperimentCardV1`(Dataset·Code·Cost·Metric·Fragility·과적합 통계 연결),
  Strategy Candidate, **`ExperimentOutcomeV1`**(통제 어휘 `lesson_codes` → 리서치 환류)
- Handoff: Release Gate(결정론) → QA 재현 검증 → Risk Capability Review → 인간 승인

## 직원 편제 (LLM Worker 5인)

| Worker | 역할 | 실행 |
|---|---|---|
| `proposal-intake-worker` (QNT-01) | 기획안 접수 → 사전등록 사양 초안, 어휘·데이터·예산 확인 | 상시 |
| `experiment-design-worker` (QNT-02) | PIT Dataset, 창·Embargo, 파라미터 범위와 **그 범위의 시도 수** | 소집 (`experiment_design`) |
| `strategy-author-worker` (QNT-05) | **기성 템플릿에 없는 방법론의 시그널 코드 작성** — 코드 해시가 사전등록 지문에 들어간다 | 소집 (`strategy_authoring`) |
| `result-interpretation-worker` (QNT-03) | DSR·PBO·국면 해석 + **소스 보고 지표와 우리 결과 대조** — 수치 재계산·판정 금지 | 소집 (`experiment_card`) |
| `outcome-lesson-worker` (QNT-04) | 종결 사유를 통제 어휘 `lesson_codes`로 사상 | 소집 (`experiment_outcome`) |

**전략 코드는 실험마다 달라도 된다 — 그게 정상이다.** 논문·서한에서 오는 방법론은 서로 다른
계산이라 템플릿 파라미터로만 표현하면 공장이 손잡이 돌리기가 된다. 그래서 QNT-05 가 시그널
코드를 쓴다. 막는 것은 코드가 다른 것이 아니라 **결과를 본 뒤 코드가 바뀌는 것**이고,
그건 코드 해시를 사전등록 지문에 넣어 막는다(고치면 새 시도 → DSR 감가).
시그널은 기준일 이하만 노출하는 뷰만 받으므로 미래를 꺼낼 경로가 아예 없다.
**작성은 에이전트가, 승인은 결정론 검증이 한다.**

직원이 7명에서 5명으로 줄어든 것은 감원이 아니다. 계산과 판정(사전등록·PIT·백테스트·
walk-forward·DSR·PBO·국면·릴리스 관문)은 **이미 `pipeline/`의 결정론 코드가 하고 있고**,
그 위에 LLM을 겹쳐 두면 계산을 두 번 하거나 판정을 흉내 낸다. 남은 5자리는 결정론 코드가
못 하는 일 — 자연어 접수, 설계 제안, **시그널 코드 작성**, 결과 해석, 교훈 사상 — 만 맡는다.

## 현재 구현

| 경로 | 역할 | 상태 |
|---|---|---|
| `agents/strategy_hypothesis_agent.py` | 관측 근거에서 반증 가능한 가설 생성·등록 | 자체 점검 9개 통과 |
| `pipeline/pit_dataset.py` | Universe·Available Time을 보존한 Dataset Manifest·Partition | 자체 점검 3개 통과, 실제 Dataset 1개 |
| `pipeline/backtest_runner.py` | t-1 Signal, FIFO 손익, 비용과 재현 Hash | 자체 점검 5개 통과, 실제 Run 3개 |
| `pipeline/walk_forward.py` | 겹치지 않는 Window와 Fragility 판정 | 자체 점검 6개 통과 |
| `pipeline/experiment_orchestrator.py` | 데이터·전략 가능성 Gate와 상태 전이 | 자체 점검 3개 통과, 실제 Experiment 4개 |
| `hermes/` | 본부장 Profile, 직원 Persona와 Strategy Research Workflow | Runtime 통합 전 |

2026-08-03 실제 Supabase에서 Dataset Manifest 1개, Hypothesis 5개(`TESTING 4`, `REJECTED 1`),
Experiment 6개(`COMPLETED 5`, `RUNNING 1`)와 Backtest Run 3개를 확인했다. 이는 Script가
DB와 한 번 이상 연결됐다는 증거지만, 상시 Worker·API·CI와 Strategy 승격이 완료됐다는 뜻은 아니다.

## 목표 Graph

현재 Quant는 통합 LangGraph가 아니라 가설, Dataset, Backtest, Walk-Forward와 Orchestrator
스크립트의 조합이다. 가설 Agent의 주 근거도 `market-api /regime/daily` 단면이므로 Research
Packet의 Claim/Evidence와 직접 연결되는 계약은 남아 있다. 기존 결정론적 Script는 폐기하지
않고 다음 Graph의 Tool/Worker로 사용한다.

```text
Quant Hermes Intake
  -> Data Curator와 PIT Dataset Certification
  -> Evidence-linked Hypothesis Planner
  -> Hypothesis Preregistration
  -> Deterministic Experiment Runner
  -> Independent Robustness Validator
  -> Model/Strategy Arbitrator
  -> ExperimentCardV1
  -> QA · Risk · CEO Gate

Optional Doctrine Branch
  -> Verified Source와 InvestmentDoctrineV1
  -> Prompt/RAG Baseline
  -> 필요할 때만 격리 SFT/LoRA Training
  -> Independent Frozen Eval
  -> DoctrineReviewV1
  -> QNT-01 Hypothesis Seed
```

핵심 규칙:

- Agent는 가설과 Experiment Spec을 만들지만 Metric과 Backtest 결과는 코드가 계산한다.
- 결과를 본 뒤 가설을 수정하지 않는다. 변경은 새 Version과 Trial로 기록한다.
- `trial_family_id`와 Trial Ledger로 전체 탐색 횟수를 숨길 수 없게 한다.
- Purged Walk-Forward와 Embargo를 P0로, CPCV·Deflated Sharpe Ratio·PBO를 P1으로 추가한다.
- 생성자와 독립 Validator는 Service Identity, Queue와 Write 권한을 분리한다.
- Quant Hermes는 Job·Retry·Escalation과 Candidate 제출만 맡고 Production을 직접 승격하지 않는다.

## 실행법

Repository Root에서 실행한다.

```bash
quant-backtest-department chat -q 'Backtest [전략 가설]'

python departments/04-quant-backtest/agents/strategy_hypothesis_agent.py
python departments/04-quant-backtest/pipeline/pit_dataset.py
python departments/04-quant-backtest/pipeline/backtest_runner.py
python departments/04-quant-backtest/pipeline/walk_forward.py
python departments/04-quant-backtest/pipeline/experiment_orchestrator.py
```

실 DB 작업은 각 파일의 `--build`, `--run`, `--register` 옵션과 `DATABASE_URL`,
`TIMESCALE_DATABASE_URL`이 필요하다. 출력 Dataset 파일은 `quant-data/`에 생성되며 Git에 올리지 않는다.

## 남은 작업

- Quant API와 Job Worker를 Docker Compose에 추가한다.
- Job Request·완료 Event, 재시도, 중복 실행과 재시작 복구 계약을 만든다.
- `ResearchPacketV2 -> HypothesisSpecV2 -> ExperimentCardV1` Lineage를 연결한다.
- Preregistration, Trial Ledger와 독립 Robustness Validator를 추가한다.
- Purge/Embargo를 먼저 구현하고 CPCV·DSR·PBO를 단계적으로 추가한다.
- Strategy Registry에 Capability, QA, Risk, CEO 승인과 Shadow/Paper 상태를 연결한다.
- Dataset·Code·Dependency·Seed·Cost Model을 CI에서 같은 결과로 재현한다.
- 현재 `TESTING` Hypothesis 2개의 종료 조건을 명시하고 고아 Experiment를 방지한다.
- AI Office에는 Tick 원문이 아니라 Experiment·Candidate Read Model만 제공한다.
- `QNT-08` Doctrine Profile을 실제 `doctrine-trainer` Worker, Dataset Registry와 Frozen Eval에 연결한다.
- 인물 Persona가 아닌 `InvestmentDoctrineV1` Fixture부터 구현하고 Fine-tuning은 Need Gate 통과 후 실행한다.

## Handoff

- Research는 Observation과 Evidence Reference를 제공한다.
- QA는 Leakage, Metric, Claim과 Model Risk를 독립 검증한다.
- Risk는 Strategy Family와 상품별 Capability·한도를 승인하거나 거절한다.
- CEO는 검증 결과를 보고 Shadow/Paper 승격만 승인한다.
- Trading은 승인된 불변 Strategy Bundle만 실행하고 Quant의 임의 Python을 직접 실행하지 않는다.
