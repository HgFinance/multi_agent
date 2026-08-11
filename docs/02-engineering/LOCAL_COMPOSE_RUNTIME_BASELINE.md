# 로컬 통합 Compose Runtime 기준선

> 기준일: 2026-08-10
>
> 이 문서는 루트 [`docker-compose.yml`](../../docker-compose.yml)의 현재 로컬 개발·통합 Runtime을 설명한다. 문서에 적힌 서비스 수나 실행 상태가 이 파일과 다르면 Compose 파일과 이 문서를 먼저 확인한다.

## 1. 적용 범위

루트 Compose는 `name: hedgefund`를 사용한다. 같은 머신의 별도 `trading` Compose 프로젝트와는 프로젝트 이름, 컨테이너 이름, 네트워크, 볼륨, 포트를 분리한다. 두 프로젝트의 DB를 공유하지 않는다.

이 파일은 로컬 개발·통합용이다. Production 배포 토폴로지, Cloud Provider, GPU Model Gateway, Broker Live Credential을 확정하는 문서가 아니다. Production으로 옮길 때는 동일한 서비스·권한 경계를 보존하되, Secret 주입·네트워크·볼륨·관측성·백업·SLO를 별도 배포 설계로 검증한다.

## 2. Compose 구성

루트 파일은 현재 다음 네 개의 부서 Fragment를 `include`한다.

| Fragment | 소유 범위 |
|---|---|
| `departments/00-ceo-office/compose.yaml` | `governance-api`, `notification-worker`, `ceo-hermes` |
| `departments/02-trading/compose.yaml` | `trading-api`, `trading-hermes` |
| `departments/05-accounting-portfolio/compose.yaml` | `accounting-api`, `accounting-hermes` |
| `departments/07-agent-workforce/compose.yaml` | `workforce-api`, `improvement-worker`, `workforce-hermes` |

Research, 공통 Platform, Risk·QA, Portfolio BFF와 Dashboard의 루트 서비스는 아직 루트 파일이 소유한다. 모든 8개 부서가 Fragment를 갖는다고 문서에 쓰지 않는다.

현재 Compose 병합 결과는 다음과 같다.

| 기동 방식 | Compose 서비스 수 | 의미 |
|---|---:|---|
| `docker compose config --services` | 26 | 기본 서비스. `portfolio`, `dashboard` Profile 제외 |
| `docker compose --profile portfolio config --services` | 28 | Portfolio BFF·Worker 추가 |
| `docker compose --profile dashboard config --services` | 27 | 공용 Hermes Dashboard 추가 |
| `docker compose --profile portfolio --profile dashboard config --services` | 29 | 모든 현재 Profile 포함 |

Profile 서비스는 기본 기동에 포함되지 않는다.

## 3. 서비스와 권한 경계

### 3.1 Research·Quant 및 공통 Platform

| 서비스 | 책임 |
|---|---|
| `timescaledb` | `market` 시계열 DB. named volume `tsdb_data` 사용 |
| `news-watcher` | NAVER 뉴스 수집·Research 적재 |
| `ls-realtime` | LS 실시간 호가·체결 수집·TimescaleDB 적재 |
| `batch-collectors` | 공시·Breadth·Calendar·거시·재무·Corporate Action 등 배치 |
| `ls-news` | LS NWS 실시간 뉴스 메타 수집 |
| `market-api` | TimescaleDB Snapshot·Bar·Breadth·DQ read API |
| `research-api` | Evidence·PIT 조회 API 및 Tool Gateway |
| `research-mcp` | Research 도구 면. DB 직접 접근 대신 API를 호출 |
| `research-hermes` | Research 부서 Hermes Supervisor |
| `quant-hermes` | Quant 부서 Hermes Supervisor |
| `redis` | Risk·QA 및 향후 공통 Event/Queue Platform. Canonical 원장이 아님 |

TimescaleDB Credential은 Collector와 Research/Quant Data Plane에만 둔다. 다른 본부와 팀원은 `market-api`를 사용한다. `research-mcp`에는 LS Credential을 주지 않는다.

### 3.2 Risk·QA

| 서비스 | 책임 |
|---|---|
| `risk-api` | Pre-trade Risk Check·Trading State·Compliance 조회면 |
| `risk-hermes` | Risk Supervisor |
| `audit-api` | Evidence QA·Trace·Tool Permission·Finding 조회면 |
| `qa-worker` | Risk Decision Event를 QA 수신 이력으로 적재 |
| `qa-hermes` | AI QA·감사 Supervisor |

Risk Engine이 binding 판정을 소유하고 Risk Agent는 근거와 권고만 만든다. QA는 Risk와 권한을 합치지 않는다.

### 3.3 Trading·Accounting·CEO·Workforce

현재 Compose에 다음 서비스가 선언돼 있다.

| 영역 | API/Worker | Hermes |
|---|---|---|
| CEO Office | `governance-api`, `notification-worker` | `ceo-hermes` |
| Trading | `trading-api` | `trading-hermes` |
| Accounting/Portfolio | `accounting-api` | `accounting-hermes` |
| Agent Workforce | `workforce-api`, `improvement-worker` | `workforce-hermes` |

이 선언은 Container·API 연결 상태를 뜻하지, Paper Investment의 전체 폐쇄 루프가 완성됐다는 뜻은 아니다. Order→Risk→Fill→Journal→Position/NAV의 Canonical Row와 Acceptance Scenario는 별도 검증 대상이다.

### 3.4 선택 서비스

| 서비스 | Profile | 책임 |
|---|---|---|
| `portfolio-bff` | `portfolio` | Read/advisory BFF. 기본적으로 Worker를 임베드하지 않음 |
| `portfolio-worker` | `portfolio` | Durable SQLite queue claim worker |
| `hermes-dashboard` | `dashboard` | 공용 Hermes 운영·Kanban Dashboard. RW Profile 상태를 다루므로 인증 필수 |

## 4. 로컬 네트워크·포트

| 대상 | Host Binding | 정책 |
|---|---|---|
| TimescaleDB | `0.0.0.0:5434:5432` | Tailscale 사설망 전제. 공유기 Port Forwarding 금지 |
| `market-api` | `0.0.0.0:8036:8036` | 팀원 조회용 read API |
| `research-api` | `127.0.0.1:8035:8035` | 로컬 전용 |
| `risk-api` | `127.0.0.1:8041:8000` | 로컬 전용 |
| `audit-api` | `127.0.0.1:8042:8000` | 로컬 전용 |
| CEO/Trading/Accounting/Workforce API | `127.0.0.1:8043`~`8046` | 로컬 전용 |
| `portfolio-bff` | `${PORTFOLIO_BFF_PORT:-8001}:8000` | `portfolio` Profile에서만 |
| `hermes-dashboard` | `127.0.0.1:9119:9119` | `dashboard` Profile에서만. 자체 인증·Tailscale 전제 |

TimescaleDB 데이터는 저장소 bind mount가 아닌 named volume에 둔다. `docker compose down`은 데이터를 보존하고, `docker compose down -v`만 볼륨을 삭제한다.

## 5. 검증 명령

Secret 값을 출력하지 않고 Compose 병합 결과만 확인한다.

```bash
docker compose config --services
docker compose --profile portfolio --profile dashboard config --services
docker compose config
docker compose up -d
docker compose ps
```

`config --services` 통과는 선언·include·interpolation 검증이며, 컨테이너가 실제로 실행 중이라는 증거가 아니다. 실제 상태는 `docker compose ps`, healthcheck, API smoke test와 DB 입출력으로 별도 기록한다.

## 6. 문서 우선순위

1. 루트 `docker-compose.yml` 및 포함된 Fragment
2. 이 문서
3. [`FINAL_RUNTIME_ARCHITECTURE.md`](FINAL_RUNTIME_ARCHITECTURE.md)의 Local Runtime 절
4. [`DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md`](DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)의 목표·이행 계획
5. [`HEDGE_FUND_MASTER_PLAN.md`](../HEDGE_FUND_MASTER_PLAN.md)의 제품·통제 원칙과 Production 목표

Master Plan이 현재 Compose의 서비스 존재 여부를 직접 재정의하지 않는다. 현재 구현 상태는 [`PROJECT_IMPLEMENTATION_STATUS.md`](../PROJECT_IMPLEMENTATION_STATUS.md)에서 `CONFIG_VERIFIED`, `TEST_VERIFIED`, `RUNTIME_VERIFIED`를 구분해 기록한다.
