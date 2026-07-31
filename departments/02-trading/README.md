# 트레이딩본부 (Trading)

## Mission

Trade Case, Signal, OrderIntent 생성과 집행을 담당한다. Research Packet을 기반으로 Bull/Bear 토론 후
Trader/PM Agent가 진입·청산·크기·무효화 조건을 갖춘 구조화된 거래 제안(OrderIntent)을 만든다.

`trader-pm-agent`는 주문을 직접 전송하지 않는다. Risk/Compliance Gate 통과가 선행 조건이다
(`CLAUDE.md` "절대 깨면 안 되는 권한 분리" 참고). Agent Decision ≠ Strategy Signal ≠ OrderIntent ≠ Order.

## Owner

도현님 — [TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE](../../docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md)

## 입력·출력 계약

- 입력: 리서치본부 Research Packet
- 출력: OrderIntent → `workflow` step 3 리스크본부로 전달. Risk 승인 후 `oms/`가 상태를 관리하고
  `broker/`가 모의 체결한다.

## 실행법

```bash
trading-department chat -q 'Propose a trade for [종목]'
python departments/02-trading/contracts/contracts.py
python departments/02-trading/oms/oms.py
python departments/02-trading/broker/paper_broker.py
python departments/02-trading/multileg/intent_group.py
python departments/02-trading/capability/derivatives.py
```

## 테스트

- `contracts/contracts.py` — 계약 8개 영역 자체 점검
- `oms/oms.py` — OMS 불변식 11개 자체 점검
- `broker/paper_broker.py` — Paper Broker 8개 영역 자체 점검
- `multileg/intent_group.py` — F30 Multi-leg 13개 영역
- `capability/derivatives.py` — F31 Derivatives Capability 14개 영역

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- `contracts/` — OrderIntent / RiskDecision / EventEnvelope 계약(Sprint D0), `philosophies.yaml`(철학별 집행 프리셋)
- `oms/` — 결정론적 OMS(Sprint D1). 구 경로 `execution/oms.py`는 2026-07-30에 삭제됐다
- `broker/` — Paper Broker(Sprint D1). 구 경로 `execution/paper_broker.py`는 2026-07-30에 삭제됐다
- `multileg/intent_group.py` — F30. Pair·Basket·Hedge·Roll처럼 여러 Leg가 하나의 의도인 주문.
  핵심은 주문 생성이 아니라 **부분 체결 복구 판정**이다 — ALL_OR_NONE에서 일부만 체결되면
  COMPLETED가 아니라 `PARTIAL_RECOVERY`이고, 복구 수단이 없으면 `FAILED_SAFE`로 떨어져
  신규 진입이 막힌다. 판정만 하고 실제 취소·청산은 OMS가 한다
- `capability/derivatives.py` — F31. **파생상품 거래를 켜는 코드가 아니다.** 팀 가이드 1.1대로
  계약 모델과 접수 차단 게이트를 만든다. Broker·Risk·Accounting 3개 Certification이 전부
  서명돼야 파생·공매도 주문이 통과하고, 그 전에는 Risk 승인이 있어도 접수 단계에서 막힌다.
  명목금액은 반드시 승수를 곱한다 — 빼먹으면 한도가 조용히 느슨해진다
- D0-D2 Prototype 단계이며 팀 가이드 v1.2(상태 머신 2단 분리, Multi-Strategy) 반영 전 재작업 예정
