begin;

insert into market.market_ticks (
  event_time,
  received_at,
  observed_at,
  instrument_id,
  provider,
  tr_code,
  market,
  price,
  quantity,
  side,
  source_event_id,
  schema_version
) values (
  '2026-07-29 01:00:00+00',
  '2026-07-29 01:00:00.010+00',
  '2026-07-29 01:00:00.020+00',
  '10000000-0000-0000-0000-000000000001',
  'LS',
  'S3_',
  'KRX',
  10000,
  10,
  1,
  'SMOKE-TICK-1',
  1
);

do $$
begin
  begin
    insert into market.market_ticks (
      event_time,
      received_at,
      observed_at,
      instrument_id,
      provider,
      market,
      price,
      quantity,
      source_event_id,
      schema_version
    ) values (
      '2026-07-29 01:00:00+00',
      '2026-07-29 01:00:00.010+00',
      '2026-07-29 01:00:00.020+00',
      '10000000-0000-0000-0000-000000000001',
      'LS',
      'KRX',
      10000,
      10,
      'SMOKE-TICK-1',
      1
    );
    raise exception 'expected duplicate source event rejection';
  exception
    when unique_violation then null;
  end;
end;
$$;

insert into market.market_quotes (
  event_time,
  received_at,
  observed_at,
  instrument_id,
  provider,
  tr_code,
  market,
  bid_prices,
  bid_sizes,
  ask_prices,
  ask_sizes,
  best_bid,
  best_ask,
  mid_price,
  spread,
  source_event_id,
  schema_version
) values (
  '2026-07-29 01:00:00+00',
  '2026-07-29 01:00:00.010+00',
  '2026-07-29 01:00:00.020+00',
  '10000000-0000-0000-0000-000000000001',
  'LS',
  'H1_',
  'KRX',
  array[9990, 9980]::numeric[],
  array[100, 200]::numeric[],
  array[10010, 10020]::numeric[],
  array[120, 220]::numeric[],
  9990,
  10010,
  10000,
  20,
  'SMOKE-QUOTE-1',
  1
);

do $$
begin
  begin
    update market.market_ticks
    set price = 10001
    where source_event_id = 'SMOKE-TICK-1';
    raise exception 'expected immutable raw event rejection';
  exception
    when others then
      if sqlerrm not like '% is immutable; write a correction event instead' then
        raise;
      end if;
  end;
end;
$$;

do $$
begin
  begin
    insert into market.market_quotes (
      event_time,
      received_at,
      observed_at,
      instrument_id,
      provider,
      market,
      bid_prices,
      bid_sizes,
      ask_prices,
      ask_sizes,
      source_event_id,
      schema_version
    ) values (
      '2026-07-29 01:00:01+00',
      '2026-07-29 01:00:01.010+00',
      '2026-07-29 01:00:01.020+00',
      '10000000-0000-0000-0000-000000000001',
      'LS',
      'KRX',
      array[9990, 9980]::numeric[],
      array[100]::numeric[],
      array[10010]::numeric[],
      array[120]::numeric[],
      'SMOKE-QUOTE-BAD',
      1
    );
    raise exception 'expected order book depth rejection';
  exception
    when check_violation then null;
  end;
end;
$$;

rollback;
