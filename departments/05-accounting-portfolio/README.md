# 회계/포트폴리오본부 (Accounting & Portfolio)

> 현재 LLM 직원 런타임은 독립 LangGraph Worker + Ollama `qwen3:1.7b`이고, `back-office-runner`는 모델을 호출하지 않는 결정론 실행기다. 이 기준이 아래의 과거 Modelfile 설명보다 우선한다.

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
현재 Head 런타임은 Hermes Profile `accounting-portfolio-department` + `openai-codex/gpt-5.6-luna`이며, 승인된 Claude Code를 대체 런타임으로 사용할 수 있다. 직원은 2명으로 개편됐다: `exception-investigation-worker`는 Ollama LLM, `back-office-runner`는 LLM 없는 결정론 Runner다. `Modelfile`의 `qwen3:14b`/`agent-accounting` alias와 구 8개 역할명은 로컬·역사적 호환용이며 현재 Worker 기준이 아니다. Build·Eval·권한 기준은 [Ollama Department Modelfile Guide](../../docs/02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)를 따른다.
현재 실행 상태와 도현님 2주 계획·Daily Scrum은 [실행 현황과 통합 계획 v2.2](../../docs/PROJECT_IMPLEMENTATION_STATUS.md#42-도현님-트레이딩본부-회계포트폴리오본부와-공통-platform)을 따른다.

## Mission

Ledger, Position, Cash, NAV와 Reconciliation을 담당한다. 승인된 주문의 체결·포지션·현금 반영,
Reconciliation과 PnL 계산을 수행한다. Accounting Engine의 공식 수치만 사용한다.

회계본부가 Signal을 생성하지 않는다. CEO는 원장 수정, NAV 확정 권한이 없다
(`CLAUDE.md` "절대 깨면 안 되는 권한 분리" 참고).

## Current Worker 구성

| Worker | 방식 | 역할 |
|---|---|---|
| `exception-investigation-worker` | LLM | Reconciliation Break, 미설명 PnL, 마감 준비 상태의 원인 후보를 조사하고 근거를 연결 |
| `back-office-runner` | 결정론 | Position·Cash·PnL·Reporting·Valuation·Corporate Action·Fee/Tax 관련 결정론 결과 조회·투영 |

기존 `portfolio-control`, `ledger-reconciliation`, `nav-close`, `treasury-liquidity`, `pnl-attribution`,
`investor-reporting`, `valuation-corporate-actions`, `fee-accrual-tax` 역할은 두 Worker와 결정론 Accounting
Engine으로 흡수됐다. 공식 수치 계산·Journal Posting·Official NAV 확정 권한은 Worker에 없다.

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
python departments/05-accounting-portfolio/reconciliation/break_triage.py
python departments/05-accounting-portfolio/corporate_actions/corporate_actions.py
python departments/05-accounting-portfolio/reporting/daily_report.py
python departments/05-accounting-portfolio/query_router.py
python departments/05-accounting-portfolio/nav_close_memory.py
python departments/05-accounting-portfolio/employee_workers.py
python departments/05-accounting-portfolio/scripts.py
python departments/05-accounting-portfolio/api/app.py
python apps/api/main.py

uvicorn app:app --app-dir departments/05-accounting-portfolio/api   # Domain API 실행

docker compose up -d accounting-api        # 컨테이너로 (127.0.0.1:8046, 로컬 전용)
curl http://127.0.0.1:8046/health
```

## 테스트

- `ledger/ledger.py` — 원장 불변식 10개 자체 점검
- `portfolio/portfolio.py` — Portfolio/NAV 12개 영역
- `reconciliation/reconciliation.py` — 대사 12개 자체 점검
- `reconciliation/break_triage.py` — Break 원인 후보·Aging/SLA 9개 영역 (실 DB 이력 조회 포함)
- `corporate_actions/corporate_actions.py` — F25 Corporate Action 13개 영역
- `reporting/daily_report.py` — F23 Daily Report 14개 영역
- `query_router.py` — 질의 Level 분류 8개 영역
- `nav_close_memory.py` — 마감 Layered Memory 9개 영역
- `employee_workers.py` — 직원 근거 주입·인용 검증 6개 영역
- `scripts.py` — 마감 파이프라인 20개 영역 (Hermes·네트워크 없음)
- `portfolio/ui_read_model.py` — OMS·Ledger·Portfolio DEMO Snapshot 계약
- `api/app.py` — Domain API 16개 영역 (TestClient. 네트워크·DB 없음)
- `apps/api/main.py` — `/health`, `/ui/snapshot`, 부서별 Agent 경로 BFF 7개 영역 점검

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- `ledger/` — 이중분개 원장과 Position/Cash Projection(Sprint D2). 구 경로 `accounting/ledger.py`는
  2026-07-30에 삭제됐다
- `reconciliation/` — OMS/Fill/Ledger Reconciliation(Sprint D2). 구 경로 `accounting/reconciliation.py`는
  2026-07-30에 삭제됐다
- `reconciliation/break_triage.py` — Break Triage(2026-08-05). **판정을 하지 않는다** —
  Break 생성과 Severity는 `reconciliation.py`가 그대로 유지하고, 여기는 이미 만들어진
  Break에 원인 후보와 과거 해소 사례를 붙인다. 코퍼스는 `accounting.breaks`의 실제 해소
  이력이고, 이력이 없는 동안은 `accounting_ops.yaml`의 원인 분류표가 Cold Start 근거다
  (이력이 쌓이면 이력이 분류표를 이긴다). **Aging/SLA는 LLM을 안 부른다** — Severity별
  기한을 넘긴 Break를 결정론으로 `OVERDUE`로 만든다. 조용히 늙는 Break가 제일 위험하다
- `query_router.py` — 회계 질의 Level 분류(2026-08-05). L0/L1/L2/L3와 모델 등급을 결정론으로
  정한다. **L0은 모델을 아예 안 부르고** 원장 읽기 경로를 알려준다 — 제일 싼 모델은 안 부르는
  모델이고, 덤으로 원장 수치가 LLM 문장을 거치지 않는다. 등급→모델 표는 지금 셋 다 같은
  모델을 가리키며(`tier_routing_approved: false`) 배선만 먼저 깔아둔 상태다.
  직원 Worker 모델은 이 라우팅과 무관하다 — `employee_runtime`이 소유하고
  [WORKER_MODEL_MATRIX](../../docs/02-engineering/WORKER_MODEL_MATRIX.md) 절차를 따른다.
  지식 그래프는 **만들지 않는다.** L2가 관계형으로 안 풀린다는 것이 실측될 때만 논의를 연다
- `nav_close_memory.py` — 마감 Layered Memory(2026-08-05, FinMem 계층 구조). shallow/
  intermediate/deep 반감기와 relevance×recency×importance 검색, 접근 강화에 따른 계층 승격.
  **기억 계층과 System of Record를 섞지 않는다** — `MemoryEntry`에 금액 필드가 아예 없고,
  수치가 들어간 문장은 `remember()`가 거부한다(금액 대신 `refs`로 원본 id를 가리킨다).
  `is_official`은 어떤 회상 뒤에도 False다 — NAV 확정은 기억이 아니라 승인 절차다
- `accounting_ops.yaml` — 위 셋의 튜닝값(SLA 시간, 원인 분류표, Level 규칙, 반감기)
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
- `api/` — Domain API(FastAPI). 위 모듈을 감싸기만 하고 **새 회계 판정 로직이 없다.**
  Hermes는 이 API/MCP 경계로만 부른다(같은 프로세스에 import하지 않는다).
  **`PUT`·`PATCH`·`DELETE`가 하나도 없다** — 불변식 2를 라우팅 표로 집행한 것이고,
  자체 점검이 라우팅 표를 훑어 이를 강제한다. 정정은 `/reverse` 하나뿐.
  설계서: [Unified Domain API Specification](../../docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md)
- D2 Prototype 단계다. 남은 것은 저장소 — 원장이 아직 프로세스 메모리이고
  `accounting.*` 연결은 트레이딩 OMS와 같은 미결 항목이다(설계서 4절)
