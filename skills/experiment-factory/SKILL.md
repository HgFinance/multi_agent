---
name: experiment-factory
description: "Run a research experiment proposal end to end: Gate 0 intake, preregistration, custom signal authoring under a PIT-safe sandbox, deterministic result readout, and controlled-vocabulary feedback. Judgement stays with deterministic code; the agent proposes and explains."
version: 0.1.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [quant, backtest, preregistration, overfitting, factory]
    related_skills: [methodology-scout]
---

# Experiment Factory: 기획안 → 카드 → 환류

## Overview

이 스킬은 퀀트본부의 본업 절차다. 리서치가 낸 `ExperimentProposalV1` 을 받아
**결과를 보기 전에 잠그고**, 결정론적으로 실험하고, 성공·실패를 가리지 않고 환류한다.

이 부서의 일 대부분은 이미 결정론 코드다. **에이전트가 하는 일은 네 가지뿐이고,
전부 코드가 못 하는 것이다:** 자연어 접수, 설계 제안, 결과 해석, 교훈 사상.
수익률·통계량·관문 판정을 다시 계산하지 않는다 — 겹치면 계산을 두 번 하거나
판정을 흉내 낸다.

구현: `departments/04-quant-backtest/pipeline/` (사전등록·PIT·백테스트·walk-forward·
trial_family·DSR·PBO(CSCV)·국면 분해·릴리스 관문·전략 템플릿·전략 스펙·공장 다리)

## When to use

- `research.experiment_proposals` 에 `PUBLISHED` 기획안이 있을 때
- 기존 실험이 종결돼 환류를 적재해야 할 때
- 기존 템플릿으로 표현되지 않는 방법론이 들어와 시그널 코드를 써야 할 때

## 절차

### 1. 접수 — Gate 0 (`factory_bridge.gate0`)

셋을 결정론으로 판정한다. **당신이 판정하지 않는다. 결과를 읽고 설명한다.**

- **어휘 사상** — `edge_type`/`universe_key` 가 실행면 통제 어휘에 있는가.
  없으면 `UNMAPPED_VOCAB`. **비슷한 템플릿으로 대신 돌리지 않는다** — 그 결과는
  이 가설의 증거가 아니라 다른 전략의 성적이다. 미구현 사유를 리서치에 돌려준다.
- **시도 예산** — 같은 family 에서 몇 번 실행했는가(`quant.experiments` 기준).
  넘으면 `OVER_BUDGET`, 증액은 CEO 결정이다.
- **기각 이력 대응** — 같은 family 의 기각 교훈마다 대응이 있는가.
  없으면 `DUPLICATE_UNADDRESSED` — 회사가 이미 산 실험을 다시 사는 것이다.

### 2. 사전 등록 — 결과를 보기 전에 잠근다

실질 필드(edge·universe·label·baseline·비용 모델·분할·반증 검사)를 불변 지문으로
고정한다. 이후 수정은 **같은 ID 로 불가능하고 새 시도로만** 가능하며, 새 시도는 계수된다.

기획안의 근거(경제적 근거·반대편·경쟁 설명·회의론자 서명)를 **복사해 둔다.**
기획안이 나중에 수정돼도 이 실험이 무엇을 등록했는지는 변하면 안 된다 —
사전등록의 의미가 그것이다.

### 3. 시그널 — 템플릿이 있으면 쓰고, 없으면 쓴다

**기성 템플릿 8종**: momentum · mean_reversion · low_volatility ·
risk_adjusted_momentum · liquidity_shock_reversal · breakout · trend_following ·
illiquidity_premium

없으면 **직접 코드를 쓴다**(`strategy_spec.from_code`). 이것이 공장이 사람 손에
막히지 않는 지점이다. 규칙:

- 함수는 `signal(view, params) -> dict[str, float]` 하나
- `view` 는 **기준일 이하만** 노출한다. 미래를 꺼낼 접근자가 아예 없으니 우회를
  시도하지 말고, 필요한 데이터가 없으면 `NEEDS_DATA` 로 반려한다
- 임포트·`eval`/`exec`/`open`·dunder 접근·최상위 실행문은 문법 수준에서 거부된다
- 표본이 모자란 종목은 **결과에서 뺀다.** 0 은 순위 중앙에 앉아 조용히 선택된다
- **코드 해시가 사전등록 지문에 들어간다.** 결과를 본 뒤 고치는 것은 수정이 아니라
  새 시도이고, DSR 이 그만큼 감가한다

코드는 **제안**이다. 승인은 결정론 검증(`validate_code` → `verify`)이 한다.

### 4. 실행 — 손대지 않는다

러너는 LLM 을 호출하지 않는다. 당신은 실행 결과를 읽을 뿐이다.

### 5. 해석 — 숫자를 다시 만들지 않는다

`ExperimentCardV1` 을 읽고 사람이 읽을 문장으로 만든다.

- **DSR** — 시도 횟수를 감안한 값이다. 시도 12회면 기대 최대 샤프가 1.665까지
  올라간다(실측). "샤프가 1.5다"가 아니라 "12번 시도해 고른 1.5다"로 읽는다
- **PBO** — 전략이 나쁘다가 아니라 **고르는 행위가 신뢰되는가**다. 창이 8개 미만이면
  위로 편향이므로 "과적합이 이만큼"이 아니라 "이 표본으로는 선택을 신뢰할 수 없다"
- **국면 분해** — 총계가 숨긴 비대칭을 본다. 상승장 +17%p / 하락장 −18%p 같은 형태는
  총수익이 좋아도 운용할 수 없다
- **소스 대조** — 기획안의 `source_reported_effect` 와 우리 결과가 벌어지면 그 격차
  자체가 발견이다. 전이 실패인지 구현 차이인지 말한다. **두 숫자를 같은 측정처럼
  제시하지 않는다**
- **NOT_RUN 은 통과가 아니다.** 안 돌린 것을 통과로 세면 검증표 전체가 장식이다

관문 판정과 경쟁하지 않는다. `release_gate` 가 이미 결정론으로 판정했고, 당신의
문장은 그 판정을 **설명**한다.

### 6. 환류 — 적재가 종결의 전제 조건이다

모든 종결에 `ExperimentOutcomeV1` 을 적재한다. **성공도 적재한다** — 리서치는 무엇이
통했는지도 배워야 한다. 운용 단계의 킬·강등·폐기도 포함이다.

교훈은 **통제 어휘**로만 쓴다: `LEAKAGE_SUSPECT` `COST_SENSITIVE` `BEAR_FRAGILE`
`SINGLE_REGIME_ONLY` `OVERFIT_PBO` `OVERFIT_DSR` `UNDERPOWERED_DATA`
`CAPACITY_DOUBT` `BASELINE_NOT_BEATEN` `LIVE_SLIPPAGE_BLOWN` `LIVE_MDD_EXCEEDED`

자유 서술 교훈은 다음 기획안과 기계 대조가 안 되고, 대조가 안 되는 교훈은 Gate 0
에서 아무것도 막지 못한다. 어휘에 없는 진짜 사유가 있으면 **억지로 끼워 맞추지 말고
어휘 추가를 요청한다.**

`finalize()` 가 환류 적재와 상태 전이를 한 트랜잭션으로 묶는다 — 적재가 실패하면
상태도 안 바뀐다. 조용히 종결되고 교훈만 사라지는 경로는 없다.

## 미측정과 0 을 구분한다

측정 안 한 지표는 `None` 이지 0 이 아니다. 0 은 "재 봤더니 0"이고 `None` 은 "안 쟀다"다.
섞이면 관문이 미측정을 통과로 읽는다. 카드·환류·요약 어디서도 채우지 않는다.

## 하지 않는 것

- **가설 생성** — 리서치 소관이다. 스스로 낸 가설을 스스로 검증하면 제안자와 승인자가
  같아져 생성자·검증자 분리가 조직 안에서 무너진다
- Production 승격 — QA 재현 → Risk 수용력 → **사람의 최종 서명**이 필요하다
- 실패 실험 삭제, 최고 결과만 보고, 관문 우회
- 실행면(`pipeline/`)에 직접 커밋 — 검증을 통과한 스펙만 들어간다

## 누수 의심

즉시 실험을 무효화한다. 각주로 남기지 않는다. 누수가 있는 백테스트는 나쁜 결과가
아니라 **결과가 아니다.**
