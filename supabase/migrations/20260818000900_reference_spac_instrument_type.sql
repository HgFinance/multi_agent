begin;

-- LS t8436 identifies acquisition-purpose companies independently from ETF
-- and ETN products.  They used to fall through the repository mapper's
-- generic STOCK branch, which made every STOCK-only backtest and promotion
-- boundary treat them as ordinary operating-company shares.
--
-- Fail closed if the flag is attached to an unexpected active KRX identity.
-- In particular, never rewrite an ETF or ETN merely because its metadata is
-- inconsistent.  Such a row requires source-data repair and explicit review.
do $spac_backfill_preflight$
begin
  if exists (
    select 1
      from reference.instruments
     where upper(market) = 'KRX'
       and upper(status) = 'ACTIVE'
       and lower(coalesce(metadata->>'is_spac', 'false')) = 'true'
       and (
         upper(asset_class) <> 'EQUITY'
         or upper(instrument_type) not in ('STOCK', 'SPAC')
       )
  ) then
    raise exception
      'active KRX is_spac identity has an unexpected asset/product type';
  end if;
end
$spac_backfill_preflight$;

update reference.instruments
   set instrument_type = 'SPAC',
       updated_at = now()
 where upper(market) = 'KRX'
   and upper(status) = 'ACTIVE'
   and upper(asset_class) = 'EQUITY'
   and upper(instrument_type) = 'STOCK'
   and lower(coalesce(metadata->>'is_spac', 'false')) = 'true';

-- This postcondition also makes a partially applied or unexpectedly filtered
-- migration fail instead of leaving SPAC rows visible through STOCK policies.
do $spac_backfill_postcondition$
begin
  if exists (
    select 1
      from reference.instruments
     where upper(market) = 'KRX'
       and upper(status) = 'ACTIVE'
       and upper(asset_class) = 'EQUITY'
       and upper(instrument_type) = 'STOCK'
       and lower(coalesce(metadata->>'is_spac', 'false')) = 'true'
  ) then
    raise exception 'active KRX SPAC rows remain classified as STOCK';
  end if;
end
$spac_backfill_postcondition$;

commit;
