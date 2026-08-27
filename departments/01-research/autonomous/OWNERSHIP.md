# Strategy Hermes runtime boundary

상태: 현행 운영 계약 v1 (2026-08-27)

이 디렉터리의 **논리적 소유자와 실제 연구 실행자는 `Strategy Hermes`**입니다.
물리적으로 `departments/01-research/` 아래에 있는 것은 저장소의 기존 부서별
배치와 롤백 호환성을 유지하기 위한 위치일 뿐, 리서치 본부(`research-department`)
가 이 런타임을 소유하거나 실행한다는 뜻이 아닙니다.

## 소유 경계

| 경계 | 소유자 | 책임 |
|---|---|---|
| 자연어 전략 연구 목표 접수 | BFF 중앙 라우터 | `intake/*.json`에 멱등 요청만 기록 |
| 전략 연구 실행 | **Strategy Hermes** | 목표·가설·계획·코드·백테스트·비판·실패 기억·계보 |
| 독립 연구실 저장 | **Strategy Hermes** | `labs/<request_id>/`의 상태와 아티팩트 |
| 기계적 아티팩트 검증 | Strategy Hermes 런타임의 validator | 결과 계약과 후보 승격 조건 검사 |
| 외부 보고 전달 | Discord notifier | 연구실을 읽고 원래 요청 스레드에 전달만 수행 |
| 방법론·시장 데이터 제공 | Research HQ / Data Plane | 읽기용 API·소스·데이터 품질 제공; 전략 연구 실행 안 함 |
| 독립 승격 검증 | Quant/QA/Risk/사람 | 후보를 별도 검증·심사; Strategy Hermes를 대신해 연구하지 않음 |

## 호출 규칙

허용된 흐름은 다음 하나입니다.

```text
Web/Discord → BFF 중앙 라우터 → autonomous_research_ingress
             → strategy_hermes_supervisor
             → 직접 실행되는 Strategy Hermes → 독립 연구실
             → validator / 별도 검증 게이트 → 보고 전달
```

`research-hermes`, `quant-hermes`, CEO/Kanban, legacy factory, 주문·브로커·OMS,
회계 원장 또는 기존 연구 DB는 이 런타임의 실행 경로가 아닙니다. Strategy Hermes는
해당 경로에 카드를 만들거나 연구를 위임하지 않습니다. BFF는 접수·상태 조회를 위한
파일 경계를 사용하지만 연구실의 가설·계획·결과를 작성하지 않습니다.

## 보존·삭제 규칙

`factory/`, `contracts/factory_contracts.py`, `pipeline/factory_bridge.py`, 과거
migration 및 Docker 볼륨은 이 경계에 연결하지 않고 보존합니다. 활성 참조, 감사
증거와 롤백 계획을 각각 확인하기 전에는 삭제하거나 이동하지 않습니다. 새 변경은
이 문서의 소유 경계와 계약 테스트를 함께 갱신해야 합니다.
