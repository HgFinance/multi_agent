begin;

-- Do not hold a database row lock while the relay waits on Redis.  A claim is
-- durable, expires after a bounded interval, and is finalized only by the
-- worker that owns the claim token.  A crash between publish and finalize may
-- replay the event after expiry; consumers deduplicate by event_id.
alter table execution.conditional_rule_outbox
  add column if not exists claim_token text,
  add column if not exists claim_expires_at timestamptz;

do $$
begin
  if not exists (
    select 1
      from pg_constraint
     where conrelid='execution.conditional_rule_outbox'::regclass
       and conname='conditional_rule_outbox_claim_pair_check'
  ) then
    alter table execution.conditional_rule_outbox
      add constraint conditional_rule_outbox_claim_pair_check
      check (
        (claim_token is null and claim_expires_at is null)
        or (claim_token is not null and claim_expires_at is not null)
      );
  end if;
end
$$;

create index if not exists conditional_rule_outbox_claim_idx
  on execution.conditional_rule_outbox (claim_expires_at, created_at, event_id)
  where published_at is null;

grant update (claim_token,claim_expires_at)
  on execution.conditional_rule_outbox to svc_conditional_rule_worker;

comment on column execution.conditional_rule_outbox.claim_token is
  'Short-lived relay lease owner; null means the row is not claimed.';
comment on column execution.conditional_rule_outbox.claim_expires_at is
  'UTC expiry for the relay lease; expired claims are safely retryable.';

commit;
