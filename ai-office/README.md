# HgFinance AI Office

`ai-office`는 HgFinance의 부서·직원 흐름을 보여주는 읽기 전용 DEMO Projection이다. 실제 Hermes 세션, LangGraph 실행, 시장 데이터, 주문, Risk Limit, 원장 상태의 Source of Truth가 아니다.

## 현재 조직 기준

실제 조직은 CEO와 7개 Hermes 부서다.

| 조직 | Hermes Head | 독립 LangGraph Worker | Worker 모델 |
|---|---|---:|---|
| CEO | `ceo-agent` · `openai-codex/gpt-5.6-luna` | 1 | `qwen3:1.7b` |
| HR | `hr-department` · `openai-codex/gpt-5.6-luna` | 5 | `qwen3:1.7b` |
| Research | `research-department` · `openai-codex/gpt-5.6-luna` | 6 | `qwen3:1.7b` |
| Trading | `trading-department` · `openai-codex/gpt-5.6-luna` | 6 | `qwen3:1.7b` |
| Risk | `risk-management` · `openai-codex/gpt-5.6-luna` | 4 | `qwen3:1.7b` |
| Quant/Backtest | `quant-backtest-department` · `openai-codex/gpt-5.6-luna` | 7 | `qwen3:1.7b` |
| Accounting/Portfolio | `accounting-portfolio-department` · `openai-codex/gpt-5.6-luna` | 8 | `qwen3:1.7b` |
| AI QA/Audit | `qa-department` · `openai-codex/gpt-5.6-luna` | 5 | `qwen3:1.7b` |

부서 흐름은 `Hermes Head → Worker별 독립 LangGraph → Worker Context → Hermes 종합`이다. Worker Context는 non-binding이며, Risk Gate와 Evidence QA Gate는 결정론적 코드가 소유한다.

화면 엔진의 고정 ID 호환성을 위해 `company.config.ts`에는 위 조직 외에 `secretary` 공간이 남아 있다. 이는 `CEO Office shared-support projection`일 뿐 별도의 Hermes 부서나 추가 인원으로 집계하지 않는다.

## 현재 구현

- CEO·7개 부서·CEO 지원 공간을 2층 Pixel Office로 표시한다.
- `RiskQaPanel`은 Risk 4명, QA 5명의 Worker Registry와 Head 모델을 별도로 표시한다.
- Head는 Hermes + Codex/Luna, Worker는 독립 LangGraph + Ollama `qwen3:1.7b`로 표시한다.
- `Simulation working`은 `app/game/sim.ts`의 데모 상태일 뿐 외부 런타임 성공 증거가 아니다.
- Risk/QA의 주문 제출·원장 기록·Risk Limit 변경 권한은 화면과 연결하지 않는다.
- `DEMO`, `PAPER`, `LIVE` 상태를 혼동하지 않으며, 현재 화면은 DEMO Projection이다.

## 실행

```bash
# 저장소 루트에서 실행
npm --prefix ai-office install
NEXT_PUBLIC_BFF_URL=http://localhost:8000 npm run dev -- --port 3000
```

기본 주소는 `http://localhost:3000`이다.

## BFF 연결

AI Office는 브라우저에서 부서 API나 DB를 직접 호출하지 않고 `operator-bff`의 Read Model을 읽는다. 화면의 직원 이동·착석·대화는 실제 `portfolio-recommendation-full` LangGraph runtime projection이 있을 때만 발생한다. 사용자 적합성 입력은 BFF의 `POST /ui/portfolio-recommendations`로 전달된다. 두 프로세스를 각각 실행한다.

```bash
# 저장소 루트, 외부 DB 없이 DEMO Read Model로 실행
DATABASE_URL='' .venv/bin/python -m uvicorn apps.api.main:app --reload --port 8000

# 별도 터미널, 저장소 루트에서 실행
NEXT_PUBLIC_BFF_URL=http://localhost:8000 npm run dev -- --port 3000
```

프론트 폴더에서 직접 실행하려면 `npm run dev`를 사용한다. 저장소 루트에서 `npm run dev`를 실행하면 루트 스크립트가 `ai-office`로 전달한다.

이미 `ai-office` 디렉터리에 있다면 BFF 명령은 `cd ..`로 저장소 루트로 이동한 뒤 실행한다. 루트의 `.venv`는 프론트 폴더 안에 있지 않다.

프론트는 `NEXT_PUBLIC_BFF_URL`이 없으면 `http://localhost:8000`을 사용하고, 연결 후 Snapshot을 5초마다 갱신한다. BFF가 꺼져 있거나 실제 LangGraph run이 없으면 오래된 Fixture나 가짜 업무를 표시하지 않고 `OFFLINE`/대기 상태를 보여준다. 투자금액별 목표 금액과 사용자 추천 승인 단계까지 표시한다. 현금화 필요 기간은 화면에서 받지 않고 BFF가 `MEDIUM` 기본값으로 처리한다. 추천 승인은 주문 제출·Risk 승인·원장 변경을 수행하지 않는다.

연동 상태는 BFF의 `GET /ui/integrations`에서 읽으며, 비밀값은 브라우저로 보내지 않는다.

`8000` 포트가 이미 사용 중이면 기존 프로세스를 확인한 뒤 종료하거나 다른 포트를 사용한다.

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
npm run bff:alt
NEXT_PUBLIC_BFF_URL=http://localhost:8001 npm run dev -- --port 3002
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
