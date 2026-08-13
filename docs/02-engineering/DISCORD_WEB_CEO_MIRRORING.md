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
