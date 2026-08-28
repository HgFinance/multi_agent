# Strategy Hermes 배포 계약

상태: 현행 운영 계약 v2 (2026-08-28)

이 문서는 Strategy Hermes가 연구 결과를 배포 경로에 넘길 때 지켜야 하는
경계다. Hermes는 연구자이며 Docker·브로커·OMS를 직접 조작하지 않는다.

## Hermes가 남기는 것

연구가 끝나면 다음 순서로 같은 lab에 기록한다.

1. `plans/<plan_id>.json`에 전략 서명, 유니버스, 기간, 데이터 경계,
   비용모델과 최신 실제 반환 봉을 기록한다.
2. 실제 백테스트를 실행하고 `results/<plan_id>.json`에 `metrics`, OOS,
   누수 여부, robustness, 실패 원인과 제한사항을 기록한다.
3. 배포 가능한 경우에만 `candidate.json`과 `CANDIDATE` 결정을 기록한다.
   `BLOCKED`, `FAILED`, `PAUSE`, `PIVOT`, `REVIEW_REQUIRED` 결과는 후보로
   위장하지 않는다.
4. 후보의 종목·기간·전략 서명은 결과와 정확히 일치시킨다. 실행 코드를
   Bundle에 넣거나 임의 이미지·호스트 경로를 제안하지 않는다.

## 사람이 배포를 요청한 뒤의 자동 흐름

```text
자연어 배포 요청
  → BFF가 request_id + 종목 범위를 연구 lab에서 확인
  → 배포 기록을 AWAITING_APPROVAL 또는 REVIEW_REQUIRED로 저장
  → 사람이 백테스트 요약을 보고 PAPER 승인
  → BFF가 immutable PAPER Bundle 생성
  → private strategy-runtime-control이 고정 이미지로 child 컨테이너 생성
  → 컨테이너가 TimescaleDB 원시 체결·호가를 읽고 확정 3분봉 생성
  → 조건 충족 시 배포별 토큰으로 runtime-control → Trading PAPER directive
    → configured LS PAPER adapter에 실제 모의주문 제출
  → 런타임 상태·컨테이너 ID·신호 수를 조회해 보고
```

사람의 자연어 `전략 배포해줘`는 배포 요청일 뿐 승인으로 간주하지 않는다.
`전략 배포 승인해줘`와 같은 명시적 승인 뒤에만 PAPER Bundle을 만든다.
`PIVOT` 또는 `REVIEW_REQUIRED`를 최상위 승인자가 예외 승인하는 경우에는
그 사실·사유·승인자를 audit에 남기고 같은 PAPER 경로로 진행한다.

## 고정 PAPER 실행기 계약

- 실행 이미지는 `hedgefund-operations-runtime:latest` 하나만 사용한다.
- 컨테이너 이름은 `strategy-paper-<deployment_id>`로 서버가 계산한다.
- child는 `STRATEGY_PAPER_NETWORK`의 private Compose 네트워크에 직접 붙는다.
  컨트롤 컨테이너의 network namespace를 공유하지 않아 `timescaledb`와
  `market-api` 서비스 DNS가 재시작 후에도 유지된다.
- Bundle은 `PAPER`, `SMA_ALIGNMENT`, `3M`, `SMA 5/20/60`, 명시된 2% 익절만
  지원한다. 다른 서명은 배포하지 않고 `REVIEW_REQUIRED`로 둔다.
- `market.market_ticks`의 체결을 OHLCV 원천으로 사용하고, `market.market_quotes`
  의 최신 호가·스프레드는 관측 및 체결 품질 검증용으로만 사용한다.
- 각 폴링은 마지막 체결 워터마크에서 3분 겹침을 다시 읽는다. 빈 구간을
  가격 0 또는 직전 가격으로 채우지 않으며, 현재 진행 중인 3분봉은 신호에
  사용하지 않는다.
- 기본 원천 체결 lookback은 24시간이다. 장중 누적 데이터만으로도 SMA 60개를
  준비할 수 있어야 하며, 부족하면 신호를 만들지 않고 `WAITING_FOR_MARKET_DATA`
  로 남긴다.
- DB 계정은 `strategy_paper_reader` 같은 SELECT 전용 계정이어야 한다.
  DB DSN, LS 키, Docker 소켓, 브로커 자격증명은 Hermes에 전달하지 않는다.
- `STRATEGY_PAPER_ORDERS_ENABLED=true`인 PAPER Bundle은
  `execution_status=PAPER_ORDERING`, `orders_enabled=true`이며 조건 충족 시
  실제 PAPER 주문을 제출한다. 주문 수량은 서버 설정
  `STRATEGY_PAPER_ORDER_QUANTITY`(기본 1주), 계좌 범위는
  `STRATEGY_PAPER_USER_ID/FUND_ID/BOOK_ID`로 고정한다.
- 전략 child에는 LS 키를 주지 않는다. child는 deployment-bound 토큰으로
  runtime-control만 호출하고, runtime-control이 짧은 내부 서비스 인증으로
  Trading API에 전달한다. Trading의 기존 PAPER directive가 세션·최신 호가·
  현금/보유수량·호가단위·멱등성 검사를 수행한 뒤 `TRADING_BROKER_ADAPTER`
  (`ls-paper` 또는 명시적 local `paper`)로 라우팅한다.
- 하위 호환을 위해 `orders_enabled=false`, `SIGNAL_ONLY` Bundle도 읽을 수
  있지만 실제 주문을 만들지 않는다. 활성 Bundle을 PAPER_ORDERING으로
  전환하려면 명시적인 사람 승인으로 재기동한다.

## 완료 보고에 반드시 포함할 항목

`ACTIVE`라고 보고하려면 다음을 모두 확인한다.

- `research_status`, 결과 판정, 승인자와 승인 유형
- Bundle hash와 전략 서명
- 데이터 원천이 `TIMESCALE_RAW_TICKS_QUOTES`인지 여부
- 종목별 원시 체결 조회·확정 3분봉 수·마지막 3분봉 시각
- 컨테이너 ID와 런타임 상태가 최신 조회값인지 여부
- `PAPER_ORDERING` 또는 `SIGNAL_ONLY`, `orders_enabled`, 주문 수량·계좌 범위
- PAPER directive ID·주문/체결 상태·오류 수와 최근 오류

확인되지 않은 값은 0이나 성공으로 쓰지 말고 `미확인` 또는 `BLOCKED`로
보고한다. 연구 결과가 완료됐다는 사실만으로 PAPER 컨테이너가 실행 중이라고
말하지 않는다.
