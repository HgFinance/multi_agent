-- TEST ONLY. Do not run this fixture against a production Supabase project.
-- It creates the minimum parent rows needed to exercise Risk/QA FK paths.
-- Run inside an isolated local/CI database transaction:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -1 -f tests/schema/supabase_risk_qa_test_fixture.sql

begin;

do $$
declare
    test_fund_id uuid;
begin
    insert into accounting.funds (
        fund_code, name, base_currency, inception_date, status, legal_entity
    )
    values (
        'TEST-RISK-QA', 'Risk QA Contract Test Fund', 'USD',
        date '2026-01-01', 'ACTIVE',
        '{"test_only": true, "owner": "risk-qa"}'::jsonb
    )
    on conflict (fund_code) do update
        set name = excluded.name,
            base_currency = excluded.base_currency,
            status = 'ACTIVE',
            legal_entity = excluded.legal_entity,
            updated_at = now()
    returning fund_id into test_fund_id;

    if test_fund_id is null then
        select fund_id into strict test_fund_id
          from accounting.funds
         where fund_code = 'TEST-RISK-QA';
    end if;

    insert into accounting.books (fund_id, book_code, name, book_type, status)
    values (
        test_fund_id, 'TEST-RISK-QA-BOOK', 'Risk QA Contract Test Book',
        'TEST', 'ACTIVE'
    )
    on conflict (fund_id, book_code) do update
        set name = excluded.name,
            book_type = excluded.book_type,
            status = 'ACTIVE',
            updated_at = now();

    insert into risk.policies (
        fund_id, policy_code, version, scope, rules, effective_from,
        status, content_hash
    )
    values (
        test_fund_id,
        'TEST-RISK-QA-BASELINE',
        1,
        '{"test_only": true, "scope": "fund"}'::jsonb,
        '{"max_notional": "1000000", "default_action": "REJECT"}'::jsonb,
        timestamptz '2026-01-01 00:00:00+00',
        'ACTIVE',
        'test-risk-qa-baseline-v1'
    )
    on conflict (fund_id, policy_code, version) do update
        set scope = excluded.scope,
            rules = excluded.rules,
            status = 'ACTIVE',
            content_hash = excluded.content_hash;
end;
$$;

-- Verify the two parent rows that unblock risk_requests/risk_decisions and
-- audit artifact/QA decision fixtures. The transaction is intentionally left
-- open for callers using psql -1; standalone runs commit at the end.
commit;
