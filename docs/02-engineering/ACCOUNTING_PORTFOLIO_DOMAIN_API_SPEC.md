# Accounting/Portfolio Domain API 설계서

> 작성: 도현님 (Trading/Accounting Domain Owner) · 작성일: 2026-08-04
> 상위 계약: [TECH_STACK_DECISIONS.md](TECH_STACK_DECISIONS.md) §7 (FastAPI+Pydantic Backend, Hermes는 API/MCP 경계로만 통신),
> [TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md](../05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md) (v1.2) §4.4·§4.5·§8.2
> 형식 참조: [TRADING_DOMAIN_API_SPEC.md](TRADING_DOMAIN_API_SPEC.md) (같은 소유자·같은 규약),
> [RISK_QA_DOMAIN_API_SPEC.md](RISK_QA_DOMAIN_API_SPEC.md) (동규님), [GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md](GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md) (영주님)
>
> **이 문서가 하는 일과 안 하는 일**: 이미 있는 결정론적 Python(`ledger.py`, `portfolio.py`,
> `reconciliation.py`, `corporate_actions.py`, `daily_report.py`)을 FastAPI로 감싸는 방법을 정의한다.
> **새 회계 판정 로직을 만들지 않는다** — 차대 균형·멱등·평가 게이트·Break Severity는 전부
> 도메인 모듈에 이미 있고, API는 JSON↔도메인 객체 변환과 에러 매핑만 한다.
> §4의 저장소 항목은 **미결이며 트레이딩 OMS와 같은 결정을 기다린다**. §6의 MCP 도구 면은
> **아직 구현 없음(설계만)**이다. 마지막 §7에 무엇이 확정이고 무엇이 제안/미구현인지 표로 정리했다.
>
> 구현: [`departments/05-accounting-portfolio/api/app.py`](../../departments/05-accounting-portfolio/api/app.py) (자체 점검 16개 영역 통과)

---

## 0. 왜 API로 감싸나

트레이딩과 같은 이유다. `TECH_STACK_DECISIONS.md` §7이 "Hermes를 Domain Backend의 Python
Environment에 직접 설치하지 않는다. 독립 Image와 API/MCP 경계로 통신한다"고 정해뒀다 —
Hermes 컨테이너(`hedgefund-accounting-hermes`)에는 우리 코드가 아예 없다.

**그게 설계 의도다.** API를 통과하는 것만 노출되고, 그 목록이 권한 경계의 집행 지점이 된다.
회계에서는 이 경계가 트레이딩보다 더 중요하다 — **여기 없는 동사는 아예 실행할 수단이 없다**는
것이 "Posted Journal은 수정하지 않는다"를 문장이 아니라 계약으로 만드는 방법이기 때문이다.

## 1. 공통 규약

### 1.1 경로와 버전

- **부서가 단독 소유하는 것**: `/accounting/v1/...` (`/trading/v1/...`, `/risk/v1/...`과 같은 규약)
- 원장은 `ledger_id` 하나로 주소를 갖는다. `POST /accounting/v1/ledgers`가 Fund/Book을 받아
  발급하고, 이후 모든 경로가 `/accounting/v1/ledgers/{ledger_id}/...`다.

> **Case 종속 경로가 없다.** 트레이딩(`paper-orders`)·리스크(`risk-check`)와 달리
> `MINIMUM_SERVICE_UNIT_SPEC.md` §11에 회계본부가 소유할 Case 경로가 없다. 유일한 후보인
> `POST /investment-cases/{case_id}/evaluate`는 §16 평가 계약이 `slippage_bps`(D4 TCA)와
> `decision_quality`(AI QA/감사 판정)를 요구하므로 **우리 것이 아니다.** 구현이 없는 채로
> 경로만 선점하지 않는다.

> ⚠ **`/accounting/v1/` 접두사가 BFF와 겹친다.** `apps/api/accounting.py`가 이미
> `GET /accounting/v1/portfolio-snapshot`을 갖고 있다(CEO Daily Report용 Snapshot 참조).
> 프로세스도 포트도 다르지만 같은 접두사라 혼동 가능하다 — §7에 정리 대상으로 올렸다.

### 1.2 인증

지금은 없다. `risk-api`/`trading-api`와 같은 정책으로 **`127.0.0.1` 바인딩만** 하고 외부에
게시하지 않는다. Service Token 발급 주체가 미정이라 여기서도 검증하지 않는다.
Frontend·Browser는 이 API를 직접 부르지 않는다 — FastAPI BFF가 유일한 진입점이다.

### 1.3 멱등성

도메인 모듈이 이미 멱등키를 갖고 있다. 새로 설계하지 않고 그대로 쓴다.

| 대상 | 멱등키 | 이미 있는 동작 |
|---|---|---|
| `POST /accounting/v1/ledgers` | `(fund_id, book_id)` | 같은 Fund/Book이면 기존 `ledger_id` 반환. **안 그러면 원장이 둘 생기고 NAV가 갈린다** |
| `POST .../ledgers/{id}/fills` | `broker_fill_id` | 같은 체결 재수신 시 기존 분개 반환(불변식 3). 재처리로 잔고가 두 배가 되지 않는다 |
| `POST .../ledgers/{id}/capital` | `source_event_id` | 같음 |
| `POST .../ledgers/{id}/corporate-actions` | `action_id` | 같은 Action이 두 번 와도 분개는 한 번 |

> ⚠ 멱등 반환이라 위 경로들은 **새로 만들어지지 않아도 201**이다.

### 1.4 에러 봉투

모든 에러가 같은 모양이다. FastAPI 기본 `HTTPException`은 본문을 `detail` 아래에 넣는데,
그대로 두면 호출자가 `error_code`를 두 군데서 찾아야 한다 — `StarletteHTTPException`
핸들러로 최상위에 평탄화했다(트레이딩과 같은 처리).

```json
{
  "error_code": "ACCOUNTING_LEDGER_REJECTED",
  "message": "보유(60)보다 많은 매도(999)입니다"
}
```

| `error_code` | HTTP | 언제 |
|---|---|---|
| `ACCOUNTING_LEDGER_REJECTED` | 400 | `LedgerError` — 불균형 분개, 보유 초과 매도, 이중 반대분개 등. **500이 아니다** |
| `ACCOUNTING_VALUATION_REJECTED` | 400 | `ValuationError` — Mark 없음/낡음, NAV≤0에서 비중 계산 |
| `ACCOUNTING_CORPORATE_ACTION_REJECTED` | 400 | `CorporateActionError` — 공시 단계 Posting 시도, 미승인 선택형, 대상 포지션 없음 |
| `ACCOUNTING_REPORT_REJECTED` | 400 | `ReportError` — 스냅샷 부족, Fund/Book 불일치 |
| `ACCOUNTING_LEDGER_NOT_FOUND` / `ACCOUNTING_JOURNAL_NOT_FOUND` | 404 | 없는 자원 |
| `ACCOUNTING_INVALID_CORPORATE_ACTION` | 422 | Pydantic 계약 위반 |
| `ACCOUNTING_INVALID_REQUEST` | 422 | 요청 본문 형식 오류 |
| `ACCOUNTING_HTTP_ERROR` | 그대로 | 위 어디에도 안 걸린 HTTP 에러를 같은 봉투로 평탄화한 것 |

### 1.5 금액은 전부 문자열이다

JSON number는 IEEE754 double이라 `Decimal`이 깨진다. 응답의 모든 금액·수량·가격이 문자열이며,
`ui_read_model.py`와 같은 규칙이다. 호출자가 `float()`로 받으면 그 시점에 정밀도가 사라진다.

## 2. Accounting/Portfolio Domain API

### 2.1 원장 — 이중분개

```text
POST /accounting/v1/ledgers                                  → ledger_id 발급(멱등)
GET  /accounting/v1/ledgers/{ledger_id}
POST /accounting/v1/ledgers/{id}/capital                     → 자본 납입 분개
POST /accounting/v1/ledgers/{id}/fills                       → 체결 → 분개
POST /accounting/v1/ledgers/{id}/corporate-actions           → 기업행위 → 분개
GET  /accounting/v1/ledgers/{id}/journals
POST /accounting/v1/ledgers/{id}/journals/{journal_id}/reverse
GET  /accounting/v1/ledgers/{id}/trial-balance
GET  /accounting/v1/ledgers/{id}/positions
```

**이 API에 `PUT`·`PATCH`·`DELETE`가 하나도 없다.** 불변식 2("Posted Journal은 수정·삭제하지
않는다")를 문장이 아니라 라우팅 표로 집행한 것이다. 정정 경로는 `/reverse` 하나뿐이고 원본은
`status: reversed`로 남는다. 자체 점검 10번이 **URL 하나를 찔러보는 대신 라우팅 표 전체를
훑어서** 수정·삭제 메서드가 없음을 확인한다 — 나중에 누가 추가하면 그 시점에 걸린다.

`POST .../fills`가 트레이딩본부 → 회계본부의 접점이다. **주문 의도가 아니라 체결에서만 회계가
움직인다**(원칙 4). 여기서 가장 중요한 설계는 **평균원가를 호출자에게서 받지 않는 것**이다:

> 실현손익은 `(체결가 − 평균원가) × 수량`이다. 평균원가를 요청 본문으로 받으면 **실현손익이
> 호출자가 정하는 값**이 되고, 그건 회계 수치를 외부에서 확정하는 것과 같다(원칙 5).
> API는 `ledger.rebuild()`로 원장에서 재계산한 포지션만 쓴다. 자체 점검 6번이 이걸 검증한다
> (100주 @70,000 매수 후 40주 @75,000 매도 → 실현이익이 정확히 200,000).

수수료·세금은 손익에 섞지 않고 별도 비용 계정(5000/5100)으로 뺀다. 섞으면 나중에 TCA에서
집행 비용과 전략 알파를 분리할 수 없다.

`GET .../trial-balance`의 `total`은 **항상 0**이다. 0이 아니면 이중분개가 깨진 것이다.

### 2.2 평가 / NAV

```text
POST /accounting/v1/ledgers/{id}/valuations                  → PortfolioSnapshot
GET  /accounting/v1/ledgers/{id}/valuations
```

**시세를 여기서 조회하지 않는다.** `marks`는 호출자(market-api 경유)가 준다 — 트레이딩·회계는
TimescaleDB 자격증명을 갖지 않으며 별도 Collector를 만들지 않는다.

**보유 종목 중 하나라도 신선한 Mark가 없으면 NAV 자체를 만들지 않는다**(400). 부분 결과를
주지 않는 것이 핵심이다 — 일부만 평가한 NAV는 틀린 NAV이고, 그걸로 주문을 내면 비중 계산이
조용히 어긋난다. 신선도 기본값은 5분이고 `max_staleness_seconds`로 넓힐 수 있다(종가 평가처럼
정당하게 더 긴 창이 필요한 경우가 있다).

**응답의 `is_official`은 항상 `false`다.** NAV를 계산했다는 것과 확정했다는 것은 다르다.
공식 확정은 승인 절차이며 이 API에 그 경로가 없다.

### 2.3 대사

```text
POST /accounting/v1/reconciliations/fills                    → 양쪽을 다 받는다
POST /accounting/v1/ledgers/{id}/reconciliations/positions   → 내부는 원장에서
POST /accounting/v1/ledgers/{id}/reconciliations/cash        → 내부는 원장에서
```

Position/Cash 대사는 **외부(브로커) 값만 받는다.** 내부 값까지 호출자가 주면 호출자가 양쪽을
다 정하는 셈이라 대사가 성립하지 않는다 — 내부는 `ledger.rebuild()`로 재계산한다.
체결 대사만 양쪽을 받는데, 내부 체결을 `FillRecord`로 보관하지 않기 때문이다.

수량 불일치는 **항상 material**이다(마스터플랜 11.2가 브로커와 내부 포지션이 어긋나면 Kill
Switch 대상이라고 규정한다). 현금은 반올림 수준(±1원) 차이를 Break로 올리지 않는다.

**응답에 `closable_here: false`가 박혀 있다.** 대사는 Break를 만들기만 하고, **종결 권한은
AI QA/감사본부에 있다.** 이 API에 Break 종결 경로가 없다.

### 2.4 일일 보고

```text
POST /accounting/v1/ledgers/{id}/daily-reports
```

저장된 스냅샷을 쓰므로 `/valuations`를 **최소 2번(기초·기말)** 부른 뒤에 가능하다. 중간
스냅샷이 많을수록 Drawdown이 정확해진다 — 기초·기말만 있으면 장중 저점을 못 봐서 Drawdown이
과소평가된다.

`unexplained_pnl`이 **0이 아니면 원장·평가·자본유출입 중 어딘가가 어긋난 것이다.**
반올림해서 없애지 않는다 — 그 값이 Break의 근거다. `is_official`은 여기서도 항상 `false`다.

`strategy_of`(= `source_event_id → strategy_id`)를 안 주면 전략별 분해가 전부
`UNATTRIBUTED`로 모인다. 원장에 전략 차원이 없어서인데(분개는 fund/book까지만 안다),
Supabase의 `accounting.journals`도 마찬가지라 **DB 델타가 필요한 항목**이다.

## 3. 부서 간 통신

| 상대 | 방향 | 내용 |
|---|---|---|
| 트레이딩본부 | 받는다 | Fill 이벤트 → `POST .../fills`. **주문 의도로는 회계가 안 움직인다**(원칙 4) |
| 리서치본부 | 받는다 | Mark Price(market-api 경유) → `POST .../valuations`. **우리가 시세를 수집하지 않는다** |
| 참조 데이터 | 받는다 | Corporate Action(`reference-api`) → `POST .../corporate-actions`. 우리가 만들지 않는다 |
| 리스크본부 | 준다 | Material Break, NAV·Exposure. **Break 종결은 우리 권한이 아니다** |
| AI QA/감사 | 준다 | 분개 원문(`GET .../journals`). 감사 증빙 삭제 권한은 우리에게 없다 |
| CEO | 준다 | Daily Report. `is_official: false` — CEO는 NAV 확정 권한이 없다 |

**어느 방향으로도 권한이 이전되지 않는다.** 담당자가 같아도(도현: 트레이딩↔회계) 합치지 않는다 —
트레이딩 API가 원장을 쓰지 않고, 이 API가 주문을 내지 않는 것이 그 경계다.

## 4. 저장소 — Supabase `accounting.*` (2026-08-04 구현)

`DATABASE_URL`이 있으면 Supabase가 원장이고, 없으면 프로세스 메모리다.
구현은 [`ledger/repository.py`](../../departments/05-accounting-portfolio/ledger/repository.py)이며
`app.py`는 `_ledger()` 한 곳에서만 두 모드를 가른다 — 엔드포인트 계약은 바뀌지 않았다.

| | DB 모드 | 인메모리 모드 |
|---|---|---|
| 조건 | `DATABASE_URL` 있음 | 없음 |
| 원장 | `accounting.journals` / `journal_lines` | `_ledgers` dict |
| Projection | `accounting.positions` / `cash_balances` | 없음(요청마다 재계산) |
| 스냅샷 | `accounting.portfolio_snapshots` | `_snapshots` dict |
| `ledger_id` | **`book_id`와 같다** | 같음(규약 일치) |
| `/health`의 `store` | `supabase accounting.*` | `in-memory (accounting.* 미연결)` |

**`ledger_id == book_id`인 이유:** Book 하나는 Fund 하나에 속하므로 book_id만으로 장부가
정해진다. 별도 매핑표를 두면 그 표가 프로세스 메모리에 남아 재시작 후 같은 id로 같은
장부를 못 연다 — 저장소를 옮긴 의미가 없어진다.

**Posting은 DRAFT → 라인 → POSTED 3단계다.** DB 트리거가 POSTED 전환 시점에 차대 균형을
검사하므로(`journals_validate_posting`) 처음부터 POSTED로 넣으면 라인을 붙일 수도 없고
(`journal_lines_protect_posted`) 균형 검사도 안 걸린다. 즉 **불변식 1·2·3이 우리 코드와
DB 양쪽에 있다** — 애플리케이션을 우회해 psql로 불균형 분개를 넣어도 거부된다.

**`unique (event_type, source_event_id)`는 Fund/Book 전역이다.** 다른 장부가 이미 쓴
`source_event_id`로 분개하면 409(`ACCOUNTING_SOURCE_EVENT_CONFLICT`)다. 조용히 "이미
반영됨"으로 넘기면 그 장부에는 분개가 없는데 있다고 착각하게 된다.

**DB 실패는 503이다.** 메모리로 말없이 후퇴하지 않는다 — 기록되지 않은 분개를 기록된 것처럼
응답하면 그 뒤의 모든 잔고가 틀어진다.

`authoritative`는 두 모드 모두 여전히 `false`다. 저장 위치가 바뀌었을 뿐 NAV 확정·Close
승인 절차는 아직 없다. `source_of_record`가 실제 저장 위치를 말한다.

**Fund/Book/계정과목은 API가 만들지 않는다.** `repository.bootstrap()`이 하며, 요청 경로에는
없다 — Fund를 여는 것은 자본 구조 결정이지 주문 처리 중에 일어날 일이 아니다. 없는 book_id로
원장을 열면 404(`ACCOUNTING_BOOK_NOT_FOUND`)다.

트레이딩 OMS 저장소(`execution.orders`)는 **아직 프로세스 메모리다**
([TRADING_DOMAIN_API_SPEC.md](TRADING_DOMAIN_API_SPEC.md) §4). 회계를 먼저 옮긴 이유는
원장이 휘발되면 대사할 내부 원천 자체가 없어지기 때문이다.

## 5. 관측

`risk-api`가 `/metrics`(Prometheus)를 갖고 있다. 회계도 같은 자리를 잡아야 하지만 **아직 없다.**
지금은 `GET /health`가 `ledgers`/`journals` 개수와 `store`를 그대로 노출한다 — DB 모드면
실제 저장된 행 수를, 인메모리면 재시작마다 0으로 돌아가는 값을 숨기지 않는 것이 목적이다.

회계에서 관측이 붙으면 우선 볼 값은 정해져 있다: **시산표 합계(항상 0)**, **미설명 손익(항상 0)**,
**Material Break 수**. 셋 다 이미 API가 내주고 있으므로 수집만 붙이면 된다.

## 6. Hermes/MCP 경계 — 설계만, 구현 없음

**이 API의 직접 호출자는 Agent가 아니다.** Hermes 페르소나가 부를 수 있는 것은 MCP 도구 면이고,
아직 만들지 않았다. 만들 때 노출할 것과 안 할 것:

| MCP 도구 | 노출 | 근거 |
|---|---|---|
| `get_positions` / `get_trial_balance` / `get_nav` | ✅ | 읽기. 단 Agent가 이 값을 **인용**할 수는 있어도 **확정**할 수는 없다 |
| `list_journals` | ✅ | 읽기(감사 추적) |
| `run_reconciliation` | ✅ | Break를 만들 뿐 종결하지 못한다 |
| `post_fill` / `post_capital` / `post_corporate_action` | ❌ | 분개 Posting은 결정론적 서비스가 한다. Agent가 장부를 쓰는 경로가 된다 |
| `reverse_journal` | ❌ | 정정은 승인 절차다. Agent가 자기 실수를 스스로 지우는 경로가 된다 |
| `close_break` | ❌ | 애초에 API에도 없다. AI QA/감사본부 권한 |

트레이딩과 같은 구조다 — API에 `reverse`가 있는데 도구에 없는 것이 모순처럼 보이지만 아니다.
API는 서비스 호출자용이고, 불변식이 HTTP 계층이 아니라 도메인 모듈에 있어서 **누가 부르든
불균형 분개는 `LedgerError`다.** MCP 도구 면은 그 위에 "Agent는 애초에 그 버튼을 못 본다"를
한 겹 더 얹는 것이다.

## 7. 확정 vs 제안 — 요약

| 항목 | 상태 |
|---|---|
| `/accounting/v1/...` 부서 단독 경로 | **확정** (TRADING·RISK_QA·GOVERNANCE 스펙과 같은 규약) |
| 에러 봉투, 멱등 규칙, 금액 문자열 | **확정·구현 완료** |
| 이중분개 원장, Reversal 전용 정정 | **확정·구현 완료** (수정·삭제 메서드 부재를 자체 점검이 강제) |
| 평균원가를 원장에서 재계산 | **확정·구현 완료** (실현손익을 호출자가 못 정한다) |
| Valuation/PnL/NAV (D3) 계산 | **구현 완료.** 단 **Mark 공급원(market-api 종가)은 미연결** — 계산은 되고 입력이 없다 |
| Corporate Action (F25) | **구현 완료.** 배당수익 계정(4200) 신설은 DB 델타 대기 — 지금은 배당이 실현손익(4000)에 섞인다 |
| Daily Report (F23) | **구현 완료.** 전략별 분해는 호출자 매핑 의존(원장에 전략 차원 없음 — DB 델타) |
| Case 종속 경로 | **없음.** `evaluate`는 D4 TCA + QA 판정이 필요해 우리 것이 아니다(§1.1) |
| `accounting-api` Container | **구현 완료.** `127.0.0.1:8046`. Build Context가 저장소 루트다(§8) |
| 저장소(in-memory → `accounting.*`) | **구현 완료** (2026-08-04, §4). `DATABASE_URL`로 갈린다 |
| ACC-01 (체결 1건 → 분개·Position·스냅샷) | **구현 완료.** `ledger/fill_consumer.py`. 단 체결 원천이 `execution.fills`가 아니라 API 주입이다(TRD-01 대기) |
| 대사·Break 영속 (`reconciliations`→`breaks`) | **구현 완료** (2026-08-04). `reconciliation/recon_repository.py`. `GET .../breaks`가 미종결 목록을 준다. **이벤트 전송로(Redis)는 PLAT-02 대기** — 리스크·QA는 지금 이 표를 읽는다 |
| `api.*` 회계 읽기 뷰 | **구현 완료** (`20260804000500`). `portfolio_snapshot_latest` / `position_holdings` / `ledger_balances` / `open_breaks`. 트레이딩 뷰는 `execution.*`가 0행이라 만들지 않았다 |
| `/ui/snapshot` 원천 | **회계 구간만 교체 완료.** `?book_id=`를 주면 portfolio·ledger가 Supabase에서 온다. trading은 Scripted Loop이며 `sources`가 구간별 출처를 밝힌다 |
| Mark 확정 여부(`is_final`) | **구현 완료.** 기본값 False(미확정). 미확정 봉으로 평가하면 스냅샷 `quality_status`가 WARN이 된다. NAV를 막지는 않는다 |
| symbol ↔ instrument_id | **구현 완료.** `repository.instrument_by_symbol()`은 Point-in-Time(`valid_from`/`valid_to`), `api.position_holdings`는 현재 대표 코드 |
| MCP 도구 면 | **설계만, 구현 없음** (§6) |
| 인증(Service Token) | **미정** — 발급 주체 미결. 지금은 `127.0.0.1` 바인딩으로 대체 |
| `/metrics`·Observability | **미구현** (§5) |
| BFF `/accounting/v1/portfolio-snapshot`과의 접두사 중복 | **정리 대상** (§1.1). 그 경로는 Domain 읽기인데 BFF에 있다 |
| Long/Short 분리, Borrow/Financing 비용 | **범위 밖** — 팀 가이드 v1.2 1.1, 미구현 |
| 관리보수·성과보수·High-Water Mark | **범위 밖** — Mandate 미확정 |

## 8. Container

```bash
docker compose up -d accounting-api      # 127.0.0.1:8046 (로컬 전용)
```

**Build Context가 저장소 루트다** — `departments/05-accounting-portfolio`가 아니다.
트레이딩(`trading-api`)은 자기 폴더 안에서 닫히지만 회계는 `ledger.py`와
`reconciliation.py`가 **모듈 최상위에서** 부서 경계를 넘기 때문이다:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "02-trading" / "contracts"))
from contracts import Side
```

이 계산이 `departments/<본부>/<모듈>/x.py` 배치를 전제하므로 이미지 안에서도 같은 상대
경로를 유지해야 한다(`03-risk`와 같은 사정). compose는
`context: ../..` + `dockerfile: departments/05-accounting-portfolio/Dockerfile`이다.

**`DATABASE_URL`이 필요하다.** 없으면 컨테이너가 인메모리 모드로 뜨고, 그 원장은 컨테이너를
지우면 사라진다. compose에 추가할 것(§4 구현 이후 남은 배선).

**아직 복제하면 안 된다.** DB 모드에서는 원장이 갈라지지 않지만, 같은 장부에 동시에 분개하면
경합 창(로드 → 계산 → INSERT)이 생긴다. DB의 `unique (event_type, source_event_id)`가
이중 분개는 막지만 평균원가 계산이 오래된 상태를 볼 수 있다. 단일 인스턴스로 둔다.

계획서 6.6의 `ledger-worker`/`portfolio-projector`는 넣지 않았다 — 코드가 없다.
