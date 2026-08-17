-- 미시구조 v5: 방향 없는 절대 호가 수용력(capacity).
--
-- v4의 depth_imbalance는 어느 쪽 잔량이 많은지만 [-1,1]로 보존한다. 양쪽
-- 합계가 2천만원인 얇은 장과 20억원인 두꺼운 장이 같은 +0.2를 가질 수 있어
-- OFI 충격이 깊이에 반비례한다는 문헌 메커니즘을 시험할 수 없었다.
-- 가격×잔량을 백만원 단위로 합쳐 종목 가격 차이를 보정하고, L1/L10을 따로
-- 둔다. 이는 주문 방향 신호가 아니라 유동성 공급자의 흡수 용량이다.

alter table market.microstructure_features
  add column if not exists book_depth_notional_l1  double precision,
  add column if not exists book_depth_notional_l10 double precision;

comment on column market.microstructure_features.book_depth_notional_l1 is
  '최우선 매수·매도호가 가격×잔량 합계의 일평균(백만원). L1 유동성 수용력';
comment on column market.microstructure_features.book_depth_notional_l10 is
  '1~10호가 매수·매도 가격×잔량 합계의 일평균(백만원). 전체 호가 수용력';

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'microstructure_v5_signed_flow_bounds'
       and conrelid = 'market.microstructure_features'::regclass
  ) then
    alter table market.microstructure_features
      add constraint microstructure_v5_signed_flow_bounds check (
        feature_set_version <> 'ms-daily-v5' or
        ((order_flow_imbalance is null or order_flow_imbalance between -1 and 1) and
         (ofi_open is null or ofi_open between -1 and 1) and
         (ofi_close is null or ofi_close between -1 and 1) and
         (size_weighted_ofi is null or size_weighted_ofi between -1 and 1) and
         (depth_imbalance_l1 is null or depth_imbalance_l1 between -1 and 1) and
         (depth_imbalance_l10 is null or depth_imbalance_l10 between -1 and 1) and
         (depth_imbalance_slope is null or depth_imbalance_slope between -2 and 2))
      );
  end if;
  if not exists (
    select 1 from pg_constraint
     where conname = 'microstructure_v5_depth_capacity_nonnegative'
       and conrelid = 'market.microstructure_features'::regclass
  ) then
    alter table market.microstructure_features
      add constraint microstructure_v5_depth_capacity_nonnegative check (
        feature_set_version <> 'ms-daily-v5' or
        ((book_depth_notional_l1 is null or book_depth_notional_l1 >= 0) and
         (book_depth_notional_l10 is null or book_depth_notional_l10 >= 0))
      );
  end if;
end $$;
