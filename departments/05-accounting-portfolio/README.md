# 회계/포트폴리오본부 (Accounting & Portfolio)

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
Local Ollama Alias는 [`Modelfile`](Modelfile)의 `qwen2.5` 기반 `agent-accounting`이고 Hermes Profile은 `accounting-portfolio-department`다. Build·Eval·권한 기준은 [Ollama Department Modelfile Guide](../../docs/02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)를 따른다.

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
- 출력: `portfolio-api` — `GET /accounting/v1/portfolio-snapshot?fund_id=&as_of=` →
  `{snapshot_id, as_of}`. CEO Daily Report의 `SnapshotRef(portfolio)` 원천이다.
  **수치는 주지 않는다** — 참조만 넘기고 값은 원장이 소유한다

## 실행법

```bash
accounting-portfolio-department chat -q 'Reconcile fills and compute PnL'
python departments/05-accounting-portfolio/ledger/ledger.py
python departments/05-accounting-portfolio/portfolio/portfolio.py
python departments/05-accounting-portfolio/portfolio/ui_read_model.py
python departments/05-accounting-portfolio/reconciliation/reconciliation.py
python departments/05-accounting-portfolio/corporate_actions/corporate_actions.py
python departments/05-accounting-portfolio/reporting/daily_report.py
python apps/api/main.py
```

## 테스트

- `ledger/ledger.py` — 원장 불변식 10개 자체 점검
- `reconciliation/reconciliation.py` — 대사 12개 자체 점검
- `corporate_actions/corporate_actions.py` — F25 Corporate Action 13개 영역
- `reporting/daily_report.py` — F23 Daily Report 14개 영역
- `portfolio/ui_read_model.py` — OMS·Ledger·Portfolio DEMO Snapshot 계약
- `apps/api/main.py` — `/health`, `/ui/snapshot`, 부서별 Agent 경로 BFF 7개 영역 점검

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- `ledger/` — 이중분개 원장과 Position/Cash Projection(Sprint D2). 구 경로 `accounting/ledger.py`는
  2026-07-30에 삭제됐다
- `reconciliation/` — OMS/Fill/Ledger Reconciliation(Sprint D2). 구 경로 `accounting/reconciliation.py`는
  2026-07-30에 삭제됐다
- `corporate_actions/` — F25. 배당·분할·종목변경 분개. **공시(Announcement)로는 분개하지 않고**
  `EFFECTIVE`만 반영하며, 선택형 Action은 `approval_id` 없이 거부한다. `action_id`가 멱등 키다
- `reporting/daily_report.py` — F23. 하루치 PnL·Drawdown·비용·오류. 수치를 새로 만들지 않고
  스냅샷·원장 확정값의 차이만 낸다. `NAV 변화 = 순손익 + 자본유출입` 항등식을 매번 검산하고
  안 맞는 만큼을 `unexplained_pnl`로 노출한다(0으로 반올림해 없애지 않는다).
  전부 Preliminary이며 `is_official`은 항상 False다 — Official NAV 확정 권한이 회계본부에 없다
- `portfolio/ui_read_model.py` — 공식 수치를 다시 계산하지 않고 화면 계약으로 옮기는 DEMO Projection
- `apps/api/main.py` — 공통 Frontend Platform의 Read-only DEMO BFF (조립만)
- `apps/api/accounting.py` — 회계본부 Router. `POST /accounting/agent/ask`가 이 본부 Hermes Profile
  하나만 부른다. 부서 이름을 요청 Body로 받지 않으므로 다른 본부 Agent를 부를 경로가 없다(5.6).
  Auth·Tool Allowlist 전까지 `ENABLE_AGENT_ASK` 없이는 503
- D2 Prototype 단계이며 팀 가이드 v1.2 반영 전 재작업 예정
