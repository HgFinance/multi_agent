# Risk·AI QA/감사본부 Docker 컨테이너 명세서

담당: 동규 (리스크/QA) · 작성: 2026-08-03
근거: [DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md](DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md) 6.4·6.7·9·10절, [HERMES_DOCKER_RUNBOOK.md](HERMES_DOCKER_RUNBOOK.md)(재일, Hermes 공통 절차)

Hermes 로그인·프로필 동기화·모델/과금 같은 8부서 공통 절차는 여기서 반복하지 않는다.
[HERMES_DOCKER_RUNBOOK.md](HERMES_DOCKER_RUNBOOK.md) 4-3(로그인)·5(모델/과금)를 그대로 따른다.
이 문서는 리스크·QA 소유 컨테이너 6개(`redis` 포함)만 다룬다.

## 1. 컨테이너 목록

| 서비스 | 이미지 | 역할 | 소유 |
|---|---|---|---|
| `redis` | `redis:7-alpine` | Risk→QA Event Stream 공통 인프라 (부서 소유 아님) | 공통 |
| `risk-api` | `departments/03-risk/Dockerfile` 빌드 | Pre-trade Risk Check, Trading State, Compliance 조회면 | Risk |
| `audit-api` | `departments/06-ai-qa-audit/Dockerfile` 빌드 | Evidence QA, Trace, Tool Permission, Finding, Incident 조회면 | QA |
| `qa-worker` | `audit-api`와 동일 이미지, command만 다름 | Risk Decision Stream → QA 감사 이력 적재 (`qa_events/worker.py`) | QA |
| `risk-hermes` | `nousresearch/hermes-agent:latest` | Risk 부서 Supervisor | Risk |
| `qa-hermes` | `nousresearch/hermes-agent:latest` | QA 부서 Supervisor | QA |

`risk-api`·`audit-api`·`qa-worker`는 research 계열과 달리 **Build Context가 부서 폴더가 아니라 저장소 루트(`.`)** 다.
`api/app.py`가 `departments/02-trading/contracts`, `skills/agentic-rag`, `apps/observability`를 부서 경계 밖에서 import하고
`_REPO_ROOT = Path(__file__).resolve().parents[3]`로 저장소 루트를 `sys.path`에 넣는 전제이기 때문이다 — 각 Dockerfile
상단 주석에 상세 근거가 있다. 저장소 루트 `.dockerignore`가 없으면 `ai-office/node_modules`(~760MB)까지 빌드마다
Docker Daemon에 올라가므로, 두 부서 Dockerfile을 쓸 때는 반드시 `.dockerignore`가 함께 있어야 한다.

## 2. Backend Image 상세 (risk-api / audit-api / qa-worker)

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
| COPY 대상 | `departments/06-ai-qa-audit`, `skills/agentic-rag`, `apps/observability` |
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
| 필수 환경변수 | `RISK_QA_EVENT_REDIS_URL` 또는 `REDIS_URL` 중 하나 — 없으면 `worker.py`가 즉시 `SystemExit`로 종료됨. compose에서는 `RISK_QA_EVENT_REDIS_URL: ${RISK_QA_EVENT_REDIS_URL:-redis://redis:6379/0}`로 기본값을 줘서 사실상 항상 채워지게 했다 |
| 동작 | `bus.consume_once(_record_risk_event, count=50, min_idle_ms=1000)`을 `QA_EVENT_POLL_INTERVAL_SECONDS`(기본 1초) 간격으로 반복 |
| 의존 | `redis` |

## 3. Hermes 컨테이너 (risk-hermes / qa-hermes)

research-hermes/quant-hermes와 같은 패턴 — Backend Image에 Hermes를 넣지 않고 공식 이미지를 별도 컨테이너로 띄운다.

| 항목 | risk-hermes | qa-hermes |
|---|---|---|
| 이미지 | `nousresearch/hermes-agent:latest` | `nousresearch/hermes-agent:latest` |
| volume | `${USERPROFILE}/.hermes-risk-management:/opt/data` | `${USERPROFILE}/.hermes-qa-department:/opt/data` |
| API 키 | `OPENAI_API_KEY` | `ANTHROPIC_API_KEY` |
| command | `gateway run` | `gateway run` |

키 배정은 CLAUDE.md 표(risk=OpenAI, qa=Anthropic)를 그대로 따른다. 로그인·`auth.json` 취급·프로필 동기화
(`sync_hermes_profiles.sh`)는 [HERMES_DOCKER_RUNBOOK.md](HERMES_DOCKER_RUNBOOK.md) 4-3절을 그대로 따른다 — 여기서 반복하지 않는다.

## 4. 자원 한도

전 서비스 공통으로 `mem_limit`/`cpus`/`pids_limit`을 건다 (로컬 P0 정책, 계획서 11절).

| 서비스 | mem_limit | cpus | 비고 |
|---|---|---|---|
| `redis` | 256m | 0.5 | 영속화(AOF) 미적용 — 재시작 시 Stream 유실 감수 |
| `risk-api` | 256m | 0.5 | |
| `audit-api` | 256m | 0.5 | |
| `qa-worker` | 256m | 0.3 | `stop_grace_period: 20s` — 소비 중인 이벤트가 있으면 강제 종료 전에 끝내도록 |
| `risk-hermes` | 1g (reservation 192m) | 1.0 | |
| `qa-hermes` | 1g (reservation 192m) | 1.0 | |

## 5. 실행·검증

```bash
# 문법 검증 (Docker Daemon 불필요)
docker compose config --quiet
docker compose config --services

# 리스크·QA 컨테이너만 올리기
docker compose up -d redis risk-api audit-api qa-worker risk-hermes qa-hermes

# risk-api / audit-api 헬스 확인
curl -s http://127.0.0.1:8041/ | head -1
curl -s http://127.0.0.1:8042/ | head -1

# qa-worker가 Risk 이벤트를 잘 받는지는 QA 자체 통합 테스트로 확인한다
python -m pytest departments/06-ai-qa-audit/tests/test_redis_event_bus_integration.py -q
```

## 6. 알려진 미결 (기록만, 코드 아님)

- **risk-projection-worker 없음** — 계획서 6.4절이 말하는 "Position·Market·Mandate Event를 Risk Snapshot으로
  투영"하는 Worker는 아직 만들지 않았다. 저장소 전체에서 `market.snapshot.v1`/`portfolio.snapshot.v1`/
  `governance.mandate.changed.v1`을 Redis Stream에 발행(`XADD`)하는 곳이 하나도 없다 (Trading/Accounting/CEO
  쪽 미구현 — 본부 경계 밖이라 Risk가 대신 만들 수 없다). `risk_events/redis_event_bus.py`는 Risk→QA 결정
  이벤트 Publisher만 있고 이 방향의 Consumer 루프는 없다. `P1RiskSnapshot`(`p1/analytics.py`)은 이미 있지만
  Pre-trade 요청 경로에서 `evaluate_p1_gate`로 동기 계산되는 것이라 이 백로그의 비동기 투영과는 다른
  메커니즘이다. 업스트림 Publisher가 생기기 전에 컨테이너·코드를 먼저 만들지 않는다 — 백로그는
  `departments/03-risk/hermes/config.yaml`(2026-08-03 항목)에 기록해 뒀다.
- `USERPROFILE` 미설정 경고 — macOS 개발 환경에서 `docker compose config` 실행 시 뜬다. Windows 배포 대상
  volume 경로 템플릿이라 발생하는 기존 동작이며(research-hermes/quant-hermes도 동일), 기능에는 영향 없다.
