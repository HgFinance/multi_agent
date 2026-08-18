# 시스템 배선 지도 (System Wiring Map)

> **Snapshot status:** 이 문서는 `origin/main`과 당시 컨테이너 상태를 대조한
> 배선 snapshot이다. 현재 아키텍처·worker registry·serving 상태는
> [CURRENT_PROJECT_ARCHITECTURE.md](../CURRENT_PROJECT_ARCHITECTURE.md)를 우선한다.

> **코드 기준: `origin/main@5054d2d` (2026-08-13 14:02, PR #237까지).** 컨테이너 36개 Up 상태의 실측 + origin/main 60커밋 대조로 그렸다.
> 교차 검증 통과: 워커 10명 = CLAUDE.md 편제표와 1:1 일치, 러너 5개 = `RUNNER_ID` 상수 5곳 실측, 컨테이너 전수 일치.
> 각 절의 근거는 `파일:행`으로 남겼다. 이 문서는 **스냅샷**이며 정본은 코드다.
>
> ✅ **로컬 스택 따라잡기 완료 (2026-08-13).** origin/main 병합(`d20bfa1`) 후 재기동 — **38/38 실행**(기본 37 + 고아 quant-api), 진입점 8001 은 portfolio-bff 로 인계됐다. 단 hermes 8개는 Dockerfile 미커밋으로 옛 이미지에 핀(§7-9) — [main] 표기 중 Discord 게이트웨이만 로컬 미적용.

---

## 0. 한눈에 보기 — 다섯 층과 두 개의 엔진

```
 ①시장수집(2)      ②저장(3+1)         ③조회면(11)        ④에이전트           ⑤사용자
┌──────────┐    ┌─────────────┐    ┌────────────┐    ┌─────────────┐    ┌──────────┐
│ls-realtime│──▶│ TimescaleDB │──▶│ market-api │──▶│ hermes 8부서장│    │ ai-office│
│batch(10종)│──▶│  (market.*) │    │research-api│    │ LLM 워커 10명 │◀──│  프런트   │
│요청형 MCP │───┼─ 비영속 조회 │──▶│research-mcp│──▶│ 결정론 러너 5개│    │          │
│(뉴스·공시 등)│ │ Control DB  │──▶│ 부서API 7종 │    └──────┬──────┘    └────┬─────┘
└──────────┘    │  Redis      │    └────────────┘           │               │
                │  Parquet아카이브│                            ▼               ▼
                └─────────────┘                      ┏━━━━━━━━━━━━━┓   ┌──────────┐
                                                     ┃엔진1: kanban- ┃◀──│ BFF :8001│
   ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓        ┃  dispatcher  ┃   │(main: portfolio-bff│
   ┃엔진2: factory-autopilot (15분 주기)        ┃───────▶┃(카드 실행 엔진)┃   │ portfolio-bff)└────┘
   ┃ 브리핑→기획→Gate0→가설→발주→실험→판정→환류 ┃        ┗━━━━━━━━━━━━━┛
   ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

움직이는 것은 둘뿐이다. **kanban-dispatcher** 가 카드를 집어 에이전트를 띄우고(사용자 질의 경로), **factory-autopilot** 이 15분마다 전략 실험 한 주기를 돌린다(전략 연구 경로). 나머지 컨테이너는 전부 이 둘에게 데이터·조회면·판정을 대주는 조연이다.

---

## 1. 컨테이너 지도 — 요건 이 기능, 저건 저 기능

> 로컬 실행 중 36개 기준으로 적되, origin/main 에서 달라진 것은 **[main]** 으로 표기했다 (기본 기동 37개).

### 🗄 인프라 (2)

| 서비스 | 하는 일 | 포트 |
|---|---|---|
| `timescaledb` | 시장 시계열 본체. 호가·체결·일봉·파생·Breadth 전부 여기(`market.*`). 호가는 나흘·체결은 6주만 hot 보관, 장기는 Parquet | `0.0.0.0:5434` |
| `redis` | Risk↔QA·governance·workforce·체결 이벤트 Stream 버스. AOF 없음 — 재시작하면 Stream 유실 감수 | 내부 6379 |

### 📡 시장데이터 수집기 (2)

| 서비스 | 하는 일 | 어디에 쓰나 |
|---|---|---|
| `ls-realtime` | LS 웹소켓 호가·체결 실시간. 전종목 2,595 = 구독 5,190 = 소켓 26개. 장 세션 창만 연결 | `market.market_ticks/quotes` |
| `batch-collectors` | **시장 배치 Job 10종만 순차 실행.** Archive·거래제한·DQ·Breadth·파생·VKOSPI·스타일지수·캘린더·라벨·전종목 일봉 | 아래 §5 |

뉴스·공시·재무·거시·문헌은 수집 서비스가 아니다. `research-mcp`가 요청 시점에
조회하고 응답 해시만 반환하며 파일·업무 DB·Storage·pgvector에 적재하지 않는다.

### 🔌 조회면 API (11)

| 서비스 | 하는 일 | 포트 | 주 소비자 |
|---|---|---|---|
| `portfolio-bff` | **[main] 새 프런트 관문 (정본 프로덕션 BFF).** 호스트 8001 을 가져갔다. 온보딩 프록시 5종(`/ui/mandates`, `/ui/mandate-assistant/suggest`…→governance-api), InvestorProfile(`/ui/investor-profiles`→accounting-api), **CEO Web/Discord mirror**(Redis Stream `hf:ui-ceo-mirror:v1`, dedupe TTL 7일) | **8001→8000** | ai-office·Discord |
| `portfolio-worker` | [main] portfolio-bff 의 백그라운드 워커 (`portfolio_worker.py`). LangSmith 트레이싱 옵션 | — | — |
| `ui-bff` | 기존 관문. CEO 질의 접수(`/ui/ceo/ask`), 공식 수치(`/ui/snapshot`), 부서 API 프록시. 에이전트 텍스트는 전부 `binding:false`. **[main] 호스트 게시 제거 — 내부 전용으로 강등** (로컬 실행 스택은 아직 8001 게시 중) | [main] 내부 8001 | (내부) |
| `market-api` | 시세 읽기 전용. 타 부서는 TSDB 자격 없이 이것만 본다. `/snapshot` `/bars` `/breadth` `/dq` `/microstructure` | **8036** (0.0.0.0, 팀원 공유) | 회계 Mark·퀀트·리서치 애널리스트·BFF |
| `research-api` | 기존 `research.*` Evidence의 **레거시 읽기 전용 호환면**. 신규 뉴스·공시·재무를 적재하지 않으며 현재 정보 조회는 `research-mcp`가 맡는다 | 8035 | 레거시 감사·시장 보조 조회 |
| `research-mcp` | Hermes ↔ 요청형 외부정보·가설 공장·등록된 직원 Worker의 도구 면. 외부 응답은 비영속이며 쓰기는 lead/proposal/검토 같은 공장 원장에 한정 | 내부 8037 | research-hermes |
| `risk-api` | 결정론 Risk Engine 래퍼 — 주문 판정은 전부 `risk_engine.check_order`. trading-state·compliance RAG·mandate assess | 8041 | BFF 프록시·트레이딩 흐름 |
| `audit-api` | Evidence QA Gate + 감사면. 실행 추적(runs/tool-calls)·시정조치·모델리스크 | 8042 | BFF 프록시·qa-worker |
| `governance-api` | Mandate 버전/활성화·승인·위원회·에스컬레이션·보고서. 이벤트를 `hf:governance` Stream 에 발행 | 8043 | BFF·notification-worker |
| `workforce-api` | Agent 인사 — 접근권한·채용·개선 후보(자기승인 차단)·roster·스코어카드 | 8044 | improvement-worker·platform_iam |
| `trading-api` | OMS 래퍼 — OrderIntent 접수, 리스크 판정 **기록**(스스로 만들지 않음), 주문 상태 머신. PAPER_DB | 8045 | 서비스 호출자 (BFF 배선은 아직 없음) |
| `accounting-api` | 원장·평가·대사·일일보고. Posted Journal 수정 불가(reverse만), NAV `is_official:false` 고정 | 8046 | 서비스 호출자 |
| `quant-api` | ⚠ **고아 컨테이너.** 실험 제출·조회면인데 현행 compose 어디에도 정의가 없다(옛 브랜치 커밋의 잔재). `--remove-orphans` 한 번이면 사라진다. 소스는 `codex-research-wip-20260811` 브랜치에 갇혀 있음 | 8037 | 공장 DB 조회용으로만 사실상 사용 |

> 부서 API 컨테이너의 내부 포트는 전부 8000이고, 호스트 게시는 `127.0.0.1` 전용(8041~8046). 밖에 열린 것은 `8001`(BFF)·`8036`(market-api)·`5434`(TSDB, Tailscale 전제) 셋뿐이다.

### 🎩 Hermes 부서장 게이트웨이 (8)

전부 같은 이미지, 같은 command(`gateway run`), 공용 kanban 볼륨(`/opt/kanban`) 마운트. **부서 페르소나·도구·인증은 각자의 프로필 마운트(`/opt/data`)가 결정한다.**

> **[main] 이미지가 8개 전부 교체됐다**: `nousresearch/hermes-agent:latest` → `hgfinance/hermes-discord:discord-idempotency-v1` (빌드 `Dockerfile.hermes-discord`). Discord 게이트웨이 통합 + 멱등성 이미지다. 로컬에 떠 있는 8개는 아직 옛 이미지.

| 서비스 | 프로필(= 카드 assignee 정본) | 비고 |
|---|---|---|
| `ceo-hermes` | `ceo-agent` | 유일하게 내부 API(8642)를 켬 — 질의 입구 |
| `research-hermes` | `research-department` | research-mcp 를 도구로 물음 |
| `quant-hermes` | `quant-backtest-department` | |
| `trading-hermes` | `trading-department` | skills/finance 마운트 없음(다른 7개와 다름) |
| `risk-hermes` | `risk-management` | |
| `accounting-hermes` | `accounting-portfolio-department` | |
| `qa-hermes` | `qa-department` | **kanban CLI 실행 컨테이너 겸직**(`KANBAN_CLI_CONTAINER` 기본값) — BFF·공장·watchdog 의 카드 생성이 여기로 나간다 |
| `workforce-hermes` | `hr-department` | ⚠ 컨테이너명(workforce)과 프로필명(hr)이 어긋나는 유일한 부서 |

> ⚠ 부서장 게이트웨이는 **카드를 실행하지 않는다**(`HERMES_KANBAN_DISPATCH_IN_GATEWAY=false`). 카드 실행 에이전트는 전부 kanban-dispatcher 안에서 뜬다.

### 🃏 Kanban 오케스트레이션 (2)

| 서비스 | 하는 일 |
|---|---|
| `kanban-dispatcher` | **카드를 실제로 돌리는 유일한 엔진.** 60초 tick 으로 ready 카드를 집어 자기 컨테이너 안에서 `profiles/<assignee>` 를 HERMES_HOME 삼아 에이전트 subprocess 를 띄운다. 8개 프로필 전체를 보는 유일한 고권한 컨테이너 — 포트 절대 미게시. **이게 죽으면 카드는 ready 로 영원히 앉는다** |
| `ceo-kanban-supervisor` | CEO 종결 감시자. `kanban watch` 로 종결 이벤트를 구독해, primary 자식이 다 끝나면 QA 카드를, QA 가 끝나면 SYNTHESIZE 카드를 만든다. 이벤트당 최대 1개 bounded action, wakeup 은 root comment 로 durable 기록 |

### 🏭 전략 공장 (3)

| 서비스 | 하는 일 |
|---|---|
| `factory-autopilot` | 공장 주기 엔진(**실효 15분** — 코드 기본 240분을 compose 가 15로 덮음). 수확→Gate0 승격→배분자 수 등록→발주→브리핑 카드→병목 개선 카드 |
| `factory-experiment-worker` | 실험 큐(`quant.experiment_jobs`) 상주 소비자. 가설 하나를 백테스트+walk-forward 로 태우고, RUNNING 30분 스톨을 PROPOSED 로 회수 |
| `card-watchdog` | 3분 주기. 죽은 부모(BLOCKED/FAILED/NO_ASSIGNEE) 밑에 갇힌 자식 카드를 "산출 없음"을 명시하고 풀어준다. 질의 카드 15분·공장 카드 1시간 기한 |

### ⚙️ 이벤트 소비 데몬 (6)

| 서비스 | 구독 → 반영 |
|---|---|
| `trading-outbox-relay` | `execution.outbox` PENDING → Redis Stream 발행 → SENT 마킹. **트레이딩→회계를 잇는 유일한 발행자** |
| `accounting-ledger-consumer` | `trading.fill.v1` → 분개 + Projection. 시세 Mark 는 market-api 에서. **유일 런타임 경로, 복제 금지** |
| `accounting-close-scheduler` | 장 마감 후 일일 마감·금요 주간 보고 → Discord |
| `qa-worker` | Risk Decision Stream → QA Audit 수신 이력 적재 |
| `notification-worker` | `hf:governance` Stream → risk.breach/qa.finding/에스컬레이션 알림 |
| `improvement-worker` | `hf:workforce` Stream → QA Eval 결과를 개선 후보 상태 전이(EVALUATING→SHADOW/REJECTED) |

> 기본 기동에서 빠지는 profiles 서비스: `hermes-dashboard`(dashboard), `paper-search-mcp` `youtube-transcript-mcp`(research-skills). **[main] `portfolio-bff`·`portfolio-worker` 는 profiles 게이트가 제거되어 기본 기동으로 편입됐다** — 기본 기동 37개.

---

## 2. 직원 편제 — LLM 워커 10명 + 결정론 러너 5개

원칙(CLAUDE.md): **LLM 은 관련성 판단·서술만.** 판정·수치·권한은 결정론 코드. 워커 출력은 `worker-context.v1`(confidence 0~1 float, evidence 없으면 escalate 강제), 러너 출력은 `{부서}.{러너}.v1`(summary 필드 자체가 없음 — 서술 금지의 기계적 표현).

### LLM 워커 10명 (Ollama qwen3:1.7b, 독립 LangGraph)

| 부서 | 워커 | 언제 뜨나 | 하는 일 |
|---|---|---|---|
| CEO | `executive-briefing-worker` | 상시 | 부서 결과를 서술로 종합 (읽기: `ceo.department_reports.read`) |
| 리서치 | `competing-explanation-worker` | 기획안 초안 시 | 앵커링 없이 경쟁 설명·반증 제시 |
| 리서치 | `holdings-analyst-worker` | 보유 질의 시 | 보유 종목 질의응답 — `/ui/portfolio-recommendations` 가 부르는 유일한 리서치 워커 |
| 트레이딩 | (0명) | — | `WORKER_SPECS=()`. 전략 번들마다 임시 워커 `alpha-<hash>` 생성(그것도 `llm=False`) |
| 리스크 | `compliance-policy-worker` | compliance 근거 존재 시 | PIT 정책 근거 분석 (Agentic RAG retrieve→grade→generate→검증) |
| 퀀트 | `strategy-author-worker` | 전략 작성 요청 시 | 시그널 코드 작성 — 작성은 에이전트, **승인은 결정론 검사**(spec_hash·PIT·반환형) |
| 퀀트 | `result-interpretation-worker` | 실험 카드 시 | DSR/PBO·국면 해석 — 숫자는 안 건드리고 관문 판정을 문장으로만 |
| 회계 | `exception-investigation-worker` | 상시 | 대사 Break·미설명 PnL 조사. 색인 밖 인용은 escalate, `is_official` 항상 False |
| QA | `hallucination-critic-worker` | UNSUPPORTED claim 존재 시 | 근거 없는 주장 semantic 검증 |
| QA | `incident-postmortem-worker` | 인시던트 존재 시 | 타임라인 postmortem, FACT/INFERENCE 분리 → `audit.incident_events` 영속화 |
| HR | `profile-architecture-worker` | 채용·개정 요청 시 | Job Profile 제안 생성 — 제출·승인·활성화·IAM **불가**, 제안은 PROPOSED 강제 |

### 결정론 러너 5개 (모델 호출 없음 — `WORKER_SPECS` 레지스트리 밖에서 직접 호출)

| 러너 | 부서 | 옮기는 것 (재판정 없음) |
|---|---|---|
| `desk-runner` | 트레이딩 | 경계 표식만: `broker_submit_allowed=False`·`risk_gate_required=True` 를 계약으로 박음 |
| `risk-runner` | 리스크 | `RiskEngine.check_order` 의 verdict/check_results → blocker. core-risk + counterparty 2명 흡수(08-06) |
| `qa-runner` | QA | 5개 결정론 엔진(Evidence/ModelRisk/InternalAudit/OpsHealth/권한)의 PASS·FAIL → 종합 decision. 3명 흡수(08-06) |
| `back-office-runner` | 회계 | 포지션·자금·손익·보고·평가·수수료 6블록을 그대로 옮기고 없는 건 `missing_blocks` 로 명시. 5명 흡수(08-07) |
| `ceo-runner` | CEO | 6단계 산출물 수집, Risk verdict 만료·QA decision → blocker, 안 온 단계는 `missing_inputs` |

**호출 경로가 둘로 갈린다** — 지도에서 가장 헷갈리는 부분:

```
경로 A (러너가 도는 곳):  orchestration/employee_dispatch.py → 각 부서 run_employee_workers()
                          → 러너 + LLM 워커 순차 실행   (paper_pipeline 이 사용)
경로 B (러너를 안 부름):   orchestration/workflows/portfolio_recommendation.py
                          → 각 부서 WORKER_SPECS 의 LLM 워커만 Send 로 fan-out
```

### 아직 배선 안 된 자리 (문서에만 있음)

- 퀀트 `hiring_priority:` 의 `proposal-intake` / `experiment-design` / `outcome-lesson-worker` 3명 — `registration: pending_hr` 로 코드·workers: 정본 어디에도 없음. 그 역할은 현재 factory_autopilot 의 결정론 코드가 대신한다.
- `pending_hr` 플래그 자체는 낡았다 — 이미 구현된 퀀트 워커 2명에도 붙어 있어 미배선 판별 기준이 못 됨.

---

## 3. 카드가 도는 길 — 사용자 질의 (CEO ask)

```
사용자 → ai-office → POST /ui/ceo/ask (ui-bff)
                          │ ①뿌리 카드만 만들고 즉시 202 (assignee=ceo-agent)
                          │   생성은 docker exec → qa-hermes 의 hermes kanban create
                          │   scope 마커 comment 실패 시 503 fail-closed
                          ▼
                 kanban-dispatcher (60초 tick)
                          │ ②ready 카드 집기 → 자기 안에서 ceo-agent 에이전트 spawn
                          ▼
                 CEO 플래너 턴 ③필요한 부서만 골라 자식 카드 생성 (고정 순서 없음)
                          │    body 에 workflow_root_task_id + role=primary
                          ▼
                 dispatcher 가 부서 카드 실행 ④부서 에이전트가 일하고 kanban_complete
                          ▼
                 ceo-kanban-supervisor ⑤primary 전부 종결 감지 → QA 카드 생성
                          ▼            ⑥QA done → SYNTHESIZE 카드 생성 (assignee=ceo-agent)
                 dispatcher 가 SYNTHESIZE 실행 ⑦CEO 가 부서 산출을 두 번째 턴에서 종합
                          ▼
사용자 ← ai-office ← GET /ui/ceo/tasks/{id} 폴링(2~5초) ← ceo_kanban_read (CLI 경유, DB 직접 안 염)
```

- **잔고·시세 같은 결정론 사실은 이 길을 안 탄다** — `fact_router`/`account_snapshot` 직행. ("내 잔고" 를 CEO 라우팅에 태웠더니 4분 걸리고 답도 못 낸 2026-08-11 실측이 근거)
- 유효 assignee 정본 8개: `ceo-agent` `research-department` `quant-backtest-department` `trading-department` `accounting-portfolio-department` `risk-management` `qa-department` `hr-department`. 이 밖의 이름은 카드 생성 시점에 거부된다(`CanonicalKanbanTaskRequest`).
- 고장 대비: 부모가 죽으면 `card-watchdog` 이 "산출 없음"을 명시하고 자식을 풀어준다. DELETE 는 없다 — 감사 추적, 정리는 `/archive` 뿐.

### [main] 2026-08-13 에 바뀐 것 — 지도 반영 필수 4건

1. **입구가 하나 더 생겼다: Web/Discord 공용 mirror ingress.** `apps/api/ceo_mirror.py` + `ceo_mirror_api.py` — `POST /ui/ceo/ingress`(202) 로 들어오면 채널(Web/Discord)이 달라도 **한 사용자 메시지 = CEO 실행 하나**가 되게 dedupe 경계(Redis, TTL 7일)를 통과한다. 결과는 `GET /ui/ceo/events` + `/events/stream`(SSE) 로 미러링. 기존 `/ui/ceo/ask` 폴링 경로도 유지.
2. **다계정이 들어왔다.** `apps/api/current_user.py` 가 `X-User-Id` 헤더 판정의 단일 지점(placeholder 회원 3명, seed). **인증이 아니다** — 서명·만료 없음, 폐쇄망 팀 테스트 전제라고 모듈 스스로 명시. Mandate 소유자 판정이 여기 걸린다.
3. **뿌리 카드 body 에 Mandate 스냅샷 블록이 실린다**(cd57f41) — CEO 플래너가 질의와 함께 사용자의 위임 조건을 읽는다.
4. **QA·SYNTHESIS 직렬 규칙이 갈라졌다** (SOUL.md + `ceo_supervisor.py` `workflow_mode`):
   - **non-binding 분석**: primary 종결 후 QA 와 SYNTHESIS 를 **병렬** 생성 — 종합이 QA 를 기다리지 않는다 (§3 그림의 ⑤→⑥→⑦ 직렬은 binding 경로에만 해당).
   - **binding/고위험**: 기존 fail-closed Risk→QA→승인 게이트 유지.
   - QA 카드는 governance plane 으로 감: `evaluation_sink=audit.eval_runs`, `feedback_consumer=hr-department` — **QA 평가 결과가 HR(에이전트 개선 루프)로 환류**되는 배선이 생겼다.

---

## 4. 전략 공장 — 한 실험의 생애 9단계

```
 ①리드          ②브리핑         ③기획안         ④Gate 0         ⑤가설
 스카우트가       autopilot 이     리서치 에이전트가   factory_bridge    quant.hypotheses
 문헌 수확   ─▶  원장 사실만    ─▶ MCP 도구로     ─▶ 결정론 검사    ─▶  PROPOSED 등록
 (URL 필수)      묶어 카드 게시     기획안 납품        (어휘·예산·        (계보 복사)
                                 (publish_gate)     교훈 대응)
                                                                        │
 ⑨교훈 환류      ⑧판정           ⑦실험            ⑥발주               │
 outcome+상태를   FRAGILE→REJECT  experiment-worker  job_queue 에    ◀──┘
 한 트랜잭션으로 ◀─ ROBUST→SUPPORT ◀─ 백테스트+창별   ◀─ 주문 (배분자가
 다음 브리핑이     (관문은 항상 돌아   walk-forward      직전에 1순위 수를
 읽는다            거리 노트 남김)     +자기반증          자동 등록)
```

| 단계 | 실행 주체 | 만지는 테이블 |
|---|---|---|
| ①리드 | 리서치 에이전트(스카우트) | `research.methodology_leads` ⊕ |
| ②브리핑 | `factory_autopilot` → 카드 | outcomes·experiments·leads·manifests 읽기, 칸반 ⊕ |
| ③기획안 | 리서치 에이전트 + `proposal_intake`/`publish_gate` | `research.experiment_proposals` ⊕ (PUBLISHED) |
| ④Gate 0 | `factory_bridge.gate0` | proposals 읽기 → `quant.hypotheses` ⊕ |
| ⑤~⑥발주 | `_dispatch_experiments` + **allocator** | `quant.experiment_jobs` ⊕ (QUEUED) |
| ⑦실험 | `experiment-worker` → `experiment_orchestrator` | hypotheses 상태 전이, `experiments`·`experiment_metrics` ⊕ |
| ⑧판정 | `fragility_summary` + `release_gate`(항상) | metrics 읽기 |
| ⑨환류 | `_finalize_with_feedback` → `factory_bridge.finalize` | `research.experiment_outcomes` ⊕ + hypotheses 종결 **(한 트랜잭션, 실패 시 롤백)** |

최근 들어온 능동 장치 둘:

- **배분자 `allocator.py`** — 도착 순서가 아니라 목표까지의 거리로 다음 수를 고른다. `projected_dsr` 로 자멸 수 식별, `futility`(I-SPY 2 식 10%)로 희망 없는 계열 중단, 판단이 필요 없는 "표본 확대" 수는 에이전트 대기 없이 직접 가설 등록(주기당 1건).
- **병목 인구조사 `bottleneck_census.py`** — 병목 종류를 미리 정하지 않고 실패 사유를 서명 군집으로 묶는 계수기 10종. 주기 끝에 owner 별 "공장 개선" 카드로 게시.

---

## 5. 데이터 흐름 — 누가 만들고 누가 읽나

### TimescaleDB (`market.*`) — 시장 시계열

| 테이블 | 쓰는 자 | 읽는 자 |
|---|---|---|
| `market_ticks` / `market_quotes` | ls-realtime | market-api, 퀀트 data_resolution, 아카이버 |
| `market_bars` | chart-daily(350종목 15:50) + chart-daily-universe(전종목 21:00) | market-api `/bars`, 공장 창 산수, 회계 Mark |
| `market_breadth` | breadth(10분) | market-api `/breadth` |
| `derivative_snapshots` | derivatives(10분) | ⚠ **읽는 코드 0건** — 옵션 NAV 보류의 원인 (§7) |
| `microstructure_features` | 퀀트 빌더 | 백테스트 (quotes/ticks 의 유도 대체) |
| `ingestion_watermarks` / `data_quality_windows` | data-steward(07:10) | market-api `/dq` |
| `archive_exports` | market-archive(06:50, Parquet+sha256) | 수동 retention/복구 검증 도구. 상주 Scheduler에는 없음 |

### Control DB — 업무 원장 (Supabase는 사용자 인증 전용)

| 영역 | 주요 테이블 | 쓰는 자 → 읽는 자 |
|---|---|---|
| 문서 | `research.documents` | 레거시 보존 데이터. 신규 writer 없음; `research-api` 호환 조회만 유지 |
| 재무 | `research.financial_facts` | 레거시 보존 데이터. 신규 writer 없음; 요청형 DART 응답은 쓰지 않음 |
| 거시 | `research.macro_observations` | 시장 가격인 vkospi·style-index만 갱신. ECOS·FRED·GPR·GDELT 응답은 쓰지 않음 |
| 공장 | `research.methodology_leads` / `experiment_proposals` / `experiment_outcomes` | §4 생애주기 그대로 |
| 퀀트 | `quant.hypotheses` / `experiments` / `experiment_jobs` / `experiment_metrics` / `dataset_manifests` | §4 |
| 기준정보 | `reference.instruments` / `instrument_symbols` / `market_sessions` | 시장 유니버스·거래제한·calendar 수집기 → 전 부서 |
| 원장 | `accounting.*` / `execution.*` | trading-api → outbox-relay → ledger-consumer |
| 사용자 | `accounting.investor_profiles` **[main 신설]** | portfolio-bff `/ui/investor-profiles` → accounting-api. seed 에 placeholder 회원 3명 |

### 제거된 비시장 수집기 (2026-08-18 확정 — 요청형 MCP 조회로 대체)

- 뉴스(NAVER·LS·Alpaca·Bluesky), DART 공시·재무·현금흐름·기업개황,
  macro/geopolitical, corporate-action, watchlist/capability crawler는 삭제됐다.
- DART·NAVER·ECOS·FRED·Tavily는 등록된 요청형 MCP 도구만 사용한다.
- KOSIS·GPR·GDELT·LS 뉴스는 요청형 어댑터가 없으므로 Registry가 `DISABLED`로
  fail-closed 한다. 구현되지 않은 소스를 `AVAILABLE`로 보고하지 않는다.

---

## 6. 보드와 프로필 — 파일이 어디 있나

```
공용 Kanban 보드 (SQLite WAL 1개를 전 컨테이너가 bind mount 로 공유)
  AWS 본체:   /home/ubuntu/.hermes/shared-kanban/kanban.db
  로컬:       %USERPROFILE%/.hermes-shared-kanban/kanban.db
  컨테이너 안: 부서장 8개 → /opt/kanban/kanban.db
              dispatcher·supervisor → /opt/data/shared-kanban/kanban.db

부서 프로필 (페르소나·도구·인증 — 부서마다 격리)
  AWS 본체:   /home/ubuntu/.hermes/profiles/<부서>
  로컬:       %USERPROFILE%/.hermes-<부서>   (docker-compose.override.yml 이 갈아끼움)
```

⚠ **윈도우 호스트에서 kanban.db 를 직접 열지 마라.** `-shm` 매핑 충돌로 컨테이너 쓰기 전체가 disk I/O error 로 죽는다(2026-08-11 실측). 조회는 `KANBAN_ACCESS_MODE=docker` — docker exec 경유.
⚠ **docker exec 는 반드시 `-u hermes`.** root 로 만지면 WAL 이 root 소유가 되어 에이전트(uid 1000)의 `kanban_complete` 가 전부 실패한다.
⚠ `docker-compose.override.yml` 은 로컬 전용, **커밋 금지**(.gitignore 등재). 리눅스에서 `${USERPROFILE}` 이 빈 값으로 풀려 프로필 없는 껍데기가 된다.

---

## 7. 알려진 구멍 — 지도에 표시해 둔 공사 구간

| # | 구멍 | 상태 |
|---|---|---|
| 1 | `quant-api` 고아 컨테이너 — compose 정의 없음, 소스는 `codex-research-wip-20260811` 브랜치 | 정의 복원 or 폐기 결정 필요 |
| 2 | `market.derivative_snapshots` 읽기 코드 0건 → 옵션 보유 book 2개 NAV 보류 무한 루프 | market-api `/snapshot` 파생 분기 필요 |
| 3 | ~~`ls-news` 재접속 루프~~ | ✅ 서비스·수집기 제거. 요청형 어댑터도 없으므로 Registry `DISABLED` |
| 4 | 런타임 프로필 8개 전부 저장소 `hermes/config.yaml` 과 크기 불일치 (트레이딩은 47%) | 동기화 + 대조 검사 필요 |
| 5 | CEO 페르소나의 부서 목록에 `workforce-management` — 유효명은 `hr-department` | 아직 안 터진 지뢰 |
| 6 | 퀀트 공장 워커 3명(접수·설계·교훈) `pending_hr` 미구현 — 결정론 코드가 대행 중 | 편제 결정 필요 |
| 7 | `ceo-kanban-supervisor` `canonical abort failed: exited 1` 반복 | [main] `55b956b`(parentless task projection 수정)이 관련 가능성 — 새 코드 반영 후 재관측 필요 |
| 8 | ~~로컬 실행 스택 60커밋 지연~~ | ✅ **2026-08-13 해소** — origin/main 병합(`d20bfa1`) + 재기동, 38/38 실행. 8001 은 portfolio-bff 로 인계 완료 |
| 9 | ~~`Dockerfile.hermes-discord` 부재~~ | ✅ **2026-08-13 해소** — `2ea7342` 로 Dockerfile 이 커밋됐고, 설계도 바뀌었다: **기본 compose 는 표준 이미지**(`nousresearch/hermes-agent`)로 돌고, Discord 멱등 이미지는 중복 전달이 실측될 때만 얹는 **선택 오버레이**(`docker-compose.discord-idempotency.yml`)가 됐다. 로컬 `.env` 핀도 제거함 — 현재 로컬 = GitHub 구성 그대로 |
| 10 | 팀원 소유 테스트 실패 2건 — `test_unavailable_aws_only_sources…`(하드코딩 목록), `test_task_status_route_reads_planning_projection`(origin/main 단독 재현) | 소유자 수리 대기 |

---

## 부록 — 포트 지도 한 장

| 포트 | 서비스 | 노출 |
|---|---|---|
| 8001 | **[main] portfolio-bff** (컨테이너 8000) / 로컬 현행은 ui-bff | 호스트 전체 (프런트 진입점) |
| 8036 | market-api | 0.0.0.0 (팀원 공유, Tailscale 전제) |
| 5434 | timescaledb | 0.0.0.0 (팀원 읽기 계정 `hedgefund_ro`) |
| 8035 | research-api | 127.0.0.1 |
| 8037 | quant-api(고아) / research-mcp(내부) | 127.0.0.1 / 미게시 |
| 8041~8046 | risk / audit / governance / workforce / trading / accounting | 전부 127.0.0.1, 컨테이너 내부는 8000 |
| 8642 | ceo-hermes 내부 API | 미게시 (컨테이너 간) |
| 6379 | redis | 미게시 |
