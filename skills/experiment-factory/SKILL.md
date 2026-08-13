---
name: experiment-factory
description: "Run a research experiment proposal end to end: Gate 0 intake, preregistration, custom signal authoring under a PIT-safe sandbox, deterministic result readout, and controlled-vocabulary feedback. Judgement stays with deterministic code; the agent proposes and explains."
version: 0.1.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [quant, backtest, preregistration, overfitting, factory]
    related_skills: [methodology-scout, wiring-audit, dataset-engineering,
                     skill-authoring]
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

**카드를 열면 먼저 내 능력을 훑는다 (한 줄)**

```bash
sh "$(find /opt/data -name capabilities.sh | head -1)"
```

실행면·경로·마운트·DSN·HTTP 창구·스킬이 한 화면에 나온다. **"없다"·"못 한다"
를 적으려면 이 목록을 먼저 봐라** - 2026-08-12 에 세 번, 있는 것을 없다고 하고
카드를 닫았다(`source_registry ABSENT`, `시세 조회 도구 없음`,
`ModuleNotFoundError`). 셋 다 있었고, 한 번 훑으면 끝날 일이었다.

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

**시세는 HTTP 로도 물어본다 - MCP 가 없어도 된다**

`market-api` 가 같은 네트워크에 떠 있다. DSN·SQL 없이 바로 읽는다.

```bash
curl -s "http://market-api:8036/bars/005930?limit=120"      # 일봉
curl -s "http://market-api:8036/microstructure/005930"      # 오늘 체결·호가 요약
curl -s "http://market-api:8036/snapshot/005930"            # 현재 스냅샷
curl -s "http://market-api:8036/breadth"                    # 시장 폭
curl -s "http://market-api:8036/regime/daily"               # 국면
curl -s "http://market-api:8036/dq/summary"                 # 데이터 품질
curl -s "http://market-api:8036/dq/bar_freshness"           # 봉 신선도
```

실측(2026-08-12): `/bars/005930` 이 일봉을, `/microstructure/005930` 이 그날
체결 660,983건·호가 29,022건 요약을 돌려줬다. **퀀트 프로필에 `mcp_servers` 가
없다고 해서 도구가 없는 게 아니다** - 이 창구는 인증 없이 열려 있다.

`/bars` 는 **최신순**으로 온다. `closes[-1]` 을 최신으로 쓰면 120봉 조회에서
가장 오래된 종가를 집는다(실측 사고). 정렬을 확인하고 써라.

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

> **데이터가 있는데 안 쓰이거나, 써 보니 안 될 때는 `dataset-engineering` 을
> 먼저 열어라.** 여덟 층을 실제로 열어 보는 `probe_dataset.py` 가 있다.
> `BadGzipFile` 로 죽은 실험이 알고 보니 파일은 멀쩡하고 148,931행이 정상이었던
> 일이 있다 — 막힌 층은 여섯 번째였고 고칠 곳이 완전히 달랐다.
> 뚫었으면 `skill-authoring` 으로 남겨라. 안 남기면 다음 주기에 또 판다.

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

**병목은 네가 푼다 - 보고하고 멈추지 않는다**

이 부서에서 "느리다·막혔다" 는 보고 대상이 아니라 **네 작업**이다. 무엇이
느린지 재고, 원인을 짚고, 손잡이가 있으면 돌려라. 손잡이가 없으면 만들어라.

판단은 재고 하라 - 카드 본문에 큐 적체와 한 건당 소요가 실린다.

```
큐: 대기 28건 / 실행 1건 · 최근 한 건 평균 110초 · 직렬이면 약 51분
```

| 병목 | 손잡이 |
|---|---|
| 큐가 밀린다 | `QUANT_EXPERIMENT_BATCH` 를 올려 병렬로 집는다 |
| 표본이 얇아 판정이 안 선다 | `pit_dataset.py --build` 로 넓은 버전을 만든다 |
| 창이 안 나온다 | `horizon_days` 를 창이 3개 이상 나오는 값으로 |
| 기성 템플릿으로 안 된다 | 시그널을 쓴다(`strategy_spec.from_code`) |
| 러너가 못 한다 | 러너를 고친다 - 다음 시도로 세어질 뿐이다 |
| 데이터가 어디 있는지 모른다 | `data_resolution.py --list` 로 카탈로그를 본다 |

병렬도를 올려도 안전한 이유는 구조에 있다: `lease` 가 `for update skip locked`
라 같은 작업을 둘이 못 집고, `input_hash` 가 중복 실험을 거부한다. **올린 값과
그 근거를 카드에 적어라** - 다음 사람이 왜 그 값인지 알아야 되돌리거나 더 올린다.

> 오늘(2026-08-12) 고친 것 중 열두 건이 "손잡이는 있는데 아무도 안 돌린" 것이었다.
> 카탈로그가 2종만 적혀 있었고, 스킬이 실행을 금지했고, 도구가 PATH 밖에 있었다.
> **막혔다고 적기 전에 손잡이를 찾아라.** 없으면 그때 없다고 적어라 - 그 구분이
> 이 카드의 값이다.

**"없다" 는 결론을 내기 전에 `wiring-audit` 을 돌린다**

`값이 None 이다` · `지표가 부족하다` · `실행면이 없다` · `데이터셋이 없다` -
이 문장들이 나오려 하면 그 전에 배관을 훑어라. 위 열두 건 중 여섯 건이 정확히
그 문장이었고 **여섯 건 다 실제로는 있었다.**

```bash
SCAN=$(find /opt/data -path "*wiring-audit/scripts/scan_wiring.py" | head -1)
python "$SCAN" /app/repo/departments/04-quant-backtest
```

`key_mismatch`(넣는 키 ≠ 읽는 키)와 `bounded_digit_regex`(수치를 잘라 읽음)가
신호가 세다. 스캔은 **후보만 낸다** - 그 경로를 실제로 불러 확인한 뒤 판정해라.

**여전히 안 되는 것**

- 결과를 본 뒤 config·시그널 코드를 고쳐 다시 돌리기 → 새 시도다(DSR 이 감가한다)
- SQL 로 `quant.*` 에 직접 쓰기 → 사전등록 지문·`content_hash`·매니페스트가
  진입점 안에서 박힌다. 밖에서 만든 숫자는 재현이 안 되므로 결과가 아니다
- 실패한 실행을 지우고 다시 돌리기

### 4-b. 알파가 있는데 낙폭에서 죽었다면 — 엣지를 바꾸지 말고 위험을 관리해라

**2026-08-12 실측.** momentum 이 이 성적을 냈다.

```
초과수익 +157.51%p   IR 1.26   Sharpe 1.28   DSR 0.976
낙폭 -50.52%  (관문 허용 -35%)   →  REJECTED
```

DSR 0.976 은 **시도 횟수를 감안해도 우연이 아니라는 뜻**이다(기준 0.95). 알파는
있었다. 죽은 자리는 위험관리였다. 그런데 그때 실행면에는 손잡이가 다섯 개
(`strategy`/`lookback_days`/`top_n`/`rebalance`/`initial_capital`)뿐이라
**완전투자 동일가중 롱온리 말고는 표현할 수가 없었다.** 그래서 리서치는 계속
새 엣지를 설계했고, 새 엣지도 위험관리가 없기는 마찬가지라 같은 자리에서 죽었다.

지금은 손잡이가 있다. `SUGGESTED_PARAMS`(=`expected_edge`)에 넣는다:

| 키 | 뜻 | 범위 |
|---|---|---|
| `max_drawdown_stop` | 고점 대비 이 낙폭이면 전량 현금 | −0.90 ~ −0.02 |
| `vol_target_annual` | 목표 연변동성. 실현이 크면 그만큼 노출을 줄인다 | 0.02 ~ 1.0 |
| `max_exposure` | 익스포저 상한 | 0.1 ~ **1.0** |
| `vol_lookback_days` | 변동성 추정 창 | 20 ~ 250 |

**안 적으면 꺼진 채로 돈다** — 예전 결과와 비트 단위로 같다(사전등록 무결성).
**레버리지는 열려 있지 않다**(`max_exposure` 상한 1.0). 개발원칙 9: 위험한
기능은 실패 시 거래 확대가 아니라 Entry 차단 방향으로 동작한다.

읽는 법:

- 관문 판정은 이제 **기각에도 적재된다.** 환류 `notes` 에
  `관문 4/8 통과. 남은 조항: max_drawdown -50.52% (기준 -35.0%, 15.52% 모자람)…`
  형태로 남는다. **"몇 개 통과했고 무엇이 얼마나 모자랐나"** 를 먼저 봐라
- 통과한 조항이 절반을 넘는 계열은 브리핑의 `[관문에 근접한 계열]` 에 뜬다.
  **거기 있는 것은 새 엣지보다 먼저다** — 찾은 알파를 버리고 새로 찾는 것보다
  붙이는 쪽이 훨씬 싸다
- **`미측정` 은 실패가 아니다.** `pbo 미측정` 은 "과적합됐다"가 아니라 "안 쟀다"이다.
  차단은 되지만 교훈으로 적히지 않는다 — 재는 것이 답이다
- 같은 계열 재시도는 trial 수를 올리고 **DSR 이 그만큼 조여진다.** 값을 여러 번
  던져 고르면 그 최고치는 실력과 운을 못 가린다. 근거를 갖고 한 번에 골라라

`risk_exposure()` 는 자기 자산곡선만 본다(구조적 PIT). 낙폭 정지는 리밸런스일에만
평가된다 — 월 리밸런스면 월 단위로 끊긴다는 뜻이다. 그것이 가설의 일부다.

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
