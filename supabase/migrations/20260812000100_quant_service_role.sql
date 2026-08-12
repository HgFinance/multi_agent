-- svc_quant - 퀀트 작업 컨테이너 전용 롤
--
-- ▶ 왜 필요한가 (2026-08-12)
--   퀀트본부 Hermes 를 작업 컨테이너로 만들면서 DB 자격이 그 컨테이너에
--   들어간다. 지금은 공용 DATABASE_URL 을 그대로 쓰고 있는데, 그 자격은
--   execution·accounting·governance 원장 전체에 닿는다. 퀀트가 주문이나
--   분개에 닿을 이유는 없다 - 권한 분리는 이름이 아니라 GRANT 다.
--
--   `db/003_roles.sql` 의 svc_* 패턴을 라이브 DB 로 가져온 첫 롤이다.
--   (루트 db/ 는 D0-D2 프로토타입 전용이라 이 DB 에 적용되지 않는다.)
--
-- ▶ 무엇을 주는가
--   - quant 스키마: 읽기 + 쓰기. 가설·실험·데이터셋 매니페스트를 스스로 굳혀야
--     한다. 이것이 "에이전트가 직접 일한다"의 실체다.
--   - market 스키마: **읽기 전용.** 데이터셋의 재료는 읽되 시세 원장은 못 고친다.
--     (market 은 TimescaleDB 에 있어 여기서는 존재할 때만 적용된다.)
--   - execution / accounting / governance: 아무것도 주지 않는다.
--
-- ▶ 적용 후 할 일
--   비밀번호를 정하고 `.env` 의 QUANT_DATABASE_URL 을 이 롤로 채운다.
--   그때까지 quant-hermes 는 공용 DATABASE_URL 로 돈다(compose override 주석).
--   비밀번호는 이 파일에 적지 않는다.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'svc_quant') then
    -- LOGIN 은 주되 비밀번호는 여기서 정하지 않는다. 적용 후
    --   alter role svc_quant with password '...';
    -- 를 따로 실행한다 - 마이그레이션 파일은 git 에 남는다.
    create role svc_quant with login;
  end if;
end
$$;

grant usage on schema quant to svc_quant;

-- 퀀트는 자기 스키마의 주인처럼 일한다 - 가설 상태 전이, 실험 기록,
-- 데이터셋 등재가 전부 여기서 일어난다.
grant select, insert, update on all tables in schema quant to svc_quant;
grant usage, select on all sequences in schema quant to svc_quant;

-- 앞으로 만들어질 테이블에도 같은 권한이 붙게 한다. 이게 없으면 새 테이블이
-- 생길 때마다 조용히 권한 오류가 나고, 그때는 원인을 찾기 어렵다.
alter default privileges in schema quant
  grant select, insert, update on tables to svc_quant;
alter default privileges in schema quant
  grant usage, select on sequences to svc_quant;

-- 삭제는 주지 않는다. Dataset 은 불변이고(pit_dataset.py 머리말) 실험은
-- "실패를 포함한 모든 기록을 남긴다"가 QNT-00 계약이다. 지울 수 있으면
-- 불리한 실험이 사라질 수 있고, 그건 다중검정 분모를 조작하는 것과 같다.
revoke delete, truncate on all tables in schema quant from svc_quant;

-- market 은 읽기만. 이 DB 에 market 스키마가 없으면(시세는 TimescaleDB 에 있다)
-- 조용히 건너뛴다 - 없는 것을 만들지 않는다.
do $$
begin
  if exists (select 1 from information_schema.schemata where schema_name = 'market') then
    execute 'grant usage on schema market to svc_quant';
    execute 'grant select on all tables in schema market to svc_quant';
    execute 'alter default privileges in schema market grant select on tables to svc_quant';
  end if;
end
$$;
