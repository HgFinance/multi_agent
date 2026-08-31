# ADR-0007: 로컬 고정 데모 PAPER 지시를 Agent 주문과 분리된 권한으로 둔다

- 상태: Accepted
- 날짜: 2026-08-18
- 영향 영역: Operator BFF, Trading, PAPER OMS/Broker, Accounting projection
- 관련 문서: [Current Architecture](../../CURRENT_PROJECT_ARCHITECTURE.md),
  [Unified Domain API Specification](../UNIFIED_DOMAIN_API_SPEC.md),
  [Trading Department README](../../../departments/02-trading/README.md)

## 배경

기존 문서의 “모든 주문은 Risk Engine을 통과한다”는 문장은 Agent와 자동 전략이
자기 판단을 주문으로 바꾸지 못하게 하는 안전 불변식이었다. 그러나 이 문장은 다음 두
권한을 하나로 취급했다.

1. Agent, alpha, 전략 Worker 또는 rebalancer가 만든 **자동 주문 후보**
2. 로컬 고정 데모 사용자가 자기 Fund/Book에 명시적으로 내린 **PAPER 주문 지시**

두 번째 입력에도 첫 번째 입력의 경제적 판단 Gate를 적용하면 사용자가 확정한 PAPER
명령을 alpha·rebalancer 또는 Risk 정책이 다시 결정하는 역전이 생긴다. 반대로 이
예외를 “Hermes가 주문할 수 있다”로 구현하면 LLM이 사용자 권한을 획득한다. 따라서
주문 객체가 아니라 **권한의 출처(authority source)** 를 먼저 구분해야 한다.
여기서 고정 데모 사용자는 폐쇄형 fixture binding이며 로그인·가입·세션 또는 외부
사용자 인증 시스템을 뜻하지 않는다.

## 결정

### 1. 두 실행 레인을 분리한다

| 레인 | 권한 출처 | 경제적 판단 | 반드시 통과할 경계 |
|---|---|---|---|
| `AUTOMATED_STRATEGY` | Agent, alpha, 전략 Worker, rebalancer | 결정론적 Risk Decision 필수. Risk가 승인·축소·거부할 수 있다. | 기존 StrategySignal → OrderIntent → Risk → OMS 계약 전체 |
| `USER_DIRECTIVE` | BFF가 선택한 고정 데모 사용자 ID | 사용자의 명시적 PAPER 결정을 alpha·rebalancer·Risk가 veto하거나 재사이징하지 않는다. | fixture identity·Fund/Book 결합·결정론 파싱·기계적 주문 검증·멱등·영속성·`PAPER` 전용 |

`USER_DIRECTIVE_HIGHEST`는 PAPER 제어면에서 사용자 지시가 자동 전략 제안보다
우선한다는 뜻이다. LIVE 권한, Broker Credential, 원장 수정 권한 또는 성공 상태를
강제로 만드는 권한이 아니다. 두 레인은 같은 DTO를 일부 공유하더라도 서로의 권한
증거를 대신 사용할 수 없다.

### 2. Hermes와 LLM은 사용자 권한을 소유하지 않는다

- CEO는 고정 데모 ID와 원문·Fund/Book을 durable 요청 row 및 차단된 Kanban root/Trading
  카드에 먼저 결합한 뒤에만 Trading 카드를 해제한다.
- Trading Hermes는 다양한 자연어를 엄격한 스키마와 exact evidence span으로
  **제안**할 수 있지만, 그 결과는 `binding=false`이고 사용자·Fund·Book·symbol 또는
  서비스 proof를 소유하지 않는다. 기억·연구 결과·모델 판단에서 새 주문을 만들지
  않는다.
- BFF의 결정론 verifier가 원문 SHA-256, 모든 evidence span, 방향·수량·주문유형,
  질문·부정·조건·복합명령·LIVE 표현을 독립적으로 다시 검사한다. 모호하거나
  지원하지 않는 문장은 추정 실행하지 않고 clarification/rejection으로 남긴다.
- 구조화된 사용자 주문도 BFF가 고정 데모 ID를 actor로 결합한다. 요청 본문의
  `user_id`나 Hermes profile 이름은 권한 증거가 아니다.
- `/trading/agent/order`는 대화 클라이언트 호환 경로 이름일 뿐, Agent 주문 권한을
  만들지 않는다. 이 경로도 고정 데모 사용자의 원문 지시와 같은 `USER_DIRECTIVE`
  경계를 통과해야 한다.

### 3. 사용자 PAPER 지시의 admission 계약

공개 BFF 경로는 다음 네 동작만 받는다.

- `PLACE_ORDER`: 명시된 단일 PAPER 주문
- `PLACE_BASKET`: 두~스무 개의 명시 종목을 하나의 추적 지시로 처리하는 PAPER
  market 바스켓. 자연어 문법은 (a) `종목A, 종목B 100만원씩 매수해`인 동일 최대 KRW
  금액 BUY, (b) `종목A 3주, 종목B 2주 시장가 매수해/매도해`인 동일 방향 명시 수량,
  (c) `종목A 100만원, 종목B 50만원 시장가 매수해`인 종목별 명시 KRW 금액 BUY다.
  (a)와 (c)는 Trading이 book lock 안에서 최신 매도호가와 lot size로
  `floor(금액 / ask, lot size)` 수량을 산정하므로 종목별 실제 주문금액이 명시 금액
  이하이다. (b)는 각 종목의 명시 수량을 보존하며 SELL인 경우 `reduce_only`다.
- `SELL_ALL`: LS PAPER 계좌 `t0424` 체결기준 snapshot을 한 번 읽고, 각 종목코드로
  해당 Fund/Book의 canonical instrument를 매칭한 뒤 broker 매도가능수량만
  `reduce_only` child 주문으로 종목별 순차 전개
- `CANCEL_ALL`: 해당 Fund/Book의 canonical 미종료 PAPER 주문을 다시 읽어 취소
  가능한 주문만 자식 취소로 전개

모든 mutation은 `Idempotency-Key`가 필수다. 같은 key와 같은 정규화 명령은 최초의
동일 directive를 반환하고, 같은 key로 다른 명령을 보내면 `409`다. 접수 성공은
directive와 대상 snapshot이 durable PAPER store에 기록된 뒤에만 반환한다.

경제적 Risk veto를 적용하지 않는 것과 기계적 admission을 생략하는 것은 다르다.
다음 검사는 항상 결정론적으로 수행하고, 실패하거나 확정할 수 없으면 주문을 만들지
않는다.

- 고정 데모 ID, ACTIVE 사용자, `OWNER | CIO | TRADER` 역할
- 요청 Fund와 Book의 canonical 결합 및 사용자 membership
- `mode == PAPER`; LIVE mode·LIVE 주문 route·계좌번호·계좌 비밀번호 입력 금지
- 지원 symbol/side/order type, 양수 수량, lot/tick, limit price, TTL
- SELL_ALL은 LS PAPER `t0424` 체결기준 보유 snapshot을 먼저 읽고, 종목코드로
  canonical instrument를 매칭한 뒤 broker sellable quantity와 lot size로 전개
- 일반 주문은 canonical PAPER cash/position, sellable quantity와 미종료 주문 reservation
- durable store readiness, payload hash와 idempotency conflict

`PLACE_BASKET`은 Broker 수준의 원자 주문이 아니다. 모든 member의 catalog 해석,
KRW 통화, 최신 quote, lot 수량과 BUY 총 cash reservation 또는 SELL 전 member의
sellable quantity를 **첫 broker 호출 전에 모두** 확정한다. 그 뒤 한 member가
reject·cancel·UNKNOWN이면 남은 미제출 member를 자동으로 더 제출하지 않는다. 이미
제출된 leg는 같은 `directive_id` 아래서 계속 대사하고, 결과는
`IN_PROGRESS`·`PARTIAL`·`FAILED`·`UNKNOWN` 중 사실에 맞는 상태로 남긴다. Theme
expansion, ambiguous ticker, `각각`, mixed BUY/SELL, limit/price basket, 또는 빠진 list
member 같은 복합문은 이 release에서 추정 실행하지 않고 clarification으로 끝낸다.

이 검사는 요청을 시장/계정 상태에 맞는 주문으로 **접수할 수 있는지** 확인하는
기계적 제약이다. alpha 점수, 목표 비중, rebalancing 선호 또는 Risk budget을 근거로
사용자의 명시적 PAPER 결정을 뒤집는 별도 투자 판단이 아니다.

### 4. 일괄 명령의 완료와 부분 실패를 숨기지 않는다

`PLACE_BASKET`, `SELL_ALL`, `CANCEL_ALL`은 하나의 원자적 성공으로 가장하지 않는다. parent
directive는 고정된 대상 snapshot과 자식별 결과를 보존하며 다음 원칙을 따른다.

- 상태 집합은 `RECEIVED | RUNNING | IN_PROGRESS | PARTIAL | COMPLETED | FAILED |
  UNKNOWN`이다. `IN_PROGRESS`와 `UNKNOWN`을 성공으로 간주하지 않는다.
- 모든 대상이 성공 terminal 결과면 `COMPLETED`다.
- 성공과 실패가 섞이거나 한 자식이라도 실패한 채 다른 자식이 성공하면 `PARTIAL`이며,
  성공/실패/건너뜀 수와 각 대상의
  오류를 함께 반환한다.
- 대상이 있었지만 하나도 성공하지 못하면 `FAILED`다.
- `SELL_ALL`의 대상이 0건인 no-op을 `COMPLETED`로 확정하는 경우는 외부 LS PAPER
  모드에서는 최신 `t0424` snapshot에 양수 매도가능 보유분이 없고 durable open SELL
  reservation도 0임을 확인했을 때뿐이다. 로컬 PAPER 모드에서는 canonical 양수 회계
  보유분(`positive accounting position`)과 기존 open SELL reservation이 모두 0임을
  확인한다. 양수 보유분이 있지만 전부 예약됐거나 상태를
  확정할 수 없는 경우는 완료가 아니다. `CANCEL_ALL`도 canonical open PAPER order가
  0건임을 확인한 경우에만 `legs: []` no-op `COMPLETED`다.
- 부분 체결 뒤 취소, snapshot 뒤 체결/취소 같은 race는 성공으로 덮지 않는다.
  이미 terminal인 자식과 unresolved 자식을 구분해 재조회 가능한 상태로 남긴다.
- PaperBroker의 `ACKNOWLEDGED`는 접수된 active order일 뿐 완료가 아니다. ACK만 있는
  parent directive는 `IN_PROGRESS`이며 fill/cancel/reject 같은 terminal 사실 없이
  `COMPLETED`로 올리지 않는다.

따라서 HTTP 접수 성공과 거래 완료는 같은 의미가 아니다. 호출자는
`directive_id` 상태 조회로 최종 aggregate와 자식 결과를 확인해야 한다.

### 5. PAPER 계정과 LS 경계를 고정한다

- 배포된 사용자 직접 주문 레인의 **경제적 canonical account는 LS증권 모의투자
  (`LS PAPER`) 계좌**다. 주문·미종료 주문·체결·보유·현금은 PAPER broker 응답과
  snapshot을 기준으로 한다. 클라이언트가 보낸 holdings나 프로세스 메모리는 권위가
  없다.
- Trading Domain의 durable directive/leg/reservation/fill ledger는 주문 전 멱등성,
  예약, 재시작, UNKNOWN 상태와 감사 증거를 보존한다. Accounting의 immutable
  content-addressed reconciliation journal은 LS PAPER cash·buying power·position과
  내부 projection 차이를 조정한다. 이 원장은 broker와 독립된 가상 계좌가 아니다.
- Trading API/worker만 PAPER 전용 AppKey로 주문·취소·상태조회를 수행한다. 주문 또는
  취소 transport 결과가 모호하면 `UNKNOWN`으로 남기고 자동 재전송하지 않는다.
  BFF server는 명시적으로 활성화된 read-only account snapshot만 만들 수 있지만
  credential을 Browser·Hermes·downstream payload로 노출하지 않는다.
- 주문 scope root는 SQL binding 후 worker에 release하지 않고 `done`으로 닫으며,
  Trading primary만 `running` 상태에서 trusted tool을 호출한다. LS 주문 adapter는
  PAPER credential과 12자리 MAC header를 모두 요구하고, 초당 1회인 계좌 주문조회
  snapshot을 짧게 재사용한다. broker 주문번호 없는 `UNKNOWN`은 hot polling 대상에서
  제외하되 감사·수동 대사 기록으로 보존한다.
- LS LIVE 연결은 계속 **시세·호가·체결 시장 관측 read-only**다. PAPER adapter는
  LIVE AppKey로 fallback하지 않으며, 이 결정에는 LIVE order route나 LIVE Broker
  adapter가 없다.

## 결과

- “Agent는 주문을 제출하지 못한다”와 “고정 데모 사용자는 자기 PAPER 계정에 직접
  명령할 수 있다”가 동시에 참이다.
- 기존 자동 alpha/strategy/rebalancer 주문의 Risk·QA·OMS 경계는 약화되지 않는다.
- 사용자 지시는 모델의 추천이나 보고서가 아니라 별도의 감사 가능한 authority로
  남는다.
- durable admission store가 없거나 LS PAPER account를 읽지 못하면 fail closed한다.
- 이 ADR은 LIVE 실행을 승인하지 않으며, LIVE Broker와 주문 API는 계속 별도 결정
  대상이다.

## 기각한 대안

- **사용자 지시도 alpha/Risk가 최종 veto한다.** 사용자 확정 PAPER 권한을 자동화
  시스템 아래에 두므로 기각한다.
- **Hermes가 대화 내용을 해석해 직접 주문한다.** 모델이 권한과 주문 의미를 함께
  소유하고 재현성·멱등성이 사라지므로 기각한다.
- **BFF가 PAPER 잔고를 보관하거나 요청 holdings를 신뢰한다.** 중복 source of truth와
  oversell/cancel race를 만들므로 기각한다.
- **LS LIVE 계좌를 PAPER 계정으로 사용한다.** LS PAPER/LIVE 격리와 credential
  경계를 깨므로 기각한다. 별도 발급된 LS PAPER 계좌 사용과는 다른 대안이다.
