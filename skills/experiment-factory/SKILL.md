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

### 4. 실행 — 네가 돌린다, 다만 결정론 진입점으로만

러너는 LLM 을 호출하지 않는다. **숫자를 만드는 것은 언제나 코드다.** 그러나
그 코드를 *돌리는 것*은 네 일이다. 예전에는 이 절이 "손대지 않는다 / 결과를
읽을 뿐이다" 였는데, 그 결과 실험 워커가 못 집는 상황에서 **아무도 실행하지
않는 구간**이 생겼다(2026-08-12 실측: 카드 3장이 11분씩 조사만 하고 전부
`NOT_RUNNABLE` 로 끝났다. 실행면은 그 자리에 있었다).

**실행면**

```
저장소   /app/departments/04-quant-backtest
파이썬   quant-py     ← pandas·numpy·psycopg2 가 들어 있다
         system python3 에는 없다. ModuleNotFoundError 가 나면 이걸 안 쓴 것이다
```

```bash
cd /app/departments/04-quant-backtest
quant-py pipeline/experiment_orchestrator.py --run --hypothesis <id>   # 실험 한 건
quant-py pipeline/pit_dataset.py --build --name <n> --version <v> \
         --from <YYYY-MM-DD> --to <YYYY-MM-DD>                         # 데이터셋
quant-py pipeline/backtest_runner.py --run                             # 백테스트 단독
quant-py pipeline/<모듈>.py                                            # 인자 없이 = 자체점검
```

**데이터를 먼저 본다 - 설계는 그다음이다**

두 원장에 직접 붙을 수 있다. 프로필 `env:` 에 DSN 이 있으므로 코드에서 그대로 읽는다.

```python
import os, psycopg2
mkt = psycopg2.connect(os.environ["TIMESCALE_DATABASE_URL"])   # 시세 (market.*)
led = psycopg2.connect(os.environ["DATABASE_URL"])             # 업무 원장 (quant.*)
```

설계 전에 **네가 직접 재라.** 브리핑의 요약을 믿고 시작하지 마라 - 요약은 한
주기 전 사실이고, 데이터는 매일 들어오고 리텐션으로 밀려난다.

```sql
-- 무엇이 얼마나 있는가 (실측 2026-08-12)
select interval_code, count(*), count(distinct instrument_id),
       min(bucket_time)::date, max(bucket_time)::date
  from market.market_bars group by 1;
--   1D  7,261,269행  3,924종목  2016-01-03 ~ 2026-08-10
--   1M  3,756,660행    350종목  2026-04-02 ~ 2026-07-31   ← 종목이 좁다
-- market_quotes 는 2026-08-09~08-12 **나흘치뿐**이다(리텐션). 호가·체결 기반
-- 가설은 지금 표본으로 검정할 수 없다 - 그렇게 적고 반려해라. 억지로 돌리면
-- 나흘로 낸 수치가 원장에 남는다.
```

**표본이 설계를 정한다.** 형성창을 정하기 전에 walk-forward 창이 몇 개 나오는지
직접 세라 - 창이 3개 미만이면 강건성 판정이 성립하지 않는다.

```python
quant-py -c "
from walk_forward import make_windows, WARMUP_TRADING_DAYS
# dates = 데이터셋의 거래일 목록
print(len(make_windows(dates, max(WARMUP_TRADING_DAYS, lookback+1))))"
```

**필요한 데이터셋이 없으면 만든다.** 매니페스트가 덮는 원천만 실험까지 가므로,
없으면 반려로 끝내지 말고 빌드한다. 빌드는 `content_hash`·파티션·품질검사를
같이 박으므로 반드시 이 진입점으로 한다.

```bash
quant-py pipeline/pit_dataset.py --build --name <이름> --version <버전> \
         --from <YYYY-MM-DD> --to <YYYY-MM-DD>
quant-py -c "..."   # quant.dataset_manifests 로 등재됐는지 확인
```

버전을 올릴 때는 **덮어쓰지 않는다.** v2 가 있으면 v3 를 만든다 - 같은 경로에
덮으면 매니페스트 해시와 파일이 어긋나고, 그건 재현성이 깨진 것이다.

**언제 직접 돌리나**

- 카드가 실험을 요구하는데 큐에 작업이 없거나 워커가 못 집을 때
- 데이터셋이 짧거나 없어서 막혔을 때 — 반려로 끝내지 말고 새 버전을 빌드한다
- 어휘에 없는 edge 라면 템플릿을 쓰거나(3절) `NOT_IMPLEMENTED` 에 사유와 함께 등재한다

**막혔다고 쓰기 전에 확인한다.** "실행면이 없다"고 적기 전에 위 경로를 실제로
열어 봤는지 본다. **있는 것을 없다고 적으면 그 보고가 다음 사람을 더 멀리
돌아가게 한다.** 진짜로 없으면 무엇이 없는지 정확히 적는다 — 그 구분이 이
카드의 값이다.

**실행면을 진화시킨다 - 러너도 고쳐도 된다**

기성 템플릿과 러너로 답이 안 나오면 **고친다.** 시그널만이 아니라 백테스트
러너·창 분할·비용 모델까지 대상이다. 공장이 발전하는 방식이 그것이다.

안전한 이유는 이미 구조에 있다:

```
code_version() = RUNNER_VERSION + sha256(파일)      # 코드가 바뀌면 값이 바뀐다
input_hash     = sha256({dataset, config, code, seed, cost})
```

러너를 고치면 `input_hash` 가 달라져 **새 실험**이 된다. 과거 결과는 그대로
남고 새 버전은 새 행으로 쌓인다 - v1 부터 v200 까지 성과를 나란히 볼 수 있다.

**다만 시도 카운터는 리셋되지 않는다.** `trial_family` 는
`(edge_type, universe_key, label, baseline)` 로만 정해지고 **코드 버전이 안
들어간다.** 그래서 러너를 고쳐 같은 개념을 다시 돌리면 그 계열의 N+1번째
시도로 세어지고 DSR 이 그만큼 감가한다.

> 결과가 나쁘다고 러너를 고쳐 다시 돌리는 것은 막히지 않는다. 대신 **세어진다.**
> 20번 고쳐 돌려 나온 Sharpe 1.5 는 20번째 시도의 1.5 로 읽힌다. 그것이
> 정직한 값이고, 그 값으로 통과하면 진짜 통과다.

고쳤으면 **무엇을 왜 고쳤는지 카드에 적는다.** QA·감사가 그것을 읽는다.
그리고 교훈은 메모리에 남긴다 - 원장은 무엇이 일어났는지를 담고, 메모리는
네가 그것에 대해 무엇을 결론지었는지를 담는다. 다음 주기의 너는 둘 다 없으면
같은 자리에서 다시 시작한다.

**여전히 안 되는 것**

- 결과를 본 뒤 config·시그널 코드를 고쳐 다시 돌리기 → 새 시도다(DSR 이 감가한다)
- SQL 로 `quant.*` 에 직접 쓰기 → 사전등록 지문·`content_hash`·매니페스트가
  진입점 안에서 박힌다. 밖에서 만든 숫자는 재현이 안 되므로 결과가 아니다
- 실패한 실행을 지우고 다시 돌리기

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
- 검증 없이 실행면에 밀어 넣는 것. **코드를 고치는 것 자체는 금지가 아니다** —
  아래 "실행면을 진화시킨다" 참고

## 누수 의심

즉시 실험을 무효화한다. 각주로 남기지 않는다. 누수가 있는 백테스트는 나쁜 결과가
아니라 **결과가 아니다.**
