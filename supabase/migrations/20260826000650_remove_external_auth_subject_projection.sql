begin;

-- Local PAPER runs use one fixed fixture identity. The control database stores
-- only that operational principal and fund membership; it is not a browser or
-- Supabase Auth identity projection.
alter table governance.user_profiles
  drop column if exists auth_subject_observed_at,
  drop column if exists identity_provider;

comment on table governance.user_profiles is
  'Operational principals for the fixed local PAPER workflow; no browser login or external auth projection.';
comment on column governance.user_profiles.user_id is
  'Stable operational user UUID. Local PAPER uses the pinned fixture user.';
comment on column governance.user_profiles.display_name is
  'Operational display label for the local PAPER principal.';

commit;
