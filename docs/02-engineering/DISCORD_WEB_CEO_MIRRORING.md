# Discord ↔ Web CEO 미러링 계약

이 문서는 현재 AWS Hermes 8-profile runtime 위에 추가된 BFF 미러링 경계다.
Hermes profile, Discord token, systemd gateway, Kanban DB를 프론트엔드가 직접
읽거나 공유하지 않는다.

## 현재 구현 범위

```text
Web/Discord human message
  → POST /ui/ceo/ingress
  → request_id + source_message_id dedup
  → single POST /ui/ceo/ask implementation
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

`deploy/hermes-discord/gateway_patch.py`가 기존 Hermes Discord gateway의 수신
경계에 설치된다. CEO/Trading 프로필의 사람 메시지는 이 shim이
`/ui/ceo/ingress`로 전달하고, 성공(`200/202`) 또는 중복(`409`)이면 원래
Hermes handler를 다시 호출하지 않는다. BFF가 runtime에서 응답하지 않으면
기존의 제한된 재시도 후 `failed_closed`로 종료하며, 원래 Hermes 경로로
우회하지 않는다. 따라서 애매한 네트워크 결과가 중복 workflow나 중복 주문으로
이어지지 않는다.

컨테이너 최초 기동 순서는 루트 `docker-compose.yml`의 기존 BFF
`/health/ready` healthcheck를 `ceo-hermes`가 `service_healthy` 조건으로
기다리도록 보장한다. 이는 startup race만 줄이며, runtime 장애의 fail-closed
정책을 대신하지 않는다.

`failed_closed`는 `discord-ingress` 구조화 key-value 로그로 남는다. 운영 알림이
필요한 경우 CEO 컨테이너에 전용 `CEO_INGRESS_ALERT_WEBHOOK_URL`을 설정한다.
알림은 별도 daemon 작업으로 전송하고 기본 60초 cooldown을 적용하므로 ingress
요청을 기다리게 하지 않는다. 웹훅이 비어 있어도 로그와 Discord 사용자 안내는
그대로 남는다. 이 웹훅은 `DISCORD_WEBHOOK_URL`과 분리해 업무 채널로 장애
알림이 잘못 전송되지 않게 한다.

Discord token/API key는 계속 AWS `~/.hermes/profiles/*/.env`에만 둔다.
프론트 번들, BFF response, event payload에 넣지 않는다.

외부 adapter 또는 Hermes bridge는 다음 규칙을 유지해야 한다.

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

## Discord 작성자 ↔ local fixture 매핑

Web과 Discord는 mirror ingress에서 같은 `CanonicalIngress` 계약으로 정규화된다.
Discord 요청은 채널이 아니라 작성자 ID를 `DISCORD_ACTOR_MAP`의 고정 fixture와
결합한다. 매핑된 `user_id`가 `owner_id`가 되며, `fund_id`는 매핑의 세 번째 값 또는
Governance membership 역참조로 결정한다. 단일 `ceo_query`는 current Mandate를 조회해 루트 카드에
`hgfinance.mandate-snapshot.v1` 값 블록을 동결한다. `requested_by=`도 같은 actor
값을 사용하므로 두 채널이 서로 다른 사용자 컨텍스트를 만들지 않는다.

`deploy/hermes-discord/gateway_patch.py`는 Discord 좌표와 actor/Fund 매핑을 이
공용 ingress로 전달한다. 예전처럼 Hermes CEO 프로필로 바로 우회해
`build_root_body`를 건너뛰는 경로를 현행 계약으로 설명하지 않는다.

현재 범위는 `DISCORD_ACTOR_MAP`과 고정 `X-User-Id`가 가리키는 local fixture다.
이 값은 로그인, 세션, 가입 또는 외부 사용자 인증이 아니다. 서버 간 Trading
proof도 브라우저 로그인 token으로 해석하지 않는다.

```text
DISCORD_ACTOR_MAP=<discord_user_id>:<user_id>
```

Backend는 위 2칸 형식을 기본으로 사용하고, Governance 역참조가 불가능한 환경을
위해 `<discord_user_id>:<user_id>:<fund_id>` 3칸 형식도 허용한다. 프론트의 고정
fixture까지 같은 환경변수에서 설정하려면 3칸 형식이 필요하다. 이 프로젝트 범위에서
별도 로그인 시스템은 추가하지 않는다.

### 매핑되지 않은 Discord 작성자

매핑이 없으면 `owner_id`·`fund_id`를 만들지 않는다. `ceo_query`는 Mandate 조회를
건너뛰고 snapshot 없이 진행하므로 “이 요청에는 사용자 한도가 없다”가 정확히
유지된다. 임의 기본 계정이나 로그인 사용자를 만들지 않는다.
