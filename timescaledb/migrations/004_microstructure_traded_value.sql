-- 미시구조 일별 집계에 거래대금·체결수량을 담는다.
--
-- ▶ 왜 (2026-08-14, 재일님 지적 "거래대금 있었던거 같은데")
--   `market.market_ticks` 는 price·quantity·cumulative_value 를 다 갖고 있는데
--   (실측: 하루 체결 1,335만건 · 2,489종목 · 거래대금 41.96조) 일별 집계는
--   `sum(quantity)` 를 **계산해 놓고 출력에 안 실었고** 거래대금은 아예 만들지
--   않았다. 그래서 미시구조 표본에서는:
--     · 유동성 필터가 쓸 값이 없어 일봉 notional 에만 의존했다
--     · "거래대금 급증" 같은 신호를 표현할 칸이 없었다
--     · 매니페스트에 거래대금 단위 선언이 없다는 경고가 매 실행 떴다
--
-- ▶ 단위는 **백만원**이다 - 일봉 `notional` 과 같은 눈금.
--   원 단위로 담으면 유동성 필터가 1e6 배 어긋난 채 돌아 전 종목을
--   통과시키거나 전부 버린다(2026-08-14 실측: 유니버스 0종목).
--   `quant.dataset_manifests.notional_unit = 'KRW_MILLION'` 로 선언하고
--   실행면이 로더 경계에서 대조한다(`assert_declared_units`).
--
-- ▶ 기존 행은 건드리지 않는다. `ms-daily-v1` 로 적재된 153,937행은 그대로
--   NULL 을 갖고, 새 집계는 `ms-daily-v2` 로 들어간다 - 그래야 v1 데이터셋으로
--   돈 실험의 재현이 산다.

alter table market.microstructure_features
  add column if not exists traded_value  double precision,
  add column if not exists traded_volume double precision;

comment on column market.microstructure_features.traded_value is
  '일별 체결 거래대금. 단위 백만원(KRW_MILLION) - 일봉 notional 과 같은 눈금';
comment on column market.microstructure_features.traded_volume is
  '일별 체결 수량(주)';
