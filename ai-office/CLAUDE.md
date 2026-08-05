# AI Office 작업 지침

`ai-office/`는 HgFinance 헤지펀드의 운영 상태를 보여주는 픽셀 오피스 Projection이다. 금융 원장, Broker credential, Risk 한도, Hermes 내부 상태를 브라우저가 소유하지 않는다. 사용자가 개발자가 아닐 수 있으므로 안내 문구는 쉬운 한국어로 작성한다.

## 현재 조직 구조

`company.config.ts`의 `DEPARTMENTS`와 `app/game/world.ts`가 현재 조직의 기준이다. 부서 ID는 아래 8개이며, 기존 SNS 콘텐츠 데모의 `brand`, `reels`, `carousel`, `partner` ID를 되살리지 않는다.

```text
research   strategy1   strategy2   ops
finance    qa          review       secretary
```

- `research`: Research 본부
- `strategy1`: Quant/Backtest 본부
- `strategy2`: Trading 본부
- `ops`: Risk 본부
- `finance`: Accounting/Portfolio 본부
- `qa`: AI QA/Audit 본부
- `review`: Agent Workforce/HR Shared Service
- `secretary`: CEO Office Shared Support Projection

부서 수와 배치는 12개 고정이 아니다. `app/game/sim.ts`의 직원 순회·zigzag 순서와 `app/game/world.ts`의 레이아웃은 현재 8개 조직을 기준으로 동작한다. 부서를 추가하거나 ID를 바꾸려면 `company.config.ts`, `world.ts`, `staff.ts`, runtime department mapping과 테스트를 함께 갱신한다.

## 화면 역할

- `라이브 오피스`: 국내 주식 포트폴리오 입력, 실제 BFF runtime에 연결된 직원 이동·업무·부서장 handoff, CEO Console과 추천 승인 Projection을 보여준다.
- `대시보드`: CEO task routing Kanban, 조직 요약, 연동 상태와 운영 지표를 보여준다. 포트폴리오 입력창과 라이브 오피스 전용 결과창을 여기에 복제하지 않는다.
- 화면의 `DEMO`, `PAPER`, `LIVE` mode는 BFF snapshot의 값을 그대로 표시한다. 값이 없거나 알 수 없는 mode를 LIVE처럼 표현하지 않는다.

## 공식 상태와 runtime 경계

- 직원 상태의 Source of Truth는 Hermes Kanban → `agent.status.v1` → BFF Agent Status Projector → `/ui/snapshot` 또는 `/ws/operations` 흐름이다.
- 브라우저에서 로컬 업무 스크립트로 실제 LangGraph 실행을 가장하지 않는다. runtime event가 없으면 직원은 `OFFLINE`/`IDLE`로 표시하고 이동·말풍선을 만들지 않는다.
- 포트폴리오 추천은 주문·Risk 승인·원장 변경이 아닌 사용자 승인 대기 자문이다. `APPROVE`도 주문을 제출하지 않는다.
- `DEMO`는 fixture/prototype projection, `PAPER`는 paper workflow, `LIVE`는 권한 있는 운영 read model이다. BFF가 알려준 mode와 상태를 임의로 승격하지 않는다.
- 부서 간 handoff는 부서장 Projection만 이동·대화시킨다. 직원끼리 부서 간 이동을 만들지 않는다.

## Frontend/BFF 실행

저장소 루트에서 실행한다. 프론트 폴더 안에는 저장소 `.venv`가 없다.

```bash
# 터미널 1 — Read-only FastAPI BFF
DATABASE_URL='' .venv/bin/python -m uvicorn apps.api.main:app --reload --port 8001

# 터미널 2 — AI Office
NEXT_PUBLIC_BFF_URL=http://127.0.0.1:8001 npm --prefix ai-office run dev -- --port 3002
```

8001을 이미 사용 중이면 BFF를 중복 실행하지 말고 기존 프로세스를 사용한다. 프론트의 BFF 기본 주소도 `127.0.0.1:8001`이다.

## 안전 규칙

- `.dev.vars`, `.env*`, `auth.json`, Supabase Service Role, Broker/LS credential을 읽거나 출력하거나 커밋하지 않는다.
- 브라우저에서 Supabase Service Role, Broker API, Hermes 내부 DB를 직접 호출하지 않는다.
- Agent 텍스트에서 Position/PnL/NAV/Risk 판정을 추출해 확정하지 않는다.
- Command는 `idempotency_key`, `expected_version`, 권한·정책 검증과 Audit Event 없이 추가하지 않는다. CEO와 프론트는 주문 제출·원장 Posting·NAV 확정 권한을 갖지 않는다.
- 실패·heartbeat 없음·sequence gap은 성공이나 완료로 표시하지 않고 `STALE`, `DEGRADED`, `BLOCKED`, `ERROR` 중 실제 상태로 보여준다.

## 작업 전후 확인

문서와 API 계약을 먼저 확인한다.

```bash
npm --prefix ai-office run lint
npm --prefix ai-office test
DATABASE_URL='' .venv/bin/python -m unittest discover -s tests/api -p 'test_*.py' -v
DATABASE_URL='' PYTHONPATH=. .venv/bin/pytest -q tests/e2e tests/contracts/test_unified_api_contract.py backend/tests
```

UI를 수정하면 라이브/대시보드 탭, BFF offline, `DEMO/PAPER/LIVE` mode, `QUEUED/RUNNING/WAITING_APPROVAL/COMPLETED/ERROR`, 09:00~18:00 영업시간 표시를 함께 확인한다.

`company.config.ts`의 조직·직원 Projection을 수정할 때는 실제 부서 Profile과 Worker Registry가 구현됐는지 확인한다. 구현되지 않은 Worker를 화면에서 실행 중이라고 만들지 않는다.
