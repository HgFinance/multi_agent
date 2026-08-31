begin;

-- The v4 envelope already constrains PLACE_BASKET to a bounded `orders`
-- array. Trading revalidates every member from the signed canonical payload;
-- extend the documented contract from notional BUY-only to same-side explicit
-- quantity BUY/SELL legs without weakening the DB envelope.

comment on constraint user_directives_payload_v3_check
  on execution.user_directives is
  'PLACE_BASKET accepts a bounded orders array. Trading requires one uniform side and exactly one member sizing policy: KRW notional BUY or explicit BUY/SELL quantity, before broker admission.';

commit;
