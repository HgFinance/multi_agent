# AWS L4 FP8 KV v1 — Historical Controlled Comparison Record

이 문서는 과거 controlled Hybrid A/B 실행에서 생성된 비교표를 보존하기 위한 기록이다.

`AWQ+Hybrid`는 외부 성능 보존을 위한 historical baseline으로 사용한다. 이후 개선 실험은 이
파이프라인을 기준으로 수행하되, 기존 표의 단일 실행값을 새 측정값과 섞지 않는다.

## 측정 용어 고정: 교차 측정

이 문서의 FP8, AWQ, AWQ+Finetune, AWQ+Reasoning, AWQ+RAG 및 Hybrid 계열 표는 **교차 측정
결과**로 부른다. 동일한 frozen dataset/scorer와 runtime protocol을 기준으로 변형별 결과를
비교한 기록이며, 모든 변형을 하나의 동일 시점에 동시에 실행한 단일 run을 뜻하지 않는다.
Historical single-run, paired replay, 후속 재실행 값은 각 출처를 유지하며 서로 평균내거나
대체하지 않는다.

대괄호로 표시한 값은 변형 전체의 aggregate score다. 아래 문항 목록은 이 표에 대응하는 보존된
per-case raw artifact에서 **AWQ는 실패하고 해당 후보가 통과한 문항**만 추린 것이다. 따라서
문항 목록이 없는 aggregate는 정답 문항을 임의로 추정하지 않는다.

### 대괄호 지표에서 AWQ보다 통과한 문항

#### AWQ+Finetune — Financial Arithmetic 40% (AWQ 20%)

| ID | 문제 | 기대값 | AWQ → Finetune |
|---|---|---:|---|
| `v2-001` | 매출 2,500 million KRW, 매출원가 1,750 million KRW일 때 gross margin percentage 계산 | 30.0% | 45 → 30% |
| `v2-006` | 순이익 36 billion KRW와 가중평균 발행주식 120 million주로 EPS를 KRW/주 단위 계산 | 300.0 | 300,000 → 300 |

#### AWQ+RAG — Accounting 83.3% (AWQ 66.7%)

| ID | 문제 | 기대값 | AWQ → AWQ+RAG |
|---|---|---:|---|
| `v2-014` | 100주에 대한 주당 배당금 500 KRW에서 원천징수세 7,700 KRW를 차감한 순배당 현금 수취액 계산 | 42,300 KRW | 4,230 → 42,300 |

#### AWQ+Finetune — FinQA 75% (AWQ 65%)

| ID | 문제 | 기대값 | AWQ → AWQ+Finetune |
|---|---|---:|---|
| `finqa:C/2008/page_44.pdf-2` | 2007년 대비 2008년 non-interest revenue의 percentage change 계산 | 2.50515 | -250.69% → 251% |
| `finqa:MRO/2003/page_45.pdf-2` | 365, 346, 345로 주어진 3개년 distillates sales의 합계를 million 단위로 계산 | 1,056.0 | 연도별 잘못된 환산값 → 1,056 |

#### AWQ+RAG — FinQA 80% (AWQ 65%)

| ID | 문제 | 기대값 | AWQ → AWQ+RAG |
|---|---|---:|---|
| `finqa:C/2008/page_44.pdf-2` | 2007년 대비 2008년 non-interest revenue의 percentage change 계산 | 2.50515 | -250.69% → 약 250.52% |
| `finqa:HWM/2018/page_96.pdf-2` | 2016년과 2017년 stock options의 평균행사가격을 반영한 total value 증가액 계산 | 16.43 million USD | 17.43 → 16.43 |
| `finqa:IP/2009/page_45.pdf-1` | 2011년 만기의 전체 contractual obligations 중 long-term debt maturity 비율 계산 | 0.40796 (약 40.8%) | 1.99% → 40.8% |

#### AWQ+RAG — FinanceBench diagnostic 62.1% (AWQ 59.0%)

FinanceBench는 frozen scorer가 `manual_required`로 정의되어 있어 아래는 공식 정답 통과가
아니라 diagnostic score가 상승한 사례다.

| ID | 문제 | AWQ → AWQ+RAG diagnostic |
|---|---|---:|
| `financebench_id_00394` | 2022년 2분기에 JPM의 어느 사업 부문이 가장 높은 순이익을 기록했는가? | 0.0923 → 1.0000 |

FinanceBench의 다른 문항은 이 교차 측정에서 diagnostic score가 상승하지 않았거나 하락했으므로,
RAG가 FinanceBench 전체를 해결했다고 해석하지 않는다. 위 문항별 근거는 Git의 해당 historical
raw artifact와 함께 보존한다.

## Historical comparison table

| 지표 | FP8 | AWQ | AWQ+Finetune | AWQ+Reasoning | AWQ+RAG | AWQ+Hybrid | AWQ+Hybrid + Unit/Scale |
|---|---:|---:|---:|---:|---:|---:|---:|
| Quality | 70% | 72% | 76% | 36% | 70% | 82% | **88%** |
| Relative Quality Delta vs FP8 | 기준 | +2.86% | +8.57% | -48.57% | 0.00% | +17.14% | **+25.71%** |
| Financial Arithmetic | 30% | 20% | 40% | 30% | 20% | 60% | **80%** |
| Risk Reasoning | 100% | 100% | 100% | 14.3% | 100% | 100% | 100% |
| Portfolio / Trading | 83.3% | 83.3% | 83.3% | 16.7% | 83.3% | 83.3% | **100%** |
| Accounting | 66.7% | 66.7% | 66.7% | 16.7% | 83.3% | 83.3% | 83.3% |
| Quant | 83.3% | 100% | 100% | 66.7% | 83.3% | 100% | 100% |
| Evidence | 83.3% | 100% | 100% | 66.7% | 83.3% | 100% | 100% |
| Structured Output | 40% | 40% | 40% | 40% | 40% | 40% | 40% |
| Uncertainty / Fail-Closed | 100% | 100% | 100% | 50% | 100% | 100% | 100% |
| FinQA | 75% | 65% | 75% | 55% | 80% | 80% | **85%** |
| TAT-QA | 100% | 93.3% | 80% | 80% | 86.7% | 93.3% | 93.3% |
| FinanceBench diagnostic | 58.7% | 59.0% | 38.5% | 50.1% | **62.1%** | **61.97% ± 6.52%**¹ | 50.4%² |
| FinanceBench auto proxy (diagnostic ≥0.5; not official) | 7/15 | 7/15 | 5/15 | 6/15 | 8/15 | 7/15 | 7/15 |
| Auto Mean | 0.8556 | 0.7709 | 0.7612 | 0.6555 | 0.8310 | 0.8563 | **0.8841** |
| Final Gate | BASELINE | HOLD | HOLD | HOLD | HOLD | HOLD | HOLD — FinanceBench pending |

¹ `AWQ+Hybrid`의 값은 동일 old runner를 현재 L4 endpoint에서 3회 재현한 baseline 평균이다.
개별 diagnostic 값은 **58.16%, 58.25%, 69.49%**, 평균은 **61.97% ± 6.52%**(표본 표준편차)였다.

² `AWQ+Hybrid + Unit/Scale`의 historical 단일 실행값이며, 이번 3회 baseline replay의 값이 아니다.

따라서 이후 개선 실험의 `AWQ+Hybrid` baseline은 **FinanceBench diagnostic 61.97% ± 6.52%**를
기준으로 삼는다. FinanceBench diagnostic은 공식
External Overall 점수가 아니며, 15개 사례 수동 adjudication이 필요하다.

## Baseline interpretation

The historical `AWQ+Hybrid` column is the baseline target for subsequent optimization. Its current
three-run replay under the same old runner produced:

| Metric | Three-run baseline |
|---|---:|
| Internal Quality | 82.0% |
| Critical Failures | 1.0 |
| Request Errors | 3.0 |
| Financial Arithmetic | 60.0% |
| Structured Output | 40.0% |
| FinQA | 80.0% |
| TAT-QA | 93.3% |
| FinanceBench diagnostic | 61.97% ± 6.52% |
| Auto Mean | 0.8563 |

The historical `AWQ+Hybrid + Unit/Scale` values `88% / 85% / 0.8841` remain historical single-run
values. They are not replaced by, or averaged with, the three-run baseline replay. FinanceBench is
diagnostic-only and still requires manual adjudication for an official result.

## 후속 AWQ Arithmetic LoRA adapter-only 재현

`2026-08-25`에 `hgfinance-awq-arithmetic-2epoch`만 적용하고 RAG, 외부 Reasoning,
계산기, 단위 정규화, Guided JSON 및 Hybrid routing을 제외한 별도 실행을 기록했다.

| 지표 | AWQ+Arithmetic LoRA adapter-only |
|---|---:|
| Internal Quality | 74.0% (37/50) |
| Critical Failures | 2 |
| Request Errors | 0 |
| Financial Arithmetic | 30.0% (3/10) |
| Structured Output | 40.0% (2/5) |
| FinQA | 75.0% (15/20) |
| TAT-QA | 80.0% (12/15) |
| FinanceBench diagnostic | 39.82% |
| Auto Mean | 0.7612 |

이 값은 `AWQ+Finetune` 열의 혼합 SFT adapter 및 `AWQ+Hybrid` 계열과 별도다. 현재
endpoint가 `max_model_len=4096`을 보고해 historical `8192` runtime 열을 덮어쓰거나 직접
평균내지 않는다. 원시 응답, scorer 출력, 데이터셋 해시와 runtime 차이는
`benchmarks/quantization/results/20260825_awq_arithmetic_lora_adapter_only/`에 보존한다.

## Frozen comparison rules

- Keep `AWQ+Hybrid` as the baseline pipeline definition.
- Add each improvement as a separate candidate column.
- Use the same L4 runtime, base model, adapter, frozen datasets, scorer, request order, and decoding settings.
- Run at least three paired repetitions for each candidate.
- Do not overwrite this historical record or the baseline artifacts.
- Reject a candidate if External Auto Mean, FinQA, TAT-QA, Critical Failures, or Request Errors regress beyond the agreed gate.
