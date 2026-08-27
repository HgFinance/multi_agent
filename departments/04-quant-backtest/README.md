# 퀀트/백테스트본부 (Quant / Backtest)

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
Hermes Profile은 `quant-backtest-department`이고 직원은 독립 LangGraph Worker다.
활성 Worker·결정론 경계는 [Worker Role Boundaries](../../docs/02-engineering/WORKER_ROLE_BOUNDARIES.md),
모델·fallback은 [Worker Model Matrix](../../docs/02-engineering/WORKER_MODEL_MATRIX.md)가 소유한다.

실제 실행 상태와 재일님 2주 계획·Daily Scrum은 [실행 현황과 통합 계획 v2.2](../../docs/PROJECT_IMPLEMENTATION_STATUS.md#41-재일님-리서치본부와-퀀트백테스트본부)을 기준으로 한다.
Research Evidence를 전략 가설과 독립 검증으로 연결하는 목표 Graph와 계약은
[Research-Quant Evidence-to-Strategy Framework](../../docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md)를 따른다.
투자자 Persona를 측정 가능한 투자 원칙과 조건부 Fine-tuned Reviewer로 만드는 설계는
[Investment Doctrine Model Factory](../../docs/02-engineering/INVESTMENT_DOCTRINE_MODEL_FACTORY.md)를 따른다.

## Mission

**검증 실험실이다.** Research의 자율 연구실이 등록한 계획과 불변 결과를 받아, Point-in-Time
데이터·현실적 비용·OOS 분리·강건성·거래 경로·실패 모드를 결정론적으로 검증한다. 결과는
QA·Risk·사람의 별도 심사를 위한 후보 보고로만 반환하며, 이 부서는 가설을 만들거나 주문·승격을
수행하지 않는다.

**가설 발굴은 이 부서 일이 아니다**(2026-08-10 이관). 스스로 낸 가설을 스스로 검증하면
제안자와 승인자가 같아져 생성자·검증자 분리가 조직 안에서 무너진다. 발굴은 리서치, 검증은 퀀트다.

`quant-backtest-department`는 Production 승격을 직접 하지 않는다. QA 재현 검증, Risk Capability와
**사람의 최종 서명**이 필요하다. Backtest 수익률이 좋아도 미래 데이터, 거래비용, 과적합 또는 필요한
거래 기능이 검증되지 않으면 후보를 거절한다.

## Owner

재일님 — [TEAM_JAEIL_RESEARCH_QUANT_GUIDE](../../docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md)

## 입력·출력 계약

- 입력: **자율 연구 계획/불변 결과 아티팩트**, TimescaleDB 시장 시계열, Versioned Universe
- 내부 기록: `quant.dataset_*`, `hypotheses`, `experiments`, `backtest_*`, `experiment_metrics`
- 출력: 검증 리포트와 `autonomous-strategy-candidate.v1` 후보 아티팩트(비용·OOS·강건성·실패 모드 포함)
- Handoff: Release Gate(결정론) → QA 재현 검증 → Risk Capability Review → 인간 승인

## 직원 편제 (LLM Worker 2인)

| Worker | 역할 | 실행 |
|---|---|---|
| `strategy-author-worker` (QNT-05) | **기성 템플릿에 없는 방법론의 시그널 코드 작성** — 코드 해시가 사전등록 지문에 들어간다 | 소집 (`strategy_authoring`) |
| `result-interpretation-worker` (QNT-03) | DSR·PBO·국면 해석 + **소스 보고 지표와 우리 결과 대조** — 수치 재계산·판정 금지 | 소집 (`experiment_card`) |

> **2026-08-11 감축 (5 → 2).** 남긴 둘은 본부장이 대신할 수 없는 이유가 있다 —
> 시그널 작성은 **격리된 긴 컨텍스트**가 필요하고, 결과 해석은 여러 실험이 동시에
> 종결될 때 **진짜 병렬**이다.
>
> - **`proposal-intake`·`experiment-design` → 본부장 흡수.** Gate 0 는
>   `factory_bridge.gate0()` 가 결정론으로 판정하므로 에이전트 몫은 자연어 서술뿐이고,
>   설계는 실험당 1회라 병렬성이 없다.
> - **`outcome-lesson` → 폐지.** `lessons_from()` 이 이미 결정론 기본값을 낸다.
>   환류가 에이전트 가용성에 묶이면 그것은 **조용히 멈추는 환류**다.

**전략 코드는 실험마다 달라도 된다 — 그게 정상이다.** 논문·서한에서 오는 방법론은 서로 다른
계산이라 템플릿 파라미터로만 표현하면 공장이 손잡이 돌리기가 된다. 그래서 QNT-05 가 시그널
코드를 쓴다. 막는 것은 코드가 다른 것이 아니라 **결과를 본 뒤 코드가 바뀌는 것**이고,
그건 코드 해시를 사전등록 지문에 넣어 막는다(고치면 새 시도 → DSR 감가).
시그널은 기준일 이하만 노출하는 뷰만 받으므로 미래를 꺼낼 경로가 아예 없다.
**작성은 에이전트가, 승인은 결정론 검증이 한다.**

직원이 7명 -> 5명 -> 2명으로 줄어든 것은 감원이 아니다. 계산과 판정(사전등록·PIT·백테스트·
walk-forward·DSR·PBO·국면·릴리스 관문)은 **이미 `pipeline/`의 결정론 코드가 하고 있고**,
그 위에 LLM을 겹쳐 두면 계산을 두 번 하거나 판정을 흉내 낸다. 남은 2자리는 결정론 코드가
못 하는 일 중에서도 **본부장이 대신할 수 없는 것** — 격리된 긴 컨텍스트가 필요한 시그널 코드 작성과, 동시 종결 시 진짜 병렬인 결과 해석 — 만 맡는다.

## 현재 구현

| 경로 | 역할 | 상태 |
|---|---|---|
| `agents/strategy_hypothesis_agent.py` | 관측 근거에서 가설 생성·등록 | **은퇴 예정.** 가설 생성은 리서치 소관이다 — 스스로 낸 가설을 스스로 검증하면 생성자·검증자 분리가 무너진다 |
| **`pipeline/factory_bridge.py`** | 기존 DB 실행·감사 기록이 참조하는 **보존된 레거시 브리지**. 새 자율 연구실의 입력·출력 계약에는 연결하지 않는다 | 의존성 이관 전 삭제 금지 |
| **`pipeline/data_resolution.py`** | 원천 테이블 -> 데이터셋 매니페스트 사상. 사상표를 코드에 박지 않고 `source_versions` 에서 유도하고, **로컬 DB 를 조회해 커버리지를 실측**한다 | 자체 점검 11/11 |
| **`pipeline/strategy_templates.py`** | 시그널 템플릿 8종 + `PITView`(기준일 초과 데이터를 꺼낼 접근자가 **없다**) | 자체 점검 10개 영역 |
| **`pipeline/strategy_spec.py`** | 템플릿에 없는 방법론을 위한 코드 작성면. AST 화이트리스트, 코드 해시가 사전등록 지문에 들어간다 | 자체 점검 12개 영역 |
| **`pipeline/preregistration.py`** | 결과를 보기 전 실질 필드를 불변 지문으로 고정. 수정은 새 시도로만 | 실전 전이 확인 |
| **`pipeline/config_binding.py`** | 가설 -> 백테스트 config. **읽지 않는 파라미터를 조용히 버리지 않는다** — 무시하면 등록한 가설과 실행한 실험이 달라진다 | 자체 점검 8개 영역 |
| `pipeline/pit_dataset.py` | Universe·Available Time을 보존한 Dataset Manifest·Partition | 자체 점검 3개 통과, 실제 Dataset 1개 |
| `pipeline/backtest_runner.py` | t-1 Signal, FIFO 손익, 비용과 재현 Hash | 자체 점검 5개 통과, 실제 Run 3개 |
| `pipeline/walk_forward.py` | 겹치지 않는 Window와 Fragility 판정 | 자체 점검 6개 통과 |
| `pipeline/intraday_microstructure.py` | 수신시각 기준 호가·체결 동기화, 복수 horizon markout, taker 및 보수적 passive FIFO 평가 | `tests/test_intraday_microstructure_lane.py` |
| `pipeline/experiment_orchestrator.py` | 데이터·전략 가능성 Gate와 상태 전이 | 자체 점검 3개 통과, 실제 Experiment 4개 |
| `hermes/` | 검증 실험실 Profile과 독립 조회 프로필 | 사용 중 |

> 새 전략 생성은 `departments/01-research/autonomous/`가 소유합니다. 이 README의
> `ExperimentProposalV1`·factory 관련 표와 기록은 기존 DB 실행 경로의 보존 설명이며, 새
> 파이프라인의 생성/제안 표면이 아닙니다.

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

### 완료 (2026-08-10 실전 관통으로 확인)

계보 연결(`proposal_id` -> `hypothesis_id` -> `experiment_id` -> `outcome_id`),
사전 등록, Trial Family·DSR·PBO(CSCV), Purge/Embargo, 환류 원자성.
기획안 1건이 리드 -> 기획 -> 회의론자 -> Gate 0 -> 사전등록 -> 백테스트 -> 종결 -> 환류를
실제로 통과했다.

### 남은 것 — 실측에서 드러난 순서

1. **형성 창과 보유 기간 분리.** 지금 `lookback_days` 하나에 묶여 있어 12-1 모멘텀 같은
   표준 사양(252일 형성 / 21일 보유)을 표현할 수 없다. 형성을 늘리면 회전이 같이 떨어지고
   walk-forward 창이 사라진다 — 실제로 126일 형성에서 창이 0개가 됐다.
2. **실패한 체인의 복구 경로.** 크래시하면 가설이 `RUNNING` 에 갇히고, 실험은 이미
   등록돼 재실행이 재현성 계약에 막힌다. 옳은 동작 둘이 겹쳐 교착이 된다 —
   "기존 실험에서 종결" 명령이 필요하다.
3. Quant API와 Job Worker를 Docker Compose에 추가한다.
4. Strategy Registry에 Capability·QA·Risk·CEO 승인과 Shadow/Paper 상태를 연결한다.
5. Dataset·Code·Dependency·Seed·Cost Model을 CI에서 같은 결과로 재현한다.
6. AI Office에는 Tick 원문이 아니라 Experiment·Candidate Read Model만 제공한다.
7. `QNT-08` Doctrine Profile — `InvestmentDoctrineV1` Fixture부터 구현하고 Fine-tuning은
   Need Gate 통과 후 실행한다.

## Handoff

- Research는 Observation과 Evidence Reference를 제공한다.
- QA는 Leakage, Metric, Claim과 Model Risk를 독립 검증한다.
- Risk는 Strategy Family와 상품별 Capability·한도를 승인하거나 거절한다.
- CEO는 검증 결과를 보고 Shadow/Paper 승격만 승인한다.
- Trading은 승인된 불변 Strategy Bundle만 실행하고 Quant의 임의 Python을 직접 실행하지 않는다.
