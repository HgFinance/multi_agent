begin;

-- P1-1 HR-03: Eval 실패는 기존 Profile을 유지하는 HOLD로 종료하고, 후보별
-- OBSERVING 판단 근거(비용·품질·안전·회귀)를 append-only Scorecard로 남긴다.
-- 기존 개발 DB에서 재실행해도 안전하도록 상태 check를 명시적으로 교체한다.

alter table workforce.improvement_candidates
  drop constraint if exists improvement_candidates_status_check;

alter table workforce.improvement_candidates
  add constraint improvement_candidates_status_check
  check (status in (
    'PROPOSED', 'EVALUATING', 'SHADOW', 'PENDING_APPROVAL', 'APPROVED',
    'REJECTED', 'HOLD', 'DEPLOYED', 'OBSERVING', 'KEPT', 'ROLLED_BACK', 'RETIRED'
  ));

create table if not exists workforce.improvement_candidate_scorecards (
  scorecard_id uuid primary key default gen_random_uuid(),
  candidate_id uuid not null references workforce.improvement_candidates(candidate_id),
  window_start timestamptz not null,
  window_end timestamptz not null,
  input_tokens bigint,
  output_tokens bigint,
  total_cost numeric(18, 6),
  qa_eval_run_id uuid references audit.eval_runs(eval_run_id),
  quality_score numeric(8, 6),
  safety_finding_count integer,
  regression_count integer,
  recorded_by text not null,
  recorded_at timestamptz not null default now(),
  check (window_end > window_start),
  check (input_tokens is null or input_tokens >= 0),
  check (output_tokens is null or output_tokens >= 0),
  check (total_cost is null or total_cost >= 0),
  check (quality_score is null or (quality_score >= 0 and quality_score <= 1)),
  check (safety_finding_count is null or safety_finding_count >= 0),
  check (regression_count is null or regression_count >= 0)
);

drop trigger if exists improvement_candidate_scorecards_append_only
  on workforce.improvement_candidate_scorecards;

create trigger improvement_candidate_scorecards_append_only
before update or delete on workforce.improvement_candidate_scorecards
for each row execute function governance.reject_append_only_change();

alter table workforce.improvement_candidate_scorecards enable row level security;

commit;
