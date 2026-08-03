# Hermes 도커 운영 Runbook

> 현재 기준(2026-08-03): 모든 부서장은 Hermes + Codex 기본/Claude Code 대체이고, 직원은 부서별 독립 LangGraph Worker + Ollama `qwen3:8b`다. 아래의 Laguna·기존 단일 Ollama 호출 예시는 과거 Smoke 기록이다.

담당: 재일 (리서치·퀀트) — 2026-08-02 작성, 2026-08-03 상태 갱신
근거: 재일님 지시 "팀원들이랑 도커로 관리하기로 했는데 어떻게 해야 할지"

이 문서는 **운영 절차서**다. 아키텍처 결정은
[DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md](DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)
3.1~3.2가 이미 정했고(“Hermes는 별도 Image·Credential·Memory Namespace,
Department Backend Image에 설치하지 않는다”), 여기서는 그 결정을 어떻게
실행하는지만 적는다. 이 문서가 계획서를 바꾸지 않는다.

---

## 1. 지금 구성 (2026-08-03 실측 기준)

현재 Git 기준은 8개 Hermes 부서장 Profile 모두 `openai-codex/gpt-5.6-luna`를 기본으로 사용하고 Claude Code를 승인된 대체 런타임으로 둔다. 직원은 부서별 독립 LangGraph Worker이며 현재 Ollama `qwen3:8b`를 사용한다. 아래에 남은 `poolside/laguna-s-2.1:free` 표기는 이전 Docker smoke 기록이며 현재 실행 기준이 아니다. 실제 런타임 반영은 `./scripts/sync_hermes_profiles.sh push` 후 Profile별 credential 상태로 확인한다.

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

현재 저장소 기준 8개 Profile의 Head는 `provider: openai-codex` / `gpt-5.6-luna`이고, 승인된 Claude Code를 대체 provider로 사용할 수 있다. 직원은 Hermes Head 모델과 분리된 부서별 독립 LangGraph Worker + Ollama `qwen3:8b`다. 이전 6개 Nous/Laguna와 Risk·QA만 Codex였던 구성은 Historical snapshot으로만 보존한다.
`scripts/check_hermes_profiles.py`는 부서별 Head 모델과 Employee 모델을 각각 검증해야 하며, 전체 Profile을 하나의 모델로 비교하지 않는다.
모델 교체는 benchmark와 HR·QA 승인 후 Profile 및 `OLLAMA_CHAT_MODEL`을 함께 변경한다.

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
- 부서별 **신원** 분리 미완 — 지금은 한 계정 토큰을 두 부서가 공유(4-3절 A)
- 리서치 두 컨테이너 모두 `tirith security scanner enabled but not available`
  경고 — 명령 스캔이 패턴 매칭으로 떨어진다. 에이전트에게 셸을 주지 않는
  현재 구성에서는 영향이 없지만, 도구를 붙일 때 다시 본다.
- 대시보드 인증 미설정 (각자 1회 필요)
- 리서치 Profile ↔ `scripts.py` 파이프라인 호출 배선 미구현 —
  현재 Hermes는 부서 페르소나로 대화만 가능하고 우리 LangGraph 파이프라인을
  도구로 호출하지 못한다. 이 배선이 "부서가 실제로 도는" 마지막 조각이다.
