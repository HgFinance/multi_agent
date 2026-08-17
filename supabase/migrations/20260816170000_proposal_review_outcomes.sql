begin;

-- Independent proposal review is a first-class factory outcome. Previously a
-- STOP existed only in the research MCP process memory: the proposal was not
-- published (correct), but its lead stayed "unused" and was selected again.
create table if not exists research.proposal_review_outcomes (
    review_id                  text primary key,
    case_id                    text        not null,
    lead_ids                   text[]      not null,
    title                      text        not null,
    proposal_draft_sha256      text        not null,
    verdict                    text        not null,
    competing_explanation      text        not null,
    competing_codes            text[]      not null,
    falsification_test         text        not null,
    planner_run                text        not null,
    skeptic_run                text        not null,
    created_at                 timestamptz not null default now(),

    constraint chk_review_leads check (cardinality(lead_ids) >= 1),
    constraint chk_review_title check (btrim(title) <> ''),
    constraint chk_review_digest check (proposal_draft_sha256 ~ '^[0-9a-f]{64}$'),
    constraint chk_review_verdict check (verdict in ('PROCEED', 'STOP')),
    constraint chk_review_explanation check (btrim(competing_explanation) <> ''),
    constraint chk_review_codes check (cardinality(competing_codes) >= 1),
    constraint chk_review_falsification check (btrim(falsification_test) <> ''),
    constraint uq_review_draft_title unique (proposal_draft_sha256, title)
);

create index if not exists idx_review_leads_stop
    on research.proposal_review_outcomes using gin (lead_ids)
    where verdict = 'STOP';
create index if not exists idx_review_created
    on research.proposal_review_outcomes (created_at desc);

comment on table research.proposal_review_outcomes is
  'Durable independent-skeptic verdicts. STOP consumes the reviewed lead version; a materially revised idea must enter as a new lead.';

commit;
