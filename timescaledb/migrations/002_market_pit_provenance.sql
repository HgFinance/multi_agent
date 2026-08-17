begin;

-- 관측 시각의 출처를 기록한다 - 측정한 것과 유도한 것을 섞지 않기 위해.
--
-- 우리 수집기는 event_time(거래소) / received_at(수신) / observed_at(관측)을 셋 다
-- 남긴다. 그런데 다른 프로젝트에서 가져온 구간은 원본에 **시각이 ts 하나뿐**이다
-- (2026-08-10 실측: public.ticks 는 ts 단일 컬럼, 밀리초가 전부 0 = 초 단위로
-- 잘린 거래소 시각). 즉 "우리가 언제 알았는가" 가 원본에 없다.
--
-- 그것을 비워 두면 PIT 질의가 그 구간을 통째로 못 쓰고, 아무 값이나 채우면
-- **측정한 것처럼 보인다.** 후자가 훨씬 위험하다 - 유도값을 측정값으로 오인한
-- 백테스트는 자기가 선견을 했는지도 모른다.
--
-- 그래서 유도하되 **유도했다고 기록한다.** 이 표를 보지 않고 그 구간을 쓰는 실험은
-- 없어야 하며, data_resolution 이 이 표를 읽어 구간별 PIT 가용성을 보고한다.
create table if not exists market.pit_provenance (
    provenance_id   bigserial primary key,
    source_table    text        not null,     -- market_ticks / market_quotes
    range_start     date        not null,
    range_end       date        not null,     -- 포함
    observed_at_kind text       not null
        check (observed_at_kind in ('MEASURED', 'DERIVED')),
    -- DERIVED 일 때만 의미가 있다. 무엇을 근거로 얼마를 더했는지 남긴다.
    derivation      text        not null default '',
    lag_bound_ms    integer,                  -- 적용한 보수적 상한
    basis           jsonb       not null default '{}'::jsonb,  -- 실측 지연 분포 등
    origin          text        not null default '',
    note            text        not null default '',
    created_at      timestamptz not null default now(),
    unique (source_table, range_start, range_end)
);

comment on table market.pit_provenance is
  '구간별 observed_at 출처. MEASURED 는 우리가 잰 것, DERIVED 는 거래소 시각에서 보수적으로 유도한 것 - 섞어 쓰면 선견을 못 잡는다';
comment on column market.pit_provenance.lag_bound_ms is
  '유도에 쓴 보수적 상한(ms). 상한은 크게 잡는다 - 실제보다 일찍 알았다고 주장하는 쪽이 선견이므로 위험하다';

-- 우리가 직접 수집한 구간을 MEASURED 로 등록한다. 이 행이 없으면
-- "출처 미상" 과 구분되지 않는다.
insert into market.pit_provenance
  (source_table, range_start, range_end, observed_at_kind, origin, note)
select t.tbl, min(t.d), max(t.d), 'MEASURED', 'hedgefund-ls-realtime',
       '수집기가 event_time/received_at/observed_at 을 모두 기록한 구간'
  from (
        select 'market_ticks'::text tbl, observed_at::date d from market.market_ticks
         union all
        select 'market_quotes', observed_at::date from market.market_quotes
       ) t
 group by t.tbl
on conflict (source_table, range_start, range_end) do nothing;

commit;
