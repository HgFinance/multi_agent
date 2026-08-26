# Trading Department

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.

## Mission

퀀트본부가 제공한 검증 가능한 Alpha Strategy Bundle을 받아 전략별 임시 Worker를 1:1로 생성한다. 모든 Worker는 같은 실시간 Paper 시장 스트림을 병렬 소비하고, 공유 Paper 계정 안에서 전략별 체결·포지션·성과 attribution을 유지한다. Trading은 Risk 소유 임계값과 Quant 소유 성과 가중치를 결정론적으로 조합해 정확히 하나의 전략을 선정한다.

고정 Bull/Bear 직원과 토론 경로는 없다. `StrategySignal` ≠ `OrderIntent` ≠ `Order`이며, 선정 결과도 Risk Gate와 기존 OMS/Broker 경계를 우회하지 않는다. 이 문장은 Agent·alpha·자동 전략 레인에 적용된다. 로컬 모의투자의 직접 PAPER 지시는 [ADR-0007](../../docs/02-engineering/adr/0007-authenticated-user-paper-directive-authority.md)의 별도 `USER_DIRECTIVE` 경계지만 현재 Compose에서는 비활성화한다.

## Runtime 구성

| Worker | 방식 | 역할 |
|---|---|---|
| 전략별 임시 Worker | 결정론, 동적 생성 | 하나의 immutable Quant 전략을 Paper 시장 이벤트에 실행 |
| `desk-runner` | 결정론 | Intent Builder, 계약 전이, 실행 가능성·비용·파생 Certification 처리 |

임시 Worker는 LLM을 호출하지 않고 전략을 수정하거나 스스로 선정·승격하지 않는다. 입력 Bundle 검증 실패 시 Worker를 만들지 않고 `REJECT`와 감사 사유를 남긴다.

## 핵심 실행 경로

- `employee_workers.py`
  - `validate_strategy_bundle()` — 필수 식별자·자본 배분·Quant 성과 가중치 검증
  - `create_temporary_worker()` — 전략 버전별 Worker ID 파생 및 1:1 생성
  - `TemporaryStrategyWorker.run()` — 동일 Paper 스트림에서 Strategy Signal 생성
- `scripts.py`
  - `SharedPaperAccount` — 공유 계정과 전략별 attribution subledger
  - `run_alpha_strategy_selection()` — 병렬 실행, Paper 보고서, Risk 기준 검사, 단일 전략 선택
- `contracts/` — StrategySignal·OrderIntent·Risk Gate 계약
- `oms/` — 주문 상태 머신과 Broker 경계

## 고정 fixture 사용자 직접 PAPER 레인

자동 전략 주문과 사용자 직접 주문은 권한 출처가 다르다.

| 항목 | 자동 전략 레인 | 사용자 직접 PAPER 레인 |
|---|---|---|
| source | Agent/alpha/strategy/rebalancer | BFF가 선택한 고정 데모 ID의 명시적 지시 |
| priority | 전략·Risk 계약이 결정 | `USER_DIRECTIVE_HIGHEST` |
| 경제적 veto | 결정론 Risk Decision 필수 | Risk·alpha·rebalancer가 사용자의 명시적 PAPER 결정을 veto/resize하지 않음 |
| 실행 전 경계 | Risk·QA·OMS | fixture actor map, ACTIVE Fund/Book membership, parser, account mechanics, idempotency, PAPER-only |
| LIVE | 승인된 별도 경로가 생기기 전 금지 | 항상 금지 |

Hermes는 사용자의 authority를 소유하지 않는다. 대화 원문을 자의로 보충하거나
종목·방향·수량을 선택하지 않으며, 결정론 parser와 Operator BFF가 구조화한다.
`/trading/agent/order`라는 호환 경로 이름도 Agent submit 권한을 뜻하지 않는다.
본문 `user_id`는 권한으로 받지 않고 BFF가 고정 데모 ID를 actor로 결합한다.

공개 BFF 명령은 단일 `PLACE_ORDER`, canonical 계정 기반 `SELL_ALL`, canonical
미종료 주문 기반 `CANCEL_ALL`이다. mutation마다 `Idempotency-Key`가 필요하고,
동일 key/동일 명령은 같은 directive를 반환하며 동일 key/다른 명령은 `409`다.
상태는 `RECEIVED | RUNNING | IN_PROGRESS | PARTIAL | COMPLETED | FAILED | UNKNOWN`만
사용한다.

- `SELL_ALL`은 client holdings를 신뢰하지 않는다. LS PAPER에서 대사된 canonical
  accounting position과 open SELL reservation을 같은 snapshot에서 다시
  읽고, 양수 sellable quantity만 `reduce_only` SELL 자식 주문으로 만든다. 현재 KRX
  주식 long-only PAPER 범위이므로 신규/확대 short와 자동 short cover는 하지 않는다.
- `CANCEL_ALL`은 canonical open PAPER orders만 대상으로 한다. snapshot 이후 이미
  체결·취소된 주문은 실제 자식 결과로 드러나며 성공으로 추정하지 않는다.
- 모든 자식이 성공해야 `COMPLETED`다. 성공과 실패가 섞이면 `PARTIAL`, 전부 실패하면
  `FAILED`, 확정할 수 없으면 `UNKNOWN`이다. 한 자식이라도 실패하면
  `COMPLETED`로 표시하지 않는다.
- PAPER broker `ACKNOWLEDGED`는 active order 접수 사실이므로 parent는
  `IN_PROGRESS`다. ACK만으로 체결·취소 완료를 만들지 않는다.
- `SELL_ALL`의 빈 `legs`가 no-op `COMPLETED`인 경우는 canonical 양수 position과
  open SELL reservation이 모두 0일 때뿐이다. `CANCEL_ALL`도 canonical open PAPER
  order가 0건임을 확인해야 빈 `legs` 완료다.

배포된 canonical 경제 계정은 LS증권 모의투자(`LS PAPER`) 계좌다. Trading의
`ls-paper` adapter만 PAPER 전용 credential로 주문·취소·상태조회를 수행하며 LIVE
credential로 fallback하지 않는다. durable directive/leg/reservation/fill ledger와
execution/accounting projection은 멱등성·예약·감사·재시작 복구를 소유하고 broker
snapshot과 immutable reconciliation journal로 대사된다. 프로세스 메모리 fallback과
요청에 포함된 잔고는 권위가 없다. LS LIVE 연결은 시세·호가·체결 시장 관측
read-only이며 Browser/Hermes에는 LS Credential 또는 LIVE order route가 없다.

## Paper 결과 계약

전략별 결과는 다음을 포함한다.

- 체결 목록
- 포지션
- 손익과 수익률
- 최대낙폭
- 거래비용
- 거래횟수
- 실패·중단 사유
- Risk 지표, 선택 점수, 선택 차단 사유

선정 결과는 `SELECTED_PENDING_IAM`이며 `live_order_submission_allowed: false`와 `risk_gate_required: true`를 항상 포함한다. 적격 전략이 없거나 Risk 입력이 불완전하면 전체 결과는 `REJECT`다.

## 자체 점검

```bash
python departments/02-trading/employee_workers.py
python departments/02-trading/scripts.py
python departments/02-trading/contracts/contracts.py
python departments/02-trading/oms/oms.py
python departments/02-trading/broker/paper_broker.py
```
