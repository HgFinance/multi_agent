begin;

-- 실험 작업 큐
--
-- 담당: 재일 (퀀트·백테스트본부 QNT)
-- 근거: 재일님 지시 2026-08-04 "실험도구를 무조건적으로 보장해야 함"
--
-- ▶ 왜 큐인가
--   백테스트는 수 분이 걸린다. HTTP 요청 안에서 돌리면 타임아웃으로 죽고,
--   죽은 요청은 **"실패했는지 아직 도는지" 를 알 수 없게** 만든다.
--   제출과 실행을 분리하면 제출은 즉시 끝나고 상태는 언제든 조회된다.
--
-- ▶ 왜 DB 인가 (Redis 가 아니라)
--   워커가 죽어도 작업이 사라지면 안 된다. 메모리 큐는 재시작이 곧 유실이고,
--   전략 공장에서 주문이 사라지는 것은 공장이 멈춘 것보다 나쁘다 -
--   멈춘 것은 보이지만 사라진 것은 안 보인다.
--   Redis 는 계획대로 "작업 큐로만" 쓸 수 있으나, 지금은 상태의 단일 출처를
--   하나로 두는 쪽이 안전하다. 처리량이 문제가 되면 그때 앞단에 붙인다.

create table if not exists quant.experiment_jobs (
  job_id uuid primary key default gen_random_uuid(),
  hypothesis_id uuid not null references quant.hypotheses(hypothesis_id),
  requested_by text not null,          -- 어느 본부/사람이 주문했나
  status text not null default 'QUEUED'
    check (status in ('QUEUED', 'LEASED', 'DONE', 'FAILED', 'CANCELLED')),
  -- ▶ **실패 사유를 반드시 남긴다.** 사유 없는 실패는 다시 시도할지
  --   폐기할지 판단할 수 없고, 결국 아무도 안 본다.
  failure_reason text,
  experiment_id uuid,                  -- 성공 시 결과 참조
  attempts integer not null default 0,
  max_attempts integer not null default 2,
  -- ▶ lease: 워커가 집어간 시각. 워커가 죽으면 이 값이 낡은 채 남으므로
  --   회수(reclaim)가 가능하다. lease 없이 status 만 쓰면 워커 사망과
  --   정상 실행을 구분할 수 없다.
  leased_at timestamptz,
  leased_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint jobs_failed_needs_reason
    check (status <> 'FAILED' or failure_reason is not null),
  constraint jobs_leased_needs_worker
    check (status <> 'LEASED' or (leased_at is not null and leased_by is not null))
);

comment on table quant.experiment_jobs is
  '실험 주문 큐. 제출과 실행을 분리해 긴 작업이 HTTP 타임아웃에 죽지 않게 한다. '
  'DB 가 단일 출처이므로 워커가 죽어도 주문이 사라지지 않는다.';

comment on column quant.experiment_jobs.leased_at is
  '워커가 집어간 시각. 오래 남아 있으면 워커가 죽은 것이다 - status 만으로는 '
  '사망과 정상 실행을 구분할 수 없다.';

-- 같은 가설에 대기/실행 중인 주문이 둘 이상 생기지 않게 한다.
-- 완료·실패한 것은 여러 개일 수 있다(재시도가 정상이다).
create unique index if not exists experiment_jobs_active_uniq
  on quant.experiment_jobs (hypothesis_id)
  where status in ('QUEUED', 'LEASED');

create index if not exists experiment_jobs_pickup_idx
  on quant.experiment_jobs (status, created_at)
  where status = 'QUEUED';

commit;
