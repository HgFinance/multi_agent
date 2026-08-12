begin;

-- 가설 상태 전이 시각
--
-- 담당: 재일 (퀀트·백테스트본부 QNT)
--
-- 왜: quant-api 의 /jobs/stuck 이 "RUNNING 인 채 오래 있는 실험" 을 찾는데,
--     테이블에 전이 시각이 없어 created_at 으로 대신 쓰고 있었다. 그러면
--     **"만들어진 지 오래됐다" 이지 "그 상태로 오래 있었다" 가 아니다** -
--     어제 만들어 방금 RUNNING 이 된 실험이 멈춘 것으로 잡히고, 반대로
--     오늘 만들어 3시간째 멈춘 실험은 안 잡힌다.
--
--     전략 공장에서 조용히 멈춘 작업이 가장 나쁘다. 그걸 못 찾으면 창구가
--     있으나 마나다.
--
-- 기본값을 created_at 으로 채운다 - now() 로 채우면 기존 13건이 "방금 전이"
-- 로 보여 진짜 멈춘 것을 가린다. 모르는 것을 유리한 쪽으로 가정하지 않는다.

alter table quant.hypotheses
  add column if not exists status_changed_at timestamptz;

update quant.hypotheses
   set status_changed_at = created_at
 where status_changed_at is null;

alter table quant.hypotheses
  alter column status_changed_at set default now();

comment on column quant.hypotheses.status_changed_at is
  '마지막 상태 전이 시각. /jobs/stuck 이 이 값으로 멈춘 실험을 찾는다. '
  '전이할 때마다 갱신한다 - 갱신을 빠뜨리면 그 실험은 영원히 멈춘 것으로 보인다.';

create index if not exists hypotheses_status_changed_idx
  on quant.hypotheses (status, status_changed_at desc);

commit;
