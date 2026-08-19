begin;

-- The quant runtime must revalidate exact product and listing identity, but it
-- must not receive the free-form reference.instruments.metadata document.
-- A security-barrier projection lets the owning role evaluate the SPAC flag
-- while exposing only the normalized, already-eligible identity fields.
create or replace view quant.current_krx_stock_instrument_identity
with (security_barrier = true, security_invoker = false) as
select
  instrument.instrument_id,
  instrument.instrument_type,
  instrument.asset_class,
  instrument.market,
  instrument.venue,
  instrument.status,
  instrument.listed_from,
  instrument.listed_to,
  false::boolean as is_spac
from reference.instruments instrument
where upper(instrument.instrument_type) = 'STOCK'
  and upper(instrument.asset_class) = 'EQUITY'
  and upper(instrument.market) = 'KRX'
  and upper(instrument.status) = 'ACTIVE'
  and coalesce(btrim(lower(instrument.metadata->>'is_spac')), '')
      not in ('1', 't', 'true', 'yes');

-- A legacy ALTER DEFAULT PRIVILEGES rule grants new quant relations
-- SELECT/INSERT/UPDATE to svc_quant.  Strip that inherited creation-time
-- surface before adding back the one read privilege this view needs.
revoke all on quant.current_krx_stock_instrument_identity
  from public, svc_quant;
grant select on quant.current_krx_stock_instrument_identity to svc_quant;

-- Keep the original relation column-scoped.  This is intentionally explicit
-- even though the earlier grant never included metadata, so a future replay
-- of role grants cannot mistake the projection for permission to read it.
revoke select (metadata) on reference.instruments from svc_quant;

do $quant_stock_identity_projection_audit$
begin
  if not has_table_privilege(
       'svc_quant',
       'quant.current_krx_stock_instrument_identity',
       'SELECT') then
    raise exception 'svc_quant cannot read the governed stock projection';
  end if;

  if has_column_privilege(
       'svc_quant', 'reference.instruments', 'metadata', 'SELECT') then
    raise exception 'svc_quant can still read raw instrument metadata';
  end if;

  if has_table_privilege(
       'svc_quant', 'quant.current_krx_stock_instrument_identity', 'INSERT')
     or has_table_privilege(
       'svc_quant', 'quant.current_krx_stock_instrument_identity', 'UPDATE')
     or has_table_privilege(
       'svc_quant', 'quant.current_krx_stock_instrument_identity', 'DELETE')
     or has_table_privilege(
       'svc_quant', 'quant.current_krx_stock_instrument_identity', 'TRUNCATE')
  then
    raise exception 'svc_quant can mutate the governed stock projection';
  end if;
end
$quant_stock_identity_projection_audit$;

comment on view quant.current_krx_stock_instrument_identity is
  'Security-barrier projection for svc_quant. Exposes only current ACTIVE KRX EQUITY/STOCK non-SPAC identity and listing bounds; raw metadata remains private.';

comment on column quant.current_krx_stock_instrument_identity.is_spac is
  'Always false: rows with a truthy source metadata.is_spac flag are excluded inside the owner-evaluated security barrier.';

commit;
