# Discord ↔ Web CEO 미러링 계약

이 문서는 현재 AWS Hermes 8-profile runtime 위에 추가된 BFF 미러링 경계다.
Hermes profile, Discord token, systemd gateway, Kanban DB를 프론트엔드가 직접
읽거나 공유하지 않는다.

## 현재 구현 범위

```text
Web/Discord human message
  → POST /ui/ceo/ingress
  → request_id + source_message_id dedup
  → existing POST /ui/ceo/ask boundary
  → one CEO root Kanban task
  → Redis hf:ui-ceo-mirror:v1
  → GET /ui/ceo/events or /ui/ceo/events/stream
```

기존 `POST /ui/ceo/ask`도 `source=web` canonical ingress로 보호된다. 기존 응답
필드와 `202 Accepted` 계약은 유지한다. 기존 `/ui/ceo/tasks*` read API도 그대로
사용한다.

## 입력 계약

### Web 호환 입력

`POST /ui/ceo/ask`

```json
{
  "query": "삼성전자 최신 리스크를 분석해줘",
  "request_id": "req_20260813_0001"
}
```

선택 헤더:

```text
X-Source-Message-Id: web:req_20260813_0001
X-Actor-Id: web-user-42
```

### 공통 Discord/Web 입력

`POST /ui/ceo/ingress`

```json
{
  "query": "삼성전자 최신 리스크를 분석해줘",
  "request_id": "req_20260813_0001",
  "source": "discord",
  "source_message_id": "discord:1536997434507657261:991",
  "actor_id": "discord-user-42",
  "actor_type": "user",
  "mirrored": false
}
```

`source_message_id`는 Discord 원본 메시지 ID 또는 Web 입력 ID여야 한다. 같은
`source + source_message_id`가 다른 `request_id`로 다시 오면 `409`이며 CEO를
실행하지 않는다.

`actor_type=bot` 또는 `mirrored=true`인 메시지는 `ignored=true`로 종료한다.
이것이 Discord bot echo가 새 CEO 요청으로 재진입하는 것을 막는 1차 방어선이다.

## 이벤트 계약

`GET /ui/ceo/events?request_id=<id>&after=<event_id>`

`GET /ui/ceo/events/stream?request_id=<id>&after=<event_id>`

SSE는 짧은 연결 후 닫히며, 클라이언트는 마지막 `event_id`를 `after`로 넣어
재연결한다. 이벤트는 `ui.ceo-mirror-event.v1`을 따른다.

```json
{
  "schema_version": "ui.ceo-mirror-event.v1",
  "event_id": "evt-...",
  "request_id": "req_20260813_0001",
  "task_id": "t_...",
  "parent_task_id": null,
  "source": "discord",
  "source_message_id": "discord:...",
  "actor_id": "ceo-agent",
  "actor_type": "agent",
  "lane": "execution",
  "event_type": "CEO_PLAN_CREATED",
  "status": "accepted",
  "summary": "CEO root Kanban workflow가 생성되었습니다.",
  "payload": {},
  "created_at": "2026-08-13T00:00:00+00:00"
}
```

`event_id`는 양쪽 클라이언트에서 반드시 Set으로 dedup한다. `source`는 원본
입력의 출처이며 agent가 만든 미러 event도 원본 source를 유지한다. `lane`은
`execution`과 `evaluation`을 별도 timeline으로 표시한다. QA event는
`evaluation` lane이고 CEO_FINAL을 block하지 않는다.

## Discord adapter 전달사항

현재 저장소 안에는 Hermes Discord gateway의 inbound/outbound loop를 직접 호출하는 코드가 없다. 따라서 이 adapter 연결 전에는 BFF 공용 timeline까지만 구현된 상태다.

AWS에서 실제 Discord 양방향 E2E를 완료하려면 기존 Hermes bridge가 `/ui/ceo/ingress`와 `/ui/ceo/events/stream`을 사용하도록 연결해야 한다. Discord token/API key는 계속 AWS `~/.hermes/profiles/*/.env`에만 둔다.

현재 저장소는 Hermes Discord gateway 내부 token/수신 loop를 수정하지 않는다.
AWS의 Discord adapter 또는 Hermes bridge가 다음 규칙으로 붙어야 한다.

1. 사람 메시지만 `/ui/ceo/ingress`에 전송한다.
2. `request_id`를 생성하고 Discord 원본 message ID를 `source_message_id`로 보낸다.
3. `/ui/ceo/events/stream` 또는 `/ui/ceo/events`를 request별로 구독한다.
4. Discord로 보낼 mirror message에는 `mirrored=true` 메타데이터를 붙인다.
5. bot이 만든 메시지는 다시 `/ingress`에 보내지 않는다.
6. `event_id`를 저장해 재시작/reconnect 시 중복 전송하지 않는다.

Discord token/API key는 현재처럼 AWS `~/.hermes/profiles/*/.env`에만 둔다.
프론트 번들, BFF response, event payload에 넣지 않는다.

## 운영 저장소

`REDIS_URL` 또는 `UI_MIRROR_REDIS_URL`이 설정된 AWS에서는 기존 compose Redis를
사용한다. request claim과 event publish가 실패하면 안전을 위해 `503`을 반환해
CEO 중복 실행을 막는다. Redis가 없는 로컬 단위 테스트에서는 명시적으로
`InMemoryMirrorStore`를 주입한다.

## 채널 ↔ 테스트 계정 매핑 제안 (2026-08-18, 미구현)

프론트엔드는 `DISCORD_ACTOR_MAP`의 첫 유효 binding을 고정 테스트 계정으로 사용하며
(`ai-office/app/lib/currentAccount.ts`), 계정마다 다른 Mandate를 참조한다. Discord
쪽에는 그 계정 개념이 없어 **같은 요청이 채널만 다를 뿐 전부 "요청자 불명"으로
들어온다.** 웹과 Discord를 같은 사용자 기준으로 비교하려면 이 매핑이 필요하다.

### 지금 무엇이 빠져 있나

Discord gateway patch(`deploy/hermes-discord/gateway_patch.py`)는 메시지 본문에
라우팅 컨텍스트(`discord_channel_id` 등)를 주입해 **Hermes CEO 프로필이 직접
처리**하게 한다. `/ui/ceo/ingress`를 호출하지 않는다. 그래서 Discord 경로는
`build_root_body`를 거치지 않고, 결과적으로 다음 둘이 붙지 않는다:

- `requested_by=` — `GET /ui/ceo/tasks?owner_id=`의 계정별 필터 근거
- `hgfinance.mandate-snapshot.v1` — 사용자 한도 스냅샷

즉 **Discord로 물으면 CEO가 사용자 Mandate를 모른 채 답한다.** 웹 경로에는 둘 다
붙으므로, 지금 두 경로는 같은 질문에 다른 근거로 답하고 있다.

### 필요한 것은 어댑터 한 겹뿐이다

계약은 이미 있다. `CanonicalIngress`(`apps/api/ceo_mirror.py`)에 `actor_id`와
`fund_id` 필드가 있고, `_ceo_query`(`apps/api/ceo_mirror_api.py`)가
`actor_type=user`인 요청의 `actor_id`를 `owner_id`로 넘긴다. 익명 fallback
(`anonymous`/`web-user`)은 걸러내 "요청자 불명"을 정확히 유지한다.

빠진 것은 Discord 어댑터가 `/ui/ceo/ingress`로 POST하면서 그 두 필드를 채우는
것뿐이다.

### 채널 기준을 제안하는 이유

Discord author 기준보다 **채널 기준**을 먼저 붙일 것을 제안한다. 채널이 더
정확해서가 아니라 - 채널은 누구나 들어가 쓸 수 있으니 오히려 덜 정확하다 -
테스트 단계에서 담당자가 아무 계정으로나 대신 시험할 수 있어야 하기 때문이다.
트레이딩 담당이 user1 채널에서 쳐도 user1 요청이 되는 편이 E2E 확인에 편하다.

```text
DISCORD_ACTOR_MAP=<discord_user_id>:<user_id>:<fund_id>
```

매핑표는 어댑터 환경변수 하나로 둔다. 진짜 인증이 붙으면 Discord author 기준으로
갈아끼운다.

### 매핑이 없는 채널·DM

`fund_id` 없이 그대로 넘긴다. `ceo_query`가 Mandate 조회를 건너뛰고 스냅샷 없이
진행하므로 "이 요청에는 사용자 한도가 없다"가 정확히 유지된다 - 임의의 기본
계정으로 채우지 않는다(개발 원칙 9). 이미 그렇게 동작한다.

### 이 문서가 정하지 않는 것

`/agent-logs` 화면이 Discord 대화를 어떻게 계정별로 나눠 보여줄지는 정하지
않는다. 그 화면은 다른 브랜치에서 개발 중이고, 위 매핑이 붙으면 이벤트에
`actor_id`가 실리므로 화면 쪽 필터는 그 값을 쓰면 된다.
