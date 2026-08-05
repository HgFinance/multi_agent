begin;
-- Migration sequence is after the already-applied 20260805000200 revision.

-- 20260805000100/20260805000200의 재발 원인을 기록하고, 같은 방어적 재확인을 한 번
-- 더 한다(멱등 - 상태를 바꾸지 않는다).
--
-- 소유: 영주 (CEO Office)
-- 근거: 2026-08-05 - 20260805000200 병합 뒤에도 Supabase의 자동 마이그레이션
--       적용기가 "already exists"로 계속 실패했다. SQL 자체(000200)는 이미 멱등했다 -
--       진짜 원인은 `supabase_migrations.schema_migrations` 이력 테이블이었다.
--       20260805000100/000200 둘 다 개발 DB에 psycopg2로 직접 적용했을 뿐 Supabase
--       CLI/API 경로를 안 거쳐서 이 이력 테이블에 기록되지 않았고, 그래서 Supabase
--       자동 적용기가 매번 "아직 안 적용됨"으로 보고 두 파일을 계속 재실행 시도했다.
--       두 버전을 이력 테이블에 수동으로 INSERT해 수복했다
--       (`supabase migration repair --status applied`와 동일한 효과).
--
-- 이 마이그레이션은 그 수복을 문서화하고, 최종 제약 상태(unique(dedup_key, channel))를
-- 한 번 더 방어적으로 재확인한다 - 이미 올바른 상태면 drop+add가 순수 no-op이다.

alter table governance.notifications
  drop constraint if exists notifications_dedup_key_channel_unique;

alter table governance.notifications
  add constraint notifications_dedup_key_channel_unique
  unique (dedup_key, channel);

commit;
