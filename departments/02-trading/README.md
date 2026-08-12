# Trading Department

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.

## Mission

퀀트본부가 제공한 검증 가능한 Alpha Strategy Bundle을 받아 전략별 임시 Worker를 1:1로 생성한다. 모든 Worker는 같은 실시간 Paper 시장 스트림을 병렬 소비하고, 공유 Paper 계정 안에서 전략별 체결·포지션·성과 attribution을 유지한다. Trading은 Risk 소유 임계값과 Quant 소유 성과 가중치를 결정론적으로 조합해 정확히 하나의 전략을 선정한다.

고정 Bull/Bear 직원과 토론 경로는 없다. `StrategySignal` ≠ `OrderIntent` ≠ `Order`이며, 선정 결과도 Risk Gate와 기존 OMS/Broker 경계를 우회하지 않는다.

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
