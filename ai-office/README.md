# HgFinance AI Office

`AI Office`는 CEO Office, 6개 투자 본부와 인사팀을 한눈에 보는 개인형
헤지펀드 운영 Frontend다. 현재는 **8개 조직·2개 층의 DEMO Prototype**이며 실제 Agent·시장·주문
상태의 Source of Truth가 아니다.

전체 제품·권한·실시간 계약은
[AI Office Frontend Plan](../docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md)을 따른다.

## 현재 구현

- Next.js, React, TypeScript 기반 Pixel Office.
- CEO Office, 리서치, 트레이딩, 리스크, 퀀트/백테스트, 회계/포트폴리오, AI QA/감사와
  인사팀 등 8개 조직.
- 1층·2층 전환, 조직별 직원과 Bull/Bear 토론 DEMO.
- Trading/Portfolio Snapshot Panel과 `DEMO` Mode 표시.
- `../apps/api/main.py`의 `GET /ui/snapshot` Read-only DEMO BFF.

직원 이동과 업무 흐름은 아직 `app/game/sim.ts`의 Scripted Simulation이다. Trading/Portfolio
Snapshot도 Supabase 운영 데이터가 아니라 테스트 Paper Loop로 만든 DEMO Projection이다.

## 실행

Node.js 22 이상을 사용한다.

```bash
cd ai-office
npm install
npm run dev
```

기본 주소는 `http://localhost:3000`이다.

DEMO BFF는 저장소 루트에서 별도로 실행한다.

```bash
uvicorn apps.api.main:app --reload --port 8000
```

부서 Agent 질의는 부서마다 경로가 다르다 — `POST /accounting/agent/ask`, `POST /trading/agent/ask`.
Body는 `{"query": "..."}`뿐이고 부서 이름을 보내지 않는다. 화면이 부서를 지정할 수 없어야
한 본부 패널에서 다른 본부 Agent를 부르는 경로 자체가 생기지 않는다(마스터플랜 5.6).

이 경로들은 Hermes가 Tool을 실행할 수 있으므로 기본 비활성화 상태다. 로컬 개발에서도
Profile Tool Allowlist와 질의 영향을 확인한 경우에만 `ENABLE_AGENT_ASK=true`로 명시적으로 연다.
Production에서는 Supabase Auth, 사용자별 권한과 Audit가 연결되기 전까지 활성화하지 않는다.

## 목표 연결 구조

```text
Hermes Kanban + Runtime Heartbeat
  -> Kanban Status Bridge
  -> Redis Streams: agent.status.v1
  -> Supabase Agent Status Read Model
  -> FastAPI GET /ui/snapshot + WS /ws/operations
  -> AI Office Projection

LS Market Worker / Risk / OMS / Ledger / QA
  -> Domain Event + Read Model
  -> 같은 BFF와 WebSocket
```

Agent 업무 상태는 Hermes Kanban을 재사용한다. 상태 매핑과 소유권은
[ADR-0001](../docs/02-engineering/adr/0001-hermes-kanban-agent-status-bridge.md)을 따른다.
Browser는 Kanban SQLite, Supabase 거래 Table, OMS나 Ledger를 직접 읽거나 수정하지 않는다.

## 소유권

| 영역 | Owner |
|---|---|
| Live Office 제품·업무 의미 | 영주님 |
| 공통 Frontend Platform 기술 DRI | 도현님 |
| Risk·QA 상태 계약 Review | 동규님 |
| Market·Research·Strategy 계약 | 재일님 |
| Trading·Portfolio·Close 계약 | 도현님 |

각 본부는 자기 Read Model과 Event 의미를 소유한다. Frontend 구현자가 금융 상태의 의미를 새로
계산하거나 추정하지 않는다.

## 검증

```bash
cd ai-office
npx tsc --noEmit
npm run build
node --test tests/rendered-html.test.mjs

cd ..
python apps/api/main.py
```

현재 Cloudflare/Vinext 관련 기존 TypeScript 환경 오류와 실제 신규 오류를 구분해 기록한다.
`DEMO`, `PAPER`, `LIVE` 데이터는 같은 화면에서 섞지 않는다.

## 다음 작업

1. Supabase Auth와 공식 `/ui/snapshot` Read Model 연결.
2. `/ws/operations`, Heartbeat, Sequence Gap과 Snapshot Recovery.
3. Kanban Status Bridge와 `agent.status.v1` Projector.
4. Market, Research, Strategy, Risk, Trading, Portfolio, Audit와 Workforce Workbench.
5. 위험 Command의 Preview, 사유, 멱등 키, Backend 재검증과 Audit.
