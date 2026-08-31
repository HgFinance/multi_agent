# 핵심 조건주문·자가개선 E2E 예시

이 문서는 발표 PDF에 넣을 수 있는 검증 가능한 예시다. 테스트는 실제 운영
registry나 브로커에 쓰지 않고 임시 저장소와 결정론적 외부 adapter에서 실행한다.
이제 E2E는 사용자 원문을 `ceo.ceo_query`로 접수하는 지점부터 시작해 조건주문
orchestrator, 기존 `ConditionalRuleWorker`, PAPER status reader까지 연결한다.
PAPER 주문의 실제 OMS·Broker·Fill·Ledger 왕복은 기존
`tests/e2e/test_selected_strategy_trace.py` 및 `tests/e2e/test_paper_loop.py`가
담당하므로 이 테스트에 두 번째 PAPER executor를 만들지 않는다.

## 실행 명령

```bash
.venv/bin/pytest -q tests/e2e/test_core_conditional_evolution_pipeline.py
```

기대 결과:

```text
2 passed
```

## 발표용 파이프라인 설명

```text
사용자 조건
  "삼성전자 현재가가 100원을 초과하면 2주 시장가 매수해"
        |
        v
CEO 사용자 쿼리 접수
  원문·SHA-256 / Fund·Book·User scope / CEO·Trading task binding
        |
        v
Hermes AST → 조건주문 orchestrator
  ConditionalRuleSpec / PAPER only / rule ACTIVE / idempotent admission
        |
        v
기존 ConditionalRuleWorker
  99원 -> False, 105원 -> True
  rule version, 만료, 장 상태, 데이터 신선도, 현금, lot size, 중복 trigger 재검증
  -> READY_FOR_PAPER_DIRECTIVE / quantity=2
        |
        v
PAPER 제출·상태 조회
  기존 worker submit → PAPER directive receipt → authority-checked status
  (외부 market/Trading은 격리 fake adapter, 실제 OMS·fill·원장은 별도 E2E)
        |
        v
QA 관찰·근거화
  서로 다른 3개 실행의 구조화된 QA benchmark evidence
        |
        v
자가개선 후보
  동일 문제 + 3개 distinct run -> Candidate
        |
        v
제안 생성
  governed 14B model -> SKILL.md + provenance + diff
        |
        v
결정론적 검증·승인
  구조/해시/권한 검증 -> QA PASS + named approver
        |
        v
격리 registry 활성화
  canonical regression PASS -> ACTIVE
  이후 3개 post-activation 성공 실행 전에는 VERIFIED_IMPROVED로 선언하지 않음
```

## 이번 E2E가 증명하는 것

- 조건이 거짓일 때 주문하지 않고, 참일 때 PAPER 실행 준비 상태가 된다.
- 수량은 사용자 확인 조건과 현재 가격·현금·lot size로 결정론적으로 계산된다.
- 단일 로그나 한 번의 실행만으로 Skill 후보를 만들지 않는다.
- 세 개의 독립 실행 근거가 있어야 후보가 생성된다.
- 제안은 정해진 14B 모델과 고정된 구조 검증을 통과해야 한다.
- QA PASS와 이름 있는 승인자 없이는 활성화되지 않는다.
- 제안·diff·canonical source가 변조되면 활성화가 차단된다.
- 테스트 활성화는 임시 registry에만 일어나며 운영 Skill과 실제 주문에는 영향을 주지 않는다.

## 발표 슬라이드용 한 문장

> HgFinance는 조건주문을 자연어 판단으로 바로 실행하지 않는다. 조건을 버전 있는
> 계약으로 고정하고, PAPER 마지막 가드와 체결·원장을 통과시킨 뒤, 반복된 QA 근거만
> 자가개선 후보로 승격한다. 제안은 14B 생성, 결정론적 검증, QA 승인, 해시 검증을
> 모두 통과해야 격리된 canonical Skill로 활성화되며, 운영 자동 변경은 허용하지 않는다.

## 범위와 한계

이 E2E는 사용자 쿼리부터 조건주문 PAPER 제출 결과까지의 연결과 자가개선
lifecycle을 증명하는 재현 가능한 통합 테스트다. 실제 AWS·시장 데이터·브로커·
Discord·Notion·LangSmith 전송의 지속 운영 성공이나 stress p95/p99를 증명하지
않는다. Evolution `ACTIVE`도 격리 canonical registry에서만 만들며, 운영
정본은 실제 QA/승인 근거 없이 변경하지 않는다. 세 post-activation 피드백 전에는
`ACTIVE_PENDING_FEEDBACK`으로 기록한다.
