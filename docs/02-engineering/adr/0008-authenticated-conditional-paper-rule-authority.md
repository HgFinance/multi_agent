# ADR-0008: 로컬 fixture 조건부 PAPER 규칙을 별도 standing authority로 둔다

- 상태: Accepted
- 날짜: 2026-08-20
- 영향 영역: Operator BFF, Conditional Rule Worker, Market Read API, Trading
- 관련 문서: [ADR-0007](0007-authenticated-user-paper-directive-authority.md)

## 배경

“삼성전자 RSI 70 이상이면 2주 매도”와 “평균 매입가 대비 5% 상승하면 보유수량의
20% 매도”는 즉시 주문이 아니다. 사용자는 미래의 결정론적 조건과 실행 동작을 함께
미리 승인한다. 이를 일반 자동 전략으로 취급하면 사용자의 확정 지시를 alpha/Risk가
다시 결정하고, 반대로 Hermes가 계속 조건을 감시하게 하면 LLM이 시세 계산·trigger
판정·주문 권한을 동시에 갖게 된다.

## 결정

### 1. 세 권한 레인을 구분한다

| 레인 | 권한 출처 | 실행 전 필수 조건 |
|---|---|---|
| `AUTOMATED_STRATEGY` | alpha/전략 Worker | 기존 Risk Decision과 OMS gate |
| `USER_DIRECTIVE` | 고정 fixture 사용자의 즉시 PAPER 지시 | ADR-0007의 기계적 admission |
| `USER_CONDITIONAL_RULE` | 고정 fixture 사용자가 정확한 규칙 지문을 확인한 standing PAPER 지시 | 결정론 평가, exactly-once trigger, 실행 직전 admission |

조건부 규칙은 자동 alpha 승격 권한이 아니며, 자동 전략이 이 테이블에 규칙을 만들어
Risk gate를 우회할 수 없다. 반대로 research/quant factory의 무한 탐색 작업은 조건부
사용자 질의·평가 워커와 DB role, 큐, 프로세스를 공유하지 않는다.

### 2. LLM은 AST 제안까지만 한다

Hermes는 자연어를 versioned JSON AST 후보로 바꿀 수 있다. BFF는 고정 fixture ID,
Fund/Book membership, canonical instrument, 원문 hash, schema/semantic/unit
검사를 독립적으로 수행한다. 사용자는 정규화된 종목·조건·봉 주기·수량·만료·PAPER
표시를 보고 그 exact `spec_sha256`을 확인해야 한다. 확인 이후 AST나 action이 한
비트라도 달라지면 재확인이 필요하다.

모호한 기준은 추정하지 않는다. 특히 “비중 20% 매도”는 보유수량의 20%인지 목표
포트폴리오 비중인지 확인하고, 수익률은 평균 매입가 기준이라는 표현이 있어야 하며,
분봉 지표는 원문에 주기가 있어야 한다. 일봉만 v1 기본 가정으로 명시해 preview에
노출한다.

### 3. 계산과 trigger는 deterministic code가 소유한다

- 지원 노드는 market/portfolio/indicator/literal, arithmetic, comparison, cross,
  AND/OR/NOT이다.
- SMA, EMA, RSI, MACD, Bollinger Bands, volume average, ATR, ADX는 `Decimal` 기반
  Indicator Engine이 final candle만 사용해 계산한다.
- cross는 이전과 현재의 완료 관측치가 모두 있어야 하며, v1에서 과거 portfolio
  snapshot이 없는 portfolio cross는 거부한다.
- 같은 `rule_id + version + bar/quote evaluation_key`는 DB unique constraint로 한
  번만 평가되고, 같은 평가에서 trigger와 execution은 한 번만 만들어진다.
- 워커가 중간에 종료되면 `CLAIMED`, `PENDING`, `SUBMITTING` 상태를 다시 읽고 같은
  idempotency key로 재개한다.

### 4. 실행 직전에 현재 사실을 다시 읽는다

trigger 이후에도 Trading의 canonical membership, Fund/Book 상태, 시장 session,
최신 quote, 현금, 보유수량, sellable quantity, lot size를 다시 읽는다. 비율·전량 매도
수량은 trigger 시점의 sellable quantity에서 계산한다. 통과한 action만 기존
ADR-0007 PAPER directive service로 들어가며, Worker는 `execution.user_directives`에
직접 INSERT할 권한이 없다.

v1은 `PAPER + ONCE + MARKET + DAY`만 지원한다. LIVE, 반복 규칙, 예약/다음 장 이월,
사용자 확인 없는 자동 실행은 허용하지 않는다.

### 5. 장 마감과 완료 의미를 숨기지 않는다

정책은 `REJECT_TRIGGER`다. 조건이 참이어도 장이 닫혀 있으면
`MARKET_CLOSED_NO_ORDER`로 rule/trigger/execution에 기록하고 주문·체결·원장 반영을
만들지 않는다. 사용자 조회 응답은 “현재 장이 열려 있지 않아 주문을 제출하지
않았고 체결·원장 반영도 없다”는 문구를 함께 제공한다.

Rule의 `COMPLETED`는 exactly-once action이 durable USER_DIRECTIVE로 접수됐다는
뜻이다. 주문 체결 완료를 뜻하지 않는다. 실제 주문 상태와 fill/원장 반영은 반환된
`directive_id`의 기존 상태 머신에서 별도로 조회한다.

### 6. 저장소와 서비스 권한을 분리한다

- `svc_conditional_rule_orchestrator`: fixture 범위 규칙 생성·확인·일시정지·취소
- `svc_conditional_rule_worker`: 평가·trigger·execution/outbox 전이
- `svc_trading_api`: rule execution을 읽고 서버 측 directive를 유도하는 마지막 admission

AWS에서는 orchestrator와 worker가 서로 다른 LOGIN과 비밀번호를 사용한다. Worker는
별도 짧은 수명의 `trading.conditional_rule.execute` service token만 발급할 수 있고,
Hermes에는 이 secret, DB URL, broker credential을 전달하지 않는다.

## 결과

- 사용자 질의/조건 평가가 research/quant factory의 무한 루프와 독립적으로 응답한다.
- LLM 출력이 잘못돼도 schema, unit, semantic, fingerprint, trigger idempotency,
  Trading admission 중 하나에서 fail closed한다.
- 평균 매입가 수익률, 보유수량 비율 매도, 멀티 타임프레임 지표 조합을 같은 AST로
  표현할 수 있고 조건별 하드코딩 없이 지표/연산자 registry를 확장할 수 있다.
- 이 결정은 LIVE 거래를 승인하지 않는다.

## 기각한 대안

- **Hermes가 주기적으로 시세를 읽고 조건을 판단한다.** 재현성·중복 방지·권한 분리가
  없어 기각한다.
- **조건마다 Python if 문을 추가한다.** 조합 폭발과 검증 누락을 만들므로 기각한다.
- **조건 충족 시 다음 장까지 주문을 보관한다.** 사용자 의도와 가격 문맥이 달라지는
  예약 권한이므로 v1에서 기각한다.
- **Worker가 directive를 직접 INSERT한다.** 기존 Trading admission을 우회하므로
  기각한다.
