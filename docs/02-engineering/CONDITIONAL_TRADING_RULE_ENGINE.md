# Conditional PAPER Rule Engine

이 문서는 구현된 v1 백엔드 계약과 운영 경계를 요약한다. 프론트엔드 계약이나 LIVE
권한은 포함하지 않는다. 권한 결정은
[ADR-0008](adr/0008-authenticated-conditional-paper-rule-authority.md)을 따른다.

## 흐름

```text
fixed local fixture user
  -> BFF preview (untrusted AST candidate)
  -> schema + semantic + ambiguity validation
  -> exact spec_sha256 confirmation
  -> conditional-rule-worker
  -> LS PAPER final candles + Trading canonical portfolio context
  -> deterministic indicator/expression evaluation
  -> PostgreSQL exactly-once trigger claim
  -> execution guard
  -> Trading rebinds quantity to the exact confirmed sizing policy
  -> existing Trading USER_DIRECTIVE PAPER admission
```

Hermes/LLM은 후보 AST를 만들 수 있지만 지표 계산, trigger 판정, 수량 확정, 주문 제출
권한을 갖지 않는다. research/quant factory와 조건 평가 워커는 프로세스·DB role·큐를
공유하지 않는다.

## 관리 API

모든 경로는 BFF가 선택한 고정 fixture ID와 Fund/Book access check를 사용한다.
브라우저 로그인·세션·Supabase Auth를 전제로 하지 않는다.

- `POST /ui/conditional-rules/preview`: 후보를 정규화하고 clarification, assumptions,
  canonical spec과 `spec_sha256` 반환
- `POST /ui/conditional-rules`: preview와 동일한 원문/spec/hash로 확인 대기 규칙 생성
- `POST /ui/conditional-rules/{rule_id}/activate`: exact hash 확인 후 활성화
- `POST .../{rule_id}/pause`, `POST .../{rule_id}/resume`, `DELETE .../{rule_id}`
- `GET /ui/conditional-rules`, `GET .../{rule_id}`: 최신 execution/guard/directive 상태 조회

장 마감 trigger는 `MARKET_CLOSED_NO_ORDER`와 사용자 메시지를 반환하며 주문·체결·원장
반영이 없다. `directive_id`가 생긴 뒤의 체결 상태는 기존 directive 상태 API가
canonical source다.

장 운영 캘린더를 읽지 못한 경우는 장 마감으로 오표시하지 않고
`MARKET_SESSION_UNAVAILABLE`로 거절한다. Trading의 결정론적 409 거절은 나중에 오래된
trigger를 다시 제출하지 않도록 terminal failure로 처리한다.

## 지원 범위

- clocks: completed `BAR_CLOSE`, fresh `QUOTE`
- timeframes: `1M`, `3M`, `5M`, `10M`, `15M`, `30M`, `1H`, `1D`
- indicators: SMA, EMA, RSI, MACD, Bollinger Bands, Envelope, volume average,
  ATR, ADX, Stochastic, CCI, MFI, OBV, ROC, VWAP, Williams %R, Donchian, PSAR
- expressions: arithmetic, comparison, cross above/below, AND, OR, NOT
- portfolio facts: quantity, sellable quantity, average entry, market value, NAV,
  weight, unrealized PnL, PnL ratio, available cash
- actions: fixed-share BUY/SELL, position-percent SELL, ALL SELL
- exit OCO bracket: exactly two same-symbol, same-sizing `SELL` rules for an
  existing position; the server derives their group ID and activates both legs
  atomically.  Once one leg obtains the PAPER OMS submission slot, the worker
  cancels the armed sibling before it can submit a duplicate exit.
- trailing exit: a root `TRAILING_STOP` `SELL` rule on fresh quotes only.  The
  worker persists the highest quote seen after `ACTIVE` by rule/version, so a
  restart cannot reset the watermark. `DRAWDOWN` is required; optional
  `ACTIVATION_RETURN` arms the stop only after canonical average entry has
  reached that return.
- hard limits: `PAPER`, `ONCE`, market order, DAY, market-closed reject

`1M`/`3M`/`5M`/`10M`/`15M`/`30M`/`1H` candle은 LS `t8452`의 final `1M`
rows에서 같은 bucket 규칙으로 생성한다. `1D`는 LS `t8451`의 adjusted daily
chart에서 가져오며, 장중의 미완성 일봉은 제외한다. 중복 또는 분 단위 내부 공백이
있는 bucket과 partial candle은 Indicator Engine에서 사용하지 않는다.

## 복합 지표·시간프레임 규칙

`BAR_CLOSE`의 `primary_timeframe`은 **주문 판정 cadence**다. 모든 지표는 각자의
`timeframe`을 명시하고, 워커는 primary candle의 종가 시각을 watermark로 잡는다.
다른 timeframe 값은 그 watermark보다 늦지 않은 가장 최근의 **완성봉**만 쓴다.
따라서 3분봉이 09:03에 마감될 때, 1분봉 09:04 값이나 아직 진행 중인 15분봉
09:00~09:15 값은 그 3분봉 판정에 섞이지 않는다.

- `CROSS ABOVE/BELOW`의 좌·우 항은 반드시 같은 timeframe이어야 한다.
  예: 3분봉 SMA(5)와 SMA(20)의 골든크로스.
- 서로 다른 timeframe은 `LOGICAL AND/OR`로 결합한다.
  예: 3분봉 골든크로스 **AND** 15분봉 RSI(14) < 70.
- `primary_timeframe`은 조건에 쓰인 가장 빠른 bar timeframe보다 느릴 수 없다.
  3분봉 조건을 5분마다 조용히 확인하는 해석을 막는다.
- `QUOTE`는 신선한 현재가/계좌 값 전용이다. 완성봉 지표와 미완성 tick을 한
  조건식에서 섞지 않는다.
- 명시적인 KST 시간창(`10:00~14:30에만`, `오전 10시부터 오후 2시 30분까지`)은
  `TIME.KST_SECONDS_SINCE_MIDNIGHT`의 직접 범위 비교로 AND 결합할 수 있다.
  24시간 표기 또는 오전/오후가 없는 `2시`는 해석하지 않는다.
- intraday 지표 warm-up은 LS 1분봉 연속조회 6,000행 및 worker 2,000봉 한도를
  preview에서 먼저 검증한다. 예를 들어 1시간봉 SMA(100)는 현재 집계 경로의
  조회 범위를 넘으므로 ACTIVE로 만들지 않고 명확한 capability gap으로 반환한다.

실행 가능한 자연어 예시는 다음과 같다. Hermes는 이 문장을 allow-listed AST로만
해석하고, 원문에 없는 종목·수량·임계값·봉 주기는 추정하지 않는다.

| 사용자 문장 | 결정론적 실행 의미 |
| --- | --- |
| `하이닉스 3분봉 5선이 20선 상향 돌파하고 15분봉 RSI(14)가 70 미만이면 2주 시장가 매수` | 3M SMA(5)/SMA(20) edge cross AND latest closed 15M RSI filter; 3M cadence |
| `삼성전자 5분봉 종가가 볼린저 상단을 상향 돌파하고 1시간봉 ADX(14)가 25 초과면 1주 매수` | 5M close/Bollinger upper edge cross AND latest closed 1H ADX filter; 5M cadence |
| `삼성전자 현재가가 7만원 이하면 100만원 시장가 매수` | `NOTIONAL_KRW=1,000,000` 최대 주문금액. trigger 시 신선한 가격과 lot size로 정수 수량을 내림 산정하고, Trading이 최우선 매도호가 기준으로 다시 상한을 검사 |
| `SK하이닉스 평균 매입가 대비 2% 상승하면 보유수량의 50% 시장가 매도` | fresh quote versus canonical average entry price; one-shot PAPER sell |
| `삼성전자 5주 시장가 매수하고 매수가 대비 3% 상승하면 매도하고 2% 하락하면 매도` | 매수가 전량 체결 뒤 `(+3% OR -2%)` 하나의 SELL rule을 활성화. 두 독립 매도 규칙/OCO 경쟁을 만들지 않으므로 먼저 충족된 한 번만 청산 시도 |
| `하이닉스 5주 시장가 매수하고 매수가 대비 3% 수익 이후 고점 대비 1% 하락하면 매도` | 매수가 전량 체결 뒤 `TRAILING_STOP` SELL rule을 활성화. +3%에서 high-water tracking을 시작하고 최고가 대비 -1%에서 1회 청산 시도 |
| `하이닉스 5주 시장가 매수하고 매수가 대비 3% 수익 이후 고점 대비 1% 하락하면 매도, 최대 5거래일 동안 추적` | 전량 체결 시점부터 공식 KRX 정규장 캘린더의 5번째 유효 세션 마감까지 위 trailing SELL을 추적. 주말·휴장일은 세지 않으며 캘린더가 부족하면 임의 날짜로 활성화하지 않음 |
| `하이닉스 3분봉 5선이 20선 상향 돌파하고 10:00~14:30에만 2주 시장가 매수` | 3M cross AND KST 10:00:00 ≤ observed time ≤ 14:30:00; market session guard remains mandatory |
| `하이닉스 보유분 전량을 평균 매입가 대비 2% 상승 시 매도하고 1% 하락 시 매도. 한 쪽 실행 시 나머지 취소하는 OCO` | two same-size SELL exit rules; server-derived OCO group; both become ACTIVE together, then first submitted leg cancels the other |
| `하이닉스 평균 매입가 대비 2% 수익 이후 고점 대비 1% 하락하면 전량 매도` | fresh-quote trailing SELL; arms at +2% versus canonical average entry, then exits at or below 1% below the highest observed fresh quote since ACTIVE |

다음은 의도적으로 아직 지원하지 않는다. 문장을 억지로 다른 주문으로 바꾸지 않고
capability gap으로 알려야 한다.

- 순차 조건·N회 반복·분할 청산 상태머신
- 트레일링 조건과 다른 AND/OR·시간창·완성봉 조건의 결합, 여러 다리의 순차 상태 전이
- 여러 종목을 하나의 원자적 basket으로 주문
- 완성봉 지표와 실시간 호가를 같은 순간의 하나의 hybrid trigger로 결합

### KRW 금액 기준 수량

`NOTIONAL_KRW`는 `100만원 매수`, `100만원어치`, `50만원만큼`처럼 원문에서 주문
동사에 직접 결합된 양의 정수 KRW 금액만 받는 MARKET 주문 수량 정책이다. 이 금액은
구조화 candidate의 수량 정책 값과 정확히 일치해야 한다. 이는 fractional share를 만들
수 없는 KRX PAPER 주문의 **최대 주문금액**이며, 정확히 그 금액을 체결한다는 보장은
아니다.

1. 조건 worker는 trigger 시점의 신선한 현재가와 종목 lot size로
   `floor(금액 / 현재가, lot size)` 수량을 만든다. 0주, 현금 부족, 매도 가능 수량
   부족은 주문 없이 guard 거절된다.
2. Trading은 주문을 만들기 직전 동일한 quantity가 자기 신선 호가 기준 금액 상한을
   넘지 않는지 다시 검사한다. 매수는 최우선 매도호가, 매도는 최우선 매수호가를
   사용한다. 가격이 올라 상한을 넘으면 거절한다.
3. 가격이 내려간 경우 worker가 이미 확정한 수량을 늘리지 않는다. 이것은 활성화된
   사용자 주문의 수량을 서버가 사후 증액하지 않기 위한 정책이다. 체결가는 시장가
   특성상 달라질 수 있고, 매수 비용 buffer까지 포함한 가용 현금 검증은 Trading이
   계속 수행한다.

`LIMIT`, 소수 KRW, `100만원씩`, 또는 주문 동사와 결합되지 않은 가격 표현에는
`NOTIONAL_KRW`를 만들지 않는다. Hermes는 수량을 임의로 추정하지 않으며, 원문의
주문 금액과 candidate 금액이 다르면 실행하지 않는다.

### 매수 후 익절·손절 브래킷

즉시 MARKET 매수 뒤 `매수가 대비 X% 상승하면 매도하고 Y% 하락하면 매도`를 함께
명시하면, 기존 durable bundle이 **매수 전량 체결 후에만** 하나의 SELL rule을 ACTIVE로
전환한다. 이 rule은 `보유수량 == 이번 매수 수량 AND (+X% OR -Y%)` 이다. `X`는 양의
익절, `Y`는 음의 손절이어야 하며 각 청산 수량은 즉시 매수 수량과 같거나 생략되어야
한다. 부분체결·실패·만료 시 보호 rule은 활성화하지 않는다.

이 경로는 기존 보유분에 대한 2-rule OCO와 다르다. 진입 브래킷은 두 개의 SELL rule을
동시에 감시하지 않으므로 sibling 취소 경쟁이나 이중 매도 위험이 없고, 평가상 먼저 참인
OR 분기에서 한 번만 청산한다. 기존 보유분을 같은 종목에 섞은 경우 평균 매입가 기준이
달라질 수 있어 보유수량 guard가 청산을 차단한다. 분할 익절, 두 개 이상의 목표가,
손절 후 재진입은 상태 전이 요구사항이 있으므로 아직 지원하지 않는다.

### 전량 체결 뒤 N거래일 추적

매수 후 청산 문장 끝에 `최대 N거래일 동안 추적` (`N=1..20`)을 붙일 수 있다.
`다음 거래일까지`는 체결 가능한 현재/다음 세션과 그 다음 세션까지, 즉 2거래일로
정규화한다. 기간은 요청 접수 시각이나 주문 접수 시각이 아니라 **즉시 PAPER 매수가
전량 체결되어 bundle이 ACTIVE가 되는 시점**에서 시작한다.

worker는 `reference.market_sessions`의 최신 KRX `REGULAR` 세션만 읽어 N번째
`closes_at`을 rule의 runtime `expires_at`으로 기록한다. 따라서 주말과 공식 휴장일을
세지 않는다. 해당 캘린더가 없거나 요청한 N번째 세션을 공급하지 못하면 worker는
보호 주문을 임의 만료로 활성화하지 않고 rule과 bundle을 `FAILED`로 종료한다. 전량
체결 전에는 14일의 제한된 pending-entry 복구 창만 적용되며, 이것은 사용자가 요청한
보유 기간이 아니다. `N거래일`을 생략하면 기존과 동일하게 당일 KRX 정규장 마감
기본값을 사용한다.

### 보호 청산 활성화 차단

즉시 PAPER 주문이 `COMPLETED`여도, N거래일 청산의 공식 KRX `closes_at`을 계산하지
못하면 보호 SELL rule을 `ACTIVE`처럼 남겨 두지 않는다. 캘린더 행이 없거나 시간대가
손상된 경우 worker는 rule과 bundle을 `FAILED`로 끝내고
`BUNDLE_ACTIVATION_BLOCKED` lifecycle/outbox event를 남긴다. 이 종료는 재시도 대기나
보정 주문을 만들지 않는다.

Discord 소비자는 event의 Redis payload를 신뢰하지 않고 lifecycle event와 bundle의 원래
즉시 주문을 다시 확인한 뒤, `보호 청산 규칙: 활성화하지 않음`, `추가 주문 생성: 없음`을
보고한다. 사용자는 KRX 캘린더 복구 후 현재 보유·주문 상태를 확인하고 새 PAPER 전략을
배포해야 한다. 이 보고도 진입 주문의 체결이나 현재 잔고를 추정하지 않는다.

### 보호 청산 활성화 확정

즉시 PAPER 주문이 전량 완료되어 보호 rule을 `ACTIVE`로 전환하면 worker는
`BUNDLE_ACTIVATED` lifecycle event와 outbox를 같은 트랜잭션에 기록한다. Discord 소비자는
Redis payload를 신뢰하지 않고 DB에서 rule의 현재 상태, action side, runtime `expires_at`,
활성화 event와 원래 bundle 요청을 다시 읽는다. 상태가 여전히 `ACTIVE`일 때만 `보호 청산:
SELL 조건 감시 중`, 실제 보호 만료 시각, `N거래일` 추적 기간(지정한 경우), `추가 주문 생성:
없음 (조건 충족 전)`을 보고한다.

outbox 전달이 늦어 이미 `EXPIRED`·`FAILED`·`COMPLETED` 등으로 바뀐 rule은 활성화 보고를
억제한다. 그 경우 뒤따르는 terminal event가 권위 있는 결과이며, 활성화 확인 자체는 청산
주문 생성·청산 체결·현재 잔고를 주장하지 않는다.

### 조건 충족 감지와 제출 전 상태

worker가 `TRUE` evaluation을 한 번만 claim하면 `TRIGGER_CLAIMED` lifecycle event와 outbox를
같은 트랜잭션에 기록한다. Discord 소비자는 Redis의 trigger id나 주문 주장을 신뢰하지 않고,
DB에서 rule·trigger·`TRUE` evaluation의 실제 `data_watermark`와 현재 rule 상태를 다시 읽는다.
상태가 `TRIGGERED` 또는 `EXECUTION_PENDING`일 때만 “조건 충족 감지”, 실행 방향, 조건 데이터
시각, `PAPER 주문 guard 검증 중` 또는 `PAPER 주문 제출 준비 중`을 보고한다.

이 알림은 **조건이 참이었다는 증거**일 뿐 Trading이 주문을 접수했거나 broker가 체결했다는
결과가 아니다. `DIRECTIVE_SUBMITTED`의 후속 권위 상태 보고가 주문 제출·체결·회계 반영을
알린다. 전달이 늦어 rule이 이미 `COMPLETED`·`FAILED`·`EXPIRED` 등으로 끝난 경우 trigger
보고는 억제해 종료 결과와 모순되는 중간 상태를 보내지 않는다.

### 만료 후 미청산 상태

worker는 만료된 `PENDING_CONFIRMATION`/`ACTIVE`/`PAUSED` rule을 행 잠금 후
`EXPIRED`로 전환하고, 매 rule마다 `CONDITIONAL_RULE_EXPIRED` lifecycle event와
outbox를 한 트랜잭션에 기록한다. 연결된 진입-청산 bundle은
`CONDITIONAL_EXIT_EXPIRED`로 `FAILED`가 된다. 이 전환은 **새 PAPER 주문을 만들지
않는다.**

Discord 소비자는 Redis payload의 주문·잔고·만료 주장을 신뢰하지 않고 rule event,
version, bundle, 원래 즉시 주문을 다시 조회한다. 사용자에게 `EXPIRED`, 실제 만료 시각,
`추가 주문 생성: 없음`을 알린다. 활성 상태의 매수 후 SELL 보호 규칙이었다면 “보유분이
남아 있을 수 있으나 자동 매도는 실행하지 않는다”고만 말하며, 이 event만으로 잔고나
체결을 추정하지 않는다. 프론트 주문 상태에도 같은 `EXPIRED` 한국어 사유와
`effective_expires_at`이 표시된다.

### 매수 후 트레일링 청산

즉시 MARKET 매수 뒤 `매수가 대비 X% 수익 이후 고점 대비 Y% 하락하면 매도`를 명시하면
동일한 full-fill bundle이 하나의 root `TRAILING_STOP` SELL rule을 활성화한다. 활성 수익률
`X`와 drawdown `Y`는 모두 명시해야 하며, 수량은 즉시 매수 수량과 같거나 생략해야 한다.
최고가는 ACTIVE 이후 장중의 신선한 현재가만 durable DB state에 기록하고, 늦게 도착한
과거 시세는 무시한다.

이 entry-originated trail에는 `EXPECTED_POSITION_QUANTITY`가 서버에서만 추가된다. 현재
보유수량이 이번 즉시 매수 수량과 정확히 같은 경우에만 high-water tracking과 청산을
시작한다. 따라서 기존 보유분 또는 별도 주문으로 동일 종목 수량이 섞이면 계좌 평균
매입가를 새 진입가처럼 사용해 매도하지 않는다.

최초 high-water 관측 뒤에는 이 수량 일치가 포지션 수명주기 불변식이다. 이후 수동
매매·별도 주문으로 수량이 달라지면 rule은 `CANCELLED`, bundle은 `FAILED`로 영구
종결한다. `ENTRY_POSITION_QUANTITY_MISMATCH` 감사 event와 outbox event를 남기며,
나중에 수량이 우연히 다시 같아져도 자동 매도를 재개하지 않는다. 단, 최초 관측 전의
불일치는 체결/잔고 반영 지연일 수 있으므로 고점 상태를 만들지 않고 대기한다.

`ENTRY_POSITION_MISMATCH` outbox event는 Discord 상태 소비자가 처리한다. Redis의
수량·계좌·workflow 메타데이터는 신뢰하지 않고, `conditional_trade_rule_events` 및
bundle이 연결한 즉시 PAPER 주문에서 다시 읽는다. 사용자에게는 `CANCELLED`, 진입/현재
수량, "추가 매도 주문 없음", 재배포 필요를 알린다. 이 알림은 주문 제출 경로를 호출하지
않는다. 프론트의 `/ui/paper-order-requests/{order_request_id}`도 즉시 주문 ID에서 bundle의
파생 rule을 다시 연결해 같은 `CANCELLED` 상태와 한국어 중단 사유를 표시한다.

## 런타임과 자격

- BFF: `hgfinance_conditional_orchestrator` LOGIN →
  `svc_conditional_rule_orchestrator`
- Worker: `hgfinance_conditional_worker` LOGIN → `svc_conditional_rule_worker`
- Trading: 기존 `svc_trading_api`, 별도
  `trading.conditional_rule.execute` short-lived token 검증

Worker 이미지에는 agent CLI, browser/BFF runtime, broker credential이 없다. AWS가 꺼진
상태에서는 코드를 main에 준비만 하고, 실제 migration/container activation은 release
deployer로 수행한다. Discord의 자연어 생성/확정 대화는 exact hash를 사용자에게 먼저
보여 주는 2단계 ingress가 연결되기 전에는 활성화하면 안 된다.

`3M`/`10M`/`30M` 또는 OCO를 운영 환경에서 사용하려면
`20260829000100_conditional_rule_intraday_oco_contract.sql`을 먼저 적용해야 한다.
이 migration은 DB의 primary timeframe 제약을 코드 계약과 일치시키고, OCO 실행 시
version JSON의 group ID를 찾는 worker 인덱스를 만든다. 적용 전에는 새 코드를 배포해도
해당 조건이 DB INSERT에서 거절되거나 OCO arbitration이 불필요하게 전체 버전을 스캔할 수 있다.

트레일링 청산에는 그 다음 migration
`20260829000200_conditional_rule_trailing_stop_state.sql`도 필요하다. 이는 worker만
쓰기 가능한 high-water 상태를 만들며, AST나 컨테이너 메모리에 최고가를 보관하지 않는다.

전량 체결 뒤 N거래일 추적에는
`20260829000300_conditional_rule_activation_lifetime.sql`도 필요하다. 이 migration은
worker에 KRX 세션의 최소 읽기 권한과 runtime `expires_at` 열만의 갱신 권한을 부여한다.
rule AST/version을 고치거나 새 rule을 만드는 권한은 추가하지 않는다.

## 검증

```bash
python -m pytest -q tests/conditional_rules tests/api/test_conditional_rules.py \
  tests/schema/test_conditional_trade_rule_schema.py
docker compose --env-file <private-env> config --quiet
docker compose --env-file <private-env> build trading-api conditional-rule-worker
```

### 조건주문 스트레스 회귀

PAPER 조건주문은 실제 broker 호출 없이 다음 불변식을 회귀로 검증한다.

- 동일 evaluation key의 중복 전달은 하나의 `TRIGGER_CLAIMED`/outbox와 하나의 실행만 만든다.
- `max_workers=1`을 포함해 한 규칙의 예상 밖 런타임 예외가 같은 batch의 뒤 규칙을 중단시키지 않는다.
- 제출 중 프로세스가 멈춰도 `SUBMITTING` lease와 실행 idempotency key로 재시도하며, OCO loser는 외부 Trading API를 호출하지 않는다.
- 늦은 quote는 trailing high-water를 되돌리거나 매도를 유발하지 않으며, 진입 후 보유 수량 드리프트는 자동 청산을 영구 중단한다.
- Redis outbox event의 payload는 모두 비권위 입력이다. 활성화·조건 충족·만료·중단 보고는 DB 현재 상태가 맞을 때만 보내며, 늦은 중간 event는 억제한다.
