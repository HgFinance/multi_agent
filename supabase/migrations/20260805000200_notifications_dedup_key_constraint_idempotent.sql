begin;
-- Migration sequence is after the already-applied 20260805000100 revision.

-- 20260805000100의 ADD CONSTRAINT를 재실행 가능하게 고친다.
--
-- 소유: 영주 (CEO Office)
-- 근거: 2026-08-05 P0-2 실 DB 검증 중 이 constraint를 개발 DB에 먼저 수동 적용한 뒤
--       같은 SQL을 마이그레이션 파일로 커밋했다 - PR #148 병합 시 Supabase 자동
--       마이그레이션 적용기가 그 파일을 다시 실행하면서
--       `relation "notifications_dedup_key_channel_unique" already exists`로 실패했다
--       (constraint는 DROP CONSTRAINT IF EXISTS 없이 ADD CONSTRAINT만 있어 재실행에
--       안전하지 않았다 - order_events_broker_id_unique.sql이 이미 쓴 "드롭 후 추가"
--       패턴을 안 따른 게 원인).
--
-- 이미 병합된 20260805000100 파일 내용은 그대로 두고(체크섬 불일치를 피한다), 이
-- 후속 마이그레이션에서 같은 이름을 먼저 드롭한 뒤 다시 추가한다 - constraint가
-- 이미 있든 없든 끝에는 항상 같은 상태(unique(dedup_key, channel))로 수렴하고,
-- 이후 재실행에도 안전하다.

alter table governance.notifications
  drop constraint if exists notifications_dedup_key_channel_unique;

alter table governance.notifications
  add constraint notifications_dedup_key_channel_unique
  unique (dedup_key, channel);

commit;
