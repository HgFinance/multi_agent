begin;

-- P1-2 HR-04: quality_snapshots 에 recorded_by 를 추가한다. 형제 테이블
-- improvement_candidate_scorecards(P1-1)는 처음부터 recorded_by not null 을 가졌는데
-- quality_snapshots(2026-07-31 최초 생성)에는 누락돼 있었다 - 누가 집계했는지 없이는
-- "인사팀이 집계한다"는 소유권 원칙을 감사로 확인할 수 없다. seed 데이터도 없어 0건이므로
-- 기본값 없이 바로 not null 로 추가한다.

alter table workforce.quality_snapshots
  add column if not exists recorded_by text not null;

commit;
