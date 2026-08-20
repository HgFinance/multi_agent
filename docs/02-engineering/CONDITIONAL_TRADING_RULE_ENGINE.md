# Conditional PAPER Rule Engine

이 문서는 구현된 v1 백엔드 계약과 운영 경계를 요약한다. 프론트엔드 계약이나 LIVE
권한은 포함하지 않는다. 권한 결정은
[ADR-0008](adr/0008-authenticated-conditional-paper-rule-authority.md)을 따른다.

## 흐름

```text
authenticated user
  -> BFF preview (untrusted AST candidate)
  -> schema + semantic + ambiguity validation
  -> exact spec_sha256 confirmation
  -> conditional-rule-worker
  -> Market API final candles + Trading canonical portfolio context
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

모든 경로는 기존 BFF 사용자 인증과 Fund/Book access check를 사용한다.

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
- timeframes: `1M`, `5M`, `15M`, `1H`, `1D`
- indicators: SMA, EMA, RSI, MACD, Bollinger Bands, volume average, ATR, ADX
- expressions: arithmetic, comparison, cross above/below, AND, OR, NOT
- portfolio facts: quantity, sellable quantity, average entry, market value, NAV,
  weight, unrealized PnL, PnL ratio, available cash
- actions: fixed-share BUY/SELL, position-percent SELL, ALL SELL
- hard limits: `PAPER`, `ONCE`, market order, DAY, market-closed reject

`5M`/`15M`/`1H` candle은 Market API가 final `1M` rows에서 같은 Timescale bucket
규칙으로 생성한다. 중복 또는 분 단위 내부 공백이 있는 bucket과 partial candle은
Indicator Engine에서 사용하지 않는다.

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

## 검증

```bash
python -m pytest -q tests/conditional_rules tests/api/test_conditional_rules.py \
  tests/schema/test_conditional_trade_rule_schema.py
docker compose --env-file <private-env> config --quiet
docker compose --env-file <private-env> build trading-api conditional-rule-worker
```
