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

```
compose 프로젝트 hedgefund
├── hermes              ← nousresearch/hermes-agent:latest, `gateway run`
│                          부서 Profile 8개를 **한 런타임이** 호스팅
├── hermes-dashboard    ← 같은 이미지, profiles:[dashboard] 로 옵트인
├── research-api / market-api   ← 부서 읽기 전용 조회면
├── batch-collectors / ls-realtime / news-watcher / ls-news
└── timescaledb

데이터: ${USERPROFILE}/.hermes  →  컨테이너 /opt/data (bind mount)
└── profiles/
    ├── research-department/{config.yaml, SOUL.md}   ← 저장소에서 동기화
    ├── quant-backtest-department/ ... (8개)
    ├── auth.json, memories/, sessions/, state.db*   ← 로컬 전용, git 제외
```

### 왜 부서별 컨테이너 8개가 아닌가

계획서의 목표 이름은 `research-hermes`처럼 **부서별 컨테이너**다. 그런데
부서별로 나누는 목적은 이름이 아니라 **Credential·Memory Namespace 분리**이고,
그러려면 데이터 디렉터리부터 부서별로 갈라야 한다. 지금 컨테이너만 8개로
쪼개면 `/opt/data` 하나를 8개가 공유해 **분리된 척만 하게 된다** — 한 부서의
세션·기억·자격이 옆 부서에서 그대로 보이는데 이름만 다른 상태다.

그래서 v1은 런타임 1개 + Profile 8개로 두고, 분리는 아래 조건이 갖춰질 때 한다.

| 부서별 분리 선행 조건 | 지금 상태 |
|---|---|
| 부서별 데이터 디렉터리 (`~/.hermes-<dept>/`) | 없음 — 공용 1개 |
| 부서별 Provider 자격 | 없음 — 공용 `auth.json` |
| 부서별 Tool Gateway 엔드포인트 | 없음 — `tool_allowlist`는 선언만 |

Profile의 `tool_allowlist` / `forbidden_tools`는 **선언**이며, 이것을 강제하는
Tool Gateway가 붙기 전까지는 계약 문서에 가깝다. 강제 지점이 생기기 전에
"권한 분리 완료"라고 말하지 않는다.

---

## 2. 처음 붙일 때 (팀원 각자 1회)

```bash
# 1) 이미지 (4GB)
docker pull nousresearch/hermes-agent:latest

# 2) 게이트웨이 기동 — 첫 실행이 ~/.hermes 를 만든다
docker compose up -d hermes

# 3) 부서 Profile 8개 생성 (컨테이너 안에서)
for p in research-department quant-backtest-department trading-department \
         accounting-portfolio-department risk-management qa-department \
         ceo-agent hr-department; do
  docker exec hedgefund-hermes hermes profile create "$p"
done

# 4) 저장소 사본 → 런타임 (config.yaml, SOUL.md 만)
HERMES_HOME="$HOME/.hermes" ./scripts/sync_hermes_profiles.sh push

# 5) 확인 — 8개가 poolside/laguna-s-2.1:free 로 보이면 반영된 것
docker exec hedgefund-hermes hermes profile list
```

### 경로 함정 (여기서 제일 많이 막힌다)

Windows 네이티브 설치본은 `%LOCALAPPDATA%\hermes`를 쓰고 컨테이너는
`${USERPROFILE}/.hermes`를 쓴다 — **다른 디렉터리다.** 섞으면 "분명 profile을
만들었는데 목록에 없다"가 된다. 도커를 기준으로 삼고 동기화 스크립트에
`HERMES_HOME`을 반드시 준다. (`~/.hermes`는 OneDrive 동기화 대상 밖이라
state.db 손상 위험이 없다 — 저장소 compose 상단의 named volume 원칙과 같은 이유.)

---

## 3. 일상 운영

```bash
git pull && HERMES_HOME="$HOME/.hermes" ./scripts/sync_hermes_profiles.sh push
docker compose restart hermes            # profile 변경 반영

# 로컬에서 profile 을 고쳤다면 저장소로 되돌린 뒤 커밋
HERMES_HOME="$HOME/.hermes" ./scripts/sync_hermes_profiles.sh pull
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
- 부서별 Credential·Memory 분리 미구현
- 대시보드 인증 미설정 (각자 1회 필요)
- 리서치 Profile ↔ `scripts.py` 파이프라인 호출 배선 미구현 —
  현재 Hermes는 부서 페르소나로 대화만 가능하고 우리 LangGraph 파이프라인을
  도구로 호출하지 못한다. 이 배선이 "부서가 실제로 도는" 마지막 조각이다.
