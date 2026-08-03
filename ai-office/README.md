# HgFinance AI Office

`ai-office`는 HgFinance의 부서·직원 흐름을 보여주는 읽기 전용 DEMO Projection이다. 실제 Hermes 세션, LangGraph 실행, 시장 데이터, 주문, Risk Limit, 원장 상태의 Source of Truth가 아니다.

## 현재 조직 기준

실제 조직은 CEO와 7개 Hermes 부서다.

| 조직 | Hermes Head | 독립 LangGraph Worker | Worker 모델 |
|---|---|---:|---|
| CEO | `ceo-agent` · `openai-codex/gpt-5.6-luna` | 1 | `qwen3:8b` |
| HR | `hr-department` · `openai-codex/gpt-5.6-luna` | 5 | `qwen3:8b` |
| Research | `research-department` · `openai-codex/gpt-5.6-luna` | 6 | `qwen3:8b` |
| Trading | `trading-department` · `openai-codex/gpt-5.6-luna` | 6 | `qwen3:8b` |
| Risk | `risk-management` · `openai-codex/gpt-5.6-luna` | 4 | `qwen3:8b` |
| Quant/Backtest | `quant-backtest-department` · `openai-codex/gpt-5.6-luna` | 7 | `qwen3:8b` |
| Accounting/Portfolio | `accounting-portfolio-department` · `openai-codex/gpt-5.6-luna` | 8 | `qwen3:8b` |
| AI QA/Audit | `qa-department` · `openai-codex/gpt-5.6-luna` | 5 | `qwen3:8b` |

부서 흐름은 `Hermes Head → Worker별 독립 LangGraph → Worker Context → Hermes 종합`이다. Worker Context는 non-binding이며, Risk Gate와 Evidence QA Gate는 결정론적 코드가 소유한다.

화면 엔진의 고정 ID 호환성을 위해 `company.config.ts`에는 위 조직 외에 `secretary` 공간이 남아 있다. 이는 `CEO Office shared-support projection`일 뿐 별도의 Hermes 부서나 추가 인원으로 집계하지 않는다.

## 현재 구현

- CEO·7개 부서·CEO 지원 공간을 2층 Pixel Office로 표시한다.
- `RiskQaPanel`은 Risk 4명, QA 5명의 Worker Registry와 Head 모델을 별도로 표시한다.
- Head는 Hermes + Codex/Luna, Worker는 독립 LangGraph + Ollama `qwen3:8b`로 표시한다.
- `Simulation working`은 `app/game/sim.ts`의 데모 상태일 뿐 외부 런타임 성공 증거가 아니다.
- Risk/QA의 주문 제출·원장 기록·Risk Limit 변경 권한은 화면과 연결하지 않는다.
- `DEMO`, `PAPER`, `LIVE` 상태를 혼동하지 않으며, 현재 화면은 DEMO Projection이다.

## 실행

```bash
cd ai-office
npm install
npm run dev
```

기본 주소는 `http://localhost:3000`이다.

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
