begin;

-- Hosted Supabase owns authentication; the private control database owns
-- operational authorization and portfolio state.
--
-- governance.user_profiles.user_id is the verified Supabase JWT `sub`, not a
-- local auth.users row.  Keeping the original cross-schema FK would freeze the
-- user set at the last database restore and reject every user created in hosted
-- Auth afterwards.  Domain tables continue to reference this PII-minimal local
-- projection and therefore remain fully relational inside the control DB.

alter table governance.user_profiles
  drop constraint if exists user_profiles_user_id_fkey;

alter table governance.user_profiles
  add column if not exists identity_provider text not null default 'supabase'
    check (identity_provider in ('supabase')),
  add column if not exists auth_subject_observed_at timestamptz;

comment on table governance.user_profiles is
  'PII-minimal operational projection of verified external identities; hosted Supabase Auth remains the identity source of truth.';

comment on column governance.user_profiles.user_id is
  'Verified Supabase access-token sub. Intentionally has no FK to the control database auth.users schema.';

comment on column governance.user_profiles.display_name is
  'Operational display label only; email, phone, credentials and auth metadata stay in hosted Supabase Auth.';

comment on column governance.user_profiles.auth_subject_observed_at is
  'Last time the external subject was verified or synchronised; null denotes a pre-cutover projection.';

commit;
