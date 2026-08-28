# Discord ↔ Web CEO 미러링 계약

이 문서는 현재 AWS Hermes 8-profile runtime 위에 추가된 BFF 미러링 경계다.
Hermes profile, Discord token, systemd gateway, Kanban DB를 프론트엔드가 직접
읽거나 공유하지 않는다.

## 현재 구현 범위

```text
Web/Discord human message
  → POST /ui/ceo/ingress
  → BFF central intent router
  ├─ 일반 질의: request_id + source_message_id dedup
  │             → one CEO root Kanban task
  │             → Redis hf:ui-ceo-mirror:v1
  │             → GET /ui/ceo/events or /ui/ceo/events/stream
  └─ 전략 질의: Strategy Hermes intake
                → labs/<request_id>/ (CEO/Kanban 없음)
                → GET /ui/strategy-research/requests/<request_id>
  └─ 명시적 전략 배포: exact completed lab + tested symbols
                → POST /ui/strategy-research/requests/<request_id>/deploy
                → AWAITING_APPROVAL + compact backtest report
                → 명시적 사람 승인
                → immutable PAPER Bundle + private strategy container
                → GET /ui/strategy-research/requests/<request_id>/deployments
                → GET .../<deployment_id> (live container state)
                → POST .../<deployment_id>/power (start/stop)
                → POST .../<deployment_id>/remove (retire container)
```

기존 `POST /ui/ceo/ask`도 `source=web` canonical ingress로 보호된다. 기존 응답
필드와 `202 Accepted` 계약은 일반 CEO 질의에 대해 유지한다. 전략 생성 의도는 같은
BFF endpoint에서 `autonomous-research-request.v1`을 반환하고 `/ui/strategy-research/
requests/<request_id>`로 상태를 조회한다. 기존 `/ui/ceo/tasks*` read API도 그대로
사용한다.

사람이 `하이닉스 전략 배포해줘`처럼 명시적으로 요청하면 중앙 라우터가 PAPER
배포 요청으로 분기한다. 종목명은 KRX 코드로 정규화하고, 완료된 연구실이 하나로
특정될 때만 연결한다. 요청은 결과 JSON의 SHA-256과 계획 ID를 함께 기록하고,
사람이 보고서 요약을 확인하기 전에는 `AWAITING_APPROVAL`에서 멈춘다. 승인 시
Strategy Hermes가 임의 코드를 실행하지 않고 allowlisted 3분봉 SMA 5/20/60
Bundle을 만든 뒤 private runtime-control에 PAPER 컨테이너를 요청한다.
현재 PAPER Bundle은 `PAPER_ORDERING`으로 실행할 수 있으며 조건 충족 시
Trading API의 PAPER directive를 거쳐 설정된 LS PAPER 계좌에 모의주문을 제출한다.
child에는 브로커 키를 넣지 않는다. `SIGNAL_ONLY` Bundle은 하위 호환용 관측
모드이고, `LIVE`는 항상 `BLOCKED`다.

승인된 배포는 Web 버튼 또는 `전략 배포 승인 <deployment_id>`로 시작할 수 있다.
연구 결과가 `PIVOT`/`REVIEW_REQUIRED`인 경우에는 일반 승인으로 열리지 않으며,
`STRATEGY_TOP_LEVEL_APPROVER_USER_IDS`에 등록된 최상위 사람이
`전략 배포 예외 승인 <deployment_id>`처럼 예외 의도를 명시해야 한다. 이 경로는
릴리스·후보 판정 게이트만 감사 기록과 함께 예외 처리하고, 결과 해시·전략 서명·2%
익절 조건·종목·PAPER 계약은 다시 검증한다. LIVE는 계속 차단되고, PAPER는
계좌·현금·호가·세션·멱등성 검사를 통과한 경우에만 주문을 생성한다.
실제 PAPER 컨테이너 수명주기를 사용하려면 운영자가 `ENABLE_STRATEGY_CONTAINER_CONTROL=true`와
양쪽 내부 서비스에 동일한 `STRATEGY_RUNTIME_SERVICE_TOKEN`을 별도로 설정해야 한다.
`전략 컨테이너 중지/시작`은 해당 컨테이너만 제어하고, `전략 배포 제거`는 컨테이너를
폐기하되 연구 원본·백테스트 결과·Bundle을 삭제하지 않고 `REMOVED` 감사 상태로
남긴다. 모든 수명주기 명령은 요청자 소유권과 정확한 deployment ID를 재검증한다.

운영 Gateway의 Discord ingress는 서비스별 허용 채널을 먼저 판정한다. CEO는
`DISCORD_CEO_ALLOWED_CHANNELS`, QA는 `QA_DISCORD_ALLOWED_CHANNELS`, HR은
`HR_DISCORD_ALLOWED_CHANNELS`를 사용하며, 허용 목록 밖 메시지는 mention 유무와
무관하게 Hermes와 중앙 claim에 도달하지 않는다. 허용 채널의 mention/free-response
정책도 adapter의 기존 설정과 함께 적용되므로 mention이 없는 미팅 메시지가 부서
Gateway를 우회하지 않는다. CEO·QA·HR 패치 Gateway는 `/opt/kanban`의 전역 inbound
claim을 공유해 같은 Discord message ID를 프로필별로 각각 소유하지 않는다.

CEO의 `DISCORD_CEO_ALLOWED_CHANNELS`와 `DISCORD_CEO_FREE_RESPONSE_CHANNELS`는
`DISCORD_CEO_CHANNEL_ID`와 같은 공용 CEO 채널을 가리켜야 한다. 이 값이 어긋나면
mention이 있는 정상 질의도 BFF ingress 전에 `CHANNEL_POLICY`로 종료된다.

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
경계에 설치된다. 패치가 활성화된 CEO/Trading 프로필의 사람 메시지는 이 shim이
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

이 경우 계정 범위 CEO 이력 API가 해당 root를 고정 fixture에 섞어 보여주지 않는
것은 정상 동작이다. 대화 확인은 `GET /ui/discord/thread`의 source/thread
projection을 사용한다. 임의의 기본 계정에 매핑해 `/ui/ceo/tasks/{id}`의 403을
우회하지 않는다. 운영에서 계정별 CEO 이력까지 필요하면 먼저
`DISCORD_ACTOR_MAP`을 서버 설정으로 등록하고 재처리하지 않고 새 요청부터
적용한다.
