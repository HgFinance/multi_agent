# 퀀트/백테스트본부 (Quant / Backtest)

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
Local Ollama Alias는 [`Modelfile`](Modelfile)의 `qwen3:14b` 기반 `agent-quant`이고 Hermes Profile은
`quant-backtest-department`다. Build·Eval·권한 기준은 [Ollama Department Modelfile Guide](../../docs/02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)를 따른다.

실제 실행 상태와 재일님 2주 계획·Daily Scrum은 [실행 현황과 통합 계획 v2.2](../../docs/PROJECT_IMPLEMENTATION_STATUS.md#41-재일님-리서치본부와-퀀트백테스트본부)을 기준으로 한다.

## Mission

전략 가설, Point-in-Time Dataset, Backtest, Walk-Forward와 Release Candidate를 담당한다. 검증된 불변
Strategy Bundle만 Shadow/Paper 배포 후보로 제출하며, 실시간 운용 중 전략 코드를 직접 수정하지 않는다.

`quant-backtest-department`는 Production 승격을 직접 하지 않는다. QA 독립 검증, Risk Capability와
CEO 승인이 필요하다. Backtest 수익률이 좋아도 미래 데이터, 거래비용, 과적합 또는 필요한 거래 기능이
검증되지 않으면 후보를 거절한다.

## Owner

재일님 — [TEAM_JAEIL_RESEARCH_QUANT_GUIDE](../../docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md)

## 입력·출력 계약

- 입력: TimescaleDB 시장 시계열, Versioned Universe, Research Observation과 전략 가설
- 내부 기록: `quant.dataset_*`, `hypotheses`, `experiments`, `backtest_*`, `experiment_metrics`
- 출력: Dataset·Code·Cost·Metric·Fragility가 연결된 Strategy Candidate
- Handoff: QA 독립 검증 → Risk Capability Review → CEO Shadow/Paper 승인

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
- Strategy Registry에 Capability, QA, Risk, CEO 승인과 Shadow/Paper 상태를 연결한다.
- Dataset·Code·Dependency·Seed·Cost Model을 CI에서 같은 결과로 재현한다.
- 현재 `TESTING` Hypothesis 2개의 종료 조건을 명시하고 고아 Experiment를 방지한다.
- AI Office에는 Tick 원문이 아니라 Experiment·Candidate Read Model만 제공한다.

## Handoff

- Research는 Observation과 Evidence Reference를 제공한다.
- QA는 Leakage, Metric, Claim과 Model Risk를 독립 검증한다.
- Risk는 Strategy Family와 상품별 Capability·한도를 승인하거나 거절한다.
- CEO는 검증 결과를 보고 Shadow/Paper 승격만 승인한다.
- Trading은 승인된 불변 Strategy Bundle만 실행하고 Quant의 임의 Python을 직접 실행하지 않는다.
