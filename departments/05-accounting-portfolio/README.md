# 회계/포트폴리오본부 (Accounting & Portfolio)

## Mission

Ledger, Position, Cash, NAV와 Reconciliation을 담당한다. 승인된 주문의 체결·포지션·현금 반영,
Reconciliation과 PnL 계산을 수행한다. Accounting Engine의 공식 수치만 사용한다.

회계본부가 Signal을 생성하지 않는다. CEO는 원장 수정, NAV 확정 권한이 없다
(`CLAUDE.md` "절대 깨면 안 되는 권한 분리" 참고).

## Owner

도현님 — [TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE](../../docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md)

## 입력·출력 계약

- 입력: OMS/브로커 체결(Fill) — 트레이딩본부(`departments/02-trading/`)가 소유하는 계약을 소비
- 출력: 분개(Journal), Position/Cash Projection, Reconciliation Break → `workflow` step 6 CEO로 전달

## 실행법

```bash
accounting-portfolio-department chat -q 'Reconcile fills and compute PnL'
python departments/05-accounting-portfolio/ledger/ledger.py
python departments/05-accounting-portfolio/reconciliation/reconciliation.py
```

## 테스트

- `ledger/ledger.py` — 원장 불변식 10개 자체 점검
- `reconciliation/reconciliation.py` — 대사 12개 자체 점검

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- `ledger/` — 이중분개 원장과 Position/Cash Projection(Sprint D2). 구 경로 `accounting/ledger.py`는
  2026-07-30에 삭제됐다
- `reconciliation/` — OMS/Fill/Ledger Reconciliation(Sprint D2). 구 경로 `accounting/reconciliation.py`는
  2026-07-30에 삭제됐다
- D2 Prototype 단계이며 팀 가이드 v1.2 반영 전 재작업 예정
