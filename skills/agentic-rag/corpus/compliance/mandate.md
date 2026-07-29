---
document_id: policy-mandate-001
document_type: mandate
version: "1.1.0"
effective_from: "2026-07-29"
effective_to: null
status: ACTIVE
---

# Investment Mandate

v1.0.0은 파이프라인 검증용 placeholder였으므로 제자리에서 교체했다. v1.1.0부터는
아래 Approval Requirements의 버전 규칙(이전 버전을 수정하지 않고 새 버전을 발행)을 적용한다.

## Objective
Capital preservation first; growth second. Paper-trading phase only (HEDGE_FUND_MASTER_PLAN.md 2.4).

## Capital
- Fund: `Paper Fund I` (KRW), 초기 납입 자본 1,000,000,000원.
- 원장 기준값은 `db/004_seed.sql`의 자본 납입 분개다. 이 문서와 원장이 어긋나면 원장이 사실이다.
- 초기 구조: Fund 1 / Pod 1 / Book 1 (`KR Equity Long Book`). 데이터 모델은 다중 Fund·Book·Strategy를 지원한다 (5.7).

## Allowed Assets
- Listed equities on KRX — **KOSPI and KOSDAQ** — and the market's representative index derivatives (2.3).
- No OTC or exotic derivatives (2.2 excluded scope).

## Tradability Filter
거래 가능 종목은 아래를 모두 만족해야 한다. 미달 종목은 신규 진입 대상이 아니다.
- 20일 평균 거래대금 300,000,000원 이상.
- 주가 1,000원 이상.
- 관리종목·거래정지·투자경고 지정 종목 제외.

## Position Limits
- Per-symbol target weight cap: 3% of NAV (2.3).
- Total gross exposure cap: 20% of NAV, subject to revision after validation (2.3).
- 초기 최대 보유 종목: 5~10개 (2.3).
- Maximum single-order step: no more than a 20 percentage-point change in target weight per decision (see risk-supervisor step-size backlog item — not yet enforced, referenced here for future retrieval testing).

## Transaction Costs
집행 계획과 Paper 체결에 적용하는 가정치다. Limited Live 전에 실제 Broker 요율로 교체한다.
- 위탁수수료: 편도 1.5bp.
- 매도 시 증권거래세 + 농어촌특별세: 15bp.
- 매수에는 거래세가 없다.

## Forbidden Actions
- No short selling in the initial Long-only MVP — `open_short` is disabled at the policy layer (10, 12.5).
- No leverage beyond what the Risk Engine's approved margin rules allow.
- No trading during `ENTRY_BLOCKED` or `HALTED` Kill Switch states (11.3).
- 시장 데이터 품질이 `ok`가 아니면(stale / wide / suspect) 신규 주문 후보를 생성하지 않는다 (11.2).

## Undecided (미확정 — 확정 전까지 이 항목으로 판정하지 말 것)
- 일일 손실 한도와 최대 Drawdown 한도 (27장 미확정 항목).
- 전략별 Risk Budget과 최대 동시 Champion 수.
- 관리보수·성과보수·High-Water Mark 적용 여부.

## Approval Requirements
- Any change to this Mandate requires a new Version with `effective_from`; the previous Version is never edited in place.
- Mandate-exceeding orders require CEO + Risk department approval (5.6 권한 분리 원칙).
