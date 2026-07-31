-- 로컬 개발용 Role 부트스트랩
--
-- 소유: 재일 (리서치본부)
-- 근거: docs/database/README.md 7절 - "Market Writer와 Reader Role은 Infrastructure/IAM에서
--       생성한다. Migration은 해당 Role이 존재할 때만 권한을 부여한다."
--       docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 8.3(Service Identity 분리),
--       9절 Sprint J0 완료 기준 - "다른 본부 Credential로 TimescaleDB에 접속할 수 없다"
--
-- ▶ 이 파일은 로컬 개발 전용이다. Production Role 과 비밀번호는 Infrastructure/IAM 이
--   만들며 이 파일을 그대로 서버에 적용하지 않는다. 비밀번호가 여기 없는 이유도 그것이다.
--
-- 001_initial_market_data.sql 은 Role 이 이미 존재할 때만 grant 를 실행한다.
-- 그래서 순서가 중요하다 - 이 파일을 먼저 적용하고 마이그레이션을 (재)적용한다.
--
-- 적용
--   docker compose exec -T timescaledb psql -U postgres -d market -v ON_ERROR_STOP=1 \
--     < timescaledb/local-dev/001_dev_roles.sql
--   docker compose exec -T timescaledb psql -U postgres -d market -v ON_ERROR_STOP=1 \
--     < timescaledb/migrations/001_initial_market_data.sql
--
-- Collector 는 market_writer, Research/Quant 조회는 market_reader 를 쓴다.
-- 트레이딩·리스크·회계·QA·CEO·인사팀은 어느 Role 도 받지 않고 market-api 를 호출한다.

begin;

do $$
begin
  -- nologin 그룹 Role 로 만든다. 실제 로그인 계정은 이 Role 을 상속받는 별도
  -- 사용자로 만들며, 비밀번호는 Secret Manager 가 관리한다.
  if not exists (select 1 from pg_roles where rolname = 'market_reader') then
    create role market_reader nologin;
  end if;

  if not exists (select 1 from pg_roles where rolname = 'market_writer') then
    create role market_writer nologin;
  end if;
end;
$$;

-- 스키마는 마이그레이션이 public 에서 권한을 회수한 상태다. 두 Role 에만 usage 를 준다.
-- 세부 Table 권한은 마이그레이션의 grant 블록이 부여한다(중복 부여는 무해하다).
grant usage on schema market to market_reader, market_writer;

commit;
