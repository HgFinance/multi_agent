begin;
-- Migration sequence is after the already-applied 20260804001200 revision.

-- `governance.notifications`의 dedup_key 유일성 범위를 채널 단위로 고친다.
--
-- 소유: 영주 (CEO Office)
-- 근거: docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md P0-2(GOV-02 전체 상태 Replay),
--       departments/00-ceo-office/src/notification/notification.py 불변식 3
--       ("dedup_key는 (fund_id, event_type, scope_key)로만 결정된다"),
--       departments/00-ceo-office/tests/test_gov02_replay.py
--
-- ## 무엇이 잘못됐나
--
-- 원래 제약이 `dedup_key text not null unique` — 표 전체에서 dedup_key 하나당 행이
-- 딱 하나만 존재할 수 있다. 그런데 `NotificationService.notify()`는 심각도별
-- 채널마다(CRITICAL=SMS+PUSH+APP 3개) 행을 하나씩 만들고, 그 행들은 전부 **같은
-- dedup_key**를 공유한다(dedup_key가 channel을 안 섞는다 - 불변식 3). 즉 LOW(채널
-- 1개)가 아닌 모든 심각도에서 두 번째 채널 insert부터 이 제약을 위반한다.
--
-- 지금까지 아무 자체 점검도 이걸 못 잡은 이유: CEO Office의 모든 자체 점검이
-- InMemoryNotificationRepository로 강제 전환한다(app.py __main__ 참고) - 이 제약은
-- 실 Postgres에만 있어서 실 DB로 GOV-02 전체 구간을 이어서 돌려본 이번 P0-2 Replay가
-- 처음으로 재현했다(2026-08-05).
--
-- ## 어떻게 고치나
--
-- `unique (dedup_key, channel)`로 바꾼다. 같은 사안(dedup_key)이라도 채널이 다르면
-- 별도 행으로 남을 수 있고, **같은 사안·같은 채널의 중복만** 막는다 - 원래 의도했던
-- "이미 보낸 채널로 또 안 보낸다"와 정확히 일치한다. 애플리케이션의 억제(SUPPRESSED)
-- 판정 로직(notify() 내부 already_sent 체크)은 dedup_key 하나로 전체 채널을 함께
-- 판단하므로 바뀌지 않는다 - 이 마이그레이션은 저장 계층의 유일성 범위만 고친다.

alter table governance.notifications
  drop constraint if exists notifications_dedup_key_key;

alter table governance.notifications
  add constraint notifications_dedup_key_channel_unique
  unique (dedup_key, channel);

commit;
