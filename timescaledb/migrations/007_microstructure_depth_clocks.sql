-- 미시구조 공간축·체결크기축 피처. 기존 행/버전은 건드리지 않는다.
--
-- `depth_imbalance` 는 과거 집계 경로마다 뜻이 달랐다. 내부 원천은 저장된
-- 1~10호가 합계, 외부 `quotes.bi` 는 1호가만 사용했다. 같은 컬럼으로 서로 다른
-- 경제적 상태를 학습하지 않도록 v4 부터 L1/L10을 명시하고 둘의 차이를 보존한다.
-- `size_weighted_ofi` 는 체결수량을 한 번 더 가중한다. 따라서 작은 체결 수백 건과
-- 큰 체결 한 건을 같은 체결량 OFI로 뭉개지 않고, 큰 유동성 수요가 어느 쪽인지 잰다.

alter table market.microstructure_features
  add column if not exists depth_imbalance_l1    double precision,
  add column if not exists depth_imbalance_l10   double precision,
  add column if not exists depth_imbalance_slope double precision,
  add column if not exists size_weighted_ofi     double precision;

comment on column market.microstructure_features.depth_imbalance_l1 is
  '최우선호가 잔량 불균형: (bid1-ask1)/(bid1+ask1) 의 일평균';
comment on column market.microstructure_features.depth_imbalance_l10 is
  '가용 1~10호가 합계 잔량 불균형의 일평균';
comment on column market.microstructure_features.depth_imbalance_slope is
  'depth_imbalance_l1-depth_imbalance_l10. 최우선호가와 깊은 호가의 공간 분리';
comment on column market.microstructure_features.size_weighted_ofi is
  'sum(signed_quantity*quantity)/sum(quantity^2). 큰 체결을 강조한 주문흐름 불균형';

-- 과거 v1~v3의 잘못된 외부 side 인코딩 값은 재현을 위해 그대로 두되, v4부터는
-- 정의상 범위를 DB 경계에서도 강제한다. DO 블록은 재실행 가능한 마이그레이션용이다.
do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'microstructure_v4_signed_flow_bounds'
       and conrelid = 'market.microstructure_features'::regclass
  ) then
    alter table market.microstructure_features
      add constraint microstructure_v4_signed_flow_bounds check (
        feature_set_version <> 'ms-daily-v4' or
        ((order_flow_imbalance is null or order_flow_imbalance between -1 and 1) and
         (ofi_open is null or ofi_open between -1 and 1) and
         (ofi_close is null or ofi_close between -1 and 1) and
         (size_weighted_ofi is null or size_weighted_ofi between -1 and 1))
      );
  end if;
  if not exists (
    select 1 from pg_constraint
     where conname = 'microstructure_v4_depth_bounds'
       and conrelid = 'market.microstructure_features'::regclass
  ) then
    alter table market.microstructure_features
      add constraint microstructure_v4_depth_bounds check (
        feature_set_version <> 'ms-daily-v4' or
        ((depth_imbalance_l1 is null or depth_imbalance_l1 between -1 and 1) and
         (depth_imbalance_l10 is null or depth_imbalance_l10 between -1 and 1) and
         (depth_imbalance_slope is null or depth_imbalance_slope between -2 and 2))
      );
  end if;
end $$;
