begin;

-- Data coverage checks happen before preregistration and before an experiment
-- row is created. They therefore do not consume family trial budgets or enter
-- DSR/PBO multiple-testing accounting. A stable coverage fingerprint makes
-- retries idempotent while last_checked_at supports low-cost periodic rechecks.
create table if not exists quant.data_feasibility_checks (
    check_id              uuid primary key default gen_random_uuid(),
    hypothesis_id         uuid        not null
      references quant.hypotheses(hypothesis_id) on delete cascade,
    research_lane         text        not null,
    cutoff                timestamptz not null,
    coverage_fingerprint  text        not null,
    status                text        not null,
    details               jsonb       not null default '{}'::jsonb,
    first_checked_at      timestamptz not null default now(),
    last_checked_at       timestamptz not null default now(),

    constraint chk_data_feasibility_lane check (
      research_lane in ('INTRADAY_EVENT')),
    constraint chk_data_feasibility_status check (
      status in ('PASS', 'NEEDS_DATA')),
    constraint chk_data_feasibility_fingerprint check (
      coverage_fingerprint ~ '^[0-9a-f]{64}$'),
    constraint uq_data_feasibility_coverage unique (
      hypothesis_id, coverage_fingerprint)
);

create index if not exists idx_data_feasibility_retry
  on quant.data_feasibility_checks
  (hypothesis_id, last_checked_at desc)
  where status = 'NEEDS_DATA';

comment on table quant.data_feasibility_checks is
  'Pre-trial causal data coverage probes. Rows are not experiments and must not count toward trial pressure, DSR, or PBO.';

commit;
