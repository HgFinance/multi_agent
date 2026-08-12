# AWS 데이터 이전 계획 (Parquet 경유)

> 상태: 초안 — 실행 전
>
> 기준일: 2026-08-12 · 소유: 재일 (리서치/퀀트)
>
> 상위 기준: [MASTER_PLAN](../HEDGE_FUND_MASTER_PLAN.md) → [DATA_GOVERNANCE_GUIDE](../02-engineering/DATA_GOVERNANCE_GUIDE.md)

## 1. 이 문서가 답하는 것

로컬에서 목표 3단계(전략 1호 → PAPER 전 부서 검증 → 사용자 질의)를 달성한 뒤
AWS 로 옮길 때 **데이터를 어떻게 옮기고, 옮긴 뒤 퀀트 실험이 끊기지 않는가.**

결론부터: **끊기지 않는다.** 다만 그러려면 옮기는 대상을 계층으로 나눠야 하고,
원시 시계열은 DB-to-DB 가 아니라 Parquet 을 거쳐야 한다.

## 2. 지금 무엇을 갖고 있나 (2026-08-12 실측)

| 계층 | 크기 | 행 수 | 범위 |
|---|---|---|---|
| `market.market_bars` 1D | **2,479 MB** | 726만 | 2016-01-03 ~ (3,924종목) |
| `market.microstructure_features` | **31 MB** | 148,931 | 59거래일 (2,558종목) |
| `market.market_ticks` 원시 | 28 GB | 4.6억 | 압축 후 **0.8 GB/일** |
| `market.market_quotes` 원시 | 74 GB | 1.8억 | 압축 후 **1.7 GB/일** |
| `research.*` (Supabase) | — | — | **이미 클라우드. 옮길 것 없음** |

## 3. 계층 분리 — 무엇을 꼭 옮기는가

### 3.1 필수 (합계 약 2.5 GB)

- `market_bars` 1D — 백테스트의 주 재료. 2,601거래일 × 3,924종목.
- `microstructure_features` — 호가·체결에서 접은 종목·일자 피처.

**백테스트가 읽는 것은 원시가 아니라 이 둘이다.** `data_resolution` 이 사상하는
데이터셋이 `krx-basket-daily/v2` 와 `krx-microstructure-daily/v1` 이고, 둘 다
이 계층을 가리킨다. 즉 **2.5 GB 만 옮겨도 퀀트 실험은 그대로 돈다.**

### 3.2 재계산용 (선택, 25~75 GB)

원시 호가·체결. 필요한 유일한 상황은 **피처 정의를 바꿔 다시 계산할 때**
(`feature_set_version` 을 `ms-daily-v1` 에서 v2 로 올릴 때)다.

| 범위 | 크기 | 언제 이 선택인가 |
|---|---|---|
| 최근 5~10거래일 | 12~25 GB | 피처 정의가 안정적일 때 |
| 최근 30거래일 | **75 GB** | 정의를 몇 번 더 바꿀 계획일 때 |

`ms-daily-v1` 이 첫 정의라 한두 번 바뀔 가능성이 높다 — **30거래일을 권한다.**

### 3.3 옮기지 않는 것

- `research.documents` / `document_instruments` / `macro_observations`
  — 원문·거시 원계열은 더 안 쌓는다. DART 를 MCP 로 조회해 **점수만** 적재하는
  쪽으로 바뀌었다([QUALITATIVE_FACTOR_SPEC](../02-engineering/QUALITATIVE_FACTOR_SPEC.md)).
- `market_quotes` / `market_ticks` 중 §3.2 범위 밖 — 저쪽(Trading_bot)에 남고,
  필요하면 그때 다시 접는다(§5.2).

## 4. 왜 Parquet 인가

`pg_dump` 는 행 형식이라 압축 컬럼 저장보다 커지고 복원도 느리다. 그리고 옮기고
나면 **사본이 안 남는다.**

Parquet 을 거치면:

1. **컬럼 저장이 유리하다.** 호가 10단계(`bid_prices`·`ask_sizes` 배열)는 열 방향
   반복이 심해 압축이 잘 든다.
2. **저장소가 이미 그 구조다.** `quant.dataset_manifests` + `dataset_partitions` +
   `content_hash` — 옮긴 것이 같은 것인지 **해시로 증명된다.** `pit_dataset.py` 가
   일봉으로 이미 그렇게 하고 있고, 백테스트는 그 파티션을 읽는다.
3. **S3 가 곧 백업이다.** 이전이 끝나도 원본이 객체 저장소에 남는다.

## 5. 실행 순서

### 5.1 이전 전 (로컬에서, AWS 없이도 가치 있음)

1. `pit_dataset.py` 빌더를 **원천별로 일반화**한다. 지금은 일봉 전용
   (`BAR_SOURCE`/`INTERVAL` 상수)이다. `microstructure_features` 와 원시
   시계열을 같은 방식으로 굳힐 수 있어야 한다.
2. 필수 계층(§3.1)을 Parquet 파티션으로 굳히고 `content_hash` 를 기록한다.
3. **로컬에서 그 파티션으로 백테스트를 한 번 돌린다.** DB 가 아니라 파일에서
   읽어도 같은 결과가 나오는지 확인 — 여기서 안 맞으면 AWS 에서 디버깅하게 된다.

### 5.2 이전 시점

4. §3.2 범위의 원시를 일자별 Parquet 으로 내보내 S3 에 올린다.
5. AWS 쪽에서 필수 계층을 로드한다. 원시는 **필요할 때 로드**한다 — 재계산을
   안 하면 S3 에 둔 채로 둔다.
6. `dataset_manifests` 를 옮긴다(메타는 Supabase 라 이미 공유된다 — 확인만).

### 5.3 이전 후

7. AWS 에서 `ls-realtime` 이 돌기 시작하면 그날부터 자기 것을 쌓는다.
   통합시세(US3/UH1) 구독이라 KRX+NXT 를 다 받고, 수집창은 08:00~20:05 다.
8. `chart-daily-universe`(21:00)가 일봉을 이어 받는다.
9. `microstructure_builder --build` 가 매일 그날치를 접는다.
   → **저쪽(Trading_bot) 의존은 이 시점에 끝난다.**

## 6. 이전 후 퀀트 실험이 끊기지 않는 근거

| 실험이 필요로 하는 것 | 이전 후 출처 | 끊기나 |
|---|---|---|
| 일봉 2,601일 | Parquet 이전 + `chart-daily-universe` | 안 끊김 |
| 마이크로구조 피처 | Parquet 이전 + `microstructure_builder` 일별 | 안 끊김 |
| 재무 정량 | `research.financial_facts` (Supabase, 그대로) | 안 끊김 |
| 정성 점수 팩터 | `research.qualitative_scores` (forward-only) | 이전과 무관 |
| 원시 재계산 | S3 Parquet → 필요 시 로드 | 지연은 있으나 가능 |

**끊기는 유일한 경로**는 §3.2 범위 **밖**의 과거 원시로 피처를 재계산하려는
경우다. 그때는 저쪽 DB 가 살아 있어야 하는데, 이전 후에는 그것을 전제할 수 없다.
그래서 §3.2 범위를 정하는 것이 곧 **"과거 어디까지 재계산할 수 있는가"** 를
정하는 것이다.

## 7. 아직 안 정한 것 (임의로 정하지 않는다)

- S3 버킷·리전·수명주기 정책
- AWS 쪽 TimescaleDB 형태(자체 호스팅 / Timescale Cloud / RDS+확장)
- §3.2 범위의 최종 결정 (5~10일 vs 30일)
- 이전 중 로컬을 언제 멈출 것인가 (수집 공백을 만들지 않으려면 겹치는 구간이 필요)

## 8. 이 계획이 지키는 원칙

- **못 옮긴 것을 옮긴 것으로 세지 않는다.** `content_hash` 가 같아야 같은 것이다.
- **원시가 없다고 실험이 멈추지 않는다.** 피처 계층이 백테스트의 입력이다.
- **선언과 실재를 대조한다.** 매니페스트에 등재하되 파일이 없는 상태를 만들지
  않는다 — 그건 레지스트리에 `ADOPTED` 라 적고 값이 안 나오는 것과 같다.
