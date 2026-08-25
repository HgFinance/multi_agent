-- TEST ONLY. Do not run this fixture against a production Supabase project.
-- It creates the minimum parent row needed to exercise the governance.mandates
-- FK path (fund_id) for PostgresMandateVersionRepository integration tests.
--
-- It deliberately does NOT create a governance.user_profiles/auth.users row -
-- Account creation is outside this fixture. A governance.mandates
-- row still cannot be inserted with this fixture alone (owner_user_id is
-- not null) - see departments/00-ceo-office/src/mandate/postgres_repository.py
-- self-check for what that blocks.
--
-- Run inside an isolated local/CI database transaction:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -1 -f tests/schema/supabase_governance_test_fixture.sql

begin;

insert into accounting.funds (
    fund_code, name, base_currency, inception_date, status, legal_entity
)
values (
    'TEST-CEO-MANDATE', 'CEO Mandate Contract Test Fund', 'KRW',
    date '2026-01-01', 'ACTIVE',
    '{"test_only": true, "owner": "ceo-office"}'::jsonb
)
on conflict (fund_code) do update
    set name = excluded.name,
        base_currency = excluded.base_currency,
        status = 'ACTIVE',
        legal_entity = excluded.legal_entity,
        updated_at = now();

commit;
