# Local Compose Runtime Baseline

검토 기준: 2026-08-11 (KST)

이 문서는 루트 [`docker-compose.yml`](../../docker-compose.yml)의 실제 로컬 개발·통합 Runtime을 설명한다. 서비스 수·포트·Profile이 이 문서와 다르면 Compose 파일을 먼저 확인하고 문서를 갱신한다.

## 1. 기준과 경계

- Compose project name은 `hedgefund`다.
- 시장 시계열 DB는 `hedgefund_tsdb_data` named volume과 호스트 포트 `5434`를 사용한다. 다른 `trading` Compose 프로젝트와 DB·Network·Volume을 공유하지 않는다.
- AWS로 옮길 때도 서비스·권한 경계를 유지하고 Secret·Network·Volume·Backup만 환경에 맞게 바꾼다. AWS Production 토폴로지와 GPU Model Gateway의 목표는 [`FINAL_RUNTIME_ARCHITECTURE.md`](FINAL_RUNTIME_ARCHITECTURE.md)가 다룬다.
- 포함된 Compose Fragment는 CEO Office, Trading, Accounting/Portfolio, Agent Workforce가 소유한다. Root Compose는 공통 Platform과 Research·Quant·Risk·QA·Dashboard를 조립한다.
- Frontend는 현재 `ai-office/`를 실행 기준으로 사용한다. Frontend는 BFF와 Hermes Dashboard만 호출하며 Supabase Service Role, Broker·LS Credential, Kanban SQLite를 직접 다루지 않는다.

## 2. 현재 서비스 수

`docker compose config --services` 기준 서비스 수는 다음과 같다. Profile을 켜도 기존 서비스가 중복 생성되지 않는다.

| 명령 | 서비스 수 | 추가 서비스 |
|---|---:|---|
| `docker compose config --services` | 30 | 기본 통합 Runtime |
| `docker compose --profile portfolio config --services` | 32 | `portfolio-bff`, `portfolio-worker` |
| `docker compose --profile dashboard config --services` | 31 | `hermes-dashboard` |
| `docker compose --profile portfolio --profile dashboard config --services` | 33 | Portfolio + Dashboard |
| `docker compose --profile research-skills config --services` | 32 | `paper-search-mcp`, `youtube-transcript-mcp` |

`config --services`는 선언·include·interpolation 검증일 뿐 컨테이너가 실행 중이라는 뜻은 아니다. 실행 여부는 `docker compose ps`, Healthcheck, API smoke test로 확인한다.

## 3. 서비스 배치

### Market/Data Plane

| 서비스 | 역할 |
|---|---|
| `timescaledb` | 로컬 시장 시계열 DB. 호스트 `0.0.0.0:5434` → 컨테이너 `5432` |
| `news-watcher` | NAVER 뉴스 수집 및 Research DB 적재 |
| `ls-realtime` | LS 실시간 호가·체결 수집 및 TimescaleDB 적재 |
| `ls-news` | LS NWS 뉴스 메타데이터 수집 |
| `batch-collectors` | 공시·Breadth·Calendar·거시·재무·Corporate Action 배치 |
| `market-api` | TimescaleDB Snapshot·Bar·Breadth·DQ Read API. 호스트 `0.0.0.0:8036` |
| `research-api` | Evidence·PIT Read API. 호스트 `127.0.0.1:8035` |
| `research-mcp` | Research Tool Gateway. 호스트 포트 미공개 |
| `paper-search-mcp`, `youtube-transcript-mcp` | Research 방법론 스카우트 도구. `research-skills` Profile 전용 |

Collector와 Research Data Plane만 TimescaleDB Credential을 가진다. 다른 부서는 `market-api`와 `research-api`를 사용한다.

### Hermes·Kanban Plane

| 서비스 | 역할 |
|---|---|
| `ceo-hermes`, `research-hermes`, `quant-hermes`, `trading-hermes` | CEO·Research·Quant·Trading Department Head |
| `risk-hermes`, `qa-hermes`, `accounting-hermes`, `workforce-hermes` | Risk·QA·Accounting/Portfolio·Agent Workforce Department Head |
| `kanban-dispatcher` | 공용 Hermes Kanban Dispatcher. 호스트 포트 미공개 |
| `hermes-dashboard` | Hermes 공식 Dashboard와 Kanban 운영 콘솔. `dashboard` Profile, 호스트 `127.0.0.1:9119` |

8개 Hermes 실행 컨테이너는 각자 `/home/ubuntu/.hermes/profiles/<profile>:/opt/data`와 공용 `/home/ubuntu/.hermes/shared-kanban:/opt/kanban`을 마운트한다. Dashboard는 공용 `/home/ubuntu/.hermes:/opt/data`를 사용하고 `HERMES_KANBAN_HOME=/opt/data/shared-kanban`으로 같은 보드를 명시한다.

Hermes Dashboard 자체가 Kanban의 공식 UI다. AI Office는 보드를 복제하지 않고 `NEXT_PUBLIC_HERMES_DASHBOARD_URL`을 iframe으로 표시한다. Browser와 BFF는 `kanban.db`를 직접 읽거나 수정하지 않는다.

### Domain·Control Plane

| 서비스 | 역할 | 호스트 포트 |
|---|---|---:|
| `governance-api` | CEO Mandate·Approval·Governance API | `127.0.0.1:8043` |
| `workforce-api` | Agent Workforce API | `127.0.0.1:8044` |
| `trading-api` | Paper OMS·Trading Read/Command 경계 | `127.0.0.1:8045` |
| `accounting-api` | Accounting/Portfolio Read API | `127.0.0.1:8046` |
| `risk-api` | 결정론 Risk Check·Compliance Read API | `127.0.0.1:8041` |
| `audit-api` | QA·Evidence·Trace·Finding Read API | `127.0.0.1:8042` |
| `redis` | Risk·QA·Governance Event Stream 및 공통 Queue | 호스트 포트 미공개 |
| `qa-worker` | Risk Decision Event를 QA 이력으로 적재 | 호스트 포트 미공개 |
| `notification-worker` | Governance/Risk/QA 알림 Event 소비 | 호스트 포트 미공개 |
| `improvement-worker` | Workforce 개선 후보 Event 소비 | 호스트 포트 미공개 |
| `accounting-ledger-consumer` | Accounting Ledger Event 소비 | 호스트 포트 미공개 |
| `accounting-close-scheduler` | Accounting Close 작업 스케줄 | 호스트 포트 미공개 |
| `trading-outbox-relay` | Trading Outbox Event Relay | 호스트 포트 미공개 |

### Operator BFF

`portfolio-bff`는 `portfolio` Profile에서만 실행하는 `apps/api` FastAPI BFF다. 호스트 `${PORTFOLIO_BFF_PORT:-8001}` → 컨테이너 `8000`으로 게시한다.

주요 경로:

- `GET /ui/snapshot`: 금융 Read Model과 운영 Projection
- `GET /ws/operations`: `agent.status.v1`·sequence 기반 운영 Event
- `POST /ui/ceo/ask`: 자연어 질의를 CEO Hermes에 전달하고, Hermes CLI가 사용 가능하면 공유 Kanban Task를 생성
- `POST /ui/portfolio-recommendations`: 비구속 포트폴리오 추천 실행
- `GET /health`, `GET /health/ready`: BFF와 의존성 상태

`/ui/ceo/ask`는 주문·Risk 승인·Ledger Posting을 수행하지 않는다. Agent 응답은 금융 수치의 Source of Truth가 아니며, 수치는 `/ui/snapshot`과 각 Domain API가 소유한다.

## 4. AI Office 연결

호스트 개발 실행:

```bash
DATABASE_URL='' .venv/bin/python -m uvicorn apps.api.main:app --reload --port 8001
NEXT_PUBLIC_BFF_URL=http://127.0.0.1:8001 \
NEXT_PUBLIC_HERMES_DASHBOARD_URL=http://127.0.0.1:9119 \
npm --prefix ai-office run dev -- --port 3002
```

AI Office의 메인 대표 지시창은 `POST /ui/ceo/ask`를 사용한다. Hermes CLI가 설치되어 있고 `ENABLE_AGENT_ASK=true`일 때 CEO Head 응답을 표시한다. `ENABLE_KANBAN_TASK_TRACKING=true`이면 BFF가 Hermes CLI를 통해 `/home/ubuntu/.hermes/shared-kanban` 보드에 사용자 질의 Task를 기록한다.

Dashboard 화면은 `NEXT_PUBLIC_HERMES_DASHBOARD_URL`에 있는 Hermes 공식 Dashboard를 그대로 표시한다. Dashboard Profile과 자체 인증이 준비되지 않은 경우 AI Office는 연결 안내를 표시하며 자체 가짜 Kanban으로 대체하지 않는다.

## 5. 시작·중지 명령

```bash
docker compose up -d
docker compose logs -f timescaledb
docker compose --profile portfolio up -d
docker compose --profile dashboard up -d
docker compose --profile portfolio --profile dashboard up -d
docker compose ps
docker compose down       # 컨테이너만 제거, named volume 유지
docker compose down -v    # 데이터까지 삭제
```

Dashboard와 Kanban을 처음 쓰는 호스트에서는 먼저 공용 디렉터리를 준비한다.

```bash
mkdir -p /home/ubuntu/.hermes/shared-kanban
docker compose --profile dashboard up -d hermes-dashboard kanban-dispatcher
```

Dashboard는 자체 인증 없이는 운영 화면으로 공개하지 않는다. 공유기 Port Forwarding은 금지하고, 접근 경로는 로컬·Reverse Proxy·승인된 사설망으로 제한한다.

## 6. 문서 우선순위

1. Root Compose와 포함된 Fragment
2. 이 문서
3. [`FINAL_RUNTIME_ARCHITECTURE.md`](FINAL_RUNTIME_ARCHITECTURE.md)의 목표 Runtime
4. [`DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md`](DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)의 이행 계획
5. [`HEDGE_FUND_MASTER_PLAN.md`](../HEDGE_FUND_MASTER_PLAN.md)의 제품·통제 원칙

Production 목표와 현재 Local 구현을 같은 상태로 표현하지 않는다. 구현 여부는 [`PROJECT_IMPLEMENTATION_STATUS.md`](../PROJECT_IMPLEMENTATION_STATUS.md)의 `CONFIG_VERIFIED`, `TEST_VERIFIED`, `RUNTIME_VERIFIED`를 구분해 기록한다.
