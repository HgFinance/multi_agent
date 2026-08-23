# HgFinance AWS L4 Quantization and Seven-axis Hybrid Report

## 공정성 기준

이 보고서의 7축 품질 비교는 동일 NVIDIA L4, 동일 L4-fp8KV-v1 runtime profile, 동일 vLLM/Python/CUDA/FlashInfer stack, 동일 max_model_len=8192, 동일 gpu_memory_utilization=0.85, 동일 kv_cache_dtype=fp8_e4m3, temperature 0, stream false, frozen dataset/scorer, sequential execution으로 수행된 결과입니다.

6열은 기존 AWQ+Hybrid에서 결정론적 Structured Output answer fallback만 제거한 baseline입니다. 7열은 6열에 Unit/Scale normalization만 추가한 A/B 단계입니다. 별도 metadata protocol, 외부 전용 router, 정답 하드코딩, 결정론적 answer fallback은 추가하지 않았습니다.

FinanceBench는 frozen scorer가 manual_required로 정의하므로 diagnostic 값만 자동 표시하며 공식 External Overall로 재명명하지 않습니다.

## 핵심 결론

- 자동 지표상 최적 후보: **AWQ+Hybrid + Unit/Scale**
- Internal: `82% → 88%`
- Financial Arithmetic: `60% → 80%`
- FinQA: `80% → 85%`
- TAT-QA: `93.3% → 93.3%`
- Auto Mean: `0.8563 → 0.8841`
- Critical Failures / Request Errors: `1 → 1`, `3 → 3`
- FinanceBench diagnostic: `56.8% → 50.4%`이므로 최종 승격은 **HOLD**

## 1. 문제 정의

기존 운영 환경:

~~~
AWS L4 24GB
Qwen2.5-14B FP8
max-model-len = 16K
gpu_memory_utilization = 0.90
~~~

에서 VRAM 사용률이 약 **98.1%**까지 올라갔습니다. 동시 요청 증가, 긴 Context, LoRA adapter 추가, KV Cache 증가 시 vLLM restart, API reset, CUDA OOM 위험이 커졌습니다.

## 2. 1차 안정화 — FP8 운영 설정 조정

기존 FP8 / 16K Context / gpu_memory_utilization 0.90에서 FP8 / 8K Context / gpu_memory_utilization 0.85로 변경했습니다.

| 지표 | FP8 16K / 0.90 | FP8 8K / 0.85 | 판정 |
|---|---:|---:|---|
| Startup | 반복 실패 | 성공 | **개선** |
| FULL CUDA Graph | 73% / OOM | 100% | **PASS** |
| API | 불안정/reset | 6/6 HTTP 200 | **PASS** |
| Restart | 계속 증가 | 1 고정 | **PASS** |
| VRAM | ~98.1% | ~95.9% | 개선 |
| Free VRAM | ~444 MiB | 508 MiB | 소폭 개선 |
| Max Context | 16K | 8K | Trade-off |
| Long-request concurrency | 1.84x @16K | 2.20x @8K | 개선 |

## 3. 2차 최적화 — FP8 → AWQ W4A16

~~~
FP8 W8A8
→ AWQ W4A16
→ Model Weight 감소
→ KV Cache 확보
→ Concurrency 증가
→ LoRA Headroom 증가
~~~

AWQ는 4-bit weight quantization이므로 정확도 저하, 복잡한 reasoning 품질 저하, kernel별 latency 변화 가능성이 있습니다. 따라서 성능만으로 승격하지 않고 품질 A/B benchmark를 함께 적용했습니다.

## 4. 메모리 비교

| 지표 | FP8 8K / 0.85 | AWQ 8K / 0.85 | 개선 |
|---|---:|---:|---:|
| Model Load Memory | 16.43 GiB | **10.42 GiB** | **-6.01 GiB** |
| KV Cache | 1.65 GiB | **6.57 GiB** | **약 4배** |
| KV Tokens | 18,032 | **71,760** | **약 4배** |
| 8K Concurrency | 2.20x | **8.76x** | **약 4배** |
| Resident VRAM | 22,084 MiB | **21,018 MiB** | -1,066 MiB |
| Free VRAM | 508 MiB | **1,574 MiB** | **+1,066 MiB** |

## 5. 추론 성능 비교

| 지표 | FP8 | AWQ | 판정 |
|---|---:|---:|---|
| TTFT C1 | 0.338s | — | — |
| E2E C1 | 17.23s | **9.32s** | **AWQ -45.9%** |
| C1 Throughput | 14.86 | **27.46 tok/s** | **AWQ +84.8%** |
| C2 Throughput | 29.94 | **56.34 tok/s** | **AWQ +88.2%** |
| C4 Throughput | 59.39 | **111.16 tok/s** | **AWQ +87.2%** |
| Startup Restart | 1 | **0** | AWQ 우세 |
| 성공률 | 100% | 100% | 동일 |

성능과 처리량은 AWQ가 명확히 우세했습니다.

## 6. Current fair-v2 Internal Quality

| 지표 | FP8 | AWQ | 판정 |
|---|---:|---:|---|
| Quality Pass Rate | **70.0% (35/50)** | 72.0% (36/50) | AWQ +1문항 |
| Relative Quality Delta | 기준 | +2.86% | ≤3% |
| Critical Failures | 1 | 1 | 동일 |
| New Critical Regression | 0 | 0 | **PASS** |
| Request Errors | 0 | 0 | 동일 |
| Financial Arithmetic | 30.0% (3/10) | 20.0% (2/10) | FP8 우세 |
| Risk Reasoning | 100.0% (7/7) | 100.0% (7/7) | 동일 |
| Portfolio / Trading | 83.3% (5/6) | 83.3% (5/6) | 동일 |
| Accounting | 66.7% (4/6) | 66.7% (4/6) | 동일 |
| Quant | 83.3% (5/6) | 100.0% (6/6) | AWQ 우세 |
| Evidence | 83.3% (5/6) | 100.0% (6/6) | AWQ 우세 |
| Structured Output | 40.0% (2/5) | 40.0% (2/5) | 동일 |
| Uncertainty / Fail-Closed | 100.0% (4/4) | 100.0% (4/4) | 동일 |

74%/72% Internal 값은 별도 historical run이며 current fair-v2 7축 표와 섞지 않습니다.

## 7. Current fair-v2 External Quality

| Dataset | FP8 | AWQ | 차이 |
|---|---:|---:|---|
| FinQA | **15/20 = 75.0%** | 13/20 = 65.0% | AWQ -10.0%p |
| TAT-QA | **15/15 = 100.0%** | 14/15 = 93.3% | AWQ -6.7%p |
| FinanceBench diagnostic | 58.74% | 58.95% | AWQ +0.21%p |
| External Overall | N/A | N/A | FinanceBench manual required |
| Auto Mean (FinQA + TAT-QA) | **0.8556** | 0.7709 | FP8 우세 |

Historical FinanceBench manual reference FP8 7/15, AWQ 8/15, Historical Overall 37/50 = 74%는 이전 run의 참고값입니다.

## 8. 전체 7축 비교표

| 지표 | FP8 | AWQ | AWQ+Finetune | AWQ+Reasoning | AWQ+RAG | AWQ+Hybrid | AWQ+Hybrid + Unit/Scale |
|---|---:|---:|---:|---:|---:|---:|---:|
| Quality | 70% | 72% | 76% | 36% | 70% | 82% | **88%** |
| Relative Quality Delta vs FP8 | 기준 | +2.86% | +8.57% | -48.57% | 0.00% | +17.14% | **+25.71%** |
| Critical Failures | 1 | 1 | 1 | 13 | 2 | 1 | 1 |
| New Critical Regression | 0 | 0 | 0 | 12 | 1 | 0 | 0 |
| Request Errors | 0 | 0 | 0 | 0 | 0 | 3 | 3 |
| Financial Arithmetic | 30% | 20% | 40% | 30% | 20% | 60% | **80%** |
| Risk Reasoning | 100% | 100% | 100% | 14.3% | 100% | 100% | 100% |
| Portfolio / Trading | 83.3% | 83.3% | 83.3% | 16.7% | 83.3% | 83.3% | **100%** |
| Accounting | 66.7% | 66.7% | 66.7% | 16.7% | 83.3% | 83.3% | 83.3% |
| Quant | 83.3% | 100% | 100% | 66.7% | 83.3% | 100% | 100% |
| Evidence | 83.3% | 100% | 100% | 66.7% | 83.3% | 100% | 100% |
| Structured Output | 40% | 40% | 40% | 40% | 40% | 40% | 40% |
| Uncertainty / Fail-Closed | 100% | 100% | 100% | 50% | 100% | 100% | 100% |
| External Overall | N/A | N/A | N/A | N/A | N/A | N/A | N/A — manual required |
| FinQA | 75% | 65% | 75% | 55% | 80% | 80% | **85%** |
| TAT-QA | 100% | 93.3% | 80% | 80% | 86.7% | 93.3% | 93.3% |
| FinanceBench diagnostic | 58.7% | 59.0% | 38.5% | 50.1% | **62.1%** | 56.8% | 50.4% |
| FinanceBench auto proxy (diagnostic ≥0.5; not official) | 7/15 | 7/15 | 5/15 | 6/15 | 8/15 | 7/15 | 7/15 |
| Auto Mean | 0.8556 | 0.7709 | 0.7612 | 0.6555 | 0.8310 | 0.8563 | **0.8841** |
| Final Gate | BASELINE | HOLD | HOLD | HOLD | HOLD | HOLD | **HOLD — FinanceBench pending** |

### 공정성 결론

- 6열: Structured fallback 제거
- 7열: 6열 + Unit/Scale normalization
- 두 열은 동일한 controlled Hybrid A/B 실행군
- Structured Output은 fallback 제거로 2/5 = 40% 유지
- TAT-QA, Critical Failures, Request Errors는 유지
- 7열은 Internal, Financial Arithmetic, FinQA, Auto Mean 개선

따라서 **자동 지표 기준 최적 후보는 AWQ+Hybrid + Unit/Scale**입니다. FinanceBench diagnostic이 56.8%에서 50.4%로 하락했으므로 공식 운영 승격은 수동 adjudication 후 결정합니다.

## 9. Hybrid 단계별 A/B 결과

| 지표 | Hybrid Baseline | EXPR + AST | Unit/Scale | Glossary + Schema | FIFO |
|---|---:|---:|---:|---:|---:|
| Internal Quality | 82% | 88% | **88%** | 84% | 86% |
| Critical Failures | 1 | 1 | 1 | 1 | 1 |
| Request Errors | 3 | 4 | **3** | 6 | 6 |
| Financial Arithmetic | 60% | 70% | **80%** | 80% | 80% |
| Risk Reasoning | 100% | 100% | 100% | 100% | 100% |
| Portfolio / Trading | 83.3% | 100% | **100%** | 83.3% | 83.3% |
| Accounting | 83.3% | 100% | 83.3% | 83.3% | **100%** |
| Quant | 100% | 100% | 100% | 83.3% | 83.3% |
| Evidence | 100% | 100% | 100% | 100% | 100% |
| Structured Output | 40% | 40% | 40% | 40% | 40% |
| Uncertainty / Fail-Closed | 100% | 100% | 100% | 100% | 100% |
| FinQA | 80% | 70% | **85%** | 70% | 70% |
| TAT-QA | 93.3% | 93.3% | 93.3% | 93.3% | 93.3% |
| FinanceBench diagnostic | 56.8% | 50.7% | 50.4% | 56.8% | 56.8% |
| Auto Mean | 0.8563 | 0.7984 | **0.8841** | 0.7984 | 0.7984 |
| Verdict | BASELINE | REJECT | **CANDIDATE** | REJECT | REJECT |

## 9-A. 여전히 틀리는 문제와 원인

### Unit/Scale로 해결된 내부 문제

`v2-001` malformed plan, `v2-004` percent conversion, `v2-006` million/billion scale, `v2-010` break-even fee equation, `v2-029` target-weight-to-shares conversion은 Unit/Scale 단계에서 통과했습니다.

### Unit/Scale 이후에도 실패하는 내부 문제

| ID | 영역 | 기대값 | Unit/Scale 출력 | 핵심 원인 |
|---|---|---:|---:|---|
| v2-008 | 금융 산술 | 150 | 1,500 | percentage-point와 ratio scale 혼동 |
| v2-009 | 금융 산술 | 3.6 | 0.036 | ratio와 percentage-point를 반대로 적용 |
| v2-016 | FIFO 손익 | 640,000 | -1,160,000 | FIFO cost basis와 수수료 반영 순서 오류 |
| v2-046 | Structured | `REJECT / stale_snapshot` | `REJECT / snapshot_age_exceeds_limit` | JSON은 유효하지만 의미값 불일치 |
| v2-047 | Structured | `pnl=268,000` | `pnl=2,000` | schema validation만으로 계산 의미를 보장하지 못함 |
| v2-049 | Structured | `RESIZE / 1,500,000` | 값·action 의미 오류 | notional·현재 exposure·action 혼동 |

Structured Output은 결정론적 fallback을 제거했기 때문에 `2/5 = 40%`가 정직한 결과입니다. Guided JSON은 문법을 보장하지만 의미적으로 올바른 값까지 보장하지 않습니다.

### FinanceBench 핵심 실패

| ID | 문제 유형 | 관찰된 실패 | 개선 방향 |
|---|---|---|---|
| 00394 | 근거/segment 선택 | 올바른 사업부·금액 선택 실패 | table row와 질문 segment 정렬 |
| 00222 | 질문 유형 | Yes/No + quick ratio 요구인데 숫자만 출력 | Boolean conclusion과 계산값 동시 생성 |
| 00206 | 회계 용어 적합성 | 금융기관 gross margin 비적합성 판단 실패 | metric applicability glossary 보강 |
| 00606 | 회계연도 매핑 | FY2023과 회사 표기의 FY2022 혼동 | fiscal-year/date mapping 추가 |
| 03473 | ROA 공식/scale | ratio·percentage scale 오류 | 평균자산·출력단위 명시 |
| 05915 | Fixed Asset Turnover | 평균 PP&E 분모 처리 오류 | denominator와 period 검증 |
| 00521 | Fail-closed 과잉 | 근거가 있어도 insufficient evidence 종료 | negative evidence와 no-acquisition 판단 분리 |

`01079` acquisition list와 `01163` cash-flow ranking은 내용상 상당 부분 맞지만 긴 목록·설명 형식 때문에 frozen diagnostic scorer가 낮게 평가하는 사례입니다. 모델 오류와 scorer 민감도를 수동 판정에서 분리해야 합니다.

핵심적으로 Unit/Scale은 산술 단위 문제에는 효과가 있지만 FinanceBench 전체 해결책은 아닙니다. 숫자형 계산에만 Unit/Scale을 유지하고 Boolean·비교·목록·relevance·evidence 문제는 기존 text/glossary 경로를 유지해야 합니다.

### 실제로 문제가 나타나는 방식

#### 1. 산술: AST는 정상 실행하지만 모델이 틀린 식을 생성

AST calculator 자체가 틀린 것이 아니라, LLM이 잘못된 스케일·분모·순서를 포함한 식을 만들고 AST가 그 식을 그대로 계산합니다.

| ID | 모델 출력 | 실제 의미 |
|---|---|---|
| v2-008 | `1500` | 240 billion / 160 billion의 결과를 150%가 아닌 1,500%로 출력 |
| v2-009 | `0.036` | 포트폴리오 비율 계산은 맞지만 요청된 percentage `3.6%`로 변환하지 않음 |
| v2-016 | `-1160000` | FIFO cost basis와 매도 수수료 차감 순서를 잘못 적용 |

즉 안전한 AST는 임의 코드 실행은 막지만, 의미적으로 잘못된 산술식을 자동 교정하지는 않습니다.

#### 2. Structured Output: JSON 문법은 맞지만 값의 의미가 틀림

| ID | 실제 출력 | 왜 실패하는가 |
|---|---|---|
| v2-046 | `{"decision":"REJECT","reason":"snapshot_age_exceeds_limit"}` | enum과 JSON 구조는 맞지만 계약상 reason 값은 `stale_snapshot` |
| v2-047 | `{"pnl":2000,"profitable":true}` | integer/boolean schema는 맞지만 PnL 계산값이 268,000이 아님 |
| v2-049 | `{"action":"APPROVE","max_additional_notional":8400000}` | 필드와 타입은 맞지만 limit - current exposure 계산 및 action 판단이 틀림 |

따라서 guided JSON/Pydantic은 형식 검증 계층이지, 금융 의미·계산 정답 검증 계층이 아닙니다. 정답 하드코딩 fallback을 제거한 현재 2/5 결과가 범용 모델의 실제 성능입니다.

#### 3. FinanceBench: 계산 외의 질문 유형을 계산기로 처리하거나 근거를 잘못 선택

| ID | 실제 출력 형태 | 문제의 본질 |
|---|---|---|
| 00394 | CIB가 아닌 다른 segment를 최고 net income으로 선택 | 표의 segment row와 질문의 대상 불일치 |
| 00222 | `1.6055...` 숫자만 출력 | 질문은 Yes/No와 quick-ratio 근거를 함께 요구 |
| 00206 | insufficient evidence라고 장문 응답 | 금융기관에서 gross margin이 부적합하다는 domain 판단 누락 |
| 00606 | FY2023 자료가 없다고 종료 | 회사 fiscal-year 표기와 질문 연도 매핑 실패 |
| 03473 | `1.424934...` 출력 | ROA 평균자산·percentage scale 해석 오류 |
| 05915 | `4.495610...` 출력 | fixed asset turnover의 평균 PP&E 분모 오류 |
| 00521 | insufficient evidence로 종료 | negative evidence를 no-acquisition 결론으로 연결하지 못함 |

반면 `01079` acquisition list와 `01163` cash-flow ranking은 내용상 상당 부분 맞지만 긴 목록·설명 형식 때문에 자동 diagnostic이 낮게 나오는 사례입니다. 이런 경우는 모델 오류와 scorer의 형식 민감도를 수동 adjudication에서 분리해야 합니다.

## 부록 A. Generic Hybrid 잔여 오류의 실제 문제 지문

아래 지문은 frozen Internal-50 v2와 External-50 v1에서 그대로 추출한 것이다. 정답은 오류 원인을 설명하기 위한 기준값이며, dataset/scorer는 변경하지 않았다.

### Internal-50 v2

#### v2-008 — Debt-to-equity percentage

- 문제: `Calculate debt-to-equity percentage.`
- 지문:
  ```
  A company has:
  - Total debt: KRW 240 billion
  - Equity: KRW 160 billion

  Debt-to-equity (%) = debt / equity * 100.
  ```
- 기준 정답: `150.0`
- Unit/Scale 출력: `1,500`
- 실패 원인: percentage-point와 ratio scale 혼동

#### v2-009 — Portfolio return percentage

- 문제: `Calculate the portfolio return percentage.`
- 지문:
  ```
  A two-asset portfolio has:
  - Asset A: 60% weight, +8% return
  - Asset B: 40% weight, -3% return

  Portfolio return is the weighted average return.
  ```
- 기준 정답: `3.6`
- Unit/Scale 출력: `0.036`
- 실패 원인: 비율 계산 결과를 요청된 percentage-point로 변환하지 않음

#### v2-016 — FIFO realized PnL

- 문제: `Calculate realized PnL in KRW.`
- 지문:
  ```
  FIFO inventory:
  - Lot 1: 50 shares at KRW 40,000
  - Lot 2: 70 shares at KRW 45,000

  The employee sells 80 shares at KRW 50,000.
  Total sell fee is KRW 10,000.

  Realized PnL =
  net sale proceeds minus FIFO cost basis.
  ```
- 기준 정답: `640000`
- Unit/Scale 출력: `-1160000`
- 실패 원인: FIFO cost basis와 매도 수수료 적용 순서 오류

#### v2-046 — Stale snapshot structured decision

- 문제: `Return the requested structured decision.`
- 지문:
  ```
  Risk rule:
  A snapshot older than 5 seconds requires rejection.
  Snapshot age is 9 seconds.

  Return JSON with exactly these keys:
  - decision: "APPROVE" or "REJECT"
  - reason: short snake_case reason
  ```
- 기준 정답: `{"decision": "REJECT", "reason": "stale_snapshot"}`
- Unit/Scale 출력: `{"decision": "REJECT", "reason": "snapshot_age_exceeds_limit"}`
- 실패 원인: JSON 형식은 맞지만 계약상 허용된 의미값과 불일치; critical failure

#### v2-047 — Realized PnL JSON

- 문제: `Return the calculation as JSON.`
- 지문:
  ```
  Trade:
  - Buy 80 shares at KRW 48,000
  - Sell 80 shares at KRW 51,500
  - Total fees KRW 12,000

  Realized PnL = sale proceeds - purchase cost - fees.

  Return JSON with exactly:
  - pnl: integer KRW
  - profitable: boolean
  ```
- 기준 정답: `{"pnl": 268000, "profitable": true}`
- Unit/Scale 출력: `{"pnl": 2000, "profitable": true}`
- 실패 원인: schema는 통과하지만 계산 의미가 틀림

#### v2-049 — Position limit resize

- 문제: `Return the permitted action and maximum additional notional.`
- 지문:
  ```
  Portfolio NAV: KRW 50,000,000
  Soft issuer limit: 25% of NAV
  Current issuer exposure: KRW 11,000,000

  The employee may add exposure only up to the soft limit.

  Return JSON with exactly:
  - action: "APPROVE" or "RESIZE"
  - max_additional_notional: integer KRW
  ```
- 기준 정답: `{"action": "RESIZE", "max_additional_notional": 1500000}`
- Unit/Scale 출력: action과 최대 추가 notional 모두 기준 계약과 불일치
- 실패 원인: soft limit, current exposure, action semantics를 혼동

### External-50 v1 자동 실패 지문

다음은 Generic Hybrid의 External Auto Mean 실패 항목이다. `External Overall`은 FinanceBench 수동 adjudication 전까지 확정하지 않는다.

| ID | 출처 | 실제 문제 지문 | 기준 정답 |
|---|---|---|---|
| `finqa:ETFC/2011/page_144.pdf-2` | FinQA | `as of december 31 , 2010 , what was the ratio of collateral pledged to the bank by its derivatives counterparties to overnight and other short-term borrowings` | `4.6` |
| `finqa:C/2008/page_44.pdf-2` | FinQA | `what was the percentage change in non-interest revenue from 2007 to 2008?` | `2.50515` |
| `finqa:MRO/2003/page_45.pdf-2` | FinQA | `what were total distillates sales in millions for the three year period ? 365 346 345` | `1056.0` |
| `finqa:HWM/2018/page_96.pdf-2` | FinQA | `considering the average exercise price of options , what is the increase in the total value of stock options observed during 2016 and 2017 , in millions of dollars?` | `16.43` |
| `finqa:IP/2009/page_45.pdf-1` | FinQA | `what percentage of contractual obligations for future payments under existing debt and lease commitments and purchase obligations at december 31 , 2009 due in 2011 are maturities of long-term debt?` | `0.40796` |
| `finqa:ILMN/2008/page_86.pdf-4` | FinQA | `what was the change in millions of company contributions to the employee benefit plans retirement plan between 2007 and 2008?` | `1.2` |
| `tatqa:0f032004-ec01-40a0-831b-aac3f7e1b5c7` | TAT-QA | `How often does the company review the actuarial assumptions which the periodic benefit cost and the actuarial present value of projected benefit obligations are based on?` | `Annual basis` |
| `tatqa:9c364cfe-84e4-479d-b3ca-dab2b412e8c4` | TAT-QA | `What is the average financing costs between 2018 and 2019?` | `-1581 million` |

이 부록의 문제 지문과 기준값은 분석용으로만 기록한다. 새 정답 규칙이나 case-ID 기반 fallback을 추가하지 않는다.
## 10. 성능까지 포함한 최종 종합

| 평가축 | FP8 | AWQ | 판정 |
|---|---:|---:|---|
| Model Load Memory | 16.43 GiB | **10.42 GiB** | AWQ |
| KV Cache | 1.65 GiB | **6.57 GiB** | AWQ 약 4배 |
| 8K Concurrency | 2.20x | **8.76x** | AWQ 약 4배 |
| Free VRAM | 508 MiB | **1,574 MiB** | AWQ |
| C1 Throughput | 14.86 | **27.46 tok/s** | AWQ +84.8% |
| C2 Throughput | 29.94 | **56.34 tok/s** | AWQ +88.2% |
| C4 Throughput | 59.39 | **111.16 tok/s** | AWQ +87.2% |
| C1 E2E | 17.23s | **9.32s** | AWQ -45.9% |
| Startup Restart | 1 | **0** | AWQ 우세 |

7열 Hybrid의 별도 throughput/E2E는 이 quality A/B run에서 재측정하지 않았으므로 AWQ model-only 성능과 섞지 않습니다.

## 11. 최종 평가

~~~
Internal Quality
82% → 88%

Financial Arithmetic
60% → 80%

FinQA
80% → 85%

TAT-QA
93.3% → 93.3%

Auto Mean
0.8563 → 0.8841

Critical Failures
1 → 1

Request Errors
3 → 3
~~~

따라서 최종 결론은:

> **AWQ+Hybrid + Unit/Scale은 현재 자동 평가상 최적 candidate이다.**

단, FinanceBench diagnostic 하락 때문에 최종 운영 승격은 아직 HOLD입니다. FinanceBench 15개 수동 adjudication에서 실제 정답 보존이 확인되면 승격을 검토합니다.

## 12. 검증 및 산출물

- Internal-50 v2 SHA256: ad2bdaf5ea381c2fc151fce1f1859f7f925b86fd03b830319cd97af17709e978
- Frozen dataset hash: PASS
- Benchmark tests: Ran 16 tests ... OK
- git diff --check: PASS
- 7축 표: aws_l4_fp8kv_v1_7axis_comparison.md
- 전체 보고서: aws_l4_fp8kv_v1_7axis_final_report.md
