# AI Office 작업 지침

`ai-office/`는 HgFinance 헤지펀드 운영 상태를 보여주는 픽셀 오피스 Projection이다. 금융 원장·Broker credential·Risk 한도·Hermes 내부 상태를 브라우저가 소유하지 않는다. 안내 문구는 쉬운 한국어로 작성한다.

## 조직 구조

`company.config.ts`의 `DEPARTMENTS`와 `app/game/world.ts`가 기준. 8개 부서 ID(SNS 데모의 `brand`/`reels`/`carousel`/`partner`는 되살리지 않음):

| ID | 본부 |
|---|---|
| `research` | Research |
| `strategy1` | Quant/Backtest |
| `strategy2` | Trading |
| `ops` | Risk |
| `finance` | Accounting/Portfolio |
| `qa` | AI QA/Audit |
| `review` | Agent Workforce/HR |
| `secretary` | CEO Office |

부서 추가·ID 변경 시 `company.config.ts`, `world.ts`, `staff.ts`, runtime department mapping, 테스트를 함께 갱신한다.

## 화면

- `라이브 오피스`: 포트폴리오 입력, BFF runtime에 연결된 직원 이동·업무·부서장 handoff, CEO Console·추천 승인.
- `대시보드`: CEO task routing Kanban, 조직 요약, 연동 상태·운영 지표(포트폴리오 입력창·라이브 오피스 결과창 복제 금지).
- `DEMO`/`PAPER`/`LIVE`는 BFF snapshot 값을 그대로 표시 — 임의로 LIVE처럼 승격하지 않는다.

## 상태·runtime 경계

- 직원 상태 Source of Truth: Hermes Kanban → `agent.status.v1` → BFF Agent Status Projector → `/ui/snapshot`/`/ws/operations`.
- runtime event가 없으면 직원은 `OFFLINE`/`IDLE`로만 표시 — 로컬 스크립트로 LangGraph 실행을 가장하지 않는다.
- 포트폴리오 추천은 사용자 승인 대기 자문일 뿐, `APPROVE`도 주문·Risk 승인·원장 변경을 하지 않는다.
- 부서 간 handoff는 부서장 Projection만 — 직원끼리 부서 간 이동을 만들지 않는다.

## Frontend/BFF 실행

저장소 루트에서 실행(프론트 폴더 안엔 `.venv` 없음):

```bash
# 터미널 1 — Read-only FastAPI BFF
DATABASE_URL='' .venv/bin/python -m uvicorn apps.api.main:app --reload --port 8001
# 터미널 2 — AI Office
NEXT_PUBLIC_BFF_URL=http://127.0.0.1:8001 npm --prefix ai-office run dev -- --port 3002
```

8001을 이미 쓰고 있으면 BFF를 중복 실행하지 말고 기존 프로세스를 쓴다.

## 안전 규칙

- `.dev.vars`, `.env*`, `auth.json`, Supabase Service Role, Broker/LS credential을 읽거나 출력하거나 커밋하지 않는다.
- 브라우저에서 Supabase Service Role, Broker API, Hermes 내부 DB를 직접 호출하지 않는다.
- Agent 텍스트에서 Position/PnL/NAV/Risk 판정을 추출해 확정하지 않는다.
- Command는 `idempotency_key`, `expected_version`, 권한·정책 검증, Audit Event 없이 추가하지 않는다. CEO·프론트는 주문 제출·원장 Posting·NAV 확정 권한이 없다.
- 실패·heartbeat 없음·sequence gap은 성공으로 표시하지 않고 `STALE`/`DEGRADED`/`BLOCKED`/`ERROR` 중 실제 상태로 보여준다.

## 작업 전후 확인

```bash
npm --prefix ai-office run lint
npm --prefix ai-office test
DATABASE_URL='' .venv/bin/python -m unittest discover -s tests/api -p 'test_*.py' -v
DATABASE_URL='' PYTHONPATH=. .venv/bin/pytest -q tests/e2e tests/contracts/test_unified_api_contract.py backend/tests
```

UI 수정 시 라이브/대시보드 탭, BFF offline, mode 3종, task 상태 5종, 영업시간(09:00~18:00) 표시를 함께 확인한다. `company.config.ts` 조직·직원 Projection 수정 시 실제 부서 Profile·Worker Registry 구현 여부를 확인하고, 미구현 Worker를 실행 중으로 보여주지 않는다.
