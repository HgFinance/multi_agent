# HgFinance AI Office

`ai-office`는 HgFinance의 부서·직원 흐름을 보여주는 DEMO Projection이다. 실제 Hermes 세션, LangGraph 실행, 주문, Risk Limit, 원장 상태의 Source of Truth는 아니다. 로컬 모의투자에서는 고정 데모 ID로 BFF에 연결하고, 시장·계좌 카드는 LS PAPER 읽기 전용 데이터를 표시한다. LIVE 주문과 브라우저 로그인·Supabase Auth는 지원하지 않는다.

## 조직 표시 경계

현재 조직·Worker 수·모델은 이 화면 문서가 소유하지 않는다. 조직과 실행 편제는
[Current Architecture](../docs/CURRENT_PROJECT_ARCHITECTURE.md), 모델은
[Worker Model Matrix](../docs/02-engineering/WORKER_MODEL_MATRIX.md)를 따른다.
Worker Context는 non-binding이며 Risk Gate와 Evidence QA Gate는 결정론적 코드가
소유한다.

화면 엔진의 고정 ID 호환성을 위해 `company.config.ts`에는 위 조직 외에 `secretary` 공간이 남아 있다. 이는 `CEO Office shared-support projection`일 뿐 별도의 Hermes 부서나 추가 인원으로 집계하지 않는다.

## 현재 구현

- CEO·7개 부서·CEO 지원 공간을 2층 Pixel Office로 표시한다.
- `RiskQaPanel`은 Risk 2명(LLM 1명 + 결정론 runner 1명), QA 3명(LLM 2명 + 결정론 runner 1명)의 Worker Registry와 Head 모델을 별도로 표시한다.
- Head·Worker 모델 표시는 Runtime 정본 값을 투영하며 이 문서에서 모델명을 고정하지 않는다.
- `Simulation working`은 `app/game/sim.ts`의 데모 상태일 뿐 외부 런타임 성공 증거가 아니다.
- Risk/QA의 주문 제출·원장 기록·Risk Limit 변경 권한은 화면과 연결하지 않는다.
- `DEMO`, `PAPER`, `LIVE` 상태를 혼동하지 않으며, 화면은 DEMO 오피스 위에 PAPER 브로커 조회를 투영한다.
- CEO Control Room은 일반 자문과 명시적 PAPER 주문을 같은 입력창에서 구분한다.
  이 주문 계약은 활성화된 PAPER 환경에서 CEO Kanban → Trading Hermes의 비구속 해석
  → 서버 검증 → PAPER OMS로 전달되고 Accounting ACK까지 별도 상태로 추적한다.
  루트 로컬 Compose는 PAPER workflow를 활성화하며 모든 요청을 고정 데모 사용자와
  LS 모의투자 계좌로 제한한다.
  여러 ACTIVE Book 중 하나를 자동 추측하지 않는다. 별도 PAPER 주문 패널의
  `/trading/agent/order` 호환 경로도 활성화 시 동일한 고정 fixture·Fund/Book·PAPER 전용
  admission을 사용한다.

## 실행

```bash
# 저장소 루트에서 실행
npm --prefix ai-office install
NEXT_PUBLIC_BFF_URL=http://127.0.0.1:8001 npm run dev -- --port 3000
```

현재 Worker 실행 구간을 화면에 반영하기 위해 Snapshot 폴링은 `400ms`로 동작한다.

기본 주소는 `http://localhost:3000`이다.

## BFF 연결

AI Office는 브라우저에서 부서 API나 DB를 직접 호출하지 않고 `portfolio-bff`의 Read Model을 읽는다. 화면의 직원 이동·착석·대화는 실제 `portfolio-recommendation-full` LangGraph runtime projection이 있을 때만 발생한다. 시장 상위종목과 계좌·보유종목·체결 요약은 BFF가 LS PAPER API에서 읽는다. 사용자 적합성 입력은 BFF의 `POST /ui/portfolio-recommendations`로 전달된다.

```bash
# 저장소 루트, 고정 데모 ID·LS PAPER 조회로 BFF 실행
npm run bff

# 별도 터미널, 저장소 루트에서 실행
NEXT_PUBLIC_BFF_URL=http://127.0.0.1:8001 npm run dev -- --port 3000
```

프론트 폴더에서 직접 실행하려면 `npm run dev`를 사용한다. 저장소 루트에서 `npm run dev`를 실행하면 루트 스크립트가 `ai-office`로 전달한다.

이미 `ai-office` 디렉터리에 있다면 BFF 명령은 `cd ..`로 저장소 루트로 이동한 뒤 실행한다. 루트의 `.venv`는 프론트 폴더 안에 있지 않다.

## BFF 통신 방식

AI Office는 사용자 계정 화면 없이 고정된 데모 ID
`00000000-0000-4000-8000-00000000cec0`를 BFF에 전달한다. 브라우저 요청은
항상 동일 출처 `/bff/*`로 보내고 Worker가 브라우저 입력과 무관하게 같은
`X-User-Id`를 내부 BFF에 설정하므로 브라우저 CORS preflight가 발생하지 않는다.
직접 `8001/ui/me`를 헤더 없이 호출해도 같은 데모 사용자로 200을 반환한다.
PAPER 계좌는 로컬 `.env`의
`LS_ACCOUNT_NO_PAPER=5601`을 사용한다.

`8001` 포트가 이미 사용 중이면 기존 프로세스를 확인한 뒤 종료하거나 다른 포트를 사용한다.

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN
npm run bff
NEXT_PUBLIC_BFF_URL=http://127.0.0.1:8001 npm run dev -- --port 3002
```

## 검증

```bash
cd ai-office
npm test
```

검증은 TypeScript build와 server-render assertion을 포함한다. 실제 외부 API·DB·Hermes 인증 검증은 별도의 부서 런북과 실행 로그로 확인한다.

## 관련 문서

- 조직·Worker Registry: [`../docs/04-organization/AGENT_EMPLOYEE_PROFILES.md`](../docs/04-organization/AGENT_EMPLOYEE_PROFILES.md)
- Worker 경계: [`../docs/02-engineering/WORKER_ROLE_BOUNDARIES.md`](../docs/02-engineering/WORKER_ROLE_BOUNDARIES.md)
- 전체 파이프라인: [`../docs/HEDGE_FUND_MASTER_PLAN.md`](../docs/HEDGE_FUND_MASTER_PLAN.md)
- Hermes Profile 런북: [`../docs/02-engineering/HERMES_DOCKER_RUNBOOK.md`](../docs/02-engineering/HERMES_DOCKER_RUNBOOK.md)
