# 조건주문 정확도 측정 보고서

- 측정일: 2026-08-31 UTC
- 대상: 자연어 조건주문 → AST/조건규칙 → 감시·PAPER 주문 경계
- 환경: 로컬 격리 테스트, 외부 Broker·LIVE 주문 없음
- 평가 방식: 저장소의 정본 pytest 골든 계약 및 결정론 E2E

## 실행 결과

실행 명령:

```bash
.venv/bin/pytest -q \
  tests/orchestration/test_user_order_language.py \
  tests/orchestration/test_compound_paper_orders.py \
  tests/api/test_conditional_rules.py \
  tests/conditional_rules/test_contracts_and_evaluator.py \
  tests/conditional_rules/test_worker.py \
  tests/api/test_conditional_rule_orchestrator.py \
  tests/e2e/test_core_conditional_evolution_pipeline.py
```

결과: **318 passed in 1.63s**

| 평가 층위 | 포함 테스트 | 통과 | 결과 |
|---|---:|---:|---:|
| 자연어 → 구조화 주문 후보/조건 계획 컴파일 | 188 + 12 | 200/200 | 100% |
| 조건주문 진입·미리보기·의미 경계 | 55 | 55/55 | 100% |
| AST → 의미검증·조건감시·PAPER 주문·상태조회 | 30 + 21 + 10 + 2 | 63/63 | 100% |
| 전체 핵심 조건주문 계약 | - | 318/318 | 100% |

## 검증된 범위

- 종목·수량·매수/매도·시장가/지정가·가격·바스켓 필드 보존
- 모호하거나 위험한 문장 fail-closed 처리
- 조건 AST의 단위·지표·시간프레임·수량 관련 의미 검증
- 조건 미충족, 시장 휴장, stale/불충분 데이터, 중복 트리거 처리
- 조건 충족 시 결정론적 가드 재검증 및 단일 PAPER 제출
- 재시작 복구, 멱등성, OCO 경쟁, 권한 확인, 사용자 상태 조회
- 사용자 문장이 CEO/Trading 작업 범위와 PAPER 상태까지 도달하는 E2E

## 실제 Discord 조건주문 canary

- 취소 문구까지 한 문장에 포함한 요청: `CANCELLATION_REQUEST_UNSUPPORTED`로 안전하게 거절
- 생성 요청만 포함한 요청: PAPER 조건규칙 `ACTIVE` 전환 후 정확한 rule ID로 취소
- 실제 LIVE 주문·체결·원장 반영: 없음

## 해석과 한계

현재 결과는 **구현된 골든 계약에 대한 통과율 100%**다. 임의의 사용자 자연어 전체에 대한 일반화 정확도 100%를 의미하지는 않는다. 아직 별도의 외부 사용자 문장 세트, field-level confusion matrix, 표본별 confidence interval이 없다.

또한 전략 투자 성과(CAGR, Sharpe, MDD, 비용·슬리피지, OOS, 시장 국면별 성능)는 이 정확도 측정에 포함되지 않았다. 전략·기간·벤치마크·비용 모델을 고정한 별도 Quant 백테스트가 필요하다.

## 판정

현 시점에서 1·2번 경로의 즉시 코드 개선을 요구할 실패 증거는 없다. 다음 개선 우선순위는 파서 로직을 임의로 바꾸는 것이 아니라, 실제 사용자 문장과 adversarial 문장을 고정한 독립 평가 세트를 추가해 일반화 정확도를 측정하는 것이다. 3번 전략 성과는 별도 측정 전까지 “검증되지 않음”으로 표시한다.
