begin;

-- 담당자: 재일 (리서치본부)
-- 근거: 재일님 지시 2026-08-02 "발생한 모든 문제들 해결해놔" 중 ③.
--
-- 문제: universe_manager(RES-01)가 거래가능 여부를 LS REST(t1404/t1405)로
--       **직접** 판정한다. 그래서 파이프라인을 돌리는 컨테이너마다 LS 자격이
--       필요해졌다(2026-08-02 research-mcp 에 LS 키를 넣어야 했던 이유).
--       자격이 퍼지는 것은 그 자체로 위험이고, 통합계획 6.2 "Agent 는 부서
--       읽기 전용 API 만" 원칙과도 어긋난다.
--
-- 해법: 자격을 **옮기지 않고 없앤다.** 수집기(자격 보유)가 제한 목록을 이
--       테이블에 남기고, 조회면(자격 없음)이 그것을 서빙한다. 우리 저장소의
--       일관된 형태다 - 수집기는 쓰고 API 는 읽는다.
--
-- 부수 효과: 판정이 PIT 로 재현된다. 예전에는 "그날 무엇이 정지였나"를
--       되짚을 수 없었다(LS 는 현재 목록만 준다). 이제 as_of 로 남는다.

create table research.symbol_restrictions (
  restriction_id uuid primary key default gen_random_uuid(),
  as_of date not null,                    -- 스냅샷 거래일 (KST)
  symbol text not null,                   -- KRX 6자리
  reason text not null,                   -- HALTED / ADMINISTERED / ...
  source_tr text not null,                -- t1404 / t1405 (근거 추적)
  observed_at timestamptz not null default now(),
  -- 같은 날 같은 종목이 여러 목록에 걸릴 수 있다(정지+관리). 사유별로 남기고
  -- 판정은 심각도 순서로 하나를 고른다 - 사실을 지우지 않는다.
  unique (as_of, symbol, reason)
);

create index symbol_restrictions_asof_idx
  on research.symbol_restrictions (as_of desc, symbol);

-- 스냅샷이 실제로 있었는지(빈 날 = 수집 실패)를 구분하려면 실행 자체를
-- 남겨야 한다. 목록이 0건인 날과 수집을 못 한 날은 완전히 다른 사실이다.
create table research.symbol_restriction_runs (
  run_id uuid primary key default gen_random_uuid(),
  as_of date not null unique,
  list_sizes jsonb not null default '{}'::jsonb,   -- 사유 -> 전체 목록 크기
  total_rows integer not null default 0,
  collected_at timestamptz not null default now(),
  collector_version text not null
);

alter table research.symbol_restrictions enable row level security;
alter table research.symbol_restriction_runs enable row level security;

commit;
