begin;

-- The independent-review input projection is part of the safety contract.
-- Existing rows were produced before the planner's competing-objection fields
-- were removed from the Worker view, so they must remain historical evidence
-- and must not silently become a v2 generation cache.
alter table research.proposal_review_outcomes
    add column if not exists review_contract_version text not null default 'legacy';

alter table research.proposal_review_outcomes
    add constraint chk_review_contract_version
    check (btrim(review_contract_version) <> '');

create index if not exists idx_review_exact_cache
    on research.proposal_review_outcomes
       (proposal_draft_sha256, review_contract_version, title);

comment on column research.proposal_review_outcomes.review_contract_version is
  'Worker input/validation contract used to produce the review; legacy rows are not reusable as a v2 generation cache.';

commit;
