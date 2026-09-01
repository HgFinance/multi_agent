# Hermes 도커 운영 Runbook

> **상태:** RUNBOOK · **검토일:** 2026-08-27 UTC
> 모든 부서장은 Hermes + `openai-codex/gpt-5.6-luna`를 사용하고, 직원은 독립 LangGraph Worker + Worker Model Gateway의 Qwen AWQ를 운영 기본으로 사용한다. Ollama `qwen3:1.7b`와 Laguna 예시는 local fallback 또는 과거 Smoke 기록이다. 로컬 서비스·profile·포트는 [LOCAL_COMPOSE_RUNTIME_BASELINE.md](LOCAL_COMPOSE_RUNTIME_BASELINE.md)가 소유한다.

담당: 재일 (리서치·퀀트) — 2026-08-02 작성, 2026-08-03 상태 갱신
근거: 재일님 지시 "팀원들이랑 도커로 관리하기로 했는데 어떻게 해야 할지"

이 문서는 **운영 절차서**다. 아키텍처 결정은
[DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md](DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)
3.1~3.2가 이미 정했고(“Hermes는 별도 Image·Credential·Memory Namespace,
Department Backend Image에 설치하지 않는다”), 여기서는 그 결정을 어떻게
실행하는지만 적는다. 이 문서가 계획서를 바꾸지 않는다.

---

## 1. 지금 구성 (2026-08-25 Compose 재검토)

현재 Git 기준은 8개 Hermes 부서장 Profile 모두 `openai-codex/gpt-5.6-luna`를 기본으로 사용하고 Claude Code를 승인된 대체 런타임으로 둔다. Discord ingress 패치는 CEO·QA·HR에만 활성화되어 `/opt/kanban` 전역 claim과 채널 allowlist를 적용한다. 직원은 부서별 독립 LangGraph Worker이며 운영 기본은 Worker Model Gateway의 Qwen2.5-14B-Instruct-AWQ다. Ollama `qwen3:1.7b`는 명시적 local fallback이고, 아래 `poolside/laguna-s-2.1:free` 표기는 이전 Docker smoke 기록이다. 실제 Head runtime 반영은 `./scripts/sync_hermes_profiles.sh push` 후 Profile별 credential 상태로 확인한다.

계획서 3.1~3.2대로 **부서별 컨테이너 1개 = 부서별 데이터 디렉터리 1개**다.

```
compose 프로젝트 hedgefund
├── research-hermes / quant-hermes / risk-hermes / qa-hermes   ← 이 파일에 직접 정의
├── ceo-hermes / workforce-hermes / trading-hermes / accounting-hermes
│     ← 각 본부 compose.yaml을 `include:`로 끌어옴(departments/<n>/compose.yaml)
│   (8개 본부 모두 2026-08-10 기준 이미 컨테이너가 있다 — 5절은 "새 9번째 본부"용)
├── hermes-dashboard  ← profiles:[dashboard] 옵트인, 공용 운영/관리 콘솔(2026-08-10) -
│     /home/ubuntu/.hermes:/opt/data 전체를 RW로 마운트해 8개 profiles/(Profile
│     생성·Config·Keys·Skills 편집 포함)와 공용 Kanban을 팀 전체가 다룬다. 위
│     "부서별 컨테이너 1개 = 부서별 데이터 디렉터리 1개" 원칙은 8개 실행 서비스에만
│     적용되고, 여러 부서 데이터를 한 컨테이너에서 다루는 Dashboard는 예외다
│     (**조회 전용이 아니다** — RW 콘솔이라 접근은 Tailscale 사설망으로만 연다)
├── research-api / market-api      ← 부서 읽기 전용 조회면
├── batch-collectors / ls-realtime  ← 시장데이터 전용
├── research-mcp / research-liaison-mcp  ← 비시장 정보 요청형·비영속 조회
└── timescaledb

각 컨테이너: /home/ubuntu/.hermes/profiles/<부서>  →  /opt/data
  (bind mount; 로컬 개발은 ~/.hermes/profiles/<부서>, HERMES_HOME 기본값)
└── <부서>/{config.yaml, SOUL.md}   ← 저장소에서 동기화
    auth.json, memories/, sessions/, state.db*  ← 로컬 전용, git 제외
```

### 분리의 핵심은 이름이 아니라 저장소다

컨테이너만 쪼개고 `/opt/data`를 공유하면 한 부서의 세션·기억·자격이 옆 부서에서
그대로 보인다 — 이름만 다른 **분리된 척**이다. 그래서 마운트를 부서별로 가른다.
이미지 기본값이 `HERMES_HOME=/opt/data`라 **마운트만 갈면 분리가 끝난다.**

Historical snapshot (2026-08-02 Docker smoke; 현재 모델 기준 아님):

```
$ docker exec hedgefund-research-hermes hermes profile list
  research-department        poolside/laguna-s-2.1:free
$ docker exec hedgefund-quant-hermes    hermes profile list
  quant-backtest-department  poolside/laguna-s-2.1:free
```

각 컨테이너가 **자기 부서 Profile만** 본다.

### 권한 강제의 현재 경계

Research MCP에는 Bearer 인증과 허용 경로를 강제하는 Tool Gateway가 생겼다. 그러나 이 강제는
Research 도구 면에만 적용되며 전 본부 공통 Gateway가 아니다. Profile 검사에서 CEO, Trading,
Quant, Accounting과 HR의 `tool_allowlist`가 미선언 경고로 남는다. 따라서 컨테이너·저장소 분리와
Research Tool 강제는 확인됐지만 전사 권한 분리가 완료됐다고 말하지 않는다.

---

### Current runtime authentication (2026-08-03)

현재 Head는 `openai-codex/gpt-5.6-luna`이며 인증 확인은 Profile별 `hermes auth status openai-codex`를 사용한다. `hermes portal login`, Nous Portal, `poolside/laguna-s-2.1:free`는 아래 Historical Docker smoke 절차에만 해당한다. 저장소 Profile은 `./scripts/sync_hermes_profiles.sh push`로 `config.yaml`과 `SOUL.md`만 동기화하고, `auth.json`, `sessions`, `memories`, `logs`는 로컬 Runtime에 둔다.

```bash
source ~/claude/bin/activate
./scripts/sync_hermes_profiles.sh push
hermes --profile risk-management auth status openai-codex
hermes --profile qa-department auth status openai-codex
```

### Historical Docker/Nous procedure

아래 Docker Image·Portal OAuth·Laguna 명령은 분리 저장소를 검증하던 Historical snapshot이다. 현재 모델·인증 상태로 해석하거나 운영 완료의 증거로 사용하지 않는다.

## 2. 처음 붙일 때 (과거 로컬 연결 절차)

```bash
# 1) 이미지 (4GB, 부서별 컨테이너가 레이어를 공유하므로 1회면 된다)
docker pull nousresearch/hermes-agent:latest

# 2) 내 본부 컨테이너 기동 — 첫 실행이 ~/.hermes/profiles/<부서> 를 만든다
docker compose up -d research-hermes quant-hermes

# 3) 각 컨테이너 안에 자기 부서 Profile 하나만 생성
docker exec hedgefund-research-hermes hermes profile create research-department
docker exec hedgefund-quant-hermes    hermes profile create quant-backtest-department

# 4) 저장소 사본 → 런타임 (config.yaml, SOUL.md 만)
./scripts/sync_hermes_profiles.sh push

# 5) 확인 — 각 컨테이너가 자기 것 하나만 보면 격리된 것이다
docker exec hedgefund-research-hermes hermes profile list
docker exec hedgefund-quant-hermes    hermes profile list
```

### 경로 함정 (여기서 제일 많이 막힌다)

Windows 네이티브 설치본은 `%LOCALAPPDATA%\hermes`를 쓰고 컨테이너는
`${USERPROFILE}/.hermes/profiles/<부서>`를 쓴다 — **다른 디렉터리다.** 섞으면
"분명 profile을 만들었는데 목록에 없다"가 된다. 도커를 기준으로 삼는다.
(`~/.hermes/`는 OneDrive 동기화 대상 밖이라 state.db 손상 위험이 없다 —
compose 상단의 named volume 원칙과 같은 이유.)

---

## 2-2. ⚠ 동기화의 두 함정 (2026-08-02 실측)

**(1) `pull` 은 주석을 지운다.** Hermes 가 config.yaml 을 건드리면(예:
`mcp add`) 파일 전체를 기계 포맷으로 다시 쓴다. 그 상태를 `pull` 하면 저장소
사본의 한국어 주석·근거가 통째로 사라진다(실측: 226줄 재작성). **저장소 사본이
사람이 쓴 원본**이고, 런타임이 추가한 블록은 손으로 추려 옮긴다.

**(2) push 후에는 인증을 확인한다.** push 는 config.yaml 을 전부 덮으므로
Hermes 가 붙여둔 런타임 부기가 사라진다. 실측에서 push 직후 `portal status` 가
`not logged in` 으로 바뀐 적이 있다(당시엔 4-3 절의 토큰 복사 사고가 겹쳐
있었다). 복구는 **해당 설치본에서 다시 로그인**한다 - 다른 곳의 auth.json 을
가져오지 않는다(4-3 절 사고 원인).

```bash
docker exec hedgefund-research-hermes hermes portal status   # push 뒤 확인
docker exec -it hedgefund-research-hermes hermes portal login  # 끊겼으면 재로그인
```

---

## 3. 일상 운영

```bash
git pull && ./scripts/sync_hermes_profiles.sh push
docker compose restart research-hermes quant-hermes   # profile 변경 반영

# 로컬에서 profile 을 고쳤다면 저장소로 되돌린 뒤 커밋
./scripts/sync_hermes_profiles.sh pull
git diff departments/*/hermes/
```

동기화 대상은 `config.yaml`, `SOUL.md` **둘뿐이다.** `auth.json`, `.env`,
`memories/`, `sessions/`, `state.db*`는 머신별 상태라 절대 올리지 않는다.

---

## 4. 대시보드 (옵트인)

기본 기동에서 제외돼 있다. 실측 결과 대시보드는 `0.0.0.0` 바인딩 시 인증
없이는 기동을 거부하고 재시작 루프에 빠진다(`--insecure`로도 안 된다).
도커는 컨테이너 안 `127.0.0.1`을 호스트로 게시할 수 없으므로 `0.0.0.0`
바인딩이 강제되고, 따라서 **인증 설정이 선행 조건**이다.

```bash
# 방법 1) Basic Auth — 해시를 만들어 ~/.hermes/config.yaml 에 넣는다
docker exec hedgefund-hermes python -c \
  "from plugins.dashboard_auth.basic import hash_password; print(hash_password('<비밀번호>'))"
#   dashboard:
#     basic_auth: {username: <id>, password_hash: <위 출력>}

# 방법 2) Nous Portal OAuth
docker exec -it hedgefund-hermes hermes dashboard register

# 준비되면
docker compose --profile dashboard up -d
```

게시는 `127.0.0.1:9119`로 묶는다. 팀원 접근은 **Tailscale 사설망으로만** 하고
공유기 포트포워딩은 하지 않는다(저장소 전역 규칙).

---

## 4-1. 중앙 Kanban dispatcher (AWS)

`kanban-dispatcher`는 `gateway run` 컨테이너가 아니다. `/home/ubuntu/.hermes:/opt/data`를 RW로 마운트한 단일 컨테이너에서 다음 명령만 실행한다.

```yaml
init: true
command:
  ["kanban", "daemon", "--force", "--interval", "60", "--max", "3", "--failure-limit", "2", "--pidfile", "/opt/data/shared-kanban/dispatcher.pid", "--verbose"]
environment:
  HERMES_HOME: /opt/data
  HERMES_KANBAN_HOME: /opt/data/shared-kanban
  HERMES_KANBAN_DISPATCH_IN_GATEWAY: "false"
  KANBAN_DISPATCH_MAX_SPAWN: "3"
  KANBAN_DISPATCH_FAILURE_LIMIT: "2"
```

`init: true`는 Hermes 이미지가 지원하는 wrapped-runtime 경로를 선택한다. entrypoint가 PID 1이 아니므로 s6 `/init`와 `02-reconcile-profiles`가 실행되지 않고, `profiles/*`를 gateway 서비스로 reconcile/start하거나 `gateway.pid`·`processes.json`을 지우지 않는다. `/opt/data/profiles/<assignee>`는 그대로 보여 worker spawn과 profile resolution에 사용된다. 이 설정을 제거하면 중앙 컨테이너가 다시 모든 named profile gateway를 기동할 수 있다.

8개 부서 gateway는 계속 `HERMES_KANBAN_DISPATCH_IN_GATEWAY: "false"`를 유지한다. 중앙 dispatcher와 gateway-embedded dispatcher를 같은 `shared-kanban/kanban.db`에 동시에 실행하지 않는다.

주의: [`hermes kanban daemon --force`](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md)는 공식 CLI에 남아 있는 deprecated standalone escape hatch다. 공식 문서는 gateway-embedded dispatcher를 기본 경로로 안내하며, 두 dispatcher를 동시에 실행하면 claim race가 발생해 지원되지 않는다고 명시한다. [`entrypoint-dispatch.sh`](https://github.com/NousResearch/hermes-agent/blob/main/docker/entrypoint-dispatch.sh)의 wrapped-runtime fallback과 [`container_boot.py`](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/container_boot.py)의 reconcile 동작을 근거로 `init: true`를 함께 둔다. 향후 Hermes release에서 `--force`가 제거될 수 있으므로 이미지 업데이트 때 아래 smoke check를 다시 통과시킨다.

```bash
docker compose config >/dev/null
docker compose up -d kanban-dispatcher
docker logs -f hedgefund-kanban-dispatcher
docker exec hedgefund-kanban-dispatcher hermes kanban daemon --help
docker exec hedgefund-kanban-dispatcher hermes kanban show t_186cb00d --json
```

`--help`에는 `--force`가 의도적으로 표시되지 않는다. 실제 지원 여부는 `hermes kanban daemon --force`가 “STANDALONE via --force”로 시작하는지로 확인한다. `docker logs`에 `reconcile: profile=... started`가 보이면 `init: true`가 적용되지 않은 것이므로 즉시 dispatcher를 중지하고 Compose 렌더링을 확인한다.

`--max`는 shared board의 한 tick spawn 예산을 명시하고, `--failure-limit`은
반복적인 worker spawn 실패를 빠르게 격리한다. Compose의
`KANBAN_DISPATCH_MAX_SPAWN`과 `KANBAN_DISPATCH_FAILURE_LIMIT`가 이 값을
소유한다. 설정 변경 후에는 dispatcher를 재생성해야 하며, 이미 실행 중인
컨테이너에는 자동 반영되지 않는다.

Research MCP는 keepalive가 event loop 정지를 감지하면
`RESEARCH_MCP_LOOP_STALL_SECONDS`(기본 90초) 후 프로세스를 종료한다.
Compose의 `restart: unless-stopped`가 재기동을 담당하며, 이 watchdog은
MCP tool payload나 외부 write를 수행하지 않는다.

### 완료 결과 handoff 계약 (2026-08-27)

중앙 dispatcher는 `Dockerfile.agent-runtime`으로 빌드한다. 이미지가 설치하는
`deploy/ceo-kanban/install_result_contract.py`는 Hermes의 단일 완료 트랜잭션 안에서
`result`가 비어 있고 `summary`만 있는 호출을 보정한다. 따라서 `done` 이벤트, `tasks.result`,
`task_runs.summary`가 서로 다른 시점에 만들어지지 않는다.

```bash
docker compose build kanban-dispatcher
docker run --rm --entrypoint sh hedgefund-agent-runtime:latest \
  -lc "grep -n hgfinance-canonical-result-v1 /opt/hermes/hermes_cli/kanban_db.py"
```

Quant 표준·신속 분석은 추가 위임 없이 primary가 직접 종료한다. 원본이 없을 때는
`retrieval_attempt`에 종목·기간·source·TR·조회/추출 시각·snapshot hash와 `UNAVAILABLE` 상태를
남기며, 검증되지 않은 성과지표는 생성하지 않는다.

### orphaned run 회수

`t_186cb00d`는 수동 SQL로 수정하지 않는다. Hermes dispatcher의 첫 tick은 stale claim 회수를 먼저 수행하며, 기본 claim TTL(15분)이 지난 `running` claim을 `ready`로 되돌리고 이전 run을 `reclaimed`로 닫는다. `worker_pid`가 없으면 종료시킬 프로세스가 없어 즉시 reclaim 경로를 탄다.

```bash
docker exec hedgefund-kanban-dispatcher hermes kanban show t_186cb00d --json
docker exec hedgefund-kanban-dispatcher hermes kanban runs t_186cb00d --json
```

기대 순서는 TTL 만료 후 `running → ready`(reclaimed run 기록) → dispatcher가 profile worker를 spawn하면 `running → done`이다. 첫 조회에서 즉시 바뀌지 않으면 TTL 만료 전일 수 있으므로 SQL 수정이나 강제 상태 전이를 하지 않고 다음 dispatcher tick을 기다린다.

### 4-1-a. CEO closed-loop supervisor

`ceo-kanban-supervisor`는 gateway나 dispatcher가 아니다. Hermes `kanban watch`가
내보내는 `completed`, `blocked`, `gave_up`, `crashed`, `timed_out`,
`spawn_failed` terminal event를 읽고, `kanban show`의 parent/child
projection을 다시 조회해 CEO supervisor action을 결정한다. `reclaimed`는 stale claim을 `ready`로 되돌리는 non-terminal event이므로 supervisor wake-up 대상이 아니다. 현재 Hermes CLI에는 `kanban watch --json` 또는 동등한 structured output 옵션이 없어서 사람이 읽는 text contract를 엄격히 검증하며, malformed line이나 watch의 예기치 않은 EOF/non-zero 종료는 supervisor process failure로 처리한다. 같은 parent의 동시 event는 parent lock과 root comment marker(`hgfinance.ceo-supervisor.wakeup.v1`)로 중복 실행을 막고, wake-up/replan 상한은 root comments와 supervisor child task에 기록되어 restart 후에도 유지된다.
projection을 통해 CEO supervisor action을 결정한다. DB를 직접 읽거나 SQL로 상태를
변경하지 않는다.

Supervisor action은 `SYNTHESIZE`, `CREATE_TASK`, `RETRY_TASK`,
`REQUEST_USER_INPUT`, `BLOCK/ABORT` 중 하나이며, retry 2회와 wake-up
8회를 기본 상한으로 둔다. `blocked`는 실패와 구별한다. `needs_input` blocked는
사용자 입력 요청으로 남기고, transient blocked만 retry하며, 그 외 blocked는 제한된
replan 후 중단한다. 일반 CEO 응답의 QA는 synthesis 선행 action이 아니다. CEO final
response를 전달한 뒤 supervisor가 `RUN_QA` audit child를 별도로 생성하며, QA는 응답을
지연·차단·재작성하지 않는다. 전략 승격·HR lifecycle처럼 별도 governance 승인이
필요한 workflow만 자체 QA→CEO gate를 가진다. 상세 정본은
[CEO_ARCHITECTURE.md](CEO_ARCHITECTURE.md) §3.4다.

모든 Hermes `kanban create` 경계는 `orchestration/canonical_profiles.py`의 exact
allowlist를 통과해야 한다. 논리 단계(`risk`, `qa`)는 CLI 직전에 각각
`risk-management`, `qa-department`로 변환되고, `risk-department`나
`ai-qa-audit-department` 같은 문자열은 fallback 없이 거부된다.

주의: supervisor도 `/home/ubuntu/.hermes`를 보지만 `init: true`와 일반 Python
command만 사용한다. dispatcher와 마찬가지로 gateway profile reconcile 로그가
나오면 즉시 중지하고 Compose 렌더링을 확인한다. standalone daemon과 embedded
dispatcher를 같은 Kanban DB에 동시에 실행하지 않는다.

```bash
docker compose config --quiet
docker compose up -d kanban-dispatcher ceo-kanban-supervisor
docker logs -f hedgefund-ceo-kanban-supervisor
```

향후 Hermes release에서 deprecated `hermes kanban daemon --force` escape hatch가
제거되거나 `kanban watch` 출력 계약이 변경될 수 있다. update 후에는 CLI help/source와
이 supervisor의 contract tests를 함께 재검증해야 한다.

## 4-1-b. Portfolio BFF와 CEO Hermes 연결

`portfolio-bff`는 자체 Hermes gateway를 시작하지 않는다. CEO 질의는 같은
Compose 네트워크의 기존 `ceo-hermes`가 제공하는 인증된 Hermes API Server
(`POST /v1/chat/completions`)로 전달한다. `ceo-hermes`의 CEO Profile·auth·
Tool Allowlist는 `ceo-hermes` 컨테이너 안에만 남는다.

BFF 이미지에는 공식 Hermes CLI를 pinned source revision으로 설치한다. 이 CLI는
repository-owned canonical Kanban create boundary를 수행할 때만 사용한다.
따라서 BFF에는 다음 최소 마운트만 있다.

```yaml
HERMES_HOME: /opt/hermes-cli
HERMES_KANBAN_HOME: /opt/kanban
HERMES_CEO_API_URL: http://ceo-hermes:8642/v1
HERMES_CEO_API_KEY: ${CEO_HERMES_API_KEY:?CEO_HERMES_API_KEY is required}
volumes:
  - /home/ubuntu/.hermes/shared-kanban:/opt/kanban
```

`/home/ubuntu/.hermes` 전체 또는 `profiles/ceo-agent`를 BFF에 마운트하지 않고,
BFF command도 `gateway run`이 아니다. 따라서 BFF 재생성으로 CEO gateway가
중복 기동되거나 profile reconciliation이 발생하지 않는다. `ceo-hermes`는
`API_SERVER_ENABLED=true`, `API_SERVER_HOST=0.0.0.0`, `API_SERVER_PORT=8642`,
`API_SERVER_KEY=${CEO_HERMES_API_KEY}`를 사용하며 host port로 공개하지 않는다.
CEO healthcheck는 Compose가 공급한 동일 키로 `/v1/models` 인증까지 검증한다.
따라서 프로필 `.env`의 오래된 `API_SERVER_KEY`가 컨테이너 환경을 덮어쓰면
gateway를 정상으로 오인하지 않고 unhealthy로 표시한다.

`CEO_HERMES_API_KEY`는 `.env` 또는 AWS secret injection으로만 주입한다. API
Server가 이 키 없이 기동되지 않도록 Hermes의 최소 16자 인증 조건을 유지한다.
root Kanban create가 실패하면 BFF는 CEO API를 호출하지 않고 503을 반환한다.

## 4-1-c. 사용자 자연어 주문의 PAPER 전용 Hermes 경계

명시적인 사용자 주문은 `portfolio-bff -> CEO root Kanban -> pre-created
Trading primary -> Trading Hermes -> paper-order-orchestrator-mcp -> trading-api`
순서로만 처리한다. `paper-order-orchestrator-mcp`는 호스트 포트를 공개하지 않고
Compose 내부 `8046/mcp`에서 정확히 한 개의 도구
`process_user_paper_order`만 제공한다. Trading Hermes의 출력은 비구속 해석이며,
이 서비스가 원문 해시·Kanban scope·사용자/Fund/Book binding을 다시 검증한 뒤에만
기존 PAPER directive admission을 호출한다. LIVE 전환 경로는 없다.

이 전용 lane의 CEO root는 실행 프롬프트가 아니라 immutable scope container다.
root는 unclaimed `running`, Trading 카드는 `blocked`로 만든 뒤 SQL binding을
완료하고, root는 worker에 release하지 않은 채 `done`으로 닫은 다음 Trading
카드만 release한다. 따라서 CEO dispatcher가 Trading tool call보다 먼저 root를
`blocked`로 바꾸는 경합이 없어야 한다. trusted tool은 root `done`과 Trading
`running` 조합만 신규 제출로 인정한다.

`MCP_TRADING_ORDER_API_KEY`는 `.env` 또는 AWS secret injection으로 주입하는
별도의 무작위 값이다. printable ASCII 32바이트 이상이어야 하며 placeholder,
공백, 단일 문자 반복 값은 서버 기동 시 거부된다. 같은 값은 MCP 서버,
`kanban-dispatcher`, `trading-hermes`에만 전달한다. `DATABASE_URL`과
`TRADING_SERVICE_AUTH_SECRET`은 MCP 서버에만 남고 Hermes/dispatcher에는 전달하지
않는다. 키를 만들 때는 예를 들어 `openssl rand -hex 32`를 사용한다.

LS PAPER 주문 서비스에는 PAPER AppKey/Secret과 함께 `LS_MAC_ADDRESS`(구분자
제거 후 12자리 hex)가 필수다. `CSPAQ13700`은 LS 계좌조회 정상 응답에서
`rsp_cd=00136`을 반환할 수 있으므로 해당 TR에 한해서 정상으로 처리한다. 이 TR은
초당 1회 제한이 있어 worker adapter가 짧은 account-wide snapshot cache를 공유하며,
broker 주문번호가 없는 ambiguous `UNKNOWN` leg는 자동 재제출도 hot polling도 하지
않고 수동 broker reconciliation 대상으로 남긴다.

Profile 변경을 먼저 동기화한 뒤 Compose를 검증하고 기동한다.

```bash
./scripts/sync_hermes_profiles.sh push
docker compose config --quiet
docker compose up -d paper-order-orchestrator-mcp trading-api \
  trading-directive-worker trading-outbox-relay accounting-ledger-consumer \
  trading-hermes kanban-dispatcher portfolio-bff
docker compose logs --tail=100 paper-order-orchestrator-mcp kanban-dispatcher
```

MCP readiness는 단순 TCP가 아니라 MCP key, `svc_order_orchestrator` DB role과 016
테이블, shared Kanban DB, Trading `/health/ready`를 모두 확인한다. Trading API가
healthy해진 뒤 MCP가 healthy가 되고, 그 전에는 dispatcher와 Trading Hermes가
시작되지 않는 것이 정상적인 fail-closed 동작이다. 주문 완료는 broker fill만으로
판단하지 않고 Accounting ACK가 기록될 때까지 `ACCOUNTING_PENDING`으로 남긴다.

## 4-2. 다른 본부를 추가하는 법 (9번째 본부가 생길 때만 — 현재 8개가 이미 있다)

8개 본부(research/quant/risk/qa는 이 파일에 직접, ceo/trading/accounting/hr은
`departments/<n>/compose.yaml`에)는 2026-08-10 기준 이미 모두 컨테이너가 있다.
새 본부가 추가될 때만 아래처럼 기존 `risk-hermes` 블록(docker-compose.yml)을
복사해 두 곳만 바꾼다. 남의 본부 컨테이너를 대신 만들지 않는다(담당자 표: CLAUDE.md).

```yaml
  newdept-hermes:                                # ① 서비스 이름
    image: nousresearch/hermes-agent:latest
    container_name: hedgefund-newdept-hermes      # ② 컨테이너 이름
    restart: unless-stopped
    command: ["gateway", "run"]
    volumes:
      - /home/ubuntu/.hermes/profiles/new-department:/opt/data   # ③ 부서 디렉터리
    environment:
      HERMES_UID: 1000
      HERMES_GID: 1000
      # LLM Provider(Codex/Claude)는 하드코딩하지 않는다 - /opt/data/config.yaml의
      # provider: 와 auth.json이 결정한다(research-hermes 블록 참고).
    mem_limit: 1g
    mem_reservation: 192m
    cpus: 1.0
    pids_limit: 256
    stop_grace_period: 20s
```

디렉터리 이름은 **Profile 이름과 정확히 같아야 한다**(`~/.hermes/profiles/<profile>`,
AWS는 `/home/ubuntu/.hermes/profiles/<profile>`). 동기화 스크립트가 그 규칙으로
경로를 찾는다(`scripts/sync_hermes_profiles.sh`의 `DEPARTMENTS` 배열에도 추가해야 한다).

### 부서 간 API 호출

계획서 3.3의 "본부 간 호출은 HTTP"는 `hermes serve`(JSON-RPC/WebSocket)가
담당한다. 다만 2026-06 하드닝 이후 **비-loopback 바인딩은 인증 없이 열리지
않는다**(`--insecure`는 no-op이 됐다). 컨테이너 간 통신은 `0.0.0.0` 바인딩이
필요하므로 인증 설정이 선행 조건이다. 이는 제약이 아니라 권한 경계를 인증으로
강제하는 것이라 우회하지 않는다 — 4절 절차로 인증을 붙인 뒤 연다.

참고: 도메인 데이터 호출(시세·Evidence)은 이미 `research-api`/`market-api`
읽기 전용 면으로 하고 있고 Hermes를 거치지 않는다. Hermes 간 호출은 **업무
위임(Kanban Handoff)**용이지 데이터 조회용이 아니다.

---

## 4-3. 로그인 (컨테이너별 1회)

모델은 API Key 가 아니라 **Nous Portal 로그인**을 쓴다. 데이터 디렉터리를
부서별로 갈랐으므로 **자격증명도 부서별로 따로**다.

### 🚫 auth.json 을 설치본끼리 복사하지 않는다 (2026-08-02 사고)

한때 이 문서는 "네이티브 설치본의 `auth.json` 을 컨테이너로 복사하면 제일
쉽다"고 적고 있었다. **그 방법이 계정 세션 전체를 죽였다.**

리프레시 토큰은 **한 번 쓰면 새 것으로 교체된다(rotation).** 같은 토큰을 세
곳(네이티브·리서치·퀀트)에 복사하면, 한 곳이 갱신해 토큰이 바뀐 뒤 다른 곳이
**이미 쓴 옛 토큰을 다시 제출**한다. 서버는 이것을 토큰 탈취 신호로 보고
세션 자체를 폐기한다. 토큰 파일에 그대로 기록돼 있었다.

```
code=invalid_grant  message=Refresh session has been revoked
reason=runtime_access_refresh_failure  relogin_required=True
```

결과: 컨테이너뿐 아니라 **원래 로그인돼 있던 네이티브 설치본까지** 로그아웃됐다.
**설치본 하나 = 로그인 하나.** 컨테이너를 새로 만들면 그 컨테이너에서 한 번
로그인한다. 그것이 유일한 방법이며 아래가 그 절차다.

### 로그인 (기기 코드 방식) — 설치본마다 1회

```powershell
docker exec -it hedgefund-research-hermes hermes portal login
```

URL 과 코드(`XXXX-XXXX`)가 출력된다. 브라우저에서 그 URL 을 열고 코드를 확인해
승인하면 터미널이 폴링으로 감지해 끝난다. **승인 전까지 터미널을 닫지 않는다.**
컨테이너는 브라우저를 못 열어 "Could not open browser automatically" 가 뜨는데
정상이다. 코드에는 만료가 있어 방치하면
`Timed out waiting for device authorization` 으로 끝난다(실측) - 바로 승인할 수
있을 때 실행한다.

### 갱신은 자동이다 — 주기적으로 할 일은 없다

로그인 뒤에는 Hermes 가 백그라운드에서 액세스 토큰을 재발급한다. 다시 로그인이
필요한 경우는 세 가지뿐이다: 비밀번호 변경, 포털에서 세션 강제 해지, 장기 미사용.
**위 사고처럼 세션이 폐기된 경우도 여기 해당한다** - 그때는 각 설치본에서 다시
한 번 로그인한다(복사하지 않는다).

### 확인

```bash
docker exec hedgefund-research-hermes hermes portal status          # Auth: ✓ logged in
docker exec hedgefund-research-hermes hermes -p research-department \
  chat -q "한 문장으로: 당신의 역할과 만들면 안 되는 산출물은?"
```

실측 응답(2026-08-02): "…트레이딩 부서에 증거와 테제를 제공하는 역할이며,
절대로 매수/매도 방향이나 포지션 크기 같은 실제 거래 결정을 산출해서는 안
됩니다." — RES-00 경계가 그대로 나오면 Profile 이 물린 것이다.

---

## 5. 모델·과금

현재 저장소 기준 8개 Profile의 Head는 `provider: openai-codex` / `gpt-5.6-luna`이고, 승인된 Claude Code를 대체 provider로 사용할 수 있다. 직원은 Hermes Head 모델과 분리된 부서별 독립 LangGraph Worker + Qwen AWQ이며 Ollama는 local fallback이다. 이전 6개 Nous/Laguna와 Risk·QA만 Codex였던 구성은 Historical snapshot으로만 보존한다.
`scripts/check_hermes_profiles.py`는 부서별 Head 모델과 Employee 모델을 각각 검증해야 하며, 전체 Profile을 하나의 모델로 비교하지 않는다.
모델 교체는 benchmark와 HR·QA 승인 후 계층별 정본을 바꾼다. Head provider는 Profile,
운영 Worker는 enterprise registry와 model overlay, local fallback만 `OLLAMA_CHAT_MODEL`을
변경한다. 세 값을 무조건 함께 바꾸지 않는다.

Nous Profile은 Portal 로그인, Provider별 API·구독 자격은 해당 Runtime의 승인된 환경변수를 사용한다.
호스트의 갱신형 자격 파일을 여러 컨테이너가 공유하거나 복사하지 않는다.

```bash
docker exec -it hedgefund-hermes hermes status     # 인증·모델 확인
```

Bedrock Claude(TECH_STACK_DECISIONS.md의 목표 Gateway)로 옮기는 것은 `MODEL-01`의 별도 결정이다.

2026-08-03 로컬에는 Claude Code CLI를 Host Proxy로 감싸 Research Hermes가 호출하는 실험 코드가
생겼지만 아직 미커밋·미승인이다. 구독 한도 공유, 동시성, Timeout, 429, Host 장애, Prompt·응답 Log와
Provider 약관을 `MODEL-04`에서 검증하기 전 기본 Runtime이나 팀 공용 Gateway로 사용하지 않는다.

---

## 5-2. 사용자 입구 — 질문 하나를 끝까지 돌리기 (2026-08-11 로컬 실측)

4-1-b 절이 `POST /ui/ceo/ask`(질문 → 뿌리 카드 + CEO 답변)를 정한다. 이 절은 그
**뒤**를 적는다: 뿌리에 매달린 본부 카드가 실제로 도는지, 그리고 그 진행·실패가
사용자에게 어떻게 보이는지.

```
사용자 → POST /ui/ceo/ask       → 뿌리 카드 생성 → CEO 가 자식 카드 N장
                                                   → dispatcher 가 부서마다 spawn
        GET /ui/ceo/ask/{뿌리}  ← 카드별 결말(아래 표)
```

상관키는 **뿌리 카드**다. Hermes 세션 id 가 아니다 — CEO 프로세스가 끊기면
세션 id 는 사라지지만 뿌리 카드는 보드에 남는다(실측: 30초에 잘린 CEO 턴이 카드
4장을 만들어 놓고 죽었는데, 세션 id 를 못 읽어 어느 카드가 그 질문 것인지 알
방법이 없었다).

### 전제: dispatcher 가 떠 있어야 한다 (4-1절)

```bash
docker compose up -d kanban-dispatcher   # 이게 없으면 카드가 ready 로 앉아 있다
docker exec -u hermes hedgefund-qa-hermes hermes kanban stats | tail -1
#   Oldest ready task age: 0s   ← 이 숫자가 곧 dispatcher 가 멈춰 있던 시간이다
```

로컬(윈도우)은 `~/.hermes` 한 덩어리가 아니라 부서마다 `~/.hermes-<부서>` 로
흩어져 있어서, `docker-compose.override.yml` 이 그것들을 `profiles/<이름>` 자리에
각각 물린다. **경로만 다르고 구조는 4-1절과 같다.**

### CEO 를 CLI 로 부르는 로컬 경로

4-1-b 의 기본 경로는 `ceo-hermes` 의 OpenAI 호환 엔드포인트(`HERMES_CEO_API_URL`)
다. 그 엔드포인트는 `hermes serve` 인증 설정이 선행 조건이라(4-2절) 로컬에는 아직
없다. 그동안은 컨테이너 안 CLI 로 같은 턴을 돌린다.

```bash
DATABASE_URL='' \
CEO_TRANSPORT=cli ENABLE_CEO_CLI=true \
HERMES_EXEC_MODE=docker \
KANBAN_ACCESS_MODE=docker \
  python -m uvicorn apps.api.main:app --port 8001
```

- **조용히 넘어가지 않는다.** URL 이 없다고 알아서 CLI 로 떨어지지 않는다 —
  그러면 배포에서 URL 을 빠뜨렸을 때 "그래도 돌아가네"가 되어 경계가 무너진다.
- `CEO_TURN_TIMEOUT_SECONDS`(기본 300)는 Profile 의 `agent.timeout_seconds`(30초)와
  **일부러 다르다.** 30초는 한 번 묻고 한 번 답하는 조회 기준이고, 카드를 만들며
  도구를 여러 번 부르는 CEO 턴은 실측 90~180초가 걸렸다.

### ⚠ 윈도우에서 `kanban.db` 를 호스트에서 열지 마라 (2026-08-11 실측)

보드는 WAL 모드다. **윈도우 호스트에서 `mode=ro` 로 열기만 해도** bind mount 위에
`-shm` 매핑이 생기고, 그때부터 **컨테이너 쪽 쓰기가 전부 `disk I/O error` 로
죽는다.** 리서치 워커가 3분 12초짜리 조사를 마치고 보고서까지 쓰고도
`kanban_complete` 를 못 해 카드가 `running` 에 영원히 남았다 — 화면에는 "작업 중"
으로 보였다. 증상이 dispatcher 나 권한 문제처럼 보여서 원인과 멀었다.

```bash
KANBAN_ACCESS_MODE=docker   # BFF 는 컨테이너 안에서 조회한다(윈도우 필수)
```

이미 망가졌다면 — 호스트 쪽 읽기 프로세스를 **먼저 끄고** 나서:

```bash
rm ~/.hermes-shared-kanban/kanban.db-wal ~/.hermes-shared-kanban/kanban.db-shm
#   "Device or resource busy" 가 나면 아직 누가 잡고 있는 것이다
```

같은 이유로 `docker exec` 로 kanban CLI 를 부를 때는 **반드시 `-u hermes`** 를
붙인다. 기본이 root 라 WAL 이 root 소유로 생기고, 그러면 정작 에이전트(uid 1000)가
자기 보드를 못 쓴다.

### 읽는 법 — 카드 결말은 보드 상태와 다르다

| 결말 | 뜻 |
|---|---|
| `ANSWERED` | 결과 본문이 있다 |
| `NO_ANSWER` | **`done` 이지만 결과가 비었다.** 성공이 아니다 |
| `BLOCKED` | 자료·입력이 없어 부서가 멈췄다 |
| `FAILED` | 크래시·타임아웃·연속 실패 |
| `STALE` | `ready` 인데 아무도 안 집어갔다 → dispatcher 를 본다 |
| `NO_ASSIGNEE` | 없는 본부 이름이라 **영원히 안 돈다.** 기다릴 이유가 없다 |

`NO_ANSWER` 는 실제로 나온다. 회계본부가 "공식 NAV 가 없어 수익률을 산출할 수
없습니다"를 **완료**로 기록했다 — 부서는 정직하게 실패했지만 보드에는 `done` 으로
남는다. **Kanban 대시보드 임베드는 이 구분을 못 한다** — 임베드는 보드 원본이고,
`GET /ui/ceo/ask/{뿌리}` 는 같은 카드를 "답이 됐는가" 기준으로 다시 판정한 것이다.
둘은 서로를 대체하지 않는다.

`NO_ASSIGNEE` 도 실제로 나왔다. CEO 가 `--assignee accounting-portfolio` 로 카드를
만들었는데 정식 이름은 `accounting-portfolio-department` 였다. **보드는 없는 이름도
받아 준다** — 카드는 만들어지고, 그러고는 아무도 집어가지 못한다. 이름표의 정본은
[`orchestration/canonical_profiles.py`](../../orchestration/canonical_profiles.py)다.

### 알아 둘 것

- **잘린 CEO 턴은 "아무 일도 없었음"이 아니다.** 30초에서 끊긴 턴이 이미 카드
  4장을 만들었고 부서들이 그걸 실행했다. 뿌리 카드를 **먼저** 만드는 4-1-b 의
  순서가 이걸 막는다 — 뿌리는 보드에 남으므로 나중에라도 추적할 수 있다.

---

## 6. Hermes가 하는 일과 하지 않는 일

| | 담당 |
|---|---|
| 부서 단위 오케스트레이션, 위임, Memory, Kanban Handoff | Hermes |
| 실제 분석 파이프라인(LangGraph 6인 분석가 → Packet) | `departments/01-research/scripts.py` |
| 시세·Evidence 조회 | research-api / market-api (Agent는 DB Credential 없음) |

**Hermes를 붙인다고 분석이 좋아지지 않는다.** Hermes는 배선이고, 분석 품질은
방법론(`evidence/methods.py`)이 늘고 사후 채점(`research.analyst_calibration`)이
그 기여를 확인해 줄 때 좋아진다. 둘을 섞어 말하지 않는다.

---

## 7. 알려진 미결

- Tool Gateway 부재 — `tool_allowlist`가 선언으로만 존재(위 1절)
- 부서 간 `serve` 엔드포인트 미개방 — 인증 설정이 선행(4-2절)
- 부서별 **신원** 분리 미완 — 지금은 한 계정 토큰을 두 부서가 공유(4-3절 A)
- 리서치 두 컨테이너 모두 `tirith security scanner enabled but not available`
  경고 — 명령 스캔이 패턴 매칭으로 떨어진다. 에이전트에게 셸을 주지 않는
  현재 구성에서는 영향이 없지만, 도구를 붙일 때 다시 본다.
- 대시보드 인증 미설정 (각자 1회 필요)
- 리서치의 MCP keepalive/read 오류와 worker retry·latency는 배포 후 다시 측정해야
  한다. MCP loop-stall watchdog은 재기동 경계를 제공하지만 외부 provider의
  안정성 자체를 보장하지 않는다.
- LangSmith·Discord·Notion 변경은 저장소에 반영되어 있으므로 dispatcher,
  supervisor, BFF, research-mcp를 재생성한 뒤 target trace로 runtime 검증한다.
