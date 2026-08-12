# AI QA/감사본부 — 최종 감사 보고서

| 항목 | 값 |
|---|---|
| **감사 대상** | AAPL 100주 매수 전략 (Volatility 관점 검증) |
| **감사 유형** | Evidence QA + Model Risk (재현성) |
| **근거 프레임워크** | TEAM_DONGGYU_RISK_QA_GUIDE.md §7.1 (8단계 Evidence QA 검사순서)·§7.2 (Strategy/Model Release 검증) |
| **판정 엔진** | departments/06-ai-qa-audit/evidence/evidence_qa_engine.py (결정론적, LLM 호출 없음) |
| **작성일** | 2026-07-31 |
| **작성/총괄** | 동규 (AI QA/감사부서장) |
| **검토 대상 부서** | 리서치본부, 퀀트/백테스트본부, 리스크본부 |

---

## 1. Executive Summary

본 보고서는 리서치본부와 퀀트/백테스트본부가 산출한 **AAPL 100주 매수 전략**에 대하여 변동성 측면에서의 근거 무결성과 재현성을 독립 검증하기 위해 AI QA/감사본부에서 수행한 최종 감사 결과를 정리한 것이다.

| 항목 | 결과 |
|---|---|
| **종합 판정** | **WARN** (조건부 승인 대기) |
| **핵심 이슈** | 변동성 1.2% 주장의 출처 미비 · 최신 시점 데이터 미확인 |
| **Finding** | 2건 (MEDIUM: 출처 미비, MEDIUM: 데이터 시점 미확인) |
| **후속 조치** | 블로크 없음 (실시간 Hot Path 대상 아님). Strategy Release 승격 전 **재검증 필수**. |

> **판정 근거**: 팀 가이드 7.1 §8 "Material Unsupported Claim이면 Block"에 따라, AAPL 100주 매수라는 **Forecast/Recommendation** 주장은 Fact가 아니므로 자동 Block 대상에서 제외된다. 그러나 출처와 시점 검증에 실패한 주장은 `CONTRADICTION`이 아니라 `UNSUPPORTED`로 분류되며, `qa-check` 결과는 `WARN`으로 집계된다(§4 참조).

---

## 2. 감사 범위 및 방법

### 2.1 검증 대상
- **Claim**: "AAPL에 대해 100주를 매수 하는 것은 변동성이 1.2%로 허용 범위 내에 있다."
- **Artifact**: 리서치본부 Research Packet (AAPL 변동성 분석 조각)
- **출처 문서**: Risk 보고서 (AAPL 언급, 구체적 기관 미기재)
- **판정 시점 (decision_time)**: 2026-07-31 (본 감사 기준)

### 2.2 검증 방법
1. **8단계 Evidence QA 검사** (TEAM_DONGGYU_RISK_QA_GUIDE.md §7.1)를 적용하여 각 Claim을 검증
2. **재현성 검증** (§7.2): 변동성 1.2% 수치의 계산 과정과 데이터 출처를 추적
3. **Tool Trace 검토**: Risk Engine이 실제로 사용한 변동성 데이터 소스 확인
4. **PII/보안 검토**: 민감 정보 누출 여부 (해당 없음)

### 2.3 검증 제외 사항
- 변동성 1.2% 수치의 수학적 정확성 (Risk Engine의 결정론적 계산 결과이므로 QA가 재계산하지 않음)
- 실시간 주문 생성 능력 (Strategy Release 단계이므로 OMS 제출 전 단계)

---

## 3. 세부 검증 결과

### 3.1 내용물 검증 (Content Verification)

| # | 검사 항목 | 결과 | 상세 |
|---|---|---|---|
| C1 | 주장 식별 | ✅ 식별됨 | "AAPL 100주 매수 → 변동성 1.2%" |
| C2 | 주장 종류 | ⚠️ Forecast/Recommendation | 투자 제안이므로 Fact가 아님 (팀 가이드 7.1 §5) |
| C3 | 인용 근거 | ❌ 부족 | Risk 보고서만 언급, 특정 기관/전문가 미기재 |
| C4 | 수치 인용 | ⚠️ 불확인 | 1.2% 수치의 출처와 계산 방법 미공개 |

> **판정**: 주장은 명료하지만, **근거의 출처가 불분명**하여 독립 검증이 어렵다.

### 3.2 무결성 검증 (Integrity Verification)

| # | 검사 항목 | 결과 | 상세 |
|---|---|---|---|
| I1 | 주장 일관성 | ⚠️ 확인 불가 | 동일 기준/관점 적용 여부 알 수 없음 |
| I2 | 데이터 시점 (PIT) | ❌ 미확인 | 변동성 1.2%가 최근 데이터 기반인지 확인 어려움 |
| I3 | 계산 재현성 | ❌ 불가 | input_hash/calculation_version 미기재로 재현 불가 |

> **판정**: **최신 데이터 사용 여부와 계산 과정의 재현성 확인 불가**로 인해 감정적 신뢰도가 낮다.

### 3.3 결과 종합 (Aggregate)

| ClaimCheckResult | 건수 | 비고 |
|---|---|---|
| SUPPORTED | 0 | — |
| PARTIAL | 0 | — |
| UNSUPPORTED | 1 | 변동성 1.2% 주장 → 출처 미비 (evidence_not_found) |
| CONTRADICTED | 0 | — |
| NOT_APPLICABLE | 1 | Forecast/Recommendation → 근거 인용 대상 아님 |

**종합 QaDecisionValue**: `WARN` (UNSUPPORTED Claim 존재)

---

## 4. Finding 목록

### Finding #1: 변동성 1.2% 주장의 출처 미비

| 필드 | 값 |
|---|---|
| **Finding ID** | `FINDING-QA-20260731-001` |
| **유형** | `unsupported_claim` |
| **심각도** | MEDIUM |
| **Owner** | 리서치본부 (Research Dossier 작성자) |
| **Due Date** | 2026-08-04 |
| **상태** | OPEN |
| **설명** | "AAPL 변동성 1.2%" 주장은 Risk 보고서에서만 인용되며, 구체적인 기관명, 보고서 제목, 발표일자가 명시되지 않았다. 팀 가이드 7.1 §2 (Evidence ID 존재와 접근 권한) 및 §3 (published_at/observed_at <= decision_time)를 위반한다. |
| **재현성** | `checker_version`: qa-evidence-p0-v1, `input_hash`: N/A (근거 없음) |
| **조치 블록** | 출처 문서의 `evidence_id`, `published_at`, `observed_at`을 명시하고, 접근 권한을 확인할 때까지 Strategy Release 승격 보류 |

### Finding #2: 변동성 데이터의 시점 및 계산 방법 미공개

| 필드 | 값 |
|---|---|
| **Finding ID** | `FINDING-QA-20260731-002` |
| **유형** | `partial_evidence_set` |
| **심각도** | MEDIUM |
| **Owner** | 퀀트/백테스트본부 (변동성 계산 모델 담당자) |
| **Due Date** | 2026-08-04 |
| **상태** | OPEN |
| **설명** | 변동성 1.2% 수치의 계산에 사용된 데이터 기간, 윈도우(100주), 모델(예: EWMA, GARCH) 및 `calculation_version`/`input_hash`가 명시되지 않았다. 팀 가이드 7.2 §1-§2 (Dataset Manifest와 Hash, Code/Container/Dependency Version) 위반. |
| **재현성** | `input_hash` 불가 (계산 입력 미공개) |
| **조치 블록** | 계산에 사용된 데이터 기간, 모델 버전, 코드 커밋 해시를 제출하고 `QaAssessment.input_hash`로 검증 통과할 때까지 재검증 대기 |

---

## 5. 재현성 평가 (Reproducibility Assessment)

| 항목 | 평가 | 상세 |
|---|---|---|
| 계산 가능성 | ⚠️ 제한적 | 1.2% 수치는 인용되었으나, 출처와 방법론이 불명확하여 다른 이가 동일한 실험을 재현하기 어렵다. |
| 논리적 허점 | ❌ 존재 | 변동성과 "100주 매수"의 연관성 설명이 미흡하다. 변동성이 허용 범위 내라는 기준 자체가 무엇인지, 누가 정의했는지 명시되지 않았다. |
| 권장 조치 | 조사 · 정제 | (1) 변동성 계산 데이터 출처와 시점 명시 (2) Risk 한도 기준 마련 (3) `QaAssessment.input_hash` 기반 재현성 검증 도입 |

> **참고**: 팀 가이드 7.2 "Strategy/Model Release" 검증 순서에 따라, Strategy Bundle은 Dataset Manifest·Hash, Code 버전, PIT 검증, Champion 대비 Regression, Stress 시나리오, Risk 승인 및 권한 분리 검증을 모두 통과해야 합니다. 현재 AAPL 100주 전략은 단계 1~3을 충족하지 못함.

---

## 6. 결론 및 권고

### 6.1 결론
AAPL 100주 매수 전략의 핵심 주장(변동성 1.2%)은 **근거의 출처가 불분명**하고 **계산 방법이 재현 불가**한 상태이다. 현재 제공된 정보로는 출처의 타당성을 평가할 수 없으며, 최신 데이터 사용 여부도 확인할 수 없다. 따라서 이 전략을 **Strategy Release Candidate**로 승급시키기에는 불충분한 증거가 제공되지 않았다.

### 6.2 권고 (CEO / 퀀트본부 / 리서치본부)
1. **즉시 (Due: 2026-08-04)**: Finding #1, #2의 조치 블록 조건 충족 — 출처 문서 메타데이터와 변동성 계산 방법론 제출
2. **재검증**: 위 증거가 제출된 후 `qa-department chat -q 'Re-validate AAPL 100wk strategy volatility claim with cited evidence'`로 **재감사** 수행
3. **프로세스 개선**: 향후 모든 Strategy Release 후보는 `RISK_QA_DOMAIN_API_SPEC.md` §3.1 `POST /investment-cases/{case_id}/qa-check`를 통해 자동 검증하도록 통합. `QaAssessment.decision == WARN` 또는 `FAIL`인 경우 `strategy_research_cycle`에서 **자동 REJECT_PROMOTION**

### 6.3 운영적 영향
| Impact | 설명 |
|---|---|
| **실시간 Hot Path** | 없음 (AAPL 100주 전략은 `strategy_research_cycle` 단계, 실시간 주문 생성과 무관) |
| **Strategy Registry** | AAPL 100주 전략은 **Champion 승격 불가**. Shadow/Paper 배포 후보로 제출 불가. |
| **재현성** | `QaAssessment.input_hash`가 일치하지 않으므로, 동일 입력으로 재감사를 수행해도 WARN 결과가 재현됨 |

---

## 7. 부록: 검증 추적 (Traceability)

| 검증 항목 | 매핑 |
|---|---|
| Claim: "AAPL 변동성 1.2%" | evidence-qa-agent Claim #0, `kind=forecast` |
| Evidence: Risk 보고서 | `evidence_id`: 미지정 (Finding #1 조치 대상) |
| Tool: Risk Engine | `tool_source`: risk-management · `output_values`: 미제공 |
| PIT: decision_time = 2026-07-31 | 팀 가이드 7.1 §3 (published_at/observed_at <= decision_time) |
| 재현성 | `QaAssessment.calculation_version=qa-evidence-p0-v1`, `input_hash=N/A` |

---

> **본 보고서는 AI QA/감사본부의 독립 검증 결과이며, QA 본부는 작성 부서의 Finding을 수정·종료할 수 없습니다.**
> Finding #1, #2는 각각 리서치본부와 퀀트/백테스트본부의 책임자가 증거를 제출하고, QA가 재검증한 후에만 종료됩니다(팀 가이드 5.10.3 및 CLAUDE.md "절대 깨면 안 되는 권한 분리" 참조).
> 문의: qa-department (Dong-gyu, Risk/QA Domain Owner)
