# Hermes 도커 운영 Runbook

담당: 재일 (리서치·퀀트) — 2026-08-02 작성
근거: 재일님 지시 "팀원들이랑 도커로 관리하기로 했는데 어떻게 해야 할지"

이 문서는 **운영 절차서**다. 아키텍처 결정은
[DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md](DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)
3.1~3.2가 이미 정했고(“Hermes는 별도 Image·Credential·Memory Namespace,
Department Backend Image에 설치하지 않는다”), 여기서는 그 결정을 어떻게
실행하는지만 적는다. 이 문서가 계획서를 바꾸지 않는다.

---

## 1. 지금 구성 (2026-08-02 실측 기준)

계획서 3.1~3.2대로 **부서별 컨테이너 1개 = 부서별 데이터 디렉터리 1개**다.

```
compose 프로젝트 hedgefund
├── research-hermes   ← hedgefund-research-hermes   ~/.hermes-research-department
├── quant-hermes      ← hedgefund-quant-hermes      ~/.hermes-quant-backtest-department
│   (나머지 6개 본부는 같은 형태로 각 담당자가 추가 — 5절)
├── hermes-dashboard  ← profiles:[dashboard] 옵트인, 한 부서만 본다
├── research-api / market-api      ← 부서 읽기 전용 조회면
├── batch-collectors / ls-realtime / news-watcher / ls-news
└── timescaledb

각 컨테이너: ~/.hermes-<부서>  →  /opt/data (bind mount, HERMES_HOME 기본값)
└── profiles/<부서>/{config.yaml, SOUL.md}   ← 저장소에서 동기화
    auth.json, memories/, sessions/, state.db*  ← 로컬 전용, git 제외
```

### 분리의 핵심은 이름이 아니라 저장소다

컨테이너만 쪼개고 `/opt/data`를 공유하면 한 부서의 세션·기억·자격이 옆 부서에서
그대로 보인다 — 이름만 다른 **분리된 척**이다. 그래서 마운트를 부서별로 가른다.
이미지 기본값이 `HERMES_HOME=/opt/data`라 **마운트만 갈면 분리가 끝난다.**

실측 검증 (2026-08-02):

```
$ docker exec hedgefund-research-hermes hermes profile list
  research-department        poolside/laguna-s-2.1:free
$ docker exec hedgefund-quant-hermes    hermes profile list
  quant-backtest-department  poolside/laguna-s-2.1:free
```

각 컨테이너가 **자기 부서 Profile만** 본다.

### 아직 선언 단계인 것

Profile의 `tool_allowlist` / `forbidden_tools`는 선언이다. 이것을 실행 시점에
강제하는 Tool Gateway가 아직 없으므로, 강제 지점이 생기기 전에 "권한 분리 완료"
라고 말하지 않는다. 컨테이너·저장소 분리는 됐고, 도구 호출 강제는 남았다.

---

## 2. 처음 붙일 때 (팀원 각자 1회)

```bash
# 1) 이미지 (4GB, 부서별 컨테이너가 레이어를 공유하므로 1회면 된다)
docker pull nousresearch/hermes-agent:latest

# 2) 내 본부 컨테이너 기동 — 첫 실행이 ~/.hermes-<부서> 를 만든다
docker compose up -d research-hermes quant-hermes

# 3) 각 컨테이너 안에 자기 부서 Profile 하나만 생성
docker exec hedgefund-research-hermes hermes profile create research-department
docker exec hedgefund-quant-hermes    hermes profile create quant-backtest-department

# 4) 저장소 사본 → 런타임 (config.yaml, SOUL.md 만)
#    스크립트가 ~/.hermes-<부서>/profiles/<부서> 를 먼저 찾고, 없으면
#    기존 공용 경로로 떨어진다 - 아직 안 옮긴 본부도 그대로 동작한다.
HERMES_HOME_PREFIX="$HOME/.hermes" ./scripts/sync_hermes_profiles.sh push

# 5) 확인 — 각 컨테이너가 자기 것 하나만 보면 격리된 것이다
docker exec hedgefund-research-hermes hermes profile list
docker exec hedgefund-quant-hermes    hermes profile list
```

### 경로 함정 (여기서 제일 많이 막힌다)

Windows 네이티브 설치본은 `%LOCALAPPDATA%\hermes`를 쓰고 컨테이너는
`${USERPROFILE}/.hermes-<부서>`를 쓴다 — **다른 디렉터리다.** 섞으면 "분명
profile을 만들었는데 목록에 없다"가 된다. 도커를 기준으로 삼고 동기화 스크립트에
`HERMES_HOME_PREFIX`를 준다. (`~/.hermes-*`는 OneDrive 동기화 대상 밖이라
state.db 손상 위험이 없다 — compose 상단의 named volume 원칙과 같은 이유.)

---

## 3. 일상 운영

```bash
git pull && HERMES_HOME_PREFIX="$HOME/.hermes" ./scripts/sync_hermes_profiles.sh push
docker compose restart research-hermes quant-hermes   # profile 변경 반영

# 로컬에서 profile 을 고쳤다면 저장소로 되돌린 뒤 커밋
HERMES_HOME_PREFIX="$HOME/.hermes" ./scripts/sync_hermes_profiles.sh pull
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

## 4-2. 다른 본부를 추가하는 법 (담당자 각자)

`docker-compose.yml`의 `research-hermes` 블록을 복사해 세 곳만 바꾼다.
남의 본부 컨테이너를 대신 만들지 않는다(담당자 표: CLAUDE.md).

```yaml
  risk-hermes:                                   # ① 서비스 이름
    image: nousresearch/hermes-agent:latest
    container_name: hedgefund-risk-hermes        # ② 컨테이너 이름
    restart: unless-stopped
    command: ["gateway", "run"]
    volumes:
      - ${USERPROFILE}/.hermes-risk-management:/opt/data   # ③ 부서 디렉터리
    environment:
      HERMES_UID: 10000
      HERMES_GID: 10000
      OPENAI_API_KEY: ${OPENAI_API_KEY:-}        # 부서별 키 배정(CLAUDE.md)
    mem_limit: 1g
    cpus: 1.0
```

디렉터리 이름은 **Profile 이름과 정확히 같아야 한다**(`~/.hermes-<profile>`).
동기화 스크립트가 그 규칙으로 경로를 찾는다.

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

## 5. 모델·과금

Profile 8개 모두 `provider: nous` / `poolside/laguna-s-2.1:free`다. 무료 티어이며
API Key가 아니라 Nous Portal 로그인 방식이라 `.env`에 키가 없어도 된다.
컨테이너는 `~/.hermes/auth.json`을 쓰므로 **로그인은 컨테이너에서 1회** 한다.

```bash
docker exec -it hedgefund-hermes hermes status     # 인증·모델 확인
```

Bedrock Claude(TECH_STACK_DECISIONS.md의 목표 Gateway)로 옮기는 것은 별개
결정이며 이 Runbook의 범위가 아니다.

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
- 나머지 6개 본부 컨테이너 미생성 (담당자별, 4-2절 절차)
- 부서 간 `serve` 엔드포인트 미개방 — 인증 설정이 선행(4-2절)
- 대시보드 인증 미설정 (각자 1회 필요)
- 리서치 Profile ↔ `scripts.py` 파이프라인 호출 배선 미구현 —
  현재 Hermes는 부서 페르소나로 대화만 가능하고 우리 LangGraph 파이프라인을
  도구로 호출하지 못한다. 이 배선이 "부서가 실제로 도는" 마지막 조각이다.
