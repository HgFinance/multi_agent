begin;

-- The operator frontend currently sends this single explicit principal in
-- X-User-Id.  The private control DB does not run supabase/seed.sql, so this
-- canonical migration keeps its user/profile/Fund grant contract in sync with
-- the frontend instead of relying on JWT-side lazy projection.
insert into accounting.funds
  (fund_id, fund_code, name, base_currency, inception_date, status)
values
  ('b13f5cd1-5df0-4025-92cf-9be03b1a0296', 'TEST-CEO-MANDATE',
   'Test CEO Mandate Fund', 'USD', date '2026-08-01', 'ACTIVE')
on conflict (fund_id) do nothing;

insert into governance.user_profiles
  (user_id, display_name, timezone, status, identity_provider, auth_subject_observed_at)
values
  ('00000000-0000-4000-8000-00000000cec0', 'Fund Owner', 'Asia/Seoul',
   'ACTIVE', 'supabase', now())
on conflict (user_id) do update
  set auth_subject_observed_at = excluded.auth_subject_observed_at;

insert into governance.fund_memberships
  (fund_id, user_id, role, status, effective_from, effective_to)
values
  ('b13f5cd1-5df0-4025-92cf-9be03b1a0296',
   '00000000-0000-4000-8000-00000000cec0', 'OWNER', 'ACTIVE', now(), null)
on conflict (fund_id, user_id, role) do update
  set status = 'ACTIVE',
      effective_from = least(governance.fund_memberships.effective_from, excluded.effective_from),
      effective_to = null;

commit;
