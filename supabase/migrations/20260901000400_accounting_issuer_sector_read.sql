begin;

-- The Accounting advisory read model enriches held instruments with the
-- issuer's governed industry classification.  It already has read-only access
-- to reference.instruments and instrument_symbols; grant the one missing
-- relation instead of broadening the role or duplicating sector data.
grant select on reference.issuers to svc_accounting_ledger;

drop policy if exists reference_issuers_svc_accounting_ledger_select
  on reference.issuers;
create policy reference_issuers_svc_accounting_ledger_select
  on reference.issuers for select to svc_accounting_ledger using (true);

commit;
