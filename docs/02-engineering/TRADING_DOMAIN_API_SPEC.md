# Trading Domain API 설계서

> 작성: 도현님 (Trading/Accounting Domain Owner) · 작성일: 2026-08-03
> 상위 계약: [MINIMUM_SERVICE_UNIT_SPEC.md](../01-product/MINIMUM_SERVICE_UNIT_SPEC.md) §11 (Case API 경로 이름),
> [TECH_STACK_DECISIONS.md](TECH_STACK_DECISIONS.md) §7 (FastAPI+Pydantic Backend, Hermes는 API/MCP 경계로만 통신),
> [TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md](../05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md) (v1.2) §4.2·§4.3
> 형식 참조: [RISK_QA_DOMAIN_API_SPEC.md](RISK_QA_DOMAIN_API_SPEC.md) (동규님), [GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md](GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md) (영주님)
>
> **이 문서가 하는 일과 안 하는 일**: 이미 있는 결정론적 Python(`contracts.py`, `oms.py`,
> `paper_broker.py`)을 FastAPI로 감싸는 방법을 정의한다. **새 주문 판정 로직을 만들지 않는다** —
> 상태 전이·멱등·수량 검증은 전부 `oms.py`에 이미 있고, API는 JSON↔도메인 객체 변환과 에러 매핑만 한다.
> §4의 저장소 항목은 **미결이며 팀장 확인 대기**다. §6의 MCP 도구 면은 **아직 구현 없음(설계만)**이다.
> 마지막 §7에 무엇이 확정이고 무엇이 제안/미구현인지 표로 정리했다.
>
> 구현: [`departments/02-trading/api/app.py`](../../departments/02-trading/api/app.py) (자체 점검 15개 영역 통과)

---

## 0. 왜 API로 감싸나

`contracts.py`/`oms.py`/`paper_broker.py`는 지금 같은 Python 프로세스에서 직접 import해서 쓰는
라이브러리다. `TECH_STACK_DECISIONS.md` §7이 "Hermes를 Domain Backend의 Python Environment에
직접 설치하지 않는다. 독립 Image와 API/MCP 경계로 통신한다"고 정해뒀다 — Hermes 컨테이너
(`hedgefund-trading-hermes`, `nousresearch/hermes-agent` 공식 이미지)에는 우리 코드가 아예 없고,
2026-08-03에 `platform_toolsets`로 셸·파일·코드실행을 다 껐으므로 넣어도 실행할 수단이 없다.

**그게 설계 의도다.** API를 통과하는 것만 노출되고, 그 목록이 권한 경계의 집행 지점이 된다.

## 1. 공통 규약

### 1.1 경로와 버전

- **Case에 종속된 것**: `MINIMUM_SERVICE_UNIT_SPEC.md` §11이 이미 이름을 지었다. 그대로 쓴다.
  - `POST /investment-cases/{case_id}/paper-orders`
  - `POST /investment-cases/{case_id}/cancel`
- **부서가 단독 소유하는 것**: `/trading/v1/...`
- `v1`은 API Path Version이다. `contracts.py`의 `SCHEMA_VERSION`(OrderIntent 계약 버전)과 다른 축이며 섞지 않는다.

### 1.2 인증

지금은 없다. `risk-api`/`audit-api`와 같은 정책으로 **`127.0.0.1` 바인딩만** 하고 외부에 게시하지 않는다.
Service Token 발급 주체가 미정이라(RISK_QA 스펙 §6와 같은 상태) 여기서도 검증하지 않는다.
Frontend·Browser는 이 API를 직접 부르지 않는다 — `AI_OFFICE_FRONTEND_PLAN.md` §6대로 FastAPI BFF가
유일한 진입점이다.

### 1.3 멱등성

`oms.py`가 이미 멱등키를 갖고 있다. 새로 설계하지 않고 그대로 쓴다.

| 대상 | 멱등키 | 이미 있는 동작 |
|---|---|---|
| `POST /trading/v1/order-intents` | `idempotency_key` (본문, 8~128자) | 같은 키면 기존 기록을 그대로 반환. 네트워크 재시도로 주문이 두 배 나가지 않는다 |
| `POST /trading/v1/orders` | `order_intent_id` | Intent 하나에 Broker Order 하나(불변식 3). 재호출 시 같은 `order_id` |
| `POST /trading/v1/orders/{id}/broker-events` | `broker_event_id` | 같은 값 재수신 시 무시(불변식 4). 브로커 재전송과 우리 재처리 양쪽에서 발생 |

> ⚠ 멱등 반환이라 `POST /trading/v1/order-intents`는 **새로 만들어지지 않아도 201**이다.
> 구분이 필요하면 응답의 `intent_status`/`version`을 본다.

### 1.4 에러 봉투

모든 에러가 같은 모양이다. FastAPI 기본 `HTTPException`은 본문을 `detail` 아래에 넣는데,
그대로 두면 호출자가 `error_code`를 두 군데서 찾아야 한다 — `StarletteHTTPException` 핸들러로
최상위에 평탄화했다.

```json
{
  "error_code": "TRADING_OMS_REJECTED",
  "message": "Risk 승인이 없는 주문은 전송할 수 없습니다"
}
```

| `error_code` | HTTP | 언제 |
|---|---|---|
| `TRADING_OMS_REJECTED` | 400 | OMS 불변식 위반(Risk 미승인 제출, 만료, 수량 초과, UNKNOWN 차단 등). **500이 아니다** — 호출자가 고칠 수 있는 요청이다 |
| `TRADING_INTENT_MISMATCH` | 400 | 경로와 본문의 `order_intent_id` 불일치 |
| `TRADING_CASE_MISMATCH` | 400 | 경로의 `case_id`와 Intent의 `trade_case_id` 불일치(§2.3) |
| `TRADING_INTENT_NOT_FOUND` / `TRADING_ORDER_NOT_FOUND` | 404 | 없는 자원 |
| `TRADING_INTENT_BODY_LOST` | 409 | 프로세스 재시작으로 Intent 원본이 사라짐(§4 저장소 미결의 직접 증상) |
| `TRADING_INVALID_INTENT` / `TRADING_INVALID_RISK_DECISION` | 422 | Pydantic 계약 위반 |
| `TRADING_INVALID_REQUEST` | 422 | 요청 본문 형식 오류 |
| `TRADING_HTTP_ERROR` | 그대로 | 위 어디에도 안 걸린 HTTP 에러(404 Route Not Found 등)를 같은 봉투로 평탄화한 것 |

## 2. Trading Domain API

### 2.1 Order Intent — 우리 쪽 심사 절차

상태 머신이 v1.2에서 둘로 나뉘었다. **이 절은 `IntentState` 머신이고 브로커는 이 상태를 모른다.**

```text
POST /trading/v1/order-intents                              → DRAFT
GET  /trading/v1/order-intents/{order_intent_id}
POST /trading/v1/order-intents/{id}/risk-review             → RISK_PENDING
POST /trading/v1/order-intents/{id}/risk-decision           → APPROVED | RESIZED | REJECTED
                                                              (거부 아니면 READY_TO_SUBMIT 까지)
```

`POST .../risk-decision`이 이 API에서 가장 오해하기 쉬운 지점이다. **판정 권한은 리스크본부에 있다.**
여기서 verdict를 계산하지 않고, 리스크본부가 준 `RiskDecision`을 계약으로 검증한 뒤 기록만 한다.
계약이 검증하는 것(`contracts.py`):

- `verdict=reject`인데 `approved_quantity`가 있으면 **거부한다** (422)
- `verdict=approve|resize`인데 승인 수량이 없거나 0 이하면 거부한다
- `reject`에 사유가 없으면 거부한다

`RESIZED`면 `requested_quantity`가 승인 수량으로 **줄어든 채** 다음 단계로 간다.

### 2.2 Broker Order — 브로커의 사실

```text
POST /trading/v1/orders                                     → CREATED
GET  /trading/v1/orders/{order_id}
POST /trading/v1/orders/{id}/submit                         → SUBMITTED
POST /trading/v1/orders/{id}/cancel                         → CANCEL_PENDING
POST /trading/v1/orders/{id}/broker-events                  → ACK/FILL/REJECT/CANCEL/EXPIRE
POST /trading/v1/orders/{id}/unknown                        → UNKNOWN
GET  /trading/v1/orders/{id}/events
```

**`POST /trading/v1/orders`가 두 머신의 유일한 접점이자 Risk Gate다.** 상태 전이가 아니라
새 객체 생성인 것이 v1.2의 핵심이다 — Intent와 Broker Order는 서로 전이하지 않는다.
`READY_TO_SUBMIT`이 아니거나 `risk_decision_id`가 없으면 여기서 막힌다.

`submit`은 **생성 때 통과했다고 전송 때도 통과라고 가정하지 않는다.** 다시 검사하는 것:
판정 만료(`risk_expires_at`), 승인 수량 초과, Intent 만료(`valid_until`).

`cancel`은 **취소가 아니라 취소 요청**이다(`CANCEL_PENDING`). 브로커가 cancel 이벤트를
돌려주기 전까지 `CANCELLED`로 쓰지 않는다 — 취소 요청과 체결이 교차하면 취소 대신 체결이 온다.

`unknown`은 브로커 응답이 없을 때다. **`FILLED`나 `CANCELLED`로 추정하지 않는다.**
종료 상태가 아니며, 남아 있는 동안 **같은 Fund의 신규 주문이 전부 막힌다**(불변식 8).
탈출은 `broker-events`에 `reconciled: true`를 준 Reconciliation 확정 결과로만 한다.

`GET .../events`는 Event Store 원문과 `rebuild_state()` 결과를 함께 준다. **둘이 다르면
Projection이 깨진 것이다**(불변식 6). 감사·Replay(F16)가 읽는 면이기도 하다.

### 2.3 Case 종속 경로

```text
POST /investment-cases/{case_id}/paper-orders
POST /investment-cases/{case_id}/cancel
```

`paper-orders`는 Broker Order 생성 → `submit` → Paper Broker `accept`(ack)까지를 한 호출로 묶는다.
`quote`를 주면 체결까지 시도하고, 없으면 `ACKNOWLEDGED`에서 멈춘다.

`quote`는 **가격만이 아니라 잔량(`bid_size`/`ask_size`)이 필수다.** 체결 수량이
`잔량 × 참여율(기본 0.05)`로 정해지기 때문이다. 잔량을 무한대로 가정하면 호가를 다 먹는
비현실적 체결이 나온다 — 자체 점검이 이걸 검증한다(1000주 호가에 100주 주문 → 50주 부분체결).

**시세는 여기서 조회하지 않는다.** 호출자가 준 값으로만 체결한다 — 시세 수집은 리서치본부
`market-api` 소관이고 우리는 별도 Collector를 만들지 않는다.

**경로의 `case_id`와 Intent의 `trade_case_id`가 같아야 한다**(`TRADING_CASE_MISMATCH`, 400).
안 막으면 Case B 경로로 낸 주문이 Case A의 승인 위에 올라타고, 정작 Case B의 `/cancel`은
그 주문을 못 본다 — 낸 쪽이 취소할 수 없는 주문이 생긴다. `/cancel`은 이미 `trade_case_id`로
거르고 있었고 `paper-orders`만 안 걸렀다. 자체 점검 11번이 이걸 검증한다.

### 2.4 시장 규칙

```text
GET  /trading/v1/market-rules/tick-size?price=70000
```

KRX 호가 단위. 브로커가 아니라 **거래소 규칙**이라 계약 계층(`contracts.py`)에 있다.
이 값이 틀리면 지정가가 거래소에서 거부되므로 실거래 전 최신 규정 대조가 필요하다.

## 3. 부서 간 통신

| 상대 | 방향 | 내용 |
|---|---|---|
| 리스크본부 | 받는다 | `risk.decision.v1` → `POST .../risk-decision`. **판정을 만들지 않는다** |
| 회계본부 | 준다 | Fill 이벤트. Position/Cash는 주문 의도가 아니라 체결에서 계산된다(원칙 4) |
| 리서치/퀀트 | 받는다 | 승인된 Strategy Signal. `intent_builder.build_order_intent`가 OrderIntent로 바꾼다 |
| AI QA/감사 | 준다 | Event Store(`GET .../events`). 감사 증빙 삭제 권한은 우리에게 없다 |

**어느 방향으로도 권한이 이전되지 않는다.** 담당자가 같아도(도현: 트레이딩↔회계) 합치지 않는다.

## 4. 저장소 — ⚠ 미결, 팀장 확인 대기

**현재 상태는 프로세스 메모리다.** `OMS(store=OrderStore())`가 dict 기반이라 API를 재시작하면
주문이 사라지고, BFF도 회계본부도 이 상태를 볼 수 없다.

실측(2026-08-03):

- Supabase `execution` 스키마에 `order_intents`/`orders`/`order_events`/`fills` 등 12개 테이블이
  **이미 있고 행은 0이다.**
- DB 트리거 `validate_order_state_transition`이 상태 전이를 강제하며, 내용이 `contracts.py`의
  `BROKER_TRANSITIONS`와 **글자 단위로 일치한다.** `CHECK (filled_quantity <= requested_quantity)`도 있다.
- `oms.py`의 `OrderStore` 위에 `ponytail:` 주석이 *"DB 자격증명이 확보되면 psycopg 구현으로"* 라고
  적혀 있고, **그 전제조건이 충족됐다**(`DATABASE_URL` 연결됨).

결정이 필요한 이유는 순서 때문이다. §6의 MCP 도구 면을 지금 in-memory 위에 올리면 주문 원장이
프로세스 메모리에 갇히고, 나중에 DB로 옮길 때 도구 계층을 다시 쓰게 된다.

바꿀 곳은 `app.py`의 `_oms = OMS(adapter="paper")` **한 줄**이다 — 엔드포인트 계약은 안 바뀐다.
그때까지 응답의 `authoritative: false`와 `source_of_record`가 이 사실을 계약으로 표시한다.

## 5. 관측

`risk-api`가 `/metrics`(Prometheus)와 `/risk/v1/observability/*`를 갖고 있다. 트레이딩도 같은
자리를 잡아야 하지만 **아직 없다.** 지금은 `GET /health`가 `intents`/`orders` 개수와
`store: "in-memory (execution.* 미연결)"`를 그대로 노출한다 — 재시작마다 0으로 돌아간다는
사실을 숨기지 않는 것이 목적이다.

## 6. Hermes/MCP 경계 — 설계만, 구현 없음

**이 API의 직접 호출자는 Agent가 아니다.** Hermes 페르소나가 부를 수 있는 것은 MCP 도구 면이고,
`departments/01-research/api/mcp_server.py`가 참고 대상이다. 아직 만들지 않았다.

만들 때 노출할 것과 안 할 것:

| MCP 도구 | 노출 | 근거 |
|---|---|---|
| `propose_order_intent` | ✅ | Agent가 만들 수 있는 유일한 산출물 |
| `get_order_intent` / `get_order` / `list_orders` | ✅ | 읽기 |
| `submit_order` | ❌ | CLAUDE.local.md 원칙 1이 명시적으로 금지 |
| `apply_risk_decision` | ❌ | 리스크본부 권한. Agent가 자기 주문을 승인하는 경로가 된다 |
| `broker_events` | ❌ | 브로커 사실을 Agent가 지어내는 경로가 된다(원칙 3) |

API에 `submit`이 있는데 도구에 없는 것이 모순처럼 보이지만 아니다 — API는 서비스 호출자용이고,
불변식이 HTTP 계층이 아니라 `oms.py`에 있어서 **누가 부르든 Risk 판정 없는 submit은 `OMSError`다.**
MCP 도구 면은 그 위에 "Agent는 애초에 그 버튼을 못 본다"를 한 겹 더 얹는 것이다.

## 7. 확정 vs 제안 — 요약

| 항목 | 상태 |
|---|---|
| `/investment-cases/{case_id}/paper-orders`·`/cancel` 경로 이름 | **확정** (상위 문서 §11이 이미 지음) |
| `/trading/v1/...` 부서 단독 경로 | **확정** (RISK_QA·GOVERNANCE 스펙과 같은 규약) |
| OrderIntent/BrokerOrder 2단 상태 머신 | **확정·구현 완료** (자체 점검 통과, DB 트리거와 일치) |
| 에러 봉투, 멱등 규칙 | **확정·구현 완료** |
| Paper 체결(참여율 상한 포함) | **구현 완료.** 시장충격·큐 대기 미반영 — D4 TCA에서 재보정 |
| 저장소(in-memory → `execution.*`) | **미결 — 팀장 확인 대기** (§4) |
| MCP 도구 면 | **설계만, 구현 없음** (§6) |
| 인증(Service Token) | **미정** — 발급 주체 미결. 지금은 `127.0.0.1` 바인딩으로 대체 |
| `/metrics`·Observability | **미구현** (§5) |
| Execution Plan / TCA / Broker Adapter | **범위 밖** — D4 |
| 회계본부 API | **범위 밖** — 별도 문서. NAV는 D3(Valuation/PnL) 선행 |
