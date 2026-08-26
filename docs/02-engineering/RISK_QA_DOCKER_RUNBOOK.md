# Risk·AI QA/감사본부 Docker 컨테이너 명세서

담당: 동규 (리스크/QA) · 작성: 2026-08-03 · 기준 갱신: 2026-08-26

현재 서비스·Profile·포트의 기준은 [LOCAL_COMPOSE_RUNTIME_BASELINE.md](LOCAL_COMPOSE_RUNTIME_BASELINE.md)이며, 이 문서는 Risk·QA 서비스의 상세 운영 절차만 다룬다.
근거: [DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md](DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md) 6.4·6.7·9·10절, [HERMES_DOCKER_RUNBOOK.md](HERMES_DOCKER_RUNBOOK.md)(재일, Hermes 공통 절차)

Hermes provider credential·프로필 동기화·모델/과금 같은 8부서 공통 절차는 여기서 반복하지 않는다.
[HERMES_DOCKER_RUNBOOK.md](HERMES_DOCKER_RUNBOOK.md)의 공통 절차를 그대로 따른다.
이 문서는 아래 표의 Risk·QA 서비스와 공통 Redis 운영 절차만 다룬다. 전체 서비스
수는 [Local Compose Runtime Baseline](LOCAL_COMPOSE_RUNTIME_BASELINE.md)이 소유한다.

## 1. 컨테이너 목록

| 서비스 | 이미지 | 역할 | 소유 |
|---|---|---|---|
| `redis` | `redis:7-alpine` | Risk→QA Event Stream 공통 인프라 (부서 소유 아님) | 공통 |
| `risk-api` | `departments/03-risk/Dockerfile` 빌드 | Pre-trade Risk Check, Trading State, Compliance 조회면 | Risk |
| `audit-api` | `departments/06-ai-qa-audit/Dockerfile` 빌드 | Evidence QA, Trace, Tool Permission, Finding, Incident 조회면 | QA |
| `qa-worker` | `audit-api`와 동일 이미지, command만 다름 | Risk Decision Stream → QA 감사 이력 적재 (`qa_events/worker.py`) | QA |
| `qa-reproduction-worker` | `audit-api`와 동일 이미지, command만 다름 | 승인된 주식 포워드 PASS를 별도 권한·별도 프로세스에서 재실행 | QA |
| `risk-hermes` | `nousresearch/hermes-agent:latest` | Risk 부서 Supervisor | Risk |
| `qa-hermes` | `Dockerfile.hermes-discord`로 빌드한 `hedgefund-hermes-discord:qa-feedback-v1` | QA 부서 Supervisor와 Discord feedback bridge | QA |

`risk-api`·`audit-api`·`qa-worker`는 research 계열과 달리 **Build Context가 부서 폴더가 아니라 저장소 루트(`.`)** 다.
`api/app.py`가 `departments/02-trading/contracts`, `skills/agentic-rag`, `apps/observability`를 부서 경계 밖에서 import하고
`_REPO_ROOT = Path(__file__).resolve().parents[3]`로 저장소 루트를 `sys.path`에 넣는 전제이기 때문이다 — 각 Dockerfile
상단 주석에 상세 근거가 있다. 저장소 루트 `.dockerignore`가 없으면 `ai-office/node_modules`(~760MB)까지 빌드마다
Docker Daemon에 올라가므로, 두 부서 Dockerfile을 쓸 때는 반드시 `.dockerignore`가 함께 있어야 한다.

## 2. Backend Image 상세 (risk-api / audit-api / qa-worker / qa-reproduction-worker)

### 2.1 risk-api

| 항목 | 값 |
|---|---|
| Dockerfile | `departments/03-risk/Dockerfile` |
| Build Context | 저장소 루트 |
| COPY 대상 | `departments/03-risk`, `departments/02-trading/contracts`, `skills/agentic-rag`, `apps/observability` |
| Command | `uvicorn app:app --app-dir departments/03-risk/api --host 0.0.0.0 --port 8000` |
| 내부 포트 | 8000 |
| 호스트 매핑 | `127.0.0.1:8041:8000` (로컬 전용, 외부 노출 금지) |
| 필수 환경변수 | 없음 — `DATABASE_URL`/`REDIS_URL`/`RISK_QA_EVENT_REDIS_URL`/`OPENAI_API_KEY` 모두 함수 내부에서 지연 조회, 없어도 기동은 됨 |
| 의존 | `redis` (Compliance 조회 시 `OPENAI_API_KEY` 필요) |

### 2.2 audit-api

| 항목 | 값 |
|---|---|
| Dockerfile | `departments/06-ai-qa-audit/Dockerfile` |
| Build Context | 저장소 루트 |
| COPY 대상 | `departments/06-ai-qa-audit`, 동결 evaluator용 `departments/04-quant-backtest/pipeline`·`departments/01-research/contracts`, `skills/agentic-rag`, `apps/observability`, `apps/security`, `orchestration` |
| Command | `uvicorn app:app --app-dir departments/06-ai-qa-audit/api --host 0.0.0.0 --port 8000` |
| 내부 포트 | 8000 |
| 호스트 매핑 | `127.0.0.1:8042:8000` (로컬 전용, 외부 노출 금지) |
| 필수 환경변수 | 없음 — `DATABASE_URL` 선택, `RISK_QA_EVENT_REDIS_URL`/`REDIS_URL` 선택 |
| 의존 | `redis` |

### 2.3 qa-worker

`audit-api`와 같은 이미지, `command`만 바뀐다 (계획서 3.1절 "부서당 Image 1개, command로 역할 분리").

| 항목 | 값 |
|---|---|
| Dockerfile | `departments/06-ai-qa-audit/Dockerfile` (audit-api와 공유) |
| Command | `python qa_events/worker.py` |
| working_dir | `/app/departments/06-ai-qa-audit` |
| 포트 | 없음 (백그라운드 Consumer, HTTP 서버 아님) |
| 필수 환경변수 | DB는 `RISK_QA_DATABASE_URL` 또는 `DATABASE_URL`, 이벤트 버스는 `RISK_QA_EVENT_REDIS_URL` 또는 `REDIS_URL`이 필요하다. 하나라도 없으면 `worker.py`가 즉시 `SystemExit`로 종료된다. compose는 `DATABASE_URL`과 내부 Redis 기본값을 전달한다 |
| 동작 | Risk 이벤트와 `quant.intraday.forward.qa_requested.v1`을 각각의 Redis Stream consumer group에서 읽는다. Quant PASS는 DB의 정본 outbox와 대조한 뒤 재현 요청·별도 작업 큐만 원자적으로 기록하며, 인라인 백테스트나 자동 승격은 하지 않는다 |
| 의존 | `redis`, Supabase PostgreSQL(`20260818000300_intraday_forward_qa_dispatch.sql` 적용 필수) |

### 2.4 qa-reproduction-worker

Quant 프로세스가 만든 결과를 그대로 승인하지 않는다. `qa-worker`가 정본 outbox를
검증해 작업 큐에 넣은 뒤, 이 프로세스가 동결된 보고서·수식·비용모형·소스 지문과
정확한 KRX ACTIVE STOCK 세션을 다시 읽어 독립적으로 계산한다.

| 항목 | 값 |
|---|---|
| Dockerfile | `departments/06-ai-qa-audit/Dockerfile` (audit-api와 공유) |
| Command | `python qa_events/reproduction_worker.py` |
| working_dir | `/app/departments/06-ai-qa-audit` |
| DB 권한 | metadata DB는 `svc_qa_reproducer`; 시장 DB는 강제 read-only |
| 필수 마이그레이션 | `20260818000300`부터 `20260818000700`까지 순서대로 적용 |
| 임대 | 30~7,200초, heartbeat/complete/fail 모두 lease token으로 fencing |
| 결과 권한 | `promotion_authority=false`; 불일치는 감사 결과일 뿐 자동 승격하지 않음 |
| 준비성 | claim API를 트랜잭션 안에서 실행 후 rollback하고 시장 DB read-only를 확인 |
| Compose 의존 | `timescaledb` healthcheck. Metadata/session DB는 URL과 준비성 검사로 fail-closed하며 Compose `depends_on` 대상은 아님 |

`DATABASE_SESSION_URL`을 우선 사용한다. Supavisor transaction pool 주소만 있으면
`SET ROLE`이 요청 사이에 유지되지 않을 수 있으므로 준비성 검사가 fail-closed된다.
운영에서는 `RISK_QA_DATABASE_SESSION_URL`과 전용 LOGIN/role grant를 제공한다.

## 3. Hermes 컨테이너 (risk-hermes / qa-hermes)

Backend Image에 Hermes를 넣지 않고 별도 컨테이너로 띄운다. Risk는 upstream 이미지를 사용하고, QA는 Discord feedback bridge가 포함된 저장소 이미지를 사용한다.

| 항목 | risk-hermes | qa-hermes |
|---|---|---|
| 이미지 | `nousresearch/hermes-agent:latest` | `hedgefund-hermes-discord:qa-feedback-v1` (`Dockerfile.hermes-discord`) |
| volume | `/home/ubuntu/.hermes/profiles/risk-management:/opt/data` | `/home/ubuntu/.hermes/profiles/qa-department:/opt/data` |
| Provider | Codex(`config.yaml`+`auth.json`, 하드코딩 안 함) | Codex(`config.yaml`+`auth.json`, 하드코딩 안 함) |
| Compose command | `sleep infinity` | `sleep infinity` |
| Gateway lifecycle | 이미지의 s6 lifecycle이 관리 | 이미지의 s6 lifecycle이 관리 |

Provider는 컨테이너 env가 아니라 각 Profile의 `/opt/data/config.yaml`(`provider:`)과 credential 파일이 결정한다.
Compose의 `sleep infinity`는 gateway 명령을 직접 소유하지 않으며, 이미지 bootstrap과 s6 lifecycle이 gateway를 관리한다.
Credential 취급과 프로필 동기화(`sync_hermes_profiles.sh`)는 [HERMES_DOCKER_RUNBOOK.md](HERMES_DOCKER_RUNBOOK.md)를 따른다.

## 4. 자원 한도

전 서비스 공통으로 `mem_limit`/`cpus`/`pids_limit`을 건다 (로컬 P0 정책, 계획서 11절).

| 서비스 | mem_limit | cpus | pids_limit | 비고 |
|---|---|---:|---:|---|
| `redis` | 256m | 0.5 | 64 | AOF `everysec` + 명명 volume `redis_data`로 Stream 영속화 |
| `risk-api` | 256m | 0.5 | 64 | |
| `audit-api` | 256m | 0.5 | 64 | |
| `qa-worker` | 256m | 0.3 | 64 | `stop_grace_period: 20s` — 소비 중인 이벤트가 있으면 강제 종료 전에 끝내도록 |
| `qa-reproduction-worker` | 2g | 1.0 | 128 | 장시간 포워드 재실행; lease와 statement timeout으로 상한 제한 |
| `risk-hermes` | 1g (reservation 192m) | 1.0 | 256 | |
| `qa-hermes` | 1g (reservation 192m) | 1.0 | 256 | |

## 5. 실행·검증

```bash
# 문법 검증 (Docker Daemon 불필요)
docker compose config --quiet
docker compose config --services

# 리스크·QA 컨테이너만 올리기
docker compose up -d redis risk-api audit-api qa-worker qa-reproduction-worker risk-hermes qa-hermes

# risk-api / audit-api 헬스 확인
curl -s http://127.0.0.1:8041/ | head -1
curl -s http://127.0.0.1:8042/ | head -1

# 세 QA 프로세스가 실제 준비성 검사를 통과했는지 확인
# qa-worker는 Redis뿐 아니라 scoped DB role과 transaction-local read-only
# outbox SELECT도 검사하므로 metadata DB 장애 시 unhealthy로 전환된다.
docker compose ps audit-api qa-worker qa-reproduction-worker
docker exec hedgefund-redis redis-cli CONFIG GET appendonly appendfsync

# qa-worker가 Risk 이벤트를 잘 받는지는 QA 자체 통합 테스트로 확인한다
python -m pytest departments/06-ai-qa-audit/tests/test_redis_event_bus_integration.py -q
```

## Model readiness와 Redis startup contract

Risk·QA Worker의 운영 모델, endpoint와 local fallback은
[Worker Model Matrix](WORKER_MODEL_MATRIX.md)와
[Final Runtime Architecture](FINAL_RUNTIME_ARCHITECTURE.md)가 소유한다. 이 Runbook은
모델명을 복사하지 않고 아래 읽기 전용 preflight로 현재 환경의 준비성을 확인한다.

Redis는 healthcheck가 통과한 뒤 Risk API, QA API, QA Worker가 시작한다.

기본 Redis는 `appendonly yes`, `appendfsync everysec`, 명명 volume `redis_data`를
사용한다. 포워드 QA stream은 미소비 감사 요청을 길이 제한으로 제거하지 않는다.
DB·Redis 같은 의존성 장애는 횟수와 무관하게 pending에서 지수 백오프로 재시도하고,
형식 오류·지원하지 않는 event type·정본 충돌만 제한된 횟수 뒤 DLQ로 보낸다.
Outbox 재조정 발행은 event ID별 원자적 `XADD + marker`라 장기 장애 중에도 같은
메시지와 AOF가 무한 증가하지 않는다.

    .venv/bin/python scripts/run_risk_qa_production_preflight.py \
      --as-of "$(date -u +%F)"

모델 preflight가 실패하면 해당 환경의 모델 정본과 artifact 준비 절차를 확인한다.
정책 Corpus가 `SAMPLE_PLACEHOLDER`인 동안에는 QA Production 승격을 수행하지 않는다.

## 6. 알려진 미결 (기록만, 코드 아님)

- **Risk input projection 배포 미연결** — `risk_events/projection_worker.py`와 단위 테스트는 구현돼 있고
  `market.snapshot.v1`·`portfolio.snapshot.v1`·`governance.mandate.changed.v1`을 `risk.input_snapshots`에
  투영한다. 그러나 루트 Compose에는 아직 `risk-projection-worker` 서비스가 없다. Governance API에는
  Mandate event publisher가 있지만 Market·Portfolio snapshot의 운영 Redis publisher와 세 stream의 통합
  기동 증거는 완성되지 않았다. 따라서 현재 상태는 코드 구현 완료가 아니라 **배포·업스트림 연결 대기**로
  판정한다. 동기 `evaluate_p1_gate` 경로와 이 비동기 투영 경로를 같은 완료 증거로 사용하지 않는다.
- `USERPROFILE` 미설정 경고 — macOS 개발 환경에서 `docker compose config` 실행 시 뜬다. Windows 배포 대상
  volume 경로 템플릿이라 발생하는 기존 동작이며(research-hermes/quant-hermes도 동일), 기능에는 영향 없다.
